from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np


_SPEC = importlib.util.spec_from_file_location(
    "test_deplosive_audio_tool_module",
    Path(__file__).parents[1] / "tools/test_deplosive_audio.py",
)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


def _band_power(audio: np.ndarray, low: float, high: float) -> float:
    return tool._band_power(audio, low, high)


def test_tonal_speech_is_preserved_without_false_plosive_detection():
    time = np.arange(2 * tool.SAMPLE_RATE, dtype=np.float64) / tool.SAMPLE_RATE
    audio = (0.08 * np.sin(2 * np.pi * 440.0 * time)).astype(np.float32)

    processed, stats, envelope = tool.process_deplosive(audio)

    assert processed.shape == audio.shape
    assert processed.dtype == np.float32
    assert stats.detected_regions == 0
    assert np.max(envelope) == 0.0
    assert abs(stats.speech_300_3000hz_change_db) < 0.01


def test_low_frequency_pressure_burst_is_attenuated_but_speech_band_is_preserved():
    sample_rate = tool.SAMPLE_RATE
    time = np.arange(2 * sample_rate, dtype=np.float64) / sample_rate
    audio = 0.035 * np.sin(2 * np.pi * 600.0 * time)
    burst_start = int(0.8 * sample_rate)
    burst_size = int(0.09 * sample_rate)
    burst_time = np.arange(burst_size, dtype=np.float64) / sample_rate
    burst = 0.75 * np.sin(2 * np.pi * 85.0 * burst_time)
    burst *= np.hanning(burst_size)
    audio[burst_start : burst_start + burst_size] += burst
    audio = audio.astype(np.float32)

    processed, stats, envelope = tool.process_deplosive(audio)

    before_low = _band_power(audio, 20.0, 250.0)
    after_low = _band_power(processed, 20.0, 250.0)
    before_speech = _band_power(audio, 300.0, 3000.0)
    after_speech = _band_power(processed, 300.0, 3000.0)
    assert stats.detected_regions >= 1
    assert np.max(envelope) >= 6.0
    assert 10.0 * np.log10(after_low / before_low) < -4.0
    assert abs(10.0 * np.log10(after_speech / before_speech)) < 0.15


def test_asr_assessment_only_claims_potential_improvement_for_empty_to_nonempty():
    assert tool._asr_assessment("", "测试") == "potential_improvement_nonempty"
    assert tool._asr_assessment("测试。", "测试") == "unchanged"
    assert tool._asr_assessment("测试", "") == "potential_regression_empty"
    assert tool._asr_assessment("天气", "天启") == "changed_needs_reference"
