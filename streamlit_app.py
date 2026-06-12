from __future__ import annotations

import hmac
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yfinance as yf
import json

from alphavault.cache_store import CacheStore
from alphavault.finance_engine import build_metrics_table, compute_portfolio_since_start_metrics
from alphavault.postgres_store import PostgresStore
from alphavault.logging_utils import configure_logging
from alphavault.market_data import MarketDataService
from alphavault.robinhood_sync import RobinhoodSyncService
from tabs.ai_analysis import (
    get_reverse_dcf_analysis,
    get_transcript_mosaic_analysis,
    get_model_name,
    generate_single_investment_thesis,
)


configure_logging()
LOGGER = logging.getLogger(__name__)

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


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_market_snapshot(tickers: tuple[str, ...]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        symbol = str(ticker).upper().strip()
        if not symbol:
            continue
        instrument = yf.Ticker(symbol)
        info = instrument.info or {}
        fast_info = getattr(instrument, "fast_info", {}) or {}
        price = _safe_float(fast_info.get("lastPrice"))
        if price is None:
            price = _safe_float(info.get("currentPrice"))
        market_cap = _safe_float(info.get("marketCap"))
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
    return merged.sort_values(by="current_value", ascending=False, na_position="last")


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
        tx_for_metrics,
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
    cols = st.columns(3)
    cols[0].metric("Total Value", f"${abs(total_value):,.0f}")
    pnl_sign = "+" if total_pnl >= 0 else "-"
    cols[1].metric("All-Time P&L", f"{pnl_sign}${abs(total_pnl):,.0f}")
    change_pct = since_start.get("change_pct")
    cols[2].metric("Change", "N/A" if change_pct is None else f"{float(change_pct):+.2f}%")


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="📈", layout="wide")
    require_login()
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
            # Provide market cap tier for display
            portfolio_summary["market_cap_tier"] = portfolio_summary.get("market_cap").apply(_market_cap_tier)
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

    tab1, tab2, tab3, tab4 = st.tabs(["📊 Portfolio Summary", "📉 AI Reverse DCF", "📋 AI Transcript Mosaic", "📜 Investment Thesis"])

    with tab1:
        st.subheader("Portfolio Summary")
        if portfolio_summary.empty:
            st.info("No active holdings found in Supabase yet.")
        else:
            total_value = float(portfolio_summary["current_value"].sum(skipna=True))
            total_cost = float(portfolio_summary["cost_basis"].sum(skipna=True))
            total_open_pnl = float(portfolio_summary["open_pnl"].sum(skipna=True))
            pnl_margin = ((total_open_pnl / total_cost) * 100.0) if total_cost > 0 else None

            kpi1, kpi2, kpi3 = st.columns(3)
            kpi1.metric("Current Value", f"${total_value:,.2f}")
            kpi2.metric("Open P&L", f"${total_open_pnl:,.2f}")
            kpi3.metric("Open P&L Margin", "N/A" if pnl_margin is None else f"{pnl_margin:+.2f}%")

            display = portfolio_summary[
                [
                    "ticker",
                    "company_name",
                    "shares",
                    "avg_cost",
                    "cost_basis",
                    "current_price",
                    "current_value",
                    "open_pnl",
                    "open_pnl_margin_pct",
                    "weight_pct",
                    "market_cap_tier",
                ]
            ].rename(
                columns={
                    "ticker": "Ticker",
                    "company_name": "Company",
                    "shares": "Shares",
                    "avg_cost": "Avg Cost",
                    "cost_basis": "Cost Basis",
                    "current_price": "Current Price",
                    "current_value": "Current Value",
                    "open_pnl": "Open P&L",
                    "open_pnl_margin_pct": "Open P&L Margin %",
                    "weight_pct": "Weight %",
                    "market_cap_tier": "Market Cap Tier",
                }
            )

            styler = display.style.format(
                {
                    "Shares": "{:.4f}",
                    "Avg Cost": _fmt_currency_2,
                    "Cost Basis": _fmt_currency_2,
                    "Current Price": _fmt_currency_2,
                    "Current Value": _fmt_currency_2,
                    "Open P&L": _fmt_currency_2,
                    "Open P&L Margin %": "{:+.2f}%",
                    "Weight %": "{:.2f}%",
                }
            )

            styled = styler.map(style_signed_value, subset=["Open P&L", "Open P&L Margin %"])
            st.dataframe(styled, width="stretch")

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
        transcript_file = st.file_uploader("Upload earnings transcript (.txt)", type=["txt"], key="mosaic_file")

        can_process = bool(mosaic_ticker and transcript_file is not None)
        if st.button("Run Transcript Mosaic Analysis", key="run_mosaic", disabled=not can_process, width="stretch"):
            try:
                transcript_text = transcript_file.getvalue().decode("utf-8", errors="ignore")
                if not transcript_text.strip():
                    st.error("The uploaded transcript appears to be empty.")
                else:
                    # persist transcript first (best-effort)
                    transcript_id = None
                    try:
                        transcript_id = db.insert_transcript(
                            ticker=mosaic_ticker or None,
                            filename=getattr(transcript_file, "name", None),
                            content=transcript_text,
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
                live_price = _safe_float(row.get("current_price"))
                current_value = float(row.get("current_value") or 0.0)
                total_portfolio_value = float(portfolio_summary["current_value"].sum(skipna=True)) if "current_value" in portfolio_summary else 0.0
                portfolio_weight_pct = (current_value / total_portfolio_value * 100.0) if total_portfolio_value > 0 else 0.0

                st.markdown(
                    f"**Shares:** {shares}  \n**Avg Cost:** ${avg_cost:,.2f}  \n**Live Price:** {'N/A' if live_price is None else f'${live_price:,.2f}'}  \n**Current Value:** ${current_value:,.2f}  \n**Total Portfolio Value:** ${total_portfolio_value:,.2f}  \n**Portfolio Weight:** {portfolio_weight_pct:.2f}%"
                )

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
                    st.subheader("Cached Investment Thesis")
                    st.markdown(cached_thesis.get("report_md") or "")
                if cached_dcf and cached_dcf.get("report_md"):
                    st.subheader("Cached Reverse DCF")
                    st.markdown(cached_dcf.get("report_md") or "")
                if cached_mosaic and cached_mosaic.get("report_md"):
                    st.subheader("Cached Transcript Mosaic")
                    st.markdown(cached_mosaic.get("report_md") or "")
                if latest_transcript and latest_transcript.get("content"):
                    st.subheader("Latest Uploaded Transcript")
                    st.text_area("Transcript (cached)", latest_transcript.get("content") or "", height=180)

                # Fetch fundamentals via yfinance with retry/backoff; on rate-limit, fall back to market_service profile
                fundamentals_missing: list[str] = []
                trailing_pe = None
                forward_pe = None
                operating_cash_flow = None
                total_assets = None
                total_debt = None
                try:
                    max_attempts = 3
                    delay = 1.0
                    last_exc = None
                    for attempt in range(1, max_attempts + 1):
                        try:
                            instrument = yf.Ticker(selected_ticker)
                            info = instrument.info or {}
                            trailing_pe = _safe_float(info.get("trailingPE"))
                            forward_pe = _safe_float(info.get("forwardPE"))
                            cashflow_df = getattr(instrument, "cashflow", None)
                            if cashflow_df is not None and not cashflow_df.empty:
                                try:
                                    operating_cash_flow = _safe_float(cashflow_df.loc["Operating Cash Flow"].tolist()[0]) if "Operating Cash Flow" in cashflow_df.index else None
                                except Exception:
                                    operating_cash_flow = None

                            balance = getattr(instrument, "balance_sheet", None)
                            total_debt = _safe_float(info.get("totalDebt")) or None
                            if balance is not None and not balance.empty:
                                total_assets = _safe_float(balance.loc["Total Assets"].tolist()[0]) if "Total Assets" in balance.index else None
                                if total_debt is None:
                                    for cand in ("Long Term Debt", "LongTermDebt", "totalDebt"):
                                        try:
                                            if cand in balance.index:
                                                total_debt = _safe_float(balance.loc[cand].tolist()[0])
                                                break
                                        except Exception:
                                            continue
                            # success - break retry loop
                            last_exc = None
                            break
                        except Exception as exc:
                            last_exc = exc
                            msg = str(exc).lower()
                            # if rate limited, wait and retry a few times
                            if "too many requests" in msg or "rate limit" in msg or "429" in msg:
                                import time

                                time.sleep(delay)
                                delay *= 2
                                continue
                            # other errors - don't retry
                            break
                    if last_exc is not None and ("too many requests" in str(last_exc).lower() or "rate limit" in str(last_exc).lower()):
                        # fallback to market service cached profile
                        try:
                            prof = market_service.fetch_asset_profile(selected_ticker)
                            # we may not have PE or cashflow, but we can at least populate price/company/currency
                            if prof.get("company_name"):
                                # attach minimal info into `info` for downstream use
                                info = {"longName": prof.get("company_name"), "currency": prof.get("currency"), "currentPrice": prof.get("price")}
                                trailing_pe = trailing_pe or None
                        except Exception:
                            LOGGER.exception("fallback market_service.fetch_asset_profile failed")
                    # Simple ROIC/WACC estimates (best-effort)
                    roic = None
                    wacc = None
                    if operating_cash_flow is not None and total_assets is not None and total_debt is not None:
                        invested_capital = (total_assets - total_debt) if (total_assets is not None and total_debt is not None) else None
                        if invested_capital and invested_capital != 0:
                            roic = (operating_cash_flow / invested_capital) * 100.0

                    # mark missing fields for user visibility
                    if trailing_pe is None:
                        fundamentals_missing.append("trailing P/E")
                    if forward_pe is None:
                        fundamentals_missing.append("forward P/E")
                    if operating_cash_flow is None:
                        fundamentals_missing.append("operating cash flow")

                except Exception as exc:
                    st.error(f"Failed to fetch fundamentals for {selected_ticker}: {exc}")
                    trailing_pe = forward_pe = operating_cash_flow = roic = wacc = None

                if fundamentals_missing:
                    st.error(f"Missing fundamentals: {', '.join(fundamentals_missing)}. Thesis will proceed but results may be incomplete.")

                transcript_file = st.file_uploader("Upload supporting transcript (.txt)", type=["txt"], key="thesis_transcript")
                transcript_text = ""
                if transcript_file is not None:
                    try:
                        transcript_text = transcript_file.getvalue().decode("utf-8", errors="ignore")
                    except Exception:
                        transcript_text = ""

                allocation_details = {
                    "shares": shares,
                    "avg_cost": avg_cost,
                    "current_value": current_value,
                    "portfolio_weight_pct": portfolio_weight_pct,
                    "live_price": live_price,
                    "trailing_pe": trailing_pe,
                    "forward_pe": forward_pe,
                    "operating_cash_flow": operating_cash_flow,
                    "roic": roic,
                    "wacc": wacc,
                }

                if st.button("Generate Institutional Thesis Report", key="gen_thesis", width="stretch"):
                    try:
                        with st.spinner("Generating investment thesis..."):
                            report = generate_single_investment_thesis(selected_ticker, allocation_details, transcript_text)
                        st.markdown(report)

                        # persist AI report (best-effort)
                        try:
                            inputs = {"ticker": selected_ticker, "allocation": allocation_details}
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
                        st.error(f"Investment thesis generation failed: {exc}")


if __name__ == "__main__":
    main()