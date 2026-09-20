from types import SimpleNamespace

import pytest

from proximic_ring.gesture_settings import BoundGestureEvent, GestureBindings, GESTURE_LABELS
from proximic_ring.voice_actions import voice_action_for_gesture
from test_interaction_controls import _controller, _close


@pytest.fixture(autouse=True)
def isolate_settings(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    import proximic_ring.ui.controller as module
    monkeypatch.setattr(module, "QSettings", lambda *args: QSettings(
        str(tmp_path / "gestures.ini"), QSettings.IniFormat))


def test_defaults_match_sdk_and_current_assignments():
    from proximic_ring.host_gestures import GESTURE_NAMES

    assert set(GESTURE_LABELS) == set(GESTURE_NAMES) - {"empty"}
    bindings = GestureBindings()
    assert bindings.confirm == ("tap", "")
    assert bindings.undo == ("swipe-left", "")
    assert bindings.switch_mode == ("swipe-right", "")
    assert GestureBindings.from_json(bindings.to_json()) == bindings


@pytest.mark.parametrize("changes", [
    {"confirm": ("tap", "tap")},
    {"confirm": ("tap", "swipe-left")},
    {"undo": ("tap", "")},
    {"switch_mode": ("swipe-left", "")},
    {"confirm": ("", "")},
    {"undo": ("snap", "", "")},
    {"undo": ("empty", "")},
    {"confirm": ("pinch", "")},
])
def test_invalid_or_conflicting_assignments_are_rejected(changes):
    with pytest.raises(ValueError):
        GestureBindings(**changes)


def test_custom_cancel_undo_and_conversion_keep_action_priority():
    bindings = GestureBindings(confirm=("swipe-up", "swipe-down"), undo=("tap", "snap"), switch_mode=("swipe-left", "swipe-right"))
    for name in bindings.undo:
        assert voice_action_for_gesture(name, interaction_active=True, bindings=bindings) == "cancel"
        assert voice_action_for_gesture(name, interaction_active=True, undo_active=True, bindings=bindings) == "undo"
    for name in bindings.switch_mode:
        assert voice_action_for_gesture(name, correction_active=True, bindings=bindings) == "switch_mode"
    for name in bindings.confirm:
        assert voice_action_for_gesture(name, interaction_active=True, undo_active=True, correction_active=True, bindings=bindings) is None


def test_settings_save_restore_conflicts_and_immediate_dispatch(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    restarted = None
    try:
        controller._runtime_active = controller._connected = True
        # Free and reassign tap; the runtime endpoint and GUI share the snapshot.
        assert controller.setGestureBinding("confirm", 0, "snap")
        assert controller.setGestureBinding("undo", 0, "tap")
        assert controller.setGestureBinding("switch_mode", 1, "swipe-left")
        assert controller.confirmGestureHint == "弹指（snap）"
        saved = controller._settings.value("input/gestureBindings")
        snapshot = controller._gesture_bindings
        assert not controller.setGestureBinding("switch_mode", 0, "tap")
        assert "不能重复分配" in controller.gestureSettingsError
        assert controller._gesture_bindings is snapshot
        assert controller._settings.value("input/gestureBindings") == saved
        assert not controller.setGestureBinding("confirm", 0, "")
        assert "至少保留一个" in controller.gestureSettingsError
        assert "tap" not in [row["value"] for row in controller.gestureOptionsForSlot("confirm", 1)]
        assert "tap" in [row["value"] for row in controller.gestureOptionsForSlot("undo", 0)]

        calls = []
        monkeypatch.setattr(controller, "cancelCurrentUtterance", lambda: calls.append("cancel"))
        controller._set_interaction_state("listening")
        controller._gestureRecognized.emit(SimpleNamespace(name="tap"), controller._disconnect_event)
        controller._gestureRecognized.emit(SimpleNamespace(name="swipe-down"), controller._disconnect_event)
        controller._gestureRecognized.emit(SimpleNamespace(name="snap"), controller._disconnect_event)
        controller.dispatchVoiceAction("cancel")
        assert calls == ["cancel", "cancel"]  # Down is no longer an undo alias.
        assert not controller.setGestureBinding("confirm", 1, "swipe-up")  # Reserved by app send.
        assert controller._app_gestures.setInputSourceGesture("swipe-down")
        assert not controller.setGestureBinding("confirm", 1, "swipe-down")  # Reserved only after user binding.
        assert controller._app_gestures.setInputSourceGesture("")
        assert controller.setGestureBinding("confirm", 1, "swipe-down")
        assert "下滑" in controller.transcriptText

        restarted = _controller(tmp_path, monkeypatch)
        assert restarted.gestureBindings == controller.gestureBindings
        restarted.resetGestureBindings()
        assert restarted.gestureBindings == GestureBindings().as_dict()
        assert restarted.gestureSettingsError == ""
    finally:
        if restarted:
            _close(restarted)
        _close(controller)


def test_queued_gesture_cannot_change_action_after_settings_change(tmp_path, monkeypatch):
    controller = _controller(tmp_path, monkeypatch)
    try:
        controller._runtime_active = controller._connected = True
        controller._set_interaction_state("listening")
        old = controller._gesture_bindings
        calls = []
        monkeypatch.setattr(controller, "cancelCurrentUtterance", lambda: calls.append("cancel"))
        assert controller.setGestureBinding("undo", 0, "snap")
        controller._gestureRecognized.emit(BoundGestureEvent(SimpleNamespace(name="snap"), old), controller._disconnect_event)
        assert calls == []
        controller._gestureRecognized.emit(BoundGestureEvent(SimpleNamespace(name="snap"), controller._gesture_bindings), controller._disconnect_event)
        assert calls == ["cancel"]
    finally:
        _close(controller)


def test_corrupt_saved_configuration_recovers_as_one_consistent_default(tmp_path, monkeypatch):
    first = _controller(tmp_path, monkeypatch)
    first._settings.setValue("input/gestureBindings", '{"confirm":["tap",""],"undo":["tap",""],"switch_mode":["snap",""]}')
    _close(first)
    recovered = _controller(tmp_path, monkeypatch)
    try:
        assert recovered.gestureBindings == GestureBindings().as_dict()
        assert "恢复默认" in recovered.gestureSettingsError
        assert GestureBindings.from_json(recovered._settings.value("input/gestureBindings")) == GestureBindings()
    finally:
        _close(recovered)
