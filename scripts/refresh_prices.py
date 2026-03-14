"""One-shot script: refresh copy_price from actual fill prices and recalculate PnL.

Usage (from project root):
    python -m scripts.refresh_prices

What it does:
1. For every BUY CopyTrade (status=success), match against our funder wallet's
   trade activity from the Data API by token_id + execution timestamp to find
   the actual fill price (not the slippage-adjusted order price).
2. For every SELL CopyTrade (status=success or dry_run), recompute realized PnL
   using the weighted-average corrected BUY price for that token.
"""

from __future__ import annotations

import sys
import os

# Allow running from project root: python -m scripts.refresh_prices
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
from collections import defaultdict
from sqlalchemy import func

from db.database import get_session_factory, init_db
from db.models import CopyTrade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _fetch_activity(wallet_address: str, limit: int = 500) -> list[dict]:
    """Fetch trade activity for a wallet from the Polymarket Data API."""
    import requests
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": wallet_address, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return [d for d in data if d.get("type", "").upper() == "TRADE"]
        return data.get("data", [])
    except Exception as exc:
        logger.error("Failed to fetch activity for %s: %s", wallet_address, exc)
        return []


def _get_avg_buy_price(session, trader_id: int, token_id: str) -> float:
    """Weighted-average buy copy_price for a trader+token (all successful BUY trades)."""
    result = (
        session.query(
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
    if total_size and total_size > 0:
        return total_cost / total_size
    return 0.0


def refresh_buy_prices(session, funder_address: str) -> int:
    """Update copy_price for all BUY trades using actual fill prices from wallet activity.

    Matches each copy trade to a trade event from our funder wallet's Data API
    activity by token_id + execution timestamp (within a 10-minute window).

    Returns the number of records updated.
    """
    buys = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status == "success",
        )
        .all()
    )
    logger.info("Found %d BUY trades to check.", len(buys))

    if not buys:
        return 0

    # Fetch our wallet's actual trade activity
    activity = _fetch_activity(funder_address, limit=500)
    logger.info("Fetched %d activity entries for funder wallet.", len(activity))

    # Build lookup: token_id → list of (unix_ts, actual_price) for BUY side
    activity_map: dict[str, list] = defaultdict(list)
    for raw in activity:
        if (raw.get("side") or "").upper() != "BUY":
            continue
        tid = raw.get("asset") or raw.get("asset_id") or ""
        price = float(raw.get("price", 0) or 0)
        ts = float(raw.get("timestamp", 0) or 0)
        if tid and price > 0 and ts > 0:
            activity_map[tid].append((ts, price))

    updated = 0
    for ct in buys:
        tid = ct.original_token_id
        if not tid or tid not in activity_map:
            continue

        ct_ts = ct.executed_at.timestamp() if ct.executed_at else 0.0

        # Find closest activity entry by time (within 10 minutes)
        best_price, best_diff = None, float("inf")
        for ts, price in activity_map[tid]:
            diff = abs(ts - ct_ts)
            if diff < best_diff:
                best_diff = diff
                best_price = price

        if best_price is None or best_diff >= 600:
            continue

        current = ct.copy_price or 0.0
        if abs(current - best_price) > 1e-6:
            logger.info(
                "BUY id=%d token=...%s: copy_price %.6f → %.6f (diff %.6f, time_gap=%.0fs)",
                ct.id, tid[-8:], current, best_price, best_price - current, best_diff,
            )
            ct.copy_price = best_price
            updated += 1

    if updated:
        session.commit()
    logger.info("Updated copy_price on %d BUY trade(s).", updated)
    return updated


def recalculate_sell_pnl(session) -> int:
    """Recompute realized PnL for all SELL trades based on current BUY copy_prices.

    Returns the number of records updated.
    """
    sells = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(["success", "dry_run"]),
            CopyTrade.copy_size > 0,
        )
        .all()
    )
    logger.info("Found %d SELL trades to recalculate PnL for.", len(sells))

    updated = 0
    for ct in sells:
        avg_buy = _get_avg_buy_price(session, ct.trader_id, ct.original_token_id)
        if avg_buy <= 0:
            continue
        sell_price = ct.copy_price or 0.0
        new_pnl = round((sell_price - avg_buy) * ct.copy_size, 4)
        old_pnl = ct.pnl or 0.0
        if abs(new_pnl - old_pnl) > 1e-6:
            logger.info(
                "SELL id=%d token=...%s: pnl %.4f → %.4f (avg_buy=%.4f, sell=%.4f, size=%.4f)",
                ct.id, ct.original_token_id[-8:], old_pnl, new_pnl, avg_buy, sell_price, ct.copy_size,
            )
            ct.pnl = new_pnl
            updated += 1

    if updated:
        session.commit()
    logger.info("Updated PnL on %d SELL trade(s).", updated)
    return updated


def main() -> None:
    from config import settings

    logger.info("=== refresh_prices: starting ===")
    init_db()
    session_factory = get_session_factory()

    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        logger.error("POLYMARKET_FUNDER_ADDRESS is not set — cannot fetch activity.")
        sys.exit(1)

    with session_factory() as session:
        buy_updated = refresh_buy_prices(session, funder)
        sell_updated = recalculate_sell_pnl(session)

    logger.info(
        "=== Done: %d BUY price(s) corrected, %d SELL PnL(s) recalculated ===",
        buy_updated, sell_updated,
    )


if __name__ == "__main__":
    main()
