"""Executes copy trades via py_clob_client (or logs them in dry-run mode)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from bot import risk, watermark
from config import settings
from db.models import CopyTrade, Trader

logger = logging.getLogger(__name__)


def _get_net_holdings(session: Session, trader_id: int, token_id: str) -> float:
    """Return net share holdings for a trader+token from successful/dry_run copy trades."""
    from sqlalchemy import func
    buy_total = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size), 0.0))
        .filter(
            CopyTrade.trader_id == trader_id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    sell_total = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size), 0.0))
        .filter(
            CopyTrade.trader_id == trader_id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    return max(buy_total - sell_total, 0.0)


def _calculate_copy_size(trader: Trader, original_size: float, price: float) -> float:
    """Determine the copy trade size (in shares) based on the trader's sizing mode.

    Fixed mode:  user sets a dollar budget → convert to shares (budget / price).
    Proportional mode: percentage of the original trade's share count.
    """
    if trader.sizing_mode == "proportional":
        return original_size * (trader.proportional_pct / 100.0)
    # Fixed mode: convert dollar amount to shares
    if price > 0:
        return trader.fixed_amount / price
    return trader.fixed_amount


def _get_clob_client():
    """Lazily import and construct the CLOB client."""
    try:
        from py_clob_client.client import ClobClient  # type: ignore

        return ClobClient(
            host="https://clob.polymarket.com",
            key=settings.POLYMARKET_PRIVATE_KEY,
            chain_id=settings.POLYMARKET_CHAIN_ID,
            signature_type=2,
            funder=settings.POLYMARKET_FUNDER_ADDRESS,
        )
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to initialise ClobClient: %s", exc)
        raise


def _apply_slippage(price: float, side: str, slippage_pct: float) -> float:
    """Apply slippage tolerance to get the order price.

    BUY:  increase price (willing to pay more to ensure fill).
    SELL: decrease price (willing to accept less to ensure fill).
    Result is clamped to [0.01, 0.99] (Polymarket price bounds).
    """
    if side == "BUY":
        return min(price * (1 + slippage_pct / 100.0), 0.99)
    return max(price * (1 - slippage_pct / 100.0), 0.01)


def _extract_price(entry) -> float:
    """Extract the price from an orderbook entry (dict or object)."""
    if isinstance(entry, dict):
        return float(entry.get("price", 0))
    return float(getattr(entry, "price", 0))


def _get_best_price(client, token_id: str, side: str) -> float | None:
    """Query the CLOB orderbook for the current best available price.

    Returns the best ask (for BUY) or best bid (for SELL), or None on failure.
    """
    try:
        book = client.get_order_book(token_id)
        if side == "BUY":
            entries = getattr(book, "asks", None)
            if entries is None and isinstance(book, dict):
                entries = book.get("asks")
        else:
            entries = getattr(book, "bids", None)
            if entries is None and isinstance(book, dict):
                entries = book.get("bids")
        if entries:
            prices = [_extract_price(e) for e in entries]
            return min(prices) if side == "BUY" else max(prices)
    except Exception as exc:
        logger.warning("Failed to query orderbook for %s: %s", token_id, exc)
    return None


def execute_copy_trade(
    session: Session,
    trader: Trader,
    trade: dict[str, Any],
) -> CopyTrade:
    """Apply risk checks and execute (or simulate) a copy trade.

    Returns the persisted CopyTrade record.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
    from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

    expected_price = trade["price"]
    copy_size = _calculate_copy_size(trader, trade["size"], expected_price)

    # For SELL trades, cap the size at what we actually hold to avoid
    # selling more shares than we own (can happen when some BUYs were
    # filtered out by risk checks).
    if trade["side"] == "SELL":
        holdings = _get_net_holdings(session, trader.id, trade["token_id"])
        if holdings <= 0:
            logger.info(
                "SELL skipped for trader %s token %s: no holdings to sell.",
                trader.wallet_address, trade["token_id"],
            )
            copy_trade = CopyTrade(
                trader_id=trader.id,
                original_trade_id=trade["trade_id"],
                original_market=trade["market"],
                original_token_id=trade["token_id"],
                market_title=trade.get("market_title", ""),
                outcome=trade.get("outcome", ""),
                original_side=trade["side"],
                original_size=trade["size"],
                original_price=trade["price"],
                original_timestamp=trade["timestamp"].replace(tzinfo=None),
                copy_size=0.0,
                copy_price=expected_price,
                status="below_threshold",
                error_message="No holdings to sell",
                executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            session.add(copy_trade)
            session.commit()
            return copy_trade
        if copy_size > holdings:
            logger.info(
                "SELL capped for trader %s token %s: wanted %.4f but only hold %.4f",
                trader.wallet_address, trade["token_id"], copy_size, holdings,
            )
            copy_size = holdings

    # Determine order type and slippage for this side
    if trade["side"] == "BUY":
        order_type_str = getattr(trader, "buy_order_type", None) or "market"
        slippage_pct = trader.buy_slippage
    else:
        order_type_str = trader.sell_order_type or "market"
        slippage_pct = trader.sell_slippage

    # Apply slippage to get the actual order price
    order_price = _apply_slippage(expected_price, trade["side"], slippage_pct)

    # In live mode, query orderbook for real best price (for slippage risk check)
    best_price = expected_price
    client = None
    if not settings.DRY_RUN:
        try:
            client = _get_clob_client()
            real_price = _get_best_price(client, trade["token_id"], trade["side"])
            if real_price is not None:
                best_price = real_price
        except Exception as exc:
            logger.warning("Could not query orderbook: %s", exc)

    rejection = risk.run_all_checks(
        session=session,
        trader=trader,
        token_id=trade["token_id"],
        market=trade["market"],
        copy_size=copy_size,
        best_price=best_price,
        expected_price=expected_price,
        original_size=trade["size"],
        original_price=trade["price"],
        side=trade["side"],
    )

    status = rejection or ("dry_run" if settings.DRY_RUN else "pending")
    order_id: str | None = None
    error_msg: str | None = None

    if rejection is None and not settings.DRY_RUN:
        try:
            if client is None:
                client = _get_clob_client()
            side = BUY if trade["side"] == "BUY" else SELL
            ot = OrderType.FOK if order_type_str == "market" else OrderType.GTC
            order_args = OrderArgs(
                token_id=trade["token_id"],
                price=round(order_price, 4),
                size=round(copy_size, 4),
                side=side,
            )
            resp = client.post_order(order_args, ot)
            order_id = str(resp.get("orderID") or resp.get("order_id") or "")
            status = "success"
            logger.info(
                "Copy trade executed for trader %s: market=%s side=%s size=%.4f "
                "price=%.4f (orig=%.4f, slippage=%.1f%%) order_type=%s order_id=%s",
                trader.wallet_address,
                trade["market"],
                trade["side"],
                copy_size,
                order_price,
                expected_price,
                slippage_pct,
                order_type_str.upper(),
                order_id,
            )
        except Exception as exc:  # pragma: no cover
            status = "failed"
            error_msg = str(exc)
            logger.error(
                "Copy trade FAILED for trader %s: %s",
                trader.wallet_address,
                exc,
            )
    elif rejection is None and settings.DRY_RUN:
        logger.info(
            "[DRY RUN] Would copy trade for trader %s: market=%s side=%s size=%.4f "
            "price=%.4f (limit=%.4f, slippage=%.1f%%) order_type=%s",
            trader.wallet_address,
            trade["market"],
            trade["side"],
            copy_size,
            expected_price,
            order_price,
            slippage_pct,
            order_type_str.upper(),
        )
    else:
        logger.info(
            "Trade skipped for trader %s: reason=%s",
            trader.wallet_address,
            rejection,
        )

    # copy_price: in dry run, use expected_price (simulated fill);
    # in live mode, use the order limit price (actual fill may differ).
    recorded_price = expected_price if settings.DRY_RUN else order_price

    copy_trade = CopyTrade(
        trader_id=trader.id,
        original_trade_id=trade["trade_id"],
        original_market=trade["market"],
        original_token_id=trade["token_id"],
        market_title=trade.get("market_title", ""),
        outcome=trade.get("outcome", ""),
        original_side=trade["side"],
        original_size=trade["size"],
        original_price=trade["price"],
        original_timestamp=trade["timestamp"].replace(tzinfo=None),
        copy_size=copy_size,
        copy_price=recorded_price,
        status=status,
        error_message=error_msg,
        order_id=order_id,
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(copy_trade)
    session.commit()
    return copy_trade
