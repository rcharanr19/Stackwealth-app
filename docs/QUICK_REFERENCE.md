# Multi-User Architecture - Quick Reference Card

## 🚀 60-Second Overview

**What**: Converting StackWealth from single-user to multi-user with Supabase authentication and Row Level Security (RLS)

**Why**: Database-level data isolation, production-grade authentication, user privacy

**How**: 4 new files + 3 updated files + 1 SQL migration script

**Timeline**: 15-24 hours of development work

---

## 📦 4 Files You Need to Create

### 1. `alphavault/auth.py`
```python
# Copy from: alphavault/auth.py (created)
# Use in streamlit_app.py like:
client = get_supabase_client()
if not require_authentication(client):
    st.stop()
```

### 2. `alphavault/user_context.py`
```python
# Copy from: alphavault/user_context.py (created)
# Helper functions for user_id handling
log_user_data_access(user_id, "SELECT", "table_name")
```

### 3. `sql/multi_user_migration.sql`
```sql
-- Copy from: sql/multi_user_migration.sql (created)
-- Run in Supabase SQL Editor
-- Adds user_id columns + RLS policies to all 5 tables
```

### 4. `requirements-multiuser.txt`
```
supabase>=2.0.0
postgrest-py>=0.10.0
python-gotrue>=0.10.0
```

---

## 🔧 3 Files to Update

### 1. `streamlit_app.py` - Add at Top
```python
from alphavault.auth import (
    get_supabase_client, require_authentication, 
    render_sidebar_logout, get_current_user_id
)

# FIRST: Check authentication
client = get_supabase_client()
if not require_authentication(client):
    st.stop()

# Get user_id
user_id = get_current_user_id()

# Render logout button
render_sidebar_logout()

# Pass user_id to ALL db calls
positions = db.load_portfolio_state(user_id)
db.insert_transaction(..., user_id=user_id)
```

### 2. `alphavault/postgres_store.py` - Update Methods
```python
# Add user_id parameter to ALL methods:
def load_portfolio_state(self, user_id: str):
    query = f"SELECT * FROM portfolio_cache WHERE user_id = '{user_id}'"
    return self._query_df(query)

def insert_transaction(self, ..., user_id: str):
    # Include user_id in INSERT
    query = f"INSERT INTO transactions (user_id, ...) VALUES ('{user_id}', ...)"
    
def get_latest_ai_report(self, ticker, analysis_type, user_id: str):
    query = f"SELECT * FROM ai_analysis_reports WHERE user_id = '{user_id}' ..."
```

### 3. `.streamlit/secrets.toml` - Add Credentials
```toml
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_ANON_KEY = "your-key-here"
DATABASE_URL = "postgresql://user:pass@db.supabase.co:5432/postgres"
```

---

## ✅ Implementation Checklist (Fast Track)

- [ ] Create 4 new files (auth.py, user_context.py, migration.sql, requirements-multiuser.txt)
- [ ] Update 3 existing files (streamlit_app.py, postgres_store.py, secrets.toml)
- [ ] Run SQL migration in Supabase SQL Editor
- [ ] Test login/logout with streamlit_app.py
- [ ] Test data isolation with 2+ accounts
- [ ] Verify no data leakage across users
- [ ] Deploy to production

---

## 🔑 Key Code Patterns

### Pattern 1: Get User ID in Any Function
```python
from alphavault.auth import get_current_user_id

def my_function():
    user_id = get_current_user_id()
    # Use user_id in all database queries
```

### Pattern 2: Pass User ID to DB Calls
```python
# WRONG:
db.load_portfolio_state()  # ❌ Missing user_id

# RIGHT:
user_id = get_current_user_id()
db.load_portfolio_state(user_id)  # ✅ Pass user_id
```

### Pattern 3: Update Database Function Signature
```python
# BEFORE:
def load_portfolio_state(self):
    query = "SELECT * FROM portfolio_cache"

# AFTER:
def load_portfolio_state(self, user_id: str):
    query = f"SELECT * FROM portfolio_cache WHERE user_id = '{user_id}'"
```

### Pattern 4: Error Handling
```python
try:
    user_id = get_current_user_id()
    data = db.load_portfolio_state(user_id)
except Exception as e:
    st.error(f"Error: {e}")
    LOGGER.exception("Failed for user_id=%s: %s", user_id[:8], e)
```

---

## 🗄️ Database Changes at a Glance

### New Columns (All Tables)
```sql
ALTER TABLE portfolio_cache ADD COLUMN user_id UUID DEFAULT auth.uid();
ALTER TABLE transactions ADD COLUMN user_id UUID DEFAULT auth.uid();
ALTER TABLE sync_profile ADD COLUMN user_id UUID DEFAULT auth.uid();
ALTER TABLE ai_analysis_reports ADD COLUMN user_id UUID DEFAULT auth.uid();
ALTER TABLE transcripts ADD COLUMN user_id UUID DEFAULT auth.uid();
```

### New RLS Policies (20 Total: 4 per Table)
```sql
-- Example for portfolio_cache
CREATE POLICY portfolio_cache_select ON portfolio_cache FOR SELECT 
  USING (auth.uid() = user_id);
-- (Same pattern for INSERT, UPDATE, DELETE)
```

