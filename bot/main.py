"""Entry point for the Pcopbot trading daemon."""

import logging
import signal
import time

from db.database import get_session_factory, init_db
from db.models import BotLog, BotSetting, CopyTrade, Position, Trader
from bot import tracker, watermark
from bot.executor import execute_copy_trade
from bot.fill_buffer import FillBuffer
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


def _get_poll_interval(session_factory) -> float:
    """Read poll interval from DB settings, falling back to env/config."""
    try:
        with session_factory() as session:
            row = session.query(BotSetting).filter(BotSetting.key == "poll_interval_seconds").first()
            if row:
                return max(0.1, float(row.value))
    except Exception as exc:
        logger.warning("Failed to read poll_interval from DB: %s", exc)
    return max(0.1, float(settings.POLL_INTERVAL_SECONDS))


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


def _sync_positions(session) -> None:
    """Fetch pre-existing positions for every active trader and save to DB."""
    traders = session.query(Trader).filter(Trader.is_active == True).all()
    for t in traders:
        label = t.label or t.wallet_address[:12]
        try:
            positions = tracker.fetch_positions(t.wallet_address)
        except Exception as exc:
            logger.error("[%s] Error fetching positions: %s", label, exc)
            continue
        # Replace old position rows for this trader
        session.query(Position).filter(Position.trader_id == t.id).delete()
        for p in positions:
            session.add(Position(
                trader_id=t.id,
                condition_id=p["condition_id"],
                asset_id=p["asset_id"],
                market_title=p["market_title"],
                outcome=p["outcome"],
                size=p["size"],
                avg_price=p["avg_price"],
                initial_value=p["initial_value"],
                current_value=p["current_value"],
                pnl=p["pnl"],
                pnl_pct=p["pnl_pct"],
                cur_price=p["cur_price"],
            ))
        session.commit()
        logger.info("[%s] Synced %d pre-existing position(s).", label, len(positions))


def _update_pnl(session) -> None:
    """Update unrealized PnL on open BUY positions using current market prices.

    - BUY trades: unrealized PnL = (cur_price - buy_price) * copy_size.
      Only updated while we still hold shares for that token (net > 0).
      Once fully sold, BUY pnl is set to 0 (the profit was captured
      as realized PnL on the SELL record).
    - SELL trades: pnl is the realized gain/loss, set at execution time
      in executor.py.  NOT updated here.
    """
    from sqlalchemy import func, distinct
    from bot.executor import _get_net_holdings

    # 1. Find all tokens we have open BUY trades on
    open_buys = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .all()
    )
    if not open_buys:
        return

    # 2. Collect unique (trader_id, token_id) pairs and condition_ids for price lookup
    token_traders: dict[tuple[int, str], list[CopyTrade]] = {}
    condition_ids: set[str] = set()
    for ct in open_buys:
        key = (ct.trader_id, ct.original_token_id)
        token_traders.setdefault(key, []).append(ct)
        condition_ids.add(ct.original_market)

    # 3. Fetch current prices from Gamma API
    price_map = tracker.fetch_token_prices(list(condition_ids))
    if not price_map:
        return

    # 4. Update unrealized PnL on BUY records
    updated = 0
    for (trader_id, token_id), buys in token_traders.items():
        cur_price = price_map.get(token_id)
        if cur_price is None:
            continue

        # Check if we still hold shares for this token
        net = _get_net_holdings(session, trader_id, token_id)

        for ct in buys:
            if net > 0:
                # Still holding — compute unrealized PnL
                ct.pnl = round((cur_price - (ct.copy_price or 0)) * ct.copy_size, 4)
            else:
                # Fully sold — unrealized PnL is 0 (realized PnL is on SELL records)
                ct.pnl = 0.0
            updated += 1

    if updated:
        session.commit()
        logger.info("Updated unrealized PnL on %d BUY trade(s).", updated)


