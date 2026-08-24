from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import json
import os
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request

from .edit_response import apply_edit_response
from .edit_tool import (
    DEFAULT_EDIT_MODE,
    EDIT_MODE_FRAGMENT,
    EDIT_MODE_FULL,
    EDIT_MODE_RACE,
    edit_tool_for_mode,
    normalize_edit_mode,
)
from .local_server import LocalModelServer
from .model import (
    INPUT_MODE_EDIT,
    LLM_PROVIDER_LOCAL,
    LLM_PROVIDER_VOLCENGINE,
    LLMSettings,
    normalize_llm_provider,
    normalize_input_mode,
    validate_edit_target_text,
)
from .prompts import (
    DICTATION_PROMPT,
    EDIT_FRAGMENT_PROMPT,
    EDIT_FRAGMENT_RETRY_PROMPT,
    EDIT_FULL_TEXT_PROMPT,
    EDIT_FULL_TEXT_RETRY_PROMPT,
    EDIT_PROMPT,
    EDIT_TOOL_REQUIRED_PROMPT,
)


class LLMResponseProcessingError(RuntimeError):
    """An LLM response was received but could not be safely executed."""

    def __init__(
        self,
        message: str,
        model_output: str,
        model_outputs: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.model_output = str(model_output or "")
        self.model_outputs = tuple(model_outputs or ()) or (
            (self.model_output,) if self.model_output else ()
        )


class OpenAICompatibleTextProcessor:
    """Call the widely-supported ``/chat/completions`` HTTP contract.

    The implementation deliberately uses the standard library so enabling text
    processing does not add another runtime dependency to the Ring application.
    """

    def __init__(
        self,
        *,
        urlopen: Callable[..., object] | None = None,
        local_server_factory: Callable[..., LocalModelServer] | None = None,
    ) -> None:
        self._urlopen = urlopen or urllib_request.urlopen
        self._local_server_factory = local_server_factory or LocalModelServer
        self._local_servers: dict[
            tuple[str, str, str, str, int, str], LocalModelServer
        ] = {}

    def process(
        self,
        text: str,
        mode: str,
        settings: LLMSettings,
        target_text: str = "",
        edit_mode: str = DEFAULT_EDIT_MODE,
    ) -> str:
        final_text, _model_output = self.process_with_trace(
            text,
            mode,
            settings,
            target_text,
            edit_mode,
        )
        return final_text

    def process_with_trace(
        self,
        text: str,
        mode: str,
        settings: LLMSettings,
        target_text: str = "",
        edit_mode: str = DEFAULT_EDIT_MODE,
    ) -> tuple[str, str]:
        """Return both the applied text and the model's unmodified response."""

        final_text, model_outputs = self.process_with_attempts(
            text,
            mode,
            settings,
            target_text,
            edit_mode,
        )
        return final_text, model_outputs[-1] if model_outputs else ""

    def process_with_attempts(
        self,
        text: str,
        mode: str,
        settings: LLMSettings,
        target_text: str = "",
        edit_mode: str = DEFAULT_EDIT_MODE,
    ) -> tuple[str, tuple[str, ...]]:
        """Return the result and every model output, including retries."""

        settings.validate()
        raw_text = str(text or "").strip()
        if not raw_text:
            return "", ()
        if not settings.enabled:
            return raw_text, ()

        normalized_mode = normalize_input_mode(mode)
        if (
            normalize_llm_provider(settings.provider) == LLM_PROVIDER_LOCAL
            and settings.local_auto_start
            and normalized_mode != INPUT_MODE_EDIT
        ):
            self._ensure_local_server(settings)

        if normalized_mode == INPUT_MODE_EDIT:
            normalized_edit_mode = normalize_edit_mode(edit_mode)
            target = validate_edit_target_text(target_text).strip()
            user_content = (
                "<待修改文本>\n"
                f"{target}\n"
                "</待修改文本>\n\n"
                "<修改要求>\n"
                f"{raw_text}\n"
                "</修改要求>"
            )
            # Whole-document rewrites remain valid in both edit strategies,
            # so the output budget must still scale with the captured source.
            max_tokens = max(1024, min(8192, len(target) * 2 + 512))
            if normalized_edit_mode == EDIT_MODE_RACE:
                return self._process_edit_race(
                    settings,
                    target=target,
                    user_content=user_content,
                    max_tokens=max_tokens,
                )
            system_prompt = (
                EDIT_FRAGMENT_PROMPT
                if normalized_edit_mode == EDIT_MODE_FRAGMENT
                else EDIT_FULL_TEXT_PROMPT
            )
        else:
            system_prompt = DICTATION_PROMPT
            user_content = raw_text
            max_tokens = 1024
        if normalized_mode == INPUT_MODE_EDIT:
            return self._process_edit_with_retry(
                settings,
                target=target,
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=max_tokens,
                edit_mode=normalized_edit_mode,
            )
        response = self._request_chat(
            settings,
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.0,
            max_tokens=max_tokens,
            edit_tool=None,
        )
        if not isinstance(response, str):
            model_output = self._model_output_text(response)
            raise LLMResponseProcessingError(
                "大模型听写响应不是文本",
                model_output,
            )
        return response, (response,)

    def _process_edit_with_retry(
        self,
        settings: LLMSettings,
        *,
        target: str,
        system_prompt: str,
        user_content: str,
        max_tokens: int,
        edit_mode: str,
        max_attempts: int = 2,
    ) -> tuple[str, tuple[str, ...]]:
        """Retry any failed edit attempt once without guessing a repair."""

        max_attempts = max(1, int(max_attempts))
        model_outputs: list[str] = []
        retry_error = ""
        retry_output = ""
        for attempt in range(max_attempts):
            retrying = attempt > 0
            final_attempt = attempt == max_attempts - 1
            try:
                if (
                    normalize_llm_provider(settings.provider)
                    == LLM_PROVIDER_LOCAL
                    and settings.local_auto_start
                ):
                    self._ensure_local_server(settings)
                response = self._request_chat(
                    settings,
                    system_prompt=(
                        system_prompt
                        + self._edit_retry_prompt_for_mode(
                            edit_mode,
                            retry_error,
                            retry_output,
                        )
                        if retrying
                        else system_prompt
                    ),
                    user_content=user_content,
                    temperature=0.0,
                    max_tokens=max_tokens,
                    edit_tool=edit_tool_for_mode(edit_mode),
                )
            except Exception as exc:
                current_outputs = tuple(getattr(exc, "model_outputs", ()) or ())
                if not current_outputs:
                    current_output = str(
                        getattr(exc, "model_output", "") or ""
                    ).strip()
                    if current_output:
                        current_outputs = (current_output,)
                if current_outputs:
                    model_outputs.extend(current_outputs)
                    retry_output = current_outputs[-1]
                else:
                    retry_output = (
                        "（本次请求未返回 function/tool arguments；"
                        f"错误：{exc}）"
                    )
                    model_outputs.append(retry_output)
                retry_error = str(exc)
                if not final_attempt:
                    continue
                raise LLMResponseProcessingError(
                    str(exc),
                    retry_output,
                    tuple(model_outputs),
                ) from exc
            model_output = self._model_output_text(response)
            model_outputs.append(model_output)
            try:
                final_text = apply_edit_response(target, response, edit_mode)
            except Exception as exc:
                if not final_attempt:
                    retry_error = str(exc)
                    retry_output = model_output
                    continue
                raise LLMResponseProcessingError(
                    str(exc),
                    model_output,
                    tuple(model_outputs),
                ) from exc
            return final_text, tuple(model_outputs)

        raise AssertionError("编辑结果重试流程异常结束")

    def _process_edit_race(
        self,
        settings: LLMSettings,
        *,
        target: str,
        user_content: str,
        max_tokens: int,
    ) -> tuple[str, tuple[str, ...]]:
        """Race both edit contracts and return the first valid response."""

        race_settings = settings
        if (
            normalize_llm_provider(settings.provider) == LLM_PROVIDER_LOCAL
            and settings.local_auto_start
        ):
            # Start the shared server once before worker threads enter it.
            self._ensure_local_server(settings)
            race_settings = replace(settings, local_auto_start=False)

        jobs = (
            (EDIT_MODE_FRAGMENT, EDIT_FRAGMENT_PROMPT),
            (EDIT_MODE_FULL, EDIT_FULL_TEXT_PROMPT),
        )
        executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="ProxiMicEditRace",
        )
        futures = {
            executor.submit(
                self._process_edit_with_retry,
                race_settings,
                target=target,
                system_prompt=prompt,
                user_content=user_content,
                max_tokens=max_tokens,
                edit_mode=mode,
                # The parallel protocols already provide two independent
                # attempts. Retrying both would double the user's wait time.
                max_attempts=1,
            ): mode
            for mode, prompt in jobs
        }
        errors: dict[str, BaseException] = {}
        failed_outputs: list[str] = []
        for future in as_completed(futures):
            edit_mode = futures[future]
            try:
                result = future.result()
            except BaseException as exc:
                errors[edit_mode] = exc
                outputs = tuple(getattr(exc, "model_outputs", ()) or ())
                if outputs:
                    failed_outputs.extend(str(item) for item in outputs)
                else:
                    output = str(getattr(exc, "model_output", "") or "")
                    if output:
                        failed_outputs.append(output)
                continue

            for other in futures:
                if other is not future:
                    other.cancel()
            # urllib cannot stop a request already in progress. Return without
            # waiting; the losing result is intentionally ignored.
            executor.shutdown(wait=False, cancel_futures=True)
            return result

        executor.shutdown(wait=True, cancel_futures=True)
        details = []
        for edit_mode, _prompt in jobs:
            error = errors.get(edit_mode)
            if error is None:
                continue
            label = "片段替换" if edit_mode == EDIT_MODE_FRAGMENT else "完整文本"
            details.append(f"{label}：{error}")
        message = "两种编辑协议均失败"
        if details:
            message += "（" + "；".join(details) + "）"
        model_output = failed_outputs[-1] if failed_outputs else ""
        raise LLMResponseProcessingError(
            message,
            model_output,
            tuple(failed_outputs),
        )

    @staticmethod
    def _edit_retry_prompt_for_mode(
        edit_mode: str,
        validation_error: str,
        invalid_output: str,
    ) -> str:
        safe_error = str(validation_error or "工具参数缺失或格式不正确")[:1000]
        safe_output = str(invalid_output or "（没有可用的工具参数）")[:4000]
        template = (
            EDIT_FRAGMENT_RETRY_PROMPT
            if normalize_edit_mode(edit_mode) == EDIT_MODE_FRAGMENT
            else EDIT_FULL_TEXT_RETRY_PROMPT
        )
        return template.format(
            validation_error=safe_error,
            invalid_output=safe_output,
        )

    def warmup(self, settings: LLMSettings) -> None:
        """Load the local model and seed both stable prompt prefixes."""

        settings.validate()
        if not settings.enabled:
            return
        if normalize_llm_provider(settings.provider) != LLM_PROVIDER_LOCAL:
            return
        self._ensure_local_server(settings)
        self._request_chat(
            settings,
            system_prompt=DICTATION_PROMPT,
            user_content="测试",
            temperature=0.0,
            max_tokens=1,
            edit_tool=None,
        )
        self._request_chat(
            settings,
            system_prompt=EDIT_PROMPT,
            user_content=(
                "<待修改文本>\n测试。\n</待修改文本>\n\n"
                "<修改要求>\n保持不变\n</修改要求>"
            ),
            temperature=0.0,
            max_tokens=1,
            edit_tool=None,
        )

    def _request_chat(
        self,
        settings: LLMSettings,
        *,
        system_prompt: str,
        user_content: str,
        temperature: float,
        max_tokens: int,
        edit_tool: dict[str, Any] | None = None,
    ) -> str | dict[str, Any]:
        provider = normalize_llm_provider(settings.provider)
        use_ark_responses = provider == LLM_PROVIDER_VOLCENGINE
        endpoint = (
            self._responses_url(settings.base_url)
            if use_ark_responses
            else self._chat_completions_url(settings.base_url)
        )
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "ProxiMic-Voice/0.6",
        }
        key_env = settings.api_key_env.strip()
        if key_env:
            api_key = os.environ.get(key_env, "").strip()
            if not api_key:
                raise RuntimeError(f"环境变量 {key_env} 尚未设置")
            headers["Authorization"] = f"Bearer {api_key}"
        edit_system_prompt = system_prompt
        if edit_tool is not None:
            edit_system_prompt += EDIT_TOOL_REQUIRED_PROMPT
        if use_ark_responses:
            request_body = {
                "model": settings.model.strip(),
                "stream": False,
                "input": [
                    {
                        "role": "system",
                        "content": [
                            {"type": "input_text", "text": edit_system_prompt}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": user_content}
                        ],
                    },
                ],
                "max_output_tokens": int(max_tokens),
            }
            # Doubao supports an explicit thinking switch.  Do not send this
            # provider-specific field to DeepSeek models on the same Ark API.
            if settings.model.strip().lower().startswith("doubao-"):
                request_body["thinking"] = {"type": "disabled"}
            if edit_tool is not None:
                function = edit_tool["function"]
                request_body["tools"] = [
                    {
                        "type": "function",
                        "name": function["name"],
                        "description": function["description"],
                        "parameters": function["parameters"],
                    }
                ]
        else:
            request_body = {
                "model": settings.model.strip(),
                "messages": [
                    {"role": "system", "content": edit_system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "temperature": float(temperature),
                "max_tokens": int(max_tokens),
            }
            if edit_tool is not None:
                request_body["tools"] = [edit_tool]
                # There is exactly one edit tool. llama.cpp and compatible
                # Chat Completions APIs accept this form to require a real
                # function call instead of JSON placed in normal content.
                request_body["tool_choice"] = "required"
        body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        http_request = urllib_request.Request(
            endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            response = self._urlopen(http_request, timeout=float(settings.timeout_s))
            with response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except BaseException:
                pass
            suffix = f"：{detail[:300]}" if detail else ""
            raise RuntimeError(f"大模型 API 返回 HTTP {exc.code}{suffix}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"无法连接大模型 API：{exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("大模型请求超时") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("大模型 API 返回了无法解析的数据") from exc

        if edit_tool is not None:
            expected_name = edit_tool["function"]["name"]
            content = self._extract_required_edit_tool_arguments(
                payload,
                expected_name,
            )
            if content is None:
                raw_payload = json.dumps(payload, ensure_ascii=False, indent=2)
                raise LLMResponseProcessingError(
                    "大模型没有调用 submit_text_edit 工具",
                    raw_payload[:4000],
                )
        else:
            content = self._extract_content(payload)
        if isinstance(content, str):
            content = content.strip()
            has_content = bool(content)
        else:
            has_content = True
        if not has_content:
            choice = None
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if isinstance(choices, list) and choices:
                choice = choices[0]
            finish_reason = (
                choice.get("finish_reason") or choice.get("stop_reason")
                if isinstance(choice, dict)
                else None
            )
            if not finish_reason and isinstance(payload, dict):
                finish_reason = payload.get("status")
                incomplete = payload.get("incomplete_details")
                if isinstance(incomplete, dict) and incomplete.get("reason"):
                    finish_reason = incomplete["reason"]
            detail = f"，finish_reason={finish_reason}" if finish_reason else ""
            raw_payload = json.dumps(payload, ensure_ascii=False, indent=2)
            raise LLMResponseProcessingError(
                f"大模型返回了空文本且没有有效工具参数{detail}",
                raw_payload[:4000],
            )
        return content

    @staticmethod
    def _model_output_text(response: str | dict[str, Any]) -> str:
        if isinstance(response, str):
            return response
        return json.dumps(response, ensure_ascii=False)

    @staticmethod
    def _extract_required_edit_tool_arguments(
        payload: object,
        expected_name: str = "submit_text_edit",
    ) -> str | dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if (
                    item.get("type") != "function_call"
                    or item.get("name") != expected_name
                ):
                    continue
                arguments = item.get("arguments")
                if isinstance(arguments, str) and arguments.strip():
                    return arguments
                if isinstance(arguments, dict):
                    return arguments
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            return None
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                if function.get("name") != expected_name:
                    continue
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments.strip():
                    return arguments
                if isinstance(arguments, dict):
                    return arguments
        function_call = message.get("function_call")
        if (
            isinstance(function_call, dict)
            and function_call.get("name") == expected_name
        ):
            arguments = function_call.get("arguments")
            if isinstance(arguments, str) and arguments.strip():
                return arguments
            if isinstance(arguments, dict):
                return arguments
        return None

    def _ensure_local_server(self, settings: LLMSettings) -> None:
        key = (
            settings.base_url.strip(),
            settings.model.strip(),
            settings.local_server_path.strip(),
            settings.local_model_path.strip(),
            int(settings.local_context_size),
            settings.local_reasoning.strip(),
        )
        server = self._local_servers.get(key)
        if server is None:
            server = self._local_server_factory(
                base_url=key[0],
                model_alias=key[1],
                server_path=key[2],
                model_path=key[3],
                context_size=key[4],
                reasoning=key[5],
            )
            self._local_servers[key] = server
        try:
            server.ensure_running()
        except BaseException:
            server.stop_started_process()
            self._local_servers.pop(key, None)
            raise

    def close(self) -> None:
        """Release local model processes started by this processor."""

        for server in self._local_servers.values():
            server.stop_started_process()
        self._local_servers.clear()

    @staticmethod
    def _chat_completions_url(base_url: str) -> str:
        url = str(base_url).strip().rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        return f"{url}/chat/completions"

    @staticmethod
    def _responses_url(base_url: str) -> str:
        url = str(base_url).strip().rstrip("/")
        if url.endswith("/responses"):
            return url
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")]
        return f"{url}/responses"

    @staticmethod
    def _extract_content(payload: object) -> str | dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("大模型 API 响应格式不正确")
        # Ark recommends the Responses API for Seed 2.0.  Accept its
        # function_call/output_text shapes as well as Chat Completions so a
        # compatible gateway can return either envelope.
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                arguments = item.get("arguments")
                if item.get("type") == "function_call":
                    if isinstance(arguments, str) and arguments.strip():
                        return arguments
                    if isinstance(arguments, dict):
                        return arguments
                content_items = item.get("content")
                if isinstance(content_items, list):
                    parts = [
                        content_item.get("text", "")
                        for content_item in content_items
                        if isinstance(content_item, dict)
                        and content_item.get("type") in {"output_text", "text"}
                        and isinstance(content_item.get("text"), str)
                    ]
                    if any(part.strip() for part in parts):
                        return "".join(parts)
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("大模型 API 响应中没有 choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise RuntimeError("大模型 API 响应中没有 message")
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments.strip():
                    return arguments
                if isinstance(arguments, dict):
                    return arguments
        function_call = message.get("function_call")
        if isinstance(function_call, dict):
            arguments = function_call.get("arguments")
            if isinstance(arguments, str) and arguments.strip():
                return arguments
            if isinstance(arguments, dict):
                return arguments
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        raise RuntimeError("大模型 API 响应中没有文本内容")
