from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


def _controller(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QSettings

    import proximic_ring.ui.controller as controller_module

    _app = QCoreApplication.instance() or QCoreApplication(["interaction-controls"])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    monkeypatch.setattr(controller_module, "app_data_root", lambda: tmp_path)
    controller = controller_module.AppController()
    controller._text_processing_worker.close(wait=True)
    return controller


def _close(controller):
    controller._text_processing_worker.close(wait=True)
    controller._close_voice_history()


def test_diagnostic_log_persists_key_events_and_mode_switch_reason(
    tmp_path, monkeypatch
):
    controller = _controller(tmp_path, monkeypatch)

    controller._apply_runtime_session_started(42)
    controller.switchCurrentInputMode()

    diagnostic_path = tmp_path / "logs" / "diagnostic.log"
    content = diagnostic_path.read_text(encoding="utf-8")
    assert "[EVENT APP_READY]" in content
    assert "[EVENT SESSION_START]" in content
    assert "session=42" in content
    assert "[EVENT MODE_SWITCH_REQUEST]" in content
    assert "reason=\"no_convertible_result\"" in content
    _close(controller)


def test_overlay_starts_on_detected_voice_and_cancel_ignores_stale_asr(
    tmp_path, monkeypatch
):
    controller = _controller(tmp_path, monkeypatch)
    controller._recognition_enabled = True
    controller._desktop_output = False

    controller._apply_runtime_status("[ASR] START t=1.000s (pre-roll=0.40s)")
    assert controller.transcriptVisible is True
    assert controller.transcriptText == "正在收听语音"
    assert controller.transcriptPrimaryText == ""
    assert controller.interactionState == "listening"
    assert controller.interactionCanCancel is True

    controller.cancelCurrentUtterance()
    assert controller._cancel_utterance_event.is_set()
    assert controller.interactionState == "cancelled"
    controller._apply_runtime_update("不应回流", True, "", 17)
    assert controller.transcriptText == "已取消，等待下一句话"

    controller._apply_runtime_status("[ASR] START t=2.000s (pre-roll=0.40s)")
    assert controller.interactionState == "listening"
    assert controller.transcriptText == "正在收听语音"
    _close(controller)


def test_overlay_keeps_asr_as_primary_text_while_status_changes(
    tmp_path, monkeypatch
):
    controller = _controller(tmp_path, monkeypatch)
    controller._recognition_enabled = True
    controller._input_routing_mode = "manual"
    controller._prepare_manual_candidates = lambda *_args, **_kwargs: None

    controller._apply_runtime_status("[ASR] START t=1.000s")
    controller._apply_runtime_update("正在形成", False, "", 23)

    assert controller.transcriptPrimaryText == "正在形成"
    assert controller.transcriptText == "正在收听语音"

    controller._apply_runtime_update("这是最终识别文本", True, "", 23)

    assert controller.transcriptPrimaryText == "这是最终识别文本"
    assert controller.transcriptText in {"正在处理文本", "正在输入文字"}

    # Later pipeline states update only the secondary status channel.
    controller._transcript_text = "未能处理文本"
    controller.transcriptChanged.emit()
    assert controller.transcriptPrimaryText == "这是最终识别文本"
    _close(controller)


def test_processing_overlay_shows_recognized_edit_command_only(
    tmp_path, monkeypatch
):
    controller = _controller(tmp_path, monkeypatch)

    assert controller._processing_overlay_text(
        "edit", "  把这句   改得更正式  "
    ) == "正在处理文本 · 指令：把这句 改得更正式"
    assert controller._processing_overlay_text(
        "dictation", "这是听写内容"
    ) == "正在处理文本"
    _close(controller)


def test_delayed_history_snapshot_cannot_overwrite_final_applied_mode(
    tmp_path, monkeypatch
):
    import numpy as np

    controller = _controller(tmp_path, monkeypatch)
    collector = controller._modification_dataset
    collector.record_audio(31, np.zeros(1600, dtype=np.float32))
    collector.record_asr_update(
        SimpleNamespace(
            session_id=31,
            text="删掉这一段",
            is_final=True,
            error=None,
            backend="test",
            model="test",
            latency_s=0.1,
            audio_duration_s=0.1,
        )
    )
    stale_snapshot = dict(controller.voiceHistoryEntries[0])
    stale_snapshot["mode"] = "dictation"
    stale_snapshot["candidateText"] = "删掉这一段"

    collector.record_application(
        action="applied",
        session_id=31,
        mode="edit",
        before_text="旧文本",
        candidate_text="新文本",
        final_text="新文本",
    )
    assert controller.voiceHistoryEntries[0]["mode"] == "edit"

    # Simulate a queued worker-thread notification arriving after the edit
    # application. Its stale payload must be treated only as an invalidation.
    controller._apply_voice_history_saved(stale_snapshot)

    assert controller.voiceHistoryEntries[0]["mode"] == "edit"
    assert controller.voiceHistoryEntries[0]["candidateText"] == "新文本"
    _close(controller)


def test_processing_edit_can_be_switched_directly_to_dictation(
    tmp_path, monkeypatch
):
    import json

    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Desktop:
        def __init__(self):
            self.injected = []

        def inject(self, captured, text):
            assert captured == target
            self.injected.append(text)

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._desktop_output = True
    result = TextProcessingResult(
        request_id=0,
        session_id=8,
        mode="dictation",
        raw_text="这是要输入的话",
        final_text="这是要输入的话",
        latency_s=0.0,
        used_llm=False,
    )
    interaction = _AutoInteraction(
        route_id=80,
        session_id=8,
        raw_text=result.raw_text,
        target=target,
        selected_mode="edit",
        routed_at=time.monotonic(),
        results={"dictation": result},
        preparing=False,
        classified=True,
        routed_by_model=True,
    )
    controller._active_auto_interaction = interaction
    controller._transcript_visible = True
    controller._transcript_text = controller._processing_overlay_text(
        "edit", result.raw_text
    )
    controller._set_interaction_state("processing")

    controller._schedule_processing_mode_correction("edit", interaction)
    assert controller._processing_mode_correction_timer.isActive() is True
    assert controller.processingModeCorrectionAvailable is False
    controller._reveal_processing_mode_correction()
    assert controller.processingModeCorrectionAvailable is True
    controller.switchCurrentInputMode()

    assert desktop.injected == ["这是要输入的话"]
    assert controller._latest_operation().mode == "dictation"
    assert controller.processingModeCorrectionAvailable is False
    interaction_id = controller._modification_dataset.interaction_id_for_session(8)
    record = json.loads(
        (
            controller._modification_dataset.interactions_root
            / interaction_id
            / "record.json"
        ).read_text(encoding="utf-8")
    )
    assert record["mode"]["selected"] == "dictation"
    assert record["mode"]["negative_mode"] == "edit"
    assert record["mode"]["label_source"] == "explicit_mode_switch"
    _close(controller)


def test_failed_initial_edit_keeps_f8_dictation_fallback(tmp_path, monkeypatch):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import (
        InputModeRoutingResult,
        TextProcessingResult,
    )

    controller = _controller(tmp_path, monkeypatch)
    routed = []
    submitted = []
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Worker:
        def submit_routing(self, request):
            routed.append(request)

        def submit(self, request):
            submitted.append(request)

        def cancel_request(self, _request_id):
            return None

        def close(self, *, wait=False):
            return None

    class Desktop:
        def __init__(self):
            self.text = "原文本"
            self.injected = []

        def capture_text_allowing_empty(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def inject(self, captured, text):
            assert captured == target
            self.injected.append(text)
            self.text += text

        def release_selection(self, _target):
            return None

    controller._text_processing_worker = Worker()
    desktop = Desktop()
    controller._desktop_target = desktop
    controller._desktop_output = True
    controller._llm_enabled = True
    controller._input_routing_mode = "auto"
    controller._capture_desktop_reference = lambda: target
    controller._connected = True
    controller._recognition_enabled = True
    controller._recognition_event.set()

    controller._apply_runtime_update("把这句话改正式", True, "", 61)
    edit_request = next(request for request in submitted if request.mode == "edit")
    controller._apply_input_mode_routed(
        InputModeRoutingResult(
            routed[0].request_id,
            61,
            "把这句话改正式",
            "edit",
            0.1,
            model_output="edit",
        )
    )
    controller._apply_text_processed(
        TextProcessingResult(
            edit_request.request_id,
            61,
            "edit",
            "把这句话改正式",
            "原文本",
            0.2,
            True,
            target_text="原文本",
            error="两种编辑协议均失败",
        )
    )

    assert controller.interactionState == "error"
    assert "两种编辑协议均失败" in controller.transcriptText
    assert controller.processingModeCorrectionAvailable is True
    assert controller._recognition_event.is_set() is False

    controller.switchCurrentInputMode()

    assert desktop.injected == ["把这句话改正式"]
    assert controller._latest_operation().mode == "dictation"
    assert controller.interactionState == "applied"
    assert controller.processingModeCorrectionAvailable is False
    assert controller.modeCorrectionAvailable is False
    assert controller.modeCorrectionFailed is True
    _close(controller)


def test_empty_final_shows_no_text_then_closes_and_resumes(
    tmp_path, monkeypatch
):
    controller = _controller(tmp_path, monkeypatch)
    controller._connected = True
    controller._recognition_enabled = True
    controller._recognition_event.set()

    controller._apply_runtime_status("[ASR] START t=1.000s")
    controller._apply_runtime_update("", True, "", 11)

    assert controller.transcriptVisible is True
    assert controller.transcriptText == "未识别到语音"
    assert controller.transcriptFinal is True
    assert controller.interactionState == "no_result"
    assert controller._hide_overlay_timer.isActive() is True
    assert controller._interaction_recognition_suspended is False
    assert controller._recognition_event.is_set() is True

    controller._hide_overlay_timer.stop()
    controller._hide_transcript()
    assert controller.transcriptVisible is False
    assert controller.interactionState == "idle"
    _close(controller)


def test_empty_final_keeps_previous_undo_but_does_not_restore_its_overlay(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Desktop:
        def is_foreground(self, captured):
            return captured == target

    controller._desktop_target = Desktop()
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation",
            target,
            10,
            0,
            "上一次听写",
            "上一次听写",
            summary="上一次听写已应用",
        ),
        message="上一次听写已应用",
    )

    controller._apply_runtime_status("[ASR] START t=2.000s")
    assert controller.appliedActionVisible is False

    controller._apply_runtime_update("", True, "", 11)

    assert controller.undoDepth == 1
    assert controller.undoAvailable is True
    assert controller.appliedActionVisible is False
    assert controller._applied_action_visible is False
    assert controller._applied_action_hide_timer.isActive() is False
    assert controller._applied_target_timer.isActive() is False
    _close(controller)


def test_applied_action_overlay_hides_after_three_seconds_without_expiring_undo(
    tmp_path, monkeypatch
):
    from PySide6.QtTest import QTest

    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    controller.appliedOverlayDurationSeconds = 3
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Desktop:
        def __init__(self):
            self.text = "应用后的文本"

        def is_foreground(self, _target):
            return True

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def undo(self, _target):
            self.text = "应用前的文本"

        def replace(self, _snapshot, text):
            self.text = text

    desktop = Desktop()
    controller._desktop_target = desktop
    auto_context = _AutoInteraction(
        route_id=10,
        session_id=10,
        raw_text="修改一下",
        target=target,
        selected_mode="edit",
        routed_at=time.monotonic(),
        preparing=False,
        classified=True,
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="edit",
            target=target,
            session_id=10,
            request_id=0,
            raw_text="修改一下",
            applied_text="应用后的文本",
            original_snapshot=DesktopTextSnapshot(target, "应用前的文本"),
            auto_context=auto_context,
        ),
        message="修改已应用",
    )

    assert controller.appliedActionVisible is True
    assert controller._applied_action_hide_timer.interval() == 3000
    assert controller._applied_action_hide_timer.isActive() is True

    # Keep the suite fast while exercising the same connected timeout slot.
    controller._applied_action_hide_timer.start(10)
    QTest.qWait(30)

    assert controller.appliedActionVisible is False
    assert controller.undoAvailable is True
    assert controller.undoDepth == 1
    assert controller.interactionState == "applied"
    assert controller.modeCorrectionAvailable is False
    assert controller.modeCorrectionHotkeyAvailable is True

    # Hiding is presentation-only; the existing undo operation still works.
    controller.undoLastApplied()
    assert desktop.text == "应用前的文本"
    assert controller.undoDepth == 0
    _close(controller)


