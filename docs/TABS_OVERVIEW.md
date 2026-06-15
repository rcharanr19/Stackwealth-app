**Project Overview**
- **Purpose**: Portfolio analytics and LLM-driven investment-thesis tools powered by Streamlit and Google Gemini, with financial data from FinancialModelingPrep (FMP), Yahoo (yfinance) fallback, and optional SEC extraction via edgartools.
- **Entry point**: `streamlit_app.py` runs the UI and calls helpers in the `tabs` package.

**Tabs Package**
- **`tabs/ai_analysis.py`**: Core AI and financial-data helpers used by the Streamlit UI. Responsibilities:
  - Build structured LLM prompts (portfolio-level and single/comparative thesis generation).
  - Fetch structured financial statements from FMP (`limit=5`) when available.
  - Provide a Yahoo (`yfinance`) fallback profile builder for unsupported tickers or when FMP keys are missing.
  - Best-effort SEC extraction using `edgartools` to retrieve numeric fields from filings (10-Ks) when available.
  - Integrate with Google GenAI (`google.genai`) including retry/backoff logic for model generation.

**Important functions in `tabs/ai_analysis.py`**
- **`generate_portfolio_ai_overview(payload)`**: Produces a portfolio-level markdown report via Gemini prompts.
- **`generate_comparative_investment_thesis(ticker, allocation_details, financial_profile, past_tx_text, current_tx_text)`**: Builds a comparative thesis using FMP/Yahoo data and optional transcripts.
- **`generate_single_investment_thesis(ticker_symbol, allocation_details, transcript_text)`**: Creates a single-ticker institutional thesis prompt.
- **`get_reverse_dcf_analysis(ticker_symbol)`**: Constructs a reverse-DCF prompt from price, shares outstanding, and FCF.
- **`get_transcript_mosaic_analysis(ticker_symbol, transcript_text)`**: Forensic earnings-call linguistic analysis.
- **`fetch_fmp_financial_profile(ticker_symbol)`**: Parallel fetch of FMP income, balance-sheet, and cash-flow endpoints (uses `FMP_API_KEY` from secrets/env). Falls back to Yahoo if FMP not available.
- **`_fetch_yahoo_financial_profile(symbol)`**: Lightweight Yahoo profile builder using `yfinance`; calls SEC helper for additional numeric fields.
- **`_fetch_sec_financials_for_symbol(ticker)`**: Best-effort EDGAR extraction using `edgartools` (optional). Extracts `total_assets`, `total_debt`, `operating_cash_flow` via regex heuristics.
- **LLM helpers**: `_gemini_client()`, `_gemini_generate_with_retries(...)`, `_response_text(response)`, `get_model_name()`.
- **Utilities**: `_df_to_period_records`, `_extract_statement_value`, `_safe_float`, `_extract_fcf`.

**Data sources & behavior**
- **FMP (FinancialModelingPrep)**: Preferred structured statements; limited to tickers in `FMP_SUPPORTED_TICKERS` and requires `FMP_API_KEY` for full access.
- **Yahoo (yfinance)**: Fallback for profile/price/info when FMP is unavailable.
- **EDGAR (edgartools)**: Optional; guarded import. When present, `tabs/ai_analysis.py` now calls a user-agent hook that uses `EDGAR_CONTACT_EMAIL` from Streamlit secrets or env to set a descriptive User-Agent string.
- **Gemini/Google GenAI**: Requires `GEMINI_API_KEY` and `GEMINI_MODEL` in Streamlit secrets to generate LLM outputs.

**Caching & performance**
- **`@st.cache_data`**: Used on heavy helpers & generators (TTL often set to 86400) to limit repeated network/LLM calls.
- **Concurrency**: FMP endpoints fetched in parallel via `ThreadPoolExecutor` to reduce latency.

**Secrets / configuration**
- **Required for full features**: `GEMINI_API_KEY`, `GEMINI_MODEL` (Gemini). 
- **Optional but recommended**: `FMP_API_KEY` (FMP data), `EDGAR_CONTACT_EMAIL` (SEC User-Agent contact).
- Add secrets via Streamlit `st.secrets` or environment variables.

**Operational checklist**
- Install dependencies:
  ```bash
  pip install -r requirements.txt
  ```
- Add secrets (example `~/.streamlit/secrets.toml`):
  ```toml
  GEMINI_API_KEY = "your_gemini_api_key"
  GEMINI_MODEL = "your_model_name"
  FMP_API_KEY = "optional_fmp_key"
  EDGAR_CONTACT_EMAIL = "you@example.com"
  ```
- Verify module compiles:
  ```bash
  python -m py_compile tabs/ai_analysis.py
  ```
- Run app locally:
  ```bash
  streamlit run streamlit_app.py
  ```

**Risks & recommended improvements**
- **EDGAR parsing fragility**: Regex extraction is heuristic—consider using XBRL or edgartools structured returns when available.
- **Rate limits**: Gemini and FMP may throttle; consider longer caching or request queuing.
- **User-Agent / contact**: Put a valid contact email in `EDGAR_CONTACT_EMAIL` to comply with SEC guidance.
- **Testing**: Add unit tests for `_parse_amount`, `_extract_statement_value`, and a mocked integration test for FMP/edgartools calls.

**References**
- `tabs/ai_analysis.py` — [tabs/ai_analysis.py](tabs/ai_analysis.py)
- `streamlit_app.py` — [streamlit_app.py](streamlit_app.py)
- `requirements.txt` — [requirements.txt](requirements.txt)

---

Generated on 2026-06-13.
