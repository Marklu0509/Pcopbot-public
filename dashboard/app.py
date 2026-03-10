"""Streamlit entry point — multi-page app."""

import importlib
import sys
from pathlib import Path

# Ensure the project root is on sys.path so that `db`, `bot`, `config`,
# and `dashboard` packages are importable regardless of cwd.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

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

_pages_dir = Path(__file__).resolve().parent / "pages"


def _load_page(name: str):
    """Import a page module by file path to avoid package-level import issues."""
    spec = importlib.util.spec_from_file_location(name, _pages_dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if page == "Traders":
    _load_page("traders").render()
elif page == "History":
    _load_page("history").render()
elif page == "PnL":
    _load_page("pnl").render()
