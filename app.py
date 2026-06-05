from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
from datetime import date
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any
from uuid import uuid4

import customtkinter as ctk
import numpy as np

from alphavault.cache_store import CacheStore
from alphavault.finance_engine import build_metrics_table, compute_portfolio_since_start_metrics
from alphavault.logging_utils import configure_logging, is_debug_enabled, mask_account, mask_email, set_debug_logging
from alphavault.market_data import MarketDataService
from alphavault.models import Position, Snapshot, Transaction
from alphavault.robinhood_sync import RobinhoodSyncService, SyncResult
from alphavault.sqlite_store import SQLiteStore


YAHOO_REFRESH_MS = 10 * 60 * 1000


configure_logging()
LOGGER = logging.getLogger(__name__)


PALETTE = {
    "bg": "#12141a",
    "panel": "#1a1c23",
    "text": "#ecf0f1",
    "muted": "#8b93a7",
    "gain": "#2ecc71",
    "loss": "#e74c3c",
    "accent": "#4aa3ff",
}


def fmt_money(value: float | None, ccy: str = "USD") -> str:
    if value is None or not np.isfinite(value):
        return "N/A"
    symbol = "$" if ccy == "USD" else ""
    return f"{symbol}{value:,.2f} {ccy if ccy != 'USD' else ''}".strip()


def fmt_pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "N/A"
    return f"{value:,.2f}%"


def fmt_market_cap(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "N/A"
    abs_val = abs(value)
    if abs_val >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}T"
    if abs_val >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if abs_val >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    return f"{value:,.0f}"


