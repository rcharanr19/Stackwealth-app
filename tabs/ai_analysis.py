from __future__ import annotations

from typing import Any
import os
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st
import yfinance as yf
from google import genai
import time
import logging
import requests
import re

LOGGER = logging.getLogger(__name__)


FMP_SUPPORTED_TICKERS = {
    "AAPL", "TSLA", "AMZN", "MSFT", "NVDA", "GOOGL", "META", "NFLX", "JPM", "V", "BAC",
    "PYPL", "DIS", "T", "PFE", "COST", "INTC", "KO", "TGT", "NKE", "SPY", "BA", "BABA",
    "XOM", "WMT", "GE", "CSCO", "VZ", "JNJ", "CVX", "PLTR", "SQ", "SHOP", "SBUX", "SOFI",
    "HOOD", "RBLX", "SNAP", "AMD", "UBER", "FDX", "ABBV", "ETSY", "MRNA", "LMT", "GM", "F",
    "LCID", "CCL", "DAL", "UAL", "AAL", "TSM", "SONY", "ET", "MRO", "COIN", "RIVN", "RIOT",
    "CPRX", "VWO", "SPYG", "NOK", "ROKU", "VIAC", "ATVI", "BIDU", "DOCU", "ZM", "PINS", "TLRY",
    "WBA", "MGM", "NIO", "C", "GS", "WFC", "ADBE", "PEP", "UNH", "CARR", "HCA", "TWTR", "BILI",
    "SIRI", "FUBO", "RKT",
}


def _df_to_period_records(frame: pd.DataFrame | None, limit: int = 5) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    try:
        table = frame.T.head(limit).copy()
        table.index = table.index.astype(str)
        return table.reset_index().rename(columns={"index": "period"}).to_dict(orient="records")
    except Exception:
        return []


def _extract_statement_value(frame: pd.DataFrame | None, row_names: list[str]) -> float | None:
    if frame is None or frame.empty:
        return None
    for row_name in row_names:
        if row_name not in frame.index:
            continue
        series = frame.loc[row_name]
        if isinstance(series, pd.Series):
            for value in series.tolist():
                parsed = _safe_float(value)
                if parsed is not None:
                    return parsed
        else:
            parsed = _safe_float(series)
            if parsed is not None:
                return parsed
    return None


def _fetch_yahoo_financial_profile(symbol: str) -> dict[str, Any]:
    LOGGER.debug("Building lightweight Yahoo financial profile for %s (fast_info + SEC fallback)", symbol)
    ticker = yf.Ticker(symbol)
    fast_info = {}
    try:
        fast_info = getattr(ticker, "fast_info", None) or {}
    except Exception as exc:
        LOGGER.debug("Yahoo fast_info fetch failed for %s: %s", symbol, exc)

    history_price = None
    try:
        history = ticker.history(period="5d")
        if not history.empty and "Close" in history:
            close_series = history["Close"].dropna()
            if not close_series.empty:
                history_price = _safe_float(close_series.iloc[-1])
    except Exception:
        history_price = None

    company_name = None
    try:
        info = {}
        # Some installations still expose a lightweight info dict; try to read shortName safely
        info = getattr(ticker, "info", None) or {}
        company_name = info.get("shortName") or info.get("longName")
    except Exception:
        company_name = None

    price = _safe_float(fast_info.get("lastPrice") or fast_info.get("last_price") or history_price)
    market_cap = _safe_float(fast_info.get("marketCap") or fast_info.get("market_cap"))

    # Try to enrich with SEC filings (EdgarTools) if available — best-effort and cached in this module
    sec_data = _fetch_sec_financials_for_symbol(symbol)

    profile = {
        "ticker": symbol,
        "source": "yfinance_light",
        "company_name": company_name,
        "currency": str((fast_info.get("currency") or None)).upper().strip() or None,
        "price": price,
        "market_cap": market_cap,
        "trailingPE": None,
        "forwardPE": None,
        "operating_cash_flow": sec_data.get("operating_cash_flow") if sec_data else None,
        "total_assets": sec_data.get("total_assets") if sec_data else None,
        "total_debt": sec_data.get("total_debt") if sec_data else None,
        "income_statement": [],
        "balance_sheet": [],
        "cash_flow": [],
    }
    LOGGER.info("Built lightweight Yahoo financial profile for %s", symbol)
    return profile


