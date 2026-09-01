"""Test XIRR calculation for BN position."""

import sys
from pathlib import Path
from datetime import date, datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from alphavault.finance_engine import Transaction, compute_xirr
from alphavault.models import Quote

# BN transaction data from user's export
transactions = [
    # (Date, Side, Shares, Price, Amount)
    ("2026-02-23", "BUY", 250, 43.905, -10976.25),
    ("2026-02-03", "BUY", 50, 44.75, -2237.5),
    ("2025-08-04", "BUY", 21, 66.4993, -1396.49),
    ("2025-07-11", "BUY", 50, 63.1082, -3155.41),
    ("2025-06-09", "BUY", 50, 57.9183, -2895.92),
    ("2025-05-06", "BUY", 60, 54.4396, -3266.38),
    ("2025-04-04", "BUY", 120, 48, -5760),
    ("2025-04-03", "BUY", 64, 50.2683, -3217.17),
    ("2025-04-03", "BUY", 35, 50.2683, -1759.39),
    ("2025-04-03", "BUY", 0.4663, 50.2683, -23.44),
    ("2025-03-26", "BUY", 50, 54.035, -2701.75),
    ("2025-03-21", "BUY", 5, 53.37, -266.85),
    ("2025-03-21", "BUY", 45, 53.37, -2401.65),
    ("2025-03-14", "SELL", 10, 50.1, 501),
    ("2025-01-15", "BUY", 10, 56.9361, -569.36),
    ("2024-10-17", "SELL", 10, 55.13, 551.3),
    ("2024-09-24", "BUY", 10, 52.8198, -528.2),
]

# Convert to Transaction objects
tx_objects = []
for date_str, side, shares, price, amount in transactions:
    tx_objects.append(
        Transaction(
            ticker="BN",
            tx_date=date.fromisoformat(date_str),
            amount=amount,
            side=side,
            shares=shares,
            price=price,
            currency="USD",
        )
    )

# Current position
current_shares = 1150.6990
current_value = 45596.45
terminal_value = current_value

print("\n" + "=" * 70)
print("BN Position XIRR Calculation Test")
print("=" * 70)

print(f"\nCurrent Position:")
print(f"  Shares: {current_shares:,.4f}")
print(f"  Current Value: ${current_value:,.2f}")
print(f"  Current Price: ${current_value/current_shares:,.4f}")

print(f"\nTransactions: {len(tx_objects)} total")
total_buys = sum(t.amount for t in tx_objects if t.side and t.side.lower() == "buy")
total_sells = sum(t.amount for t in tx_objects if t.side and t.side.lower() == "sell")
print(f"  Total Bought: ${abs(total_buys):,.2f}")
print(f"  Total Sold: ${total_sells:,.2f}")
print(f"  Net Invested: ${abs(total_buys) - total_sells:,.2f}")

# Calculate XIRR
xirr = compute_xirr(tx_objects, terminal_value)

print(f"\nXIRR Calculation:")
print(f"  Terminal Value: ${terminal_value:,.2f}")
print(f"  XIRR Result: {xirr*100:.2f}%" if xirr else "  XIRR: N/A")

# Sanity check
total_invested = abs(total_buys) - total_sells
simple_return = (terminal_value - total_invested) / total_invested
print(f"\nSanity Check:")
print(f"  Total Invested (Net): ${total_invested:,.2f}")
print(f"  Terminal Value: ${terminal_value:,.2f}")
print(f"  Simple Return: {simple_return*100:.2f}%")
print(f"  P&L: ${terminal_value - total_invested:,.2f}")

# First to last date
first_date = min(tx.tx_date for tx in tx_objects)
last_date = max(tx.tx_date for tx in tx_objects)
days_held = (date.today() - first_date).days
print(f"\nHolding Period:")
print(f"  From: {first_date}")
print(f"  To: {date.today()}")
print(f"  Days: {days_held}")

print("\n" + "=" * 70)

# Transaction details
print("\nTransaction History:")
print(f"{'Date':<12} {'Side':<6} {'Shares':<12} {'Price':<10} {'Amount':<12}")
print("-" * 60)
for tx in sorted(tx_objects, key=lambda x: x.tx_date):
    print(f"{tx.tx_date} {tx.side:<6} {tx.shares:>10.4f} ${tx.price:>8.2f} ${tx.amount:>10.2f}")

print("\n" + "=" * 70)
print("Note: XIRR uses actual transaction dates and values")
print("A negative XIRR indicates the investment has underperformed")
print("compared to the time-weighted cost of capital.")
print("=" * 70 + "\n")
