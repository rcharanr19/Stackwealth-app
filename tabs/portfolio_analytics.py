"""Portfolio analytics helpers: tax loss harvesting, dividends, benchmark comparison."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import yfinance as yf
import numpy as np
import numpy_financial as npf

from tabs.tax_settings import (
    TaxSettings,
    get_federal_ltcg_rate,
    get_state_ltcg_rate,
    apply_niit,
    calculate_total_tax_rate,
)


def identify_tax_loss_harvesting_candidates(
    portfolio_summary: pd.DataFrame,
) -> pd.DataFrame:
    """Identify positions suitable for tax-loss harvesting.
    
    Returns DataFrame with:
    - Ticker, Company, Current Value, Loss Amount, Loss %
    - Wash-sale recommendation (30-day holding period)
    """
    if portfolio_summary.empty:
        return pd.DataFrame()
    
    # Filter to positions with unrealized losses
    losses = portfolio_summary[portfolio_summary["open_pnl"] < 0].copy()
    
    if losses.empty:
        return pd.DataFrame()
    
    # Calculate loss metrics
    losses["loss_amount"] = losses["open_pnl"].abs()
    losses["loss_pct"] = (losses["open_pnl"] / losses["cost_basis"] * 100).round(2)
    losses["harvest_priority"] = losses["loss_amount"]  # Sort by largest loss first
    
    result = losses[
        ["ticker", "company_name", "current_value", "loss_amount", "loss_pct", "weight_pct"]
    ].copy()
    
    result.columns = ["Ticker", "Company", "Current Value", "Loss Amount", "Loss %", "Portfolio Weight %"]
    
    return result.sort_values("Loss Amount", ascending=False)


def calculate_tax_impact(
    portfolio_summary: pd.DataFrame,
    tax_settings: TaxSettings | None = None,
) -> dict[str, Any]:
    """Calculate realized and unrealized tax impact.
    
    Args:
        portfolio_summary: Portfolio DataFrame
        tax_settings: TaxSettings object with AGI, state, filing status
    
    Returns dict with tax metrics
    """
    if tax_settings is None:
        tax_settings = TaxSettings()
    
    if portfolio_summary.empty:
        return {
            "total_unrealized_gain": 0.0,
            "total_unrealized_loss": 0.0,
            "estimated_tax_on_gains": 0.0,
            "tax_loss_harvesting_benefit": 0.0,
            "net_after_tax_value": 0.0,
            "federal_rate": 0.0,
            "state_rate": 0.0,
            "niit_amount": 0.0,
        }
    
    # Separate gains and losses
    gains = portfolio_summary[portfolio_summary["open_pnl"] > 0]["open_pnl"].sum()
    losses = portfolio_summary[portfolio_summary["open_pnl"] < 0]["open_pnl"].sum()
    
    gains = max(0, gains)
    losses_abs = abs(losses) if losses < 0 else 0
    
    # Calculate net taxable gain (losses offset gains)
    net_taxable_gain = max(0, gains - losses_abs)
    
    # Get tax rates
    federal_rate = get_federal_ltcg_rate(tax_settings.agi, tax_settings.filing_status)
    state_rate = get_state_ltcg_rate(tax_settings.state)
    
    # Calculate federal tax
    federal_tax = net_taxable_gain * federal_rate
    
    # Calculate state tax
    state_tax = net_taxable_gain * state_rate
    
    # Calculate NIIT
    niit_amount = apply_niit(tax_settings.agi, net_taxable_gain, tax_settings.filing_status)
    
    total_tax = federal_tax + state_tax + niit_amount
    
    # Tax benefit from harvesting losses
    tax_benefit_from_losses = losses_abs * (federal_rate + state_rate)
    
    current_portfolio_value = portfolio_summary["current_value"].sum()
    
    return {
        "total_unrealized_gain": float(gains),
        "total_unrealized_loss": float(losses_abs),
        "net_taxable_gain": float(net_taxable_gain),
        "estimated_tax_on_gains": float(total_tax),
        "federal_tax": float(federal_tax),
        "state_tax": float(state_tax),
        "niit_amount": float(niit_amount),
        "tax_loss_harvesting_benefit": float(tax_benefit_from_losses),
        "net_after_tax_value": float(current_portfolio_value - total_tax),
        "federal_rate": float(federal_rate),
        "state_rate": float(state_rate),
        "effective_tax_rate": float(total_tax / current_portfolio_value * 100) if current_portfolio_value > 0 else 0.0,
    }


def get_wash_sale_alert(ticker: str, current_holdings: pd.DataFrame) -> str:
    """Generate wash-sale alert message."""
    msg = f"**Wash-Sale Rule**: If you harvest {ticker}, wait 30+ days before buying similar securities. "
    msg += "Selling and buying within 30 days will disallow the loss deduction."
    return msg


def calculate_dividend_projections(
    portfolio_summary: pd.DataFrame,
    market_service: Any = None,
) -> pd.DataFrame:
    """Project annual dividend income by position.
    
    Fetches dividend yield from yfinance and projects annual income.
    """
    if portfolio_summary.empty:
        return pd.DataFrame()
    
    rows = []
    for _, row in portfolio_summary.iterrows():
        ticker = str(row.get("ticker") or "").upper().strip()
        shares = float(row.get("shares") or 0.0)
        current_price = float(row.get("current_price") or 0.0)
        current_value = float(row.get("current_value") or 0.0)
        
        if not ticker or shares <= 0:
            continue
        
        # Fetch dividend info from yfinance
        try:
            t = yf.Ticker(ticker)
            dividend_yield = 0.0
            
            # Try to get dividend yield from fast_info
            try:
                fast = getattr(t, "fast_info", {})
                if isinstance(fast, dict):
                    div_yield = fast.get("dividendYield")
                else:
                    div_yield = getattr(fast, "dividendYield", None)
                
                if div_yield is not None:
                    dividend_yield = float(div_yield)
            except Exception:
                pass
            
            # Fallback: try to get from info
            if dividend_yield == 0.0:
                try:
                    info = getattr(t, "info", {})
                    div_yield = info.get("dividendYield")
                    if div_yield is not None:
                        dividend_yield = float(div_yield)
                except Exception:
                    pass
            
            # Calculate annual dividend
            annual_dividend = current_value * dividend_yield if dividend_yield > 0 else 0.0
            
            if annual_dividend > 0:  # Only show dividend-paying stocks
                rows.append({
                    "Ticker": ticker,
                    "Company": row.get("company_name") or ticker,
                    "Shares": shares,
                    "Current Price": current_price,
                    "Position Value": current_value,
                    "Dividend Yield %": dividend_yield * 100,
                    "Annual Dividend $": annual_dividend,
                    "Quarterly Estimate": annual_dividend / 4,
                })
        except Exception:
            # Skip if yfinance fails
            continue
    
    if not rows:
        return pd.DataFrame()
    
    result = pd.DataFrame(rows)
    result = result.sort_values("Annual Dividend $", ascending=False)
    
    return result


def get_dividend_summary(dividend_df: pd.DataFrame) -> dict[str, Any]:
    """Summarize dividend metrics from dividend projection DataFrame."""
    if dividend_df.empty:
        return {
            "total_annual_dividend": 0.0,
            "blended_yield": 0.0,
            "monthly_income": 0.0,
            "dividend_count": 0,
        }
    
    total_annual = dividend_df["Annual Dividend $"].sum()
    total_position_value = dividend_df["Position Value"].sum()
    blended_yield = (total_annual / total_position_value * 100) if total_position_value > 0 else 0.0
    
    return {
        "total_annual_dividend": float(total_annual),
        "blended_yield": float(blended_yield),
        "monthly_income": float(total_annual / 12),
        "dividend_count": len(dividend_df),
        "top_yielder": dividend_df.iloc[0]["Ticker"] if len(dividend_df) > 0 else None,
        "top_yield_pct": float(dividend_df.iloc[0]["Dividend Yield %"]) if len(dividend_df) > 0 else 0.0,
    }


def benchmark_portfolio_returns(
    portfolio_summary: pd.DataFrame,
    transactions: list[Any],
    baseline_date: date,
    indices: list[str] | None = None,
) -> pd.DataFrame:
    """Compare portfolio returns against major indices.
    
    Returns DataFrame with portfolio vs index returns.
    """
    if indices is None:
        indices = ["SPY", "QQQ", "VXUS"]  # S&P 500, Nasdaq-100, Int'l
    
    rows = []
    today = date.today()
    
    # Calculate portfolio XIRR (simple approximation using current value)
    try:
        # Get portfolio level returns
        total_invested = 0.0
        for tx in transactions:
            tx_amount = float(getattr(tx, "amount", 0) or 0)
            if tx_amount < 0:  # investments are negative
                total_invested += abs(tx_amount)
        
        total_current = portfolio_summary["current_value"].sum() if not portfolio_summary.empty else 0.0
        total_cash = 0.0  # Assume no cash for simplicity
        
        if total_invested > 0:
            portfolio_return_pct = ((total_current - total_invested) / total_invested) * 100
        else:
            portfolio_return_pct = 0.0
    except Exception:
        portfolio_return_pct = 0.0
    
    rows.append({
        "Ticker": "PORTFOLIO",
        "Name": "Your Portfolio",
        "Return %": portfolio_return_pct,
        "Type": "Your Portfolio",
    })
    
    # Fetch benchmark returns
    for ticker in indices:
        try:
            bench = yf.Ticker(ticker)
            
            # Get historical data
            hist = bench.history(start=baseline_date, end=today)
            if not hist.empty:
                start_price = hist.iloc[0]["Close"]
                end_price = hist.iloc[-1]["Close"]
                
                if start_price > 0:
                    return_pct = ((end_price - start_price) / start_price) * 100
                else:
                    return_pct = 0.0
            else:
                return_pct = 0.0
            
            rows.append({
                "Ticker": ticker,
                "Name": _get_index_name(ticker),
                "Return %": return_pct,
                "Type": "Benchmark",
            })
        except Exception:
            continue
    
    result = pd.DataFrame(rows)
    return result.sort_values("Return %", ascending=False)


def _get_index_name(ticker: str) -> str:
    """Map ticker to index name."""
    names = {
        "SPY": "S&P 500 (SPY)",
        "QQQ": "Nasdaq-100 (QQQ)",
        "VXUS": "Intl Stocks (VXUS)",
        "IVV": "Core S&P 500 (IVV)",
        "VOO": "Vanguard S&P 500 (VOO)",
        "VTI": "Total US Market (VTI)",
        "ACWX": "MSCI World Ex-US (ACWX)",
    }
    return names.get(ticker, ticker)


def get_benchmark_summary(benchmark_df: pd.DataFrame) -> dict[str, Any]:
    """Summarize benchmark comparison."""
    if benchmark_df.empty:
        return {
            "portfolio_return": 0.0,
            "avg_benchmark_return": 0.0,
            "alpha": 0.0,
            "outperformance": False,
        }
    
    portfolio_row = benchmark_df[benchmark_df["Ticker"] == "PORTFOLIO"]
    benchmark_rows = benchmark_df[benchmark_df["Ticker"] != "PORTFOLIO"]
    
    portfolio_return = float(portfolio_row.iloc[0]["Return %"]) if not portfolio_row.empty else 0.0
    avg_benchmark = float(benchmark_rows["Return %"].mean()) if not benchmark_rows.empty else 0.0
    
    alpha = portfolio_return - avg_benchmark
    outperformance = alpha > 0
    
    return {
        "portfolio_return": portfolio_return,
        "avg_benchmark_return": avg_benchmark,
        "alpha": alpha,
        "outperformance": outperformance,
    }
