"""Trader configuration management page with per-trader detail tabs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests as _req
import streamlit as st

from config import settings as _settings
from db.database import get_session_factory, init_db
from db.models import CopyTrade, Position, Trader

_logger = logging.getLogger(__name__)
init_db()
_SessionLocal = get_session_factory()


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_position_prices() -> dict[str, float]:
    """Fetch current prices from funder wallet positions (cached 30s)."""
    funder = (_settings.POLYMARKET_FUNDER_ADDRESS or "").strip()
    if not funder:
        return {}
    try:
        resp = _req.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder},
            timeout=10,
        )
        if not resp.ok:
            return {}
        price_map: dict[str, float] = {}
        for pos in resp.json():
            asset = pos.get("asset", "")
            cur = pos.get("curPrice", 0.0)
            if asset and cur:
                price_map[asset] = float(cur)
        return price_map
    except Exception as exc:
        _logger.warning("Failed to fetch position prices: %s", exc)
        return {}


@st.cache_data(ttl=30, show_spinner="Fetching prices...")
def _fetch_clob_prices(token_ids: tuple[str, ...]) -> dict[str, float]:
    """Fetch prices from CLOB API for tokens not in wallet (cached 30s).

    Uses /price endpoint (fast) with /book best_bid fallback.
    """
    price_map: dict[str, float] = {}
    for tid in token_ids:
        # Try /price first (fastest)
        try:
            resp = _req.get(
                "https://clob.polymarket.com/price",
                params={"token_id": tid, "side": "sell"},
                timeout=3,
            )
            if resp.ok:
                price_val = float(resp.json().get("price", 0) or 0)
                if price_val > 0:
                    price_map[tid] = price_val
                    continue
        except Exception:
            pass
        # Fallback: orderbook best_bid
        try:
            resp = _req.get(
                "https://clob.polymarket.com/book",
                params={"token_id": tid},
                timeout=3,
            )
            if resp.ok:
                book = resp.json()
                best_bid = float(book.get("bids", [{}])[0].get("price", 0) or 0) if book.get("bids") else 0.0
                if best_bid > 0:
                    price_map[tid] = best_bid
        except Exception:
            pass
    return price_map

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


def _delete_trader(trader_id: int) -> int:
    """Delete a trader and all related records. Returns count of deleted CopyTrade rows."""
    with _SessionLocal() as session:
        # Deactivate first to prevent bot race condition
        trader = session.get(Trader, trader_id)
        if trader:
            trader.is_active = False
            session.flush()

        ct_count = session.query(CopyTrade).filter(CopyTrade.trader_id == trader_id).delete()
        session.query(Position).filter(Position.trader_id == trader_id).delete()
        if trader:
            session.delete(trader)
        session.commit()
    return ct_count


def _reset_trade_history(trader_id: int, is_dry_run: bool) -> int:
    """Clear trade records for a trader. Returns count of deleted rows.

    DRY_RUN: deletes ALL CopyTrade records (everything is simulated).
    LIVE: keeps only BUY success records with open positions; deletes everything else.
    """
    with _SessionLocal() as session:
        if is_dry_run:
            # Wipe everything — all statuses (dry_run, below_threshold, failed, etc.)
            deleted = session.query(CopyTrade).filter(
                CopyTrade.trader_id == trader_id,
            ).delete()
            trader = session.get(Trader, trader_id)
            if trader:
                trader.watermark_timestamp = None
            session.commit()
            return deleted

        # LIVE: keep only BUY success records that still have open positions
        all_trades = (
            session.query(CopyTrade)
            .filter(CopyTrade.trader_id == trader_id)
            .all()
        )
        if not all_trades:
            return 0

        # First, delete all non-success records (failed, below_threshold, etc.)
        deleted = 0
        success_trades: list[CopyTrade] = []
        for ct in all_trades:
            if ct.status != "success":
                session.delete(ct)
                deleted += 1
            else:
                success_trades.append(ct)

        # Group success trades by (market, token_id) to find open positions
        from collections import defaultdict
        groups: dict[tuple, list[CopyTrade]] = defaultdict(list)
        for ct in success_trades:
            key = (ct.original_market or "", ct.original_token_id or "")
            groups[key].append(ct)

        for (_market, _token), group_trades in groups.items():
            buy_size = sum((ct.copy_size or 0) for ct in group_trades if ct.original_side == "BUY")
            sell_size = sum((ct.copy_size or 0) for ct in group_trades if ct.original_side == "SELL")
            net = buy_size - sell_size

            if net <= 0:
                # Fully closed — delete all records in this group
                for ct in group_trades:
                    session.delete(ct)
                    deleted += 1
            else:
                # Open position — delete only SELL records, keep BUYs
                for ct in group_trades:
                    if ct.original_side == "SELL":
                        session.delete(ct)
                        deleted += 1

        # Reset watermark
        trader = session.get(Trader, trader_id)
        if trader:
            trader.watermark_timestamp = None
        session.commit()
    return deleted


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
    """Load holdings from DB (fast, no API calls). Returns base data + token IDs."""
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

    holdings: list[dict] = []
    for (market, outcome, cid), group in df.groupby(["Market", "Outcome", "ConditionId"]):
        buy_size = group.loc[group["Side"] == "BUY", "Size"].sum()
        sell_size = group.loc[group["Side"] == "SELL", "Size"].sum()
        net_size = buy_size - sell_size
        if net_size <= 0:
            continue
        buy_rows = group[group["Side"] == "BUY"]
        avg_price = (
            (buy_rows["Price"] * buy_rows["Size"]).sum() / buy_rows["Size"].sum()
            if buy_rows["Size"].sum() > 0 else 0
        )
        token_id = buy_rows["TokenId"].iloc[0] if not buy_rows.empty else ""
        cost_basis = avg_price * net_size
        holdings.append({
            "Market": market,
            "Outcome": outcome,
            "Position": round(net_size, 4),
            "Avg Price": round(avg_price, 4),
            "Cost": round(cost_basis, 2),
            "Cur Price": 0.0,
            "Value": 0.0,
            "Unrealized": 0.0,
            "Change %": 0.0,
            "Trades": len(group),
            "_token_id": token_id,  # internal, hidden in display
        })

    return pd.DataFrame(holdings)


def _enrich_holdings_with_prices(holdings_df: pd.DataFrame) -> pd.DataFrame:
    """Add live prices to holdings DataFrame. May be slow on first call (API)."""
    if holdings_df.empty or "_token_id" not in holdings_df.columns:
        return holdings_df

    # 1. Funder wallet (cached)
    price_map = dict(_fetch_position_prices())

    # 2. CLOB API for missing (cached 30s)
    all_tids = holdings_df["_token_id"].dropna().unique().tolist()
    missing_tids = tuple(t for t in all_tids if t and price_map.get(t, 0.0) == 0.0)
    if missing_tids:
        price_map.update(_fetch_clob_prices(missing_tids))

    # 3. Gamma fallback for still missing
    still_missing = [t for t in all_tids if t and price_map.get(t, 0.0) == 0.0]
    if still_missing:
        from bot.tracker import fetch_prices_by_token_ids
        try:
            price_map.update(fetch_prices_by_token_ids(still_missing))
        except Exception as exc:
            _logger.warning("Gamma price fetch failed: %s", exc)

    # Update price columns
    enriched = holdings_df.copy()
    for idx, row in enriched.iterrows():
        cur_price = price_map.get(row["_token_id"], 0.0)
        avg_price = row["Avg Price"]
        net_size = row["Position"]
        current_value = cur_price * net_size
        cost_basis = row["Cost"]
        unrealized = current_value - cost_basis if cur_price > 0 else 0.0
        change_pct = ((cur_price - avg_price) / avg_price * 100) if avg_price > 0 and cur_price > 0 else 0.0
        enriched.at[idx, "Cur Price"] = round(cur_price, 4)
        enriched.at[idx, "Value"] = round(current_value, 2)
        enriched.at[idx, "Unrealized"] = round(unrealized, 2)
        enriched.at[idx, "Change %"] = round(change_pct, 1)

    return enriched


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
    "Today":    timedelta(days=1),
    "3 Days":   timedelta(days=3),
    "7 Days":   timedelta(days=7),
    "30 Days":  timedelta(days=30),
    "6 Months": timedelta(days=183),
    "1 Year":   timedelta(days=365),
    "All":      None,
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
    _dry = getattr(t, "dry_run", None)
    _dry = True if _dry is None else bool(_dry)
    _mode_badge = "🔵 DRY RUN" if _dry else "🟢 LIVE"
    _active_badge = "Active" if t.is_active else "Inactive"
    st.markdown(f"### {'🟢' if t.is_active else '🔴'} {t.label or 'Unnamed'} &nbsp; `{_mode_badge}`")
    st.code(t.wallet_address, language=None)

    # ── Summary metrics ──
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Copy %/$", f"{t.proportional_pct:.0f}%" if t.sizing_mode == "proportional" else f"${t.fixed_amount:.2f}")
    _buy_ot = getattr(t, 'buy_order_type', 'market') or 'market'
    if _buy_ot == "limit":
        _buy_off = getattr(t, 'buy_price_offset_pct', 1.0) or 1.0
        c2.metric("Buy Order", f"LIMIT (+{_buy_off:.1f}%)")
    else:
        c2.metric("Buy Order", f"FOK ({t.buy_slippage:.0f}%)")
    c3.metric("TP", f"{t.tp_pct:.1f}%" if t.tp_pct else "—")
    c4.metric("SL", f"{t.sl_pct:.1f}%" if t.sl_pct else "—")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Total Spend Limit", f"${t.total_spend_limit:.2f}" if t.total_spend_limit else "—")
    c6.metric("Max / Trade", f"${t.max_per_trade:.2f}" if t.max_per_trade else "—")
    c7.metric("Max / Market", f"${t.max_per_market:.2f}" if t.max_per_market else "—")
    _sell_ot = t.sell_order_type or 'market'
    if _sell_ot == "limit":
        _sell_off = getattr(t, 'sell_price_offset_pct', 1.0) or 1.0
        c8.metric("Sell Order", f"LIMIT (-{_sell_off:.1f}%)")
    else:
        c8.metric("Sell Order", f"FOK ({t.sell_slippage:.0f}%)")

    # ── Toggles ──
    tc1, tc2, tc3 = st.columns(3)
    with tc1:
        new_active = st.toggle("Active", value=t.is_active, key=f"toggle_{t.id}")
        if new_active != t.is_active:
            _toggle_trader(t.id, new_active)
            st.rerun()
    with tc2:
        cur_dry_run = True if getattr(t, "dry_run", None) is None else bool(t.dry_run)
        new_dry_run = st.toggle(
            "Dry Run", value=cur_dry_run, key=f"dry_run_{t.id}",
            help="Simulate trades without placing real orders. Turn off to go live.",
        )
        if new_dry_run != cur_dry_run:
            _update_trader(t.id, {"dry_run": new_dry_run})
            st.rerun()
    with tc3:
        cur_sell_only = bool(getattr(t, "sell_only", False))
        new_sell_only = st.toggle(
            "Sell Only (skip BUY)", value=cur_sell_only, key=f"sell_only_{t.id}",
            help="Only copy SELL trades from this trader. BUY trades will be ignored.",
        )
        if new_sell_only != cur_sell_only:
            _update_trader(t.id, {"sell_only": new_sell_only})
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
                help="Market (FOK): fill at current market price or cancel. Limit (GTC): place order at trader's price + offset and wait.",
            )
            buy_slippage = st.number_input(
                "Buy Slippage (%)",
                value=t.buy_slippage, min_value=0.0, max_value=100.0, key=f"bs_{t.id}",
                help="Used for Market (FOK) orders and as the FOK fallback price ceiling.",
            )
            _cur_buy_offset = getattr(t, 'buy_price_offset_pct', 1.0) or 1.0
            buy_price_offset_pct = st.number_input(
                "Buy Limit Price Offset (%)",
                value=_cur_buy_offset, min_value=0.0, max_value=50.0, step=0.5,
                key=f"bpop_{t.id}",
                help="For Limit (GTC) BUY: your limit = trader's price × (1 + offset%). "
                     "CLOB fills at best available up to this limit — you won't overpay in stable markets.",
            )
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
                help="Market (FOK): fill at current market price or cancel. Limit (GTC): place order at trader's price - offset and wait.",
            )
            sell_slippage = st.number_input(
                "Sell Slippage (%)",
                value=t.sell_slippage, min_value=0.0, max_value=100.0, key=f"ss_{t.id}",
                help="Used for Market (FOK) orders and as the FOK fallback price floor.",
            )
            _cur_sell_offset = getattr(t, 'sell_price_offset_pct', 1.0) or 1.0
            sell_price_offset_pct = st.number_input(
                "Sell Limit Price Offset (%)",
                value=_cur_sell_offset, min_value=0.0, max_value=50.0, step=0.5,
                key=f"spop_{t.id}",
                help="For Limit (GTC) SELL: your limit = trader's price × (1 - offset%). "
                     "CLOB fills at best available down to this limit.",
            )

            st.markdown("##### Limit Order Settings")
            _cur_timeout = getattr(t, 'limit_timeout_seconds', 30) or 30
            limit_timeout_seconds = st.number_input(
                "Limit Order Timeout (seconds)",
                value=_cur_timeout,
                min_value=5, max_value=300, step=5,
                key=f"lto_{t.id}",
                help="How long to wait for a limit (GTC) order to fill before cancelling.",
            )
            _cur_buy_fb = getattr(t, 'buy_limit_fallback', None)
            if _cur_buy_fb is None:
                _cur_buy_fb = getattr(t, 'limit_fallback_market', True)
            if _cur_buy_fb is None:
                _cur_buy_fb = True
            buy_limit_fallback = st.checkbox(
                "BUY: Fallback to Market if Limit times out",
                value=_cur_buy_fb,
                key=f"blfb_{t.id}",
                help="If a BUY limit order doesn't fill, retry with a market (FOK) order using the slippage price.",
            )
            _cur_sell_fb = getattr(t, 'sell_limit_fallback', None)
            if _cur_sell_fb is None:
                _cur_sell_fb = getattr(t, 'limit_fallback_market', True)
            if _cur_sell_fb is None:
                _cur_sell_fb = True
            sell_limit_fallback = st.checkbox(
                "SELL: Fallback to Market if Limit times out",
                value=_cur_sell_fb,
                key=f"slfb_{t.id}",
                help="If a SELL limit order doesn't fill, retry with a market (FOK) order using the slippage price.",
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
                        "buy_price_offset_pct": buy_price_offset_pct,
                        "sell_price_offset_pct": sell_price_offset_pct,
                        "limit_timeout_seconds": limit_timeout_seconds,
                        "buy_limit_fallback": buy_limit_fallback,
                        "sell_limit_fallback": sell_limit_fallback,
                        "limit_fallback_market": buy_limit_fallback,
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

    _holdings_col_config = {
        "Avg Price": st.column_config.NumberColumn(format="$%.4f"),
        "Cost": st.column_config.NumberColumn(format="$%.2f"),
        "Cur Price": st.column_config.NumberColumn(format="$%.4f"),
        "Value": st.column_config.NumberColumn(format="$%.2f"),
        "Unrealized": st.column_config.NumberColumn(format="$%.2f"),
        "Change %": st.column_config.NumberColumn(format="%.1f%%"),
    }
    _hidden_cols = ["_token_id"]

    for htab, statuses, empty_msg in [
        (htab_live, ["success"], "No live copy-trade holdings yet."),
        (htab_dry, ["dry_run"], "No dry-run holdings yet."),
    ]:
        with htab:
            holdings_df = _load_trader_holdings(t.id, statuses=statuses)
            if holdings_df.empty:
                st.info(empty_msg)
                continue

            # Enrich with prices (cached for 30s, fast on subsequent loads)
            holdings_df = _enrich_holdings_with_prices(holdings_df)
            display_cols = [c for c in holdings_df.columns if c not in _hidden_cols]

            total_value = holdings_df["Value"].sum()
            total_unrealized = holdings_df["Unrealized"].sum()
            total_cost = holdings_df["Cost"].sum()
            pct = (total_unrealized / total_cost * 100) if total_cost > 0 else 0

            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Markets", len(holdings_df))
            mc2.metric("Total Value", f"${total_value:,.2f}")
            mc3.metric("Unrealized PnL", f"${total_unrealized:,.2f}")
            mc4.metric("Change %", f"{pct:+.1f}%")

            st.dataframe(
                holdings_df[display_cols],
                use_container_width=True,
                hide_index=True,
                column_config=_holdings_col_config,
            )

    st.divider()

    # ── Realized PnL (closed / sold trades) ──
    col_pnl_hdr, col_pnl_range = st.columns([3, 1])
    col_pnl_hdr.subheader("💰 Realized PnL")
    selected_range = col_pnl_range.selectbox(
        "Time Range",
        list(_PNL_RANGE_OPTIONS.keys()),
        index=len(_PNL_RANGE_OPTIONS) - 1,  # default: All
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

    # ── Danger Zone ──
    st.divider()
    with st.expander("Danger Zone", expanded=False):
        _dry = getattr(t, "dry_run", None)
        _dry = True if _dry is None else bool(_dry)

        # Get record counts for display
        with _SessionLocal() as _sess:
            trade_count = _sess.query(CopyTrade).filter(
                CopyTrade.trader_id == t.id,
            ).count()
            pos_count = _sess.query(Position).filter(Position.trader_id == t.id).count()

        dz_left, dz_right = st.columns(2)

        # ── Reset Trade History ──
        with dz_left:
            reset_disabled = trade_count == 0
            reset_key = f"confirm_reset_{t.id}"
            if reset_key not in st.session_state:
                st.session_state[reset_key] = False

            if not st.session_state[reset_key]:
                if st.button(
                    "Reset Trade History",
                    key=f"reset_btn_{t.id}",
                    disabled=reset_disabled,
                    help="No trade records to reset." if reset_disabled else None,
                ):
                    st.session_state[reset_key] = True
                    st.rerun()
            else:
                if _dry:
                    st.warning(
                        f"This will delete **all {trade_count} dry-run** trade records "
                        f"for this trader. Everything will be wiped clean."
                    )
                else:
                    st.warning(
                        f"This will delete SELL records and fully-closed positions "
                        f"({trade_count} total records). Open positions (BUY records "
                        f"with remaining holdings) will be preserved."
                    )
                rc1, rc2, rc3 = st.columns(3)
                with rc1:
                    if st.button("Confirm Reset", key=f"reset_confirm_{t.id}", type="primary"):
                        deleted = _reset_trade_history(t.id, is_dry_run=_dry)
                        st.session_state[reset_key] = False
                        st.success(f"Deleted {deleted} trade records.")
                        st.cache_data.clear()
                        st.rerun()
                with rc2:
                    if not _dry and st.button("Force Reset (delete ALL)", key=f"reset_force_{t.id}"):
                        deleted = _reset_trade_history(t.id, is_dry_run=True)
                        st.session_state[reset_key] = False
                        st.success(f"Force deleted {deleted} trade records (including open positions).")
                        st.cache_data.clear()
                        st.rerun()
                with rc3:
                    if st.button("Cancel", key=f"reset_cancel_{t.id}"):
                        st.session_state[reset_key] = False
                        st.rerun()

        # ── Delete Trader ──
        with dz_right:
            delete_key = f"confirm_delete_{t.id}"
            if delete_key not in st.session_state:
                st.session_state[delete_key] = False

            if not st.session_state[delete_key]:
                if st.button("Delete Trader", key=f"delete_btn_{t.id}"):
                    st.session_state[delete_key] = True
                    st.rerun()
            else:
                st.warning(
                    f"This will **permanently delete** this trader, "
                    f"**{trade_count} trades**, and **{pos_count} positions**. "
                    f"This cannot be undone."
                )
                dc1, dc2 = st.columns(2)
                with dc1:
                    if st.button("Confirm Delete", key=f"delete_confirm_{t.id}", type="primary"):
                        _delete_trader(t.id)
                        st.session_state[delete_key] = False
                        st.cache_data.clear()
                        st.rerun()
                with dc2:
                    if st.button("Cancel", key=f"delete_cancel_{t.id}"):
                        st.session_state[delete_key] = False
                        st.rerun()


def render() -> None:
    st.title("Tracked Traders")
    st.caption("Manage the wallets you want to copy-trade. Select a trader tab to view details.")

    traders = _get_all_traders()

    if traders:
        tab_labels = []
        for t in traders:
            _dr = getattr(t, "dry_run", None)
            _dr = True if _dr is None else bool(_dr)
            _icon = "🔴" if not t.is_active else ("🔵" if _dr else "🟢")
            _mode = "[DRY]" if _dr else "[LIVE]"
            tab_labels.append(f"{_icon} {t.label or t.wallet_address[:10]}… {_mode}")
        tabs = st.tabs(tab_labels)
        for tab, t in zip(tabs, traders):
            with tab:
                _render_trader_detail(t)
    else:
        st.info("No traders tracked yet. Go to **Add Trader** in the sidebar to add one.")
