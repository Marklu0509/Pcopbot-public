"""Trader configuration management page with per-trader detail tabs."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from db.database import get_session_factory, init_db
from db.models import CopyTrade, Trader
from bot.watermark import set_watermark

init_db()
_SessionLocal = get_session_factory()

STATUS_ICONS = {
    "success": "🟢",
    "dry_run": "🔵",
    "failed": "🔴",
    "slippage_exceeded": "🟡",
    "below_threshold": "⚪",
    "position_limit": "🟠",
}


def _get_all_traders():
    with _SessionLocal() as session:
        return session.query(Trader).order_by(Trader.id).all()


def _add_trader(data: dict) -> str | None:
    with _SessionLocal() as session:
        existing = session.query(Trader).filter(Trader.wallet_address == data["wallet_address"]).first()
        if existing:
            return "Wallet address already exists."
        trader = Trader(**data)
        session.add(trader)
        session.commit()
        session.refresh(trader)
        # Set watermark immediately upon adding
        set_watermark(session, trader)
    return None


def _update_trader(trader_id: int, data: dict) -> None:
    with _SessionLocal() as session:
        trader = session.get(Trader, trader_id)
        if trader:
            for key, value in data.items():
                setattr(trader, key, value)
            trader.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.commit()


def _toggle_trader(trader_id: int, is_active: bool) -> None:
    _update_trader(trader_id, {"is_active": is_active})


def _load_trader_trades(trader_id: int) -> pd.DataFrame:
    """Load all copy trades for a trader, newest first."""
    with _SessionLocal() as session:
        rows = (
            session.query(
                CopyTrade.id,
                CopyTrade.executed_at,
                CopyTrade.original_market,
                CopyTrade.original_token_id,
                CopyTrade.original_side,
                CopyTrade.original_size,
                CopyTrade.original_price,
                CopyTrade.copy_size,
                CopyTrade.copy_price,
                CopyTrade.status,
                CopyTrade.pnl,
                CopyTrade.error_message,
                CopyTrade.order_id,
            )
            .filter(CopyTrade.trader_id == trader_id)
            .order_by(CopyTrade.executed_at.desc())
            .all()
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows,
        columns=[
            "ID", "Time", "Market", "Token ID", "Side",
            "Orig Size", "Orig Price", "Copy Size", "Copy Price",
            "Status", "PnL", "Error", "Order ID",
        ],
    )
    df["Status"] = df["Status"].map(lambda s: f"{STATUS_ICONS.get(s, '')} {s}")
    return df


def _load_trader_holdings(trader_id: int) -> pd.DataFrame:
    """Aggregate current holdings per token for a trader.

    Net position = sum of BUY copy sizes minus sum of SELL copy sizes
    for successful / dry_run trades.
    """
    with _SessionLocal() as session:
        rows = (
            session.query(
                CopyTrade.original_market,
                CopyTrade.original_token_id,
                CopyTrade.original_side,
                CopyTrade.copy_size,
                CopyTrade.copy_price,
                CopyTrade.pnl,
            )
            .filter(
                CopyTrade.trader_id == trader_id,
                CopyTrade.status.in_(["success", "dry_run"]),
            )
            .all()
        )
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["Market", "Token ID", "Side", "Size", "Price", "PnL"])

    # Calculate net position per token
    holdings: list[dict] = []
    for (market, token_id), group in df.groupby(["Market", "Token ID"]):
        buy_size = group.loc[group["Side"] == "BUY", "Size"].sum()
        sell_size = group.loc[group["Side"] == "SELL", "Size"].sum()
        net_size = buy_size - sell_size
        avg_price = (
            (group["Price"] * group["Size"]).sum() / group["Size"].sum()
            if group["Size"].sum() > 0 else 0
        )
        total_pnl = group["PnL"].sum()
        trade_count = len(group)
        holdings.append({
            "Market": market,
            "Token ID": token_id,
            "Buy Size": round(buy_size, 4),
            "Sell Size": round(sell_size, 4),
            "Net Position": round(net_size, 4),
            "Avg Price": round(avg_price, 4),
            "Total PnL": round(total_pnl, 2),
            "Trades": trade_count,
        })

    return pd.DataFrame(holdings)


def _render_trader_detail(t) -> None:
    """Render the detail view for a single trader inside its tab."""
    # ── Info card ──
    st.markdown(f"### {'🟢' if t.is_active else '🔴'} {t.label or 'Unnamed'}")
    st.code(t.wallet_address, language=None)

    col1, col2, col3 = st.columns(3)
    col1.metric("Sizing", f"{t.sizing_mode.title()}")
    if t.sizing_mode == "fixed":
        col2.metric("Fixed Amt", f"${t.fixed_amount:.2f}")
    else:
        col2.metric("Proportional", f"{t.proportional_pct:.1f}%")
    col3.metric("Max Position", f"${t.max_position_limit:.2f}")

    col4, col5, col6 = st.columns(3)
    col4.metric("Max Slippage", f"{t.max_slippage:.1f}%")
    col5.metric("Min Threshold", f"${t.min_trade_threshold:.2f}")
    col6.metric("Watermark", str(t.watermark_timestamp or "—"))

    # ── Toggle & Edit ──
    new_active = st.toggle("Active", value=t.is_active, key=f"toggle_{t.id}")
    if new_active != t.is_active:
        _toggle_trader(t.id, new_active)
        st.rerun()

    with st.expander("⚙️ Edit Parameters"):
        with st.form(key=f"edit_{t.id}"):
            label = st.text_input("Label", value=t.label or "")
            sizing_mode = st.selectbox(
                "Sizing mode",
                ["fixed", "proportional"],
                index=0 if t.sizing_mode == "fixed" else 1,
            )
            fixed_amount = st.number_input("Fixed amount ($)", value=t.fixed_amount, min_value=0.0)
            proportional_pct = st.number_input("Proportional %", value=t.proportional_pct, min_value=0.0, max_value=100.0)
            max_position_limit = st.number_input("Max position limit ($)", value=t.max_position_limit, min_value=0.0)
            max_slippage = st.number_input("Max slippage (%)", value=t.max_slippage, min_value=0.0, max_value=100.0)
            min_trade_threshold = st.number_input("Min trade threshold ($)", value=t.min_trade_threshold, min_value=0.0)
            if st.form_submit_button("Save"):
                _update_trader(
                    t.id,
                    {
                        "label": label,
                        "sizing_mode": sizing_mode,
                        "fixed_amount": fixed_amount,
                        "proportional_pct": proportional_pct,
                        "max_position_limit": max_position_limit,
                        "max_slippage": max_slippage,
                        "min_trade_threshold": min_trade_threshold,
                    },
                )
                st.success("Saved!")
                st.rerun()

    st.divider()

    # ── Holdings ──
    st.subheader("📊 Holdings")
    holdings_df = _load_trader_holdings(t.id)
    if holdings_df.empty:
        st.info("No holdings yet.")
    else:
        # Summary metrics
        total_net = holdings_df["Net Position"].sum()
        total_pnl = holdings_df["Total PnL"].sum()
        num_tokens = len(holdings_df)
        mc1, mc2, mc3 = st.columns(3)
        mc1.metric("Tokens Held", num_tokens)
        mc2.metric("Net Exposure", f"{total_net:.4f}")
        mc3.metric("Total PnL", f"${total_pnl:.2f}")
        st.dataframe(
            holdings_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Total PnL": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

    st.divider()

    # ── Trade History ──
    st.subheader("📜 Trade History")
    trades_df = _load_trader_trades(t.id)
    if trades_df.empty:
        st.info("No trades recorded yet.")
    else:
        st.caption(f"Total: {len(trades_df)} trades")
        st.dataframe(
            trades_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "PnL": st.column_config.NumberColumn(format="$%.2f"),
                "Orig Price": st.column_config.NumberColumn(format="%.4f"),
                "Copy Price": st.column_config.NumberColumn(format="%.4f"),
            },
        )


def render() -> None:
    st.title("Tracked Traders")
    st.caption("Manage the wallets you want to copy-trade. Select a trader tab to view details.")

    traders = _get_all_traders()

    if traders:
        # ── Per-trader tabs ──
        tab_labels = [
            f"{'🟢' if t.is_active else '🔴'} {t.label or t.wallet_address[:10]}…"
            for t in traders
        ]
        tabs = st.tabs(tab_labels)
        for tab, t in zip(tabs, traders):
            with tab:
                _render_trader_detail(t)
    else:
        st.info("No traders tracked yet. Add one below.")

    st.divider()
    st.subheader("➕ Add New Trader")
    with st.form("add_trader"):
        wallet_address = st.text_input("Wallet address *")
        label = st.text_input("Label (optional)")
        sizing_mode = st.selectbox("Sizing mode", ["fixed", "proportional"])
        fixed_amount = st.number_input("Fixed amount ($)", value=50.0, min_value=0.0)
        proportional_pct = st.number_input("Proportional %", value=10.0, min_value=0.0, max_value=100.0)
        max_position_limit = st.number_input("Max position limit ($)", value=500.0, min_value=0.0)
        max_slippage = st.number_input("Max slippage (%)", value=2.0, min_value=0.0, max_value=100.0)
        min_trade_threshold = st.number_input("Min trade threshold ($)", value=5.0, min_value=0.0)

        if st.form_submit_button("Add Trader"):
            if not wallet_address.strip():
                st.error("Wallet address is required.")
            else:
                err = _add_trader(
                    {
                        "wallet_address": wallet_address.strip(),
                        "label": label.strip(),
                        "sizing_mode": sizing_mode,
                        "fixed_amount": fixed_amount,
                        "proportional_pct": proportional_pct,
                        "max_position_limit": max_position_limit,
                        "max_slippage": max_slippage,
                        "min_trade_threshold": min_trade_threshold,
                        "is_active": True,
                    }
                )
                if err:
                    st.error(err)
                else:
                    st.success("Trader added!")
                    st.rerun()
