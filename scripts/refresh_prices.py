"""One-shot script: refresh copy_price from actual fill prices and recalculate PnL.

Usage (from project root):
    python -m scripts.refresh_prices

What it does:
1. Fetch our funder wallet's trade activity from the Data API.
2. For every BUY CopyTrade (status=success), match against activity by
   token_id + execution timestamp (±10 min) to get the actual fill price.
3. For every SELL CopyTrade (status=success), do the same for the sell price.
4. Recompute realized PnL for all SELL trades using the corrected buy/sell prices.
"""

from __future__ import annotations

import sys
import os

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


# ── Data API ──────────────────────────────────────────────────────────────────

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


def _build_activity_map(activity: list[dict], side: str) -> dict[str, list]:
    """Build {token_id: [(unix_ts, price), ...]} for trades of the given side."""
    result: dict[str, list] = defaultdict(list)
    for raw in activity:
        if (raw.get("side") or "").upper() != side:
            continue
        tid = raw.get("asset") or raw.get("asset_id") or ""
        price = float(raw.get("price", 0) or 0)
        ts = float(raw.get("timestamp", 0) or 0)
        if tid and price > 0 and ts > 0:
            result[tid].append((ts, price))
    return result


def _find_best_price(activity_map: dict, token_id: str, executed_ts: float, window: int = 600) -> float | None:
    """Return the activity price closest in time to executed_ts, or None if no match within window."""
    candidates = activity_map.get(token_id, [])
    best_price, best_diff = None, float("inf")
    for ts, price in candidates:
        diff = abs(ts - executed_ts)
        if diff < best_diff:
            best_diff = diff
            best_price = price
    if best_price is not None and best_diff < window:
        return best_price
    return None


# ── Price correction ──────────────────────────────────────────────────────────

def refresh_buy_prices(session, funder_address: str, activity: list[dict] | None = None) -> int:
    """Update copy_price for BUY trades using actual fill prices from wallet activity.

    Returns the number of records updated.
    """
    buys = (
        session.query(CopyTrade)
        .filter(CopyTrade.original_side == "BUY", CopyTrade.status == "success")
        .all()
    )
    logger.info("Found %d BUY trades to check.", len(buys))
    if not buys:
        return 0

    if activity is None:
        activity = _fetch_activity(funder_address)
        logger.info("Fetched %d activity entries.", len(activity))

    act_map = _build_activity_map(activity, "BUY")
    updated = 0
    for ct in buys:
        if not ct.original_token_id:
            continue
        ct_ts = ct.executed_at.timestamp() if ct.executed_at else 0.0
        actual = _find_best_price(act_map, ct.original_token_id, ct_ts)
        if actual is None:
            continue
        current = ct.copy_price or 0.0
        if abs(current - actual) > 1e-6:
            logger.info(
                "BUY id=%d ...%s: %.6f → %.6f",
                ct.id, ct.original_token_id[-8:], current, actual,
            )
            ct.copy_price = actual
            updated += 1

    if updated:
        session.commit()
    logger.info("Updated copy_price on %d BUY trade(s).", updated)
    return updated


def refresh_sell_prices(session, funder_address: str, activity: list[dict] | None = None) -> int:
    """Update copy_price for SELL trades using actual fill prices from wallet activity.

    Returns the number of records updated.
    """
    sells = (
        session.query(CopyTrade)
        .filter(CopyTrade.original_side == "SELL", CopyTrade.status == "success")
        .all()
    )
    logger.info("Found %d SELL trades to check.", len(sells))
    if not sells:
        return 0

    if activity is None:
        activity = _fetch_activity(funder_address)
        logger.info("Fetched %d activity entries.", len(activity))

    act_map = _build_activity_map(activity, "SELL")
    updated = 0
    for ct in sells:
        if not ct.original_token_id:
            continue
        ct_ts = ct.executed_at.timestamp() if ct.executed_at else 0.0
        actual = _find_best_price(act_map, ct.original_token_id, ct_ts)
        if actual is None:
            continue
        current = ct.copy_price or 0.0
        if abs(current - actual) > 1e-6:
            logger.info(
                "SELL id=%d ...%s: %.6f → %.6f",
                ct.id, ct.original_token_id[-8:], current, actual,
            )
            ct.copy_price = actual
            updated += 1

    if updated:
        session.commit()
    logger.info("Updated copy_price on %d SELL trade(s).", updated)
    return updated


# ── PnL recalculation ────────────────────────────────────────────────────────

def recalculate_sell_pnl(session) -> int:
    """Recompute realized PnL for all SELL trades using corrected BUY + SELL prices.

    realized_pnl = (sell_price - weighted_avg_buy_price) * sell_size

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
        result = (
            session.query(
                func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0),
                func.coalesce(func.sum(CopyTrade.copy_size), 0.0),
            )
            .filter(
                CopyTrade.trader_id == ct.trader_id,
                CopyTrade.original_token_id == ct.original_token_id,
                CopyTrade.original_side == "BUY",
                CopyTrade.status.in_(["success", "dry_run"]),
            )
            .first()
        )
        total_cost, total_size = result
        if not total_size or total_size <= 0:
            continue

        avg_buy = total_cost / total_size
        sell_price = ct.copy_price or 0.0
        new_pnl = round((sell_price - avg_buy) * ct.copy_size, 4)
        old_pnl = ct.pnl or 0.0
        if abs(new_pnl - old_pnl) > 1e-6:
            logger.info(
                "SELL id=%d ...%s: pnl %.4f → %.4f (avg_buy=%.4f sell=%.4f size=%.4f)",
                ct.id, ct.original_token_id[-8:], old_pnl, new_pnl,
                avg_buy, sell_price, ct.copy_size,
            )
            ct.pnl = new_pnl
            updated += 1

    if updated:
        session.commit()
    logger.info("Updated PnL on %d SELL trade(s).", updated)
    return updated


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    from config import settings

    logger.info("=== refresh_prices: starting ===")
    init_db()
    session_factory = get_session_factory()

    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        logger.error("POLYMARKET_FUNDER_ADDRESS is not set.")
        sys.exit(1)

    # Fetch activity once, reuse for both BUY and SELL correction
    logger.info("Fetching wallet activity for %s...", funder[:12])
    activity = _fetch_activity(funder, limit=500)
    logger.info("Got %d trade activity entries.", len(activity))

    with session_factory() as session:
        buy_updated  = refresh_buy_prices(session, funder, activity)
        sell_updated = refresh_sell_prices(session, funder, activity)
        pnl_updated  = recalculate_sell_pnl(session)

    logger.info(
        "=== Done: %d BUY price(s) corrected, %d SELL price(s) corrected, "
        "%d realized PnL(s) recalculated ===",
        buy_updated, sell_updated, pnl_updated,
    )


if __name__ == "__main__":
    main()
