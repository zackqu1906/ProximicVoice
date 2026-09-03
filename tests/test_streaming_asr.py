from __future__ import annotations

import sys
import threading
import time
import tomllib
import types
from pathlib import Path

import numpy as np

from proximic_ring.asr.backends.streaming_sensevoice import (
    StreamingSenseVoiceASR,
    _macos_cpu_chunk_size,
)
from proximic_ring.asr.backends.funasr_nano import FunASRNanoStreamingASR
from proximic_ring.asr.factory import asr_backend_kind, available_asr_backends
from proximic_ring.asr.streaming import StreamingASRWorker


def test_streaming_sensevoice_uses_less_aggressive_macos_cpu_partials(monkeypatch):
    from proximic_ring.asr.backends import streaming_sensevoice

    monkeypatch.setattr(streaming_sensevoice.sys, "platform", "darwin")
    assert _macos_cpu_chunk_size("cpu") == 8
    assert _macos_cpu_chunk_size("cuda:0") == 4

    monkeypatch.setattr(streaming_sensevoice.sys, "platform", "win32")
    assert _macos_cpu_chunk_size("cpu") == 4


def test_streaming_sensevoice_extra_includes_torchaudio():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = config["project"]["optional-dependencies"]["asr-streaming-sensevoice"]

    assert any(item.startswith("torchaudio") for item in dependencies)


def test_all_extra_contains_streaming_and_volcengine_runtime_dependencies():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = config["project"]["optional-dependencies"]["all"]

    assert "asr-decoder" in dependencies
    assert "online-fbank" in dependencies
    assert any(item.startswith("websocket-client") for item in dependencies)


class FakeStreamingBackend:
    backend_name = "fake_stream"
    model_name = "fake-model"

    def __init__(self):
        self.parts = []
        self.started = 0

    def start(self):
        self.parts = []
        self.started += 1

    def feed(self, audio):
        self.parts.append(np.asarray(audio).copy())
        total = sum(x.size for x in self.parts)
        return f"n={total}"

    def finish(self, final_audio):
        return f"final={len(final_audio)}"


class FailingStreamingBackend:
    backend_name = "failing_stream"
    model_name = "failing-model"

    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")
        raise RuntimeError("connect failed")

    def feed(self, audio):
        self.calls.append("feed")

    def finish(self, final_audio):
        self.calls.append("finish")


def test_streaming_backend_is_discoverable_and_classified():
    assert "funasr_nano" in available_asr_backends()
    assert "streaming_sensevoice" in available_asr_backends()
    assert asr_backend_kind("funasr_nano") == "streaming"
    assert asr_backend_kind("streaming_sensevoice") == "streaming"
    assert asr_backend_kind("sensevoice") == "batch"


def test_streaming_worker_emits_partial_then_final_without_blocking_caller():
    updates = []
    states = []
    backend = FakeStreamingBackend()
    worker = StreamingASRWorker(
        backend,
        on_update=updates.append,
        on_state=states.append,
    )

    worker.start(np.ones(160, dtype=np.float32))
    worker.feed(np.ones(80, dtype=np.float32))
    worker.end(np.ones(200, dtype=np.float32))
    worker.close()

    assert backend.started == 1
    assert [u.is_final for u in updates] == [False, False, True]
    assert [u.text for u in updates] == ["n=160", "n=240", "final=200"]
    assert [u.session_id for u in updates] == [1, 1, 1]
    assert all(u.chunk_ready_time_s is not None for u in updates)
    assert all(u.latency_s >= 0 for u in updates)
    assert any("session=1 ASR模型开始接收音频" in state for state in states)
    assert any("session=1 ASR最终推理开始" in state for state in states)
    assert any("session=1 ASR模型结束" in state for state in states)
    assert any("queue=" in state for state in states)
    assert any("final_inference=" in state for state in states)


