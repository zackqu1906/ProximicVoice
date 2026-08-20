"""Function schema shared by all LLM providers for deterministic edits."""

from __future__ import annotations


EDIT_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_text_edit_plan",
        "description": (
            "提交对待修改文本执行的确定性编辑计划。位置必须完全服从用户指令；"
            "一次连续插入必须合并为一个 operation。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["operations", "rewrite", "noop"],
                    "description": (
                        "局部编辑用 operations；全文生成式改写用 rewrite；"
                        "无法可靠定位时用 noop。"
                    ),
                },
                "operations": {
                    "type": "array",
                    "description": (
                        "局部操作列表。一段连续新增内容只能生成一个 insert，"
                        "不得拆成多个 insert。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["delete", "replace", "insert"],
                                "description": (
                                    "编辑动作：delete 删除 target；replace 用 value"
                                    "替换 target；insert 不删除原文，只在 position 指定"
                                    "的位置加入 value。"
                                ),
                            },
                            "target": {
                                "type": "string",
                                "description": (
                                    "从待修改文本逐字复制的最短唯一定位片段。"
                                    "before/after 的句内锚点不要包含句末标点。"
                                ),
                            },
                            "value": {
                                "type": "string",
                                "description": (
                                    "replace 的完整新内容，或一次 insert 要连续加入的"
                                    "完整内容；例如“一杯咖啡”不能拆开。"
                                ),
                            },
                            "occurrence": {
                                "type": "string",
                                "description": (
                                    "target 的匹配位置：unique、first、last、all 或"
                                    "从 1 开始的序号字符串。"
                                ),
                            },
                            "position": {
                                "type": "string",
                                "enum": ["start", "end", "before", "after"],
                                "description": (
                                    "insert 位置。start/end 是整篇文本开头/末尾，只有"
                                    "用户明确这样说时使用；before/after 是紧邻 target"
                                    "之前/之后，必须与用户说的前面/后面一致。"
                                ),
                            },
                        },
                        "required": ["op"],
                        "additionalProperties": False,
                    },
                },
                "text": {
                    "type": "string",
                    "description": "kind=rewrite 时的完整修改后文本。",
                },
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
}
