# Multi-User Architecture Implementation Guide

## Overview
This guide explains how to integrate the new multi-user authentication system into your StackWealth application. The refactoring includes database schema changes, authentication UI, and data isolation at the application layer.

---

## 1. Database Migration Setup

### Prerequisites
- Supabase project created and configured
- Connection string saved in Streamlit secrets as `DATABASE_URL`
- PostgreSQL access to run migrations

### Steps

1. **Open Supabase SQL Editor**
   - Go to your Supabase dashboard
   - Navigate to SQL Editor
   - Create a new query

2. **Copy and Run the Migration**
   - Copy the contents of `sql/multi_user_migration.sql`
   - Paste into the SQL Editor
   - Click "Run" to execute all migration steps

3. **Verify RLS is Active**
   ```sql
   SELECT tablename, rowsecurity FROM pg_tables 
   WHERE schemaname = 'public' 
   ORDER BY tablename;
   ```
   All tables should show `rowsecurity = t` (true)

### What the Migration Does

- **Adds `user_id` column** to all relevant tables (portfolio_cache, transactions, sync_profile, ai_analysis_reports, transcripts)
- **Sets default** to `auth.uid()` (Supabase's authenticated user function)
- **Adds Foreign Keys** to Supabase's `auth.users` table
- **Enables Row Level Security** on all tables
- **Creates RLS Policies** that enforce: users can only access their own data
- **Creates Indexes** for query performance

---

## 2. Update Streamlit Secrets

Add these to your `.streamlit/secrets.toml`:

```toml
# Supabase Configuration
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_ANON_KEY = "your-anon-key-from-supabase"

# PostgreSQL (same as Supabase)
DATABASE_URL = "postgresql://postgres:password@db.your-project.supabase.co:5432/postgres"

# Existing API Keys (unchanged)
GEMINI_API_KEY = "..."
GEMINI_MODEL = "..."
# ... other existing secrets
```

---

## 3. Update Main Streamlit App

### 3.1 Modify `streamlit_app.py` Entry Point

Replace the current authentication check with the new auth module:

```python
from alphavault.auth import get_supabase_client, require_authentication, render_sidebar_logout
import streamlit as st

# Initialize page config
st.set_page_config(
    page_title="StackWealth",
    page_icon="📊",
    layout="wide"
)

# Initialize Supabase client
client = get_supabase_client()

# Enforce authentication
if not require_authentication(client):
    st.stop()  # Stop execution if not authenticated

# Render logout button in sidebar
render_sidebar_logout()

# Your existing tabs and content here...
# Now users automatically isolated by RLS!
```

### 3.2 Remove Old Password-Based Authentication

Delete or comment out the old `require_login()` function that checked a single password.

---

## 4. Update Database Functions in `postgres_store.py`

### 4.1 Add User Context to Queries

```python
from alphavault.user_context import inject_user_id_into_insert, log_user_data_access
import streamlit as st

def load_portfolio_state(self, user_id: str):
    """Load positions and transactions for a specific user."""
    log_user_data_access(user_id, "SELECT", "portfolio_cache")
    
    positions_df = self._query_df(
        f"SELECT * FROM public.portfolio_cache WHERE user_id = '{user_id}'"
    )
    # ... rest of function
```

### 4.2 Add User ID to Insert Operations

```python
def insert_transaction(self, ticker: str, tx_date: str, side: str, 
                       shares: float, price: float, amount: float, user_id: str):
    """Insert a new transaction for the authenticated user."""
    
    data = {
        "execution_id": str(uuid.uuid4()),
        "ticker": ticker,
        "tx_date": tx_date,
        "side": side,
        "shares": shares,
        "price": price,
        "amount": amount,
        "user_id": user_id,  # ADD THIS
    }
    
    log_user_data_access(user_id, "INSERT", "transactions", 
                        f"ticker={ticker}, shares={shares}")
    
    # Insert operation...
```

### 4.3 Update Schema Initialization

```python
def ensure_schema(self):
    """Updated schema initialization with user_id columns."""
    statements = [
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
        # ... other tables with user_id added
    ]
    # ... execute statements
```

---

## 5. Update Data-Fetching Functions in `streamlit_app.py`

### 5.1 Get User ID from Session State

```python
from alphavault.auth import get_current_user_id

def compute_dashboard(db, market_service):
    """Compute dashboard for the authenticated user."""
    user_id = get_current_user_id()
    
    if not user_id:
        st.error("User not authenticated")
        st.stop()
    
    LOGGER.info("Loading portfolio for user_id=%s", user_id)
    
    # Load user's positions and transactions
    positions, transactions = db.load_portfolio_state(user_id)
    
    # Rest of function remains the same
    # All data is now isolated by user!
```

### 5.2 Pass User ID When Inserting Data

```python
if st.button("Add Position"):
    ticker = st.text_input("Ticker")
    # ... other inputs
    
    user_id = get_current_user_id()
    
    try:
        db.insert_transaction(
            ticker=ticker,
            tx_date=tx_date,
            side="BUY",
            shares=shares,
            price=price,
            amount=amount,
            user_id=user_id  # ADD THIS
        )
        st.success("Position added!")
    except Exception as e:
        st.error(f"Failed to add position: {e}")
```

---

## 6. Update AI Report Insertion

```python
def save_ai_report(db, report_md: str, analysis_type: str, ticker: str, inputs: dict):
    """Save AI report for the authenticated user."""
    user_id = get_current_user_id()
    
    try:
        db.insert_ai_analysis_report(
            ticker=ticker,
            analysis_type=analysis_type,
            model=get_model_name(),
            report_md=report_md,
            inputs=inputs,
            user_id=user_id  # ADD THIS
        )
    except Exception as exc:
        LOGGER.exception("Failed to save AI report for user_id=%s: %s", user_id, exc)
```

---

## 7. Error Handling Template

```python
from alphavault.auth import login_user

try:
    LOGGER.info("Attempting to fetch user data")
    
    if not is_authenticated():
        st.error("❌ You must be logged in to access this feature.")
        st.stop()
    
    user_id = get_current_user_id()
    positions = db.load_portfolio_state(user_id)
    
    if positions.empty:
        st.info("📊 No positions found. Add your first stock holding.")
    else:
        st.dataframe(positions)
        
except ValueError as e:
    LOGGER.warning("Validation error: %s", e)
    st.error(f"❌ Invalid input: {e}")
    
except RuntimeError as e:
    LOGGER.error("Database error: %s", e)
    st.error(f"❌ Database error: {e}")
    
except Exception as e:
    LOGGER.exception("Unexpected error: %s", e)
    st.error(f"❌ An unexpected error occurred: {e}")
```

---

## 8. Testing Checklist

- [ ] Migration script runs without errors in Supabase
- [ ] RLS policies are active on all tables
- [ ] Login page renders when not authenticated
- [ ] Login succeeds with valid credentials
- [ ] Session state stores user_id correctly
- [ ] Portfolio data only shows logged-in user's data
- [ ] Logout clears session and returns to login
- [ ] New data inserts include user_id automatically
- [ ] AI reports are saved with correct user_id
- [ ] No data leakage between users (test with 2 accounts)

---

## 9. Security Best Practices

✅ **Enabled**
- Row Level Security at database layer (no code can bypass)
- Foreign key constraints (orphaned data impossible)
- User ID stored in session state (not localStorage)
- All queries filtered by user_id
- Logging of all data access for audit trail

✅ **Recommendations**
- Enable 2FA in Supabase Auth settings
- Set strong password requirements
- Use JWT token rotation
- Monitor auth logs for suspicious activity
- Regularly audit RLS policies

---

## 10. Backwards Compatibility Notes

All existing functionality remains the same:
- Portfolio calculations don't change
- AI analysis logic unchanged
- Market data fetch operations identical
- Only difference: data is now filtered by user_id

---

## Troubleshooting

### Issue: "Invalid API Key" when logging in
- Verify SUPABASE_URL and SUPABASE_ANON_KEY in secrets
- Check Supabase project is active
- Confirm user exists in Supabase Auth

### Issue: "Permission denied" on database operations
- Run the full migration script again
- Verify RLS is enabled: check with `SELECT rowsecurity FROM pg_tables...`
- Check RLS policies are created: `SELECT * FROM pg_policies`

### Issue: No data showing after login
- Verify sync_profile and portfolio_cache have user_id values
- Check user_id matches authenticated user: `SELECT auth.uid()`
- Test RLS policy: run query with correct user_id

---

## Support

For questions or issues:
1. Check Supabase logs: Dashboard → Database → Logs
2. Check Streamlit logs: Terminal output
3. Review RLS policies: Supabase → Database → Policies
4. Verify secrets: Streamlit → Settings → Secrets
