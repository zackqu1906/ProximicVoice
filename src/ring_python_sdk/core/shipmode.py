"""SHIPMODE ENTER / RESULT packet helpers (cmd 0x2F)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_SHIPMODE,
    SHIPMODE_RESULT_PACKET_LEN,
    SUBCMD_SHIPMODE_RESULT,
    build_shipmode_enter,
)

__all__ = [
    "ShipmodeResult",
    "build_shipmode_enter",
    "format_shipmode_result",
    "parse_shipmode_result",
]


@dataclass(frozen=True)
class ShipmodeResult:
    ok: bool
    err_code: int


def parse_shipmode_result(packet: bytes | bytearray) -> ShipmodeResult | None:
    if len(packet) < SHIPMODE_RESULT_PACKET_LEN:
        return None
    if packet[0] != CMD_SHIPMODE or packet[1] != SUBCMD_SHIPMODE_RESULT:
        return None
    err_code = struct.unpack_from("<h", packet, 3)[0]
    return ShipmodeResult(ok=bool(packet[2]), err_code=int(err_code))


def format_shipmode_result(status: ShipmodeResult | None) -> str:
    if status is None:
        return "SHIPMODE —"
    if status.ok:
        return "SHIPMODE ok"
    return f"SHIPMODE fail err={status.err_code}"
