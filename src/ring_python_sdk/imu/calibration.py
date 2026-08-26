"""IMU automatic zero-drift calibration status parsing."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_IMU,
    IMU_CALIBRATION_FORMAT_VERSION,
    IMU_CALIBRATION_STATUS_LEN,
    IMU_CAL_FLAG_CORRECTION_ACTIVE,
    IMU_CAL_FLAG_OWNS_IMU,
    IMU_CAL_FLAG_PERSISTED,
    SUBCMD_IMU_CALIBRATION_STATUS,
)

_OP_NAMES = {0: "get", 1: "run", 2: "auto"}
_RESULT_NAMES = {
    0: "ok",
    1: "busy",
    2: "not_calibrated",
    3: "unsupported_config",
    4: "sensor_error",
    5: "store_error",
    6: "internal_error",
}
_STATE_NAMES = {0: "uncalibrated", 1: "calibrating", 2: "calibrated"}


@dataclass(frozen=True)
class ImuCalibrationStatus:
    operation: int
    result: int
    state: int
    flags: int
    imu_model: int
    format_version: int
    stable_count: int
    gyro_full_scale_dps: int
    bias: tuple[int, int, int]

    @property
    def operation_name(self) -> str:
        return _OP_NAMES.get(self.operation, str(self.operation))

    @property
    def result_name(self) -> str:
        return _RESULT_NAMES.get(self.result, str(self.result))

    @property
    def state_name(self) -> str:
        return _STATE_NAMES.get(self.state, str(self.state))

    @property
    def persisted(self) -> bool:
        return bool(self.flags & IMU_CAL_FLAG_PERSISTED)

    @property
    def correction_active(self) -> bool:
        return bool(self.flags & IMU_CAL_FLAG_CORRECTION_ACTIVE)

    @property
    def owns_imu(self) -> bool:
        return bool(self.flags & IMU_CAL_FLAG_OWNS_IMU)


def parse_imu_calibration_status(
    data: bytes | bytearray,
) -> ImuCalibrationStatus | None:
    if (
        len(data) < IMU_CALIBRATION_STATUS_LEN
        or data[0] != CMD_IMU
        or data[1] != SUBCMD_IMU_CALIBRATION_STATUS
        or data[7] != IMU_CALIBRATION_FORMAT_VERSION
    ):
        return None

    stable_count, gyro_fs, bx, by, bz = struct.unpack_from("<HHhhh", data, 8)
    return ImuCalibrationStatus(
        operation=data[2],
        result=data[3],
        state=data[4],
        flags=data[5],
        imu_model=data[6],
        format_version=data[7],
        stable_count=stable_count,
        gyro_full_scale_dps=gyro_fs,
        bias=(bx, by, bz),
    )


def format_imu_calibration_status(status: ImuCalibrationStatus) -> str:
    bx, by, bz = status.bias
    return (
        "imu calibration "
        f"op={status.operation_name} result={status.result_name} "
        f"state={status.state_name} stable={status.stable_count}/400 "
        f"persisted={status.persisted} active={status.correction_active} "
        f"imu={status.imu_model} fs={status.gyro_full_scale_dps}dps "
        f"bias=({bx},{by},{bz})"
    )
