"""Add new trader page — moved to its own navigation entry."""

from __future__ import annotations

import streamlit as st

from db.database import get_session_factory, init_db
from db.models import Position, Trader
from bot.watermark import set_watermark
from bot import tracker

init_db()
_SessionLocal = get_session_factory()


def _add_trader(data: dict) -> str | None:
    with _SessionLocal() as session:
        existing = (
            session.query(Trader)
            .filter(Trader.wallet_address == data["wallet_address"])
            .first()
        )
        if existing:
            return "Wallet address already exists."
        trader = Trader(**data)
        session.add(trader)
        session.commit()
        session.refresh(trader)
        set_watermark(session, trader)

        # Fetch pre-existing positions for the new trader
        try:
            positions = tracker.fetch_positions(trader.wallet_address)
            for p in positions:
                session.add(Position(
                    trader_id=trader.id,
                    condition_id=p["condition_id"],
                    asset_id=p["asset_id"],
                    market_title=p["market_title"],
                    outcome=p["outcome"],
                    size=p["size"],
                    avg_price=p["avg_price"],
                    initial_value=p["initial_value"],
                    current_value=p["current_value"],
                    pnl=p["pnl"],
                    pnl_pct=p["pnl_pct"],
                    cur_price=p["cur_price"],
                ))
            session.commit()
        except Exception:
            pass  # non-critical — positions will be fetched on next bot startup
    return None


def render() -> None:
    st.title("➕ Add New Trader")
    st.caption("Enter the target wallet and copy-trade parameters.")

    with st.form("add_trader"):
        st.markdown("##### Target Wallet")
        wallet_address = st.text_input("Wallet Address *")
        label = st.text_input("Tag (Label)")

        st.markdown("##### Sizing")
        sizing_mode = st.selectbox("Sizing mode", ["fixed", "proportional"])
        fixed_amount = st.number_input("Fixed amount ($)", value=50.0, min_value=0.0)
        proportional_pct = st.number_input("Copy Percentage (%)", value=100.0, min_value=0.0, max_value=100.0)

        st.markdown("##### Buy Settings")
        buy_order_type = st.selectbox(
            "Buy Order Type",
            ["market", "limit"],
            help="Market (FOK): fill at current market price or cancel. Limit (GTC): place order at target's price ± slippage and wait.",
        )
        buy_slippage = st.number_input("Buy Slippage (%)", value=30.0, min_value=0.0, max_value=100.0)
        buy_at_min = st.checkbox("Below Min Limit, Buy at Min", value=True)

        st.markdown("##### Take-Profit / Stop-Loss")
        tp_pct = st.number_input("TP % (0 = disabled)", value=0.0, min_value=0.0)
        sl_pct = st.number_input("SL % (0 = disabled)", value=0.0, min_value=0.0)

        st.markdown("##### Filters")
        ignore_trades_under = st.number_input("Ignore Target Wallet Trades Under ($)", value=0.0, min_value=0.0)
        min_price = st.number_input("Min Price ($, 0 = no limit)", value=0.0, min_value=0.0)
        max_price = st.number_input("Max Price ($, 0 = no limit)", value=0.0, min_value=0.0)

        st.markdown("##### Spending / Position Limits")
        total_spend_limit = st.number_input("Total Spend Limit ($, 0 = no limit)", value=0.0, min_value=0.0)
        min_per_trade = st.number_input("Min Per Trade ($)", value=0.0, min_value=0.0)
        max_per_yes_no = st.number_input("Max Per Yes/No ($, 0 = no limit)", value=0.0, min_value=0.0)
        max_per_trade = st.number_input("Max Per Trade ($, 0 = no limit)", value=0.0, min_value=0.0)
        max_per_market = st.number_input("Max Per Market ($, 0 = no limit)", value=0.0, min_value=0.0)
        max_position_limit = st.number_input("Max Position Limit ($)", value=500.0, min_value=0.0)
        max_holder_market_number = st.number_input("Max Holder Market Number (0 = no limit)", value=0, min_value=0)

        st.markdown("##### Sell Settings")
        sell_order_type = st.selectbox(
            "Sell Order Type",
            ["market", "limit"],
            help="Market (FOK): fill at current market price or cancel. Limit (GTC): place order at target's price ± slippage and wait.",
        )
        sell_slippage = st.number_input("Sell Slippage (%)", value=30.0, min_value=0.0, max_value=100.0)

        st.markdown("##### Limit Order Settings")
        limit_timeout_seconds = st.number_input(
            "Limit Order Timeout (seconds)", value=30, min_value=5, max_value=300, step=5,
            help="How long to wait for a limit (GTC) order to fill before cancelling.",
        )
        limit_fallback_market = st.checkbox(
            "Fallback to Market if Limit times out", value=True,
            help="If a limit order doesn't fill within the timeout, automatically retry with a market (FOK) order.",
        )

        if st.form_submit_button("🚀 Add Trader"):
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
                        "is_active": True,
                    }
                )
                if err:
                    st.error(err)
                else:
                    st.success("Trader added! Go to **Traders** page to view details.")