# Optional EdgarTools integration for this module
try:
    import edgartools as edgar  # type: ignore
except Exception:
    edgar = None


def _configure_edgar_user_agent() -> None:
    """Configure a descriptive User-Agent for EDGAR access using secrets or env var.

    SEC prefers a User-Agent string with contact information. The contact email
    should be placed in Streamlit secrets as `EDGAR_CONTACT_EMAIL` or in the
    environment variable `EDGAR_CONTACT_EMAIL`.
    """
    try:
        contact = str(st.secrets.get("EDGAR_CONTACT_EMAIL", "") or os.environ.get("EDGAR_CONTACT_EMAIL", "")).strip()
    except Exception:
        contact = str(os.environ.get("EDGAR_CONTACT_EMAIL", "")).strip()

    if contact:
        ua = f"Stackwealth-App/1.0 (+{contact})"
    else:
        ua = "Stackwealth-App/1.0 (contact not-provided)"

    # If edgartools exposes a setter, try to use it.
    try:
        if edgar and hasattr(edgar, "set_user_agent"):
            try:
                edgar.set_user_agent(ua)
            except Exception:
                LOGGER.debug("edgartools.set_user_agent unavailable or failed")
    except Exception:
        LOGGER.debug("Failed to configure edgartools user agent via edgartools API")

    # Also expose via environment for libraries that read a custom header env var
    try:
        os.environ["EDGAR_USER_AGENT"] = ua
    except Exception:
        pass


def _fetch_sec_financials_for_symbol(ticker: str) -> dict[str, Any] | None:
    """Best-effort SEC extraction using EdgarTools; returns minimal numeric fields or None.

    This function is cached via Streamlit's cache_data where called by the app layer.
    """
    # Ensure we set a descriptive User-Agent before making any EDGAR requests
    try:
        _configure_edgar_user_agent()
    except Exception:
        LOGGER.debug("Failed to configure EDGAR user-agent hook")

    if not edgar:
        return None
    try:
        # Try common EdgarTools fetchers; adapt to available API
        candidates = [
            getattr(edgar, "get_company_filings", None),
            getattr(edgar, "fetch_filings", None),
            getattr(edgar, "search_filings", None),
            getattr(edgar, "get_filings", None),
        ]
        filings = None
        for fn in candidates:
            if callable(fn):
                try:
                    filings = fn(ticker, filing_type="10-K", count=2)
                    break
                except TypeError:
                    try:
                        filings = fn(ticker)
                        break
                    except Exception:
                        continue
        if not filings:
            return None

        # Attempt to extract text bodies
        bodies: list[str] = []
        if isinstance(filings, dict):
            for v in filings.values():
                if isinstance(v, list):
                    for it in v:
                        if isinstance(it, dict):
                            bodies.append(it.get("content") or it.get("text") or "")
        elif isinstance(filings, list):
            for it in filings:
                if isinstance(it, dict):
                    bodies.append(it.get("content") or it.get("text") or "")
                elif isinstance(it, str):
                    bodies.append(it)

        text = "\n\n".join([b for b in bodies if b])
        if not text:
            return None

        def _parse_amount(label: str) -> float | None:
            pattern = re.compile(r"""(?:%s)\s{0,60}[$]?[\(]?[0-9,\.\)\-]+""" % re.escape(label), re.IGNORECASE)
            m = pattern.search(text)
            if not m:
                return None
            token = m.group(0)
            nums = re.findall(r"[\-\(]?[0-9,]{1,}[\.0-9]*[\)]?", token)
            if not nums:
                return None
            raw = nums[-1]
            raw = raw.replace("(", "-").replace(")", "")
            raw = raw.replace(",", "")
            try:
                return float(raw)
            except Exception:
                return None

        total_assets = _parse_amount("Total Assets") or _parse_amount("Total assets")
        total_debt = _parse_amount("Total Debt") or _parse_amount("Long-Term Debt") or _parse_amount("Short-Term Debt")
        operating_cash_flow = _parse_amount("Operating Cash Flow") or _parse_amount("Net Cash Provided By Operating Activities")

        result = {}
        if total_assets is not None:
            result["total_assets"] = total_assets
        if total_debt is not None:
            result["total_debt"] = total_debt
        if operating_cash_flow is not None:
            result["operating_cash_flow"] = operating_cash_flow
        return result if result else None
    except Exception:
        LOGGER.exception("Edgar extraction failed for %s", ticker)
        return None


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


