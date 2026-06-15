# Multi-User Architecture Implementation Checklist

## Phase 1: Infrastructure Setup (Day 1)

### Supabase Project Configuration
- [ ] Create Supabase project at https://supabase.com (or use existing)
- [ ] Copy Supabase Project URL from Settings → API
- [ ] Copy Supabase Anon Key from Settings → API
- [ ] Enable Email/Password authentication in Auth → Providers
- [ ] Configure email templates (optional but recommended)

### Secrets Configuration
- [ ] Update `.streamlit/secrets.toml` with:
  ```toml
  SUPABASE_URL = "https://your-project.supabase.co"
  SUPABASE_ANON_KEY = "your-anon-key"
  DATABASE_URL = "postgresql://postgres:password@..."
  ```
- [ ] Verify secrets file is in `.gitignore`
- [ ] Test connection locally with `streamlit run` (should not show connection errors)

### Database Migration
- [ ] Open Supabase SQL Editor
- [ ] Copy entire contents of `sql/multi_user_migration.sql`
- [ ] Paste into SQL Editor
- [ ] Click "Run" to execute (should take < 30 seconds)
- [ ] Verify success: no error messages displayed
- [ ] Verify RLS is active:
  ```sql
  SELECT tablename, rowsecurity FROM pg_tables 
  WHERE schemaname = 'public' ORDER BY tablename;
  ```
  All should show `rowsecurity = t`

---

## Phase 2: Code Implementation (Day 2-3)

### Step 1: Add New Modules
- [ ] Create `alphavault/auth.py` from provided template
  - [ ] Copy `auth.py` template to file
  - [ ] Verify imports are available (supabase package)
  - [ ] No syntax errors in IDE
  
- [ ] Create `alphavault/user_context.py` from provided template
  - [ ] Copy `user_context.py` template to file
  - [ ] Verify logging is imported correctly
  - [ ] No syntax errors

- [ ] Update `requirements.txt`
  - [ ] Add `supabase>=2.0.0`
  - [ ] Add `postgrest-py>=0.10.0`
  - [ ] Add `python-gotrue>=0.10.0`
  - [ ] Run `pip install -r requirements.txt` to verify no conflicts

### Step 2: Update PostgreSQL Store
- [ ] Backup existing `alphavault/postgres_store.py`
  - [ ] Rename to `postgres_store.py.backup`
  
- [ ] Update `load_portfolio_state()` method
  - [ ] Add `user_id: str | UUID` parameter
  - [ ] Add WHERE clause: `WHERE user_id = '{user_id}'`
  - [ ] Add logging: `log_user_data_access(user_id, "SELECT", "portfolio_cache")`
  - [ ] Test with sample user_id
  
- [ ] Update `load_sync_profile()` method
  - [ ] Add `user_id: str | UUID` parameter
  - [ ] Add WHERE clause: `WHERE user_id = '{user_id}'`
  - [ ] Create `_create_default_sync_profile()` helper (provided in template)
  
- [ ] Update `insert_transaction()` method
  - [ ] Add `user_id: str | UUID` parameter
  - [ ] Include user_id in INSERT statement
  - [ ] Add logging before/after insert
  
- [ ] Update `insert_ai_analysis_report()` method
  - [ ] Add `user_id: str | UUID` parameter
  - [ ] Include user_id in INSERT statement
  - [ ] Return report_id
  
- [ ] Update `get_latest_ai_report()` method
  - [ ] Add `user_id: str | UUID` parameter
  - [ ] Filter by user_id in WHERE clause
  
- [ ] Add helper methods
  - [ ] `_create_default_sync_profile(user_id)` for new users
  - [ ] `update_sync_profile()` to update baseline values
  - [ ] Update `ensure_schema()` to initialize with RLS enabled

### Step 3: Update Streamlit App Entry Point
- [ ] Open `streamlit_app.py`
- [ ] Add imports at top:
  ```python
  from alphavault.auth import (
      get_supabase_client,
      require_authentication,
      render_sidebar_logout,
      get_current_user_id,
      get_current_user_info
  )
  ```

