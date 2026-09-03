from __future__ import annotations

import pytest


def test_final_utterance_blocks_next_recognition_until_routing_and_input_finish(
    tmp_path, monkeypatch
):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication

    import proximic_ring.ui.controller as controller_module
    from proximic_ring.text_processing import InputModeRoutingResult

    _app = QCoreApplication.instance() or QCoreApplication(["interaction-gate-test"])
    monkeypatch.setattr(controller_module, "app_data_root", lambda: tmp_path)
    controller = controller_module.AppController()
    controller._text_processing_worker.close(wait=True)

    routed = []

    class FakeWorker:
        def submit_routing(self, request):
            routed.append(request)

        def submit(self, request):
            raise AssertionError("dictation LLM is disabled in this test")

        def close(self, *, wait=False):
            return None

    controller._text_processing_worker = FakeWorker()
    controller._connected = True
    controller._recognition_enabled = True
    controller._recognition_event.set()
    controller._llm_enabled = False
    controller._input_routing_mode = "auto"

    controller._apply_runtime_update("把这句话输入进去", True, "", 41)
    assert controller._interaction_recognition_suspended is True
    assert controller._recognition_event.is_set() is False
    assert len(routed) == 1

    controller._apply_input_mode_routed(
        InputModeRoutingResult(
            request_id=routed[0].request_id,
            session_id=41,
            raw_text=routed[0].raw_text,
            mode="dictation",
            latency_s=0.1,
            model_output="dictation",
        )
    )
    assert controller.modeCorrectionAvailable is True
    assert controller._interaction_recognition_suspended is True
    controller._dictation_commit_timer.stop()
    controller._commit_pending_dictation()
    assert controller._interaction_recognition_suspended is False
    assert controller._recognition_event.is_set() is True
    controller._voice_history.close(wait=True)
