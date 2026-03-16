"""Remove duplicate auto_sell SELL records that were never actually filled.

For each (trader_id, token_id) with multiple auto_sell SELL records,
keep only the first one (if wallet confirms the position was sold)
or delete all of them (if wallet still holds the position).
"""
import sys
sys.path.insert(0, "/app")

from db.database import get_session_factory, init_db
from db.models import CopyTrade
from bot.tracker import fetch_positions
from config import settings

init_db()
session = get_session_factory()()

# Fetch current wallet positions to know what's actually sold
funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
wallet_sizes = {}
if funder:
    try:
        positions = fetch_positions(funder)
        for p in positions:
            tid = p.get("asset_id", "")
            if tid:
                wallet_sizes[tid] = p.get("size", 0.0)
    except Exception as e:
        print("WARNING: Could not fetch wallet positions:", e)
        print("Proceeding with DB-only cleanup (removing duplicates, keeping first)")

# Find all auto_sell SELL records
auto_sells = (
    session.query(CopyTrade)
    .filter(
        CopyTrade.original_side == "SELL",
        CopyTrade.status == "success",
        CopyTrade.original_trade_id.like("auto_sell:%"),
    )
    .order_by(CopyTrade.executed_at)
    .all()
)

# Group by (trader_id, token_id)
groups = {}
for t in auto_sells:
    key = (t.trader_id, t.original_token_id)
    groups.setdefault(key, []).append(t)

total_deleted = 0
total_pnl_removed = 0.0

for (trader_id, token_id), records in groups.items():
    if len(records) <= 1:
        continue

    wallet_size = wallet_sizes.get(token_id, -1)  # -1 = unknown

    if wallet_size > 0:
        # Wallet still has shares — none of these sells actually filled
        # Delete ALL auto_sell records for this token
        print(
            "Token " + token_id[:16] + ": wallet still has "
            + str(wallet_size) + " shares. Deleting ALL "
            + str(len(records)) + " fake auto_sell records."
        )
        for r in records:
            total_pnl_removed += (r.pnl or 0)
            session.delete(r)
            total_deleted += 1
    else:
        # Wallet is 0 or unknown — keep first record, delete rest
        print(
            "Token " + token_id[:16] + ": keeping 1st record, deleting "
            + str(len(records) - 1) + " duplicates."
        )
        for r in records[1:]:
            total_pnl_removed += (r.pnl or 0)
            session.delete(r)
            total_deleted += 1

if total_deleted > 0:
    session.commit()

print("\n=== Summary ===")
print("Deleted:", total_deleted, "duplicate/fake SELL records")
print("PnL removed:", round(total_pnl_removed, 2))

# Recalculate totals
remaining = session.query(CopyTrade).filter(
    CopyTrade.original_side == "SELL",
    CopyTrade.status == "success",
).all()
new_pnl = sum(t.pnl or 0 for t in remaining)
print("New total SELL pnl:", round(new_pnl, 2))
print("Remaining SELL records:", len(remaining))

session.close()
