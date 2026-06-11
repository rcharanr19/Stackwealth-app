from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import logging
from pathlib import Path
import re
import time
from typing import Callable, Any

from .logging_utils import mask_account, mask_email
from .market_data import MarketDataService
from .sqlite_store import SQLiteStore


@dataclass(slots=True)
class SyncResult:
    imported_count: int
    new_tickers: list[str]


LOGGER = logging.getLogger(__name__)

SYNC_SESSION_EXPIRES_IN_SECONDS = 300


class RobinhoodSyncService:
    def __init__(self, db: SQLiteStore, market_service: MarketDataService) -> None:
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
    ) -> SyncResult:
        LOGGER.info(
            "Starting Robinhood sync for user=%s account=%s.",
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

        def mfa_provider() -> str:
            emit("Awaiting 2FA Verification...")
            code = mfa_callback().strip()
            emit("Syncing Data...")
            return code

        imported_count = 0

        try:
            profile = self.db.get_sync_profile()
            if profile is None:
                tracked = self.db.list_cache_tickers()
                self.db.initialize_sync_profile_if_missing(date.today().isoformat(), tracked)
                profile = self.db.get_sync_profile()

            tracked_tickers = {t.upper().strip() for t in (profile or {}).get("tracked_tickers", [])}

            emit("Syncing Data...")
            pickle_dir = str((Path.cwd() / "cache").resolve())
            pickle_name = "_alphavault"
            self._clear_saved_session(pickle_dir, pickle_name)
            login_password = password
            
            # Try login with app push approval first
            try:
                login_resp = r.login(
                    username=email,
                    password=login_password,
                    store_session=True,  # Allow session persistence during MFA flow
                    pickle_path=pickle_dir,
                    pickle_name=pickle_name,
                    expiresIn=SYNC_SESSION_EXPIRES_IN_SECONDS,
                )
                LOGGER.debug("Initial login response: %s (type: %s)", type(login_resp), login_resp)
            except Exception as login_exc:
                LOGGER.error("Initial login raised exception: %s", login_exc, exc_info=True)
                login_resp = None

            if not login_resp:
                # Give the app approval a moment to register, then retry with longer waits
                emit("Awaiting app approval confirmation... (this may take up to 30 seconds)")
                for retry_attempt in range(6):
                    time.sleep(5)  # Increased wait time from 2 to 5 seconds
                    self._clear_saved_session(pickle_dir, pickle_name)
                    try:
                        login_resp = r.login(
                            username=email,
                            password=login_password,
                            store_session=True,  # Allow session persistence during MFA flow
                            pickle_path=pickle_dir,
                            pickle_name=pickle_name,
                            expiresIn=SYNC_SESSION_EXPIRES_IN_SECONDS,
                        )
                        LOGGER.debug("Retry %d login response: %s (type: %s)", retry_attempt + 1, type(login_resp), login_resp)
                        if login_resp:
                            LOGGER.info("Login succeeded on retry attempt %d", retry_attempt + 1)
                            break
                    except Exception as retry_exc:
                        LOGGER.error("Retry %d login raised exception: %s", retry_attempt + 1, retry_exc, exc_info=True)
                    LOGGER.debug("Login retry %d failed, trying again...", retry_attempt + 1)
            
            # If app approval failed after retries, require explicit SMS/2FA code
            if not login_resp:
                emit("App approval not detected. SMS 2FA code is required.")
                LOGGER.warning("App push approval failed; prompting for SMS/2FA code instead.")
                mfa_code = mfa_provider()
                if not mfa_code:
                    raise RuntimeError(
                        "2FA code is required. Please enter the code sent to your phone via SMS or authenticator app."
                    )
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
                    LOGGER.debug("MFA login response: %s (type: %s)", type(login_resp), login_resp)
                except Exception as mfa_exc:
                    LOGGER.error("MFA login raised exception: %s", mfa_exc, exc_info=True)
                    login_resp = None
            # Best-effort secret lifetime reduction.
            password = None
            login_password = None

            if not login_resp:
                raise RuntimeError(
                    "Robinhood login failed. Check that your email and password are correct, "
                    "2FA is enabled on your account, and you have approved/entered the verification code. "
                    "Check app logs for detailed error information."
                )

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
