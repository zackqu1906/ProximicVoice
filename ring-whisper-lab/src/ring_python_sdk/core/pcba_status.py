"""PCBA STATUS packet helpers (factory status / button sample)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.battery_status import format_battery
from ring_python_sdk.core.constants import (
    CMD_PCBA,
    PCBA_FLAG_BUTTON_PRESSED,
    PCBA_STATUS_DEBUG_PACKET_LEN,
    PCBA_STATUS_PACKET_LEN,
    SUBCMD_PCBA_STATUS,
    SUBCMD_PCBA_STATUS_GET,
)

__all__ = [
    "PcbaStatus",
    "build_pcba_status_get",
    "parse_pcba_status",
    "format_battery",
    "format_pcba_status_line",
]


@dataclass(frozen=True)
class PcbaStatus:
    flags: int
    battery_mv: int
    battery_pct: int
    charge_status: int
    fw_major: int
    fw_minor: int
    fw_patch: int
    imu_ok: int = 0
    whoami_75: int | None = None
    whoami_72: int | None = None
    imu_init_ret: int | None = None

    @property
    def button_pressed(self) -> bool:
        return bool(self.flags & PCBA_FLAG_BUTTON_PRESSED)


def build_pcba_status_get() -> bytes:
    return bytes([CMD_PCBA, SUBCMD_PCBA_STATUS_GET])


def parse_pcba_status(packet: bytes | bytearray) -> PcbaStatus | None:
    if len(packet) < PCBA_STATUS_PACKET_LEN:
        return None
    if packet[0] != CMD_PCBA or packet[1] != SUBCMD_PCBA_STATUS:
        return None
    flags, battery_mv = struct.unpack_from("<HH", packet, 2)
    whoami_75: int | None = None
    whoami_72: int | None = None
    imu_init_ret: int | None = None
    if len(packet) >= PCBA_STATUS_DEBUG_PACKET_LEN:
        whoami_75 = int(packet[21])
        whoami_72 = int(packet[22])
        (imu_init_ret,) = struct.unpack_from("<h", packet, 23)
    return PcbaStatus(
        flags=flags,
        battery_mv=battery_mv,
        battery_pct=int(packet[6]),
        charge_status=int(packet[7]),
        fw_major=int(packet[12]),
        fw_minor=int(packet[13]),
        fw_patch=int(packet[14]),
        imu_ok=int(packet[9]),
        whoami_75=whoami_75,
        whoami_72=whoami_72,
        imu_init_ret=imu_init_ret,
    )


def format_pcba_status_line(status: PcbaStatus) -> str:
    """One-line STATUS for Log: battery + button GPIO sample + IMU probe."""
    btn = "pressed" if status.button_pressed else "released"
    line = (
        f"{format_battery(status)} button={btn} flags=0x{status.flags:04x} "
        f"imu_ok={status.imu_ok}"
    )
    if (
        status.whoami_75 is not None
        and status.whoami_72 is not None
        and status.imu_init_ret is not None
    ):
        line += (
            f" whoami75=0x{status.whoami_75:02x}"
            f" whoami72=0x{status.whoami_72:02x}"
            f" imu_ret={status.imu_init_ret}"
        )
    return line
