"""Global settings loaded from environment / .env file, with database fallback.

Polymarket credentials can be set either via environment variables (.env)
or through the dashboard UI (stored in the bot_settings DB table).
Environment variables take priority; if empty, the DB value is used.
"""

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


def _get_db_setting(key: str) -> str:
    """Read a single value from the bot_settings table, or return '' on any error."""
    try:
        from db.database import get_session_factory, init_db
        from db.models import BotSetting
        init_db()
        Session = get_session_factory()
        with Session() as session:
            row = session.query(BotSetting).filter(BotSetting.key == key).first()
            return row.value if row else ""
    except Exception:
        return ""


def _get_credential(env_key: str, db_key: str) -> str:
    """Return env var if set, otherwise fall back to bot_settings DB table."""
    val = os.environ.get(env_key, "").strip()
    if val:
        return val
    return _get_db_setting(db_key)


# Polymarket credentials (env → DB fallback)
POLYMARKET_PRIVATE_KEY: str = _get_credential("POLYMARKET_PRIVATE_KEY", "polymarket_private_key")
POLYMARKET_API_KEY: str = _get_credential("POLYMARKET_API_KEY", "polymarket_api_key")
POLYMARKET_API_SECRET: str = _get_credential("POLYMARKET_API_SECRET", "polymarket_api_secret")
POLYMARKET_API_PASSPHRASE: str = _get_credential("POLYMARKET_API_PASSPHRASE", "polymarket_api_passphrase")
POLYMARKET_FUNDER_ADDRESS: str = _get_credential("POLYMARKET_FUNDER_ADDRESS", "polymarket_funder_address")
POLYMARKET_CHAIN_ID: int = int(_get_credential("POLYMARKET_CHAIN_ID", "polymarket_chain_id") or "137")

# Database
DATABASE_URL: str = _get("DATABASE_URL", "sqlite:///./data/pcopbot.db")

# Bot
POLL_INTERVAL_SECONDS: int = _get_int("POLL_INTERVAL_SECONDS", 15)
DRY_RUN: bool = _get_bool("DRY_RUN", True)
LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")

# Streamlit
STREAMLIT_PORT: int = _get_int("STREAMLIT_PORT", 8501)
DASHBOARD_PASSWORD: str = _get("DASHBOARD_PASSWORD")

# External API base URLs
GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"
DATA_API_BASE: str = "https://data-api.polymarket.com"
