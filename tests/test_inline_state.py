"""Metadata and interruption routing without a native client or target app."""
import json

import pytest
from PySide6.QtCore import QCoreApplication

from proximic_ring.ui.inline_controller import InlineInputController


class FakeBridge:
    def __init__(self, callback):
        self.callback = callback
        self.messages = []
        self.accept = True

    def start(self):
        pass

    def send(self, message):
        self.messages.append(message)
        return self.accept

    def close(self):
        pass


@pytest.fixture
def component(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = InlineInputController(enabled=True, bridge_factory=FakeBridge)
    monkeypatch.setattr(controller, "_foreground_identity", lambda: None)
    monkeypatch.setattr(controller, "_warm_input_method", lambda: None)
    events = []
    controller.diagnostic.connect(events.append)
    controller._received.emit({"type": "connected", "epoch": "epoch-one"})
    controller._received.emit({
        "type": "state", "epoch": "epoch-one", "client_id": "client-one",
        "utterance_id": "", "ready": True, "phase": "idle",
        "application": "test.editor", "original": "private editor context",
        "selection": [22, 0], "context_complete": True,
    })
    yield controller, events
    controller.close()
    app.processEvents()


def receive(controller, kind="state", **fields):
    controller._received.emit({
        "type": kind, "epoch": controller._epoch,
        "client_id": controller._client_id, "utterance_id": controller._utterance_id,
        **fields,
    })


def test_multi_undo_setting_survives_shortcut_updates_and_reconnect(component):
    c, _ = component
    c.configure("F8", multi_undo_enabled=True)
    c.configure("F9")
    assert c._bridge.messages[-1]["multi_undo_enabled"] is True
    c._accept({"type": "connected", "epoch": "new"})
    assert c._bridge.messages[-1]["multi_undo_enabled"] is True


def test_undo_history_remains_accessible_after_current_sentence_is_undone(component):
    c, _ = component
    c.configure("F8", multi_undo_enabled=True)
    c.begin()
    receive(c, phase="undone", ready=True, undo_history_depth=2)
    assert c.canUndo and not c.active
    c.cancel()
    assert c._bridge.messages[-1]["type"] == "cancel"
    c.configure("F8", multi_undo_enabled=False)
    before = len(c._bridge.messages)
    c.cancel()
    assert not c.canUndo and len(c._bridge.messages) == before


def test_history_settlement_records_original_audio_session_and_deduplicates(component):
    c, _ = component
    c.begin(); c.bind_session(11)
    old = c._utterance_id
    c.begin(); c.bind_session(12)
    received = []
    c.settled.connect(lambda phase, text: received.append((phase, c.settled_session_id)))
    for revision in (2, 2, 4):
        receive(c, "settled", phase="undone", revision=revision, source_utterance_id=old)
    assert received == [("undone", 11), ("undone", 11)]
    assert c._session_id == 12


@pytest.mark.parametrize("application", ["com.openai.codex", "com.microsoft.VSCode", "com.apple.TextEdit", "org.example.Editor"])
def test_default_undo_command_is_fenced_by_current_app_and_transaction(component, monkeypatch, application):
    from proximic_ring import wechat_native_keys
    c, _ = component
    calls = []
    monkeypatch.setattr(wechat_native_keys, "send_input_method_key", lambda *a: calls.append(a))
    c.begin()
    receive(c, phase="dictated", ready=True, application=application)
    receive(c, "wechat_key", request_id="delete-one", command="delete", count=0,
            event_tag=123, application=application)
    receive(c, "wechat_key", request_id="delete-one", command="delete", count=0,
            event_tag=123, application=application)
    assert calls == [(application, "delete", 0, 123)]
    receive(c, "wechat_key", request_id="wrong-app", command="delete", count=0,
            event_tag=123, application="other.editor")
    assert calls == [(application, "delete", 0, 123)]


@pytest.mark.parametrize("application", ["com.microsoft.VSCode", "com.apple.TextEdit", "org.example.Editor", None])
def test_startup_foreground_identity_is_not_limited_to_codex_or_wechat(component, monkeypatch, application):
    import sys
    from types import SimpleNamespace
    import proximic_ring.ui.inline_controller as module
    c, _ = component
    app = SimpleNamespace(bundleIdentifier=lambda: application, processIdentifier=lambda: 42)
    workspace = SimpleNamespace(frontmostApplication=lambda: app)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)))
    actual = InlineInputController._foreground_identity(c)
    assert actual == ((application, 42) if application else None)


