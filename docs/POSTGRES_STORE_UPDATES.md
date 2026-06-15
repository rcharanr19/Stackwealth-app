"""
Updated PostgreSQL Store Methods for Multi-User Support
These are the key methods to update in alphavault/postgres_store.py
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import pandas as pd
from sqlalchemy import text, MetaData, Table, Column, String, Numeric, DateTime, Integer, Boolean, JSON, TIMESTAMP, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID

from alphavault.user_context import inject_user_id_into_insert, log_user_data_access

LOGGER = logging.getLogger(__name__)


# ============================================================================
# UPDATED SCHEMA INITIALIZATION WITH USER_ID SUPPORT
# ============================================================================

def ensure_schema_multi_user(self):
    """
    Initialize database schema with multi-user support.
    Run this after SQL migration to ensure app-level schema consistency.
    """
    statements = [
        # Portfolio Cache: composite key (user_id, ticker)
        """
        CREATE TABLE IF NOT EXISTS public.portfolio_cache (
            user_id UUID NOT NULL DEFAULT auth.uid(),
            ticker VARCHAR(32) NOT NULL,
            company_name VARCHAR(255) NOT NULL,
            shares NUMERIC(12, 4) NOT NULL DEFAULT 0,
            avg_price NUMERIC(12, 4) NOT NULL DEFAULT 0,
            currency CHAR(3) NOT NULL DEFAULT 'USD',
            last_price NUMERIC(14, 4),
            market_cap NUMERIC(18, 2),
            unrealized_pnl_usd NUMERIC(14, 2),
            realized_pnl_usd NUMERIC(14, 2),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, ticker),
            FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE
        )
        """,
        # Transactions: indexed by user_id
        """
        CREATE TABLE IF NOT EXISTS public.transactions (
            user_id UUID NOT NULL DEFAULT auth.uid(),
            execution_id VARCHAR(128) PRIMARY KEY,
            order_id VARCHAR(128),
            ticker VARCHAR(32) NOT NULL,
            tx_date DATE NOT NULL,
            side VARCHAR(8) NOT NULL,
            shares NUMERIC(12, 4) NOT NULL DEFAULT 0,
            price NUMERIC(12, 4) NOT NULL DEFAULT 0,
            amount NUMERIC(14, 2) NOT NULL,
            currency CHAR(3) NOT NULL DEFAULT 'USD',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE
        )
        """,
        # Sync Profile: one per user
        """
        CREATE TABLE IF NOT EXISTS public.sync_profile (
            user_id UUID NOT NULL DEFAULT auth.uid() PRIMARY KEY,
            baseline_date DATE NOT NULL,
            baseline_value_usd NUMERIC(14, 2),
            baseline_assets TEXT NOT NULL DEFAULT '[]',
            initialized BOOLEAN NOT NULL DEFAULT FALSE,
            initialized_at TIMESTAMPTZ,
            last_sync_at TIMESTAMPTZ,
            sync_version INTEGER NOT NULL DEFAULT 1,
            initial_sync_completed BOOLEAN NOT NULL DEFAULT FALSE,
            tracked_tickers TEXT NOT NULL DEFAULT '[]',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE
        )
        """,
        # AI Analysis Reports: indexed by user_id
        """
        CREATE TABLE IF NOT EXISTS public.ai_analysis_reports (
            user_id UUID NOT NULL DEFAULT auth.uid(),
            id SERIAL PRIMARY KEY,
            ticker VARCHAR(32) NOT NULL,
            analysis_type VARCHAR(64) NOT NULL,
            model VARCHAR(128),
            prompt_summary TEXT,
            report_md TEXT,
            inputs JSONB,
            run_by VARCHAR(128),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE
        )
        """,
        # Transcripts: indexed by user_id
        """
        CREATE TABLE IF NOT EXISTS public.transcripts (
            user_id UUID NOT NULL DEFAULT auth.uid(),
            id SERIAL PRIMARY KEY,
            ticker VARCHAR(32),
            filename VARCHAR(255),
            content TEXT NOT NULL,
            source VARCHAR(64),
            uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE
        )
        """,
        "ALTER TABLE public.portfolio_cache ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.sync_profile ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.ai_analysis_reports ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.transcripts ENABLE ROW LEVEL SECURITY",
    ]

    try:
        with self.connection.session as session:
            for statement in statements:
                session.execute(text(statement))
            session.commit()
        LOGGER.info("Multi-user schema initialized successfully")
    except Exception as exc:
        LOGGER.exception("Multi-user schema initialization failed: %s", exc)
        raise RuntimeError("Unable to initialize multi-user schema") from exc


# ============================================================================
# UPDATED DATA LOADING METHODS
# ============================================================================

def load_portfolio_state(self, user_id: str | UUID) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load positions and transactions for a specific user.
    
    Args:
        user_id: User's UUID from authenticated session
    
    Returns:
        Tuple of (positions_df, transactions_df)
    """
    user_id_str = str(user_id)
    log_user_data_access(user_id, "SELECT", "portfolio_cache")
    log_user_data_access(user_id, "SELECT", "transactions")
    
    try:
        # Load positions filtered by user_id
        positions_query = f"""
            SELECT ticker, company_name, shares, avg_price, last_price, 
                   unrealized_pnl_usd, realized_pnl_usd, currency, updated_at
            FROM public.portfolio_cache
            WHERE user_id = '{user_id_str}'
            ORDER BY updated_at DESC
        """
        positions_df = self._query_df(positions_query)
        
        # Load transactions filtered by user_id
        transactions_query = f"""
            SELECT execution_id, order_id, ticker, tx_date, side, shares, price, 
                   amount, currency, created_at
            FROM public.transactions
            WHERE user_id = '{user_id_str}'
            ORDER BY tx_date DESC
        """
        transactions_df = self._query_df(transactions_query)
        
        LOGGER.info("Loaded portfolio for user_id=%s: %d positions, %d transactions",
                   user_id_str[:8], len(positions_df), len(transactions_df))
        
        return positions_df, transactions_df
        
    except Exception as exc:
        LOGGER.exception("Failed to load portfolio state for user_id=%s: %s", user_id_str, exc)
        raise


