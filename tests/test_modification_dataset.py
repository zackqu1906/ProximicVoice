from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
import threading
import time
import wave

import numpy as np
import pytest

from proximic_ring.modification_dataset import ModificationDatasetCollector
from proximic_ring.asr.session_sink import RawAudioObserverSessionSink
from proximic_ring.text_processing import (
    EDIT_MODE_RACE,
    INPUT_MODE_EDIT,
    LLMBranchTrace,
    LLMSettings,
    OpenAICompatibleTextProcessor,
    TextProcessingRequest,
    TextProcessingResult,
)


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


def _update(session_id: int, text: str, *, final: bool):
    return SimpleNamespace(
        session_id=session_id,
        backend="funasr_nano",
        model="Fun-ASR-Nano-2512",
        text=text,
        is_final=final,
        latency_s=0.12,
        audio_duration_s=1.5,
        error=None,
    )


def _result(request_id: int, session_id: int, candidate: str, winner: str):
    return TextProcessingResult(
        request_id=request_id,
        session_id=session_id,
        mode="edit",
        raw_text="改得更正式",
        final_text=candidate,
        latency_s=0.4,
        used_llm=True,
        target_text="原文。",
        model_output='{"modified_text":"正式文本。"}',
        winner_branch=winner,
        llm_branches=(
            LLMBranchTrace(
                branch="fragment",
                raw_returns=(' {"original_text":"原文。","modified_text":"正式文本。"}',),
                validation="valid",
                candidate_text="正式文本。",
                latency_s=0.3,
                won=winner == "fragment",
            ),
            LLMBranchTrace(
                branch="full",
                raw_returns=(' {"modified_text":"更正式的文本。"}',),
                validation="valid",
                candidate_text="更正式的文本。",
                latency_s=0.4,
                won=winner == "full",
            ),
        ),
    )