def _refresh_copy_trade_fill_prices(session) -> None:
    """Backfill copy_price from our wallet's actual trade activity.

    Polymarket FOK orders are not retained in the CLOB after fill, so
    client.get_order() returns None.  Instead, we query our funder wallet's
    trade activity from the Data API and match each BUY copy trade by
    token_id + execution timestamp (within a 10-minute window).
    """
    # Only refresh fill prices for live trades (dry_run trades use simulated prices)
    # No global gate — individual trades are already filtered by status="success"

    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        return

    buys = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status == "success",
        )
        .all()
    )
    if not buys:
        return

    try:
        our_activity = tracker.fetch_trades(funder, limit=500)
    except Exception as exc:
        logger.warning("Skip fill-price refresh: failed to fetch activity: %s", exc)
        return

    if not our_activity:
        return

    from collections import defaultdict

    def _build_map(side: str) -> dict[str, list]:
        m: dict[str, list] = defaultdict(list)
        for raw in our_activity:
            if (raw.get("side") or "").upper() != side:
                continue
            tid = raw.get("asset") or raw.get("asset_id") or ""
            price = float(raw.get("price", 0) or 0)
            ts = float(raw.get("timestamp", 0) or 0)
            if tid and price > 0 and ts > 0:
                m[tid].append((ts, price))
        return m

    def _match(act_map: dict, ct) -> float | None:
        candidates = act_map.get(ct.original_token_id, [])
        ct_ts = ct.executed_at.timestamp() if ct.executed_at else 0.0
        best_price, best_diff = None, float("inf")
        for ts, price in candidates:
            diff = abs(ts - ct_ts)
            if diff < best_diff:
                best_diff = diff
                best_price = price
        return best_price if best_price is not None and best_diff < 600 else None

    buy_map = _build_map("BUY")
    sell_map = _build_map("SELL")

    # Fix BUY copy_prices
    buy_updated = 0
    for ct in buys:
        if not ct.original_token_id:
            continue
        actual = _match(buy_map, ct)
        if actual is not None and abs((ct.copy_price or 0.0) - actual) > 1e-6:
            ct.copy_price = actual
            buy_updated += 1

    if buy_updated:
        session.commit()
        logger.info("Refreshed copy_price from activity on %d BUY trade(s).", buy_updated)

    # Fix SELL copy_prices
    sells = (
        session.query(CopyTrade)
        .filter(CopyTrade.original_side == "SELL", CopyTrade.status == "success")
        .all()
    )
    sell_updated = 0
    for ct in sells:
        if not ct.original_token_id:
            continue
        actual = _match(sell_map, ct)
        if actual is not None and abs((ct.copy_price or 0.0) - actual) > 1e-6:
            ct.copy_price = actual
            sell_updated += 1

    if sell_updated:
        session.commit()
        logger.info("Refreshed copy_price from activity on %d SELL trade(s).", sell_updated)

    if buy_updated or sell_updated:
        _recalculate_sell_pnl(session)


def _recalculate_sell_pnl(session) -> None:
    """Recompute realized PnL for all SELL trades using corrected BUY copy_prices."""
    from sqlalchemy import func

    sells = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(["success", "dry_run"]),
            CopyTrade.copy_size > 0,
        )
        .all()
    )
    if not sells:
        return

    sell_updated = 0
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
        if abs((ct.pnl or 0.0) - new_pnl) > 1e-6:
            ct.pnl = new_pnl
            sell_updated += 1

    if sell_updated:
        session.commit()
        logger.info("Recalculated realized PnL on %d SELL trade(s).", sell_updated)