@pytest.mark.parametrize("command,count", [("caret_to_end", 0), ("caret_backward", 2), ("caret_forward", 2), ("select_all", 0), ("select_to_start", 0), ("caret_from_end", 2)])
@pytest.mark.parametrize("application", ["com.openai.codex", "com.microsoft.VSCode", "org.example.Editor"])
def test_default_caret_command_requires_current_edit_and_is_not_replayed(component, monkeypatch, command, count, application):
    from proximic_ring import wechat_native_keys
    c, _ = component
    calls = []
    monkeypatch.setattr(wechat_native_keys, "send_input_method_key", lambda *a: calls.append(a))
    c.begin()
    receive(c, phase="listening", ready=True, application=application)
    fields = dict(request_id="caret-one", command=command, count=count, event_tag=123, application=application)
    receive(c, "wechat_key", **fields)
    assert not calls
    receive(c, phase="editing", ready=True, application=application)
    receive(c, "wechat_key", **{**fields, "client_id": "old-field"})
    assert not calls
    receive(c, "wechat_key", **fields)
    receive(c, "wechat_key", **fields)
    assert calls == [(application, command, count, 123)]


@pytest.mark.parametrize("application", ["com.openai.codex", "com.microsoft.VSCode", "org.example.Editor"])
def test_first_tap_waits_for_initial_connection_and_flushes_only_latest_asr_after_begin_ack(component, monkeypatch, application):
    c, events = component
    c._accept({"type": "disconnected", "epoch": c._epoch})
    monkeypatch.setattr(c, "_foreground_identity", lambda: (application, 123))
    assert c.begin() and c.active and c._reply_stage == "ready"
    utterance = c._utterance_id
    c.bind_session(17)
    assert not c.update("old session", True, 16)
    assert c.update("first", False, 17)
    assert c.update("whole sentence", True, 17)
    c._accept({"type": "connected", "epoch": "new-epoch"})
    assert c._utterance_id == utterance and c._session_id == 17
    receive(c, phase="idle", ready=False, client_id="", utterance_id="")
    assert c.active and c._reply_stage == "ready"
    receive(c, phase="idle", ready=True, client_id="codex", utterance_id="", application=application)
    assert c._reply_stage == "begin" and c._utterance_id == utterance
    assert c._bridge.messages[-1]["type"] == "begin"
    receive(c, phase="listening", ready=True)
    assert c._bridge.messages[-1]["type"] == "update"
    assert c._bridge.messages[-1]["text"] == "whole sentence"
    assert c._reply_stage == "final"
    assert not any(e.get("reason") == "input_method_not_ready" for e in events)


@pytest.mark.parametrize("action", ["cancel", "timeout", "switch_app", "reset"])
def test_pending_first_tap_never_writes_after_cancellation_or_target_change(component, monkeypatch, action):
    c, _ = component
    receive(c, ready=False, phase="idle", client_id="", utterance_id="")
    monkeypatch.setattr(c, "_foreground_identity", lambda: ("com.openai.codex", 123))
    assert c.begin()
    c.bind_session(2)
    c.update("buffered", False, 2)
    count = len(c._bridge.messages)
    if action == "cancel": c.cancel()
    elif action == "timeout": c._reply_timed_out()
    elif action == "reset": c.reset()
    else: monkeypatch.setattr(c, "_foreground_identity", lambda: ("com.tencent.xinWeChat", 456))
    receive(c, phase="idle", ready=True, client_id="codex", utterance_id="", application="com.openai.codex")
    assert not c._startup_buffering
    assert not any(m["type"] in {"begin", "update"} for m in c._bridge.messages[count:])


