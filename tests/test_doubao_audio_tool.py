from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import wave

import numpy as np


_SPEC = importlib.util.spec_from_file_location(
    "test_doubao_audio", Path(__file__).parents[1] / "tools/test_doubao_audio.py"
)
assert _SPEC and _SPEC.loader
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


def _write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(np.rint(audio * 32767).astype("<i2").tobytes())


def test_acoustic_metrics_distinguish_quiet_and_clipped_audio(tmp_path):
    t = np.arange(16_000, dtype=np.float32) / 16_000
    quiet = np.sin(2 * np.pi * 440 * t) * 0.01
    clipped = np.sign(np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    quiet_metrics = tool.acoustic_metrics(quiet)
    clipped_metrics = tool.acoustic_metrics(clipped)

    assert quiet_metrics.rms_dbfs < -39
    assert clipped_metrics.rms_dbfs > -0.1
    assert clipped_metrics.clipped_samples_pct > 90


def test_recent_cases_select_newest_empty_and_recognized(tmp_path):
    interactions = tmp_path / "user_a" / "interactions"
    for name, text in (("interaction_001", "旧"), ("interaction_002", ""), ("interaction_003", "新")):
        directory = interactions / name
        _write_wav(directory / "audio.wav", np.zeros(320, dtype=np.float32))
        (directory / "record.json").write_text(
            json.dumps({
                "asr": {
                    "backend": "volcengine",
                    "final_recorded": True,
                    "final_text": text,
                },
                "audio": {"file": "audio.wav"},
            }),
            encoding="utf-8",
        )

    cases = tool.recent_cases(tmp_path, empty_limit=1, recognized_limit=1)

    assert [(case.group, case.interaction_id) for case in cases] == [
        ("recent-empty", "interaction_002"),
        ("recent-recognized", "interaction_003"),
    ]


def test_evenly_spaced_keeps_first_and_last():
    paths = [Path(str(index)) for index in range(10)]
    selected = tool._evenly_spaced(paths, 3)
    assert selected[0] == paths[0]
    assert selected[-1] == paths[-1]
    assert tool._evenly_spaced(paths, 0) == []
    assert tool._evenly_spaced(paths, -1) == paths


def test_excluded_paths_reads_prior_report(tmp_path):
    audio = tmp_path / "sample.wav"
    report = tmp_path / "report.json"
    report.write_text(json.dumps([{"path": str(audio)}]), encoding="utf-8")

    assert tool.excluded_paths([report]) == {audio.resolve()}
