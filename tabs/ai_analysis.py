from __future__ import annotations

from typing import Any
import os

import pandas as pd
import streamlit as st
import yfinance as yf
from google import genai


def _gemini_client() -> genai.Client:
    api_key = str(st.secrets.get("GEMINI_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured in Streamlit secrets.")
    return genai.Client(api_key=api_key)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _extract_fcf(cashflow_df: pd.DataFrame | None) -> float | None:
    if cashflow_df is None or cashflow_df.empty:
        return None

    for field in ["Free Cash Flow", "Operating Cash Flow"]:
        if field in cashflow_df.index:
            series = cashflow_df.loc[field]
            for value in series.tolist():
                parsed = _safe_float(value)
                if parsed is not None and parsed != 0:
                    if field == "Operating Cash Flow" and "Capital Expenditure" in cashflow_df.index:
                        capex_series = cashflow_df.loc["Capital Expenditure"]
                        for capex in capex_series.tolist():
                            parsed_capex = _safe_float(capex)
                            if parsed_capex is not None:
                                return parsed + parsed_capex
                    return parsed
    return None


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        chunks = []
        for part in parts:
            chunk = getattr(part, "text", None)
            if chunk:
                chunks.append(str(chunk))
        if chunks:
            return "\n".join(chunks)

    return "No model output was returned."


DEFAULT_GEMINI_MODEL = "gemini-1.5-flash"


def get_model_name() -> str:
    """Resolve the Gemini model name from environment, Streamlit secrets, or default."""
    model = os.environ.get("GEMINI_MODEL")
    if model:
        return model.strip()
    model = str(st.secrets.get("GEMINI_MODEL", "")).strip()
    if model:
        return model
    return DEFAULT_GEMINI_MODEL


@st.cache_data(ttl=86400, show_spinner=False)
def get_reverse_dcf_analysis(ticker_symbol: str) -> str:
    symbol = str(ticker_symbol or "").upper().strip()
    if not symbol:
        raise ValueError("Ticker symbol is required.")

    ticker = yf.Ticker(symbol)
    info = ticker.info or {}
    fast_info = getattr(ticker, "fast_info", {}) or {}

    live_price = _safe_float(fast_info.get("lastPrice"))
    if live_price is None:
        live_price = _safe_float(info.get("currentPrice"))
    shares_outstanding = _safe_float(info.get("sharesOutstanding"))
    annual_fcf = _extract_fcf(ticker.cashflow)

    if live_price is None or shares_outstanding is None or annual_fcf is None:
        raise RuntimeError(
            "Could not retrieve enough market fundamentals (price, shares outstanding, FCF) for this ticker."
        )

    market_cap = live_price * shares_outstanding
    client = _gemini_client()

    prompt = f"""
You are a senior equity research analyst.

Perform a reverse DCF on {symbol} using:
- Current price: {live_price:,.4f}
- Shares outstanding: {shares_outstanding:,.0f}
- Implied market capitalization: {market_cap:,.2f}
- Most recent annual Free Cash Flow: {annual_fcf:,.2f}
- Discount rate: 10%
- Terminal growth rate: 3%

Tasks:
1) Estimate the implied FCF CAGR required for the business to justify today's market value.
2) Show the calculation logic and assumptions clearly.
3) Produce a concise risk assessment discussing what would make that implied growth unrealistic.
4) End with a verdict section: Bull case vs Bear case vs Most likely.

Output requirements:
- Use clean Markdown with section headers.
- Include a small assumptions table.
- Keep it professional, objective, and numerically grounded.
""".strip()

    model_name = get_model_name()
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)
    except Exception as exc:
        msg = str(exc).lower()
        try:
            available = [m.name for m in client.models.list()]
        except Exception:
            available = None
        if "404" in msg or "not found" in msg:
            avail_text = ", ".join(available) if available else "(could not list models)"
            raise RuntimeError(f"Model '{model_name}' not available for generation. Available: {avail_text}") from exc
        raise
    return _response_text(response)


