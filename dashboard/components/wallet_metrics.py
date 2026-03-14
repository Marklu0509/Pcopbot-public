"""Sidebar wallet metrics: cash balance, positions value, total portfolio value.

Fetches data from the Polymarket Data API with a 30-second cache and auto-refreshes
via st.fragment so the numbers update without requiring user interaction.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import requests
import streamlit as st

logger = logging.getLogger(__name__)

_BALANCE_URL = "https://data-api.polymarket.com/balance"
_VALUE_URL   = "https://data-api.polymarket.com/value"


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_cash_balance(funder_address: str) -> float | None:
    """Return USDC cash balance for the funder wallet, or None on failure."""
    try:
        resp = requests.get(_BALANCE_URL, params={"user": funder_address}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return float(data.get("balance", data.get("value", 0.0)) or 0.0)
        if isinstance(data, (int, float)):
            return float(data)
    except Exception as exc:
        logger.warning("Failed to fetch cash balance for %s: %s", funder_address[:12], exc)
    return None


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_portfolio_value(funder_address: str) -> float | None:
    """Return total portfolio value (positions + cash) for the funder wallet, or None on failure."""
    try:
        resp = requests.get(_VALUE_URL, params={"user": funder_address}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return float(data.get("value", data.get("portfolioValue", 0.0)) or 0.0)
        if isinstance(data, (int, float)):
            return float(data)
    except Exception as exc:
        logger.warning("Failed to fetch portfolio value for %s: %s", funder_address[:12], exc)
    return None


@st.fragment(run_every=timedelta(seconds=30))
def render_wallet_sidebar(funder_address: str) -> None:
    """Render cash balance, positions value and total portfolio value in the sidebar.

    Auto-refreshes every 30 seconds via st.fragment.
    """
    cash = _fetch_cash_balance(funder_address)
    total = _fetch_portfolio_value(funder_address)

    if cash is None and total is None:
        st.sidebar.caption("💰 餘額：無法取得")
        return

    cash_val   = cash if cash is not None else 0.0
    total_val  = total if total is not None else 0.0
    pos_val    = max(0.0, total_val - cash_val)

    st.sidebar.markdown("**💼 帳戶餘額**")
    c1, c2 = st.sidebar.columns(2)
    c1.metric("現金 (USDC)", f"${cash_val:,.2f}")
    c2.metric("持倉價值", f"${pos_val:,.2f}")
    st.sidebar.metric("總資產", f"${total_val:,.2f}")
