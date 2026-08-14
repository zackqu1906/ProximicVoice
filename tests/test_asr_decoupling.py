from __future__ import annotations

import time

import numpy as np

from proximic_ring.asr.console import StreamingASRConsole
from proximic_ring.asr.factory import (
    ASRBackendSettings,
    available_asr_backends,
    parse_backend_options,
    parse_model_overrides,
)
from proximic_ring.asr.worker import ASRFanout, ASRWorker
from proximic_ring.asr.streaming import StreamingASRUpdate


class EchoBackend:
    def __init__(self, name: str, model: str):
        self.backend_name = name
        self.model_name = model

    def transcribe(self, audio_16k):
        return f"samples={len(audio_16k)}"


def test_builtin_backend_modules_are_discoverable():
    names = available_asr_backends()
    assert "funasr_nano" in names
    assert "sensevoice" in names
    assert "whisper" in names
    assert "http" in names
    assert "volcengine" in names


def test_model_override_single_and_multi():
    assert parse_model_overrides(["m1"], ["sensevoice"]) == {"sensevoice": "m1"}
    assert parse_model_overrides(
        ["sensevoice=s1", "whisper=w1"], ["sensevoice", "whisper"]
    ) == {"sensevoice": "s1", "whisper": "w1"}


def test_backend_options_single_and_prefixed_multi():
    assert parse_backend_options(["beam_size=3"], ["whisper"]) == {
        "whisper": {"beam_size": "3"}
    }
    assert parse_backend_options(
        ["http.url=https://example.test", "whisper.beam_size=2"],
        ["http", "whisper"],
    ) == {
        "http": {"url": "https://example.test"},
        "whisper": {"beam_size": "2"},
    }


def test_worker_normalizes_result_and_fanout_gets_same_audio():
    results = []
    w1 = ASRWorker(EchoBackend("a", "m1"), on_result=results.append)
    w2 = ASRWorker(EchoBackend("b", "m2"), on_result=results.append)
    fanout = ASRFanout([w1, w2])
    x = np.ones(1600, dtype=np.float32)
    fanout.submit(x)
    fanout.close()

    assert len(results) == 2
    assert {r.backend for r in results} == {"a", "b"}
    assert {r.text for r in results} == {"samples=1600"}
    assert all(r.audio_duration_s == 0.1 for r in results)
    assert all(r.latency_s >= 0 for r in results)


def test_comparison_console_throttles_local_partials_but_keeps_cloud_text(capsys):
    console = StreamingASRConsole(
        selected=["streaming_sensevoice", "volcengine"], local_partial_interval_s=60.0
    )
    local = StreamingASRUpdate("streaming_sensevoice", "local", "本地一", False, 0.01, 1.0)
    console(local)
    console(StreamingASRUpdate("streaming_sensevoice", "local", "本地二", False, 0.01, 1.0))
    console(StreamingASRUpdate("volcengine", "seed", "云端", False, 0.01, 1.0))
    console(StreamingASRUpdate("streaming_sensevoice", "local", "本地最终", True, 0.01, 1.0))

    output = capsys.readouterr().out
    assert "本地一" in output
    assert "本地二" not in output
    assert "云端" in output
    assert "本地最终" in output


def test_console_deduplicates_seed_partials_in_all_modes(capsys):
    console = StreamingASRConsole(selected=["volcengine"], local_partial_interval_s=0.0)
    seed = StreamingASRUpdate("volcengine", "seed", "你好", False, 0.01, 1.0)
    console(seed)
    console(seed)
    console(StreamingASRUpdate("volcengine", "seed", "你好。", False, 0.01, 1.0))
    console(StreamingASRUpdate("volcengine", "seed", "你好。", True, 0.01, 1.0))

    output = capsys.readouterr().out
    assert output.count("ASR-PARTIAL[Seed-ASR/seed]") == 2
    assert output.count("ASR-FINAL[Seed-ASR/seed]") == 1


def test_console_labels_and_deduplicates_funasr_nano(capsys):
    console = StreamingASRConsole(selected=["funasr_nano"], local_partial_interval_s=0.0)
    partial = StreamingASRUpdate("funasr_nano", "Fun-ASR-Nano-2512", "你好", False, 0.01, 1.0)
    console(partial)
    console(partial)
    console(StreamingASRUpdate("funasr_nano", "Fun-ASR-Nano-2512", "你好。", True, 0.01, 1.0))

    output = capsys.readouterr().out
    assert output.count("ASR-PARTIAL[Fun-ASR-Nano/Fun-ASR-Nano-2512]") == 1
    assert output.count("ASR-FINAL[Fun-ASR-Nano/Fun-ASR-Nano-2512]") == 1
