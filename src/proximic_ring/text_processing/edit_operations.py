"""Deterministic execution of short edit plans produced by the local LLM."""

from __future__ import annotations

import json
import re
from typing import Any


class EditPlanError(ValueError):
    pass


class EditPlanFormatError(EditPlanError):
    """The model plan is not valid JSON or does not match the edit contract."""


def apply_edit_response(original: str, response: str | dict[str, Any]) -> str:
    """Parse an edit JSON response and return the complete resulting text.

    The model generates only a short operation list for surgical edits.  This
    function performs the actual mutation so unchanged text never has to be
    regenerated token by token.
    """

    source = str(original or "")
    plan = _parse_json_object(response)
    _validate_edit_plan(plan)
    kind = str(plan.get("kind") or "").strip().lower()
    if kind == "rewrite":
        text = plan.get("text")
        if not isinstance(text, str) or not text.strip():
            raise EditPlanError("改写计划没有返回完整文本")
        return text.strip()
    if kind == "noop":
        return source
    if kind != "operations":
        raise EditPlanError(f"未知编辑计划类型：{kind or '空'}")

    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise EditPlanError("编辑计划中没有 operations")
    result = source
    for index, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            raise EditPlanError(f"第 {index} 个编辑操作格式错误")
        try:
            result = _apply_operation(result, operation)
        except EditPlanError as exc:
            raise EditPlanError(f"第 {index} 个编辑操作失败：{exc}") from exc
    return result


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
        raise EditPlanFormatError(
            "大模型没有返回有效 JSON 编辑计划"
            f"（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"
        ) from exc
    if not isinstance(value, dict):
        raise EditPlanFormatError("JSON 编辑计划必须是对象")
    return value


def _validate_edit_plan(plan: dict[str, Any]) -> None:
    """Validate the existing tool contract without mutating model arguments."""

    _reject_unknown_fields(plan, {"kind", "operations", "text"}, "编辑计划")
    kind = plan.get("kind")
    if not isinstance(kind, str) or kind not in {
        "operations",
        "rewrite",
        "noop",
    }:
        rendered_kind = str(kind or "空")
        raise EditPlanFormatError(f"未知编辑计划类型：{rendered_kind}")

    if "text" in plan and not isinstance(plan["text"], str):
        raise EditPlanFormatError("编辑计划的 text 必须是字符串")
    if kind == "rewrite" and not str(plan.get("text") or "").strip():
        raise EditPlanFormatError("改写计划没有返回完整文本")

    if "operations" in plan and not isinstance(plan["operations"], list):
        raise EditPlanFormatError("编辑计划的 operations 必须是数组")
    if kind != "operations":
        return

    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise EditPlanFormatError("编辑计划中没有 operations")
    for index, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            raise EditPlanFormatError(f"第 {index} 个编辑操作格式错误")
        try:
            _validate_operation(operation)
        except EditPlanFormatError as exc:
            raise EditPlanFormatError(
                f"第 {index} 个编辑操作格式错误：{exc}"
            ) from exc


def _validate_operation(operation: dict[str, Any]) -> None:
    _reject_unknown_fields(
        operation,
        {"op", "target", "value", "occurrence", "position"},
        "编辑操作",
    )
    op = operation.get("op")
    if not isinstance(op, str) or op not in {"delete", "replace", "insert"}:
        raise EditPlanFormatError(f"不支持的操作：{str(op or '空')}")

    for key in ("target", "value", "occurrence", "position"):
        if key in operation and not isinstance(operation[key], str):
            raise EditPlanFormatError(f"操作字段 {key} 必须是字符串")

    if op in {"delete", "replace"}:
        _require_plan_text(operation, "target")
    if op == "replace" and "value" not in operation:
        raise EditPlanFormatError("操作缺少 value")
    if op == "insert":
        _require_plan_text(operation, "value")
        position = str(operation.get("position", "end")).strip().lower()
        if position not in {"start", "end", "before", "after"}:
            raise EditPlanFormatError(f"不支持的插入位置：{position}")
        if position in {"before", "after"}:
            _require_plan_text(operation, "target")

    if "occurrence" in operation:
        selector = operation["occurrence"].strip().lower()
        named_occurrences = {
            "unique",
            "唯一",
            "first",
            "第一个",
            "last",
            "最后一个",
            "all",
            "全部",
        }
        if selector not in named_occurrences:
            try:
                occurrence_index = int(selector)
            except ValueError as exc:
                raise EditPlanFormatError(
                    f"不支持的 occurrence：{operation['occurrence']}"
                ) from exc
            if occurrence_index < 1:
                raise EditPlanFormatError(
                    f"不支持的 occurrence：{operation['occurrence']}"
                )


