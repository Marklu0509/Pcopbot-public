"""Copy-trade history page with filtering and color-coded status."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from db.database import get_session_factory, init_db
from db.models import CopyTrade, Trader

init_db()
_SessionLocal = get_session_factory()

STATUS_COLORS = {
    "success": "🟢",
    "dry_run": "🔵",
    "failed": "🔴",
    "slippage_exceeded": "🟡",
    "below_threshold": "⚪",
    "position_limit": "🟠",
}

PAGE_SIZE = 50


def _load_history(
    trader_id: int | None = None,
    status_filter: str | None = None,
    page: int = 1,
) -> tuple[pd.DataFrame, int]:
    with _SessionLocal() as session:
        q = (
            session.query(
                CopyTrade.id,
                CopyTrade.executed_at,
                Trader.label,
                Trader.wallet_address,
                CopyTrade.market_title,
                CopyTrade.outcome,
                CopyTrade.original_side,
                CopyTrade.original_size,
                CopyTrade.copy_size,
                CopyTrade.copy_price,
                CopyTrade.status,
                CopyTrade.error_message,
                CopyTrade.order_id,
            )
            .join(Trader, Trader.id == CopyTrade.trader_id)
        )
        if trader_id:
            q = q.filter(CopyTrade.trader_id == trader_id)
        if status_filter and status_filter != "All":
            q = q.filter(CopyTrade.status == status_filter)
        total = q.count()
        rows = (
            q.order_by(CopyTrade.executed_at.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

    if not rows:
        return pd.DataFrame(), total

    df = pd.DataFrame(
        rows,
        columns=[
            "ID",
            "Executed At",
            "Label",
            "Wallet",
            "Market",
            "Outcome",
            "Side",
            "Orig Size",
            "Copy Size",
            "Copy Price",
            "Status",
            "Error",
            "Order ID",
        ],
    )
    df["Status"] = df["Status"].map(lambda s: f"{STATUS_COLORS.get(s, '')} {s}")
    return df, total


def _get_traders() -> list[Trader]:
    with _SessionLocal() as session:
        return session.query(Trader).order_by(Trader.id).all()


def render() -> None:
    st.title("Copy-Trade History")

    traders = _get_traders()
    trader_options = {"All": None} | {f"{t.label or t.wallet_address}": t.id for t in traders}
    status_options = ["All", "success", "dry_run", "failed", "slippage_exceeded", "below_threshold", "position_limit"]

    col1, col2, col3 = st.columns(3)
    with col1:
        selected_trader_label = st.selectbox("Trader", list(trader_options.keys()))
        trader_id = trader_options[selected_trader_label]
    with col2:
        status_filter = st.selectbox("Status", status_options)
    with col3:
        page = st.number_input("Page", min_value=1, value=1, step=1)

    df, total = _load_history(trader_id=trader_id, status_filter=status_filter, page=int(page))
    st.caption(f"Total records: {total} | Page size: {PAGE_SIZE}")

    if df.empty:
        st.info("No trades found for the selected filters.")
    else:
        st.dataframe(df, use_container_width=True)

    if st.button("🔄 Refresh"):
        st.rerun()
