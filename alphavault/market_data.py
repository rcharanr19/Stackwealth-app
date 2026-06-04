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

from .cache_store import CacheStore
from .models import Quote, Snapshot


_yf_logger = logging.getLogger("yfinance")
_yf_logger.handlers.clear()
_yf_logger.addHandler(logging.NullHandler())
_yf_logger.propagate = False
_yf_logger.disabled = True


class MarketDataService:
    def __init__(self, cache: CacheStore, base_currency: str = "USD") -> None:
        self.cache = cache
        self.base_currency = base_currency

    def _quiet_call(self, fn, *args, **kwargs):
        sink = io.StringIO()
        with redirect_stdout(sink), redirect_stderr(sink):
            return fn(*args, **kwargs)

    def _fetch_quote_live(self, ticker: str) -> Quote:
        tk = yf.Ticker(ticker)
        fast_info = self._quiet_call(lambda: getattr(tk, "fast_info", None)) or {}

        price = fast_info.get("lastPrice")
        market_cap = fast_info.get("marketCap")

        if price is None:
            history = self._quiet_call(tk.history, period="5d")
            if not history.empty:
                price = float(history["Close"].iloc[-1])

        company_name = None
        raw_currency = fast_info.get("currency")
        currency = str(raw_currency).upper().strip() if raw_currency else None

        return Quote(
            price=float(price) if price is not None else None,
            market_cap=float(market_cap) if market_cap is not None else None,
            company_name=company_name,
            currency=currency,
        )

    def _fetch_company_profile(self, ticker: str) -> tuple[str | None, str | None]:
        tk = yf.Ticker(ticker)
        info = self._quiet_call(lambda: getattr(tk, "info", None))
        if not isinstance(info, dict):
            return None, None
        company_name = info.get("longName") or info.get("shortName")
        raw_currency = info.get("currency")
        currency = str(raw_currency).upper().strip() if raw_currency else None
        return company_name, currency

    def fetch_asset_profile(self, ticker: str) -> dict[str, float | str | None]:
        try:
            quote = self._fetch_quote_live(ticker)
            company_name, currency = self._fetch_company_profile(ticker)
            return {
                "price": quote.price,
                "market_cap": quote.market_cap,
                "company_name": company_name or quote.company_name,
                "currency": currency or quote.currency,
            }
        except Exception:
            # Some symbols return incomplete metadata (e.g., missing exchange timezone fields).
            # Return a minimal profile so sync can continue and the ticker is still provisioned.
            return {
                "price": None,
                "market_cap": None,
                "company_name": None,
                "currency": None,
            }

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

        cache_data = self.cache.load()
        cached_cutoffs = cache_data.get("cutoff_prices", {}) if isinstance(cache_data.get("cutoff_prices", {}), dict) else {}
        cache_changed = False

        ticker_set = sorted({t.upper().strip() for t in tickers})
        missing_tickers: list[str] = []
        ticker_missing_keys: dict[str, list[str]] = {}

        for ticker in ticker_set:
            ticker_prices: dict[date, float | None] = {cutoff: None for cutoff in cutoff_list}
            ticker_cached = cached_cutoffs.get(ticker, {}) if isinstance(cached_cutoffs.get(ticker, {}), dict) else {}
            missing_cutoff_keys = [k for k in cutoff_keys if k not in ticker_cached]
            ticker_missing_keys[ticker] = missing_cutoff_keys

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
            cache_data["cutoff_prices"] = cached_cutoffs
            self.cache.save(cache_data)

        return prices

    def refresh_snapshot(self, tickers: Iterable[str], currencies: Iterable[str]) -> Snapshot:
        cache_data = self.cache.load()
        cached_quotes = cache_data.get("quotes", {})
        cached_fx = cache_data.get("fx", {})

        quotes: dict[str, Quote] = {}
        online = True
        stale_tickers: set[str] = set()

        ticker_list = [str(t).upper().strip() for t in tickers]
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
                    quotes[quote_ticker] = Quote(
                        price=raw_quote.get("price"),
                        market_cap=raw_quote.get("market_cap"),
                        company_name=raw_quote.get("company_name"),
                        currency=raw_quote.get("currency"),
                    )
                    continue

                quotes[quote_ticker] = Quote(
                    price=quote.price,
                    market_cap=quote.market_cap,
                    company_name=raw_quote.get("company_name") or quote.company_name,
                    currency=raw_quote.get("currency") or quote.currency,
                )

        try:
            fx_to_usd = self._fetch_fx_live_to_usd(currencies)
        except Exception:
            online = False
            fx_to_usd = {k: float(v) for k, v in cached_fx.items()} if cached_fx else {"USD": 1.0}

        serializable = dict(cache_data)
        serializable["quotes"] = {k: asdict(v) for k, v in quotes.items()}
        serializable["fx"] = fx_to_usd
        self.cache.save(serializable)
        return Snapshot(quotes=quotes, fx_to_usd=fx_to_usd, online=online, stale_tickers=stale_tickers)
