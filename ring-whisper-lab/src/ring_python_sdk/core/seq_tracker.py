"""Sequence gap / duplicate tracking for 16- or 32-bit packet counters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SeqTrackerStats:
    received_count: int = 0
    first_seq: int = -1
    last_seq: int = -1
    gap_events: int = 0
    missing_count: int = 0
    duplicate_count: int = 0
    out_of_order_count: int = 0


class SeqTracker:
    """Track monotonic sequence numbers with wrap-around."""

    def __init__(self, bits: int = 16) -> None:
        if bits not in {16, 32}:
            raise ValueError("bits must be 16 or 32")
        self._mod = 1 << bits
        self._half = self._mod // 2
        self._last = -1
        self.stats = SeqTrackerStats()

    def observe(self, seq: int) -> None:
        seq &= self._mod - 1
        self.stats.received_count += 1

        if self.stats.first_seq < 0:
            self.stats.first_seq = seq

        if self._last >= 0:
            if seq == self._last:
                self.stats.duplicate_count += 1
            else:
                delta = (seq - self._last) % self._mod
                if delta == 0:
                    self.stats.duplicate_count += 1
                elif delta == 1:
                    pass
                elif delta < self._half:
                    self.stats.gap_events += 1
                    self.stats.missing_count += delta - 1
                else:
                    self.stats.out_of_order_count += 1

        self._last = seq
        self.stats.last_seq = seq

    def expected_count(self) -> int:
        """Inclusive span from first to last seq, if at least one sample."""
        if self.stats.first_seq < 0 or self.stats.last_seq < 0:
            return 0
        return (
            (self.stats.last_seq - self.stats.first_seq) % self._mod
        ) + 1

    def loss_rate_percent(self, received: int | None = None) -> float:
        """Estimated loss = missing / (missing + received)."""
        recv = self.stats.received_count if received is None else received
        missing = self.stats.missing_count
        denom = missing + recv
        if denom <= 0:
            return 0.0
        return 100.0 * missing / denom
