"""Auto-redeem resolved Polymarket positions via on-chain contract calls.

Supports:
- Binary (neg_risk=false) markets → CTF contract redeemPositions
- Multi-outcome (neg_risk=true) markets → NegRiskAdapter redeemPositions

Flow:
1. Find all open copy-trade BUY positions (net shares > 0)
2. For each unique (condition_id, token_id), check if market is resolved
3. If winning token (price ≈ 1.0), call the appropriate on-chain contract
4. Record a synthetic SELL entry in DB and zero out unrealized PnL
"""

from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from config import settings
from bot.tracker import fetch_market
from db.models import CopyTrade

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Polygon contract addresses ───────────────────────────────────────────────
USDC_ADDRESS     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Polygon USDC
CTF_ADDRESS      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Gnosis CTF
NEG_RISK_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # NegRiskAdapter
POLYGON_RPC      = "https://polygon-rpc.com"

# ── Minimal ABIs ─────────────────────────────────────────────────────────────
_CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken",      "type": "address"},
            {"name": "parentCollectionId",   "type": "bytes32"},
            {"name": "conditionId",          "type": "bytes32"},
            {"name": "indexSets",            "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

_NEG_RISK_ABI = [
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amount",      "type": "uint256"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _condition_bytes(condition_id: str) -> bytes:
    """Convert a 0x-prefixed hex condition_id string to 32 bytes."""
    hex_str = condition_id.removeprefix("0x").zfill(64)
    return bytes.fromhex(hex_str)


def _get_web3():
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    if not w3.is_connected():
        raise RuntimeError("Cannot connect to Polygon RPC")
    return w3


def _get_account(w3):
    from eth_account import Account
    private_key = (settings.POLYMARKET_PRIVATE_KEY or "").strip()
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    return Account.from_key(private_key)


def _get_token_balance(w3, contract_address: str, abi: list, owner: str, token_id_hex: str) -> int:
    """Return ERC-1155 token balance from contract. Returns 0 on error."""
    try:
        contract = w3.eth.contract(
            address=w3.to_checksum_address(contract_address),
            abi=abi,
        )
        token_int = int(token_id_hex, 16)
        return contract.functions.balanceOf(w3.to_checksum_address(owner), token_int).call()
    except Exception as exc:
        logger.debug("balanceOf(%s) failed on %s: %s", token_id_hex[:12], contract_address[:12], exc)
        return 0


def _get_market_info(condition_id: str, token_id: str | None = None) -> dict | None:
    """Fetch market from Gamma API and return resolution details.

    Returns None if the market is not yet resolved.
    Returns a dict with keys:
        neg_risk, condition_id, token_info ({token_id: {outcome, price, index}}), winner

    When Gamma returns 422 (resolved market), falls back to price lookup via token_id.
    """
    data = fetch_market(condition_id)
    if not data:
        # Fallback for resolved markets that return 422 on /markets/{id}
        if token_id:
            from bot.tracker import fetch_prices_by_token_ids
            price_map = fetch_prices_by_token_ids([token_id])
            price = price_map.get(token_id, 0.0)
            if price >= 0.99:
                # Gamma returned 422 — assume binary (neg_risk=False).
                # If this is actually a neg_risk market, _redeem_binary will
                # fail fast on zero CTF balance (ValueError, no gas wasted).
                logger.warning(
                    "Market %s returned 422 from Gamma — assuming binary for redemption. "
                    "If this is a neg_risk market, please redeem manually.",
                    condition_id[:16],
                )
                return {
                    "neg_risk": False,
                    "condition_id": condition_id,
                    "token_info": {token_id: {"outcome": "", "price": price, "index": 0}},
                    "winner": "",
                }
        return None

    resolved = data.get("resolved", False) or data.get("closed", False)
    if not resolved:
        return None

    # ── Parse token list ─────────────────────────────────────────────────────
    import json

    def _load(val):
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return []
        return val or []

    token_info: dict[str, dict] = {}

    tokens_raw = _load(data.get("tokens", []))
    if tokens_raw and isinstance(tokens_raw[0], dict):
        # Format: [{"token_id": "...", "outcome": "Yes", "price": "0.97"}, ...]
        for i, t in enumerate(tokens_raw):
            tid = t.get("token_id") or t.get("tokenId") or ""
            token_info[tid] = {
                "outcome": t.get("outcome", ""),
                "price":   float(t.get("price", 0) or 0),
                "index":   i,
            }
    else:
        # Flat arrays: clobTokenIds + outcomePrices + outcomes
        clob_ids     = _load(data.get("clobTokenIds", []))
        prices_raw   = _load(data.get("outcomePrices", []))
        outcomes_raw = _load(data.get("outcomes", []))
        for i, (tid, price_str) in enumerate(zip(clob_ids, prices_raw)):
            token_info[tid] = {
                "outcome": outcomes_raw[i] if i < len(outcomes_raw) else "",
                "price":   float(price_str or 0),
                "index":   i,
            }

    return {
        "neg_risk":     data.get("negRisk", False) or data.get("neg_risk", False),
        "condition_id": condition_id,
        "token_info":   token_info,
        "winner":       data.get("winner", ""),
    }


# ── On-chain redemption ───────────────────────────────────────────────────────

def _redeem_binary(
    w3, account, condition_id: str, token_id: str, outcome_index: int,
    holder_address: str | None = None,
) -> str:
    """Redeem a winning binary CTF position. Returns tx hash string.

    ``holder_address`` is the wallet that holds the CTF tokens (funder wallet
    in proxy-key setups). Falls back to account.address when not provided.
    """
    owner = holder_address or account.address
    ctf = w3.eth.contract(
        address=w3.to_checksum_address(CTF_ADDRESS),
        abi=_CTF_ABI,
    )

    balance = _get_token_balance(w3, CTF_ADDRESS, _CTF_ABI, owner, token_id)
    if balance == 0:
        raise ValueError(
            f"No CTF balance for token {token_id[:12]} in wallet {owner[:12]}"
        )

    # indexSet bitmask: outcome at index i → 1 << i
    index_set = 1 << outcome_index

    tx = ctf.functions.redeemPositions(
        w3.to_checksum_address(USDC_ADDRESS),
        b"\x00" * 32,               # parentCollectionId = zero hash (top-level)
        _condition_bytes(condition_id),
        [index_set],
    ).build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address),
        "gas":      250_000,
        "gasPrice": w3.eth.gas_price * 2,  # 2× for faster inclusion
    })

    signed   = account.sign_transaction(tx)
    tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"Binary redeem tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def _redeem_neg_risk(
    w3, account, condition_id: str, token_id: str,
    holder_address: str | None = None,
) -> str:
    """Redeem a winning neg_risk position via NegRiskAdapter. Returns tx hash string.

    ``holder_address`` is the wallet that holds the tokens (funder wallet
    in proxy-key setups). Falls back to account.address when not provided.
    """
    owner = holder_address or account.address
    # Check NegRiskAdapter balance first, then fall back to CTF
    balance = _get_token_balance(w3, NEG_RISK_ADDRESS, _NEG_RISK_ABI, owner, token_id)
    if balance == 0:
        balance = _get_token_balance(w3, CTF_ADDRESS, _CTF_ABI, owner, token_id)
    if balance == 0:
        raise ValueError(
            f"No on-chain balance for neg_risk token {token_id[:12]} in wallet {owner[:12]}"
        )

    adapter = w3.eth.contract(
        address=w3.to_checksum_address(NEG_RISK_ADDRESS),
        abi=_NEG_RISK_ABI,
    )

    tx = adapter.functions.redeemPositions(
        _condition_bytes(condition_id),
        balance,
    ).build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address),
        "gas":      350_000,
        "gasPrice": w3.eth.gas_price * 2,
    })

    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"Neg-risk redeem tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()


