"""Validation and application of fragment or full-text LLM edit responses."""

from __future__ import annotations

import json
import re
from typing import Any

from .edit_tool import EDIT_MODE_FRAGMENT, normalize_edit_mode


class EditResponseError(ValueError):
    pass


class EditResponseFormatError(EditResponseError):
    """The model response is not valid JSON or does not match the contract."""


_PROMPT_LEAK_MARKERS = (
    "你是文本编辑",
    "不是聊天助手",
    "待修改文本",
    "修改要求",
    "用户要求",
    "original_text",
    "modified_text",
    "只调用",
    "不输出解释",
)


def apply_edit_response(
    original: str,
    response: str | dict[str, Any],
    edit_mode: str = EDIT_MODE_FRAGMENT,
) -> str:
    """Validate and apply an edit response, returning the complete final text."""

    source = str(original or "")
    result = _parse_json_object(response)
    if normalize_edit_mode(edit_mode) != EDIT_MODE_FRAGMENT:
        _validate_full_text_response(result)
        _reject_prompt_leak(source, result["modified_text"])
        return result["modified_text"]

    _validate_fragment_response(result)
    original_text = result["original_text"]
    modified_text = result["modified_text"]
    _reject_prompt_leak(source, modified_text)
    count = source.count(original_text)
    if count == 0:
        raise EditResponseError(
            f"original_text 不在待修改文本中：{original_text}"
        )
    # A repeated fragment means replace every match. To edit only one
    # occurrence, the model includes enough context to make it unique.
    return source.replace(original_text, modified_text)


def _reject_prompt_leak(source: str, modified_text: str) -> None:
    """Reject an echoed system prompt without blocking edits of prompt documents."""

    source_markers = sum(marker in source for marker in _PROMPT_LEAK_MARKERS)
    output_markers = sum(
        marker in modified_text for marker in _PROMPT_LEAK_MARKERS
    )
    if output_markers >= 3 and source_markers < 3:
        raise EditResponseFormatError(
            "modified_text 疑似包含系统提示词，不是有效的修改结果"
        )


def _parse_json_object(response: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    text = str(response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditResponseFormatError(
            "大模型没有返回有效 JSON 编辑结果"
            f"（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"
        ) from exc
    if not isinstance(value, dict):
        raise EditResponseFormatError("JSON 编辑结果必须是对象")
    return value


def _validate_fragment_response(result: dict[str, Any]) -> None:
    _validate_fields(result, {"original_text", "modified_text"})
    original_text = result["original_text"]
    if not isinstance(original_text, str) or not original_text:
        raise EditResponseFormatError("original_text 必须是非空字符串")
    if not isinstance(result["modified_text"], str):
        raise EditResponseFormatError("modified_text 必须是字符串")


def _validate_full_text_response(result: dict[str, Any]) -> None:
    _validate_fields(result, {"modified_text"})
    if not isinstance(result["modified_text"], str):
        raise EditResponseFormatError("modified_text 必须是字符串")


def _validate_fields(result: dict[str, Any], expected: set[str]) -> None:
    unknown = sorted(str(key) for key in result if key not in expected)
    if unknown:
        raise EditResponseFormatError(
            f"编辑结果包含未定义字段：{', '.join(unknown)}"
        )
    missing = sorted(expected - result.keys())
    if missing:
        raise EditResponseFormatError(
            f"编辑结果缺少字段：{', '.join(missing)}"
        )
