from __future__ import annotations

import csv
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ring_python_sdk.core.seq_tracker import SeqTracker
from ring_python_sdk.core.constants import (
    CMD_IMU,
    IMU_BYTES_PER_FRAME,
    IMU_BYTES_PER_FRAME_ACCEL,
    IMU_DELTA_FIXED_BYTES,
    IMU_DELTA_FIRST_FRAME_BYTES,
    IMU_PACKET_HEADER_LEN,
    IMU_TOKEN_VERSION,
    SUBCMD_IMU_PACKET,
    SUBCMD_IMU_PACKET_DELTA,
    SUBCMD_IMU_PACKET_TOKEN,
    imu_packet_len,
    imu_token_packet_len,
)
from ring_python_sdk.imu.float16 import decode_imu_token_frames
from ring_python_sdk.imu.frame import apply_chip_to_host_physical
from ring_python_sdk.imu.io import IMU_SAMPLE_COLUMNS, save_imu_samples
from ring_python_sdk.core.constants import DEFAULT_IMU_CHIP
from ring_python_sdk.imu.units import (
    accel_raw_to_ms2,
    gyro_raw_to_dps,
    normalize_imu_chip,
)

_UPTIME_U32_MODULUS = float(1 << 32)
_IMU_AXIS_COUNT = 6


@dataclass(frozen=True)
class ImuSample:
    """One decoded IMU frame for real-time callbacks."""

    sample_index: int
    packet_seq: int
    uptime_ms: float
    accel_ms2: tuple[float, float, float]
    gyro_dps: tuple[float, float, float]
    raw: tuple[int, int, int, int, int, int] | None = None


