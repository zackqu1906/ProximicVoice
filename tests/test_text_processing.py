import io
import json
import threading
import time

import pytest

from proximic_ring.text_processing import (
    INPUT_MODE_DICTATION,
    INPUT_MODE_EDIT,
    InputModeRoutingRequest,
    EDIT_MODE_FULL,
    EDIT_MODE_RACE,
    LLM_PROVIDER_LOCAL,
    LLM_PROVIDER_VOLCENGINE,
    LLMSettings,
    MAX_EDIT_TARGET_CHARS,
    OpenAICompatibleTextProcessor,
    TextProcessingRequest,
    TextProcessingWorker,
    validate_edit_target_text,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_openai_compatible_processor_uses_mode_specific_prompt(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret-value")
    requests = []

    def urlopen(http_request, *, timeout):
        requests.append((http_request, timeout))
        request_body = json.loads(http_request.data.decode("utf-8"))
        is_edit = "submit_text_edit" in request_body["messages"][0]["content"]
        if is_edit:
            message = {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "submit_text_edit",
                            "arguments": {
                                "original_text": "这是原来的草稿",
                                "modified_text": "这是正式的原草稿。",
                            },
                        },
                    }
                ]
            }
        else:
            message = {"content": "整理后的文本。"}
        payload = {"choices": [{"message": message}]}
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    settings = LLMSettings(
        enabled=True,
        base_url="https://llm.example/v1/",
        model="example-model",
        api_key_env="TEST_LLM_KEY",
        timeout_s=12.0,
    )

    result = processor.process("原始口述", INPUT_MODE_DICTATION, settings)

    assert result == "整理后的文本。"
    http_request, timeout = requests[0]
    assert http_request.full_url == "https://llm.example/v1/chat/completions"
    assert timeout == 12.0
    assert http_request.headers["Authorization"] == "Bearer secret-value"
    body = json.loads(http_request.data.decode("utf-8"))
    assert body["model"] == "example-model"
    assert "不得增加信息" in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == "原始口述"

    edit_result = processor.process(
        "改得正式一点", INPUT_MODE_EDIT, settings, "这是原来的草稿"
    )
    assert edit_result == "这是正式的原草稿。"
    edit_body = json.loads(requests[1][0].data.decode("utf-8"))
    assert "modified_text：用于替换 original_text 的新片段" in (
        edit_body["messages"][0]["content"]
    )
    assert "<待修改文本>\n这是原来的草稿" in edit_body["messages"][1]["content"]
    assert "<修改要求>\n改得正式一点" in edit_body["messages"][1]["content"]
    function = edit_body["tools"][0]["function"]
    assert function["name"] == "submit_text_edit"
    assert set(function["parameters"]["properties"]) == {
        "original_text",
        "modified_text",
    }
    assert edit_body["tool_choice"] == "required"
    assert "必须真正调用 submit_text_edit 工具" in (
        edit_body["messages"][0]["content"]
    )
    assert "不要主动翻译" in body["messages"][0]["content"]


def test_direct_llm_api_key_from_ui_takes_precedence_over_environment(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "environment-key")
    captured = []

    def urlopen(http_request, *, timeout):
        captured.append(http_request)
        return _Response(
            json.dumps(
                {"choices": [{"message": {"content": "整理后的文本。"}}]}
            ).encode("utf-8")
        )

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    result = processor.process(
        "原始口述",
        INPUT_MODE_DICTATION,
        LLMSettings(
            enabled=True,
            model="example-model",
            api_key_env="TEST_LLM_KEY",
            api_key="llm-ui-key",
        ),
    )

    assert result == "整理后的文本。"
    assert captured[0].headers["Authorization"] == "Bearer llm-ui-key"


