"""SQLAlchemy ORM models."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Trader(Base):
    __tablename__ = "traders"

    id = Column(Integer, primary_key=True)
    wallet_address = Column(String, unique=True, nullable=False)
    label = Column(String, default="")
    is_active = Column(Boolean, default=True)

    # Sizing mode: "fixed" or "proportional"
    sizing_mode = Column(String, default="fixed")
    fixed_amount = Column(Float, default=50.0)
    proportional_pct = Column(Float, default=10.0)

    # Risk parameters
    max_position_limit = Column(Float, default=500.0)
    max_slippage = Column(Float, default=2.0)
    min_trade_threshold = Column(Float, default=5.0)

    # Watermark — set on startup / when trader is added
    watermark_timestamp = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    copy_trades = relationship("CopyTrade", back_populates="trader")

    def __repr__(self) -> str:
        return f"<Trader id={self.id} wallet={self.wallet_address} label={self.label!r}>"


class CopyTrade(Base):
    __tablename__ = "copy_trades"

    id = Column(Integer, primary_key=True)
    trader_id = Column(Integer, ForeignKey("traders.id"))

    # Original trade info
    original_trade_id = Column(String)
    original_market = Column(String)
    original_token_id = Column(String)
    original_side = Column(String)
    original_size = Column(Float)
    original_price = Column(Float)
    original_timestamp = Column(DateTime)

    # Copy trade parameters
    copy_size = Column(Float)
    copy_price = Column(Float, nullable=True)

    # Execution outcome
    status = Column(String)  # success | failed | slippage_exceeded | dry_run | below_threshold | position_limit
    error_message = Column(String, nullable=True)
    order_id = Column(String, nullable=True)

    # PnL (updated externally once market resolves)
    pnl = Column(Float, default=0.0)

    executed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    trader = relationship("Trader", back_populates="copy_trades")

    def __repr__(self) -> str:
        return f"<CopyTrade id={self.id} status={self.status} market={self.original_market!r}>"
