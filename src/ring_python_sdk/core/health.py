"""HEALTH (cmd 0x32) packet helpers: status/data pull and flash-record parse."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_HEALTH,
    HEALTH_DATA_HEADER_LEN,
    HEALTH_ERROR_NONE,
    HEALTH_ERROR_REASON_LABELS,
    HEALTH_IMU_HEADER_LEN,
    HEALTH_LIST_END_PACKET_LEN,
    HEALTH_LIST_ITEM_PACKET_LEN,
    HEALTH_READ_END_PACKET_LEN,
    HEALTH_RAW_HEADER_LEN,
    HEALTH_STATUS_LEGACY_PACKET_LEN,
    HEALTH_STATUS_PACKET_LEN,
    HEALTH_VITALS_LEN,
    IMU_BYTES_PER_FRAME_ACCEL,
    PPG_RAW_BYTES_PER_VALUE,
    PPG_RAW_CH_GREEN,
    PPG_RAW_CH_IR,
    PPG_RAW_CH_RED,
    SUBCMD_HEALTH_DATA,
    SUBCMD_HEALTH_LIST_END,
    SUBCMD_HEALTH_LIST_ITEM,
    SUBCMD_HEALTH_READ_END,
    SUBCMD_HEALTH_REC_IMU,
    SUBCMD_HEALTH_REC_RAW,
    SUBCMD_HEALTH_REC_VITALS,
    SUBCMD_HEALTH_STATUS,
    build_health_list,
    build_health_read,
    build_health_start,
    build_health_status_get,
    build_health_stop,
)

__all__ = [
    "HealthDataChunk",
    "HealthImu",
    "HealthListItem",
    "HealthRaw",
    "HealthReadEnd",
    "HealthRecord",
    "HealthStatus",
    "HealthVitals",
    "build_health_list",
    "build_health_read",
    "build_health_start",
    "build_health_status_get",
    "build_health_stop",
    "format_health_status",
    "parse_health_data",
    "parse_health_list_end",
    "parse_health_list_item",
    "parse_health_read_end",
    "parse_health_records",
    "parse_health_status",
    "raw_channel_count",
]


@dataclass(frozen=True)
class HealthStatus:
    collecting: bool
    bytes: int
    records: int
    err_code: int
    session_id: int = 0
    error_reason: int = HEALTH_ERROR_NONE

    @property
    def error_reason_name(self) -> str:
        return HEALTH_ERROR_REASON_LABELS.get(
            self.error_reason, f"unknown_{self.error_reason}"
        )


@dataclass(frozen=True)
class HealthListItem:
    session_id: int
    bytes: int
    records: int
    uptime_ms: int
    unix_ms: int


@dataclass(frozen=True)
class HealthDataChunk:
    offset: int
    payload: bytes


@dataclass(frozen=True)
class HealthReadEnd:
    next_offset: int
    done: bool


@dataclass(frozen=True)
class HealthVitals:
    kind: str
    seq: int
    hr: int
    spo2: int
    wear: int
    uptime_ms: int
    hrv_rri_ms: int
    hrv_stress: int
    hrv_rri_num: int


@dataclass(frozen=True)
class HealthRaw:
    kind: str
    seq: int
    mode: int
    sample_count: int
    channels_mask: int
    uptime_ms: int
    green: tuple[int, ...]
    red: tuple[int, ...]
    ir: tuple[int, ...]


@dataclass(frozen=True)
class HealthImu:
    kind: str
    frame_count: int
    uptime_ms: int
    frames: tuple[tuple[int, int, int], ...]


HealthRecord = HealthVitals | HealthRaw | HealthImu


def parse_health_status(packet: bytes | bytearray) -> HealthStatus | None:
    if len(packet) < HEALTH_STATUS_LEGACY_PACKET_LEN:
        return None
    if packet[0] != CMD_HEALTH or packet[1] != SUBCMD_HEALTH_STATUS:
        return None
    bytes_, records, err, session_id = struct.unpack_from("<IIhH", packet, 3)
    return HealthStatus(
        collecting=bool(packet[2]),
        bytes=int(bytes_),
        records=int(records),
        err_code=int(err),
        session_id=int(session_id),
        error_reason=(
            int(packet[HEALTH_STATUS_LEGACY_PACKET_LEN])
            if len(packet) >= HEALTH_STATUS_PACKET_LEN
            else HEALTH_ERROR_NONE
        ),
    )


def format_health_status(status: HealthStatus | None) -> str:
    if status is None:
        return "HEALTH —"
    state = "on" if status.collecting else "off"
    err = f" err={status.err_code}" if status.err_code else ""
    reason = (
        f" reason={status.error_reason_name}"
        if status.error_reason != HEALTH_ERROR_NONE
        else ""
    )
    return (
        f"HEALTH {state} id={status.session_id} bytes={status.bytes} "
        f"records={status.records}{err}{reason}"
    )


def parse_health_list_item(packet: bytes | bytearray) -> HealthListItem | None:
    if len(packet) < HEALTH_LIST_ITEM_PACKET_LEN:
        return None
    if packet[0] != CMD_HEALTH or packet[1] != SUBCMD_HEALTH_LIST_ITEM:
        return None
    session_id, nbytes, records, uptime_ms, unix_ms = struct.unpack_from(
        "<HIIIq", packet, 2
    )
    return HealthListItem(
        session_id=int(session_id),
        bytes=int(nbytes),
        records=int(records),
        uptime_ms=int(uptime_ms),
        unix_ms=int(unix_ms),
    )


def parse_health_list_end(packet: bytes | bytearray) -> int | None:
    if len(packet) < HEALTH_LIST_END_PACKET_LEN:
        return None
    if packet[0] != CMD_HEALTH or packet[1] != SUBCMD_HEALTH_LIST_END:
        return None
    return int(packet[2])


def parse_health_data(packet: bytes | bytearray) -> HealthDataChunk | None:
    if len(packet) < HEALTH_DATA_HEADER_LEN:
        return None
    if packet[0] != CMD_HEALTH or packet[1] != SUBCMD_HEALTH_DATA:
        return None
    offset, length = struct.unpack_from("<IH", packet, 2)
    if len(packet) < HEALTH_DATA_HEADER_LEN + length:
        return None
    payload = bytes(packet[HEALTH_DATA_HEADER_LEN : HEALTH_DATA_HEADER_LEN + length])
    return HealthDataChunk(offset=int(offset), payload=payload)


def parse_health_read_end(packet: bytes | bytearray) -> HealthReadEnd | None:
    if len(packet) < HEALTH_READ_END_PACKET_LEN:
        return None
    if packet[0] != CMD_HEALTH or packet[1] != SUBCMD_HEALTH_READ_END:
        return None
    next_offset = struct.unpack_from("<I", packet, 2)[0]
    return HealthReadEnd(next_offset=int(next_offset), done=bool(packet[6]))


def raw_channel_count(channels_mask: int) -> int:
    n = 0
    if channels_mask & PPG_RAW_CH_GREEN:
        n += 1
    if channels_mask & PPG_RAW_CH_RED:
        n += 1
    if channels_mask & PPG_RAW_CH_IR:
        n += 1
    return n


def _parse_vitals(packet: bytes) -> HealthVitals:
    seq, hr, spo2, wear, uptime_ms, hrv_rri_ms, hrv_stress, hrv_rri_num = (
        struct.unpack_from("<HHBHIHBB", packet, 2)
    )
    return HealthVitals(
        kind="vitals",
        seq=int(seq),
        hr=int(hr),
        spo2=int(spo2),
        wear=int(wear),
        uptime_ms=int(uptime_ms),
        hrv_rri_ms=int(hrv_rri_ms),
        hrv_stress=int(hrv_stress),
        hrv_rri_num=int(hrv_rri_num),
    )


def _parse_raw(packet: bytes) -> HealthRaw | None:
    if len(packet) < HEALTH_RAW_HEADER_LEN:
        return None
    seq, mode, _reserved, sample_count, channels_mask, uptime_ms = struct.unpack_from(
        "<HBBBBI", packet, 2
    )
    n_ch = raw_channel_count(channels_mask)
    need = HEALTH_RAW_HEADER_LEN + sample_count * n_ch * PPG_RAW_BYTES_PER_VALUE
    if n_ch == 0 or len(packet) < need:
        return None
    off = HEALTH_RAW_HEADER_LEN
    green: list[int] = []
    red: list[int] = []
    ir: list[int] = []
    for _ in range(sample_count):
        if channels_mask & PPG_RAW_CH_GREEN:
            green.append(struct.unpack_from("<i", packet, off)[0])
            off += 4
        if channels_mask & PPG_RAW_CH_RED:
            red.append(struct.unpack_from("<i", packet, off)[0])
            off += 4
        if channels_mask & PPG_RAW_CH_IR:
            ir.append(struct.unpack_from("<i", packet, off)[0])
            off += 4
    return HealthRaw(
        kind="raw",
        seq=int(seq),
        mode=int(mode),
        sample_count=int(sample_count),
        channels_mask=int(channels_mask),
        uptime_ms=int(uptime_ms),
        green=tuple(green),
        red=tuple(red),
        ir=tuple(ir),
    )


def _parse_imu(packet: bytes) -> HealthImu | None:
    if len(packet) < HEALTH_IMU_HEADER_LEN:
        return None
    frame_count = packet[2]
    uptime_ms = struct.unpack_from("<I", packet, 3)[0]
    need = HEALTH_IMU_HEADER_LEN + frame_count * IMU_BYTES_PER_FRAME_ACCEL
    if frame_count == 0 or len(packet) < need:
        return None
    frames: list[tuple[int, int, int]] = []
    off = HEALTH_IMU_HEADER_LEN
    for _ in range(frame_count):
        ax, ay, az = struct.unpack_from("<hhh", packet, off)
        frames.append((int(ax), int(ay), int(az)))
        off += IMU_BYTES_PER_FRAME_ACCEL
    return HealthImu(
        kind="imu",
        frame_count=int(frame_count),
        uptime_ms=int(uptime_ms),
        frames=tuple(frames),
    )


def parse_health_records(payload: bytes | bytearray) -> list[HealthRecord]:
    """Parse concatenated HEALTH flash payloads (each record starts with 0x32)."""
    data = bytes(payload)
    records: list[HealthRecord] = []
    i = 0
    while i + 2 <= len(data):
        if data[i] != CMD_HEALTH:
            break
        sub = data[i + 1]
        if sub == SUBCMD_HEALTH_REC_VITALS:
            if i + HEALTH_VITALS_LEN > len(data):
                break
            records.append(_parse_vitals(data[i : i + HEALTH_VITALS_LEN]))
            i += HEALTH_VITALS_LEN
        elif sub == SUBCMD_HEALTH_REC_RAW:
            if i + HEALTH_RAW_HEADER_LEN > len(data):
                break
            count = data[i + 6]
            mask = data[i + 7]
            n_ch = raw_channel_count(mask)
            rec_len = HEALTH_RAW_HEADER_LEN + count * n_ch * PPG_RAW_BYTES_PER_VALUE
            if n_ch == 0 or i + rec_len > len(data):
                break
            rec = _parse_raw(data[i : i + rec_len])
            if rec is None:
                break
            records.append(rec)
            i += rec_len
        elif sub == SUBCMD_HEALTH_REC_IMU:
            if i + HEALTH_IMU_HEADER_LEN > len(data):
                break
            frame_count = data[i + 2]
            rec_len = HEALTH_IMU_HEADER_LEN + frame_count * IMU_BYTES_PER_FRAME_ACCEL
            if frame_count == 0 or i + rec_len > len(data):
                break
            rec = _parse_imu(data[i : i + rec_len])
            if rec is None:
                break
            records.append(rec)
            i += rec_len
        else:
            break
    return records
