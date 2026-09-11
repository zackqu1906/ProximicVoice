"""Decode and persist always-on Raise-to-Wake/Sleep BLE events."""

from __future__ import annotations

import csv
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ring_python_sdk.core.constants import (
    CMD_RAISE_TO_WAKE,
    RAISE_TO_WAKE_EVENT_LABELS,
    RAISE_TO_WAKE_EVENT_PACKET_LEN,
    SUBCMD_RAISE_TO_WAKE_EVENT,
)


def _host_unix_ms() -> int:
    return time.time_ns() // 1_000_000


def format_raise_to_wake_event_line(
    seq: int, event: int, uptime_ms: int
) -> str:
    label = RAISE_TO_WAKE_EVENT_LABELS[event]
    return f"r2w event seq={seq} {label} uptime_ms={uptime_ms}"


@dataclass
class RaiseToWakeStats:
    event_count: int = 0
    packet_count: int = 0
    dropped_packet_count: int = 0


@dataclass
class RaiseToWakeProcessor:
    csv_path: Path
    log: Callable[[str], None] | None = field(default=None, repr=False)
    clock_ms: Callable[[], int] = field(default=_host_unix_ms, repr=False)
    stats: RaiseToWakeStats = field(default_factory=RaiseToWakeStats)
    last_event: str = ""
    _writer: csv.writer | None = field(default=None, repr=False)
    _file: object | None = field(default=None, repr=False)
    _last_seq: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(
            ["host_unix_ms", "seq", "event", "label", "uptime_ms"]
        )

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._writer = None

    def _emit(self, line: str) -> None:
        if self.log is not None:
            self.log(line)
        else:
            print(line)

    def handle_notification(self, _sender, data: bytearray) -> None:
        if len(data) != RAISE_TO_WAKE_EVENT_PACKET_LEN:
            return
        if (
            data[0] != CMD_RAISE_TO_WAKE
            or data[1] != SUBCMD_RAISE_TO_WAKE_EVENT
        ):
            return

        seq, event, uptime_ms = struct.unpack_from("<HBI", data, 2)
        label = RAISE_TO_WAKE_EVENT_LABELS.get(event)
        if label is None:
            return

        self.stats.packet_count += 1
        if self._last_seq is not None:
            expected = (self._last_seq + 1) & 0xFFFF
            if seq != expected:
                self.stats.dropped_packet_count += (seq - expected) & 0xFFFF
        self._last_seq = seq
        self.stats.event_count += 1
        self.last_event = label

        if self._writer is not None:
            self._writer.writerow(
                [self.clock_ms(), seq, event, label, uptime_ms]
            )

        self._emit(format_raise_to_wake_event_line(seq, event, uptime_ms))
