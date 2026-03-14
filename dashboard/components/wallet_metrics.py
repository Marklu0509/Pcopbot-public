"""Sidebar wallet metrics: total portfolio value from Polymarket Data API.

Fetches data with a 30-second cache and auto-refreshes via st.fragment.
Must be called inside a ``with st.sidebar:`` block.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import requests
import streamlit as st

logger = logging.getLogger(__name__)

_VALUE_URL = "https://data-api.polymarket.com/value"


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_portfolio_value(funder_address: str) -> float | None:
    """Return total portfolio value for the funder wallet, or None on failure.

    Endpoint returns: [{"user": "0x...", "value": 123.45}]
    """
    try:
        resp = requests.get(_VALUE_URL, params={"user": funder_address}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Response is a list: [{"user": "...", "value": ...}]
        if isinstance(data, list) and data:
            return float(data[0].get("value", 0.0) or 0.0)
        if isinstance(data, dict):
            return float(data.get("value", 0.0) or 0.0)
    except Exception as exc:
        logger.warning("Failed to fetch portfolio value for %s: %s", funder_address[:12], exc)
    return None


@st.fragment(run_every=timedelta(seconds=30))
def render_wallet_metrics(funder_address: str) -> None:
    """Render total portfolio value in the sidebar.

    Must be called inside a ``with st.sidebar:`` block.
    Auto-refreshes every 30 seconds via st.fragment.
    """
    total = _fetch_portfolio_value(funder_address)

    if total is None:
        st.caption("💰 資產：無法取得")
        return

    st.metric("💼 總資產 (USDC)", f"${total:,.2f}")