def decode_imu_delta_payload(
    payload: bytes, frame_count: int
) -> list[tuple[int, int, int, int, int, int]]:
    """Decode one lossless first-order Delta + bit-packed IMU payload."""
    if frame_count <= 0 or len(payload) < IMU_DELTA_FIXED_BYTES:
        raise ValueError("invalid IMU Delta payload shape")

    first_frame = struct.unpack_from("<6h", payload, 0)
    widths = [
        ((payload[IMU_DELTA_FIRST_FRAME_BYTES + axis // 2] >> ((axis % 2) * 4))
         & 0x0F)
        + 1
        for axis in range(_IMU_AXIS_COUNT)
    ]
    packed_bit_count = (frame_count - 1) * sum(widths)
    expected_len = IMU_DELTA_FIXED_BYTES + (packed_bit_count + 7) // 8
    if len(payload) != expected_len:
        raise ValueError(
            f"invalid IMU Delta payload length={len(payload)}, expected={expected_len}"
        )

    frames = [first_frame]
    bit_offset = 0
    packed = payload[IMU_DELTA_FIXED_BYTES:]
    for _ in range(1, frame_count):
        previous = frames[-1]
        current: list[int] = []
        for axis, width in enumerate(widths):
            encoded = 0
            for bit in range(width):
                absolute_bit = bit_offset + bit
                encoded |= (
                    (packed[absolute_bit // 8] >> (absolute_bit % 8)) & 1
                ) << bit
            bit_offset += width

            if encoded & (1 << (width - 1)):
                delta = encoded - (1 << width)
            else:
                delta = encoded
            restored = ((previous[axis] + delta + 32768) % 65536) - 32768
            current.append(restored)
        frames.append(tuple(current))

    return frames


def imu_frame_uptime_ms(
    last_frame_uptime_ms: int,
    frame_count: int,
    frame_idx: int,
    sample_hz: int,
) -> float:
    """Backfill one frame timestamp from the packet's final-frame uptime."""
    if frame_count <= 0 or frame_idx < 0 or frame_idx >= frame_count or sample_hz <= 0:
        return float(last_frame_uptime_ms)

    remaining_frames = frame_count - 1 - frame_idx
    offset_ms = remaining_frames * 1000.0 / float(sample_hz)
    return (float(last_frame_uptime_ms) - offset_ms) % _UPTIME_U32_MODULUS


def _format_uptime_ms(uptime_ms: float) -> str:
    rounded = round(uptime_ms)
    if abs(uptime_ms - rounded) < 1e-9:
        return str(int(rounded))
    return f"{uptime_ms:.6f}".rstrip("0").rstrip(".")


@dataclass
class ImuStats:
    packet_count: int = 0
    sample_count: int = 0
    dropped_packet_count: int = 0
    invalid_packet_count: int = 0
    estimated_lost_samples: int = 0
    raw_packet_count: int = 0
    delta_packet_count: int = 0
    token_packet_count: int = 0
    wire_byte_count: int = 0


class ImuProcessor:
    def __init__(
        self,
        csv_path: Path | None,
        npy_path: Path | None,
        print_samples: bool,
        gyro_fs_dps: int,
        accel_fs_g: int,
        gyro_hz: int,
        accel_hz: int,
        frames_per_packet: int,
        live_plot: Any | None = None,
        imu_chip: str = DEFAULT_IMU_CHIP,
        encode_mode: str = "raw",
        lp: bool = False,
        on_sample: Callable[[ImuSample], None] | None = None,
    ) -> None:
        self.csv_path = csv_path
        self.npy_path = npy_path
        self.print_samples = print_samples
        self.gyro_fs_dps = gyro_fs_dps
        self.accel_fs_g = accel_fs_g
        self.gyro_hz = gyro_hz
        self.accel_hz = accel_hz
        self.frames_per_packet = frames_per_packet
        self.imu_chip = normalize_imu_chip(imu_chip)
        self.encode_mode = encode_mode
        self.lp = bool(lp)
        self._live_plot = live_plot
        self.on_sample = on_sample
        self.stats = ImuStats()
        self._packet_seq = SeqTracker(bits=16)
        self._sample_index = 0
        self._csv_file = None
        self._csv_writer = None
        self._npy_rows: list[list[float]] = []

        if csv_path is not None:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            self._csv_file = csv_path.open("w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._csv_file)
            self._csv_writer.writerow(list(IMU_SAMPLE_COLUMNS))

        if npy_path is not None:
            npy_path.parent.mkdir(parents=True, exist_ok=True)

    def set_full_scale(self, gyro_fs_dps: int, accel_fs_g: int) -> None:
        self.gyro_fs_dps = gyro_fs_dps
        self.accel_fs_g = accel_fs_g

    def close(self) -> None:
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

        if self.npy_path is not None and self._npy_rows:
            save_imu_samples(
                self.npy_path,
                self._npy_rows,
                gyro_fs_dps=self.gyro_fs_dps,
                accel_fs_g=self.accel_fs_g,
                gyro_hz=self.gyro_hz,
                accel_hz=self.accel_hz,
                imu_chip=self.imu_chip,
            )

    def _convert_sample(
        self,
        accel_x: int,
        accel_y: int,
        accel_z: int,
        gyro_x: int,
        gyro_y: int,
        gyro_z: int,
    ) -> tuple[float, float, float, float, float, float]:
        return (
            accel_raw_to_ms2(accel_x, self.accel_fs_g, chip=self.imu_chip),
            accel_raw_to_ms2(accel_y, self.accel_fs_g, chip=self.imu_chip),
            accel_raw_to_ms2(accel_z, self.accel_fs_g, chip=self.imu_chip),
            gyro_raw_to_dps(gyro_x, self.gyro_fs_dps, chip=self.imu_chip),
            gyro_raw_to_dps(gyro_y, self.gyro_fs_dps, chip=self.imu_chip),
            gyro_raw_to_dps(gyro_z, self.gyro_fs_dps, chip=self.imu_chip),
        )

    def _append_row(
        self,
        seq: int,
        packet_seq: int,
        uptime_ms: float,
        ax: float,
        ay: float,
        az: float,
        gx: float,
        gy: float,
        gz: float,
    ) -> None:
        row = [float(seq), float(packet_seq), float(uptime_ms), ax, ay, az, gx, gy, gz]
        if self._csv_writer is not None:
            self._csv_writer.writerow(
                [
                    seq,
                    packet_seq,
                    _format_uptime_ms(uptime_ms),
                    f"{ax:.6f}",
                    f"{ay:.6f}",
                    f"{az:.6f}",
                    f"{gx:.6f}",
                    f"{gy:.6f}",
                    f"{gz:.6f}",
                ]
            )
        if self.npy_path is not None:
            self._npy_rows.append(row)

    def handle_notification(self, _: int, data: bytearray) -> None:
        self.stats.packet_count += 1
        packet = bytes(data)
        self.stats.wire_byte_count += len(packet)

        if len(packet) < IMU_PACKET_HEADER_LEN:
            self.stats.dropped_packet_count += 1
            return
        if packet[0] != CMD_IMU or packet[1] not in (
            SUBCMD_IMU_PACKET,
            SUBCMD_IMU_PACKET_DELTA,
            SUBCMD_IMU_PACKET_TOKEN,
        ):
            self.stats.invalid_packet_count += 1
            return

        packet_seq = packet[2] | (packet[3] << 8)
        frame_count = packet[4]
        last_frame_uptime_ms = struct.unpack_from("<I", packet, 5)[0]
        if frame_count == 0:
            self.stats.dropped_packet_count += 1
            return

        is_token = packet[1] == SUBCMD_IMU_PACKET_TOKEN
        if packet[1] == SUBCMD_IMU_PACKET:
            bytes_per_frame = (
                IMU_BYTES_PER_FRAME_ACCEL if self.lp else IMU_BYTES_PER_FRAME
            )
            expected_len = imu_packet_len(frame_count, lp=self.lp)
            if len(packet) < expected_len:
                self.stats.dropped_packet_count += 1
                return
            if self.lp:
                frames = [
                    struct.unpack_from(
                        "<3h",
                        packet,
                        IMU_PACKET_HEADER_LEN + frame_idx * bytes_per_frame,
                    )
                    + (0, 0, 0)
                    for frame_idx in range(frame_count)
                ]
            else:
                frames = [
                    struct.unpack_from(
                        "<6h",
                        packet,
                        IMU_PACKET_HEADER_LEN + frame_idx * bytes_per_frame,
                    )
                    for frame_idx in range(frame_count)
                ]
            self.stats.raw_packet_count += 1
        elif packet[1] == SUBCMD_IMU_PACKET_DELTA:
            try:
                frames = decode_imu_delta_payload(
                    packet[IMU_PACKET_HEADER_LEN:], frame_count
                )
            except ValueError:
                self.stats.dropped_packet_count += 1
                return
            self.stats.delta_packet_count += 1
        else:
            expected_len = imu_token_packet_len(frame_count)
            if len(packet) < expected_len:
                self.stats.dropped_packet_count += 1
                return
            try:
                frames = decode_imu_token_frames(
                    packet[IMU_PACKET_HEADER_LEN:],
                    frame_count,
                    expect_version=IMU_TOKEN_VERSION,
                )
            except ValueError:
                self.stats.dropped_packet_count += 1
                return
            self.stats.token_packet_count += 1

        prev_missing = self._packet_seq.stats.missing_count
        self._packet_seq.observe(packet_seq)
        delta_missing = self._packet_seq.stats.missing_count - prev_missing
        if delta_missing > 0:
            self.stats.estimated_lost_samples += delta_missing * self.frames_per_packet

        sample_hz = max(self.gyro_hz, self.accel_hz, 1)
        for frame_idx, frame in enumerate(frames):
            frame_uptime_ms = imu_frame_uptime_ms(
                last_frame_uptime_ms,
                frame_count,
                frame_idx,
                sample_hz,
            )

            self._sample_index += 1
            self.stats.sample_count += 1

            raw: tuple[int, int, int, int, int, int] | None
            if is_token:
                ax, ay, az, gx, gy, gz = frame
                raw = None
            else:
                accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z = frame
                # Wire frame is chip LSB; keep raw as chip, rotate after unit convert.
                raw = (accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z)
                ax, ay, az, gx, gy, gz = self._convert_sample(
                    accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z
                )
                ax, ay, az, gx, gy, gz = apply_chip_to_host_physical(
                    ax, ay, az, gx, gy, gz
                )

            if self.on_sample is not None:
                self.on_sample(
                    ImuSample(
                        sample_index=self._sample_index,
                        packet_seq=packet_seq,
                        uptime_ms=frame_uptime_ms,
                        accel_ms2=(ax, ay, az),
                        gyro_dps=(gx, gy, gz),
                        raw=raw,
                    )
                )

            if self.print_samples:
                if is_token:
                    print(
                        f"imu token pkt_seq={packet_seq} "
                        f"uptime_ms={_format_uptime_ms(frame_uptime_ms)} "
                        f"frame={frame_idx} "
                        f"sample={self._sample_index} "
                        f"token=[{ax:.4f},{ay:.4f},{az:.4f},{gx:.4f},{gy:.4f},{gz:.4f}]"
                    )
                else:
                    print(
                        f"imu pkt_seq={packet_seq} "
                        f"uptime_ms={_format_uptime_ms(frame_uptime_ms)} "
                        f"frame={frame_idx} "
                        f"sample={self._sample_index} "
                        f"accel_ms2=[{ax:.3f},{ay:.3f},{az:.3f}] "
                        f"gyro_dps=[{gx:.3f},{gy:.3f},{gz:.3f}]"
                    )

            if self._live_plot is not None:
                self._live_plot.add_sample(
                    ax,
                    ay,
                    az,
                    gx,
                    gy,
                    gz,
                    t=(self._sample_index - 1) / float(sample_hz),
                )

            self._append_row(
                self._sample_index,
                packet_seq,
                frame_uptime_ms,
                ax,
                ay,
                az,
                gx,
                gy,
                gz,
            )
