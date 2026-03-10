"""Tests for trade tracker — parsing and filtering."""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from bot.tracker import get_new_trades, parse_trade


class TestParseTrade:
    def test_unix_timestamp(self):
        raw = {
            "id": "t1",
            "market": "mkt",
            "asset_id": "tok",
            "side": "BUY",
            "size": "10.5",
            "price": "0.6",
            "timestamp": 1700000000,
        }
        trade = parse_trade(raw)
        assert trade["trade_id"] == "t1"
        assert trade["market"] == "mkt"
        assert trade["token_id"] == "tok"
        assert trade["side"] == "BUY"
        assert trade["size"] == pytest.approx(10.5)
        assert trade["price"] == pytest.approx(0.6)
        assert isinstance(trade["timestamp"], datetime)
        assert trade["timestamp"].tzinfo == timezone.utc

    def test_iso_timestamp(self):
        raw = {
            "id": "t2",
            "conditionId": "mkt2",
            "tokenId": "tok2",
            "side": "sell",
            "size": "5.0",
            "price": "0.4",
            "createdAt": "2024-01-15T10:30:00Z",
        }
        trade = parse_trade(raw)
        assert trade["side"] == "SELL"
        assert trade["timestamp"].year == 2024

    def test_missing_fields_default_gracefully(self):
        trade = parse_trade({})
        assert trade["size"] == 0
        assert trade["price"] == 0
        assert isinstance(trade["timestamp"], datetime)


class TestGetNewTrades:
    def _make_trade_raw(self, ts_unix: int, trade_id: str = "t1"):
        return {
            "id": trade_id,
            "market": "mkt",
            "asset_id": "tok",
            "side": "BUY",
            "size": "10",
            "price": "0.5",
            "timestamp": ts_unix,
        }

    def test_only_returns_trades_after_watermark(self):
        watermark = datetime(2024, 1, 1, 12, 0, 0)  # naive UTC
        old_ts = int(datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc).timestamp())
        new_ts = int(datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc).timestamp())

        raw_trades = [
            self._make_trade_raw(old_ts, "old"),
            self._make_trade_raw(new_ts, "new"),
        ]

        with patch("bot.tracker.fetch_trades", return_value=raw_trades):
            trades = get_new_trades("0xABC", watermark)

        assert len(trades) == 1
        assert trades[0]["trade_id"] == "new"

    def test_empty_response_returns_empty(self):
        watermark = datetime(2024, 1, 1)
        with patch("bot.tracker.fetch_trades", return_value=[]):
            trades = get_new_trades("0xABC", watermark)
        assert trades == []

    def test_results_sorted_oldest_first(self):
        watermark = datetime(2024, 1, 1, 0, 0, 0)
        ts1 = int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=timezone.utc).timestamp())
        ts2 = int(datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc).timestamp())
        ts3 = int(datetime(2024, 1, 1, 15, 0, 0, tzinfo=timezone.utc).timestamp())

        raw_trades = [
            self._make_trade_raw(ts1, "t14"),
            self._make_trade_raw(ts2, "t13"),
            self._make_trade_raw(ts3, "t15"),
        ]
        with patch("bot.tracker.fetch_trades", return_value=raw_trades):
            trades = get_new_trades("0xABC", watermark)

        assert [t["trade_id"] for t in trades] == ["t13", "t14", "t15"]

    def test_deduplication_via_watermark(self):
        """Same trade reappearing in API response should not appear if watermark advanced."""
        watermark = datetime(2024, 1, 1, 14, 0, 0)  # already past this trade
        ts = int(datetime(2024, 1, 1, 13, 0, 0, tzinfo=timezone.utc).timestamp())
        raw_trades = [self._make_trade_raw(ts, "dup")]
        with patch("bot.tracker.fetch_trades", return_value=raw_trades):
            trades = get_new_trades("0xABC", watermark)
        assert trades == []
