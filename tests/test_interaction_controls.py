from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


def _controller(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    import proximic_ring.ui.controller as controller_module

    _app = QCoreApplication.instance() or QCoreApplication(["interaction-controls"])
    monkeypatch.setattr(controller_module, "app_data_root", lambda: tmp_path)
    controller = controller_module.AppController()
    controller._text_processing_worker.close(wait=True)
    return controller


def _close(controller):
    controller._text_processing_worker.close(wait=True)
    controller._close_voice_history()


def test_overlay_starts_on_detected_voice_and_cancel_ignores_stale_asr(
    tmp_path, monkeypatch
):
    controller = _controller(tmp_path, monkeypatch)
    controller._recognition_enabled = True
    controller._desktop_output = False

    controller._apply_runtime_status("[ASR] START t=1.000s (pre-roll=0.40s)")
    assert controller.transcriptVisible is True
    assert controller.transcriptText == "正在收听语音"
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
    assert record["mode"]["label_source"] == "explicit_tab"
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


def test_pre_application_cancel_labels_near_field_not_asr(tmp_path, monkeypatch):
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
    assert record["near_field"]["training_label"] == "negative"
    assert record["near_field"]["label_source"] == "pre_application_cancel"
    assert record["asr"]["training_label"] is None
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
    controller._poll_applied_target_foreground()
    assert controller.appliedActionVisible is False
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
    before = len(submitted)
    controller.switchCurrentInputMode()
    assert len(submitted) == before
    assert controller._latest_operation().mode == "edit"
    assert desktop.text == "新的正式文本"
    assert controller.modeCorrectionLabel == "刚刚是输入内容"
    interaction_id = controller._modification_dataset.interaction_id_for_session(21)
    record = json.loads(
        (
            controller._modification_dataset.interactions_root
            / interaction_id
            / "record.json"
        ).read_text(encoding="utf-8")
    )
    assert record["mode"]["selected"] == "edit"
    assert record["mode"]["negative_mode"] == "dictation"
    assert record["mode"]["label_source"] == "explicit_tab"
    assert record["asr"]["training_label"] == "positive"
    assert record["near_field"]["training_label"] == "positive"
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


def test_undo_only_records_failure_and_does_not_show_association_prompt(
    tmp_path, monkeypatch
):
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import _EditReview

    controller = _controller(tmp_path, monkeypatch)
    target = DesktopTargetRef(1, 2, "编辑器", process_id=30)

    class Desktop:
        def __init__(self):
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
    assert controller.modeCorrectionAvailable is True
    controller.switchCurrentInputMode()
    assert controller._latest_operation().mode == "dictation"
    assert controller.appliedActionText == "另一种处理方式不可用"
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

    controller._apply_runtime_update("把它改正式", True, "", 51)
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
        def replace(self, snapshot, text):
            replacements.append((snapshot.text, text))

        def capture_text(self, captured):
            return DesktopTextSnapshot(captured, "新文本")

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
    assert controller.appliedActionText == '已将“旧”改为“新”'
    assert not hasattr(controller, "confirmEdit")
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

        def replace(self, _snapshot, text):
            self.text = text

        def capture_text(self, _target):
            raise AssertionError("清空结果不应使用非空文本读取路径")

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