def test_streaming_worker_coalesces_stale_feed_backlog():
    entered = threading.Event()
    release = threading.Event()

    class SlowBackend(FakeStreamingBackend):
        def feed(self, audio):
            self.parts.append(np.asarray(audio).copy())
            if len(self.parts) == 1:
                entered.set()
                assert release.wait(2.0)
            return None

    backend = SlowBackend()
    worker = StreamingASRWorker(backend)
    worker.start(np.ones(160, dtype=np.float32))
    assert entered.wait(1.0)
    for _ in range(12):
        worker.feed(np.ones(320, dtype=np.float32))
    release.set()
    worker.end(np.ones(200, dtype=np.float32))
    worker.close()

    assert [part.size for part in backend.parts] == [160, 12 * 320]


def test_streaming_worker_reports_connect_error_once_then_drops_failed_session():
    updates = []
    backend = FailingStreamingBackend()
    worker = StreamingASRWorker(backend, on_update=updates.append, on_error=lambda _: None)

    worker.start(np.ones(160, dtype=np.float32))
    worker.feed(np.ones(80, dtype=np.float32))
    worker.end(np.ones(200, dtype=np.float32))
    worker.close()

    assert backend.calls == ["start"]
    assert len(updates) == 1
    assert updates[0].error == "connect failed"


def test_streaming_worker_aborts_lost_feed_and_reconnects_on_next_start():
    updates = []

    class RecoveringBackend:
        backend_name = "recovering_stream"
        model_name = "recovering-model"

        def __init__(self):
            self.calls = []
            self.session = 0
            self.feed_count = 0

        def start(self):
            self.session += 1
            self.feed_count = 0
            self.calls.append(f"start-{self.session}")

        def feed(self, audio):
            self.feed_count += 1
            self.calls.append(f"feed-{self.session}-{self.feed_count}")
            if self.session == 1 and self.feed_count == 2:
                raise ConnectionError("connection lost")
            return None

        def finish(self, final_audio):
            self.calls.append(f"finish-{self.session}")
            return "recovered"

        def abort(self):
            self.calls.append(f"abort-{self.session}")

    backend = RecoveringBackend()
    worker = StreamingASRWorker(
        backend,
        on_update=updates.append,
        on_error=lambda _: None,
    )

    worker.start(np.ones(160, dtype=np.float32))
    worker.feed(np.ones(80, dtype=np.float32))
    worker.feed(np.ones(80, dtype=np.float32))
    worker.end(np.ones(320, dtype=np.float32))
    worker.start(np.ones(160, dtype=np.float32))
    worker.end(np.ones(320, dtype=np.float32))
    worker.close()

    assert backend.calls == [
        "start-1",
        "feed-1-1",
        "feed-1-2",
        "abort-1",
        "start-2",
        "feed-2-1",
        "finish-2",
    ]
    assert len(updates) == 2
    assert updates[0].error == "connection lost"
    assert updates[1].text == "recovered"
    assert updates[1].is_final


def test_third_party_adapter_uses_external_api_and_redecodes_trimmed_final(monkeypatch):
    calls = []

    class FakeExternal:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))
            self.total = 0

        def reset(self):
            calls.append(("reset", None))
            self.total = 0

        def streaming_inference(self, audio, is_last):
            arr = np.asarray(audio)
            self.total += arr.size
            calls.append(("infer", (arr.copy(), is_last)))
            yield {"timestamps": [], "text": f"samples={self.total}", "rich": {}}

    fake_module = types.ModuleType("streaming_sensevoice")
    fake_module.StreamingSenseVoice = FakeExternal
    monkeypatch.setitem(sys.modules, "streaming_sensevoice", fake_module)

    backend = StreamingSenseVoiceASR(
        model="iic/SenseVoiceSmall",
        device="cpu",
        language="zh",
        final_redecode=True,
    )
    backend.start()
    partial = backend.feed(np.full(160, 0.5, dtype=np.float32))
    final = backend.finish(np.full(320, 0.25, dtype=np.float32))

    assert partial == "samples=160"
    assert final == "samples=320"
    infer_calls = [payload for name, payload in calls if name == "infer"]
    assert infer_calls[0][1] is False
    assert infer_calls[-1][1] is True
    # Adapter follows upstream realtime.py's float -> *32768 convention.
    np.testing.assert_allclose(infer_calls[0][0], np.full(160, 16384.0, dtype=np.float32))


