"""Watermarking logic — records startup timestamp per trader and filters stale trades."""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from db.models import Trader

logger = logging.getLogger(__name__)


def set_watermark(session: Session, trader: Trader, ts: datetime | None = None) -> None:
    """Set (or reset) the watermark timestamp for a trader.

    Args:
        session: Active SQLAlchemy session.
        trader:  Trader ORM object.
        ts:      Timestamp to use as watermark. Defaults to now (UTC).
    """
    if ts is None:
        ts = datetime.now(timezone.utc).replace(tzinfo=None)
    trader.watermark_timestamp = ts
    session.commit()
    logger.info("Watermark set for trader %s: %s", trader.wallet_address, ts.isoformat())


def is_new_trade(trader: Trader, trade_timestamp: datetime) -> bool:
    """Return True if the trade occurred STRICTLY AFTER the trader's watermark.

    Args:
        trader:          Trader ORM object (must have watermark_timestamp set).
        trade_timestamp: UTC datetime of the trade to evaluate.
    """
    if trader.watermark_timestamp is None:
        # No watermark set — treat every trade as new (edge case)
        return True
    # Strip tz info for comparison (DB stores naive UTC datetimes)
    trade_ts = trade_timestamp.replace(tzinfo=None) if trade_timestamp.tzinfo else trade_timestamp
    return trade_ts > trader.watermark_timestamp


def advance_watermark(session: Session, trader: Trader, trade_timestamp: datetime) -> None:
    """Advance the watermark to the given trade's timestamp if it's newer.

    This prevents re-processing the same trade on the next poll.
    """
    trade_ts = trade_timestamp.replace(tzinfo=None) if trade_timestamp.tzinfo else trade_timestamp
    if trader.watermark_timestamp is None or trade_ts > trader.watermark_timestamp:
        trader.watermark_timestamp = trade_ts
        session.commit()
        logger.debug(
            "Watermark advanced for trader %s to %s",
            trader.wallet_address,
            trade_ts.isoformat(),
        )