@pytest.mark.parametrize(
    ("model_content", "expected"),
    [
        ("dictation", INPUT_MODE_DICTATION),
        ("edit", INPUT_MODE_EDIT),
        ('{"mode":"edit"}', INPUT_MODE_EDIT),
    ],
)
def test_processor_classifies_dictation_and_edit_instructions(
    model_content, expected
):
    requests = []

    def urlopen(http_request, *, timeout):
        requests.append(json.loads(http_request.data.decode("utf-8")))
        payload = {"choices": [{"message": {"content": model_content}}]}
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    mode, model_output = processor.classify_input_mode_with_trace(
        "把上一句改得正式一点",
        LLMSettings(enabled=True, model="router-model", api_key_env=""),
    )

    assert mode == expected
    assert model_output == model_content
    assert "模式路由器" in requests[0]["messages"][0]["content"]
    assert "<用户语音>\n把上一句改得正式一点\n</用户语音>" == (
        requests[0]["messages"][1]["content"]
    )
    assert "不要输出 JSON" in requests[0]["messages"][0]["content"]
    assert requests[0]["max_tokens"] == 32


def test_deepseek_router_disables_reasoning(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    requests = []

    def urlopen(http_request, *, timeout):
        requests.append(json.loads(http_request.data.decode("utf-8")))
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "edit"}],
                }
            ],
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    mode = processor.classify_input_mode(
        "把上一句改短一点",
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="deepseek-v4-flash-260425",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
    )

    assert mode == INPUT_MODE_EDIT
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["max_output_tokens"] == 32


def test_routing_worker_returns_fallback_mode_and_latency_on_failure():
    class FailingRouter:
        def classify_input_mode_with_trace(self, *_args):
            raise RuntimeError("router unavailable")

    completed = threading.Event()
    results = []
    worker = TextProcessingWorker(
        processor=FailingRouter(),
        on_result=lambda _result: None,
        on_routing_result=lambda result: (results.append(result), completed.set()),
    )
    worker.submit_routing(
        InputModeRoutingRequest(
            request_id=91,
            session_id=8,
            raw_text="测试",
            settings=LLMSettings(enabled=True, model="router-model"),
            fallback_mode=INPUT_MODE_EDIT,
        )
    )

    assert completed.wait(1.0)
    worker.close(wait=True)
    assert results[0].mode == INPUT_MODE_EDIT
    assert results[0].error == "router unavailable"
    assert results[0].latency_s >= 0.0


def test_cancelled_slow_request_does_not_block_or_publish_before_next_request():
    slow_started = threading.Event()
    release_slow = threading.Event()
    fast_finished = threading.Event()
    results = []

    class Processor:
        def process(self, text, *_args):
            if text == "slow":
                slow_started.set()
                release_slow.wait(2.0)
            return text

    worker = TextProcessingWorker(
        processor=Processor(),
        on_result=lambda result: (
            results.append(result),
            fast_finished.set() if result.raw_text == "fast" else None,
        ),
    )
    settings = LLMSettings(enabled=False)
    worker.submit(TextProcessingRequest(1, 1, "dictation", "slow", settings))
    assert slow_started.wait(1.0)
    worker.cancel_request(1)
    worker.submit(TextProcessingRequest(2, 2, "dictation", "fast", settings))

    assert fast_finished.wait(1.0)
    assert [item.raw_text for item in results] == ["fast"]
    release_slow.set()
    time.sleep(0.05)
    worker.close(wait=True)
    assert [item.raw_text for item in results] == ["fast"]
    assert worker._cancelled_request_ids == set()


def test_processor_can_use_local_endpoint_without_api_key():
    captured = []

    def urlopen(http_request, *, timeout):
        captured.append(http_request)
        return _Response(
            json.dumps(
                {"choices": [{"message": {"content": [{"text": "本地结果"}]}}]}
            ).encode("utf-8")
        )

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    result = processor.process(
        "测试",
        INPUT_MODE_DICTATION,
        LLMSettings(
            enabled=True,
            base_url="http://127.0.0.1:11434/v1/chat/completions",
            model="local-model",
            api_key_env="",
        ),
    )

    assert result == "本地结果"
    assert "Authorization" not in captured[0].headers


