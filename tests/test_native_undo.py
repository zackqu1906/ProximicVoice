"""User undo is native, serialized and independent of clipboard/disk latency."""
from __future__ import annotations

import json
import threading

import pytest

from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
from test_interaction_controls import _controller, _close


@pytest.fixture
def controller(tmp_path, monkeypatch):
    value = _controller(tmp_path, monkeypatch)
    yield value
    _close(value)


def _push(controller, session=1, *, mode="edit", before="old"):
    from proximic_ring.ui.controller import _AppliedInteraction

    target = DesktopTargetRef(1, 2, "微信", process_id=123)
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode, target, session, 0, "修改", f"new-{session}",
            original_snapshot=(
                DesktopTextSnapshot(target, before) if before is not None else None
            ),
        ),
        message="已应用",
    )
    return target


def _forbidden(*_args, **_kwargs):
    raise AssertionError("native user undo must not read, copy, replace or settle")


class NativeDesktop:
    focused = True

    def __init__(self):
        self.calls = []

    def is_foreground(self, _target):
        return self.focused

    def send_native_undo(self, target):
        self.calls.append(target)

    undo = _forbidden
    observe_text = _forbidden
    observe_focused_text = _forbidden
    capture_text = _forbidden
    capture_text_allowing_empty = _forbidden
    replace = _forbidden
    release_selection = _forbidden


@pytest.mark.parametrize("mode", ["dictation", "edit"])
@pytest.mark.parametrize("before", [None, "", "old"])
def test_native_undo_never_reads_or_restores(controller, mode, before):
    desktop = NativeDesktop()
    controller._desktop_target = desktop
    target = _push(controller, mode=mode, before=before)
    # Even with smart association enabled, no unverified baseline is observed.
    controller._smart_association_enabled = True
    controller.undoLastApplied()
    assert desktop.calls == [target]
    assert controller.undoDepth == 0
    assert controller._manual_association_watch is None
    assert "已发送撤销" in controller.sessionHistoryText
    assert "verified=false" in controller.logText
    assert "dispatch_ms=" in controller.logText


@pytest.mark.parametrize("reenter_at", ["focus", "send", "notify"])
def test_reentrant_requests_reserve_distinct_operations(controller, reenter_at):
    from PySide6.QtCore import QCoreApplication, QTimer
    from PySide6.QtTest import QTest

    entered = False
    depth = 0
    max_depth = 0
    sessions = []

    def reenter():
        nonlocal entered
        if entered:
            return
        entered = True
        for _ in range(5):
            QTimer.singleShot(0, controller.undoLastApplied)
        QCoreApplication.processEvents()

    class Desktop(NativeDesktop):
        def is_foreground(self, target):
            if reenter_at == "focus":
                reenter()
            return super().is_foreground(target)

        def send_native_undo(self, target):
            nonlocal depth, max_depth
            depth += 1
            max_depth = max(depth, max_depth)
            sessions.append(controller._undo_active.session_id)
            if reenter_at == "send":
                reenter()
            super().send_native_undo(target)
            depth -= 1

    desktop = Desktop()
    controller._desktop_target = desktop
    for session in (1, 2, 3):
        _push(controller, session)
    if reenter_at == "notify":
        controller.interactionChanged.connect(reenter)
    controller.undoLastApplied()
    QTest.qWait(30)
    assert sessions == [3, 2, 1]
    assert max_depth == 1
    assert len(desktop.calls) == 3
    assert not controller._undo_queue
    assert controller.undoDepth == 0


@pytest.mark.parametrize("failure", ["send", "focus"])
def test_failure_stops_queued_undos_and_preserves_records(controller, failure):
    from PySide6.QtTest import QTest

    class Desktop(NativeDesktop):
        def send_native_undo(self, target):
            controller.undoLastApplied()
            if failure == "send":
                raise RuntimeError("cannot post shortcut")
            super().send_native_undo(target)
            self.focused = False

    desktop = Desktop()
    controller._desktop_target = desktop
    _push(controller, 1)
    _push(controller, 2)
    controller.undoLastApplied()
    QTest.qWait(30)
    assert len(desktop.calls) == (0 if failure == "send" else 1)
    assert controller.undoDepth == (2 if failure == "send" else 1)
    assert not controller._undo_queue


@pytest.mark.parametrize("boundary", ["disconnect", "quit"])
def test_focus_callback_boundary_invalidates_reserved_undo(controller, boundary):
    class Desktop(NativeDesktop):
        def is_foreground(self, _target):
            controller.undoLastApplied()
            if boundary == "disconnect":
                controller._clear_undo_stack_for_device_boundary()
            else:
                controller._quitting = True
            return True

    desktop = Desktop()
    controller._desktop_target = desktop
    _push(controller, 1)
    _push(controller, 2)
    controller.undoLastApplied()
    assert not desktop.calls
    assert not controller._undo_queue


def test_queued_undo_does_not_consume_a_later_application(controller):
    from PySide6.QtTest import QTest

    class Desktop(NativeDesktop):
        def send_native_undo(self, target):
            controller.undoLastApplied()
            super().send_native_undo(target)

    desktop = Desktop()
    controller._desktop_target = desktop
    _push(controller, 1)
    _push(controller, 2)
    controller.undoLastApplied()
    _push(controller, 3)
    QTest.qWait(30)
    assert len(desktop.calls) == 1
    assert controller.undoDepth == 2
    assert controller._latest_operation().session_id == 3
    assert not controller._undo_queue


def test_slow_writer_does_not_block_undo_or_mislabel_reused_session(controller):
    collector = controller._modification_dataset
    old_id = collector.begin_session(1)
    collector.record_application(
        action="applied", session_id=1, mode="edit", final_text="new-1",
    )
    desktop = NativeDesktop()
    controller._desktop_target = desktop
    _push(controller)
    entered = threading.Event()
    release = threading.Event()

    def blocked_write():
        entered.set()
        assert release.wait(5)

    controller._undo_writer.submit(blocked_write)
    assert entered.wait(1)
    try:
        controller.undoLastApplied()
        # Both desktop dispatch and UI completion happen before disk unblocks.
        assert len(desktop.calls) == 1
        assert controller.undoDepth == 0
        assert not release.is_set()
        collector.reset_runtime()
        new_id = collector.begin_session(1)
        collector.record_application(
            action="applied", session_id=1, mode="dictation", final_text="new run",
        )
        assert new_id != old_id
    finally:
        release.set()
    controller._undo_writer.submit(lambda: None).result(timeout=3)
    old = json.loads(collector._interaction_path(old_id).read_text())
    new = json.loads(collector._interaction_path(new_id).read_text())
    assert old["outcome"]["status"] == "native_undo_sent"
    assert old["outcome"]["final_text"] is None
    assert old["outcome"]["accepted"] is None
    assert old["outcome"]["acceptance_strength"] == "unverified_undo"
    assert old["mode"]["training_target"] is None
    assert new["outcome"]["status"] == "applied"
    assert new["outcome"]["final_text"] == "new run"
    assert collector._display_candidate_available(old) is False
    assert collector._display_candidate_text(old) == ""


def test_deferred_undo_does_not_recreate_deleted_data(controller):
    collector = controller._modification_dataset
    interaction_id = collector.begin_session(1)
    collector.clear()
    result = collector.record_application(
        action="native_undo_sent", method="native_shortcut",
        interaction_id=interaction_id, session_id=1,
    )
    assert result == ""
    assert not collector._interaction_path(interaction_id).exists()
