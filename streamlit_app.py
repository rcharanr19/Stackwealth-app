from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yfinance as yf
import re
import json

from alphavault.cache_store import CacheStore
from alphavault.finance_engine import (
    build_metrics_table,
    compute_portfolio_since_start_metrics,
    compute_xirr,
    exclude_seed_transactions_with_real_history,
)
from alphavault.postgres_store import PostgresStore
from alphavault.logging_utils import configure_logging
from alphavault.market_data import MarketDataService
from alphavault.robinhood_sync import RobinhoodSyncService
from tabs.ai_analysis import (
    get_reverse_dcf_analysis,
    get_transcript_mosaic_analysis,
    get_model_name,
    generate_portfolio_ai_overview,
    fetch_fmp_financial_profile,
    generate_comparative_investment_thesis,
    FMP_SUPPORTED_TICKERS,
    _fetch_sec_financials_for_symbol,
)
from tabs.ai_analysis import FMP_SUPPORTED_TICKERS
from tabs.portfolio_analytics import (
    identify_tax_loss_harvesting_candidates,
    calculate_tax_impact,
    get_wash_sale_alert,
    calculate_dividend_projections,
    get_dividend_summary,
    benchmark_portfolio_returns,
    get_benchmark_summary,
)
from tabs.tax_settings import (
    TaxSettings,
    get_state_list,
    get_no_income_tax_states,
    calculate_total_tax_rate,
    format_tax_rate,
)


configure_logging()
LOGGER = logging.getLogger(__name__)

# Optional EdgarTools integration (open-source SEC wrapper). If unavailable, fallback safely.
try:
    import edgartools as edgar  # type: ignore
except Exception:
    # Some installs expose the runtime package as `edgar` (module name differs from PyPI name).
    try:
        import edgar as edgar  # type: ignore
    except Exception as exc:
        LOGGER.warning(
            "Optional EDGAR integration unavailable (edgartools/edgar import failed): %s",
            exc,
        )
        edgar = None

APP_TITLE = "StackWealth"
PORTFOLIO_JSON = Path("data/portfolio.json")
CACHE_PATH = Path("cache/market_cache.json")


def format_whole_number(value: Any) -> Any:
    if value is None or pd.isna(value):
        return value
    return f"{int(abs(value)):,}"


def format_currency(value: Any) -> Any:
    if value is None or pd.isna(value):
        return value
    amount = int(abs(float(value)))
    return f"-${amount:,}" if float(value) < 0 else f"${amount:,}"


def format_signed_pct(value: Any) -> Any:
    if value is None or pd.isna(value):
        return value
    return f"{float(value):+.2f}%"


def format_unsigned_pct(value: Any) -> Any:
    if value is None or pd.isna(value):
        return value
    return f"{float(value):.2f}%"


def style_signed_value(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    numeric_value = float(value)
    if numeric_value > 0:
        return "color: #2ecc71"
    if numeric_value < 0:
        return "color: #e74c3c"
    return ""


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _market_cap_tier(market_cap: float | None) -> str:
    if market_cap is None:
        return "Unknown"
    if market_cap >= 200_000_000_000:
        return "Mega Cap"
    if market_cap >= 10_000_000_000:
        return "Large Cap"
    if market_cap >= 2_000_000_000:
        return "Mid Cap"
    if market_cap >= 300_000_000:
        return "Small Cap"
    if market_cap >= 50_000_000:
        return "Micro Cap"
    return "Nano Cap"


def _fmt_currency_2(value: Any) -> Any:
    if value is None or pd.isna(value):
        return value
    return f"${float(value):,.2f}"


def _fmt_iso_ts(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts)).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


def _transaction_rows_for_ticker(transactions: list[Any], ticker: str) -> pd.DataFrame:
    rows = []
    ticker_upper = str(ticker or "").upper().strip()
    for tx in transactions:
        if str(getattr(tx, "ticker", "")).upper().strip() != ticker_upper:
            continue
        rows.append(
            {
                "Date": getattr(tx, "tx_date", None),
                "Side": str(getattr(tx, "side", "") or "").upper(),
                "Shares": getattr(tx, "shares", None),
                "Price": getattr(tx, "price", None),
                "Amount": getattr(tx, "amount", None),
                "Currency": getattr(tx, "currency", None) or "USD",
                "Execution": getattr(tx, "execution_id", None),
            }
        )
    return pd.DataFrame(rows)


def _ticker_xirr(transactions: list[Any], ticker: str, terminal_value: float, start_date: date | None = None) -> float | None:
    ticker_upper = str(ticker or "").upper().strip()
    ticker_transactions = [
        tx
        for tx in transactions
        if str(getattr(tx, "ticker", "")).upper().strip() == ticker_upper
        and (start_date is None or getattr(tx, "tx_date", date.min) >= start_date)
    ]
    if not ticker_transactions:
        return None
    return compute_xirr(ticker_transactions, terminal_value)


