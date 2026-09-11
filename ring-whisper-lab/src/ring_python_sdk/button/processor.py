"""Decode button BLE event packets."""

from __future__ import annotations

import argparse
import csv
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ring_python_sdk.core.constants import (
    BUTTON_EVENT_LABELS,
    BUTTON_EVENT_PACKET_LEN,
    CMD_BUTTON,
    DEFAULT_BUTTON_OUTPUT,
    SUBCMD_BUTTON_EVENT,
)
from ring_python_sdk.core.data_paths import MODE_BUTTON, new_session_dir, resolve_capture_path


def resolve_button_output_path(args: argparse.Namespace) -> Path:
    session = new_session_dir(MODE_BUTTON)
    output = args.output or DEFAULT_BUTTON_OUTPUT
    return resolve_capture_path(
        MODE_BUTTON, output, DEFAULT_BUTTON_OUTPUT, session_dir=session
    )


def format_button_event_line(seq: int, event: int, uptime_ms: int) -> str:
    label = BUTTON_EVENT_LABELS.get(event, str(event))
    return f"button event seq={seq} {label} uptime_ms={uptime_ms}"


@dataclass
class ButtonStats:
    event_count: int = 0
    packet_count: int = 0
    dropped_packet_count: int = 0


@dataclass
class ButtonProcessor:
    csv_path: Path
    print_events: bool = True
    log: Callable[[str], None] | None = field(default=None, repr=False)
    stats: ButtonStats = field(default_factory=ButtonStats)
    _writer: csv.writer | None = field(default=None, repr=False)
    _file: object | None = field(default=None, repr=False)
    _last_seq: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["seq", "event", "label", "uptime_ms"])

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
        if len(data) < BUTTON_EVENT_PACKET_LEN:
            return
        if data[0] != CMD_BUTTON or data[1] != SUBCMD_BUTTON_EVENT:
            return

        seq, event, uptime_ms = struct.unpack_from("<HBI", data, 2)
        label = BUTTON_EVENT_LABELS.get(event, str(event))

        self.stats.packet_count += 1
        if self._last_seq is not None:
            expected = (self._last_seq + 1) & 0xFFFF
            if seq != expected:
                gap = (seq - expected) & 0xFFFF
                self.stats.dropped_packet_count += gap
        self._last_seq = seq
        self.stats.event_count += 1

        if self._writer is not None:
            self._writer.writerow([seq, event, label, uptime_ms])

        # Always surface button events (capture is always-on after connect).
        self._emit(format_button_event_line(seq, event, uptime_ms))