# ── DB recording ──────────────────────────────────────────────────────────────

def _record_redemption(
    session: "Session",
    buy_trades: list[CopyTrade],
    net_shares: float,
    tx_hash: str,
) -> None:
    """Create a synthetic SELL record for the redemption and zero out BUY unrealized PnL."""
    from sqlalchemy import func

    if not buy_trades:
        return

    sample = buy_trades[0]

    # Weighted average buy price across all BUY trades for this token
    result = (
        session.query(
            func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0),
            func.coalesce(func.sum(CopyTrade.copy_size), 0.0),
        )
        .filter(
            CopyTrade.trader_id         == sample.trader_id,
            CopyTrade.original_token_id == sample.original_token_id,
            CopyTrade.original_side     == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
        )
        .first()
    )
    total_cost, total_size = result
    avg_buy = (total_cost / total_size) if total_size and total_size > 0 else 0.0

    # Redemption always pays $1.00 per share
    pnl = round((1.0 - avg_buy) * net_shares, 4)

    redemption = CopyTrade(
        trader_id           = sample.trader_id,
        original_trade_id   = f"redemption:{tx_hash[:24]}",
        original_market     = sample.original_market,
        original_token_id   = sample.original_token_id,
        market_title        = sample.market_title,
        outcome             = sample.outcome,
        original_side       = "SELL",
        original_size       = net_shares,
        original_price      = 1.0,
        original_timestamp  = datetime.datetime.utcnow(),
        copy_size           = net_shares,
        copy_price          = 1.0,
        status              = "success",
        order_id            = f"onchain:{tx_hash}",
        pnl                 = pnl,
    )
    session.add(redemption)

    # Zero out unrealized PnL on all BUY entries for this position
    for ct in buy_trades:
        ct.pnl = 0.0

    session.commit()
    logger.info(
        "Recorded redemption: market=%s shares=%.4f avg_buy=%.4f realized_pnl=%.4f tx=%s",
        sample.market_title or sample.original_market[:12],
        net_shares, avg_buy, pnl, tx_hash[:16],
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def redeem_resolved_positions(session: "Session") -> int:
    """Check all open copy-trade BUY positions for resolved markets and auto-redeem.

    Returns the number of positions successfully redeemed.
    """
    if settings.DRY_RUN:
        logger.debug("DRY_RUN: skipping auto-redemption check")
        return 0

    if not settings.POLYMARKET_PRIVATE_KEY:
        logger.warning("POLYMARKET_PRIVATE_KEY not set — cannot auto-redeem")
        return 0

    funder_address = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()

    # ── Collect all open BUY positions ────────────────────────────────────────
    open_buys = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success"]),
            CopyTrade.original_market.is_not(None),
            CopyTrade.original_token_id.is_not(None),
        )
        .all()
    )
    if not open_buys:
        return 0

    # Group by (trader_id, condition_id, token_id)
    groups: dict[tuple, list[CopyTrade]] = defaultdict(list)
    for ct in open_buys:
        key = (ct.trader_id, ct.original_market, ct.original_token_id)
        groups[key].append(ct)

    # Keep only groups where net holdings > 0 (not already sold)
    from bot.executor import _get_net_holdings
    active: list[tuple] = []
    for (trader_id, condition_id, token_id), trades in groups.items():
        if not condition_id or not token_id:
            continue
        net = _get_net_holdings(session, trader_id, token_id)
        if net > 0:
            active.append((trader_id, condition_id, token_id, trades, net))

    if not active:
        return 0

    # Initialize web3 once for all redemptions
    try:
        w3      = _get_web3()
        account = _get_account(w3)
    except Exception as exc:
        logger.warning("Cannot init web3 for redemption: %s", exc)
        return 0

    # Determine the address that actually holds CTF tokens on-chain.
    # In a proxy-wallet setup: POLYMARKET_PRIVATE_KEY = proxy key,
    # POLYMARKET_FUNDER_ADDRESS = funder wallet (holds the tokens).
    # In a direct-key setup: both point to the same address.
    holder_address = funder_address if funder_address else account.address
    if funder_address and funder_address.lower() != account.address.lower():
        logger.info(
            "Proxy-wallet mode: token holder=%s  signing key=%s",
            holder_address[:12], account.address[:12],
        )

    redeemed       = 0
    seen_conditions: set[str] = set()  # avoid double-redeeming same market in one pass

    for trader_id, condition_id, token_id, trades, net_shares in active:
        if condition_id in seen_conditions:
            continue

        # ── Check if market is resolved ───────────────────────────────────────
        try:
            market = _get_market_info(condition_id, token_id=token_id)
        except Exception as exc:
            logger.debug("Market info fetch failed for %s: %s", condition_id[:12], exc)
            continue

        if market is None:
            continue  # not resolved yet

        # ── Check if our token is the winning one ─────────────────────────────
        token_data = market["token_info"].get(token_id)
        if token_data is None:
            logger.debug("Token %s not in market %s token list", token_id[:12], condition_id[:12])
            continue

        price = token_data["price"]
        if price < 0.99:
            # Our token lost (or market is still live) — nothing to redeem
            continue

        is_neg_risk   = market["neg_risk"]
        label         = trades[0].market_title or condition_id[:12]
        outcome       = token_data["outcome"]
        outcome_index = token_data["index"]

        logger.info(
            "Redeeming: market=%r outcome=%r shares=%.4f neg_risk=%s",
            label, outcome, net_shares, is_neg_risk,
        )

        # ── Execute on-chain redemption ───────────────────────────────────────
        try:
            if is_neg_risk:
                tx_hash = _redeem_neg_risk(w3, account, condition_id, token_id, holder_address)
            else:
                tx_hash = _redeem_binary(w3, account, condition_id, token_id, outcome_index, holder_address)

            seen_conditions.add(condition_id)
            logger.info("Redemption successful: market=%r tx=%s", label, tx_hash[:20])

            _record_redemption(session, trades, net_shares, tx_hash)
            redeemed += 1

        except Exception as exc:
            logger.error(
                "Redemption failed: market=%r token=%s neg_risk=%s error=%s",
                label, token_id[:12], is_neg_risk, exc,
            )

    if redeemed:
        logger.info("Auto-redemption complete: %d position(s) redeemed", redeemed)

    return redeemed


