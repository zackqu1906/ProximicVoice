import json

import pytest

from proximic_ring.text_processing.edit_operations import (
    EditPlanError,
    EditPlanFormatError,
    apply_edit_response,
)


def response(plan):
    return json.dumps(plan, ensure_ascii=False)


def test_multiple_surgical_operations_are_applied_without_regenerating_text():
    original = "周一评审。周二开发。预算三万元。"
    plan = {
        "kind": "operations",
        "operations": [
            {"op": "delete", "target": "周二开发。", "occurrence": "unique"},
            {
                "op": "replace",
                "target": "三万元",
                "value": "两万元",
                "occurrence": "unique",
            },
            {"op": "insert", "position": "end", "value": "周五前回复。"},
        ],
    }

    assert apply_edit_response(original, response(plan)) == (
        "周一评审。预算两万元。周五前回复。"
    )


def test_function_arguments_dict_is_applied_without_json_round_trip():
    plan = {
        "kind": "operations",
        "operations": [
            {
                "op": "replace",
                "target": "周四",
                "value": "周五",
                "occurrence": "unique",
            }
        ],
    }

    assert apply_edit_response("会议安排在周四。", plan) == "会议安排在周五。"


def test_insert_before_and_rewrite_are_supported():
    insert = {
        "kind": "operations",
        "operations": [
            {
                "op": "insert",
                "position": "before",
                "target": "谢谢。",
                "value": "请尽快回复。",
                "occurrence": "unique",
            }
        ],
    }
    assert apply_edit_response("请查看附件。谢谢。", response(insert)) == (
        "请查看附件。请尽快回复。谢谢。"
    )
    assert apply_edit_response(
        "口语内容", response({"kind": "rewrite", "text": "正式内容。"})
    ) == "正式内容。"


def test_ambiguous_or_invalid_operation_never_guesses():
    ambiguous = {
        "kind": "operations",
        "operations": [
            {
                "op": "replace",
                "target": "项目",
                "value": "任务",
                "occurrence": "unique",
            }
        ],
    }
    with pytest.raises(EditPlanError, match="出现 2 次"):
        apply_edit_response("项目一和项目二", response(ambiguous))
    with pytest.raises(EditPlanError, match="找不到目标"):
        apply_edit_response(
            "原文",
            response(
                {
                    "kind": "operations",
                    "operations": [
                        {"op": "delete", "target": "不存在", "occurrence": "unique"}
                    ],
                }
            ),
        )


def test_noop_and_json_fences_are_accepted():
    assert apply_edit_response("保持原文", '{"kind":"noop"}') == "保持原文"
    fenced = '```json\n{"kind":"rewrite","text":"新文本"}\n```'
    assert apply_edit_response("原文", fenced) == "新文本"


def test_invalid_json_and_schema_are_reported_as_format_errors():
    with pytest.raises(EditPlanFormatError, match="有效 JSON"):
        apply_edit_response("一天", '{"kind":"rewrite","text":一天"}')
    with pytest.raises(EditPlanFormatError, match="未定义字段"):
        apply_edit_response("原文", {"kind": "noop", "explanation": "不修改"})
    with pytest.raises(EditPlanFormatError, match="操作缺少 target"):
        apply_edit_response(
            "原文",
            {
                "kind": "operations",
                "operations": [{"op": "delete"}],
            },
        )
