-- Multi-User Migration for Stackwealth App
-- Run this in Supabase SQL Editor to enable secure multi-user data isolation

-- =============================================================================
-- STEP 1: Add user_id columns to existing tables
-- =============================================================================

-- Add user_id to portfolio_cache
ALTER TABLE public.portfolio_cache 
ADD COLUMN user_id UUID DEFAULT auth.uid() NOT NULL;

ALTER TABLE public.portfolio_cache
ADD CONSTRAINT fk_portfolio_cache_user_id 
FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

-- Add user_id to transactions
ALTER TABLE public.transactions 
ADD COLUMN user_id UUID DEFAULT auth.uid() NOT NULL;

ALTER TABLE public.transactions
ADD CONSTRAINT fk_transactions_user_id 
FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

-- Add user_id to sync_profile
ALTER TABLE public.sync_profile 
ADD COLUMN user_id UUID DEFAULT auth.uid() NOT NULL;

-- Remove the singleton constraint since each user has their own profile
ALTER TABLE public.sync_profile
DROP CONSTRAINT IF EXISTS id;

ALTER TABLE public.sync_profile
ADD CONSTRAINT fk_sync_profile_user_id 
FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

-- Add unique constraint: only one profile per user
ALTER TABLE public.sync_profile
ADD CONSTRAINT sync_profile_user_id_unique UNIQUE (user_id);

-- Add user_id to ai_analysis_reports
ALTER TABLE public.ai_analysis_reports 
ADD COLUMN user_id UUID DEFAULT auth.uid() NOT NULL;

ALTER TABLE public.ai_analysis_reports
ADD CONSTRAINT fk_ai_analysis_reports_user_id 
FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

-- Add user_id to transcripts
ALTER TABLE public.transcripts 
ADD COLUMN user_id UUID DEFAULT auth.uid() NOT NULL;

ALTER TABLE public.transcripts
ADD CONSTRAINT fk_transcripts_user_id 
FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;

-- =============================================================================
-- STEP 2: Create composite primary keys for portfolio_cache and sync_profile
-- =============================================================================

-- portfolio_cache: change primary key to (user_id, ticker)
ALTER TABLE public.portfolio_cache
DROP CONSTRAINT portfolio_cache_pkey;

ALTER TABLE public.portfolio_cache
ADD PRIMARY KEY (user_id, ticker);

-- sync_profile: change primary key to (user_id)
ALTER TABLE public.sync_profile
DROP CONSTRAINT sync_profile_pkey;

ALTER TABLE public.sync_profile
ADD PRIMARY KEY (user_id);

-- =============================================================================
-- STEP 3: Enable Row Level Security (RLS)
-- =============================================================================

ALTER TABLE public.portfolio_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_profile ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_analysis_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transcripts ENABLE ROW LEVEL SECURITY;

-- =============================================================================
-- STEP 4: Create RLS Policies for portfolio_cache
-- =============================================================================

CREATE POLICY portfolio_cache_select 
ON public.portfolio_cache 
FOR SELECT 
USING (auth.uid() = user_id);

CREATE POLICY portfolio_cache_insert 
ON public.portfolio_cache 
FOR INSERT 
WITH CHECK (auth.uid() = user_id);

CREATE POLICY portfolio_cache_update 
ON public.portfolio_cache 
FOR UPDATE 
USING (auth.uid() = user_id)
WITH CHECK (auth.uid() = user_id);

CREATE POLICY portfolio_cache_delete 
ON public.portfolio_cache 
FOR DELETE 
USING (auth.uid() = user_id);

-- =============================================================================
-- STEP 5: Create RLS Policies for transactions
-- =============================================================================

CREATE POLICY transactions_select 
ON public.transactions 
FOR SELECT 
USING (auth.uid() = user_id);

CREATE POLICY transactions_insert 
ON public.transactions 
FOR INSERT 
WITH CHECK (auth.uid() = user_id);

CREATE POLICY transactions_update 
ON public.transactions 
FOR UPDATE 
USING (auth.uid() = user_id)
WITH CHECK (auth.uid() = user_id);

CREATE POLICY transactions_delete 
ON public.transactions 
FOR DELETE 
USING (auth.uid() = user_id);

-- =============================================================================
-- STEP 6: Create RLS Policies for sync_profile
-- =============================================================================

CREATE POLICY sync_profile_select 
ON public.sync_profile 
FOR SELECT 
USING (auth.uid() = user_id);

CREATE POLICY sync_profile_insert 
ON public.sync_profile 
FOR INSERT 
WITH CHECK (auth.uid() = user_id);

CREATE POLICY sync_profile_update 
ON public.sync_profile 
FOR UPDATE 
USING (auth.uid() = user_id)
WITH CHECK (auth.uid() = user_id);

CREATE POLICY sync_profile_delete 
ON public.sync_profile 
FOR DELETE 
USING (auth.uid() = user_id);

-- =============================================================================
-- STEP 7: Create RLS Policies for ai_analysis_reports
-- =============================================================================

CREATE POLICY ai_analysis_reports_select 
ON public.ai_analysis_reports 
FOR SELECT 
USING (auth.uid() = user_id);

CREATE POLICY ai_analysis_reports_insert 
ON public.ai_analysis_reports 
FOR INSERT 
WITH CHECK (auth.uid() = user_id);

CREATE POLICY ai_analysis_reports_update 
ON public.ai_analysis_reports 
FOR UPDATE 
USING (auth.uid() = user_id)
WITH CHECK (auth.uid() = user_id);

CREATE POLICY ai_analysis_reports_delete 
ON public.ai_analysis_reports 
FOR DELETE 
USING (auth.uid() = user_id);

-- =============================================================================
-- STEP 8: Create RLS Policies for transcripts
-- =============================================================================

CREATE POLICY transcripts_select 
ON public.transcripts 
FOR SELECT 
USING (auth.uid() = user_id);

CREATE POLICY transcripts_insert 
ON public.transcripts 
FOR INSERT 
WITH CHECK (auth.uid() = user_id);

CREATE POLICY transcripts_update 
ON public.transcripts 
FOR UPDATE 
USING (auth.uid() = user_id)
WITH CHECK (auth.uid() = user_id);

CREATE POLICY transcripts_delete 
ON public.transcripts 
FOR DELETE 
USING (auth.uid() = user_id);

-- =============================================================================
-- STEP 9: Create indexes for performance
-- =============================================================================

CREATE INDEX idx_portfolio_cache_user_id ON public.portfolio_cache(user_id);
CREATE INDEX idx_transactions_user_id ON public.transactions(user_id);
CREATE INDEX idx_ai_analysis_reports_user_id ON public.ai_analysis_reports(user_id);
CREATE INDEX idx_transcripts_user_id ON public.transcripts(user_id);

-- =============================================================================
-- STEP 10: Verify RLS is active
-- =============================================================================

-- Query to verify RLS status:
-- SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public';
