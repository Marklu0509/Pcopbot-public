"""Unit tests for bot.fill_buffer.FillBuffer (sliding window + record tracking)."""

import sys
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.fill_buffer import FillBuffer, AggregationResult


def _make_trade(size: float, price: float, token_id: str = "tok_abc", side: str = "BUY", trade_id: str = "t1") -> dict:
    return {
        "trade_id": trade_id,
        "market": "market_1",
        "token_id": token_id,
        "side": side,
        "size": size,
        "price": price,
        "timestamp": datetime.now(timezone.utc),
        "market_title": "Test Market",
        "outcome": "Yes",
    }


def test_single_fill_above_threshold_returns_immediate():
    buf = FillBuffer()
    trade = _make_trade(size=200, price=0.60)  # $120 > $100 threshold
    result = buf.add_fill(1, "tok_abc", trade, threshold=100, window_seconds=30)
    assert result.action == "immediate"
    assert result.aggregated_trade is None
    assert not buf._slots  # nothing buffered


def test_single_fill_below_threshold_buffered():
    buf = FillBuffer()
    trade = _make_trade(size=50, price=0.50)  # $25 < $100
    result = buf.add_fill(1, "tok_abc", trade, threshold=100, window_seconds=30)
    assert result.action == "buffered"
    assert result.total_value == 25.0
    assert result.buffered_count == 1
    assert len(buf._slots) == 1  # one slot


def test_accumulated_fills_trigger_execute():
    buf = FillBuffer()
    # Fill 1: $25
    t1 = _make_trade(50, 0.50, trade_id="t1")
    r1 = buf.add_fill(1, "tok_abc", t1, threshold=100, window_seconds=30)
    assert r1.action == "buffered"
    t1["_record_id"] = 101  # simulate DB record creation

    # Fill 2: $30
    t2 = _make_trade(60, 0.50, trade_id="t2")
    r2 = buf.add_fill(1, "tok_abc", t2, threshold=100, window_seconds=30)
    assert r2.action == "buffered"
    t2["_record_id"] = 102

    # Fill 3: $50 → total = $105 > $100
    r3 = buf.add_fill(1, "tok_abc", _make_trade(100, 0.50, trade_id="t3"), threshold=100, window_seconds=30)
    assert r3.action == "execute"
    assert r3.buffered_count == 3
    assert r3.total_value == 105.0
    assert r3.aggregated_trade is not None
    assert r3.aggregated_trade["size"] == 210.0  # 50 + 60 + 100
    assert r3.aggregated_trade["price"] == 0.50
    assert r3.aggregated_trade["trade_id"].startswith("agg_")
    # Should include record IDs of first two fills (not the triggering one)
    assert r3.buffered_record_ids == (101, 102)
    assert not buf._slots


def test_vwap_calculation():
    buf = FillBuffer()
    buf.add_fill(1, "tok_abc", _make_trade(100, 0.50, trade_id="t1"), threshold=200, window_seconds=30)
    buf.add_fill(1, "tok_abc", _make_trade(200, 0.60, trade_id="t2"), threshold=200, window_seconds=30)
    r = buf.add_fill(1, "tok_abc", _make_trade(50, 0.80, trade_id="t3"), threshold=200, window_seconds=30)
    assert r.action == "execute"
    assert abs(r.aggregated_trade["price"] - 0.6) < 0.001
    assert r.aggregated_trade["size"] == 350.0


def test_flush_expired_removes_old_entries():
    buf = FillBuffer()
    t0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    with patch("bot.fill_buffer.datetime") as mock_dt:
        mock_dt.now.return_value = t0
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        trade = _make_trade(10, 0.50)
        buf.add_fill(1, "tok_abc", trade, threshold=100, window_seconds=30)
    trade["_record_id"] = 201
    assert len(buf._slots) == 1  # one slot

    future = t0 + timedelta(seconds=60)
    expired = buf.flush_expired(future, window_seconds_map={1: 30})
    assert len(expired) == 1
    trader_id, token_id, entry = expired[0]
    assert trader_id == 1
    assert token_id == "tok_abc"
    assert entry.total_value == 5.0
    assert entry.collect_record_ids() == (201,)
    assert not buf._slots


def test_flush_expired_keeps_active_entries():
    buf = FillBuffer()
    buf.add_fill(1, "tok_abc", _make_trade(10, 0.50), threshold=100, window_seconds=30)

    now = datetime.now(timezone.utc)
    expired = buf.flush_expired(now, window_seconds_map={1: 30})
    assert len(expired) == 0
    assert len(buf._slots) == 1  # one slot