@pytest.mark.parametrize("stale_ready", [False, True])
def test_first_tap_ignores_previous_window_state_and_keeps_same_audio(component, monkeypatch, stale_ready):
    c, _ = component
    receive(c, ready=stale_ready, phase="idle", client_id="previous-window", utterance_id="",
            application="test.previous")
    monkeypatch.setattr(c, "_foreground_identity", lambda: ("com.openai.codex", 123))
    before = len(c._bridge.messages)
    assert c.begin() and c._reply_stage == "ready"
    utterance = c._utterance_id
    c.bind_session(8)
    c.update("第一句话", False, 8)
    receive(c, ready=True, phase="idle", client_id="previous-window", utterance_id="",
            application="test.previous")
    assert c.active and c._startup_buffering and c._utterance_id == utterance
    assert not any(m["type"] in {"begin", "update"} for m in c._bridge.messages[before:])
    receive(c, ready=True, phase="idle", client_id="current-window", utterance_id="",
            application="com.openai.codex")
    assert c._bridge.messages[-1]["type"] == "begin"
    assert c._bridge.messages[-1]["client_id"] == "current-window"
    receive(c, phase="listening", ready=True)
    assert c._bridge.messages[-1]["text"] == "第一句话"
    assert c._utterance_id == utterance


def test_wechat_keys_require_current_transaction_and_are_not_replayed(component, monkeypatch):
    from proximic_ring import wechat_native_keys
    controller, _ = component
    calls = []
    monkeypatch.setattr(wechat_native_keys, "send_wechat_key", lambda *args: calls.append(args))
    assert controller.begin()
    receive(controller, phase="editing", ready=True, application="com.tencent.xinWeChat")
    fields = dict(request_id="key-one", command="select_all", count=0, event_tag=123,
                  application="com.tencent.xinWeChat")
    receive(controller, "wechat_key", **fields)
    receive(controller, "wechat_key", **fields)
    receive(controller, "wechat_key", **{**fields, "request_id": "stale", "epoch": "old"})
    receive(controller, "wechat_key", **{**fields, "request_id": "stale-client", "client_id": "other"})
    assert calls == [("select_all", 0, 123)]
    replies = [m for m in controller._bridge.messages if m["type"] == "wechat_key_result"]
    assert len(replies) == 1 and replies[0]["error"] == ""


def test_native_key_request_cannot_activate_compatibility_in_other_apps(component, monkeypatch):
    from proximic_ring import wechat_native_keys
    controller, _ = component
    calls = []
    monkeypatch.setattr(wechat_native_keys, "send_wechat_key", lambda *args: calls.append(args))
    assert controller.begin()
    receive(controller, phase="editing", ready=True, application="com.openai.codex")
    receive(controller, "wechat_key", request_id="key-one", command="undo", count=0,
            event_tag=123, application="com.tencent.xinWeChat")
    assert calls == []