- [ ] Add authentication check (BEFORE any data loading):
  ```python
  client = get_supabase_client()
  if not require_authentication(client):
      st.stop()
  user_id = get_current_user_id()
  ```

- [ ] Add sidebar logout:
  ```python
  with st.sidebar:
      render_sidebar_logout()
  ```

- [ ] Update database initialization:
  - [ ] Create `db = PostgresStore()` (no changes needed)
  
- [ ] Update `compute_dashboard()` function:
  - [ ] Add `user_id = get_current_user_id()` at start
  - [ ] Pass `user_id` to `db.load_portfolio_state(user_id)`
  - [ ] Pass `user_id` to all other db methods
  
- [ ] Update AI analysis functions:
  - [ ] Get `user_id = get_current_user_id()`
  - [ ] Pass `user_id` when saving reports: `db.insert_ai_analysis_report(..., user_id=user_id)`

- [ ] Update all forms and inputs:
  - [ ] Any INSERT operation gets `user_id=user_id` parameter
  - [ ] Any query gets `user_id=user_id` filter

### Step 4: Error Handling
- [ ] Wrap all database operations in try/except:
  ```python
  try:
      user_id = get_current_user_id()
      data = db.load_portfolio_state(user_id)
  except ValueError as e:
      st.error(f"Validation error: {e}")
  except Exception as e:
      st.error(f"Database error: {e}")
  ```

- [ ] Add logging to all critical paths:
  ```python
  LOGGER.info("User action: user_id=%s, action=%s", user_id[:8], action)
  ```

- [ ] Test error scenarios:
  - [ ] Login with invalid credentials → Should see error message
  - [ ] Logout → Should return to login page
  - [ ] Access page without authentication → Should be redirected to login

---

## Phase 3: Testing (Day 4-5)

### Unit Testing
- [ ] Test `auth.py` functions:
  - [ ] `login_user()` with valid credentials
  - [ ] `login_user()` with invalid credentials
  - [ ] `logout_user()` clears session state
  - [ ] `is_authenticated()` returns correct state
  - [ ] `get_current_user_id()` returns UUID

- [ ] Test `postgres_store.py` with user_id:
  - [ ] `load_portfolio_state(user_id)` for specific user
  - [ ] `insert_transaction(..., user_id)` creates record
  - [ ] `get_latest_ai_report(..., user_id)` retrieves correct report

- [ ] Test `user_context.py` helpers:
  - [ ] `inject_user_id_into_insert()` adds user_id to data
  - [ ] `build_user_filter_clause()` generates correct SQL
  - [ ] `validate_user_ownership()` works correctly

