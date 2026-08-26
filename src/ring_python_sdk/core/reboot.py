"""REBOOT ENTER / RESULT packet helpers (cmd 0x31)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_REBOOT,
    REBOOT_RESULT_PACKET_LEN,
    SUBCMD_REBOOT_RESULT,
    build_reboot_enter,
)

__all__ = [
    "RebootResult",
    "build_reboot_enter",
    "format_reboot_result",
    "parse_reboot_result",
]


@dataclass(frozen=True)
class RebootResult:
    ok: bool
    err_code: int


def parse_reboot_result(packet: bytes | bytearray) -> RebootResult | None:
    if len(packet) < REBOOT_RESULT_PACKET_LEN:
        return None
    if packet[0] != CMD_REBOOT or packet[1] != SUBCMD_REBOOT_RESULT:
        return None
    err_code = struct.unpack_from("<h", packet, 3)[0]
    return RebootResult(ok=bool(packet[2]), err_code=int(err_code))


def format_reboot_result(status: RebootResult | None) -> str:
    if status is None:
        return "REBOOT —"
    if status.ok:
        return "REBOOT ok"
    return f"REBOOT fail err={status.err_code}"
