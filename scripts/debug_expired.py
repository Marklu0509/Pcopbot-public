"""Step-by-step debug of detect_expired_losses logic."""
import sys
sys.path.insert(0, "/app")

from db.database import get_session_factory, init_db
from db.models import CopyTrade
from bot.executor import _get_net_holdings
from bot.tracker import fetch_positions
from config import settings

init_db()
s = get_session_factory()()

# Step 1: collect all BUY tokens
open_buys = (
    s.query(CopyTrade)
    .filter(
        CopyTrade.original_side == "BUY",
        CopyTrade.status.in_(["success", "dry_run"]),
    )
    .all()
)
token_buys = {}
for ct in open_buys:
    if ct.original_token_id:
        token_buys.setdefault(ct.original_token_id, []).append(ct)

print("Step 1: total BUY tokens:", len(token_buys))

# Step 2: fetch wallet
funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
wallet_token_ids = set()
if funder:
    for p in fetch_positions(funder):
        tid = p.get("asset_id", "")
        if tid and p.get("size", 0) > 0:
            wallet_token_ids.add(tid)

print("Step 2: wallet tokens:", len(wallet_token_ids))

# Step 3: check each missing token
missing_tids = [
    "1021184125280091",
    "3751037991664819",
    "6905538726988882",
    "2630386975290558",
    "7088414181319328",
    "6050711940524463",
    "8325368344381247",
    "2993851418497041",
    "1087985026104979",
]

print()
print("Step 3: checking missing tokens")
for tid in missing_tids:
    in_token_buys = tid in token_buys
    in_wallet = tid in wallet_token_ids
    if in_token_buys:
        buys = token_buys[tid]
        trader_id = buys[0].trader_id
        net = _get_net_holdings(s, trader_id, tid)
        mkt = (buys[0].market_title or "")[:40]
        line = "tk=" + tid[:16]
        line += " in_buys=YES"
        line += " in_wallet=" + str(in_wallet)
        line += " net=" + str(round(net, 4))
        line += " skip=" + str(net < 0.1)
        line += " " + mkt
        print("  " + line)
    else:
        print("  tk=" + tid[:16] + " in_buys=NO (not in token_buys!)")

# Step 4: check if any existing expired_loss records block these
print()
print("Step 4: existing expired_loss records for these tokens")
for tid in missing_tids:
    existing = (
        s.query(CopyTrade)
        .filter(
            CopyTrade.original_token_id == tid,
            CopyTrade.order_id.like("expired_loss:%"),
        )
        .all()
    )
    if existing:
        for e in existing:
            print("  FOUND tk=" + tid[:16] + " oid=" + (e.order_id or ""))
    else:
        print("  NONE  tk=" + tid[:16])

s.close()
