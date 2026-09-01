"""Debug script to diagnose Robinhood margin sync issues."""

import sys
import os
import json
import logging
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from alphavault.logging_utils import configure_logging

# Enable debug logging
configure_logging(debug_enabled=True)

LOGGER = logging.getLogger(__name__)


def diagnose_margin_sync():
    """Test margin API endpoints directly."""
    
    print("\n" + "=" * 70)
    print("StackWealth Robinhood Margin Sync Diagnostics")
    print("=" * 70)
    
    # Check for credentials
    print("\n1. Checking credentials...")
    try:
        import keyring
        email = input("Enter Robinhood email: ").strip()
        password = keyring.get_password("StackWealthApp", email)
        if not password:
            password = keyring.get_password("AlphaVaultApp", email)
        if not password:
            password = input("Enter Robinhood password (will not be stored): ").strip()
        
        if not email or not password:
            print("❌ Email or password missing")
            return
        
        print(f"✓ Credentials provided for {email[:10]}***")
    except Exception as e:
        print(f"❌ Credential check failed: {e}")
        return
    
    # Try login
    print("\n2. Attempting login...")
    try:
        from robin_stocks import robinhood as r
        
        mfa_code = input("Enter 2FA code (or press Enter for push): ").strip()
        
        if mfa_code:
            print(f"Logging in with MFA code...")
            login_resp = r.login(
                username=email,
                password=password,
                mfa_code=mfa_code,
            )
        else:
            print(f"Logging in with push approval (check your app)...")
            login_resp = r.login(
                username=email,
                password=password,
            )
        
        if not login_resp or not login_resp.get("access_token"):
            print(f"❌ Login failed: {login_resp}")
            return
        
        print("✓ Login successful")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Test account profile
    print("\n3. Testing Account Profile endpoint...")
    try:
        account_number = input("Enter account number (or press Enter for default): ").strip() or None
        account_profile = r.profiles.load_account_profile(account_number=account_number)
        
        if account_profile:
            print(f"✓ Account profile retrieved")
            print(f"  Keys: {sorted(account_profile.keys())}")
            
            # Look for margin data
            if "margin_balances" in account_profile:
                print(f"  margin_balances: {account_profile['margin_balances']}")
            if "cash" in account_profile:
                print(f"  cash: {account_profile['cash']}")
        else:
            print("❌ Account profile is None or empty")
    except Exception as e:
        print(f"❌ Account profile failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Test portfolio profile
    print("\n4. Testing Portfolio Profile endpoint...")
    try:
        portfolio_profile = r.profiles.load_portfolio_profile(account_number=account_number)
        
        if portfolio_profile:
            print(f"✓ Portfolio profile retrieved")
            print(f"  Keys: {sorted(portfolio_profile.keys())}")
            
            # Look for margin data
            if "excess_margin" in portfolio_profile:
                print(f"  excess_margin: {portfolio_profile['excess_margin']}")
            if "excess_maintenance" in portfolio_profile:
                print(f"  excess_maintenance: {portfolio_profile['excess_maintenance']}")
        else:
            print("❌ Portfolio profile is None or empty")
    except Exception as e:
        print(f"❌ Portfolio profile failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Test phoenix account
    print("\n5. Testing Phoenix Account endpoint...")
    try:
        phoenix_account = r.account.load_phoenix_account()
        
        if phoenix_account:
            print(f"✓ Phoenix account retrieved")
            if isinstance(phoenix_account, list):
                print(f"  Type: list with {len(phoenix_account)} items")
                if phoenix_account and isinstance(phoenix_account[0], dict):
                    print(f"  Keys: {sorted(phoenix_account[0].keys())}")
                    if "levered_amount" in phoenix_account[0]:
                        print(f"  levered_amount: {phoenix_account[0]['levered_amount']}")
            elif isinstance(phoenix_account, dict):
                print(f"  Type: dict")
                print(f"  Keys: {sorted(phoenix_account.keys())}")
                if "levered_amount" in phoenix_account:
                    print(f"  levered_amount: {phoenix_account['levered_amount']}")
        else:
            print("❌ Phoenix account is None or empty")
    except Exception as e:
        print(f"❌ Phoenix account failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Test direct margin balance endpoints
    print("\n6. Testing Direct Margin Balance endpoints...")
    try:
        candidate_urls = [
            f"https://api.robinhood.com/margin/accounts/{account_number}/",
            f"https://api.robinhood.com/accounts/{account_number}/margin_balances/",
        ]
        
        for url in candidate_urls:
            try:
                data = r.request_get(url)
                if data:
                    print(f"✓ {url}")
                    if isinstance(data, dict):
                        print(f"  Keys: {sorted(data.keys())}")
                        if "margin_balance" in data:
                            print(f"  margin_balance: {data['margin_balance']}")
                        if "outstanding_margin_balance" in data:
                            print(f"  outstanding_margin_balance: {data['outstanding_margin_balance']}")
                else:
                    print(f"⚠ {url} returned empty/None")
            except Exception as e:
                print(f"✗ {url}: {e}")
    except Exception as e:
        print(f"❌ Direct margin balance test failed: {e}")
        import traceback
        traceback.print_exc()
    
    # Summary
    print("\n" + "=" * 70)
    print("Diagnosis Complete")
    print("=" * 70)
    print("\nIf margin balance is not appearing:")
    print("1. Check which endpoints returned data")
    print("2. Look for 'margin_balance', 'levered_amount', or negative 'cash' values")
    print("3. Enable debug logging: STACKWEALTH_DEBUG=1 streamlit run streamlit_app.py")
    print("4. Check logs in terminal for detailed responses")


if __name__ == "__main__":
    diagnose_margin_sync()
