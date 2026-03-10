"""Wallet & API credentials configuration page.

Credentials are stored in the bot_settings table (not in .env) so they
can be updated at runtime from the dashboard.  The bot reads them on each
poll cycle via config/settings.py fallbacks.
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from db.database import get_session_factory, init_db
from db.models import BotSetting

init_db()
_SessionLocal = get_session_factory()

# Keys we manage here, with human-readable labels and help text.
_CREDENTIAL_FIELDS = [
    {
        "key": "polymarket_funder_address",
        "label": "Funder Wallet Address",
        "help": "Your Polymarket funder / deposit address (the wallet that holds USDC).",
        "type": "text",
    },
    {
        "key": "polymarket_private_key",
        "label": "Private Key",
        "help": "Ethereum private key used to sign transactions. Keep this secret!",
        "type": "password",
    },
    {
        "key": "polymarket_api_key",
        "label": "API Key",
        "help": "Polymarket CLOB API key.",
        "type": "password",
    },
    {
        "key": "polymarket_api_secret",
        "label": "API Secret",
        "help": "Polymarket CLOB API secret.",
        "type": "password",
    },
    {
        "key": "polymarket_api_passphrase",
        "label": "API Passphrase",
        "help": "Polymarket CLOB API passphrase.",
        "type": "password",
    },
    {
        "key": "polymarket_chain_id",
        "label": "Chain ID",
        "help": "Polygon chain ID (default 137 for mainnet).",
        "type": "text",
    },
]


def _get_setting(key: str) -> str:
    with _SessionLocal() as session:
        row = session.query(BotSetting).filter(BotSetting.key == key).first()
        return row.value if row else ""


def _set_setting(key: str, value: str) -> None:
    with _SessionLocal() as session:
        row = session.query(BotSetting).filter(BotSetting.key == key).first()
        if row:
            row.value = value
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            session.add(BotSetting(key=key, value=value))
        session.commit()


def render() -> None:
    st.title("🔑 Wallet / API Configuration")
    st.caption(
        "Enter your Polymarket credentials here. They are stored in the database "
        "and used by the bot to place orders. **Never share your private key.**"
    )

    st.info(
        "To operate your Polymarket account the bot needs:\n"
        "1. **Funder Wallet Address** — the wallet holding your USDC on Polygon.\n"
        "2. **Private Key** — to sign on-chain transactions.\n"
        "3. **API Key / Secret / Passphrase** — from [polymarket.com](https://polymarket.com) "
        "developer settings to interact with the CLOB order book.\n"
        "4. **Chain ID** — usually `137` (Polygon mainnet)."
    )

    with st.form("wallet_form"):
        values: dict[str, str] = {}
        for field in _CREDENTIAL_FIELDS:
            current = _get_setting(field["key"])
            if field["type"] == "password":
                values[field["key"]] = st.text_input(
                    field["label"],
                    value=current,
                    type="password",
                    help=field["help"],
                    key=f"w_{field['key']}",
                )
            else:
                values[field["key"]] = st.text_input(
                    field["label"],
                    value=current,
                    help=field["help"],
                    key=f"w_{field['key']}",
                )

        if st.form_submit_button("💾 Save Credentials"):
            for key, val in values.items():
                _set_setting(key, val.strip())
            st.success("Credentials saved!")

    st.divider()
    st.subheader("Current Status")
    all_set = True
    for field in _CREDENTIAL_FIELDS:
        val = _get_setting(field["key"])
        if val:
            st.text(f"✅ {field['label']}: configured")
        else:
            st.text(f"❌ {field['label']}: not set")
            all_set = False

    if all_set:
        st.success("All credentials configured! The bot is ready to place real orders (disable Dry Run in Settings).")
    else:
        st.warning("Some credentials are missing. The bot will run in dry-run mode only.")
