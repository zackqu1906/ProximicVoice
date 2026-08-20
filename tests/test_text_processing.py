import io
import json
import threading

import pytest

from proximic_ring.text_processing import (
    INPUT_MODE_DICTATION,
    INPUT_MODE_EDIT,
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
        is_edit = "submit_text_edit_plan" in request_body["messages"][0]["content"]
        content = (
            '{"kind":"rewrite","text":"这是正式的原草稿。"}'
            if is_edit
            else "整理后的文本。"
        )
        payload = {"choices": [{"message": {"content": content}}]}
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
    assert '"text":"完整文本"' in edit_body["messages"][0]["content"]
    assert "<待修改文本>\n这是原来的草稿" in edit_body["messages"][1]["content"]
    assert "<修改要求>\n改得正式一点" in edit_body["messages"][1]["content"]
    assert edit_body["tools"][0]["function"]["name"] == "submit_text_edit_plan"
    assert "必须调用 submit_text_edit_plan 工具" in edit_body["messages"][0]["content"]
    assert "不要主动翻译" in body["messages"][0]["content"]


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
                    "name": "submit_text_edit_plan",
                    "arguments": (
                        '{"kind":"operations","operations":['
                        '{"op":"insert","position":"after",'
                        '"target":"喝","value":"一杯咖啡",'
                        '"occurrence":"unique"}]}'
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
    assert json.loads(model_output)["kind"] == "operations"
    body = json.loads(captured[0].data.decode("utf-8"))
    assert captured[0].full_url == "https://ark.cn-beijing.volces.com/api/v3/responses"
    assert body["tools"][0]["name"] == "submit_text_edit_plan"
    assert "tool_choice" not in body
    system_prompt = body["input"][0]["content"][0]["text"]
    assert "必须调用 submit_text_edit_plan 工具" in system_prompt
    assert "一次连续新增内容必须放在一个 value 中" in system_prompt
    assert "在 X 后面加 Y" in system_prompt
    operation_schema = body["tools"][0]["parameters"]["properties"][
        "operations"
    ]["items"]
    assert "不得拆成多个 insert" in body["tools"][0]["parameters"][
        "properties"
    ]["operations"]["description"]
    assert "整篇文本开头/末尾" in operation_schema["properties"]["position"][
        "description"
    ]


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
                    "name": "submit_text_edit_plan",
                    "arguments": (
                        '{"kind":"operations","operations":['
                        '{"op":"replace","target":一天",'
                        '"value":"两天","occurrence":"unique"}]}'
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
                    "name": "submit_text_edit_plan",
                    "arguments": (
                        '{"kind":"operations","operations":['
                        '{"op":"replace","target":"一天",'
                        '"value":"两天","occurrence":"unique"}]}'
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
    assert json.loads(model_output)["operations"][0]["target"] == "一天"
    assert len(captured) == 2
    first_prompt = captured[0]["input"][0]["content"][0]["text"]
    retry_prompt = captured[1]["input"][0]["content"][0]["text"]
    assert "上一次编辑尝试失败" not in first_prompt
    assert "上一次编辑尝试失败" in retry_prompt
    assert '"target":一天"' in retry_prompt
    assert "第 1 行第" in retry_prompt
    assert "strict" not in captured[0]["tools"][0]
    assert "strict" not in captured[1]["tools"][0]


def test_process_with_attempts_preserves_every_retry_output(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    outputs = [
        '{"kind":"rewrite","text":两天"}',
        '{"kind":"rewrite","text":"两天"}',
    ]

    def urlopen(_http_request, *, timeout):
        arguments = outputs.pop(0)
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit_plan",
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
        '{"kind":"rewrite","text":两天"}',
        '{"kind":"rewrite","text":"两天"}',
    )


def test_edit_stops_after_one_failed_format_retry(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    call_count = 0
    invalid_arguments = '{"kind":"rewrite","text":两天"}'

    def urlopen(_http_request, *, timeout):
        nonlocal call_count
        call_count += 1
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit_plan",
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


def test_edit_execution_ambiguity_is_retried_once(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    call_count = 0
    captured = []

    def urlopen(http_request, *, timeout):
        nonlocal call_count
        call_count += 1
        captured.append(json.loads(http_request.data.decode("utf-8")))
        occurrence = "unique" if call_count == 1 else "last"
        payload = {
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "submit_text_edit_plan",
                    "arguments": (
                        '{"kind":"operations","operations":['
                        '{"op":"replace","target":"咖啡",'
                        f'"value":"牛奶","occurrence":"{occurrence}"}}]}}'
                    ),
                }
            ],
        }
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    final_text, model_outputs = processor.process_with_attempts(
        "把咖啡改成牛奶",
        INPUT_MODE_EDIT,
        LLMSettings(
            enabled=True,
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            model="doubao-seed-2-0-lite-260215",
            api_key_env="ARK_API_KEY",
            provider=LLM_PROVIDER_VOLCENGINE,
        ),
        "我要去咖啡厅喝咖啡。",
    )

    assert call_count == 2
    assert final_text == "我要去咖啡厅喝牛奶。"
    assert len(model_outputs) == 2
    retry_prompt = captured[1]["input"][0]["content"][0]["text"]
    assert "目标出现 2 次" in retry_prompt
    assert '"occurrence":"unique"' in retry_prompt


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
                    "name": "submit_text_edit_plan",
                    "arguments": '{"kind":"noop"}',
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
    assert model_outputs[1] == '{"kind":"noop"}'


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
                    "name": "submit_text_edit_plan",
                    "arguments": '{"kind":"noop"}',
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
    assert body["tools"][0]["name"] == "submit_text_edit_plan"
    assert "thinking" not in body
    assert "只能返回以下三种之一" in body["input"][0]["content"][0]["text"]


def test_processor_accepts_ark_responses_function_call_shape():
    payload = {
        "object": "response",
        "output": [
            {
                "type": "function_call",
                "name": "submit_text_edit_plan",
                "arguments": '{"kind":"noop"}',
            }
        ],
    }

    assert OpenAICompatibleTextProcessor._extract_content(payload) == (
        '{"kind":"noop"}'
    )


def test_processor_keeps_function_arguments_dict_for_executor():
    arguments = {"kind": "noop"}
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "submit_text_edit_plan",
                                "arguments": arguments,
                            },
                        }
                    ]
                }
            }
        ]
    }

    assert OpenAICompatibleTextProcessor._extract_content(payload) is arguments


