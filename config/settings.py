"""Global settings loaded from environment / .env file."""

import os
from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_clean(key: str, default: str = "") -> str:
    """Read env var and normalize common copy/paste formatting issues."""
    value = os.environ.get(key, default)
    if value is None:
        return default
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1].strip()
    return value


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, str(default)).strip().lower()
    return val in ("true", "1", "yes")


# Polymarket credentials (from .env only)
POLYMARKET_PRIVATE_KEY: str = _get_clean("POLYMARKET_PRIVATE_KEY")
POLYMARKET_API_KEY: str = _get_clean("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET: str = _get_clean("POLYMARKET_API_SECRET")
POLYMARKET_API_PASSPHRASE: str = _get_clean("POLYMARKET_API_PASSPHRASE")
POLYMARKET_FUNDER_ADDRESS: str = _get_clean("POLYMARKET_FUNDER_ADDRESS")
# Optional: funder wallet private key for on-chain redemptions (proxy-wallet setups).
# In proxy-wallet mode, POLYMARKET_PRIVATE_KEY is the proxy key and cannot sign
# redeemPositions transactions (tokens are held by the funder wallet).
# Set this to the funder wallet's private key to enable auto-redemption.
# Leave empty if POLYMARKET_PRIVATE_KEY already IS the funder wallet key.
POLYMARKET_FUNDER_PRIVATE_KEY: str = _get_clean("POLYMARKET_FUNDER_PRIVATE_KEY")
POLYMARKET_CHAIN_ID: int = _get_int("POLYMARKET_CHAIN_ID", 137)

# Database
DATABASE_URL: str = _get("DATABASE_URL", "sqlite:///./data/pcopbot.db")

# Bot
POLL_INTERVAL_SECONDS: float = _get_float("POLL_INTERVAL_SECONDS", 15.0)
DRY_RUN: bool = _get_bool("DRY_RUN", True)
LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")

# Streamlit
STREAMLIT_PORT: int = _get_int("STREAMLIT_PORT", 8501)
DASHBOARD_PASSWORD: str = _get("DASHBOARD_PASSWORD")

# Polygon RPC (override to use a private/reliable endpoint, e.g. Alchemy or QuickNode)
POLYGON_RPC_URL: str = _get("POLYGON_RPC_URL", "")

# External API base URLs
GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"
DATA_API_BASE: str = "https://data-api.polymarket.com"
