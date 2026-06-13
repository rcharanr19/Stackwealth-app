from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
import io
import logging
from datetime import date
from typing import Iterable

import pandas as pd
import yfinance as yf
import os
import requests
import time

from .cache_store import CacheStore
from .models import Quote, Snapshot


_yf_logger = logging.getLogger("yfinance")
_yf_logger.handlers.clear()
_yf_logger.addHandler(logging.NullHandler())
_yf_logger.propagate = False
_yf_logger.disabled = True


LOGGER = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self, cache: CacheStore, base_currency: str = "USD") -> None:
        self.cache = cache
        self.base_currency = base_currency

    def _quiet_call(self, fn, *args, **kwargs):
        sink = io.StringIO()
        with redirect_stdout(sink), redirect_stderr(sink):
            return fn(*args, **kwargs)

    def _fetch_quote_live(self, ticker: str) -> Quote:
        LOGGER.debug("Fetching live quote for %s", ticker)
        tk = yf.Ticker(ticker)
        fast_info = self._quiet_call(lambda: getattr(tk, "fast_info", None)) or {}

        price = fast_info.get("lastPrice")
        market_cap = fast_info.get("marketCap")
        previous_close = (
            fast_info.get("previousClose")
            or fast_info.get("regularMarketPreviousClose")
            or fast_info.get("previous_close")
        )

        if price is None or previous_close is None:
            history = self._quiet_call(tk.history, period="5d")
            if not history.empty:
                close_series = history["Close"].dropna()
                if price is None and not close_series.empty:
                    price = float(close_series.iloc[-1])
                if previous_close is None and len(close_series) >= 2:
                    previous_close = float(close_series.iloc[-2])

        company_name = None
        raw_currency = fast_info.get("currency")
        currency = str(raw_currency).upper().strip() if raw_currency else None

        quote = Quote(
            price=float(price) if price is not None else None,
            market_cap=float(market_cap) if market_cap is not None else None,
            previous_close=float(previous_close) if previous_close is not None else None,
            company_name=company_name,
            currency=currency,
        )
        LOGGER.debug(
            "Fetched live quote for %s: price=%s market_cap=%s previous_close=%s currency=%s",
            ticker,
            quote.price,
            quote.market_cap,
            quote.previous_close,
            quote.currency,
        )
        return quote

    def _fetch_company_profile(self, ticker: str) -> tuple[str | None, str | None]:
        LOGGER.debug("Fetching yfinance company profile for %s", ticker)
        tk = yf.Ticker(ticker)
        # Prefer lightweight fast_info for company metadata to avoid heavy .info network calls
        fast_info = self._quiet_call(lambda: getattr(tk, "fast_info", None)) or {}
        company_name = None
        raw_currency = None
        if isinstance(fast_info, dict):
            company_name = fast_info.get("longName") or fast_info.get("shortName") or fast_info.get("short_name")
            raw_currency = fast_info.get("currency")

        # Fallback to (possibly heavier) info only if fast_info didn't provide metadata
        if not company_name or not raw_currency:
            try:
                info = self._quiet_call(lambda: getattr(tk, "info", None)) or {}
                if isinstance(info, dict):
                    company_name = company_name or info.get("longName") or info.get("shortName")
                    raw_currency = raw_currency or info.get("currency")
            except Exception:
                LOGGER.debug("yfinance company profile fallback info fetch failed for %s", ticker)

        currency = str(raw_currency).upper().strip() if raw_currency else None
        LOGGER.debug("Fetched yfinance company profile for %s: company_name=%s currency=%s", ticker, company_name, currency)
        return company_name, currency

    def fetch_asset_profile(self, ticker: str) -> dict[str, float | str | None]:
        LOGGER.debug("Building asset profile for %s", ticker)
        try:
            quote = self._fetch_quote_live(ticker)
            company_name, currency = self._fetch_company_profile(ticker)
            profile = {
                "price": quote.price,
                "market_cap": quote.market_cap,
                "company_name": company_name or quote.company_name,
                "currency": currency or quote.currency,
            }
            LOGGER.info("Asset profile ready for %s via yfinance", ticker)
            return profile
        except Exception as primary_exc:
            LOGGER.warning("Primary yfinance asset profile failed for %s: %s", ticker, primary_exc)
            # Attempt to fall back to FinancialModelingPrep (if API key provided)
            try:
                fmp = self._fetch_company_profile_fmp(ticker)
                if fmp:
                    LOGGER.info("Asset profile ready for %s via FMP fallback", ticker)
                    return {
                        "price": fmp.get("price"),
                        "market_cap": fmp.get("market_cap"),
                        "company_name": fmp.get("company_name"),
                        "currency": fmp.get("currency"),
                    }
            except Exception:
                LOGGER.exception("FMP fallback failed for %s", ticker)

            # Some symbols return incomplete metadata; return a minimal profile so sync can continue.
            LOGGER.exception("Returning empty asset profile for %s after all providers failed", ticker)
            return {
                "price": None,
                "market_cap": None,
                "company_name": None,
                "currency": None,
            }

    def _fetch_company_profile_fmp(self, ticker: str) -> dict[str, object] | None:
        """Fetch basic profile and recent cash-flow/balance-sheet entries from FinancialModelingPrep.

        Requires environment variable `FMP_API_KEY` or none will be attempted.
        Returns a dict with keys: company_name, currency, price, market_cap, trailingPE, forwardPE,
        operating_cash_flow, total_assets, total_debt when available.
        """
        api_key = os.environ.get("FMP_API_KEY") or os.environ.get("FMP_API")
        if not api_key:
            LOGGER.debug("Skipping FMP profile fetch for %s because no API key is configured", ticker)
            return None

        base = "https://financialmodelingprep.com/stable"
        out: dict[str, object] = {}
        ticker_up = str(ticker or "").upper().strip()
        LOGGER.debug("Fetching FMP profile for %s", ticker_up)
        try:
            # Profile
            resp = requests.get(f"{base}/profile", params={"symbol": ticker_up, "apikey": api_key}, timeout=10)
            if resp.ok:
                data = resp.json()
                if isinstance(data, list) and data:
                    p = data[0]
                    out["company_name"] = p.get("companyName")
                    out["currency"] = p.get("currency")
                    out["price"] = float(p.get("price")) if p.get("price") is not None else None
                    out["market_cap"] = float(p.get("mktCap")) if p.get("mktCap") is not None else None
                    out["trailingPE"] = float(p.get("pe")) if p.get("pe") not in (None, "") else None
                    out["forwardPE"] = float(p.get("forwardPE")) if p.get("forwardPE") not in (None, "") else None

            # Cash flow (most recent)
            try:
                resp_cf = requests.get(f"{base}/cash-flow-statement", params={"symbol": ticker_up, "limit": 1, "apikey": api_key}, timeout=10)
                if resp_cf.ok:
                    cf = resp_cf.json()
                    if isinstance(cf, list) and cf:
                        latest = cf[0]
                        # FMP uses different key names; try common ones
                        ocf = latest.get("operatingCashFlow") or latest.get("Operating Cash Flow") or latest.get("netCashProvidedByOperatingActivities")
                        out["operating_cash_flow"] = float(ocf) if ocf not in (None, "") else None
            except Exception:
                pass

            # Balance sheet (most recent)
            try:
                resp_bs = requests.get(f"{base}/balance-sheet-statement", params={"symbol": ticker_up, "limit": 1, "apikey": api_key}, timeout=10)
                if resp_bs.ok:
                    bs = resp_bs.json()
                    if isinstance(bs, list) and bs:
                        latest = bs[0]
                        ta = latest.get("totalAssets") or latest.get("Total assets")
                        td = latest.get("totalDebt") or latest.get("totalLiabilities") or latest.get("shortTermDebt")
                        out["total_assets"] = float(ta) if ta not in (None, "") else None
                        out["total_debt"] = float(td) if td not in (None, "") else None
            except Exception:
                pass

            return out
        except Exception:
            LOGGER.exception("FMP profile fetch failed for %s", ticker_up)
            return None

    def _fetch_fx_live_to_usd(self, currencies: Iterable[str]) -> dict[str, float]:
        fx: dict[str, float] = {"USD": 1.0}
        for ccy in {c.upper().strip() for c in currencies}:
            if ccy == "USD":
                fx[ccy] = 1.0
                continue
            pair = f"USD{ccy}=X"
            tk = yf.Ticker(pair)
            history = self._quiet_call(tk.history, period="1d")
            if history.empty:
                raise RuntimeError(f"No FX data for pair {pair}")
            usd_to_ccy = float(history["Close"].iloc[-1])
            if usd_to_ccy <= 0:
                raise RuntimeError(f"Invalid FX rate for {pair}: {usd_to_ccy}")
            # Convert native currency amount to USD by dividing by USD->CCY.
            fx[ccy] = 1.0 / usd_to_ccy
        return fx

    def fetch_cutoff_prices(self, tickers: Iterable[str], cutoffs: Iterable[date]) -> dict[str, dict[date, float | None]]:
        cutoff_list = sorted(set(cutoffs))
        cutoff_keys = [c.isoformat() for c in cutoff_list]
        prices: dict[str, dict[date, float | None]] = {}
        ticker_set = sorted({t.upper().strip() for t in tickers})

        LOGGER.debug("Fetching cutoff prices for %d tickers across %d cutoffs", len(ticker_set), len(cutoff_list))

        cache_data = self.cache.load()
        cached_cutoffs = cache_data.get("cutoff_prices", {}) if isinstance(cache_data.get("cutoff_prices", {}), dict) else {}
        cache_changed = False

        missing_tickers: list[str] = []
        ticker_missing_keys: dict[str, list[str]] = {}

        for ticker in ticker_set:
            ticker_prices: dict[date, float | None] = {cutoff: None for cutoff in cutoff_list}
            ticker_cached = cached_cutoffs.get(ticker, {}) if isinstance(cached_cutoffs.get(ticker, {}), dict) else {}
            missing_cutoff_keys = [k for k in cutoff_keys if k not in ticker_cached]
            ticker_missing_keys[ticker] = missing_cutoff_keys

            if len(missing_cutoff_keys) != len(cutoff_keys):
                LOGGER.debug(
                    "Cutoff price cache hit for %s: %d/%d values cached",
                    ticker,
                    len(cutoff_keys) - len(missing_cutoff_keys),
                    len(cutoff_keys),
                )

            for cutoff in cutoff_list:
                key = cutoff.isoformat()
                if key in ticker_cached:
                    raw_value = ticker_cached.get(key)
                    ticker_prices[cutoff] = float(raw_value) if raw_value is not None else None

            if not missing_cutoff_keys:
                prices[ticker] = ticker_prices
                continue
            missing_tickers.append(ticker)
            prices[ticker] = ticker_prices

        if missing_tickers:
            LOGGER.debug("Loading historical prices for %d tickers from yfinance: %s", len(missing_tickers), ", ".join(missing_tickers))
            try:
                history = self._quiet_call(
                    yf.download,
                    tickers=" ".join(missing_tickers),
                    period="10y",
                    interval="1d",
                    auto_adjust=False,
                    group_by="ticker",
                    progress=False,
                    threads=True,
                )
            except Exception:
                LOGGER.exception("Historical price download failed for tickers: %s", ", ".join(missing_tickers))
                history = None

            for ticker in missing_tickers:
                ticker_cached = cached_cutoffs.get(ticker, {}) if isinstance(cached_cutoffs.get(ticker, {}), dict) else {}
                missing_cutoff_keys = ticker_missing_keys.get(ticker, [])
                ticker_prices = prices.get(ticker, {cutoff: None for cutoff in cutoff_list})
                close_series = None

                if isinstance(history, pd.DataFrame) and not history.empty:
                    try:
                        if isinstance(history.columns, pd.MultiIndex):
                            if ticker in history.columns.get_level_values(0):
                                ticker_df = history[ticker]
                                if "Close" in ticker_df:
                                    close_series = pd.Series(
                                        ticker_df["Close"].astype(float).values,
                                        index=pd.to_datetime(ticker_df.index).date,
                                    ).dropna()
                        elif "Close" in history and len(missing_tickers) == 1:
                            close_series = pd.Series(
                                history["Close"].astype(float).values,
                                index=pd.to_datetime(history.index).date,
                            ).dropna()
                    except Exception:
                        close_series = None

                for cutoff in cutoff_list:
                    key = cutoff.isoformat()
                    if key not in missing_cutoff_keys:
                        continue
                    value = None
                    if close_series is not None:
                        prior = close_series[close_series.index <= cutoff]
                        value = float(prior.iloc[-1]) if not prior.empty else None
                    ticker_prices[cutoff] = value
                    ticker_cached[key] = value

                prices[ticker] = ticker_prices
                cached_cutoffs[ticker] = ticker_cached
                cache_changed = True

        if cache_changed:
            LOGGER.debug("Writing cutoff price cache for %d tickers", len(cached_cutoffs))
            cache_data["cutoff_prices"] = cached_cutoffs
            self.cache.save(cache_data)

        return prices

    def refresh_snapshot(self, tickers: Iterable[str], currencies: Iterable[str]) -> Snapshot:
        ticker_list = [str(t).upper().strip() for t in tickers]
        currency_list = [str(c).upper().strip() for c in currencies]
        LOGGER.debug("Refreshing market snapshot for %d tickers and %d currencies", len(ticker_list), len(currency_list))
        cache_data = self.cache.load()
        cached_quotes = cache_data.get("quotes", {})
        cached_fx = cache_data.get("fx", {})

        quotes: dict[str, Quote] = {}
        online = True
        stale_tickers: set[str] = set()

        max_workers = max(1, min(8, len(ticker_list)))

        def _fetch_one(ticker: str) -> tuple[str, Quote | None]:
            try:
                return ticker, self._fetch_quote_live(ticker)
            except Exception:
                return ticker, None

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(_fetch_one, ticker): ticker for ticker in ticker_list}
            for future in as_completed(future_map):
                ticker = future_map[future]
                quote_ticker, quote = future.result()
                raw_quote = cached_quotes.get(quote_ticker, {})

                if quote is None or quote.price is None:
                    online = False
                    stale_tickers.add(quote_ticker)
                    LOGGER.debug("Using cached quote for %s because live fetch was unavailable", quote_ticker)
                    quotes[quote_ticker] = Quote(
                        price=raw_quote.get("price"),
                        market_cap=raw_quote.get("market_cap"),
                        previous_close=raw_quote.get("previous_close"),
                        company_name=raw_quote.get("company_name"),
                        currency=raw_quote.get("currency"),
                    )
                    continue

                quotes[quote_ticker] = Quote(
                    price=quote.price,
                    market_cap=quote.market_cap,
                    previous_close=quote.previous_close,
                    company_name=raw_quote.get("company_name") or quote.company_name,
                    currency=raw_quote.get("currency") or quote.currency,
                )
                LOGGER.debug("Live quote refreshed for %s", quote_ticker)

        try:
            LOGGER.debug("Fetching live FX rates for currencies: %s", ", ".join(sorted(set(currency_list))) or "USD")
            fx_to_usd = self._fetch_fx_live_to_usd(currency_list)
        except Exception:
            online = False
            LOGGER.exception("FX fetch failed; falling back to cached FX data")
            fx_to_usd = {k: float(v) for k, v in cached_fx.items()} if cached_fx else {"USD": 1.0}

        serializable = dict(cache_data)
        serializable["quotes"] = {k: asdict(v) for k, v in quotes.items()}
        serializable["fx"] = fx_to_usd
        LOGGER.debug("Persisting refreshed snapshot cache with %d quotes and %d FX rates", len(quotes), len(fx_to_usd))
        self.cache.save(serializable)
        return Snapshot(quotes=quotes, fx_to_usd=fx_to_usd, online=online, stale_tickers=stale_tickers)
