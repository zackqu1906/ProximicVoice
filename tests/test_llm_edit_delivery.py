import io
import json
import threading

import pytest

from proximic_ring.text_processing import (
    EDIT_MODE_RACE, INPUT_MODE_EDIT, LLMSettings, OpenAICompatibleTextProcessor,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


@pytest.mark.parametrize("source_size,result_size,target", [(79, 20, 20), (36, 58, 50), (80, 89, 100)])
@pytest.mark.parametrize("entry", ["trace", "collection", "callback"])
def test_changed_result_is_not_rejected_by_hard_expansion_length(source_size, result_size, target, entry):
    source = "甲" * source_size
    result = "乙" * result_size

    def urlopen(request, *, timeout):
        body = json.loads(request.data)
        fields = body["tools"][0]["function"]["parameters"]["properties"]
        args = {"modified_text": result}
        if "original_text" in fields:
            args["original_text"] = source
        return Response(json.dumps({"choices": [{"message": {"tool_calls": [{
            "type": "function", "function": {"name": "submit_text_edit", "arguments": args}
        }]}}]}).encode())

    p = OpenAICompatibleTextProcessor(urlopen=urlopen)
    args = (f"扩写到{target}个字。", INPUT_MODE_EDIT,
            LLMSettings(enabled=True, model="test", api_key_env=""), source, EDIT_MODE_RACE)
    if entry == "trace":
        actual, _ = p.process_with_trace(*args)
    else:
        done = threading.Event()
        actual, *_ = p.process_with_collection_trace(
            *args, on_collection_complete=(lambda *_: done.set()) if entry == "callback" else None)
        if entry == "callback":
            assert done.wait(1)
    assert actual == result


@pytest.mark.parametrize("entry", ["trace", "collection"])
def test_fragment_edit_preserves_original_indentation_and_trailing_newlines(entry):
    source = "  第一句。最后一句。\n\n"

    def urlopen(request, *, timeout):
        body = json.loads(request.data)
        assert source in body["messages"][-1]["content"]
        fields = body["tools"][0]["function"]["parameters"]["properties"]
        args = ({"original_text": "最后一句。", "modified_text": ""} if "original_text" in fields
                else {"modified_text": "  第一句。\n\n"})
        return Response(json.dumps({"choices": [{"message": {"tool_calls": [{
            "type": "function", "function": {"name": "submit_text_edit", "arguments": args}
        }]}}]}).encode())

    p = OpenAICompatibleTextProcessor(urlopen=urlopen)
    args = ("删掉最后一句话。", INPUT_MODE_EDIT,
            LLMSettings(enabled=True, model="test", api_key_env=""), source, EDIT_MODE_RACE)
    method = p.process_with_trace if entry == "trace" else p.process_with_collection_trace
    actual, *_ = method(*args)
    assert actual == "  第一句。\n\n"
