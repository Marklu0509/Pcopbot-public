"""Trader configuration management page with per-trader detail tabs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from db.database import get_session_factory, init_db
from db.models import CopyTrade, Position, Trader

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


def _load_trader_trades(trader_id: int, limit: int | None = 300) -> pd.DataFrame:
    """Load copy trades for a trader, newest first.

    By default, only recent rows are loaded for UI responsiveness.
    Pass ``limit=None`` to load full history.
    """
    with _SessionLocal() as session:
        query = (
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
        )
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows,
        columns=[
            "ID", "Time", "Market", "Outcome", "Side",
            "Orig Position", "Orig Price", "Copy Position", "Copy Price",
            "Status", "PnL", "Error", "Order ID",
        ],
    )
    df["Orig Value"] = (df["Orig Position"] * df["Orig Price"]).round(2)
    df["Copy Value"] = (df["Copy Position"] * df["Copy Price"].fillna(0)).round(2)
    df["Status"] = df["Status"].map(lambda s: f"{STATUS_ICONS.get(s, '')} {s}")
    # Reorder columns
    df = df[["ID", "Time", "Market", "Outcome", "Side",
             "Orig Position", "Orig Price", "Orig Value",
             "Copy Position", "Copy Price", "Copy Value",
             "Status", "PnL", "Error", "Order ID"]]
    return df


def _load_trader_holdings(trader_id: int, statuses: list[str] | None = None) -> pd.DataFrame:
    """Aggregate current holdings per token for a trader, with current price."""
    statuses = statuses or ["success"]
    with _SessionLocal() as session:
        rows = (
            session.query(
                CopyTrade.market_title,
                CopyTrade.outcome,
                CopyTrade.original_market,
                CopyTrade.original_token_id,
                CopyTrade.original_side,
                CopyTrade.copy_size,
                CopyTrade.copy_price,
                CopyTrade.pnl,
            )
            .filter(
                CopyTrade.trader_id == trader_id,
                CopyTrade.status.in_(statuses),
            )
            .all()
        )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["Market", "Outcome", "ConditionId", "TokenId", "Side", "Size", "Price", "PnL"])

    # Fetch current prices from our own wallet's open positions (most accurate source)
    import requests as _req
    from config import settings as _settings
    price_map: dict[str, float] = {}
    funder = (_settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if funder:
        try:
            resp = _req.get(
                "https://data-api.polymarket.com/positions",
                params={"user": funder},
                timeout=10,
            )
            if resp.ok:
                for pos in resp.json():
                    asset = pos.get("asset", "")
                    cur = pos.get("curPrice", 0.0)
                    if asset and cur:
                        price_map[asset] = float(cur)
        except Exception:
            pass

    # Fallback: Gamma API via clob_token_ids for any token not in our positions
    all_buy_token_ids = df[df["Side"] == "BUY"]["TokenId"].dropna().unique().tolist()
    missing_tids = [t for t in all_buy_token_ids if t and price_map.get(t, 0.0) == 0.0]
    if missing_tids:
        from bot.tracker import fetch_prices_by_token_ids
        try:
            price_map.update(fetch_prices_by_token_ids(missing_tids))
        except Exception:
            pass

    holdings: list[dict] = []
    for (market, outcome, cid), group in df.groupby(["Market", "Outcome", "ConditionId"]):
        buy_size = group.loc[group["Side"] == "BUY", "Size"].sum()
        sell_size = group.loc[group["Side"] == "SELL", "Size"].sum()
        net_size = buy_size - sell_size
        if net_size <= 0:
            continue  # fully sold or redeemed — skip
        buy_rows = group[group["Side"] == "BUY"]
        avg_price = (
            (buy_rows["Price"] * buy_rows["Size"]).sum() / buy_rows["Size"].sum()
            if buy_rows["Size"].sum() > 0 else 0
        )
        # Use token_id from BUY rows to look up live price from Gamma API
        token_id = buy_rows["TokenId"].iloc[0] if not buy_rows.empty else ""
        cur_price = price_map.get(token_id, 0.0)
        cost_basis = avg_price * net_size
        current_value = cur_price * net_size
        unrealized = current_value - cost_basis if cur_price > 0 else 0.0
        change_pct = ((cur_price - avg_price) / avg_price * 100) if avg_price > 0 and cur_price > 0 else 0.0
        holdings.append({
            "Market": market,
            "Outcome": outcome,
            "Position": round(net_size, 4),
            "Avg Price": round(avg_price, 4),
            "Cur Price": round(cur_price, 4),
            "Value": round(current_value, 2),
            "Unrealized": round(unrealized, 2),
            "Change %": round(change_pct, 1),
            "Trades": len(group),
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
        "Market", "Outcome", "Position", "Avg Price", "Value", "PnL", "PnL %", "Cur Price", "Fetched",
    ])


_PNL_RANGE_OPTIONS: dict[str, timedelta | None] = {
    "今日":   timedelta(days=1),
    "近3天":  timedelta(days=3),
    "近7天":  timedelta(days=7),
    "近30天": timedelta(days=30),
    "近半年": timedelta(days=183),
    "近一年": timedelta(days=365),
    "全部":   None,
}


def _load_realized_pnl(
    trader_id: int,
    statuses: list[str] | None = None,
    since: datetime | None = None,
) -> pd.DataFrame:
    """Compute realized PnL per market/outcome from SELL copy trades.

    ``since`` filters SELL trades by executed_at. BUY trades are always loaded
    in full so that the weighted average buy price is always accurate.
    """
    statuses = statuses or ["success"]
    with _SessionLocal() as session:
        rows = (
            session.query(
                CopyTrade.market_title,
                CopyTrade.outcome,
                CopyTrade.original_side,
                CopyTrade.copy_size,
                CopyTrade.copy_price,
                CopyTrade.pnl,
                CopyTrade.executed_at,
            )
            .filter(
                CopyTrade.trader_id == trader_id,
                CopyTrade.status.in_(statuses),
            )
            .all()
        )
    empty_cols = ["Market", "Outcome", "Sold Position", "Avg Buy Price", "Avg Sell Price",
                  "Total Cost", "Revenue", "Realized PnL", "ROI %"]
    if not rows:
        return pd.DataFrame(columns=empty_cols)

    df = pd.DataFrame(rows, columns=["Market", "Outcome", "Side", "Size", "Price", "PnL", "ExecutedAt"])
    realized: list[dict] = []
    for (market, outcome), group in df.groupby(["Market", "Outcome"]):
        buys = group[group["Side"] == "BUY"]
        # Apply date filter to SELL rows only; keep all BUYs for correct avg price
        sells = group[group["Side"] == "SELL"]
        if since is not None:
            sells = sells[sells["ExecutedAt"].notna() & (sells["ExecutedAt"] >= since)]
        if sells.empty:
            continue
        sell_size = sells["Size"].sum()
        sell_value = (sells["Price"] * sells["Size"]).sum()
        buy_value = (buys["Price"] * buys["Size"]).sum()
        buy_size = buys["Size"].sum()
        avg_buy = buy_value / buy_size if buy_size > 0 else 0
        avg_sell = sell_value / sell_size if sell_size > 0 else 0
        total_cost = avg_buy * sell_size
        realized_pnl = sell_value - total_cost
        roi = (realized_pnl / total_cost * 100) if total_cost > 0 else 0
        realized.append({
            "Market": market,
            "Outcome": outcome,
            "Sold Position": round(sell_size, 4),
            "Avg Buy Price": round(avg_buy, 4),
            "Avg Sell Price": round(avg_sell, 4),
            "Total Cost": round(total_cost, 2),
            "Revenue": round(sell_value, 2),
            "Realized PnL": round(realized_pnl, 2),
            "ROI %": round(roi, 1),
        })
    if not realized:
        return pd.DataFrame(columns=empty_cols)
    return pd.DataFrame(realized)


def _render_trader_detail(t) -> None:
    """Render the detail view for a single trader inside its tab."""
    # ── Info card ──
    st.markdown(f"### {'🟢' if t.is_active else '🔴'} {t.label or 'Unnamed'}")
    st.code(t.wallet_address, language=None)

    # ── Summary metrics ──
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Copy %/$", f"{t.proportional_pct:.0f}%" if t.sizing_mode == "proportional" else f"${t.fixed_amount:.2f}")
    _buy_ot = getattr(t, 'buy_order_type', 'market') or 'market'
    c2.metric("Buy Order", f"{_buy_ot.upper()} ({t.buy_slippage:.0f}%)")
    c3.metric("TP", f"{t.tp_pct:.1f}%" if t.tp_pct else "—")
    c4.metric("SL", f"{t.sl_pct:.1f}%" if t.sl_pct else "—")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Total Spend Limit", f"${t.total_spend_limit:.2f}" if t.total_spend_limit else "—")
    c6.metric("Max / Trade", f"${t.max_per_trade:.2f}" if t.max_per_trade else "—")
    c7.metric("Max / Market", f"${t.max_per_market:.2f}" if t.max_per_market else "—")
    _sell_ot = t.sell_order_type or 'market'
    c8.metric("Sell Order", f"{_sell_ot.upper()} ({t.sell_slippage:.0f}%)")

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
            buy_order_type = st.selectbox(
                "Buy Order Type",
                ["market", "limit"],
                index=0 if (getattr(t, 'buy_order_type', 'market') or 'market') == "market" else 1,
                key=f"bot_{t.id}",
                help="Market (FOK): fill at current market price or cancel. Limit (GTC): place order at target's price ± slippage and wait.",
            )
            buy_slippage = st.number_input("Buy Slippage (%)", value=t.buy_slippage, min_value=0.0, max_value=100.0, key=f"bs_{t.id}")
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
            _cur_sell_ot = t.sell_order_type or "market"
            sell_order_type = st.selectbox(
                "Sell Order Type",
                ["market", "limit"],
                index=0 if _cur_sell_ot == "market" else 1,
                key=f"sot_{t.id}",
                help="Market (FOK): fill at current market price or cancel. Limit (GTC): place order at target's price ± slippage and wait.",
            )
            sell_slippage = st.number_input("Sell Slippage (%)", value=t.sell_slippage, min_value=0.0, max_value=100.0, key=f"ss_{t.id}")

            st.markdown("##### Limit Order Settings")
            _cur_timeout = getattr(t, 'limit_timeout_seconds', 30) or 30
            limit_timeout_seconds = st.number_input(
                "Limit Order Timeout (seconds)",
                value=_cur_timeout,
                min_value=5, max_value=300, step=5,
                key=f"lto_{t.id}",
                help="How long to wait for a limit (GTC) order to fill before cancelling.",
            )
            _cur_fallback = getattr(t, 'limit_fallback_market', True)
            if _cur_fallback is None:
                _cur_fallback = True
            limit_fallback_market = st.checkbox(
                "Fallback to Market if Limit times out",
                value=_cur_fallback,
                key=f"lfm_{t.id}",
                help="If a limit order doesn't fill within the timeout, automatically retry with a market (FOK) order.",
            )

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
                        "buy_order_type": buy_order_type,
                        "sell_order_type": sell_order_type,
                        "sell_slippage": sell_slippage,
                        "limit_timeout_seconds": limit_timeout_seconds,
                        "limit_fallback_market": limit_fallback_market,
                        "max_slippage": buy_slippage,
                        "min_trade_threshold": min_per_trade,
                    },
                )
                st.success("Saved!")
                st.rerun()

    st.divider()

    # ── Pre-existing Positions (target trader's own holdings) ──
    st.subheader("📌 Trader's Current Positions")
    pos_df = _load_trader_positions(t.id)
    if pos_df.empty:
        st.info("No pre-existing positions found.")
    else:
        pm1, pm2, pm3, pm4 = st.columns(4)
        pm1.metric("Markets", len(pos_df))
        pm2.metric("Total Position", f"{pos_df['Position'].sum():,.2f}")
        pm3.metric("Total Value", f"${pos_df['Value'].sum():,.2f}")
        pm4.metric("Unrealized PnL", f"${pos_df['PnL'].sum():,.2f}")
        st.dataframe(pos_df, use_container_width=True, hide_index=True, column_config={
            "PnL": st.column_config.NumberColumn(format="$%.2f"),
            "Value": st.column_config.NumberColumn(format="$%.2f"),
            "Avg Price": st.column_config.NumberColumn(format="$%.4f"),
            "Cur Price": st.column_config.NumberColumn(format="$%.4f"),
            "PnL %": st.column_config.NumberColumn(format="%.1f%%"),
        })

    st.divider()

    # ── Copy-Trade Holdings (our positions) ──
    st.subheader("📊 Copy-Trade Holdings")
    htab_live, htab_dry = st.tabs(["Live (success)", "Dry Run (simulated)"])

    with htab_live:
        holdings_df = _load_trader_holdings(t.id, statuses=["success"])
        if holdings_df.empty:
            st.info("No live copy-trade holdings yet.")
        else:
            total_value = holdings_df["Value"].sum()
            total_unrealized = holdings_df["Unrealized"].sum()
            total_cost = (holdings_df["Avg Price"] * holdings_df["Position"]).sum()
            pct = (total_unrealized / total_cost * 100) if total_cost > 0 else 0
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Markets", len(holdings_df))
            mc2.metric("Total Value", f"${total_value:,.2f}")
            mc3.metric("Unrealized PnL", f"${total_unrealized:,.2f}")
            mc4.metric("Change %", f"{pct:+.1f}%")
            st.dataframe(
                holdings_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Avg Price": st.column_config.NumberColumn(format="$%.4f"),
                    "Cur Price": st.column_config.NumberColumn(format="$%.4f"),
                    "Value": st.column_config.NumberColumn(format="$%.2f"),
                    "Unrealized": st.column_config.NumberColumn(format="$%.2f"),
                    "Change %": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )

    with htab_dry:
        holdings_df = _load_trader_holdings(t.id, statuses=["dry_run"])
        if holdings_df.empty:
            st.info("No dry-run holdings yet.")
        else:
            total_value = holdings_df["Value"].sum()
            total_unrealized = holdings_df["Unrealized"].sum()
            total_cost = (holdings_df["Avg Price"] * holdings_df["Position"]).sum()
            pct = (total_unrealized / total_cost * 100) if total_cost > 0 else 0
            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Markets", len(holdings_df))
            mc2.metric("Total Value", f"${total_value:,.2f}")
            mc3.metric("Unrealized PnL", f"${total_unrealized:,.2f}")
            mc4.metric("Change %", f"{pct:+.1f}%")
            st.dataframe(
                holdings_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Avg Price": st.column_config.NumberColumn(format="$%.4f"),
                    "Cur Price": st.column_config.NumberColumn(format="$%.4f"),
                    "Value": st.column_config.NumberColumn(format="$%.2f"),
                    "Unrealized": st.column_config.NumberColumn(format="$%.2f"),
                    "Change %": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )

    st.divider()

    # ── Realized PnL (closed / sold trades) ──
    col_pnl_hdr, col_pnl_range = st.columns([3, 1])
    col_pnl_hdr.subheader("💰 Realized PnL")
    selected_range = col_pnl_range.selectbox(
        "時間範圍",
        list(_PNL_RANGE_OPTIONS.keys()),
        index=len(_PNL_RANGE_OPTIONS) - 1,  # default: 全部
        key=f"pnl_range_{t.id}",
        label_visibility="collapsed",
    )
    _td = _PNL_RANGE_OPTIONS[selected_range]
    _pnl_since = (
        datetime.now(timezone.utc).replace(tzinfo=None) - _td if _td is not None else None
    )
    rtab_live, rtab_dry = st.tabs(["Live (success)", "Dry Run (simulated)"])

    def _render_realized_block(realized_df: pd.DataFrame, empty_msg: str) -> None:
        if realized_df.empty or len(realized_df) == 0:
            rm1, rm2, rm3, rm4 = st.columns(4)
            rm1.metric("Closed Markets", 0)
            rm2.metric("Total Invested", "$0.00")
            rm3.metric("Total Realized", "$0.00")
            rm4.metric("Win Rate", "—")
            st.info(empty_msg)
            st.dataframe(realized_df, use_container_width=True, hide_index=True)
            return

        total_cost = realized_df["Total Cost"].sum()
        total_realized = realized_df["Realized PnL"].sum()
        roi = (total_realized / total_cost * 100) if total_cost > 0 else 0
        # Exclude near-breakeven markets (|ROI| < 3%) from win rate
        _roi_pct = realized_df["Realized PnL"] / realized_df["Total Cost"].abs().where(
            realized_df["Total Cost"].abs() > 0, other=float("nan")
        ) * 100
        _decisive = realized_df[_roi_pct.abs() >= 3]
        wins = (_decisive["Realized PnL"] > 0).sum()
        total_markets = len(_decisive)
        win_rate = (wins / total_markets * 100) if total_markets > 0 else 0

        rm1, rm2, rm3, rm4, rm5 = st.columns(5)
        rm1.metric("Closed Markets", total_markets)
        rm2.metric("Total Invested", f"${total_cost:,.2f}")
        rm3.metric("Total Realized", f"${total_realized:+,.2f}")
        rm4.metric("ROI", f"{roi:+.1f}%")
        rm5.metric("Win Rate", f"{win_rate:.0f}% ({wins}/{total_markets})")
        st.dataframe(
            realized_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Realized PnL": st.column_config.NumberColumn(format="$%.2f"),
                "Total Cost": st.column_config.NumberColumn(format="$%.2f"),
                "Revenue": st.column_config.NumberColumn(format="$%.2f"),
                "Avg Buy Price": st.column_config.NumberColumn(format="$%.4f"),
                "Avg Sell Price": st.column_config.NumberColumn(format="$%.4f"),
                "ROI %": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )

    with rtab_live:
        _render_realized_block(
            _load_realized_pnl(t.id, statuses=["success"], since=_pnl_since),
            "No live realized PnL yet.",
        )

    with rtab_dry:
        _render_realized_block(
            _load_realized_pnl(t.id, statuses=["dry_run"], since=_pnl_since),
            "No dry-run realized PnL yet.",
        )

    st.divider()

    # ── Trade History ──
    st.subheader("📜 Trade History")
    c_recent, c_full = st.columns([2, 1])
    with c_recent:
        recent_limit = st.selectbox(
            "Recent rows",
            [100, 300, 500, 1000],
            index=1,
            key=f"history_limit_{t.id}",
            help="Load only recent rows for faster rendering.",
        )
    with c_full:
        load_full = st.toggle(
            "Load full history",
            value=False,
            key=f"history_full_{t.id}",
            help="May be slow if you have many trades.",
        )

    trades_df = _load_trader_trades(t.id, limit=None if load_full else int(recent_limit))
    if trades_df.empty:
        st.info("No trades recorded yet.")
    else:
        if load_full:
            st.caption(f"Showing full history: {len(trades_df)} trades")
        else:
            st.caption(f"Showing latest {len(trades_df)} trades")
        st.dataframe(
            trades_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Orig Position": st.column_config.NumberColumn(format="%.4f"),
                "Orig Price": st.column_config.NumberColumn(format="$%.4f"),
                "Orig Value": st.column_config.NumberColumn(format="$%.2f"),
                "Copy Position": st.column_config.NumberColumn(format="%.4f"),
                "Copy Price": st.column_config.NumberColumn(format="$%.4f"),
                "Copy Value": st.column_config.NumberColumn(format="$%.2f"),
                "PnL": st.column_config.NumberColumn(format="$%.2f"),
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
