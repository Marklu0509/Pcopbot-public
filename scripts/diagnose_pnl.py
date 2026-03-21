"""Diagnostic script for PnL issues.

Run on server:
    docker exec pcopbot-bot python3 scripts/diagnose_pnl.py
"""

import sys
sys.path.insert(0, "/app")

from config import settings
from db.database import get_session_factory, init_db
from db.models import CopyTrade, Trader, BotSetting
from sqlalchemy import func

init_db()
Session = get_session_factory()

print("=" * 60)
print("PnL DIAGNOSTIC REPORT")
print("=" * 60)

# 1. Check critical env vars
print("\n--- Configuration ---")
funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
print(f"FUNDER_ADDRESS:    {'SET (' + funder[:16] + '...)' if funder else 'NOT SET ❌'}")
print(f"PRIVATE_KEY:       {'SET' if settings.POLYMARKET_PRIVATE_KEY else 'NOT SET ❌'}")
print(f"FUNDER_PRIV_KEY:   {'SET' if settings.POLYMARKET_FUNDER_PRIVATE_KEY else 'NOT SET'}")
print(f"RELAYER_API_KEY:   {'SET' if settings.POLYMARKET_RELAYER_API_KEY else 'NOT SET'}")
print(f"AUTO_SELL_THRESHOLD: {settings.AUTO_SELL_THRESHOLD}")
print(f"DRY_RUN (global):  {settings.DRY_RUN}")

if not funder:
    print("\n⚠️  POLYMARKET_FUNDER_ADDRESS is NOT SET!")
    print("   → detect_manual_redemptions() and detect_manual_sells() will NOT work.")
    print("   → Auto-sell wallet position data unavailable.")

if not settings.POLYMARKET_PRIVATE_KEY:
    print("\n⚠️  POLYMARKET_PRIVATE_KEY is NOT SET!")
    print("   → Auto-redeem (on-chain) will NOT work.")

if not settings.POLYMARKET_RELAYER_API_KEY and not settings.POLYMARKET_FUNDER_PRIVATE_KEY:
    print("\n⚠️  Neither RELAYER_API_KEY nor FUNDER_PRIVATE_KEY is set!")
    print("   → On-chain redemption requires one of these for proxy-wallet setups.")

