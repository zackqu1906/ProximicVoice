"""IMU raw LSB to physical units (chip-aware full-scale tables)."""

from __future__ import annotations

from ring_python_sdk.core.constants import DEFAULT_IMU_CHIP, IMU_CHIPS

GRAVITY_MS2 = 9.80665

# Sensitivity (LSB/g) by accel full-scale
_ACCEL_LSB_PER_G_ICM42688: dict[int, float] = {
    16: 2048.0,
    8: 4096.0,
    4: 8192.0,
    2: 16384.0,
}

_ACCEL_LSB_PER_G_ICM45686: dict[int, float] = {
    32: 1024.0,
    16: 2048.0,
    8: 4096.0,
    4: 8192.0,
    2: 16384.0,
}

# Sensitivity (LSB/°/s) by gyro full-scale
_GYRO_LSB_PER_DPS_ICM42688: dict[int, float] = {
    2000: 16.4,
    1000: 32.8,
    500: 65.5,
    250: 131.0,
    125: 262.0,
}

_GYRO_LSB_PER_DPS_ICM45686: dict[int, float] = {
    4000: 8.2,
    2000: 16.4,
    1000: 32.8,
    500: 65.5,
    250: 131.0,
    125: 262.0,
}

ACCEL_LSB_PER_G = _ACCEL_LSB_PER_G_ICM42688
GYRO_LSB_PER_DPS = _GYRO_LSB_PER_DPS_ICM42688


def normalize_imu_chip(chip: str | None) -> str:
    key = (chip or DEFAULT_IMU_CHIP).strip().lower()
    if key not in IMU_CHIPS:
        raise ValueError(f"unsupported imu chip: {chip!r} (expected one of {IMU_CHIPS})")
    return key


def accel_lsb_table(chip: str | None = None) -> dict[int, float]:
    key = normalize_imu_chip(chip)
    if key == "icm45686":
        return _ACCEL_LSB_PER_G_ICM45686
    return _ACCEL_LSB_PER_G_ICM42688


def gyro_lsb_table(chip: str | None = None) -> dict[int, float]:
    key = normalize_imu_chip(chip)
    if key == "icm45686":
        return _GYRO_LSB_PER_DPS_ICM45686
    return _GYRO_LSB_PER_DPS_ICM42688


def accel_raw_to_ms2(
    raw: int, full_scale_g: int, *, chip: str | None = None
) -> float:
    lsb_per_g = accel_lsb_table(chip).get(full_scale_g)
    if lsb_per_g is None:
        raise ValueError(
            f"unsupported accel full-scale for {normalize_imu_chip(chip)}: "
            f"{full_scale_g}g"
        )
    return (raw / lsb_per_g) * GRAVITY_MS2


def gyro_raw_to_dps(
    raw: int, full_scale_dps: int, *, chip: str | None = None
) -> float:
    lsb_per_dps = gyro_lsb_table(chip).get(full_scale_dps)
    if lsb_per_dps is None:
        raise ValueError(
            f"unsupported gyro full-scale for {normalize_imu_chip(chip)}: "
            f"{full_scale_dps} dps"
        )
    return raw / lsb_per_dps


def temperature_raw_to_celsius(raw: int) -> float:
    """ICM-42688-P: Temp (°C) = TEMP_DATA / 132.48 + 25."""
    return (raw / 132.48) + 25.0
