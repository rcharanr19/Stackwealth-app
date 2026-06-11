from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .models import Position, Transaction, parse_position, parse_transaction


LOGGER = logging.getLogger(__name__)
EPSILON = 0.0001


class PostgresStore:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @staticmethod
    def _normalize_tickers(tickers: list[str] | set[str]) -> list[str]:
        return sorted({str(t).upper().strip() for t in tickers if str(t).strip()})

    def _query_df(self, sql: str, *, params: dict[str, Any] | None = None, ttl: int = 0) -> pd.DataFrame:
        return self.connection.query(sql, ttl=ttl, params=params or {})

    def _fetch_one(self, sql: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        df = self._query_df(sql, params=params, ttl=0)
        if df.empty:
            return None
        return df.iloc[0].to_dict()

    def _execute(self, sql: str, *, params: dict[str, Any] | None = None, action: str) -> None:
        try:
            with self.connection.session as session:
                session.execute(sql, params or {})
                session.commit()
        except Exception as exc:
            LOGGER.exception("%s failed: %s", action, exc)
            raise RuntimeError(f"{action} failed") from exc

    def ensure_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS public.portfolio_cache (
                ticker VARCHAR(32) PRIMARY KEY,
                company_name VARCHAR(255) NOT NULL,
                shares NUMERIC(12, 4) NOT NULL DEFAULT 0,
                avg_price NUMERIC(12, 4) NOT NULL DEFAULT 0,
                currency CHAR(3) NOT NULL DEFAULT 'USD',
                last_price NUMERIC(14, 4),
                market_cap NUMERIC(18, 2),
                unrealized_pnl_usd NUMERIC(14, 2),
                realized_pnl_usd NUMERIC(14, 2),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.transactions (
                execution_id VARCHAR(128) PRIMARY KEY,
                order_id VARCHAR(128),
                ticker VARCHAR(32) NOT NULL,
                tx_date DATE NOT NULL,
                side VARCHAR(8) NOT NULL,
                shares NUMERIC(12, 4) NOT NULL DEFAULT 0,
                price NUMERIC(12, 4) NOT NULL DEFAULT 0,
                amount NUMERIC(14, 2) NOT NULL,
                currency CHAR(3) NOT NULL DEFAULT 'USD',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.sync_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                baseline_date DATE NOT NULL,
                baseline_value_usd NUMERIC(14, 2),
                baseline_assets TEXT NOT NULL DEFAULT '[]',
                initialized BOOLEAN NOT NULL DEFAULT FALSE,
                initialized_at TIMESTAMPTZ,
                last_sync_at TIMESTAMPTZ,
                sync_version INTEGER NOT NULL DEFAULT 1,
                initial_sync_completed BOOLEAN NOT NULL DEFAULT FALSE,
                tracked_tickers TEXT NOT NULL DEFAULT '[]',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            "ALTER TABLE public.portfolio_cache ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE public.sync_profile ENABLE ROW LEVEL SECURITY",
        ]

        try:
            with self.connection.session as session:
                for statement in statements:
                    session.execute(statement)
                session.commit()
        except Exception as exc:
            LOGGER.exception("PostgreSQL schema initialization failed: %s", exc)
            raise RuntimeError("Unable to initialize PostgreSQL schema") from exc

    def load_baseline_assets_from_portfolio_json(self, json_path: Path) -> list[str]:
        if not json_path.exists():
            raise FileNotFoundError(f"Baseline file not found: {json_path}")

        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Invalid portfolio JSON in {json_path}") from exc

        if not isinstance(payload, dict):
            raise ValueError("portfolio.json must contain a JSON object.")

        raw_positions = payload.get("positions", [])
        if not isinstance(raw_positions, list):
            raise ValueError("portfolio.json field 'positions' must be a list.")

        baseline_assets: list[str] = []
        for item in raw_positions:
            if not isinstance(item, dict):
                continue
            ticker = item.get("ticker")
            if ticker is None:
                continue
            baseline_assets.append(str(ticker))

        normalized = self._normalize_tickers(baseline_assets)
        if not normalized:
            raise ValueError("portfolio.json must include at least one position ticker for first-run baseline.")
        return normalized

    def load_baseline_date_from_portfolio_json(self, json_path: Path) -> str:
        if not json_path.exists():
            raise FileNotFoundError(f"Baseline file not found: {json_path}")

        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Invalid portfolio JSON in {json_path}") from exc

        if not isinstance(payload, dict):
            raise ValueError("portfolio.json must contain a JSON object.")

        raw_transactions = payload.get("transactions", [])
        if not isinstance(raw_transactions, list) or not raw_transactions:
            return date.today().isoformat()

        parsed_dates: list[date] = []
        for item in raw_transactions:
            if not isinstance(item, dict):
                continue
            raw_date = item.get("date")
            if not raw_date:
                continue
            try:
                parsed_dates.append(date.fromisoformat(str(raw_date)))
            except ValueError:
                continue

        if not parsed_dates:
            return date.today().isoformat()

        return min(parsed_dates).isoformat()

    def seed_from_json(self, json_path: Path) -> None:
        if not json_path.exists():
            return

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        positions = [parse_position(item) for item in payload.get("positions", [])]
        transactions = [parse_transaction(item) for item in payload.get("transactions", [])]

        try:
            position_count = self._query_df("SELECT COUNT(*) AS c FROM public.portfolio_cache", ttl=0).iloc[0]["c"]
            if int(position_count or 0) == 0:
                now = datetime.utcnow().isoformat()
                with self.connection.session as session:
                    for position in positions:
                        session.execute(
                            """
                            INSERT INTO public.portfolio_cache
                            (ticker, company_name, shares, avg_price, currency, last_price, market_cap, unrealized_pnl_usd, realized_pnl_usd, updated_at)
                            VALUES (:ticker, :company_name, :shares, :avg_price, :currency, NULL, NULL, NULL, NULL, :updated_at)
                            ON CONFLICT (ticker) DO NOTHING
                            """,
                            {
                                "ticker": position.ticker,
                                "company_name": position.company_name,
                                "shares": float(position.shares),
                                "avg_price": float(position.avg_price),
                                "currency": position.currency,
                                "updated_at": now,
                            },
                        )
                    session.commit()

            transaction_count = self._query_df("SELECT COUNT(*) AS c FROM public.transactions", ttl=0).iloc[0]["c"]
            if int(transaction_count or 0) == 0:
                rows: list[dict[str, Any]] = []
                for idx, tx in enumerate(transactions):
                    rows.append(
                        {
                            "execution_id": f"seed-{tx.ticker}-{tx.tx_date.isoformat()}-{idx}",
                            "order_id": "seed",
                            "ticker": tx.ticker,
                            "tx_date": tx.tx_date.isoformat(),
                            "side": "buy" if tx.amount < 0 else "sell",
                            "shares": 0.0,
                            "price": 0.0,
                            "amount": float(tx.amount),
                            "currency": "USD",
                        }
                    )
                with self.connection.session as session:
                    for row in rows:
                        session.execute(
                            """
                            INSERT INTO public.transactions
                            (execution_id, order_id, ticker, tx_date, side, shares, price, amount, currency)
                            VALUES (:execution_id, :order_id, :ticker, :tx_date, :side, :shares, :price, :amount, :currency)
                            ON CONFLICT (execution_id) DO NOTHING
                            """,
                            row,
                        )
                    session.commit()
        except Exception as exc:
            LOGGER.exception("Seeding PostgreSQL from portfolio JSON failed: %s", exc)
            raise RuntimeError("Unable to seed PostgreSQL from portfolio JSON") from exc

    def load_portfolio_state(self) -> tuple[list[Position], list[Transaction]]:
        pos_df = self._query_df(
            """
            SELECT ticker, company_name, shares, avg_price, currency
            FROM public.portfolio_cache
            ORDER BY ticker
            """,
            ttl=0,
        )
        tx_df = self._query_df(
            """
            SELECT execution_id, order_id, ticker, tx_date, side, shares, price, amount, currency, created_at
            FROM public.transactions
            ORDER BY tx_date, created_at, execution_id
            """,
            ttl=0,
        )

        positions = [
            Position(
                ticker=str(row["ticker"]),
                company_name=str(row["company_name"]),
                shares=float(row["shares"]),
                avg_price=float(row["avg_price"]),
                currency=str(row["currency"]),
            )
            for _, row in pos_df.iterrows()
        ]
        transactions = [
            parse_transaction(
                {
                    "execution_id": row["execution_id"],
                    "order_id": row["order_id"],
                    "ticker": row["ticker"],
                    "date": row["tx_date"],
                    "side": row["side"],
                    "shares": row["shares"],
                    "price": row["price"],
                    "amount": row["amount"],
                    "currency": row["currency"],
                    "created_at": row["created_at"],
                }
            )
            for _, row in tx_df.iterrows()
        ]
        return positions, transactions

    def insert_transaction_if_new(
        self,
        execution_id: str,
        order_id: str,
        ticker: str,
        tx_date: str,
        side: str,
        shares: float,
        price: float,
        amount: float,
        currency: str,
    ) -> bool:
        try:
            with self.connection.session as session:
                result = session.execute(
                    """
                    INSERT INTO public.transactions
                    (execution_id, order_id, ticker, tx_date, side, shares, price, amount, currency)
                    VALUES (:execution_id, :order_id, :ticker, :tx_date, :side, :shares, :price, :amount, :currency)
                    ON CONFLICT (execution_id) DO NOTHING
                    """,
                    {
                        "execution_id": execution_id,
                        "order_id": order_id,
                        "ticker": ticker,
                        "tx_date": tx_date,
                        "side": side,
                        "shares": float(shares),
                        "price": float(price),
                        "amount": float(amount),
                        "currency": currency,
                    },
                )
                session.commit()
            return getattr(result, "rowcount", 0) > 0
        except Exception as exc:
            LOGGER.exception("Insert transaction failed: %s", exc)
            raise RuntimeError("Unable to insert transaction") from exc

    def get_incremental_start_date(self, lookback_days: int = 7) -> str | None:
        df = self._query_df(
            """
            SELECT MAX(tx_date) AS max_tx_date
            FROM public.transactions
            WHERE execution_id NOT LIKE 'seed-%'
            """,
            ttl=0,
        )
        if df.empty:
            return None

        raw = df.iloc[0]["max_tx_date"]
        if not raw:
            return None

        try:
            max_day = date.fromisoformat(str(raw))
        except ValueError:
            return None

        safe_lookback = max(0, int(lookback_days))
        return (max_day - timedelta(days=safe_lookback)).isoformat()

    def initialize_sync_profile_if_missing(
        self,
        baseline_date: str,
        tracked_tickers: set[str],
        baseline_assets: set[str] | None = None,
        initialized: bool = False,
    ) -> None:
        now = datetime.utcnow().isoformat()
        normalized_tracked = self._normalize_tickers(tracked_tickers)
        normalized_baseline = self._normalize_tickers(
            baseline_assets if baseline_assets is not None else set(normalized_tracked)
        )
        tracked_payload = json.dumps(normalized_tracked, separators=(",", ":"))
        baseline_payload = json.dumps(normalized_baseline, separators=(",", ":"))
        initialized_flag = bool(initialized)
        try:
            with self.connection.session as session:
                session.execute(
                    """
                    INSERT INTO public.sync_profile
                    (id, baseline_date, baseline_value_usd, baseline_assets, initialized, initialized_at, last_sync_at,
                     sync_version, initial_sync_completed, tracked_tickers, updated_at)
                    VALUES (1, :baseline_date, NULL, :baseline_assets, :initialized, NULL, NULL, 1, :initial_sync_completed,
                            :tracked_tickers, :updated_at)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    {
                        "baseline_date": baseline_date,
                        "baseline_assets": baseline_payload,
                        "initialized": initialized_flag,
                        "initial_sync_completed": initialized_flag,
                        "tracked_tickers": tracked_payload,
                        "updated_at": now,
                    },
                )
                session.commit()
        except Exception as exc:
            LOGGER.exception("Initialize sync profile failed: %s", exc)
            raise RuntimeError("Unable to initialize sync profile") from exc

    def bootstrap_sync_profile_from_portfolio_json(self, json_path: Path) -> dict[str, Any]:
        baseline_assets = self.load_baseline_assets_from_portfolio_json(json_path)
        baseline_date = self.load_baseline_date_from_portfolio_json(json_path)
        baseline_set = set(baseline_assets)
        self.initialize_sync_profile_if_missing(
            baseline_date=baseline_date,
            tracked_tickers=baseline_set,
            baseline_assets=baseline_set,
            initialized=False,
        )

        profile = self.get_sync_profile()
        if profile is None:
            raise RuntimeError("Failed to initialize sync profile.")
        return profile

    def get_sync_profile(self) -> dict[str, Any] | None:
        df = self._query_df(
            """
            SELECT baseline_date, baseline_value_usd, baseline_assets, initialized, initialized_at, last_sync_at,
                   sync_version, initial_sync_completed, tracked_tickers
            FROM public.sync_profile
            WHERE id = 1
            """,
            ttl=0,
        )
        if df.empty:
            return None

        row = df.iloc[0].to_dict()

        try:
            parsed = json.loads(str(row.get("tracked_tickers")))
            tracked = self._normalize_tickers(parsed) if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            tracked = []

        try:
            parsed_baseline = json.loads(str(row.get("baseline_assets")))
            baseline_assets = self._normalize_tickers(parsed_baseline) if isinstance(parsed_baseline, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            baseline_assets = []

        initialized = bool(row.get("initialized", False)) or bool(row.get("initial_sync_completed", False))
        sync_version = int(row.get("sync_version") or 1)

        return {
            "baseline_date": str(row.get("baseline_date") or date.today().isoformat()),
            "baseline_value_usd": float(row["baseline_value_usd"]) if row.get("baseline_value_usd") is not None else None,
            "baseline_assets": baseline_assets,
            "initialized": initialized,
            "initialized_at": str(row["initialized_at"]) if row.get("initialized_at") else None,
            "last_sync_at": str(row["last_sync_at"]) if row.get("last_sync_at") else None,
            "sync_version": sync_version,
            "initial_sync_completed": initialized,
            "tracked_tickers": tracked,
        }

    def set_baseline_value_usd(self, value: float) -> None:
        self._execute(
            """
            UPDATE public.sync_profile
            SET baseline_value_usd = :value, updated_at = :updated_at
            WHERE id = 1
            """,
            params={"value": float(value), "updated_at": datetime.utcnow().isoformat()},
            action="Set baseline value",
        )

    def add_tracked_tickers(self, tickers: set[str]) -> None:
        normalized = set(self._normalize_tickers(tickers))
        if not normalized:
            return

        profile = self.get_sync_profile()
        if profile is None:
            self.initialize_sync_profile_if_missing(date.today().isoformat(), normalized)
            return

        existing = {str(t).upper().strip() for t in profile.get("tracked_tickers", []) if str(t).strip()}
        merged = sorted(existing | normalized)
        self._execute(
            """
            UPDATE public.sync_profile
            SET tracked_tickers = :tracked_tickers, updated_at = :updated_at
            WHERE id = 1
            """,
            params={
                "tracked_tickers": json.dumps(merged, separators=(",", ":")),
                "updated_at": datetime.utcnow().isoformat(),
            },
            action="Add tracked tickers",
        )

    def set_tracked_tickers(self, tickers: set[str]) -> None:
        normalized = self._normalize_tickers(tickers)
        self._execute(
            """
            UPDATE public.sync_profile
            SET tracked_tickers = :tracked_tickers, updated_at = :updated_at
            WHERE id = 1
            """,
            params={
                "tracked_tickers": json.dumps(normalized, separators=(",", ":")),
                "updated_at": datetime.utcnow().isoformat(),
            },
            action="Set tracked tickers",
        )

    def mark_sync_initialized(self) -> None:
        now = datetime.utcnow().isoformat()
        self._execute(
            """
            UPDATE public.sync_profile
            SET
                initialized = TRUE,
                initial_sync_completed = TRUE,
                initialized_at = COALESCE(initialized_at, :now),
                last_sync_at = :now,
                updated_at = :now
            WHERE id = 1
            """,
            params={"now": now},
            action="Mark sync initialized",
        )

    def touch_last_sync(self) -> None:
        now = datetime.utcnow().isoformat()
        self._execute(
            """
            UPDATE public.sync_profile
            SET last_sync_at = :now, updated_at = :now
            WHERE id = 1
            """,
            params={"now": now},
            action="Touch last sync",
        )

    def mark_initial_sync_completed(self) -> None:
        self.mark_sync_initialized()

    def list_cache_tickers(self) -> set[str]:
        df = self._query_df("SELECT ticker FROM public.portfolio_cache", ttl=0)
        return {str(row["ticker"]) for _, row in df.iterrows()}

    def get_portfolio_position(self, ticker: str) -> Position | None:
        row = self._fetch_one(
            """
            SELECT ticker, company_name, shares, avg_price, currency
            FROM public.portfolio_cache
            WHERE ticker = :ticker
            """,
            params={"ticker": ticker},
        )
        if row is None:
            return None

        return Position(
            ticker=str(row["ticker"]),
            company_name=str(row["company_name"]),
            shares=float(row["shares"]),
            avg_price=float(row["avg_price"]),
            currency=str(row["currency"]),
        )

    def list_unprovisioned_tickers(self) -> list[str]:
        df = self._query_df(
            """
            SELECT DISTINCT t.ticker
            FROM public.transactions t
            LEFT JOIN public.portfolio_cache p ON p.ticker = t.ticker
            WHERE p.ticker IS NULL
            ORDER BY t.ticker
            """,
            ttl=0,
        )
        return [str(row["ticker"]) for _, row in df.iterrows()]

    def list_unprovisioned_tickers_since(self, cutoff_date: date) -> list[str]:
        df = self._query_df(
            """
            SELECT DISTINCT t.ticker
            FROM public.transactions t
            LEFT JOIN public.portfolio_cache p ON p.ticker = t.ticker
            WHERE p.ticker IS NULL AND t.tx_date > :cutoff_date
            ORDER BY t.ticker
            """,
            params={"cutoff_date": cutoff_date},
            ttl=0,
        )
        return [str(row["ticker"]) for _, row in df.iterrows()]

    def derive_position_from_transactions(self, ticker: str) -> tuple[float, float, str]:
        df = self._query_df(
            """
            SELECT side, shares, price, currency
            FROM public.transactions
            WHERE ticker = :ticker
            ORDER BY tx_date, created_at, execution_id
            """,
            params={"ticker": ticker},
            ttl=0,
        )

        shares = 0.0
        avg_price = 0.0
        currency = "USD"

        for _, row in df.iterrows():
            side = str(row["side"]).lower()
            qty = float(row["shares"])
            px = float(row["price"])
            currency = str(row["currency"] or currency).upper()

            if qty <= 0 or px <= 0:
                continue

            if side == "buy":
                new_total = shares + qty
                if new_total > 0:
                    avg_price = ((shares * avg_price) + (qty * px)) / new_total
                shares = new_total
            elif side == "sell":
                shares = max(shares - qty, 0.0)
                if shares < EPSILON:
                    shares = 0.0
                    avg_price = 0.0

        if shares < EPSILON:
            shares = 0.0
            avg_price = 0.0

        return shares, avg_price, currency

    def upsert_portfolio_cache(
        self,
        ticker: str,
        company_name: str,
        shares: float,
        avg_price: float,
        currency: str,
        last_price: float | None,
        market_cap: float | None,
        unrealized_pnl_usd: float | None = None,
        realized_pnl_usd: float | None = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO public.portfolio_cache
            (ticker, company_name, shares, avg_price, currency, last_price, market_cap, unrealized_pnl_usd,
             realized_pnl_usd, updated_at)
            VALUES (:ticker, :company_name, :shares, :avg_price, :currency, :last_price, :market_cap,
                    :unrealized_pnl_usd, :realized_pnl_usd, :updated_at)
            ON CONFLICT (ticker) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                shares = EXCLUDED.shares,
                avg_price = EXCLUDED.avg_price,
                currency = EXCLUDED.currency,
                last_price = EXCLUDED.last_price,
                market_cap = EXCLUDED.market_cap,
                unrealized_pnl_usd = EXCLUDED.unrealized_pnl_usd,
                realized_pnl_usd = EXCLUDED.realized_pnl_usd,
                updated_at = EXCLUDED.updated_at
            """,
            params={
                "ticker": ticker,
                "company_name": company_name,
                "shares": float(shares),
                "avg_price": float(avg_price),
                "currency": currency,
                "last_price": None if last_price is None else float(last_price),
                "market_cap": None if market_cap is None else float(market_cap),
                "unrealized_pnl_usd": unrealized_pnl_usd,
                "realized_pnl_usd": realized_pnl_usd,
                "updated_at": datetime.utcnow().isoformat(),
            },
            action=f"Upsert portfolio cache for {ticker}",
        )

    def refresh_existing_position_core(self, ticker: str) -> None:
        shares, avg_price, currency = self.derive_position_from_transactions(ticker)
        existing = self._fetch_one(
            """
            SELECT company_name, last_price, market_cap, unrealized_pnl_usd, realized_pnl_usd
            FROM public.portfolio_cache
            WHERE ticker = :ticker
            """,
            params={"ticker": ticker},
        )
        if existing is None:
            return

        self.upsert_portfolio_cache(
            ticker=ticker,
            company_name=str(existing["company_name"]),
            shares=shares,
            avg_price=avg_price,
            currency=currency,
            last_price=existing.get("last_price"),
            market_cap=existing.get("market_cap"),
            unrealized_pnl_usd=existing.get("unrealized_pnl_usd"),
            realized_pnl_usd=existing.get("realized_pnl_usd"),
        )

    def update_market_snapshot(self, rows: list[dict[str, Any]]) -> None:
        try:
            with self.connection.session as session:
                for row in rows:
                    session.execute(
                        """
                        UPDATE public.portfolio_cache
                        SET
                            last_price = :last_price,
                            market_cap = :market_cap,
                            unrealized_pnl_usd = :unrealized_pnl_usd,
                            realized_pnl_usd = :realized_pnl_usd,
                            updated_at = :updated_at
                        WHERE ticker = :ticker
                        """,
                        {
                            "last_price": row.get("current_price"),
                            "market_cap": row.get("market_cap"),
                            "unrealized_pnl_usd": row.get("unrealized_pnl_usd"),
                            "realized_pnl_usd": row.get("realized_pnl_usd"),
                            "updated_at": datetime.utcnow().isoformat(),
                            "ticker": row.get("ticker"),
                        },
                    )
                session.commit()
        except Exception as exc:
            LOGGER.exception("Update market snapshot failed: %s", exc)
            raise RuntimeError("Unable to update market snapshot") from exc

    def override_portfolio_position(
        self,
        ticker: str,
        company_name: str,
        shares: float,
        avg_price: float,
        currency: str,
    ) -> None:
        existing = self._fetch_one(
            """
            SELECT last_price, market_cap, unrealized_pnl_usd, realized_pnl_usd
            FROM public.portfolio_cache
            WHERE ticker = :ticker
            """,
            params={"ticker": ticker},
        )
        last_price = existing["last_price"] if existing else None
        market_cap = existing["market_cap"] if existing else None
        unrealized_pnl = existing["unrealized_pnl_usd"] if existing else None
        realized_pnl = existing["realized_pnl_usd"] if existing else None

        self.upsert_portfolio_cache(
            ticker=ticker,
            company_name=company_name,
            shares=shares,
            avg_price=avg_price,
            currency=currency,
            last_price=last_price,
            market_cap=market_cap,
            unrealized_pnl_usd=unrealized_pnl,
            realized_pnl_usd=realized_pnl,
        )

    def delete_portfolio_position(self, ticker: str, delete_transactions: bool = True) -> None:
        try:
            with self.connection.session as session:
                session.execute("DELETE FROM public.portfolio_cache WHERE ticker = :ticker", {"ticker": ticker})
                if delete_transactions:
                    session.execute("DELETE FROM public.transactions WHERE ticker = :ticker", {"ticker": ticker})
                session.commit()
        except Exception as exc:
            LOGGER.exception("Delete portfolio position failed: %s", exc)
            raise RuntimeError("Unable to delete portfolio position") from exc