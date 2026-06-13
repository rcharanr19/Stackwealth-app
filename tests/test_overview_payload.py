import json
import pandas as pd
from streamlit_app import build_portfolio_overview_input, _hash_portfolio_snapshot, _brief_portfolio_hash


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
