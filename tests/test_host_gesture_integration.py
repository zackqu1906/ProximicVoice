"""Host gestures share audio transport and existing GUI actions safely."""

import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from proximic_ring import app_runtime
from proximic_ring.audio.ring import RingAudioSource


@pytest.mark.parametrize("backend,initial,expected", [
    ("volcengine", 8, 1), ("volcengine", 1, 1),
    ("streaming_sensevoice", 4, 4), ("funasr_nano", 4, 4),
])
def test_online_asr_matches_standalone_cpu_budget_without_changing_local_asr(backend, initial, expected):
    settings = {"threads": initial}
    torch = SimpleNamespace(
        get_num_threads=lambda: settings["threads"],
        set_num_threads=lambda count: settings.update(threads=count),
    )
    recognizer = SimpleNamespace(classifier=SimpleNamespace(_torch=torch))
    assert app_runtime._configure_gesture_cpu_threads(recognizer, backend) == expected
    assert settings["threads"] == expected


@pytest.mark.parametrize("failure", ["", "load", "inference", "imu"])
@pytest.mark.parametrize("collect_imu", [False, True])
def test_runtime_gestures_share_source_and_isolate_failures(
    monkeypatch, failure, collect_imu
):
    import ring_python_sdk.gestures as gestures

    calls, states, received, records = [], [], [], []
    processed = threading.Event()
    instances = []
    recognizers = []
    gesture = SimpleNamespace(name="swipe-left", confidence=0.95)
    sample = SimpleNamespace(
        sample_index=0, packet_seq=0, uptime_ms=100.0,
        accel_ms2=(1., 2., 3.), gyro_dps=(4., 5., 6.), raw=(1, 2, 3, 4, 5, 6),
    )

    class FakeRecognizer:
        prediction_count = 0
        reset_count = 1

        def __init__(self, *, on_gesture):
            calls.append("gesture-load")
            if failure == "load":
                raise RuntimeError("missing checkpoint")
            self.on_gesture = on_gesture
            self.classifier = SimpleNamespace(
                window_size=60, predict=lambda _x: calls.append("warmup")
            )
            recognizers.append(self)

        def on_sample(self, observed):
            try:
                assert observed is sample
                assert threading.current_thread().name == "RingHostGestures"
                if failure == "inference":
                    raise RuntimeError("inference failed")
                self.prediction_count += 1
                self.on_gesture(gesture)
            finally:
                processed.set()

    class FakeSession:
        async def imu_on(self, **kwargs):
            calls.append("imu-start")
            assert kwargs["gyro_hz"] == kwargs["accel_hz"] == 200
            if failure == "imu":
                raise RuntimeError("IMU unsupported")
            kwargs["on_sample"](sample)

    class FakeSource(RingAudioSource):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.read_count = 0
            instances.append(self)

        def connect(self):
            calls.append("connect")

        def start_stream(self, **_kwargs):
            calls.append("mic-start")
            asyncio.run(self._start_imu_best_effort(FakeSession()))

        def read(self, frames):
            self.read_count += 1
            if self.read_count > 1:
                return None
            if self.imu_sample_observer is not None and failure != "imu":
                assert processed.wait(1)
                # Wait for the worker's error handler as well as the callback.
                if failure == "inference":
                    self.imu_sample_observer.__self__._thread.join(1)
            return np.zeros(frames, dtype=np.float32)

        def close(self):
            calls.append("source-close")
            # A late inference completion during teardown must be inert.
            for recognizer in recognizers:
                recognizer.on_gesture(gesture)

    class FakeDetector:
        def reset(self):
            pass

        def feed(self, _block):
            return []

    def build_controller(*_args, **kwargs):
        calls.append("asr-load")
        return SimpleNamespace(
            close=lambda: calls.append("asr-close"),
            reset=lambda: None,
            process=lambda block, *_a, **_kw: kwargs["raw_audio_observer"](9, block),
            flush=lambda: None,
        )

    monkeypatch.setattr(gestures, "GestureRecognizer", FakeRecognizer)
    monkeypatch.setattr(app_runtime, "RingAudioSource", FakeSource)
    monkeypatch.setattr(app_runtime, "_build_detector", lambda _args: FakeDetector())
    monkeypatch.setattr(app_runtime, "_build_session_controller", build_controller)
    monkeypatch.setattr(app_runtime, "GESTURE_STATUS_INTERVAL_S", 0)
    recognition = threading.Event()
    # Gestures must work while voice recognition is suspended for interaction.
    if collect_imu:
        recognition.set()
    app_runtime.RecognitionRuntime(
        app_runtime.RuntimeSettings(collect_imu=collect_imu)
    ).run(
        threading.Event(), recognition,
        on_update=lambda _u: None, on_state=states.append,
        on_connected=lambda: None, on_disconnected=lambda: None,
        on_started=lambda: calls.append("started"),
        on_gesture=received.append,
        on_raw_imu=lambda session, rows, metadata: records.append((rows, metadata)),
    )

    assert len(instances) == 1
    assert instances[0].error is None
    assert calls.index("asr-load") < calls.index("gesture-load") < calls.index("mic-start")
    assert "started" in calls and "asr-close" in calls
    assert received == ([gesture] if not failure else [])
    if not failure:
        import json
        status = next(message for message in states if message.startswith("[GESTURE_STATUS]"))
        assert json.loads(status.split("] ", 1)[1])["received_samples"] == 1
        assert any("[GESTURE_MODEL]" in message and "sha256" in message for message in states)
        recognized = [message for message in states if "[GESTURE_RECOGNIZED]" in message]
        assert any('"forwarded": true' in message for message in recognized)
        assert any('"forwarded": false' in message for message in recognized)
    if failure:
        assert any("按键和语音继续工作" in message for message in states)
    else:
        assert calls.index("warmup") < calls.index("imu-start")
    if collect_imu:
        assert records[0][1]["sample_rate_hz"] == 200
        if failure != "imu":
            assert records[0][0][0]["gyro_dps"] == [4., 5., 6.]
    assert not any(t.name == "RingHostGestures" for t in threading.enumerate())


