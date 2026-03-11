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

    # For slippage check we use the expected price as the "best price" proxy.
    # In production this could query the orderbook; here we use a 0% slippage
    # baseline so the check can still be exercised via max_slippage config.
    best_price = expected_price

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
            client = _get_clob_client()
            side = BUY if trade["side"] == "BUY" else SELL
            order_args = OrderArgs(
                token_id=trade["token_id"],
                price=round(expected_price, 4),
                size=round(copy_size, 4),
                side=side,
            )
            resp = client.post_order(order_args, OrderType.GTC)
            order_id = str(resp.get("orderID") or resp.get("order_id") or "")
            status = "success"
            logger.info(
                "Copy trade executed for trader %s: market=%s side=%s size=%.4f price=%.4f order_id=%s",
                trader.wallet_address,
                trade["market"],
                trade["side"],
                copy_size,
                expected_price,
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
            "[DRY RUN] Would copy trade for trader %s: market=%s side=%s size=%.4f price=%.4f",
            trader.wallet_address,
            trade["market"],
            trade["side"],
            copy_size,
            expected_price,
        )
    else:
        logger.info(
            "Trade skipped for trader %s: reason=%s",
            trader.wallet_address,
            rejection,
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
        copy_size=copy_size,
        copy_price=expected_price,
        status=status,
        error_message=error_msg,
        order_id=order_id,
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(copy_trade)
    session.commit()
    return copy_trade