### New Indexes
```sql
CREATE INDEX idx_portfolio_cache_user_id ON portfolio_cache(user_id);
CREATE INDEX idx_transactions_user_id ON transactions(user_id);
CREATE INDEX idx_ai_analysis_reports_user_id ON ai_analysis_reports(user_id);
CREATE INDEX idx_transcripts_user_id ON transcripts(user_id);
```

---

## 🧪 Testing Matrix

| Test Case | Expected Result | Pass? |
|-----------|-----------------|-------|
| User A logs in | Sees login page → enters credentials → redirects to portfolio | [ ] |
| User A adds AAPL | 10 shares added to User A's portfolio | [ ] |
| User A logs out | Logged out, returns to login page | [ ] |
| User B logs in | Sees login page, different user | [ ] |
| User B checks portfolio | Empty (doesn't see User A's AAPL) | [ ] |
| User A logs back in | Sees their 10 shares of AAPL | [ ] |
| Try manual SQL bypass | RLS blocks access to other user's data | [ ] |
| Query performance | < 2 second page load time | [ ] |

---

## 🚨 Critical Points (Don't Miss!)

1. **Order Matters**: Authentication check MUST be first in streamlit_app.py
   ```python
   client = get_supabase_client()
   if not require_authentication(client):  # ✅ BEFORE everything else
       st.stop()
   ```

2. **User ID in ALL Queries**: Every SELECT/INSERT/UPDATE must include user_id
   ```python
   query = f"... WHERE user_id = '{user_id}'"  # ✅ Always
   ```

3. **RLS Enforces Security**: Database layer, not app layer
   - Even if you forget user_id in code, RLS blocks cross-user access
   - This is the safety net that prevents data leaks

4. **Secrets Must Be Set**: App will fail if SUPABASE_URL not in secrets
   ```toml
   SUPABASE_URL = "..."  # ✅ Required in .streamlit/secrets.toml
   ```

5. **Migration Script First**: Run SQL before updating Python code
   - Schema changes must happen before app tries to access new columns

---

## 📊 Architecture Diagram

```
User Logs In
    ↓
require_authentication(client) 
    ↓ [success]
st.session_state.user_id = "uuid-123"
    ↓
get_current_user_id() → "uuid-123"
    ↓
db.load_portfolio_state(user_id)
    ↓
SQL: SELECT * WHERE user_id = 'uuid-123'
    ↓ [RLS CHECK]
Only User's Data Returned
    ↓
Render Portfolio
```

---

## 🎯 Files Quick Links

| File | Purpose | Status |
|------|---------|--------|
| `sql/multi_user_migration.sql` | Database schema changes | ✅ Created |
| `alphavault/auth.py` | Login/logout + session | ✅ Created |
| `alphavault/user_context.py` | User_id helpers | ✅ Created |
| `docs/MULTI_USER_SETUP.md` | Detailed setup (10 sections) | ✅ Created |
| `docs/POSTGRES_STORE_UPDATES.md` | Database function examples | ✅ Created |
| `docs/STREAMLIT_APP_REFACTORING.md` | App integration guide | ✅ Created |
| `docs/IMPLEMENTATION_CHECKLIST.md` | Phase-by-phase checklist | ✅ Created |
| `docs/MULTI_USER_SUMMARY.md` | Complete deliverables | ✅ Created |

---

## ⏰ Estimated Effort per Task

| Task | Effort | Notes |
|------|--------|-------|
| Copy auth.py | 5 min | Copy + paste |
| Copy user_context.py | 5 min | Copy + paste |
| Run SQL migration | 10 min | Run in Supabase SQL Editor |
| Update secrets.toml | 10 min | Add 3 variables |
| Update streamlit_app.py | 30 min | Add imports + auth check + get_user_id |
| Update postgres_store.py | 2 hrs | Add user_id to 6 functions |
| Testing | 2 hrs | Test with 2+ accounts |
| **Total** | **4-5 hrs** | (Fast track) |

---

## 🆘 When You Get Stuck

### "Invalid API Key"
```
→ Check SUPABASE_ANON_KEY in secrets
→ Verify Supabase project is active
```

### "Permission denied" Errors
```
→ Run SQL migration (enables RLS)
→ Verify all queries include user_id filter
```

### No Data Shows After Login
```
→ Check sync_profile has user_id value
→ Verify WHERE clause: WHERE user_id = '{user_id}'
→ Check RLS is enabled: SELECT rowsecurity FROM pg_tables
```

### Login Page Loops
```
→ Ensure st.rerun() is called after successful login
→ Check session_state.authenticated is set correctly
```

---

## ✨ Success = All 3 Must Be True

1. ✅ **User A can log in** and see their portfolio
2. ✅ **User B can log in** and see DIFFERENT portfolio (not A's)
3. ✅ **Database blocks** any direct attempt to access other user's data

---

## 📞 Documentation Map

- **Quick overview?** → Read this file (QUICK_REFERENCE.md)
- **Step-by-step instructions?** → Read IMPLEMENTATION_CHECKLIST.md
- **Database questions?** → Read POSTGRES_STORE_UPDATES.md
- **App integration?** → Read STREAMLIT_APP_REFACTORING.md
- **Complete details?** → Read MULTI_USER_SETUP.md + MULTI_USER_SUMMARY.md

---

**Version**: 1.0  
**Created**: January 15, 2025  
**Status**: Ready to implement  
**Estimated Completion**: 15-24 hours
