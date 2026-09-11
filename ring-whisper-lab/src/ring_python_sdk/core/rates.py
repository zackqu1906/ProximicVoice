"""Sliding-window frame-rate counters for live sensor streams."""

from __future__ import annotations

import time
from collections import deque


class StreamRateTracker:
    def __init__(self, window_s: float = 1.5) -> None:
        self.window_s = window_s
        self._events: dict[str, deque[float]] = {
            "mic": deque(),
            "imu": deque(),
            "ppg": deque(),
            "ble_test": deque(),
            "swipe": deque(),
            "button": deque(),
            "r2w": deque(),
        }

    def record(self, channel: str, count: int = 1) -> None:
        if channel not in self._events or count <= 0:
            return
        now = time.monotonic()
        bucket = self._events[channel]
        for _ in range(count):
            bucket.append(now)
        self._trim(bucket, now)

    def _trim(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self.window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def fps(self) -> dict[str, float]:
        now = time.monotonic()
        result: dict[str, float] = {}
        for channel, bucket in self._events.items():
            self._trim(bucket, now)
            result[channel] = (
                len(bucket) / self.window_s if self.window_s > 0 else 0.0
            )
        return result

    def reset(self) -> None:
        for bucket in self._events.values():
            bucket.clear()
