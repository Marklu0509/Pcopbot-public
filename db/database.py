"""Database engine and session setup."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import settings
from db.models import Base

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        db_url = settings.DATABASE_URL
        # Ensure the data directory exists for SQLite
        if db_url.startswith("sqlite"):
            db_path = db_url.replace("sqlite:///", "")
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        _engine = create_engine(db_url, connect_args=connect_args)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return _SessionLocal


def init_db() -> None:
    """Create all tables if they don't exist."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    # Schema migration: add columns that may not exist in older databases
    import logging
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    _db_logger = logging.getLogger(__name__)
    with engine.begin() as conn:
        for stmt in [
            "ALTER TABLE traders ADD COLUMN buy_order_type VARCHAR DEFAULT 'market'",
            "ALTER TABLE traders ADD COLUMN limit_timeout_seconds INTEGER DEFAULT 30",
            "ALTER TABLE traders ADD COLUMN limit_fallback_market BOOLEAN DEFAULT 1",
        ]:
            try:
                conn.execute(text(stmt))
            except OperationalError:
                pass  # Column already exists
            except Exception as exc:
                _db_logger.warning("Schema migration failed: %s — %s", stmt[:50], exc)


def get_db():
    """Context-managed DB session (for use in 'with' statements)."""
    SessionLocal = get_session_factory()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
