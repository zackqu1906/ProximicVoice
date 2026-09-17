import io
import json
import threading

import pytest

from proximic_ring.text_processing import (
    EDIT_MODE_RACE,
    INPUT_MODE_EDIT,
    LLMSettings,
    OpenAICompatibleTextProcessor,
)
from proximic_ring.text_processing.edit_constraints import validate_expansion_result
from proximic_ring.text_processing.llm import LLMResponseProcessingError


@pytest.mark.parametrize("instruction,count", [
    ("扩写到100个字。", 100),
    ("扩写到一百个字。", 100),
    ("请帮我把这段话扩写至一百零五字！", 105),
    ("将全文扩写成两百字", 200),
    ("扩写到一百二十字", 120),
])
def test_explicit_expansion_count_and_increase(instruction, count):
    source = "甲" * 80
    validate_expansion_result(instruction, source, "乙" * count)
    with pytest.raises(ValueError, match="字数要求"):
        validate_expansion_result(instruction, source, "乙" * 80)
    with pytest.raises(ValueError, match="字数要求"):
        validate_expansion_result(instruction, source, "乙" * (count * 2))


@pytest.mark.parametrize("instruction", [
    "润色一下", "不要扩写，只修改标点", "把扩写到100字改成缩写到100字",
    "只把第二句扩写到100字", "扩写到大约100字", "扩写到一百二字", "扩写到一二字",
    "扩写到100字，再翻译成英文", "扩写到0字",
])
def test_other_or_ambiguous_instructions_are_not_guessed(instruction):
    validate_expansion_result(instruction, "甲" * 80, "乙" * 80)


@pytest.mark.parametrize("count", [90, 100, 110])
def test_length_boundaries_and_whitespace(count):
    validate_expansion_result("扩写到100字", "甲" * 80, "\n".join("乙" * count))


@pytest.mark.parametrize("count", [89, 111])
def test_outside_length_boundaries_is_rejected(count):
    with pytest.raises(ValueError, match="90～110"):
        validate_expansion_result("扩写到100字", "甲" * 80, "乙" * count)


def test_in_range_but_not_expanded_is_rejected():
    with pytest.raises(ValueError, match="增加篇幅"):
        validate_expansion_result("扩写到100字", "甲" * 100, "乙" * 100)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _processor(fragment_count):
    calls = []
    source = "甲" * 80
    full_requested = threading.Event()

    def urlopen(request, *, timeout):
        body = json.loads(request.data)
        is_fragment = "original_text" in body["tools"][0]["function"]["parameters"]["properties"]
        calls.append("fragment" if is_fragment else "full")
        if is_fragment:
            assert full_requested.wait(1)
            # Reproduce a faster, ineffective full response and slower expansion.
            threading.Event().wait(0.02)
            args = {"original_text": source, "modified_text": "乙" * fragment_count}
        else:
            full_requested.set()
            args = {"modified_text": source[:-1] + "丙"}
        return _Response(json.dumps({"choices": [{"message": {"tool_calls": [{
            "type": "function", "function": {"name": "submit_text_edit", "arguments": args}
        }]}}]}).encode())

    return OpenAICompatibleTextProcessor(urlopen=urlopen), calls, source


@pytest.mark.parametrize("entry", ["trace", "collection", "callback"])
def test_fast_ineffective_expansion_cannot_win_the_race(entry):
    processor, calls, source = _processor(100)
    args = ("扩写到100个字。", INPUT_MODE_EDIT,
            LLMSettings(enabled=True, model="test", api_key_env=""), source, EDIT_MODE_RACE)
    if entry == "trace":
        result, _ = processor.process_with_trace(*args)
    else:
        completed = threading.Event()
        saved = []

        def finish(traces, winner):
            saved.append((traces, winner))
            completed.set()

        result, _, traces, winner = processor.process_with_collection_trace(
            *args, on_collection_complete=finish if entry == "callback" else None
        )
        if entry == "callback":
            assert completed.wait(1)
            traces, winner = saved[0]
        assert winner == "fragment"
        full = next(t for t in traces if t.branch == "full")
        assert full.validation == "invalid"
        assert "结果80字" in full.error
    assert result == "乙" * 100
    assert sorted(calls) == ["fragment", "full"]


def test_two_wrong_lengths_fail_instead_of_producing_an_applied_candidate():
    processor, calls, source = _processor(123)
    with pytest.raises(LLMResponseProcessingError, match="字数要求") as error:
        processor.process_with_collection_trace(
            "扩写到100个字。", INPUT_MODE_EDIT,
            LLMSettings(enabled=True, model="test", api_key_env=""), source, EDIT_MODE_RACE,
        )
    assert all(t.validation == "invalid" for t in error.value.branch_traces)
    assert len(error.value.model_outputs) == 2
    assert sorted(calls) == ["fragment", "full"]
