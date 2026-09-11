"""Golden Ringo transforms generated from ai-ring feat/ringo's original class."""

from dataclasses import replace
import math
import struct

import numpy as np
import pytest

from ring_python_sdk.core.constants import CMD_IMU, SUBCMD_IMU_PACKET
from ring_python_sdk.gestures.preprocess import ImuGestureAdapter
from ring_python_sdk.imu.processor import ImuProcessor, ImuSample


RAW = (
    (200, -100, 2048, 40, -60, 20),
    (-500, 300, 1850, -90, 100, 60),
    (50, 200, 2000, 200, -140, 75),
)


def samples() -> list[ImuSample]:
    result = []
    processor = ImuProcessor(
        None, None, False, 2000, 16, 200, 200, 3, on_sample=result.append
    )
    packet = struct.pack("<BBHBI", CMD_IMU, SUBCMD_IMU_PACKET, 1, 3, 1000)
    packet += b"".join(struct.pack("<6h", *raw) for raw in RAW)
    processor.handle_notification(0, bytearray(packet))
    processor.close()
    return result


def test_matches_upstream_mount_compensation_and_bias_goldens():
    # Reference: source RingXRotationAugment(135, (0, -.01, 0), .005, .8),
    # fed by source parse_imu_packet's 16 g / 2000 dps ideal Q15 mapping.
    expected = (
        (-.478821738544, 6.257084473883, 7.611340952350, -.063851560565, -.015049957155, .045149871465),
        (1.353861144148, 8.005031364875, 4.454787800009, .106419267608, .112874678662, -.022574935732),
        (1.106938946072, 6.569863496383, 7.022848257854, -.148986974651, -.094062232218, .206936910880),
    )
    biased_expected = (
        (-.478603662076, 6.258875616346, 7.612535274890, -.163851560565, .184950042845, -.254850128535),
        (1.354029570959, 8.007301993706, 4.456989518740, .006419267608, .312874678662, -.322574935732),
        (1.107246611861, 6.569891771047, 7.022982476913, -.248986974651, .105937767782, -.093063089120),
    )
    for bias, golden in (((0, 0, 0), expected), ((.1, -.2, .3), biased_expected)):
        adapter = ImuGestureAdapter(gyro_bias_rad_s=bias)
        actual = np.asarray([adapter(sample) for sample in samples()])
        assert actual.shape == (3, 6) and actual.dtype == np.float32
        np.testing.assert_allclose(actual, golden, rtol=1e-6, atol=1e-7)
        adapter.reset()
        np.testing.assert_allclose(adapter(samples()[0]), golden[0], rtol=1e-6, atol=1e-7)


def test_mount_disabled_preserves_source_axes_and_radians():
    # With mounting correction disabled: chip (x,y,z) -> (y,-z,-x), Q15-scaled.
    adapter = ImuGestureAdapter(mount_rotation_enabled=False)
    value = adapter(samples()[0])
    scale = 32767 / 32768
    expected = np.asarray((
        -100 / 2048 * 9.80665, -2048 / 2048 * 9.80665, -200 / 2048 * 9.80665,
        math.radians(-60 / 16.4), math.radians(-20 / 16.4), math.radians(-40 / 16.4),
    )) * scale
    np.testing.assert_allclose(value, expected, rtol=1e-6, atol=1e-7)


def test_calibration_knobs_and_reset_are_effective():
    first, second, _ = samples()
    slow = ImuGestureAdapter(sample_hz=100, alpha_smoothing=0)
    fast = ImuGestureAdapter(sample_hz=200, alpha_smoothing=0)
    np.testing.assert_array_equal(slow(first), fast(first))
    slow_accel = slow(second)[:3]
    fast_accel = fast(second)[:3]
    fast.reset()
    no_derivative_accel = fast(second)[:3]
    np.testing.assert_allclose(fast_accel - no_derivative_accel, 2 * (slow_accel - no_derivative_accel), atol=1e-6)
    # At zero angle or radius, the mounting offset is zero and adds no acceleration.
    for config in ({"mount_angle_deg": 0}, {"mount_radius_m": 0}, {"lever_arm_enabled": False}):
        adapter = ImuGestureAdapter(**config)
        adapter(first)
        advanced = adapter(second)
        adapter.reset()
        np.testing.assert_array_equal(advanced, adapter(second))
    unsmoothed = ImuGestureAdapter(alpha_smoothing_enabled=False)
    zero_smoothing = ImuGestureAdapter(alpha_smoothing=0)
    post_bias = ImuGestureAdapter(gyro_bias_precomp_enabled=False, gyro_bias_rad_s=(.1, -.2, .3))
    unbiased = ImuGestureAdapter()
    for sample in samples():
        np.testing.assert_array_equal(unsmoothed(sample), zero_smoothing(sample))
        expected = unbiased(sample).astype(np.float64)
        expected[3:] -= (.1, -.2, .3)
        np.testing.assert_allclose(post_bias(sample), expected, rtol=1e-6, atol=1e-7)


def test_rejects_token_nonfinite_and_invalid_configuration():
    sample = samples()[0]
    adapter = ImuGestureAdapter()
    with pytest.raises(ValueError, match="token"):
        adapter(replace(sample, raw=None))
    with pytest.raises(ValueError, match="finite"):
        adapter(replace(sample, gyro_dps=(float("nan"), 0, 0)))
    for config in (
        {"sample_hz": 0}, {"sample_hz": float("inf")},
        {"mount_angle_deg": float("nan")}, {"mount_radius_m": -1},
        {"alpha_smoothing": 1}, {"gyro_bias_rad_s": (0, 0, float("nan"))},
    ):
        with pytest.raises(ValueError):
            ImuGestureAdapter(**config)