def get_model_name() -> str:
    """Resolve the Gemini model name from Streamlit secrets."""
    model = str(st.secrets.get("GEMINI_MODEL", "")).strip()
    if not model:
        raise RuntimeError("GEMINI_MODEL is not configured in Streamlit secrets.")
    return model


def _gemini_generate_with_retries(client: genai.Client, prompt: str, models: list[str] | None = None, max_attempts: int = 3, initial_delay: float = 1.0) -> Any:
    """Attempt generation across one or more Gemini models with retries/backoff on transient 503/429 errors.

    - `models` is tried in order. For each model we will retry up to `max_attempts` with exponential backoff on retriable errors.
    - Raises the last exception encountered if all attempts/models fail.
    """
    if models is None:
        models = [get_model_name()]

    last_exc = None
    for model in models:
        if not model:
            continue
        delay = initial_delay
        for attempt in range(1, max_attempts + 1):
            try:
                LOGGER.debug("Gemini generate attempt model=%s attempt=%d", model, attempt)
                return client.models.generate_content(model=model, contents=prompt)
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                # If it's a model-not-found style error, raise immediately so caller can inspect available models
                if "404" in msg or "not found" in msg:
                    try:
                        available = [m.name for m in client.models.list()]
                    except Exception:
                        available = None
                    avail_text = ", ".join(available) if available else "(could not list models)"
                    LOGGER.warning("Gemini model not found: %s; available: %s", model, avail_text)
                    raise RuntimeError(f"Model '{model}' not available for generation. Available: {avail_text}") from exc

                # Retries for transient service-side errors
                if any(k in msg for k in ("503", "unavailable", "high demand", "too many requests", "rate limit", "429")):
                    LOGGER.warning("Transient Gemini error for model=%s attempt=%d: %s", model, attempt, msg)
                    LOGGER.debug("Full exception", exc_info=exc)
                    if attempt < max_attempts:
                        time.sleep(delay)
                        delay *= 2
                        continue
                # Non-retriable or exhausted attempts -> break to try next model
                break

    # If we exhausted all models/attempts, raise the last exception
    if last_exc:
        raise last_exc
    raise RuntimeError("Gemini generation failed without a specific exception.")


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_fmp_financial_profile(ticker_symbol: str) -> dict[str, Any]:
    symbol = str(ticker_symbol or "").upper().strip()
    if not symbol:
        raise ValueError("Ticker symbol is required.")

    if symbol not in FMP_SUPPORTED_TICKERS:
        LOGGER.info("Ticker %s is not in the FMP-supported list; using Yahoo finance profile", symbol)
        return _fetch_yahoo_financial_profile(symbol)

    api_key = str(st.secrets.get("FMP_API_KEY", "") or os.environ.get("FMP_API_KEY", "")).strip()
    if not api_key:
        LOGGER.warning("FMP_API_KEY missing; falling back to Yahoo finance profile for %s", symbol)
        return _fetch_yahoo_financial_profile(symbol)

    LOGGER.debug("Starting FMP financial profile fetch for %s", symbol)

    endpoints = {
        "income_statement": f"https://financialmodelingprep.com/stable/income-statement?symbol={symbol}&limit=5&apikey={api_key}",
        "balance_sheet": f"https://financialmodelingprep.com/stable/balance-sheet-statement?symbol={symbol}&limit=5&apikey={api_key}",
        "cash_flow": f"https://financialmodelingprep.com/stable/cash-flow-statement?symbol={symbol}&limit=5&apikey={api_key}",
    }

    def _fetch_one(name: str, url: str) -> tuple[str, list[dict[str, Any]]]:
        LOGGER.debug("Fetching FMP endpoint %s for %s", name, symbol)
        response = requests.get(url, timeout=25)
        LOGGER.debug(
            "FMP endpoint %s for %s returned HTTP %s",
            name,
            symbol,
            response.status_code,
        )
        if response.status_code in (401, 403):
            LOGGER.warning(
                "FMP endpoint %s for %s returned %s; body=%s",
                name,
                symbol,
                response.status_code,
                response.text[:500],
            )
            raise RuntimeError(f"FMP endpoint '{name}' returned HTTP {response.status_code} for {symbol}; check endpoint version, plan, and key.")
        if response.status_code != 200:
            LOGGER.warning(
                "FMP endpoint %s for %s returned unexpected HTTP %s; body=%s",
                name,
                symbol,
                response.status_code,
                response.text[:500],
            )
            raise RuntimeError(f"FMP endpoint '{name}' failed with HTTP {response.status_code} for {symbol}.")
        payload = response.json()
        if not isinstance(payload, list) or len(payload) == 0:
            LOGGER.warning("FMP endpoint %s for %s returned empty payload", name, symbol)
            raise RuntimeError(f"FMP endpoint '{name}' returned empty data for {symbol}.")
        LOGGER.debug("FMP endpoint %s for %s returned %d rows", name, symbol, len(payload))
        return name, payload

    results: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_fetch_one, name, url): name for name, url in endpoints.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                key, payload = future.result()
                results[key] = payload
                LOGGER.info("Completed FMP endpoint %s for %s", key, symbol)
            except Exception as exc:
                LOGGER.exception("FMP endpoint %s failed for %s; falling back to Yahoo profile", name, symbol)
                return _fetch_yahoo_financial_profile(symbol)

    LOGGER.info("Completed FMP financial profile fetch for %s", symbol)
    return {
        "ticker": symbol,
        "source": "fmp",
        "income_statement": results.get("income_statement", []),
        "balance_sheet": results.get("balance_sheet", []),
        "cash_flow": results.get("cash_flow", []),
    }



