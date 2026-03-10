"""Tests for risk management checks."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, CopyTrade, Trader
from bot.risk import (
    STATUS_BELOW_THRESHOLD,
    STATUS_POSITION_LIMIT,
    STATUS_SLIPPAGE_EXCEEDED,
    check_min_threshold,
    check_position_limit,
    check_slippage,
    run_all_checks,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        yield s


@pytest.fixture()
def trader(session):
    t = Trader(
        wallet_address="0xDEF",
        min_trade_threshold=5.0,
        max_position_limit=500.0,
        max_slippage=2.0,
    )
    session.add(t)
    session.commit()
    return t


class TestMinThreshold:
    def test_below_threshold_rejected(self, trader):
        assert check_min_threshold(4.99, trader) == STATUS_BELOW_THRESHOLD

    def test_at_threshold_accepted(self, trader):
        assert check_min_threshold(5.0, trader) is None

    def test_above_threshold_accepted(self, trader):
        assert check_min_threshold(100.0, trader) is None


class TestPositionLimit:
    def test_no_existing_exposure_accepted(self, session, trader):
        assert check_position_limit(session, trader, "token-1", 100.0) is None

    def test_would_exceed_limit_rejected(self, session, trader):
        # Add existing exposure of 450
        existing = CopyTrade(
            trader_id=trader.id,
            original_trade_id="t1",
            original_market="mkt",
            original_token_id="token-1",
            original_side="BUY",
            original_size=450.0,
            original_price=0.5,
            copy_size=450.0,
            status="success",
        )
        session.add(existing)
        session.commit()
        # Adding 100 would exceed 500 limit
        assert check_position_limit(session, trader, "token-1", 100.0) == STATUS_POSITION_LIMIT

    def test_exactly_at_limit_accepted(self, session, trader):
        existing = CopyTrade(
            trader_id=trader.id,
            original_trade_id="t2",
            original_market="mkt",
            original_token_id="token-2",
            original_side="BUY",
            original_size=400.0,
            original_price=0.5,
            copy_size=400.0,
            status="success",
        )
        session.add(existing)
        session.commit()
        assert check_position_limit(session, trader, "token-2", 100.0) is None

    def test_failed_trades_not_counted(self, session, trader):
        existing = CopyTrade(
            trader_id=trader.id,
            original_trade_id="t3",
            original_market="mkt",
            original_token_id="token-3",
            original_side="BUY",
            original_size=450.0,
            original_price=0.5,
            copy_size=450.0,
            status="failed",
        )
        session.add(existing)
        session.commit()
        assert check_position_limit(session, trader, "token-3", 100.0) is None


class TestSlippageCheck:
    def test_within_slippage_accepted(self, trader):
        assert check_slippage(0.505, 0.5, trader) is None  # 1% slippage

    def test_exceeds_slippage_rejected(self, trader):
        assert check_slippage(0.52, 0.5, trader) == STATUS_SLIPPAGE_EXCEEDED  # 4% slippage

    def test_zero_expected_price_skips_check(self, trader):
        assert check_slippage(0.5, 0.0, trader) is None


class TestRunAllChecks:
    def test_all_pass(self, session, trader):
        assert run_all_checks(session, trader, "tok", 10.0, 0.5, 0.5) is None

    def test_threshold_fails_first(self, session, trader):
        result = run_all_checks(session, trader, "tok", 1.0, 0.5, 0.5)
        assert result == STATUS_BELOW_THRESHOLD

    def test_slippage_checked_after_threshold(self, session, trader):
        result = run_all_checks(session, trader, "tok", 10.0, 0.55, 0.5)  # 10% slippage
        assert result == STATUS_SLIPPAGE_EXCEEDED
