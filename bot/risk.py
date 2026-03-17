"""Risk checks performed before copying any trade."""

import logging
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from db.models import CopyTrade, Trader

logger = logging.getLogger(__name__)

# Status constants
STATUS_BELOW_THRESHOLD = "below_threshold"
STATUS_POSITION_LIMIT = "position_limit"
STATUS_SLIPPAGE_EXCEEDED = "slippage_exceeded"
STATUS_BELOW_MINIMUM_ORDER = "below_minimum_order"

# Hard floor: reject orders below $1 USD
MINIMUM_ORDER_VALUE_USD = 1.0


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


def check_ignore_trades_under(original_size: float, original_price: float, trader: Trader) -> Optional[str]:
    """Reject if the target trader's trade USD value is below ignore threshold."""
    if trader.ignore_trades_under > 0:
        trade_value = original_size * original_price
        if trade_value < trader.ignore_trades_under:
            logger.info(
                "Original trade value $%.2f below ignore threshold $%.2f for trader %s",
                trade_value, trader.ignore_trades_under, trader.wallet_address,
            )
            return STATUS_BELOW_THRESHOLD
    return None


def check_price_filter(price: float, trader: Trader) -> Optional[str]:
    """Reject if price is outside trader's min/max price range."""
    if trader.min_price > 0 and price < trader.min_price:
        logger.info("Price $%.4f below min $%.4f for trader %s", price, trader.min_price, trader.wallet_address)
        return STATUS_BELOW_THRESHOLD
    if trader.max_price > 0 and price > trader.max_price:
        logger.info("Price $%.4f above max $%.4f for trader %s", price, trader.max_price, trader.wallet_address)
        return STATUS_BELOW_THRESHOLD
    return None


def cap_per_trade_limit(copy_size: float, price: float, trader: Trader) -> float:
    """Cap copy_size so trade value does not exceed max_per_trade.

    Returns the (possibly reduced) copy_size. Does NOT reject.
    min_per_trade rejection is handled separately in run_all_checks.
    """
    if trader.max_per_trade > 0 and price > 0:
        max_shares = trader.max_per_trade / price
        if copy_size > max_shares:
            logger.info(
                "Capping trade from %.4f to %.4f shares (max $%.2f) for trader %s",
                copy_size, max_shares, trader.max_per_trade, trader.wallet_address,
            )
            return max_shares
    return copy_size


def cap_total_spend_limit(
    session: Session, trader: Trader, copy_size: float, price: float,
    status_filter: list[str] | None = None,
) -> float:
    """Cap copy_size so cumulative spend does not exceed total_spend_limit."""
    if status_filter is None:
        status_filter = ["success", "dry_run"]
    if trader.total_spend_limit <= 0 or price <= 0:
        return copy_size
    total_spent = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(status_filter),
        )
        .scalar()
    )
    remaining = trader.total_spend_limit - total_spent
    if remaining <= 0:
        logger.info("Total spend limit $%.2f reached for trader %s", trader.total_spend_limit, trader.wallet_address)
        return 0.0
    max_shares = remaining / price
    if copy_size > max_shares:
        logger.info(
            "Capping trade from %.4f to %.4f shares (remaining budget $%.2f) for trader %s",
            copy_size, max_shares, remaining, trader.wallet_address,
        )
        return max_shares
    return copy_size


def cap_max_per_market(
    session: Session, trader: Trader, market: str, copy_size: float, price: float,
    status_filter: list[str] | None = None,
) -> float:
    """Cap copy_size so total market exposure does not exceed max_per_market."""
    if status_filter is None:
        status_filter = ["success", "dry_run"]
    if trader.max_per_market <= 0 or price <= 0:
        return copy_size
    existing = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_market == market,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(status_filter),
        )
        .scalar()
    )
    remaining = trader.max_per_market - existing
    if remaining <= 0:
        logger.info("Max per market $%.2f reached for trader %s", trader.max_per_market, trader.wallet_address)
        return 0.0
    max_shares = remaining / price
    if copy_size > max_shares:
        logger.info(
            "Capping trade from %.4f to %.4f shares (market remaining $%.2f) for trader %s",
            copy_size, max_shares, remaining, trader.wallet_address,
        )
        return max_shares
    return copy_size


def cap_max_per_yes_no(
    session: Session, trader: Trader, token_id: str, copy_size: float, price: float,
    status_filter: list[str] | None = None,
) -> float:
    """Cap copy_size so total outcome exposure does not exceed max_per_yes_no."""
    if status_filter is None:
        status_filter = ["success", "dry_run"]
    if trader.max_per_yes_no <= 0 or price <= 0:
        return copy_size
    existing = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(status_filter),
        )
        .scalar()
    )
    remaining = trader.max_per_yes_no - existing
    if remaining <= 0:
        logger.info("Max per yes/no $%.2f reached for trader %s", trader.max_per_yes_no, trader.wallet_address)
        return 0.0
    max_shares = remaining / price
    if copy_size > max_shares:
        logger.info(
            "Capping trade from %.4f to %.4f shares (yes/no remaining $%.2f) for trader %s",
            copy_size, max_shares, remaining, trader.wallet_address,
        )
        return max_shares
    return copy_size