def test_applied_overlay_duration_is_persisted_and_live_adjustable(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Desktop:
        def is_foreground(self, _target):
            return True

    controller._desktop_target = Desktop()
    controller._connected = True
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 10, 0, "测试", "测试", summary="已输入测试"
        ),
        message="听写已应用",
    )

    controller.appliedOverlayDurationSeconds = 7

    assert controller.appliedOverlayDurationSeconds == 7
    assert controller._applied_action_hide_timer.interval() == 7000
    assert controller._applied_action_hide_timer.isActive() is True
    assert controller.appliedActionVisible is True
    assert controller.undoDepth == 1
    assert int(
        controller._settings.value("ui/appliedOverlayDurationSeconds")
    ) == 7

    controller.appliedOverlayDurationSeconds = 99
    assert controller.appliedOverlayDurationSeconds == 10
    assert controller._applied_action_hide_timer.interval() == 10000
    assert controller.undoDepth == 1
    _close(controller)


def test_cancel_is_recorded_without_inventing_model_labels(tmp_path, monkeypatch):
    import json

    controller = _controller(tmp_path, monkeypatch)
    controller._recognition_enabled = True
    controller._apply_runtime_status("[ASR] START t=1.000s")
    controller._apply_runtime_session_started(7)

    controller.cancelCurrentUtterance()

    interaction_id = controller._modification_dataset.interaction_id_for_session(7)
    record = json.loads(
        (
            controller._modification_dataset.interactions_root
            / interaction_id
            / "record.json"
        ).read_text(encoding="utf-8")
    )
    assert record["near_field"]["training_label"] is None
    assert record["near_field"]["label_source"] is None
    assert record["asr"]["training_label"] is None
    assert record["outcome"]["status"] == "cancelled"
    assert record["outcome"]["accepted"] is False
    assert record["outcome"]["acceptance_strength"] == "explicit"
    events = [
        json.loads(line)
        for line in (
            controller._modification_dataset.interactions_root
            / interaction_id
            / "events.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(
        event.get("type") == "application"
        and event.get("action") == "cancelled"
        and event.get("method") == "explicit_user"
        for event in events
    )
    assert not any(
        event.get("type") == "model_label"
        and event.get("model") == "near_field"
        for event in events
    )
    _close(controller)


def test_detector_evidence_does_not_attach_to_previous_interaction(
    tmp_path, monkeypatch
):
    import json

    controller = _controller(tmp_path, monkeypatch)
    collector = controller._modification_dataset
    collector.begin_session(1)
    first_id = collector.interaction_id_for_session(1)
    controller._latest_asr_session_id = 1
    controller._utterance_active = False

    controller._apply_runtime_status("STAGE2 score=-0.2 reject")
    assert "score=-0.2" not in controller.logText
    controller._apply_runtime_status("STAGE2 score=+0.9 ACTIVATE")
    controller._apply_runtime_status("[ASR] START t=2.000s")
    controller._apply_runtime_session_started(2)
    controller._apply_runtime_status("STAGE2 score=-0.1 REJECT")
    controller._apply_runtime_status("STAGE2 score=-0.2 reject")
    controller._apply_runtime_status(
        "[ASR] END reason=2-rejects duration=1.40s rejects=2"
    )
    assert "[session=2] STAGE2 score=-0.1 REJECT" in controller.logText
    assert "[session=2] STAGE2 score=-0.2 reject" in controller.logText

    second_id = collector.interaction_id_for_session(2)
    first_events = [
        json.loads(line)
        for line in (collector.interactions_root / first_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    second_events = [
        json.loads(line)
        for line in (collector.interactions_root / second_id / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert first_events == []
    stage2_events = [
        row for row in second_events if row.get("stage") == "stage2"
    ]
    assert [row["score"] for row in stage2_events] == [0.9, -0.1, -0.2]
    assert [row["decision"] for row in stage2_events] == [
        "activate",
        "reject",
        "reject",
    ]
    assert all(row["session_id"] == 2 for row in stage2_events)
    assert [
        row["phase"]
        for row in second_events
        if row.get("component") == "asr_session"
    ] == ["start", "end"]
    _close(controller)


def test_applied_operations_support_multiple_undo_steps(tmp_path, monkeypatch):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Desktop:
        def __init__(self):
            self.text = "ABC"
            self.foreground = True

        def undo(self, _target):
            self.text = {"ABC": "AB", "AB": "A"}[self.text]

        def replace(self, snapshot, text):
            assert snapshot.target == target
            self.text = text

        def is_foreground(self, captured):
            assert captured == target
            return self.foreground

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            "edit", target, 1, 0, "第一次", "AB",
            DesktopTextSnapshot(target, "A"), summary="第一次修改",
        ),
        message="第一次已应用",
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            "edit", target, 2, 0, "第二次", "ABC",
            DesktopTextSnapshot(target, "AB"), summary="第二次修改",
        ),
        message="第二次已应用",
    )
    assert controller.undoDepth == 2
    assert controller.appliedActionText == "第二次修改"

    controller.undoLastApplied()
    assert desktop.text == "AB"
    assert controller.undoDepth == 1
    assert controller.appliedActionText == "第一次修改"
    controller.undoLastApplied()
    assert desktop.text == "A"
    assert controller.undoDepth == 0

    desktop.foreground = False
    controller._show_applied_interaction(
        _AppliedInteraction(
            "edit", target, 3, 0, "第三次", "AB",
            DesktopTextSnapshot(target, "A"), summary="第三次修改",
        ),
        message="第三次已应用",
    )
    controller.beginAppliedOverlayDrag()
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    controller.endAppliedOverlayDrag()
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    controller._applied_overlay_foreground_grace_until = 0.0
    controller._poll_applied_target_foreground()
    controller._poll_applied_target_foreground()
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is False
    _close(controller)


def test_undo_stacks_are_isolated_by_application_and_text_field(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    chat_a = DesktopTargetRef(10, 101, "微信", process_id=30)
    chat_b = DesktopTargetRef(10, 102, "微信", process_id=30)
    notes = DesktopTargetRef(20, 201, "备忘录", process_id=40)

    class Desktop:
        def __init__(self):
            self.focused = notes
            self.texts = {
                101: "A2",
                102: "B1",
                201: "N1",
            }

        def is_foreground(self, target):
            return target == self.focused

        def observe_text(self, target):
            return DesktopTextSnapshot(target, self.texts[target.control_handle])

        def undo(self, target):
            current = self.texts[target.control_handle]
            self.texts[target.control_handle] = {
                "A2": "A1", "A1": "A0", "B1": "B0", "N1": "N0",
            }[current]

        def replace(self, snapshot, text):
            self.texts[snapshot.target.control_handle] = text

    desktop = Desktop()
    controller._desktop_target = desktop
    assert controller._association_target_key(chat_a) != (
        controller._association_target_key(chat_b)
    )

    for operation in (
        _AppliedInteraction(
            "edit", chat_a, 1, 0, "A1", "A1",
            DesktopTextSnapshot(chat_a, "A0"), summary="微信 A 第一次",
        ),
        _AppliedInteraction(
            "edit", chat_a, 2, 0, "A2", "A2",
            DesktopTextSnapshot(chat_a, "A1"), summary="微信 A 第二次",
        ),
        _AppliedInteraction(
            "edit", chat_b, 3, 0, "B1", "B1",
            DesktopTextSnapshot(chat_b, "B0"), summary="微信 B",
        ),
        _AppliedInteraction(
            "edit", notes, 4, 0, "N1", "N1",
            DesktopTextSnapshot(notes, "N0"), summary="备忘录",
        ),
    ):
        controller._show_applied_interaction(operation, message="已应用")

    assert len(controller._operation_stacks) == 3
    assert controller.undoDepth == 1
    assert controller.appliedActionText == "备忘录"

    desktop.focused = chat_b
    controller._poll_applied_target_foreground()
    assert controller.undoDepth == 1
    assert controller.appliedActionText == "微信 B"
    controller.undoLastApplied()
    assert desktop.texts[102] == "B0"
    assert desktop.texts[101] == "A2"
    assert desktop.texts[201] == "N1"
    assert len(controller._operation_stacks) == 2

    desktop.focused = chat_a
    controller._poll_applied_target_foreground()
    assert controller.undoDepth == 2
    assert controller.appliedActionText == "微信 A 第二次"
    controller.undoLastApplied()
    assert desktop.texts[101] == "A1"
    assert controller.undoDepth == 1

    desktop.focused = None
    controller._poll_applied_target_foreground()
    controller._poll_applied_target_foreground()
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is False
    controller.undoLastApplied()
    assert desktop.texts[101] == "A1"
    assert desktop.texts[201] == "N1"

    desktop.focused = notes
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    assert controller.appliedActionText == "备忘录"
    _close(controller)


def test_undo_rechecks_focus_before_the_foreground_poll_catches_up(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    original = DesktopTargetRef(10, 101, "微信", process_id=30)
    another_field = DesktopTargetRef(10, 102, "微信", process_id=30)

    class Desktop:
        def __init__(self):
            self.focused = original
            self.text = "语音结果"
            self.replace_calls = 0

        def is_foreground(self, target):
            return target == self.focused

        def observe_text(self, target):
            return DesktopTextSnapshot(target, self.text)

        def replace(self, _snapshot, text):
            self.replace_calls += 1
            self.text = text

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            "edit", original, 1, 0, "修改", "语音结果",
            DesktopTextSnapshot(original, "原文"), summary="已修改",
        ),
        message="已应用",
    )
    assert controller.appliedActionVisible is True

    # Focus changes inside the same app before the 300 ms visibility poll.
    desktop.focused = another_field
    controller.undoLastApplied()

    assert desktop.replace_calls == 0
    assert desktop.text == "语音结果"
    assert controller.undoDepth == 1
    assert controller.appliedActionVisible is False
    _close(controller)


def test_delayed_mode_switch_result_stays_bound_to_its_text_field(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    field_a = DesktopTargetRef(10, 101, "微信", process_id=30)
    field_b = DesktopTargetRef(10, 102, "微信", process_id=30)

    class Desktop:
        focused = field_a

        def is_foreground(self, target):
            return target == self.focused

    desktop = Desktop()
    controller._desktop_target = desktop
    context_a = _AutoInteraction(
        route_id=77,
        session_id=7,
        raw_text="改一下",
        target=field_a,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        results={},
        preparing=False,
        classified=True,
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", field_a, 7, 0, "改一下", "改一下",
            auto_context=context_a, summary="字段 A",
        ),
        message="字段 A 已应用",
    )
    field_a_key = controller._active_operation_target_key
    controller.switchCurrentInputMode()
    assert controller._pending_applied_mode_switches[field_a_key] == (77, "edit")

    desktop.focused = field_b
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", field_b, 8, 0, "字段 B", "字段 B", summary="字段 B",
        ),
        message="字段 B 已应用",
    )
    field_b_key = controller._active_operation_target_key
    assert field_b_key != field_a_key

    routed = []
    controller._apply_alternate_result = (
        lambda mode, target_key="", route_id=None: routed.append(
            (mode, target_key, route_id)
        )
    )
    controller._accept_auto_candidate(
        77,
        TextProcessingResult(
            request_id=71,
            session_id=7,
            mode="edit",
            raw_text="改一下",
            final_text="修改结果",
            latency_s=0.1,
            used_llm=True,
            target_text="原文",
        ),
        field_a,
    )
    from PySide6.QtTest import QTest

    QTest.qWait(70)

    assert routed == [("edit", field_a_key, 77)]
    assert field_a_key not in controller._pending_applied_mode_switches
    assert controller._active_operation_target_key == field_b_key
    assert controller.appliedActionText == "字段 B"
    _close(controller)


