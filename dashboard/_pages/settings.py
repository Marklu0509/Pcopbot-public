"""Bot settings page — configure runtime parameters."""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from db.database import get_session_factory, init_db
from db.models import BotSetting

init_db()
_SessionLocal = get_session_factory()

# Default settings and their descriptions
_DEFAULTS = {
    "poll_interval_seconds": {"default": "15.0", "label": "Poll Interval (seconds)", "help": "How often the bot polls for new trades. Supports decimals, e.g. 0.5."},
    "dry_run": {"default": "false", "label": "Global Dry Run Override", "help": "When enabled, ALL traders are forced into dry run mode regardless of their individual setting. When disabled, each trader uses its own dry_run setting."},
    "auto_sell_enabled": {"default": "true", "label": "Auto-Sell", "help": "When enabled, automatically sells positions at $0.999 when price >= $0.95. When disabled, waits for market resolution and auto-redeems at $1.00 (free, but slower)."},
    "log_level": {"default": "INFO", "label": "Log Level", "help": "Logging verbosity: DEBUG, INFO, WARNING, ERROR."},
}


def _get_setting(key: str) -> str:
    with _SessionLocal() as session:
        row = session.query(BotSetting).filter(BotSetting.key == key).first()
        if row:
            return row.value
    return _DEFAULTS.get(key, {}).get("default", "")


def _set_setting(key: str, value: str) -> None:
    with _SessionLocal() as session:
        row = session.query(BotSetting).filter(BotSetting.key == key).first()
        if row:
            row.value = value
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            row = BotSetting(key=key, value=value)
            session.add(row)
        session.commit()


def render() -> None:
    st.title("Bot Settings")
    st.caption("Changes take effect on the next bot poll cycle (no restart needed).")

    with st.form("settings_form"):
        # Poll Interval
        current_interval = _get_setting("poll_interval_seconds")
        poll_interval = st.number_input(
            _DEFAULTS["poll_interval_seconds"]["label"],
            min_value=0.1,
            max_value=3600.0,
            value=float(current_interval),
            step=0.1,
            format="%.1f",
            help=_DEFAULTS["poll_interval_seconds"]["help"],
        )

        # Dry Run
        current_dry_run = _get_setting("dry_run").lower() in ("true", "1", "yes")
        dry_run = st.toggle(
            _DEFAULTS["dry_run"]["label"],
            value=current_dry_run,
            help=_DEFAULTS["dry_run"]["help"],
        )

        # Auto-Sell
        current_auto_sell = _get_setting("auto_sell_enabled").lower() in ("true", "1", "yes")
        auto_sell = st.toggle(
            _DEFAULTS["auto_sell_enabled"]["label"],
            value=current_auto_sell,
            help=_DEFAULTS["auto_sell_enabled"]["help"],
        )

        # Log Level
        log_levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
        current_log_level = _get_setting("log_level").upper()
        if current_log_level not in log_levels:
            current_log_level = "INFO"
        log_level = st.selectbox(
            _DEFAULTS["log_level"]["label"],
            log_levels,
            index=log_levels.index(current_log_level),
            help=_DEFAULTS["log_level"]["help"],
        )

        if st.form_submit_button("💾 Save Settings"):
            _set_setting("poll_interval_seconds", str(poll_interval))
            _set_setting("dry_run", str(dry_run).lower())
            _set_setting("auto_sell_enabled", str(auto_sell).lower())
            _set_setting("log_level", log_level)
            st.success("Settings saved! Changes will apply on the next poll cycle.")

    st.divider()
    st.subheader("Current Settings")
    for key, meta in _DEFAULTS.items():
        val = _get_setting(key)
        st.text(f"{meta['label']}: {val}")
