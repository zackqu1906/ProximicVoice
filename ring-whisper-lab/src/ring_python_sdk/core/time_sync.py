"""TIME SET / GET / STATUS packet helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_TIME,
    SUBCMD_TIME_GET,
    SUBCMD_TIME_SET,
    SUBCMD_TIME_STATUS,
    TIME_SET_PACKET_LEN,
    TIME_STATUS_PACKET_LEN,
)

MAX_FIRMWARE_UNIX_MS = (1 << 63) - 1


@dataclass(frozen=True)
class TimeStatus:
    synced: bool
    unix_ms: int
    uptime_ms: int


def build_time_set(unix_ms: int) -> bytes:
    if not 0 <= unix_ms <= MAX_FIRMWARE_UNIX_MS:
        raise ValueError(
            f"unix_ms must be in range 0..{MAX_FIRMWARE_UNIX_MS}, got {unix_ms}"
        )
    packet = bytes([CMD_TIME, SUBCMD_TIME_SET]) + struct.pack("<Q", unix_ms)
    if len(packet) != TIME_SET_PACKET_LEN:
        raise RuntimeError(f"TIME SET packet length mismatch: {len(packet)}")
    return packet


def build_time_get() -> bytes:
    return bytes([CMD_TIME, SUBCMD_TIME_GET])


def parse_time_status(packet: bytes | bytearray) -> TimeStatus | None:
    if len(packet) != TIME_STATUS_PACKET_LEN:
        return None
    if packet[0] != CMD_TIME or packet[1] != SUBCMD_TIME_STATUS:
        return None
    synced, unix_ms, uptime_ms = struct.unpack_from("<BQQ", packet, 2)
    return TimeStatus(
        synced=synced != 0,
        unix_ms=unix_ms,
        uptime_ms=uptime_ms,
    )


def format_time_status(status: TimeStatus) -> str:
    return (
        f"time synced={'yes' if status.synced else 'no'} "
        f"unix_ms={status.unix_ms} uptime_ms={status.uptime_ms}"
    )