def test_diagnostics_have_only_metadata_and_deduplicate_state(component):
    controller, events = component
    assert controller.begin()
    receive(controller, phase="listening", ready=True, revision=1,
            raw="private speech", original="private editor context",
            text="private response", request_id="first",
            lifecycle_event="begin", has_composition=True,
            awaiting_readback=True, readback_stage="composition",
            readback_diagnostics={"expected_characters": 3, "observed_characters": 0,
                                  "text_matches": False, "observed_selection": [0, 0],
                                  "raw": "private ASR text", "expected_marked": ["private", 3]},
            client_read_diagnostics={"query": "probe", "returned_characters": 0, "text_queried": True,
                                     "actual_range": [0, 0], "raw": "private editor text"},
            handoff_state="ready", handoff_diagnostics={"action": "ready", "writes": 1,
                "marked_characters": -1, "selection": [0, 0], "document_access": True,
                "raw": "private previous input method text"})
    first_count = len(events)
    receive(controller, phase="listening", ready=True, revision=1,
            raw="private speech", original="different private context",
            request_id="second", lifecycle_event="begin", has_composition=True)
    assert len(events) == first_count
    event = events[-1]
    assert event["characters"] == len("private speech")
    assert event["lifecycle_event"] == "begin"
    assert event["has_composition"] is True
    assert event["awaiting_readback"] is True
    assert event["readback_stage"] == "composition"
    assert event["readback_diagnostics"] == {
        "expected_characters": 3, "observed_characters": 0,
        "text_matches": False, "observed_selection": [0, 0],
    }
    assert event["client_read_diagnostics"] == {
        "query": "probe", "returned_characters": 0, "actual_range": [0, 0], "text_queried": True,
    }
    assert event["handoff_state"] == "ready"
    assert event["handoff_diagnostics"] == {"action": "ready", "writes": 1,
        "marked_characters": -1, "selection": [0, 0], "document_access": True}
    assert set(event) == {
        "type", "phase", "ready", "application", "client_id", "utterance_id",
        "revision", "characters", "error", "reason", "lifecycle_event", "has_composition",
        "awaiting_readback", "readback_stage",
        "readback_diagnostics",
        "client_read_diagnostics",
        "handoff_state", "handoff_diagnostics",
    }
    assert "private" not in json.dumps(events)


def test_updates_diagnose_first_nonempty_and_final_once_per_sentence(component):
    controller, events = component
    assert controller.begin()
    assert controller.update("", False, 3)
    assert controller.update("secret", False, 3)
    assert controller.update("secret words", False, 3)
    assert controller.update("secret words final", True, 3)
    assert not controller.update("late final", True, 3)
    updates = [event for event in events if event["type"] == "update"]
    assert [(event["reason"], event["characters"]) for event in updates] == [
        ("first_partial", 6), ("final", 18),
    ]
    assert "secret" not in json.dumps(events)
    assert controller.begin()
    assert controller.update("new", False, 4)
    assert events[-1]["reason"] == "first_partial"


def test_audio_start_binds_session_before_first_result(component):
    controller, _ = component
    assert controller.begin()
    controller.bind_session(12)
    assert not controller.update("late old result", True, 11)
    assert controller.update("current result", False, 12)


@pytest.mark.parametrize("stage", ["begin", "final"])
def test_missing_native_ack_fails_once_and_drops_late_transaction(component, stage):
    controller, _ = component
    settlements = []
    controller.settled.connect(lambda phase, text: settlements.append(phase))
    assert controller.begin()
    old_id = controller._utterance_id
    if stage == "final":
        receive(controller, ready=True, phase="listening")
        assert controller.update("", True, 1)
    assert controller._reply_stage == stage
    controller._reply_timed_out()
    controller._reply_timed_out()
    assert settlements == ["error"]
    assert controller._bridge.messages[-1]["type"] == "reset"
    receive(controller, ready=True, phase="dictated", utterance_id=old_id)
    assert controller._view["phase"] == "error"


def test_failed_final_send_settles_without_waiting_forever(component):
    controller, _ = component
    settlements = []
    controller.settled.connect(lambda phase, text: settlements.append(phase))
    assert controller.begin()
    old_id = controller._utterance_id
    controller._bridge.accept = False
    assert not controller.update("", True, 1)
    assert settlements == ["error"]
    assert not controller.active
    edits = []
    controller.editRequested.connect(lambda *args: edits.append(args))
    receive(controller, phase="editing", ready=True, utterance_id=old_id)
    receive(controller, "edit_requested", utterance_id=old_id, instruction="late")
    assert not controller.active
    assert not edits


