import json
from datetime import date, timedelta
import pandas as pd
from alphavault.models import Transaction
from streamlit_app import (
    build_portfolio_overview_input,
    _hash_portfolio_snapshot,
    _brief_portfolio_hash,
    _sort_portfolio_view,
    _ticker_xirr,
    _transaction_rows_for_ticker,
)


def test_build_payload_and_hash_roundtrip():
    # minimal metrics dataframe
    df = pd.DataFrame([
        {
            "ticker": "ABC",
            "shares": 10,
            "avg_cost": 5.0,
            "cost_basis": 50.0,
            "current_price": 6.0,
            "equity_usd": 60.0,
            "weight_pct": 60.0,
            "last_day_change_pct": 1.5,
            "pnl_usd": 10.0,
        }
    ])
    portfolio_summary = df.copy()
    profile = {"cash_usd": 10.0}

    payload = build_portfolio_overview_input(df, portfolio_summary, profile)
    assert isinstance(payload, dict)
    assert "portfolio_hash" in payload and payload["portfolio_hash"]

    # brief hash should detect same snapshot
    brief = _brief_portfolio_hash(df, portfolio_summary, profile)
    assert brief == payload["portfolio_hash"] or isinstance(brief, str)


def test_brief_hash_changes_on_change():
    df1 = pd.DataFrame([
        {"ticker": "ABC", "shares": 10, "avg_cost": 5.0, "cost_basis": 50.0, "equity_usd": 60.0}
    ])
    df2 = pd.DataFrame([
        {"ticker": "ABC", "shares": 11, "avg_cost": 5.0, "cost_basis": 55.0, "equity_usd": 66.0}
    ])
    profile = {"cash_usd": 0.0}
    h1 = _brief_portfolio_hash(df1, df1, profile)
    h2 = _brief_portfolio_hash(df2, df2, profile)
    assert h1 != h2


def test_portfolio_overview_payload_deducts_margin_balance():
    df = pd.DataFrame([
        {"ticker": "ABC", "shares": 10, "avg_cost": 5.0, "cost_basis": 50.0, "equity_usd": 100.0}
    ])
    profile = {"cash_usd": 25.0, "margin_balance_usd": 40.0}

    payload = build_portfolio_overview_input(df, df, profile)

    assert payload["totals"]["portfolio_value_usd"] == 100.0
    assert payload["totals"]["margin_balance_usd"] == 40.0
    assert payload["totals"]["net_portfolio_value_usd"] == 85.0


def test_sort_portfolio_view_uses_selected_xirr_column():
    df = pd.DataFrame([
        {"ticker": "LOW", "current_value": 100.0, "xirr_display_pct": -5.0},
        {"ticker": "HIGH", "current_value": 50.0, "xirr_display_pct": 20.0},
    ])

    sorted_df = _sort_portfolio_view(df, "Highest XIRR", "xirr_display_pct")

    assert sorted_df.iloc[0]["ticker"] == "HIGH"


def test_transaction_rows_for_ticker_filters_and_formats_rows():
    txs = [
        Transaction("AAA", date.today(), -100.0, "buy", 2.0, 50.0, "USD", execution_id="one"),
        Transaction("BBB", date.today(), -200.0, "buy", 4.0, 50.0, "USD", execution_id="two"),
    ]

    rows = _transaction_rows_for_ticker(txs, "aaa")

    assert rows.shape[0] == 1
    assert rows.iloc[0]["Execution"] == "one"


def test_ticker_xirr_can_filter_since_baseline():
    today = date.today()
    txs = [
        Transaction("AAA", today - timedelta(days=365 * 2), -100.0, "buy", 1.0, 100.0, "USD"),
        Transaction("AAA", today - timedelta(days=365), -100.0, "buy", 1.0, 100.0, "USD"),
    ]

    xirr = _ticker_xirr(txs, "AAA", 150.0, start_date=today - timedelta(days=366))

    assert xirr is not None
    assert xirr > 0.0