def test_full_text_edit_mode_uses_single_field_schema_for_comparison():
    captured = []

    def urlopen(http_request, *, timeout):
        captured.append(json.loads(http_request.data.decode("utf-8")))
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "submit_text_edit",
                                    "arguments": {
                                        "modified_text": "会议安排在周五。",
                                    },
                                },
                            }
                        ]
                    }
                }
            ]
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    result = processor.process(
        "把周四改成周五",
        INPUT_MODE_EDIT,
        LLMSettings(enabled=True, model="test", api_key_env=""),
        "会议安排在周四。",
        EDIT_MODE_FULL,
    )

    assert result == "会议安排在周五。"
    body = captured[0]
    parameters = body["tools"][0]["function"]["parameters"]
    assert set(parameters["properties"]) == {"modified_text"}
    assert parameters["required"] == ["modified_text"]
    assert "modified_text 必须是修改后的完整文本" in (
        body["messages"][0]["content"]
    )


def test_race_mode_returns_the_first_valid_edit_protocol(monkeypatch):
    barrier = threading.Barrier(2)
    fragment_finished = threading.Event()
    captured = []

    def urlopen(http_request, *, timeout):
        body = json.loads(http_request.data.decode("utf-8"))
        captured.append(body)
        properties = body["tools"][0]["function"]["parameters"]["properties"]
        is_fragment = "original_text" in properties
        barrier.wait(timeout=1.0)
        if is_fragment:
            threading.Event().wait(0.3)
            arguments = {
                "original_text": "周四",
                "modified_text": "周五",
            }
            fragment_finished.set()
        else:
            arguments = {"modified_text": "完整文本先返回。"}
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "submit_text_edit",
                                    "arguments": arguments,
                                },
                            }
                        ]
                    }
                }
            ]
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, model_output = processor.process_with_trace(
        "把周四改成周五",
        INPUT_MODE_EDIT,
        LLMSettings(enabled=True, model="test", api_key_env=""),
        "会议安排在周四。",
        EDIT_MODE_RACE,
    )

    assert final_text == "完整文本先返回。"
    assert json.loads(model_output) == {"modified_text": "完整文本先返回。"}
    assert fragment_finished.is_set() is False
    assert len(captured) == 2


def test_race_rejects_prompt_echo_and_uses_other_protocol_without_retry():
    barrier = threading.Barrier(2)
    calls = []

    def urlopen(http_request, *, timeout):
        body = json.loads(http_request.data.decode("utf-8"))
        properties = body["tools"][0]["function"]["parameters"]["properties"]
        is_fragment = "original_text" in properties
        calls.append("fragment" if is_fragment else "full")
        barrier.wait(timeout=1.0)
        if is_fragment:
            threading.Event().wait(0.05)
            arguments = {
                "original_text": "辅助",
                "modified_text": "输出",
            }
        else:
            arguments = {
                "modified_text": (
                    "你是文本编辑规划器，不是聊天助手。根据待修改文本和"
                    "用户要求，返回 original_text 和 modified_text。"
                )
            }
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "submit_text_edit",
                                    "arguments": arguments,
                                },
                            }
                        ]
                    }
                }
            ]
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text = processor.process(
        "把辅助改成输出",
        INPUT_MODE_EDIT,
        LLMSettings(enabled=True, model="test", api_key_env=""),
        "保留现有的辅助模式。",
        EDIT_MODE_RACE,
    )

    assert final_text == "保留现有的输出模式。"
    assert sorted(calls) == ["fragment", "full"]


