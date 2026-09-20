"""Offline integration of gesture endpoints, ASR worker, Qt host and IME IPC.

Run this file in its own pytest process. Only the cloud backend, source-selection helper and native IPC
peer are simulated; the session controller, worker, host and input controller
are the production classes. No window, microphone, socket or external app opens.
"""
from dataclasses import dataclass, field
import threading
import time

import numpy as np
import pytest

from proximic_ring.asr.controller import ProximitySessionController
from proximic_ring.asr.session_sink import RawAudioObserverSessionSink, SessionFanout
from proximic_ring.asr.streaming import StreamingASRWorker


@dataclass
class Reply:
    text: str = ""
    partial: str = ""
    error: str = ""
    blocked: bool = False
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)


class ScriptedBackend:
    backend_name = "offline-pipeline-test"
    model_name = "scripted"

    def __init__(self, replies):
        self.replies = replies
        self.starts = 0
        self.final_samples = []
        self.finished = []
        self.current = None
        self.partial_sent = False

    def start(self):
        self.current = self.replies[self.starts]
        self.starts += 1
        self.partial_sent = False

    def feed(self, audio):
        if not self.partial_sent:
            self.partial_sent = True
            return self.current.partial
        return None

    def finish(self, audio):
        reply = self.current
        self.final_samples.append(len(audio))
        reply.entered.set()
        if reply.blocked and not reply.release.wait(4):
            raise RuntimeError("test did not release final inference")
        self.finished.append(reply)
        if reply.error:
            raise RuntimeError(reply.error)
        return reply.text

    def abort(self):
        pass


class NativePeer:
    """Deliver ACKs on a later Qt turn, as the real native socket does."""
    def __init__(self, callback):
        self.callback = callback
        self.epoch = "pipeline-epoch"
        self.messages = []
        self.commits = []
        self.current = ""
        self.raw = ""
        self.phase = "idle"
        self.revision = 0
        self.closed = False
        self.hold_terminal_ack = False
        self.terminal_acks = []

    def _event(self, kind="state", **fields):
        from PySide6.QtCore import QTimer
        if kind == "state" and "phase" in fields:
            self.phase = fields["phase"]
        payload = {
            "type": kind, "epoch": self.epoch, "client_id": "offline-editor",
            "utterance_id": self.current, "ready": True, "revision": self.revision,
            "application": "test.offline.editor", "context_complete": True,
            "original": "", "selection": [0, 0], "raw": self.raw,
            "error": "", "can_convert": True, "can_cancel": True, **fields,
        }
        QTimer.singleShot(0, lambda payload=payload: None if self.closed else self.callback(payload))

    def start(self):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self.callback({"type": "connected", "epoch": self.epoch}))
        self._event(phase="idle")

    def send(self, message):
        self.messages.append(dict(message))
        kind = message["type"]
        if kind == "ping":
            self._event(phase=self.phase)
        elif kind == "begin":
            self.current = message["utterance_id"]
            self.raw = ""
            self.revision = 0
            self._event(phase="listening")
        elif kind == "finish":
            self._event(phase="finishing")
            self._event("finish_audio")
        elif kind == "update":
            if not message.get("error"):
                self.raw = message["text"]
            if message.get("final") or message.get("error"):
                self.commits.append((self.current, self.raw, message.get("error", "")))
                def acknowledge(transaction=self.current, raw=self.raw, error=message.get("error", "")):
                    self._event(phase="dictated", utterance_id=transaction, raw=raw, error=error)
                    self._event("settled", phase="dictated", utterance_id=transaction, text=raw, raw=raw, error=error)
                if self.hold_terminal_ack:
                    self.terminal_acks.append(acknowledge)
                else:
                    acknowledge()
            else:
                self._event(phase="listening")
        elif kind == "cancel":
            self.revision += 1
            self._event(phase="undone")
            self._event("finish_audio")
            self._event("settled", phase="undone", text="")
        return True

    def asr_context(self):
        return {"status": "captured", "source": "input_method", "text": "", "context_complete": True}

    def close(self):
        self.closed = True

    def release_terminal_acks(self):
        self.hold_terminal_ack = False
        callbacks, self.terminal_acks = self.terminal_acks, []
        for callback in callbacks:
            callback()