def test_delayed_mode_switch_never_converts_a_newer_operation_in_same_stack(
    tmp_path, monkeypatch
):
    from PySide6.QtTest import QTest

    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(10, 101, "微信", process_id=30)

    class Desktop:
        def is_foreground(self, _target):
            return True

    controller._desktop_target = Desktop()
    old_context = _AutoInteraction(
        route_id=77,
        session_id=7,
        raw_text="改一下",
        target=target,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        results={},
        preparing=False,
        classified=True,
    )
    new_context = _AutoInteraction(
        route_id=88,
        session_id=8,
        raw_text="新一句",
        target=target,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        results={},
        preparing=False,
        classified=True,
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 7, 0, "改一下", "改一下",
            auto_context=old_context, summary="旧结果",
        ),
        message="旧结果已应用",
    )
    target_key = controller._active_operation_target_key
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 8, 0, "新一句", "新一句",
            auto_context=new_context, summary="新结果",
        ),
        message="新结果已应用",
    )
    # Simulate an older asynchronous F8 request resolving after the next
    # utterance has already become the top operation in the same field.
    controller._pending_applied_mode_switches[target_key] = (77, "edit")
    applied = []
    controller._apply_alternate_result = (
        lambda mode, target_key="", route_id=None: applied.append(
            (mode, target_key, route_id)
        )
    )
    controller._accept_auto_candidate(
        77,
        TextProcessingResult(
            request_id=71,
            session_id=7,
            mode="edit",
            raw_text="改一下",
            final_text="旧句修改结果",
            latency_s=0.1,
            used_llm=True,
            target_text="原文",
        ),
        target,
    )
    QTest.qWait(70)

    assert applied == []
    assert controller._pending_applied_mode_switches == {}
    assert controller.undoDepth == 2
    assert controller._latest_operation().session_id == 8
    assert controller._latest_operation().mode == "dictation"
    _close(controller)


def test_cancelling_next_utterance_keeps_previous_alternate_request(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import (
        _AppliedInteraction,
        _AutoInteraction,
        _PendingInteraction,
        _PendingModeRoute,
    )

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(10, 101, "编辑器", process_id=30)
    cancelled = []

    class Worker:
        def cancel_request(self, request_id):
            cancelled.append(request_id)

        def close(self, *, wait=False):
            return None

    class Desktop:
        def __init__(self):
            self.text = "原文旧听写"

        def is_foreground(self, captured):
            return captured == target

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def replace(self, _snapshot, text):
            self.text = text

        def inject(self, _captured, text):
            self.text += text

        def release_selection(self, _target):
            return None

    controller._text_processing_worker = Worker()
    desktop = Desktop()
    controller._desktop_target = desktop
    controller._desktop_output = True
    controller._capture_desktop_reference = lambda: target

    old_context = _AutoInteraction(
        route_id=77,
        session_id=7,
        raw_text="旧听写",
        target=target,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        snapshot=DesktopTextSnapshot(target, "原文"),
        request_ids={"edit": 71},
        results={},
        preparing=False,
        classified=True,
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation",
            target,
            7,
            0,
            "旧听写",
            "旧听写",
            original_snapshot=DesktopTextSnapshot(target, "原文"),
            auto_context=old_context,
            summary="旧听写已应用",
        ),
        message="旧听写已应用",
    )
    controller._pending_text_requests.add(71)
    controller._pending_interactions[71] = _PendingInteraction(
        target=target,
        snapshot=old_context.snapshot,
        auto_route_id=77,
        session_id=7,
    )

    controller._recognition_enabled = True
    controller._apply_runtime_status("[ASR] START t=2.000s")
    controller._apply_runtime_session_started(8)
    controller._pending_mode_routes.add(81)
    controller._pending_mode_route_contexts[81] = _PendingModeRoute(
        target=target,
        session_id=8,
    )
    controller.cancelCurrentUtterance()

    assert cancelled == [81]
    assert 71 in controller._pending_text_requests
    assert 71 in controller._pending_interactions
    assert 81 not in controller._pending_mode_routes

    controller._apply_text_processed(
        TextProcessingResult(
            71,
            7,
            "edit",
            "旧听写",
            "正式文本",
            0.2,
            True,
            target_text="原文",
        )
    )
    assert controller.modeCorrectionAvailable is True
    assert controller.modeCorrectionFailed is False
    controller.switchCurrentInputMode()
    from PySide6.QtTest import QTest

    QTest.qWait(70)
    assert controller._latest_operation().mode == "edit"
    assert desktop.text == "正式文本"
    _close(controller)


def test_applied_action_summary_is_limited_without_character_count(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation",
            target,
            1,
            0,
            "很长的听写",
            "很长的听写",
            summary="这是一个非常长的操作摘要" * 8,
        ),
        message="听写已应用",
    )

    assert len(controller.appliedActionText) == 42
    assert controller.appliedActionText.endswith("...")
    assert not hasattr(controller, "appliedActionCharacterText")
    _close(controller)


def test_applied_overlay_requires_the_exact_text_field(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "微信", process_id=30)

    class Desktop:
        field_foreground = True

        def is_foreground(self, _target):
            return self.field_foreground

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 1, 0, "测试", "测试", summary="已输入：测试"
        ),
        message="听写已应用",
    )

    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    desktop.field_foreground = False
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is False
    _close(controller)


def test_applied_overlay_survives_transient_text_field_focus_misses(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "微信", process_id=30)

    class Desktop:
        checks = iter((False, False, True))

        def is_foreground(self, _target):
            return next(self.checks)

    controller._desktop_target = Desktop()
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 1, 0, "测试", "测试", summary="已输入：测试"
        ),
        message="听写已应用",
    )

    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    _close(controller)


def test_applied_overlay_refreshes_the_field_reference_after_application(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    before = DesktopTargetRef(
        0, 0, "微信", process_id=30, accessibility_id="ax:before",
        screen_x=100, screen_y=500, screen_width=420, screen_height=36,
    )
    after = DesktopTargetRef(
        0, 0, "微信", process_id=30, accessibility_id="ax:after",
        screen_x=100, screen_y=470, screen_width=420, screen_height=66,
        caret_x=300, caret_y=515, caret_width=2, caret_height=18,
    )

    class Desktop:
        def capture_reference(self):
            return after

        def caret_bounds(self, _target):
            return (300, 515, 2, 18)

        def is_foreground(self, target):
            return target.accessibility_id == "ax:after"

    controller._desktop_target = Desktop()
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", before, 1, 0, "测试", "测试", summary="已输入：测试"
        ),
        message="听写已应用",
    )

    assert controller._latest_operation().target.accessibility_id == "ax:after"
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is True
    _close(controller)


def test_applied_overlay_uses_frontmost_application_when_ax_field_is_transient(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "微信", process_id=30)

    class Desktop:
        application_foreground = True

        def is_foreground(self, _target):
            return False

        def is_application_foreground(self, _target):
            return self.application_foreground

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 1, 0, "测试", "测试", summary="已输入：测试"
        ),
        message="听写已应用",
    )

    for _ in range(5):
        controller._poll_applied_target_foreground()
        assert controller.appliedActionVisible is True

    desktop.application_foreground = False
    for _ in range(3):
        controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is False
    _close(controller)