class HoldingsTable(ctk.CTkScrollableFrame):
    def __init__(self, master: Any, sort_mode: ctk.StringVar, perf_mode: ctk.StringVar, on_sort_change, on_perf_change) -> None:
        super().__init__(master, fg_color=PALETTE["panel"], corner_radius=16)
        self.sort_mode = sort_mode
        self.perf_mode = perf_mode
        self.on_sort_change = on_sort_change
        self.on_perf_change = on_perf_change
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=3)
        self.grid_columnconfigure(2, weight=3)
        self.grid_columnconfigure(3, weight=3)
        self.grid_columnconfigure(4, weight=1)
        self.grid_columnconfigure(5, weight=1)

        sort_bar = ctk.CTkFrame(self, fg_color="transparent")
        sort_bar.grid(row=0, column=0, columnspan=6, sticky="ew", padx=10, pady=(10, 2))
        sort_bar.grid_columnconfigure(0, weight=1)
        sort_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            sort_bar,
            text="Sort By",
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(family="Bahnschrift", size=11, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        sort_menu = ctk.CTkOptionMenu(
            sort_bar,
            values=[
                "Position: Equity (Desc)",
                "Position: Equity (Asc)",
                "Performance: Change (Desc)",
                "Performance: Change (Asc)",
                "Performance: XIRR (Desc)",
                "Performance: XIRR (Asc)",
            ],
            variable=self.sort_mode,
            command=lambda _value: self.on_sort_change(),
            width=260,
        )
        sort_menu.grid(row=0, column=1, sticky="e")

        headers = ["Asset", "Market", "Position", "Performance", "Allocation", "Currency"]
        header_row = 1
        for idx, title in enumerate(headers):
            if idx == 3:
                perf_header = ctk.CTkFrame(self, fg_color="transparent")
                perf_header.grid(row=header_row, column=idx, sticky="ew", padx=10, pady=(6, 8))
                ctk.CTkLabel(
                    perf_header,
                    text=title,
                    text_color=PALETTE["muted"],
                    font=ctk.CTkFont(family="Bahnschrift", size=12, weight="bold"),
                ).pack(anchor="w")
                perf_toggle = ctk.CTkSegmentedButton(
                    perf_header,
                    values=["unrealized", "total"],
                    variable=self.perf_mode,
                    command=lambda _value: self.on_perf_change(),
                    width=160,
                )
                perf_toggle.pack(anchor="w", pady=(4, 0))
                continue
            ctk.CTkLabel(
                self,
                text=title,
                text_color=PALETTE["muted"],
                font=ctk.CTkFont(family="Bahnschrift", size=12, weight="bold"),
            ).grid(row=header_row, column=idx, sticky="w", padx=10, pady=(6, 8))

        self.rows_start = 2

    def clear_rows(self) -> None:
        for child in self.winfo_children():
            info = child.grid_info()
            if int(info.get("row", 0)) >= self.rows_start:
                child.destroy()

    def render(self, metrics, mode: str) -> None:
        self.clear_rows()

        rows = metrics.itertuples(index=False)
        for display_idx, row in enumerate(rows):
            row_data = row._asdict()
            ticker = str(row_data["ticker"])
            company = str(row_data["company_name"])
            ccy = str(row_data["currency"])
            is_closed = bool(row_data.get("is_closed", False))
            is_stale = bool(row_data.get("is_stale", False))
            price = row_data["current_price"]
            market_cap = row_data["market_cap"]
            shares = row_data["shares"]
            avg_price = row_data["avg_price"]
            change_pct = row_data["change_pct"]
            xirr = row_data["xirr"]
            alloc = row_data["allocation_pct"]
            total_change_pct = row_data.get("total_change_pct", np.nan)

            total_pnl = row_data["pnl_usd"] if mode == "usd" else row_data["pnl_native"]
            realized_pnl = row_data["realized_pnl_usd"] if mode == "usd" else row_data["realized_pnl_native"]
            unrealized_pnl = row_data["unrealized_pnl_usd"] if mode == "usd" else row_data["unrealized_pnl_native"]
            equity = row_data["equity_usd"] if mode == "usd" else row_data["equity_native"]
            out_ccy = "USD" if mode == "usd" else ccy
            perf_choice = self.perf_mode.get()
            unrealized_change_pct = row_data.get("unrealized_change_pct", np.nan)
            if perf_choice == "total":
                primary_label, primary_value = "Total P&L", total_pnl
            else:
                primary_label, primary_value = "Unrealized", unrealized_pnl

            grid_row = self.rows_start + int(display_idx)

            asset_frame = ctk.CTkFrame(self, fg_color="transparent")
            asset_frame.grid(row=grid_row, column=0, sticky="ew", padx=10, pady=8)
            ctk.CTkLabel(
                asset_frame,
                text=ticker,
                text_color=PALETTE["text"],
                font=ctk.CTkFont(family="Bahnschrift", size=16, weight="bold"),
            ).pack(anchor="w")
            ctk.CTkLabel(
                asset_frame,
                text=f"{company}{' • closed' if is_closed else ''}{' • stale cache' if is_stale else ''}",
                text_color=PALETTE["muted"],
                font=ctk.CTkFont(family="Bahnschrift", size=12),
            ).pack(anchor="w")

            market_frame = ctk.CTkFrame(self, fg_color="transparent")
            market_frame.grid(row=grid_row, column=1, sticky="ew", padx=10, pady=8)
            ctk.CTkLabel(
                market_frame,
                text=fmt_money(price, ccy),
                text_color=PALETTE["text"],
                font=ctk.CTkFont(family="Bahnschrift", size=13, weight="bold"),
            ).pack(anchor="w")
            ctk.CTkLabel(
                market_frame,
                text=f"Mkt Cap: {fmt_market_cap(market_cap)}",
                text_color=PALETTE["muted"],
                font=ctk.CTkFont(family="Bahnschrift", size=11),
            ).pack(anchor="w")

            pos_frame = ctk.CTkFrame(self, fg_color="transparent")
            pos_frame.grid(row=grid_row, column=2, sticky="ew", padx=10, pady=8)
            ctk.CTkLabel(
                pos_frame,
                text=f"{shares:,.4g} shares",
                text_color=PALETTE["text"],
                font=ctk.CTkFont(family="Bahnschrift", size=12, weight="bold"),
            ).pack(anchor="w")
            ctk.CTkLabel(
                pos_frame,
                text=f"Avg: {fmt_money(avg_price, ccy)}",
                text_color=PALETTE["muted"],
                font=ctk.CTkFont(family="Bahnschrift", size=11),
            ).pack(anchor="w")
            ctk.CTkLabel(
                pos_frame,
                text=f"Equity: {fmt_money(equity, out_ccy)}",
                text_color=PALETTE["muted"],
                font=ctk.CTkFont(family="Bahnschrift", size=11),
            ).pack(anchor="w", padx=(2, 0))

            perf_frame = ctk.CTkFrame(self, fg_color="transparent")
            perf_frame.grid(row=grid_row, column=3, sticky="ew", padx=10, pady=8)
            color = PALETTE["gain"] if np.isfinite(primary_value) and primary_value >= 0 else PALETTE["loss"]
            ctk.CTkLabel(
                perf_frame,
                text=f"{primary_label}: {fmt_money(primary_value, out_ccy)}",
                text_color=color,
                font=ctk.CTkFont(family="Bahnschrift", size=12, weight="bold"),
            ).pack(anchor="w")

            if perf_choice == "unrealized":
                details = [
                    (f"Change: {fmt_pct(unrealized_change_pct)}", PALETTE["muted"]),
                    (f"XIRR: {fmt_pct(xirr * 100 if xirr is not None else np.nan)}", PALETTE["muted"]),
                ]
            elif perf_choice == "total":
                details = [
                    (f"Unrealized: {fmt_money(unrealized_pnl, out_ccy)}", PALETTE["muted"]),
                    (f"Total Change: {fmt_pct(total_change_pct)}", PALETTE["muted"]),
                ]
                if is_closed:
                    details.insert(1, (f"Realized: {fmt_money(realized_pnl, out_ccy)}", PALETTE["muted"]))

            for label, label_color in details:
                ctk.CTkLabel(
                    perf_frame,
                    text=label,
                    text_color=label_color,
                    font=ctk.CTkFont(family="Bahnschrift", size=11),
                ).pack(anchor="w")

            alloc_frame = ctk.CTkFrame(self, fg_color="transparent")
            alloc_frame.grid(row=grid_row, column=4, sticky="ew", padx=10, pady=8)
            alloc_value = float(alloc) if np.isfinite(alloc) else 0.0
            ctk.CTkLabel(
                alloc_frame,
                text=fmt_pct(alloc_value),
                text_color=PALETTE["text"],
                font=ctk.CTkFont(family="Bahnschrift", size=12, weight="bold"),
            ).pack(anchor="w")
            bar = ctk.CTkProgressBar(alloc_frame, width=140, progress_color=PALETTE["accent"])
            bar.pack(anchor="w", pady=(4, 0))
            bar.set(max(min(alloc_value / 100.0, 1.0), 0.0))

            ctk.CTkLabel(
                self,
                text=out_ccy,
                text_color=PALETTE["muted"],
                font=ctk.CTkFont(family="Bahnschrift", size=12),
            ).grid(row=grid_row, column=5, sticky="w", padx=10, pady=8)


class StackWealthApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("StackWealth - Multi-Currency Portfolio")
        self.geometry("1440x860")
        self.minsize(1100, 700)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.configure(fg_color=PALETTE["bg"])
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.view_mode = ctk.StringVar(value="native")
        self.sort_mode = ctk.StringVar(value="Position: Equity (Desc)")
        self.perf_mode = ctk.StringVar(value="unrealized")
        self.show_closed_positions = ctk.BooleanVar(value=False)
        self.debug_logging = ctk.BooleanVar(value=is_debug_enabled())

        self.db = SQLiteStore(Path("data/alphavault.db"))
        self.db.seed_from_json(Path("data/portfolio.json"))

        cache = CacheStore(Path("cache/market_cache.json"))
        self.market_service = MarketDataService(cache=cache)
        self.sync_service = RobinhoodSyncService(self.db, self.market_service)
        self.positions, self.transactions = self.db.load_portfolio_state()
        self.db.bootstrap_sync_profile_from_portfolio_json(Path("data/portfolio.json"))

        self.snapshot: Snapshot | None = None
        self.metrics = None
        self.portfolio_change_pct: float | None = None

        self.result_queue: queue.Queue[tuple[Snapshot, Any, float | None]] = queue.Queue()
        self.sync_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.refresh_in_progress = False
        self.sync_in_progress = False
        self.manual_in_progress = False
        self.robinhood_email: str | None = None
        self.robinhood_account_number: str | None = None

        self._build_ui()
        self.start_refresh_thread()
        self.after(500, self._poll_queue)
        self.after(YAHOO_REFRESH_MS, self._schedule_yahoo_refresh)
        LOGGER.info("App initialized. Yahoo refresh interval set to %d minutes.", YAHOO_REFRESH_MS // 60000)

    def _transaction_signature(self) -> str:
        rows: list[str] = []
        for tx in sorted(
            self.transactions,
            key=lambda t: (
                t.ticker,
                t.tx_date,
                t.created_at or "",
                t.execution_id or "",
                float(t.amount),
            ),
        ):
            rows.append(
                "|".join(
                    [
                        tx.ticker,
                        tx.tx_date.isoformat(),
                        f"{float(tx.amount):.10f}",
                        tx.side or "",
                        f"{float(tx.shares or 0.0):.10f}",
                        f"{float(tx.price or 0.0):.10f}",
                        tx.currency or "",
                        tx.execution_id or "",
                        tx.created_at or "",
                    ]
                )
            )
        payload = "\n".join(rows).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color=PALETTE["panel"], corner_radius=18)
        header.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        header.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.kpi_total_value = self._make_kpi_card(header, 0, "Total Value", "Loading...")
        self.kpi_total_pnl = self._make_kpi_card(header, 1, "All-Time P&L", "Loading...")
        self.kpi_change = self._make_kpi_card(header, 2, "Total Return %", "Loading...")

        control_panel = ctk.CTkFrame(header, fg_color="transparent")
        control_panel.grid(row=0, column=3, sticky="nsew", padx=10, pady=10)
        control_panel.grid_columnconfigure(0, weight=1)

        toolbar_row = ctk.CTkFrame(control_panel, fg_color="transparent")
        toolbar_row.pack(fill="x", anchor="e")

        refresh_btn = ctk.CTkButton(
            toolbar_row,
            text="Refresh Now",
            width=110,
            height=34,
            command=self.start_refresh_thread,
            fg_color=PALETTE["accent"],
            hover_color="#2f7fd4",
            corner_radius=10,
        )
        refresh_btn.pack(side="right", padx=(6, 0))

        self.status_label = ctk.CTkLabel(
            control_panel,
            text="● Syncing...",
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(family="Bahnschrift", size=12, weight="bold"),
        )
        self.status_label.pack(anchor="e", pady=(2, 8))

        debug_toggle = ctk.CTkSwitch(
            control_panel,
            text="Debug Logs",
            variable=self.debug_logging,
            command=self._toggle_debug_logging,
            text_color=PALETTE["muted"],
            progress_color=PALETTE["accent"],
            button_color="#d5e7ff",
            button_hover_color="#f4f9ff",
        )
        debug_toggle.pack(anchor="e", pady=(0, 8))

        self.sync_button = ctk.CTkButton(
            control_panel,
            text="Sync Robinhood Transactions",
            command=self.start_sync_thread,
            fg_color="#2d6cdf",
            hover_color="#1f56b6",
            corner_radius=10,
        )
        self.sync_button.pack(anchor="e", pady=(0, 8))

        self.manual_tx_button = ctk.CTkButton(
            control_panel,
            text="Add Manual Transaction",
            command=self.start_manual_transaction_flow,
            fg_color="#16a085",
            hover_color="#117a65",
            corner_radius=10,
        )
        self.manual_tx_button.pack(anchor="e", pady=(0, 8))

        self.manage_position_button = ctk.CTkButton(
            control_panel,
            text="Manage Position",
            command=self.start_position_manager_flow,
            fg_color="#7b5cff",
            hover_color="#6547da",
            corner_radius=10,
        )
        self.manage_position_button.pack(anchor="e", pady=(0, 8))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        body.grid_columnconfigure(0, weight=4)
        body.grid_columnconfigure(1, weight=0, minsize=300)
        body.grid_rowconfigure(0, weight=1)

        self.table = HoldingsTable(body, self.sort_mode, self.perf_mode, self.refresh_ui, self.refresh_ui)
        self.table.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        side_panel = ctk.CTkFrame(body, fg_color=PALETTE["panel"], corner_radius=16)
        side_panel.grid(row=0, column=1, sticky="nsew")
        side_panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            side_panel,
            text="Alpha Insights",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(family="Bahnschrift", size=20, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 6))

        self.side_summary = ctk.CTkTextbox(
            side_panel,
            fg_color=PALETTE["bg"],
            text_color=PALETTE["text"],
            corner_radius=10,
            border_width=0,
            wrap="word",
            font=ctk.CTkFont(family="Bahnschrift", size=12),
            activate_scrollbars=True,
        )
        self.side_summary.grid(row=1, column=0, sticky="nsew", padx=14, pady=(4, 14))
        side_panel.grid_rowconfigure(1, weight=1)

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))
        footer.grid_columnconfigure(0, weight=1)
        footer.grid_columnconfigure(1, weight=1)

        view_mode_panel = ctk.CTkFrame(footer, fg_color="transparent")
        view_mode_panel.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            view_mode_panel,
            text="View Mode",
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(family="Bahnschrift", size=11, weight="bold"),
        ).pack(anchor="w")

        switch = ctk.CTkSegmentedButton(
            view_mode_panel,
            values=["native", "usd"],
            variable=self.view_mode,
            command=lambda _value: self.refresh_ui(),
        )
        switch.pack(anchor="w", pady=(4, 0))

        closed_panel = ctk.CTkFrame(footer, fg_color="transparent")
        closed_panel.grid(row=0, column=1, sticky="e")

        closed_toggle = ctk.CTkCheckBox(
            closed_panel,
            text="Show Closed Positions",
            variable=self.show_closed_positions,
            command=self.refresh_ui,
            text_color=PALETTE["muted"],
            checkbox_width=18,
            checkbox_height=18,
            font=ctk.CTkFont(family="Bahnschrift", size=11, weight="bold"),
        )
        closed_toggle.pack(anchor="e")

    def _make_kpi_card(self, parent: Any, column: int, title: str, value: str) -> ctk.CTkLabel:
        card = ctk.CTkFrame(parent, fg_color=PALETTE["bg"], corner_radius=12)
        card.grid(row=0, column=column, sticky="nsew", padx=8, pady=10)

        title_label = ctk.CTkLabel(
            card,
            text=title,
            text_color=PALETTE["muted"],
            font=ctk.CTkFont(family="Bahnschrift", size=11, weight="bold"),
        )
        title_label.pack(anchor="w", padx=12, pady=(10, 4))

        value_label = ctk.CTkLabel(
            card,
            text=value,
            text_color=PALETTE["text"],
            font=ctk.CTkFont(family="Bahnschrift", size=18, weight="bold"),
        )
        value_label.pack(anchor="w", padx=12, pady=(0, 12))
        value_label._title_label = title_label
        return value_label

    def _schedule_yahoo_refresh(self) -> None:
        LOGGER.debug("Running scheduled Yahoo refresh.")
        self.start_refresh_thread()
        self.after(YAHOO_REFRESH_MS, self._schedule_yahoo_refresh)

    def _toggle_debug_logging(self) -> None:
        enabled = bool(self.debug_logging.get())
        set_debug_logging(enabled)
        LOGGER.info("Debug logging %s.", "enabled" if enabled else "disabled")

    def _emit_sync_progress(self, status: str) -> None:
        self.sync_queue.put(("progress", status))

    def _reload_portfolio_state(self) -> None:
        self.positions, self.transactions = self.db.load_portfolio_state()
        self.start_refresh_thread()

    def _prompt_position_manager(self) -> None:
        modal = ctk.CTkToplevel(self)
        modal.title("Manage Position")
        modal.geometry("560x470")
        modal.resizable(False, False)
        modal.configure(fg_color=PALETTE["panel"])
        modal.grab_set()

        tickers = sorted(self.db.list_cache_tickers())
        result = {"ticker": ""}

        ctk.CTkLabel(
            modal,
            text="Search, override, or delete a position",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(family="Bahnschrift", size=16, weight="bold"),
        ).pack(padx=16, pady=(16, 8), anchor="w")

        form = ctk.CTkFrame(modal, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        ctk.CTkLabel(form, text="Ticker", text_color=PALETTE["muted"]).pack(anchor="w")
        ticker_var = tk.StringVar(value=tickers[0] if tickers else "")
        ticker_combo = ttk.Combobox(form, textvariable=ticker_var, values=tickers, state="normal")
        ticker_combo.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Company Name", text_color=PALETTE["muted"]).pack(anchor="w")
        company_entry = ctk.CTkEntry(form)
        company_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Shares", text_color=PALETTE["muted"]).pack(anchor="w")
        shares_entry = ctk.CTkEntry(form)
        shares_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Avg Price", text_color=PALETTE["muted"]).pack(anchor="w")
        avg_price_entry = ctk.CTkEntry(form)
        avg_price_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Currency", text_color=PALETTE["muted"]).pack(anchor="w")
        currency_var = tk.StringVar(value="USD")
        currency_combo = ttk.Combobox(form, textvariable=currency_var, values=["USD", "CAD", "SEK", "AUD"], state="normal")
        currency_combo.pack(fill="x", pady=(0, 10))

        button_row = ctk.CTkFrame(modal, fg_color="transparent")
        button_row.pack(fill="x", padx=16, pady=(0, 16))

        def _load_position() -> None:
            ticker = ticker_combo.get().strip().upper()
            if not ticker:
                messagebox.showwarning("Missing Ticker", "Pick a ticker to load.")
                return

            pos = self.db.get_portfolio_position(ticker)
            if pos is None:
                messagebox.showwarning("Not Found", f"{ticker} is not in the local portfolio cache.")
                return

            result["ticker"] = pos.ticker
            ticker_var.set(pos.ticker)
            company_entry.delete(0, "end")
            company_entry.insert(0, pos.company_name)
            shares_entry.delete(0, "end")
            shares_entry.insert(0, str(pos.shares))
            avg_price_entry.delete(0, "end")
            avg_price_entry.insert(0, str(pos.avg_price))
            currency_var.set(pos.currency)

        def _save_override() -> None:
            ticker = ticker_combo.get().strip().upper()
            company_name = company_entry.get().strip()
            currency = currency_combo.get().strip().upper()

            try:
                shares = float(shares_entry.get().strip())
                avg_price = float(avg_price_entry.get().strip())
            except ValueError:
                messagebox.showwarning("Invalid Input", "Check shares and average price values.")
                return

            if not ticker or not company_name or shares < 0 or avg_price < 0 or currency not in {"USD", "CAD", "SEK", "AUD"}:
                messagebox.showwarning("Invalid Input", "Ticker, company name, shares, avg price, and currency are required.")
                return

            self.db.override_portfolio_position(
                ticker=ticker,
                company_name=company_name,
                shares=shares,
                avg_price=avg_price,
                currency=currency,
            )
            self._reload_portfolio_state()
            modal.destroy()
            messagebox.showinfo("Position Updated", f"{ticker} was overridden locally.")

        def _delete_position() -> None:
            ticker = ticker_combo.get().strip().upper()
            if not ticker:
                messagebox.showwarning("Missing Ticker", "Pick a ticker to delete.")
                return

            confirmed = messagebox.askyesno(
                "Delete Position",
                f"Delete {ticker} permanently from the local portfolio and its transaction history?",
            )
            if not confirmed:
                return

            self.db.delete_portfolio_position(ticker, delete_transactions=True)
            self._reload_portfolio_state()
            modal.destroy()
            messagebox.showinfo("Position Deleted", f"{ticker} was deleted permanently.")

        def _close() -> None:
            modal.destroy()

        ctk.CTkButton(button_row, text="Load", fg_color="#6b7280", command=_load_position).pack(side="left", padx=6)
        ctk.CTkButton(button_row, text="Save Override", fg_color=PALETTE["accent"], command=_save_override).pack(side="left", padx=6)
        ctk.CTkButton(button_row, text="Delete Permanently", fg_color=PALETTE["loss"], command=_delete_position).pack(side="left", padx=6)
        ctk.CTkButton(button_row, text="Close", fg_color="#6b7280", command=_close).pack(side="right")

        modal.protocol("WM_DELETE_WINDOW", _close)
        if tickers:
            _load_position()
        ticker_combo.focus_set()
        self.wait_window(modal)

    def start_position_manager_flow(self) -> None:
        if self.manual_in_progress or self.sync_in_progress or self.refresh_in_progress:
            return

        self._prompt_position_manager()

    def _robinhood_accounts_path(self) -> Path:
        return Path("cache/robinhood_accounts.json")

    def _load_saved_robinhood_accounts(self) -> list[str]:
        path = self._robinhood_accounts_path()
        if not path.exists():
            return []

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        accounts = payload.get("accounts", []) if isinstance(payload, dict) else []
        cleaned: list[str] = []
        for value in accounts:
            account_number = str(value).strip()
            if account_number and account_number not in cleaned:
                cleaned.append(account_number)
        return cleaned

    def _save_robinhood_account(self, account_number: str) -> None:
        account_number = account_number.strip()
        if not account_number:
            return

        accounts = self._load_saved_robinhood_accounts()
        if account_number not in accounts:
            accounts.insert(0, account_number)

        path = self._robinhood_accounts_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"accounts": accounts}, indent=2), encoding="utf-8")


    def _prompt_robinhood_credentials(self) -> tuple[str, str, str | None] | None:
        modal = ctk.CTkToplevel(self)
        modal.title("Robinhood Login")
        modal.geometry("460x340")
        modal.resizable(False, False)
        modal.configure(fg_color=PALETTE["panel"])
        modal.grab_set()

        result = {"email": "", "password": "", "account_number": ""}
        saved_accounts = self._load_saved_robinhood_accounts()
        account_choices = ["Default account"] + saved_accounts

        ctk.CTkLabel(
            modal,
            text="Enter Robinhood credentials",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(family="Bahnschrift", size=16, weight="bold"),
        ).pack(padx=16, pady=(16, 8), anchor="w")

        ctk.CTkLabel(modal, text="Email", text_color=PALETTE["muted"]).pack(padx=16, pady=(0, 4), anchor="w")
        email_entry = ctk.CTkEntry(modal, width=420)
        email_entry.pack(padx=16, pady=(0, 10), fill="x")
        if self.robinhood_email:
            email_entry.insert(0, self.robinhood_email)

        ctk.CTkLabel(modal, text="Password", text_color=PALETTE["muted"]).pack(padx=16, pady=(0, 4), anchor="w")
        password_entry = ctk.CTkEntry(modal, width=420, show="*")
        password_entry.pack(padx=16, pady=(0, 14), fill="x")

        ctk.CTkLabel(
            modal,
            text="Account Number",
            text_color=PALETTE["muted"],
        ).pack(padx=16, pady=(0, 4), anchor="w")
        account_var = tk.StringVar(value=account_choices[0])
        account_combo = ttk.Combobox(modal, textvariable=account_var, values=account_choices, state="normal")
        account_combo.pack(padx=16, pady=(0, 14), fill="x")

        button_row = ctk.CTkFrame(modal, fg_color="transparent")
        button_row.pack(padx=16, pady=(0, 16), anchor="e")

        def _cancel() -> None:
            modal.destroy()

        def _submit() -> None:
            email = email_entry.get().strip()
            password = password_entry.get().strip()
            account_number = account_combo.get().strip()
            if not email or not password:
                messagebox.showwarning("Missing Credentials", "Please enter both email and password.")
                return
            result["email"] = email
            result["password"] = password
            result["account_number"] = "" if account_number == "Default account" else account_number
            modal.destroy()

        ctk.CTkButton(button_row, text="Cancel", fg_color="#6b7280", command=_cancel).pack(side="left", padx=6)
        ctk.CTkButton(button_row, text="Continue", fg_color=PALETTE["accent"], command=_submit).pack(side="left")

        modal.protocol("WM_DELETE_WINDOW", _cancel)
        email_entry.focus_set()
        self.wait_window(modal)

        if result["email"] and result["password"]:
            return result["email"], result["password"], result["account_number"] or None
        return None

    def _request_mfa_code(self) -> str:
        result = {"code": ""}
        done = threading.Event()

        def _show_modal() -> None:
            modal = ctk.CTkToplevel(self)
            modal.title("Robinhood 2FA Verification")
            modal.geometry("420x180")
            modal.resizable(False, False)
            modal.configure(fg_color=PALETTE["panel"])
            modal.grab_set()

            ctk.CTkLabel(
                modal,
                text="Enter your Robinhood 2FA code",
                text_color=PALETTE["text"],
                font=ctk.CTkFont(family="Bahnschrift", size=16, weight="bold"),
            ).pack(padx=16, pady=(16, 8), anchor="w")

            entry = ctk.CTkEntry(modal, width=360)
            entry.pack(padx=16, pady=(0, 12), fill="x")
            entry.focus_set()

            button_row = ctk.CTkFrame(modal, fg_color="transparent")
            button_row.pack(padx=16, pady=(0, 16), anchor="e")

            def _submit() -> None:
                result["code"] = entry.get().strip()
                modal.destroy()
                done.set()

            def _cancel() -> None:
                result["code"] = ""
                modal.destroy()
                done.set()

            ctk.CTkButton(button_row, text="Cancel", fg_color="#6b7280", command=_cancel).pack(side="left", padx=6)
            ctk.CTkButton(button_row, text="Submit", fg_color=PALETTE["accent"], command=_submit).pack(side="left")

            modal.protocol("WM_DELETE_WINDOW", _cancel)

        self.after(0, _show_modal)
        done.wait()
        return result["code"]

    def _prompt_manual_transaction(self) -> dict[str, Any] | None:
        modal = ctk.CTkToplevel(self)
        modal.title("Add Manual Transaction")
        modal.geometry("520x560")
        modal.resizable(False, False)
        modal.configure(fg_color=PALETTE["panel"])
        modal.grab_set()

        result: dict[str, Any] = {}
        side_var = ctk.StringVar(value="buy")
        currency_var = ctk.StringVar(value="USD")

        ctk.CTkLabel(
            modal,
            text="Manual Transaction (Non-Robinhood)",
            text_color=PALETTE["text"],
            font=ctk.CTkFont(family="Bahnschrift", size=16, weight="bold"),
        ).pack(padx=16, pady=(16, 8), anchor="w")

        body = ctk.CTkFrame(modal, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        form = ctk.CTkScrollableFrame(body, fg_color="transparent", corner_radius=0)
        form.pack(fill="both", expand=True)

        ctk.CTkLabel(form, text="Ticker", text_color=PALETTE["muted"]).pack(anchor="w")
        ticker_entry = ctk.CTkEntry(form)
        ticker_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Company Name (optional)", text_color=PALETTE["muted"]).pack(anchor="w")
        company_entry = ctk.CTkEntry(form)
        company_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Date (YYYY-MM-DD)", text_color=PALETTE["muted"]).pack(anchor="w")
        date_entry = ctk.CTkEntry(form)
        date_entry.insert(0, date.today().isoformat())
        date_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Side", text_color=PALETTE["muted"]).pack(anchor="w")
        ctk.CTkSegmentedButton(form, values=["buy", "sell"], variable=side_var).pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Shares", text_color=PALETTE["muted"]).pack(anchor="w")
        shares_entry = ctk.CTkEntry(form)
        shares_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Price", text_color=PALETTE["muted"]).pack(anchor="w")
        price_entry = ctk.CTkEntry(form)
        price_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(form, text="Currency", text_color=PALETTE["muted"]).pack(anchor="w")
        ctk.CTkSegmentedButton(
            form,
            values=["USD", "CAD", "SEK", "AUD"],
            variable=currency_var,
        ).pack(fill="x", pady=(0, 10))

        footer = ctk.CTkFrame(modal, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=(0, 16))

        button_row = ctk.CTkFrame(footer, fg_color="transparent")
        button_row.pack(anchor="e")

        def _cancel() -> None:
            modal.destroy()

        def _submit() -> None:
            ticker = ticker_entry.get().strip().upper()
            company_name = company_entry.get().strip()
            tx_date = date_entry.get().strip()
            side = side_var.get().strip().lower()
            currency = currency_var.get().strip().upper()

            try:
                shares = float(shares_entry.get().strip())
                price = float(price_entry.get().strip())
                date.fromisoformat(tx_date)
            except ValueError:
                messagebox.showwarning("Invalid Input", "Check date, shares, and price values.")
                return

            if not ticker or shares <= 0 or price <= 0 or side not in {"buy", "sell"}:
                messagebox.showwarning("Invalid Input", "Ticker, side, shares, and price are required.")
                return

            result.update(
                {
                    "ticker": ticker,
                    "company_name": company_name,
                    "date": tx_date,
                    "side": side,
                    "shares": shares,
                    "price": price,
                    "currency": currency,
                }
            )
            modal.destroy()

        ctk.CTkButton(button_row, text="Cancel", fg_color="#6b7280", command=_cancel).pack(side="left", padx=6)
        ctk.CTkButton(button_row, text="Save Transaction", fg_color=PALETTE["accent"], command=_submit).pack(side="left")

        modal.protocol("WM_DELETE_WINDOW", _cancel)
        ticker_entry.focus_set()
        self.wait_window(modal)

        return result or None

    def start_manual_transaction_flow(self) -> None:
        if self.manual_in_progress:
            return

        payload = self._prompt_manual_transaction()
        if not payload:
            return

        self.manual_in_progress = True
        self.manual_tx_button.configure(state="disabled", text="Adding...")
        threading.Thread(target=self._manual_transaction_worker, args=(payload,), daemon=True).start()

    def _manual_transaction_worker(self, payload: dict[str, Any]) -> None:
        try:
            ticker = str(payload["ticker"])
            tx_date = str(payload["date"])
            side = str(payload["side"])
            shares = float(payload["shares"])
            price = float(payload["price"])
            currency = str(payload["currency"])
            company_name = str(payload.get("company_name") or "").strip()

            amount = shares * price
            if side == "buy":
                amount = -amount

            execution_id = f"manual-{uuid4()}"
            inserted = self.db.insert_transaction_if_new(
                execution_id=execution_id,
                order_id="manual",
                ticker=ticker,
                tx_date=tx_date,
                side=side,
                shares=shares,
                price=price,
                amount=amount,
                currency=currency,
            )

            if not inserted:
                self.sync_queue.put(("manual_error", "Manual transaction already exists."))
                return

            existing_tickers = self.db.list_cache_tickers()
            if ticker in existing_tickers:
                self.db.refresh_existing_position_core(ticker)
            else:
                derived_shares, derived_avg, derived_ccy = self.db.derive_position_from_transactions(ticker)
                try:
                    profile = self.market_service.fetch_asset_profile(ticker)
                except Exception:
                    profile = {
                        "price": None,
                        "market_cap": None,
                        "company_name": None,
                        "currency": None,
                    }

                resolved_currency = str(profile.get("currency") or derived_ccy or currency or "USD").upper()
                resolved_name = company_name or str(profile.get("company_name") or ticker)
                self.db.upsert_portfolio_cache(
                    ticker=ticker,
                    company_name=resolved_name,
                    shares=derived_shares,
                    avg_price=derived_avg,
                    currency=resolved_currency,
                    last_price=profile.get("price"),
                    market_cap=profile.get("market_cap"),
                )

            self.sync_queue.put(("manual_success", f"Added manual transaction for {ticker}."))
        except Exception as exc:
            self.sync_queue.put(("manual_error", f"Manual transaction failed: {exc}"))
        finally:
            self.sync_queue.put(("manual_done", None))

    def start_sync_thread(self) -> None:
        if self.sync_in_progress:
            return

        creds = self._prompt_robinhood_credentials()
        if not creds:
            return

        email, password, account_number = creds
        LOGGER.info(
            "Manual Robinhood sync requested for user=%s account=%s.",
            mask_email(email),
            mask_account(account_number),
        )

        self.robinhood_email = email
        self.robinhood_account_number = account_number
        self.sync_in_progress = True
        self.sync_button.configure(state="disabled", text="Syncing Data...")
        threading.Thread(target=self._sync_worker, args=(email, password, account_number, False), daemon=True).start()

    def _sync_worker(self, email: str, password: str | None, account_number: str | None, is_auto: bool) -> None:
        try:
            try:
                result = self.sync_service.sync_transactions(
                    email=email,
                    password=password,
                    account_number=account_number,
                    mfa_callback=self._request_mfa_code,
                    status_callback=self._emit_sync_progress,
                )
            finally:
                # Best-effort secret lifetime reduction.
                if password is not None:
                    password = ""
            self.sync_queue.put(("success", {"result": result, "is_auto": is_auto, "email": email, "account_number": account_number}))
        except Exception as exc:
            self.sync_queue.put(("error", {"message": str(exc), "is_auto": is_auto}))
        finally:
            self.sync_queue.put(("done", None))

    def start_refresh_thread(self) -> None:
        if self.refresh_in_progress:
            return
        self.refresh_in_progress = True
        LOGGER.debug("Starting market refresh worker thread.")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        LOGGER.debug("Refreshing portfolio snapshot for %d positions.", len(self.positions))
        tickers = [p.ticker for p in self.positions]
        currencies = [p.currency for p in self.positions]
        profile = self.db.get_sync_profile()
        if profile is None:
            self.db.bootstrap_sync_profile_from_portfolio_json(Path("data/portfolio.json"))
            profile = self.db.get_sync_profile()

        baseline_date = date.fromisoformat(str(profile.get("baseline_date"))) if profile else date.today()
        tracked_tickers = set(profile.get("tracked_tickers", [])) if profile else set()
        tx_for_metrics = [
            tx
            for tx in self.transactions
            if tx.tx_date >= baseline_date and (not tracked_tickers or tx.ticker in tracked_tickers)
        ]

        snapshot = self.market_service.refresh_snapshot(tickers=tickers, currencies=currencies)
        metrics = build_metrics_table(
            positions=self.positions,
            transactions=tx_for_metrics,
            quotes=snapshot.quotes,
            fx_to_usd=snapshot.fx_to_usd,
            stale_tickers=snapshot.stale_tickers,
        )
        self.db.update_market_snapshot(metrics.to_dict(orient="records"))
        baseline_value_raw = profile.get("baseline_value_usd") if profile else None
        baseline_value = float(baseline_value_raw) if baseline_value_raw is not None else None
        current_equity_usd = float(metrics["equity_usd"].sum(skipna=True)) if "equity_usd" in metrics else 0.0
        if baseline_value is None or baseline_value <= 0:
            if current_equity_usd > 0:
                baseline_value = current_equity_usd
                self.db.set_baseline_value_usd(baseline_value)
            elif baseline_value is None:
                baseline_value = 0.0

        since_start = compute_portfolio_since_start_metrics(
            tx_for_metrics,
            self.positions,
            snapshot.quotes,
            snapshot.fx_to_usd,
            baseline_date=baseline_date,
            baseline_value_usd=float(baseline_value),
            tracked_tickers=tracked_tickers,
        )
        LOGGER.info(
            "Market refresh complete. online=%s tracked_tickers=%d tx_used=%d",
            snapshot.online,
            len(tracked_tickers),
            len(tx_for_metrics),
        )
        self.result_queue.put((snapshot, metrics, since_start.get("change_pct")))

    def _poll_queue(self) -> None:
        try:
            try:
                snapshot, metrics, portfolio_change_pct = self.result_queue.get_nowait()
                self.snapshot = snapshot
                self.metrics = metrics
                self.portfolio_change_pct = portfolio_change_pct
                self.refresh_in_progress = False
                self.refresh_ui()
            except queue.Empty:
                pass

            while True:
                try:
                    event, payload = self.sync_queue.get_nowait()
                except queue.Empty:
                    break

                if event == "progress":
                    self.sync_button.configure(text=str(payload))
                elif event == "success":
                    sync_payload = payload if isinstance(payload, dict) else {"result": payload, "is_auto": False}
                    sync_result = sync_payload.get("result")
                    is_auto = bool(sync_payload.get("is_auto", False))
                    synced_account = str(sync_payload.get("account_number") or "").strip() or None
                    if isinstance(sync_result, SyncResult):
                        LOGGER.info(
                            "Robinhood sync completed. imported=%d new_tickers=%d mode=%s account=%s",
                            sync_result.imported_count,
                            len(sync_result.new_tickers),
                            "auto" if is_auto else "manual",
                            mask_account(synced_account),
                        )
                        if synced_account:
                            self._save_robinhood_account(synced_account)
                        self.positions, self.transactions = self.db.load_portfolio_state()
                        self.start_refresh_thread()
                        if not is_auto:
                            details = f"Imported {sync_result.imported_count} new transactions."
                            if sync_result.new_tickers:
                                details += "\nNew assets added: " + ", ".join(sync_result.new_tickers)
                            else:
                                details += "\nNo new tickers were introduced."
                            messagebox.showinfo("Robinhood Sync Complete", details)
                elif event == "error":
                    err_payload = payload if isinstance(payload, dict) else {"message": str(payload), "is_auto": False}
                    err_text = str(err_payload.get("message") or "Unknown error")
                    LOGGER.warning("Robinhood sync failed: %s", err_text)
                    if bool(err_payload.get("is_auto", False)):
                        self.status_label.configure(text=f"● Auto sync failed ({err_text})", text_color=PALETTE["loss"])
                    else:
                        messagebox.showerror("Robinhood Sync Failed", err_text)
                elif event == "done":
                    self.sync_in_progress = False
                    self.sync_button.configure(state="normal", text="Sync Robinhood Transactions")
                elif event == "manual_success":
                    self.positions, self.transactions = self.db.load_portfolio_state()
                    self.start_refresh_thread()
                    messagebox.showinfo("Manual Transaction", str(payload))
                elif event == "manual_error":
                    messagebox.showerror("Manual Transaction", str(payload))
                elif event == "manual_done":
                    self.manual_in_progress = False
                    self.manual_tx_button.configure(state="normal", text="Add Manual Transaction")
        finally:
            self.after(350, self._poll_queue)

    def _apply_selected_sort(self, metrics) -> Any:
        sort_label = self.sort_mode.get()
        if metrics is None or metrics.empty:
            return metrics

        if sort_label == "Position: Equity (Asc)":
            return metrics.sort_values(by="equity_usd", ascending=True, na_position="last")
        if sort_label == "Position: Equity (Desc)":
            return metrics.sort_values(by="equity_usd", ascending=False, na_position="last")
        if sort_label == "Performance: Change (Asc)":
            return metrics.sort_values(by="change_pct", ascending=True, na_position="last")
        if sort_label == "Performance: Change (Desc)":
            return metrics.sort_values(by="change_pct", ascending=False, na_position="last")
        if sort_label == "Performance: XIRR (Asc)":
            return metrics.sort_values(by="xirr", ascending=True, na_position="last")
        if sort_label == "Performance: XIRR (Desc)":
            return metrics.sort_values(by="xirr", ascending=False, na_position="last")

        # Default sort: total equity in base currency descending.
        return metrics.sort_values(by="equity_usd", ascending=False, na_position="last")

    def _on_perf_mode_change(self) -> None:
        self.refresh_ui()

    def refresh_ui(self) -> None:
        if self.metrics is None or self.snapshot is None:
            return

        metrics = self._apply_selected_sort(self.metrics)
        if "is_closed" not in metrics.columns:
            metrics = metrics.copy()
            metrics["is_closed"] = False

        visible_metrics = metrics if self.show_closed_positions.get() else metrics[~metrics["is_closed"]]
        mode = self.view_mode.get()

        total_value = float(metrics["equity_usd"].sum(skipna=True)) if "equity_usd" in metrics else 0.0
        total_pnl = float(metrics["pnl_usd"].sum(skipna=True)) if "pnl_usd" in metrics else 0.0

        self.kpi_total_value.configure(text=fmt_money(total_value, "USD"))
        self.kpi_total_pnl.configure(
            text=fmt_money(total_pnl, "USD"),
            text_color=PALETTE["gain"] if total_pnl >= 0 else PALETTE["loss"],
        )
        change_value = self.portfolio_change_pct
        self.kpi_change._title_label.configure(text="Total Return % (Since Start)")
        self.kpi_change.configure(text=fmt_pct(change_value))

        status_text = "● Online" if self.snapshot.online else "● Offline (cache)"
        status_color = PALETTE["gain"] if self.snapshot.online else PALETTE["loss"]
        self.status_label.configure(text=status_text, text_color=status_color)

        self.table.render(visible_metrics, mode)
        self._render_side_summary(metrics)

    def _render_side_summary(self, metrics) -> None:
        if metrics.empty:
            return

        if "realized_pnl_usd" not in metrics.columns:
            return

        rows = []
        sorted_alloc = metrics.sort_values(by="allocation_pct", ascending=False)
        rows.append("Top Allocation Weights")
        for row in sorted_alloc.head(5).itertuples(index=False):
            rows.append(f"- {row.ticker}: {fmt_pct(row.allocation_pct)}")

        rows.append("\nStrongest Total Change")
        strongest = metrics.sort_values(by="change_pct", ascending=False).head(5)
        for row in strongest.itertuples(index=False):
            rows.append(f"- {row.ticker}: {fmt_pct(row.change_pct)}")

        rows.append("\nHighest XIRR")
        xirr_ranked = metrics.dropna(subset=["xirr"]).sort_values(by="xirr", ascending=False).head(5)
        if xirr_ranked.empty:
            rows.append("- Not enough mixed-sign cash flows yet")
        else:
            for row in xirr_ranked.itertuples(index=False):
                rows.append(f"- {row.ticker}: {fmt_pct(row.xirr * 100)}")

        realized = metrics["realized_pnl_usd"].fillna(0.0)

        rows.append("\nTop Realized Gains")
        gains = metrics.loc[realized > 0].sort_values(by="realized_pnl_usd", ascending=False).head(5)
        if gains.empty:
            rows.append("- No realized gains yet")
        else:
            for row in gains.itertuples(index=False):
                rows.append(f"- {row.ticker}: {fmt_money(row.realized_pnl_usd, 'USD')}")

        rows.append("\nTop Realized Losses")
        losses = metrics.loc[realized < 0].sort_values(by="realized_pnl_usd", ascending=True).head(5)
        if losses.empty:
            rows.append("- No realized losses yet")
        else:
            for row in losses.itertuples(index=False):
                rows.append(f"- {row.ticker}: {fmt_money(row.realized_pnl_usd, 'USD')}")

        self.side_summary.delete("1.0", "end")
        self.side_summary.insert("1.0", "\n".join(rows))


def main() -> None:
    app = StackWealthApp()
    app.mainloop()


if __name__ == "__main__":
    main()
