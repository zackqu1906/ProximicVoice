"""Match the Ringo IMU preprocessing in ai-ring's feat/ringo branch."""

from __future__ import annotations

import math

import numpy as np

from ring_python_sdk.imu.frame import chip_to_host_matrix_float
from ring_python_sdk.imu.processor import ImuSample


class ImuGestureAdapter:
    """Convert SDK raw/delta samples to model-frame m/s² and rad/s.

    The SDK already applies its calibrated chip-to-host rotation. Undo that
    rotation before applying ai-ring's ideal chip mapping and mounting-point
    correction. Token frames are normalized features, not physical IMU data.

    Defaults match ``core/ringo.py:Ringo`` in ai-ring ``feat/ringo``. Gyro bias
    is in the final model coordinates, in rad/s. Use one adapter per stream and
    call :meth:`reset` after a restart or packet gap. ``sample_hz`` must match
    the physical stream; it sets the angular-acceleration derivative interval.
    """

    def __init__(
        self,
        *,
        sample_hz: float = 200.0,
        mount_angle_deg: float = 135.0,
        mount_radius_m: float = 0.01,
        alpha_smoothing: float = 0.8,
        gyro_bias_rad_s: tuple[float, float, float] = (0.0, 0.0, 0.0),
        mount_rotation_enabled: bool = True,
        lever_arm_enabled: bool = True,
        gyro_bias_precomp_enabled: bool = True,
        alpha_smoothing_enabled: bool = True,
    ) -> None:
        if not math.isfinite(sample_hz) or sample_hz <= 0:
            raise ValueError("sample_hz must be finite and positive")
        if not math.isfinite(mount_angle_deg):
            raise ValueError("mount_angle_deg must be finite")
        if not math.isfinite(mount_radius_m) or mount_radius_m < 0:
            raise ValueError("mount_radius_m must be finite and nonnegative")
        if not 0 <= alpha_smoothing < 1:
            raise ValueError("alpha_smoothing must be in [0, 1)")
        self._bias = np.asarray(gyro_bias_rad_s, dtype=np.float64)
        if self._bias.shape != (3,) or not np.isfinite(self._bias).all():
            raise ValueError("gyro_bias_rad_s must contain three finite values")
        self.sample_hz = float(sample_hz)
        self._smoothing = float(alpha_smoothing)
        self._mount_enabled = bool(mount_rotation_enabled)
        self._lever_arm_enabled = bool(lever_arm_enabled)
        self._bias_precomp_enabled = bool(gyro_bias_precomp_enabled)
        self._smoothing_enabled = bool(alpha_smoothing_enabled)

        source_chip_to_ring = np.asarray(
            ((0, 32767, 0), (0, 0, -32767), (-32767, 0, 0)),
            dtype=np.float64,
        ) / 32768.0
        self._host_to_ring = source_chip_to_ring @ np.linalg.inv(
            np.asarray(chip_to_host_matrix_float(), dtype=np.float64)
        )
        angle = math.radians(mount_angle_deg)
        c, s = math.cos(angle), math.sin(angle)
        # R.T changes coordinates from the old mounting point to the new one.
        self._rotation = np.asarray(((1, 0, 0), (0, c, s), (0, -s, c)))
        r_old = np.asarray((0.0, -mount_radius_m, 0.0))
        self._dr = self._rotation.T @ r_old - r_old
        self.reset()

    def reset(self) -> None:
        """Forget the derivative and smoothing history for this stream."""
        self._previous_gyro: np.ndarray | None = None
        self._previous_alpha: np.ndarray | None = None

    def __call__(self, sample: ImuSample) -> np.ndarray:
        if sample.raw is None:
            raise ValueError("gesture IMU adapter requires raw/delta samples; token frames are unsupported")
        values = np.asarray((*sample.accel_ms2, *sample.gyro_dps), dtype=np.float64)
        if values.shape != (6,) or not np.isfinite(values).all():
            raise ValueError("IMU sample must contain six finite physical values")
        accel, gyro = values.reshape(2, 3) @ self._host_to_ring.T
        gyro = np.deg2rad(gyro)
        bias_before = self._mount_enabled and self._bias_precomp_enabled
        if bias_before:
            gyro = gyro - self._rotation.T @ self._bias

        if self._mount_enabled:
            # Ported from core/ringo.py:RingXRotationAugment.__call__.
            alpha = (
                np.zeros(3)
                if self._previous_gyro is None
                else (gyro - self._previous_gyro) * self.sample_hz
            )
            if self._smoothing_enabled and self._smoothing > 0 and self._previous_alpha is not None:
                alpha = self._smoothing * self._previous_alpha + (1 - self._smoothing) * alpha
            if self._lever_arm_enabled:
                accel = accel + np.cross(alpha, self._dr) + np.cross(gyro, np.cross(gyro, self._dr))
            self._previous_gyro = gyro.copy()
            self._previous_alpha = alpha.copy()
            accel = self._rotation @ accel
            gyro = self._rotation @ gyro

        if not bias_before:
            gyro = gyro - self._bias
        return np.concatenate((accel, gyro)).astype(np.float32)