@st.cache_data(ttl=86400, show_spinner=False)
def get_transcript_mosaic_analysis(ticker_symbol: str, transcript_text: str) -> str:
    symbol = str(ticker_symbol or "").upper().strip()
    text = str(transcript_text or "").strip()
    if not symbol:
        raise ValueError("Ticker symbol is required.")
    if not text:
        raise ValueError("Transcript text is required.")

    client = _gemini_client()

    prompt = f"""
You are a forensic short-seller and earnings-call linguistic analyst.

Ticker: {symbol}

Analyze the following earnings transcript and produce a forensic mosaic report.
Focus on:
- Management evasions and non-answers.
- Structural shifts in language/tone between prepared remarks and Q&A.
- Missing discussion of obvious industry or balance-sheet blindspots.
- Inconsistencies between confidence language and disclosed fundamentals.

Transcript:
{text}

Output requirements:
- Return Markdown with sections: Red Flags, Tone Shift Signals, Blindspots, Key Questions for Next Call.
- Include direct quotes where possible.
- Be specific and evidence-based, not sensational.
""".strip()

    model_name = get_model_name()
    try:
        response = client.models.generate_content(model=model_name, contents=prompt)
    except Exception as exc:
        msg = str(exc).lower()
        try:
            available = [m.name for m in client.models.list()]
        except Exception:
            available = None
        if "404" in msg or "not found" in msg:
            avail_text = ", ".join(available) if available else "(could not list models)"
            raise RuntimeError(f"Model '{model_name}' not available for generation. Available: {avail_text}") from exc
        raise
    return _response_text(response)


