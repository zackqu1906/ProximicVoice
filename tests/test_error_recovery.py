from __future__ import annotations

import time

import pytest

from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
from proximic_ring.text_processing import TextProcessingResult
from test_interaction_controls import _controller, _close


@pytest.fixture
def controller(tmp_path, monkeypatch):
    value = _controller(tmp_path, monkeypatch)
    value._connected = True
    value._recognition_enabled = True
    value._recognition_event.set()
    value._capture_desktop_reference = lambda: None
    value._suspend_recognition_for_interaction()
    yield value
    _close(value)


def _prepare_failed_edit(controller, reason):
    from proximic_ring.ui.controller import _AutoInteraction, _PendingInteraction

    target = DesktopTargetRef(1, 2, "编辑器", process_id=123)
    snapshot = DesktopTextSnapshot(target, "原文")
    interaction = _AutoInteraction(
        route_id=10, session_id=10, raw_text="改一下", target=target,
        selected_mode="edit", routed_at=time.monotonic(), snapshot=snapshot,
        preparing=False, classified=True,
    )
    interaction.results["dictation"] = TextProcessingResult(
        0, 10, "dictation", "改一下", "改一下", 0.0, False,
    )
    interaction.request_ids["dictation"] = 11
    controller._pending_text_requests.add(11)
    controller._pending_interactions[11] = _PendingInteraction(
        target=target, auto_route_id=10, session_id=10,
    )
    controller._active_auto_interaction = interaction
    cancelled = []

    class Worker:
        def cancel_request(self, request_id):
            cancelled.append(request_id)

        def close(self, *, wait=False):
            pass

    class Desktop:
        def release_selection(self, _target):
            pass

    controller._text_processing_worker = Worker()
    controller._desktop_target = Desktop()
    if reason == "unreadable":
        interaction.candidate_errors["edit"] = "无法读取目标文本"
        controller._present_auto_selection()
    else:
        controller._begin_failed_edit_review(
            TextProcessingResult(
                12, 10, "edit", "改一下", "原文", 0.2, True,
                target_text="原文", error="模型请求失败" if reason == "model" else None,
            ),
            snapshot,
        )
    return interaction, cancelled


@pytest.mark.parametrize("reason", ["unreadable", "model", "noop"])
def test_error_reopens_gate_and_next_speech_retires_old_callbacks(controller, reason):
    interaction, cancelled = _prepare_failed_edit(controller, reason)
    assert controller.interactionState == "error"
    assert controller.transcriptVisible is True
    assert controller.processingModeCorrectionAvailable is True
    assert controller._recognition_event.is_set() is True

    controller._apply_runtime_status("[ASR] START t=2.000s")
    controller._apply_runtime_session_started(20)
    assert cancelled == [11]
    assert controller._active_auto_interaction is None
    assert controller.interactionState == "listening"
    assert controller._hide_overlay_timer.isActive() is False
    assert controller.processingModeCorrectionAvailable is False

    controller._apply_text_processed(TextProcessingResult(
        11, 10, "dictation", "旧语音", "迟到的旧结果", 3.0, True,
    ))
    controller._hide_transcript()  # Simulate an old timeout already dispatched.
    assert controller.interactionState == "listening"
    assert controller.transcriptVisible is True
    assert controller.transcriptText == "正在收听语音 · tap 结束"
    assert controller._recognition_event.is_set() is True


def test_error_timeout_cannot_reopen_the_next_processing_gate(controller):
    _prepare_failed_edit(controller, "model")
    controller._apply_runtime_status("[ASR] START t=2.000s")
    controller._utterance_active = False
    controller._suspend_recognition_for_interaction()
    controller._set_interaction_state("processing")
    controller._hide_transcript()
    assert controller.interactionState == "processing"
    assert controller.transcriptVisible is True
    assert controller._recognition_event.is_set() is False


def test_live_asr_failure_retires_tap_session_and_accepts_new_activation(controller):
    controller._apply_runtime_status("[ASR] START t=1.000s")
    controller._apply_runtime_session_started(10)
    controller._apply_runtime_update("", False, "连接中断", 10)
    assert controller.interactionState == "error"
    assert not controller._utterance_active
    assert controller._cancel_utterance_event.is_set()
    assert controller._recognition_event.is_set()
    controller._apply_runtime_update("迟到结果", True, "", 10)
    assert controller.interactionState == "error"
    assert controller._recognition_event.is_set()
    controller._apply_runtime_status("[ASR] START t=2.000s")
    controller._apply_runtime_session_started(11)
    controller._apply_runtime_update("新的一句", False, "", 11)
    assert controller.interactionState == "listening"
    assert controller._utterance_active
    assert controller.transcriptPrimaryText == "新的一句"


def test_choosing_error_fallback_closes_gate_until_write_finishes(controller):
    _prepare_failed_edit(controller, "model")
    assert controller._recognition_event.is_set() is True
    observed = []

    def commit(result, _target):
        observed.append(controller._recognition_event.is_set())
        assert result.mode == "dictation"
        controller._finish_auto_interaction("dictation")
        controller._resume_recognition_after_interaction()
        return True

    controller._commit_input_text = commit
    controller.switchCurrentInputMode()
    assert observed == [False]
    assert controller._recognition_event.is_set() is True
