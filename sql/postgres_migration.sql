BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'stackwealth_service'
    ) THEN
        CREATE ROLE stackwealth_service LOGIN;
    END IF;
END
$$;

ALTER ROLE stackwealth_service BYPASSRLS;

GRANT USAGE ON SCHEMA public TO stackwealth_service;

CREATE TABLE IF NOT EXISTS public.portfolio_cache (
    ticker VARCHAR(32) PRIMARY KEY,
    company_name VARCHAR(255) NOT NULL,
    shares NUMERIC(12, 4) NOT NULL DEFAULT 0,
    avg_price NUMERIC(12, 4) NOT NULL DEFAULT 0,
    currency CHAR(3) NOT NULL DEFAULT 'USD',
    last_price NUMERIC(14, 4),
    market_cap NUMERIC(18, 2),
    unrealized_pnl_usd NUMERIC(14, 2),
    realized_pnl_usd NUMERIC(14, 2),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.transactions (
    execution_id VARCHAR(128) PRIMARY KEY,
    order_id VARCHAR(128),
    ticker VARCHAR(32) NOT NULL,
    tx_date DATE NOT NULL,
    side VARCHAR(8) NOT NULL,
    shares NUMERIC(12, 4) NOT NULL DEFAULT 0,
    price NUMERIC(12, 4) NOT NULL DEFAULT 0,
    amount NUMERIC(14, 2) NOT NULL,
    currency CHAR(3) NOT NULL DEFAULT 'USD',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.sync_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    baseline_date DATE NOT NULL,
    baseline_value_usd NUMERIC(14, 2),
    baseline_assets TEXT NOT NULL DEFAULT '[]',
    initialized BOOLEAN NOT NULL DEFAULT FALSE,
    initialized_at TIMESTAMPTZ,
    last_sync_at TIMESTAMPTZ,
    sync_version INTEGER NOT NULL DEFAULT 1,
    initial_sync_completed BOOLEAN NOT NULL DEFAULT FALSE,
    tracked_tickers TEXT NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.portfolio_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_profile ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.portfolio_cache TO stackwealth_service;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.transactions TO stackwealth_service;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.sync_profile TO stackwealth_service;

COMMIT;