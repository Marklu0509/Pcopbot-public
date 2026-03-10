"""Tests for the trade executor."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, CopyTrade, Trader
from bot.executor import _calculate_copy_size, execute_copy_trade


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
    def test_fixed_mode_ignores_original_size(self):
        t = Trader(sizing_mode="fixed", fixed_amount=50.0)
        assert _calculate_copy_size(t, 1000.0) == 50.0

    def test_proportional_mode(self):
        t = Trader(sizing_mode="proportional", proportional_pct=10.0)
        assert _calculate_copy_size(t, 200.0) == pytest.approx(20.0)

    def test_default_mode_is_fixed(self):
        t = Trader(fixed_amount=75.0)
        t.sizing_mode = "fixed"
        assert _calculate_copy_size(t, 999.0) == 75.0


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
        trader.min_trade_threshold = 100.0
        session.commit()
        with patch("bot.executor.settings") as mock_settings:
            mock_settings.DRY_RUN = False
            # fixed_amount=50 < min_threshold=100
            ct = execute_copy_trade(session, trader, _sample_trade())
        assert ct.status == "below_threshold"


class TestLiveExecution:
    def test_successful_order_sets_order_id(self, session, trader):
        mock_resp = {"orderID": "order-999"}
        mock_clob = MagicMock()
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