### Integration Testing
- [ ] Test login/logout flow:
  - [ ] User A logs in
  - [ ] Verifies sees their own data
  - [ ] Logs out
  - [ ] User B logs in
  - [ ] Verifies sees their own data (not User A's)

- [ ] Test data isolation:
  - [ ] Create test account A with portfolio
  - [ ] Create test account B with different portfolio
  - [ ] Account A logs in → sees only their data
  - [ ] Account B logs in → sees only their data
  - [ ] Try to manually query other user's data → denied by RLS

- [ ] Test RLS enforcement:
  - [ ] Query sync_profile for account A
  - [ ] Try to query with different user_id in WHERE
  - [ ] Should return no results (RLS blocks it)

### Security Testing
- [ ] Verify JWT tokens:
  - [ ] Token stored in `st.session_state.access_token`
  - [ ] Token not visible in browser storage
  
- [ ] Verify password requirements:
  - [ ] Weak passwords rejected by Supabase auth
  - [ ] Strong passwords accepted
  
- [ ] Verify session timeout:
  - [ ] Leave session idle for > configured timeout
  - [ ] Should require re-authentication

---

## Phase 4: Deployment (Day 6)

### Pre-Deployment
- [ ] Remove all test data from production database
- [ ] Verify all API keys are correct in Supabase
- [ ] Test with 2 different user accounts on staging
- [ ] Verify no hardcoded passwords in code
- [ ] Run security checklist:
  - [ ] RLS enabled on all tables
  - [ ] RLS policies correct for each table
  - [ ] No backdoor queries that bypass RLS
  - [ ] Logging configured for audit trail

### Deployment Steps
- [ ] Push code to repository
- [ ] Deploy to staging environment
- [ ] Test on staging:
  - [ ] Create new account
  - [ ] Add portfolio data
  - [ ] Generate AI analysis
  - [ ] Verify data isolation
  
- [ ] Create data backup (optional):
  ```sql
  pg_dump --username postgres --db-name your_db > backup_pre_multiuser.sql
  ```

- [ ] Deploy to production
- [ ] Monitor logs for errors
- [ ] Have rollback plan ready

### Post-Deployment
- [ ] Send announcement to users about new multi-user support
- [ ] Provide documentation:
  - [ ] How to create account
  - [ ] How to reset password
  - [ ] Data security explanation
  
- [ ] Monitor:
  - [ ] Check logs for authentication errors
  - [ ] Monitor database query performance
  - [ ] Review RLS policy usage in Supabase logs

---

## Phase 5: Migration (Optional)

### Migrate Existing Single-User Data
- [ ] Identify single-user data in current system
- [ ] Create migration script to assign user_id
  ```sql
  UPDATE portfolio_cache SET user_id = 'admin-user-uuid' WHERE user_id IS NULL;
  UPDATE transactions SET user_id = 'admin-user-uuid' WHERE user_id IS NULL;
  ```
- [ ] Verify data integrity before and after
- [ ] Test that admin user can see migrated data

---

## Troubleshooting Matrix

| Issue | Cause | Fix |
|-------|-------|-----|
| "Invalid API Key" on login | Wrong SUPABASE_ANON_KEY | Verify key in Supabase Settings → API |
| "Permission denied" on queries | RLS policy blocking | Check RLS is disabled for development (enable later) |
| No data showing after login | user_id not in WHERE clause | Verify all queries include `WHERE user_id = ...` |
| Login page loops | `st.rerun()` not called | Ensure `st.rerun()` after successful login |
| "auth.uid() not available" | App not using Supabase auth | Ensure using Supabase auth, not custom auth |
| Database connection fails | Wrong DATABASE_URL | Verify connection string in secrets |
| User can see other user's data | RLS not enabled | Run SQL migration `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` |

---

## Validation Checklist (Before Going Live)

- [ ] All tables have user_id column
- [ ] All tables have RLS enabled
- [ ] All RLS policies created correctly
- [ ] No hardcoded single-user assumptions in code
- [ ] All database calls include user_id
- [ ] Authentication flow works (login → app → logout → login)
- [ ] Data isolation verified with 2+ test accounts
- [ ] Error messages are user-friendly
- [ ] Logging includes user_id for audit trail
- [ ] Documentation updated for users and developers
- [ ] Backup created before migration
- [ ] Rollback plan documented and tested

---

## Success Criteria

✅ Users can create individual accounts  
✅ Each user sees only their own portfolio data  
✅ Data is isolated at database layer (RLS)  
✅ No user can access another user's data  
✅ Performance is acceptable (< 2s page load)  
✅ All audit logs include user_id  
✅ Logout works correctly  
✅ New users can add their portfolio  

---

## Estimated Timeline

- **Phase 1 (Infrastructure):** 2-4 hours
- **Phase 2 (Implementation):** 6-8 hours
- **Phase 3 (Testing):** 4-6 hours
- **Phase 4 (Deployment):** 2-4 hours
- **Phase 5 (Migration):** 1-2 hours (if needed)

**Total: 15-24 hours of development time**

---

## Support Resources

- [Supabase Documentation](https://supabase.com/docs)
- [Row Level Security Guide](https://supabase.com/docs/guides/auth/row-level-security)
- [Streamlit Documentation](https://docs.streamlit.io)
- [SQLAlchemy ORM Guide](https://docs.sqlalchemy.org/)
