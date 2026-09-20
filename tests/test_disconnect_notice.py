from types import SimpleNamespace
import threading

import pytest

from test_interaction_controls import _controller, _close


@pytest.fixture
def controller(tmp_path, monkeypatch):
    value = _controller(tmp_path, monkeypatch)
    value._device_name = "Ringo2CC7"
    value._runtime_active = value._runtime_had_connection = value._connected = True
    value._recognition_enabled = True
    value._recognition_event.set()
    yield value
    _close(value)


@pytest.mark.parametrize("ending", ["stopping", "disconnected", "finished_error", "finished_clean"])
@pytest.mark.parametrize("interaction", ["listening", "processing", "paused"])
def test_unexpected_stop_alerts_once_and_retires_all_interaction(controller, monkeypatch, ending, interaction):
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import InputModeRoutingResult

    cancelled = []
    monkeypatch.setattr(controller._text_processing_worker, "cancel_request", cancelled.append)
    controller._pending_text_requests.add(101)
    controller._pending_mode_routes.add(102)
    controller._manual_association_watch = SimpleNamespace()
    controller._manual_association_timer.start()
    controller._transcript_visible = True
    controller._utterance_active = interaction == "listening"
    controller._set_interaction_state(interaction if interaction != "paused" else "idle")
    if interaction == "paused":
        controller.pauseRecognition()
    changes = []
    controller.ringDisconnectNoticeChanged.connect(lambda: changes.append(controller.ringDisconnectNoticeVisible))
    # The shared event is also set automatically, without a Disconnect click.
    controller._disconnect_event.set()
    if ending == "stopping":
        controller._apply_runtime_stopping(controller._disconnect_event)
    elif ending == "disconnected":
        controller._apply_runtime_disconnected()
    else:
        controller._apply_runtime_finished("BLE physically lost" if ending == "finished_error" else "")

    assert controller.ringDisconnectNoticeVisible
    assert controller.ringDisconnectNoticeTitle == "Ring 已断开连接"
    assert controller.ringDisconnectNoticeDevice == "Ringo2CC7"
    assert sorted(cancelled) == [101, 102]
    assert not controller.connected
    assert not controller.recognitionEnabled
    assert not controller._recognition_event.is_set()
    assert not controller._utterance_active
    assert not controller.interactionCanCancel
    assert not controller.nativeUndoAvailable
    assert not controller.modeCorrectionHotkeyAvailable
    assert not controller.transcriptVisible
    assert controller._manual_association_watch is None
    assert not controller._manual_association_timer.isActive()
    assert controller.interactionState == "idle"

    # Queued ASR/model callbacks and hotkeys cannot resurrect the interaction.
    controller._apply_runtime_status("[ASR] START t=3.000s")
    controller._apply_runtime_update("迟到的语音", True, "", 5)
    controller._apply_text_processed(TextProcessingResult(101, 5, "dictation", "旧语音", "迟到结果", 0.2, True))
    controller._apply_input_mode_routed(InputModeRoutingResult(102, 5, "旧语音", "dictation", 0.1))
    controller._apply_runtime_started()
    controller._apply_runtime_connected()
    controller.dispatchVoiceAction("switch_mode")
    controller.startRecognition()
    assert not controller.connected
    assert not controller.transcriptVisible
    assert controller.interactionState == "idle"

    controller.dismissRingDisconnectNotice()
    controller._apply_runtime_disconnected()
    controller._apply_runtime_finished("cleanup error")
    assert changes == [True, False]
    assert not controller.ringDisconnectNoticeVisible


@pytest.mark.parametrize("quitting", [False, True])
def test_explicit_disconnect_or_quit_remains_quiet(controller, quitting):
    controller._worker = SimpleNamespace(is_alive=lambda: True)
    controller._quitting = quitting
    controller.disconnectDevice()
    controller._apply_runtime_stopping(controller._disconnect_event)
    controller._apply_runtime_disconnected()
    controller._apply_runtime_finished("cleanup error")
    assert controller._disconnect_requested_by_user
    assert not controller.ringDisconnectNoticeVisible
    assert not controller.connected


