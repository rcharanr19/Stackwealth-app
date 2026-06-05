from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import numpy as np
import numpy_financial as npf
import pandas as pd

from .models import Position, Quote, Transaction


EPSILON = 0.0001


def _xnpv(rate: float, cashflows: list[tuple[date, float]]) -> float:
    d0 = cashflows[0][0]
    return float(
        sum(
            amount / (1.0 + rate) ** ((d - d0).days / 365.0)
            for d, amount in cashflows
        )
    )


def _xirr_fallback(cashflows: list[tuple[date, float]]) -> float | None:
    if not cashflows:
        return None

    amounts = [amount for _, amount in cashflows]
    has_pos = any(a > 0 for a in amounts)
    has_neg = any(a < 0 for a in amounts)
    if not (has_pos and has_neg):
        return None

    low, high = -0.99, 1.0
    f_low = _xnpv(low, cashflows)
    f_high = _xnpv(high, cashflows)

    while np.sign(f_low) == np.sign(f_high) and high < 1_000_000.0:
        high *= 2.0
        f_high = _xnpv(high, cashflows)

    if np.sign(f_low) == np.sign(f_high):
        return None

    for _ in range(80):
        mid = (low + high) / 2.0
        f_mid = _xnpv(mid, cashflows)
        if abs(f_mid) < 1e-7:
            return mid
        if np.sign(f_mid) == np.sign(f_low):
            low, f_low = mid, f_mid
        else:
            high, f_high = mid, f_mid
    return (low + high) / 2.0


def compute_xirr(transactions: Iterable[Transaction], terminal_value: float) -> float | None:
    flows = [(t.tx_date, float(t.amount)) for t in transactions]
    flows.append((date.today(), float(terminal_value)))
    flows.sort(key=lambda x: x[0])

    amounts = [a for _, a in flows]
    if not (any(a > 0 for a in amounts) and any(a < 0 for a in amounts)):
        return None

    return _xirr_fallback(flows)


def _sort_transaction_key(tx: Transaction) -> tuple:
    return (
        tx.tx_date,
        tx.created_at or "",
        tx.execution_id or "",
        tx.amount,
    )


def _normalised_side(tx: Transaction) -> str:
    if tx.side:
        return tx.side.lower().strip()
    return "buy" if tx.amount < 0 else "sell"


def _shares_as_of(transactions: list[Transaction]) -> float:
    shares = 0.0
    for tx in sorted(transactions, key=_sort_transaction_key):
        side = _normalised_side(tx)
        qty = float(tx.shares or 0.0)
        if side == "buy" and qty > 0:
            shares += qty
        elif side == "sell" and qty > 0:
            shares = max(shares - qty, 0.0)
    return shares


def _position_pnl_from_transactions(transactions: list[Transaction]) -> tuple[float, float, float, float]:
    ordered = sorted(transactions, key=_sort_transaction_key)

    shares = 0.0
    avg_cost = 0.0
    realized = 0.0
    total_buy_cost = 0.0

    for tx in ordered:
        side = _normalised_side(tx)
        qty = float(tx.shares or 0.0)
        px = float(tx.price or 0.0)
        amount = float(tx.amount)

        if side == "buy" and qty > 0 and px > 0:
            total_buy_cost += qty * px
            new_total = shares + qty
            avg_cost = ((shares * avg_cost) + (qty * px)) / new_total if new_total > 0 else 0.0
            shares = new_total
            continue

        if side == "sell" and qty > 0 and px > 0:
            if shares > 0:
                realized += (px - avg_cost) * qty
            shares = max(shares - qty, 0.0)
            if shares < EPSILON:
                shares = 0.0
                avg_cost = 0.0
            continue

        if amount > 0 and qty <= 0 and px <= 0:
            # Cash-only inflows such as dividends. They contribute to XIRR, not cost basis.
            realized += amount

    return realized, shares, avg_cost, total_buy_cost


