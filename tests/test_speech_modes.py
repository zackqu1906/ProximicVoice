import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from proximic_ring import app_runtime
from proximic_ring.asr.controller import ProximitySessionController
from proximic_ring.gesture_settings import GestureBindings
from test_asr_controller import StreamingRecorder, activate_event, make_gate


def test_gesture_control_ignores_detector_and_hold_and_keeps_only_confirmed_audio():
    sink = StreamingRecorder()
    gate = make_gate(sink, start_on_gesture=True, manual_active=lambda: True)
    block = np.full(320, 0.2, dtype=np.float32)
    for _ in range(5):
        gate.process(block * 4, [activate_event(320)])
    assert not gate.active and not sink.started
    assert gate.request_gesture_toggle() == "start"
    assert gate.request_gesture_toggle() is None
    gate.process(block, [])
    assert gate.active
    for _ in range(105):  # Past the 2 s automatic limit and silence endpoint.
        gate.process(block, [])
    assert not sink.ended
    assert gate.request_gesture_toggle() == "end"
    assert gate.request_gesture_toggle() is None
    gate.process(block, [])
    assert not gate.active
    np.testing.assert_array_equal(sink.ended[0], np.full(107 * 320, 0.2, dtype=np.float32))


@pytest.mark.parametrize("boundary", ["discard_current", "reset", "abort", "flush"])
def test_pending_start_does_not_survive_cancel_pause_or_disconnect(boundary):
    sink = StreamingRecorder()
    gate = make_gate(sink, start_on_gesture=True)
    assert gate.request_gesture_toggle() == "start"
    getattr(gate, boundary)()
    gate.process(np.ones(320, dtype=np.float32), [])
    assert not gate.active and not sink.started and not sink.ended
    assert gate.request_gesture_toggle() == "start"
    gate.process(np.ones(320, dtype=np.float32), [])
    assert gate.active


