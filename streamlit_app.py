from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from alphavault.cache_store import CacheStore
from alphavault.finance_engine import build_metrics_table, compute_portfolio_since_start_metrics
from alphavault.logging_utils import configure_logging
from alphavault.market_data import MarketDataService
from alphavault.robinhood_sync import RobinhoodSyncService
from alphavault.sqlite_store import SQLiteStore


configure_logging()
LOGGER = logging.getLogger(__name__)

APP_TITLE = "StackWealth"
DB_PATH = Path("data/alphavault.db")
PORTFOLIO_JSON = Path("data/portfolio.json")
CACHE_PATH = Path("cache/market_cache.json")


def _secrets_section(name: str) -> dict[str, str]:
    raw = st.secrets.get(name, {}) if hasattr(st, "secrets") else {}
    return raw if isinstance(raw, dict) else {}


def _secret_value(*keys: str) -> str:
    for key in keys:
        value = st.secrets.get(key) if hasattr(st, "secrets") else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@st.cache_resource(show_spinner=False)
def get_services() -> tuple[SQLiteStore, MarketDataService, RobinhoodSyncService]:
    db = SQLiteStore(DB_PATH)
    db.seed_from_json(PORTFOLIO_JSON)
    db.bootstrap_sync_profile_from_portfolio_json(PORTFOLIO_JSON)
    cache = CacheStore(CACHE_PATH)
    market_service = MarketDataService(cache=cache)
    sync_service = RobinhoodSyncService(db, market_service)
    return db, market_service, sync_service


def compute_dashboard(db: SQLiteStore, market_service: MarketDataService) -> tuple[pd.DataFrame, dict[str, object], dict[str, object]]:
    profile = db.get_sync_profile() or db.bootstrap_sync_profile_from_portfolio_json(PORTFOLIO_JSON)
    positions, transactions = db.load_portfolio_state()
    baseline_date = date.fromisoformat(str(profile.get("baseline_date") or date.today().isoformat()))
    tracked_assets = set(profile.get("tracked_tickers") or profile.get("baseline_assets") or [])
    tx_for_metrics = [
        tx
        for tx in transactions
        if tx.tx_date >= baseline_date and (not tracked_assets or tx.ticker in tracked_assets)
    ]

    tickers = [position.ticker for position in positions]
    currencies = [position.currency for position in positions]
    snapshot = market_service.refresh_snapshot(tickers=tickers, currencies=currencies)
    metrics = build_metrics_table(
        positions=positions,
        transactions=tx_for_metrics,
        quotes=snapshot.quotes,
        fx_to_usd=snapshot.fx_to_usd,
        stale_tickers=snapshot.stale_tickers,
    )
    db.update_market_snapshot(metrics.to_dict(orient="records"))

    baseline_value = profile.get("baseline_value_usd")
    if baseline_value is None:
        baseline_value = float(metrics["equity_usd"].sum(skipna=True)) if not metrics.empty else 0.0
        db.set_baseline_value_usd(float(baseline_value))

    since_start = compute_portfolio_since_start_metrics(
        tx_for_metrics,
        positions,
        snapshot.quotes,
        snapshot.fx_to_usd,
        baseline_date=baseline_date,
        baseline_value_usd=float(baseline_value),
        tracked_tickers=tracked_assets,
    )
    return metrics, since_start, profile


def sync_robinhood(sync_service: RobinhoodSyncService) -> str:
    robinhood = _secrets_section("robinhood")
    email = robinhood.get("email") or _secret_value("robinhood_email", "ROBINHOOD_EMAIL")
    password = robinhood.get("password") or _secret_value("robinhood_password", "ROBINHOOD_PASSWORD")
    account_number = robinhood.get("account_number") or _secret_value("robinhood_account_number", "ROBINHOOD_ACCOUNT_NUMBER")
    mfa_code = st.session_state.get("mfa_code", "").strip()

    if not email or not password:
        raise RuntimeError("Set Robinhood credentials in Streamlit secrets before syncing.")

    result = sync_service.sync_transactions(
        email=email,
        password=password,
        account_number=account_number or None,
        mfa_callback=lambda: mfa_code,
        status_callback=lambda message: st.session_state.__setitem__("sync_status", message),
    )
    return f"Imported {result.imported_count} transactions; new assets: {', '.join(result.new_tickers) if result.new_tickers else 'none'}"


def render_kpis(metrics: pd.DataFrame, since_start: dict[str, object]) -> None:
    total_value = float(metrics["equity_usd"].sum(skipna=True)) if "equity_usd" in metrics else 0.0
    total_pnl = float(metrics["pnl_usd"].sum(skipna=True)) if "pnl_usd" in metrics else 0.0
    cols = st.columns(4)
    cols[0].metric("Total Value", f"${total_value:,.2f}")
    cols[1].metric("All-Time P&L", f"${total_pnl:,.2f}")
    xirr = since_start.get("xirr")
    change_pct = since_start.get("change_pct")
    cols[2].metric("XIRR", "N/A" if xirr is None else f"{float(xirr) * 100:,.2f}%")
    cols[3].metric("Change", "N/A" if change_pct is None else f"{float(change_pct):,.2f}%")


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="📈", layout="wide")
    st.title("StackWealth")
    st.caption("Baseline-first Robinhood sync with durable first-run state.")

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
        st.subheader("Robinhood Sync")
        st.text_input("MFA code", key="mfa_code", type="password")
        if st.button("Sync Robinhood", use_container_width=True):
            try:
                message = sync_robinhood(sync_service)
                st.success(message)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

        if st.button("Refresh market data", use_container_width=True):
            st.rerun()

    auto_sync_enabled = not profile.get("initialized") and not st.session_state.get("auto_sync_attempted")
    if auto_sync_enabled:
        st.session_state["auto_sync_attempted"] = True
        try:
            message = sync_robinhood(sync_service)
            st.success(message)
            st.rerun()
        except Exception as exc:
            st.warning(f"Automatic first-run sync was skipped or failed: {exc}")

    try:
        metrics, since_start, profile = compute_dashboard(db, market_service)
    except Exception as exc:
        st.error(f"Unable to compute portfolio metrics: {exc}")
        st.stop()

    render_kpis(metrics, since_start)

    st.subheader("Holdings")
    if metrics.empty:
        st.info("No holdings found yet.")
    else:
        display_columns = [
            "ticker",
            "company_name",
            "shares",
            "avg_price",
            "current_price",
            "equity_usd",
            "realized_pnl_usd",
            "unrealized_pnl_usd",
            "pnl_usd",
            "change_pct",
            "xirr",
        ]
        available_columns = [column for column in display_columns if column in metrics.columns]
        st.dataframe(metrics[available_columns].sort_values(by="equity_usd", ascending=False), use_container_width=True)

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


if __name__ == "__main__":
    main()