def build_metrics_table(
    positions: list[Position],
    transactions: list[Transaction],
    quotes: dict[str, Quote],
    fx_to_usd: dict[str, float],
    stale_tickers: set[str] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, float | str | None]] = []
    tx_by_ticker: dict[str, list[Transaction]] = {}
    stale_set = stale_tickers or set()
    for tx in transactions:
        tx_by_ticker.setdefault(tx.ticker, []).append(tx)

    for pos in positions:
        ticker_transactions = tx_by_ticker.get(pos.ticker, [])
        realized_native, _, derived_avg_cost, total_buy_cost_native = _position_pnl_from_transactions(ticker_transactions)

        current_shares = float(pos.shares)
        avg_price = float(pos.avg_price)
        if current_shares < EPSILON:
            current_shares = 0.0
        if current_shares <= 0 and derived_avg_cost > 0:
            avg_price = derived_avg_cost

        quote = quotes.get(pos.ticker)
        price = quote.price if quote and quote.price is not None else np.nan
        market_cap = quote.market_cap if quote else np.nan
        previous_close = quote.previous_close if quote and quote.previous_close is not None else np.nan
        fx_rate = fx_to_usd.get(pos.currency, np.nan)

        unrealized_native = (price - avg_price) * current_shares if np.isfinite(price) and current_shares > 0 and avg_price > 0 else 0.0 if current_shares <= 0 else np.nan
        total_native = realized_native + (unrealized_native if np.isfinite(unrealized_native) else 0.0)

        total_change_pct = (total_native / total_buy_cost_native) * 100.0 if total_buy_cost_native > 0 else np.nan
        last_day_change_pct = (
            ((price - previous_close) / previous_close) * 100.0
            if np.isfinite(price) and np.isfinite(previous_close) and previous_close > 0
            else np.nan
        )
        unrealized_change_pct = (
            ((price - avg_price) / avg_price) * 100.0
            if np.isfinite(price) and current_shares > 0 and avg_price > 0
            else np.nan
        )

        equity_native = current_shares * price if np.isfinite(price) and current_shares > 0 else 0.0 if current_shares <= 0 else np.nan
        equity_usd = equity_native * fx_rate if np.isfinite(equity_native) and np.isfinite(fx_rate) else np.nan
        realized_usd = realized_native * fx_rate if np.isfinite(realized_native) and np.isfinite(fx_rate) else np.nan
        unrealized_usd = unrealized_native * fx_rate if np.isfinite(unrealized_native) and np.isfinite(fx_rate) else np.nan
        total_usd = realized_usd + unrealized_usd if np.isfinite(realized_usd) and np.isfinite(unrealized_usd) else np.nan

        terminal_value_native = equity_native if current_shares > 0 else 0.0
        xirr = compute_xirr(ticker_transactions, float(terminal_value_native) if np.isfinite(terminal_value_native) else 0.0)

        rows.append(
            {
                "ticker": pos.ticker,
                "company_name": pos.company_name,
                "shares": current_shares,
                "avg_price": pos.avg_price,
                "currency": pos.currency,
                "current_price": price,
                "market_cap": market_cap,
                "equity_native": equity_native,
                "realized_pnl_native": realized_native,
                "unrealized_pnl_native": unrealized_native,
                "unrealized_change_pct": unrealized_change_pct,
                "pnl_native": total_native,
                "change_pct": total_change_pct,
                "total_change_pct": total_change_pct,
                "last_day_change_pct": last_day_change_pct,
                "equity_usd": equity_usd,
                "realized_pnl_usd": realized_usd,
                "unrealized_pnl_usd": unrealized_usd,
                "pnl_usd": total_usd,
                "xirr": xirr,
                "is_stale": pos.ticker in stale_set,
                "is_closed": current_shares < EPSILON,
            }
        )

    df = pd.DataFrame(rows)
    total_usd = float(df["equity_usd"].sum(skipna=True)) if not df.empty else 0.0
    if total_usd > 0 and "equity_usd" in df:
        df["allocation_pct"] = (df["equity_usd"] / total_usd) * 100.0
    else:
        df["allocation_pct"] = np.nan

    return df


def compute_portfolio_xirr(
    transactions: list[Transaction],
    positions: list[Position],
    quotes: dict[str, Quote],
    fx_to_usd: dict[str, float],
) -> float | None:
    """Compute blended portfolio XIRR with all cash flows normalized to USD."""
    currency_by_ticker: dict[str, str] = {p.ticker: p.currency for p in positions}

    # Convert each historical transaction to USD
    usd_transactions: list[Transaction] = []
    for tx in transactions:
        ccy = currency_by_ticker.get(tx.ticker, "USD")
        rate = fx_to_usd.get(ccy, 1.0)
        usd_transactions.append(
            Transaction(
                ticker=tx.ticker,
                tx_date=tx.tx_date,
                amount=tx.amount * rate,
            )
        )

    # Terminal value: sum of all position equities converted to USD
    terminal_usd = 0.0
    for pos in positions:
        q = quotes.get(pos.ticker)
        if q and q.price is not None:
            rate = fx_to_usd.get(pos.currency, 1.0)
            terminal_usd += pos.shares * q.price * rate

    return compute_xirr(usd_transactions, terminal_usd)