@pytest.mark.parametrize("audio_source", ["ring", "microphone"])
@pytest.mark.parametrize("control_mode", ["proximity", "gesture"])
def test_four_combinations_use_selected_audio_and_respect_processing_gate(monkeypatch, audio_source, control_mode):
    import ring_python_sdk.gestures as gestures

    state, logs, finals, actions, detections = {}, [], [], [], []
    disconnect, recognition, cancel = threading.Event(), threading.Event(), threading.Event()
    recognition.set()
    bindings = GestureBindings(confirm=("snap", "tap"))
    producer = threading.get_ident()

    def gesture(name="snap"):
        thread = threading.Thread(target=lambda: state["callback"](SimpleNamespace(name=name, confidence=.99)))
        thread.start()
        thread.join(1)
        assert not thread.is_alive()

    class Recognizer:
        prediction_count = 0
        reset_count = 0

        def __init__(self, *, on_gesture):
            state["callback"] = on_gesture
            self.classifier = SimpleNamespace(window_size=60, predict=lambda _: None)

    class Sink:
        def start(self, audio):
            assert threading.get_ident() == producer

        def feed(self, audio):
            pass

        def end(self, audio):
            assert not recognition.is_set()
            assert threading.get_ident() == producer
            finals.append(audio.copy())

        def close(self):
            pass

        def abort(self):
            pass

    def read_audio(frames):
        if not state.get("ready"):
            return np.zeros(frames, dtype=np.float32)  # DJI initial PCM proof.
        count = state["reads"] = state.get("reads", 0) + 1
        gate = state["gate"]
        if count == 1 and control_mode == "gesture":
            gesture()
        elif count == 2:
            assert gate.active
            gesture("swipe-right")
        elif count == 3:
            gesture("tap")
            gesture("tap")
        elif count == 4:
            assert not recognition.is_set() and len(finals) == 1
            gesture()
            assert not gate.active
        elif count == 5:
            recognition.set()
            if control_mode == "gesture":
                gesture()
        elif count == 6:
            assert gate.active
            cancel.set()
            gesture()  # Cancel wins over a simultaneous confirm.
        elif count == 7:
            assert not gate.active
            disconnect.set()
            return None
        return np.full(frames, .2 if audio_source == "microphone" else .1, dtype=np.float32)

    class Ring:
        error = None

        def __init__(self, **kwargs):
            assert kwargs["imu_hz"] == 200
            assert kwargs.get("audio_enabled", True) is (audio_source == "ring")

        def connect(self):
            pass

        def start_stream(self, **kwargs):
            state["imu_started"] = True

        def read(self, frames):
            assert audio_source == "ring", "Ring PCM must not feed DJI mode"
            return read_audio(frames)

        def close(self):
            state["ring_closed"] = True

    class Mic:
        error = None
        device_name = "DJI Mic"

        def __init__(self, *, selection):
            assert selection == "saved-device"

        def open(self):
            assert state["imu_started"]

        def read(self, frames):
            return read_audio(frames)

        def close(self):
            state["mic_closed"] = True

    class Detector:
        def reset(self):
            pass

        def feed(self, block):
            detections.append(block.copy())
            return [activate_event(320)]

    def build_detector(args):
        assert control_mode == "proximity", "pure gestures must not load a proximity model"
        return Detector()

    def build_controller(args, detector, **kwargs):
        assert (detector is None) is (control_mode == "gesture")
        gate = ProximitySessionController(
            Sink(), pre_roll_s=.02, min_utterance_s=.02,
            start_on_gesture=args.asr_start_on_gesture, end_on_tap=args.asr_end_on_tap,
            on_session_end=kwargs["session_end_observer"], on_state=kwargs["on_state"],
        )
        state["gate"] = gate
        return gate

    monkeypatch.setattr(gestures, "GestureRecognizer", Recognizer)
    monkeypatch.setattr(app_runtime, "RingAudioSource", Ring)
    monkeypatch.setattr(app_runtime, "MicrophoneSource", Mic)
    monkeypatch.setattr(app_runtime, "_build_detector", build_detector)
    monkeypatch.setattr(app_runtime, "_build_session_controller", build_controller)
    settings = app_runtime.RuntimeSettings(
        audio_source=audio_source, speech_control_mode=control_mode,
        asr_end_on_tap=True, microphone_device="saved-device", gesture_bindings=bindings,
    )
    app_runtime.RecognitionRuntime(settings).run(
        disconnect, recognition, cancel_utterance_event=cancel,
        on_update=lambda _: None, on_state=logs.append,
        on_connected=lambda: None, on_disconnected=lambda: None,
        on_started=lambda: state.update(ready=True), on_session_ended=recognition.clear,
        on_gesture=actions.append,
    )
    assert state["ring_closed"] and state.get("mic_closed", audio_source == "ring")
    assert len(finals) == 1
    np.testing.assert_array_equal(finals[0], np.full(3 * 320, .2 if audio_source == "microphone" else .1, dtype=np.float32))
    assert [event.name for event in actions] == ["swipe-right"]
    assert len(detections) == (2 if control_mode == "proximity" else 0)
    for block in detections:
        np.testing.assert_array_equal(block, np.full(320, .2 if audio_source == "microphone" else .1, dtype=np.float32))


def test_settings_persist_independent_choices_and_lock_during_connection(tmp_path, monkeypatch):
    from test_interaction_controls import _controller, _close

    controller = _controller(tmp_path, monkeypatch)
    restarted = None
    row = {"name": "DJI Mic", "api": "Core Audio", "index": 5}
    row.update(value=json.dumps(row), label="DJI Mic")
    try:
        assert controller.audioSource == "ring"
        assert controller.speechControlMode == "proximity"
        controller._apply_microphone_scan_finished([row], "")
        controller.audioSource = "microphone"
        controller.speechControlMode = "gesture"
        controller.microphoneDevice = row["value"]
        controller._selector = "ring-id"
        controller._model_path = str(tmp_path / "absent.model")
        settings = controller._runtime_settings()
        assert settings.audio_source == "microphone" and settings.speech_control_mode == "gesture"
        assert settings.microphone_device == row["value"]
        controller._connected = True
        controller.audioSource = "ring"
        controller.speechControlMode = "proximity"
        controller.microphoneDevice = ""
        assert controller.audioSource == "microphone" and controller.speechControlMode == "gesture"
        assert controller.microphoneDevice == row["value"]
        controller.startRecognition()
        assert controller.statusTitle == "等待开始手势"
        assert "开始" in controller.statusDetail
        restarted = _controller(tmp_path, monkeypatch)
        assert restarted.audioSource == "microphone" and restarted.speechControlMode == "gesture"
        assert restarted.microphoneDevice == row["value"]
        assert "未检测到" in restarted.microphoneDevices[-1]["label"]
    finally:
        if restarted:
            _close(restarted)
        _close(controller)


