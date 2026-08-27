from __future__ import annotations

import json
from types import SimpleNamespace
import threading
import wave

import numpy as np

from proximic_ring.modification_dataset import ModificationDatasetCollector
from proximic_ring.asr.session_sink import RawAudioObserverSessionSink
from proximic_ring.text_processing import (
    EDIT_MODE_RACE,
    INPUT_MODE_EDIT,
    LLMBranchTrace,
    LLMSettings,
    OpenAICompatibleTextProcessor,
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


def test_retry_attempts_share_episode_and_persist_complete_training_trace(tmp_path):
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
    collector.feedback(10, "retry")

    collector.record_audio(2, np.zeros(800, dtype=np.float32))
    collector.record_asr_update(_update(2, "改成公文语气", final=True))
    same_episode, second_attempt = collector.begin_attempt(
        request_id=11,
        session_id=2,
        target_text="原文。",
        application="editor.exe",
        provider="local",
        model="qwen.gguf",
    )
    collector.record_llm_result(11, _result(11, 2, "公文文本。", "full"))
    collector.feedback(
        11,
        "confirm",
        final_text="用户修正后的公文文本。",
        manually_corrected=True,
    )

    assert same_episode == episode_id
    assert (first_attempt, second_attempt) == ("attempt_001", "attempt_002")
    episode_dir = tmp_path / "dataset" / "anonymous-1" / episode_id
    episode = json.loads((episode_dir / "episode.json").read_text(encoding="utf-8"))
    assert episode["attempt_ids"] == ["attempt_001", "attempt_002"]
    assert episode["final_status"] == "completed"
    assert episode["final_user_text"] == "用户修正后的公文文本。"
    assert episode["manually_corrected"] is True

    first_dir = episode_dir / "attempt_001"
    with wave.open(str(first_dir / "audio_raw.wav"), "rb") as audio_file:
        assert audio_file.getframerate() == 16_000
        assert audio_file.getnchannels() == 1
        assert audio_file.getnframes() == first_audio.size
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
    assert first_meta["feedback"][0]["action"] == "retry"
    assert "preview_dwell_ms" in first_meta["feedback"][0]
    assert first_meta["llm"]["winner_branch"] == "fragment"


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