@pytest.mark.parametrize("end_event", ["disconnected", "finished"])
def test_device_session_end_clears_undo_stack_without_changing_text(
    tmp_path, monkeypatch, end_event
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)
    operation = _AppliedInteraction(
        "edit",
        target,
        1,
        0,
        "改一下",
        "已修改文本",
        DesktopTextSnapshot(target, "原始文本"),
        summary="修改已应用",
    )
    controller._show_applied_interaction(
        operation,
        message="修改已应用",
    )
    target_key = controller._operation_target_key(target)
    controller._pending_applied_mode_switches[target_key] = (1, "dictation")
    controller._set_interaction_state("applied")
    controller._runtime_active = True
    controller._connected = True
    controller._transcript_visible = True
    controller._transcript_mode = "edit"
    controller._association_recommendation = SimpleNamespace()
    controller._association_detail_visible = True
    controller._association_center_visible = True

    if end_event == "disconnected":
        controller._apply_runtime_disconnected()
    else:
        controller._apply_runtime_finished("设备连接中断")

    assert controller.undoAvailable is False
    assert controller.undoDepth == 0
    assert controller.appliedActionVisible is False
    assert controller._pending_applied_mode_switches == {}
    assert controller.interactionState == "idle"
    assert controller.transcriptVisible is False
    assert controller.associationRecommendationVisible is False
    assert controller.associationDetailVisible is False
    assert controller.associationCenterVisible is False
    assert operation.applied_text == "已修改文本"
    _close(controller)


def test_applied_action_uses_post_application_caret_bounds(tmp_path, monkeypatch):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(
        1,
        2,
        "编辑器",
        process_id=30,
        screen_x=100,
        screen_y=200,
        screen_width=500,
        screen_height=100,
    )

    class Desktop:
        def caret_bounds(self, captured):
            assert captured == target
            return 410, 242, 2, 19

    controller._desktop_target = Desktop()
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 1, 0, "测试", "测试", summary="已输入"
        ),
        message="听写已应用",
    )

    assert controller.appliedPopupCaretX == 410
    assert controller.appliedPopupCaretY == 242
    assert controller.appliedPopupCaretWidth == 2
    assert controller.appliedPopupCaretHeight == 19
    _close(controller)


def test_success_recommends_recent_asr_failures_and_writes_link_index(
    tmp_path, monkeypatch
):
    import json
    import numpy as np

    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    controller.smartAssociationEnabled = True
    controller._llm_enabled = False
    target = DesktopTargetRef(
        1,
        2,
        "编辑器",
        process_id=30,
        screen_x=120,
        screen_y=240,
        screen_width=480,
        screen_height=160,
    )

    def persist(session_id: int, text: str, action: str) -> None:
        controller._modification_dataset.record_audio(
            session_id, np.zeros(16_000, dtype=np.float32)
        )
        controller._modification_dataset.record_asr_update(
            SimpleNamespace(
                session_id=session_id,
                backend="test-asr",
                model="test-model",
                text=text,
                is_final=True,
                latency_s=0.1,
                audio_duration_s=1.0,
                error=None,
            )
        )
        controller._modification_dataset.record_application(
            action=action,
            session_id=session_id,
            mode="dictation",
            target_key=controller._association_target_key(target),
        )

    persist(1, "", "no_result")
    controller._record_association_failure(
        session_id=1, target=target, mode="dictation", status="未识别"
    )
    persist(2, "", "no_result")
    controller._record_association_failure(
        session_id=2, target=target, mode="dictation", status="未识别"
    )
    persist(3, "重复成功的句子", "applied")
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="dictation",
            target=target,
            session_id=3,
            request_id=0,
            raw_text="重复成功的句子",
            applied_text="重复成功的句子",
        ),
        message="听写已应用到原文本框",
    )

    assert controller.associationRecommendationVisible is True
    assert controller.associationRecommendationTitle == "发现 2 条可关联的听写"
    assert controller.undoAvailable is True
    assert controller.undoDepth == 1
    assert controller.associationPopupTargetX == 120
    assert controller.associationPopupTargetY == 240
    controller.performAssociationAction("recommendation.details.open", "")
    assert controller.associationDetailVisible is True
    controller.performAssociationAction("center.open", "")
    assert controller.associationCenterVisible is True
    assert controller.associationDetailVisible is False
    controller.performAssociationAction("center.close", "")
    assert controller.associationRecommendationVisible is True
    controller.performAssociationAction("recommendation.accept", "")
    assert controller.undoAvailable is False
    assert controller.undoDepth == 0

    rows = controller._modification_dataset.load_associations()
    assert len(rows) == 1
    assert rows[0]["kind"] == "asr"
    assert rows[0]["member_interaction_ids"] == [
        controller._modification_dataset.interaction_id_for_session(3),
        controller._modification_dataset.interaction_id_for_session(1),
        controller._modification_dataset.interaction_id_for_session(2),
    ]
    success_id = controller._modification_dataset.interaction_id_for_session(3)
    success_record = json.loads(
        (
            controller._modification_dataset.interactions_root
            / success_id
            / "record.json"
        ).read_text(encoding="utf-8")
    )
    assert success_record["outcome"]["accepted"] is True
    assert success_record["outcome"]["acceptance_strength"] == "explicit"
    _close(controller)


def test_applied_result_remains_on_multi_undo_stack(
    tmp_path, monkeypatch
):
    import json
    import numpy as np

    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)
    collector = controller._modification_dataset
    collector.record_audio(9, np.zeros(16_000, dtype=np.float32))
    collector.record_asr_update(
        SimpleNamespace(
            session_id=9,
            backend="test-asr",
            model="test-model",
            text="五秒后确认",
            is_final=True,
            latency_s=0.1,
            audio_duration_s=1.0,
            error=None,
        )
    )
    collector.record_application(
        action="applied",
        session_id=9,
        mode="dictation",
        target_key=controller._association_target_key(target),
        final_text="五秒后确认",
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="dictation",
            target=target,
            session_id=9,
            request_id=0,
            raw_text="五秒后确认",
            applied_text="五秒后确认",
        ),
        message="听写已应用到原文本框",
    )
    assert controller.undoAvailable is True
    assert controller.transcriptVisible is False
    assert controller.appliedActionVisible is True
    assert controller.undoDepth == 1
    assert not hasattr(controller, "_undo_deadline")
    interaction_id = collector.interaction_id_for_session(9)
    record = json.loads(
        (collector.interactions_root / interaction_id / "record.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["outcome"]["accepted"] is True
    assert record["outcome"]["acceptance_strength"] == "implicit"
    _close(controller)


def test_auto_route_prepares_both_modes_and_switch_uses_cached_edit(
    tmp_path, monkeypatch
):
    import json
    import numpy as np
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import (
        InputModeRoutingResult,
        TextProcessingResult,
    )

    controller = _controller(tmp_path, monkeypatch)
    routed = []
    submitted = []

    class Worker:
        def submit_routing(self, request):
            routed.append(request)

        def submit(self, request):
            submitted.append(request)

        def cancel_request(self, _request_id):
            return None

        def close(self, *, wait=False):
            return None

    target = DesktopTargetRef(1, 2, "编辑器")

    class Desktop:
        def __init__(self):
            self.text = "旧文本"

        def capture_text(self, captured):
            assert captured == target
            return DesktopTextSnapshot(target, self.text)

        def inject(self, captured, text):
            assert captured == target
            self.text += text

        def replace(self, snapshot, text):
            assert snapshot.target == target
            self.text = text

        def release_selection(self, _target):
            return None

    controller._text_processing_worker = Worker()
    desktop = Desktop()
    controller._desktop_target = desktop
    controller._desktop_output = True
    controller._llm_enabled = True
    controller._input_routing_mode = "auto"
    controller._capture_desktop_reference = lambda: target
    controller._modification_dataset.record_audio(
        21, np.zeros(1600, dtype=np.float32)
    )

    controller._apply_runtime_update("改得更正式", True, "", 21)
    assert len(routed) == 1
    assert {request.mode for request in submitted} == {"dictation", "edit"}

    edit_request = next(request for request in submitted if request.mode == "edit")
    dictation_request = next(
        request for request in submitted if request.mode == "dictation"
    )
    controller._apply_text_processed(
        TextProcessingResult(
            edit_request.request_id,
            21,
            "edit",
            "改得更正式",
            "新的正式文本",
            0.2,
            True,
            target_text="旧文本",
            model_output='{"modified_text":"新的正式文本"}',
        )
    )
    controller._apply_input_mode_routed(
        InputModeRoutingResult(
            routed[0].request_id,
            21,
            "改得更正式",
            "dictation",
            0.1,
            model_output="dictation",
        )
    )
    controller._apply_text_processed(
        TextProcessingResult(
            dictation_request.request_id,
            21,
            "dictation",
            "改得更正式",
            "改得更正式",
            0.1,
            True,
        )
    )
    controller._dictation_commit_timer.stop()
    controller._commit_pending_dictation()
    assert controller.modeCorrectionAvailable is True
    assert controller.appliedActionVisible is True
    assert controller.modeCorrectionLabel == "刚刚是指令"
    assert controller.voiceHistoryEntries[0]["mode"] == "dictation"
    model_resets = []
    controller.voiceHistoryModel.modelReset.connect(
        lambda: model_resets.append(True)
    )
    before = len(submitted)
    visibility_during_switch = []
    depths_during_switch = []
    controller.interactionChanged.connect(
        lambda: (
            visibility_during_switch.append(controller.appliedActionVisible),
            depths_during_switch.append(controller.undoDepth),
        )
    )
    controller.switchCurrentInputMode()
    assert len(submitted) == before
    assert controller.modeCorrectionPending is True
    assert controller.appliedActionText == "正在按指令重新处理…"
    # A second press while the desktop replacement is pending must not queue
    # another switch back to dictation.
    controller.switchCurrentInputMode()
    from PySide6.QtTest import QTest

    QTest.qWait(70)
    assert controller._latest_operation().mode == "edit"
    assert controller.undoDepth == 1
    assert len(controller._all_operations()) == 1
    assert visibility_during_switch
    assert all(visibility_during_switch)
    assert depths_during_switch
    assert set(depths_during_switch) == {1}
    # A new physical press after the completed replacement starts the reverse
    # conversion immediately; there is no invisible post-success cooldown.
    controller.switchCurrentInputMode()
    assert controller._latest_operation().mode == "edit"
    assert controller.modeCorrectionPending is True
    QTest.qWait(70)
    assert controller._latest_operation().mode == "dictation"
    assert desktop.text == "旧文本改得更正式"
    assert controller.modeCorrectionLabel == "刚刚是指令"
    assert controller.voiceHistoryEntries[0]["mode"] == "dictation"
    assert model_resets
    model_entry = controller.voiceHistoryModel.data(
        controller.voiceHistoryModel.index(0, 0)
    )
    assert model_entry["mode"] == "dictation"
    assert controller.undoDepth == 1
    assert len(controller._all_operations()) == 1

    # Every later successful conversion updates the same Interaction row and
    # its live QML model; it must never append or leave an earlier label behind.
    controller.switchCurrentInputMode()
    assert controller.modeCorrectionPending is True
    QTest.qWait(70)
    assert controller._latest_operation().mode == "edit"
    assert controller.voiceHistoryEntries[0]["mode"] == "edit"
    assert len(controller.voiceHistoryEntries) == 1
    assert controller.undoDepth == 1
    assert len(controller._all_operations()) == 1
    model_entry = controller.voiceHistoryModel.data(
        controller.voiceHistoryModel.index(0, 0)
    )
    assert model_entry["mode"] == "edit"

    controller.switchCurrentInputMode()
    assert controller.modeCorrectionPending is True
    QTest.qWait(70)
    assert controller._latest_operation().mode == "dictation"
    assert controller.voiceHistoryEntries[0]["mode"] == "dictation"
    assert len(controller.voiceHistoryEntries) == 1
    assert controller.undoDepth == 1
    assert len(controller._all_operations()) == 1
    interaction_id = controller._modification_dataset.interaction_id_for_session(21)
    record = json.loads(
        (
            controller._modification_dataset.interactions_root
            / interaction_id
            / "record.json"
        ).read_text(encoding="utf-8")
    )
    assert record["mode"]["selected"] == "dictation"
    assert record["mode"]["negative_mode"] == "edit"
    assert record["mode"]["label_source"] == "explicit_mode_switch"
    assert record["outcome"]["manually_corrected"] is True
    assert record["asr"]["training_label"] == "positive"
    assert record["near_field"]["training_label"] == "positive"
    _close(controller)


def test_green_mode_switch_executes_after_accessibility_wrapper_rebuild(
    tmp_path, monkeypatch
):
    from PySide6.QtTest import QTest

    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(
        1,
        2,
        "网页编辑器",
        process_id=30,
        accessibility_id="ax:before-paste",
    )
    instruction = "把原文扩写"
    context = _AutoInteraction(
        route_id=91,
        session_id=19,
        raw_text=instruction,
        target=target,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        snapshot=DesktopTextSnapshot(target, "原文"),
        results={
            "edit": TextProcessingResult(
                request_id=92,
                session_id=19,
                mode="edit",
                raw_text=instruction,
                final_text="扩写后的原文",
                latency_s=0.2,
                used_llm=True,
                target_text="原文",
            )
        },
        preparing=False,
        classified=True,
    )

    class Desktop:
        def __init__(self):
            self.text = "原文" + instruction

        def is_foreground(self, _target):
            # Simulate Electron/WebKit rebuilding the focused AX object after
            # the dictation paste even though the user stayed in this field.
            return False

        def is_application_foreground(self, _target):
            return True

        def observe_text(self, captured):
            raise RuntimeError("用户焦点已经离开旧的控件边界")

        def observe_focused_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def replace(self, _snapshot, text):
            self.text = text

        def inject(self, _target, text):
            self.text += text

        def release_selection(self, _target):
            return None

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._desktop_output = True
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="dictation",
            target=target,
            session_id=19,
            request_id=0,
            raw_text=instruction,
            applied_text=instruction,
            original_snapshot=DesktopTextSnapshot(target, "原文"),
            auto_context=context,
            summary="听写已应用",
        ),
        message="听写已应用",
    )

    assert controller.modeCorrectionAvailable is True
    assert controller.modeCorrectionHotkeyAvailable is True
    # Exercise the same signal path used by the global F8 hook, not a direct
    # call into the conversion implementation.
    controller.dispatchVoiceAction("switch_mode")
    assert controller.modeCorrectionPending is True
    QTest.qWait(70)

    assert desktop.text == "扩写后的原文"
    assert controller._latest_operation().mode == "edit"
    assert controller.modeCorrectionPending is False
    assert controller.modeCorrectionLabel == "刚刚是输入内容"
    _close(controller)


