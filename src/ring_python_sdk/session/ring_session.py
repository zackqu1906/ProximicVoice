"""RingSession dataclass: fields + thin helpers; behavior from mixins."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bleak import BleakClient

from ring_python_sdk.audio import AudioProcessor
from ring_python_sdk.ble_test.processor import BleTestProcessor
from ring_python_sdk.button.processor import ButtonProcessor
from ring_python_sdk.core.constants import DEFAULT_IMU_CHIP
from ring_python_sdk.core.data_paths import set_data_dir
from ring_python_sdk.core.device_info import DeviceInfo
from ring_python_sdk.core.health import (
    HealthDataChunk,
    HealthListItem,
    HealthReadEnd,
    HealthRecord,
    HealthStatus,
)
from ring_python_sdk.core.mac_status import MacStatus
from ring_python_sdk.core.mic_recording import (
    MicRecordingDataChunk,
    MicRecordingListItem,
    MicRecordingReadEnd,
    MicRecordingStatus,
)
from ring_python_sdk.core.identity import (
    IdentityError,
    IdentitySignature,
    IdentityStatus,
)
from ring_python_sdk.core.temperature import TemperatureStatus
from ring_python_sdk.core.time_sync import TimeStatus
from ring_python_sdk.core.rates import StreamRateTracker
from ring_python_sdk.imu import ImuProcessor
from ring_python_sdk.imu.calibration import ImuCalibrationStatus
from ring_python_sdk.ppg.calibration import PpgWearCalibrationStatus
from ring_python_sdk.ppg.processor import PpgProcessor
from ring_python_sdk.raise_to_wake.processor import RaiseToWakeProcessor
from ring_python_sdk.session.connection import ConnectionMixin
from ring_python_sdk.session.demux import DemuxMixin
from ring_python_sdk.session.identity import IdentityMixin
from ring_python_sdk.session.sensors import SensorsMixin
from ring_python_sdk.session.status import StatusMixin
from ring_python_sdk.session.types import PrintFlags
from ring_python_sdk.swipe.processor import SwipeProcessor


@dataclass
class RingSession(DemuxMixin, ConnectionMixin, IdentityMixin, SensorsMixin, StatusMixin):
    name_keyword: str
    timeout_s: float
    imu_chip: str = DEFAULT_IMU_CHIP
    imu_plot_window: float = 3.0
    data_root: Path | None = None
    client: BleakClient | None = None
    target_name: str = ""
    target_address: str = ""
    tx_uuid: str = ""
    rx_uuid: str = ""
    session_dir: Path | None = None
    rates: StreamRateTracker = field(default_factory=StreamRateTracker)
    print_flags: PrintFlags = field(default_factory=PrintFlags)
    # Async sensor lines (BLE notify) for TUI Log; also printed for REPL.
    live_logs: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    mic: AudioProcessor | None = None
    imu: ImuProcessor | None = None
    ppg: PpgProcessor | None = None
    swipe: SwipeProcessor | None = None
    button: ButtonProcessor | None = None
    raise_to_wake: RaiseToWakeProcessor | None = None
    ble_test: BleTestProcessor | None = None
    mic_active: bool = False
    mic_recording_status: MicRecordingStatus | None = None
    mic_recording_latest_id: int | None = None
    mic_recording_latest_info: MicRecordingListItem | None = None
    mic_recording_last_data: MicRecordingDataChunk | None = None
    mic_recording_last_read_end: MicRecordingReadEnd | None = None
    _mic_record_read_buf: bytearray = field(default_factory=bytearray, repr=False)
    _mic_record_read_off: int = 0
    _mic_record_status_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    _mic_record_list_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    _mic_record_read_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    imu_active: bool = False
    imu_calibration_status: ImuCalibrationStatus | None = None
    ppg_active: bool = False
    ppg_mode: str = ""
    ppg_send_raw: bool = False
    ppg_calibration_status: PpgWearCalibrationStatus | None = None
    swipe_active: bool = False
    button_active: bool = False
    raise_to_wake_active: bool = False
    raise_to_wake_enabled: bool | None = None
    hid_enabled: bool | None = None
    ble_test_active: bool = False
    imu_plot: Any | None = None
    audio_plot: Any | None = None
    imu_plot_enabled: bool = False
    audio_plot_enabled: bool = False
    _seg: dict[str, int] = field(default_factory=dict)
    saved_paths: list[Path] = field(default_factory=list)
    scanned: list[Any] = field(default_factory=list)
    # Product default: a dropped link stays disconnected until the user
    # explicitly reconnects. Diagnostic callers may still opt in manually.
    auto_reconnect: bool = False
    # Long-running product audio sessions can disable periodic control writes
    # and issue one explicit battery query before MIC ON.  This reduces
    # contention with high-rate microphone notifications on CoreBluetooth.
    battery_poll_enabled: bool = True
    reconnecting: bool = False
    _user_closing: bool = False
    _reconnect_task: asyncio.Task | None = field(default=None, repr=False)
    _battery_task: asyncio.Task | None = field(default=None, repr=False)
    _was_connected: bool = False
    _connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    battery_pct: int | None = None
    battery_mv: int | None = None
    charge_status: int | None = None
    temperature_status: TemperatureStatus | None = None
    temperature_mc: int | None = None
    temperature_c: float | None = None
    _temperature_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    time_status: TimeStatus | None = None
    _time_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    device_info: DeviceInfo | None = None
    mac_status: MacStatus | None = None
    mac_str: str | None = None
    mac_addr_type: int | None = None
    identity_status: IdentityStatus | None = None
    identity_signature: IdentitySignature | None = None
    identity_error: IdentityError | None = None
    _identity_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    shipmode_last_ok: bool | None = None
    shipmode_last_err: int | None = None
    reboot_last_ok: bool | None = None
    reboot_last_err: int | None = None
    health_status: HealthStatus | None = None
    health_sessions: list[HealthListItem] = field(default_factory=list)
    health_last_data: HealthDataChunk | None = None
    health_last_read_end: HealthReadEnd | None = None
    health_records: list[HealthRecord] = field(default_factory=list)
    _health_read_buf: bytearray = field(default_factory=bytearray, repr=False)
    _health_read_off: int = 0
    _health_read_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    _health_list_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )

    def __post_init__(self) -> None:
        if self.data_root is not None:
            set_data_dir(self.data_root)

    def _seg_path(self, key: str, default_name: str) -> Path:
        assert self.session_dir is not None
        n = self._seg.get(key, 0) + 1
        self._seg[key] = n
        if n == 1:
            name = default_name
        else:
            stem = Path(default_name).stem
            suffix = Path(default_name).suffix
            name = f"{stem}_{n}{suffix}"
        return (self.session_dir / name).resolve()
