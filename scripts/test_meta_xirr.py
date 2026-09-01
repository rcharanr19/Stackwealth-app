"""Debug XIRR calculation for META and NOW positions."""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from alphavault.sqlite_store import SQLiteStore
from alphavault.finance_engine import compute_xirr
import logging

logging.basicConfig(level=logging.DEBUG, format='%(name)s - %(levelname)s - %(message)s')
LOGGER = logging.getLogger(__name__)

# Connect to SQLite database
db = SQLiteStore(project_root / "cache" / "portfolio.db")
positions, transactions = db.load_portfolio_state()

# Find META and NOW positions
meta_pos = next((p for p in positions if p.ticker == "META"), None)
now_pos = next((p for p in positions if p.ticker == "NOW"), None)

# Get transactions for each
meta_txs = [tx for tx in transactions if tx.ticker == "META"]
now_txs = [tx for tx in transactions if tx.ticker == "NOW"]

print("=" * 70)
print("META XIRR Debug")
print("=" * 70)
if meta_pos:
    print(f"Position: {meta_pos.ticker} - {meta_pos.company_name}")
    print(f"  Shares: {meta_pos.shares}")
    print(f"  Avg Price: {meta_pos.avg_price}")
    print(f"  Expected Value: {meta_pos.shares * meta_pos.avg_price}")
    print(f"\nTransactions: {len(meta_txs)} total")
    total_bought = 0
    total_sold = 0
    for tx in sorted(meta_txs, key=lambda t: t.tx_date):
        print(f"  {tx.tx_date} {str(tx.side or '').upper():5} {tx.shares:10.4f} @ ${tx.price:10.2f} amount=${tx.amount:12.2f}")
        if tx.amount < 0:
            total_bought += abs(tx.amount)
        else:
            total_sold += tx.amount
    
    print(f"\nCash Flow Summary:")
    print(f"  Total Bought (invested): ${total_bought:.2f}")
    print(f"  Total Sold (returned): ${total_sold:.2f}")
    print(f"  Net Flows: ${total_sold - total_bought:.2f}")
    
    # Compute XIRR
    terminal_value = meta_pos.shares * meta_pos.avg_price
    print(f"\nXIRR Calculation:")
    print(f"  Terminal Value: ${terminal_value:.2f}")
    xirr = compute_xirr(meta_txs, terminal_value)
    print(f"  XIRR Result: {xirr}")
    if xirr:
        print(f"  XIRR %: {xirr * 100:.2f}%")
else:
    print("META not found in portfolio")

print("\n" + "=" * 70)
print("NOW XIRR Debug")
print("=" * 70)
if now_pos:
    print(f"Position: {now_pos.ticker} - {now_pos.company_name}")
    print(f"  Shares: {now_pos.shares}")
    print(f"  Avg Price: {now_pos.avg_price}")
    print(f"  Expected Value: {now_pos.shares * now_pos.avg_price}")
    print(f"\nTransactions: {len(now_txs)} total")
    total_bought = 0
    total_sold = 0
    for tx in sorted(now_txs, key=lambda t: t.tx_date):
        print(f"  {tx.tx_date} {str(tx.side or '').upper():5} {tx.shares:10.4f} @ ${tx.price:10.2f} amount=${tx.amount:12.2f}")
        if tx.amount < 0:
            total_bought += abs(tx.amount)
        else:
            total_sold += tx.amount
    
    print(f"\nCash Flow Summary:")
    print(f"  Total Bought (invested): ${total_bought:.2f}")
    print(f"  Total Sold (returned): ${total_sold:.2f}")
    print(f"  Net Flows: ${total_sold - total_bought:.2f}")
    
    # Compute XIRR
    terminal_value = now_pos.shares * now_pos.avg_price
    print(f"\nXIRR Calculation:")
    print(f"  Terminal Value: ${terminal_value:.2f}")
    xirr = compute_xirr(now_txs, terminal_value)
    print(f"  XIRR Result: {xirr}")
    if xirr:
        print(f"  XIRR %: {xirr * 100:.2f}%")
else:
    print("NOW not found in portfolio")
