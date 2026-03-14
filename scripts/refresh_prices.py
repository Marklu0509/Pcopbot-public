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


def _fetch_activity_all(wallet_address: str, limit: int = 500) -> list[dict]:
    """Fetch all activity types (TRADE + REDEEM) for a wallet."""
    import requests
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": wallet_address, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])
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


# ── Manual redemption sync ───────────────────────────────────────────────────

def sync_manual_redemptions(session, funder_address: str, all_activity: list[dict] | None = None) -> int:
    """Create SELL records for manual Polymarket redemptions not yet in DB.

    REDEEM activities have empty asset/outcome fields, so we match by
    conditionId to find which traders held positions in that market.
    For each trader with net holdings > 0 in the redeemed market, we
    create a synthetic SELL at price=1.0 (redemption always pays $1/share).

    Returns the number of records created.
    """
    import datetime
    from sqlalchemy import or_
    from bot.executor import _get_net_holdings

    if all_activity is None:
        all_activity = _fetch_activity_all(funder_address, limit=500)

    redeems = [d for d in all_activity if d.get("type", "").upper() == "REDEEM"]
    if not redeems:
        logger.info("No REDEEM activities found.")
        return 0

    logger.info("Found %d REDEEM activities to process.", len(redeems))
    created = 0

    for raw in redeems:
        condition_id = raw.get("conditionId", "")
        tx_hash = raw.get("transactionHash", "")
        market_title = raw.get("title", "")
        if not condition_id or not tx_hash:
            continue

        ts = float(raw.get("timestamp", 0) or 0)
        redeem_time = datetime.datetime.utcfromtimestamp(ts) if ts else datetime.datetime.utcnow()

        # Duplicate check: if we already have a record for this tx, skip
        order_id_key = f"manual_redeem:{tx_hash[:20]}"
        if session.query(CopyTrade).filter(CopyTrade.order_id == order_id_key).first():
            logger.debug("Already recorded: %s", order_id_key)
            continue

        # Find all BUY trades for this market across all traders
        buy_trades = (
            session.query(CopyTrade)
            .filter(
                CopyTrade.original_market == condition_id,
                CopyTrade.original_side == "BUY",
                CopyTrade.status.in_(["success", "dry_run"]),
            )
            .all()
        )
        if not buy_trades:
            logger.debug("No BUY trades for redeemed market %s (%s)", market_title, condition_id[:12])
            continue

        # Group by trader_id → unique token_ids
        by_trader: dict[int, set] = defaultdict(set)
        for bt in buy_trades:
            if bt.original_token_id:
                by_trader[bt.trader_id].add(bt.original_token_id)

        for trader_id, token_ids in by_trader.items():
            for token_id in token_ids:
                net_shares = _get_net_holdings(session, trader_id, token_id)
                if net_shares <= 0:
                    continue  # already fully sold or no holdings

                # Weighted avg buy price for this trader + token
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
                avg_buy = (total_cost / total_size) if total_size and total_size > 0 else 0.0
                pnl = round((1.0 - avg_buy) * net_shares, 4)

                # Get outcome from any buy trade for this token
                sample_buy = next(
                    (bt for bt in buy_trades if bt.trader_id == trader_id and bt.original_token_id == token_id),
                    buy_trades[0],
                )

                redemption = CopyTrade(
                    trader_id=trader_id,
                    original_trade_id=f"redeem:{tx_hash[:24]}",
                    original_market=condition_id,
                    original_token_id=token_id,
                    market_title=market_title or sample_buy.market_title,
                    outcome=sample_buy.outcome,
                    original_side="SELL",
                    original_size=net_shares,
                    original_price=1.0,
                    original_timestamp=redeem_time,
                    copy_size=net_shares,
                    copy_price=1.0,
                    status="success",
                    order_id=order_id_key,
                    pnl=pnl,
                    executed_at=redeem_time,
                )
                session.add(redemption)
                created += 1
                logger.info(
                    "Redemption recorded: market=%s trader=%d size=%.4f avg_buy=%.4f pnl=%.4f",
                    market_title or condition_id[:12], trader_id, net_shares, avg_buy, pnl,
                )

    if created:
        session.commit()
    logger.info("Created %d manual redemption record(s).", created)
    return created


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