def test_buffer_reset_after_execute():
    buf = FillBuffer()
    buf.add_fill(1, "tok_abc", _make_trade(200, 0.50, trade_id="t1"), threshold=150, window_seconds=30)
    r = buf.add_fill(1, "tok_abc", _make_trade(100, 0.60, trade_id="t2"), threshold=150, window_seconds=30)
    assert r.action == "execute"
    assert not buf._slots

    r2 = buf.add_fill(1, "tok_abc", _make_trade(10, 0.50, trade_id="t3"), threshold=150, window_seconds=30)
    assert r2.action == "buffered"
    assert r2.buffered_count == 1
    assert r2.total_value == 5.0


def test_different_tokens_buffered_separately():
    buf = FillBuffer()
    buf.add_fill(1, "tok_a", _make_trade(50, 0.50, token_id="tok_a"), threshold=100, window_seconds=30)
    buf.add_fill(1, "tok_b", _make_trade(50, 0.50, token_id="tok_b"), threshold=100, window_seconds=30)
    assert len(buf._slots) == 2
    assert buf._slots[(1, "tok_a", "BUY")].total_value == 25.0
    assert buf._slots[(1, "tok_b", "BUY")].total_value == 25.0


def test_different_traders_buffered_separately():
    buf = FillBuffer()
    buf.add_fill(1, "tok_abc", _make_trade(50, 0.50), threshold=100, window_seconds=30)
    buf.add_fill(2, "tok_abc", _make_trade(50, 0.50), threshold=100, window_seconds=30)
    assert len(buf._slots) == 2


def test_zero_threshold_returns_immediate():
    buf = FillBuffer()
    trade = _make_trade(10, 0.50)
    result = buf.add_fill(1, "tok_abc", trade, threshold=0, window_seconds=30)
    assert result.action == "immediate"


def test_zero_window_returns_immediate():
    buf = FillBuffer()
    trade = _make_trade(10, 0.50)
    result = buf.add_fill(1, "tok_abc", trade, threshold=100, window_seconds=0)
    assert result.action == "immediate"


def test_sell_fills_aggregate_same_as_buy():
    buf = FillBuffer()
    r1 = buf.add_fill(1, "tok_abc", _make_trade(50, 0.50, side="SELL", trade_id="s1"), threshold=100, window_seconds=30)
    assert r1.action == "buffered"
    r2 = buf.add_fill(1, "tok_abc", _make_trade(80, 0.50, side="SELL", trade_id="s2"), threshold=100, window_seconds=30)
    assert r2.action == "buffered"
    r3 = buf.add_fill(1, "tok_abc", _make_trade(80, 0.50, side="SELL", trade_id="s3"), threshold=100, window_seconds=30)
    assert r3.action == "execute"
    assert r3.aggregated_trade["side"] == "SELL"


def test_aggregated_trade_dict_structure():
    buf = FillBuffer()
    buf.add_fill(1, "tok_abc", _make_trade(100, 0.50, trade_id="t1"), threshold=80, window_seconds=30)
    r = buf.add_fill(1, "tok_abc", _make_trade(100, 0.60, trade_id="t2"), threshold=80, window_seconds=30)
    assert r.action == "execute"
    t = r.aggregated_trade
    required_keys = {"trade_id", "market", "token_id", "side", "size", "price", "timestamp", "market_title", "outcome"}
    assert required_keys.issubset(set(t.keys()))
    assert t["trade_id"].startswith("agg_t1_")
    assert t["market"] == "market_1"
    assert t["token_id"] == "tok_abc"


def test_buy_and_sell_same_token_buffered_separately():
    """BUY and SELL fills for the same token must not mix."""
    buf = FillBuffer()
    buf.add_fill(1, "tok_abc", _make_trade(50, 0.50, side="BUY", trade_id="b1"), threshold=100, window_seconds=30)
    buf.add_fill(1, "tok_abc", _make_trade(50, 0.50, side="SELL", trade_id="s1"), threshold=100, window_seconds=30)
    assert len(buf._slots) == 2
    assert (1, "tok_abc", "BUY") in buf._slots
    assert (1, "tok_abc", "SELL") in buf._slots
    r = buf.add_fill(1, "tok_abc", _make_trade(160, 0.50, side="SELL", trade_id="s2"), threshold=100, window_seconds=30)
    assert r.action == "execute"
    assert r.aggregated_trade["side"] == "SELL"
    assert (1, "tok_abc", "BUY") in buf._slots
    assert buf._slots[(1, "tok_abc", "BUY")].total_value == 25.0