def load_sync_profile(self, user_id: str | UUID) -> dict[str, Any]:
    """
    Load sync profile for a specific user.
    
    Args:
        user_id: User's UUID
    
    Returns:
        Dictionary with sync profile data
    """
    user_id_str = str(user_id)
    log_user_data_access(user_id, "SELECT", "sync_profile")
    
    try:
        query = f"""
            SELECT baseline_date, baseline_value_usd, baseline_assets, initialized,
                   initialized_at, last_sync_at, sync_version, initial_sync_completed,
                   tracked_tickers, updated_at
            FROM public.sync_profile
            WHERE user_id = '{user_id_str}'
            LIMIT 1
        """
        result = self._query_df(query)
        
        if result.empty:
            LOGGER.info("No sync profile found for user_id=%s, creating default", user_id_str[:8])
            return self._create_default_sync_profile(user_id)
        
        profile = result.iloc[0].to_dict()
        # Parse JSON fields
        profile["baseline_assets"] = json.loads(profile.get("baseline_assets", "[]"))
        profile["tracked_tickers"] = json.loads(profile.get("tracked_tickers", "[]"))
        
        return profile
        
    except Exception as exc:
        LOGGER.exception("Failed to load sync profile for user_id=%s: %s", user_id_str, exc)
        raise


def _create_default_sync_profile(self, user_id: str | UUID) -> dict[str, Any]:
    """Create and insert default sync profile for new user."""
    user_id_str = str(user_id)
    
    default_profile = {
        "user_id": user_id_str,
        "baseline_date": pd.Timestamp.now().date(),
        "baseline_value_usd": 0.0,
        "baseline_assets": "[]",
        "initialized": False,
        "initialized_at": None,
        "last_sync_at": None,
        "sync_version": 1,
        "initial_sync_completed": False,
        "tracked_tickers": "[]",
    }
    
    try:
        insert_query = f"""
            INSERT INTO public.sync_profile 
            (user_id, baseline_date, baseline_value_usd, baseline_assets, 
             initialized, sync_version, initial_sync_completed, tracked_tickers)
            VALUES 
            ('{user_id_str}', '{default_profile["baseline_date"]}'::DATE, 0.0, '[]', 
             false, 1, false, '[]')
            ON CONFLICT (user_id) DO NOTHING
        """
        
        with self.connection.session as session:
            session.execute(text(insert_query))
            session.commit()
        
        log_user_data_access(user_id, "INSERT", "sync_profile", "default profile created")
        LOGGER.info("Created default sync profile for user_id=%s", user_id_str[:8])
        
        return default_profile
        
    except Exception as exc:
        LOGGER.exception("Failed to create sync profile for user_id=%s: %s", user_id_str, exc)
        raise


# ============================================================================
# UPDATED INSERT/UPDATE METHODS
# ============================================================================

def insert_transaction(self, ticker: str, tx_date: str, side: str, 
                       shares: float, price: float, amount: float, 
                       user_id: str | UUID) -> str:
    """
    Insert a new transaction for the authenticated user.
    
    Args:
        ticker: Stock ticker
        tx_date: Transaction date (YYYY-MM-DD)
        side: BUY or SELL
        shares: Number of shares
        price: Price per share
        amount: Total transaction amount
        user_id: User's UUID
    
    Returns:
        execution_id of inserted transaction
    """
    user_id_str = str(user_id)
    import uuid
    
    try:
        execution_id = str(uuid.uuid4())
        
        query = f"""
            INSERT INTO public.transactions 
            (execution_id, user_id, ticker, tx_date, side, shares, price, amount, currency)
            VALUES 
            ('{execution_id}', '{user_id_str}', '{ticker}', '{tx_date}'::DATE, 
             '{side}', {shares}, {price}, {amount}, 'USD')
        """
        
        with self.connection.session as session:
            session.execute(text(query))
            session.commit()
        
        log_user_data_access(user_id, "INSERT", "transactions", 
                           f"ticker={ticker}, shares={shares}, side={side}")
        LOGGER.info("Inserted transaction: user_id=%s, ticker=%s, side=%s, shares=%s",
                   user_id_str[:8], ticker, side, shares)
        
        return execution_id
        
    except Exception as exc:
        LOGGER.exception("Failed to insert transaction for user_id=%s: %s", user_id_str, exc)
        raise


