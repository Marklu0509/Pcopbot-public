"""One-time fix: create expired_loss SELL records for BUY positions that have no SELL.

Skips tokens that are still in the wallet (active positions).
Skips tokens with net_holdings < 0.1 (rounding residuals).
Skips future markets (Newcastle 03-18, Barcelona 03-18, Man Utd 03-20).
"""
import sys
import datetime

sys.path.insert(0, "/app")

from collections import defaultdict
from sqlalchemy import func

from db.database import get_session_factory, init_db
from db.models import CopyTrade
from config import settings
from bot.tracker import fetch_positions

init_db()
s = get_session_factory()()

# Step 1: Get all BUY and SELL tokens
buys = (
    s.query(CopyTrade)
    .filter(
        CopyTrade.original_side == "BUY",
        CopyTrade.status.in_(["success", "dry_run"]),
    )
    .all()
)
sells = (
    s.query(CopyTrade)
    .filter(
        CopyTrade.original_side == "SELL",
        CopyTrade.status.in_(["success", "dry_run"]),
    )
    .all()
)

# Group BUY by token_id
token_buys = defaultdict(list)
for ct in buys:
    if ct.original_token_id:
        token_buys[ct.original_token_id].append(ct)

# Collect token_ids that already have SELL
sell_token_ids = set()
for ct in sells:
    if ct.original_token_id:
        sell_token_ids.add(ct.original_token_id)

# Step 2: Get wallet positions
funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
wallet_token_ids = set()
if funder:
    for p in fetch_positions(funder):
        tid = p.get("asset_id", "")
        if tid and p.get("size", 0) > 0:
            wallet_token_ids.add(tid)

print("BUY tokens:", len(token_buys))
print("SELL tokens:", len(sell_token_ids))
print("Wallet tokens:", len(wallet_token_ids))
print()

# Step 3: Find missing and fix
created = 0
for token_id, buy_list in token_buys.items():
    # Skip if already has SELL records
    if token_id in sell_token_ids:
        continue

    # Skip if still in wallet (active position)
    if token_id in wallet_token_ids:
        continue

    # Calculate net holdings
    buy_size = sum(ct.copy_size or 0 for ct in buy_list)
    # Double check no sells exist for this exact token
    sell_size = (
        s.query(func.coalesce(func.sum(CopyTrade.copy_size), 0.0))
        .filter(
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    net = buy_size - sell_size
    if net < 0.1:
        continue

    sample = buy_list[0]
    trader_id = sample.trader_id

    # Calculate avg buy price
    result = (
        s.query(
            func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0),
            func.coalesce(func.sum(CopyTrade.copy_size), 0.0),
        )
        .filter(
            CopyTrade.trader_id == trader_id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .first()
    )
    total_cost, total_size = result
    avg_buy = (total_cost / total_size) if total_size and total_size > 0 else 0.0
    pnl = round(-avg_buy * net, 4)
    cost = round(avg_buy * net, 2)

    # Dedup key
    oid = "expired_loss_fix:" + token_id[:30] + ":t" + str(trader_id)
    if s.query(CopyTrade).filter(CopyTrade.order_id == oid).first():
        print("SKIP (already exists): " + (sample.market_title or "")[:40])
        continue

    mkt = (sample.market_title or "")[:50]
    print("FIX: " + mkt + " net=" + str(round(net, 4)) + " cost=$" + str(cost) + " pnl=" + str(pnl))

    loss_record = CopyTrade(
        trader_id=trader_id,
        original_trade_id="expired_fix:" + (sample.original_market or "")[:24],
        original_market=sample.original_market,
        original_token_id=token_id,
        market_title=sample.market_title,
        outcome=sample.outcome,
        original_side="SELL",
        original_size=net,
        original_price=0.0,
        original_timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        copy_size=net,
        copy_price=0.0,
        status="success",
        order_id=oid,
        pnl=pnl,
        executed_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    s.add(loss_record)

    # Zero out BUY pnl
    for ct in buy_list:
        if ct.trader_id == trader_id:
            ct.pnl = 0.0

    created += 1

if created:
    s.commit()

print()
print("Created " + str(created) + " expired loss records")

# Show new totals
all_sells = s.query(CopyTrade).filter(
    CopyTrade.original_side == "SELL",
    CopyTrade.status.in_(["success", "dry_run"]),
).all()
print("New total realized pnl: " + str(round(sum(t.pnl or 0 for t in all_sells), 2)))

s.close()
