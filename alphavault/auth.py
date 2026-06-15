"""
Streamlit Authentication Module for Supabase
Handles login/logout, session state management, and user isolation
"""

from __future__ import annotations

import logging
from typing import Any

import streamlit as st
from supabase import create_client, Client

LOGGER = logging.getLogger(__name__)


def _init_supabase_client() -> Client:
    """Initialize Supabase client from Streamlit secrets."""
    supabase_url = st.secrets.get("SUPABASE_URL", "").strip()
    supabase_key = st.secrets.get("SUPABASE_ANON_KEY", "").strip()
    
    if not supabase_url or not supabase_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be configured in Streamlit secrets.")
    
    return create_client(supabase_url, supabase_key)


@st.cache_resource(show_spinner=False)
def get_supabase_client() -> Client:
    """Get cached Supabase client."""
    LOGGER.info("Initializing Supabase authentication client")
    return _init_supabase_client()


def init_auth_state() -> None:
    """Initialize authentication state in st.session_state."""
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "user_info" not in st.session_state:
        st.session_state.user_info = None
    if "access_token" not in st.session_state:
        st.session_state.access_token = None
    if "user_id" not in st.session_state:
        st.session_state.user_id = None


def login_user(email: str, password: str, client: Client) -> bool:
    """
    Authenticate user with email/password against Supabase.
    
    Args:
        email: User email address
        password: User password
        client: Supabase client instance
    
    Returns:
        True if login successful, False otherwise
    """
    try:
        LOGGER.info("START: User login attempt for email=%s", email)
        
        # Call Supabase authentication
        response = client.auth.sign_in_with_password({
            "email": email,
            "password": password,
        })
        
        if response and response.user:
            LOGGER.info("SUCCESS: User authenticated. user_id=%s, email=%s", response.user.id, email)
            
            # Store authentication details in session state
            st.session_state.authenticated = True
            st.session_state.user_info = {
                "user_id": response.user.id,
                "email": response.user.email,
                "created_at": str(response.user.created_at) if hasattr(response.user, 'created_at') else None,
            }
            st.session_state.access_token = response.session.access_token if response.session else None
            st.session_state.user_id = response.user.id
            
            LOGGER.info("User session stored in state: user_id=%s", st.session_state.user_id)
            return True
        else:
            LOGGER.warning("Login failed: No user in response for email=%s", email)
            return False
            
    except Exception as exc:
        LOGGER.warning("END: Login failed for email=%s: %s", email, str(exc))
        return False


def logout_user() -> None:
    """Clear authentication state and log out user."""
    try:
        LOGGER.info("START: User logout for user_id=%s", st.session_state.get("user_id"))
        
        # Clear session state
        st.session_state.authenticated = False
        st.session_state.user_info = None
        st.session_state.access_token = None
        st.session_state.user_id = None
        
        LOGGER.info("END: User session cleared successfully")
    except Exception as exc:
        LOGGER.error("Logout failed: %s", exc)


def is_authenticated() -> bool:
    """Check if user is currently authenticated."""
    return st.session_state.get("authenticated", False)


def get_current_user_id() -> str | None:
    """Get the current authenticated user's UUID."""
    return st.session_state.get("user_id")


def get_current_user_info() -> dict[str, Any] | None:
    """Get the current authenticated user's info."""
    return st.session_state.get("user_info")


def render_login_page(client: Client) -> None:
    """
    Render a clean, minimalist login card.
    
    Args:
        client: Supabase client instance
    """
    st.set_page_config(
        page_title="StackWealth - Login",
        page_icon="📊",
        layout="centered"
    )
    
    # Centered container
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        st.markdown("# 📊 StackWealth")
        st.markdown("**Multi-user Portfolio Tracker**")
        st.divider()
        
        st.markdown("### Sign In")
        
        email = st.text_input(
            "Email",
            placeholder="your@email.com",
            help="Your registered email address"
        )
        
        password = st.text_input(
            "Password",
            type="password",
            placeholder="••••••••",
            help="Your account password"
        )
        
        col_login, col_signup = st.columns(2)
        
        with col_login:
            login_button = st.button("Sign In", use_container_width=True, type="primary")
        
        with col_signup:
            st.markdown(
                "[Create Account →](https://your-supabase-instance.supabase.co/auth/v1/authorize)",
                help="Open Supabase auth UI to sign up"
            )
        
        # Handle login attempt
        if login_button:
            if not email or not password:
                st.error("❌ Please enter both email and password.")
            else:
                with st.spinner("Authenticating..."):
                    if login_user(email, password, client):
                        st.success("✅ Login successful! Redirecting...")
                        st.balloons()
                        st.rerun()
                    else:
                        st.error("❌ Invalid email or password. Please try again.")
        
        st.divider()
        st.markdown(
            """
            <div style='text-align: center; color: #888; font-size: 0.9em;'>
            <p><strong>StackWealth</strong> • Secure Multi-user Portfolio Tracking</p>
            <p>Powered by Supabase & Streamlit</p>
            </div>
            """,
            unsafe_allow_html=True
        )


def render_sidebar_logout() -> None:
    """Render logout button in sidebar."""
    if is_authenticated():
        user_info = get_current_user_info()
        user_email = user_info.get("email", "User") if user_info else "User"
        
        st.sidebar.divider()
        st.sidebar.markdown(f"**Logged in as:** {user_email}")
        
        if st.sidebar.button("🚪 Sign Out", use_container_width=True):
            LOGGER.info("Logout triggered by user")
            logout_user()
            st.rerun()


def require_authentication(client: Client) -> bool:
    """
    Check authentication and render login if needed.
    Returns True if authenticated, False if login page was shown.
    
    Args:
        client: Supabase client instance
    
    Returns:
        True if user is authenticated, False otherwise
    """
    init_auth_state()
    
    if not is_authenticated():
        render_login_page(client)
        return False
    
    return True


def get_user_context() -> dict[str, Any]:
    """
    Get complete user context for data isolation.
    To be used when querying or inserting user-specific data.
    
    Returns:
        Dictionary with user_id and access_token
    """
    return {
        "user_id": get_current_user_id(),
        "access_token": st.session_state.get("access_token"),
        "user_info": get_current_user_info(),
    }
