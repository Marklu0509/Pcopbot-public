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


def fetch_trades(wallet_address: str) -> list[dict]:
    """Fetch recent trades for *wallet_address* from the Polymarket Data API.

    Returns a list of raw trade dicts, newest-first.
    """
    url = f"{settings.DATA_API_BASE}/trades"
    params = {"wallet": wallet_address, "limit": 100}
    try:
        resp = _http.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        # Some endpoints wrap in {"data": [...]}
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
    except requests.RequestException as exc:
        logger.warning("Failed to fetch market %s: %s", condition_id, exc)
        return {}


def parse_trade(raw: dict) -> dict[str, Any]:
    """Normalise a raw trade dict from the Data API into a consistent shape.

    Returned keys:
        trade_id, market, token_id, side, size, price, timestamp (datetime UTC)
    """
    # The Data API uses camelCase; handle both styles defensively.
    trade_id = raw.get("id") or raw.get("tradeId") or ""
    market = raw.get("market") or raw.get("conditionId") or ""
    token_id = raw.get("asset_id") or raw.get("tokenId") or raw.get("assetId") or ""
    side = (raw.get("side") or raw.get("type") or "BUY").upper()
    size = float(raw.get("size", 0) or raw.get("shares", 0) or 0)
    price = float(raw.get("price", 0) or 0)

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
