"""Tests for watermarking logic."""

from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, Trader
from bot.watermark import advance_watermark, is_new_trade, set_watermark


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        yield s


@pytest.fixture()
def trader(session):
    t = Trader(wallet_address="0xABC", label="test")
    session.add(t)
    session.commit()
    return t


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestSetWatermark:
    def test_sets_watermark_to_now_when_no_ts_given(self, session, trader):
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        set_watermark(session, trader)
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        assert trader.watermark_timestamp is not None
        assert before <= trader.watermark_timestamp <= after

    def test_sets_watermark_to_provided_ts(self, session, trader):
        ts = datetime(2024, 1, 1, 12, 0, 0)
        set_watermark(session, trader, ts)
        assert trader.watermark_timestamp == ts

    def test_overwrites_existing_watermark(self, session, trader):
        set_watermark(session, trader, datetime(2024, 1, 1))
        set_watermark(session, trader, datetime(2025, 6, 1))
        assert trader.watermark_timestamp == datetime(2025, 6, 1)


class TestIsNewTrade:
    def test_trade_after_watermark_is_new(self, trader):
        trader.watermark_timestamp = datetime(2024, 1, 1, 12, 0, 0)
        assert is_new_trade(trader, _utc(2024, 1, 1, 13, 0, 0)) is True

    def test_trade_before_watermark_is_not_new(self, trader):
        trader.watermark_timestamp = datetime(2024, 1, 1, 12, 0, 0)
        assert is_new_trade(trader, _utc(2024, 1, 1, 11, 0, 0)) is False

    def test_trade_equal_to_watermark_is_not_new(self, trader):
        wm = datetime(2024, 1, 1, 12, 0, 0)
        trader.watermark_timestamp = wm
        assert is_new_trade(trader, wm.replace(tzinfo=timezone.utc)) is False

    def test_no_watermark_treats_all_as_new(self, trader):
        trader.watermark_timestamp = None
        assert is_new_trade(trader, _utc(2020, 1, 1)) is True


class TestAdvanceWatermark:
    def test_advances_watermark_when_newer(self, session, trader):
        trader.watermark_timestamp = datetime(2024, 1, 1)
        new_ts = _utc(2024, 6, 1)
        advance_watermark(session, trader, new_ts)
        assert trader.watermark_timestamp == datetime(2024, 6, 1)

    def test_does_not_go_backwards(self, session, trader):
        trader.watermark_timestamp = datetime(2024, 6, 1)
        old_ts = _utc(2024, 1, 1)
        advance_watermark(session, trader, old_ts)
        assert trader.watermark_timestamp == datetime(2024, 6, 1)

    def test_advances_from_none(self, session, trader):
        trader.watermark_timestamp = None
        ts = _utc(2024, 3, 15)
        advance_watermark(session, trader, ts)
        assert trader.watermark_timestamp == datetime(2024, 3, 15)