def test_confirm_persists_one_complete_training_attempt_and_retry_is_rejected(tmp_path):
    collector = ModificationDatasetCollector(tmp_path / "dataset", "anonymous-1")
    first_audio = np.linspace(-0.25, 0.25, 1600, dtype=np.float32)
    collector.record_audio(1, first_audio)
    collector.record_asr_update(_update(1, "改得", final=False))
    collector.record_asr_update(_update(1, "改得更正式", final=True))
    episode_id, first_attempt = collector.begin_attempt(
        request_id=10,
        session_id=1,
        target_text="原文。",
        application="editor.exe",
        provider="local",
        model="qwen.gguf",
    )
    collector.record_llm_result(10, _result(10, 1, "正式文本。", "fragment"))
    collector.feedback(
        10,
        "confirm",
        final_text="用户修正后的正式文本。",
        manually_corrected=True,
    )
    first_attempt_path = (
        tmp_path
        / "dataset"
        / "anonymous-1"
        / episode_id
        / first_attempt
        / "attempt.json"
    )
    episode_dir = tmp_path / "dataset" / "anonymous-1" / episode_id
    episode = json.loads((episode_dir / "episode.json").read_text(encoding="utf-8"))
    assert episode["attempt_ids"] == ["attempt_001"]
    assert episode["final_status"] == "completed"
    assert episode["final_user_text"] == "用户修正后的正式文本。"
    assert episode["manually_corrected"] is True

    first_dir = episode_dir / "attempt_001"
    with wave.open(str(first_dir / "audio_raw.wav"), "rb") as audio_file:
        assert audio_file.getframerate() == 16_000
        assert audio_file.getnchannels() == 1
        assert audio_file.getnframes() == first_audio.size
    interaction_audio = next(
        (collector.interactions_root).glob("*/audio.wav")
    )
    assert (first_dir / "audio_raw.wav").samefile(interaction_audio)
    asr_rows = [
        json.loads(line)
        for line in (first_dir / "asr_updates.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["kind"] for row in asr_rows] == ["partial", "final"]
    assert asr_rows[-1]["text"] == "改得更正式"
    branch_rows = [
        json.loads(line)
        for line in (first_dir / "llm_branches.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["branch"] for row in branch_rows} == {"fragment", "full"}
    assert all(row["candidate_text"] for row in branch_rows)
    first_meta = json.loads((first_dir / "attempt.json").read_text(encoding="utf-8"))
    assert first_meta["feedback"][0]["action"] == "confirm"
    assert "preview_dwell_ms" in first_meta["feedback"][0]
    assert first_meta["llm"]["winner_branch"] == "fragment"
    with pytest.raises(ValueError, match="unsupported feedback action"):
        collector.feedback(10, "retry")


def test_cancel_records_objective_action_without_reason_prompt_data(tmp_path):
    collector = ModificationDatasetCollector(tmp_path / "dataset", "anonymous-1")
    episode_id, attempt_id = collector.begin_attempt(
        request_id=20,
        session_id=2,
        target_text="原文。",
        application="editor.exe",
        provider="local",
        model="qwen.gguf",
    )
    collector.feedback(20, "cancel", final_text="原文。")
    attempt_path = (
        tmp_path
        / "dataset"
        / "anonymous-1"
        / episode_id
        / attempt_id
        / "attempt.json"
    )
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    assert attempt["feedback"][-1]["action"] == "cancel"
    assert "failure_reason" not in attempt["feedback"][-1]
    assert not hasattr(collector, "annotate_feedback_reason")


def test_slow_branch_trace_can_fill_attempt_after_winner_is_available(tmp_path):
    collector = ModificationDatasetCollector(tmp_path / "dataset", "anonymous-1")
    episode_id, attempt_id = collector.begin_attempt(
        request_id=30,
        session_id=3,
        target_text="原文。",
        application="editor.exe",
        provider="local",
        model="qwen.gguf",
    )
    completed = _result(30, 3, "正式文本。", "fragment")
    collector.record_llm_result(30, replace(completed, llm_branches=()))
    branch_path = (
        tmp_path
        / "dataset"
        / "anonymous-1"
        / episode_id
        / attempt_id
        / "llm_branches.jsonl"
    )
    assert branch_path.read_text(encoding="utf-8") == ""

    collector.record_llm_branches(
        30,
        completed.llm_branches,
        completed.winner_branch,
    )
    branch_rows = [
        json.loads(line)
        for line in branch_path.read_text(encoding="utf-8").splitlines()
    ]
    assert {row["branch"] for row in branch_rows} == {"fragment", "full"}
    attempt = json.loads(
        (branch_path.parent / "attempt.json").read_text(encoding="utf-8")
    )
    assert attempt["llm"]["winner_branch"] == "fragment"
    assert "branches_collected_at" in attempt["llm"]


def test_unified_interaction_collects_history_llm_outcome_reason_and_imu(tmp_path):
    saved = []
    collector = ModificationDatasetCollector(
        tmp_path / "dataset", "anonymous-1", on_saved=saved.append
    )
    collector.record_runtime_event(
        "STAGE2 sample=100 score=+0.900 threshold=0.700 ACTIVATE"
    )
    collector.record_audio(9, np.zeros(1600, dtype=np.float32))
    collector.record_asr_update(_update(9, "帮我整理一下", final=False))
    collector.record_asr_update(_update(9, "帮我整理一下。", final=True))

    request = TextProcessingRequest(
        request_id=90,
        session_id=9,
        mode="dictation",
        raw_text="帮我整理一下。",
        settings=LLMSettings(
            enabled=True,
            provider="openai",
            model="test-model",
            api_key="must-not-be-stored",
        ),
    )
    collector.record_text_request(request)
    result = TextProcessingResult(
        request_id=90,
        session_id=9,
        mode="dictation",
        raw_text=request.raw_text,
        final_text="请帮我整理一下。",
        latency_s=0.2,
        used_llm=True,
        model_output="请帮我整理一下。",
    )
    collector.record_llm_result(90, result)
    collector.record_application(
        action="applied",
        session_id=9,
        request_id=90,
        mode="dictation",
        candidate_text=result.final_text,
        final_text=result.final_text,
    )
    applied_record_path = next(collector.interactions_root.glob("*/record.json"))
    applied_record = json.loads(applied_record_path.read_text(encoding="utf-8"))
    assert applied_record["outcome"]["accepted"] is None
    assert applied_record["outcome"]["acceptance_strength"] == "pending_undo"
    collector.feedback(90, "cancel", final_text="")
    collector.record_application(
        action="undone",
        session_id=9,
        request_id=90,
        mode="dictation",
    )
    collector.record_imu_samples(
        9,
        [
            {"timestamp_ms": 0, "ax": 0.1, "ay": 0.2, "az": 0.9},
            {"timestamp_ms": 10, "ax": 0.1, "ay": 0.2, "az": 1.0},
        ],
        sample_rate_hz=100,
        clock_offset_ms=2.5,
    )

    assert len(saved) == 1
    assert len(collector.load_entries()) == 1
    record_path = next(collector.interactions_root.glob("*/record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["asr"]["final_text"] == "帮我整理一下。"
    assert record["llm"]["requests"][0]["input"]["user_content"] == request.raw_text
    assert record["llm"]["requests"][0]["raw_output"] == result.model_output
    assert "must-not-be-stored" not in record_path.read_text(encoding="utf-8")
    assert record["outcome"]["status"] == "undone"
    assert record["outcome"]["accepted"] is False
    assert "failure_reason" not in record["outcome"]
    assert record["near_field"]["audio_score"] == 0.9
    assert record["near_field"]["stage2_threshold"] == 0.7
    assert record["near_field"]["detector_decision"] == "activate"
    assert record["imu"]["sample_count"] == 2
    assert (record_path.parent / "imu.jsonl").is_file()


def test_llm_association_index_links_rejected_and_manual_chosen_result(tmp_path):
    collector = ModificationDatasetCollector(tmp_path / "dataset", "anonymous-1")
    interaction_ids = []
    for session_id, request_id, instruction, candidate in (
        (1, 101, "改正式一点", "失败结果一"),
        (2, 102, "把它写得正式", "失败结果二"),
    ):
        collector.record_asr_update(_update(session_id, instruction, final=True))
        request = TextProcessingRequest(
            request_id=request_id,
            session_id=session_id,
            mode="edit",
            raw_text=instruction,
            target_text="共同原文",
            settings=LLMSettings(enabled=True, model="test-model"),
        )
        collector.record_text_request(request)
        collector.record_llm_result(
            request_id,
            TextProcessingResult(
                request_id=request_id,
                session_id=session_id,
                mode="edit",
                raw_text=instruction,
                final_text=candidate,
                latency_s=0.1,
                used_llm=True,
                target_text="共同原文",
                model_output=candidate,
            ),
        )
        collector.record_application(
            action="undone",
            session_id=session_id,
            request_id=request_id,
            mode="edit",
            before_text=candidate,
            final_text="共同原文",
        )
        interaction_ids.append(collector.interaction_id_for_session(session_id))

    result_id = collector.record_manual_result(
        interaction_ids[-1], text="人工写出的正式文本", mode="edit"
    )
    group_id = collector.create_association(
        kind="llm",
        subtype="edit_preference",
        chosen={"interaction_id": interaction_ids[-1], "result_id": result_id},
        rejected=[
            {"interaction_id": interaction_ids[0], "request_id": 101},
            {"interaction_id": interaction_ids[1], "request_id": 102},
        ],
        source="manual_association_center",
    )
    group = collector.load_associations()[0]
    assert group["association_id"] == group_id
    assert group["source"] == "manual_association_center"
    assert group["chosen"]["result_id"] == result_id
    assert [item["request_id"] for item in group["rejected"]] == [101, 102]


def test_asr_association_index_points_to_all_original_interactions(tmp_path):
    collector = ModificationDatasetCollector(tmp_path / "dataset", "anonymous-1")
    for session_id in (1, 2):
        collector.record_audio(session_id, np.zeros(16_000, dtype=np.float32))
        collector.record_asr_update(_update(session_id, "", final=True))
        time.sleep(0.002)
    collector.record_audio(3, np.zeros(16_000, dtype=np.float32))
    collector.record_asr_update(_update(3, "今天下午三点开会", final=True))

    failed_ids = [
        collector.interaction_id_for_session(1),
        collector.interaction_id_for_session(2),
    ]
    reference_id = collector.interaction_id_for_session(3)
    group_id = collector.create_association(
        kind="asr",
        subtype="dictation_retry",
        relation_type="probable_exact_repeat",
        chosen={"interaction_id": reference_id},
        rejected=[{"interaction_id": item} for item in failed_ids],
        source="auto_recommended",
    )
    group = collector.load_associations()[0]
    assert group["association_id"] == group_id
    assert group["kind"] == "asr"
    assert group["chosen"]["interaction_id"] == reference_id
    assert group["member_interaction_ids"] == [reference_id, *failed_ids]
    for interaction_id in group["member_interaction_ids"]:
        record = json.loads(
            (collector.interactions_root / interaction_id / "record.json").read_text(
                encoding="utf-8"
            )
        )
        assert group_id in record["association_ids"]
    assert collector.association_index_path.is_file()


def test_collection_race_waits_for_and_records_both_parallel_branches():
    barrier = threading.Barrier(2)

    def urlopen(http_request, *, timeout):
        del timeout
        body = json.loads(http_request.data.decode("utf-8"))
        properties = body["tools"][0]["function"]["parameters"]["properties"]
        fragment = "original_text" in properties
        barrier.wait(timeout=1.0)
        if fragment:
            threading.Event().wait(0.05)
            arguments = {"original_text": "原文", "modified_text": "片段结果"}
        else:
            arguments = {"modified_text": "全文结果。"}
        payload = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "type": "function",
                        "function": {
                            "name": "submit_text_edit",
                            "arguments": arguments,
                        },
                    }]
                }
            }]
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, raw_output, branches, winner = processor.process_with_collection_trace(
        "改一下",
        INPUT_MODE_EDIT,
        LLMSettings(enabled=True, model="test", api_key_env=""),
        "原文。",
        EDIT_MODE_RACE,
    )

    assert final_text == "全文结果。"
    assert json.loads(raw_output) == {"modified_text": "全文结果。"}
    assert winner == "full"
    assert {branch.branch for branch in branches} == {"fragment", "full"}
    assert all(branch.validation == "valid" for branch in branches)
    assert all(branch.raw_returns for branch in branches)
    assert next(branch for branch in branches if branch.branch == "fragment").candidate_text == "片段结果。"


def test_collection_callback_returns_winner_before_slower_branch_finishes():
    release_full = threading.Event()
    trace_finished = threading.Event()
    collected = []

    def urlopen(http_request, *, timeout):
        del timeout
        body = json.loads(http_request.data.decode("utf-8"))
        properties = body["tools"][0]["function"]["parameters"]["properties"]
        fragment = "original_text" in properties
        if fragment:
            arguments = {"original_text": "原文。", "modified_text": "快速结果。"}
        else:
            release_full.wait(timeout=2.0)
            arguments = {"modified_text": "较慢的完整结果。"}
        payload = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "type": "function",
                        "function": {
                            "name": "submit_text_edit",
                            "arguments": arguments,
                        },
                    }]
                }
            }]
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    def on_collection_complete(branches, winner):
        collected.append((branches, winner))
        trace_finished.set()

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    started = time.perf_counter()
    final_text, _raw_output, immediate_branches, winner = (
        processor.process_with_collection_trace(
            "改一下",
            INPUT_MODE_EDIT,
            LLMSettings(enabled=True, model="test", api_key_env=""),
            "原文。",
            EDIT_MODE_RACE,
            on_collection_complete=on_collection_complete,
        )
    )
    elapsed = time.perf_counter() - started

    assert final_text == "快速结果。"
    assert winner == "fragment"
    assert immediate_branches == ()
    assert elapsed < 1.0
    assert trace_finished.is_set() is False

    release_full.set()
    assert trace_finished.wait(timeout=1.0)
    branches, collected_winner = collected[0]
    assert collected_winner == "fragment"
    assert {branch.branch for branch in branches} == {"fragment", "full"}
    assert sum(branch.won for branch in branches) == 1