def test_old_idle_ping_cannot_retire_pending_begin(component):
    controller, _ = component
    assert controller.begin()
    current_id = controller._utterance_id
    receive(controller, phase="idle", ready=True, utterance_id="", request_id="old-ping")
    assert controller._utterance_id == current_id
    assert controller._reply_stage == "begin"
    receive(controller, phase="listening", ready=True)
    assert controller.active
    assert controller._reply_stage == ""


def test_new_connection_retires_pending_sentence_before_late_disconnect(component):
    controller, _ = component
    assert controller.begin()
    assert controller.update("", True, 4)
    settlements = []
    controller.settled.connect(lambda phase, text: settlements.append(phase))
    controller._accept({"type": "connected", "epoch": "replacement"})
    controller._accept({"type": "disconnected", "epoch": "epoch-one"})
    assert settlements == ["error"]
    assert controller._reply_stage == ""
    assert controller._epoch == "replacement"
    assert not controller._utterance_id


def test_interrupt_reason_is_available_before_cancellation_signal(component):
    controller, events = component
    assert controller.begin()
    seen = []
    controller.interrupted.connect(lambda: seen.append(controller.error))
    receive(controller, "interrupted", reason="应用结束了当前输入组合",
            lifecycle_event="commit_composition", has_composition=True,
            raw="private speech", original="private editor context")
    assert seen == ["应用结束了当前输入组合"]
    assert controller.error == seen[0]
    assert events[-1]["type"] == "interrupted"
    assert events[-1]["reason"] == seen[0]
    assert events[-1]["lifecycle_event"] == "commit_composition"
    assert events[-1]["has_composition"] is True
    assert "private" not in json.dumps(events)


@pytest.mark.parametrize("changed", [
    {"epoch": "stale-epoch"}, {"client_id": "stale-client"},
    {"utterance_id": "stale-utterance"},
])
def test_stale_interruption_cannot_change_error_or_diagnostics(component, changed):
    controller, events = component
    assert controller.begin()
    seen = []
    controller.interrupted.connect(lambda: seen.append(controller.error))
    count = len(events)
    receive(controller, "interrupted", reason="stale reason", **changed)
    assert seen == []
    assert controller.error == ""
    assert len(events) == count


def test_not_ready_state_has_reason_before_cancellation(component):
    controller, events = component
    assert controller.begin()
    seen = []
    controller.interrupted.connect(lambda: seen.append(controller.error))
    receive(controller, ready=False, phase="idle", utterance_id="", client_id="")
    assert seen == [controller.connectionStatus]
    assert controller.error
    assert events[-1]["error"] == controller.connectionStatus
    assert "original" not in controller._view


def test_begin_failure_and_disconnect_are_observable(component):
    controller, events = component
    controller._bridge.accept = False
    assert not controller.begin()
    assert events[-1]["type"] == "begin"
    assert events[-1]["reason"] == "send_failed"
    controller._bridge.accept = True
    assert controller.begin()
    receive(controller, "disconnected")
    assert events[-1]["type"] == "disconnected"
    assert not events[-1]["ready"]
    assert events[-1]["error"]
    assert not controller.begin()
    assert events[-1]["reason"] == "input_method_not_ready"


def test_context_response_does_not_interrupt_current_session(component):
    controller, events = component
    assert controller.begin()
    seen = []
    controller.interrupted.connect(lambda: seen.append(controller.error))
    receive(controller, ready=True, phase="listening", revision=0, raw="")
    receive(controller, ready=True, phase="listening", revision=0, raw="",
            request_id="context-refresh", original="private editor context")
    assert controller.active
    assert seen == []


def test_changed_client_reports_takeover_and_ignores_late_old_state(component):
    controller, events = component
    assert controller.begin()
    old_utterance = controller._utterance_id
    seen = []
    controller.interrupted.connect(lambda: seen.append(controller.error))
    receive(controller, ready=True, phase="idle", client_id="client-two", utterance_id="")
    assert seen == ["已切换输入框"]
    count = len(events)
    receive(controller, ready=True, phase="listening", client_id="client-one", utterance_id=old_utterance)
    assert controller._client_id == "client-two"
    assert len(events) == count


