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

    # Sizing — "fixed" or "proportional"
    sizing_mode = Column(String, default="fixed")
    fixed_amount = Column(Float, default=50.0)
    proportional_pct = Column(Float, default=100.0)

    # Buy-side slippage for market orders
    buy_slippage = Column(Float, default=30.0)

    # Take-profit / Stop-loss (0 = disabled)
    tp_pct = Column(Float, default=0.0)
    sl_pct = Column(Float, default=0.0)

    # "Below Min Limit, Buy at Min" toggle
    buy_at_min = Column(Boolean, default=True)

    # Ignore target wallet trades under this USD value
    ignore_trades_under = Column(Float, default=0.0)

    # Price filters (0 = no limit)
    min_price = Column(Float, default=0.0)
    max_price = Column(Float, default=0.0)

    # Spending / position limits (0 = no limit)
    total_spend_limit = Column(Float, default=0.0)
    min_per_trade = Column(Float, default=0.0)
    max_per_yes_no = Column(Float, default=0.0)
    max_per_trade = Column(Float, default=0.0)
    max_per_market = Column(Float, default=0.0)

    # Max number of markets with holders
    max_holder_market_number = Column(Integer, default=0)

    # Sell settings
    sell_order_type = Column(String, default="market")  # market | limit
    sell_slippage = Column(Float, default=30.0)

    # Legacy / convenience aliases
    max_position_limit = Column(Float, default=500.0)
    max_slippage = Column(Float, default=30.0)
    min_trade_threshold = Column(Float, default=0.0)

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


class BotLog(Base):
    __tablename__ = "bot_logs"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    level = Column(String)       # DEBUG, INFO, WARNING, ERROR
    logger_name = Column(String)
    message = Column(String)


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
                        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
