import os
from pathlib import Path
import threading

import pytest

from proximic_ring import app_runtime
from proximic_ring.app_runtime import (
    RecognitionRuntime,
    RuntimeSettings,
    SilentTranscriptOverlay,
    normalize_funasr_nano_hotwords,
)
from proximic_ring.events import Stage2Event


def test_runtime_defaults_only_enable_windows_desktop_features():
    args = RuntimeSettings().to_namespace()

    assert args.encoding == "pcm"
    expected = os.name == "nt"
    assert args.desktop_output is expected
    assert args.push_to_talk is expected


def test_runtime_settings_preserve_working_detector_and_asr_values():
    settings = RuntimeSettings(
        detector_model=Path("runs/model.model"),
        stage1_threshold=0.005,
        asr_backend="streaming_sensevoice",
        asr_model="iic/SenseVoiceSmall",
        streaming_sensevoice_repo=Path("../streaming-sensevoice"),
        desktop_output=True,
        push_to_talk=True,
    )
    args = settings.to_namespace()

    assert args.stage1_threshold == 0.005
    assert args.asr == ["streaming_sensevoice"]
    assert args.asr_model == ["streaming_sensevoice=iic/SenseVoiceSmall"]
    assert args.streaming_sensevoice_repo == Path("../streaming-sensevoice")
    assert args.desktop_output_backend == "streaming_sensevoice"
    assert args.push_to_talk is True


def test_qml_runtime_overlay_adapter_is_deliberately_silent():
    overlay = SilentTranscriptOverlay()
    overlay.show_partial("partial")
    overlay.show_final("final")
    overlay.show_error("error")
    overlay.close()


def test_non_local_backend_does_not_receive_streaming_sensevoice_repo():
    args = RuntimeSettings(
        asr_backend="volcengine",
        asr_model="seedasr-streaming",
        streaming_sensevoice_repo=Path("../streaming-sensevoice"),
    ).to_namespace()

    assert args.streaming_sensevoice_repo is None


def test_funasr_backend_receives_its_repo_and_can_auto_select_local_model():
    repo = Path("../Fun-ASR-main")
    args = RuntimeSettings(
        asr_backend="funasr_nano",
        asr_model="",
        streaming_sensevoice_repo=Path("../streaming-sensevoice"),
        funasr_nano_repo=repo,
        funasr_nano_hotwords=" ProxiMic，豆包\n瑞幸,豆包；张三 ",
    ).to_namespace()

    assert args.asr == ["funasr_nano"]
    assert args.asr_model is None
    assert args.funasr_nano_repo == repo
    assert args.streaming_sensevoice_repo is None
    assert args.asr_option == [
        "funasr_nano.hotwords=ProxiMic,豆包,瑞幸,张三"
    ]


def test_nano_hotwords_normalize_ui_separators_and_duplicates():
    assert normalize_funasr_nano_hotwords(
        " ProxiMic，豆包\n瑞幸,豆包；proximic "
    ) == ("ProxiMic", "豆包", "瑞幸")

    args = RuntimeSettings(
        asr_backend="streaming_sensevoice",
        funasr_nano_hotwords="豆包",
    ).to_namespace()
    assert args.asr_option is None


def test_ui_runtime_connects_and_validates_audio_before_loading_models(monkeypatch):
    events = []

    class FakeSource:
        error = None

        def __init__(self, **_kwargs):
            events.append("source-created")

        def connect(self):
            events.append("device-connected")

        def start_stream(self, *, buffer_audio=True):
            assert buffer_audio is False
            events.append("audio-validated")

        def pause_stream(self):
            events.append("audio-paused")

        def begin_buffering(self):
            events.append("audio-buffering")

        def read(self, _frames):
            return None

        def close(self):
            events.append("source-closed")

    class FakeDetector:
        def reset(self):
            return None

        def feed(self, _block):
            return []

    class FakeController:
        def close(self):
            events.append("controller-closed")

    def build_detector(_args):
        events.append("detector-loaded")
        return FakeDetector()

    def build_controller(*_args, **_kwargs):
        events.append("asr-loaded")
        return FakeController()

    monkeypatch.setattr(app_runtime, "RingAudioSource", FakeSource)
    monkeypatch.setattr(app_runtime, "_build_detector", build_detector)
    monkeypatch.setattr(app_runtime, "_build_session_controller", build_controller)

    RecognitionRuntime(RuntimeSettings(ring_name="Selected Ring")).run(
        threading.Event(),
        threading.Event(),
        on_update=lambda _update: None,
        on_state=lambda message: events.append(message),
        on_connected=lambda: events.append("ui-connected"),
        on_disconnected=lambda: events.append("ui-disconnected"),
        on_started=lambda: events.append("runtime-ready"),
    )

    assert events.index("device-connected") < events.index("audio-validated")
    assert events.index("audio-validated") < events.index("ui-connected")
    assert events.index("ui-connected") < events.index("detector-loaded")
    assert "audio-paused" not in events
    assert events.index("detector-loaded") < events.index("asr-loaded")
    assert events.index("asr-loaded") < events.index("audio-buffering")
    assert events.index("audio-buffering") < events.index("runtime-ready")
    assert events.index("runtime-ready") < events.index("ui-disconnected")


