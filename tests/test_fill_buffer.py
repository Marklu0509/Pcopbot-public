"""Unit tests for bot.fill_buffer.FillBuffer."""

import sys
import os
from datetime import datetime, timedelta, timezone

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
    assert len(buf._slots) == 0  # nothing buffered


def test_single_fill_below_threshold_buffered():
    buf = FillBuffer()
    trade = _make_trade(size=50, price=0.50)  # $25 < $100
    result = buf.add_fill(1, "tok_abc", trade, threshold=100, window_seconds=30)
    assert result.action == "buffered"
    assert result.total_value == 25.0
    assert result.buffered_count == 1
    assert len(buf._slots) == 1


def test_accumulated_fills_trigger_execute():
    buf = FillBuffer()
    # Fill 1: $25
    r1 = buf.add_fill(1, "tok_abc", _make_trade(50, 0.50, trade_id="t1"), threshold=100, window_seconds=30)
    assert r1.action == "buffered"

    # Fill 2: $30
    r2 = buf.add_fill(1, "tok_abc", _make_trade(60, 0.50, trade_id="t2"), threshold=100, window_seconds=30)
    assert r2.action == "buffered"

    # Fill 3: $50 → total = $105 > $100
    r3 = buf.add_fill(1, "tok_abc", _make_trade(100, 0.50, trade_id="t3"), threshold=100, window_seconds=30)
    assert r3.action == "execute"
    assert r3.buffered_count == 3
    assert r3.total_value == 105.0
    assert r3.aggregated_trade is not None
    assert r3.aggregated_trade["size"] == 210.0  # 50 + 60 + 100
    assert r3.aggregated_trade["price"] == 0.50  # all same price → VWAP = 0.50
    assert r3.aggregated_trade["trade_id"].startswith("agg_")
    # Buffer should be cleared
    assert len(buf._slots) == 0


def test_vwap_calculation():
    buf = FillBuffer()
    # 100 shares @ $0.50 = $50
    buf.add_fill(1, "tok_abc", _make_trade(100, 0.50, trade_id="t1"), threshold=200, window_seconds=30)
    # 200 shares @ $0.60 = $120
    buf.add_fill(1, "tok_abc", _make_trade(200, 0.60, trade_id="t2"), threshold=200, window_seconds=30)
    # 50 shares @ $0.80 = $40 → total = $210 > $200
    r = buf.add_fill(1, "tok_abc", _make_trade(50, 0.80, trade_id="t3"), threshold=200, window_seconds=30)
    assert r.action == "execute"
    # VWAP = (100*0.50 + 200*0.60 + 50*0.80) / 350 = (50+120+40)/350 = 210/350 = 0.6
    assert abs(r.aggregated_trade["price"] - 0.6) < 0.001
    assert r.aggregated_trade["size"] == 350.0


def test_flush_expired_removes_old_entries():
    buf = FillBuffer()
    # Add a fill
    buf.add_fill(1, "tok_abc", _make_trade(10, 0.50), threshold=100, window_seconds=30)
    assert len(buf._slots) == 1

    # Flush with a time far in the future
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    expired = buf.flush_expired(future, window_seconds_map={1: 30})
    assert len(expired) == 1
    trader_id, token_id, entry = expired[0]
    assert trader_id == 1
    assert token_id == "tok_abc"
    assert entry.total_value == 5.0
    assert len(entry.fills) == 1
    assert len(buf._slots) == 0


def test_flush_expired_keeps_active_entries():
    buf = FillBuffer()
    buf.add_fill(1, "tok_abc", _make_trade(10, 0.50), threshold=100, window_seconds=30)

    # Flush with current time (within window)
    now = datetime.now(timezone.utc)
    expired = buf.flush_expired(now, window_seconds_map={1: 30})
    assert len(expired) == 0
    assert len(buf._slots) == 1


def test_buffer_reset_after_execute():
    buf = FillBuffer()
    buf.add_fill(1, "tok_abc", _make_trade(200, 0.50, trade_id="t1"), threshold=150, window_seconds=30)
    r = buf.add_fill(1, "tok_abc", _make_trade(100, 0.60, trade_id="t2"), threshold=150, window_seconds=30)
    assert r.action == "execute"
    assert len(buf._slots) == 0

    # New fill should start fresh
    r2 = buf.add_fill(1, "tok_abc", _make_trade(10, 0.50, trade_id="t3"), threshold=150, window_seconds=30)
    assert r2.action == "buffered"
    assert r2.buffered_count == 1
    assert r2.total_value == 5.0


def test_different_tokens_buffered_separately():
    buf = FillBuffer()
    buf.add_fill(1, "tok_a", _make_trade(50, 0.50, token_id="tok_a"), threshold=100, window_seconds=30)
    buf.add_fill(1, "tok_b", _make_trade(50, 0.50, token_id="tok_b"), threshold=100, window_seconds=30)
    assert len(buf._slots) == 2
    assert buf._slots[(1, "tok_a")].total_value == 25.0
    assert buf._slots[(1, "tok_b")].total_value == 25.0


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
    # $25 < $100
    r1 = buf.add_fill(1, "tok_abc", _make_trade(50, 0.50, side="SELL", trade_id="s1"), threshold=100, window_seconds=30)
    assert r1.action == "buffered"
    # $40 → total = $65 < $100
    r2 = buf.add_fill(1, "tok_abc", _make_trade(80, 0.50, side="SELL", trade_id="s2"), threshold=100, window_seconds=30)
    assert r2.action == "buffered"
    # $40 → total = $105 > $100
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