def generate_portfolio_ai_overview(payload: dict[str, Any]) -> str:
    """Generate a portfolio-level institutional AI Overview report.

    `payload` should follow the portfolio_overview_input contract described in streamlit_app.
    This function returns the generated markdown string.
    """
    symbol = "PORTFOLIO"
    # Keep prompt concise and structured — LLM used for synthesis only.
    safe_payload = json.dumps(payload, indent=2, default=str)
    prompt = f"""
You are the Lead Investment Architect for a disciplined institutional allocator.
Do not use platitudes. Produce a rigorous, evidence-based portfolio evaluation for the provided portfolio payload.

REQUIRED OUTPUT SECTIONS (use these exact headers):
### 1. High-Level Portfolio Vital Signs
### 2. Multi-Period CAGR Projections (3-Year, 5-Year, 10-Year)
### 3. Structural Risk Audit & "What to Watch Out For"
### 4. Capital Efficiency & The "Economic Spread" Matrix
### 5. Tactical Rebalancing Suggestions

INPUT_PAYLOAD_JSON:
{safe_payload}

Output rules:
- Use numeric values from the payload when possible and cite ticker-level lines.
- Provide 1 table for section 2 and 1 table for section 4.
- Keep bullets short (max 2-3 lines).
- Do NOT provide price targets or broad market commentary.
""".strip()

    client = _gemini_client()
    try:
        response = _gemini_generate_with_retries(client, prompt, models=[get_model_name()])
    except Exception:
        raise
    
    report_text = _response_text(response)
    
    # Add data sources footer
    sources = ["Yahoo Finance (real-time market data)", "User portfolio holdings"]
    if edgar:
        sources.append("SEC EDGAR (via EdgarTools)")
    sources_footer = _build_data_sources_footer(sources)
    
    # Add data source acknowledgment for portfolio overview
    data_note = """## Data Sources & Limitations

This portfolio overview is built from:
- **Real-time market data**: Yahoo Finance fast_info and SEC EDGAR filings
- **Holdings data**: Your current portfolio positions and transaction history
- **P&L calculations**: Realized and unrealized gains/losses based on your cost basis

For deep historical financial statement analysis (5+ year forensic trends), direct EDGAR SEC filing access or premium financial data providers are recommended.

---

"""
    
    result = data_note + report_text
    if sources_footer:
        result = result + "\n\n" + sources_footer
    
    return result


