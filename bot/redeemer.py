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
from db.models import CopyTrade, Trader

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Polygon contract addresses ───────────────────────────────────────────────
USDC_ADDRESS     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Polygon USDC
CTF_ADDRESS      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Gnosis CTF
NEG_RISK_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # NegRiskAdapter
POLYGON_RPC      = "https://polygon-rpc.com"
# Public fallback RPCs tried in order when the primary is unreachable
_POLYGON_RPC_FALLBACKS = [
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://rpc.ankr.com/polygon",
]
_RELAYER_URL = "https://relayer-v2.polymarket.com/submit"
_ZERO_ADDR   = "0x0000000000000000000000000000000000000000"

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

_SAFE_ABI_NONCE = [
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
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

def _get_safe_nonce(w3, safe_address: str) -> int:
    """Read the current nonce from a Gnosis Safe contract (read-only, no gas)."""
    safe = w3.eth.contract(
        address=w3.to_checksum_address(safe_address),
        abi=_SAFE_ABI_NONCE,
    )
    return safe.functions.nonce().call()


def _compute_safe_tx_hash(
    chain_id: int, safe_address: str, to: str, data: bytes, nonce: int
) -> bytes:
    """Compute the EIP-712 Safe transaction hash for a gasless CALL operation."""
    from eth_abi import encode as _enc
    from web3 import Web3

    zero = Web3.to_checksum_address(_ZERO_ADDR)

    domain_typehash = Web3.keccak(
        text="EIP712Domain(uint256 chainId,address verifyingContract)"
    )
    safe_tx_typehash = Web3.keccak(
        text=(
            "SafeTx(address to,uint256 value,bytes data,uint8 operation,"
            "uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,"
            "address gasToken,address refundReceiver,uint256 nonce)"
        )
    )

    domain_sep = Web3.keccak(
        _enc(
            ["bytes32", "uint256", "address"],
            [domain_typehash, chain_id, Web3.to_checksum_address(safe_address)],
        )
    )
    struct_hash = Web3.keccak(
        _enc(
            [
                "bytes32", "address", "uint256", "bytes32", "uint8",
                "uint256", "uint256", "uint256", "address", "address", "uint256",
            ],
            [
                safe_tx_typehash,
                Web3.to_checksum_address(to),
                0,                   # value
                Web3.keccak(data),   # keccak256(data)
                0,                   # operation = CALL
                0,                   # safeTxGas
                0,                   # baseGas
                0,                   # gasPrice
                zero,                # gasToken
                zero,                # refundReceiver
                nonce,
            ],
        )
    )
    return Web3.keccak(b"\x19\x01" + domain_sep + struct_hash)


def _sign_safe_hash(account, hash_bytes: bytes) -> str:
    """Sign a raw Safe tx hash with the account key. Returns 0x-prefixed hex."""
    try:
        signed = account.unsafe_sign_hash(hash_bytes)
    except AttributeError:
        signed = account.signHash(hash_bytes)  # older eth_account fallback
    return "0x" + signed.signature.hex()


def _submit_via_relayer(
    from_addr: str, to: str, proxy_wallet: str,
    data: bytes, nonce: int, signature: str,
) -> str:
    """POST a Safe transaction to the Polymarket Relayer (gasless). Returns transaction ID."""
    import requests as _req

    relayer_key = (settings.POLYMARKET_RELAYER_API_KEY or "").strip()
    if not relayer_key:
        raise RuntimeError(
            "POLYMARKET_RELAYER_API_KEY is not set. "
            "Get it from Polymarket Settings → API Keys and add to .env."
        )

    payload = {
        "from": from_addr,
        "to": to,
        "proxyWallet": proxy_wallet,
        "data": "0x" + data.hex(),
        "nonce": str(nonce),
        "signature": signature,
        "signatureParams": {
            "gasPrice": "0",
            "operation": "0",
            "safeTxnGas": "0",
            "baseGas": "0",
            "gasToken": _ZERO_ADDR,
            "refundReceiver": _ZERO_ADDR,
        },
        "type": "SAFE",
    }

    key_address = (settings.POLYMARKET_RELAYER_API_KEY_ADDRESS or "").strip() or from_addr

    resp = _req.post(
        _RELAYER_URL,
        json=payload,
        headers={
            "RELAYER_API_KEY": relayer_key,
            "RELAYER_API_KEY_ADDRESS": key_address,
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if not resp.ok:
        # Sanitize error body to avoid leaking internal details in logs
        body = resp.text[:200] if resp.text else ""
        # Redact any accidentally echoed API keys
        if relayer_key and relayer_key in body:
            body = body.replace(relayer_key, "[REDACTED]")
        raise RuntimeError(f"Relayer API error {resp.status_code}: {body}")

    result = resp.json()
    return result.get("transactionID") or result.get("transactionHash") or ""


def _condition_bytes(condition_id: str) -> bytes:
    """Convert a 0x-prefixed hex condition_id string to 32 bytes."""
    hex_str = condition_id.removeprefix("0x").zfill(64)
    return bytes.fromhex(hex_str)


def _get_web3():
    from web3 import Web3

    # Build ordered list: env override first, then hardcoded primary, then fallbacks
    rpc_primary = (settings.POLYGON_RPC_URL or "").strip() or POLYGON_RPC
    candidates = [rpc_primary] + [r for r in _POLYGON_RPC_FALLBACKS if r != rpc_primary]

    last_err: Exception | None = None
    for rpc_url in candidates:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                if rpc_url != rpc_primary:
                    logger.info("Connected to Polygon RPC via fallback: %s", rpc_url)
                return w3
            last_err = RuntimeError(f"is_connected() returned False for {rpc_url}")
        except Exception as exc:
            last_err = exc
            logger.debug("Polygon RPC %s unreachable: %s", rpc_url, exc)

    raise RuntimeError(f"Cannot connect to any Polygon RPC. Last error: {last_err}")


def _get_account(w3, use_funder_key: bool = False):
    """Return an Account for signing transactions.

    When ``use_funder_key=True`` and POLYMARKET_FUNDER_PRIVATE_KEY is set,
    returns the funder wallet account (needed for on-chain CTF redemptions in
    proxy-wallet setups where tokens are held by the funder, not the proxy).
    Falls back to POLYMARKET_PRIVATE_KEY when the funder key is not set.
    """
    from eth_account import Account

    raw_key = ""
    if use_funder_key:
        raw_key = (settings.POLYMARKET_FUNDER_PRIVATE_KEY or "").strip()
    if not raw_key:
        raw_key = (settings.POLYMARKET_PRIVATE_KEY or "").strip()

    if not raw_key.startswith("0x"):
        raw_key = "0x" + raw_key
    return Account.from_key(raw_key)


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
            import json as _json
            import requests as _req

            # Try Gamma batch endpoint first — it works for resolved markets
            # and returns full data including negRisk.
            try:
                resp = _req.get(
                    f"{settings.GAMMA_API_BASE}/markets",
                    params={"clob_token_ids": _json.dumps([token_id])},
                    timeout=15,
                )
                if resp.ok:
                    markets = resp.json()
                    if not isinstance(markets, list):
                        markets = [markets]
                    if markets:
                        data = markets[0]
                        logger.info(
                            "Market %s: Gamma /markets/{id} returned 422, "
                            "recovered via batch endpoint.",
                            condition_id[:16],
                        )
            except Exception as exc:
                logger.debug("Gamma batch fallback failed for %s: %s", condition_id[:16], exc)

            # If batch endpoint returned data, process it normally below
            if data:
                resolved = data.get("resolved", False) or data.get("closed", False)
                if not resolved:
                    return None
                # Fall through to the normal parsing logic below
            else:
                # Last resort: price-only fallback
                from bot.tracker import fetch_prices_by_token_ids, fetch_position_prices
                price_map = fetch_prices_by_token_ids([token_id])
                price = price_map.get(token_id, 0.0)

                if price < 0.99:
                    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
                    if funder:
                        try:
                            funder_prices = fetch_position_prices(funder)
                            funder_price = funder_prices.get(token_id, 0.0)
                            if funder_price > price:
                                price = funder_price
                        except Exception:
                            pass

                if price >= 0.99:
                    logger.warning(
                        "Market %s: all Gamma endpoints failed — using price-only "
                        "fallback (assuming binary). If neg_risk, redeem manually.",
                        condition_id[:16],
                    )
                    return {
                        "neg_risk": False,
                        "condition_id": condition_id,
                        "token_info": {token_id: {"outcome": "", "price": price, "index": 0}},
                        "winner": "",
                    }
                return None
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
    """Redeem a winning binary CTF position. Returns tx/relayer ID string.

    When POLYMARKET_RELAYER_API_KEY is set, submits via Relayer (gasless Safe tx).
    Otherwise falls back to a direct on-chain tx (requires MATIC for gas).
    ``holder_address`` is the Gnosis Safe / funder wallet that holds the tokens.
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

    index_set = 1 << outcome_index
    data_hex = ctf.encodeABI(
        fn_name="redeemPositions",
        args=[
            w3.to_checksum_address(USDC_ADDRESS),
            b"\x00" * 32,
            _condition_bytes(condition_id),
            [index_set],
        ],
    )
    data_bytes = bytes.fromhex(data_hex.removeprefix("0x"))

    if (settings.POLYMARKET_RELAYER_API_KEY or "").strip():
        nonce     = _get_safe_nonce(w3, owner)
        safe_hash = _compute_safe_tx_hash(
            settings.POLYMARKET_CHAIN_ID, owner, CTF_ADDRESS, data_bytes, nonce
        )
        signature = _sign_safe_hash(account, safe_hash)
        tx_id = _submit_via_relayer(account.address, CTF_ADDRESS, owner, data_bytes, nonce, signature)
        logger.info("Binary redeem submitted via Relayer: id=%s", tx_id)
        return tx_id

    # Fallback: direct on-chain (requires MATIC for gas)
    tx = ctf.functions.redeemPositions(
        w3.to_checksum_address(USDC_ADDRESS),
        b"\x00" * 32,
        _condition_bytes(condition_id),
        [index_set],
    ).build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address),
        "gas":      250_000,
        "gasPrice": w3.eth.gas_price * 2,
    })
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        raise RuntimeError(f"Binary redeem tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def _redeem_neg_risk(
    w3, account, condition_id: str, token_id: str,
    holder_address: str | None = None,
) -> str:
    """Redeem a winning neg_risk position via NegRiskAdapter. Returns tx/relayer ID.

    When POLYMARKET_RELAYER_API_KEY is set, submits via Relayer (gasless Safe tx).
    ``holder_address`` is the Gnosis Safe / funder wallet that holds the tokens.
    """
    owner = holder_address or account.address
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
    data_hex = adapter.encodeABI(
        fn_name="redeemPositions",
        args=[_condition_bytes(condition_id), balance],
    )
    data_bytes = bytes.fromhex(data_hex.removeprefix("0x"))

    if (settings.POLYMARKET_RELAYER_API_KEY or "").strip():
        nonce     = _get_safe_nonce(w3, owner)
        safe_hash = _compute_safe_tx_hash(
            settings.POLYMARKET_CHAIN_ID, owner, NEG_RISK_ADDRESS, data_bytes, nonce
        )
        signature = _sign_safe_hash(account, safe_hash)
        tx_id = _submit_via_relayer(account.address, NEG_RISK_ADDRESS, owner, data_bytes, nonce, signature)
        logger.info("Neg-risk redeem submitted via Relayer: id=%s", tx_id)
        return tx_id

    # Fallback: direct on-chain (requires MATIC for gas)
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
            CopyTrade.status            == "success",
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
        original_timestamp  = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        copy_size           = net_shares,
        copy_price          = 1.0,
        status              = "success",
        order_id            = f"onchain:{tx_hash}",
        pnl                 = pnl,
    )
    session.add(redemption)

    # Zero out unrealized PnL on BUY entries for THIS trader only
    for ct in buy_trades:
        if ct.trader_id == sample.trader_id and ct.status == "success":
            ct.pnl = 0.0

    session.commit()
    logger.info(
        "Recorded redemption: market=%s shares=%.4f avg_buy=%.4f realized_pnl=%.4f tx=%s",
        sample.market_title or sample.original_market[:12],
        net_shares, avg_buy, pnl, tx_hash[:16],
    )


def _record_simulated_redemption(
    session: "Session",
    buy_trades: list[CopyTrade],
    trader_id: int,
    token_id: str,
    net_shares: float,
    market_title: str,
    outcome: str,
) -> None:
    """Create a simulated SELL record for dry_run trader redemptions."""
    from sqlalchemy import func
    from bot.executor import _get_avg_buy_price

    # Check for duplicate
    order_id_key = f"sim_redeem:{token_id[:20]}:t{trader_id}"
    if session.query(CopyTrade).filter(CopyTrade.order_id == order_id_key).first():
        return

    avg_buy = _get_avg_buy_price(session, trader_id, token_id, status_filter=["dry_run"])
    pnl = round((1.0 - avg_buy) * net_shares, 4)

    sample = buy_trades[0] if buy_trades else None
    redemption = CopyTrade(
        trader_id           = trader_id,
        original_trade_id   = f"sim_redemption:{token_id[:24]}",
        original_market     = sample.original_market if sample else "",
        original_token_id   = token_id,
        market_title        = market_title or (sample.market_title if sample else ""),
        outcome             = outcome or (sample.outcome if sample else ""),
        original_side       = "SELL",
        original_size       = net_shares,
        original_price      = 1.0,
        original_timestamp  = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        copy_size           = net_shares,
        copy_price          = 1.0,
        status              = "dry_run",
        order_id            = order_id_key,
        pnl                 = pnl,
        executed_at         = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    session.add(redemption)

    # Zero out unrealized PnL on dry_run BUY entries
    for ct in buy_trades:
        if ct.trader_id == trader_id and ct.status == "dry_run":
            ct.pnl = 0.0

    session.commit()
    logger.info(
        "Simulated redemption recorded: market=%s trader=%d shares=%.4f avg_buy=%.4f pnl=%.4f",
        market_title, trader_id, net_shares, avg_buy, pnl,
    )


def _record_expired_loss(
    session: "Session",
    trader_id: int,
    buy_trades: list[CopyTrade],
    net_shares: float,
    is_dry_run: bool = False,
) -> None:
    """Create a synthetic SELL at price=0 for a resolved losing position.

    Called when a market resolved and our token is the losing side (price ≈ 0).
    The full cost paid becomes a realized loss.
    """
    from sqlalchemy import func

    if not buy_trades:
        return

    record_status = "dry_run" if is_dry_run else "success"
    status_filter = ["dry_run"] if is_dry_run else ["success"]
    sample = buy_trades[0]
    order_id_key = f"expired_loss:{sample.original_market[:20]}:t{trader_id}:{sample.original_token_id[:12]}"
    if is_dry_run:
        order_id_key = f"sim_expired:{sample.original_market[:20]}:t{trader_id}:{sample.original_token_id[:12]}"
    if session.query(CopyTrade).filter(CopyTrade.order_id == order_id_key).first():
        return  # already recorded

    result = (
        session.query(
            func.coalesce(func.sum(CopyTrade.copy_size * CopyTrade.copy_price), 0.0),
            func.coalesce(func.sum(CopyTrade.copy_size), 0.0),
        )
        .filter(
            CopyTrade.trader_id         == trader_id,
            CopyTrade.original_token_id == sample.original_token_id,
            CopyTrade.original_side     == "BUY",
            CopyTrade.status.in_(status_filter),
        )
        .first()
    )
    total_cost, total_size = result
    avg_buy = (total_cost / total_size) if total_size and total_size > 0 else 0.0
    pnl = round(-avg_buy * net_shares, 4)  # paid avg_buy per share, got $0 back

    loss_record = CopyTrade(
        trader_id           = trader_id,
        original_trade_id   = f"expired:{sample.original_market[:24]}",
        original_market     = sample.original_market,
        original_token_id   = sample.original_token_id,
        market_title        = sample.market_title,
        outcome             = sample.outcome,
        original_side       = "SELL",
        original_size       = net_shares,
        original_price      = 0.0,
        original_timestamp  = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
        copy_size           = net_shares,
        copy_price          = 0.0,
        status              = record_status,
        order_id            = order_id_key,
        pnl                 = pnl,
        executed_at         = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    session.add(loss_record)

    for ct in buy_trades:
        if ct.trader_id == trader_id and ct.status in status_filter:
            ct.pnl = 0.0

    session.commit()
    logger.info(
        "Expired loss recorded: market=%s trader=%d shares=%.4f avg_buy=%.4f pnl=%.4f status=%s",
        sample.market_title or sample.original_market[:12],
        trader_id, net_shares, avg_buy, pnl, record_status,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def redeem_resolved_positions(session: "Session") -> int:
    """Check all open copy-trade BUY positions for resolved markets and auto-redeem.

    Returns the number of positions successfully redeemed.
    """
    # Per-trader dry_run is checked per-group below; no global gate needed

    if not settings.POLYMARKET_PRIVATE_KEY:
        logger.warning("POLYMARKET_PRIVATE_KEY not set — cannot auto-redeem")
        return 0

    funder_address = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()

    # ── Collect all open BUY positions (live + dry_run) ───────────────────────
    open_buys = (
        session.query(CopyTrade)
        .filter(
            CopyTrade.original_side == "BUY",
            CopyTrade.status.in_(["success", "dry_run"]),
            CopyTrade.original_market.is_not(None),
            CopyTrade.original_token_id.is_not(None),
        )
        .all()
    )

    # Group by (trader_id, condition_id, token_id)
    groups: dict[tuple, list[CopyTrade]] = defaultdict(list)
    for ct in open_buys:
        key = (ct.trader_id, ct.original_market, ct.original_token_id)
        groups[key].append(ct)

    # Fetch funder wallet's actual positions for accurate share counts
    from bot.tracker import fetch_positions as _fetch_positions
    funder_wallet_sizes: dict[str, float] = {}
    if funder_address:
        try:
            for p in _fetch_positions(funder_address):
                tid = p.get("asset_id", "")
                if tid and p.get("size", 0) > 0:
                    funder_wallet_sizes[tid] = p["size"]
        except Exception as exc:
            logger.warning("Failed to fetch funder positions for redeem: %s", exc)

    # Keep only groups where we actually hold shares (wallet or DB)
    from bot.executor import _get_net_holdings
    active: list[tuple] = []
    for (trader_id, condition_id, token_id), trades in groups.items():
        if not condition_id or not token_id:
            continue
        wallet_size = funder_wallet_sizes.pop(token_id, 0.0)
        db_net = _get_net_holdings(session, trader_id, token_id)
        net = wallet_size if wallet_size > 0 else db_net
        if net > 0:
            active.append((trader_id, condition_id, token_id, trades, net))

    # Also include wallet-only positions not tracked in DB
    if funder_wallet_sizes and open_buys:
        sample_trader_id = open_buys[0].trader_id
        for token_id, wallet_size in funder_wallet_sizes.items():
            if wallet_size > 0:
                active.append((sample_trader_id, "", token_id, [], wallet_size))

    if not active:
        return 0

    # Initialize web3 once for all redemptions.
    # Use funder key for signing if available (proxy-wallet setups where the
    # proxy key cannot sign on behalf of the funder for CTF redeemPositions).
    try:
        w3      = _get_web3()
        account = _get_account(w3, use_funder_key=True)
    except Exception as exc:
        logger.warning("Cannot init web3 for redemption: %s", exc)
        return 0

    # Determine the address that actually holds CTF tokens on-chain.
    holder_address = funder_address if funder_address else account.address

    using_funder_key = bool((settings.POLYMARKET_FUNDER_PRIVATE_KEY or "").strip())
    if funder_address and funder_address.lower() != account.address.lower():
        if using_funder_key:
            logger.info(
                "Proxy-wallet mode: signing with funder key=%s (holder=%s)",
                account.address[:12], holder_address[:12],
            )
        else:
            has_relayer = bool((settings.POLYMARKET_RELAYER_API_KEY or "").strip())
            if not has_relayer:
                logger.warning(
                    "Proxy-wallet mode: token holder=%s, signing with=%s. "
                    "Set POLYMARKET_RELAYER_API_KEY in .env for gasless auto-redemption, "
                    "or set POLYMARKET_FUNDER_PRIVATE_KEY if you have the funder wallet key.",
                    holder_address[:12], account.address[:12],
                )

    redeemed       = 0
    seen_conditions: set[str] = set()  # avoid double-redeeming same market in one pass

    for trader_id, condition_id, token_id, trades, net_shares in active:
        trader_obj = session.query(Trader).filter(Trader.id == trader_id).first()
        from bot.executor import is_trader_dry_run
        trader_is_dry = is_trader_dry_run(trader_obj, session) if trader_obj else True

        # For wallet-only positions, try to find condition_id from CLOB API
        if not condition_id:
            try:
                from bot.tracker import fetch_complement_token_ids
                comp_map = fetch_complement_token_ids([token_id])
                # We just need condition_id — fetch it from CLOB market endpoint
                import requests as _req
                resp = _req.get(
                    f"https://clob.polymarket.com/markets",
                    params={"token_id": token_id},
                    timeout=10,
                )
                if resp.ok:
                    mdata = resp.json()
                    condition_id = mdata.get("condition_id", "") if isinstance(mdata, dict) else ""
            except Exception:
                pass
            if not condition_id:
                logger.debug("Cannot find condition_id for wallet-only token %s, skipping", token_id[:12])
                continue

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
            # Cannot reliably detect losses from CLOB prices alone —
            # resolved winning tokens may also show price=0 after market closes.
            # Skip: market still live, or outcome uncertain.
            continue

        is_neg_risk   = market["neg_risk"]
        label         = (trades[0].market_title if trades else "") or condition_id[:12]
        outcome       = token_data["outcome"]
        outcome_index = token_data["index"]

        # ── Dry-run traders: simulate redemption (no on-chain tx) ──────────
        if trader_is_dry:
            logger.info(
                "Simulated redemption (dry_run): market=%r outcome=%r shares=%.4f",
                label, outcome, net_shares,
            )
            # Record simulated redemption with status="dry_run"
            _record_simulated_redemption(session, trades, trader_id, token_id, net_shares, label, outcome)
            redeemed += 1
            continue

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
            datetime.datetime.fromtimestamp(activity_ts, tz=datetime.timezone.utc).replace(tzinfo=None)
            if activity_ts else datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
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
            datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).replace(tzinfo=None) if ts else datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
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


def detect_expired_losses(session: "Session") -> int:
    """Record SELL at price=0 for positions where the market closed and our token lost.

    Uses Gamma /markets?clob_token_ids to check both price AND market status.
    Only triggers when Gamma confirms: closed/inactive AND outcomePrices[token] == 0.
    This is safe — winning tokens show ~1.0 even after resolution.

    Returns count of new loss records created.
    """
    import json as _json
    import requests as _req

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

    # Collect tokens that still have open holdings
    token_buys: dict[str, list[CopyTrade]] = {}
    for ct in open_buys:
        if ct.original_token_id:
            token_buys.setdefault(ct.original_token_id, []).append(ct)

    all_token_ids = list(token_buys.keys())
    if not all_token_ids:
        return 0

    # Fetch market data via Gamma — works for both active and recently-resolved
    markets_data: list = []
    try:
        resp = _req.get(
            f"{settings.GAMMA_API_BASE}/markets",
            params={"clob_token_ids": _json.dumps(all_token_ids)},
            timeout=15,
        )
        if resp.ok:
            markets_data = resp.json()
            if not isinstance(markets_data, list):
                markets_data = [markets_data]
        else:
            logger.info("detect_expired_losses: Gamma returned %s, using wallet fallback", resp.status_code)
    except Exception as exc:
        logger.warning("detect_expired_losses: Gamma fetch failed: %s, using wallet fallback", exc)

    # Build token_id -> {price, closed}
    token_info: dict[str, dict] = {}
    for mkt in markets_data:
        t_ids = mkt.get("clobTokenIds", [])
        prices = mkt.get("outcomePrices", [])
        if isinstance(t_ids, str):
            t_ids = _json.loads(t_ids)
        if isinstance(prices, str):
            prices = _json.loads(prices)
        # Market is closed when active=false OR closed=true
        is_closed = (not mkt.get("active", True)) or bool(mkt.get("closed", False))
        for tid, p in zip(t_ids, prices):
            try:
                token_info[str(tid)] = {"price": float(p), "closed": is_closed}
            except (ValueError, TypeError):
                pass

    # Fetch wallet positions to detect losses when Gamma has no data
    from bot.tracker import fetch_positions as _fetch_pos
    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    wallet_tokens: dict[str, float] = {}  # token_id -> cur_price
    if funder:
        try:
            for p in _fetch_pos(funder):
                tid = p.get("asset_id", "")
                if tid and p.get("size", 0) > 0:
                    wallet_tokens[tid] = float(p.get("cur_price", 0) or 0)
        except Exception:
            pass

    from bot.executor import _get_net_holdings
    created = 0
    for token_id, buys in token_buys.items():
        info = token_info.get(token_id)

        is_loss = False
        if info is not None and info["closed"] and info["price"] < 0.02:
            # Gamma confirms: closed market, losing token
            is_loss = True
        elif info is None and token_id not in wallet_tokens:
            # Gamma returned no data (422) AND wallet doesn't hold it.
            # Position is gone — treat as expired loss.
            is_loss = True
        elif info is None and wallet_tokens.get(token_id, 1.0) < 0.02:
            # Gamma returned no data AND wallet holds it at price ~0.
            # Token lost but shares still in wallet.
            is_loss = True

        if not is_loss:
            continue

        by_trader: dict[int, list] = {}
        for ct in buys:
            by_trader.setdefault(ct.trader_id, []).append(ct)

        for trader_id, trader_buys in by_trader.items():
            trader_obj = session.query(Trader).filter(Trader.id == trader_id).first()
            from bot.executor import is_trader_dry_run
            is_dry = is_trader_dry_run(trader_obj, session) if trader_obj else True
            sf = ["dry_run"] if is_dry else ["success"]
            net = _get_net_holdings(session, trader_id, token_id, status_filter=sf)
            if net < 0.1:
                continue  # skip rounding residuals
            _record_expired_loss(session, trader_id, trader_buys, net, is_dry_run=is_dry)
            created += 1

    return created
