"""Executes copy trades via py_clob_client (or logs them in dry-run mode)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from math import floor as _floor
from typing import Any

from sqlalchemy.orm import Session

from bot import risk, watermark
from config import settings
from db.models import BotSetting, CopyTrade, Trader

logger = logging.getLogger(__name__)


def is_trader_dry_run(trader: Trader, session: Session) -> bool:
    """Resolve effective dry_run mode for a trader.

    Priority: global DB override > env override > per-trader setting.
    """
    # 1. Check global override from bot_settings table
    try:
        row = session.query(BotSetting).filter(BotSetting.key == "dry_run").first()
        if row and row.value.lower() in ("true", "1", "yes"):
            return True
    except Exception:
        pass

    # 2. Check env override
    if settings.DRY_RUN:
        return True

    # 3. Per-trader setting (default True if column is None)
    trader_dry_run = getattr(trader, "dry_run", None)
    if trader_dry_run is None:
        return True
    return bool(trader_dry_run)

# If a SELL would leave less than this USD value, close out the full position.
SELL_DUST_CLOSEOUT_USD = 1.5

# Residual positions below this share count are auto-cleared from DB.
DUST_CLEAR_THRESHOLD_SHARES = 0.1

# Polymarket contract addresses on Polygon (chain_id=137).
_CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # ConditionalTokens (ERC-1155)
_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Regular
_NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # NegRiskAdapter

# Minimal ABI for ERC-1155 approval check + set
_ERC1155_APPROVAL_ABI = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# Cache: set of exchange addresses already approved this session.
_approved_exchanges: set[str] = set()

# Cooldown between FOK sell attempts per token (seconds).
# Prevents spamming the CLOB API when price is near but below threshold.
_AUTO_SELL_COOLDOWN = 30
_auto_sell_last_attempt: dict[str, float] = {}


def _ensure_sell_approval(client, token_id: str) -> None:
    """Ensure the CLOB recognises our token balance and allowance.

    Strategy 1 (always): Call CLOB API update_balance_allowance to refresh
    the server's cached view of our on-chain balance/allowance.

    Strategy 2 (only if POLYMARKET_FUNDER_PRIVATE_KEY is set): Send an
    on-chain setApprovalForAll tx.  This only works when we have the
    funder wallet's own private key (EOA).  In proxy-wallet (Gnosis Safe)
    setups the proxy key cannot sign on-chain txs for the Safe.
    """
    # --- Strategy 1: CLOB API refresh (works for all wallet types) ---
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType  # type: ignore
        client.update_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        logger.debug("Refreshed CLOB balance/allowance for token=%s", token_id[:16])
    except Exception as exc:
        logger.warning("update_balance_allowance failed for token=%s: %s", token_id[:16], exc)

    # --- Strategy 2: On-chain approval (only with explicit funder key) ---
    funder_key = (settings.POLYMARKET_FUNDER_PRIVATE_KEY or "").strip()
    if not funder_key:
        return  # Proxy-wallet setup — can't sign on-chain txs

    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        return

    from web3 import Web3

    # Check both exchange contracts
    exchanges_to_check = [_EXCHANGE_ADDRESS, _NEG_RISK_EXCHANGE]
    rpc_url = settings.POLYGON_RPC_URL or "https://polygon-rpc.com"
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(_CTF_ADDRESS),
        abi=_ERC1155_APPROVAL_ABI,
    )
    funder_cs = Web3.to_checksum_address(funder)

    for exchange_addr in exchanges_to_check:
        if exchange_addr in _approved_exchanges:
            continue

        exchange_cs = Web3.to_checksum_address(exchange_addr)
        try:
            approved = ctf.functions.isApprovedForAll(funder_cs, exchange_cs).call()
        except Exception as exc:
            logger.warning("Failed to check approval for %s: %s", exchange_addr[:10], exc)
            continue

        if approved:
            _approved_exchanges.add(exchange_addr)
            continue

        logger.info(
            "Setting ERC-1155 approval for Exchange %s (funder=%s)…",
            exchange_addr[:10], funder[:10],
        )
        try:
            tx = ctf.functions.setApprovalForAll(exchange_cs, True).build_transaction({
                "from": funder_cs,
                "nonce": w3.eth.get_transaction_count(funder_cs),
                "gas": 100_000,
                "gasPrice": w3.eth.gas_price,
                "chainId": settings.POLYMARKET_CHAIN_ID,
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=funder_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt["status"] == 1:
                _approved_exchanges.add(exchange_addr)
                logger.info("Approval tx confirmed: %s (exchange=%s)", tx_hash.hex(), exchange_addr[:10])
            else:
                logger.error("Approval tx reverted: %s", tx_hash.hex())
        except Exception as exc:
            logger.error("Failed to send approval tx for %s: %s", exchange_addr[:10], exc)


def _get_net_holdings(
    session: Session,
    trader_id: int,
    token_id: str,
    status_filter: list[str] | None = None,
) -> float:
    """Return net share holdings for a trader+token.

    status_filter controls which trade statuses to count.
    Defaults to ["success", "dry_run"] for backward compatibility.
    Pass ["success"] for live-only or ["dry_run"] for dry-run-only.
    """
    if status_filter is None:
        status_filter = ["success", "dry_run"]
    from sqlalchemy import func
    buy_total = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size), 0.0))
        .filter(
            CopyTrade.trader_id == trader_id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(status_filter),
        )
        .scalar()
    )
    sell_total = (
        session.query(func.coalesce(func.sum(CopyTrade.copy_size), 0.0))
        .filter(
            CopyTrade.trader_id == trader_id,
            CopyTrade.original_token_id == token_id,
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(status_filter),
        )
        .scalar()
    )
    return max(buy_total - sell_total, 0.0)


def _get_avg_buy_price(
    session: Session,
    trader_id: int,
    token_id: str,
    status_filter: list[str] | None = None,
) -> float:
    """Return the weighted-average buy price for a trader+token.

    Weighted average = SUM(copy_size * copy_price) / SUM(copy_size) across
    BUY trades matching the given status_filter.
    """
    if status_filter is None:
        status_filter = ["success", "dry_run"]
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
            CopyTrade.status.in_(status_filter),
        )
        .first()
    )
    total_cost, total_size = result
    if total_size > 0:
        return total_cost / total_size
    return 0.0


def _snap_size_2dp(size: float) -> float:
    """Floor size to 2 decimal places (CLOB taker_amount precision for limit orders and sells)."""
    return _floor(size * 100) / 100 if size > 0 else 0.0


# Backward-compatible alias
_snap_sell_size = _snap_size_2dp


def _calculate_copy_size(trader: Trader, original_size: float, price: float) -> float:
    """Determine the copy trade size (in shares) based on the trader's sizing mode.

    Fixed mode:  user sets a dollar budget → convert to shares (budget / price).
    Proportional mode: percentage of the original trade's share count.
    """
    if trader.sizing_mode == "proportional":
        raw = original_size * (trader.proportional_pct / 100.0)
    elif price > 0:
        raw = trader.fixed_amount / price
    else:
        raw = trader.fixed_amount
    return raw


def _calculate_buy_amount(trader: Trader, original_size: float, price: float) -> float:
    """Calculate USDC amount for a BUY order (used with MarketOrderArgs).

    Fixed mode:  use the dollar budget directly.
    Proportional mode: match the original trade's dollar value proportionally.
    """
    if trader.sizing_mode == "proportional":
        raw = original_size * price * (trader.proportional_pct / 100.0)
    else:
        raw = trader.fixed_amount
    # Floor to 2 decimals — py_clob_client will also round_down to 2dp
    return _floor(raw * 100) / 100


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


def _compute_limit_price(trader_price: float, side: str, offset_pct: float) -> float:
    """Compute limit order price based on trader's fill price + offset.

    BUY:  trader_price * (1 + offset_pct / 100) — ceiling, CLOB fills at best ask
    SELL: trader_price * (1 - offset_pct / 100) — floor, CLOB fills at best bid
    Result is clamped to [0.01, 0.99] (Polymarket price bounds).
    """
    if side == "BUY":
        return min(max(trader_price * (1 + offset_pct / 100.0), 0.01), 0.99)
    return max(min(trader_price * (1 - offset_pct / 100.0), 0.99), 0.01)


def _extract_price(entry) -> float:
    """Extract the price from an orderbook entry (dict or object)."""
    if isinstance(entry, dict):
        return float(entry.get("price", 0))
    return float(getattr(entry, "price", 0))


# Cache token_ids whose orderbook returned 404 (market delisted/resolved).
# Cleared on bot restart. Avoids hammering the API for dead markets.
_orderbook_404_cache: set[str] = set()


def _get_best_price(client, token_id: str, side: str) -> float | None:
    """Query the CLOB orderbook for the current best available price.

    Returns the best ask (for BUY) or best bid (for SELL), or None on failure.
    Caches 404 responses to avoid repeated queries for delisted markets.
    """
    if token_id in _orderbook_404_cache:
        return None
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
        if "404" in str(exc) or "No orderbook" in str(exc):
            _orderbook_404_cache.add(token_id)
            logger.debug("Orderbook 404 cached for %s (delisted market)", token_id[:16])
        else:
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


def _get_filled_price(
    client,
    order_id: str,
    fallback_price: float,
    *,
    token_id: str = "",
    side: str = "",
) -> float:
    """Fetch actual filled/average price from order details.

    Lookup chain:
    1. CLOB ``get_order`` (works for GTC, usually gone for FOK)
    2. Data API ``/activity`` on the funder wallet (matches by token_id +
       side within a 60-second window of *now*)
    3. *fallback_price* (caller should pass ``expected_price``, NOT the
       slippage-adjusted order price)
    """
    # --- 1. CLOB get_order ---
    if order_id:
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

    # --- 2. Data API activity fallback ---
    if token_id and side:
        filled = _query_activity_fill_price(token_id, side)
        if filled is not None:
            return filled

    return fallback_price


def _query_activity_fill_price(
    token_id: str, side: str, window_seconds: int = 60
) -> float | None:
    """Query funder wallet activity for a recent fill matching *token_id* + *side*.

    Returns the price of the closest matching trade within *window_seconds*
    of now, or ``None`` if no match is found.
    """
    from bot import tracker

    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        return None

    try:
        activity = tracker.fetch_trades(funder, limit=50)
    except Exception as exc:
        logger.warning("Activity fill-price query failed: %s", exc)
        return None

    now_ts = datetime.now(timezone.utc).timestamp()
    best_price: float | None = None
    best_diff = float("inf")

    for raw in activity:
        if (raw.get("side") or "").upper() != side.upper():
            continue
        tid = raw.get("asset") or raw.get("asset_id") or ""
        if tid != token_id:
            continue
        price = float(raw.get("price", 0) or 0)
        ts = float(raw.get("timestamp", 0) or 0)
        if price <= 0 or ts <= 0:
            continue
        diff = abs(now_ts - ts)
        if diff < best_diff and diff <= window_seconds:
            best_diff = diff
            best_price = price

    if best_price is not None:
        logger.info(
            "Resolved fill price from Data API activity: token=%s side=%s price=%.4f",
            token_id[:16], side, best_price,
        )
    return best_price


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
    if threshold <= 0:
        return 0

    # Fetch both live and dry_run BUY trades
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

    # Fetch funder wallet's ACTUAL positions (Data API).
    # This is the source of truth for what shares we hold — covers both
    # bot-copied trades AND pre-existing/manually-bought positions.
    funder_prices: dict[str, float] = {}
    funder_sizes: dict[str, float] = {}
    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if funder:
        try:
            from bot.tracker import fetch_positions
            wallet_positions = fetch_positions(funder)
            for p in wallet_positions:
                tid = p.get("asset_id", "")
                if tid:
                    funder_prices[tid] = p.get("cur_price", 0.0)
                    funder_sizes[tid] = p.get("size", 0.0)
        except Exception as exc:
            logger.warning("auto_sell: funder position fetch failed: %s", exc)
            wallet_positions = []

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

    # Also include tokens that are in the wallet but not in DB
    # (pre-existing positions or manually bought)
    for tid, wallet_size in funder_sizes.items():
        if wallet_size > 0 and not any(tid == t for (_, t) in token_trader_map):
            # Find a trader_id to associate with (use first active trader)
            if token_trader_map:
                sample_trader_id = next(iter(token_trader_map))[0]
            else:
                continue
            token_trader_map[(sample_trader_id, tid)] = []

    # ── Regroup by token_id to sell once per token, then distribute ──────────
    # Build per-token structures: all (trader_id, buys) pairs for each token
    token_groups: dict[str, list[tuple[int, list[CopyTrade]]]] = {}
    for (trader_id, token_id), buys in token_trader_map.items():
        token_groups.setdefault(token_id, []).append((trader_id, buys))

    sold = 0
    for token_id, trader_entries in token_groups.items():
        # Use wallet's actual position size as source of truth.
        wallet_size = funder_sizes.get(token_id, 0.0)
        if funder_sizes and wallet_size <= 0:
            # Wallet data is available and shows no holdings — trust it.
            # But dry_run traders may still have simulated holdings (no wallet).
            # Check if ANY trader has dry_run holdings.
            has_dry_run = False
            for trader_id, _ in trader_entries:
                dry_net = _get_net_holdings(session, trader_id, token_id, status_filter=["dry_run"])
                if dry_net > 0:
                    has_dry_run = True
                    break
            if not has_dry_run:
                continue

        # Classify traders into live vs dry_run for this token
        live_traders: list[tuple[int, float]] = []   # (trader_id, db_net)
        dry_traders: list[tuple[int, float]] = []     # (trader_id, db_net)
        sample_buy: CopyTrade | None = None
        for trader_id, buys in trader_entries:
            if not sample_buy and buys:
                sample_buy = buys[0]
            trader_obj = session.query(Trader).filter(Trader.id == trader_id).first()
            is_dry = is_trader_dry_run(trader_obj, session) if trader_obj else True
            status_f = ["dry_run"] if is_dry else ["success"]
            db_net = _get_net_holdings(session, trader_id, token_id, status_filter=status_f)
            if db_net <= 0:
                continue
            if is_dry:
                dry_traders.append((trader_id, db_net))
            else:
                live_traders.append((trader_id, db_net))

        if not live_traders and not dry_traders:
            continue

        # ── Price check (shared across all traders for this token) ─────────
        best_bid = _get_best_price(client, token_id, "SELL")
        gamma_price = gamma_prices.get(token_id, 0.0)
        funder_price = funder_prices.get(token_id, 0.0)

        complement_price = 0.0
        comp_token = complement_map.get(token_id)
        if comp_token:
            comp_best_ask = _get_best_price(client, comp_token, "BUY")
            if comp_best_ask is not None and comp_best_ask > 0:
                complement_price = round(1.0 - comp_best_ask, 4)

        effective = max(gamma_price, funder_price, complement_price, best_bid or 0.0)
        attempt_floor = 0.95

        total_net = wallet_size if wallet_size > 0 else sum(n for _, n in live_traders)
        logger.info(
            "auto_sell check: token=%s net=%.4f effective=%.4f "
            "CLOB_bid=%s Comp=%.4f Gamma=%.4f Funder=%.4f live=%d dry=%d",
            token_id[:16], total_net, effective,
            f"{best_bid:.4f}" if best_bid is not None else "None",
            complement_price, gamma_price, funder_price,
            len(live_traders), len(dry_traders),
        )

        if effective < attempt_floor:
            continue

        if effective < threshold:
            now_ts = time.monotonic()
            last = _auto_sell_last_attempt.get(token_id, 0.0)
            if now_ts - last < _AUTO_SELL_COOLDOWN:
                continue
            _auto_sell_last_attempt[token_id] = now_ts
            logger.info(
                "auto_sell ATTEMPT: token=%s effective=%.4f < threshold=%.4f, "
                "trying FOK at %.4f (CLOB_bid=%s Comp=%.4f Gamma=%.4f Funder=%.4f)",
                token_id[:16], effective, threshold, threshold,
                f"{best_bid:.4f}" if best_bid is not None else "None",
                complement_price, gamma_price, funder_price,
            )

        sell_price = threshold
        if funder_price >= 0.995:
            sell_price = round(funder_price, 4)

        # ── Execute live sell (one order for the entire wallet position) ────
        live_sell_ok = False
        order_id: str | None = None
        recorded_price = sell_price

        if live_traders and total_net > 0:
            try:
                _ensure_sell_approval(client, token_id)
            except Exception as exc:
                logger.warning("auto_sell: approval check failed for token=%s: %s", token_id[:16], exc)

            try:
                from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
                from py_clob_client.order_builder.constants import SELL as _SELL  # type: ignore

                prices_to_try: list[float] = []
                if sell_price > 0.999:
                    prices_to_try.append(round(sell_price, 4))
                prices_to_try.append(0.999)
                seen: set[float] = set()
                unique_prices: list[float] = []
                for p in prices_to_try:
                    if p not in seen:
                        seen.add(p)
                        unique_prices.append(p)
                prices_to_try = unique_prices

                last_exc: Exception | None = None
                approval_retried = False
                for attempt_price in prices_to_try:
                    try:
                        order_args = OrderArgs(
                            token_id=token_id,
                            price=attempt_price,
                            size=_snap_sell_size(total_net),
                            side=_SELL,
                        )
                        signed_order = client.create_order(order_args)
                        resp = client.post_order(signed_order, OrderType.FOK)
                        order_id = str(resp.get("orderID") or resp.get("order_id") or "")

                        filled = False
                        if order_id:
                            try:
                                order_info = client.get_order(order_id)
                                raw_status = ""
                                if isinstance(order_info, dict):
                                    raw_status = (order_info.get("status") or "").upper()
                                else:
                                    raw_status = (getattr(order_info, "status", "") or "").upper()
                                filled = raw_status in ("FILLED", "MATCHED")
                            except Exception:
                                filled = False

                        if not filled:
                            logger.info(
                                "auto_sell FOK not filled (order cancelled): token=%s price=%.4f order_id=%s",
                                token_id[:16], attempt_price, order_id,
                            )
                            last_exc = Exception("FOK not filled")
                            continue

                        recorded_price = _get_filled_price(
                            client, order_id, attempt_price,
                            token_id=token_id, side="SELL",
                        )
                        live_sell_ok = True
                        logger.info(
                            "auto_sell SUCCESS: token=%s size=%.4f price=%.4f filled=%.4f order_id=%s",
                            token_id[:16], total_net, attempt_price, recorded_price, order_id,
                        )
                        last_exc = None
                        break
                    except Exception as exc:
                        last_exc = exc
                        err_str = str(exc)
                        if "max: 0.99" in err_str or "max:0.99" in err_str:
                            logger.info(
                                "auto_sell: price %.4f rejected (bounds), retrying at next price for token=%s",
                                attempt_price, token_id[:16],
                            )
                            continue
                        if "balance" in err_str.lower() and "allowance" in err_str.lower() and not approval_retried:
                            approval_retried = True
                            logger.info(
                                "auto_sell: balance/allowance error for token=%s, forcing approval and retrying…",
                                token_id[:16],
                            )
                            _approved_exchanges.clear()
                            try:
                                _ensure_sell_approval(client, token_id)
                            except Exception:
                                pass
                            try:
                                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType  # type: ignore
                                client.update_balance_allowance(
                                    BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
                                )
                            except Exception:
                                pass
                            continue
                        break

                if last_exc is not None:
                    logger.info(
                        "auto_sell FOK not filled for token=%s (will retry next cycle): %s",
                        token_id[:16], last_exc,
                    )
            except Exception as exc:
                logger.info(
                    "auto_sell FOK not filled for token=%s (will retry next cycle): %s",
                    token_id[:16], exc,
                )

        # ── Distribute SELL records across live traders proportionally ──────
        if live_sell_ok and live_traders:
            live_total_db = sum(n for _, n in live_traders)
            for trader_id, db_net in live_traders:
                ratio = db_net / live_total_db if live_total_db > 0 else 1.0
                trader_shares = round(db_net, 4)  # Each trader sells their own DB holdings
                avg_buy = _get_avg_buy_price(session, trader_id, token_id, status_filter=["success"])
                pnl = round((recorded_price - avg_buy) * trader_shares, 4)

                sell_record = CopyTrade(
                    trader_id=trader_id,
                    original_trade_id=f"auto_sell:{token_id[:24]}",
                    original_market=sample_buy.original_market if sample_buy else "",
                    original_token_id=token_id,
                    market_title=sample_buy.market_title if sample_buy else "",
                    outcome=sample_buy.outcome if sample_buy else "",
                    original_side="SELL",
                    original_size=trader_shares,
                    original_price=sell_price,
                    original_timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
                    copy_size=trader_shares,
                    copy_price=recorded_price,
                    status="success",
                    order_id=order_id,
                    pnl=pnl,
                    executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                session.add(sell_record)
                logger.info(
                    "auto_sell record: trader=%d shares=%.4f pnl=%.4f (of total %.4f)",
                    trader_id, trader_shares, pnl, total_net,
                )
            session.commit()
            sold += 1

        # ── Simulate SELL for dry_run traders (no real order) ──────────────
        if dry_traders and effective >= attempt_floor:
            sim_price = recorded_price if live_sell_ok else effective
            for trader_id, db_net in dry_traders:
                avg_buy = _get_avg_buy_price(session, trader_id, token_id, status_filter=["dry_run"])
                pnl = round((sim_price - avg_buy) * db_net, 4)

                sell_record = CopyTrade(
                    trader_id=trader_id,
                    original_trade_id=f"auto_sell_sim:{token_id[:24]}",
                    original_market=sample_buy.original_market if sample_buy else "",
                    original_token_id=token_id,
                    market_title=sample_buy.market_title if sample_buy else "",
                    outcome=sample_buy.outcome if sample_buy else "",
                    original_side="SELL",
                    original_size=db_net,
                    original_price=sim_price,
                    original_timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
                    copy_size=db_net,
                    copy_price=sim_price,
                    status="dry_run",
                    order_id=None,
                    pnl=pnl,
                    executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                session.add(sell_record)
                logger.info(
                    "auto_sell DRY_RUN: trader=%d token=%s shares=%.4f sim_price=%.4f pnl=%.4f",
                    trader_id, token_id[:16], db_net, sim_price, pnl,
                )
            session.commit()
            sold += 1

    return sold


# ── Dust Position Cleanup ─────────────────────────────────────────────

def clear_dust_positions(session: Session) -> int:
    """Write synthetic SELL records to zero out residual positions < threshold.

    These tiny leftovers (e.g. 0.006 shares) are caused by 2dp precision
    flooring on SELL orders. Writing a dust_cleared SELL zeroes net holdings
    so the position no longer appears in the dashboard.

    Returns count of positions cleared.
    """
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

    # Group by (trader_id, token_id, status)
    groups: dict[tuple[int, str, str], list[CopyTrade]] = {}
    for ct in open_buys:
        if ct.original_token_id:
            key = (ct.trader_id, ct.original_token_id, ct.status)
            groups.setdefault(key, []).append(ct)

    cleared = 0
    for (trader_id, token_id, status), buys in groups.items():
        status_f = [status]
        net = _get_net_holdings(session, trader_id, token_id, status_filter=status_f)

        if net <= 0 or net >= DUST_CLEAR_THRESHOLD_SHARES:
            continue

        avg_buy = _get_avg_buy_price(session, trader_id, token_id, status_filter=status_f)
        sample = buys[0]

        sell_record = CopyTrade(
            trader_id=trader_id,
            original_trade_id=f"dust_clear:{token_id[:24]}",
            original_market=sample.original_market or "",
            original_token_id=token_id,
            market_title=sample.market_title or "",
            outcome=sample.outcome or "",
            original_side="SELL",
            original_size=net,
            original_price=avg_buy,
            original_timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            copy_size=net,
            copy_price=avg_buy,
            status=status,
            order_id=None,
            pnl=0.0,
            error_message=f"Dust cleared: {net:.4f} shares < {DUST_CLEAR_THRESHOLD_SHARES}",
            executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.add(sell_record)
        logger.info(
            "dust_clear: trader=%d token=%s cleared %.4f shares (%s)",
            trader_id, token_id[:16], net, status,
        )
        cleared += 1

    if cleared:
        session.commit()
    return cleared


# ── Tiered Take-Profit ────────────────────────────────────────────────
_TP_COOLDOWN = 30  # seconds between TP attempts per token
_tp_last_attempt: dict[str, float] = {}


def _parse_tp_rules(raw: str | None) -> list[dict[str, float]]:
    """Parse tp_rules JSON string into sorted list of {max_entry, target}."""
    if not raw or not raw.strip():
        return []
    try:
        rules = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(rules, list):
        return []
    parsed = []
    for r in rules:
        if isinstance(r, dict) and "max_entry" in r and "target" in r:
            try:
                parsed.append({
                    "max_entry": float(r["max_entry"]),
                    "target": float(r["target"]),
                })
            except (ValueError, TypeError):
                continue
    return sorted(parsed, key=lambda x: x["max_entry"])


def _get_tp_target(avg_buy: float, rules: list[dict[str, float]]) -> float | None:
    """Match avg_buy price against tiered rules, return target price or None."""
    for rule in rules:
        if avg_buy <= rule["max_entry"]:
            return rule["target"]
    return None


def take_profit_monitor(session: Session) -> int:
    """Check open positions against per-trader tiered TP rules and sell when target hit.

    Returns count of positions successfully sold.
    """
    from datetime import datetime, timezone

    # Find traders with tp_rules configured
    traders = (
        session.query(Trader)
        .filter(Trader.is_active == True)
        .all()
    )
    traders_with_rules = []
    for t in traders:
        rules = _parse_tp_rules(getattr(t, "tp_rules", None))
        if rules:
            traders_with_rules.append((t, rules))

    if not traders_with_rules:
        return 0

    # Collect all open BUY trades for these traders
    trader_ids = [t.id for t, _ in traders_with_rules]
    open_buys = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.trader_id.in_(trader_ids),
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

    # Build lookup: trader_id → (Trader, rules)
    trader_lookup: dict[int, tuple[Trader, list[dict[str, float]]]] = {
        t.id: (t, rules) for t, rules in traders_with_rules
    }

    try:
        client = _get_clob_client()
    except Exception as exc:
        logger.error("take_profit: failed to get CLOB client: %s", exc)
        return 0

    # Batch-fetch prices
    unique_token_ids = list({tid for (_, tid) in token_trader_map})

    try:
        from bot.tracker import fetch_prices_by_token_ids
        gamma_prices = fetch_prices_by_token_ids(unique_token_ids)
    except Exception as exc:
        logger.warning("take_profit: Gamma price fetch failed: %s", exc)
        gamma_prices = {}

    # Funder wallet prices and sizes
    funder_prices: dict[str, float] = {}
    funder_sizes: dict[str, float] = {}
    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if funder:
        try:
            from bot.tracker import fetch_positions
            wallet_positions = fetch_positions(funder)
            for p in wallet_positions:
                tid = p.get("asset_id", "")
                if tid:
                    funder_prices[tid] = p.get("cur_price", 0.0)
                    funder_sizes[tid] = p.get("size", 0.0)
        except Exception as exc:
            logger.warning("take_profit: funder position fetch failed: %s", exc)

    # Complement token map for accurate sell pricing
    unique_condition_ids = list({
        ct.original_market for cts in token_trader_map.values()
        for ct in cts if ct.original_market
    })
    complement_map: dict[str, str] = {}
    try:
        from bot.tracker import fetch_complement_token_ids
        complement_map = fetch_complement_token_ids(unique_token_ids, unique_condition_ids)
    except Exception as exc:
        logger.warning("take_profit: complement token fetch failed: %s", exc)

    sold = 0
    for (trader_id, token_id), buys in token_trader_map.items():
        if trader_id not in trader_lookup:
            continue
        trader_obj, rules = trader_lookup[trader_id]
        is_dry = is_trader_dry_run(trader_obj, session)
        status_f = ["dry_run"] if is_dry else ["success"]

        net = _get_net_holdings(session, trader_id, token_id, status_filter=status_f)
        if net <= 0:
            continue

        avg_buy = _get_avg_buy_price(session, trader_id, token_id, status_filter=status_f)
        if avg_buy <= 0:
            continue

        target = _get_tp_target(avg_buy, rules)
        if target is None:
            continue

        # Get current market price (same multi-source logic as auto_sell)
        best_bid = _get_best_price(client, token_id, "SELL")
        gamma_price = gamma_prices.get(token_id, 0.0)
        funder_price = funder_prices.get(token_id, 0.0)

        complement_price = 0.0
        comp_token = complement_map.get(token_id)
        if comp_token:
            comp_best_ask = _get_best_price(client, comp_token, "BUY")
            if comp_best_ask is not None and comp_best_ask > 0:
                complement_price = round(1.0 - comp_best_ask, 4)

        effective = max(gamma_price, funder_price, complement_price, best_bid or 0.0)

        logger.debug(
            "take_profit check: trader=%d token=%s avg_buy=%.4f target=%.4f "
            "effective=%.4f (bid=%s comp=%.4f gamma=%.4f funder=%.4f)",
            trader_id, token_id[:16], avg_buy, target, effective,
            f"{best_bid:.4f}" if best_bid is not None else "None",
            complement_price, gamma_price, funder_price,
        )

        if effective < target:
            continue

        # Cooldown check
        cooldown_key = f"tp:{trader_id}:{token_id}"
        now_ts = time.monotonic()
        last = _tp_last_attempt.get(cooldown_key, 0.0)
        if now_ts - last < _TP_COOLDOWN:
            continue

        logger.info(
            "take_profit TRIGGERED: trader=%d token=%s avg_buy=%.4f "
            "target=%.4f effective=%.4f net=%.4f",
            trader_id, token_id[:16], avg_buy, target, effective, net,
        )

        # CLOB enforces price <= 0.999 for SELL orders
        sell_price = min(round(target, 4), 0.999)
        tp_note = f"TP rule: avg_buy={avg_buy:.4f} target={target:.2f}"
        sample_buy = buys[0] if buys else None

        if is_dry:
            # Dry-run: simulate sell at effective price
            sim_price = effective
            pnl = round((sim_price - avg_buy) * net, 4)
            sell_record = CopyTrade(
                trader_id=trader_id,
                original_trade_id=f"tp_sell_sim:{token_id[:24]}",
                original_market=sample_buy.original_market if sample_buy else "",
                original_token_id=token_id,
                market_title=sample_buy.market_title if sample_buy else "",
                outcome=sample_buy.outcome if sample_buy else "",
                original_side="SELL",
                original_size=net,
                original_price=sim_price,
                original_timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
                copy_size=net,
                copy_price=sim_price,
                status="dry_run",
                order_id=None,
                pnl=pnl,
                error_message=tp_note,
                executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            session.add(sell_record)
            session.commit()
            _tp_last_attempt[cooldown_key] = now_ts
            logger.info(
                "take_profit DRY_RUN: trader=%d token=%s shares=%.4f "
                "sim_price=%.4f pnl=%.4f",
                trader_id, token_id[:16], net, sim_price, pnl,
            )
            sold += 1
        else:
            # Live sell: FOK at target price
            # Use tracked wallet size (decremented after each successful sell)
            wallet_size = funder_sizes.get(token_id, 0.0)
            sell_size = min(net, wallet_size) if wallet_size > 0 else net
            if sell_size <= 0:
                continue
            try:
                _ensure_sell_approval(client, token_id)
            except Exception as exc:
                logger.warning("take_profit: approval failed for token=%s: %s", token_id[:16], exc)

            try:
                from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
                from py_clob_client.order_builder.constants import SELL as _SELL  # type: ignore

                order_args = OrderArgs(
                    token_id=token_id,
                    price=sell_price,
                    size=_snap_sell_size(sell_size),
                    side=_SELL,
                )
                signed_order = client.create_order(order_args)
                resp = client.post_order(signed_order, OrderType.FOK)
                order_id = str(resp.get("orderID") or resp.get("order_id") or "")

                filled = False
                if order_id:
                    try:
                        order_info = client.get_order(order_id)
                        raw_status = ""
                        if isinstance(order_info, dict):
                            raw_status = (order_info.get("status") or "").upper()
                        else:
                            raw_status = (getattr(order_info, "status", "") or "").upper()
                        filled = raw_status in ("FILLED", "MATCHED")
                    except Exception:
                        filled = False

                if not filled:
                    _tp_last_attempt[cooldown_key] = now_ts
                    logger.info(
                        "take_profit FOK not filled: trader=%d token=%s price=%.4f "
                        "(will retry next cycle)",
                        trader_id, token_id[:16], sell_price,
                    )
                    continue

                recorded_price = _get_filled_price(
                    client, order_id, sell_price,
                    token_id=token_id, side="SELL",
                )
                pnl = round((recorded_price - avg_buy) * sell_size, 4)

                sell_record = CopyTrade(
                    trader_id=trader_id,
                    original_trade_id=f"tp_sell:{token_id[:24]}",
                    original_market=sample_buy.original_market if sample_buy else "",
                    original_token_id=token_id,
                    market_title=sample_buy.market_title if sample_buy else "",
                    outcome=sample_buy.outcome if sample_buy else "",
                    original_side="SELL",
                    original_size=sell_size,
                    original_price=sell_price,
                    original_timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
                    copy_size=sell_size,
                    copy_price=recorded_price,
                    status="success",
                    order_id=order_id,
                    pnl=pnl,
                    error_message=tp_note,
                    executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                session.add(sell_record)
                session.commit()
                _tp_last_attempt[cooldown_key] = now_ts
                # Decrement local wallet size to prevent over-sell on same token
                funder_sizes[token_id] = max(wallet_size - sell_size, 0.0)
                logger.info(
                    "take_profit SUCCESS: trader=%d token=%s shares=%.4f "
                    "price=%.4f pnl=%.4f order=%s",
                    trader_id, token_id[:16], sell_size, recorded_price, pnl, order_id,
                )
                sold += 1
            except Exception as exc:
                _tp_last_attempt[cooldown_key] = now_ts
                logger.error(
                    "take_profit FOK error: trader=%d token=%s: %s",
                    trader_id, token_id[:16], exc,
                )

    return sold


def execute_copy_trade(
    session: Session,
    trader: Trader,
    trade: dict[str, Any],
) -> CopyTrade:
    """Apply risk checks and execute (or simulate) a copy trade.

    Returns the persisted CopyTrade record.
    """
    from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType  # type: ignore
    from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

    expected_price = trade["price"]
    orig_value = trade["size"] * expected_price
    copy_size = _calculate_copy_size(trader, trade["size"], expected_price)

    logger.info(
        "copy_calc: trader=%s side=%s orig_shares=%.4f orig_price=%.4f orig_value=$%.2f "
        "mode=%s pct=%.4f%% fixed=$%.2f → copy_shares=%.4f",
        trader.wallet_address[:12], trade["side"],
        trade["size"], expected_price, orig_value,
        trader.sizing_mode, trader.proportional_pct, trader.fixed_amount,
        copy_size,
    )

    # Resolve per-trader dry_run mode
    dry_run = is_trader_dry_run(trader, session)
    # Status filter ensures dry_run and live trades are counted separately
    mode_status = ["dry_run"] if dry_run else ["success"]

    # For SELL trades, cap the size at what we actually hold to avoid
    # selling more shares than we own (can happen when some BUYs were
    # filtered out by risk checks).
    if trade["side"] == "SELL":
        holdings = _get_net_holdings(session, trader.id, trade["token_id"], status_filter=mode_status)
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

    # Compute order price: limit orders use tight offset, market orders use slippage
    offset_pct = (
        getattr(trader, "buy_price_offset_pct", 1.0)
        if trade["side"] == "BUY"
        else getattr(trader, "sell_price_offset_pct", 1.0)
    ) or 1.0
    if order_type_str == "limit":
        order_price = _compute_limit_price(expected_price, trade["side"], offset_pct)
    else:
        order_price = _apply_slippage(expected_price, trade["side"], slippage_pct)

    # In live mode, query orderbook for real best price (for slippage risk check)
    best_price = expected_price
    client = None
    if not dry_run:
        try:
            client = _get_clob_client()
            real_price = _get_best_price(client, trade["token_id"], trade["side"])
            if real_price is not None:
                best_price = real_price
        except Exception as exc:
            logger.warning("Could not query orderbook: %s", exc)

    # For BUY cap calculations, use order_price (post-slippage) so actual
    # cost stays within limits. expected_price (trader's fill price) can be
    # lower than what we'll actually pay.
    copy_size, rejection = risk.cap_and_check(
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
        status_filter=mode_status,
        order_price=order_price if trade["side"] == "BUY" else None,
    )

    if copy_size != _calculate_copy_size(trader, trade["size"], expected_price):
        logger.info(
            "copy_calc: risk cap adjusted copy_shares=%.4f (was %.4f) rejection=%s",
            copy_size, _calculate_copy_size(trader, trade["size"], expected_price), rejection,
        )

    status = rejection or ("dry_run" if dry_run else "pending")
    order_id: str | None = None
    error_msg: str | None = None

    if rejection is None and not dry_run:
        try:
            if client is None:
                client = _get_clob_client()
            side = BUY if trade["side"] == "BUY" else SELL
            ot = OrderType.FOK if order_type_str == "market" else OrderType.GTC

            if trade["side"] == "BUY":
                # Derive buy_amount from risk-adjusted copy_size (not from scratch)
                # so that buy_at_min bumps and cap adjustments are honored.
                buy_amount = _floor(copy_size * order_price * 100) / 100
                logger.info(
                    "copy_calc: BUY order → buy_amount=$%.2f order_price=%.4f order_type=%s",
                    buy_amount, order_price, order_type_str,
                )

                if order_type_str == "market":
                    # FOK market order: maker(USDC)≤2dp, taker(shares)≤4dp
                    # MarketOrderArgs passes USDC directly, library handles precision.
                    mkt_args = MarketOrderArgs(
                        token_id=trade["token_id"],
                        amount=buy_amount,
                        price=round(order_price, 4),
                        side=side,
                    )
                    signed_order = client.create_market_order(mkt_args)
                else:
                    # GTC limit order: maker(USDC)≤4dp, taker(shares)≤2dp
                    # Must use OrderArgs with size floored to 2dp.
                    limit_size = _snap_size_2dp(copy_size)
                    order_args = OrderArgs(
                        token_id=trade["token_id"],
                        price=round(order_price, 4),
                        size=limit_size,
                        side=side,
                    )
                    signed_order = client.create_order(order_args)
            else:
                order_args = OrderArgs(
                    token_id=trade["token_id"],
                    price=round(order_price, 4),
                    size=_snap_sell_size(copy_size),
                    side=side,
                )
                signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, ot)
            order_id = str(resp.get("orderID") or resp.get("order_id") or "")

            if ot == OrderType.GTC and order_id:
                # Poll for GTC fill, cancel + fallback if timeout
                timeout = getattr(trader, "limit_timeout_seconds", 30) or 30
                # Per-side fallback toggle (fall back to legacy shared setting)
                if trade["side"] == "BUY":
                    fallback = getattr(trader, "buy_limit_fallback", None)
                else:
                    fallback = getattr(trader, "sell_limit_fallback", None)
                if fallback is None:
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

                    # Re-check: order may have filled between last poll and cancel
                    try:
                        final_order = client.get_order(order_id)
                        final_status = ""
                        if isinstance(final_order, dict):
                            final_status = final_order.get("status", "").upper()
                        else:
                            final_status = getattr(final_order, "status", "").upper()
                        if final_status in ("FILLED", "MATCHED"):
                            logger.info("GTC order %s filled during cancel race window", order_id)
                            filled = True
                            status = "success"
                    except Exception as recheck_exc:
                        logger.warning("Failed to re-check order %s: %s", order_id, recheck_exc)

                    if filled:
                        pass  # already set status = "success" above
                    elif fallback:
                        # FOK fallback uses wider slippage price for better fill chance
                        fallback_price = _apply_slippage(expected_price, trade["side"], slippage_pct)
                        logger.info("Falling back to FOK market order for trader %s", trader.wallet_address)
                        try:
                            if trade["side"] == "BUY":
                                # Derive from risk-adjusted copy_size, not recalculated
                                fok_buy_amt = _floor(copy_size * fallback_price * 100) / 100
                                fok_mkt = MarketOrderArgs(
                                    token_id=trade["token_id"],
                                    amount=fok_buy_amt,
                                    price=round(fallback_price, 4),
                                    side=side,
                                )
                                signed_fok = client.create_market_order(fok_mkt)
                            else:
                                fok_args = OrderArgs(
                                    token_id=trade["token_id"],
                                    price=round(fallback_price, 4),
                                    size=_snap_sell_size(copy_size),
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

            _price_label = "offset" if order_type_str == "limit" else "slippage"
            _price_pct = offset_pct if order_type_str == "limit" else slippage_pct
            logger.info(
                "Copy trade executed for trader %s: market=%s side=%s size=%.4f "
                "price=%.4f (orig=%.4f, %s=%.1f%%) order_type=%s order_id=%s",
                trader.wallet_address,
                trade["market"],
                trade["side"],
                copy_size,
                order_price,
                expected_price,
                _price_label,
                _price_pct,
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
    elif rejection is None and dry_run:
        if trade["side"] == "BUY":
            # copy_size already adjusted by cap_and_check; just snap for display
            pass
        elif trade["side"] == "SELL":
            copy_size = _snap_sell_size(copy_size)
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
    # - fallback: expected_price (market price, NOT slippage-adjusted order_price)
    if dry_run:
        recorded_price = expected_price
    elif status == "success":
        recorded_price = _get_filled_price(
            client, order_id or "", expected_price,
            token_id=trade["token_id"], side=trade["side"],
        )
    else:
        recorded_price = expected_price

    # Calculate realized PnL for SELL trades at execution time
    realized_pnl = 0.0
    if trade["side"] == "SELL" and status in ("success", "dry_run") and copy_size > 0:
        avg_buy = _get_avg_buy_price(session, trader.id, trade["token_id"], status_filter=mode_status)
        realized_pnl = round((recorded_price - avg_buy) * copy_size, 4)

    copy_value = copy_size * recorded_price
    logger.info(
        "copy_result: side=%s status=%s copy_shares=%.4f copy_price=%.4f "
        "copy_value=$%.2f orig_value=$%.2f ratio=%.4f%%",
        trade["side"], status, copy_size, recorded_price,
        copy_value, orig_value,
        (copy_value / orig_value * 100) if orig_value > 0 else 0,
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
        copy_price=recorded_price,
        status=status,
        error_message=error_msg,
        order_id=order_id,
        pnl=realized_pnl,
        agg_fill_count=trade.get("_agg_fill_count"),
        agg_total_value=trade.get("_agg_total_value"),
        executed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(copy_trade)
    session.commit()
    return copy_trade