@st.cache_data(ttl=86400, show_spinner=False)
def generate_comparative_investment_thesis(
    ticker: str,
    allocation_details: dict[str, Any],
    financial_profile: dict[str, Any],
    past_tx_text: str = "",
    current_tx_text: str = "",
) -> str:
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        raise RuntimeError("Ticker symbol is required for thesis generation.")
    if not allocation_details or not isinstance(allocation_details, dict):
        raise RuntimeError("Allocation details are required and must be a dict.")
    if not financial_profile or not isinstance(financial_profile, dict):
        raise RuntimeError("Financial profile is required and must be a dict.")

    current_text = str(current_tx_text or "").strip()
    past_text = str(past_tx_text or "").strip()

    financial_profile_json_dump = json.dumps(financial_profile, indent=2, default=str)

    if past_text and current_text:
        prompt = f"""Act as a world-class institutional value investor, forensic equity research analyst, and expert capital allocator. You have been provided with 5 years of historical financial data from the Financial Modeling Prep (FMP) API, the user's current allocation footprint, and TWO distinct corporate earnings call transcripts: a 'Baseline/Past' transcript and a 'Current/Latest' transcript.

Your primary objective is to conduct a strict 'Strategic Commitment Audit' to evaluate if management is sticking to its stated vision, milestones, and promises, or if they are moving goalposts, hiding execution failures, or shifting strategy due to deteriorating fundamentals.

Important: The target ticker for this assignment is {symbol}. Focus your entire analysis on {symbol} and its business.

### USER'S CURRENT ALLOCATION DETAIL:
- Shares Owned: {allocation_details['shares']}
- Average Cost Basis: ${allocation_details['avg_cost']:.2f}
- Allocation Weighting in Total Portfolio: {allocation_details['portfolio_weight_pct']:.2f}%

### HISTORICAL FUNDAMENTAL STATEMENT DATASETS (FMP JSON):
{financial_profile_json_dump}

### TRANSCRIPT ARTIFACTS FOR ANALYSIS:
- BASELINE/PAST TRANSCRIPT TEXT:
{past_text}

- CURRENT/LATEST TRANSCRIPT TEXT:
{current_text}

Structure your comprehensive evaluation into the following distinct sections:

### 1. The Promise vs. Performance Ledger
Construct a clean Markdown table mapping out explicit promises or metric guidance made in the Baseline Transcript vs. the actual execution/results reported in the Current Transcript.
- Column 1: Stated Goal / Management Promise (Past)
- Column 2: Actual Outcome / Current Status (Present)
- Column 3: Verdict (Delivered / Progressing / Broken / Moving Goalposts)

### 2. Executive Summary & Thesis Drift
* Provide a 2-3 sentence overview of whether the core business model and economic moat have remained stable or drifted over this timeline.
* Identify if management's tone, rhetoric, or preferred Key Performance Indicators (KPIs) have shifted.

### 3. Ecosystem Interdependency & Capital Allocation Discipline
* Review the historical 5-year financials provided by FMP. Cross-reference management's past capital allocation commentary with actual balance sheet behavior today.
* Evaluate ROIC relative to WACC using the provided arrays and state whether they are compounding value or shifting to defensive behavior.

### 4. The Draconian Scenarios & Broken Moat Red Flags
* Based on the Q&A sections of both transcripts, evaluate if prior structural risks manifested.
* Highlight contradictions or evasive language in the current Q&A when compared against past guidance.

### 5. Multi-Year Valuation Scenarios & Return Profiles
* Map Bull/Base/Bear profiles for a 3-to-4-year horizon, adjusting management execution credibility.

### 6. Final Investment Verdict & Monitor Dashboard
* **The Decision:** Buy, Hold, Watchlist, or Avoid. Address whether current portfolio weight ({allocation_details['portfolio_weight_pct']:.2f}%) is appropriate.
* **The Promise-Tracker Dashboard:** Provide 3 forward operational metrics or milestones to monitor quarter-over-quarter.
""".strip()
    elif current_text or past_text:
        prompt = f"""Act as a world-class institutional value investor, forensic equity research analyst, and expert capital allocator. You have been provided with 5 years of historical financial data from the Financial Modeling Prep (FMP) API, the user's current allocation footprint, and one current earnings call transcript.

Important: The target ticker for this assignment is {symbol}. Focus your entire analysis on {symbol} and its business.

### USER'S CURRENT ALLOCATION DETAIL:
- Shares Owned: {allocation_details['shares']}
- Average Cost Basis: ${allocation_details['avg_cost']:.2f}
- Allocation Weighting in Total Portfolio: {allocation_details['portfolio_weight_pct']:.2f}%

### HISTORICAL FUNDAMENTAL STATEMENT DATASETS (FMP JSON):
{financial_profile_json_dump}

### CURRENT/LATEST TRANSCRIPT TEXT:
{current_text}

Structure your comprehensive evaluation into the following distinct sections:

### 1. Current Transcript-to-Financial Timeline Ledger
Create a Markdown table that maps current management claims versus evidence in the 5-year financial history.
- Column 1: Current Claim / KPI Focus
- Column 2: 5-Year Financial Evidence
- Column 3: Verdict (Supported / Weakly Supported / Contradicted)

### 2. Executive Summary & Thesis Drift
* Provide a 2-3 sentence view on business model durability and moat trajectory.
* Identify key language/tone shifts and what they imply for execution quality.

### 3. Ecosystem Interdependency & Capital Allocation Discipline
* Evaluate 5-year capital allocation behavior and management discipline using FMP history.
* Estimate whether the business appears to be compounding value (ROIC vs WACC framing) or deteriorating.

### 4. Draconian Scenarios & Broken Moat Red Flags
* Identify key downside pathways including margin traps, demand deterioration, and balance-sheet stress.

### 5. Multi-Year Valuation Scenarios & Return Profiles
* Build Bull/Base/Bear profiles over 3-to-4 years with assumptions tied to execution credibility.

### 6. Final Investment Verdict & Monitor Dashboard
* **The Decision:** Buy, Hold, Watchlist, or Avoid, including whether portfolio weight ({allocation_details['portfolio_weight_pct']:.2f}%) is appropriate.
* **The Dashboard:** Provide 3 concrete forward metrics/milestones to track.
""".strip()
    else:
        prompt = f"""Act as a world-class institutional value investor, forensic equity research analyst, and expert capital allocator. You have been provided with 5 years of historical financial data from the Financial Modeling Prep (FMP) API or Yahoo Finance fallback data, and the user's current allocation footprint.

Important: The target ticker for this assignment is {symbol}. Focus your entire analysis on {symbol} and its business.

### USER'S CURRENT ALLOCATION DETAIL:
- Shares Owned: {allocation_details['shares']}
- Average Cost Basis: ${allocation_details['avg_cost']:.2f}
- Allocation Weighting in Total Portfolio: {allocation_details['portfolio_weight_pct']:.2f}%

### HISTORICAL FUNDAMENTAL STATEMENT DATASETS:
{financial_profile_json_dump}

No transcript text was provided. Base your thesis on the financial profile, balance sheet/income statement trends, capital allocation history, market positioning, and the portfolio weight.

Structure your comprehensive evaluation into the following distinct sections:

### 1. Financial History Ledger
Construct a clean Markdown table mapping the most important trends, inflections, and risks visible in the provided financial history.
- Column 1: Metric / Trend
- Column 2: Evidence from the historical data
- Column 3: Verdict (Improving / Stable / Deteriorating / Mixed)

### 2. Executive Summary & Thesis Drift
* Provide a 2-3 sentence view on business model durability and moat trajectory.
* State whether the thesis is strong enough to support the current weight.

### 3. Ecosystem Interdependency & Capital Allocation Discipline
* Evaluate capital allocation behavior and management discipline using the historical data.
* Estimate whether the business appears to be compounding value or deteriorating.

### 4. Draconian Scenarios & Broken Moat Red Flags
* Identify key downside pathways including margin traps, demand deterioration, and balance-sheet stress.

### 5. Multi-Year Valuation Scenarios & Return Profiles
* Build Bull/Base/Bear profiles over a 3-to-4-year horizon with assumptions tied to execution credibility.

### 6. Final Investment Verdict & Monitor Dashboard
* **The Decision:** Buy, Hold, Watchlist, or Avoid, including whether portfolio weight ({allocation_details['portfolio_weight_pct']:.2f}%) is appropriate.
* **The Dashboard:** Provide 3 concrete forward metrics/milestones to track.
""".strip()

    client = _gemini_client()
    response = _gemini_generate_with_retries(client, prompt)
    
    report_text = _response_text(response)
    
    # Add data sources footer
    sources = ["Financial Modeling Prep (historical financials)", "User allocation details"]
    if edgar:
        sources.append("SEC EDGAR (via EdgarTools)")
    if past_tx_text or current_tx_text:
        sources.append("Earnings call transcripts (user-provided)")
    sources_footer = _build_data_sources_footer(sources)
    
    result = report_text
    if sources_footer:
        result = result + "\n\n" + sources_footer
    
    return result


