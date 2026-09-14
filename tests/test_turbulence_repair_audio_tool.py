from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np


_SPEC = importlib.util.spec_from_file_location(
    "test_turbulence_repair_audio_tool_module",
    Path(__file__).parents[1] / "tools/test_turbulence_repair_audio.py",
)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


def test_clean_tone_is_exactly_unchanged_when_detector_is_inactive():
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = (0.08 * np.sin(2.0 * np.pi * 440.0 * time)).astype(np.float32)

    processed, stats, events = tool.process_turbulence(audio)

    assert stats.trigger_frames == 0
    assert events == []
    assert np.array_equal(processed, audio)


def test_short_flat_high_frequency_burst_is_reduced_without_low_cut():
    rng = np.random.default_rng(12)
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = 0.04 * np.sin(2.0 * np.pi * 350.0 * time)
    start = int(0.9 * tool.SAMPLE_RATE)
    size = int(0.08 * tool.SAMPLE_RATE)
    noise = rng.normal(0.0, 0.55, size)
    noise *= np.hanning(size)
    audio[start : start + size] += noise

    processed, stats, events = tool.process_turbulence(audio.astype(np.float32))

    assert stats.trigger_frames > 0
    assert events
    assert stats.maximum_attenuation_db >= 5.0
    assert stats.high_4000_7500hz_change_db < -1.0
    assert abs(stats.low_20_300hz_change_db) < 0.1


def test_stationary_harmonic_signal_does_not_trigger_as_turbulence():
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = sum(
        (0.12 / harmonic)
        * np.sin(2.0 * np.pi * 180.0 * harmonic * time)
        for harmonic in range(1, 25)
    ).astype(np.float32)

    processed, stats, _events = tool.process_turbulence(audio)

    assert stats.trigger_frames == 0
    assert np.array_equal(processed, audio)


def test_aggressive_profile_reduces_more_of_same_high_frequency_burst():
    rng = np.random.default_rng(73)
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = 0.035 * np.sin(2.0 * np.pi * 310.0 * time)
    start = int(0.85 * tool.SAMPLE_RATE)
    size = int(0.11 * tool.SAMPLE_RATE)
    noise = rng.normal(0.0, 0.4, size) * np.hanning(size)
    audio[start : start + size] += noise
    audio = audio.astype(np.float32)

    _balanced, balanced_stats, _ = tool.process_turbulence(audio)
    _aggressive, aggressive_stats, _ = tool.process_turbulence(
        audio, tool._config_for_profile("aggressive")
    )

    assert aggressive_stats.processed_duration_ms >= balanced_stats.processed_duration_ms
    assert aggressive_stats.maximum_attenuation_db > balanced_stats.maximum_attenuation_db
    assert (
        aggressive_stats.high_4000_7500hz_change_db
        < balanced_stats.high_4000_7500hz_change_db - 1.0
    )