def test_edit_output_budget_scales_for_complete_modified_text():
    captured = []
    target = "原" * 2000

    def urlopen(http_request, *, timeout):
        captured.append(json.loads(http_request.data.decode("utf-8")))
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "submit_text_edit",
                                    "arguments": {
                                        "original_text": target,
                                        "modified_text": target,
                                    },
                                },
                            }
                        ]
                    }
                }
            ]
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    result = processor.process(
        "保持不变",
        INPUT_MODE_EDIT,
        LLMSettings(enabled=True, model="test", api_key_env=""),
        target,
    )

    assert result == target
    assert captured[0]["max_tokens"] == 4512


def test_volcengine_request_disables_thinking(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    captured = []

    def urlopen(http_request, *, timeout):
        captured.append(http_request)
        return _Response(
            json.dumps(
                {
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "整理结果"}
                            ],
                        }
                    ],
                }
            ).encode("utf-8")
        )

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    result = processor.process(
        "原始文本",
        INPUT_MODE_DICTATION,
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seed-2-0-lite-260215",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
    )

    assert result == "整理结果"
    body = json.loads(captured[0].data.decode("utf-8"))
    assert captured[0].full_url == "https://ark.cn-beijing.volces.com/api/v3/responses"
    assert body["stream"] is False
    assert body["input"][0]["role"] == "system"
    assert body["input"][1]["content"][0]["text"] == "原始文本"
    assert body["thinking"] == {"type": "disabled"}
    assert "tools" not in body
    assert captured[0].headers["Authorization"] == "Bearer test-ark-key"


def test_volcengine_edit_uses_function_call_json(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    captured = []

    def urlopen(http_request, *, timeout):
        captured.append(http_request)
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit",
                    "arguments": (
                        '{"original_text":"我想喝。",'
                        '"modified_text":"我想喝一杯咖啡。"}'
                    ),
                }
            ],
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, model_output = processor.process_with_trace(
        "在喝后面加一杯咖啡",
        INPUT_MODE_EDIT,
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seed-2-0-lite-260215",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
        "我想喝。",
    )

    assert final_text == "我想喝一杯咖啡。"
    assert json.loads(model_output)["original_text"] == "我想喝。"
    body = json.loads(captured[0].data.decode("utf-8"))
    assert captured[0].full_url == "https://ark.cn-beijing.volces.com/api/v3/responses"
    assert body["tools"][0]["name"] == "submit_text_edit"
    assert "tool_choice" not in body
    system_prompt = body["input"][0]["content"][0]["text"]
    assert "必须真正调用 submit_text_edit 工具" in system_prompt
    assert "modified_text：用于替换 original_text 的新片段" in system_prompt
    parameters = body["tools"][0]["parameters"]
    assert set(parameters["properties"]) == {"original_text", "modified_text"}
    assert parameters["required"] == ["original_text", "modified_text"]
    assert parameters["additionalProperties"] is False


def test_edit_retries_malformed_ark_arguments_once(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    captured = []
    payloads = [
        {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit",
                    "arguments": (
                        '{"original_text":一天",'
                        '"modified_text":"两天"}'
                    ),
                }
            ],
        },
        {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit",
                    "arguments": (
                        '{"original_text":"一天",'
                        '"modified_text":"两天"}'
                    ),
                }
            ],
        },
    ]

    def urlopen(http_request, *, timeout):
        captured.append(json.loads(http_request.data.decode("utf-8")))
        return _Response(json.dumps(payloads.pop(0)).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, model_output = processor.process_with_trace(
        "把一天改为两天",
        INPUT_MODE_EDIT,
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seed-2-0-lite-260215",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
        "我想申请一天调休。",
    )

    assert final_text == "我想申请两天调休。"
    assert json.loads(model_output)["original_text"] == "一天"
    assert len(captured) == 2
    first_prompt = captured[0]["input"][0]["content"][0]["text"]
    retry_prompt = captured[1]["input"][0]["content"][0]["text"]
    assert "上一次编辑尝试失败" not in first_prompt
    assert "上一次编辑尝试失败" in retry_prompt
    assert '"original_text":一天"' in retry_prompt
    assert "第 1 行第" in retry_prompt
    assert "strict" not in captured[0]["tools"][0]
    assert "strict" not in captured[1]["tools"][0]


def test_process_with_attempts_preserves_every_retry_output(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    outputs = [
        '{"original_text":"一天","modified_text":两天}',
        '{"original_text":"一天","modified_text":"两天"}',
    ]

    def urlopen(_http_request, *, timeout):
        arguments = outputs.pop(0)
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit",
                    "arguments": arguments,
                }
            ],
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, model_outputs = processor.process_with_attempts(
        "改成两天",
        INPUT_MODE_EDIT,
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seed-2-0-lite-260215",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
        "一天",
    )

    assert final_text == "两天"
    assert model_outputs == (
        '{"original_text":"一天","modified_text":两天}',
        '{"original_text":"一天","modified_text":"两天"}',
    )


