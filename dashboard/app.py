"""Streamlit entry point — multi-page app."""

import sys
from pathlib import Path

# Ensure the project root is on sys.path so that `db`, `bot`, `config`,
# and `dashboard` packages are importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

st.set_page_config(
    page_title="Pcopbot Dashboard",
    page_icon="🤖",
    layout="wide",
)

st.sidebar.title("Pcopbot 🤖")
page = st.sidebar.radio(
    "Navigate",
    ["Traders", "History", "PnL"],
)

if page == "Traders":
    from dashboard.pages import traders
    traders.render()
elif page == "History":
    from dashboard.pages import history
    history.render()
elif page == "PnL":
    from dashboard.pages import pnl
    pnl.render()