def test_green_mode_switch_rejects_a_different_field_in_the_same_app(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(
        1,
        2,
        "网页编辑器",
        process_id=30,
        accessibility_id="ax:original-field",
    )
    instruction = "把原文扩写"
    context = _AutoInteraction(
        route_id=91,
        session_id=19,
        raw_text=instruction,
        target=target,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        snapshot=DesktopTextSnapshot(target, "原文"),
        results={
            "edit": TextProcessingResult(
                request_id=92,
                session_id=19,
                mode="edit",
                raw_text=instruction,
                final_text="扩写后的原文",
                latency_s=0.2,
                used_llm=True,
                target_text="原文",
            )
        },
        preparing=False,
        classified=True,
    )

    class Desktop:
        def __init__(self):
            self.text = "另一个输入框的内容"
            self.replace_calls = 0

        def is_foreground(self, _target):
            return False

        def is_application_foreground(self, _target):
            return True

        def observe_text(self, captured):
            raise RuntimeError("用户焦点已经离开旧的控件边界")

        def observe_focused_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def replace(self, _snapshot, text):
            self.replace_calls += 1
            self.text = text

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._desktop_output = True
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="dictation",
            target=target,
            session_id=19,
            request_id=0,
            raw_text=instruction,
            applied_text=instruction,
            original_snapshot=DesktopTextSnapshot(target, "原文"),
            auto_context=context,
            summary="听写已应用",
        ),
        message="听写已应用",
    )

    assert controller.modeCorrectionAvailable is True
    assert controller.modeCorrectionHotkeyAvailable is True
    controller.dispatchVoiceAction("switch_mode")

    assert desktop.replace_calls == 0
    assert desktop.text == "另一个输入框的内容"
    assert controller._latest_operation().mode == "dictation"
    assert controller.modeCorrectionPending is False
    assert controller.modeCorrectionFailed is True
    assert "光标放回原文本框" in controller.appliedActionText
    _close(controller)


def test_mode_switch_accepts_replacement_with_refreshed_target_identity(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    original_target = DesktopTargetRef(
        1,
        2,
        "编辑器",
        process_id=30,
        accessibility_id="ax:first",
    )
    refreshed_target = DesktopTargetRef(
        1,
        2,
        "编辑器",
        process_id=30,
        accessibility_id="ax:second",
    )
    edit_result = TextProcessingResult(
        72,
        7,
        "edit",
        "改正式",
        "正式文本",
        0.2,
        True,
        target_text="原文",
    )
    context = _AutoInteraction(
        route_id=77,
        session_id=7,
        raw_text="改正式",
        target=original_target,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        snapshot=DesktopTextSnapshot(original_target, "原文"),
        results={"edit": edit_result},
        preparing=False,
        classified=True,
    )

    class Desktop:
        def __init__(self):
            self.text = "原文旧听写"
            self.live_target = original_target
            self.replacements = []

        def is_foreground(self, _target):
            return True

        def capture_reference(self):
            return self.live_target

        def observe_text(self, target):
            return DesktopTextSnapshot(target, self.text)

        def capture_text(self, target):
            return DesktopTextSnapshot(target, self.text)

        def replace(self, _snapshot, text):
            self.replacements.append(text)
            self.text = text

        def inject(self, _target, text):
            self.text += text

        def release_selection(self, _target):
            return None

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._desktop_output = True
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation",
            original_target,
            7,
            0,
            "旧听写",
            "旧听写",
            original_snapshot=DesktopTextSnapshot(original_target, "原文"),
            auto_context=context,
            summary="旧听写已应用",
        ),
        message="旧听写已应用",
    )
    old_key = controller._active_operation_target_key
    desktop.live_target = refreshed_target

    controller._apply_alternate_result("edit", target_key=old_key)

    operations = controller._all_operations()
    assert desktop.replacements == ["原文", "正式文本"]
    assert desktop.text == "正式文本"
    assert len(operations) == 1
    assert operations[0][1].mode == "edit"
    # The live target identity may change after paste, but the logical undo
    # stack remains on its original key and the exact slot is replaced.
    assert controller._active_operation_target_key == old_key
    assert controller._latest_operation().target == refreshed_target
    assert controller.undoDepth == 1
    _close(controller)


def test_failed_mode_switch_restores_exact_previous_text(tmp_path, monkeypatch):
    import proximic_ring.ui.controller as controller_module
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    monkeypatch.setattr(controller_module.sys, "platform", "darwin")
    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(
        1,
        2,
        "编辑器",
        process_id=30,
        accessibility_id="ax:stable",
    )
    edit_result = TextProcessingResult(
        72,
        7,
        "edit",
        "改正式",
        "正式文本",
        0.2,
        True,
        target_text="原文",
    )
    context = _AutoInteraction(
        route_id=77,
        session_id=7,
        raw_text="改正式",
        target=target,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        snapshot=DesktopTextSnapshot(target, "原文"),
        results={"edit": edit_result},
        preparing=False,
        classified=True,
    )

    class Desktop:
        def __init__(self):
            self.text = "原文旧听写"

        def is_foreground(self, _target):
            return True

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def capture_text(self, captured):
            if self.text == "正式文本":
                raise RuntimeError("模拟写入后回读失败")
            return DesktopTextSnapshot(captured, self.text)

        def replace(self, _snapshot, text):
            self.text = text

        def release_selection(self, _target):
            return None

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._desktop_output = True
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation",
            target,
            7,
            0,
            "旧听写",
            "旧听写",
            original_snapshot=DesktopTextSnapshot(target, "原文"),
            auto_context=context,
            summary="旧听写已应用",
        ),
        message="旧听写已应用",
    )
    old_key = controller._active_operation_target_key

    controller._apply_alternate_result("edit", target_key=old_key)

    assert desktop.text == "原文旧听写"
    assert len(controller._all_operations()) == 1
    assert controller._latest_operation().mode == "dictation"
    assert controller._active_operation_target_key == old_key
    _close(controller)