def cap_position_limit(
    session: Session,
    trader: Trader,
    token_id: str,
    copy_size: float,
    price: float,
    status_filter: list[str] | None = None,
) -> float:
    """Cap copy_size so net position exposure does not exceed max_position_limit."""
    if status_filter is None:
        status_filter = ["success", "dry_run"]
    if trader.max_position_limit <= 0 or price <= 0:
        return copy_size
    buy_total = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(status_filter),
        )
        .scalar()
    )
    sell_total = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(status_filter),
        )
        .scalar()
    )
    net_exposure = buy_total - sell_total
    remaining = trader.max_position_limit - net_exposure
    if remaining <= 0:
        logger.info(
            "Position limit $%.2f reached for trader %s token %s",
            trader.max_position_limit, trader.wallet_address, token_id,
        )
        return 0.0
    max_shares = remaining / price
    if copy_size > max_shares:
        logger.info(
            "Capping trade from %.4f to %.4f shares (position remaining $%.2f) for trader %s",
            copy_size, max_shares, remaining, trader.wallet_address,
        )
        return max_shares
    return copy_size


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


def cap_and_check(
    session: Session,
    trader: Trader,
    token_id: str,
    market: str,
    copy_size: float,
    best_price: float,
    expected_price: float,
    original_size: float,
    original_price: float,
    side: str,
    status_filter: list[str] | None = None,
    order_price: float | None = None,
) -> tuple[float, Optional[str]]:
    """Run all risk checks: cap copy_size to limits, then reject if below minimum.

    Returns (capped_copy_size, rejection_status).
    rejection_status is None if the trade should proceed.
    status_filter controls which trade statuses to count for limit checks.
    order_price: the actual price we'll pay (post-slippage). Used for cap
    calculations so actual cost stays within limits. Falls back to expected_price.
    """
    if status_filter is None:
        status_filter = ["success", "dry_run"]

    # Price used for cap calculations — order_price accounts for slippage
    cap_price = order_price if order_price is not None else expected_price

    # ── Filters that apply to ALL trades (BUY and SELL) ──
    rejection = check_ignore_trades_under(original_size, original_price, trader)
    if rejection:
        return copy_size, rejection

    rejection = check_price_filter(expected_price, trader)
    if rejection:
        return copy_size, rejection

    # ── Buy-side: cap to limits instead of rejecting ──
    if side == "BUY":
        copy_size = cap_per_trade_limit(copy_size, cap_price, trader)
        copy_size = cap_total_spend_limit(session, trader, copy_size, cap_price, status_filter=status_filter)
        copy_size = cap_max_per_market(session, trader, market, copy_size, cap_price, status_filter=status_filter)
        copy_size = cap_max_per_yes_no(session, trader, token_id, copy_size, cap_price, status_filter=status_filter)
        copy_size = cap_position_limit(session, trader, token_id, copy_size, cap_price, status_filter=status_filter)

    # ── Check min_per_trade (reject or bump if below, after capping) ──
    buy_at_min = getattr(trader, "buy_at_min", False)
    if side == "BUY":
        trade_value = copy_size * cap_price
        if trader.min_per_trade > 0 and trade_value < trader.min_per_trade:
            if buy_at_min and cap_price > 0:
                min_shares = trader.min_per_trade / cap_price
                logger.info(
                    "Bumping trade from %.4f to %.4f shares (min_per_trade $%.2f) for trader %s",
                    copy_size, min_shares, trader.min_per_trade, trader.wallet_address,
                )
                copy_size = min_shares
            else:
                logger.info(
                    "Trade value $%.2f below min_per_trade $%.2f for trader %s (after capping)",
                    trade_value, trader.min_per_trade, trader.wallet_address,
                )
                return copy_size, STATUS_BELOW_THRESHOLD

    # ── Minimum order value $1 USD hard floor ──
    order_value = copy_size * cap_price
    if order_value < MINIMUM_ORDER_VALUE_USD:
        if side == "BUY" and buy_at_min and cap_price > 0:
            min_shares = MINIMUM_ORDER_VALUE_USD / cap_price
            logger.info(
                "Bumping trade from %.4f to %.4f shares ($%.2f hard floor) for trader %s",
                copy_size, min_shares, MINIMUM_ORDER_VALUE_USD, trader.wallet_address,
            )
            copy_size = min_shares
        else:
            logger.info(
                "Order value $%.2f below $%.2f minimum for trader %s",
                order_value, MINIMUM_ORDER_VALUE_USD, trader.wallet_address,
            )
            return copy_size, STATUS_BELOW_MINIMUM_ORDER

    # ── Slippage (applies to all) ──
    rejection = check_slippage(best_price, expected_price, trader)
    if rejection:
        return copy_size, rejection

    return copy_size, None


# Keep backward-compatible alias
def run_all_checks(
    session: Session,
    trader: Trader,
    token_id: str,
    market: str,
    copy_size: float,
    best_price: float,
    expected_price: float,
    original_size: float,
    original_price: float,
    side: str,
) -> Optional[str]:
    """Legacy wrapper — returns rejection only (does not cap)."""
    _, rejection = cap_and_check(
        session, trader, token_id, market, copy_size,
        best_price, expected_price, original_size, original_price, side,
    )
    return rejection