def _sort_portfolio_view(frame: pd.DataFrame, view_name: str, xirr_column: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    if view_name == "Largest Losses" and "open_pnl" in frame:
        return frame.sort_values("open_pnl", ascending=True, na_position="last")
    if view_name == "Highest XIRR" and xirr_column in frame:
        return frame.sort_values(xirr_column, ascending=False, na_position="last")
    if view_name == "Lowest XIRR" and xirr_column in frame:
        return frame.sort_values(xirr_column, ascending=True, na_position="last")
    if view_name == "Highest Allocation" and "weight_pct" in frame:
        return frame.sort_values("weight_pct", ascending=False, na_position="last")
    return frame.sort_values("current_value", ascending=False, na_position="last") if "current_value" in frame else frame


def _history_sample_dates(start_date: date, max_points: int = 48) -> list[date]:
    today = date.today()
    if start_date >= today:
        return [today]
    dates = [d.date() for d in pd.date_range(start=start_date, end=today, periods=max_points)]
    dates.append(today)
    return sorted(set(dates))


def _shares_as_of_from_current(transactions: list[Any], ticker: str, current_shares: float, as_of: date) -> float:
    shares = float(current_shares or 0.0)
    ticker_upper = str(ticker or "").upper().strip()
    for tx in transactions:
        if str(getattr(tx, "ticker", "")).upper().strip() != ticker_upper:
            continue
        tx_date = getattr(tx, "tx_date", None)
        qty = float(getattr(tx, "shares", None) or 0.0)
        if not tx_date or qty <= 0 or tx_date <= as_of:
            continue
        side = str(getattr(tx, "side", "") or "").lower().strip()
        if side == "buy":
            shares -= qty
        elif side == "sell":
            shares += qty
    return max(shares, 0.0)


def _portfolio_value_history(
    portfolio_summary: pd.DataFrame,
    transactions: list[Any],
    market_service: MarketDataService,
    baseline_date: date,
    margin_balance: float,
    cash_usd: float = 0.0,
) -> pd.DataFrame:
    if portfolio_summary.empty:
        return pd.DataFrame(columns=["Date", "Gross Holdings", "Net Portfolio Value"])

    tickers = [str(ticker).upper().strip() for ticker in portfolio_summary["ticker"].dropna().tolist()]
    sample_dates = _history_sample_dates(baseline_date)
    historical_dates = [sample_date for sample_date in sample_dates if sample_date < date.today()]
    cutoff_prices = market_service.fetch_cutoff_prices(tickers, historical_dates) if historical_dates else {}

    rows = []
    for sample_date in sample_dates:
        gross_value = 0.0
        for _, row in portfolio_summary.iterrows():
            ticker = str(row.get("ticker") or "").upper().strip()
            if not ticker:
                continue
            current_shares = float(row.get("shares") or 0.0)
            shares = _shares_as_of_from_current(transactions, ticker, current_shares, sample_date)
            if shares <= 0:
                continue

            if sample_date >= date.today():
                price = _safe_float(row.get("current_price"))
            else:
                price = cutoff_prices.get(ticker, {}).get(sample_date)

            current_price = _safe_float(row.get("current_price"))
            current_price_usd = _safe_float(row.get("current_price_usd")) or current_price
            fx_rate = (current_price_usd / current_price) if current_price and current_price_usd else 1.0
            if price is not None:
                gross_value += shares * float(price) * float(fx_rate or 1.0)

        rows.append(
            {
                "Date": sample_date,
                "Gross Holdings": gross_value,
                "Net Portfolio Value": gross_value + cash_usd - float(margin_balance or 0.0),
            }
        )

    return pd.DataFrame(rows)


def _hash_portfolio_snapshot(payload: dict[str, Any]) -> str:
    try:
        s = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _brief_portfolio_hash(metrics: pd.DataFrame, portfolio_summary: pd.DataFrame, profile: dict[str, Any]) -> str:
    """Compute a lightweight, deterministic hash from key portfolio fields without external API calls.

    Uses ticker, shares, avg_cost, current_value and cash to detect snapshot changes for cache staleness.
    """
    try:
        rows = []
        frame = metrics if not metrics.empty else portfolio_summary
        for _, r in frame.iterrows():
            rows.append({
                "ticker": str(r.get("ticker")),
                "shares": float(r.get("shares") or 0.0),
                "avg_cost": float(r.get("avg_cost") or 0.0),
                "current_value": float(r.get("equity_usd") or r.get("current_value") or 0.0),
            })
        brief = {
            "rows": sorted(rows, key=lambda x: x["ticker"]),
            "cash_usd": float(profile.get("cash_usd") or 0.0),
            "margin_balance_usd": float(profile.get("margin_balance_usd") or 0.0),
        }
        s = json.dumps(brief, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def build_portfolio_overview_input(metrics: pd.DataFrame, portfolio_summary: pd.DataFrame, profile: dict[str, Any], since_start: dict[str, Any] | None = None) -> dict[str, Any]:
    """Construct the deterministic payload consumed by the portfolio overview generator."""
    now_iso = datetime.utcnow().isoformat()

    totals = {
        "portfolio_value_usd": float(metrics["equity_usd"].sum(skipna=True)) if "equity_usd" in metrics else 0.0,
        "cash_usd": float(profile.get("cash_usd") or 0.0) if isinstance(profile, dict) else 0.0,
        "margin_balance_usd": float(profile.get("margin_balance_usd") or 0.0) if isinstance(profile, dict) else 0.0,
    }
    totals["net_portfolio_value_usd"] = totals["portfolio_value_usd"] + totals["cash_usd"] - totals["margin_balance_usd"]
    totals["cash_weight_pct"] = (totals["cash_usd"] / totals["portfolio_value_usd"] * 100.0) if totals["portfolio_value_usd"] > 0 else 0.0
    if since_start and since_start.get("xirr") is not None:
        totals["portfolio_xirr_pct"] = round(float(since_start["xirr"]) * 100.0, 2)

    # Build holdings list from metrics (fallback to portfolio_summary)
    frame = metrics if not metrics.empty else portfolio_summary
    holdings: list[dict[str, Any]] = []
    for _, r in frame.iterrows():
        xirr_val = _safe_float(r.get("xirr"))
        holdings.append(
            {
                "ticker": str(r.get("ticker")),
                "shares": float(r.get("shares") or 0.0),
                "avg_cost": float(r.get("avg_cost") or 0.0),
                "cost_basis": float(r.get("cost_basis") or 0.0),
                "current_price": float(r.get("current_price") or 0.0),
                "current_value": float(r.get("equity_usd") or r.get("current_value") or 0.0),
                "weight_pct": float(r.get("weight_pct") or 0.0),
                "day_change_pct": float(r.get("last_day_change_pct") or 0.0),
                "pnl_total_usd": float(r.get("pnl_usd") or 0.0),
                "xirr_pct": round(xirr_val * 100.0, 2) if xirr_val is not None else None,
                "segment_exposure": r.get("segment") or r.get("company_name") or "Unknown",
            }
        )

    # Compute simple ROIC/WACC proxies per ticker using lightweight yfinance fast_info only (avoid heavy financial endpoints)
    economics_by_ticker: list[dict[str, Any]] = []
    for h in holdings:
        ticker = str(h.get("ticker") or "").upper().strip()
        roic = None
        wacc = None
        # Lightweight market cap via yfinance.fast_info
        try:
            t = yf.Ticker(ticker)
            raw_fast = getattr(t, "fast_info", None)
            if raw_fast is None:
                fast = {}
            elif isinstance(raw_fast, dict):
                fast = raw_fast
            else:
                try:
                    fast = {
                        "marketCap": getattr(raw_fast, "marketCap", None),
                        "market_cap": getattr(raw_fast, "market_cap", None),
                    }
                    fast = {k: v for k, v in fast.items() if v is not None}
                except Exception:
                    fast = {}
            mcap = _safe_float(fast.get("marketCap") or fast.get("market_cap"))
        except Exception:
            mcap = None

        # Prefer FMP data for supported tickers (free tier); otherwise use Edgar SEC extraction when available
        ocf = None
        total_assets = None
        total_debt = None
        try:
            if ticker in FMP_SUPPORTED_TICKERS:
                try:
                    fp = fetch_fmp_financial_profile(ticker)
                    # FMP returns lists for cash_flow and balance_sheet; extract most recent row if present
                    cf = fp.get("cash_flow") or []
                    bs = fp.get("balance_sheet") or []
                    if isinstance(cf, list) and len(cf) > 0 and isinstance(cf[0], dict):
                        ocf = _safe_float(cf[0].get("operatingCashFlow") or cf[0].get("operating_cash_flow") or cf[0].get("operatingCashflow"))
                    if isinstance(bs, list) and len(bs) > 0 and isinstance(bs[0], dict):
                        total_assets = _safe_float(bs[0].get("totalAssets") or bs[0].get("total_assets"))
                        total_debt = _safe_float(bs[0].get("totalDebt") or bs[0].get("total_debt"))
                except Exception:
                    ocf = None
                    total_assets = None
                    total_debt = None
            else:
                # try SEC extraction via EdgarTools (best-effort)
                # Prefer module-level Edgar helper when available, fallback to local fetcher
                try:
                    sec = None
                    try:
                        sec = _fetch_sec_financials_for_symbol(ticker)
                    except Exception:
                        sec = fetch_sec_financials_via_edgar(ticker)
                    if sec:
                        ocf = _safe_float(sec.get("operating_cash_flow"))
                        total_assets = _safe_float(sec.get("total_assets"))
                        total_debt = _safe_float(sec.get("total_debt"))
                except Exception:
                    ocf = None
                    total_assets = None
                    total_debt = None
        except Exception:
            ocf = None
            total_assets = None
            total_debt = None

        # Compute ROIC if we have OCF and net invested capital
        roic = None
        try:
            if ocf is not None and total_assets is not None and total_debt is not None and (total_assets - total_debt) > 0:
                roic = (ocf / (total_assets - total_debt)) * 100.0
        except Exception:
            roic = None

        # Deterministic WACC proxy by market cap band
        if mcap is None:
            wacc = 8.0
        elif mcap >= 200_000_000_000:
            wacc = 7.0
        elif mcap >= 10_000_000_000:
            wacc = 8.0
        else:
            wacc = 9.0

        spread = None if roic is None else (roic - wacc)
        economics_by_ticker.append({"ticker": ticker, "roic_pct": roic, "wacc_pct": wacc, "spread_pct": spread, "market_cap": mcap})

    # Build projections using simple deterministic rules (placeholder numeric proxies)
    # For handoff: cheaper model will refine these from fundamentals; keep deterministic defaults.
    projections = {"3y": {"low": 0.0, "base": 0.0, "high": 0.0}, "5y": {"low": 0.0, "base": 0.0, "high": 0.0}, "10y": {"low": 0.0, "base": 0.0, "high": 0.0}}
    # Weighted average of per-ticker simple proxy: use pnl_total_usd weight as short proxy for expected growth sensitivity
    total_value = totals["portfolio_value_usd"] or 1.0
    weighted_base = 0.0
    for h in holdings:
        w = float(h.get("weight_pct") or 0.0) / 100.0
        # crude base proxy: 3% + (weighted pnl ratio)
        base = 0.03 + max(min((h.get("pnl_total_usd") or 0.0) / max(h.get("cost_basis") or 1.0, 1.0), 0.20), -0.10)
        weighted_base += w * base
    projections["3y"]["base"] = round((1 + weighted_base) ** (1) - 1 if False else weighted_base * 100.0, 2)
    projections["5y"]["base"] = round(weighted_base * 100.0, 2)
    projections["10y"]["base"] = round(weighted_base * 100.0, 2)

    payload = {
        "generated_at_utc": now_iso,
        "portfolio_hash": None,
        "totals": totals,
        "holdings": holdings,
        "economics": {"by_ticker": economics_by_ticker, "portfolio_weighted_spread_pct": None},
        "projections": projections,
    }
    payload["portfolio_hash"] = _hash_portfolio_snapshot(payload)
    return payload


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_market_snapshot(tickers: tuple[str, ...]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        symbol = str(ticker).upper().strip()
        if not symbol:
            continue
        instrument = yf.Ticker(symbol)
        raw_fast = getattr(instrument, "fast_info", None)
        if raw_fast is None:
            fast_info = {}
        elif isinstance(raw_fast, dict):
            fast_info = raw_fast
        else:
            try:
                fast_info = {
                    "lastPrice": getattr(raw_fast, "lastPrice", None),
                    "last_price": getattr(raw_fast, "last_price", None),
                    "marketCap": getattr(raw_fast, "marketCap", None),
                    "market_cap": getattr(raw_fast, "market_cap", None),
                    "currency": getattr(raw_fast, "currency", None),
                }
                fast_info = {k: v for k, v in fast_info.items() if v is not None}
            except Exception:
                fast_info = {}
        # Prefer fast_info for lightweight snapshot (avoids heavy network calls)
        price = _safe_float(fast_info.get("lastPrice") or fast_info.get("last_price"))
        if price is None:
            # fallback to a very small history request which is lighter than .info
            try:
                history = instrument.history(period="5d")
                if not history.empty and "Close" in history:
                    close_series = history["Close"].dropna()
                    if not close_series.empty:
                        price = _safe_float(close_series.iloc[-1])
            except Exception:
                price = None
        market_cap = _safe_float(fast_info.get("marketCap") or fast_info.get("market_cap"))
        rows.append(
            {
                "ticker": symbol,
                "current_price": price,
                "market_cap": market_cap,
                "market_cap_tier": _market_cap_tier(market_cap),
            }
        )
    return pd.DataFrame(rows)


def load_active_holdings_df(db: PostgresStore) -> pd.DataFrame:
    positions, _transactions = db.load_portfolio_state()
    rows = [
        {
            "ticker": position.ticker,
            "company_name": position.company_name,
            "shares": float(position.shares),
            "avg_cost": float(position.avg_price),
            "cost_basis": float(position.shares) * float(position.avg_price),
        }
        for position in positions
        if float(position.shares) > 0
    ]
    return pd.DataFrame(rows)


def build_portfolio_summary(holdings: pd.DataFrame) -> pd.DataFrame:
    if holdings.empty:
        return holdings

    unique_tickers = tuple(sorted({str(item).upper().strip() for item in holdings["ticker"].tolist()}))
    market = fetch_market_snapshot(unique_tickers)
    merged = holdings.merge(market, how="left", on="ticker")
    merged["current_value"] = merged["shares"] * merged["current_price"]
    merged["open_pnl"] = merged["current_value"] - merged["cost_basis"]
    merged["open_pnl_margin_pct"] = (
        (merged["open_pnl"] / merged["cost_basis"]) * 100.0
    ).where(merged["cost_basis"] > 0)

    total_value = float(merged["current_value"].sum(skipna=True))
    merged["weight_pct"] = ((merged["current_value"] / total_value) * 100.0) if total_value > 0 else 0.0
    return merged.sort_values(by="weight_pct", ascending=False, na_position="last")


def _supports_dialog() -> bool:
    return callable(getattr(st, "dialog", None))


def require_login() -> None:
    if st.session_state.get("authenticated"):
        return

    expected_password = str(st.secrets.get("APP_PASSWORD", "")).strip()
    if not expected_password:
        st.error("APP_PASSWORD is not configured. Add it in Streamlit app secrets.")
        st.stop()

    st.title("Sign in")
    entered_password = st.text_input("Password", type="password", key="app_password_input")
    if st.button("Enter", width="stretch"):
        if hmac.compare_digest(str(entered_password or ""), expected_password):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Invalid password")
    st.stop()


@st.cache_resource(show_spinner=False)
def get_services() -> tuple[PostgresStore, MarketDataService, RobinhoodSyncService]:
    # WARNING: keep .streamlit/secrets.toml out of git; it contains live database credentials.
    pg_conn = st.connection("postgresql", type="sql")
    db = PostgresStore(pg_conn)
    db.ensure_schema()
    db.seed_from_json(PORTFOLIO_JSON)
    db.bootstrap_sync_profile_from_portfolio_json(PORTFOLIO_JSON)
    cache = CacheStore(CACHE_PATH)
    market_service = MarketDataService(cache=cache)
    sync_service = RobinhoodSyncService(db, market_service)
    return db, market_service, sync_service


def compute_dashboard(db: PostgresStore, market_service: MarketDataService) -> tuple[pd.DataFrame, dict[str, object], dict[str, object]]:
    profile = db.get_sync_profile() or db.bootstrap_sync_profile_from_portfolio_json(PORTFOLIO_JSON)
    positions, transactions = db.load_portfolio_state()
    transactions = exclude_seed_transactions_with_real_history(transactions)
    baseline_date = date.fromisoformat(str(profile.get("baseline_date") or date.today().isoformat()))
    tracked_assets = set(profile.get("tracked_tickers") or profile.get("baseline_assets") or [])
    tx_since_start = [
        tx
        for tx in transactions
        if tx.tx_date >= baseline_date and (not tracked_assets or tx.ticker in tracked_assets)
    ]

    tickers = [position.ticker for position in positions]
    currencies = [position.currency for position in positions]
    snapshot = market_service.refresh_snapshot(tickers=tickers, currencies=currencies)
    metrics = build_metrics_table(
        positions=positions,
        transactions=transactions,
        quotes=snapshot.quotes,
        fx_to_usd=snapshot.fx_to_usd,
        stale_tickers=snapshot.stale_tickers,
        baseline_date=baseline_date,
    )
    db.update_market_snapshot(metrics.to_dict(orient="records"))

    baseline_value_raw = profile.get("baseline_value_usd")
    baseline_value = float(baseline_value_raw) if baseline_value_raw is not None else None
    current_equity_usd = float(metrics["equity_usd"].sum(skipna=True)) if "equity_usd" in metrics else 0.0
    fallback_equity_usd = 0.0
    if baseline_value is None or baseline_value <= 0:
        for position in positions:
            rate = float(snapshot.fx_to_usd.get(position.currency, 1.0) or 1.0)
            if position.shares > 0 and position.avg_price > 0:
                fallback_equity_usd += float(position.shares) * float(position.avg_price) * rate
    if baseline_value is None or baseline_value <= 0:
        if current_equity_usd > 0:
            baseline_value = current_equity_usd
            db.set_baseline_value_usd(float(baseline_value))
        elif fallback_equity_usd > 0:
            baseline_value = fallback_equity_usd
            db.set_baseline_value_usd(float(baseline_value))
        elif baseline_value is None:
            baseline_value = 0.0

    since_start = compute_portfolio_since_start_metrics(
        tx_since_start,
        positions,
        snapshot.quotes,
        snapshot.fx_to_usd,
        baseline_date=baseline_date,
        baseline_value_usd=float(baseline_value),
        tracked_tickers=tracked_assets,
    )
    return metrics, since_start, profile


def sync_robinhood(
    sync_service: RobinhoodSyncService,
    *,
    email: str,
    password: str,
    account_number: str | None = None,
    mfa_code: str = "",
    push_only: bool = False,
) -> str:
    email = str(email or "").strip()
    password = str(password or "").strip()
    account_number = str(account_number or "").strip() or None
    if account_number and account_number.lower() == "default":
        account_number = None
    mfa_code = str(mfa_code or "").strip()

    if not email or not password:
        raise RuntimeError("Robinhood email and password are required.")

    result = sync_service.sync_transactions(
        email=email,
        password=password,
        account_number=account_number,
        mfa_callback=lambda: mfa_code,
        status_callback=None,
        push_only=push_only,
    )
    return f"Imported {result.imported_count} transactions; new assets: {', '.join(result.new_tickers) if result.new_tickers else 'none'}"


def _open_robinhood_dialog(sync_service: RobinhoodSyncService) -> None:
    if _supports_dialog():
        @st.dialog("Robinhood Credentials")
        def _dialog() -> None:
            with st.form("robinhood_credentials_form", clear_on_submit=True):
                email = st.text_input("Robinhood email", type="default", key="robinhood_email_input")
                password = st.text_input("Robinhood password", type="password", key="robinhood_password_input")
                account_number = st.text_input("Account number (optional)", key="robinhood_account_input")
                st.caption("Auth mode: Push only")
                submitted = st.form_submit_button("Sync now", width="stretch")

            if submitted:
                try:
                    message = sync_robinhood(
                        sync_service,
                        email=email,
                        password=password,
                        account_number=account_number,
                        mfa_code="",
                        push_only=True,
                    )
                    st.balloons()
                    st.success(f"✅ Sync Successful!\n\n{message}")
                    if callable(getattr(st, "toast", None)):
                        st.toast("Robinhood sync completed successfully!", icon="✅")
                    st.session_state["refresh_after_sync"] = True
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

        _dialog()
        return

    st.warning("This Streamlit version does not support modal dialogs; use the sidebar form below.")


def render_kpis(metrics: pd.DataFrame, since_start: dict[str, object]) -> None:
    total_value = float(metrics["equity_usd"].sum(skipna=True)) if "equity_usd" in metrics else 0.0
    total_pnl = float(metrics["pnl_usd"].sum(skipna=True)) if "pnl_usd" in metrics else 0.0
    cols = st.columns(4)
    cols[0].metric("Total Value", f"${abs(total_value):,.0f}")
    pnl_sign = "+" if total_pnl >= 0 else "-"
    cols[1].metric("All-Time P&L", f"{pnl_sign}${abs(total_pnl):,.0f}")
    change_pct = since_start.get("change_pct")
    cols[2].metric("Total Return", "N/A" if change_pct is None else f"{float(change_pct):+.2f}%")
    xirr_val = since_start.get("xirr")
    cols[3].metric("Portfolio XIRR", "N/A" if xirr_val is None else f"{float(xirr_val) * 100.0:+.2f}%")


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="📈", layout="wide")
    require_login()
    st.title("StackWealth")
    st.caption("Baseline-first Robinhood sync with durable first-run state.")
    
    # Initialize tax settings in session state
    if "tax_settings" not in st.session_state:
        st.session_state.tax_settings = TaxSettings()
    
    # Initialize margin override in session state
    if "margin_override" not in st.session_state:
        st.session_state.margin_override = None
    
    try:
        db, market_service, sync_service = get_services()
    except Exception as exc:
        st.error(f"Unable to initialize the portfolio store: {exc}")
        st.stop()

    profile = db.get_sync_profile()
    if profile is None:
        st.error("portfolio.json is missing or invalid, so the app cannot establish a first-run baseline.")
        st.stop()

    if not profile.get("initialized"):
        st.info("First run will use portfolio.json as the baseline universe, then sync only those assets from Robinhood.")

    with st.sidebar:
        st.subheader("Sync State")
        st.write(f"Initialized: {bool(profile.get('initialized', False))}")
        st.write(f"Baseline assets: {len(profile.get('baseline_assets') or [])}")
        st.write(f"Tracked assets: {len(profile.get('tracked_tickers') or [])}")
        st.write(f"Last sync: {profile.get('last_sync_at') or 'never'}")
        st.write(f"Version: {profile.get('sync_version', 1)}")

        st.divider()
        
        # Tax settings configuration
        st.subheader("⚙️ Tax Settings")
        with st.expander("Configure Tax Parameters", expanded=False):
            tax_settings = st.session_state.tax_settings
            
            # AGI Input
            tax_settings.agi = st.number_input(
                "Adjusted Gross Income (AGI)",
                min_value=0.0,
                value=tax_settings.agi,
                step=10000.0,
                help="Your 2024 AGI affects LTCG rate brackets and NIIT eligibility",
                key="agi_input",
            )
            
            # Filing status
            filing_options = ["single", "married_filing_jointly", "married_filing_separately", "head_of_household"]
            tax_settings.filing_status = st.selectbox(
                "Filing Status",
                filing_options,
                index=filing_options.index(tax_settings.filing_status),
                key="filing_status_input",
            )
            
            # State selection
            state_list = get_state_list()
            state_options = [s[0] for s in state_list]
            state_names = [f"{s[0]} - {s[1]}" for s in state_list]
            current_idx = state_options.index(tax_settings.state) if tax_settings.state in state_options else 0
            
            selected_state_display = st.selectbox(
                "State",
                state_names,
                index=current_idx,
                key="state_input",
            )
            tax_settings.state = selected_state_display.split(" - ")[0]
            
            # Display calculated rates
            st.divider()
            col1, col2 = st.columns(2)
            
            # Get rates
            from tabs.tax_settings import get_federal_ltcg_rate, get_state_ltcg_rate
            fed_rate = get_federal_ltcg_rate(tax_settings.agi, tax_settings.filing_status)
            state_rate = get_state_ltcg_rate(tax_settings.state)
            total_rate = fed_rate + state_rate
            
            col1.metric("Federal LTCG Rate", format_tax_rate(fed_rate))
            col2.metric("State Tax Rate", format_tax_rate(state_rate))
            
            st.metric("Combined Rate", format_tax_rate(total_rate))
            
            # No income tax states highlight
            if tax_settings.state in get_no_income_tax_states():
                st.success(f"✅ {tax_settings.state} has no state income tax!")

        st.divider()
        
        # Margin override configuration
        st.subheader("💳 Margin Override")
        actual_margin = float(profile.get("margin_balance_usd") or 0.0)
        
        use_override = st.checkbox(
            "Override Margin Balance",
            value=st.session_state.margin_override is not None,
            help="Temporarily override the margin balance for Net Portfolio Value calculation",
            key="margin_override_toggle",
        )
        
        if use_override:
            st.session_state.margin_override = st.number_input(
                "Margin Balance Override",
                min_value=0.0,
                value=st.session_state.margin_override if st.session_state.margin_override is not None else actual_margin,
                step=100.0,
                help="Enter custom margin balance amount",
                key="margin_override_input",
            )
            
            if st.session_state.margin_override != actual_margin:
                diff = st.session_state.margin_override - actual_margin
                st.metric(
                    "Difference from Actual",
                    f"${diff:,.2f}",
                    delta=f"Override: ${st.session_state.margin_override:,.2f} vs Actual: ${actual_margin:,.2f}",
                )
        else:
            st.session_state.margin_override = None
            st.caption(f"Using actual margin balance: ${actual_margin:,.2f}")
        
        # Margin diagnostics
        with st.expander("🔍 Margin Diagnostics", expanded=False):
            st.subheader("Margin Sync Status")
            
            margin_updated = profile.get("margin_updated_at")
            margin_balance = profile.get("margin_balance_usd")
            
            col1, col2 = st.columns(2)
            col1.metric("Margin Balance", f"${margin_balance:,.2f}" if margin_balance else "Not synced")
            col2.metric("Last Synced", margin_updated or "Never")
            
            if not margin_balance or margin_balance == 0.0:
                st.warning("""
                ⚠️ **Margin balance not synced or is $0**
                
                **This could mean:**
                - **SSL/TLS Connection Issue** (Streamlit Cloud) - Robinhood API blocked due to certificate handshake failure
                - First time syncing (margin fetches after login)
                - No margin is in use (account is cash-only)
                
                **Streamlit Cloud Note:**
                Streamlit Cloud may have SSL certificate issues with Robinhood's Phoenix API. The app will:
                1. Try Account Profile (most reliable) ✓
                2. Try Portfolio Profile ✓
                3. Skip Phoenix Account (may fail on SSL) ⏭️
                4. Try direct margin balance endpoints
                
                **To check if sync worked:**
                - Margin data may come from Account or Portfolio profiles
                - Check app logs for: "Updated Robinhood margin balance:"
                - If you see "Robinhood margin balance was unavailable" → no margin data found
                """)
            
            # Show raw profile values
            if st.checkbox("Show raw margin data", key="show_raw_margin"):
                st.json({
                    "margin_balance_usd": profile.get("margin_balance_usd"),
                    "margin_updated_at": profile.get("margin_updated_at"),
                })

        st.divider()
        st.subheader("Robinhood Sync")
        if st.button("Sync Robinhood", width="stretch"):
            try:
                _open_robinhood_dialog(sync_service)
            except Exception as exc:
                st.error(str(exc))

        if st.button("Refresh market data", width="stretch"):
            st.rerun()

    if st.session_state.pop("refresh_after_sync", False):
        st.rerun()

    try:
        # Use compute_dashboard to get the FX-normalized metrics (USD-aware)
        metrics, since_start, profile = compute_dashboard(db, market_service)
        _positions_for_detail, transactions_for_detail = db.load_portfolio_state()
        transactions_for_detail = exclude_seed_transactions_with_real_history(transactions_for_detail)
        portfolio_summary = metrics if not metrics.empty else metrics
        # Derive display columns expected by the UI using USD-normalized fields
        if not portfolio_summary.empty:
            portfolio_summary = portfolio_summary.copy()
            # Prefer USD-normalized per-share price when available
            if "current_price_usd" in portfolio_summary.columns:
                portfolio_summary["current_price"] = portfolio_summary["current_price_usd"]
            # Current value and open P&L in USD
            portfolio_summary["current_value"] = portfolio_summary.get("equity_usd")
            portfolio_summary["cost_basis"] = portfolio_summary.get("cost_basis")
            portfolio_summary["open_pnl"] = portfolio_summary.get("pnl_usd")
            portfolio_summary["open_pnl_margin_pct"] = (
                (portfolio_summary["open_pnl"] / portfolio_summary["cost_basis"]) * 100.0
            ).where(portfolio_summary.get("cost_basis") > 0)
            
            # Unrealized P&L % = (current_price - avg_cost) / avg_cost * 100
            portfolio_summary["unrealized_pnl_pct"] = portfolio_summary.get("unrealized_change_pct")
            
            # Total P&L % = (realized_pnl_usd + unrealized_pnl_usd) / cost_basis * 100
            realized_pnl = portfolio_summary.get("realized_pnl_usd", 0.0)
            unrealized_pnl = portfolio_summary.get("unrealized_pnl_usd", 0.0)
            cost_basis = portfolio_summary.get("cost_basis", 0.0)
            portfolio_summary["total_pnl_pct"] = (
                ((realized_pnl.fillna(0.0) + unrealized_pnl.fillna(0.0)) / cost_basis.fillna(0.0)) * 100.0
            ).where(cost_basis.fillna(0.0) > 0)
            
            # Provide market cap tier for display
            portfolio_summary["market_cap_tier"] = portfolio_summary.get("market_cap").apply(_market_cap_tier)
            # Express XIRR as percentage for table display
            portfolio_summary["xirr_pct"] = (portfolio_summary["xirr"] * 100.0).where(portfolio_summary["xirr"].notna())
            baseline_date_for_returns = date.fromisoformat(str(profile.get("baseline_date") or date.today().isoformat()))
            portfolio_summary["open_xirr_pct"] = portfolio_summary["xirr_pct"]
            portfolio_summary["lifetime_xirr_pct"] = portfolio_summary.apply(
                lambda row: (
                    lambda value: value * 100.0 if value is not None else None
                )(
                    _ticker_xirr(
                        transactions_for_detail,
                        str(row.get("ticker")),
                        float(row.get("equity_native") or 0.0),
                    )
                ),
                axis=1,
            )
            portfolio_summary["baseline_xirr_pct"] = portfolio_summary.apply(
                lambda row: (
                    lambda value: value * 100.0 if value is not None else None
                )(
                    _ticker_xirr(
                        transactions_for_detail,
                        str(row.get("ticker")),
                        float(row.get("equity_native") or 0.0),
                        start_date=baseline_date_for_returns,
                    )
                ),
                axis=1,
            )
    except Exception as exc:
        st.error(f"Unable to load holdings and market data: {exc}")
        st.stop()

    # Persist fetched market snapshot to separate cache table (non-blocking)
    try:
        if not portfolio_summary.empty:
            rows: list[dict[str, Any]] = []
            now_iso = datetime.utcnow().isoformat()
            for _, r in portfolio_summary.iterrows():
                rows.append(
                    {
                        "ticker": r.get("ticker"),
                        "current_price": float(r.get("current_price")) if r.get("current_price") is not None else None,
                        "market_cap": float(r.get("market_cap")) if r.get("market_cap") is not None else None,
                        "market_cap_tier": r.get("market_cap_tier"),
                        "fetched_at": now_iso,
                    }
                )
            try:
                db.upsert_market_snapshot_cache(rows)
            except Exception:
                LOGGER.exception("market_snapshot_cache writeback failed; continuing without blocking UI")
    except Exception:
        LOGGER.exception("preparing market snapshot cache rows failed; continuing")

    tickers = sorted({str(item).upper().strip() for item in portfolio_summary.get("ticker", pd.Series(dtype=str)).tolist()})

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "📊 Portfolio Summary",
        "📉 AI Reverse DCF",
        "📋 AI Transcript Mosaic",
        "📜 Investment Thesis",
        "💰 Tax Loss Harvesting",
        "💵 Dividend Dashboard",
        "📈 Benchmark Comparison",
    ])

    with tab1:
        st.subheader("Portfolio Summary")
        if portfolio_summary.empty:
            st.info("No active holdings found in Supabase yet.")
        else:
            total_value = float(portfolio_summary["current_value"].sum(skipna=True))
            # Use margin override if set, otherwise use actual margin balance
            margin_balance = st.session_state.margin_override if st.session_state.margin_override is not None else float(profile.get("margin_balance_usd") or 0.0)
            cash_usd = float(profile.get("cash_usd") or 0.0)
            net_portfolio_value = total_value + cash_usd - margin_balance
            total_cost = float(portfolio_summary["cost_basis"].sum(skipna=True))
            total_open_pnl = float(portfolio_summary["open_pnl"].sum(skipna=True))
            total_change_pct = ((total_open_pnl / total_cost) * 100.0) if total_cost > 0 else None
            
            # Calculate portfolio day change % as weighted average
            portfolio_summary_filled = portfolio_summary.copy()
            portfolio_summary_filled["last_day_change_pct"] = portfolio_summary_filled["last_day_change_pct"].fillna(0.0)
            portfolio_summary_filled["weight_pct"] = portfolio_summary_filled["weight_pct"].fillna(0.0)
            day_change_pct = (portfolio_summary_filled["last_day_change_pct"] * portfolio_summary_filled["weight_pct"] / 100.0).sum()

            kpi1, kpi2, kpi3, kpi4 = st.columns(4)
            margin_label = "💳 Net Portfolio Value*" if st.session_state.margin_override is not None else "Net Portfolio Value"
            kpi1.metric(margin_label, f"${net_portfolio_value:,.2f}", help="Gross holdings minus margin balance." + (" *Using override value" if st.session_state.margin_override is not None else ""))
            kpi2.metric("Margin Balance", f"${margin_balance:,.2f}")
            kpi3.metric("Total Change %", "N/A" if total_change_pct is None else f"{total_change_pct:+.2f}%")
            kpi4.metric("Day Change %", f"{day_change_pct:+.2f}%")
            margin_updated = _fmt_iso_ts(profile.get("margin_updated_at")) or "not synced"
            st.caption(f"Gross holdings: ${total_value:,.2f} | Total P&L: ${total_open_pnl:,.2f} | Margin synced: {margin_updated}")

            baseline_date_for_chart = date.fromisoformat(str(profile.get("baseline_date") or date.today().isoformat()))
            value_history = _portfolio_value_history(
                portfolio_summary,
                transactions_for_detail,
                market_service,
                baseline_date_for_chart,
                margin_balance,
                cash_usd,
            )
            if len(value_history) > 1:
                st.subheader("Portfolio Value Over Time")
                st.line_chart(value_history.set_index("Date"), height=320)

            with st.expander("Portfolio Diagnostics", expanded=False):
                top_holding = float(portfolio_summary["weight_pct"].max(skipna=True)) if "weight_pct" in portfolio_summary else 0.0
                top_five = float(portfolio_summary["weight_pct"].nlargest(5).sum(skipna=True)) if "weight_pct" in portfolio_summary else 0.0
                margin_utilization = (margin_balance / total_value * 100.0) if total_value > 0 else 0.0
                stale_count = int(portfolio_summary.get("is_stale", pd.Series(dtype=bool)).fillna(False).sum())
                d1, d2, d3, d4 = st.columns(4)
                d1.metric("Top Holding Weight", f"{top_holding:.2f}%")
                d2.metric("Top 5 Weight", f"{top_five:.2f}%")
                d3.metric("Margin Utilization", f"{margin_utilization:.2f}%")
                d4.metric("Stale Quotes", str(stale_count))
                st.json(
                    {
                        "initialized": bool(profile.get("initialized", False)),
                        "baseline_assets": len(profile.get("baseline_assets") or []),
                        "tracked_assets": len(profile.get("tracked_tickers") or []),
                        "baseline_date": profile.get("baseline_date"),
                        "last_sync_at": profile.get("last_sync_at"),
                        "margin_updated_at": profile.get("margin_updated_at"),
                    }
                )

            controls = st.container(border=True)
            with controls:
                c1, c2, c3, c4 = st.columns([1.3, 1.1, 1.3, 1.2])
                return_mode = c1.radio(
                    "Return Mode",
                    ["Open Position", "Lifetime Ticker", "Since Baseline"],
                    horizontal=True,
                    key="portfolio_return_mode",
                )
                saved_view = c2.selectbox(
                    "View",
                    ["All Holdings", "Largest Losses", "Highest XIRR", "Lowest XIRR", "Highest Allocation", "Stale Quotes"],
                    key="portfolio_saved_view",
                )
                search_text = ""
                selected_tiers = []

            xirr_column_by_mode = {
                "Open Position": "open_xirr_pct",
                "Lifetime Ticker": "lifetime_xirr_pct",
                "Since Baseline": "baseline_xirr_pct",
            }
            selected_xirr_column = xirr_column_by_mode[return_mode]
            view_summary = portfolio_summary.copy()
            view_summary["xirr_display_pct"] = view_summary[selected_xirr_column]
            if search_text.strip():
                query = search_text.strip().lower()
                view_summary = view_summary[
                    view_summary["ticker"].astype(str).str.lower().str.contains(query, na=False)
                    | view_summary["company_name"].astype(str).str.lower().str.contains(query, na=False)
                ]
            if selected_tiers and "market_cap_tier" in view_summary:
                view_summary = view_summary[view_summary["market_cap_tier"].isin(selected_tiers)]
            if saved_view == "Stale Quotes" and "is_stale" in view_summary:
                view_summary = view_summary[view_summary["is_stale"].fillna(False)]
            else:
                view_summary = _sort_portfolio_view(view_summary, saved_view, "xirr_display_pct")

            if view_summary.empty:
                st.info("No holdings match the current filters.")

            default_columns = [
                "ticker",
                "company_name",
                "shares",
                "avg_cost",
                "current_price",
                "last_day_change_pct",
                "cost_basis",
                "current_value",
                "unrealized_pnl_pct",
                "total_pnl_pct",
                "xirr_display_pct",
                "open_pnl",
                "weight_pct",
            ]
            available_columns = [column for column in default_columns + ["market_cap_tier"] if column in view_summary.columns]
            selected_columns = available_columns

            display = view_summary[selected_columns].rename(
                columns={
                    "ticker": "Ticker",
                    "company_name": "Company",
                    "shares": "Shares",
                    "avg_cost": "Avg Cost",
                    "current_price": "Current Price",
                    "last_day_change_pct": "Day Change %",
                    "cost_basis": "Cost Basis",
                    "current_value": "Current Value",
                    "unrealized_pnl_pct": "Unrealized P&L %",
                    "total_pnl_pct": "Total P&L %",
                    "xirr_display_pct": f"XIRR % ({return_mode})",
                    "open_pnl": "Total P&L",
                    "weight_pct": "Weight %",
                    "market_cap_tier": "Market Cap Tier",
                }
            )

            styler = display.style.format(
                {
                    "Shares": "{:.4f}",
                    "Avg Cost": _fmt_currency_2,
                    "Current Price": _fmt_currency_2,
                    "Day Change %": "{:+.2f}%",
                    "Cost Basis": _fmt_currency_2,
                    "Current Value": _fmt_currency_2,
                    "Unrealized P&L %": "{:+.2f}%",
                    "Total P&L %": "{:+.2f}%",
                    f"XIRR % ({return_mode})": lambda v: f"{float(v):+.2f}%" if pd.notna(v) else "N/A",
                    "Total P&L": _fmt_currency_2,
                    "Weight %": "{:.2f}%",
                }
            )

            signed_columns = [column for column in ["Total P&L", "Unrealized P&L %", "Total P&L %", f"XIRR % ({return_mode})", "Day Change %"] if column in display.columns]
            styled = styler.map(style_signed_value, subset=signed_columns)
            st.dataframe(styled, width="stretch")

            if not view_summary.empty:
                selected_detail_ticker = st.selectbox("Ticker Detail", view_summary["ticker"].tolist(), key="portfolio_detail_ticker")
                detail_row = view_summary[view_summary["ticker"] == selected_detail_ticker].iloc[0]
                detail_cols = st.columns(5)
                detail_cols[0].metric("Shares", f"{float(detail_row.get('shares') or 0.0):,.4f}")
                detail_cols[1].metric("Current Value", _fmt_currency_2(detail_row.get("current_value")))
                detail_cols[2].metric("Total P&L", _fmt_currency_2(detail_row.get("open_pnl")))
                detail_cols[3].metric("Weight", f"{float(detail_row.get('weight_pct') or 0.0):.2f}%")
                detail_cols[4].metric("XIRR", "N/A" if pd.isna(detail_row.get("xirr_display_pct")) else f"{float(detail_row.get('xirr_display_pct')):+.2f}%")
                tx_display = _transaction_rows_for_ticker(transactions_for_detail, selected_detail_ticker)
                if tx_display.empty:
                    st.info("No transactions found for the selected ticker.")
                else:
                    st.dataframe(
                        tx_display.style.format({"Shares": "{:.4f}", "Price": _fmt_currency_2, "Amount": _fmt_currency_2}),
                        width="stretch",
                    )

            if st.button("Run AI Overview", key="run_ai_overview", width="stretch"):
                try:
                    with st.spinner("Building portfolio payload..."):
                        payload = build_portfolio_overview_input(metrics, portfolio_summary, profile, since_start)
                    with st.spinner("Generating portfolio overview (LLM)..."):
                        report = generate_portfolio_ai_overview(payload)
                    st.markdown(report)
                    # persist AI report
                    try:
                        db.insert_ai_analysis_report(
                            ticker="PORTFOLIO",
                            analysis_type="portfolio_overview",
                            model=get_model_name(),
                            prompt_summary=None,
                            report_md=report,
                            inputs=payload,
                            run_by=None,
                        )
                    except Exception:
                        LOGGER.exception("Failed to persist portfolio overview report; continuing.")
                except Exception as exc:
                    st.error(f"AI Overview generation failed: {exc}")

            # --- Show cached portfolio overview report below the button ---
            try:
                cached_portfolio_overview = db.get_latest_ai_report("PORTFOLIO", "portfolio_overview")
            except Exception:
                cached_portfolio_overview = None

            if cached_portfolio_overview and cached_portfolio_overview.get("report_md"):
                ts = _fmt_iso_ts(cached_portfolio_overview.get("created_at"))
                title = "Latest Saved AI Overview" + (f" — Generated {ts}" if ts else "")
                # compute lightweight current hash to detect staleness without expensive enrichment
                current_brief_hash = _brief_portfolio_hash(metrics, portfolio_summary, profile)
                saved_inputs = cached_portfolio_overview.get("inputs") or {}
                saved_hash = saved_inputs.get("portfolio_hash") if isinstance(saved_inputs, dict) else None
                if saved_hash and current_brief_hash and saved_hash != current_brief_hash:
                    title += " — Stale (snapshot changed)"
                st.subheader(title)
                st.markdown(cached_portfolio_overview.get("report_md") or "")

        st.subheader("Status")
        st.json(
            {
                "initialized": bool(profile.get("initialized", False)),
                "baseline_assets": profile.get("baseline_assets") or [],
                "tracked_assets": profile.get("tracked_tickers") or [],
                "baseline_date": profile.get("baseline_date"),
                "last_sync_at": profile.get("last_sync_at"),
            }
        )

    with tab2:
        st.subheader("AI Reverse DCF")
        if not tickers:
            st.info("No active tickers are available for analysis.")
        else:
            selected_ticker = st.selectbox("Select ticker", options=tickers, key="reverse_dcf_ticker")
            if st.button("Run Reverse DCF Analysis", key="run_reverse_dcf", width="stretch"):
                try:
                    with st.spinner("Running reverse DCF analysis..."):
                        report = get_reverse_dcf_analysis(selected_ticker)
                    st.markdown(report)
                    # persist AI report (best-effort)
                    try:
                        # gather simple inputs from cached portfolio summary if available
                        inputs = {"ticker": selected_ticker}
                        if not portfolio_summary.empty and selected_ticker in portfolio_summary["ticker"].values:
                            row = portfolio_summary[portfolio_summary["ticker"] == selected_ticker].iloc[0]
                            inputs.update(
                                {
                                    "current_price": float(row.get("current_price")) if row.get("current_price") is not None else None,
                                    "market_cap": float(row.get("market_cap")) if row.get("market_cap") is not None else None,
                                }
                            )
                        try:
                            db.insert_ai_analysis_report(
                                ticker=selected_ticker,
                                analysis_type="reverse_dcf",
                                model=get_model_name(),
                                prompt_summary=None,
                                report_md=report,
                                inputs=inputs,
                                run_by=None,
                            )
                        except Exception:
                            LOGGER.exception("Failed to persist reverse dcf report; continuing.")
                    except Exception:
                        LOGGER.exception("Preparing inputs for reverse dcf report failed; continuing.")
                except Exception as exc:
                    st.error(f"Reverse DCF analysis failed: {exc}")

    with tab3:
        st.subheader("AI Transcript Mosaic")
        mosaic_ticker = st.text_input("Ticker", key="mosaic_ticker").upper().strip()
        cached_mosaic = None
        latest_transcript = None

        if mosaic_ticker:
            try:
                cached_mosaic = db.get_latest_ai_report(mosaic_ticker, "transcript_mosaic")
            except Exception:
                LOGGER.exception("Failed to load cached transcript mosaic report")

            try:
                latest_transcript = db.get_latest_transcript(mosaic_ticker)
            except Exception:
                LOGGER.exception("Failed to load latest transcript for transcript mosaic")

        if cached_mosaic and cached_mosaic.get("report_md"):
            ts = _fmt_iso_ts(cached_mosaic.get("created_at"))
            title = "Latest Saved Transcript Mosaic" + (f" — Generated {ts}" if ts else "")
            st.subheader(title)
            st.markdown(cached_mosaic.get("report_md") or "")

        transcript_file = st.file_uploader("Upload earnings transcript (.txt)", type=["txt"], key="mosaic_file")

        uploaded_transcript_text = ""
        if transcript_file is not None:
            try:
                uploaded_transcript_text = transcript_file.getvalue().decode("utf-8", errors="ignore")
            except Exception:
                uploaded_transcript_text = ""

        latest_transcript_text = ""
        if latest_transcript and latest_transcript.get("content"):
            latest_transcript_text = str(latest_transcript.get("content") or "").strip()

        can_process = bool(mosaic_ticker and (uploaded_transcript_text.strip() or latest_transcript_text))
        if st.button("Run Transcript Mosaic Analysis", key="run_mosaic", disabled=not can_process, width="stretch"):
            try:
                transcript_text = uploaded_transcript_text.strip() or latest_transcript_text
                if not transcript_text.strip():
                    st.error("Upload a transcript or save one for this ticker before running the analysis.")
                else:
                    # persist uploaded transcript first (best-effort)
                    transcript_id = None
                    if transcript_file is not None and uploaded_transcript_text.strip():
                        try:
                            transcript_id = db.insert_transcript(
                                ticker=mosaic_ticker or None,
                                filename=getattr(transcript_file, "name", None),
                                content=uploaded_transcript_text,
                                source="upload",
                            )
                        except Exception:
                            LOGGER.exception("Failed to persist uploaded transcript; continuing without transcript id.")

                    with st.spinner("Running transcript mosaic analysis..."):
                        report = get_transcript_mosaic_analysis(mosaic_ticker, transcript_text)
                    st.markdown(report)

                    # persist AI report (best-effort)
                    try:
                        inputs = {"ticker": mosaic_ticker, "transcript_id": transcript_id}
                        db.insert_ai_analysis_report(
                            ticker=mosaic_ticker,
                            analysis_type="transcript_mosaic",
                            model=get_model_name(),
                            prompt_summary=None,
                            report_md=report,
                            inputs=inputs,
                            run_by=None,
                        )
                    except Exception:
                        LOGGER.exception("Failed to persist transcript mosaic report; continuing.")
            except Exception as exc:
                st.error(f"Transcript mosaic analysis failed: {exc}")

    with tab4:
        st.subheader("Investment Thesis")

        if portfolio_summary.empty:
            st.error("No active holdings found in Supabase yet. Cannot generate thesis.")
        else:
            tickers = sorted({str(item).upper().strip() for item in portfolio_summary.get("ticker", pd.Series(dtype=str)).tolist()})
            selected_ticker = st.selectbox("Select ticker", options=tickers, key="thesis_ticker")

            # Compute allocation details before running AI
            row = None
            try:
                if not portfolio_summary.empty and selected_ticker in portfolio_summary["ticker"].values:
                    row = portfolio_summary[portfolio_summary["ticker"] == selected_ticker].iloc[0]
            except Exception:
                row = None

            if row is None:
                st.error("Selected ticker not found in portfolio summary.")
            else:
                shares = float(row.get("shares") or 0.0)
                avg_cost = float(row.get("avg_cost") or 0.0)
                current_value = float(row.get("current_value") or 0.0)
                total_portfolio_value = float(portfolio_summary["current_value"].sum(skipna=True)) if "current_value" in portfolio_summary else 0.0
                portfolio_weight_pct = (current_value / total_portfolio_value * 100.0) if total_portfolio_value > 0 else 0.0

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Shares", f"{shares:,.4f}")
                c2.metric("Avg Cost Basis", f"${avg_cost:,.2f}")
                c3.metric("Current Value", f"${current_value:,.2f}")
                c4.metric("Portfolio Weight", f"{portfolio_weight_pct:.2f}%")

                # Show cached AI artifacts (if any) for this ticker
                try:
                    cached_thesis = db.get_latest_ai_report(selected_ticker, "investment_thesis")
                except Exception:
                    cached_thesis = None

                try:
                    cached_dcf = db.get_latest_ai_report(selected_ticker, "reverse_dcf")
                except Exception:
                    cached_dcf = None

                try:
                    cached_mosaic = db.get_latest_ai_report(selected_ticker, "transcript_mosaic")
                except Exception:
                    cached_mosaic = None

                try:
                    latest_transcript = db.get_latest_transcript(selected_ticker)
                except Exception:
                    latest_transcript = None

                if cached_thesis and cached_thesis.get("report_md"):
                    ts = _fmt_iso_ts(cached_thesis.get("created_at"))
                    title = "Cached Investment Thesis" + (f" — Generated {ts}" if ts else "")
                    st.subheader(title)
                    st.markdown(cached_thesis.get("report_md") or "")
                if cached_dcf and cached_dcf.get("report_md"):
                    ts = _fmt_iso_ts(cached_dcf.get("created_at"))
                    title = "Cached Reverse DCF" + (f" — Generated {ts}" if ts else "")
                    st.subheader(title)
                    st.markdown(cached_dcf.get("report_md") or "")
                if cached_mosaic and cached_mosaic.get("report_md"):
                    ts = _fmt_iso_ts(cached_mosaic.get("created_at"))
                    title = "Cached Transcript Mosaic" + (f" — Generated {ts}" if ts else "")
                    st.subheader(title)
                    st.markdown(cached_mosaic.get("report_md") or "")
                if latest_transcript and latest_transcript.get("content"):
                    ts = _fmt_iso_ts(latest_transcript.get("uploaded_at"))
                    title = "Latest Uploaded Transcript" + (f" — Uploaded {ts}" if ts else "")
                    st.subheader(title)
                    st.text_area("Transcript (cached)", latest_transcript.get("content") or "", height=180)

                past_transcript_file = st.file_uploader(
                    "Upload Baseline/Past Transcript (optional, e.g., 1-2 Years Ago)",
                    type=["txt"],
                    key="past_tx",
                )
                current_transcript_file = st.file_uploader(
                    "Upload Current/Latest Transcript (optional)",
                    type=["txt"],
                    key="current_tx",
                )

                st.caption("Transcript uploads are optional. If none are provided, the thesis will use the financial profile and portfolio context only.")

                past_tx_text = ""
                current_tx_text = ""
                if past_transcript_file is not None:
                    try:
                        past_tx_text = past_transcript_file.getvalue().decode("utf-8", errors="ignore")
                    except Exception:
                        past_tx_text = ""
                if current_transcript_file is not None:
                    try:
                        current_tx_text = current_transcript_file.getvalue().decode("utf-8", errors="ignore")
                    except Exception:
                        current_tx_text = ""

                allocation_details = {
                    "shares": shares,
                    "avg_cost": avg_cost,
                    "portfolio_weight_pct": portfolio_weight_pct,
                    "current_value": current_value,
                }

                if st.button("Generate Institutional Thesis", key="gen_thesis", width="stretch"):
                    try:
                        with st.spinner("Generating investment thesis..."):
                            financial_profile = fetch_fmp_financial_profile(selected_ticker)
                            report = generate_comparative_investment_thesis(
                                selected_ticker,
                                allocation_details,
                                financial_profile,
                                past_tx_text,
                                current_tx_text,
                            )
                        st.markdown(report)

                        # persist AI report (best-effort)
                        try:
                            inputs = {
                                "ticker": selected_ticker,
                                "allocation": allocation_details,
                                "has_past_transcript": bool(past_tx_text.strip()),
                                "has_current_transcript": bool(current_tx_text.strip()),
                            }
                            db.insert_ai_analysis_report(
                                ticker=selected_ticker,
                                analysis_type="investment_thesis",
                                model=get_model_name(),
                                prompt_summary=None,
                                report_md=report,
                                inputs=inputs,
                                run_by=None,
                            )
                        except Exception:
                            LOGGER.exception("Failed to persist investment thesis report; continuing.")
                    except Exception as exc:
                        # Friendly message for transient Gemini/service overloads
                        msg = str(exc).lower()
                        LOGGER.exception("FMP thesis generation failed for %s", selected_ticker)
                        if "missing from streamlit secrets or environment" in msg:
                            st.error("FMP_API_KEY is not configured. Add it to Streamlit secrets or the environment.")
                        elif "appears invalid or unauthorized" in msg:
                            st.error("FMP API key is missing or invalid. Please set `FMP_API_KEY` in Streamlit secrets.")
                        elif "empty data" in msg or "endpoint" in msg:
                            st.error(f"Could not load FMP financial statements for {selected_ticker}. Please verify ticker/API key and try again.")
                        elif any(k in msg for k in ("yfinance", "yahoo", "rate limited", "too many requests")):
                            st.error(
                                "Yahoo Finance rate-limited the fallback market-data lookup. Try again later or pick a ticker supported by FMP."
                            )
                        elif any(k in msg for k in ("503", "unavailable", "high demand", "too many requests", "rate limit", "429")):
                            st.error(
                                "AI service temporarily overloaded (503/rate-limit). Try again in a few minutes. "
                                "You can also set a different fallback model via the GEMINI_MODEL Streamlit secret."
                            )
                            st.info("For reliability, try again after a short wait or reduce request frequency.")
                        elif "current transcript missing" in msg:
                            pass
                        else:
                            st.error(f"Investment thesis generation failed: {exc}")

    # ===== TAB 5: TAX LOSS HARVESTING =====
    with tab5:
        st.subheader("💰 Tax Loss Harvesting")
        st.markdown("""
        Identify positions with unrealized losses that can be harvested for tax benefits.
        
        **Key Concepts:**
        - **Tax Loss Harvesting**: Sell losing positions to offset capital gains elsewhere
        - **Wash-Sale Rule**: Wait 30+ days before repurchasing the same or substantially identical security
        - **Tax-Loss Carryforward**: Unused losses can offset future gains (up to $3,000/year of ordinary income)
        """)
        
        if portfolio_summary.empty:
            st.info("No holdings available for analysis.")
        else:
            # Identify candidates
            candidates = identify_tax_loss_harvesting_candidates(portfolio_summary)
            
            if candidates.empty:
                st.success("✅ No positions with losses. Great job!")
            else:
                st.warning(f"⚠️ Found {len(candidates)} position(s) with unrealized losses")
                
                # Display candidates
                st.subheader("Harvesting Candidates")
                candidates_display = candidates.copy()
                styler = candidates_display.style.format({
                    "Current Value": "${:,.2f}",
                    "Loss Amount": "${:,.2f}",
                    "Loss %": "{:.2f}%",
                    "Portfolio Weight %": "{:.2f}%",
                })
                st.dataframe(styler, use_container_width=True)
                
                # Tax impact analysis with user's configured settings
                st.subheader("Tax Impact Analysis")
                tax_metrics = calculate_tax_impact(portfolio_summary, st.session_state.tax_settings)
                
                c1, c2, c3, c4 = st.columns(4)
                c1.metric(
                    "Total Unrealized Gains",
                    f"${tax_metrics['total_unrealized_gain']:,.2f}",
                )
                c2.metric(
                    "Total Unrealized Losses",
                    f"${tax_metrics['total_unrealized_loss']:,.2f}",
                )
                c3.metric(
                    "Est. Total Tax",
                    f"${tax_metrics['estimated_tax_on_gains']:,.2f}",
                    help=f"Federal: ${tax_metrics['federal_tax']:,.0f} + State: ${tax_metrics['state_tax']:,.0f} + NIIT: ${tax_metrics['niit_amount']:,.0f}",
                )
                c4.metric(
                    "Harvest Tax Benefit",
                    f"${tax_metrics['tax_loss_harvesting_benefit']:,.2f}",
                    delta=f"Effective tax rate: {tax_metrics['effective_tax_rate']:.2f}%",
                )
                
                # Tax breakdown
                st.caption(f"""
                Tax Breakdown: Federal {format_tax_rate(tax_metrics['federal_rate'])} + 
                State {format_tax_rate(tax_metrics['state_rate'])} 
                {f"+ NIIT {format_tax_rate(3.8/100)}" if tax_metrics['niit_amount'] > 0 else ""}
                """)
                
                # Harvesting strategy
                st.subheader("Harvesting Strategy")
                if candidates.iloc[0]["Loss Amount"] > 0:
                    top_candidate = candidates.iloc[0]
                    tax_benefit = top_candidate["Loss Amount"] * (tax_metrics['federal_rate'] + tax_metrics['state_rate'])
                    st.markdown(f"""
                    **Top Harvesting Candidate: {top_candidate['Ticker']}**
                    - **Company**: {top_candidate['Company']}
                    - **Loss Amount**: ${top_candidate['Loss Amount']:,.2f}
                    - **Loss %**: {top_candidate['Loss %']:.2f}%
                    - **Tax Benefit** ({format_tax_rate(tax_metrics['federal_rate'] + tax_metrics['state_rate'])} combined): ${tax_benefit:,.2f}
                    
                    {get_wash_sale_alert(top_candidate['Ticker'], portfolio_summary)}
                    """)
                
                # Harvesting calculator
                st.subheader("Harvesting Calculator")
                selected_harvest_ticker = st.selectbox(
                    "Select position to harvest",
                    candidates["Ticker"].tolist(),
                    key="harvest_ticker",
                )
                
                if selected_harvest_ticker:
                    harvest_row = candidates[candidates["Ticker"] == selected_harvest_ticker].iloc[0]
                    tax_rate = tax_metrics['federal_rate'] + tax_metrics['state_rate']
                    
                    tax_benefit = harvest_row["Loss Amount"] * tax_rate
                    
                    st.markdown(f"""
                    **Harvest Summary for {harvest_row['Ticker']}**
                    - **Realized Loss**: ${harvest_row['Loss Amount']:,.2f}
                    - **Your Tax Rate**: {format_tax_rate(tax_rate)} 
                      (Federal: {format_tax_rate(tax_metrics['federal_rate'])} + State: {format_tax_rate(tax_metrics['state_rate'])})
                    - **Tax Benefit**: ${tax_benefit:,.2f}
                    
                    **Your Tax Profile:**
                    - **AGI**: ${st.session_state.tax_settings.agi:,.0f}
                    - **Filing Status**: {st.session_state.tax_settings.filing_status}
                    - **State**: {st.session_state.tax_settings.state}
                    
                    **Next Steps:**
                    1. Harvest loss by selling position
                    2. Wait 30+ days (wash-sale period)
                    3. Reinvest in similar but non-identical security
                    4. Track for tax reporting (Schedule D)
                    """)
            
            # Diversification suggestion
            st.subheader("Reinvestment Ideas")
            st.markdown("""
            After harvesting, consider these alternatives to avoid wash-sale violations while maintaining exposure:
            
            | If You Sold | Consider Instead |
            |---|---|
            | VOO (Vanguard S&P 500) | SPY, IVV (similar index funds) |
            | QQQ (Nasdaq-100) | VUG, XLK (growth/tech sector funds) |
            | Individual stocks | Sector ETFs or similar-cap alternatives |
            """)

    # ===== TAB 6: DIVIDEND DASHBOARD =====
    with tab6:
        st.subheader("💵 Dividend Dashboard")
        st.markdown("""
        Track dividend income across your portfolio. Use this to monitor passive income generation.
        """)
        
        if portfolio_summary.empty:
            st.info("No holdings available for analysis.")
        else:
            # Calculate dividend projections
            dividends = calculate_dividend_projections(portfolio_summary)
            div_summary = get_dividend_summary(dividends)
            
            # Summary metrics
            st.subheader("Dividend Summary")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric(
                "Annual Dividend Income",
                f"${div_summary['total_annual_dividend']:,.2f}",
            )
            m2.metric(
                "Monthly Income (avg)",
                f"${div_summary['monthly_income']:,.2f}",
            )
            m3.metric(
                "Blended Yield",
                f"{div_summary['blended_yield']:.2f}%",
            )
            m4.metric(
                "Dividend Payers",
                f"{div_summary['dividend_count']}",
            )
            
            if dividends.empty:
                st.info("📊 No dividend-paying positions currently in portfolio.")
                st.markdown("""
                **Income-focused investors** might consider adding:
                - Dividend aristocrats (25+ years of increases)
                - High-yield dividend ETFs
                - REITs for stable income
                - Preferred stocks for consistent distributions
                """)
            else:
                # Dividend by position
                st.subheader("Income by Position")
                div_display = dividends.copy()
                styler = div_display.style.format({
                    "Shares": "{:.4f}",
                    "Current Price": "${:,.2f}",
                    "Position Value": "${:,.2f}",
                    "Dividend Yield %": "{:.2f}%",
                    "Annual Dividend $": "${:,.2f}",
                    "Quarterly Estimate": "${:,.2f}",
                })
                st.dataframe(styler, use_container_width=True)
                
                # Dividend contribution
                st.subheader("Income Contribution by Position")
                chart_data = dividends.set_index("Ticker")["Annual Dividend $"]
                st.bar_chart(chart_data, height=300)
                
                # Income timeline
                st.subheader("Projected Monthly Income")
                monthly_projection = div_summary["total_annual_dividend"] / 12
                st.metric(
                    "Average Monthly Dividend Income",
                    f"${monthly_projection:,.2f}",
                    help="Assumes consistent dividend payments throughout the year",
                )
                
                # Income growth strategy
                st.subheader("Income Growth Strategy")
                current_income = div_summary["total_annual_dividend"]
                target_income = st.slider(
                    "Target Annual Dividend Income",
                    int(current_income) if current_income > 0 else 1000,
                    int(current_income * 2) if current_income > 0 else 5000,
                    step=500,
                    key="dividend_target",
                )
                
                income_gap = target_income - current_income
                if income_gap > 0:
                    avg_yield = div_summary["blended_yield"] / 100
                    if avg_yield > 0:
                        capital_needed = income_gap / avg_yield
                        st.markdown(f"""
                        **To reach ${target_income:,.2f} annual income:**
                        - **Capital Gap**: ${capital_needed:,.2f}
                        - **At current blended yield** ({div_summary['blended_yield']:.2f}%)
                        - Consider adding dividend stocks or shifting portfolio allocation
                        """)

    # ===== TAB 7: BENCHMARK COMPARISON =====
    with tab7:
        st.subheader("📈 Benchmark Comparison")
        st.markdown("""
        Compare your portfolio returns against major market indices to assess performance and alpha generation.
        """)
        
        if portfolio_summary.empty or not transactions_for_detail:
            st.info("Insufficient data for benchmark comparison.")
        else:
            baseline_date_for_benchmark = date.fromisoformat(str(profile.get("baseline_date") or date.today().isoformat()))
            
            # Benchmark comparison
            benchmark_df = benchmark_portfolio_returns(
                portfolio_summary,
                transactions_for_detail,
                baseline_date_for_benchmark,
                indices=["SPY", "QQQ", "VXUS"],
            )
            
            bench_summary = get_benchmark_summary(benchmark_df)
            
            # Summary metrics
            st.subheader("Performance vs Benchmarks")
            p1, p2, p3, p4 = st.columns(4)
            
            p1.metric(
                "Your Portfolio Return",
                f"{bench_summary['portfolio_return']:+.2f}%",
                delta="Performance" if bench_summary['outperformance'] else "Underperformance",
            )
            p2.metric(
                "Avg Benchmark Return",
                f"{bench_summary['avg_benchmark_return']:+.2f}%",
            )
            p3.metric(
                "Alpha (Over-/Underperformance)",
                f"{bench_summary['alpha']:+.2f}%",
                delta="Outperforming" if bench_summary['outperformance'] else "Underperforming",
            )
            p4.metric(
                "Baseline Period",
                baseline_date_for_benchmark.strftime("%b %d, %Y"),
            )
            
            # Benchmark table
            st.subheader("Detailed Comparison")
            bench_display = benchmark_df.copy()
            bench_display["Return %"] = bench_display["Return %"].apply(lambda x: f"{x:+.2f}%")
            st.dataframe(bench_display, use_container_width=True, hide_index=True)
            
            # Performance chart
            st.subheader("Return Comparison")
            chart_data = benchmark_df.set_index("Ticker")["Return %"].str.rstrip('%').astype(float)
            st.bar_chart(chart_data, height=300)
            
            # Analysis
            st.subheader("Performance Analysis")
            
            if bench_summary['outperformance']:
                st.success(f"""
                ✅ **Outperforming by {bench_summary['alpha']:.2f}%**
                
                Your portfolio is beating the average benchmark return. This could indicate:
                - Strong stock-picking decisions
                - Good market timing
                - Successful tactical allocation shifts
                - Sector or style advantages
                """)
            else:
                st.warning(f"""
                ⚠️ **Underperforming by {abs(bench_summary['alpha']):.2f}%**
                
                Your portfolio is trailing the average benchmark. Consider:
                - Review position sizing and allocation
                - Analyze which holdings are dragging performance
                - Consider rebalancing toward underweight sectors
                - Evaluate if fees/costs are impacting returns
                """)
            
            # Peer comparison context
            st.subheader("Benchmark Information")
            st.markdown("""
            | Benchmark | Ticker | Coverage |
            |---|---|---|
            | S&P 500 | SPY | Large-cap US equities |
            | Nasdaq-100 | QQQ | Growth & tech-heavy |
            | Intl Stocks | VXUS | Developed & emerging markets |
            
            **Note**: Alpha calculation is a simple comparison. True alpha analysis requires risk-adjustment (Sharpe ratio, beta).
            """)

if __name__ == "__main__":
    main()