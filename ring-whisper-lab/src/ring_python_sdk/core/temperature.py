"""GXT310W0 TEMPERATURE GET / STATUS packet helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_TEMPERATURE,
    SUBCMD_TEMPERATURE_GET,
    SUBCMD_TEMPERATURE_STATUS,
    TEMPERATURE_STATUS_MAGIC,
    TEMPERATURE_STATUS_PACKET_LEN,
)


@dataclass(frozen=True)
class TemperatureStatus:
    temperature_mc: int
    error_code: int

    @property
    def ok(self) -> bool:
        return self.error_code == 0

    @property
    def temperature_c(self) -> float:
        return self.temperature_mc / 1000.0


def build_temperature_get() -> bytes:
    return bytes([CMD_TEMPERATURE, SUBCMD_TEMPERATURE_GET])


def parse_temperature_status(
    packet: bytes | bytearray,
) -> TemperatureStatus | None:
    if len(packet) < TEMPERATURE_STATUS_PACKET_LEN:
        return None
    if (
        packet[0] != CMD_TEMPERATURE
        or packet[1] != SUBCMD_TEMPERATURE_STATUS
        or packet[8] != TEMPERATURE_STATUS_MAGIC
    ):
        return None
    temperature_mc, error_code = struct.unpack_from("<ih", packet, 2)
    return TemperatureStatus(
        temperature_mc=temperature_mc,
        error_code=error_code,
    )


def format_temperature(status: TemperatureStatus | None) -> str:
    if status is None:
        return "TEMP —"
    if not status.ok:
        return f"TEMP read failed: {status.error_code}"
    return f"TEMP {status.temperature_c:.3f} C"
