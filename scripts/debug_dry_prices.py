"""Debug script: check why dry_run holdings show cur_price = 0."""

import requests

from bot.tracker import fetch_prices_by_token_ids
from db.database import get_session_factory, init_db
from db.models import CopyTrade

init_db()
session = get_session_factory()()

# 1. Get all dry_run BUY token IDs
tids = [
    r[0]
    for r in session.query(CopyTrade.original_token_id)
    .filter(
        CopyTrade.status == "dry_run",
        CopyTrade.original_side == "BUY",
        CopyTrade.original_token_id.isnot(None),
    )
    .distinct()
    .all()
]
print(f"\n=== Dry-run BUY token IDs: {len(tids)} ===")
for t in tids:
    print(f"  {t}")

# 2. Try Gamma API
print(f"\n=== Gamma API (fetch_prices_by_token_ids) ===")
gamma_prices = fetch_prices_by_token_ids(tids)
print(f"Returned: {len(gamma_prices)} prices")
for tid, p in gamma_prices.items():
    print(f"  {tid[:24]}... = {p}")
gamma_missing = [t for t in tids if gamma_prices.get(t, 0.0) == 0.0]
print(f"Missing/zero: {len(gamma_missing)} of {len(tids)}")

# 3. Try CLOB book API for missing ones
print(f"\n=== CLOB Book API fallback ===")
for tid in gamma_missing[:10]:
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": tid},
            timeout=5,
        )
        if not resp.ok:
            print(f"  {tid[:24]}... HTTP {resp.status_code}")
            continue
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        mid = round((best_bid + best_ask) / 2, 4) if best_bid and best_ask else best_bid or best_ask
        print(f"  {tid[:24]}... bid={best_bid} ask={best_ask} mid={mid}")
    except Exception as exc:
        print(f"  {tid[:24]}... ERROR: {exc}")

# 4. Try funder wallet positions
print(f"\n=== Funder Wallet Positions ===")
from config import settings

funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
if funder:
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder},
            timeout=10,
        )
        wallet_tids = set()
        if resp.ok:
            for pos in resp.json():
                asset = pos.get("asset", "")
                if asset:
                    wallet_tids.add(asset)
        print(f"Wallet holds {len(wallet_tids)} tokens")
        overlap = set(tids) & wallet_tids
        print(f"Overlap with dry_run tokens: {len(overlap)}")
        for t in overlap:
            print(f"  {t[:24]}...")
    except Exception as exc:
        print(f"Wallet fetch error: {exc}")
else:
    print("No POLYMARKET_FUNDER_ADDRESS set")

session.close()
print("\n=== Done ===")