def test_ui_runtime_reports_stage2_decisions_to_the_log(monkeypatch):
    states = []

    class FakeSource:
        error = None

        def __init__(self, **_kwargs):
            self.read_count = 0

        def connect(self):
            return None

        def start_stream(self, *, buffer_audio=True):
            return None

        def pause_stream(self):
            return None

        def begin_buffering(self):
            return None

        def read(self, _frames):
            self.read_count += 1
            return [0.0] if self.read_count == 1 else None

        def close(self):
            return None

    stage2_event = Stage2Event(
        sample_index=16000,
        time_s=1.0,
        window_start_s=0.0,
        window_end_s=1.0,
        score=0.9,
        logits=(0.7, -0.2),
        activated=True,
    )

    class FakeDetector:
        def reset(self):
            return None

        def feed(self, _block):
            return [stage2_event]

    class FakeController:
        def reset(self):
            return None

        def process(self, _block, _events):
            return None

        def flush(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(app_runtime, "RingAudioSource", FakeSource)
    monkeypatch.setattr(app_runtime, "_build_detector", lambda _args: FakeDetector())
    monkeypatch.setattr(
        app_runtime,
        "_build_session_controller",
        lambda *_args, **_kwargs: FakeController(),
    )
    recognition_event = threading.Event()
    recognition_event.set()

    RecognitionRuntime(RuntimeSettings()).run(
        threading.Event(),
        recognition_event,
        on_update=lambda _update: None,
        on_state=states.append,
        on_connected=lambda: None,
        on_disconnected=lambda: None,
        on_started=lambda: None,
    )

    assert any(
        state.startswith("STAGE2 ") and state.endswith("ACTIVATE")
        for state in states
    )


def test_disconnect_interrupts_connection_before_models_load(monkeypatch):
    connect_started = threading.Event()
    close_requested = threading.Event()
    events = []

    class BlockingSource:
        error = None

        def __init__(self, **_kwargs):
            pass

        def connect(self):
            connect_started.set()
            assert close_requested.wait(2.0)

        def start_stream(self, *, buffer_audio=True):
            events.append("audio-started")

        def close(self):
            events.append("source-closed")
            close_requested.set()

    monkeypatch.setattr(app_runtime, "RingAudioSource", BlockingSource)
    monkeypatch.setattr(
        app_runtime,
        "_build_detector",
        lambda _args: events.append("detector-loaded"),
    )

    disconnect_event = threading.Event()
    runtime_thread = threading.Thread(
        target=lambda: RecognitionRuntime(RuntimeSettings()).run(
            disconnect_event,
            threading.Event(),
            on_update=lambda _update: None,
            on_state=lambda _message: None,
            on_connected=lambda: events.append("ui-connected"),
            on_disconnected=lambda: events.append("ui-disconnected"),
            on_started=lambda: events.append("runtime-ready"),
        )
    )
    runtime_thread.start()
    assert connect_started.wait(1.0)
    disconnect_event.set()
    runtime_thread.join(timeout=3.0)

    assert not runtime_thread.is_alive()
    assert "source-closed" in events
    assert "ui-disconnected" in events
    assert "audio-started" not in events
    assert "ui-connected" not in events
    assert "detector-loaded" not in events


def test_connection_failure_closes_device_and_reports_disconnected(monkeypatch):
    events = []

    class FailingSource:
        error = None

        def __init__(self, **_kwargs):
            pass

        def connect(self):
            events.append("connect-attempted")
            raise RuntimeError("NUS service unavailable")

        def close(self):
            events.append("source-closed")

    monkeypatch.setattr(app_runtime, "RingAudioSource", FailingSource)
    monkeypatch.setattr(
        app_runtime,
        "_build_detector",
        lambda _args: events.append("detector-loaded"),
    )

    with pytest.raises(RuntimeError, match="NUS service unavailable"):
        RecognitionRuntime(RuntimeSettings()).run(
            threading.Event(),
            threading.Event(),
            on_update=lambda _update: None,
            on_state=lambda _message: None,
            on_connected=lambda: events.append("ui-connected"),
            on_disconnected=lambda: events.append("ui-disconnected"),
            on_started=lambda: events.append("runtime-ready"),
        )

    assert events.index("connect-attempted") < events.index("source-closed")
    assert events.index("source-closed") < events.index("ui-disconnected")
    assert "ui-connected" not in events
    assert "detector-loaded" not in events
