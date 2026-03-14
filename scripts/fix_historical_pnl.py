"""One-shot script: fix all historical PnL data since account creation.

Corrects three classes of errors:
  1. BUY copy_price was recorded wrong → back-fill from Data API activity
  2. SELL copy_price was recorded wrong → same
  3. Manual Polymarket UI redemptions were never recorded → insert synthetic SELL records

Then recomputes all realized PnL with the corrected prices.

Usage (from project root):
    python -m scripts.fix_historical_pnl

Safe to run multiple times — all operations are idempotent.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from sqlalchemy import func

from db.database import get_session_factory, init_db
from db.models import CopyTrade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _snapshot(session) -> dict:
    """Capture current PnL state for before/after comparison."""
    realized = (
        session.query(func.coalesce(func.sum(CopyTrade.pnl), 0.0))
        .filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    unrealized = (
        session.query(func.coalesce(func.sum(CopyTrade.pnl), 0.0))
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    sell_count = (
        session.query(func.count(CopyTrade.id))
        .filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    buy_count = (
        session.query(func.count(CopyTrade.id))
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    return {
        "realized_pnl": float(realized or 0.0),
        "unrealized_pnl": float(unrealized or 0.0),
        "sell_records": int(sell_count or 0),
        "buy_records": int(buy_count or 0),
    }


def _print_summary(before: dict, after: dict, results: dict) -> None:
    print()
    print("=" * 60)
    print("  fix_historical_pnl — SUMMARY")
    print("=" * 60)
    print(f"  BUY  prices corrected : {results['buy_updated']:>6d}")
    print(f"  SELL prices corrected : {results['sell_updated']:>6d}")
    print(f"  Manual redemptions added : {results['redemptions_added']:>4d}")
    print(f"  PnL records recalculated : {results['pnl_updated']:>4d}")
    print("-" * 60)
    print(f"  {'Metric':<30}  {'Before':>9}  {'After':>9}  {'Delta':>9}")
    print(f"  {'-'*30}  {'-'*9}  {'-'*9}  {'-'*9}")

    def _row(label, key):
        b, a = before[key], after[key]
        d = a - b
        sign = "+" if d >= 0 else ""
        print(f"  {label:<30}  {b:>9.4f}  {a:>9.4f}  {sign}{d:>8.4f}")

    def _row_int(label, key):
        b, a = before[key], after[key]
        d = a - b
        sign = "+" if d >= 0 else ""
        print(f"  {label:<30}  {b:>9d}  {a:>9d}  {sign}{d:>8d}")

    _row("Realized PnL ($)", "realized_pnl")
    _row("Unrealized PnL ($)", "unrealized_pnl")
    _row_int("SELL records", "sell_records")
    _row_int("BUY records", "buy_records")
    print("=" * 60)
    print()


def main() -> None:
    from config import settings
    from scripts.refresh_prices import (
        _fetch_activity_all,
        _build_activity_map,
        _find_best_price,
        refresh_buy_prices,
        refresh_sell_prices,
        recalculate_sell_pnl,
    )
    from bot.redeemer import detect_manual_redemptions

    logger.info("=== fix_historical_pnl: starting ===")
    init_db()
    session_factory = get_session_factory()

    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        logger.error("POLYMARKET_FUNDER_ADDRESS is not set — cannot fetch activity.")
        sys.exit(1)

    logger.info("Fetching full wallet activity for %s ...", funder[:12])
    all_activity = _fetch_activity_all(funder, limit=500)
    logger.info("Fetched %d total activity entries.", len(all_activity))
    if len(all_activity) == 500:
        logger.warning(
            "Exactly 500 entries returned — there may be older records not fetched. "
            "This is unlikely given the 3-day history, but re-check if totals look off."
        )

    # Filter to TRADE-type only for price correction functions
    trade_activity = [d for d in all_activity if d.get("type", "").upper() == "TRADE"]
    logger.info("Of which %d are TRADE entries (used for price correction).", len(trade_activity))

    with session_factory() as session:
        before = _snapshot(session)
        logger.info(
            "Before: realized_pnl=%.4f unrealized_pnl=%.4f sell_records=%d buy_records=%d",
            before["realized_pnl"], before["unrealized_pnl"],
            before["sell_records"], before["buy_records"],
        )

        # Step 1 & 2: Fix BUY and SELL fill prices from Data API activity
        logger.info("--- Step 1: Correcting BUY copy_prices ---")
        buy_updated = refresh_buy_prices(session, funder, trade_activity)

        logger.info("--- Step 2: Correcting SELL copy_prices ---")
        sell_updated = refresh_sell_prices(session, funder, trade_activity)

        # Step 3: Insert missing manual redemption SELL records
        logger.info("--- Step 3: Detecting manual redemptions ---")
        redemptions_added = detect_manual_redemptions(session)

        # Step 4: Recompute all realized PnL with corrected prices
        logger.info("--- Step 4: Recalculating realized PnL ---")
        pnl_updated = recalculate_sell_pnl(session)

        after = _snapshot(session)
        logger.info(
            "After:  realized_pnl=%.4f unrealized_pnl=%.4f sell_records=%d buy_records=%d",
            after["realized_pnl"], after["unrealized_pnl"],
            after["sell_records"], after["buy_records"],
        )

    _print_summary(
        before, after,
        {
            "buy_updated": buy_updated,
            "sell_updated": sell_updated,
            "redemptions_added": redemptions_added,
            "pnl_updated": pnl_updated,
        },
    )
    logger.info("=== fix_historical_pnl: done ===")


if __name__ == "__main__":
    main()
