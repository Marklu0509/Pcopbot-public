"""Tests for the trade executor."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, CopyTrade, Trader
from bot.executor import _apply_slippage, _calculate_copy_size, execute_copy_trade


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
        wallet_address="0xEXEC",
        sizing_mode="fixed",
        fixed_amount=50.0,
        proportional_pct=10.0,
        min_trade_threshold=1.0,
        max_position_limit=1000.0,
        max_slippage=5.0,
    )
    session.add(t)
    session.commit()
    return t


def _sample_trade(**kwargs):
    base = {
        "trade_id": "trade-001",
        "market": "market-abc",
        "token_id": "token-xyz",
        "side": "BUY",
        "size": 100.0,
        "price": 0.5,
        "timestamp": datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    }
    base.update(kwargs)
    return base


class TestCalculateCopySize:
    def test_fixed_mode_converts_dollars_to_shares(self):
        t = Trader(sizing_mode="fixed", fixed_amount=50.0)
        # $50 at $0.50/share = 100 shares
        assert _calculate_copy_size(t, 1000.0, 0.5) == pytest.approx(100.0)

    def test_proportional_mode(self):
        t = Trader(sizing_mode="proportional", proportional_pct=10.0)
        assert _calculate_copy_size(t, 200.0, 0.5) == pytest.approx(20.0)

    def test_fixed_mode_zero_price_fallback(self):
        t = Trader(sizing_mode="fixed", fixed_amount=75.0)
        # price=0 edge case: falls back to raw fixed_amount
        assert _calculate_copy_size(t, 999.0, 0.0) == 75.0


class TestApplySlippage:
    def test_buy_increases_price(self):
        assert _apply_slippage(0.50, "BUY", 30.0) == pytest.approx(0.65)

    def test_sell_decreases_price(self):
        assert _apply_slippage(0.50, "SELL", 30.0) == pytest.approx(0.35)

    def test_buy_clamped_at_099(self):
        # 0.90 * 1.30 = 1.17 → clamped to 0.99
        assert _apply_slippage(0.90, "BUY", 30.0) == 0.99

    def test_sell_clamped_at_001(self):
        # 0.02 * 0.70 = 0.014 → but 0.01 * (1 - 99/100) = 0.0001 → clamped to 0.01
        assert _apply_slippage(0.01, "SELL", 99.0) == 0.01

    def test_zero_slippage_unchanged(self):
        assert _apply_slippage(0.50, "BUY", 0.0) == pytest.approx(0.50)
        assert _apply_slippage(0.50, "SELL", 0.0) == pytest.approx(0.50)


class TestDryRun:
    def test_dry_run_creates_record_with_dry_run_status(self, session, trader):
        with patch("bot.executor.settings") as mock_settings:
            mock_settings.DRY_RUN = True
            copy_trade = execute_copy_trade(session, trader, _sample_trade())
        assert copy_trade.status == "dry_run"
        assert copy_trade.order_id is None

    def test_dry_run_does_not_call_post_order(self, session, trader):
        with patch("bot.executor.settings") as mock_settings, \
             patch("bot.executor._get_clob_client") as mock_client:
            mock_settings.DRY_RUN = True
            execute_copy_trade(session, trader, _sample_trade())
            mock_client.assert_not_called()

    def test_dry_run_record_persisted_to_db(self, session, trader):
        with patch("bot.executor.settings") as mock_settings:
            mock_settings.DRY_RUN = True
            execute_copy_trade(session, trader, _sample_trade())
        count = session.query(CopyTrade).count()
        assert count == 1


class TestBelowThreshold:
    def test_below_threshold_skipped(self, session, trader):
        trader.min_per_trade = 100.0  # min $100 per trade
        session.commit()
        with patch("bot.executor.settings") as mock_settings:
            mock_settings.DRY_RUN = False
            # fixed_amount=50 / price=0.5 = 100 shares, trade_value = 100*0.5 = $50 < $100 min
            ct = execute_copy_trade(session, trader, _sample_trade())
        assert ct.status == "below_threshold"
        assert ct.status == "below_threshold"


class TestLiveExecution:
    def test_successful_order_sets_order_id(self, session, trader):
        mock_resp = {"orderID": "order-999"}
        mock_signed_order = MagicMock()
        mock_clob = MagicMock()
        mock_clob.create_order.return_value = mock_signed_order
        mock_clob.post_order.return_value = mock_resp

        with patch("bot.executor.settings") as mock_settings, \
             patch("bot.executor._get_clob_client", return_value=mock_clob), \
             patch("py_clob_client.clob_types.OrderArgs", MagicMock()), \
             patch("py_clob_client.clob_types.OrderType", MagicMock()), \
             patch("py_clob_client.order_builder.constants.BUY", "BUY"), \
             patch("py_clob_client.order_builder.constants.SELL", "SELL"):
            mock_settings.DRY_RUN = False
            ct = execute_copy_trade(session, trader, _sample_trade())

        assert ct.status == "success"
        assert ct.order_id == "order-999"
        mock_clob.create_order.assert_called_once()
        mock_clob.post_order.assert_called_once()


class TestSellCap:
    """SELL orders should be capped at actual holdings."""

    def test_sell_no_holdings_skipped(self, session, trader):
        """Selling with zero holdings should be skipped."""
        with patch("bot.executor.settings") as mock_settings:
            mock_settings.DRY_RUN = True
            ct = execute_copy_trade(session, trader, _sample_trade(side="SELL"))
        assert ct.status == "below_threshold"
        assert ct.copy_size == 0.0

    def test_sell_capped_at_holdings(self, session, trader):
        """Sell size should be capped to what we actually hold."""
        # First, create a BUY record of 30 shares
        buy = CopyTrade(
            trader_id=trader.id, original_trade_id="buy-1", original_market="market-abc",
            original_token_id="token-xyz", original_side="BUY", original_size=100.0,
            original_price=0.5, copy_size=30.0, copy_price=0.5, status="dry_run",
        )
        session.add(buy)
        session.commit()
        with patch("bot.executor.settings") as mock_settings:
            mock_settings.DRY_RUN = True
            # Try to sell 100 shares (proportional would be more than 30)
            ct = execute_copy_trade(session, trader, _sample_trade(side="SELL", size=1000.0))
        # Should be capped at 30 (our holdings), not the calculated copy_size
        assert ct.copy_size <= 30.0
        assert ct.status == "dry_run"

    def test_sell_within_holdings_not_capped(self, session, trader):
        """If sell size <= holdings, it should proceed normally."""
        buy = CopyTrade(
            trader_id=trader.id, original_trade_id="buy-1", original_market="market-abc",
            original_token_id="token-xyz", original_side="BUY", original_size=200.0,
            original_price=0.5, copy_size=200.0, copy_price=0.5, status="dry_run",
        )
        session.add(buy)
        session.commit()
        with patch("bot.executor.settings") as mock_settings:
            mock_settings.DRY_RUN = True
            # fixed_amount=50 / price=0.5 = 100 shares, well within 200 holdings
            ct = execute_copy_trade(session, trader, _sample_trade(side="SELL"))
        assert ct.copy_size == pytest.approx(100.0)
        assert ct.status == "dry_run"

    def test_sell_closes_out_when_leftover_value_below_one_dollar(self, session, trader):
        """If a SELL would leave <$1 residual value, it should sell all holdings."""
        buy = CopyTrade(
            trader_id=trader.id, original_trade_id="buy-1", original_market="market-abc",
            original_token_id="token-xyz", original_side="BUY", original_size=100.0,
            original_price=0.5, copy_size=30.0, copy_price=0.5, status="dry_run",
        )
        session.add(buy)
        session.commit()

        # fixed_amount=14.8 at price=0.5 => sell 29.6 shares, leaving 0.4 shares.
        # leftover value = 0.4 * 0.5 = $0.20 < $1, so should close out to 30.0 shares.
        trader.fixed_amount = 14.8
        session.commit()

        with patch("bot.executor.settings") as mock_settings:
            mock_settings.DRY_RUN = True
            ct = execute_copy_trade(session, trader, _sample_trade(side="SELL", price=0.5))

        assert ct.copy_size == pytest.approx(30.0)
        assert ct.status == "dry_run"