class Pipeline:
    def __init__(self, app, host, replies):
        self.app = app
        self.host = host
        self.backend = ScriptedBackend(replies)
        self.updates = []
        self.audio = []

        def publish(update):
            self.updates.append(update)
            host._publish_update(update)

        self.worker = StreamingASRWorker(
            self.backend, on_update=publish,
            on_state=host._runtimeStatus.emit, on_error=host._runtimeStatus.emit,
        )
        self.gate = ProximitySessionController(
            SessionFanout([
                RawAudioObserverSessionSink(
                    lambda sid, audio: self.audio.append((sid, audio.copy())),
                    on_start=host._runtimeSessionStarted.emit,
                ), self.worker,
            ]),
            start_on_gesture=True, end_on_tap=True, min_utterance_s=0.1,
            on_state=host._runtimeStatus.emit,
            on_session_end=host._suspend_recognition_for_interaction,
        )
        self.peer = host._inline_input._bridge
        self.wait(lambda: host._inline_input.ready)

    def wait(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            time.sleep(0.001)
        pytest.fail(
            f"pipeline timed out: phase={self.host._inline_input._view.get('phase')}, "
            f"gate={self.host._recognition_event.is_set()}, "
            f"session={self.host._latest_asr_session_id}, commits={self.peer.commits}"
        )

    def consume_runtime_commands(self):
        # Same public session-controller boundary used by RecognitionRuntime.
        # No state inside the worker or native input controller is modified.
        if self.host._cancel_utterance_event.is_set():
            self.host._cancel_utterance_event.clear()
            self.gate.discard_current()
            self.gate.cancel_pending()
            self.host._runtimeStatus.emit("[ASR] CANCELLED reason=user-request")
        if self.host._finish_utterance_event.is_set():
            self.host._finish_utterance_event.clear()
            self.gate.request_user_end()

    def start(self):
        self.consume_runtime_commands()
        assert self.host._recognition_event.is_set(), "next tap was blocked by stale UI gate"
        assert self.gate.request_gesture_toggle() == "start"
        self.gate.process(np.zeros(320, dtype=np.float32), [])
        self.wait(lambda: self.host._inline_input._view.get("phase") == "listening")
        return self.host._latest_asr_session_id

    def end(self, *, short=False):
        assert self.gate.request_gesture_toggle() == "end"
        self.gate.process(np.zeros(320 if short else 1600, dtype=np.float32), [])
        assert not self.gate.active
        assert not self.host._recognition_event.is_set(), "END did not close the gate before final inference"

    def settled(self, count):
        self.wait(lambda: len(self.peer.commits) == count
                  and self.host._recognition_event.is_set()
                  and self.host._inline_input._view.get("phase") == "dictated")
        assert self.host._inline_input._view["phase"] == "dictated"
        assert not self.host._interaction_recognition_suspended

    def close(self):
        for reply in self.backend.replies:
            reply.release.set()
        self.worker.abort()
        self.worker.close()
        self.host._inline_input.close()
        self.host._text_processing_worker.close(wait=True)
        self.host._close_voice_history()
        self.host.deleteLater()
        self.app.processEvents()


@pytest.fixture
def pipeline_factory(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings, QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication
    import proximic_ring.ui.controller as host_module
    import proximic_ring.ui.inline_controller as inline_module

    app = QCoreApplication.instance() or QApplication(["inline-pipeline", "-platform", "offscreen"])
    monkeypatch.setattr(host_module, "app_data_root", lambda: tmp_path)
    monkeypatch.setattr(host_module, "QSettings", lambda *_args: QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat))
    monkeypatch.setattr(inline_module, "IMEBridge", NativePeer)
    monkeypatch.setattr(inline_module, "warm_input_method", lambda: None)
    monkeypatch.setattr(inline_module.InlineInputController, "_foreground_identity", lambda self: ("test.offline.editor", 123))
    class ReadyInputSource(QObject):
        event = Signal(object)
        cancelled = False

        def start(self, origin):
            QTimer.singleShot(0, lambda: None if self.cancelled else self.event.emit(
                {"event": "selected", "already_selected": True}))

        def cancel(self):
            self.cancelled = True

    pipelines = []

    def create(replies):
        host = host_module.AppController(inline_input_enabled=True)
        host._inline_input._source_switch_factory = ReadyInputSource
        host._connected = True
        host._desktop_output = True
        host._speech_control_mode = "gesture"
        host._recognition_enabled = True
        host._recognition_event.set()
        pipeline = Pipeline(app, host, replies)
        pipelines.append(pipeline)
        return pipeline

    yield create
    for pipeline in pipelines:
        pipeline.close()


def test_consecutive_empty_finals_restore_gate_and_next_tap_dictates(pipeline_factory):
    pipeline = pipeline_factory([Reply(), Reply(), Reply(text="下一句成功", partial="下一句")])
    for expected in (1, 2):
        assert pipeline.start() == expected
        pipeline.end(short=True)
        pipeline.settled(expected)
        assert pipeline.peer.commits[-1][1] == ""
        assert pipeline.backend.final_samples[-1] == 0
    assert pipeline.start() == 3
    pipeline.end()
    pipeline.settled(3)
    assert [entry[1] for entry in pipeline.peer.commits] == ["", "", "下一句成功"]
    assert [update.session_id for update in pipeline.updates if update.is_final] == [1, 2, 3]


def test_cancel_after_end_discards_late_final_and_next_sentence_survives(pipeline_factory):
    old = Reply(text="旧句迟到定稿", blocked=True)
    pipeline = pipeline_factory([old, Reply(text="新的句子")])
    first = pipeline.start()
    pipeline.end()
    pipeline.wait(old.entered.is_set)
    pipeline.host.cancelCurrentUtterance()
    pipeline.wait(lambda: pipeline.host._inline_input._view.get("phase") == "undone")
    assert pipeline.host._recognition_event.is_set()
    pipeline.consume_runtime_commands()
    assert pipeline.start() == first + 1
    new_transaction = pipeline.host._inline_input._utterance_id
    # Also exercise a final that had crossed the worker boundary before cancel
    # but reaches the Qt slot after a new sentence has acquired its session ID.
    pipeline.host._runtimeUpdate.emit("旧句排队回调", True, "", first)
    pipeline.app.processEvents()
    assert pipeline.host._inline_input._utterance_id == new_transaction
    assert pipeline.host._recognition_event.is_set()
    old.release.set()
    pipeline.end()
    pipeline.settled(1)
    assert [entry[1] for entry in pipeline.peer.commits] == ["新的句子"]
    assert not any(update.is_final and update.session_id == first for update in pipeline.updates)


def test_backend_final_error_settles_and_next_tap_recovers(pipeline_factory):
    pipeline = pipeline_factory([Reply(error="离线模拟识别失败"), Reply(text="错误后恢复")])
    pipeline.peer.hold_terminal_ack = True
    pipeline.start()
    pipeline.end()
    pipeline.wait(lambda: len(pipeline.peer.commits) == 1)
    assert pipeline.host._recognition_event.is_set(), "error final reclosed the gate before native ACK"
    pipeline.peer.release_terminal_acks()
    pipeline.settled(1)
    assert pipeline.peer.commits[0][2] == "离线模拟识别失败"
    assert any(update.is_final and update.error for update in pipeline.updates)
    pipeline.start()
    pipeline.end()
    pipeline.settled(2)
    assert pipeline.peer.commits[-1][1] == "错误后恢复"


def test_delayed_source_selection_does_not_delay_audio_start_or_end(pipeline_factory):
    from PySide6.QtCore import QObject, Signal

    class DelayedSource(QObject):
        event = Signal(object)
        def start(self, origin):
            self.origin = origin
        def cancel(self):
            pass

    pipeline = pipeline_factory([Reply(text="切换期间的话", partial="切换期间")])
    pipeline.host._inline_input._source_switch_factory = DelayedSource
    assert pipeline.gate.request_gesture_toggle() == "start"
    pipeline.gate.process(np.zeros(320, dtype=np.float32), [])
    pipeline.wait(lambda: pipeline.backend.starts == 1 and pipeline.host._inline_input._source_pending)
    assert pipeline.gate.active
    pipeline.gate.process(np.zeros(3200, dtype=np.float32), [])
    pipeline.wait(lambda: pipeline.backend.partial_sent)  # ASR receives audio before source selection
    pipeline.end()
    pipeline.wait(lambda: bool(pipeline.backend.finished))
    assert not pipeline.gate.active
    assert pipeline.audio  # the completed sentence was captured independently
    assert not pipeline.peer.commits
    assert pipeline.host._inline_input._source_pending
    pipeline.host._inline_input._source_activation.event.emit({'event':'selected'})
    pipeline.settled(1)
    assert pipeline.peer.commits[0][1] == "切换期间的话"