@pytest.fixture
def controller(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QSettings
    import proximic_ring.ui.controller as controller_module

    app = QCoreApplication.instance() or QCoreApplication(["gesture-controls"])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    monkeypatch.setattr(controller_module, "app_data_root", lambda: tmp_path)
    instance = controller_module.AppController()
    instance._text_processing_worker.close(wait=True)
    instance._runtime_active = True
    instance._connected = True
    instance._desktop_output = False
    yield instance
    app.processEvents()
    instance._text_processing_worker.close(wait=True)
    instance._close_voice_history()


def test_gesture_callback_queues_to_gui_and_cancel_alternates_with_keyboard(controller):
    from PySide6.QtCore import QCoreApplication

    controller._recognition_enabled = True
    for gesture in ("swipe-left", "keyboard", "swipe-down"):
        controller._apply_runtime_status("[ASR] START t=1.000s")
        assert controller.interactionState == "listening"
        if gesture == "keyboard":
            controller.dispatchVoiceAction("cancel")
        else:
            callback = threading.Thread(target=lambda: controller._gestureRecognized.emit(
                SimpleNamespace(name=gesture, confidence=0.9), controller._disconnect_event
            ))
            callback.start()
            callback.join(1)
            assert controller.interactionState == "listening"
            QCoreApplication.processEvents()
        assert controller.interactionState == "cancelled"
        assert controller._cancel_utterance_event.is_set()


def test_gesture_conversion_and_undo_use_existing_availability(controller, monkeypatch):
    calls = []
    operation = SimpleNamespace(auto_context=object())
    monkeypatch.setattr(controller, "_active_operation_stack", lambda: [operation])
    monkeypatch.setattr(controller, "_latest_operation", lambda: operation)
    monkeypatch.setattr(controller, "_operation_can_switch_mode", lambda _op: True)
    monkeypatch.setattr(controller, "undoLastApplied", lambda: calls.append("undo"))
    monkeypatch.setattr(controller, "cancelCurrentUtterance", lambda: calls.append("cancel"))
    monkeypatch.setattr(controller, "switchCurrentInputMode", lambda: calls.append("switch_mode"))
    controller._interaction_state = "applied"
    controller._applied_action_visible = True
    controller._applied_target_foreground = True
    controller._pending_text_requests.add(123)  # alternate result still loading
    original_mode = controller.inputMode

    def emit(name):
        controller._gestureRecognized.emit(SimpleNamespace(name=name), controller._disconnect_event)

    emit("swipe-left")
    controller.dispatchVoiceAction("undo")
    emit("swipe-down")
    emit("swipe-right")
    controller.dispatchVoiceAction("switch_mode")
    emit("swipe-up")
    assert calls == ["undo"] * 3 + ["switch_mode"] * 3
    assert controller.inputMode == original_mode

    controller._applied_action_visible = False
    emit("swipe-left")  # Escape is unavailable once the undo overlay disappears.
    emit("swipe-up")  # Conversion key is deliberately available after timeout.
    assert calls == ["undo"] * 3 + ["switch_mode"] * 4
    controller._applied_target_foreground = False
    for name in ("swipe-up", "swipe-right", "swipe-left", "swipe-down", "tap", "snap"):
        emit(name)
    assert calls == ["undo"] * 3 + ["switch_mode"] * 4


@pytest.mark.parametrize("boundary", ["old_connection", "disconnecting", "disconnected", "finished"])
def test_stale_connection_gesture_cannot_apply(controller, monkeypatch, boundary):
    calls = []
    monkeypatch.setattr(controller, "cancelCurrentUtterance", lambda: calls.append("cancel"))
    controller._interaction_state = "listening"
    token = controller._disconnect_event
    if boundary == "old_connection":
        token = threading.Event()
    elif boundary == "disconnecting":
        controller._disconnect_event.set()
    elif boundary == "disconnected":
        controller._connected = False
    elif boundary == "finished":
        controller._runtime_active = False
    controller._gestureRecognized.emit(SimpleNamespace(name="swipe-left"), token)
    assert calls == []
    log = controller._diagnostic_log.path.read_text()
    assert "GESTURE_ACTION" in log and 'action="ignored"' in log
    assert "reason=" in log


def test_ignored_gesture_logs_reason_and_health_does_not_replace_ui_status(controller):
    controller._interaction_state = "idle"
    controller._gestureRecognized.emit(
        SimpleNamespace(name="swipe-up", confidence=0.91, timestamp_ms=12345),
        controller._disconnect_event,
    )
    controller._gestureRecognized.emit(SimpleNamespace(name="tap"), controller._disconnect_event)
    log = controller._diagnostic_log.path.read_text()
    assert 'gesture="swipe-up"' in log and 'reason="no_convertible_result"' in log
    assert 'reason="unmapped_gesture"' in log and "device_timestamp_ms=12345" in log
    assert "[手势] 上滑 → 当前无可转换结果" in controller.logText
    assert "GESTURE_ACTION" not in controller.logText
    assert "confidence" not in controller.logText
    assert "tap" not in controller.logText
    controller._set_status("识别中", "等待说话", "running")
    controller._apply_runtime_status('[GESTURE_STATUS] {"received_samples": 1000}')
    assert controller._status_detail == "等待说话"
    assert "GESTURE_STATUS" not in controller.logText
    for _ in range(3):
        controller._apply_runtime_status('[GESTURE_STATUS] {"status": "inference_backlog"}')
    assert controller.logText.count("[手势] 识别延迟偏高") == 1
    controller._apply_runtime_status('[GESTURE_STATUS] {"status": "running"}')
    assert controller.logText.count("[手势] 识别已恢复") == 1


@pytest.mark.parametrize("failing_consumer", ["dataset", "gesture"])
def test_imu_consumers_cannot_disable_each_other(failing_consumer):
    rows, samples = [], []

    def fail(_sample):
        raise RuntimeError("consumer failed")

    source = RingAudioSource(
        imu_observer=fail if failing_consumer == "dataset" else rows.append,
        imu_sample_observer=fail if failing_consumer == "gesture" else samples.append,
    )
    sample = SimpleNamespace(
        sample_index=0, packet_seq=0, uptime_ms=100,
        accel_ms2=(1, 2, 3), gyro_dps=(4, 5, 6), raw=(1, 2, 3, 4, 5, 6),
    )
    source._on_imu_sample(sample)
    source._on_imu_sample(sample)
    assert source.error is None
    assert len(samples if failing_consumer == "dataset" else rows) == 2
