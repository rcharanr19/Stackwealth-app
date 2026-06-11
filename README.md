# StackWealth

StackWealth tracks a global, multi-currency equity portfolio with live market data, FX normalization, and performance analytics.

## Stack

- Python 3.10+
- customtkinter (desktop UI)
- streamlit (web UI for Streamlit Community Cloud)
- pandas, numpy, numpy-financial
- jinja2 (required for pandas Styler rendering in Streamlit)
- yfinance
- pyinstaller

## Features

- Internal portfolio position schema with ticker, company, shares, average price, and native currency.
- Live quote and market cap fetching through Yahoo Finance.
- Live FX conversion to a unified USD base from USD/CAD/SEK/AUD (and extensible).
- Global mode toggle:
  - `native`: each row in its local currency
  - `usd`: all valuation metrics normalized to USD
- Portfolio KPI dashboard:
  - Total Portfolio Value (USD)
  - All-Time Portfolio P&L (USD)
  - Blended Portfolio XIRR
  - Top Performer card
- Scrollable holdings table with:
  - asset identity
  - market data
  - position metrics
  - change %, XIRR, P&L
  - allocation % and progress bar
- Resilience:
  - all market/FX API calls wrapped in `try/except`
  - local cache fallback when offline/rate-limited
  - online/offline status indicator
- Thread-safe startup and refresh execution using `threading.Thread`.

## Run Locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

For the Streamlit Cloud version:

```powershell
streamlit run streamlit_app.py
```

## Portfolio Input

Copy `data/portfolio.example.json` to `data/portfolio.json`, then edit your private file:

- `positions`: current holdings metadata
- `transactions`: dated cash flows used for XIRR

Cash flow convention:

- buys/invested capital: negative
- dividends/sale proceeds: positive

The first time the app starts, it uses `data/portfolio.json` as the baseline universe. Robinhood sync is then restricted to the assets in that file. After the first successful sync, any newly discovered holdings from transactions are added to the tracked universe automatically on later runs.

## Streamlit Community Cloud

Deploy the web app by pointing Streamlit Cloud at `streamlit_app.py`.

Add an app password in Streamlit Cloud secrets to keep the app private behind a sign-in gate:

```toml
APP_PASSWORD = "your-strong-password"
```

Robinhood sync now uses a popup login form in the app. Credentials are entered only when you click Sync and are not written to files, secrets, or logs.

First-run behavior:

- `portfolio.json` is treated as the baseline universe.
- Only those baseline assets are used for the initial Robinhood sync.
- The first completed sync persists initialization state in `data/alphavault.db`.

Redeploy behavior:

- The app checks the persisted sync profile on startup.
- If initialization already completed, later syncs expand tracked assets when new holdings appear in transactions.
- If initialization has not completed, the app falls back to the baseline in `portfolio.json`.

Notes:

- Robinhood credentials are transient and only used for the sync request in the current session.
- The Streamlit sync flow clears any saved Robinhood pickle before login and disables session reuse, so a fresh login challenge is required instead of silently reusing a cached session.
- `Change` is defined as total return percentage: `(ending value - baseline value) / baseline value`.
- `XIRR` uses actual transaction dates and terminal market value for open positions.

## Build Executable (Windows)

```powershell
pyinstaller --noconfirm --onedir --windowed --name StackWealth --add-data "data;data" app.py
```

Output bundle:

- `dist/StackWealth/StackWealth.exe`

## Notes

- If your installed `numpy-financial` does not expose `xirr`, StackWealth uses a robust fallback XIRR solver with date-based discounting.
- First launch may take a few seconds while initial market and FX snapshots load.
