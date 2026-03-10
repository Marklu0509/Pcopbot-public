"""Global settings loaded from environment / .env file."""

import os
from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, str(default)).strip().lower()
    return val in ("true", "1", "yes")


# Polymarket credentials
POLYMARKET_PRIVATE_KEY: str = _get("POLYMARKET_PRIVATE_KEY")
POLYMARKET_API_KEY: str = _get("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET: str = _get("POLYMARKET_API_SECRET")
POLYMARKET_API_PASSPHRASE: str = _get("POLYMARKET_API_PASSPHRASE")
POLYMARKET_FUNDER_ADDRESS: str = _get("POLYMARKET_FUNDER_ADDRESS")
POLYMARKET_CHAIN_ID: int = _get_int("POLYMARKET_CHAIN_ID", 137)

# Database
DATABASE_URL: str = _get("DATABASE_URL", "sqlite:///./data/pcopbot.db")

# Bot
POLL_INTERVAL_SECONDS: int = _get_int("POLL_INTERVAL_SECONDS", 15)
DRY_RUN: bool = _get_bool("DRY_RUN", True)
LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")

# Streamlit
STREAMLIT_PORT: int = _get_int("STREAMLIT_PORT", 8501)

# External API base URLs
GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"
DATA_API_BASE: str = "https://data-api.polymarket.com"
