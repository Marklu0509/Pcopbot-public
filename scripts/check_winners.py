"""Show all wallet positions with price >= 0.95 (potential winners)."""
from bot.tracker import fetch_positions
from config import settings

positions = fetch_positions(settings.POLYMARKET_FUNDER_ADDRESS)
found = 0
for p in positions:
    price = p.get("cur_price", 0)
    if price >= 0.95:
        found += 1
        print(
            f"token={p['asset_id'][:16]}  size={p['size']}  "
            f"price={price}  title={p.get('market_title', '')[:60]}"
        )

if not found:
    print("No positions with price >= 0.95 found.")
print(f"\nTotal positions in wallet: {len(positions)}")
