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
    "skipped_sell_only": "⏭️",
}

PAGE_SIZE = 50


def _load_history(
    trader_id: int | None = None,
    status_filter: str | None = None,
    page: int = 1,
    market_search: str = "",
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
                CopyTrade.original_price,
                CopyTrade.copy_size,
                CopyTrade.copy_price,
                CopyTrade.pnl,
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
        if market_search:
            q = q.filter(CopyTrade.market_title.ilike(f"%{market_search}%"))
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
            "Orig Position",
            "Orig Price",
            "Copy Position",
            "Copy Price",
            "PnL",
            "Status",
            "Error",
            "Order ID",
        ],
    )
    df["Orig Value"] = (df["Orig Position"] * df["Orig Price"]).round(2)
    df["Copy Value"] = (df["Copy Position"] * df["Copy Price"].fillna(0)).round(2)
    df["Status"] = df["Status"].map(lambda s: f"{STATUS_COLORS.get(s, '')} {s}")
    # Reorder columns
    df = df[["ID", "Executed At", "Label", "Wallet", "Market", "Outcome", "Side",
             "Orig Position", "Orig Price", "Orig Value",
             "Copy Position", "Copy Price", "Copy Value",
             "PnL", "Status", "Error", "Order ID"]]
    return df, total


def _get_traders() -> list[Trader]:
    with _SessionLocal() as session:
        return session.query(Trader).order_by(Trader.id).all()


def render() -> None:
    st.title("Copy-Trade History")

    traders = _get_traders()
    trader_options = {"All": None} | {f"{t.label or t.wallet_address}": t.id for t in traders}
    status_options = ["All", "success", "dry_run", "failed", "slippage_exceeded", "below_threshold", "position_limit"]

    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    with col1:
        selected_trader_label = st.selectbox("Trader", list(trader_options.keys()))
        trader_id = trader_options[selected_trader_label]
    with col2:
        status_filter = st.selectbox("Status", status_options)
    with col3:
        market_search = st.text_input("Search Market", placeholder="e.g. Lakers, Bitcoin...")
    with col4:
        page = st.number_input("Page", min_value=1, value=1, step=1)

    df, total = _load_history(trader_id=trader_id, status_filter=status_filter, page=int(page), market_search=market_search)
    st.caption(f"Total records: {total} | Page size: {PAGE_SIZE}")

    if df.empty:
        st.info("No trades found for the selected filters.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True, column_config={
            "Orig Position": st.column_config.NumberColumn(format="%.4f"),
            "Orig Price": st.column_config.NumberColumn(format="$%.4f"),
            "Orig Value": st.column_config.NumberColumn(format="$%.2f"),
            "Copy Position": st.column_config.NumberColumn(format="%.4f"),
            "Copy Price": st.column_config.NumberColumn(format="$%.4f"),
            "Copy Value": st.column_config.NumberColumn(format="$%.2f"),
            "PnL": st.column_config.NumberColumn(format="$%.2f"),
        })

    if st.button("🔄 Refresh"):
        st.rerun()