def test_unchanged_branch_cannot_beat_a_later_executable_edit():
    def urlopen(http_request, *, timeout):
        del timeout
        body = json.loads(http_request.data.decode("utf-8"))
        properties = body["tools"][0]["function"]["parameters"]["properties"]
        if "original_text" in properties:
            arguments = {"original_text": "原文。", "modified_text": "原文。"}
        else:
            arguments = {"modified_text": "真正修改后的文本。"}
        payload = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "type": "function",
                        "function": {
                            "name": "submit_text_edit",
                            "arguments": arguments,
                        },
                    }]
                }
            }]
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, _raw_output, branches, winner = (
        processor.process_with_collection_trace(
            "请修改",
            INPUT_MODE_EDIT,
            LLMSettings(enabled=True, model="test", api_key_env=""),
            "原文。",
            EDIT_MODE_RACE,
        )
    )

    assert final_text == "真正修改后的文本。"
    assert winner == "full"
    fragment = next(branch for branch in branches if branch.branch == "fragment")
    assert fragment.validation == "invalid"
    assert fragment.error == "大模型未找到可可靠执行的修改"


def test_raw_audio_observer_sink_keeps_controller_waveform_unchanged():
    observed = []
    sink = RawAudioObserverSessionSink(
        lambda session_id, audio: observed.append((session_id, audio))
    )
    raw = np.array([-0.5, 0.25], dtype=np.float32)
    sink.start(raw[:1])
    sink.feed(raw[1:])
    sink.end(raw)

    assert observed[0][0] == 1
    np.testing.assert_array_equal(observed[0][1], raw)