def _reject_unknown_fields(
    value: dict[str, Any],
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise EditPlanFormatError(
            f"{label}包含未定义字段：{', '.join(unknown)}"
        )


def _require_plan_text(operation: dict[str, Any], key: str) -> None:
    value = operation.get(key)
    if not isinstance(value, str) or not value:
        raise EditPlanFormatError(f"操作缺少 {key}")


def _apply_operation(text: str, operation: dict[str, Any]) -> str:
    op = str(operation.get("op") or "").strip().lower()
    if op == "replace":
        target = _required_text(operation, "target")
        value = str(operation.get("value") or "")
        return _replace_occurrence(
            text,
            target,
            value,
            str(operation.get("occurrence") or "unique"),
        )
    if op == "delete":
        target = _required_text(operation, "target")
        result = _replace_occurrence(
            text,
            target,
            "",
            str(operation.get("occurrence") or "unique"),
        )
        return _clean_after_delete(result)
    if op == "insert":
        value = _required_text(operation, "value")
        position = str(operation.get("position") or "end").strip().lower()
        if position == "start":
            return value + text
        if position == "end":
            return text + value
        if position not in {"before", "after"}:
            raise EditPlanError(f"不支持的插入位置：{position}")
        target = _required_text(operation, "target")
        occurrence = str(operation.get("occurrence") or "unique")
        replacement = value + target if position == "before" else target + value
        return _replace_occurrence(text, target, replacement, occurrence)
    raise EditPlanError(f"不支持的操作：{op or '空'}")


def _replace_occurrence(
    text: str,
    target: str,
    replacement: str,
    occurrence: str,
) -> str:
    count = text.count(target)
    if count == 0:
        raise EditPlanError(f"原文中找不到目标：{target}")
    selector = occurrence.strip().lower()
    if selector in {"all", "全部"}:
        return text.replace(target, replacement)
    if selector in {"first", "第一个"}:
        return text.replace(target, replacement, 1)
    if selector in {"last", "最后一个"}:
        position = text.rfind(target)
        return text[:position] + replacement + text[position + len(target) :]
    if selector not in {"", "unique", "唯一"}:
        try:
            wanted = int(selector)
        except ValueError as exc:
            raise EditPlanError(f"不支持的 occurrence：{occurrence}") from exc
        if wanted < 1 or wanted > count:
            raise EditPlanError(f"目标只有 {count} 处，无法修改第 {wanted} 处")
        start = -1
        for _ in range(wanted):
            start = text.find(target, start + 1)
        return text[:start] + replacement + text[start + len(target) :]
    if count != 1:
        raise EditPlanError(f"目标出现 {count} 次，但指令没有说明修改哪一处")
    return text.replace(target, replacement, 1)


def _required_text(operation: dict[str, Any], key: str) -> str:
    value = operation.get(key)
    if not isinstance(value, str) or not value:
        raise EditPlanError(f"操作缺少 {key}")
    return value


def _clean_after_delete(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", text)
    text = re.sub(r"([，,；;：:])\1+", r"\1", text)
    return text
