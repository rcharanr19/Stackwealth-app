from datetime import date, timedelta
import pytest
from alphavault.models import Position, Transaction, Quote
from alphavault.finance_engine import (
    compute_xirr,
    build_metrics_table,
    compute_portfolio_xirr,
    compute_portfolio_since_start_metrics,
)
from alphavault.models import Snapshot
from streamlit_app import compute_dashboard


def test_company_xirr_with_robinhood_history():
    # Buy 10 shares @ $100 one year ago (-1000 cash outflow)
    one_year_ago = date.today() - timedelta(days=365)
    txs = [
        Transaction(
            ticker="HOOD_STOCK",
            tx_date=one_year_ago,
            amount=-1000.0,
            side="buy",
            shares=10.0,
            price=100.0,
            currency="USD",
        )
    ]
    # Current value: 10 shares @ $150 = $1500 terminal valuation
    terminal_value = 1500.0
    xirr = compute_xirr(txs, terminal_value)
    
    assert xirr is not None
    # 50% gain in 1 year should yield ~0.50 (50% XIRR)
    assert pytest.approx(xirr, abs=0.02) == 0.50


def test_build_metrics_table_includes_xirr():
    one_year_ago = date.today() - timedelta(days=365)
    positions = [
        Position(ticker="AAPL", company_name="Apple Inc", shares=10.0, avg_price=100.0, currency="USD"),
    ]
    transactions = [
        Transaction(ticker="AAPL", tx_date=one_year_ago, amount=-1000.0, side="buy", shares=10.0, price=100.0, currency="USD"),
    ]
    quotes = {
        "AAPL": Quote(price=120.0, market_cap=2_000_000_000.0, previous_close=118.0)
    }
    fx_to_usd = {"USD": 1.0}

    df = build_metrics_table(positions, transactions, quotes, fx_to_usd, baseline_date=one_year_ago)

    assert "xirr" in df.columns
    row = df[df["ticker"] == "AAPL"].iloc[0]
    assert row["xirr"] is not None
    assert row["xirr"] > 0.0


def test_total_portfolio_xirr():
    one_year_ago = date.today() - timedelta(days=365)
    positions = [
        Position(ticker="MSFT", company_name="Microsoft", shares=5.0, avg_price=200.0, currency="USD"),
    ]
    transactions = [
        Transaction(ticker="MSFT", tx_date=one_year_ago, amount=-1000.0, side="buy", shares=5.0, price=200.0, currency="USD"),
    ]
    quotes = {
        "MSFT": Quote(price=250.0, market_cap=2_000_000_000.0)
    }
    fx_to_usd = {"USD": 1.0}

    port_xirr = compute_portfolio_xirr(transactions, positions, quotes, fx_to_usd)
    assert port_xirr is not None
    assert port_xirr > 0.0

    since_start = compute_portfolio_since_start_metrics(
        transactions=transactions,
        positions=positions,
        quotes=quotes,
        fx_to_usd=fx_to_usd,
        baseline_date=one_year_ago,
        baseline_value_usd=1000.0,
        tracked_tickers={"MSFT"},
    )
    assert since_start["xirr"] is not None
    assert since_start["xirr"] > 0.0


def test_short_holding_period_defaults_to_absolute_return():
    # Buy 10 shares @ $100 ninety days ago (-1000 cash outflow)
    ninety_days_ago = date.today() - timedelta(days=90)
    txs = [
        Transaction(
            ticker="NEW_STOCK",
            tx_date=ninety_days_ago,
            amount=-1000.0,
            side="buy",
            shares=10.0,
            price=100.0,
            currency="USD",
        )
    ]
    # Current value: 10 shares @ $120 = $1200 terminal valuation (20% absolute return)
    terminal_value = 1200.0
    xirr = compute_xirr(txs, terminal_value)

    assert xirr is not None
    # For < 365 days holding period, should return exact absolute return (0.20), NOT annualized (~1.07)
    assert pytest.approx(xirr, abs=1e-4) == 0.20


def test_dashboard_stock_xirr_uses_full_robinhood_history_for_untracked_assets():
    one_year_ago = date.today() - timedelta(days=365)
    hood_position = Position(ticker="HOOD", company_name="Robinhood", shares=4.0, avg_price=10.0, currency="USD")
    hood_transaction = Transaction(
        ticker="HOOD",
        tx_date=one_year_ago,
        amount=-40.0,
        side="buy",
        shares=4.0,
        price=10.0,
        currency="USD",
    )

    class FakeDb:
        def get_sync_profile(self):
            return {
                "baseline_date": one_year_ago.isoformat(),
                "baseline_value_usd": 40.0,
                "tracked_tickers": ["MSFT"],
            }

        def load_portfolio_state(self):
            return [hood_position], [hood_transaction]

        def update_market_snapshot(self, _records):
            pass

    class FakeMarketService:
        def refresh_snapshot(self, *, tickers, currencies):
            return Snapshot(
                quotes={"HOOD": Quote(price=15.0, market_cap=1_000_000_000.0)},
                fx_to_usd={"USD": 1.0},
                online=True,
                stale_tickers=set(),
            )

    metrics, _since_start, _profile = compute_dashboard(FakeDb(), FakeMarketService())

    row = metrics[metrics["ticker"] == "HOOD"].iloc[0]
    assert row["xirr"] is not None
    assert pytest.approx(row["xirr"], abs=0.02) == 0.50