# ── Sliding window specific tests ──

def test_sliding_window_keeps_recent_fills_across_old_boundary():
    """Fill at t=25s and t=35s should combine even though t=0 fill would have expired."""
    buf = FillBuffer()
    t0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    with patch("bot.fill_buffer.datetime") as mock_dt:
        mock_dt.now.return_value = t0
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        trade1 = _make_trade(60, 0.50, trade_id="t1")
        r1 = buf.add_fill(1, "tok_abc", trade1, threshold=100, window_seconds=30)
    assert r1.action == "buffered"
    trade1["_record_id"] = 301

    t25 = t0 + timedelta(seconds=25)
    with patch("bot.fill_buffer.datetime") as mock_dt:
        mock_dt.now.return_value = t25
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        r2 = buf.add_fill(1, "tok_abc", _make_trade(80, 0.50, trade_id="t2"), threshold=100, window_seconds=30)
    assert r2.action == "buffered"
    assert r2.total_value == 70.0

    # t=35s: t=0 fill pruned, remaining t=25($40) + t=35($60) = $100 ≥ threshold
    t35 = t0 + timedelta(seconds=35)
    with patch("bot.fill_buffer.datetime") as mock_dt:
        mock_dt.now.return_value = t35
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        r3 = buf.add_fill(1, "tok_abc", _make_trade(120, 0.50, trade_id="t3"), threshold=100, window_seconds=30)
    assert r3.action == "execute"
    assert r3.buffered_count == 2
    assert r3.total_value == 100.0
    assert r3.aggregated_trade["size"] == 200.0
    # t=0 fill was pruned, its record ID should be in pruned_record_ids
    assert r3.pruned_record_ids == (301,)


def test_sliding_window_prunes_old_fills():
    """Old fills outside the window should be pruned, reducing total value."""
    buf = FillBuffer()
    t0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    with patch("bot.fill_buffer.datetime") as mock_dt:
        mock_dt.now.return_value = t0
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        trade1 = _make_trade(60, 0.50, trade_id="t1")
        buf.add_fill(1, "tok_abc", trade1, threshold=100, window_seconds=30)
    trade1["_record_id"] = 401

    t40 = t0 + timedelta(seconds=40)
    with patch("bot.fill_buffer.datetime") as mock_dt:
        mock_dt.now.return_value = t40
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        r = buf.add_fill(1, "tok_abc", _make_trade(40, 0.50, trade_id="t2"), threshold=100, window_seconds=30)
    assert r.action == "buffered"
    assert r.total_value == 20.0
    assert r.buffered_count == 1
    assert r.pruned_record_ids == (401,)


def test_sliding_window_all_fills_in_window_kept():
    buf = FillBuffer()
    t0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    with patch("bot.fill_buffer.datetime") as mock_dt:
        mock_dt.now.return_value = t0
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        buf.add_fill(1, "tok_abc", _make_trade(50, 0.50, trade_id="t1"), threshold=100, window_seconds=30)

    t10 = t0 + timedelta(seconds=10)
    with patch("bot.fill_buffer.datetime") as mock_dt:
        mock_dt.now.return_value = t10
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        r = buf.add_fill(1, "tok_abc", _make_trade(50, 0.50, trade_id="t2"), threshold=100, window_seconds=30)
    assert r.action == "buffered"
    assert r.total_value == 50.0
    assert r.buffered_count == 2
    assert r.pruned_record_ids == ()


def test_record_ids_tracked_through_execute():
    """Record IDs stored on trade dicts are collected on execute."""
    buf = FillBuffer()
    t1 = _make_trade(50, 0.50, trade_id="t1")
    buf.add_fill(1, "tok_abc", t1, threshold=60, window_seconds=30)
    t1["_record_id"] = 501

    t2 = _make_trade(80, 0.50, trade_id="t2")  # $40 → total $65 > $60
    r = buf.add_fill(1, "tok_abc", t2, threshold=60, window_seconds=30)
    # t2 triggers execute but has no _record_id yet (will be set by main.py after)
    assert r.action == "execute"
    assert r.buffered_record_ids == (501,)


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