def update_portfolio_cache(self, cache_data: dict[str, Any], user_id: str | UUID) -> None:
    """
    Update portfolio cache for a user.
    
    Args:
        cache_data: Dictionary with ticker and market data
        user_id: User's UUID
    """
    user_id_str = str(user_id)
    
    try:
        for ticker, data in cache_data.items():
            # Build update/insert query
            query = f"""
                INSERT INTO public.portfolio_cache 
                (user_id, ticker, company_name, shares, avg_price, last_price, market_cap, updated_at)
                VALUES 
                ('{user_id_str}', '{ticker}', '{data.get("company_name", "")}', 
                 {data.get("shares", 0)}, {data.get("avg_price", 0)}, 
                 {data.get("last_price", 0)}, {data.get("market_cap", 0)}, NOW())
                ON CONFLICT (user_id, ticker) DO UPDATE SET
                  last_price = EXCLUDED.last_price,
                  market_cap = EXCLUDED.market_cap,
                  updated_at = NOW()
            """
            
            with self.connection.session as session:
                session.execute(text(query))
            
            log_user_data_access(user_id, "UPDATE", "portfolio_cache", f"ticker={ticker}")
        
        LOGGER.info("Updated portfolio cache for user_id=%s: %d tickers", 
                   user_id_str[:8], len(cache_data))
        
    except Exception as exc:
        LOGGER.exception("Failed to update portfolio cache for user_id=%s: %s", user_id_str, exc)
        raise


def insert_ai_analysis_report(self, ticker: str, analysis_type: str, model: str,
                              report_md: str, inputs: dict, user_id: str | UUID) -> int:
    """
    Insert AI analysis report for a user.
    
    Args:
        ticker: Stock ticker (or 'portfolio' for portfolio-level)
        analysis_type: Type of analysis
        model: Model used
        report_md: Report markdown content
        inputs: Input parameters as dictionary
        user_id: User's UUID
    
    Returns:
        Report ID
    """
    user_id_str = str(user_id)
    inputs_json = json.dumps(inputs)
    
    try:
        query = f"""
            INSERT INTO public.ai_analysis_reports
            (user_id, ticker, analysis_type, model, report_md, inputs)
            VALUES
            ('{user_id_str}', '{ticker}', '{analysis_type}', '{model}', 
             :report_md, '{inputs_json}'::JSONB)
            RETURNING id
        """
        
        with self.connection.session as session:
            result = session.execute(text(query), {"report_md": report_md})
            report_id = result.scalar()
            session.commit()
        
        log_user_data_access(user_id, "INSERT", "ai_analysis_reports",
                           f"ticker={ticker}, analysis_type={analysis_type}")
        LOGGER.info("Inserted AI report: user_id=%s, report_id=%s, ticker=%s",
                   user_id_str[:8], report_id, ticker)
        
        return report_id
        
    except Exception as exc:
        LOGGER.exception("Failed to insert AI report for user_id=%s: %s", user_id_str, exc)
        raise


def get_latest_ai_report(self, ticker: str, analysis_type: str, user_id: str | UUID) -> dict[str, Any] | None:
    """
    Get most recent AI report for a user's ticker.
    
    Args:
        ticker: Stock ticker
        analysis_type: Type of analysis
        user_id: User's UUID
    
    Returns:
        Report data or None if not found
    """
    user_id_str = str(user_id)
    log_user_data_access(user_id, "SELECT", "ai_analysis_reports",
                        f"ticker={ticker}, analysis_type={analysis_type}")
    
    try:
        query = f"""
            SELECT id, report_md, inputs, created_at
            FROM public.ai_analysis_reports
            WHERE user_id = '{user_id_str}' 
              AND ticker = '{ticker}'
              AND analysis_type = '{analysis_type}'
            ORDER BY created_at DESC
            LIMIT 1
        """
        
        result = self._query_df(query)
        
        if result.empty:
            return None
        
        row = result.iloc[0]
        return {
            "id": row["id"],
            "report_md": row["report_md"],
            "inputs": json.loads(row["inputs"]) if isinstance(row["inputs"], str) else row["inputs"],
            "created_at": row["created_at"],
        }
        
    except Exception as exc:
        LOGGER.exception("Failed to get AI report for user_id=%s, ticker=%s: %s",
                        user_id_str, ticker, exc)
        return None


# ============================================================================
# HELPER METHODS
# ============================================================================

def _query_df(self, query: str) -> pd.DataFrame:
    """Execute query and return DataFrame."""
    try:
        with self.connection.session as session:
            result = session.execute(text(query))
            columns = [col[0] for col in result.cursor.description] if result.cursor.description else []
            data = result.fetchall()
            return pd.DataFrame(data, columns=columns)
    except Exception as exc:
        LOGGER.exception("Query execution failed: %s", exc)
        return pd.DataFrame()