with Session() as session:
    # 2. Check auto_sell_enabled setting
    print("\n--- Bot Settings ---")
    row = session.query(BotSetting).filter(BotSetting.key == "auto_sell_enabled").first()
    if row:
        print(f"auto_sell_enabled: {row.value}")
        if row.value.lower() in ("false", "0", "no"):
            print("   ⚠️  Auto-sell is DISABLED!")
    else:
        print("auto_sell_enabled: not set (defaults to True)")

    # 3. Count records by type
    print("\n--- Trade Records ---")
    total = session.query(func.count(CopyTrade.id)).scalar()
    buys = session.query(func.count(CopyTrade.id)).filter(CopyTrade.original_side == "BUY").scalar()
    sells = session.query(func.count(CopyTrade.id)).filter(CopyTrade.original_side == "SELL").scalar()
    print(f"Total records: {total}")
    print(f"BUY records:   {buys}")
    print(f"SELL records:  {sells}")

    if sells == 0:
        print("   ⚠️  NO SELL records exist! Realized PnL will be $0.")

    # 4. SELL records breakdown
    if sells > 0:
        print("\n--- SELL Record Sources ---")
        auto_sell = session.query(func.count(CopyTrade.id)).filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.original_trade_id.like("auto_sell%"),
        ).scalar()
        redemption = session.query(func.count(CopyTrade.id)).filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.original_trade_id.like("redemption%"),
        ).scalar()
        sim_redeem = session.query(func.count(CopyTrade.id)).filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.original_trade_id.like("sim_redemption%"),
        ).scalar()
        manual_redeem = session.query(func.count(CopyTrade.id)).filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.order_id.like("manual_redeem%"),
        ).scalar()
        manual_sell = session.query(func.count(CopyTrade.id)).filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.order_id.like("manual_sell%"),
        ).scalar()
        expired = session.query(func.count(CopyTrade.id)).filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.order_id.like("expired_loss%"),
        ).scalar()
        mark_sold = session.query(func.count(CopyTrade.id)).filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.order_id.like("mark_sold%"),
        ).scalar()
        copied_sell = session.query(func.count(CopyTrade.id)).filter(
            CopyTrade.original_side == "SELL",
            ~CopyTrade.original_trade_id.like("auto_sell%"),
            ~CopyTrade.original_trade_id.like("redemption%"),
            ~CopyTrade.original_trade_id.like("sim_redemption%"),
            ~CopyTrade.original_trade_id.like("redeem%"),
            ~CopyTrade.original_trade_id.like("expired%"),
        ).scalar()
        print(f"  Auto-sell:         {auto_sell}")
        print(f"  On-chain redeem:   {redemption}")
        print(f"  Simulated redeem:  {sim_redeem}")
        print(f"  Manual redemption: {manual_redeem}")
        print(f"  Manual sell:       {manual_sell}")
        print(f"  Expired loss:      {expired}")
        print(f"  Mark as sold:      {mark_sold}")
        print(f"  Copied sell:       {copied_sell}")

    # 5. PnL summary
    print("\n--- PnL Summary ---")
    sell_pnl = session.query(func.coalesce(func.sum(CopyTrade.pnl), 0.0)).filter(
        CopyTrade.original_side == "SELL",
        CopyTrade.status.in_(["success", "dry_run"]),
    ).scalar()
    buy_pnl = session.query(func.coalesce(func.sum(CopyTrade.pnl), 0.0)).filter(
        CopyTrade.original_side == "BUY",
        CopyTrade.status.in_(["success", "dry_run"]),
    ).scalar()
    print(f"Realized PnL (SELL sum):   ${sell_pnl:+,.4f}")
    print(f"Unrealized PnL (BUY sum):  ${buy_pnl:+,.4f}")

    # 6. Open positions that should trigger auto-sell or auto-redeem
    print("\n--- Open Positions (net > 0) ---")
    from bot.executor import _get_net_holdings
    open_buys = (
        session.query(
            CopyTrade.trader_id,
            CopyTrade.original_token_id,
            CopyTrade.market_title,
        )
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
            CopyTrade.original_token_id.is_not(None),
        )
        .group_by(CopyTrade.trader_id, CopyTrade.original_token_id, CopyTrade.market_title)
        .all()
    )
    shown = 0
    for trader_id, token_id, title in open_buys:
        net = _get_net_holdings(session, trader_id, token_id)
        if net > 0:
            shown += 1
            if shown <= 20:
                print(f"  trader={trader_id} net={net:.4f} token={token_id[:16]}... {title}")
    if shown > 20:
        print(f"  ... and {shown - 20} more")
    if shown == 0:
        print("  No open positions found.")

    # 7. Check Data API connectivity
    print("\n--- Data API Check ---")
    if funder:
        import requests
        try:
            resp = requests.get(
                f"{settings.DATA_API_BASE}/activity",
                params={"user": funder, "limit": 5},
                timeout=15,
            )
            print(f"Status: {resp.status_code}")
            data = resp.json()
            if isinstance(data, list):
                print(f"Returned {len(data)} activity items")
                types = set(d.get("type", "?") for d in data)
                print(f"Activity types: {types}")
                for d in data[:2]:
                    print(f"  type={d.get('type')} side={d.get('side')} title={str(d.get('title', ''))[:40]}")
            else:
                print(f"Response format: {type(data).__name__}")
        except Exception as exc:
            print(f"❌ Failed: {exc}")
    else:
        print("Skipped (no FUNDER_ADDRESS)")

print("\n" + "=" * 60)
print("END OF DIAGNOSTIC REPORT")
print("=" * 60)
