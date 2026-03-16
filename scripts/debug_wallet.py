"""Check if missing tokens are still in wallet (explains why detect_expired_losses skips them)."""
import sys
sys.path.insert(0, "/app")

from config import settings
from bot.tracker import fetch_positions

funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
positions = fetch_positions(funder)

missing = [
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

wallet_tids = {}
for p in positions:
    tid = p.get("asset_id", "")
    if tid:
        wallet_tids[tid] = p

print("Wallet positions:", len(positions))
print()

for tid in missing:
    if tid in wallet_tids:
        p = wallet_tids[tid]
        sz = str(p.get("size", 0))
        pr = str(p.get("cur_price", 0))
        vl = str(p.get("current_value", 0))
        print("YES " + tid[:16] + " sz=" + sz + " pr=" + pr + " val=" + vl)
    else:
        print("NO  " + tid[:16])
