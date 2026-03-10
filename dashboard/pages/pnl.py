"""PnL monitoring dashboard page."""

import pandas as pd
import streamlit as st

from dashboard.components.charts import pnl_line_chart
from db.database import get_session_factory, init_db
from db.models import CopyTrade, Trader

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
        columns=["executed_at", "pnl", "status", "trader_id", "label", "wallet_address"],
    )
    df["trader_name"] = df["label"].where(df["label"] != "", df["wallet_address"])
    return df


def render() -> None:
    st.title("PnL Dashboard")

    df = _load_pnl_data()

    if df.empty:
        st.info("No trade data available yet.")
        if st.button("🔄 Refresh"):
            st.rerun()
        return

    total_pnl = df["pnl"].sum()
    total_trades = len(df)
    success_count = (df["status"].isin(["success", "dry_run"])).sum()
    success_rate = success_count / total_trades * 100 if total_trades else 0.0

    col1, col2, col3 = st.columns(3)
    col1.metric("Total PnL", f"${total_pnl:.2f}")
    col2.metric("Total Trades", total_trades)
    col3.metric("Success Rate", f"{success_rate:.1f}%")

    st.divider()
    st.subheader("PnL Over Time")
    df_sorted = df.sort_values("executed_at").copy()
    df_sorted["cumulative_pnl"] = df_sorted["pnl"].cumsum()
    fig = pnl_line_chart(df_sorted)
    st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.subheader("Per-Trader PnL Breakdown")
    breakdown = (
        df.groupby("trader_name")
        .agg(
            total_pnl=("pnl", "sum"),
            trades=("pnl", "count"),
            successes=("status", lambda s: s.isin(["success", "dry_run"]).sum()),
        )
        .reset_index()
    )
    breakdown["success_rate"] = (breakdown["successes"] / breakdown["trades"] * 100).round(1)
    breakdown["total_pnl"] = breakdown["total_pnl"].map(lambda x: f"${x:.2f}")
    st.dataframe(breakdown, use_container_width=True)

    if st.button("🔄 Refresh"):
        st.rerun()