def test_auto_dictation_commits_asr_without_waiting_for_llm_candidates(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import InputModeRoutingResult

    controller = _controller(tmp_path, monkeypatch)
    routed = []
    submitted = []
    cancelled = []
    injected = []

    class Worker:
        def submit_routing(self, request):
            routed.append(request)

        def submit(self, request):
            submitted.append(request)

        def cancel_request(self, request_id):
            cancelled.append(request_id)

        def close(self, *, wait=False):
            return None

    target = DesktopTargetRef(1, 2, "编辑器")

    class Desktop:
        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, "旧文本")

        def inject(self, _captured, _text):
            return None

        def inject(self, captured, text):
            injected.append((captured, text))

        def release_selection(self, _target):
            return None

    controller._text_processing_worker = Worker()
    controller._desktop_target = Desktop()
    controller._desktop_output = True
    controller._llm_enabled = True
    controller._input_routing_mode = "auto"
    controller._capture_desktop_reference = lambda: target

    controller._apply_runtime_update("直接输入这句话", True, "", 31)
    assert {request.mode for request in submitted} == {"dictation", "edit"}
    controller._apply_input_mode_routed(
        InputModeRoutingResult(
            routed[0].request_id,
            31,
            "直接输入这句话",
            "dictation",
            0.1,
            model_output="dictation",
        )
    )

    pending, pending_target = controller._pending_dictation_result
    assert pending.request_id == 0
    assert pending.final_text == "直接输入这句话"
    assert pending_target == target
    controller._dictation_commit_timer.stop()
    controller._commit_pending_dictation()

    assert injected == [(target, "直接输入这句话")]
    assert cancelled == []
    assert controller._active_auto_interaction is None
    assert controller.interactionState == "applied"
    assert controller.modeCorrectionAvailable is True
    assert controller.modeCorrectionPending is False
    controller.switchCurrentInputMode()
    assert controller.modeCorrectionPending is True
    assert controller.appliedActionText == "正在准备指令结果…"
    pending_switches = dict(controller._pending_applied_mode_switches)
    controller.switchCurrentInputMode()
    assert controller._pending_applied_mode_switches == pending_switches
    _close(controller)


def test_late_edit_protocol_failure_retries_only_after_explicit_f8(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Desktop:
        def is_foreground(self, _target):
            return True

    controller._desktop_target = Desktop()
    context = _AutoInteraction(
        route_id=81,
        session_id=18,
        raw_text="把这句话改正式",
        target=target,
        selected_mode="dictation",
        routed_at=time.monotonic(),
        results={},
        preparing=False,
        classified=True,
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation",
            target,
            18,
            0,
            context.raw_text,
            context.raw_text,
            original_snapshot=DesktopTextSnapshot(target, ""),
            auto_context=context,
            summary="已输入原始听写",
        ),
        message="听写已应用",
    )

    controller.switchCurrentInputMode()
    assert controller.modeCorrectionPending is True

    controller._accept_auto_candidate(
        81,
        TextProcessingResult(
            request_id=82,
            session_id=18,
            mode="edit",
            raw_text=context.raw_text,
            final_text="原文本",
            latency_s=0.2,
            used_llm=True,
            target_text="原文本",
            error="两种编辑协议均失败",
        ),
        target,
    )

    assert controller.modeCorrectionPending is False
    assert controller.modeCorrectionAvailable is False
    assert controller.modeCorrectionFailed is True
    assert controller._latest_operation().mode == "dictation"
    assert "两种编辑协议均失败" in controller.appliedActionText
    submitted = []

    class Worker:
        def submit(self, request):
            submitted.append(request)

        def close(self, *, wait=False):
            return None

    controller._text_processing_worker = Worker()
    controller.switchCurrentInputMode()
    assert controller.modeCorrectionPending is True
    assert len(submitted) == 1
    assert submitted[0].mode == "edit"
    assert controller.appliedActionText == "正在重新处理指令…"
    _close(controller)


def test_first_dictation_keeps_a_reliable_empty_pre_application_snapshot(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "微信", process_id=30)

    class Desktop:
        def capture_text(self, _target):
            raise AssertionError("empty-aware capture should be preferred")

        def capture_text_allowing_empty(self, captured):
            return DesktopTextSnapshot(captured, "")

    controller._desktop_target = Desktop()
    controller._llm_enabled = False
    controller._prepare_auto_candidates(7, 3, "第一句话。", target, "dictation")

    assert controller._active_auto_interaction.snapshot == DesktopTextSnapshot(
        target, ""
    )
    assert "edit" in controller._active_auto_interaction.candidate_errors
    _close(controller)


def test_dictation_recaptures_missing_target_at_commit(tmp_path, monkeypatch):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.text_processing import TextProcessingResult

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器")
    injected = []

    class Desktop:
        def inject(self, captured, text):
            injected.append((captured, text))

    controller._desktop_target = Desktop()
    controller._desktop_output = True
    controller._capture_desktop_reference = lambda: target
    copied = []
    controller._copy_text_to_clipboard = copied.append
    controller._commit_input_text(
        TextProcessingResult(
            0, 1, "dictation", "重试目标", "重试目标", 0.0, False
        ),
        None,
    )

    assert injected == [(target, "重试目标")]
    assert copied == ["重试目标"]
    assert controller.interactionState == "applied"
    _close(controller)


def test_dictation_injection_failure_is_visible_in_overlay(tmp_path, monkeypatch):
    from proximic_ring.desktop_target import DesktopTargetRef
    from proximic_ring.text_processing import TextProcessingResult

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "失效编辑器")

    class Desktop:
        def inject(self, _target, _text):
            raise RuntimeError("文本框已关闭")

    controller._desktop_target = Desktop()
    controller._desktop_output = True
    copied = []
    controller._copy_text_to_clipboard = copied.append
    controller._commit_input_text(
        TextProcessingResult(0, 1, "dictation", "内容", "内容", 0.0, False),
        target,
    )

    assert controller.interactionState == "error"
    assert copied == ["内容"]
    assert controller.transcriptText == "未能输入文本"
    assert "注入失败：文本框已关闭" in controller.logText
    assert controller._hide_overlay_timer.isActive() is True
    _close(controller)


def test_native_undo_does_not_show_association_prompt(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _EditReview

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Desktop:
        def __init__(self):
            self.text = "原文"

        def undo(self, _target):
            self.text = "原文"

        def replace(self, snapshot, text):
            assert snapshot.target == target
            self.text = text

        def capture_text(self, captured):
            assert captured == target
            return DesktopTextSnapshot(target, self.text)

        def release_selection(self, _target):
            return None

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._edit_review = _EditReview(
        request_id=501,
        session_id=51,
        instruction="改正式一点",
        proposed_text="不满意的结果",
        snapshot=DesktopTextSnapshot(target, "原文"),
    )
    controller._set_interaction_state("review")

    controller._apply_edit_result()
    controller.undoLastApplied()

    assert desktop.text == "原文"
    assert controller.associationRecommendationVisible is False
    assert controller.transcriptVisible is False
    assert controller.undoAvailable is False
    _close(controller)


def test_manual_edit_after_failed_llm_creates_recommended_dpo_link(
    tmp_path, monkeypatch
):
    import numpy as np

    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import (
        LLMSettings,
        TextProcessingRequest,
        TextProcessingResult,
    )

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)
    collector = controller._modification_dataset
    collector.record_audio(56, np.zeros(16_000, dtype=np.float32))
    collector.record_asr_update(
        SimpleNamespace(
            session_id=56,
            backend="test-asr",
            model="test-model",
            text="把它改正式",
            is_final=True,
            latency_s=0.1,
            audio_duration_s=1.0,
            error=None,
        )
    )
    request = TextProcessingRequest(
        request_id=506,
        session_id=56,
        mode="edit",
        raw_text="把它改正式",
        target_text="原文",
        settings=LLMSettings(enabled=True, model="test-llm"),
    )
    collector.record_text_request(request)
    collector.record_llm_result(
        506,
        TextProcessingResult(
            request_id=506,
            session_id=56,
            mode="edit",
            raw_text=request.raw_text,
            final_text="失败结果",
            latency_s=0.1,
            used_llm=True,
            target_text=request.target_text,
            model_output="失败结果",
        ),
    )
    collector.record_application(
        action="undone",
        session_id=56,
        request_id=506,
        mode="edit",
        target_key=controller._association_target_key(target),
        candidate_text="失败结果",
        final_text="原文",
    )
    failed = controller._record_association_failure(
        session_id=56,
        target=target,
        mode="edit",
        status="已撤回",
    )
    assert failed is not None

    class Desktop:
        text = "原文"

        def observe_text(self, captured):
            assert captured == target
            return DesktopTextSnapshot(captured, self.text)

        def capture_text(self, _target):
            raise AssertionError("manual observation must not select or copy text")

        def release_selection(self, _target):
            raise AssertionError("manual observation must not move the caret")

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._start_manual_association_watch(target, failed, baseline="原文")
    desktop.text = "用户手动改好的正式文本"
    controller._poll_manual_association_result()
    assert controller.associationRecommendationVisible is False
    controller._manual_association_candidate_since -= 2.0
    controller._poll_manual_association_result()

    assert controller.associationRecommendationVisible is True
    assert controller.associationRecommendationTitle == "发现 1 个可关联的失败编辑"
    controller.performAssociationAction("recommendation.accept", "")

    association = collector.load_associations()[0]
    assert association["kind"] == "llm"
    assert association["chosen"]["interaction_id"] == failed.interaction_id
    assert association["chosen"]["result_id"].startswith("manual-result_")
    assert association["rejected"] == [
        {
            "interaction_id": failed.interaction_id,
            "record_path": (
                f"interactions/{failed.interaction_id}/record.json"
            ),
            "request_id": 506,
        }
    ]
    _close(controller)


