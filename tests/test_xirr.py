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
from alphavault.robinhood_sync import RobinhoodSyncService


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


def test_xirr_prefers_positive_root_when_net_return_is_positive():
    today = date.today()
    txs = [
        Transaction(ticker="MROOT", tx_date=today - timedelta(days=3045), amount=-16412.61),
        Transaction(ticker="MROOT", tx_date=today - timedelta(days=2851), amount=2581.49),
        Transaction(ticker="MROOT", tx_date=today - timedelta(days=1623), amount=8824.38),
        Transaction(ticker="MROOT", tx_date=today - timedelta(days=1145), amount=15794.18),
        Transaction(ticker="MROOT", tx_date=today - timedelta(days=1052), amount=2356.39),
        Transaction(ticker="MROOT", tx_date=today - timedelta(days=430), amount=-14840.80),
    ]

    xirr = compute_xirr(txs, terminal_value=1853.03)

    assert xirr is not None
    assert xirr > 0.0
    assert pytest.approx(xirr, abs=0.001) == 0.0039


def test_metrics_stock_xirr_uses_open_lots_for_active_holdings():
    today = date.today()
    positions = [
        Position(ticker="OPEN", company_name="Open Lots Inc", shares=10.0, avg_price=100.0, currency="USD"),
    ]
    transactions = [
        Transaction(ticker="OPEN", tx_date=today - timedelta(days=365 * 5), amount=-1000.0, side="buy", shares=10.0, price=100.0, currency="USD"),
        Transaction(ticker="OPEN", tx_date=today - timedelta(days=365 * 4), amount=500.0, side="sell", shares=10.0, price=50.0, currency="USD"),
        Transaction(ticker="OPEN", tx_date=today - timedelta(days=365), amount=-1000.0, side="buy", shares=10.0, price=100.0, currency="USD"),
    ]
    quotes = {"OPEN": Quote(price=180.0, market_cap=1_000_000_000.0)}

    df = build_metrics_table(positions, transactions, quotes, {"USD": 1.0}, baseline_date=today - timedelta(days=365 * 6))

    row = df[df["ticker"] == "OPEN"].iloc[0]
    assert row["total_change_pct"] > 0.0
    assert row["xirr"] is not None
    assert row["xirr"] > 0.0
    assert pytest.approx(row["xirr"], abs=0.02) == 0.80


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


def test_robinhood_margin_balance_prefers_phoenix_levered_amount():
    margin_balance = RobinhoodSyncService._extract_margin_balance_usd(
        account_profile={"cash": "-250.00"},
        portfolio_profile={"excess_margin": "100.00", "excess_maintenance": "100.00"},
        phoenix_account=[{"levered_amount": "1234.56", "near_margin_call": False}],
    )

    assert margin_balance == 1234.56


def test_robinhood_margin_balance_uses_linked_margin_balances():
    margin_balance = RobinhoodSyncService._extract_margin_balance_usd(
        account_profile={"margin_balances": "https://api.robinhood.com/margin/accounts/abc/"},
        portfolio_profile={},
        phoenix_account={},
        margin_balances={"outstanding_margin_balance": "4567.89"},
    )

    assert margin_balance == 4567.89


def test_robinhood_margin_balance_checks_link_when_phoenix_is_zero():
    margin_balance = RobinhoodSyncService._extract_margin_balance_usd(
        account_profile={"margin_balances": "https://api.robinhood.com/margin/accounts/abc/"},
        portfolio_profile={},
        phoenix_account={"levered_amount": "0.00"},
        margin_balances={"outstanding_margin_balance": "4567.89"},
    )

    assert margin_balance == 4567.89


def test_robinhood_margin_balance_records_zero_when_margin_response_has_no_debt():
    margin_balance = RobinhoodSyncService._extract_margin_balance_usd(
        account_profile={},
        portfolio_profile={},
        phoenix_account={},
        margin_balances={"margin_limit": "10000.00", "cash": "25.00"},
    )

    assert margin_balance == 0.0


def test_robinhood_sync_margin_balance_follows_link_and_persists():
    class FakeProfiles:
        @staticmethod
        def load_account_profile(account_number=None):
            return {"margin_balances": "https://api.robinhood.com/margin/accounts/abc/"}

        @staticmethod
        def load_portfolio_profile(account_number=None):
            return {}

    class FakeAccount:
        @staticmethod
        def load_phoenix_account():
            return {}

    class FakeRobinhood:
        profiles = FakeProfiles()
        account = FakeAccount()

        @staticmethod
        def request_get(url):
            assert url == "https://api.robinhood.com/margin/accounts/abc/"
            return {"outstanding_margin_balance": "321.09"}

    class FakeDb:
        def __init__(self):
            self.margin_balance = None

        def set_margin_balance_usd(self, value):
            self.margin_balance = value

    fake_db = FakeDb()
    service = RobinhoodSyncService(fake_db, market_service=None)

    service._sync_margin_balance(FakeRobinhood(), account_number=None)

    assert fake_db.margin_balance == 321.09


def test_robinhood_sync_margin_balance_tries_direct_account_endpoint():
    class FakeProfiles:
        @staticmethod
        def load_account_profile(account_number=None):
            return {"account_number": "ABC123", "margin_balances": None}

        @staticmethod
        def load_portfolio_profile(account_number=None):
            return {}

    class FakeAccount:
        @staticmethod
        def load_phoenix_account():
            return {}

    class FakeRobinhood:
        profiles = FakeProfiles()
        account = FakeAccount()

        @staticmethod
        def request_get(url):
            if url == "https://api.robinhood.com/margin/accounts/ABC123/":
                return {"margin_debit": "654.32"}
            return None

    class FakeDb:
        def __init__(self):
            self.margin_balance = None

        def set_margin_balance_usd(self, value):
            self.margin_balance = value

    fake_db = FakeDb()
    service = RobinhoodSyncService(fake_db, market_service=None)

    service._sync_margin_balance(FakeRobinhood(), account_number="default")

    assert fake_db.margin_balance == 654.32
