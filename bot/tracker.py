"""Polls Polymarket Data API for new trades per tracked trader."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import settings

logger = logging.getLogger(__name__)

_RETRY_TOTAL = 3
_BACKOFF_FACTOR = 0.5


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=_RETRY_TOTAL,
        backoff_factor=_BACKOFF_FACTOR,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_http = _make_session()


def fetch_trades(wallet_address: str, limit: int = 100) -> list[dict]:
    """Fetch recent trades for *wallet_address* from the Polymarket Data API.

    Uses the ``/activity`` endpoint with ``user`` param, which correctly
    filters by wallet.  Returns a list of raw trade dicts, newest-first.
    """
    url = f"{settings.DATA_API_BASE}/activity"
    params = {"user": wallet_address, "limit": limit}
    try:
        resp = _http.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            # Only keep actual TRADE entries (the endpoint may also
            # return other activity types such as REDEEM, DEPOSIT, etc.)
            return [d for d in data if d.get("type", "").upper() == "TRADE"]
        return data.get("data", [])
    except requests.RequestException as exc:
        logger.error("Failed to fetch trades for %s: %s", wallet_address, exc)
        return []


def fetch_market(condition_id: str) -> dict:
    """Fetch market metadata from the Gamma API."""
    url = f"{settings.GAMMA_API_BASE}/markets/{condition_id}"
    try:
        resp = _http.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        status_code = getattr(exc.response, "status_code", None)
        if status_code == 422:
            logger.info("Market %s not available from Gamma endpoint (422).", condition_id)
            return {}
        logger.warning("Failed to fetch market %s: %s", condition_id, exc)
        return {}
    except requests.RequestException as exc:
        logger.warning("Failed to fetch market %s: %s", condition_id, exc)
        return {}


def fetch_token_prices(condition_ids: list[str]) -> dict[str, float]:
    """Fetch current prices for tokens by their market condition_ids.

    Queries the Gamma API and returns a mapping of
    ``{token_id: current_price}`` for every token across the requested
    markets.
    """
    price_map: dict[str, float] = {}
    # Batch by condition_id (Gamma API handles one market per request)
    for cid in condition_ids:
        try:
            data = fetch_market(cid)
            if not data:
                continue
            token_ids = data.get("clobTokenIds", [])
            outcome_prices = data.get("outcomePrices", [])
            if isinstance(token_ids, str):
                import json
                token_ids = json.loads(token_ids)
            if isinstance(outcome_prices, str):
                import json
                outcome_prices = json.loads(outcome_prices)
            for tid, price_str in zip(token_ids, outcome_prices):
                try:
                    price_map[tid] = float(price_str)
                except (ValueError, TypeError):
                    pass
        except Exception as exc:
            logger.warning("Error fetching prices for market %s: %s", cid, exc)
    return price_map


def parse_trade(raw: dict) -> dict[str, Any]:
    """Normalise a raw trade dict from the Data API ``/activity`` endpoint.

    Returned keys:
        trade_id, market, token_id, side, size, price, timestamp (datetime UTC),
        market_title, outcome
    """
    # /activity uses transactionHash as unique identifier
    trade_id = raw.get("transactionHash") or raw.get("id") or raw.get("tradeId") or ""
    market = raw.get("conditionId") or raw.get("market") or ""
    token_id = raw.get("asset") or raw.get("asset_id") or raw.get("tokenId") or raw.get("assetId") or ""
    side = (raw.get("side") or raw.get("type") or "BUY").upper()
    size = float(raw.get("size", 0) or raw.get("shares", 0) or 0)
    price = float(raw.get("price", 0) or 0)
    market_title = raw.get("title") or ""
    outcome = raw.get("outcome") or ""

    ts_raw = raw.get("timestamp") or raw.get("createdAt") or raw.get("created_at") or 0
    if isinstance(ts_raw, (int, float)):
        timestamp = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
    else:
        try:
            timestamp = datetime.fromisoformat(str(ts_raw).rstrip("Z")).replace(tzinfo=timezone.utc)
        except ValueError:
            timestamp = datetime.now(timezone.utc)

    return {
        "trade_id": str(trade_id),
        "market": market,
        "token_id": token_id,
        "side": side,
        "size": size,
        "price": price,
        "timestamp": timestamp,
        "market_title": market_title,
        "outcome": outcome,
    }


def get_new_trades(wallet_address: str, watermark: datetime) -> list[dict[str, Any]]:
    """Return parsed trades that are strictly AFTER *watermark*.

    Results are sorted oldest-first so callers can process in chronological order.
    """
    raw_trades = fetch_trades(wallet_address)
    parsed = [parse_trade(t) for t in raw_trades]
    # Strip tz for comparison (watermark is naive UTC)
    wm_naive = watermark.replace(tzinfo=None) if watermark.tzinfo else watermark
    new = [
        t for t in parsed
        if t["timestamp"].replace(tzinfo=None) > wm_naive
    ]
    new.sort(key=lambda t: t["timestamp"])
    return new


def fetch_prices_by_token_ids(token_ids: list[str]) -> dict[str, float]:
    """Fetch current prices using token IDs directly via Gamma API.

    Queries ``/markets?clob_token_ids=[...]`` which works for both active
    and recently-resolved markets (unlike the ``/markets/{condition_id}``
    path which returns 422 for resolved markets).

    Batches requests in chunks of 5 to avoid URL length limits.
    Returns ``{token_id: price}`` for every token found.
    """
    if not token_ids:
        return {}

    import json as _json

    price_map: dict[str, float] = {}
    chunk_size = 5
    for i in range(0, len(token_ids), chunk_size):
        chunk = token_ids[i : i + chunk_size]
        try:
            resp = _http.get(
                f"{settings.GAMMA_API_BASE}/markets",
                params={"clob_token_ids": _json.dumps(chunk)},
                timeout=15,
            )
            if not resp.ok:
                continue
            markets = resp.json()
            if not isinstance(markets, list):
                markets = [markets]
            for data in markets:
                t_ids = data.get("clobTokenIds", [])
                prices = data.get("outcomePrices", [])
                if isinstance(t_ids, str):
                    t_ids = _json.loads(t_ids)
                if isinstance(prices, str):
                    prices = _json.loads(prices)
                for tid, p in zip(t_ids, prices):
                    try:
                        price_map[str(tid)] = float(p)
                    except (ValueError, TypeError):
                        pass
        except Exception as exc:
            logger.warning("Error fetching prices by token IDs (chunk %d): %s", i, exc)

    return price_map


def fetch_complement_token_ids(
    token_ids: list[str],
    condition_ids: list[str] | None = None,
) -> dict[str, str]:
    """Return ``{token_id: complement_token_id}`` for binary markets.

    Two strategies:
    1. Gamma ``/markets?clob_token_ids=[...]`` — works for active markets.
    2. CLOB REST API ``/markets/{condition_id}`` fallback — works even for
       resolved markets where Gamma returns 422/empty.

    *condition_ids* is an optional list of condition IDs to try via CLOB
    for any tokens not found via Gamma.
    """
    if not token_ids:
        return {}

    import json as _json

    complement_map: dict[str, str] = {}
    found_tokens: set[str] = set()

    # --- Strategy 1: Gamma batch query ---
    try:
        resp = _http.get(
            f"{settings.GAMMA_API_BASE}/markets",
            params={"clob_token_ids": _json.dumps(token_ids)},
            timeout=15,
        )
        if resp.ok:
            markets = resp.json()
            if not isinstance(markets, list):
                markets = [markets]
            for data in markets:
                t_ids = data.get("clobTokenIds", [])
                if isinstance(t_ids, str):
                    t_ids = _json.loads(t_ids)
                if len(t_ids) == 2:
                    complement_map[str(t_ids[0])] = str(t_ids[1])
                    complement_map[str(t_ids[1])] = str(t_ids[0])
                    found_tokens.update(str(t) for t in t_ids)
    except Exception as exc:
        logger.warning("Gamma complement lookup failed: %s", exc)

    # --- Strategy 2: CLOB REST API fallback for missing tokens ---
    missing = [t for t in token_ids if t not in found_tokens]
    if missing and condition_ids:
        for cid in set(condition_ids):
            try:
                resp = _http.get(
                    f"https://clob.polymarket.com/markets/{cid}",
                    timeout=10,
                )
                if not resp.ok:
                    continue
                market_data = resp.json()
                tokens = market_data.get("tokens", [])
                if len(tokens) == 2:
                    t0 = str(tokens[0].get("token_id", ""))
                    t1 = str(tokens[1].get("token_id", ""))
                    if t0 and t1:
                        complement_map[t0] = t1
                        complement_map[t1] = t0
                        found_tokens.update([t0, t1])
            except Exception as exc:
                logger.warning("CLOB complement lookup failed for %s: %s", cid, exc)

    return complement_map


def fetch_position_prices(wallet_address: str) -> dict[str, float]:
    """Fetch current prices from a wallet's Data API positions.

    Returns ``{token_id: curPrice}`` for every position held by the wallet.
    Unlike CLOB orderbook bids, ``curPrice`` accounts for complement matching
    in binary markets (e.g. selling Yes@0.999 via BUY No@0.001).
    """
    url = f"{settings.DATA_API_BASE}/positions"
    params = {"user": wallet_address}
    try:
        resp = _http.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            data = data.get("data", [])
    except requests.RequestException as exc:
        logger.warning("Failed to fetch position prices for %s: %s", wallet_address, exc)
        return {}

    price_map: dict[str, float] = {}
    for raw in data:
        asset = raw.get("asset", "")
        cur_price = float(raw.get("curPrice", 0) or 0)
        if asset and cur_price > 0:
            price_map[asset] = cur_price
    return price_map


def fetch_positions(wallet_address: str) -> list[dict[str, Any]]:
    """Fetch current open positions for *wallet_address* from the Data API.

    Returns a list of dicts with keys:
        condition_id, asset_id, market_title, outcome, size, avg_price,
        initial_value, current_value, pnl, pnl_pct, cur_price
    """
    url = f"{settings.DATA_API_BASE}/positions"
    params = {"user": wallet_address}
    try:
        resp = _http.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            data = data.get("data", [])
    except requests.RequestException as exc:
        logger.error("Failed to fetch positions for %s: %s", wallet_address, exc)
        return []

    positions = []
    for raw in data:
        size = float(raw.get("size", 0) or 0)
        if size <= 0:
            continue  # skip closed / zero positions
        positions.append({
            "condition_id": raw.get("conditionId", ""),
            "asset_id": raw.get("asset", ""),
            "market_title": raw.get("title", ""),
            "outcome": raw.get("outcome", ""),
            "size": size,
            "avg_price": float(raw.get("avgPrice", 0) or 0),
            "initial_value": float(raw.get("initialValue", 0) or 0),
            "current_value": float(raw.get("currentValue", 0) or 0),
            "pnl": float(raw.get("cashPnl", 0) or 0),
            "pnl_pct": float(raw.get("percentPnl", 0) or 0),
            "cur_price": float(raw.get("curPrice", 0) or 0),
        })
    return positions
