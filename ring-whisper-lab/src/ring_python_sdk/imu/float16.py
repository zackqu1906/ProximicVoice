from __future__ import annotations

import struct


def float16_decode_le(data: bytes, offset: int = 0) -> float:
    """Decode IEEE754 binary16 little-endian to Python float."""
    half = data[offset] | (data[offset + 1] << 8)
    sign = -1.0 if (half & 0x8000) else 1.0
    exponent = (half >> 10) & 0x1F
    mantissa = half & 0x03FF

    if exponent == 0:
        if mantissa == 0:
            return 0.0 * sign
        return sign * (mantissa / 1024.0) * (2.0**-14)

    if exponent == 0x1F:
        if mantissa == 0:
            return sign * float("inf")
        return float("nan")

    return sign * (1.0 + mantissa / 1024.0) * (2.0 ** (exponent - 15))


def decode_imu_token_frames(
    payload: bytes, frame_count: int, *, expect_version: int = 0x01
) -> list[tuple[float, float, float, float, float, float]]:
    """Decode token payload: version u8 + frame_count × 6×float16 LE."""
    expected_len = 1 + frame_count * 12
    if frame_count <= 0 or len(payload) < expected_len:
        raise ValueError("invalid IMU token payload shape")
    if payload[0] != expect_version:
        raise ValueError(f"unsupported IMU token version={payload[0]}")

    frames: list[tuple[float, float, float, float, float, float]] = []
    offset = 1
    for _ in range(frame_count):
        values = tuple(
            float16_decode_le(payload, offset + channel * 2) for channel in range(6)
        )
        frames.append(values)  # type: ignore[arg-type]
        offset += 12
    return frames