def test_microphone_failure_notice_identifies_audio_device_and_stops_everything(controller):
    controller._audio_source = "microphone"
    controller._disconnect_event.set()
    controller._apply_runtime_status("[AUDIO_INPUT_ERROR] DJI unplugged")
    controller._apply_runtime_stopping(controller._disconnect_event)
    assert controller.ringDisconnectNoticeVisible
    assert controller.ringDisconnectNoticeTitle == "麦克风连接中断"
    assert controller.ringDisconnectNoticeDevice == "电脑麦克风（DJI）"
    assert not controller.recognitionEnabled and not controller.connected
    assert not controller.recognitionEnabled


def test_click_after_automatic_stop_cannot_suppress_alert(controller):
    controller._worker = SimpleNamespace(is_alive=lambda: True)
    controller._disconnect_event.set()
    controller.disconnectDevice()
    assert not controller._disconnect_requested_by_user
    assert controller.ringDisconnectNoticeVisible


def test_connection_failure_before_ready_still_alerts(controller):
    controller._connected = controller._runtime_had_connection = False
    controller._apply_runtime_finished("NUS service unavailable")
    assert controller.ringDisconnectNoticeVisible
    assert controller.ringDisconnectNoticeTitle == "Ring 连接中断"


def test_new_connection_resets_notice_and_ignores_old_stop(controller, monkeypatch):
    import proximic_ring.ui.controller as module
    from PySide6.QtCore import QCoreApplication

    controller._apply_runtime_finished("old connection lost")
    old_connection = controller._disconnect_event
    started = threading.Event()
    release = threading.Event()

    class Runtime:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, stop, recognition, **callbacks):
            callbacks["on_connected"]()
            started.set()
            assert release.wait(3)
            stop.set()
            callbacks["on_stopping"]()
            callbacks["on_disconnected"]()

    monkeypatch.setattr(module, "RecognitionRuntime", Runtime)
    controller._disconnect_requested_by_user = True
    worker = None
    try:
        controller._start_selected_device()
        worker = controller._worker
        assert started.wait(2)
        QCoreApplication.processEvents()
        assert not controller.ringDisconnectNoticeVisible
        assert not controller._disconnect_requested_by_user
        assert controller.connected
        controller._apply_runtime_stopping(old_connection)
        assert controller.connected
        assert not controller.ringDisconnectNoticeVisible
    finally:
        release.set()
        if worker is not None:
            worker.join(timeout=3)
        QCoreApplication.processEvents()
    assert controller.ringDisconnectNoticeVisible
    assert not controller.connected


def test_pausing_recognition_does_not_show_disconnect_notice(controller):
    controller.pauseRecognition()
    assert controller.connected
    assert not controller.ringDisconnectNoticeVisible


def test_macos_notice_is_visible_on_other_spaces_without_taking_keyboard_focus(monkeypatch):
    import sys
    from proximic_ring.ui.notifications import _show_on_macos_spaces

    events = []
    native = SimpleNamespace(
        collectionBehavior=lambda: 2 | 64,
        setCollectionBehavior_=lambda value: events.append(("spaces", value)),
        orderFrontRegardless=lambda: events.append("front"),
    )

    def objc_object(*, c_void_p):
        assert c_void_p.value == 123
        return SimpleNamespace(window=lambda: native)

    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(objc_object=objc_object))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWindowCollectionBehaviorCanJoinAllSpaces=1,
        NSWindowCollectionBehaviorFullScreenAuxiliary=256,
        NSWindowCollectionBehaviorMoveToActiveSpace=2,
    ))
    _show_on_macos_spaces(SimpleNamespace(winId=lambda: 123))
    assert events == [("spaces", 1 | 64 | 256), "front"]
