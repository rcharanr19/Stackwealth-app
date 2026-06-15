"""
Streamlit App Refactoring for Multi-User Support
This shows the updated entry point and key functions
"""

import logging
import streamlit as st
from alphavault.auth import (
    get_supabase_client,
    require_authentication,
    render_sidebar_logout,
    get_current_user_id,
    get_current_user_info
)
from alphavault.postgres_store import PostgresStore

LOGGER = logging.getLogger(__name__)

# =============================================================================
# PAGE CONFIGURATION
# =============================================================================

st.set_page_config(
    page_title="StackWealth Portfolio",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# =============================================================================
# AUTHENTICATION - FIRST CHECK IN APP
# =============================================================================

# Initialize Supabase client
client = get_supabase_client()

# Enforce authentication - if returns False, show login and stop
if not require_authentication(client):
    st.stop()

# At this point, user is guaranteed to be authenticated
user_id = get_current_user_id()
user_info = get_current_user_info()

LOGGER.info("User authenticated: user_id=%s, email=%s", user_id[:8], user_info.get("email"))

# Render logout button in sidebar
render_sidebar_logout()

# =============================================================================
# INITIALIZE DATABASE WITH USER CONTEXT
# =============================================================================

# Initialize database connection (works with Streamlit secrets)
db = PostgresStore()

# All database calls now include user_id for isolation
# Example: db.load_portfolio_state(user_id)

# =============================================================================
# SIDEBAR INFORMATION
# =============================================================================

with st.sidebar:
    st.divider()
    st.markdown("### 📊 Portfolio Dashboard")
    
    # Display user email
    st.markdown(f"**User:** {user_info.get('email')}")
    
    # Display options
    view_option = st.radio(
        "View",
        ["Portfolio", "AI Analysis", "Settings"],
        horizontal=False
    )

# =============================================================================
# MAIN CONTENT AREA
# =============================================================================

if view_option == "Portfolio":
    render_portfolio_view(db, user_id)

elif view_option == "AI Analysis":
    render_ai_analysis_view(db, user_id)

elif view_option == "Settings":
    render_settings_view(db, user_id)


# =============================================================================
# VIEW FUNCTIONS (EXAMPLES)
# =============================================================================

def render_portfolio_view(db: PostgresStore, user_id: str):
    """Render portfolio view with user-specific data."""
    st.header("📈 Your Portfolio")
    
    try:
        LOGGER.info("Loading portfolio for user_id=%s", user_id[:8])
        
        # Load user's portfolio (RLS ensures only their data is fetched)
        positions_df, transactions_df = db.load_portfolio_state(user_id)
        
        if positions_df.empty:
            st.info("📊 No positions found. Add your first holding to get started!")
            
            # Example: Add position form
            with st.form("add_position"):
                col1, col2, col3 = st.columns(3)
                with col1:
                    ticker = st.text_input("Ticker", "AAPL").upper()
                with col2:
                    shares = st.number_input("Shares", 1.0, step=0.1)
                with col3:
                    price = st.number_input("Avg Cost", 100.0, step=0.01)
                
                if st.form_submit_button("Add Position", use_container_width=True):
                    try:
                        # Insert with user_id
                        db.insert_transaction(
                            ticker=ticker,
                            tx_date=str(pd.Timestamp.now().date()),
                            side="BUY",
                            shares=shares,
                            price=price,
                            amount=shares * price,
                            user_id=user_id  # CRITICAL: Pass user_id
                        )
                        st.success(f"✅ Added {shares} shares of {ticker}")
                        st.rerun()
                    except Exception as e:
                        LOGGER.exception("Failed to add position for user_id=%s: %s", user_id[:8], e)
                        st.error(f"❌ Failed to add position: {e}")
        else:
            # Display positions
            st.subheader("Holdings")
            st.dataframe(positions_df, use_container_width=True)
            
            # Display transactions
            st.subheader("Transaction History")
            st.dataframe(transactions_df, use_container_width=True)
    
    except Exception as e:
        LOGGER.exception("Portfolio view error for user_id=%s: %s", user_id[:8], e)
        st.error(f"❌ Error loading portfolio: {e}")


def render_ai_analysis_view(db: PostgresStore, user_id: str):
    """Render AI analysis view with user-specific reports."""
    st.header("🤖 AI Analysis")
    
    try:
        # Load user's portfolio to get tickers
        positions_df, _ = db.load_portfolio_state(user_id)
        
        if positions_df.empty:
            st.info("Add positions to your portfolio first to generate AI analysis")
            return
        
        tickers = positions_df["ticker"].tolist()
        selected_ticker = st.selectbox("Select ticker", tickers)
        
        if st.button("Generate Analysis", use_container_width=True):
            with st.spinner("Generating AI analysis..."):
                try:
                    # Generate report
                    report_md = generate_analysis(selected_ticker, user_id)  # Your analysis function
                    
                    # Save report with user_id
                    db.insert_ai_analysis_report(
                        ticker=selected_ticker,
                        analysis_type="investment_thesis",
                        model="gemini-2.0-flash",
                        report_md=report_md,
                        inputs={"ticker": selected_ticker},
                        user_id=user_id  # CRITICAL: Pass user_id
                    )
                    
                    st.success("✅ Analysis saved")
                    st.markdown(report_md)
                    
                except Exception as e:
                    LOGGER.exception("AI analysis failed for user_id=%s: %s", user_id[:8], e)
                    st.error(f"❌ Analysis failed: {e}")
        
        # Display cached reports (user's own only)
        st.subheader("Previous Analyses")
        report = db.get_latest_ai_report(selected_ticker, "investment_thesis", user_id)
        if report:
            st.markdown(report["report_md"])
        else:
            st.info("No previous analysis found")
    
    except Exception as e:
        LOGGER.exception("AI analysis view error for user_id=%s: %s", user_id[:8], e)
        st.error(f"❌ Error: {e}")


def render_settings_view(db: PostgresStore, user_id: str):
    """Render settings view with user account options."""
    st.header("⚙️ Settings")
    
    st.subheader("Account Information")
    col1, col2 = st.columns(2)
    
    with col1:
        user_info = get_current_user_info()
        st.markdown(f"**Email:** {user_info.get('email')}")
        st.markdown(f"**User ID:** `{user_id}`")
    
    with col2:
        st.markdown(f"**Created:** {user_info.get('created_at')}")
    
    st.divider()
    
    st.subheader("Preferences")
    
    # Load user's sync profile
    try:
        sync_profile = db.load_sync_profile(user_id)
        
        col1, col2 = st.columns(2)
        with col1:
            baseline_value = st.number_input(
                "Baseline Portfolio Value ($)",
                value=float(sync_profile.get("baseline_value_usd", 0)),
                step=100.0
            )
        
        if st.button("Update Settings"):
            try:
                # Update sync profile
                db.update_sync_profile(
                    baseline_value_usd=baseline_value,
                    user_id=user_id
                )
                st.success("✅ Settings updated")
                st.rerun()
            except Exception as e:
                LOGGER.exception("Failed to update settings for user_id=%s: %s", user_id[:8], e)
                st.error(f"❌ Update failed: {e}")
    
    except Exception as e:
        LOGGER.exception("Settings view error for user_id=%s: %s", user_id[:8], e)
        st.error(f"❌ Error loading settings: {e}")


# =============================================================================
# KEY IMPLEMENTATION POINTS
# =============================================================================

"""
✅ REQUIRED CHANGES SUMMARY:

1. AUTHENTICATION ENTRY POINT (lines 30-32)
   - Call require_authentication(client) FIRST thing
   - This shows login page if not authenticated
   - Returns False if login shown, True if authenticated

2. GET USER ID (line 34-37)
   - Get user_id from session_state via get_current_user_id()
   - Use this user_id in ALL database calls

3. DATABASE INITIALIZATION (line 43)
   - Create PostgresStore instance
   - No changes needed to initialization

4. DATABASE CALLS (throughout)
   - ALL queries include user_id parameter
   - insert_transaction(..., user_id=user_id)
   - db.load_portfolio_state(user_id)
   - db.get_latest_ai_report(ticker, analysis_type, user_id)

5. ERROR HANDLING
   - Wrap database calls in try/except
   - Log user_id[:8] for audit trail
   - Display user-friendly error messages

6. LOGOUT (sidebar)
   - Automatically handled by render_sidebar_logout()
   - No code changes needed

7. ROW LEVEL SECURITY
   - Database enforces data isolation via RLS policies
   - No user can access another user's data
   - Even if app code has bugs, database protects data
"""