def _build_data_limitation_disclaimer(symbol: str, has_statements: bool = False) -> str:
    """Build a data limitation acknowledgment for reports when historical financials are unavailable."""
    if has_statements:
        return ""
    
    return f"""## Acknowledgment of Data Limitations

**Note:** The raw API data payload for historical statement line items (income_statement, balance_sheet, cash_flow) for {symbol} was empty or incomplete. 

To deliver comprehensive analysis despite limited historical financial data, this report:
- Relies on currently available market data from Yahoo Finance and SEC EDGAR
- Uses real-time fundamentals (current price, shares outstanding, recent FCF if available)
- Focuses on forward-looking valuation and risk assessment rather than deep historical trend analysis
- May reconstruct or estimate metrics based on publicly available quarterly filings

For institutional-grade forensic analysis with full historical ledgers (2020-present), direct access to SEC filings via EDGAR or a premium financial data provider is recommended.

---

""".strip()


def _build_data_sources_footer(sources: list[str]) -> str:
    """Build a footer showing which data sources were used in the analysis."""
    if not sources:
        return ""
    
    sources_text = ", ".join(sources)
    return f"""## Data Sources

This analysis was built using data from: **{sources_text}**

---
""".strip()


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
        response = _gemini_generate_with_retries(client, prompt, models=[model_name])
    except Exception:
        # Let caller surface the underlying exception
        raise
    
    # Fetch profile to check data availability
    profile = _fetch_yahoo_financial_profile(symbol)
    has_statements = bool(profile.get("income_statement") or profile.get("balance_sheet") or profile.get("cash_flow"))
    
    # Build disclaimer if statements are empty
    disclaimer = _build_data_limitation_disclaimer(symbol, has_statements)
    report_text = _response_text(response)
    
    # Add data sources footer
    sources = ["Yahoo Finance (live_price, shares_outstanding, FCF)"]
    if edgar:
        sources.append("SEC EDGAR (via EdgarTools)")
    sources_footer = _build_data_sources_footer(sources)
    
    result = report_text
    if disclaimer:
        result = disclaimer + "\n\n" + result
    if sources_footer:
        result = result + "\n\n" + sources_footer
    
    return result


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
        response = _gemini_generate_with_retries(client, prompt, models=[model_name])
    except Exception:
        raise
    
    # Fetch profile to check data availability
    profile = _fetch_yahoo_financial_profile(symbol)
    has_statements = bool(profile.get("income_statement") or profile.get("balance_sheet") or profile.get("cash_flow"))
    
    # Build disclaimer if statements are empty
    disclaimer = _build_data_limitation_disclaimer(symbol, has_statements)
    report_text = _response_text(response)
    
    # Add data sources footer
    sources = ["User-provided earnings transcript", "Yahoo Finance (company fundamentals)"]
    if edgar:
        sources.append("SEC EDGAR (via EdgarTools)")
    sources_footer = _build_data_sources_footer(sources)
    
    result = report_text
    if disclaimer:
        result = disclaimer + "\n\n" + result
    if sources_footer:
        result = result + "\n\n" + sources_footer
    
    return result


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
Important: The target ticker for this assignment is {symbol}. Focus your entire analysis on {symbol} and its business. If the provided transcript discusses other tickers or companies, ignore those references and do not use them as the basis for your thesis.
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
        response = _gemini_generate_with_retries(client, prompt, models=[get_model_name()])
    except Exception:
        raise
    
    report_text = _response_text(response)
    
    # Add data sources footer
    sources = ["Yahoo Finance (market fundamentals)", "User allocation details"]
    if edgar:
        sources.append("SEC EDGAR (via EdgarTools)")
    if transcript_text:
        sources.append("Earnings call transcript (user-provided)")
    sources_footer = _build_data_sources_footer(sources)
    
    result = report_text
    if sources_footer:
        result = result + "\n\n" + sources_footer
    
    return result
