from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np


_SPEC = importlib.util.spec_from_file_location(
    "test_blast_repair_audio_tool_module",
    Path(__file__).parents[1] / "tools/test_blast_repair_audio.py",
)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


def test_quiet_tonal_speech_is_not_dynamically_attenuated():
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = (0.08 * np.sin(2.0 * np.pi * 440.0 * time)).astype(np.float32)

    repaired, stats, events = tool.process_blast_repair(audio)

    assert repaired.shape == audio.shape
    assert repaired.dtype == np.float32
    assert stats.detected_events == 0
    assert events == []
    assert abs(stats.speech_300_3000hz_change_db) < 0.02


def test_low_pressure_burst_is_audibly_attenuated_without_losing_speech_band():
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = 0.04 * np.sin(2.0 * np.pi * 650.0 * time)
    start = int(0.75 * tool.SAMPLE_RATE)
    size = int(0.08 * tool.SAMPLE_RATE)
    burst_time = np.arange(size, dtype=np.float64) / tool.SAMPLE_RATE
    burst = 0.85 * np.sin(2.0 * np.pi * 90.0 * burst_time) * np.hanning(size)
    audio[start : start + size] += burst

    repaired, stats, events = tool.process_blast_repair(audio.astype(np.float32))

    assert stats.detected_events >= 1
    assert stats.maximum_low_attenuation_db >= 10.0
    assert stats.low_20_300hz_change_db < -6.0
    assert abs(stats.speech_300_3000hz_change_db) < 0.5
    assert events


def test_clipped_broadband_burst_gets_short_fullband_attenuation():
    rng = np.random.default_rng(7)
    audio = np.zeros(2 * tool.SAMPLE_RATE, dtype=np.float64)
    start = int(0.9 * tool.SAMPLE_RATE)
    size = int(0.025 * tool.SAMPLE_RATE)
    burst = np.clip(rng.normal(0.0, 1.3, size), -1.0, 32767.0 / 32768.0)
    audio[start : start + size] = burst

    repaired, stats, _events = tool.process_blast_repair(audio.astype(np.float32))

    assert stats.overload_trigger_frames >= 1
    assert stats.maximum_broadband_attenuation_db >= 3.5
    assert stats.peak_after_dbfs < stats.peak_before_dbfs - 2.0
    assert stats.clipped_after_pct == 0.0
    assert np.max(np.abs(repaired)) < 0.8


def test_index_parser_accepts_ranges_and_rejects_descending_ranges():
    assert tool._parse_indices("3-7,9") == [3, 4, 5, 6, 7, 9]
    try:
        tool._parse_indices("7-3")
    except ValueError as exc:
        assert "descending" in str(exc)
    else:
        raise AssertionError("descending ranges must be rejected")
