from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Position, Transaction, parse_position, parse_transaction


EPSILON = 0.0001


LOGGER = logging.getLogger(__name__)


class SQLiteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.debug("Initializing SQLite store at %s", self.db_path)
        self._initialize()

    def _ensure_sync_profile_schema(self, conn: sqlite3.Connection) -> None:
        # Keep existing databases compatible with newer sync_profile fields.
        migration_statements = [
            "ALTER TABLE sync_profile ADD COLUMN initial_sync_completed INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sync_profile ADD COLUMN baseline_assets TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE sync_profile ADD COLUMN initialized INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE sync_profile ADD COLUMN initialized_at TEXT",
            "ALTER TABLE sync_profile ADD COLUMN last_sync_at TEXT",
            "ALTER TABLE sync_profile ADD COLUMN sync_version INTEGER NOT NULL DEFAULT 1",
        ]
        for statement in migration_statements:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _prepare_sync_profile(self) -> set[str]:
        with self._connect() as conn:
            self._ensure_sync_profile_schema(conn)
            rows = conn.execute("PRAGMA table_info(sync_profile)").fetchall()
        return {str(row["name"]) for row in rows}

    def _table_columns(self, table_name: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _initialize(self) -> None:
        LOGGER.debug("Ensuring SQLite schema exists at %s", self.db_path)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    execution_id TEXT PRIMARY KEY,
                    order_id TEXT,
                    ticker TEXT NOT NULL,
                    tx_date TEXT NOT NULL,
                    side TEXT NOT NULL,
                    shares REAL NOT NULL,
                    price REAL NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_cache (
                    ticker TEXT PRIMARY KEY,
                    company_name TEXT NOT NULL,
                    shares REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    currency TEXT NOT NULL,
                    last_price REAL,
                    market_cap REAL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_profile (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    baseline_date TEXT NOT NULL,
                    baseline_value_usd REAL,
                    baseline_assets TEXT NOT NULL DEFAULT '[]',
                    initialized INTEGER NOT NULL DEFAULT 0,
                    initialized_at TEXT,
                    last_sync_at TEXT,
                    sync_version INTEGER NOT NULL DEFAULT 1,
                    initial_sync_completed INTEGER NOT NULL DEFAULT 0,
                    tracked_tickers TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_sync_profile_schema(conn)

    @staticmethod
    def _normalize_tickers(tickers: list[str] | set[str]) -> list[str]:
        return sorted({str(t).upper().strip() for t in tickers if str(t).strip()})

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
            LOGGER.debug("Skipping seed_from_json because %s does not exist", json_path)
            return

        payload = json.loads(json_path.read_text(encoding="utf-8"))
        positions = [parse_position(item) for item in payload.get("positions", [])]
        transactions = [parse_transaction(item) for item in payload.get("transactions", [])]
        LOGGER.debug(
            "Seeding SQLite store from %s with %d positions and %d transactions",
            json_path,
            len(positions),
            len(transactions),
        )

        with self._connect() as conn:
            existing_positions = conn.execute("SELECT COUNT(*) AS c FROM portfolio_cache").fetchone()["c"]
            if existing_positions == 0:
                now = datetime.utcnow().isoformat()
                conn.executemany(
                    """
                    INSERT INTO portfolio_cache (ticker, company_name, shares, avg_price, currency, last_price, market_cap, updated_at)
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    [
                        (
                            p.ticker,
                            p.company_name,
                            float(p.shares),
                            float(p.avg_price),
                            p.currency,
                            now,
                        )
                        for p in positions
                    ],
                )
                LOGGER.info("Seeded %d portfolio positions into SQLite cache", len(positions))

            existing_transactions = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
            if existing_transactions == 0:
                rows: list[tuple[Any, ...]] = []
                for idx, tx in enumerate(transactions):
                    synthetic_id = f"seed-{tx.ticker}-{tx.tx_date.isoformat()}-{idx}"
                    rows.append(
                        (
                            synthetic_id,
                            "seed",
                            tx.ticker,
                            tx.tx_date.isoformat(),
                            "buy" if tx.amount < 0 else "sell",
                            0.0,
                            0.0,
                            float(tx.amount),
                            "USD",
                        )
                    )
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO transactions
                    (execution_id, order_id, ticker, tx_date, side, shares, price, amount, currency)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                LOGGER.info("Seeded %d transactions into SQLite store", len(rows))

    def load_portfolio_state(self) -> tuple[list[Position], list[Transaction]]:
        with self._connect() as conn:
            pos_rows = conn.execute(
                """
                SELECT ticker, company_name, shares, avg_price, currency
                FROM portfolio_cache
                ORDER BY ticker
                """
            ).fetchall()
            tx_rows = conn.execute(
                """
                SELECT execution_id, order_id, ticker, tx_date, side, shares, price, amount, currency, created_at
                FROM transactions
                ORDER BY tx_date
                """
            ).fetchall()

        LOGGER.debug("Loaded %d cached positions and %d transactions from SQLite", len(pos_rows), len(tx_rows))

        positions = [
            Position(
                ticker=str(row["ticker"]),
                company_name=str(row["company_name"]),
                shares=float(row["shares"]),
                avg_price=float(row["avg_price"]),
                currency=str(row["currency"]),
            )
            for row in pos_rows
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
            for row in tx_rows
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
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO transactions
                (execution_id, order_id, ticker, tx_date, side, shares, price, amount, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    order_id,
                    ticker,
                    tx_date,
                    side,
                    float(shares),
                    float(price),
                    float(amount),
                    currency,
                ),
            )
            inserted = cur.rowcount > 0
        LOGGER.debug(
            "%s transaction %s for %s",
            "Inserted" if inserted else "Skipped duplicate",
            execution_id,
            ticker,
        )
        return inserted

    def get_incremental_start_date(self, lookback_days: int = 7) -> str | None:
        """Return a safe start date for incremental Robinhood pulls.

        Uses latest non-seed transaction date and subtracts a small lookback window
        to avoid missing delayed/edited records. Duplicates are harmless due to
        execution_id dedupe.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(tx_date) AS max_tx_date
                FROM transactions
                WHERE execution_id NOT LIKE 'seed-%'
                """
            ).fetchone()

        raw = row["max_tx_date"] if row else None
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
        LOGGER.debug(
            "Initializing sync profile if missing: baseline_date=%s tracked=%d baseline_assets=%d initialized=%s",
            baseline_date,
            len(normalized_tracked),
            len(normalized_baseline),
            initialized,
        )
        tracked_payload = json.dumps(normalized_tracked, separators=(",", ":"))
        baseline_payload = json.dumps(normalized_baseline, separators=(",", ":"))
        initialized_flag = 1 if initialized else 0
        columns = self._prepare_sync_profile()
        insert_columns = ["id", "baseline_date", "tracked_tickers", "updated_at"]
        insert_values: list[Any] = [1, baseline_date, tracked_payload, now]

        if "baseline_value_usd" in columns:
            insert_columns.insert(2, "baseline_value_usd")
            insert_values.insert(2, None)
        if "baseline_assets" in columns:
            insert_columns.insert(3 if "baseline_value_usd" in columns else 2, "baseline_assets")
            insert_values.insert(3 if "baseline_value_usd" in columns else 2, baseline_payload)
        if "initialized" in columns:
            insert_columns.insert(len(insert_columns) - 1, "initialized")
            insert_values.insert(len(insert_values) - 1, initialized_flag)
        if "initialized_at" in columns:
            insert_columns.insert(len(insert_columns) - 1, "initialized_at")
            insert_values.insert(len(insert_values) - 1, None)
        if "last_sync_at" in columns:
            insert_columns.insert(len(insert_columns) - 1, "last_sync_at")
            insert_values.insert(len(insert_values) - 1, None)
        if "sync_version" in columns:
            insert_columns.insert(len(insert_columns) - 1, "sync_version")
            insert_values.insert(len(insert_values) - 1, 1)
        if "initial_sync_completed" in columns:
            insert_columns.insert(len(insert_columns) - 1, "initial_sync_completed")
            insert_values.insert(len(insert_values) - 1, initialized_flag)

        placeholders = ", ".join(["?"] * len(insert_columns))
        column_sql = ", ".join(insert_columns)
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO sync_profile ({column_sql}) VALUES ({placeholders})",
                insert_values,
            )

    def bootstrap_sync_profile_from_portfolio_json(self, json_path: Path) -> dict[str, Any]:
        baseline_assets = self.load_baseline_assets_from_portfolio_json(json_path)
        baseline_date = self.load_baseline_date_from_portfolio_json(json_path)
        baseline_set = set(baseline_assets)
        LOGGER.debug(
            "Bootstrapping sync profile from %s with %d baseline assets and date %s",
            json_path,
            len(baseline_set),
            baseline_date,
        )
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
        columns = self._prepare_sync_profile()
        if not columns:
            return None

        selected_columns = [
            column
            for column in [
                "baseline_date",
                "baseline_value_usd",
                "baseline_assets",
                "initialized",
                "initialized_at",
                "last_sync_at",
                "sync_version",
                "initial_sync_completed",
                "tracked_tickers",
            ]
            if column in columns
        ]

        if not selected_columns:
            return None

        sql = f"SELECT {', '.join(selected_columns)} FROM sync_profile WHERE id = 1"
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()

        if row is None:
            LOGGER.debug("No sync profile row exists yet")
            return None

        raw_tickers = row["tracked_tickers"] if "tracked_tickers" in row.keys() else None
        raw_baseline = row["baseline_assets"] if "baseline_assets" in row.keys() else None
        try:
            parsed = json.loads(str(raw_tickers))
            tracked = self._normalize_tickers(parsed) if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            tracked = []

        try:
            parsed_baseline = json.loads(str(raw_baseline))
            baseline_assets = self._normalize_tickers(parsed_baseline) if isinstance(parsed_baseline, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            baseline_assets = []

        initialized_flag = bool(int(row["initialized"] or 0)) if "initialized" in row.keys() else False
        legacy_completed_flag = bool(int(row["initial_sync_completed"] or 0)) if "initial_sync_completed" in row.keys() else False
        initialized = initialized_flag or legacy_completed_flag

        baseline_date = str(row["baseline_date"] or date.today().isoformat()) if "baseline_date" in row.keys() else date.today().isoformat()
        sync_version = int(row["sync_version"] or 1) if "sync_version" in row.keys() else 1

        return {
            "baseline_date": baseline_date,
            "baseline_value_usd": float(row["baseline_value_usd"]) if "baseline_value_usd" in row.keys() and row["baseline_value_usd"] is not None else None,
            "baseline_assets": baseline_assets,
            "initialized": initialized,
            "initialized_at": str(row["initialized_at"]) if "initialized_at" in row.keys() and row["initialized_at"] else None,
            "last_sync_at": str(row["last_sync_at"]) if "last_sync_at" in row.keys() and row["last_sync_at"] else None,
            "sync_version": sync_version,
            "initial_sync_completed": initialized,
            "tracked_tickers": tracked,
        }

    def set_baseline_value_usd(self, value: float) -> None:
        now = datetime.utcnow().isoformat()
        LOGGER.debug("Updating baseline value USD to %.2f", float(value))
        with self._connect() as conn:
            self._ensure_sync_profile_schema(conn)
            conn.execute(
                """
                UPDATE sync_profile
                SET baseline_value_usd = ?, updated_at = ?
                WHERE id = 1
                """,
                (float(value), now),
            )

    def add_tracked_tickers(self, tickers: set[str]) -> None:
        normalized = set(self._normalize_tickers(tickers))
        if not normalized:
            return

        LOGGER.debug("Adding %d tracked tickers", len(normalized))

        profile = self.get_sync_profile()
        if profile is None:
            self.initialize_sync_profile_if_missing(date.today().isoformat(), normalized)
            return

        existing = {str(t).upper().strip() for t in profile.get("tracked_tickers", []) if str(t).strip()}
        merged = sorted(existing | normalized)
        now = datetime.utcnow().isoformat()
        payload = json.dumps(merged, separators=(",", ":"))

        with self._connect() as conn:
            self._ensure_sync_profile_schema(conn)
            conn.execute(
                """
                UPDATE sync_profile
                SET tracked_tickers = ?, updated_at = ?
                WHERE id = 1
                """,
                (payload, now),
            )
            LOGGER.info("Tracked ticker set updated to %d tickers", len(merged))

    def set_tracked_tickers(self, tickers: set[str]) -> None:
        normalized = self._normalize_tickers(tickers)
        payload = json.dumps(normalized, separators=(",", ":"))
        now = datetime.utcnow().isoformat()
        LOGGER.debug("Replacing tracked ticker set with %d tickers", len(normalized))
        with self._connect() as conn:
            self._ensure_sync_profile_schema(conn)
            conn.execute(
                """
                UPDATE sync_profile
                SET tracked_tickers = ?, updated_at = ?
                WHERE id = 1
                """,
                (payload, now),
            )

    def mark_sync_initialized(self) -> None:
        now = datetime.utcnow().isoformat()
        LOGGER.info("Marking sync profile as initialized")
        with self._connect() as conn:
            self._ensure_sync_profile_schema(conn)
            conn.execute(
                """
                UPDATE sync_profile
                SET
                    initialized = 1,
                    initial_sync_completed = 1,
                    initialized_at = COALESCE(initialized_at, ?),
                    last_sync_at = ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (now, now, now),
            )

    def touch_last_sync(self) -> None:
        now = datetime.utcnow().isoformat()
        LOGGER.debug("Touching last sync timestamp")
        with self._connect() as conn:
            self._ensure_sync_profile_schema(conn)
            conn.execute(
                """
                UPDATE sync_profile
                SET last_sync_at = ?, updated_at = ?
                WHERE id = 1
                """,
                (now, now),
            )

    def mark_initial_sync_completed(self) -> None:
        # Backward-compatible alias used by existing callers.
        self.mark_sync_initialized()

    def list_cache_tickers(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT ticker FROM portfolio_cache").fetchall()
        LOGGER.debug("Loaded %d tickers from portfolio cache", len(rows))
        return {str(row["ticker"]) for row in rows}

    def get_portfolio_position(self, ticker: str) -> Position | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ticker, company_name, shares, avg_price, currency
                FROM portfolio_cache
                WHERE ticker = ?
                """,
                (ticker,),
            ).fetchone()

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
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT t.ticker
                FROM transactions t
                LEFT JOIN portfolio_cache p ON p.ticker = t.ticker
                WHERE p.ticker IS NULL
                ORDER BY t.ticker
                """
            ).fetchall()
        return [str(row["ticker"]) for row in rows]

    def list_unprovisioned_tickers_since(self, cutoff_date: date) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT t.ticker
                FROM transactions t
                LEFT JOIN portfolio_cache p ON p.ticker = t.ticker
                WHERE p.ticker IS NULL AND date(t.tx_date) > date(?)
                ORDER BY t.ticker
                """,
                (cutoff_date.isoformat(),),
            ).fetchall()
        return [str(row["ticker"]) for row in rows]

    def derive_position_from_transactions(self, ticker: str) -> tuple[float, float, str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT side, shares, price, currency
                FROM transactions
                WHERE ticker = ?
                ORDER BY tx_date, created_at, execution_id
                """,
                (ticker,),
            ).fetchall()

        shares = 0.0
        avg_price = 0.0
        currency = "USD"

        for row in rows:
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
    ) -> None:
        now = datetime.utcnow().isoformat()
        LOGGER.debug(
            "Upserting portfolio cache for %s: shares=%.4f avg_price=%.4f currency=%s last_price=%s market_cap=%s",
            ticker,
            float(shares),
            float(avg_price),
            currency,
            last_price,
            market_cap,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_cache
                (ticker, company_name, shares, avg_price, currency, last_price, market_cap, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    company_name = excluded.company_name,
                    shares = excluded.shares,
                    avg_price = excluded.avg_price,
                    currency = excluded.currency,
                    last_price = excluded.last_price,
                    market_cap = excluded.market_cap,
                    updated_at = excluded.updated_at
                """,
                (
                    ticker,
                    company_name,
                    float(shares),
                    float(avg_price),
                    currency,
                    None if last_price is None else float(last_price),
                    None if market_cap is None else float(market_cap),
                    now,
                ),
            )

    def refresh_existing_position_core(self, ticker: str) -> None:
        shares, avg_price, currency = self.derive_position_from_transactions(ticker)
        LOGGER.debug("Refreshing existing position core for %s: shares=%.4f avg_price=%.4f currency=%s", ticker, shares, avg_price, currency)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT company_name, last_price, market_cap FROM portfolio_cache WHERE ticker = ?",
                (ticker,),
            ).fetchone()

        if existing is None:
            return

        self.upsert_portfolio_cache(
            ticker=ticker,
            company_name=str(existing["company_name"]),
            shares=shares,
            avg_price=avg_price,
            currency=currency,
            last_price=existing["last_price"],
            market_cap=existing["market_cap"],
        )

    def update_market_snapshot(self, rows: list[dict[str, Any]]) -> None:
        now = datetime.utcnow().isoformat()
        LOGGER.debug("Updating market snapshot for %d portfolio rows", len(rows))
        with self._connect() as conn:
            for row in rows:
                conn.execute(
                    """
                    UPDATE portfolio_cache
                    SET
                        last_price = ?,
                        market_cap = ?,
                        updated_at = ?
                    WHERE ticker = ?
                    """,
                    (
                        row.get("current_price"),
                        row.get("market_cap"),
                        now,
                        row.get("ticker"),
                    ),
                )

    def override_portfolio_position(
        self,
        ticker: str,
        company_name: str,
        shares: float,
        avg_price: float,
        currency: str,
    ) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT last_price, market_cap FROM portfolio_cache WHERE ticker = ?",
                (ticker,),
            ).fetchone()

        last_price = existing["last_price"] if existing else None
        market_cap = existing["market_cap"] if existing else None

        self.upsert_portfolio_cache(
            ticker=ticker,
            company_name=company_name,
            shares=shares,
            avg_price=avg_price,
            currency=currency,
            last_price=last_price,
            market_cap=market_cap,
        )

    def delete_portfolio_position(self, ticker: str, delete_transactions: bool = True) -> None:
        LOGGER.info("Deleting portfolio position for %s (delete_transactions=%s)", ticker, delete_transactions)
        with self._connect() as conn:
            conn.execute("DELETE FROM portfolio_cache WHERE ticker = ?", (ticker,))
            if delete_transactions:
                conn.execute("DELETE FROM transactions WHERE ticker = ?", (ticker,))
