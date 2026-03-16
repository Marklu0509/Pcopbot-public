"""PnL monitoring dashboard page."""

import pandas as pd
import streamlit as st

from dashboard.components.charts import pnl_line_chart
from db.database import get_session_factory, init_db
from db.models import CopyTrade, Trader
from config import settings

init_db()
_SessionLocal = get_session_factory()


def _load_pnl_data():
    with _SessionLocal() as session:
        rows = (
            session.query(
                CopyTrade.executed_at,
                CopyTrade.pnl,
                CopyTrade.status,
                CopyTrade.trader_id,
                CopyTrade.copy_size,
                CopyTrade.copy_price,
                CopyTrade.original_side,
                CopyTrade.market_title,
                CopyTrade.outcome,
                Trader.label,
                Trader.wallet_address,
            )
            .join(Trader, Trader.id == CopyTrade.trader_id)
            .order_by(CopyTrade.executed_at)
            .all()
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows,
        columns=["executed_at", "pnl", "status", "trader_id", "copy_size",
                 "copy_price", "side", "market", "outcome", "label", "wallet_address"],
    )
    df["trader_name"] = df["label"].where(df["label"] != "", df["wallet_address"])
    df["cost"] = df["copy_size"] * df["copy_price"].fillna(0)
    return df


def render() -> None:
    st.title("PnL Dashboard")

    df = _load_pnl_data()

    if df.empty:
        st.info("No trade data available yet.")
        if st.button("🔄 Refresh"):
            st.rerun()
        return

    # Trade mode filter
    pnl_mode = st.radio("Show", ["Live", "Dry Run", "All"], horizontal=True)
    if pnl_mode == "Live":
        mode_statuses = ["success"]
    elif pnl_mode == "Dry Run":
        mode_statuses = ["dry_run"]
    else:
        mode_statuses = ["success", "dry_run"]

    total_pnl = df["pnl"].sum()
    total_trades = len(df)
    executed = df[df["status"].isin(mode_statuses)]
    success_count = len(executed)
    success_rate = success_count / total_trades * 100 if total_trades else 0.0

    # Separate realized (SELL) and unrealized (BUY) PnL
    buys = executed[executed["side"] == "BUY"]
    sells = executed[executed["side"] == "SELL"]
    realized_pnl = sells["pnl"].sum()
    total_invested = buys["cost"].sum()
    total_revenue = sells["cost"].sum()

    # Unrealized PnL from wallet positions (live data)
    unrealized_pnl = 0.0
    funder = (settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if funder:
        try:
            from bot.tracker import fetch_positions
            wallet_positions = fetch_positions(funder)
            for pos in wallet_positions:
                unrealized_pnl += pos.get("pnl", 0.0)
        except Exception:
            # Fallback to DB-based calculation if wallet fetch fails
            unrealized_pnl = buys["pnl"].sum()
    else:
        unrealized_pnl = buys["pnl"].sum()

    net_pnl = realized_pnl + unrealized_pnl
    roi = (net_pnl / total_invested * 100) if total_invested > 0 else 0

    # Win rate per market — exclude near-breakeven trades (|ROI| < 3%)
    sell_groups = executed[executed["side"] == "SELL"].groupby(["market", "outcome"])
    buy_cost_groups = executed[executed["side"] == "BUY"].groupby(["market", "outcome"])
    market_pnl = sell_groups["pnl"].sum()
    market_cost = buy_cost_groups["cost"].sum().reindex(market_pnl.index, fill_value=0.0)
    roi_pct = market_pnl / market_cost.where(market_cost > 0, other=float("nan")) * 100
    decisive = market_pnl[roi_pct.abs() >= 3]
    wins = (decisive > 0).sum()
    total_markets = len(decisive)
    win_rate = (wins / total_markets * 100) if total_markets > 0 else 0

    st.subheader("📈 Overall Summary")
    r1c1, r1c2, r1c3, r1c4 = st.columns(4)
    r1c1.metric("Total Invested", f"${total_invested:,.2f}")
    r1c2.metric("Total PnL", f"${net_pnl:+,.2f}")
    r1c3.metric("ROI", f"{roi:+.1f}%")
    r1c4.metric("Total Trades", total_trades)

    r2c1, r2c2, r2c3, r2c4 = st.columns(4)
    r2c1.metric("Realized PnL", f"${realized_pnl:+,.2f}")
    r2c2.metric("Unrealized PnL", f"${unrealized_pnl:+,.2f}")
    r2c3.metric("Execution Rate", f"{success_rate:.1f}%")
    r2c4.metric("Win Rate (by market)", f"{win_rate:.0f}% ({wins}/{total_markets})")

    st.divider()
    st.subheader("PnL Over Time")
    df_sorted = df.sort_values("executed_at").copy()
    df_sorted["cumulative_pnl"] = df_sorted["pnl"].cumsum()
    fig = pnl_line_chart(df_sorted)
    st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Per-Trader PnL Breakdown")
    breakdown_data = []
    for name, grp in df.groupby("trader_name"):
        grp_exec = grp[grp["status"].isin(["success", "dry_run"])]
        grp_buys = grp_exec[grp_exec["side"] == "BUY"]
        invested = grp_buys["cost"].sum()
        pnl_sum = grp["pnl"].sum()
        trader_roi = (pnl_sum / invested * 100) if invested > 0 else 0
        mkt_pnl = grp_exec.groupby(["market", "outcome"])["pnl"].sum()
        w = (mkt_pnl > 0).sum()
        m = len(mkt_pnl)
        wr = (w / m * 100) if m > 0 else 0
        breakdown_data.append({
            "Trader": name,
            "Invested": round(invested, 2),
            "PnL": round(pnl_sum, 2),
            "ROI %": round(trader_roi, 1),
            "Trades": len(grp),
            "Executed": len(grp_exec),
            "Win Rate": f"{wr:.0f}% ({w}/{m})",
        })
    breakdown = pd.DataFrame(breakdown_data)
    st.dataframe(breakdown, use_container_width=True, hide_index=True, column_config={
        "Invested": st.column_config.NumberColumn(format="$%.2f"),
        "PnL": st.column_config.NumberColumn(format="$%.2f"),
        "ROI %": st.column_config.NumberColumn(format="%.1f%%"),
    })

    if st.button("🔄 Refresh"):
        st.rerun()
