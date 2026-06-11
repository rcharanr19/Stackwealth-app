from __future__ import annotations

from typing import Any

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

    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
    )
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

    response = client.models.generate_content(
        model="gemini-1.5-flash",
        contents=prompt,
    )
    return _response_text(response)
