"""Simplified diagnostic to test margin extraction logic."""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from alphavault.robinhood_sync import RobinhoodSyncService


def test_margin_extraction():
    """Test the margin extraction logic with sample data."""
    
    print("\n" + "=" * 70)
    print("StackWealth Margin Extraction Diagnostic")
    print("=" * 70)
    
    service = RobinhoodSyncService
    
    # Test case 1: Portfolio with negative excess margin
    print("\n1. Testing Portfolio Profile with negative excess_margin:")
    portfolio_1 = {
        "excess_margin": "-1234.56",
        "excess_maintenance": "5000.00",
    }
    account_1 = None
    phoenix_1 = None
    margin_1 = None
    
    result_1 = service._extract_margin_balance_usd(account_1, portfolio_1, phoenix_1, margin_1)
    print(f"   Input: excess_margin = {portfolio_1['excess_margin']}")
    print(f"   Result: {result_1}")
    print(f"   Expected: 1234.56")
    assert result_1 == 1234.56, f"Expected 1234.56 but got {result_1}"
    print("   ✓ PASS")
    
    # Test case 2: Account profile with margin_balances
    print("\n2. Testing Account Profile with margin_balances:")
    account_2 = {
        "margin_balances": {
            "outstanding_margin_balance": "2500.00",
        }
    }
    portfolio_2 = None
    phoenix_2 = None
    margin_2 = None
    
    result_2 = service._extract_margin_balance_usd(account_2, portfolio_2, phoenix_2, margin_2)
    print(f"   Input: margin_balances.outstanding_margin_balance = {account_2['margin_balances']['outstanding_margin_balance']}")
    print(f"   Result: {result_2}")
    print(f"   Expected: 2500.0")
    assert result_2 == 2500.0, f"Expected 2500.0 but got {result_2}"
    print("   ✓ PASS")
    
    # Test case 3: Phoenix account with levered_amount
    print("\n3. Testing Phoenix Account with levered_amount:")
    account_3 = None
    portfolio_3 = None
    phoenix_3 = [
        {
            "levered_amount": "3750.25",
        }
    ]
    margin_3 = None
    
    result_3 = service._extract_margin_balance_usd(account_3, portfolio_3, phoenix_3, margin_3)
    print(f"   Input: phoenix[0].levered_amount = {phoenix_3[0]['levered_amount']}")
    print(f"   Result: {result_3}")
    print(f"   Expected: 3750.25")
    assert result_3 == 3750.25, f"Expected 3750.25 but got {result_3}"
    print("   ✓ PASS")
    
    # Test case 4: Account with negative cash
    print("\n4. Testing Account Profile with negative cash:")
    account_4 = {
        "cash": "-1500.00",
    }
    portfolio_4 = None
    phoenix_4 = None
    margin_4 = None
    
    result_4 = service._extract_margin_balance_usd(account_4, portfolio_4, phoenix_4, margin_4)
    print(f"   Input: cash = {account_4['cash']}")
    print(f"   Result: {result_4}")
    print(f"   Expected: 1500.0")
    assert result_4 == 1500.0, f"Expected 1500.0 but got {result_4}"
    print("   ✓ PASS")
    
    # Test case 5: No margin data
    print("\n5. Testing with no margin data:")
    account_5 = {"account_number": "123"}
    portfolio_5 = None
    phoenix_5 = None
    margin_5 = None
    
    result_5 = service._extract_margin_balance_usd(account_5, portfolio_5, phoenix_5, margin_5)
    print(f"   Input: account only (no margin fields)")
    print(f"   Result: {result_5}")
    print(f"   Expected: None")
    assert result_5 is None, f"Expected None but got {result_5}"
    print("   ✓ PASS")
    
    # Test case 6: Cash-only account (zero margin)
    print("\n6. Testing cash-only account (zero margin):")
    account_6 = {
        "cash": "10000.00",
        "margin_balances": {
            "margin_balance": "0.00",
        }
    }
    portfolio_6 = None
    phoenix_6 = None
    margin_6 = None
    
    result_6 = service._extract_margin_balance_usd(account_6, portfolio_6, phoenix_6, margin_6)
    print(f"   Input: margin_balance = 0.00, cash = 10000.00")
    print(f"   Result: {result_6}")
    print(f"   Expected: 0.0")
    assert result_6 == 0.0, f"Expected 0.0 but got {result_6}"
    print("   ✓ PASS")
    
    print("\n" + "=" * 70)
    print("All extraction tests PASSED! ✓")
    print("=" * 70)
    
    print("""
Next steps to debug margin sync:

1. Enable debug logging when running the app:
   Windows:  set STACKWEALTH_DEBUG=1
   Then:     streamlit run streamlit_app.py

2. In the app sidebar:
   - Look for "🔍 Margin Diagnostics" expander
   - Check if margin balance is showing $0 or "Not synced"

3. Check terminal output for these log lines:
   - "Robinhood margin response keys:" - shows what data was received
   - "Updated Robinhood margin balance:" - shows successful extraction
   - "Robinhood margin balance was unavailable:" - no margin data found

4. Common causes:
   - Account is cash-only (no margin used) → Result: $0.00
   - Robinhood API endpoints changed → No data returned
   - robin_stocks library outdated → pip install --upgrade robin-stocks
   - First sync not completed → Run sync and retry

5. If margin is still $0 after sync:
   - Account may truly have no margin debt
   - Robinhood API may have changed field names
   - Check Robinhood web app to verify margin status
    """)


if __name__ == "__main__":
    test_margin_extraction()
