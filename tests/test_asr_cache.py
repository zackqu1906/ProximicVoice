from types import SimpleNamespace

from proximic_ring.asr import ASRBackendCache
from proximic_ring.asr.factory import ASRBackendSettings
from proximic_ring.app_runtime import RuntimeSettings
from proximic_ring import asr
from proximic_ring.cli import _build_session_controller


def test_backend_cache_identity_includes_all_model_affecting_settings():
    cache = ASRBackendCache()
    backend = object()
    settings = ASRBackendSettings(
        model="example/model",
        device="cpu",
        language="zh",
        options={"repo": "third_party/example", "chunk_ms": "720"},
    )
    cache.put("funasr_nano", "streaming", settings, backend)

    equivalent = ASRBackendSettings(
        model="example/model",
        device="cpu",
        language="zh",
        options={"chunk_ms": "720", "repo": "third_party/example"},
    )
    assert cache.get("funasr-nano", "streaming", equivalent) is backend
    assert (
        cache.get(
            "funasr_nano",
            "streaming",
            ASRBackendSettings(
                model="example/model",
                device="cuda:0",
                language="zh",
                options=equivalent.options,
            ),
        )
        is None
    )


def test_session_controller_reuses_cached_heavy_backend(monkeypatch):
    created = []
    states = []

    class FakeStreamingBackend:
        backend_name = "funasr_nano"
        model_name = "fake-heavy-model"

        def start(self):
            pass

        def feed(self, _audio):
            return None

        def finish(self, _audio):
            return ""

        def abort(self):
            pass

    def create_backend(_name, _settings):
        backend = FakeStreamingBackend()
        created.append(backend)
        return backend

    monkeypatch.setattr(asr, "create_streaming_asr_backend", create_backend)
    args = RuntimeSettings(
        asr_backend="funasr_nano",
        asr_model="example/model",
        funasr_nano_repo=None,
        desktop_output=False,
        push_to_talk=False,
    ).to_namespace()
    detector = SimpleNamespace(config=SimpleNamespace(stage2_delay_s=0.5))
    cache = ASRBackendCache()

    first = _build_session_controller(
        args,
        detector,
        show_streaming_console=False,
        on_state=states.append,
        backend_cache=cache,
    )
    first.close()
    second = _build_session_controller(
        args,
        detector,
        show_streaming_console=False,
        on_state=states.append,
        backend_cache=cache,
    )
    second.close()

    assert len(created) == 1
    assert any("正在复用已加载语音模型" in state for state in states)


def test_session_context_callbacks_are_wired_only_to_volcengine(monkeypatch):
    workers = []
    original_worker = asr.StreamingASRWorker

    class FakeStreamingBackend:
        model_name = "fake-model"

        def __init__(self, name):
            self.backend_name = name

        def start(self):
            return None

        def feed(self, _audio):
            return None

        def finish(self, _audio):
            return ""

        def abort(self):
            return None

    def create_backend(name, _settings):
        return FakeStreamingBackend(name)

    def create_worker(*args, **kwargs):
        worker = original_worker(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(asr, "create_streaming_asr_backend", create_backend)
    monkeypatch.setattr(asr, "StreamingASRWorker", create_worker)
    detector = SimpleNamespace(config=SimpleNamespace(stage2_delay_s=0.5))
    provider = lambda: {"status": "captured", "text": "上文"}
    observer = lambda *_args: None

    for backend_name in ("volcengine", "funasr_nano"):
        args = RuntimeSettings(
            asr_backend=backend_name,
            asr_model="fake-model",
            desktop_output=False,
            push_to_talk=False,
        ).to_namespace()
        controller = _build_session_controller(
            args,
            detector,
            show_streaming_console=False,
            on_state=lambda _message: None,
            asr_context_provider=provider,
            asr_context_observer=observer,
        )
        controller.close()

    assert workers[0].backend.backend_name == "volcengine"
    assert workers[0].context_provider is provider
    assert workers[0].on_context is observer
    assert workers[1].backend.backend_name == "funasr_nano"
    assert workers[1].context_provider is None
    assert workers[1].on_context is None