def test_local_edit_requires_a_real_tool_call():
    payload = {
        "choices": [
            {
                "message": {
                    "content": '{"kind":"noop"}',
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
        match="没有调用 submit_text_edit_plan 工具",
    ) as exc_info:
        processor.process("保持不变", INPUT_MODE_EDIT, settings, "原文。")

    assert '"content": "{\\"kind\\":\\"noop\\"}"' in (
        exc_info.value.model_output
    )


def test_empty_ark_edit_response_preserves_debug_payload(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")

    def urlopen(_http_request, *, timeout):
        payload = {"object": "response", "status": "completed", "output": []}
        return _Response(json.dumps(payload).encode("utf-8"))

    processor = OpenAICompatibleTextProcessor(urlopen=urlopen)
    with pytest.raises(
        RuntimeError, match="没有有效工具参数.*finish_reason=completed"
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
                                "content": (
                                    '{"kind":"operations","operations":['
                                    '{"op":"replace","target":"周四",'
                                    '"value":"周五","occurrence":"unique"}]}'
                                )
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
    assert "ASR错误纠正" in system_prompt
    assert "target 必须来自<待修改文本>，必须逐字复制" in system_prompt
    assert "X 是目标内容" in system_prompt
    assert "唯一、明显的近音/错字对应词" in system_prompt
    assert "星巴克" not in system_prompt
    assert 'target="周四"' in system_prompt
    assert "把周丝替换成周五" in captured[0]["messages"][1]["content"]


def test_invalid_edit_plan_keeps_raw_model_output_on_the_error():
    raw_output = (
        '{"kind":"operations","operations":['
        '{"op":"replace","target":"新巴克","value":"瑞幸",'
        '"occurrence":"unique"}]}'
    )

    def urlopen(_http_request, *, timeout):
        return _Response(
            json.dumps(
                {"choices": [{"message": {"content": raw_output}}]},
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

    with pytest.raises(RuntimeError, match="原文中找不到目标") as exc_info:
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
                                            "name": "submit_text_edit_plan",
                                            "arguments": {
                                                "kind": "rewrite",
                                                "text": "本地结果",
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
    assert captured[0]["tools"][0]["function"]["name"] == (
        "submit_text_edit_plan"
    )
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


def test_local_warmup_loads_once_and_seeds_both_prompt_prefixes():
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
    assert len(requests) == 2
    assert all(item["max_tokens"] == 1 for item in requests)
    assert requests[0]["messages"][0]["content"] != requests[1]["messages"][0]["content"]


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
    class TracedProcessor:
        def process_with_trace(self, *_args):
            return (
                "我准备去瑞幸开会。",
                '{"kind":"operations","operations":['
                '{"op":"replace","target":"星巴克","value":"瑞幸",'
                '"occurrence":"unique"}]}',
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
    assert '"target":"星巴克"' in results[0].model_output