def test_edit_stops_after_one_failed_format_retry(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    call_count = 0
    invalid_arguments = '{"original_text":"一天","modified_text":两天}'

    def urlopen(_http_request, *, timeout):
        nonlocal call_count
        call_count += 1
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit",
                    "arguments": invalid_arguments,
                }
            ],
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    with pytest.raises(RuntimeError, match="有效 JSON") as exc_info:
        processor.process(
            "把一天改为两天",
            INPUT_MODE_EDIT,
            LLMSettings(
                enabled=True,
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="doubao-seed-2-0-lite-260215",
                api_key_env="ARK_API_KEY",
                provider=LLM_PROVIDER_VOLCENGINE,
            ),
            "我想申请一天调休。",
        )

    assert call_count == 2
    assert exc_info.value.model_output == invalid_arguments
    assert exc_info.value.model_outputs == (
        invalid_arguments,
        invalid_arguments,
    )


def test_repeated_fragment_is_applied_to_all_matches_without_retry(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    call_count = 0

    def urlopen(_http_request, *, timeout):
        nonlocal call_count
        call_count += 1
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit",
                    "arguments": (
                        '{"original_text":"项目",'
                        '"modified_text":"任务"}'
                    ),
                }
            ],
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, model_outputs = processor.process_with_attempts(
        "把项目改成任务",
        INPUT_MODE_EDIT,
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seed-2-0-lite-260215",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
        "项目一和项目二",
    )

    assert call_count == 1
    assert final_text == "任务一和任务二"
    assert model_outputs == (
        '{"original_text":"项目","modified_text":"任务"}',
    )


def test_edit_request_error_is_retried_and_recorded(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    call_count = 0

    def urlopen(_http_request, *, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("timed out")
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit",
                    "arguments": (
                        '{"original_text":"原文。",'
                        '"modified_text":"原文。"}'
                    ),
                }
            ],
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, model_outputs = processor.process_with_attempts(
        "保持不变",
        INPUT_MODE_EDIT,
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seed-2-0-lite-260215",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
        "原文。",
    )

    assert call_count == 2
    assert final_text == "原文。"
    assert "未返回 function/tool arguments" in model_outputs[0]
    assert "大模型请求超时" in model_outputs[0]
    assert model_outputs[1] == (
        '{"original_text":"原文。","modified_text":"原文。"}'
    )


def test_deepseek_v4_flash_reuses_ark_edit_pipeline(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    captured = []

    def urlopen(http_request, *, timeout):
        captured.append(http_request)
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit",
                    "arguments": (
                        '{"original_text":"原文。",'
                        '"modified_text":"原文。"}'
                    ),
                }
            ],
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    result = processor.process(
        "保持不变",
        INPUT_MODE_EDIT,
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="deepseek-v4-flash-260425",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
        "原文。",
    )

    assert result == "原文。"
    body = json.loads(captured[0].data.decode("utf-8"))
    assert captured[0].full_url.endswith("/api/v3/responses")
    assert body["model"] == "deepseek-v4-flash-260425"
    assert body["tools"][0]["name"] == "submit_text_edit"
    assert "thinking" not in body
    assert "original_text：从待修改文本逐字复制" in (
        body["input"][0]["content"][0]["text"]
    )


