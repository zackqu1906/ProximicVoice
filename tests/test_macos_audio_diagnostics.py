from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import wave


def _load_diagnostic_module():
    path = Path(__file__).parents[1] / "tools" / "diagnose_macos_audio.py"
    spec = importlib.util.spec_from_file_location("diagnose_macos_audio", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_macos_audio_diagnostic_reports_pcm_quality(tmp_path):
    module = _load_diagnostic_module()
    wav_path = tmp_path / "data" / "session" / "latest" / "ring_audio.wav"
    wav_path.parent.mkdir(parents=True)
    samples = [0, 8192, -8192, 16384, -16384] * 3200
    with wave.open(str(wav_path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    report = module._analyze_wav(wav_path)

    assert report["sample_rate"] == 16_000
    assert report["channels"] == 1
    assert report["sample_width"] == 2
    assert report["duration_s"] == 1.0
    assert -15.0 < report["rms_dbfs"] < -5.0
    assert report["clipped_pct"] == 0.0
    assert module._latest_wav(tmp_path) == wav_path