def test_main_history_manual_mode_labels_one_correct_and_multiple_wrong(
    tmp_path, monkeypatch
):
    import json
    import numpy as np

    from proximic_ring.text_processing import (
        LLMSettings,
        TextProcessingRequest,
        TextProcessingResult,
    )

    controller = _controller(tmp_path, monkeypatch)
    interaction_ids = []
    for session_id, request_id, candidate in (
        (61, 601, "错误结果"),
        (62, 602, "另一个错误结果"),
        (63, 603, "正确结果"),
    ):
        collector = controller._modification_dataset
        collector.record_audio(session_id, np.zeros(16_000, dtype=np.float32))
        collector.record_asr_update(
            SimpleNamespace(
                session_id=session_id,
                backend="test-asr",
                model="test-model",
                text="把它改正式",
                is_final=True,
                latency_s=0.1,
                audio_duration_s=1.0,
                error=None,
            )
        )
        request = TextProcessingRequest(
            request_id=request_id,
            session_id=session_id,
            mode="edit",
            raw_text="把它改正式",
            target_text="原文",
            settings=LLMSettings(enabled=True, model="test-llm"),
        )
        collector.record_text_request(request)
        collector.record_llm_result(
            request_id,
            TextProcessingResult(
                request_id=request_id,
                session_id=session_id,
                mode="edit",
                raw_text=request.raw_text,
                final_text=candidate,
                latency_s=0.1,
                used_llm=True,
                target_text=request.target_text,
                model_output=candidate,
            ),
        )
        collector.record_application(
            action="applied" if candidate == "正确结果" else "undone",
            session_id=session_id,
            request_id=request_id,
            mode="edit",
            candidate_text=candidate,
            final_text=candidate,
        )
        interaction_ids.append(collector.interaction_id_for_session(session_id))

    controller.performAssociationAction("center.open", "")
    assert controller.associationCenterStage == "home"
    controller.performAssociationAction("center.create", "")
    assert controller.associationCenterStage == "type"
    controller.performAssociationAction("center.kind", "llm")
    assert controller.associationCenterVisible is True
    assert controller.associationCenterStage == "select"
    controller.performAssociationAction("center.chosen", interaction_ids[2])
    controller.performAssociationAction("center.rejected", interaction_ids[0])
    controller.performAssociationAction("center.rejected", interaction_ids[1])
    assert controller.associationCenterCanSave is True
    assert "1 个正例 · 2 个反例" in controller.associationCenterSelectionSummary
    controller.performAssociationAction("center.confirm", "")
    assert controller.associationCenterStage == "confirm"
    assert controller._modification_dataset.load_associations() == []
    assert [
        item["role"] for item in controller.associationCenterConfirmationEntries
    ] == ["chosen", "rejected", "rejected"]
    controller.performAssociationAction("center.commit", "")

    group = controller._modification_dataset.load_associations()[0]
    assert group["chosen"]["interaction_id"] == interaction_ids[2]
    assert {
        item["interaction_id"] for item in group["rejected"]
    } == set(interaction_ids[:2])
    assert group["source"] == "manual_association_center"
    assert controller.associationCenterStage == "home"
    assert controller.associationCenterLastCreatedId == group["association_id"]
    _close(controller)


def test_auto_edit_failure_is_cached_until_classification_finishes(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import (
        InputModeRoutingResult,
        TextProcessingResult,
    )

    controller = _controller(tmp_path, monkeypatch)
    routed = []
    submitted = []

    class Worker:
        def submit_routing(self, request):
            routed.append(request)

        def submit(self, request):
            submitted.append(request)

        def cancel_request(self, _request_id):
            return None

        def close(self, *, wait=False):
            return None

    target = DesktopTargetRef(1, 2, "编辑器")

    class Desktop:
        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, "旧文本")

        def inject(self, _captured, _text):
            return None

        def release_selection(self, _target):
            return None

    controller._text_processing_worker = Worker()
    controller._desktop_target = Desktop()
    controller._desktop_output = True
    controller._llm_enabled = True
    controller._input_routing_mode = "auto"
    controller._capture_desktop_reference = lambda: target

    controller._apply_runtime_update("请改一下", True, "", 41)
    edit_request = next(request for request in submitted if request.mode == "edit")
    controller._apply_text_processed(
        TextProcessingResult(
            edit_request.request_id,
            41,
            "edit",
            "请改一下",
            "旧文本",
            0.1,
            True,
            target_text="旧文本",
            error="编辑候选失败",
        )
    )

    assert controller.modeCorrectionAvailable is False
    assert controller.interactionState == "processing"

    controller._apply_input_mode_routed(
        InputModeRoutingResult(
            routed[0].request_id,
            41,
            "请改一下",
            "dictation",
            0.2,
            model_output="dictation",
        )
    )
    controller._dictation_commit_timer.stop()
    controller._commit_pending_dictation()
    assert controller.modeCorrectionAvailable is False
    assert controller.modeCorrectionFailed is True
    before_retry = len(submitted)
    controller.switchCurrentInputMode()
    assert controller._latest_operation().mode == "dictation"
    assert controller.modeCorrectionPending is True
    assert len(submitted) == before_retry + 1
    assert submitted[-1].mode == "edit"
    assert controller.appliedActionText == "正在重新处理指令…"
    _close(controller)


def test_auto_error_without_edit_target_still_allows_switch_and_cancel(
    tmp_path, monkeypatch
):
    from proximic_ring.text_processing import InputModeRoutingResult

    controller = _controller(tmp_path, monkeypatch)
    routed = []

    class Worker:
        def submit_routing(self, request):
            routed.append(request)

        def submit(self, _request):
            return None

        def cancel_request(self, _request_id):
            return None

        def close(self, *, wait=False):
            return None

    controller._text_processing_worker = Worker()
    controller._desktop_output = True
    controller._llm_enabled = False
    controller._input_routing_mode = "auto"
    controller._capture_desktop_reference = lambda: None
    controller._connected = True
    controller._recognition_enabled = True
    controller._recognition_event.set()

    controller._apply_runtime_update("把它改正式", True, "", 51)
    assert controller._interaction_recognition_suspended is True
    assert controller._recognition_event.is_set() is False
    controller._apply_input_mode_routed(
        InputModeRoutingResult(
            routed[0].request_id,
            51,
            "把它改正式",
            "edit",
            0.1,
            model_output="edit",
        )
    )

    assert controller.interactionState == "error"
    assert controller.interactionCanCancel is True
    assert controller.modeCorrectionAvailable is False
    assert controller.processingModeCorrectionAvailable is True
    assert controller._interaction_recognition_suspended is True
    assert controller._recognition_event.is_set() is False
    controller._hide_overlay_timer.stop()
    controller._hide_transcript()
    assert controller._active_auto_interaction is None
    assert controller._interaction_recognition_suspended is False
    assert controller._recognition_event.is_set() is True
    _close(controller)


def test_failed_edit_cancel_skips_readback_and_survives_release_error(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "已关闭的编辑器")

    class StaleDesktop:
        def capture_text(self, _target):
            raise AssertionError("失败修改取消不应重新读取目标")

        def release_selection(self, _target):
            raise RuntimeError("目标已失效")

    controller._desktop_target = StaleDesktop()
    result = TextProcessingResult(
        61,
        1,
        "edit",
        "改一下",
        "旧文本",
        0.1,
        True,
        target_text="旧文本",
        error="指令执行失败",
    )
    controller._begin_failed_edit_review(
        result, DesktopTextSnapshot(target, "旧文本")
    )

    assert controller.interactionState == "error"
    assert controller.interactionCanCancel is False
    _close(controller)


def test_edit_result_applies_immediately_without_preview_confirmation(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=123)
    replacements = []

    class Desktop:
        def __init__(self):
            self.text = "旧文本"

        def undo(self, _target):
            self.text = "旧文本"

        def replace(self, snapshot, text):
            replacements.append((snapshot.text, text))
            self.text = text

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def release_selection(self, _target):
            return None

    controller._desktop_target = Desktop()
    result = TextProcessingResult(
        31, 1, "edit", "改一下", "新文本", 0.1, True, target_text="旧文本"
    )
    controller._begin_edit_review(result, DesktopTextSnapshot(target, "旧文本"))
    assert replacements == [("旧文本", "新文本")]
    assert controller.interactionState == "applied"
    assert controller.undoAvailable is True
    assert controller.appliedActionTitle == "已应用上一次修改"
    assert controller.appliedActionText == '已将“旧”改为“新”'
    # A speculative alternate-mode request may still be running after the
    # application. It must not consume the first Escape as a cancellation.
    controller._pending_text_requests.add(999)
    assert controller.interactionCanCancel is False
    controller._apply_voice_action("undo")
    assert replacements == [("旧文本", "新文本")]
    assert controller._desktop_target.text == "旧文本"
    assert controller.undoAvailable is False
    assert not hasattr(controller, "confirmEdit")
    _close(controller)


def test_undo_keeps_stack_when_native_shortcut_send_raises(
    tmp_path, monkeypatch
):
    import proximic_ring.ui.controller as controller_module
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    monkeypatch.setattr(controller_module.sys, "platform", "darwin")
    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=123)

    class Desktop:
        def __init__(self):
            self.text = "新文本"
            self.replace_calls = 0

        def undo(self, _target):
            raise RuntimeError("发送失败")

        def replace(self, _snapshot, _text):
            self.replace_calls += 1
            # Simulate an editor accepting the event without changing its text.

        def capture_text(self, _captured):
            raise AssertionError("撤销验证不得触发选择或复制读取")

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def release_selection(self, _target):
            return None

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="edit",
            target=target,
            session_id=1,
            request_id=0,
            raw_text="改一下",
            applied_text="新文本",
            original_snapshot=DesktopTextSnapshot(target, "旧文本"),
        ),
        message="修改已应用",
    )

    controller.undoLastApplied()

    assert desktop.replace_calls == 0
    assert desktop.text == "新文本"
    assert controller.undoDepth == 1
    assert controller.undoAvailable is True
    assert controller.interactionState == "error"
    assert "撤销发送失败" in controller.transcriptText
    assert "撤销记录" in controller.statusDetail
    _close(controller)


def test_undo_does_not_rewrite_when_app_ignores_native_shortcut(
    tmp_path, monkeypatch
):
    import proximic_ring.ui.controller as controller_module
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    monkeypatch.setattr(controller_module.sys, "platform", "darwin")
    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=123)

    class Desktop:
        def __init__(self):
            self.text = "新文本"
            self.replace_calls = 0
            self.undo_calls = 0

        def replace(self, _snapshot, text):
            self.replace_calls += 1
            self.text = text

        def undo(self, _target):
            self.undo_calls += 1

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def observe_text(self, _captured):
            raise RuntimeError("当前文本框不支持无干扰读取")

        def release_selection(self, _target):
            return None

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="edit",
            target=target,
            session_id=1,
            request_id=0,
            raw_text="改一下",
            applied_text="新文本",
            original_snapshot=DesktopTextSnapshot(target, "旧文本"),
        ),
        message="修改已应用",
    )

    controller.undoLastApplied()

    assert desktop.undo_calls == 1
    assert desktop.replace_calls == 0
    assert desktop.text == "新文本"
    assert controller.undoDepth == 0
    assert controller.interactionState == "idle"
    _close(controller)