def test_processor_accepts_ark_responses_function_call_shape():
    payload = {
        "object": "response",
        "output": [
            {
                "type": "function_call",
                "name": "submit_text_edit",
                "arguments": (
                    '{"original_text":"原文。",'
                    '"modified_text":"原文。"}'
                ),
            }
        ],
    }

    assert OpenAICompatibleTextProcessor._extract_required_edit_tool_arguments(
        payload
    ) == (
        '{"original_text":"原文。","modified_text":"原文。"}'
    )


def test_processor_keeps_function_arguments_dict_for_executor():
    arguments = {"original_text": "原文。", "modified_text": "原文。"}
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "submit_text_edit",
                                "arguments": arguments,
                            },
                        }
                    ]
                }
            }
        ]
    }

    assert (
        OpenAICompatibleTextProcessor._extract_required_edit_tool_arguments(
            payload
        )
        is arguments
    )


def test_local_edit_requires_a_real_tool_call():
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"original_text":"原文。",'
                        '"modified_text":"原文。"}'
                    ),
                },
                "finish_reason": "stop",
            }
        ]
    }

    def urlopen(_http_request, *, timeout):
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    settings = LLMSettings(
        enabled=True,
        provider=LLM_PROVIDER_LOCAL,
        base_url="http://127.0.0.1:11435/v1",
        model="qwen3-4b-instruct-2507-local",
        api_key_env="",
    )

    with pytest.raises(
        RuntimeError,
        match="没有调用 submit_text_edit 工具",
    ) as exc_info:
        processor.process("保持不变", INPUT_MODE_EDIT, settings, "原文。")

    assert '\\"original_text\\":\\"原文。\\"' in (
        exc_info.value.model_output
    )


