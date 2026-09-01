from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import logging
from pathlib import Path
import re
from typing import Callable, Any

from .logging_utils import mask_account, mask_email
from .market_data import MarketDataService


@dataclass(slots=True)
class SyncResult:
    imported_count: int
    new_tickers: list[str]


LOGGER = logging.getLogger(__name__)

SYNC_SESSION_EXPIRES_IN_SECONDS = 300
ROBINHOOD_SYNC_VERSION = "margin-v2"


class RobinhoodSyncService:
    def __init__(self, db: Any, market_service: MarketDataService) -> None:
        self.db = db
        self.market_service = market_service

    @staticmethod
    def _clear_saved_session(pickle_dir: str, pickle_name: str) -> None:
        session_dir = Path(pickle_dir)
        if not session_dir.exists():
            return

        candidates = [
            session_dir / pickle_name,
            session_dir / f"{pickle_name}.pickle",
            session_dir / f"{pickle_name}.pkl",
        ]
        for candidate in candidates:
            try:
                if candidate.exists():
                    candidate.unlink()
            except Exception:
                pass

        for candidate in session_dir.glob(f"{pickle_name}*"):
            try:
                if candidate.is_file():
                    candidate.unlink()
            except Exception:
                pass

    @staticmethod
    def _normalize_ticker(value: Any) -> str:
        return str(value or "").upper().strip()

    @staticmethod
    def _normalize_account_number(value: Any) -> str | None:
        account_number = str(value or "").strip()
        if not account_number or account_number.lower() == "default":
            return None
        return account_number

    @staticmethod
    def _is_login_success(login_resp: Any) -> bool:
        if not login_resp:
            return False
        if isinstance(login_resp, dict):
            return bool(login_resp.get("access_token"))
        return False

    @staticmethod
    def _extract_login_error(login_resp: Any) -> str:
        if isinstance(login_resp, dict):
            detail = str(login_resp.get("detail") or "").strip()
            challenge = str(login_resp.get("challenge") or "").strip()
            verification = str(login_resp.get("verification_workflow") or "").strip()
            if detail:
                return detail
            if challenge:
                return f"challenge={challenge}"
            if verification:
                return f"verification_workflow={verification}"
        return "unknown login response"

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _extract_margin_balance_usd(
        cls,
        account_profile: Any,
        portfolio_profile: Any,
        phoenix_account: Any,
        margin_balances: Any = None,
    ) -> float | None:
        saw_margin_data = False
        zero_candidate = False

        def choose_value(raw: Any) -> float | None:
            nonlocal saw_margin_data, zero_candidate
            value = cls._safe_float(raw)
            if value is None:
                return None
            saw_margin_data = True
            if value > 0:
                return value
            if value == 0:
                zero_candidate = True
            return None

        phoenix_rows = phoenix_account if isinstance(phoenix_account, list) else [phoenix_account]
        for row in phoenix_rows:
            if not isinstance(row, dict):
                continue
            value = choose_value(row.get("levered_amount"))
            if value is not None:
                return value

        balance_sources = []
        if isinstance(margin_balances, dict):
            balance_sources.append(margin_balances)
        if isinstance(account_profile, dict):
            account_margin_balances = account_profile.get("margin_balances")
            if isinstance(account_margin_balances, dict):
                balance_sources.append(account_margin_balances)

        for source in balance_sources:
            for key in (
                "outstanding_margin_balance",
                "margin_used",
                "margin_balance",
                "margin_debit",
                "debit_balance",
                "levered_amount",
            ):
                value = choose_value(source.get(key))
                if value is not None:
                    return value

            for key in ("cash", "uninvested_cash"):
                value = cls._safe_float(source.get(key))
                if value is not None and value < 0:
                    saw_margin_data = True
                    return abs(value)

        if balance_sources:
            saw_margin_data = True

        if isinstance(account_profile, dict):
            cash = cls._safe_float(account_profile.get("cash"))
            if cash is not None and cash < 0:
                saw_margin_data = True
                return abs(cash)

        if isinstance(portfolio_profile, dict):
            excess_margin = cls._safe_float(portfolio_profile.get("excess_margin"))
            excess_maintenance = cls._safe_float(portfolio_profile.get("excess_maintenance"))
            if excess_margin is not None and excess_maintenance is not None and excess_margin < 0:
                saw_margin_data = True
                return abs(excess_margin)

        if saw_margin_data or zero_candidate:
            return 0.0

        return None

    @staticmethod
    def _load_linked_margin_balances(robinhood_module: Any, account_profile: Any) -> Any:
        if not isinstance(account_profile, dict):
            return None
        margin_balances = account_profile.get("margin_balances")
        if isinstance(margin_balances, dict):
            return margin_balances
        if isinstance(margin_balances, str) and margin_balances.startswith("http"):
            request_get = getattr(robinhood_module, "request_get", None)
            if callable(request_get):
                return request_get(margin_balances)
        return None

    @classmethod
    def _candidate_margin_urls(cls, account_number: str | None, account_profile: Any) -> list[str]:
        account_numbers = []
        for raw in (account_number,):
            normalized = cls._normalize_account_number(raw)
            if normalized:
                account_numbers.append(normalized)
        if isinstance(account_profile, dict):
            for key in ("account_number", "rhs_account_number"):
                normalized = cls._normalize_account_number(account_profile.get(key))
                if normalized:
                    account_numbers.append(normalized)

        urls = []
        for number in dict.fromkeys(account_numbers):
            urls.extend(
                [
                    f"https://api.robinhood.com/margin/accounts/{number}/",
                    f"https://api.robinhood.com/accounts/{number}/margin_balances/",
                ]
            )
        return urls

    @staticmethod
    def _load_direct_margin_balances(robinhood_module: Any, account_number: str | None, account_profile: Any) -> Any:
        request_get = getattr(robinhood_module, "request_get", None)
        if not callable(request_get):
            return None

        for url in RobinhoodSyncService._candidate_margin_urls(account_number, account_profile):
            data = request_get(url)
            if isinstance(data, dict) and data:
                return data
        return None

    @staticmethod
    def _payload_keys(payload: Any) -> list[str]:
        if isinstance(payload, dict):
            return sorted(str(key) for key in payload.keys())
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return sorted(str(key) for key in payload[0].keys())
        return []

    def _sync_margin_balance(self, robinhood_module: Any, account_number: str | None) -> None:
        setter = getattr(self.db, "set_margin_balance_usd", None)
        if not callable(setter):
            LOGGER.warning("Robinhood margin sync skipped because the store cannot persist margin balance.")
            return

        try:
            account_number = self._normalize_account_number(account_number)
            LOGGER.info("Fetching Robinhood margin details for account=%s.", mask_account(account_number))
            
            # Attempt 1: Account profile
            account_profile = None
            try:
                account_profile = robinhood_module.profiles.load_account_profile(account_number=account_number)
                LOGGER.debug("Account profile retrieved: keys=%s", self._payload_keys(account_profile))
            except Exception as e:
                LOGGER.debug("Failed to load account profile: %s", e)
            
            # Attempt 2: Portfolio profile
            portfolio_profile = None
            try:
                portfolio_profile = robinhood_module.profiles.load_portfolio_profile(account_number=account_number)
                LOGGER.debug("Portfolio profile retrieved: keys=%s", self._payload_keys(portfolio_profile))
            except Exception as e:
                LOGGER.debug("Failed to load portfolio profile: %s", e)
            
            # Attempt 3: Phoenix account
            phoenix_account = None
            try:
                phoenix_account = robinhood_module.account.load_phoenix_account()
                LOGGER.debug("Phoenix account retrieved: keys=%s", self._payload_keys(phoenix_account))
            except Exception as e:
                LOGGER.debug("Failed to load phoenix account: %s", e)
            
            # Attempt 4: Linked margin balances
            margin_balances = None
            try:
                margin_balances = self._load_linked_margin_balances(robinhood_module, account_profile)
                LOGGER.debug("Linked margin balances retrieved: keys=%s", self._payload_keys(margin_balances))
            except Exception as e:
                LOGGER.debug("Failed to load linked margin balances: %s", e)
            
            # Attempt 5: Direct margin balances if linked failed
            if margin_balances is None:
                try:
                    margin_balances = self._load_direct_margin_balances(robinhood_module, account_number, account_profile)
                    LOGGER.debug("Direct margin balances retrieved: keys=%s", self._payload_keys(margin_balances))
                except Exception as e:
                    LOGGER.debug("Failed to load direct margin balances: %s", e)
            
            LOGGER.info(
                "Robinhood margin response keys: account=%s portfolio=%s phoenix=%s margin=%s.",
                self._payload_keys(account_profile),
                self._payload_keys(portfolio_profile),
                self._payload_keys(phoenix_account),
                self._payload_keys(margin_balances),
            )
            margin_balance = self._extract_margin_balance_usd(
                account_profile,
                portfolio_profile,
                phoenix_account,
                margin_balances=margin_balances,
            )
            if margin_balance is not None:
                setter(margin_balance)
                LOGGER.info("Updated Robinhood margin balance: %.2f", margin_balance)
            else:
                LOGGER.warning("Robinhood margin balance was unavailable in account, portfolio, and Phoenix responses.")
        except Exception:
            LOGGER.exception("Unable to update Robinhood margin balance; continuing sync.")

    def _parse_tx_date(self, raw: Any) -> str:
        value = str(raw or "").strip()
        if not value:
            return datetime.utcnow().date().isoformat()
        # Robinhood timestamps can include timezone suffixes not handled by datetime.fromisoformat.
        try:
            normalized = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            return dt.date().isoformat()
        except Exception:
            pass

        match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
        if match:
            return match.group(1)

        return datetime.utcnow().date().isoformat()

    def sync_transactions(
        self,
        email: str,
        mfa_callback: Callable[[], str],
        status_callback: Callable[[str], None] | None = None,
        password: str | None = None,
            account_number: str | None = None,
        push_only: bool = False,
    ) -> SyncResult:
        account_number = self._normalize_account_number(account_number)
        LOGGER.info(
            "Starting Robinhood sync version=%s for user=%s account=%s.",
            ROBINHOOD_SYNC_VERSION,
            mask_email(email),
            mask_account(account_number),
        )
        try:
            from robin_stocks import robinhood as r
        except Exception as exc:
            raise RuntimeError("The robin_stocks package is not available. Install dependencies first.") from exc

        if not password:
            try:
                import keyring
            except Exception as exc:
                raise RuntimeError(
                    "No password provided and keyring is not available for credential lookup."
                ) from exc

            password = keyring.get_password("StackWealthApp", email)
            if not password:
                password = keyring.get_password("AlphaVaultApp", email)

        if not password:
            raise RuntimeError(
                "No Robinhood password provided. Enter credentials in the sync prompt or save password in keyring."
            )

        def emit(status: str) -> None:
            if status_callback:
                status_callback(status)

        imported_count = 0

        try:
            profile = self.db.get_sync_profile()
            if profile is None:
                tracked = self.db.list_cache_tickers()
                self.db.initialize_sync_profile_if_missing(date.today().isoformat(), tracked)
                profile = self.db.get_sync_profile()

            tracked_tickers = {t.upper().strip() for t in (profile or {}).get("tracked_tickers", [])}

            # Validate credentials before attempting login
            if not email or not email.strip():
                raise RuntimeError("Email cannot be empty.")
            if not password or not password.strip():
                raise RuntimeError("Password cannot be empty.")
            
            LOGGER.debug("Attempting login for email=%s", mask_email(email))
            
            emit("Syncing Data...")
            pickle_dir = str((Path.cwd() / "cache").resolve())
            pickle_name = "_alphavault"
            self._clear_saved_session(pickle_dir, pickle_name)
            login_password = password
            
            login_resp = None
            mfa_code = "" if push_only else mfa_callback().strip()

            # If user provides a code, try that path first to avoid push challenge loops.
            if mfa_code:
                emit("Verifying 2FA code...")
                self._clear_saved_session(pickle_dir, pickle_name)
                try:
                    login_resp = r.login(
                        username=email,
                        password=login_password,
                        store_session=True,
                        pickle_path=pickle_dir,
                        pickle_name=pickle_name,
                        mfa_code=mfa_code,
                        expiresIn=SYNC_SESSION_EXPIRES_IN_SECONDS,
                    )
                    LOGGER.debug("MFA login response: %s", login_resp)
                except Exception as mfa_exc:
                    LOGGER.error("MFA login raised exception: %s", mfa_exc, exc_info=True)
                    login_resp = None
            else:
                # Only one app-push attempt; repeated logins trigger fresh pushes and invalidate prior approvals.
                emit("Check Robinhood app and approve the login request...")
                self._clear_saved_session(pickle_dir, pickle_name)
                try:
                    login_resp = r.login(
                        username=email,
                        password=login_password,
                        store_session=True,
                        pickle_path=pickle_dir,
                        pickle_name=pickle_name,
                        expiresIn=SYNC_SESSION_EXPIRES_IN_SECONDS,
                    )
                    LOGGER.debug("Initial login response: %s", login_resp)
                except Exception as login_exc:
                    LOGGER.error("Initial login raised exception: %s", login_exc, exc_info=True)
                    login_resp = None

                if not self._is_login_success(login_resp):
                    LOGGER.warning(
                        "Push verification did not produce access token: %s",
                        self._extract_login_error(login_resp),
                    )

            # Best-effort secret lifetime reduction.
            password = None
            login_password = None

            if not self._is_login_success(login_resp):
                if push_only:
                    raise RuntimeError(
                        "Robinhood login did not return an access token after push approval. "
                        "Push-only mode is enabled, so SMS/authenticator fallback is disabled. "
                        "Retry sync and approve the most recent push immediately."
                    )
                raise RuntimeError(
                    "Robinhood login did not return an access token after verification. "
                    "This is a known robin_stocks challenge-flow issue for app push approvals. "
                    "Enter an SMS/authenticator 2FA code in the sync dialog and retry."
                )

            LOGGER.info("About to fetch Robinhood margin details before order sync.")
            self._sync_margin_balance(r, account_number)
            LOGGER.info("Finished Robinhood margin details step.")

            profile = self.db.get_sync_profile()
            if profile is None:
                profile = self.db.bootstrap_sync_profile_from_portfolio_json(Path("data/portfolio.json"))

            baseline_tickers = {self._normalize_ticker(t) for t in profile.get("baseline_assets", []) if str(t).strip()}
            tracked_tickers = {self._normalize_ticker(t) for t in profile.get("tracked_tickers", []) if str(t).strip()}
            is_initialized = bool(profile.get("initialized", False))
            if not baseline_tickers:
                baseline_tickers = set(tracked_tickers)
            if not tracked_tickers:
                tracked_tickers = set(baseline_tickers)

            if not baseline_tickers:
                raise RuntimeError("portfolio.json must define at least one baseline asset before Robinhood sync.")

            baseline_cutoff = date.fromisoformat(str(profile.get("baseline_date") or date.today().isoformat()))

            incremental_start = self.db.get_incremental_start_date(lookback_days=7)
            baseline_start = str((profile or {}).get("baseline_date") or "")
            start_date = incremental_start or baseline_start or date.today().isoformat()
            allow_new_symbols = is_initialized
            emit(f"Syncing Data... (from {start_date})")
            LOGGER.info(
                "Fetching Robinhood orders from %s (allow_new_symbols=%s).",
                start_date,
                allow_new_symbols,
            )
            orders = r.orders.get_all_stock_orders(
                account_number=account_number,
                start_date=start_date,
            ) or []
            LOGGER.debug("Fetched %d orders from Robinhood.", len(orders))
            touched_tickers: set[str] = set()

            for order in orders:
                order_id = str(order.get("id") or "")
                symbol = self._normalize_ticker(order.get("symbol"))
                if not symbol and order.get("instrument"):
                    symbol = self._normalize_ticker(r.stocks.get_symbol_by_url(order["instrument"]))

                if not symbol:
                    continue
                if not allow_new_symbols and symbol not in baseline_tickers:
                    continue
                if allow_new_symbols and tracked_tickers and symbol not in tracked_tickers:
                    # Later runs can absorb newly observed assets, but only after the initial baseline sync.
                    pass
                elif tracked_tickers and symbol not in tracked_tickers and not allow_new_symbols:
                    continue

                side = str(order.get("side") or "buy").lower().strip()
                currency = str(order.get("currency_code") or "USD").upper().strip()
                executions = order.get("executions") or []

                for execution in executions:
                    execution_id = str(execution.get("id") or "").strip()
                    if not execution_id:
                        continue

                    shares = float(execution.get("quantity") or 0.0)
                    price = float(execution.get("price") or 0.0)
                    if shares <= 0 or price <= 0:
                        continue

                    tx_date = self._parse_tx_date(
                        execution.get("timestamp")
                        or order.get("last_transaction_at")
                        or order.get("updated_at")
                    )

                    if allow_new_symbols and symbol not in tracked_tickers and date.fromisoformat(tx_date) <= baseline_cutoff:
                        continue

                    gross = shares * price
                    amount = -gross if side == "buy" else gross

                    inserted = self.db.insert_transaction_if_new(
                        execution_id=execution_id,
                        order_id=order_id,
                        ticker=symbol,
                        tx_date=tx_date,
                        side=side,
                        shares=shares,
                        price=price,
                        amount=amount,
                        currency=currency,
                    )
                    if inserted:
                        imported_count += 1
                        touched_tickers.add(symbol)

            if allow_new_symbols and touched_tickers:
                self.db.add_tracked_tickers(touched_tickers)
                tracked_tickers |= touched_tickers
                LOGGER.info("Expanded tracked ticker set by %d symbol(s).", len(touched_tickers))

            existing_cache = self.db.list_cache_tickers()
            for ticker in sorted(touched_tickers & existing_cache):
                self.db.refresh_existing_position_core(ticker)

            new_tickers = []
            if allow_new_symbols:
                new_tickers = self.db.list_unprovisioned_tickers_since(baseline_cutoff)
            for ticker in new_tickers:
                shares, avg_price, tx_currency = self.db.derive_position_from_transactions(ticker)
                try:
                    profile = self.market_service.fetch_asset_profile(ticker)
                except Exception:
                    profile = {
                        "price": None,
                        "market_cap": None,
                        "company_name": None,
                        "currency": None,
                    }

                resolved_currency = profile.get("currency") or tx_currency or "USD"
                self.db.upsert_portfolio_cache(
                    ticker=ticker,
                    company_name=profile.get("company_name") or ticker,
                    shares=shares,
                    avg_price=avg_price,
                    currency=resolved_currency,
                    last_price=profile.get("price"),
                    market_cap=profile.get("market_cap"),
                )

            if not is_initialized:
                self.db.set_tracked_tickers(baseline_tickers)
                self.db.mark_sync_initialized()
            else:
                self.db.touch_last_sync()

            LOGGER.info(
                "Robinhood sync finished. imported=%d new_tickers=%d.",
                imported_count,
                len(new_tickers),
            )

            return SyncResult(imported_count=imported_count, new_tickers=new_tickers)
        except Exception as exc:
            LOGGER.exception("Robinhood sync failed.")
            raise RuntimeError(f"Robinhood sync failed: {exc}") from exc
        finally:
            try:
                r.logout()
                LOGGER.debug("Robinhood logout complete.")
            except Exception:
                pass