def test_conversion_only_accepts_underlined_sentence(component):
    controller, _ = component
    assert controller.begin()
    for phase, marked, allowed in [("listening", False, False), ("listening", True, True),
                                   ("finishing", True, True), ("dictated", False, False)]:
        receive(controller, phase=phase, ready=True, has_composition=marked,
                edit_requested=False, can_convert=allowed)
        count = len(controller._bridge.messages)
        controller.convert()
        assert (len(controller._bridge.messages) > count) == allowed
        assert bool(controller.canConvert) == allowed


def test_scoped_native_edit_request_is_not_silently_dropped_or_labeled_complete(component):
    controller, _ = component
    assert controller.begin()
    requests = []
    controller.editRequested.connect(lambda *args: requests.append(args))
    receive(controller, phase="editing", ready=True, revision=1, edit_requested=True,
            context_complete=False, edit_context_available=True, edit_scope="readable_range")
    receive(controller, "edit_requested", revision=1, original="可读原文", instruction="修改指令",
            edit_context_available=True, edit_scope="readable_range")
    receive(controller, "edit_requested", revision=1, original="可读原文", instruction="修改指令",
            edit_context_available=True, edit_scope="readable_range")
    assert len(requests) == 1
    assert requests[0][1:3] == ("修改指令", "可读原文")
    assert controller._view["context_complete"] is False


def test_committed_range_ack_is_visible_in_diagnostics(component):
    c, events = component
    receive(c, phase="dictated", ready=True,
            readback_diagnostics={"ack_source": "committed_range", "expected_selection": [19, 0], "observed_selection": [19, 0]})
    assert events[-1]["readback_diagnostics"]["ack_source"] == "committed_range"


@pytest.mark.parametrize('initial_ready', [False, True])
def test_first_tap_survives_activate_deactivate_activate_before_text(component, monkeypatch, initial_ready):
    c, _ = component
    origin = ('com.openai.codex', 123)
    monkeypatch.setattr(c, '_foreground_identity', lambda: origin)
    receive(c, ready=initial_ready, application=origin[0], utterance_id='')
    cancelled = []
    c.interrupted.connect(lambda: cancelled.append(True))
    assert c.begin()
    utterance = c._utterance_id
    deadline = c._startup_deadline
    c.bind_session(17)
    c.update('第一句完整内容', True, 17)
    c.finish()
    receive(c, ready=True, phase='idle', client_id='short-lived', utterance_id='', application=origin[0])
    receive(c, ready=False, phase='idle', client_id='', utterance_id='', lifecycle_event='deactivate')
    receive(c, 'interrupted', client_id='short-lived', utterance_id=utterance)
    receive(c, 'settled', client_id='short-lived', utterance_id=utterance, phase='interrupted')
    assert not cancelled and c._startup_buffering
    assert c._startup_deadline == deadline
    assert not any(m['type'] == 'update' for m in c._bridge.messages)
    receive(c, ready=True, phase='idle', client_id='actual', utterance_id='', application=origin[0])
    receive(c, ready=True, phase='listening', client_id='short-lived', utterance_id=utterance)
    assert c._startup_buffering
    receive(c, ready=True, phase='listening', application=origin[0])
    updates = [m for m in c._bridge.messages if m['type'] == 'update']
    assert len(updates) == 1 and updates[0]['text'] == '第一句完整内容'
    assert updates[0]['client_id'] == 'actual' and updates[0]['utterance_id'] == utterance
    assert not c._activation_timer.isActive() and not cancelled


