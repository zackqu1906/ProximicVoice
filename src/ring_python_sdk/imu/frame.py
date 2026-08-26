"""Ringo chip → BCL-compatible host frame (documented in ring-firmware doc/ble_protocol.md)."""

from __future__ import annotations

from typing import Sequence

# Dual-align 20260825_214923: Ringo643D chip frame fitted directly to the
# BCL6034B4E host frame with the IMUs co-located on opposite PCB faces.
CHIP_TO_HOST_Q15: tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]] = (
    (-1071, 32708, -1672),
    (2532, -1585, -32632),
    (-32652, -1196, -2476),
)
Q15_SCALE = 32768.0


def chip_to_host_matrix_float() -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(value / Q15_SCALE for value in row) for row in CHIP_TO_HOST_Q15
    )


def rotate_vec_float(
    x: float, y: float, z: float, *, matrix: Sequence[Sequence[float]] | None = None
) -> tuple[float, float, float]:
    """Apply chip→host rotation to one float vector (m/s² or rad/s/dps)."""
    m = matrix if matrix is not None else chip_to_host_matrix_float()
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z,
        m[1][0] * x + m[1][1] * y + m[1][2] * z,
        m[2][0] * x + m[2][1] * y + m[2][2] * z,
    )


def _sat_i16(value: int) -> int:
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return value


def rotate_vec_i16(x: int, y: int, z: int) -> tuple[int, int, int]:
    """Q15 rotate + round (matches former firmware app_imu_frame_rotate_vec)."""
    out: list[int] = []
    for row in CHIP_TO_HOST_Q15:
        total = row[0] * x + row[1] * y + row[2] * z
        out.append(_sat_i16((total + 16384) >> 15))
    return out[0], out[1], out[2]


def apply_chip_to_host_raw(
    ax: int, ay: int, az: int, gx: int, gy: int, gz: int
) -> tuple[int, int, int, int, int, int]:
    rax, ray, raz = rotate_vec_i16(ax, ay, az)
    rgx, rgy, rgz = rotate_vec_i16(gx, gy, gz)
    return rax, ray, raz, rgx, rgy, rgz


def apply_chip_to_host_physical(
    ax: float,
    ay: float,
    az: float,
    gx: float,
    gy: float,
    gz: float,
) -> tuple[float, float, float, float, float, float]:
    """Rotate after unit conversion (preferred host path)."""
    rax, ray, raz = rotate_vec_float(ax, ay, az)
    rgx, rgy, rgz = rotate_vec_float(gx, gy, gz)
    return rax, ray, raz, rgx, rgy, rgz
