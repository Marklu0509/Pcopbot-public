"""One-shot script: refresh copy_price from actual fill prices and recalculate PnL.

Usage (from project root):
    python -m scripts.refresh_prices

What it does:
1. For every BUY CopyTrade with a real order_id (status=success), fetch the
   actual average fill price from the CLOB API and update copy_price.
2. For every SELL CopyTrade (status=success or dry_run), recompute realized PnL
   using the weighted-average corrected BUY price for that token.
"""

from __future__ import annotations

import sys
import os

# Allow running from project root: python -m scripts.refresh_prices
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


def _to_float_or_none(value) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _get_clob_client():
    from py_clob_client.client import ClobClient  # type: ignore
    from py_clob_client.clob_types import ApiCreds  # type: ignore
    from config import settings

    private_key = (settings.POLYMARKET_PRIVATE_KEY or "").strip()
    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=settings.POLYMARKET_CHAIN_ID,
        signature_type=2,
        funder=funder,
    )

    env_api_key = (settings.POLYMARKET_API_KEY or "").strip()
    env_api_secret = (settings.POLYMARKET_API_SECRET or "").strip()
    env_api_passphrase = (settings.POLYMARKET_API_PASSPHRASE or "").strip()

    if env_api_key and env_api_secret and env_api_passphrase:
        creds = ApiCreds(
            api_key=env_api_key,
            api_secret=env_api_secret,
            api_passphrase=env_api_passphrase,
        )
        client.set_api_creds(creds)
    else:
        derived = client.create_or_derive_api_creds()
        client.set_api_creds(derived)
    return client


def _fetch_fill_price(client, order_id: str) -> float | None:
    """Return actual average fill price for an order, or None if unavailable."""
    try:
        order = client.get_order(order_id)
        if isinstance(order, dict):
            candidates = [
                order.get("avgPrice"),
                order.get("averagePrice"),
                order.get("filledAvgPrice"),
                order.get("price"),
            ]
        else:
            candidates = [
                getattr(order, "avgPrice", None),
                getattr(order, "averagePrice", None),
                getattr(order, "filledAvgPrice", None),
                getattr(order, "price", None),
            ]
        for raw in candidates:
            parsed = _to_float_or_none(raw)
            if parsed is not None:
                return parsed
    except Exception as exc:
        logger.debug("Could not fetch fill price for order %s: %s", order_id, exc)
    return None


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


def refresh_buy_prices(session, client) -> int:
    """Update copy_price for all BUY trades with order_id using actual fill price.

    Returns the number of records updated.
    """
    buys = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status == "success",
            CopyTrade.order_id.is_not(None),
            CopyTrade.order_id != "",
        )
        .all()
    )
    logger.info("Found %d BUY trades with order_id to check.", len(buys))

    updated = 0
    for ct in buys:
        actual = _fetch_fill_price(client, ct.order_id)
        if actual is None:
            continue
        current = ct.copy_price or 0.0
        if abs(current - actual) > 1e-6:
            logger.info(
                "BUY id=%d token=%s: copy_price %.6f → %.6f (diff %.6f)",
                ct.id, ct.original_token_id, current, actual, actual - current,
            )
            ct.copy_price = actual
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
                "SELL id=%d token=%s: pnl %.4f → %.4f (avg_buy=%.4f, sell_price=%.4f, size=%.4f)",
                ct.id, ct.original_token_id, old_pnl, new_pnl, avg_buy, sell_price, ct.copy_size,
            )
            ct.pnl = new_pnl
            updated += 1

    if updated:
        session.commit()
    logger.info("Updated PnL on %d SELL trade(s).", updated)
    return updated


def main() -> None:
    logger.info("=== refresh_prices: starting ===")
    init_db()
    SessionLocal = get_session_factory()

    try:
        client = _get_clob_client()
    except Exception as exc:
        logger.error("Failed to connect to CLOB API: %s", exc)
        sys.exit(1)

    with SessionLocal() as session:
        buy_updated = refresh_buy_prices(session, client)
        sell_updated = recalculate_sell_pnl(session)

    logger.info(
        "=== Done: %d BUY price(s) corrected, %d SELL PnL(s) recalculated ===",
        buy_updated, sell_updated,
    )


if __name__ == "__main__":
    main()