def detect_manual_sells(session: "Session") -> int:
    """Detect SELL trades made manually via Polymarket UI and create synthetic SELL records.

    Matches funder wallet SELL activity against existing CopyTrade SELL records by
    token_id + timestamp (±10 min window). Unmatched sells are treated as manual.
    Returns count of new records created.
    """
    import requests as _req
    from sqlalchemy import func
    from bot.executor import _get_net_holdings

    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        return 0

    url = f"{settings.DATA_API_BASE}/activity"
    try:
        resp = _req.get(url, params={"user": funder, "limit": 500}, timeout=15)
        resp.raise_for_status()
        all_activity = resp.json()
        if not isinstance(all_activity, list):
            all_activity = all_activity.get("data", [])
    except Exception as exc:
        logger.warning("Failed to fetch funder activity for manual sell detection: %s", exc)
        return 0

    # Only TRADE-type SELL entries from our funder wallet
    sell_activity = [
        d for d in all_activity
        if d.get("type", "").upper() == "TRADE" and (d.get("side") or "").upper() == "SELL"
    ]
    if not sell_activity:
        return 0

    # Build lookup of existing SELL CopyTrade records: token_id → [(ts, size), ...]
    existing_sells = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "SELL",
            CopyTrade.status.in_(["success", "dry_run"]),
            CopyTrade.original_token_id.is_not(None),
        )
        .all()
    )
    existing_map: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for ct in existing_sells:
        ts = ct.executed_at.timestamp() if ct.executed_at else 0.0
        existing_map[ct.original_token_id].append((ts, ct.copy_size or 0.0))

    _MATCH_WINDOW = 600  # 10-minute window for timestamp matching

    def _is_matched(token_id: str, activity_ts: float, activity_size: float) -> bool:
        """Return True if this sell activity already has a CopyTrade SELL record."""
        for rec_ts, rec_size in existing_map.get(token_id, []):
            if abs(rec_ts - activity_ts) < _MATCH_WINDOW and abs(rec_size - activity_size) < 0.01:
                return True
        return False

    created = 0

    for raw in sell_activity:
        token_id = raw.get("asset") or raw.get("asset_id") or raw.get("assetId") or ""
        tx_hash = raw.get("transactionHash") or raw.get("id") or ""
        condition_id = raw.get("conditionId") or raw.get("market") or ""
        market_title = raw.get("title") or ""
        sell_price = float(raw.get("price", 0) or 0)
        sell_size = float(raw.get("size", 0) or raw.get("shares", 0) or 0)
        activity_ts = float(raw.get("timestamp", 0) or 0)

        if not token_id or not tx_hash or sell_price <= 0 or sell_size <= 0:
            continue

        # Skip if already matched to an existing CopyTrade SELL
        if _is_matched(token_id, activity_ts, sell_size):
            continue

        # Dedup: avoid creating duplicate manual sell records across runs
        order_id_key = f"manual_sell:{tx_hash[:20]}:{token_id[:12]}"
        if session.query(CopyTrade).filter(CopyTrade.order_id == order_id_key).first():
            continue

        # Find which traders hold (or held) this token via BUY records
        buy_trades = (
            session.query(CopyTrade)
            .filter(
                CopyTrade.original_token_id == token_id,
                CopyTrade.original_side == "BUY",
                CopyTrade.status.in_(["success", "dry_run"]),
            )
            .all()
        )
        if not buy_trades:
            continue

        trader_ids = {int(bt.trader_id) for bt in buy_trades}
        sell_time = (
            datetime.datetime.utcfromtimestamp(activity_ts)
            if activity_ts else datetime.datetime.utcnow()
        )

        for trader_id in trader_ids:
            net_shares = _get_net_holdings(session, trader_id, token_id)
            if net_shares <= 0:
                continue

            # Weighted avg buy price for this trader + token
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
            avg_buy = (total_cost / total_size) if total_size and total_size > 0 else 0.0
            actual_size = min(sell_size, net_shares)
            pnl = round((sell_price - avg_buy) * actual_size, 4)

            sample_buy = next(
                (bt for bt in buy_trades if bt.trader_id == trader_id),
                buy_trades[0],
            )
            manual_sell = CopyTrade(
                trader_id=trader_id,
                original_trade_id=f"manual_sell:{tx_hash[:24]}",
                original_market=condition_id or sample_buy.original_market,
                original_token_id=token_id,
                market_title=market_title or sample_buy.market_title,
                outcome=sample_buy.outcome,
                original_side="SELL",
                original_size=actual_size,
                original_price=sell_price,
                original_timestamp=sell_time,
                copy_size=actual_size,
                copy_price=sell_price,
                status="success",
                order_id=order_id_key,
                pnl=pnl,
                executed_at=sell_time,
            )
            session.add(manual_sell)

            # Register in local map so subsequent activity entries for the same
            # token don't create duplicates within the same run
            existing_map[token_id].append((activity_ts, actual_size))

            # Zero out unrealized PnL on BUY records if position is fully closed
            remaining = net_shares - actual_size
            if remaining <= 0:
                for bt in buy_trades:
                    if bt.trader_id == trader_id and bt.original_token_id == token_id:
                        bt.pnl = 0.0

            created += 1
            logger.info(
                "Manual sell recorded: market=%s trader=%d size=%.4f price=%.4f pnl=%.4f",
                market_title or token_id[:12], trader_id, actual_size, sell_price, pnl,
            )

    if created:
        session.commit()
    return created