def test_dataset_marks_audio_source_and_does_not_label_gesture_start_as_proximity(tmp_path):
    from proximic_ring.modification_dataset import ModificationDatasetCollector

    collector = ModificationDatasetCollector(tmp_path, user_id="test")
    collector.set_capture_configuration(audio_source="microphone", speech_control_mode="gesture")
    identifier = collector.begin_session(1)
    collector.record_near_field_label(1, label="positive", source="successful_application")
    record = collector._interaction_data(identifier)
    assert record["capture"]["audio_source"] == "microphone"
    assert record["near_field"]["enabled"] is False
    assert record["near_field"]["training_label"] is None


@pytest.mark.parametrize("failure", ["ring", "microphone", "manual"])
def test_either_device_failure_closes_gates_before_unblocking_microphone(monkeypatch, failure):
    entered_read, released, stopped = threading.Event(), threading.Event(), threading.Event()
    disconnect, recognition = threading.Event(), threading.Event()
    recognition.set()
    calls, logs, sources, errors = [], [], {}, []

    class Ring:
        error = None
        def __init__(self, **kwargs):
            sources["ring"] = self
        def connect(self):
            pass
        def start_stream(self, **kwargs):
            pass
        def close(self):
            calls.append("ring-close")

    class Mic:
        error = None
        device_name = "DJI Mic"
        count = 0
        def __init__(self, **kwargs):
            sources["microphone"] = self
        def open(self):
            pass
        def read(self, frames):
            self.count += 1
            if self.count == 1:
                return np.zeros(frames, dtype=np.float32)
            entered_read.set()
            assert released.wait(2)
            return None
        def close(self):
            assert stopped.is_set() and disconnect.is_set() and not recognition.is_set()
            calls.append("mic-close")
            released.set()

    class Controller:
        def abort(self):
            calls.append("abort")
        def close(self):
            calls.append("controller-close")

    monkeypatch.setattr(app_runtime, "RingAudioSource", Ring)
    monkeypatch.setattr(app_runtime, "MicrophoneSource", Mic)
    monkeypatch.setattr(app_runtime, "_build_detector", lambda args: object())
    monkeypatch.setattr(app_runtime, "_build_session_controller", lambda *args, **kwargs: Controller())
    def run():
        try:
            app_runtime.RecognitionRuntime(app_runtime.RuntimeSettings(audio_source="microphone")).run(
                disconnect, recognition, on_update=lambda _: None, on_state=logs.append,
                on_connected=lambda: None, on_disconnected=lambda: calls.append("disconnected"),
                on_started=lambda: None, on_stopping=stopped.set,
            )
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered_read.wait(2)
        if failure == "manual":
            disconnect.set()
        else:
            sources[failure].error = RuntimeError("unplugged")
        assert stopped.wait(1)
    finally:
        disconnect.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert calls == ["mic-close", "ring-close", "disconnected", "abort", "controller-close"]
    assert any("[AUDIO_INPUT_ERROR]" in line for line in logs) is (failure == "microphone")


def test_real_session_factory_needs_no_proximity_model_for_gesture_mode(monkeypatch):
    from proximic_ring import asr
    from proximic_ring.cli import _build_session_controller

    backend = SimpleNamespace(backend_name="funasr_nano", model_name="stub", abort=lambda: None)
    monkeypatch.setattr(asr, "create_streaming_asr_backend", lambda *args: backend)
    args = app_runtime.RuntimeSettings(
        speech_control_mode="gesture", asr_backend="funasr_nano",
        desktop_output=False, push_to_talk=True,
    ).to_namespace()
    assert not args.push_to_talk
    gate = _build_session_controller(args, None, show_streaming_console=False, on_state=lambda text: None)
    try:
        assert gate.start_on_gesture and gate.end_on_tap
        assert gate.pre_roll_samples == 0
    finally:
        gate.close()
