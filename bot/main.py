"""Entry point for the Pcopbot trading daemon."""

import logging
import signal
import time

from db.database import get_session_factory, init_db
from db.models import BotLog, BotSetting, Trader
from bot import tracker, watermark
from bot.executor import execute_copy_trade
from config import settings


class _DBLogHandler(logging.Handler):
    """Logging handler that writes log records into the bot_logs table."""

    def __init__(self, session_factory):
        super().__init__()
        self._session_factory = session_factory

    def emit(self, record):
        try:
            with self._session_factory() as session:
                entry = BotLog(
                    level=record.levelname,
                    logger_name=record.name,
                    message=self.format(record),
                )
                session.add(entry)
                session.commit()
        except Exception:
            self.handleError(record)


def _get_poll_interval(session_factory) -> int:
    """Read poll interval from DB settings, falling back to env/config."""
    try:
        with session_factory() as session:
            row = session.query(BotSetting).filter(BotSetting.key == "poll_interval_seconds").first()
            if row:
                return max(1, int(row.value))
    except Exception:
        pass
    return settings.POLL_INTERVAL_SECONDS


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
    if not traders:
        logger.info("No active traders configured — nothing to poll.")
        return

    for t in traders:
        label = t.label or t.wallet_address[:12]
        if t.watermark_timestamp is None:
            watermark.set_watermark(session, t)
            logger.info("[%s] Watermark initialised — will start polling next cycle.", label)
            continue
        try:
            logger.info("[%s] Polling for new trades (watermark=%s)…", label, t.watermark_timestamp.isoformat())
            new_trades = tracker.get_new_trades(t.wallet_address, t.watermark_timestamp)
        except Exception as exc:
            logger.error("[%s] Error fetching trades: %s", label, exc)
            continue

        if not new_trades:
            logger.info("[%s] No new trades found.", label)
        else:
            logger.info("[%s] Found %d new trade(s).", label, len(new_trades))

        for trade in new_trades:
            try:
                execute_copy_trade(session, t, trade)
            except Exception as exc:
                logger.error(
                    "[%s] Unexpected error executing copy trade: %s",
                    label,
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

    # Attach DB log handler so logs are visible in the dashboard
    db_handler = _DBLogHandler(SessionLocal)
    db_handler.setLevel(logging.INFO)
    db_handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(db_handler)

    with SessionLocal() as session:
        _init_watermarks(session)

    while _running:
        poll_interval = _get_poll_interval(SessionLocal)
        logger.debug("Poll interval: %ss", poll_interval)
        with SessionLocal() as session:
            try:
                _poll_once(session)
            except Exception as exc:
                logger.error("Unhandled error in poll loop: %s", exc)
        logger.debug("Sleeping %s seconds…", poll_interval)
        time.sleep(poll_interval)

    logger.info("Pcopbot stopped.")


if __name__ == "__main__":
    run()
