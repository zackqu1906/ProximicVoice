"""Session snapshot / print-flag types and encode maps."""

from __future__ import annotations

from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    MIC_ENCODE_ADPCM,
    MIC_ENCODE_OPUS,
    MIC_ENCODE_PCM,
)

MIC_ENCODE = {
    "pcm": MIC_ENCODE_PCM,
    "adpcm": MIC_ENCODE_ADPCM,
    "opus": MIC_ENCODE_OPUS,
}

PPG_MODE = {
    "hrs": 0,
    "hr": 0,
    "h": 0,
    "spo2": 1,
    "s": 1,
    "wear": 2,
    "w": 2,
}

RECONNECT_INTERVAL_S = 2.0


@dataclass
class SensorSnap:
    name: str
    active: bool
    rate: float
    rate_unit: str
    count_label: str
    count: int
    packets: int
    dropped: int
    nominal_rate: float
    extras: str = ""


@dataclass
class SessionSnapshot:
    device_name: str
    device_address: str
    session_dir: str
    connected: bool
    reconnecting: bool
    auto_reconnect: bool
    imu_chip: str
    plot_imu: bool
    plot_audio: bool
    sensors: list[SensorSnap]
    extras: str
    scanned: list[tuple[str, str]]  # (name, address)
    battery_text: str = "BAT —"
    battery_pct: int | None = None
    battery_mv: int | None = None
    charge_status: int | None = None
    info_text: str = "INFO —"
    hw_rev: int | None = None
    fw_version: str | None = None


@dataclass
class PrintFlags:
    mic: bool = False
    imu: bool = False
    ppg: bool = False
    swipe: bool = False
    # Button events always log to TUI; flag kept for status display only.
    button: bool = True
