"""Tests for risk management checks."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, CopyTrade, Trader
from bot.risk import (
    STATUS_BELOW_THRESHOLD,
    STATUS_BELOW_MINIMUM_ORDER,
    STATUS_POSITION_LIMIT,
    STATUS_SLIPPAGE_EXCEEDED,
    check_min_threshold,
    check_position_limit,
    check_slippage,
    check_ignore_trades_under,
    check_price_filter,
    check_per_trade_limit,
    check_total_spend_limit,
    check_max_per_market,
    check_max_per_yes_no,
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


class TestIgnoreTradesUnder:
    def test_below_threshold_rejected(self, trader):
        trader.ignore_trades_under = 10.0
        assert check_ignore_trades_under(5.0, 1.0, trader) == STATUS_BELOW_THRESHOLD

    def test_above_threshold_accepted(self, trader):
        trader.ignore_trades_under = 10.0
        assert check_ignore_trades_under(100.0, 0.5, trader) is None

    def test_disabled_when_zero(self, trader):
        trader.ignore_trades_under = 0.0
        assert check_ignore_trades_under(1.0, 0.01, trader) is None


class TestPriceFilter:
    def test_below_min_rejected(self, trader):
        trader.min_price = 0.10
        assert check_price_filter(0.05, trader) == STATUS_BELOW_THRESHOLD

    def test_above_max_rejected(self, trader):
        trader.max_price = 0.90
        assert check_price_filter(0.95, trader) == STATUS_BELOW_THRESHOLD

    def test_within_range_accepted(self, trader):
        trader.min_price = 0.10
        trader.max_price = 0.90
        assert check_price_filter(0.50, trader) is None

    def test_disabled_when_zero(self, trader):
        trader.min_price = 0.0
        trader.max_price = 0.0
        assert check_price_filter(0.50, trader) is None


class TestPerTradeLimit:
    def test_below_min_rejected(self, trader):
        trader.min_per_trade = 10.0
        assert check_per_trade_limit(5.0, 1.0, trader) == STATUS_BELOW_THRESHOLD

    def test_above_max_rejected(self, trader):
        trader.max_per_trade = 100.0
        assert check_per_trade_limit(200.0, 1.0, trader) == STATUS_POSITION_LIMIT

    def test_within_range_accepted(self, trader):
        trader.min_per_trade = 10.0
        trader.max_per_trade = 100.0
        assert check_per_trade_limit(50.0, 1.0, trader) is None


class TestTotalSpendLimit:
    def test_exceeds_limit_rejected(self, session, trader):
        trader.total_spend_limit = 100.0
        session.commit()
        existing = CopyTrade(
            trader_id=trader.id, original_trade_id="t1", original_market="mkt",
            original_token_id="tok", original_side="BUY", original_size=80.0,
            original_price=1.0, copy_size=80.0, copy_price=1.0, status="success",
        )
        session.add(existing)
        session.commit()
        assert check_total_spend_limit(session, trader, 30.0, 1.0) == STATUS_POSITION_LIMIT

    def test_within_limit_accepted(self, session, trader):
        trader.total_spend_limit = 100.0
        session.commit()
        assert check_total_spend_limit(session, trader, 50.0, 1.0) is None

    def test_disabled_when_zero(self, session, trader):
        trader.total_spend_limit = 0.0
        assert check_total_spend_limit(session, trader, 9999.0, 1.0) is None


class TestMaxPerMarket:
    def test_exceeds_rejected(self, session, trader):
        trader.max_per_market = 100.0
        session.commit()
        existing = CopyTrade(
            trader_id=trader.id, original_trade_id="t1", original_market="mkt-A",
            original_token_id="tok", original_side="BUY", original_size=80.0,
            original_price=1.0, copy_size=80.0, copy_price=1.0, status="success",
        )
        session.add(existing)
        session.commit()
        assert check_max_per_market(session, trader, "mkt-A", 30.0, 1.0) == STATUS_POSITION_LIMIT

    def test_different_market_accepted(self, session, trader):
        trader.max_per_market = 100.0
        session.commit()
        existing = CopyTrade(
            trader_id=trader.id, original_trade_id="t1", original_market="mkt-A",
            original_token_id="tok", original_side="BUY", original_size=80.0,
            original_price=1.0, copy_size=80.0, copy_price=1.0, status="success",
        )
        session.add(existing)
        session.commit()
        assert check_max_per_market(session, trader, "mkt-B", 80.0, 1.0) is None


class TestMaxPerYesNo:
    def test_exceeds_rejected(self, session, trader):
        trader.max_per_yes_no = 50.0
        session.commit()
        existing = CopyTrade(
            trader_id=trader.id, original_trade_id="t1", original_market="mkt",
            original_token_id="tok-yes", original_side="BUY", original_size=40.0,
            original_price=1.0, copy_size=40.0, copy_price=1.0, status="success",
        )
        session.add(existing)
        session.commit()
        assert check_max_per_yes_no(session, trader, "tok-yes", 20.0, 1.0) == STATUS_POSITION_LIMIT


class TestPositionLimit:
    def test_no_existing_exposure_accepted(self, session, trader):
        assert check_position_limit(session, trader, "token-1", 100.0, 1.0) is None

    def test_would_exceed_limit_rejected(self, session, trader):
        existing = CopyTrade(
            trader_id=trader.id, original_trade_id="t1", original_market="mkt",
            original_token_id="token-1", original_side="BUY", original_size=450.0,
            original_price=1.0, copy_size=450.0, copy_price=1.0, status="success",
        )
        session.add(existing)
        session.commit()
        assert check_position_limit(session, trader, "token-1", 100.0, 1.0) == STATUS_POSITION_LIMIT

    def test_sells_reduce_exposure(self, session, trader):
        """SELL trades should offset BUY exposure."""
        buy = CopyTrade(
            trader_id=trader.id, original_trade_id="t1", original_market="mkt",
            original_token_id="token-1", original_side="BUY", original_size=400.0,
            original_price=1.0, copy_size=400.0, copy_price=1.0, status="success",
        )
        sell = CopyTrade(
            trader_id=trader.id, original_trade_id="t2", original_market="mkt",
            original_token_id="token-1", original_side="SELL", original_size=300.0,
            original_price=1.0, copy_size=300.0, copy_price=1.0, status="success",
        )
        session.add(buy)
        session.add(sell)
        session.commit()
        # Net = 400-300 = $100 exposure, adding $100 = $200 < $500 limit
        assert check_position_limit(session, trader, "token-1", 100.0, 1.0) is None

    def test_failed_trades_not_counted(self, session, trader):
        existing = CopyTrade(
            trader_id=trader.id, original_trade_id="t3", original_market="mkt",
            original_token_id="token-3", original_side="BUY", original_size=450.0,
            original_price=1.0, copy_size=450.0, copy_price=1.0, status="failed",
        )
        session.add(existing)
        session.commit()
        assert check_position_limit(session, trader, "token-3", 100.0, 1.0) is None


class TestSlippageCheck:
    def test_within_slippage_accepted(self, trader):
        assert check_slippage(0.505, 0.5, trader) is None  # 1% slippage

    def test_exceeds_slippage_rejected(self, trader):
        assert check_slippage(0.52, 0.5, trader) == STATUS_SLIPPAGE_EXCEEDED  # 4% slippage

    def test_zero_expected_price_skips_check(self, trader):
        assert check_slippage(0.5, 0.0, trader) is None


class TestRunAllChecks:
    def _call(self, session, trader, **kw):
        defaults = {
            "token_id": "tok", "market": "mkt",
            "copy_size": 10.0, "best_price": 0.5, "expected_price": 0.5,
            "original_size": 100.0, "original_price": 0.5, "side": "BUY",
        }
        defaults.update(kw)
        return run_all_checks(session, trader, **defaults)

    def test_all_pass(self, session, trader):
        assert self._call(session, trader) is None

    def test_per_trade_limit_rejects_small_trade(self, session, trader):
        trader.min_per_trade = 100.0
        session.commit()
        # copy_size=10 * price=0.5 = $5 < $100 min
        result = self._call(session, trader)
        assert result == STATUS_BELOW_THRESHOLD

    def test_slippage_checked(self, session, trader):
        result = self._call(session, trader, best_price=0.55)  # 10% slippage
        assert result == STATUS_SLIPPAGE_EXCEEDED

    def test_sell_skips_buy_spending_limits(self, session, trader):
        """SELL side should skip spending/position limits but still check filters."""
        trader.max_per_trade = 1.0  # Would reject BUY due to spending limit
        session.commit()
        # SELL should pass — max_per_trade only applies to BUY
        result = self._call(session, trader, side="SELL")
        assert result is None

    def test_sell_still_checks_ignore_trades_under(self, session, trader):
        """SELL side should still apply ignore_trades_under filter."""
        trader.ignore_trades_under = 9999.0
        session.commit()
        result = self._call(session, trader, side="SELL")
        assert result == STATUS_BELOW_THRESHOLD

    def test_below_minimum_order_rejected(self, session, trader):
        """Orders below per-market minimum should be rejected."""
        # copy_size=10 * price=0.5 = $5 < $15 minimum
        result = self._call(session, trader, copy_size=10.0, minimum_order_size=15.0)
        assert result == STATUS_BELOW_MINIMUM_ORDER

    def test_no_minimum_order_size_allows_small_orders(self, session, trader):
        """When minimum_order_size is 0 (default), no minimum check applies."""
        result = self._call(session, trader, copy_size=1.0)
        assert result is None