def test_missing_activation_only_polls_and_never_switches_source(component, monkeypatch):
    c, events = component
    monkeypatch.setattr(c, '_foreground_identity', lambda: ('com.openai.codex', 123))
    receive(c, ready=False, client_id='', utterance_id='', phase='idle')
    c.begin()
    for _ in range(5): c._refresh_activation()
    assert c._bridge.messages[-1]['type'] == 'ping'
    assert not any(m['type'] == 'recover_activation' for m in c._bridge.messages)
    assert not c.ready and c._startup_buffering
    c.cancel()
    before = len(c._bridge.messages)
    c._refresh_activation()
    assert len(c._bridge.messages) == before

def test_obsolete_recovery_reply_cannot_change_new_startup(component, monkeypatch):
    c, _ = component
    monkeypatch.setattr(c, '_foreground_identity', lambda: ('com.openai.codex', 123))
    receive(c, ready=False, client_id='', utterance_id='', phase='idle')
    c.begin()
    deadline = c._startup_deadline
    receive(c, 'activation_recovery', request_id='retired', result='not_selected', error='old error')
    assert c._startup_deadline == deadline and 'old error' not in c.error
    c._startup_deadline = 0
    c._refresh_activation()
    assert '输入法未连接' in c.error
    assert not c._startup_buffering and not c._activation_timer.isActive()

def test_ready_first_tap_buffers_until_ack_and_never_recovers_after_text(component, monkeypatch):
    c, _ = component
    monkeypatch.setattr(c, '_foreground_identity', lambda: ('test.editor', 123))
    c.begin()
    c.update('首句', False, 7)
    c._refresh_activation()
    assert not any(m['type'] in {'update', 'recover_activation'} for m in c._bridge.messages)
    receive(c, ready=True, phase='listening')
    assert c._bridge.messages[-1]['text'] == '首句'
    receive(c, ready=False, phase='idle', client_id='', utterance_id='')
    c._refresh_activation()
    assert not any(m['type'] == 'recover_activation' for m in c._bridge.messages)


def test_app_change_during_begin_ack_wait_drops_buffer(component, monkeypatch):
    c, _ = component
    monkeypatch.setattr(c, '_foreground_identity', lambda: ('test.editor', 123))
    c.begin()
    c.update('禁止写入新应用', True, 1)
    monkeypatch.setattr(c, '_foreground_identity', lambda: ('other.editor', 456))
    receive(c, ready=True, phase='listening')
    assert '已切换应用' in c.error
    assert not any(m['type'] == 'update' for m in c._bridge.messages)


def test_startup_does_not_ignore_real_keyboard_takeover(component, monkeypatch):
    c, _ = component
    monkeypatch.setattr(c, '_foreground_identity', lambda: ('test.editor', 123))
    c.begin()
    c.update('buffered', True, 7)
    receive(c, 'interrupted', reason='键盘输入已接管本句')
    receive(c, ready=True, phase='listening')
    assert not c._startup_buffering
    assert not any(m['type'] == 'update' for m in c._bridge.messages)


def test_repeated_inactive_states_do_not_extend_startup_or_cycle_source(component, monkeypatch):
    c, _ = component
    monkeypatch.setattr(c, '_foreground_identity', lambda: ('test.editor', 123))
    receive(c, ready=False, client_id='', utterance_id='', phase='idle')
    c.begin()
    deadline = c._startup_deadline
    for _ in range(4):
        receive(c, ready=False, client_id='', utterance_id='', phase='idle')
        c._refresh_activation()
    assert c._startup_deadline == deadline
    assert not any(m['type'] == 'recover_activation' for m in c._bridge.messages)

def test_activation_trace_does_not_change_input_state_or_log_text(component):
    c, events = component
    before = dict(c._view)
    receive(c, 'activation_trace', event='ignored_retired_sender', lease=3,
            sender='old', bound_sender='new', original='private text', extra=['private'])
    assert c._view == before
    assert events[-1]['event'] == 'ignored_retired_sender'
    assert 'original' not in events[-1] and 'extra' not in events[-1]
    count = len(events)
    c._accept({'type': 'activation_trace', 'epoch': 'obsolete', 'event': 'deactivate'})
    assert len(events) == count
