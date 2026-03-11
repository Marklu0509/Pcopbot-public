"""Streamlit entry point — multi-page app."""

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure the project root is on sys.path so that `db`, `bot`, `config`,
# and `dashboard` packages are importable regardless of cwd.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import streamlit as st
from config import settings as _settings

st.set_page_config(
    page_title="Pcopbot Dashboard",
    page_icon="🤖",
    layout="wide",
)

# ── Password gate ──
_pw = _settings.DASHBOARD_PASSWORD
if _pw:
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if not st.session_state.authenticated:
        st.title("🔒 Login")
        entered = st.text_input("Password", type="password")
        if st.button("Login"):
            if entered == _pw:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("密碼錯誤")
        st.stop()

# ── Live clock in sidebar (UTC, matching trading system) ──
_now_utc = datetime.now(timezone.utc)
st.sidebar.markdown(
    f"🕐 **{_now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC**"
)
st.sidebar.divider()

st.sidebar.title("Pcopbot 🤖")
page = st.sidebar.radio(
    "Navigate",
    ["Traders", "Add Trader", "History", "PnL", "Logs", "Settings", "Wallet / API"],
)

_pages_dir = Path(__file__).resolve().parent / "_pages"


def _load_page(name: str):
    """Import a page module by file path to avoid package-level import issues."""
    spec = importlib.util.spec_from_file_location(name, _pages_dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if page == "Traders":
    _load_page("traders").render()
elif page == "Add Trader":
    _load_page("add_trader").render()
elif page == "History":
    _load_page("history").render()
elif page == "PnL":
    _load_page("pnl").render()
elif page == "Logs":
    _load_page("logs").render()
elif page == "Settings":
    _load_page("settings").render()
elif page == "Wallet / API":
    _load_page("wallet").render()