def test_unobservable_native_undo_never_appends_snapshot_after_command_z(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "微信", process_id=123)

    class Desktop:
        def __init__(self):
            self.text = "12345。七八小时。沙雕剧情。"
            self.undo_calls = 0
            self.replace_calls = 0
            self.undo_states = ["12345。七八小时。", ""]

        def is_foreground(self, _target):
            return True

        def observe_text(self, _target):
            raise RuntimeError("当前文本框不支持无干扰读取")

        def undo(self, _target):
            self.undo_calls += 1
            self.text = self.undo_states.pop(0)

        def replace(self, _snapshot, text):
            self.replace_calls += 1
            # This models a content-editable that ignored Select-All and would
            # therefore append a whole historical snapshot at the caret.
            self.text += text

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="dictation",
            target=target,
            session_id=2,
            request_id=0,
            raw_text="12345。七八小时。",
            applied_text="12345。七八小时。",
            original_snapshot=DesktopTextSnapshot(target, ""),
        ),
        message="第一段听写已应用",
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="dictation",
            target=target,
            session_id=3,
            request_id=0,
            raw_text="沙雕剧情",
            applied_text="沙雕剧情。",
            original_snapshot=DesktopTextSnapshot(target, "12345。七八小时。"),
        ),
        message="听写已应用",
    )

    controller.undoLastApplied()
    controller.undoLastApplied()

    assert desktop.undo_calls == 2
    assert desktop.replace_calls == 0
    assert desktop.text == ""
    assert controller.undoDepth == 0
    _close(controller)


def test_undo_sends_native_shortcut_without_snapshot_rewrite(
    tmp_path, monkeypatch
):
    import proximic_ring.ui.controller as controller_module
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    monkeypatch.setattr(controller_module.sys, "platform", "darwin")
    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "微信", process_id=123)

    class Desktop:
        def __init__(self):
            self.text = "润色后的文本"
            self.replace_calls = 0
            self.undo_calls = 0

        def replace(self, _snapshot, _text):
            self.replace_calls += 1

        def undo(self, captured):
            assert captured == target
            self.undo_calls += 1
            self.text = "润色前的文本"

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def release_selection(self, _target):
            return None

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="edit",
            target=target,
            session_id=1,
            request_id=0,
            raw_text="润色一下",
            applied_text="润色后的文本",
            original_snapshot=DesktopTextSnapshot(target, "润色前的文本"),
        ),
        message="修改已应用",
    )

    controller.undoLastApplied()

    assert desktop.replace_calls == 0
    assert desktop.undo_calls == 1
    assert desktop.text == "润色前的文本"
    assert controller.undoDepth == 0
    _close(controller)


def test_multi_undo_leaves_granularity_to_native_history(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "微信", process_id=123)

    class Desktop:
        def __init__(self):
            self.text = "第一句。第二句。"
            self.undo_calls = 0
            self.replace_calls = []

        def is_foreground(self, _target):
            return True

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def undo(self, _target):
            self.undo_calls += 1
            if self.undo_calls == 1:
                self.text = "第一句。"
            else:
                # An app can group history differently. Do not compensate
                # with a snapshot write even when its result is unexpected.
                self.text = "第一句。第二句。"

        def replace(self, _snapshot, text):
            self.replace_calls.append(text)
            self.text = text

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 1, 0, "第一句。", "第一句。",
            original_snapshot=DesktopTextSnapshot(target, ""),
        ),
        message="第一句已应用",
    )
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 2, 0, "第二句。", "第二句。",
            original_snapshot=DesktopTextSnapshot(target, "第一句。"),
        ),
        message="第二句已应用",
    )

    controller.undoLastApplied()
    assert desktop.text == "第一句。"
    assert controller.undoDepth == 1
    controller.undoLastApplied()

    assert desktop.text == "第一句。第二句。"
    assert desktop.undo_calls == 2
    assert desktop.replace_calls == []
    assert controller.undoDepth == 0
    _close(controller)


def test_legacy_first_dictation_also_uses_native_history(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "微信", process_id=123)

    class Desktop:
        text = "第一句。"
        undo_calls = 0

        def is_foreground(self, _target):
            return True

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def undo(self, _target):
            self.undo_calls += 1
            self.text = "错误恢复的旧内容"

        def replace(self, _snapshot, text):
            self.text = text

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            "dictation", target, 1, 0, "第一句。", "第一句。"
        ),
        message="第一句已应用",
    )

    controller.undoLastApplied()

    assert desktop.text == "错误恢复的旧内容"
    assert desktop.undo_calls == 1
    assert controller.undoDepth == 0
    _close(controller)


def test_unobservable_edit_undo_does_not_add_a_snapshot_write(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=123)

    class Desktop:
        def __init__(self):
            self.text = "修改后的文本"
            self.undo_calls = 0
            self.replace_calls = 0

        def undo(self, _target):
            self.undo_calls += 1
            # AX-based edits may be absent from the application's undo history.

        def replace(self, _snapshot, text):
            self.replace_calls += 1
            self.text = text

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="edit",
            target=target,
            session_id=1,
            request_id=0,
            raw_text="改一下",
            applied_text="修改后的文本",
            original_snapshot=DesktopTextSnapshot(target, "修改前的文本"),
        ),
        message="修改已应用",
    )

    controller.undoLastApplied()

    assert desktop.undo_calls == 1
    assert desktop.replace_calls == 0
    assert desktop.text == "修改后的文本"
    assert controller.undoDepth == 0
    _close(controller)


def test_undo_follows_native_history_after_manual_edit(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _AppliedInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=123)

    class Desktop:
        def __init__(self):
            self.text = "用户后来手写的文本"
            self.replace_calls = 0

        def undo(self, _target):
            self.text = "语音修改后的文本"

        def replace(self, _snapshot, _text):
            self.replace_calls += 1

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def observe_text(self, captured):
            return DesktopTextSnapshot(captured, self.text)

        def release_selection(self, _target):
            return None

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="edit",
            target=target,
            session_id=1,
            request_id=0,
            raw_text="改一下",
            applied_text="语音修改后的文本",
            original_snapshot=DesktopTextSnapshot(target, "修改前文本"),
        ),
        message="修改已应用",
    )

    controller.undoLastApplied()

    assert desktop.replace_calls == 0
    assert desktop.text == "语音修改后的文本"
    assert controller.undoDepth == 0
    assert "已发送撤销" in controller.sessionHistoryText
    _close(controller)


def test_explicit_full_delete_finishes_and_enters_undo_stack(
    tmp_path, monkeypatch
):
    import proximic_ring.ui.controller as controller_module
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult

    monkeypatch.setattr(controller_module.sys, "platform", "darwin")
    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=123)

    class Desktop:
        def __init__(self):
            self.text = "需要删除的整句话。"

        def undo(self, _target):
            self.text = "需要删除的整句话。"

        def replace(self, _snapshot, text):
            self.text = text

        def capture_text(self, captured):
            assert captured == target
            return DesktopTextSnapshot(captured, self.text)

        def capture_text_allowing_empty(self, captured):
            assert captured == target
            return DesktopTextSnapshot(captured, self.text)

        def release_selection(self, _target):
            return None

        def caret_bounds(self, _target):
            return 320, 245, 2, 18

    desktop = Desktop()
    controller._desktop_target = desktop
    result = TextProcessingResult(
        32,
        2,
        "edit",
        "删除整句话",
        "",
        0.1,
        True,
        target_text="需要删除的整句话。",
        model_output='{"modified_text":""}',
    )

    controller._begin_edit_review(
        result,
        DesktopTextSnapshot(target, "需要删除的整句话。"),
        allow_empty=True,
    )

    assert desktop.text == ""
    assert controller.interactionState == "applied"
    assert controller.undoAvailable is True
    assert controller.appliedActionText == "已清空当前文本"

    controller.undoLastApplied()
    assert desktop.text == "需要删除的整句话。"
    assert controller.undoAvailable is False
    _close(controller)


def test_unresolved_full_clear_cannot_be_reclassified_by_late_tab(
    tmp_path, monkeypatch
):
    import json

    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import TextProcessingResult
    from proximic_ring.ui.controller import _AppliedInteraction, _AutoInteraction

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(0, 0, "微信", process_id=475, process_name="WeChat")
    snapshot = DesktopTextSnapshot(target, "原文本")
    dictation = TextProcessingResult(
        0, 10, "dictation", "色情这段话。", "色情这段话。", 0.1, False
    )
    auto = _AutoInteraction(
        route_id=16,
        session_id=10,
        raw_text=dictation.raw_text,
        target=target,
        selected_mode="edit",
        routed_at=time.monotonic(),
        snapshot=snapshot,
        results={"dictation": dictation},
        classified=True,
        routed_by_model=True,
    )

    class Desktop:
        def __init__(self):
            self.injected = []
            self.replaced = []

        def is_foreground(self, _target):
            return True

        def inject(self, _target, text):
            self.injected.append(text)

        def replace(self, _snapshot, text):
            self.replaced.append(text)

    desktop = Desktop()
    controller._desktop_target = desktop
    controller._modification_dataset.record_asr_update(
        SimpleNamespace(
            session_id=10,
            text=dictation.raw_text,
            is_final=True,
            error=None,
            backend="test",
            model="test",
            latency_s=0.1,
            audio_duration_s=1.0,
        )
    )
    controller._modification_dataset.record_application(
        action="applied",
        session_id=10,
        request_id=17,
        mode="edit",
        before_text=snapshot.text,
        candidate_text="",
        final_text="",
    )
    controller._modification_dataset.record_mode_acceptance(10, mode="edit")
    controller._show_applied_interaction(
        _AppliedInteraction(
            mode="edit",
            target=target,
            session_id=10,
            request_id=17,
            raw_text=dictation.raw_text,
            applied_text="",
            original_snapshot=snapshot,
            auto_context=auto,
            summary="已清空当前文本",
        ),
        message="修改已应用",
    )

    assert controller.modeCorrectionAvailable is False
    controller.switchCurrentInputMode()

    assert desktop.injected == []
    assert desktop.replaced == []
    assert controller._latest_operation().mode == "edit"
    interaction_id = controller._modification_dataset.interaction_id_for_session(10)
    record = json.loads(
        (
            controller._modification_dataset.interactions_root
            / interaction_id
            / "record.json"
        ).read_text(encoding="utf-8")
    )
    assert record["mode"]["selected"] == "edit"
    assert record["mode"]["final_applied"] == "edit"
    assert record["mode"]["training_target"] == "edit"
    _close(controller)
