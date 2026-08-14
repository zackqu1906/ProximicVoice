"""Resolve IMU output paths and save numpy arrays."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from ring_python_sdk.core.constants import DEFAULT_IMU_OUTPUT
from ring_python_sdk.core.data_paths import resolve_imu_capture_paths

IMU_SAMPLE_COLUMNS = (
    "seq",
    "packet_seq",
    "uptime_ms",
    "accel_x_ms2",
    "accel_y_ms2",
    "accel_z_ms2",
    "gyro_x_dps",
    "gyro_y_dps",
    "gyro_z_dps",
)


def resolve_imu_output_paths(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    """
    Return (csv_path, npy_path) under data/imu/<session>/.

    - --output ring_imu.csv  -> CSV
    - --output ring_imu.npy   -> NPY only
    - --output ring_imu.csv --imu-npy  -> CSV + ring_imu.npy (same stem)
    - --imu-npy path.npy      -> explicit NPY path (with or without CSV output)
    """
    csv_path, npy_path, _session = resolve_imu_capture_paths(
        args, default_csv=DEFAULT_IMU_OUTPUT
    )
    return csv_path, npy_path


def save_imu_samples(
    path: Path,
    rows: list[list[float]],
    *,
    gyro_fs_dps: int,
    accel_fs_g: int,
    gyro_hz: int,
    accel_hz: int,
    imu_chip: str = "icm42688",
) -> Path:
    """Save (N, 9) float64 samples; use .npz to include metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.asarray(rows, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples.reshape(0, len(IMU_SAMPLE_COLUMNS))

    if path.suffix.lower() == ".npz":
        np.savez_compressed(
            path,
            samples=samples,
            columns=np.asarray(IMU_SAMPLE_COLUMNS),
            gyro_fs_dps=np.int32(gyro_fs_dps),
            accel_fs_g=np.int32(accel_fs_g),
            gyro_hz=np.int32(gyro_hz),
            accel_hz=np.int32(accel_hz),
            imu_chip=np.asarray(imu_chip),
        )
    else:
        np.save(path, samples)

    return path