def detect_manual_redemptions(session: "Session") -> int:
    """Fetch REDEEM-type activities from funder wallet and create synthetic SELL records.

    Handles manual redemptions done via Polymarket UI.
    Returns count of new records created.
    """
    import requests as _req
    from sqlalchemy import func
    from bot.executor import _get_net_holdings

    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        return 0

    url = f"{settings.DATA_API_BASE}/activity"
    try:
        resp = _req.get(url, params={"user": funder, "limit": 500}, timeout=15)
        resp.raise_for_status()
        all_activity = resp.json()
        if not isinstance(all_activity, list):
            all_activity = all_activity.get("data", [])
    except Exception as exc:
        logger.warning("Failed to fetch funder activity for manual redemption detection: %s", exc)
        return 0

    redeems = [d for d in all_activity if d.get("type", "").upper() == "REDEEM"]
    if not redeems:
        return 0

    created = 0

    for raw in redeems:
        condition_id = raw.get("conditionId", "")
        tx_hash = raw.get("transactionHash", "")
        market_title = raw.get("title", "")
        if not condition_id or not tx_hash:
            continue

        ts = float(raw.get("timestamp", 0) or 0)
        redeem_time = (
            datetime.datetime.utcfromtimestamp(ts) if ts else datetime.datetime.utcnow()
        )

        buy_trades = (
            session.query(CopyTrade)
            .filter(
                CopyTrade.original_market == condition_id,
                CopyTrade.original_side == "BUY",
                CopyTrade.status.in_(["success", "dry_run"]),
            )
            .all()
        )
        if not buy_trades:
            continue

        by_trader: dict[int, set] = defaultdict(set)
        for bt in buy_trades:
            if bt.original_token_id:
                by_trader[int(bt.trader_id)].add(bt.original_token_id)

        for trader_id, token_ids in by_trader.items():
            for token_id in token_ids:
                # Dedup key is scoped per trader+token+tx to avoid skipping
                # traders on a second run after a mid-run interruption.
                order_id_key = f"manual_redeem:{tx_hash[:20]}:t{trader_id}:{token_id[:12]}"
                if session.query(CopyTrade).filter(CopyTrade.order_id == order_id_key).first():
                    continue

                net_shares = _get_net_holdings(session, trader_id, token_id)
                if net_shares <= 0:
                    continue

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
                avg_buy = (total_cost / total_size) if total_size and total_size > 0 else 0.0
                pnl = round((1.0 - avg_buy) * net_shares, 4)

                sample_buy = next(
                    (bt for bt in buy_trades if bt.trader_id == trader_id and bt.original_token_id == token_id),
                    buy_trades[0],
                )
                redemption = CopyTrade(
                    trader_id=trader_id,
                    original_trade_id=f"redeem:{tx_hash[:24]}",
                    original_market=condition_id,
                    original_token_id=token_id,
                    market_title=market_title or sample_buy.market_title,
                    outcome=sample_buy.outcome,
                    original_side="SELL",
                    original_size=net_shares,
                    original_price=1.0,
                    original_timestamp=redeem_time,
                    copy_size=net_shares,
                    copy_price=1.0,
                    status="success",
                    order_id=order_id_key,
                    pnl=pnl,
                    executed_at=redeem_time,
                )
                session.add(redemption)

                # Zero out unrealized PnL on BUY records for this position
                for bt in buy_trades:
                    if bt.trader_id == trader_id and bt.original_token_id == token_id:
                        bt.pnl = 0.0

                created += 1
                logger.info(
                    "Manual redemption recorded: market=%s trader=%d size=%.4f pnl=%.4f",
                    market_title or condition_id[:12], trader_id, net_shares, pnl,
                )

    if created:
        session.commit()
    return created