def _poll_once(session, fill_buffer: FillBuffer) -> None:
    """One iteration: poll all active traders and copy new trades."""
    from datetime import datetime, timezone

    traders = session.query(Trader).filter(Trader.is_active == True).all()
    if not traders:
        logger.info("No active traders configured — nothing to poll.")
        return

    # Build per-trader window map for buffer expiry
    window_map: dict[int, int] = {}
    for t in traders:
        # Use the larger of buy/sell windows for expiry
        buy_w = getattr(t, "buy_agg_window_seconds", 30) or 0
        sell_w = getattr(t, "sell_agg_window_seconds", 0) or 0
        window_map[t.id] = max(buy_w, sell_w, 30)

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
            # Sell-only mode: skip BUY trades for this trader
            if getattr(t, "sell_only", False) and trade["side"] == "BUY":
                logger.info("[%s] Sell-only mode: skipping BUY trade.", label)
                skip_record = CopyTrade(
                    trader_id=t.id,
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
                    copy_price=trade["price"],
                    status="skipped_sell_only",
                    error_message="Sell-only mode: BUY skipped",
                    executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                session.add(skip_record)
                session.commit()
                watermark.advance_watermark(session, t, trade["timestamp"])
                continue

            # ── Fill aggregation: buffer small fills, execute when threshold crossed ──
            threshold = t.ignore_trades_under or 0.0
            side = trade["side"]
            agg_window = (
                getattr(t, "buy_agg_window_seconds", 30) or 0
                if side == "BUY"
                else getattr(t, "sell_agg_window_seconds", 0) or 0
            )

            if threshold > 0 and agg_window > 0:
                fill_value = trade["size"] * trade["price"]
                if fill_value < threshold:
                    # Sub-threshold fill: try aggregation buffer
                    result = fill_buffer.add_fill(
                        t.id, trade["token_id"], trade, threshold, agg_window,
                    )
                    # Always advance watermark so fill is not re-polled
                    watermark.advance_watermark(session, t, trade["timestamp"])

                    # Update pruned records (sliding window eviction)
                    if result.pruned_record_ids:
                        session.query(CopyTrade).filter(
                            CopyTrade.id.in_(result.pruned_record_ids)
                        ).update(
                            {CopyTrade.status: "below_threshold",
                             CopyTrade.error_message: "Sliding window eviction (fill too old)"},
                            synchronize_session="fetch",
                        )
                        session.commit()

                    if result.action == "execute":
                        logger.info(
                            "[%s] Aggregated %d fills ($%.2f) for %s — executing",
                            label, result.buffered_count, result.total_value, side,
                        )
                        try:
                            copy_trade = execute_copy_trade(session, t, result.aggregated_trade)
                            # Override orig data with triggering fill's individual data
                            copy_trade.original_trade_id = trade["trade_id"]
                            copy_trade.original_size = trade["size"]
                            copy_trade.original_price = trade["price"]
                            copy_trade.original_timestamp = trade["timestamp"].replace(tzinfo=None)
                            session.commit()

                            # Build group annotation, preserving original error
                            prev_ids = list(result.buffered_record_ids)
                            all_ids = prev_ids + [copy_trade.id]
                            vwap = result.aggregated_trade["price"]
                            agg_note = (
                                f"Agg group [{', '.join(f'#{i}' for i in all_ids)}]: "
                                f"{result.buffered_count} fills, VWAP ${vwap:.4f}, "
                                f"total ${result.total_value:.2f}"
                            )
                            # Preserve the original error from execute_copy_trade
                            orig_error = copy_trade.error_message or ""
                            if orig_error:
                                copy_trade.error_message = f"{orig_error} | {agg_note}"
                            else:
                                copy_trade.error_message = agg_note
                            # Update all previous buffered records with status + note
                            prev_note = f"{orig_error} | {agg_note}" if orig_error else agg_note
                            if prev_ids:
                                session.query(CopyTrade).filter(
                                    CopyTrade.id.in_(prev_ids)
                                ).update(
                                    {CopyTrade.status: copy_trade.status,
                                     CopyTrade.error_message: prev_note},
                                    synchronize_session="fetch",
                                )
                            session.commit()
                        except Exception as exc:
                            logger.error("[%s] Error executing aggregated trade: %s", label, exc)
                    elif result.action == "buffered":
                        # Record individual fill immediately
                        buffered_record = CopyTrade(
                            trader_id=t.id,
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
                            copy_price=0.0,
                            status="buffered",
                            error_message=f"Buffering: {result.buffered_count} fills, ${result.total_value:.2f} / ${threshold:.2f}",
                            agg_fill_count=result.buffered_count,
                            agg_total_value=round(result.total_value, 4),
                            executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        )
                        session.add(buffered_record)
                        session.commit()
                        # Store record ID on the trade dict so buffer can retrieve it later
                        trade["_record_id"] = buffered_record.id
                        logger.info(
                            "[%s] Buffered %s fill $%.2f (total $%.2f / $%.2f, %d fills) → record #%d",
                            label, side, fill_value, result.total_value,
                            threshold, result.buffered_count, buffered_record.id,
                        )
                    # action == "immediate" shouldn't happen here (fill_value < threshold)
                    continue

            # Single fill >= threshold, no aggregation, or aggregation disabled: execute immediately
            try:
                execute_copy_trade(session, t, trade)
            except Exception as exc:
                logger.error(
                    "[%s] Unexpected error executing copy trade: %s",
                    label,
                    exc,
                )
            watermark.advance_watermark(session, t, trade["timestamp"])

    # Flush expired aggregation buffers — update existing records to below_threshold
    expired = fill_buffer.flush_expired(
        datetime.now(timezone.utc), window_seconds_map=window_map,
    )
    for trader_id, token_id, entry in expired:
        record_ids = list(entry.collect_record_ids())
        note = (
            f"Aggregation expired: {len(entry.fills)} fills totaling "
            f"${entry.total_value:.2f} (threshold not reached)"
        )
        if record_ids:
            session.query(CopyTrade).filter(
                CopyTrade.id.in_(record_ids)
            ).update(
                {CopyTrade.status: "below_threshold",
                 CopyTrade.error_message: note},
                synchronize_session="fetch",
            )
            session.commit()


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
        _sync_positions(session)

    _poll_count = 0
    fill_buffer = FillBuffer()

    while _running:
        poll_interval = _get_poll_interval(SessionLocal)
        logger.debug("Poll interval: %ss", poll_interval)
        with SessionLocal() as session:
            try:
                _poll_once(session, fill_buffer)
            except Exception as exc:
                logger.error("Unhandled error in poll loop: %s", exc)

            # Sync positions & update PnL every 10 poll cycles
            _poll_count += 1
            if _poll_count % 10 == 0:
                try:
                    _refresh_copy_trade_fill_prices(session)
                    _sync_positions(session)
                    _update_pnl(session)
                except Exception as exc:
                    logger.error("Error syncing positions/PnL: %s", exc)

            # Auto-sell positions at threshold price every poll cycle
            # Per-trader dry_run filtering is handled inside auto_sell_winning_positions
            # (only live trades with status="success" are considered)
            auto_sell_on = True
            try:
                row = session.query(BotSetting).filter(BotSetting.key == "auto_sell_enabled").first()
                if row and row.value.lower() in ("false", "0", "no"):
                    auto_sell_on = False
            except Exception:
                pass
            if auto_sell_on:
                try:
                    from bot.executor import auto_sell_winning_positions
                    sold = auto_sell_winning_positions(session)
                    if sold:
                        logger.info("Auto-sold %d winning position(s) at threshold.", sold)
                except Exception as exc:
                    logger.error("Error during auto-sell: %s", exc)

            # Auto-redeem resolved winning positions every 20 poll cycles
            if _poll_count % 20 == 0:
                from bot.redeemer import (
                    redeem_resolved_positions,
                    detect_manual_redemptions,
                    detect_manual_sells,
                    detect_expired_losses,
                )
                try:
                    redeemed = redeem_resolved_positions(session)
                    if redeemed:
                        logger.info("Auto-redeemed %d resolved position(s).", redeemed)
                except Exception as exc:
                    logger.error("Error during auto-redemption: %s", exc)

                try:
                    manual = detect_manual_redemptions(session)
                    if manual:
                        logger.info("Recorded %d manual redemption(s) from funder wallet.", manual)
                except Exception as exc:
                    logger.error("Error detecting manual redemptions: %s", exc)

                try:
                    manual_sells = detect_manual_sells(session)
                    if manual_sells:
                        logger.info("Recorded %d manual sell(s) from funder wallet.", manual_sells)
                except Exception as exc:
                    logger.error("Error detecting manual sells: %s", exc)

                try:
                    expired = detect_expired_losses(session)
                    if expired:
                        logger.info("Recorded %d expired losing position(s).", expired)
                except Exception as exc:
                    logger.error("Error detecting expired losses: %s", exc)

        logger.debug("Sleeping %s seconds…", poll_interval)
        time.sleep(poll_interval)

    logger.info("Pcopbot stopped.")


if __name__ == "__main__":
    run()
