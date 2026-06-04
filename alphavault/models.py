from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(slots=True)
class Position:
    ticker: str
    company_name: str
    shares: float
    avg_price: float
    currency: str


@dataclass(slots=True)
class Transaction:
    ticker: str
    tx_date: date
    amount: float
    side: str | None = None
    shares: float | None = None
    price: float | None = None
    currency: str | None = None
    execution_id: str | None = None
    created_at: str | None = None


@dataclass(slots=True)
class Quote:
    price: float | None
    market_cap: float | None
    company_name: str | None = None
    currency: str | None = None


@dataclass(slots=True)
class Snapshot:
    quotes: dict[str, Quote]
    fx_to_usd: dict[str, float]
    online: bool
    stale_tickers: set[str]


def parse_position(raw: dict[str, Any]) -> Position:
    return Position(
        ticker=str(raw["ticker"]).upper().strip(),
        company_name=str(raw.get("company_name", "")).strip(),
        shares=float(raw["shares"]),
        avg_price=float(raw["avg_price"]),
        currency=str(raw["currency"]).upper().strip(),
    )


def parse_transaction(raw: dict[str, Any]) -> Transaction:
    return Transaction(
        ticker=str(raw["ticker"]).upper().strip(),
        tx_date=date.fromisoformat(str(raw["date"])),
        amount=float(raw["amount"]),
        side=str(raw.get("side") or "").strip().lower() or None,
        shares=float(raw["shares"]) if raw.get("shares") not in (None, "") else None,
        price=float(raw["price"]) if raw.get("price") not in (None, "") else None,
        currency=str(raw.get("currency") or "").upper().strip() or None,
        execution_id=str(raw.get("execution_id") or "").strip() or None,
        created_at=str(raw.get("created_at") or "").strip() or None,
    )
