"""Aggregation buffer for fragmented order fills.

Large limit orders on Polymarket often get split into many small fills.
This buffer accumulates fills per (trader_id, token_id, side) using a
sliding window and triggers execution once the combined value crosses
the ignore_trades_under threshold.

Each buffered fill is recorded individually in CopyTrade. The record ID
is stored on the fill dict as ``_record_id`` so it can be retrieved when
the group triggers or expires.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AggregationResult:
    """Result of adding a fill to the buffer."""

    action: str  # "immediate" | "execute" | "buffered"
    aggregated_trade: dict | None = None
    total_value: float = 0.0
    buffered_count: int = 0
    # CopyTrade IDs of previously buffered fills (available on "execute")
    buffered_record_ids: tuple[int, ...] = ()
    # CopyTrade IDs of fills pruned by sliding window (available on "buffered" and "execute")
    pruned_record_ids: tuple[int, ...] = ()


@dataclass
class _BufferEntry:
    """Internal state for a single (trader_id, token_id, side) buffer slot."""

    fills: list[dict] = field(default_factory=list)
    total_size: float = 0.0
    weighted_price_sum: float = 0.0  # sum(size * price) for VWAP
    total_value: float = 0.0  # sum(size * price) — same as weighted_price_sum
    latest_fill: dict | None = None

    def _recalculate(self) -> None:
        """Recalculate totals from current fills list."""
        self.total_size = sum(f["size"] for f in self.fills)
        self.weighted_price_sum = sum(f["size"] * f["price"] for f in self.fills)
        self.total_value = self.weighted_price_sum
        self.latest_fill = self.fills[-1] if self.fills else None

    def prune_before(self, cutoff: datetime) -> list[dict]:
        """Remove fills older than cutoff. Returns pruned fills."""
        kept: list[dict] = []
        pruned: list[dict] = []
        for f in self.fills:
            ts = f.get("_buffer_ts")
            if ts is not None and ts < cutoff:
                pruned.append(f)
            else:
                kept.append(f)
        if pruned:
            self.fills = kept
            self._recalculate()
        return pruned

    def collect_record_ids(self) -> tuple[int, ...]:
        """Collect _record_id values from all fills that have one."""
        return tuple(
            f["_record_id"] for f in self.fills
            if f.get("_record_id") is not None
        )


class FillBuffer:
    """In-memory aggregation buffer for sub-threshold fills.

    Uses a **sliding window**: each new fill prunes entries older than
    window_seconds from the current time, so fills near the boundary
    of a fixed window are never lost.

    Keyed by (trader_id, token_id, side). Each slot accumulates fills
    until the combined value crosses the threshold, then returns an
    aggregated trade dict for immediate execution.

    Thread-safety: not required — the bot is single-threaded.
    """

    def __init__(self) -> None:
        # Key: (trader_id, token_id, side) — BUY and SELL are buffered separately
        self._slots: dict[tuple[int, str, str], _BufferEntry] = {}

    def add_fill(
        self,
        trader_id: int,
        token_id: str,
        trade: dict,
        threshold: float,
        window_seconds: int,
    ) -> AggregationResult:
        """Add a fill and decide whether to execute, buffer, or pass through.

        Returns:
            AggregationResult with action:
            - "immediate": single fill exceeds threshold, return as-is
            - "execute": accumulated value crossed threshold, return aggregated trade
            - "buffered": fill added to buffer, no action yet
        """
        fill_value = trade["size"] * trade["price"]

        # No threshold or single fill exceeds it: immediate execution
        if threshold <= 0 or fill_value >= threshold:
            return AggregationResult(action="immediate")

        # Aggregation disabled (window=0): treat as immediate
        if window_seconds <= 0:
            return AggregationResult(action="immediate")

        side = trade.get("side", "BUY")
        key = (trader_id, token_id, side)
        now = datetime.now(timezone.utc)

        # Get or create buffer slot
        entry = self._slots.get(key)
        if entry is None:
            entry = _BufferEntry()
            self._slots[key] = entry

        # Sliding window: prune fills older than window_seconds from now
        cutoff = now - timedelta(seconds=window_seconds)
        pruned_fills = entry.prune_before(cutoff)
        pruned_ids = tuple(
            f["_record_id"] for f in pruned_fills
            if f.get("_record_id") is not None
        )

        # Stamp the fill with buffer arrival time and add it
        trade["_buffer_ts"] = now
        entry.fills.append(trade)
        entry.total_size += trade["size"]
        entry.weighted_price_sum += trade["size"] * trade["price"]
        entry.total_value = entry.weighted_price_sum
        entry.latest_fill = trade

        # Check if accumulated value crosses threshold
        if entry.total_value >= threshold:
            aggregated = self._build_aggregated_trade(entry)
            # Collect record IDs from previously buffered fills (not the triggering one)
            buffered_ids = entry.collect_record_ids()
            del self._slots[key]
            return AggregationResult(
                action="execute",
                aggregated_trade=aggregated,
                total_value=entry.total_value,
                buffered_count=len(entry.fills),
                buffered_record_ids=buffered_ids,
                pruned_record_ids=pruned_ids,
            )

        return AggregationResult(
            action="buffered",
            total_value=entry.total_value,
            buffered_count=len(entry.fills),
            pruned_record_ids=pruned_ids,
        )

    def flush_expired(self, now: datetime, window_seconds_map: dict[int, int] | None = None) -> list[tuple[int, str, _BufferEntry]]:
        """Remove buffer entries with no recent fills. Returns (trader_id, token_id, entry) tuples.

        A slot is expired if the latest fill is older than the window.
        window_seconds_map: optional {trader_id: window_seconds} for per-trader windows.
        Falls back to 30s if not provided.
        """
        expired: list[tuple[int, str, _BufferEntry]] = []
        for key, entry in list(self._slots.items()):
            trader_id, token_id, _side = key
            window = 30
            if window_seconds_map and trader_id in window_seconds_map:
                window = window_seconds_map[trader_id]

            # Check latest fill time — if no fills remain or latest is too old, expire
            if not entry.fills:
                expired.append((trader_id, token_id, entry))
                del self._slots[key]
                continue

            latest_ts = entry.latest_fill.get("_buffer_ts") if entry.latest_fill else None
            if latest_ts is None:
                latest_ts = entry.fills[-1].get("_buffer_ts", now)

            elapsed = (now - latest_ts).total_seconds()
            if elapsed > window:
                expired.append((trader_id, token_id, entry))
                logger.info(
                    "Aggregation expired: trader_id=%d token=%s fills=%d value=$%.2f (window=%ds)",
                    trader_id, token_id[:16], len(entry.fills), entry.total_value, window,
                )
                del self._slots[key]
        return expired

    @staticmethod
    def _build_aggregated_trade(entry: _BufferEntry) -> dict:
        """Build a synthetic trade dict from accumulated fills."""
        vwap = entry.weighted_price_sum / entry.total_size if entry.total_size > 0 else 0.0
        latest = entry.latest_fill or entry.fills[-1]
        first = entry.fills[0]

        return {
            "trade_id": f"agg_{first.get('trade_id', 'unknown')}_{len(entry.fills)}",
            "market": latest.get("market", ""),
            "token_id": latest.get("token_id", ""),
            "side": latest.get("side", "BUY"),
            "size": round(entry.total_size, 6),
            "price": round(vwap, 6),
            "timestamp": latest.get("timestamp", datetime.now(timezone.utc)),
            "market_title": latest.get("market_title", ""),
            "outcome": latest.get("outcome", ""),
            # Aggregation metadata for CopyTrade record
            "_agg_fill_count": len(entry.fills),
            "_agg_total_value": round(entry.total_value, 4),
        }
