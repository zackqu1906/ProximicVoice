from dataclasses import replace
import json
import subprocess
import time

import pytest

from proximic_ring import input_source_switch as switch
from proximic_ring.gesture_settings import GestureBindings
from test_app_gestures import route


def test_helper_selects_exact_mode_and_checks_result(tmp_path, monkeypatch):
    helper = tmp_path / 'InputMethodAdmin'
    helper.touch()
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, json.dumps({'sources': [
            {'id': switch.BUNDLE_ID + '.dictation', 'selected': True}]}), '')
    monkeypatch.setattr(switch.subprocess, 'run', run)
    switch.select_voice_input_source(123, helper=helper)
    assert calls[0][0] == [str(helper), 'select', '123']
    assert calls[0][1]['timeout'] == 3


@pytest.mark.parametrize('code,output,error', [
    (1, '', '请先安装输入法'), (0, '{}', ''),
    (0, json.dumps({'sources': [{'id': 'other', 'selected': True}]}), '')])
def test_failed_or_wrong_selection_is_not_success(tmp_path, monkeypatch, code, output, error):
    helper = tmp_path / 'InputMethodAdmin'
    helper.touch()
    monkeypatch.setattr(switch.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, code, output, error))
    with pytest.raises(RuntimeError):
        switch.select_voice_input_source(123, helper=helper)


def wait_result(service):
    from PySide6.QtCore import QCoreApplication
    deadline = time.monotonic() + 2
    while service._source_busy and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.005)
    assert not service._source_busy


def prepare(monkeypatch, service):
    assert service.setInputSourceGesture("swipe-down")
    import proximic_ring.ui.app_gesture_controller as module
    calls = []
    monkeypatch.setattr(module, 'foreground_pid', lambda: 123)
    monkeypatch.setattr(module, 'select_voice_input_source', lambda pid: calls.append(pid))
    return calls


def test_user_bound_down_switches_with_asr_paused_and_no_supported_app(route, monkeypatch):
    controller, service, inline, backend, keys, messages, emit = route
    calls = prepare(monkeypatch, service)
    backend.target = None  # global action, independent of app profiles or AX
    assert service.inputSourceGesture == 'swipe-down'
    assert not controller._recognition_enabled
    emit('swipe-down')
    wait_result(service)
    assert calls == [123]
    assert not keys and not messages
    assert service.notice == '已切换到 ProxiMic Voice'
    emit('swipe-down')  # duplicate model event, no second dispatch
    assert calls == [123]


@pytest.mark.parametrize('phase', ['starting', 'listening', 'finishing', 'editing', 'applying'])
def test_switch_does_not_gate_on_cached_sentence_state(route, monkeypatch, phase):
    controller, service, inline, backend, keys, messages, emit = route
    calls = prepare(monkeypatch, service)
    inline._view['phase'] = phase
    monkeypatch.setattr(service, 'phase_state', lambda: pytest.fail('input-source switch must not inspect sentence state'))
    emit('swipe-down')
    wait_result(service)
    assert calls == [123]
    assert not messages and not keys
    assert inline._view['phase'] == phase  # selecting is not cancellation or restart
    assert service.notice == '已切换到 ProxiMic Voice'


@pytest.mark.parametrize('busy', ['writing', 'send', 'audio', 'model', 'undo'])
def test_pending_operations_do_not_block_source_selection(route, monkeypatch, busy):
    controller, service, inline, backend, keys, messages, emit = route
    calls = prepare(monkeypatch, service)
    if busy == 'writing':
        inline._view['awaiting_readback'] = True
    elif busy == 'send':
        service._pending = object()
    elif busy == 'audio':
        controller._utterance_active = True
    elif busy == 'model':
        controller._inline_requests['old'] = object()
    else:
        controller._undo_running = True
    emit('swipe-down')
    wait_result(service)
    assert calls == [123] and not keys and not messages


def test_binding_conflicts_are_rejected_in_both_directions(route):
    controller, service, *_ = route
    assert service.setInputSourceGesture('swipe-down')
    assert not service.setInputSourceGesture('tap')
    assert not service.setInputSourceGesture('swipe-up')
    assert not controller.setGestureBinding('undo', 1, 'swipe-down')
    assert not service.setBinding('codex', 'send', 'swipe-down', 'Return', True)
    assert service.setInputSourceGesture('')
    assert controller.setGestureBinding('undo', 1, 'swipe-down')
    assert not service.setInputSourceGesture('swipe-down')
    assert service.setInputSourceGesture('clench')
    assert controller._settings.value('gestures/inputSourceGesture') == 'clench'


def test_stale_and_disconnected_gestures_do_not_select(route, monkeypatch):
    controller, service, inline, backend, keys, messages, emit = route
    calls = prepare(monkeypatch, service)
    from types import SimpleNamespace
    event = service.envelope(SimpleNamespace(name='swipe-down'))
    controller._apply_gesture(replace(event, created=event.created - 2), controller._disconnect_event)
    assert service.setInputSourceGesture('clench')
    controller._apply_gesture(event, controller._disconnect_event)
    controller._connected = False
    emit('clench')
    assert not calls


def test_error_feedback_has_no_false_success(route, monkeypatch):
    controller, service, *_rest, emit = route
    prepare(monkeypatch, service)
    import proximic_ring.ui.app_gesture_controller as module
    def fail(pid):
        raise RuntimeError('未找到 ProxiMic Voice，请先安装')
    monkeypatch.setattr(module, 'select_voice_input_source', fail)
    emit('swipe-down')
    wait_result(service)
    assert service.notice == '未找到 ProxiMic Voice，请先安装'


def test_initial_selection_preserves_existing_custom_bindings(route):
    controller, service, *_ = route
    from proximic_ring.ui.app_gesture_controller import AppGestureController
    controller._settings.remove('gestures/inputSourceGesture')
    controller._gesture_bindings = GestureBindings(undo=('swipe-down', ''))
    second = AppGestureController(controller)
    try:
        assert second.inputSourceGesture == ''
        assert controller._gesture_bindings.undo == ('swipe-down', '')
        second.setInputSourceGesture('')
    finally:
        second.close()
    third = AppGestureController(controller)
    try:
        assert third.inputSourceGesture == ''  # explicit off survives restart
    finally:
        third.close()


def test_default_unbound_never_selects_or_starts_audio(route, monkeypatch):
    controller, service, inline, backend, keys, messages, emit = route
    import proximic_ring.ui.app_gesture_controller as module
    calls = []
    monkeypatch.setattr(module, 'select_voice_input_source', lambda pid: calls.append(pid))
    assert service.inputSourceGesture == ''
    assert controller._settings.value('gestures/inputSourceGesture') == ''
    emit('swipe-down')
    assert not calls and not messages and not keys
    assert not controller._utterance_active and not controller._recognition_enabled


def test_user_selected_binding_survives_restart(route):
    controller, service, *_ = route
    from proximic_ring.ui.app_gesture_controller import AppGestureController
    assert service.setInputSourceGesture('swipe-down')
    second = AppGestureController(controller)
    try:
        assert second.inputSourceGesture == 'swipe-down'
    finally:
        second.close()
