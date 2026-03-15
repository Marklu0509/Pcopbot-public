"""Executes copy trades via py_clob_client (or logs them in dry-run mode)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from bot import risk, watermark
from config import settings
from db.models import CopyTrade, Trader

logger = logging.getLogger(__name__)

# If a SELL would leave less than this USD value, close out the full position.
SELL_DUST_CLOSEOUT_USD = 1.0

# Cooldown between FOK sell attempts per token (seconds).
# Prevents spamming the CLOB API when price is near but below threshold.
_AUTO_SELL_COOLDOWN = 30
_auto_sell_last_attempt: dict[str, float] = {}


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


def _get_avg_buy_price(session: Session, trader_id: int, token_id: str) -> float:
    """Return the weighted-average buy price for a trader+token.

    Weighted average = SUM(copy_size * copy_price) / SUM(copy_size) across
    all successful/dry_run BUY trades for that token.
    """
    from sqlalchemy import func
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
    if total_size > 0:
        return total_cost / total_size
    return 0.0


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
    """Lazily import and construct the CLOB client with Level 2 auth."""
    try:
        from py_clob_client.client import ClobClient  # type: ignore
        from py_clob_client.clob_types import ApiCreds  # type: ignore

        private_key = (settings.POLYMARKET_PRIVATE_KEY or "").strip()
        funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
        if not private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY is empty")
        if not funder:
            raise ValueError("POLYMARKET_FUNDER_ADDRESS is empty")

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

        # First, try env-provided API creds (if complete).
        if env_api_key and env_api_secret and env_api_passphrase:
            env_creds = ApiCreds(
                api_key=env_api_key,
                api_secret=env_api_secret,
                api_passphrase=env_api_passphrase,
            )
            client.set_api_creds(env_creds)
            try:
                client.get_api_keys()
                return client
            except Exception as exc:
                logger.warning("Env API creds rejected, falling back to derived creds: %s", exc)

        # Fallback: derive/create API creds from private key + funder.
        derived = client.create_or_derive_api_creds()
        client.set_api_creds(derived)
        return client
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


def _wait_for_fill(client, order_id: str, timeout: int) -> bool:
    """Poll order status until filled or timeout (seconds).

    Returns True if the order was fully filled, False otherwise.
    """
    deadline = time.monotonic() + timeout
    poll_interval = min(3, max(1, timeout // 10))
    while time.monotonic() < deadline:
        try:
            order = client.get_order(order_id)
            raw_status = ""
            if isinstance(order, dict):
                raw_status = order.get("status", "").upper()
            else:
                raw_status = getattr(order, "status", "").upper()
            if raw_status in ("FILLED", "MATCHED"):
                return True
            if raw_status in ("CANCELLED", "EXPIRED", "REJECTED"):
                return False
        except Exception as exc:
            logger.warning("Error polling order %s: %s", order_id, exc)
        time.sleep(poll_interval)
    return False


def _to_float_or_none(value) -> float | None:
    """Best-effort float conversion for values returned by API payloads."""
    try:
        if value is None:
            return None
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _get_filled_price(client, order_id: str, fallback_price: float) -> float:
    """Fetch actual filled/average price from order details.

    Returns fallback_price when order details are unavailable or don't contain
    a usable fill/average price.
    """
    if not order_id:
        return fallback_price
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
        logger.warning("Could not fetch filled price for order %s: %s", order_id, exc)

    return fallback_price


def auto_sell_winning_positions(session: Session, threshold: float | None = None) -> int:
    """Sell open positions when price >= threshold.

    Four trigger sources (any one suffices):
    1. CLOB orderbook best-bid >= threshold (direct bids)
    2. Complement price >= threshold: 1 - best_ask(complement token)
       This is how Polymarket UI calculates sell price for binary markets.
    3. Gamma outcomePrices >= threshold
    4. Data API funder position curPrice >= threshold

    Source #2 is the most reliable — it matches the Polymarket UI sell price.
    In binary markets, selling Yes@0.999 is matched via BUY No@0.001.

    Returns count of positions successfully sold.
    """
    from datetime import datetime, timezone

    if threshold is None:
        threshold = settings.AUTO_SELL_THRESHOLD
    if threshold <= 0 or settings.DRY_RUN:
        return 0

    open_buys = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .all()
    )
    if not open_buys:
        return 0

    # Group by (trader_id, token_id)
    token_trader_map: dict[tuple[int, str], list[CopyTrade]] = {}
    for ct in open_buys:
        if ct.original_token_id:
            key = (ct.trader_id, ct.original_token_id)
            token_trader_map.setdefault(key, []).append(ct)

    try:
        client = _get_clob_client()
    except Exception as exc:
        logger.error("auto_sell: failed to get CLOB client: %s", exc)
        return 0

    # Batch-fetch Gamma prices for all unique token_ids (one API call)
    unique_token_ids = list({tid for (_, tid) in token_trader_map})
    try:
        from bot.tracker import fetch_prices_by_token_ids
        gamma_prices = fetch_prices_by_token_ids(unique_token_ids)
    except Exception as exc:
        logger.warning("auto_sell: Gamma price fetch failed: %s", exc)
        gamma_prices = {}

    # Fetch funder wallet position prices (Data API curPrice).
    funder_prices: dict[str, float] = {}
    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if funder:
        try:
            from bot.tracker import fetch_position_prices
            funder_prices = fetch_position_prices(funder)
        except Exception as exc:
            logger.warning("auto_sell: funder position price fetch failed: %s", exc)
            funder_prices = {}

    # Fetch complement token IDs for binary markets so we can compute
    # effective sell price = 1 - best_ask(complement).
    # This is the most reliable method: it's exactly how Polymarket UI
    # calculates the sell price.
    # Collect condition_ids from buys for CLOB API fallback
    unique_condition_ids = list({
        ct.original_market for cts in token_trader_map.values()
        for ct in cts if ct.original_market
    })
    complement_map: dict[str, str] = {}
    try:
        from bot.tracker import fetch_complement_token_ids
        complement_map = fetch_complement_token_ids(unique_token_ids, unique_condition_ids)
    except Exception as exc:
        logger.warning("auto_sell: complement token fetch failed: %s", exc)

    sold = 0
    for (trader_id, token_id), buys in token_trader_map.items():
        net_shares = _get_net_holdings(session, trader_id, token_id)
        if net_shares <= 0:
            continue

        best_bid = _get_best_price(client, token_id, "SELL")
        gamma_price = gamma_prices.get(token_id, 0.0)
        funder_price = funder_prices.get(token_id, 0.0)

        # Compute complement-matched effective price:
        # Selling Yes = matching with BUY No. Effective price = 1 - best_ask(No).
        complement_price = 0.0
        comp_token = complement_map.get(token_id)
        if comp_token:
            comp_best_ask = _get_best_price(client, comp_token, "BUY")
            if comp_best_ask is not None and comp_best_ask > 0:
                complement_price = round(1.0 - comp_best_ask, 4)

        # Best observed price from all sources
        effective = max(gamma_price, funder_price, complement_price, best_bid or 0.0)

        # Strategy: CLOB handles complement matching transparently.
        # FOK orders are free (off-chain, auto-cancel if no fill).
        # When price is close (>= ATTEMPT_FLOOR), attempt FOK SELL at
        # threshold price — CLOB may fill via complement matching even
        # though the visible orderbook shows a lower price.
        attempt_floor = max(threshold - 0.05, 0.90)

        if effective < attempt_floor:
            continue  # Price too far from threshold, skip silently

        # Cooldown: don't spam FOK attempts every cycle when price < threshold
        if effective < threshold:
            now_ts = time.monotonic()
            last = _auto_sell_last_attempt.get(token_id, 0.0)
            if now_ts - last < _AUTO_SELL_COOLDOWN:
                continue  # Still in cooldown, skip
            _auto_sell_last_attempt[token_id] = now_ts
            logger.info(
                "auto_sell ATTEMPT: token=%s effective=%.4f < threshold=%.4f, "
                "trying FOK at %.4f (CLOB_bid=%s Comp=%.4f Gamma=%.4f Funder=%.4f)",
                token_id[:16], effective, threshold, threshold,
                f"{best_bid:.4f}" if best_bid is not None else "None",
                complement_price, gamma_price, funder_price,
            )

        # Always sell at threshold price — let CLOB find the best match
        sell_price = threshold
        sample = buys[0]
        avg_buy = _get_avg_buy_price(session, trader_id, token_id)
        pnl = round((sell_price - avg_buy) * net_shares, 4)

        logger.info(
            "auto_sell: token=%s net=%.4f sell_price=%.4f CLOB_bid=%s Comp=%.4f "
            "Gamma=%.4f Funder=%.4f threshold=%.4f avg_buy=%.4f",
            token_id[:16], net_shares, sell_price,
            f"{best_bid:.4f}" if best_bid is not None else "None",
            complement_price, gamma_price, funder_price, threshold, avg_buy,
        )

        order_id: str | None = None
        status = "failed"
        error_msg: str | None = None
        recorded_price = sell_price

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
            from py_clob_client.order_builder.constants import SELL as _SELL  # type: ignore

            order_args = OrderArgs(
                token_id=token_id,
                price=round(sell_price, 4),
                size=round(net_shares, 4),
                side=_SELL,
            )
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, OrderType.FOK)
            order_id = str(resp.get("orderID") or resp.get("order_id") or "")
            recorded_price = _get_filled_price(client, order_id or "", sell_price)
            pnl = round((recorded_price - avg_buy) * net_shares, 4)
            status = "success"
            logger.info(
                "auto_sell SUCCESS: token=%s size=%.4f filled_price=%.4f pnl=%.4f order_id=%s",
                token_id[:16], net_shares, recorded_price, pnl, order_id,
            )
        except Exception as exc:
            status = "failed"
            error_msg = str(exc)
            # FOK not filling is expected when attempting below visible price.
            # Only log at INFO to avoid noisy error logs on every cycle.
            logger.info(
                "auto_sell FOK not filled for token=%s (will retry next cycle): %s",
                token_id[:16], exc,
            )

        if status != "success":
            # Don't persist failed attempts — they pollute trade history
            # and create spurious records on retries.
            continue

        sell_record = CopyTrade(
            trader_id=trader_id,
            original_trade_id=f"auto_sell:{token_id[:24]}",
            original_market=sample.original_market,
            original_token_id=token_id,
            market_title=sample.market_title,
            outcome=sample.outcome,
            original_side="SELL",
            original_size=net_shares,
            original_price=sell_price,
            original_timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            copy_size=net_shares,
            copy_price=recorded_price,
            status="success",
            order_id=order_id,
            pnl=pnl,
            executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.add(sell_record)
        session.commit()
        sold += 1

    return sold


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

        remaining_size = max(holdings - copy_size, 0.0)
        remaining_value = remaining_size * expected_price
        if remaining_size > 0 and remaining_value < SELL_DUST_CLOSEOUT_USD:
            logger.info(
                "SELL closeout for trader %s token %s: remaining value $%.4f < $%.2f, "
                "selling full holdings %.4f",
                trader.wallet_address,
                trade["token_id"],
                remaining_value,
                SELL_DUST_CLOSEOUT_USD,
                holdings,
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
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, ot)
            order_id = str(resp.get("orderID") or resp.get("order_id") or "")

            if ot == OrderType.GTC and order_id:
                # Poll for GTC fill, cancel + fallback if timeout
                timeout = getattr(trader, "limit_timeout_seconds", 30) or 30
                fallback = getattr(trader, "limit_fallback_market", True)
                filled = _wait_for_fill(client, order_id, timeout)
                if filled:
                    status = "success"
                else:
                    # Cancel the unfilled GTC order
                    try:
                        client.cancel(order_id)
                        logger.info("Cancelled unfilled GTC order %s", order_id)
                    except Exception as cancel_exc:
                        logger.warning("Failed to cancel GTC order %s: %s", order_id, cancel_exc)

                    if fallback:
                        logger.info("Falling back to FOK market order for trader %s", trader.wallet_address)
                        try:
                            fok_args = OrderArgs(
                                token_id=trade["token_id"],
                                price=round(order_price, 4),
                                size=round(copy_size, 4),
                                side=side,
                            )
                            signed_fok = client.create_order(fok_args)
                            fok_resp = client.post_order(signed_fok, OrderType.FOK)
                            order_id = str(fok_resp.get("orderID") or fok_resp.get("order_id") or "")
                            status = "success"
                            logger.info("FOK fallback succeeded: order_id=%s", order_id)
                        except Exception as fok_exc:
                            status = "failed"
                            error_msg = f"GTC timeout + FOK fallback failed: {fok_exc}"
                            logger.error("FOK fallback FAILED: %s", fok_exc)
                    else:
                        status = "failed"
                        error_msg = f"GTC order not filled within {timeout}s (no fallback)"
            else:
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

    # copy_price policy:
    # - dry run: expected_price (simulated fill)
    # - live success: actual filled/average price from order details
    # - fallback: order_price when actual fill price is unavailable
    if settings.DRY_RUN:
        recorded_price = expected_price
    elif status == "success":
        recorded_price = _get_filled_price(client, order_id or "", order_price)
    else:
        recorded_price = order_price

    # Calculate realized PnL for SELL trades at execution time
    realized_pnl = 0.0
    if trade["side"] == "SELL" and status in ("success", "dry_run") and copy_size > 0:
        avg_buy = _get_avg_buy_price(session, trader.id, trade["token_id"])
        realized_pnl = round((recorded_price - avg_buy) * copy_size, 4)

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
        pnl=realized_pnl,
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(copy_trade)
    session.commit()
    return copy_trade
