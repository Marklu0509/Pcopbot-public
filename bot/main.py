"""Entry point for the Pcopbot trading daemon."""

import logging
import signal
import time

from db.database import get_session_factory, init_db
from db.models import Trader
from bot import tracker, watermark
from bot.executor import execute_copy_trade
from config import settings

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_running = True


def _handle_signal(signum, frame):  # pragma: no cover
    global _running
    logger.info("Received signal %s — shutting down gracefully…", signum)
    _running = False


def _init_watermarks(session) -> None:
    """Set watermarks for any active traders that don't have one yet."""
    traders = session.query(Trader).filter(Trader.is_active == True).all()
    for t in traders:
        if t.watermark_timestamp is None:
            watermark.set_watermark(session, t)


def _poll_once(session) -> None:
    """One iteration: poll all active traders and copy new trades."""
    traders = session.query(Trader).filter(Trader.is_active == True).all()
    for t in traders:
        if t.watermark_timestamp is None:
            watermark.set_watermark(session, t)
            continue  # Skip this round — watermark just set
        try:
            new_trades = tracker.get_new_trades(t.wallet_address, t.watermark_timestamp)
        except Exception as exc:
            logger.error("Error fetching trades for %s: %s", t.wallet_address, exc)
            continue

        for trade in new_trades:
            try:
                execute_copy_trade(session, t, trade)
            except Exception as exc:
                logger.error(
                    "Unexpected error executing copy trade for %s: %s",
                    t.wallet_address,
                    exc,
                )
            # Advance watermark regardless of execution outcome
            watermark.advance_watermark(session, t, trade["timestamp"])


def run() -> None:
    """Main daemon loop."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Pcopbot starting (DRY_RUN=%s, POLL_INTERVAL=%ss)", settings.DRY_RUN, settings.POLL_INTERVAL_SECONDS)
    init_db()

    SessionLocal = get_session_factory()

    with SessionLocal() as session:
        _init_watermarks(session)

    while _running:
        with SessionLocal() as session:
            try:
                _poll_once(session)
            except Exception as exc:
                logger.error("Unhandled error in poll loop: %s", exc)
        logger.debug("Sleeping %s seconds…", settings.POLL_INTERVAL_SECONDS)
        time.sleep(settings.POLL_INTERVAL_SECONDS)

    logger.info("Pcopbot stopped.")


if __name__ == "__main__":
    run()
