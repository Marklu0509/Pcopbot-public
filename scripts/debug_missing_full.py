"""Debug: show exactly why each 'missing' position is not getting a SELL record."""
import sys
sys.path.insert(0, "/app")

from collections import defaultdict
from sqlalchemy import func
from db.database import get_session_factory, init_db
from db.models import CopyTrade
from config import settings
from bot.tracker import fetch_positions

init_db()
s = get_session_factory()()

buys = s.query(CopyTrade).filter(
    CopyTrade.original_side == "BUY",
    CopyTrade.status.in_(["success", "dry_run"]),
).all()
sells = s.query(CopyTrade).filter(
    CopyTrade.original_side == "SELL",
    CopyTrade.status.in_(["success", "dry_run"]),
).all()

token_buys = defaultdict(list)
for ct in buys:
    if ct.original_token_id:
        token_buys[ct.original_token_id].append(ct)

sell_tids = set()
for ct in sells:
    if ct.original_token_id:
        sell_tids.add(ct.original_token_id)

funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
wallet_map = {}
if funder:
    for p in fetch_positions(funder):
        tid = p.get("asset_id", "")
        if tid:
            wallet_map[tid] = p

print("Missing positions (BUY with no SELL):")
print()
count = 0
for tid, bl in token_buys.items():
    if tid in sell_tids:
        continue
    buy_size = sum(ct.copy_size or 0 for ct in bl)
    cost = sum((ct.copy_size or 0) * (ct.copy_price or 0) for ct in bl)
    mkt = (bl[0].market_title or "")[:45]
    in_wallet = tid in wallet_map
    w_info = ""
    if in_wallet:
        wp = wallet_map[tid]
        w_info = " sz=" + str(wp.get("size", 0))
        w_info += " pr=" + str(wp.get("cur_price", 0))
    reason = ""
    if in_wallet:
        reason = "IN WALLET" + w_info
    elif buy_size < 0.1:
        reason = "NET TOO SMALL"
    else:
        reason = "SHOULD BE FIXED"
    count += 1
    print(str(count) + ". " + mkt)
    print("   cost=$" + str(round(cost, 2)) + " net=" + str(round(buy_size, 4)))
    print("   reason: " + reason)
    print("   tid=" + tid[:40] + "...")
    print()

s.close()
