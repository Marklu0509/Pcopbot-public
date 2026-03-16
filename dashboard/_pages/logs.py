"""Bot log viewer page."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from db.database import get_session_factory, init_db
from db.models import BotLog

init_db()
_SessionLocal = get_session_factory()

LEVEL_ICONS = {
    "DEBUG": "🔍",
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "ERROR": "❌",
}

PAGE_SIZE = 100


def _load_logs(level_filter: str = "All", page: int = 1) -> tuple:
    with _SessionLocal() as session:
        q = session.query(BotLog)
        if level_filter != "All":
            q = q.filter(BotLog.level == level_filter)
        total = q.count()
        rows = (
            q.order_by(BotLog.timestamp.desc())
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
            .all()
        )

    if not rows:
        return pd.DataFrame(), total

    df = pd.DataFrame(
        [
            {
                "Time": r.timestamp,
                "Level": f"{LEVEL_ICONS.get(r.level, '')} {r.level}",
                "Logger": r.logger_name,
                "Message": r.message,
            }
            for r in rows
        ]
    )
    return df, total


def render() -> None:
    st.title("Bot Logs")

    col1, col2 = st.columns(2)
    with col1:
        level_filter = st.selectbox("Level", ["All", "DEBUG", "INFO", "WARNING", "ERROR"])
    with col2:
        page = st.number_input("Page", min_value=1, value=1, step=1)

    df, total = _load_logs(level_filter=level_filter, page=int(page))
    st.caption(f"Total logs: {total} | Page size: {PAGE_SIZE}")

    if df.empty:
        st.info("No logs yet. Start the bot to generate logs.")
    else:
        st.dataframe(df, use_container_width=True)

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("🔄 Refresh"):
            st.rerun()
    with col_b:
        if st.button("🗑️ Clear Logs"):
            st.session_state["confirm_clear_logs"] = True

    if st.session_state.get("confirm_clear_logs"):
        st.warning("This will permanently delete all logs. Are you sure?")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Yes, delete all logs"):
                with _SessionLocal() as session:
                    session.query(BotLog).delete()
                    session.commit()
                st.session_state["confirm_clear_logs"] = False
                st.success("Logs cleared.")
                st.rerun()
        with c2:
            if st.button("Cancel"):
                st.session_state["confirm_clear_logs"] = False
                st.rerun()
