"""Dedicated BATTERY GET / STATUS packet helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from ring_python_sdk.core.constants import (
    CHARGE_STATUS_LABELS,
    CMD_BATTERY,
    SUBCMD_BATTERY_GET,
    SUBCMD_BATTERY_STATUS,
)


@dataclass(frozen=True)
class BatteryStatus:
    battery_mv: int
    battery_pct: int
    charge_status: int
    raw_sample: int | None = None
    adc_mv: int | None = None


def build_battery_get() -> bytes:
    return bytes([CMD_BATTERY, SUBCMD_BATTERY_GET])


def parse_battery_status(packet: bytes | bytearray) -> BatteryStatus | None:
    # Base fields need at least cmd+sub+mv(+pct/charge); debug tail is optional
    # for older 6-byte firmware replies.
    if len(packet) < 5:
        return None
    if packet[0] != CMD_BATTERY or packet[1] != SUBCMD_BATTERY_STATUS:
        return None
    (battery_mv,) = struct.unpack_from("<H", packet, 2)
    battery_pct = int(packet[4]) if len(packet) > 4 else 0
    if len(packet) > 5:
        charge_status = int(packet[5]) & 0x7F
    else:
        charge_status = 0

    raw_sample: int | None = None
    adc_mv: int | None = None
    if len(packet) >= 10:
        raw_sample, adc_mv = struct.unpack_from("<hh", packet, 6)

    return BatteryStatus(
        battery_mv=battery_mv,
        battery_pct=battery_pct,
        charge_status=charge_status,
        raw_sample=raw_sample,
        adc_mv=adc_mv,
    )


def format_battery(
    status: Any | None = None,
    *,
    battery_pct: int | None = None,
    battery_mv: int | None = None,
    charge_status: int | None = None,
) -> str:
    """Compact battery line for TUI / status, e.g. ``BAT 85% 3700mV charging``."""
    raw_sample = None
    adc_mv = None
    if status is not None:
        battery_pct = status.battery_pct
        battery_mv = status.battery_mv
        charge_status = status.charge_status
        raw_sample = getattr(status, "raw_sample", None)
        adc_mv = getattr(status, "adc_mv", None)
    if battery_pct is None or battery_mv is None:
        return "BAT —"
    charge = CHARGE_STATUS_LABELS.get(
        int(charge_status) if charge_status is not None else -1,
        f"chg={charge_status}",
    )
    line = f"BAT {int(battery_pct)}% {int(battery_mv)}mV {charge}"
    if raw_sample is not None and adc_mv is not None:
        line += f" raw={int(raw_sample)} adc_mv={int(adc_mv)}"
    return line