@st.cache_data(ttl=86400, show_spinner=False)
def generate_single_investment_thesis(ticker_symbol: str, allocation_details: dict, transcript_text: str) -> str:
    """Generate a single-ticker institutional-style investment thesis.

    This function expects `allocation_details` to include at minimum the keys:
    'shares', 'avg_cost', 'current_value', 'portfolio_weight_pct'. Additional
    financial entries (trailing_pe, forward_pe, operating_cash_flow, roic, wacc)
    may be present and will be injected into the prompt if available.
    """
    symbol = str(ticker_symbol or "").upper().strip()
    if not symbol:
        raise RuntimeError("Ticker symbol is required for thesis generation.")

    if not allocation_details or not isinstance(allocation_details, dict):
        raise RuntimeError("Allocation details are required and must be a dict.")

    # Ensure required allocation fields are present
    required_keys = ("shares", "avg_cost", "current_value", "portfolio_weight_pct")
    missing = [k for k in required_keys if k not in allocation_details]
    if missing:
        raise RuntimeError(f"Allocation details missing required keys: {', '.join(missing)}")

    # Build a simple financial block if available
    trailing_pe = allocation_details.get("trailing_pe")
    forward_pe = allocation_details.get("forward_pe")
    operating_cash_flow = allocation_details.get("operating_cash_flow")
    roic = allocation_details.get("roic")
    wacc = allocation_details.get("wacc")

    financial_block_lines: list[str] = []
    financial_block_lines.append(f"- Trailing P/E: {trailing_pe if trailing_pe is not None else 'N/A'}")
    financial_block_lines.append(f"- Forward P/E: {forward_pe if forward_pe is not None else 'N/A'}")
    financial_block_lines.append(f"- Operating Cash Flow: {operating_cash_flow if operating_cash_flow is not None else 'N/A'}")
    financial_block_lines.append(f"- ROIC (est): {roic if roic is not None else 'N/A'}")
    financial_block_lines.append(f"- WACC (est): {wacc if wacc is not None else 'N/A'}")
    financial_block = "\n".join(financial_block_lines)

    prompt = f"""Act as a world-class institutional value investor, equity research analyst, and expert capital allocator. You are reviewing a target stock that is already an active position in the user's portfolio.

Consider the user's current allocation footprint for this specific stock when delivering your final risk assessment:
### USER'S CURRENT ALLOCATION DETAIL:
- Shares Owned: {allocation_details['shares']}
- Average Cost Basis: ${allocation_details['avg_cost']:.2f}
- Current Market Value of Position: ${allocation_details['current_value']:.2f}
- Allocation Weighting in Total Portfolio: {allocation_details['portfolio_weight_pct']:.2f}%

Provide the following raw fundamental data for the company for context (if available):
{financial_block}

Analyze the provided raw fundamental data and transcript for the target company, synthesize the investment thesis, evaluate the management's capital discipline, and deliver a clear, actionable investment decision ("Buy", "Hold", "Watchlist", or "Avoid").

Structure your analysis into the following distinct sections:

### 1. Executive Summary & Thesis
* Provide a concise 2-3 sentence overview of the business model and the core investment thesis.
* Outline the primary near-term and long-term structural drivers (the "Flywheel" or "Moat") that sustain this business.

### 2. Ecosystem Interdependency & Segment Flywheels
* Map the synergy between business units. Does the company possess a core, high-volume segment that serves as a low-cost customer acquisition engine for a secondary, hyper-profitable, or high-ROIC segment?
* If a credit, financing, or lending book is integrated into the business model, explicitly conduct a risk audit on asset quality, non-performing loans (NPLs), and underwriting discipline.

### 3. Financial Health & Valuation Drilldown
* Analyze the valuation using multiple lenses: current Trailing P/E vs. Forward P/E based on forward guidance. Is the multiple contraction justified, or does it offer a compelling "margin of safety"?
* Evaluate the company's Return on Invested Capital (ROIC) relative to its Weighted Average Cost of Capital (WACC). Is the "Economic Spread" (ROIC - WACC) expanding or shrinking? Explicitly analyze whether the company is compounding value or destroying/diluting it.

### 4. Capital Allocation Mastery Checklist
Evaluate the management team's track record using a strict "Outsiders-style" prioritization framework. Determine if they act as "growth compounders" or value destroyers:
* **Internal Reinvestment:** Are they plowing FCF into high-ROIC projects? Or are they chasing low-margin top-line revenue?
* **Share Buybacks & Dividends:** If the stock is overvalued, are they destroying capital via buybacks? If the stock is undervalued, are they aggressively reducing share count to boost intrinsic value per share?
* **M&A vs. Organic Growth:** Do they favor disciplined internal development, or are they wasting cash on dilutive acquisitions?
* **Insider Alignment:** Detail executive skin-in-game (e.g., percentage of company stock held by the CEO/insiders).

### 5. Near-Term & Long-Term Bear Case (The Draconian Scenarios)
Identify the structural catalysts that could kill the thesis. Specifically look for:
* **Margin Traps:** Is margin compression a temporary strategic choice or a permanent baseline shift caused by intense competition?
* **Terminal Value Concerns / Moat Erosion:** Are competitors gaining ground?
* **Regulatory/Macro Risks:** Detail regulatory, antitrust, or structural industry changes that pose ever-present risks.

### 6. Multi-Year Valuation Scenarios & Expected Returns
Map out three clear return profiles based on a 3-to-4-year forward horizon:
* **Bullish/Optimistic Case:** Revenue growth range, margin targets, fair exit multiple, and expected annualized return.
* **Base/Moderate Case:** Slower but stable growth, standard multiple, and annualized return.
* **Draconian/Bear Case:** Fading moat, contracting exit multiple, and worst-case annualized return.

### 7. Final Investment Verdict & Monitor Dashboard
* **The Decision:** Give a definitive verdict: Buy, Hold, Watchlist, or Avoid. **Directly address whether the user's current allocation weight ({allocation_details['portfolio_weight_pct']:.2f}%) is appropriate, over-indexed, or under-indexed given the asset's risk profile.**
* **The Dashboard Triggers:** Provide 3 distinct key operational metrics that an investor must track quarter-over-quarter to ensure the thesis remains intact.

Additional context (transcript):
{transcript_text}
""".strip()

    client = _gemini_client()
    try:
        response = client.models.generate_content(model="gemini-3.5-flash", contents=prompt)
    except Exception as exc:
        msg = str(exc).lower()
        try:
            available = [m.name for m in client.models.list()]
        except Exception:
            available = None
        if "404" in msg or "not found" in msg:
            avail_text = ", ".join(available) if available else "(could not list models)"
            raise RuntimeError(f"Model 'gemini-3.5-flash' not available for generation. Available: {avail_text}") from exc
        raise
    return _response_text(response)
