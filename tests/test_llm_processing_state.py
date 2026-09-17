import pytest

from proximic_ring.text_processing import TextProcessingResult
from test_interaction_controls import _close, _controller


@pytest.fixture
def processing_controller(tmp_path, monkeypatch):
    from proximic_ring.ui.controller import _AutoInteraction, _PendingInteraction

    controller = _controller(tmp_path, monkeypatch)
    controller._latest_asr_session_id = 1
    controller._transcript_visible = True
    controller._interaction_state = "processing"
    controller._pending_text_requests.update((11, 12))
    controller._pending_interactions.update({
        11: _PendingInteraction(auto_route_id=10, session_id=1),
        12: _PendingInteraction(auto_route_id=10, session_id=1),
    })
    controller._active_auto_interaction = _AutoInteraction(
        route_id=10, session_id=1, raw_text="润色这段话", target=None,
        selected_mode="edit", routed_at=0, classified=True,
        request_ids={"edit": 11, "dictation": 12},
    )
    try:
        yield controller
    finally:
        _close(controller)


@pytest.mark.parametrize("state", ["idle", "listening", "error", "cancelled", "applied"])
def test_background_llm_does_not_show_processing_in_other_stages(processing_controller, state):
    controller = processing_controller
    assert controller.llmTextProcessing
    controller._set_interaction_state(state)
    assert not controller.llmTextProcessing


def test_routing_only_and_old_session_never_show_text_processing(processing_controller):
    controller = processing_controller
    interaction = controller._active_auto_interaction
    interaction.classified = False
    controller._pending_mode_routes.add(10)
    assert controller.textProcessing
    assert not controller.llmTextProcessing
    interaction.classified = True
    interaction.session_id = 99
    assert not controller.llmTextProcessing


@pytest.mark.parametrize("error", [None, "model unavailable"])
def test_selected_result_clears_indicator_even_with_alternate_pending(processing_controller, error):
    controller = processing_controller
    notifications = []
    controller.llmTextProcessingChanged.connect(lambda: notifications.append(controller.llmTextProcessing))
    controller._apply_text_processed(TextProcessingResult(
        request_id=11, session_id=1, mode="edit", raw_text="润色这段话",
        final_text="修改后的内容", latency_s=0.2, used_llm=True,
        target_text="原文", error=error,
    ))
    assert controller._pending_text_requests == {12}
    assert controller.textProcessing
    assert notifications and not notifications[0]
    assert not controller.llmTextProcessing


def test_dictation_paste_is_not_labelled_as_llm_processing(processing_controller, monkeypatch):
    controller = processing_controller
    controller._active_auto_interaction.selected_mode = "dictation"
    result = TextProcessingResult(12, 1, "dictation", "原文", "原文", 0, False)
    controller._pending_dictation_result = result, None
    assert controller.llmTextProcessing
    during_paste = []
    monkeypatch.setattr(controller, "_commit_input_text", lambda *_: during_paste.append(controller.llmTextProcessing))
    controller._commit_pending_dictation()
    assert during_paste == [False]
    assert controller._pending_text_requests == {11, 12}


def test_cancel_clears_indicator_without_waiting_for_http(processing_controller):
    controller = processing_controller
    assert controller.llmTextProcessing
    controller.cancelCurrentUtterance()
    assert not controller.llmTextProcessing


def test_disconnect_clears_indicator(processing_controller):
    controller = processing_controller
    controller._disconnect_event.set()
    assert not controller.llmTextProcessing