def compute_portfolio_window_metrics(
    transactions: list[Transaction],
    positions: list[Position],
    quotes: dict[str, Quote],
    fx_to_usd: dict[str, float],
    cutoff_prices: dict[str, dict[date, float | None]],
    windows: Iterable[int] = (1, 2, 3, 5, 10),
) -> dict[str, dict[str, float | None]]:
    """Compute lookback performance windows from the current ledger and snapshot."""
    currency_by_ticker: dict[str, str] = {p.ticker: p.currency for p in positions}
    tx_by_ticker: dict[str, list[Transaction]] = {}
    for tx in transactions:
        tx_by_ticker.setdefault(tx.ticker, []).append(tx)
    for ticker in tx_by_ticker:
        tx_by_ticker[ticker].sort(key=_sort_transaction_key)

    end_value_usd_total = 0.0
    position_meta: list[tuple[str, float, float, float | None]] = []
    for pos in positions:
        ticker = pos.ticker
        ccy = currency_by_ticker.get(ticker, pos.currency)
        rate = fx_to_usd.get(ccy, 1.0)
        current_quote = quotes.get(ticker)
        current_price = current_quote.price if current_quote and current_quote.price is not None else None
        shares_now = float(pos.shares)
        if current_price is not None and shares_now > 0:
            end_value_usd_total += shares_now * current_price * rate
        position_meta.append((ticker, rate, shares_now, current_price))

    today = date.today()
    results: dict[str, dict[str, float | None]] = {}
    for years in windows:
        cutoff = today - timedelta(days=365 * int(years))
        cutoff_price_map = {ticker: prices.get(cutoff) for ticker, prices in cutoff_prices.items()}

        start_value_usd = 0.0
        cashflows: list[tuple[date, float]] = []

        for ticker, rate, _shares_now, _current_price in position_meta:
            ticker_transactions = tx_by_ticker.get(ticker, [])
            shares_at_cutoff = 0.0
            for tx in ticker_transactions:
                if tx.tx_date < cutoff:
                    side = _normalised_side(tx)
                    qty = float(tx.shares or 0.0)
                    if side == "buy" and qty > 0:
                        shares_at_cutoff += qty
                    elif side == "sell" and qty > 0:
                        shares_at_cutoff = max(shares_at_cutoff - qty, 0.0)
                    continue
                cashflows.append((tx.tx_date, float(tx.amount) * rate))

            cutoff_price = cutoff_price_map.get(ticker)

            if cutoff_price is not None and shares_at_cutoff > 0:
                start_value_usd += shares_at_cutoff * cutoff_price * rate

        if start_value_usd <= 0:
            results[f"{years}Y"] = {"xirr": None, "change_pct": None}
            continue

        window_flows = [(cutoff, -start_value_usd)]
        window_flows.extend(cashflows)
        window_flows.append((today, end_value_usd_total))
        window_flows.sort(key=lambda x: x[0])

        xirr = _xirr_fallback(window_flows)
        change_pct = ((end_value_usd_total - start_value_usd) / start_value_usd) * 100.0

        results[f"{years}Y"] = {"xirr": xirr, "change_pct": change_pct}

    return results


def compute_portfolio_since_start_metrics(
    transactions: list[Transaction],
    positions: list[Position],
    quotes: dict[str, Quote],
    fx_to_usd: dict[str, float],
    baseline_date: date,
    baseline_value_usd: float,
    tracked_tickers: set[str],
) -> dict[str, float | None]:
    """Compute portfolio XIRR and total change since baseline date.

    The change metric is a simple total return percentage:
    (ending value - baseline value) / baseline value.
    """
    tracked = {t.upper().strip() for t in tracked_tickers if t}
    currency_by_ticker: dict[str, str] = {p.ticker: p.currency for p in positions}

    end_value_usd = 0.0
    for pos in positions:
        if tracked and pos.ticker not in tracked:
            continue
        q = quotes.get(pos.ticker)
        if q and q.price is not None and pos.shares > 0:
            rate = fx_to_usd.get(pos.currency, 1.0)
            end_value_usd += pos.shares * q.price * rate

    if baseline_value_usd <= 0:
        return {"xirr": None, "change_pct": None}

    flows: list[tuple[date, float]] = [(baseline_date, -float(baseline_value_usd))]
    for tx in transactions:
        if tracked and tx.ticker not in tracked:
            continue
        if tx.tx_date < baseline_date:
            continue
        ccy = currency_by_ticker.get(tx.ticker, "USD")
        rate = fx_to_usd.get(ccy, 1.0)
        flows.append((tx.tx_date, float(tx.amount) * rate))
    flows.append((date.today(), float(end_value_usd)))
    flows.sort(key=lambda x: x[0])

    xirr = _xirr_fallback(flows)
    if xirr is None:
        return {"xirr": None, "change_pct": ((end_value_usd - baseline_value_usd) / baseline_value_usd) * 100.0}

    change_pct = ((end_value_usd - baseline_value_usd) / baseline_value_usd) * 100.0
    return {"xirr": xirr, "change_pct": change_pct}
