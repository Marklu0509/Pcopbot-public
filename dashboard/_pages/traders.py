"""Trader configuration management page with per-trader detail tabs."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from db.database import get_session_factory, init_db
from db.models import CopyTrade, Position, Trader
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
                CopyTrade.market_title,
                CopyTrade.outcome,
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
            "ID", "Time", "Market", "Outcome", "Side",
            "Orig Size", "Orig Price", "Copy Size", "Copy Price",
            "Status", "PnL", "Error", "Order ID",
        ],
    )
    df["Status"] = df["Status"].map(lambda s: f"{STATUS_ICONS.get(s, '')} {s}")
    return df


def _load_trader_holdings(trader_id: int) -> pd.DataFrame:
    """Aggregate current holdings per token for a trader."""
    with _SessionLocal() as session:
        rows = (
            session.query(
                CopyTrade.market_title,
                CopyTrade.outcome,
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

    df = pd.DataFrame(rows, columns=["Market", "Outcome", "Side", "Size", "Price", "PnL"])

    holdings: list[dict] = []
    for (market, outcome), group in df.groupby(["Market", "Outcome"]):
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
            "Outcome": outcome,
            "Buy Size": round(buy_size, 4),
            "Sell Size": round(sell_size, 4),
            "Net Position": round(net_size, 4),
            "Avg Price": round(avg_price, 4),
            "Total PnL": round(total_pnl, 2),
            "Trades": trade_count,
        })

    return pd.DataFrame(holdings)


def _load_trader_positions(trader_id: int) -> pd.DataFrame:
    """Load pre-existing positions (fetched on startup) for a trader."""
    with _SessionLocal() as session:
        rows = (
            session.query(
                Position.market_title,
                Position.outcome,
                Position.size,
                Position.avg_price,
                Position.current_value,
                Position.pnl,
                Position.pnl_pct,
                Position.cur_price,
                Position.fetched_at,
            )
            .filter(Position.trader_id == trader_id)
            .order_by(Position.current_value.desc())
            .all()
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=[
        "Market", "Outcome", "Size", "Avg Price", "Value", "PnL", "PnL %", "Cur Price", "Fetched",
    ])


def _render_trader_detail(t) -> None:
    """Render the detail view for a single trader inside its tab."""
    # ── Info card ──
    st.markdown(f"### {'🟢' if t.is_active else '🔴'} {t.label or 'Unnamed'}")
    st.code(t.wallet_address, language=None)

    # ── Summary metrics ──
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Copy %/$", f"{t.proportional_pct:.0f}%" if t.sizing_mode == "proportional" else f"${t.fixed_amount:.2f}")
    c2.metric("Buy Slippage", f"{t.buy_slippage:.0f}%")
    c3.metric("TP", f"{t.tp_pct:.1f}%" if t.tp_pct else "—")
    c4.metric("SL", f"{t.sl_pct:.1f}%" if t.sl_pct else "—")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Total Spend Limit", f"${t.total_spend_limit:.2f}" if t.total_spend_limit else "—")
    c6.metric("Max / Trade", f"${t.max_per_trade:.2f}" if t.max_per_trade else "—")
    c7.metric("Max / Market", f"${t.max_per_market:.2f}" if t.max_per_market else "—")
    c8.metric("Sell Slippage", f"{t.sell_slippage:.0f}%")

    # ── Toggle active ──
    new_active = st.toggle("Active", value=t.is_active, key=f"toggle_{t.id}")
    if new_active != t.is_active:
        _toggle_trader(t.id, new_active)
        st.rerun()

    # ── Edit full parameters ──
    with st.expander("⚙️ Edit Copy-Trade Settings"):
        with st.form(key=f"edit_{t.id}"):
            st.markdown("##### Basic")
            label = st.text_input("Tag (Label)", value=t.label or "")

            st.markdown("##### Sizing")
            sizing_mode = st.selectbox(
                "Sizing mode",
                ["fixed", "proportional"],
                index=0 if t.sizing_mode == "fixed" else 1,
                key=f"sm_{t.id}",
            )
            fixed_amount = st.number_input("Fixed amount ($)", value=t.fixed_amount, min_value=0.0, key=f"fa_{t.id}")
            proportional_pct = st.number_input("Copy Percentage (%)", value=t.proportional_pct, min_value=0.0, max_value=100.0, key=f"pp_{t.id}")

            st.markdown("##### Buy Settings")
            buy_slippage = st.number_input("Market Order Slippage (%)", value=t.buy_slippage, min_value=0.0, max_value=100.0, key=f"bs_{t.id}")
            buy_at_min = st.checkbox("Below Min Limit, Buy at Min", value=t.buy_at_min, key=f"bam_{t.id}")

            st.markdown("##### Take-Profit / Stop-Loss")
            tp_pct = st.number_input("TP % (0 = disabled)", value=t.tp_pct, min_value=0.0, key=f"tp_{t.id}")
            sl_pct = st.number_input("SL % (0 = disabled)", value=t.sl_pct, min_value=0.0, key=f"sl_{t.id}")

            st.markdown("##### Filters")
            ignore_trades_under = st.number_input("Ignore Target Wallet Trades Under ($)", value=t.ignore_trades_under, min_value=0.0, key=f"itu_{t.id}")
            min_price = st.number_input("Min Price ($, 0 = no limit)", value=t.min_price, min_value=0.0, key=f"mnp_{t.id}")
            max_price = st.number_input("Max Price ($, 0 = no limit)", value=t.max_price, min_value=0.0, key=f"mxp_{t.id}")

            st.markdown("##### Spending / Position Limits")
            total_spend_limit = st.number_input("Total Spend Limit ($, 0 = no limit)", value=t.total_spend_limit, min_value=0.0, key=f"tsl_{t.id}")
            min_per_trade = st.number_input("Min Per Trade ($)", value=t.min_per_trade, min_value=0.0, key=f"mnt_{t.id}")
            max_per_yes_no = st.number_input("Max Per Yes/No ($, 0 = no limit)", value=t.max_per_yes_no, min_value=0.0, key=f"myn_{t.id}")
            max_per_trade = st.number_input("Max Per Trade ($, 0 = no limit)", value=t.max_per_trade, min_value=0.0, key=f"mxt_{t.id}")
            max_per_market = st.number_input("Max Per Market ($, 0 = no limit)", value=t.max_per_market, min_value=0.0, key=f"mxm_{t.id}")
            max_position_limit = st.number_input("Max Position Limit ($)", value=t.max_position_limit, min_value=0.0, key=f"mpl_{t.id}")
            max_holder_market_number = st.number_input("Max Holder Market Number (0 = no limit)", value=t.max_holder_market_number, min_value=0, key=f"mhm_{t.id}")

            st.markdown("##### Sell Settings")
            sell_order_type = st.selectbox(
                "Sell Order Type",
                ["market", "limit"],
                index=0 if t.sell_order_type == "market" else 1,
                key=f"sot_{t.id}",
            )
            sell_slippage = st.number_input("Sell Market Order Slippage (%)", value=t.sell_slippage, min_value=0.0, max_value=100.0, key=f"ss_{t.id}")

            if st.form_submit_button("💾 Save"):
                _update_trader(
                    t.id,
                    {
                        "label": label,
                        "sizing_mode": sizing_mode,
                        "fixed_amount": fixed_amount,
                        "proportional_pct": proportional_pct,
                        "buy_slippage": buy_slippage,
                        "buy_at_min": buy_at_min,
                        "tp_pct": tp_pct,
                        "sl_pct": sl_pct,
                        "ignore_trades_under": ignore_trades_under,
                        "min_price": min_price,
                        "max_price": max_price,
                        "total_spend_limit": total_spend_limit,
                        "min_per_trade": min_per_trade,
                        "max_per_yes_no": max_per_yes_no,
                        "max_per_trade": max_per_trade,
                        "max_per_market": max_per_market,
                        "max_position_limit": max_position_limit,
                        "max_holder_market_number": max_holder_market_number,
                        "sell_order_type": sell_order_type,
                        "sell_slippage": sell_slippage,
                        "max_slippage": buy_slippage,
                        "min_trade_threshold": min_per_trade,
                    },
                )
                st.success("Saved!")
                st.rerun()

    st.divider()

    # ── Pre-existing Positions ──
    st.subheader("📌 Pre-existing Positions")
    pos_df = _load_trader_positions(t.id)
    if pos_df.empty:
        st.info("No pre-existing positions found.")
    else:
        st.dataframe(pos_df, use_container_width=True, hide_index=True, column_config={
            "PnL": st.column_config.NumberColumn(format="$%.2f"),
            "Value": st.column_config.NumberColumn(format="$%.2f"),
        })

    st.divider()

    # ── Copy-Trade Holdings ──
    st.subheader("📊 Copy-Trade Holdings")
    holdings_df = _load_trader_holdings(t.id)
    if holdings_df.empty:
        st.info("No copy-trade holdings yet.")
    else:
        total_net = holdings_df["Net Position"].sum()
        total_pnl = holdings_df["Total PnL"].sum()
        num_tokens = len(holdings_df)
        mc1, mc2, mc3 = st.columns(3)
        mc1.metric("Markets", num_tokens)
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
        tab_labels = [
            f"{'🟢' if t.is_active else '🔴'} {t.label or t.wallet_address[:10]}…"
            for t in traders
        ]
        tabs = st.tabs(tab_labels)
        for tab, t in zip(tabs, traders):
            with tab:
                _render_trader_detail(t)
    else:
        st.info("No traders tracked yet. Go to **Add Trader** in the sidebar to add one.")
