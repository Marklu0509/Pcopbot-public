"""Trader configuration management page."""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from db.database import get_session_factory, init_db
from db.models import Trader
from bot.watermark import set_watermark

init_db()
_SessionLocal = get_session_factory()


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


def render() -> None:
    st.title("Tracked Traders")
    st.caption("Manage the wallets you want to copy-trade.")

    traders = _get_all_traders()

    if traders:
        st.subheader("Active Traders")
        for t in traders:
            with st.expander(f"{'🟢' if t.is_active else '🔴'} {t.label or t.wallet_address}"):
                col1, col2 = st.columns(2)
                with col1:
                    st.text(f"Wallet: {t.wallet_address}")
                    st.text(f"Sizing mode: {t.sizing_mode}")
                    if t.sizing_mode == "fixed":
                        st.text(f"Fixed amount: ${t.fixed_amount:.2f}")
                    else:
                        st.text(f"Proportional: {t.proportional_pct:.1f}%")
                with col2:
                    st.text(f"Max position: ${t.max_position_limit:.2f}")
                    st.text(f"Max slippage: {t.max_slippage:.1f}%")
                    st.text(f"Min threshold: ${t.min_trade_threshold:.2f}")
                    st.text(f"Watermark: {t.watermark_timestamp}")

                new_active = st.toggle("Active", value=t.is_active, key=f"toggle_{t.id}")
                if new_active != t.is_active:
                    _toggle_trader(t.id, new_active)
                    st.rerun()

                with st.form(key=f"edit_{t.id}"):
                    st.markdown("**Edit parameters**")
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
    else:
        st.info("No traders tracked yet. Add one below.")

    st.divider()
    st.subheader("Add New Trader")
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
