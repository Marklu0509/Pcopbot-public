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


def check_per_trade_limit(copy_size: float, price: float, trader: Trader) -> Optional[str]:
    """Reject if this trade's USD value exceeds max_per_trade or is below min_per_trade."""
    trade_value = copy_size * price
    if trader.min_per_trade > 0 and trade_value < trader.min_per_trade:
        logger.info("Trade value $%.2f below min_per_trade $%.2f for trader %s",
                     trade_value, trader.min_per_trade, trader.wallet_address)
        return STATUS_BELOW_THRESHOLD
    if trader.max_per_trade > 0 and trade_value > trader.max_per_trade:
        logger.info("Trade value $%.2f exceeds max_per_trade $%.2f for trader %s",
                     trade_value, trader.max_per_trade, trader.wallet_address)
        return STATUS_POSITION_LIMIT
    return None


def check_total_spend_limit(session: Session, trader: Trader, copy_size: float, price: float) -> Optional[str]:
    """Reject if adding this trade would exceed the trader's total spend limit."""
    if trader.total_spend_limit <= 0:
        return None
    total_spent = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    new_cost = copy_size * price
    if total_spent + new_cost > trader.total_spend_limit:
        logger.info("Total spend $%.2f + $%.2f would exceed limit $%.2f for trader %s",
                     total_spent, new_cost, trader.total_spend_limit, trader.wallet_address)
        return STATUS_POSITION_LIMIT
    return None


def check_max_per_market(session: Session, trader: Trader, market: str, copy_size: float, price: float) -> Optional[str]:
    """Reject if total USD exposure on this market would exceed max_per_market."""
    if trader.max_per_market <= 0:
        return None
    existing = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_market == market,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    new_cost = copy_size * price
    if existing + new_cost > trader.max_per_market:
        logger.info("Market exposure $%.2f + $%.2f would exceed max_per_market $%.2f for trader %s",
                     existing, new_cost, trader.max_per_market, trader.wallet_address)
        return STATUS_POSITION_LIMIT
    return None


def check_max_per_yes_no(session: Session, trader: Trader, token_id: str, copy_size: float, price: float) -> Optional[str]:
    """Reject if total USD exposure on this outcome (token_id) would exceed max_per_yes_no."""
    if trader.max_per_yes_no <= 0:
        return None
    existing = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    new_cost = copy_size * price
    if existing + new_cost > trader.max_per_yes_no:
        logger.info("Token exposure $%.2f + $%.2f would exceed max_per_yes_no $%.2f for trader %s",
                     existing, new_cost, trader.max_per_yes_no, trader.wallet_address)
        return STATUS_POSITION_LIMIT
    return None


def check_position_limit(
    session: Session,
    trader: Trader,
    token_id: str,
    copy_size: float,
    price: float,
) -> Optional[str]:
    """Return a rejection status if adding copy_size would exceed the max position limit ($).

    Current exposure is the sum of (copy_size * copy_price) for all successful or dry_run
    BUY trades minus SELL trades for the same trader + token.
    """
    buy_total = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    sell_total = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0))
        .filter(
            CopyTrade.trader_id == trader.id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .scalar()
    )
    net_exposure = buy_total - sell_total
    new_cost = copy_size * price
    if net_exposure + new_cost > trader.max_position_limit:
        logger.info(
            "Position limit exceeded for trader %s token %s: net=$%.2f new=$%.2f limit=$%.2f",
            trader.wallet_address, token_id, net_exposure, new_cost, trader.max_position_limit,
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
    market: str,
    copy_size: float,
    best_price: float,
    expected_price: float,
    original_size: float,
    original_price: float,
    side: str,
) -> Optional[str]:
    """Run all risk checks in order and return the first rejection status, or None if OK."""
    # ── Filters that apply to ALL trades (BUY and SELL) ──
    rejection = check_ignore_trades_under(original_size, original_price, trader)
    if rejection:
        return rejection

    rejection = check_price_filter(expected_price, trader)
    if rejection:
        return rejection

    # ── Buy-side spending / position limits (only on BUY) ──
    if side == "BUY":
        rejection = check_per_trade_limit(copy_size, expected_price, trader)
        if rejection:
            return rejection

        rejection = check_total_spend_limit(session, trader, copy_size, expected_price)
        if rejection:
            return rejection

        rejection = check_max_per_market(session, trader, market, copy_size, expected_price)
        if rejection:
            return rejection

        rejection = check_max_per_yes_no(session, trader, token_id, copy_size, expected_price)
        if rejection:
            return rejection

        rejection = check_position_limit(session, trader, token_id, copy_size, expected_price)
        if rejection:
            return rejection

    # ── Slippage (applies to all) ──
    rejection = check_slippage(best_price, expected_price, trader)
    if rejection:
        return rejection

    return None
