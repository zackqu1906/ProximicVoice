"""Function schemas for the fragment and full-text edit strategies."""

from __future__ import annotations


EDIT_MODE_FRAGMENT = "fragment"
EDIT_MODE_FULL = "full"
EDIT_MODE_RACE = "race"
DEFAULT_EDIT_MODE = EDIT_MODE_FRAGMENT


FRAGMENT_EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_text_edit",
        "description": (
            "提交需要替换的原文片段和修改后的对应片段；完整文本由程序替换生成。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "original_text": {
                    "type": "string",
                    "description": (
                        "从待修改文本逐字复制并完整覆盖修改位置。"
                        "需要替换全部重复项时可以是重复片段；只修改一处时"
                        "加入上下文，使该片段唯一。"
                    ),
                },
                "modified_text": {
                    "type": "string",
                    "description": (
                        "用于替换 original_text 的完整新片段，不是整篇文本；"
                        "删除该片段时返回空字符串。"
                    ),
                },
            },
            "required": ["original_text", "modified_text"],
            "additionalProperties": False,
        },
    },
}


FULL_TEXT_EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_text_edit",
        "description": "提交执行修改要求后得到的完整文本。",
        "parameters": {
            "type": "object",
            "properties": {
                "modified_text": {
                    "type": "string",
                    "description": (
                        "执行修改要求后的完整文本，可直接覆盖原文本框；"
                        "清空全文时返回空字符串。"
                    ),
                },
            },
            "required": ["modified_text"],
            "additionalProperties": False,
        },
    },
}


def normalize_edit_mode(value: str) -> str:
    mode = str(value or DEFAULT_EDIT_MODE).strip().lower()
    aliases = {
        EDIT_MODE_FRAGMENT: EDIT_MODE_FRAGMENT,
        "patch": EDIT_MODE_FRAGMENT,
        "片段": EDIT_MODE_FRAGMENT,
        EDIT_MODE_FULL: EDIT_MODE_FULL,
        "full_text": EDIT_MODE_FULL,
        "全文": EDIT_MODE_FULL,
        EDIT_MODE_RACE: EDIT_MODE_RACE,
        "parallel": EDIT_MODE_RACE,
        "竞速": EDIT_MODE_RACE,
    }
    normalized = aliases.get(mode)
    if normalized is None:
        raise ValueError(f"不支持的编辑输出模式：{value}")
    return normalized


def edit_tool_for_mode(mode: str) -> dict:
    return (
        FULL_TEXT_EDIT_TOOL
        if normalize_edit_mode(mode) == EDIT_MODE_FULL
        else FRAGMENT_EDIT_TOOL
    )
