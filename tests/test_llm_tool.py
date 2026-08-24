from argparse import Namespace

from tools import test_llm


def _args(**overrides):
    values = {
        "base_url": None,
        "model": None,
        "api_key_env": None,
        "timeout": None,
        "local_server_path": "llama-server.exe",
        "local_model_path": "model.gguf",
    }
    values.update(overrides)
    return Namespace(**values)


def test_compare_selection_uses_the_three_models_available_in_the_ui():
    providers = test_llm._providers_for(test_llm.PROVIDER_COMPARE)

    assert providers == (
        test_llm.PROVIDER_LOCAL,
        test_llm.PROVIDER_DOUBAO,
        test_llm.PROVIDER_DEEPSEEK,
    )
    settings = [test_llm._settings_for(_args(), provider) for provider in providers]
    assert settings[0].local_auto_start is True
    assert settings[0].local_reasoning == "off"
    assert settings[0].timeout_s == 180.0
    assert settings[1].model == test_llm.DEFAULT_ARK_MODEL
    assert settings[2].model == test_llm.DEFAULT_ARK_DEEPSEEK_MODEL


def test_comparison_sends_identical_instruction_input_to_every_model(capsys):
    calls = []

    class FakeProcessor:
        def process_with_trace(
            self, text, mode, settings, target_text, edit_mode
        ):
            calls.append((text, mode, target_text, settings.model, edit_mode))
            suffix = "甲" if settings.model == "model-a" else "乙"
            arguments = (
                '{"original_text":"周四","modified_text":"周五"}'
                if edit_mode == test_llm.EDIT_MODE_FRAGMENT
                else '{"modified_text":"会议安排在周五。"}'
            )
            return (
                f"会议安排在周五。-{suffix}",
                arguments,
            )

    targets = [
        (
            test_llm.PROVIDER_LOCAL,
            test_llm.LLMSettings(enabled=True, model="model-a", api_key_env=""),
        ),
        (
            test_llm.PROVIDER_DEEPSEEK,
            test_llm.LLMSettings(enabled=True, model="model-b", api_key_env=""),
        ),
    ]

    success = test_llm._run_models(
        FakeProcessor(),
        targets,
        mode=test_llm.INPUT_MODE_EDIT,
        text="把周四改成周五",
        target_text="会议安排在周四。",
        show_prompt=False,
    )

    assert success is True
    assert [call[:3] for call in calls] == [
        ("把周四改成周五", test_llm.INPUT_MODE_EDIT, "会议安排在周四。"),
        ("把周四改成周五", test_llm.INPUT_MODE_EDIT, "会议安排在周四。"),
        ("把周四改成周五", test_llm.INPUT_MODE_EDIT, "会议安排在周四。"),
        ("把周四改成周五", test_llm.INPUT_MODE_EDIT, "会议安排在周四。"),
    ]
    assert [call[4] for call in calls] == [
        test_llm.EDIT_MODE_FRAGMENT,
        test_llm.EDIT_MODE_FULL,
        test_llm.EDIT_MODE_FRAGMENT,
        test_llm.EDIT_MODE_FULL,
    ]
    output = capsys.readouterr().out
    assert "Python 片段替换后的完整文本" in output
    assert "模型返回的完整文本" in output
    assert "[竞速胜出]" in output
    assert "最终结果集中比较（4 次运行）" in output
    assert "本地 Qwen / model-a" in output
    assert "DeepSeek V4 Flash / model-b" in output
    assert " vs " not in output


def test_comparison_groups_all_models_with_elapsed_time(capsys):
    runs = [
        test_llm.ModelRunResult(
            provider=test_llm.PROVIDER_LOCAL,
            label="本地 Qwen",
            model="qwen-local",
            success=True,
            elapsed_s=2.55,
            final_text="本地结果",
        ),
        test_llm.ModelRunResult(
            provider=test_llm.PROVIDER_DOUBAO,
            label="豆包 Seed 2.0 Lite",
            model="doubao-test",
            success=True,
            elapsed_s=3.98,
            final_text="豆包结果",
        ),
        test_llm.ModelRunResult(
            provider=test_llm.PROVIDER_DEEPSEEK,
            label="DeepSeek V4 Flash",
            model="deepseek-test",
            success=False,
            elapsed_s=4.12,
            error="编辑失败",
        ),
    ]

    test_llm._display_comparison(runs)

    output = capsys.readouterr().out
    assert "最终结果集中比较（3 次运行）" in output
    assert "本地 Qwen / qwen-local | 成功 | 2.55s" in output
    assert "豆包 Seed 2.0 Lite / doubao-test | 成功 | 3.98s" in output
    assert "DeepSeek V4 Flash / deepseek-test | 失败 | 4.12s" in output
    assert "本地结果" in output
    assert "豆包结果" in output
    assert "错误：编辑失败" in output
    assert " vs " not in output


def test_retry_outputs_are_all_displayed(capsys):
    run = test_llm.ModelRunResult(
        provider=test_llm.PROVIDER_DOUBAO,
        label="豆包 Seed 2.0 Lite",
        model="doubao-test",
        success=False,
        elapsed_s=8.91,
        model_output='{"original_text":"一天","modified_text":两天}',
        model_outputs=(
            '{"original_text":"一天","modified_text":一天}',
            '{"original_text":"一天","modified_text":两天}',
        ),
        error="大模型没有返回有效 JSON 编辑结果",
        edit_mode=test_llm.EDIT_MODE_FRAGMENT,
    )

    test_llm._display_run(run, mode=test_llm.INPUT_MODE_EDIT)

    captured = capsys.readouterr()
    assert "第 1 次 function/tool arguments：失败，已重试" in captured.out
    assert '{"original_text":"一天","modified_text":一天}' in captured.out
    assert "第 2 次 function/tool arguments：失败" in captured.out
    assert '{"original_text":"一天","modified_text":两天}' in captured.out
