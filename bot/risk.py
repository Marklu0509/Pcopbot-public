"""Risk checks performed before copying any trade."""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from db.models import CopyTrade, Trader

logger = logging.getLogger(__name__)

# Status constants
STATUS_BELOW_THRESHOLD = "below_threshold"
STATUS_POSITION_LIMIT = "position_limit"
STATUS_SLIPPAGE_EXCEEDED = "slippage_exceeded"


def check_min_threshold(copy_size: float, trader: Trader) -> Optional[str]:
    """Return a rejection status if copy_size is below trader's min threshold."""
    if copy_size < trader.min_trade_threshold:
        logger.info(
            "Trade size %.4f below min threshold %.4f for trader %s",
            copy_size,
            trader.min_trade_threshold,
            trader.wallet_address,
        )
        return STATUS_BELOW_THRESHOLD
    return None


def check_position_limit(
    session: Session,
    trader: Trader,
    token_id: str,
    copy_size: float,
) -> Optional[str]:
    """Return a rejection status if adding copy_size would exceed the max position limit.

    Current exposure is calculated as the sum of copy_size for all successful or dry_run
    trades for the same trader + token.
    """
    existing_exposure: float = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .with_entities(CopyTrade.copy_size)
        .all()
    )
    total_exposure = sum(row[0] for row in existing_exposure if row[0])
    if total_exposure + copy_size > trader.max_position_limit:
        logger.info(
            "Position limit exceeded for trader %s token %s: current=%.2f new=%.2f limit=%.2f",
            trader.wallet_address,
            token_id,
            total_exposure,
            copy_size,
            trader.max_position_limit,
        )
        return STATUS_POSITION_LIMIT
    return None


def check_slippage(
    best_price: float,
    expected_price: float,
    trader: Trader,
) -> Optional[str]:
    """Return a rejection status if estimated slippage exceeds trader's max.

    Slippage = abs(best_price - expected_price) / expected_price * 100
    """
    if expected_price <= 0:
        return None  # Cannot calculate — let trade proceed
    slippage_pct = abs(best_price - expected_price) / expected_price * 100.0
    if slippage_pct > trader.max_slippage:
        logger.info(
            "Slippage %.2f%% exceeds max %.2f%% for trader %s",
            slippage_pct,
            trader.max_slippage,
            trader.wallet_address,
        )
        return STATUS_SLIPPAGE_EXCEEDED
    return None


def run_all_checks(
    session: Session,
    trader: Trader,
    token_id: str,
    copy_size: float,
    best_price: float,
    expected_price: float,
) -> Optional[str]:
    """Run all risk checks in order and return the first rejection status, or None if OK."""
    rejection = check_min_threshold(copy_size, trader)
    if rejection:
        return rejection

    rejection = check_position_limit(session, trader, token_id, copy_size)
    if rejection:
        return rejection

    rejection = check_slippage(best_price, expected_price, trader)
    if rejection:
        return rejection

    return None
