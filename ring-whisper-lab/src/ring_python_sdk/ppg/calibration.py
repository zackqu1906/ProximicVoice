"""PPG wear-calibration status parsing and formatting."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_PPG,
    PPG_WEAR_CAL_STATUS_LEN,
    SUBCMD_PPG_WEAR_CALIBRATION_STATUS,
)


_OP_NAMES = {0: "get", 1: "run"}
_RESULT_NAMES = {
    0: "ok",
    1: "busy",
    2: "not_calibrated",
    3: "sensor_error",
    4: "range_error",
    5: "store_error",
    6: "internal_error",
}


@dataclass(frozen=True)
class PpgWearCalibrationStatus:
    operation: int
    result: int
    valid: bool
    wear_offset: int
    ref_offset: int
    wear_base: int
    ref_base: int

    @property
    def operation_name(self) -> str:
        return _OP_NAMES.get(self.operation, str(self.operation))

    @property
    def result_name(self) -> str:
        return _RESULT_NAMES.get(self.result, str(self.result))


def parse_ppg_wear_calibration_status(
    data: bytes | bytearray,
) -> PpgWearCalibrationStatus | None:
    if (
        len(data) < PPG_WEAR_CAL_STATUS_LEN
        or data[0] != CMD_PPG
        or data[1] != SUBCMD_PPG_WEAR_CALIBRATION_STATUS
    ):
        return None

    wear_offset, ref_offset, wear_base, ref_base = struct.unpack_from(
        "<hhhh", data, 6
    )
    return PpgWearCalibrationStatus(
        operation=data[2],
        result=data[3],
        valid=bool(data[4]),
        wear_offset=wear_offset,
        ref_offset=ref_offset,
        wear_base=wear_base,
        ref_base=ref_base,
    )


def format_ppg_wear_calibration_status(status: PpgWearCalibrationStatus) -> str:
    return (
        "wear calibration "
        f"op={status.operation_name} result={status.result_name} "
        f"valid={status.valid} "
        f"offsets=({status.wear_offset},{status.ref_offset}) "
        f"bases=({status.wear_base},{status.ref_base})"
    )