def test_low_latency_sensevoice_redecodes_short_utterance_without_partial(monkeypatch):
    calls = []

    class FakeExternal:
        def __init__(self, **_kwargs):
            return None

        def reset(self):
            calls.append(("reset", None))

        def streaming_inference(self, audio, is_last):
            size = int(np.asarray(audio).size)
            calls.append(("infer", (size, is_last)))
            if is_last and size:
                yield {"text": "短句结果"}

    fake_module = types.ModuleType("streaming_sensevoice")
    fake_module.StreamingSenseVoice = FakeExternal
    monkeypatch.setitem(sys.modules, "streaming_sensevoice", fake_module)

    backend = StreamingSenseVoiceASR(
        model="iic/SenseVoiceSmall",
        device="cpu",
        language="zh",
        final_redecode=False,
    )
    backend.start()
    assert backend.feed(np.ones(80, dtype=np.float32)) is None
    assert backend.finish(np.ones(160, dtype=np.float32)) == "短句结果"
    assert ("infer", (160, True)) in calls


def test_funasr_nano_buffers_chunks_and_redecodes_trimmed_final(monkeypatch, tmp_path):
    (tmp_path / "model.py").write_text("# fake external model", encoding="utf-8")
    calls = []

    class FakeTokenizer:
        @staticmethod
        def encode(text):
            return list(text)

        @staticmethod
        def decode(tokens):
            return "".join(tokens)

    class FakeModel:
        def eval(self):
            calls.append(("eval", None))

        def inference(self, audio, **kwargs):
            samples = int(audio[0].numel())
            calls.append(
                (
                    "infer",
                    (
                        samples,
                        kwargs.get("prev_text"),
                        kwargs.get("language"),
                        kwargs.get("hotwords"),
                    ),
                )
            )
            prefix = str(kwargs.get("prev_text") or "")
            return [[{"text": f"{prefix}n={samples}"}]]

    class FakeClass:
        @staticmethod
        def from_pretrained(**kwargs):
            calls.append(("load", kwargs))
            return FakeModel(), {"tokenizer": FakeTokenizer(), "device": kwargs["device"]}

    monkeypatch.setattr(
        FunASRNanoStreamingASR,
        "_load_external_class",
        lambda self, _model_py: FakeClass,
    )
    backend = FunASRNanoStreamingASR(
        model="fake-model",
        device="cpu",
        language="zh",
        repo_path=tmp_path,
        chunk_ms=10,
        rollback_tokens=0,
        hotwords=["ProxiMic", "豆包"],
        final_redecode=True,
    )

    backend.start()
    assert backend.feed(np.ones(159, dtype=np.float32)) is None
    assert backend.feed(np.ones(1, dtype=np.float32)) == "n=160"
    assert backend.feed(np.ones(160, dtype=np.float32)) == "n=160n=320"
    assert backend.finish(np.ones(240, dtype=np.float32)) == "n=240"

    infer_calls = [payload for name, payload in calls if name == "infer"]
    assert infer_calls == [
        (160, "", "中文", ["ProxiMic", "豆包"]),
        (320, "n=160", "中文", ["ProxiMic", "豆包"]),
        (240, "", "中文", ["ProxiMic", "豆包"]),
    ]
    assert (
        FunASRNanoStreamingASR._merge_context_boundary(
            "今天北京的心", "今天北京的心心情不错"
        )
        == "今天北京的心情不错"
    )
