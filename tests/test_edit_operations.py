import json

import pytest

from proximic_ring.text_processing.edit_response import (
    EditResponseError,
    EditResponseFormatError,
    apply_edit_response,
)
from proximic_ring.text_processing.edit_tool import EDIT_MODE_FULL


def response(result):
    return json.dumps(result, ensure_ascii=False)


def test_fragment_is_replaced_by_python_without_regenerating_the_document():
    original = "周一评审。周二开发。预算三万元。"
    result = {
        "original_text": "周二开发。预算三万元。",
        "modified_text": "预算两万元。周五前回复。",
    }

    assert apply_edit_response(original, response(result)) == (
        "周一评审。预算两万元。周五前回复。"
    )


def test_function_arguments_dict_is_accepted_without_json_round_trip():
    result = {
        "original_text": "周四",
        "modified_text": "周五",
    }

    assert apply_edit_response("会议安排在周四。", result) == "会议安排在周五。"


def test_unchanged_fragment_deletion_and_full_clear_are_supported():
    unchanged = {
        "original_text": "保持原文",
        "modified_text": "保持原文",
    }
    cleared = {"original_text": "清空我", "modified_text": ""}

    assert apply_edit_response("保持原文", response(unchanged)) == "保持原文"
    assert apply_edit_response(
        "第一句。第二句。",
        {"original_text": "第二句。", "modified_text": ""},
    ) == "第一句。"
    assert apply_edit_response("清空我", cleared) == ""


def test_repeated_fragment_replaces_all_and_unique_context_replaces_one():
    assert apply_edit_response(
        "项目一和项目二",
        {"original_text": "项目", "modified_text": "任务"},
    ) == "任务一和任务二"
    assert apply_edit_response(
        "项目一和项目二",
        {"original_text": "项目二", "modified_text": "任务二"},
    ) == "项目一和任务二"


def test_original_text_must_exist():
    with pytest.raises(EditResponseError, match="不在待修改文本中"):
        apply_edit_response(
            "原文",
            {"original_text": "不存在", "modified_text": "新文本"},
        )


def test_full_text_mode_needs_only_modified_text():
    assert apply_edit_response(
        "原文",
        {"modified_text": "完整新文本"},
        EDIT_MODE_FULL,
    ) == "完整新文本"
    with pytest.raises(EditResponseFormatError, match="未定义字段"):
        apply_edit_response(
            "原文",
            {"original_text": "原文", "modified_text": "完整新文本"},
            EDIT_MODE_FULL,
        )


def test_prompt_echo_is_rejected_but_prompt_documents_can_still_be_edited():
    leaked_prompt = (
        "你是文本编辑规划器，不是聊天助手。根据待修改文本和用户要求，"
        "返回 original_text 和 modified_text，只调用工具。"
    )

    with pytest.raises(EditResponseFormatError, match="系统提示词"):
        apply_edit_response(
            "这是一句普通原文。",
            {"modified_text": leaked_prompt},
            EDIT_MODE_FULL,
        )

    source_prompt = leaked_prompt + "旧结尾"
    assert apply_edit_response(
        source_prompt,
        {"modified_text": leaked_prompt + "新结尾"},
        EDIT_MODE_FULL,
    ).endswith("新结尾")


def test_json_fences_are_accepted():
    fenced = (
        '```json\n{"original_text":"原文",'
        '"modified_text":"新文本"}\n```'
    )
    assert apply_edit_response("原文", fenced) == "新文本"


def test_invalid_json_and_schema_are_reported_as_format_errors():
    with pytest.raises(EditResponseFormatError, match="有效 JSON"):
        apply_edit_response(
            "一天",
            '{"original_text":"一天","modified_text":两天}',
        )
    with pytest.raises(EditResponseFormatError, match="未定义字段"):
        apply_edit_response(
            "原文",
            {
                "original_text": "原文",
                "modified_text": "原文",
                "kind": "noop",
            },
        )
    with pytest.raises(EditResponseFormatError, match="缺少字段"):
        apply_edit_response("原文", {"original_text": "原文"})
    with pytest.raises(EditResponseFormatError, match="非空字符串"):
        apply_edit_response(
            "原文", {"original_text": "", "modified_text": "原文"}
        )
