"""Decode swipe recognition BLE packets (logits EVENT + debounce TRIGGER)."""

from __future__ import annotations

import argparse
import csv
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ring_python_sdk.core.constants import (
    CMD_SWIPE,
    DEFAULT_SWIPE_OUTPUT,
    SUBCMD_SWIPE_EVENT,
    SUBCMD_SWIPE_TRIGGER,
    SWIPE_CLASS_LABELS,
    SWIPE_EVENT_MAP,
    SWIPE_EVENT_PACKET_LEN,
    SWIPE_NUM_SCORES,
    SWIPE_TRIGGER_PACKET_LEN,
)
from ring_python_sdk.core.data_paths import MODE_SWIPE, new_session_dir, resolve_capture_path


def resolve_swipe_output_path(args: argparse.Namespace) -> Path:
    session = new_session_dir(MODE_SWIPE)
    output = args.output or DEFAULT_SWIPE_OUTPUT
    return resolve_capture_path(
        MODE_SWIPE, output, DEFAULT_SWIPE_OUTPUT, session_dir=session
    )


def _parse_swipe_payload(
    data: bytes | bytearray, subcmd: int, min_len: int
) -> tuple[int, int, tuple[int, ...], int] | None:
    if len(data) < min_len:
        return None
    if data[0] != CMD_SWIPE or data[1] != subcmd:
        return None
    seq, class_id = struct.unpack_from("<HB", data, 2)
    scores = struct.unpack_from(f"<{SWIPE_NUM_SCORES}b", data, 5)
    (uptime_ms,) = struct.unpack_from("<I", data, 5 + SWIPE_NUM_SCORES)
    return seq, class_id, scores, uptime_ms


def parse_swipe_event_packet(
    data: bytes | bytearray,
) -> tuple[int, int, tuple[int, ...], int] | None:
    """Return (seq, class_id, scores[7], uptime_ms) for every-inference EVENT."""
    return _parse_swipe_payload(data, SUBCMD_SWIPE_EVENT, SWIPE_EVENT_PACKET_LEN)


def parse_swipe_trigger_packet(
    data: bytes | bytearray,
) -> tuple[int, int, tuple[int, ...], int] | None:
    """Return (seq, class_id, scores[7], uptime_ms) for debounce TRIGGER."""
    return _parse_swipe_payload(data, SUBCMD_SWIPE_TRIGGER, SWIPE_TRIGGER_PACKET_LEN)


def format_swipe_infer_line(
    seq: int, class_id: int, scores: tuple[int, ...], uptime_ms: int
) -> str:
    """Human-readable board model output for Log / stdout."""
    label = SWIPE_CLASS_LABELS.get(class_id, str(class_id))
    return (
        f"swipe infer seq={seq} class={class_id}({label}) "
        f"scores={list(scores)} uptime_ms={uptime_ms}"
    )


def format_swipe_trigger_line(
    seq: int, class_id: int, scores: tuple[int, ...], uptime_ms: int
) -> str:
    """Recognized gesture event (ai-ring aligned debounce)."""
    label = SWIPE_CLASS_LABELS.get(class_id, str(class_id))
    event = SWIPE_EVENT_MAP.get(class_id, f"class-{class_id}")
    return (
        f"swipe event seq={seq} event={event} class={class_id}({label}) "
        f"scores={list(scores)} uptime_ms={uptime_ms}"
    )


@dataclass
class SwipeStats:
    event_count: int = 0  # inference EVENT packets
    trigger_count: int = 0
    packet_count: int = 0
    dropped_packet_count: int = 0


@dataclass
class SwipeProcessor:
    csv_path: Path
    print_events: bool = True
    log: Callable[[str], None] | None = field(default=None, repr=False)
    stats: SwipeStats = field(default_factory=SwipeStats)
    _writer: csv.writer | None = field(default=None, repr=False)
    _file: object | None = field(default=None, repr=False)
    _last_infer_seq: int | None = field(default=None, repr=False)
    _last_trigger_seq: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(
            [
                "kind",
                "seq",
                "class_id",
                "label",
                "event",
                "s0",
                "s1",
                "s2",
                "s3",
                "s4",
                "s5",
                "s6",
                "uptime_ms",
            ]
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

    def _note_seq_gap(self, seq: int, last: int | None) -> int | None:
        if last is not None:
            expected = (last + 1) & 0xFFFF
            if seq != expected:
                gap = (seq - expected) & 0xFFFF
                self.stats.dropped_packet_count += gap
        return seq

    def handle_notification(self, _sender, data: bytearray) -> None:
        trigger = parse_swipe_trigger_packet(data)
        if trigger is not None:
            seq, class_id, scores, uptime_ms = trigger
            label = SWIPE_CLASS_LABELS.get(class_id, str(class_id))
            event = SWIPE_EVENT_MAP.get(class_id, "")
            self.stats.packet_count += 1
            self._last_trigger_seq = self._note_seq_gap(seq, self._last_trigger_seq)
            self.stats.trigger_count += 1
            if self._writer is not None:
                self._writer.writerow(
                    ["trigger", seq, class_id, label, event, *scores, uptime_ms]
                )
            # Always log recognized gestures when swipe is on.
            self._emit(format_swipe_trigger_line(seq, class_id, scores, uptime_ms))
            return

        parsed = parse_swipe_event_packet(data)
        if parsed is None:
            return

        seq, class_id, scores, uptime_ms = parsed
        label = SWIPE_CLASS_LABELS.get(class_id, str(class_id))

        self.stats.packet_count += 1
        self._last_infer_seq = self._note_seq_gap(seq, self._last_infer_seq)
        self.stats.event_count += 1

        if self._writer is not None:
            self._writer.writerow(
                ["infer", seq, class_id, label, "", *scores, uptime_ms]
            )

        if self.print_events:
            self._emit(format_swipe_infer_line(seq, class_id, scores, uptime_ms))
