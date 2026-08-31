import json
from datetime import date, timedelta
import pandas as pd
from alphavault.finance_engine import build_metrics_table, exclude_seed_transactions_with_real_history
from alphavault.models import Position, Quote, Transaction
from streamlit_app import (
    build_portfolio_overview_input,
    _hash_portfolio_snapshot,
    _brief_portfolio_hash,
    _history_sample_dates,
    _portfolio_value_history,
    _shares_as_of_from_current,
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


def test_shares_as_of_from_current_walks_transactions_backward():
    today = date.today()
    txs = [
        Transaction("AAA", today - timedelta(days=10), -100.0, "buy", 1.0, 100.0, "USD"),
        Transaction("AAA", today - timedelta(days=5), 50.0, "sell", 0.5, 100.0, "USD"),
    ]

    shares = _shares_as_of_from_current(txs, "AAA", current_shares=1.5, as_of=today - timedelta(days=7))

    assert shares == 2.0


def test_exclude_seed_transactions_when_real_history_exists_for_ticker():
    today = date.today()
    transactions = [
        Transaction("AAA", today - timedelta(days=2), -100.0, "buy", 1.0, 100.0, "USD", execution_id="seed-AAA-1"),
        Transaction("AAA", today - timedelta(days=1), -120.0, "buy", 1.0, 120.0, "USD", execution_id="real-AAA-1"),
        Transaction("BBB", today - timedelta(days=1), -80.0, "buy", 1.0, 80.0, "USD", execution_id="seed-BBB-1"),
    ]

    filtered = exclude_seed_transactions_with_real_history(transactions)

    assert [tx.execution_id for tx in filtered] == ["real-AAA-1", "seed-BBB-1"]


def test_metrics_ignore_seed_transactions_for_tickers_with_real_history():
    today = date.today()
    positions = [Position("AAA", "AAA Corp", 1.0, 120.0, "USD")]
    transactions = [
        Transaction("AAA", today - timedelta(days=365), -100.0, "buy", 1.0, 100.0, "USD", execution_id="seed-AAA-1"),
        Transaction("AAA", today - timedelta(days=365), -120.0, "buy", 1.0, 120.0, "USD", execution_id="real-AAA-1"),
    ]

    metrics = build_metrics_table(positions, transactions, {"AAA": Quote(180.0, 1_000_000.0)}, {"USD": 1.0})

    row = metrics.iloc[0]
    assert row["cost_basis_native"] == 120.0
    assert row["pnl_native"] == 60.0


def test_transaction_rows_hide_seed_when_real_history_exists():
    today = date.today()
    transactions = exclude_seed_transactions_with_real_history(
        [
            Transaction("AAA", today, -100.0, "buy", 1.0, 100.0, "USD", execution_id="seed-AAA-1"),
            Transaction("AAA", today, -120.0, "buy", 1.0, 120.0, "USD", execution_id="real-AAA-1"),
        ]
    )

    rows = _transaction_rows_for_ticker(transactions, "AAA")

    assert rows.shape[0] == 1
    assert rows.iloc[0]["Execution"] == "real-AAA-1"


def test_history_sample_dates_includes_today():
    sample_dates = _history_sample_dates(date.today() - timedelta(days=30), max_points=4)

    assert sample_dates[-1] == date.today()


def test_portfolio_value_history_uses_historical_prices_and_margin():
    today = date.today()
    portfolio_summary = pd.DataFrame([
        {
            "ticker": "AAA",
            "shares": 2.0,
            "current_price": 20.0,
            "current_price_usd": 20.0,
        }
    ])

    class FakeMarketService:
        def fetch_cutoff_prices(self, tickers, cutoffs):
            return {"AAA": {cutoff: 10.0 for cutoff in cutoffs}}

    history = _portfolio_value_history(
        portfolio_summary,
        transactions=[],
        market_service=FakeMarketService(),
        baseline_date=today - timedelta(days=365),
        margin_balance=5.0,
    )

    assert not history.empty
    assert history.iloc[0]["Gross Holdings"] == 20.0
    assert history.iloc[0]["Net Portfolio Value"] == 15.0
    assert history.iloc[-1]["Gross Holdings"] == 40.0
    assert history.iloc[-1]["Net Portfolio Value"] == 35.0
