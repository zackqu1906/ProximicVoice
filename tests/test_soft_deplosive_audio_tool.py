from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np


_SPEC = importlib.util.spec_from_file_location(
    "test_soft_deplosive_audio_tool_module",
    Path(__file__).parents[1] / "tools/test_soft_deplosive_audio.py",
)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


def test_inactive_detector_recombines_to_the_original():
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = (0.08 * np.sin(2.0 * np.pi * 440.0 * time)).astype(np.float32)

    processed, stats, events, envelope = tool.process_soft_deplosive(audio)

    assert stats.detected_events == 0
    assert events == []
    assert np.max(envelope) == 0.0
    assert np.array_equal(processed, audio)


def test_pressure_burst_is_reduced_with_smooth_gain_and_preserved_speech_band():
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    speech = 0.04 * np.sin(2.0 * np.pi * 650.0 * time)
    audio = speech.copy()
    start = int(0.75 * tool.SAMPLE_RATE)
    size = int(0.08 * tool.SAMPLE_RATE)
    burst_time = np.arange(size, dtype=np.float64) / tool.SAMPLE_RATE
    audio[start : start + size] += (
        0.85 * np.sin(2.0 * np.pi * 90.0 * burst_time) * np.hanning(size)
    )

    processed, stats, events, envelope = tool.process_soft_deplosive(
        audio.astype(np.float32)
    )

    assert stats.detected_events >= 1
    assert stats.maximum_attenuation_db >= 4.0
    assert stats.low_20_300hz_change_db < -2.0
    assert abs(stats.speech_300_3000hz_change_db) < 0.1
    assert events
    # An 18 ms cosine attack stays far below a hard frame-wise gain step.
    assert np.max(np.abs(np.diff(envelope))) < 0.08


def test_high_frequency_content_is_unchanged_outside_lowpass_leakage():
    rng = np.random.default_rng(9)
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = 0.03 * np.sin(2.0 * np.pi * 3000.0 * time)
    start = int(0.8 * tool.SAMPLE_RATE)
    size = int(0.07 * tool.SAMPLE_RATE)
    burst_time = np.arange(size, dtype=np.float64) / tool.SAMPLE_RATE
    audio[start : start + size] += (
        0.9 * np.sin(2.0 * np.pi * 85.0 * burst_time) * np.hanning(size)
    )
    audio += rng.normal(0.0, 1e-5, audio.size)

    processed, stats, _events, _envelope = tool.process_soft_deplosive(
        audio.astype(np.float32)
    )

    assert stats.high_3000_7500hz_change_db > -0.01
    assert stats.high_3000_7500hz_change_db < 0.01
    assert processed.shape == audio.shape


def test_stronger_profile_increases_only_low_band_reduction():
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = 0.04 * np.sin(2.0 * np.pi * 700.0 * time)
    start = int(0.7 * tool.SAMPLE_RATE)
    size = int(0.1 * tool.SAMPLE_RATE)
    burst_time = np.arange(size, dtype=np.float64) / tool.SAMPLE_RATE
    audio[start : start + size] += (
        0.9 * np.sin(2.0 * np.pi * 100.0 * burst_time) * np.hanning(size)
    )
    audio = audio.astype(np.float32)

    _gentle, gentle_stats, _events, _envelope = tool.process_soft_deplosive(
        audio, tool.config_for_strength("gentle")
    )
    _strong, strong_stats, _events, _envelope = tool.process_soft_deplosive(
        audio, tool.config_for_strength("stronger")
    )

    assert strong_stats.low_20_300hz_change_db < (
        gentle_stats.low_20_300hz_change_db - 2.0
    )
    assert abs(strong_stats.speech_300_3000hz_change_db) < 0.1