def test_empty_ark_edit_response_preserves_debug_payload(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")

    def urlopen(_http_request, *, timeout):
        payload = {"object": "response", "status": "completed", "output": []}
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    with pytest.raises(
        RuntimeError, match="没有调用 submit_text_edit 工具"
    ) as exc:
        processor.process(
            "删除上一句话",
            INPUT_MODE_EDIT,
            LLMSettings(
                enabled=True,
                base_url="https://ark.cn-beijing.volces.com/api/v3",
                model="doubao-seed-2-0-lite-260215",
                api_key_env="ARK_API_KEY",
                provider=LLM_PROVIDER_VOLCENGINE,
            ),
            "第一句。第二句。",
        )

    assert '"status": "completed"' in exc.value.model_output


def test_oversized_edit_capture_is_rejected_before_llm_request():
    calls = []
    processor = OpenAICompatibleTextProcessor(
        urlopen=lambda *_args, **_kwargs: calls.append(True)
    )
    settings = LLMSettings(
        enabled=True,
        base_url="http://127.0.0.1:11435/v1",
        model="local-model",
        api_key_env="",
    )
    oversized = "页面内容" * (MAX_EDIT_TARGET_CHARS // 4 + 1)

    with pytest.raises(ValueError, match="超过单次修改上限"):
        processor.process("润色一下", INPUT_MODE_EDIT, settings, oversized)

    assert calls == []
    assert validate_edit_target_text("正常文本") == "正常文本"


def test_edit_prompt_grounds_asr_misrecognition_to_exact_original_target():
    captured = []

    def urlopen(http_request, *, timeout):
        captured.append(json.loads(http_request.data.decode("utf-8")))
        return _Response(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "submit_text_edit",
                                            "arguments": (
                                                '{"original_text":"周四",'
                                                '"modified_text":'
                                                '"周五"}'
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    settings = LLMSettings(
        enabled=True,
        base_url="http://127.0.0.1:11435/v1",
        model="local-model",
        api_key_env="",
    )

    result = processor.process(
        "把周丝替换成周五",
        INPUT_MODE_EDIT,
        settings,
        "会议安排在周四。",
    )

    assert result == "会议安排在周五。"
    system_prompt = captured[0]["messages"][0]["content"]
    assert "明显 ASR 错词" in system_prompt
    assert "original_text：从待修改文本逐字复制" in system_prompt
    assert "星巴克" not in system_prompt
    assert "把周丝替换成周五" in captured[0]["messages"][1]["content"]


def test_invalid_edit_response_keeps_raw_model_output_on_the_error():
    raw_output = (
        '{"original_text":"新巴克",'
        '"modified_text":"我准备去瑞幸开会。"}'
    )

    def urlopen(_http_request, *, timeout):
        return _Response(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "submit_text_edit",
                                            "arguments": raw_output,
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    settings = LLMSettings(
        enabled=True,
        base_url="http://127.0.0.1:11435/v1",
        model="local-model",
        api_key_env="",
    )

    with pytest.raises(RuntimeError, match="不在待修改文本中") as exc_info:
        processor.process(
            "把新巴克替换成瑞幸",
            INPUT_MODE_EDIT,
            settings,
            "我准备去星巴克开会。",
        )

    assert exc_info.value.model_output == raw_output


def test_processor_auto_starts_configured_local_server():
    created = []
    captured = []

    class FakeLocalServer:
        def __init__(self, **kwargs):
            created.append((kwargs, self))
            self.ensure_count = 0

        def ensure_running(self):
            self.ensure_count += 1
            return True

        def stop_started_process(self):
            raise AssertionError("successful startup must not be stopped")

    def urlopen(http_request, *, timeout):
        assert timeout == 30.0
        captured.append(json.loads(http_request.data.decode("utf-8")))
        return _Response(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {
                                            "name": "submit_text_edit",
                                            "arguments": {
                                                "original_text": "原始草稿",
                                                "modified_text": "本地结果",
                                            },
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ).encode("utf-8")
        )

    processor = OpenAICompatibleTextProcessor(
        urlopen=urlopen,
        local_server_factory=FakeLocalServer,
    )
    settings = LLMSettings(
        enabled=True,
        provider=LLM_PROVIDER_LOCAL,
        base_url="http://127.0.0.1:11435/v1",
        model="qwen3-4b-instruct-2507-local",
        api_key_env="",
        local_server_path=r"E:\\Ollama\\llama-server.exe",
        local_model_path=r"E:\\Models\\qwen.gguf",
        local_auto_start=True,
    )

    assert (
        processor.process("改正式一点", INPUT_MODE_EDIT, settings, "原始草稿")
        == "本地结果"
    )
    assert created[0][0]["model_alias"] == "qwen3-4b-instruct-2507-local"
    assert created[0][0]["reasoning"] == "off"
    assert created[0][1].ensure_count == 1
    assert captured[0]["tools"][0]["function"]["name"] == "submit_text_edit"
    assert captured[0]["tool_choice"] == "required"


def test_processor_requires_configured_api_key(monkeypatch):
    monkeypatch.delenv("MISSING_LLM_KEY", raising=False)
    processor = OpenAICompatibleTextProcessor(urlopen=lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="MISSING_LLM_KEY"):
        processor.process(
            "测试",
            INPUT_MODE_DICTATION,
            LLMSettings(
                enabled=True,
                model="example-model",
                api_key_env="MISSING_LLM_KEY",
            ),
        )


def test_local_warmup_loads_once_and_seeds_all_prompt_prefixes():
    requests = []
    servers = []

    class FakeLocalServer:
        def __init__(self, **_kwargs):
            self.ensure_count = 0
            servers.append(self)

        def ensure_running(self):
            self.ensure_count += 1
            return True

        def stop_started_process(self):
            return None

    def urlopen(http_request, *, timeout):
        requests.append(json.loads(http_request.data.decode("utf-8")))
        return _Response(
            json.dumps({"choices": [{"message": {"content": "x"}}]}).encode(
                "utf-8"
            )
        )

    processor = OpenAICompatibleTextProcessor(
        urlopen=urlopen,
        local_server_factory=FakeLocalServer,
    )
    settings = LLMSettings(
        enabled=True,
        provider=LLM_PROVIDER_LOCAL,
        base_url="http://127.0.0.1:11435/v1",
        model="local",
        api_key_env="",
        local_server_path="llama-server.exe",
        local_model_path="model.gguf",
        local_auto_start=True,
    )

    processor.warmup(settings)

    assert servers[0].ensure_count == 1
    assert len(requests) == 3
    assert all(item["max_tokens"] == 1 for item in requests)
    assert requests[0]["messages"][0]["content"] != requests[1]["messages"][0]["content"]
    assert requests[1]["messages"][0]["content"] != requests[2]["messages"][0]["content"]


def test_worker_falls_back_to_raw_text_when_llm_fails():
    class FailingProcessor:
        def process(self, *_args):
            raise RuntimeError("service unavailable")

    completed = threading.Event()
    results = []
    worker = TextProcessingWorker(
        processor=FailingProcessor(),
        on_result=lambda result: (results.append(result), completed.set()),
    )
    worker.submit(
        TextProcessingRequest(
            request_id=7,
            session_id=3,
            mode=INPUT_MODE_EDIT,
            raw_text="改得正式一点",
            settings=LLMSettings(enabled=True, model="example-model"),
            target_text="原始草稿",
        )
    )

    assert completed.wait(1.0)
    worker.close(wait=True)
    assert results[0].final_text == "原始草稿"
    assert results[0].target_text == "原始草稿"
    assert results[0].used_llm is True
    assert results[0].error == "service unavailable"


def test_worker_preserves_raw_model_output_for_debugging():
    calls = []

    class TracedProcessor:
        def process_with_trace(self, *args):
            calls.append(args)
            return (
                "我准备去瑞幸开会。",
                '{"original_text":"星巴克",'
                '"modified_text":"我准备去瑞幸开会。"}',
            )

    completed = threading.Event()
    results = []
    worker = TextProcessingWorker(
        processor=TracedProcessor(),
        on_result=lambda result: (results.append(result), completed.set()),
    )
    worker.submit(
        TextProcessingRequest(
            request_id=8,
            session_id=4,
            mode=INPUT_MODE_EDIT,
            raw_text="把新巴克替换成瑞幸",
            settings=LLMSettings(enabled=True, model="example-model"),
            target_text="我准备去星巴克开会。",
        )
    )

    assert completed.wait(1.0)
    worker.close(wait=True)
    assert results[0].final_text == "我准备去瑞幸开会。"
    assert '"original_text":"星巴克"' in results[0].model_output
    assert calls[0][-1] == EDIT_MODE_RACE


def test_worker_keeps_background_trace_bound_to_its_original_request():
    callbacks = []

    class CollectingProcessor:
        def process_with_collection_trace(
            self, *_args, on_collection_complete
        ):
            callbacks.append(on_collection_complete)
            return "候选文本。", "{}", (), "fragment"

    completed = threading.Event()
    results = []
    traces = []

    def on_result(result):
        results.append(result)
        if len(results) == 2:
            completed.set()

    worker = TextProcessingWorker(
        processor=CollectingProcessor(),
        on_result=on_result,
        on_trace=traces.append,
    )
    for request_id in (71, 72):
        worker.submit(
            TextProcessingRequest(
                request_id=request_id,
                session_id=request_id,
                mode=INPUT_MODE_EDIT,
                raw_text="改一下",
                settings=LLMSettings(enabled=True, model="example-model"),
                target_text="原文。",
            )
        )

    assert completed.wait(1.0)
    callbacks[0]((), "fragment")
    callbacks[1]((), "fragment")
    worker.close(wait=True)

    assert [trace.request_id for trace in traces] == [71, 72]
