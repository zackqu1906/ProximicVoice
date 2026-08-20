"""Interactive and comparative test for the project's LLM text-processing flow.

This program deliberately skips the Ring, microphone, ASR, desktop injection,
and Agent.  It reuses the production prompts and HTTP processor so a result
observed here is representative of the text stage used by the application.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.machinery
import json
import os
import sys
import time
import types
from pathlib import Path
from urllib.parse import urlsplit


if os.name == "nt":
    # Keep Chinese prompts and results readable in Windows terminals whose
    # inherited Python stream encoding does not match the host application.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


# Allow ``python tools/test_llm.py`` without requiring an editable install.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"


def _register_lightweight_package() -> None:
    """Expose the package path without executing its PyTorch-heavy __init__."""

    package_name = "proximic_ring"
    if package_name in sys.modules:
        return
    package_path = str(SRC_ROOT / package_name)
    package = types.ModuleType(package_name)
    package.__file__ = str(Path(package_path) / "__init__.py")
    package.__package__ = package_name
    package.__path__ = [package_path]
    package.__spec__ = importlib.machinery.ModuleSpec(
        package_name,
        loader=None,
        is_package=True,
    )
    sys.modules[package_name] = package


_register_lightweight_package()

from proximic_ring.text_processing import (  # noqa: E402
    DEFAULT_ARK_API_KEY_ENV,
    DEFAULT_ARK_BASE_URL,
    DEFAULT_ARK_DEEPSEEK_MODEL,
    DEFAULT_ARK_MODEL,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_CONTEXT_SIZE,
    DEFAULT_LOCAL_MODEL,
    DEFAULT_LOCAL_MODEL_PATH,
    DEFAULT_LOCAL_REASONING,
    DEFAULT_LOCAL_SERVER_PATH,
    INPUT_MODE_DICTATION,
    INPUT_MODE_EDIT,
    LLM_PROVIDER_LOCAL,
    LLM_PROVIDER_OPENAI,
    LLM_PROVIDER_VOLCENGINE,
    LLMSettings,
    OpenAICompatibleTextProcessor,
)
from proximic_ring.text_processing.llm import (  # noqa: E402
    DICTATION_PROMPT,
    EDIT_PROMPT,
)


MODE_ALIASES = {
    "1": INPUT_MODE_EDIT,
    "edit": INPUT_MODE_EDIT,
    "修改": INPUT_MODE_EDIT,
    "instruction": INPUT_MODE_EDIT,
    "指令": INPUT_MODE_EDIT,
    "2": INPUT_MODE_DICTATION,
    "dictation": INPUT_MODE_DICTATION,
    "听写": INPUT_MODE_DICTATION,
}

PROVIDER_LOCAL = "local"
PROVIDER_OPENAI = "openai"
PROVIDER_DOUBAO = "doubao"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_COMPARE = "compare"
PROVIDER_ALIASES = {
    "1": PROVIDER_LOCAL,
    "local": PROVIDER_LOCAL,
    "本地": PROVIDER_LOCAL,
    "2": PROVIDER_OPENAI,
    "openai": PROVIDER_OPENAI,
    "online": PROVIDER_OPENAI,
    "线上": PROVIDER_OPENAI,
    "3": PROVIDER_DOUBAO,
    "doubao": PROVIDER_DOUBAO,
    "豆包": PROVIDER_DOUBAO,
    "4": PROVIDER_DEEPSEEK,
    "deepseek": PROVIDER_DEEPSEEK,
    "深度求索": PROVIDER_DEEPSEEK,
    "5": PROVIDER_COMPARE,
    "compare": PROVIDER_COMPARE,
    "all": PROVIDER_COMPARE,
    "对比": PROVIDER_COMPARE,
}

COMPARE_PROVIDERS = (PROVIDER_LOCAL, PROVIDER_DOUBAO, PROVIDER_DEEPSEEK)


@dataclass(frozen=True)
class ModelRunResult:
    provider: str
    label: str
    model: str
    success: bool
    elapsed_s: float
    final_text: str = ""
    model_output: str = ""
    model_outputs: tuple[str, ...] = ()
    error: str = ""

def _parse_mode(value: str) -> str:
    mode = MODE_ALIASES.get(str(value).strip().lower())
    if mode is None:
        valid = "1/edit/修改 或 2/dictation/输入"
        raise argparse.ArgumentTypeError(f"未知类型 {value!r}，可选值：{valid}")
    return mode


def _parse_provider(value: str) -> str:
    provider = PROVIDER_ALIASES.get(str(value).strip().lower())
    if provider is None:
        valid = (
            "1/local/本地、2/openai/线上、3/doubao/豆包、"
            "4/deepseek 或 5/compare/对比"
        )
        raise argparse.ArgumentTypeError(f"未知模型来源 {value!r}，可选值：{valid}")
    return provider


def _mode_label(mode: str) -> str:
    return "指令" if mode == INPUT_MODE_EDIT else "听写"


def _provider_label(provider: str) -> str:
    return {
        PROVIDER_LOCAL: "本地 Qwen",
        PROVIDER_OPENAI: "OpenAI-compatible API",
        PROVIDER_DOUBAO: "豆包 Seed 2.0 Lite",
        PROVIDER_DEEPSEEK: "DeepSeek V4 Flash",
        PROVIDER_COMPARE: "本地 Qwen / 豆包 / DeepSeek 对比",
    }[provider]


def _prompt_for(mode: str) -> str:
    return EDIT_PROMPT if mode == INPUT_MODE_EDIT else DICTATION_PROMPT


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="独立测试 ProxiMic Voice 的输入/修改大模型处理流程。",
    )
    parser.add_argument(
        "--provider",
        type=_parse_provider,
        help=(
            "模型来源：local、openai、doubao、deepseek 或 compare；"
            "交互模式会主动询问"
        ),
    )
    parser.add_argument(
        "--mode",
        type=_parse_mode,
        help="单次测试类型：edit/指令/1 或 dictation/听写/2",
    )
    parser.add_argument("--text", help="单次测试文本；不填写时进入交互模式")
    parser.add_argument(
        "--target-text",
        help="指令模式的待修改全文；使用 --mode edit 时必填",
    )
    parser.add_argument(
        "--model",
        help="覆盖模型名称",
    )
    parser.add_argument(
        "--base-url",
        help="覆盖 OpenAI-compatible API Base URL",
    )
    parser.add_argument(
        "--api-key-env",
        help="覆盖保存 API Key 的环境变量名；本地模式默认不需要 Key",
    )
    parser.add_argument("--timeout", type=float, help="请求超时秒数")
    parser.add_argument(
        "--local-server-path",
        default=os.environ.get("LOCAL_LLM_SERVER_PATH", DEFAULT_LOCAL_SERVER_PATH),
        help="本地 llama-server.exe 路径",
    )
    parser.add_argument(
        "--local-model-path",
        default=os.environ.get(
            "LOCAL_LLM_MODEL_PATH",
            str(DEFAULT_LOCAL_MODEL_PATH),
        ),
        help="本地 GGUF 模型文件路径",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="请求前显示该类型实际使用的 system prompt",
    )
    return parser


def _choose_provider(args: argparse.Namespace) -> str | None:
    if args.provider:
        return args.provider
    if args.base_url:
        host = (urlsplit(args.base_url).hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1"}:
            return PROVIDER_LOCAL
    if args.text is not None:
        return PROVIDER_OPENAI

    while True:
        try:
            choice = input(
                "模型 [1=本地 Qwen, 2=OpenAI API, 3=豆包, "
                "4=DeepSeek, 5=三者对比, q=退出]："
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if choice.lower() in {"q", "quit", "exit"}:
            return None
        try:
            return _parse_provider(choice or "1")
        except argparse.ArgumentTypeError as exc:
            print(exc, file=sys.stderr)


def _settings_for(args: argparse.Namespace, provider: str) -> LLMSettings:
    if provider == PROVIDER_LOCAL:
        return LLMSettings(
            enabled=True,
            base_url=args.base_url
            or os.environ.get("LOCAL_LLM_BASE_URL", DEFAULT_LOCAL_BASE_URL),
            model=args.model
            or os.environ.get("LOCAL_LLM_MODEL", DEFAULT_LOCAL_MODEL),
            api_key_env=args.api_key_env if args.api_key_env is not None else "",
            timeout_s=args.timeout if args.timeout is not None else 180.0,
            provider=LLM_PROVIDER_LOCAL,
            local_server_path=args.local_server_path,
            local_model_path=args.local_model_path,
            local_auto_start=True,
            local_context_size=DEFAULT_LOCAL_CONTEXT_SIZE,
            local_reasoning=DEFAULT_LOCAL_REASONING,
        )
    if provider in {PROVIDER_DOUBAO, PROVIDER_DEEPSEEK}:
        default_model = (
            DEFAULT_ARK_MODEL
            if provider == PROVIDER_DOUBAO
            else DEFAULT_ARK_DEEPSEEK_MODEL
        )
        return LLMSettings(
            enabled=True,
            base_url=args.base_url
            or os.environ.get("ARK_BASE_URL", DEFAULT_ARK_BASE_URL),
            model=args.model or default_model,
            api_key_env=(
                args.api_key_env
                if args.api_key_env is not None
                else DEFAULT_ARK_API_KEY_ENV
            ),
            timeout_s=args.timeout if args.timeout is not None else 60.0,
            provider=LLM_PROVIDER_VOLCENGINE,
        )
    return LLMSettings(
        enabled=True,
        base_url=args.base_url
        or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        model=args.model or os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
        api_key_env=(
            args.api_key_env if args.api_key_env is not None else "OPENAI_API_KEY"
        ),
        timeout_s=args.timeout if args.timeout is not None else 30.0,
        provider=LLM_PROVIDER_OPENAI,
    )


def _providers_for(provider: str) -> tuple[str, ...]:
    if provider == PROVIDER_COMPARE:
        return COMPARE_PROVIDERS
    return (provider,)


def _request_once(
    processor: OpenAICompatibleTextProcessor,
    settings: LLMSettings,
    *,
    provider: str,
    mode: str,
    text: str,
    target_text: str = "",
) -> ModelRunResult:
    started = time.perf_counter()
    try:
        process_with_attempts = getattr(processor, "process_with_attempts", None)
        if callable(process_with_attempts):
            result, model_outputs = process_with_attempts(
                text,
                mode,
                settings,
                target_text,
            )
            model_outputs = tuple(model_outputs)
            model_output = model_outputs[-1] if model_outputs else ""
        else:
            result, model_output = processor.process_with_trace(
                text,
                mode,
                settings,
                target_text,
            )
            model_outputs = (model_output,) if model_output else ()
    except (RuntimeError, ValueError) as exc:
        elapsed = time.perf_counter() - started
        model_output = str(getattr(exc, "model_output", "") or "").strip()
        model_outputs = tuple(getattr(exc, "model_outputs", ()) or ())
        if not model_outputs and model_output:
            model_outputs = (model_output,)
        return ModelRunResult(
            provider=provider,
            label=_provider_label(provider),
            model=settings.model,
            success=False,
            elapsed_s=elapsed,
            model_output=model_output,
            model_outputs=model_outputs,
            error=str(exc),
        )

    elapsed = time.perf_counter() - started
    return ModelRunResult(
        provider=provider,
        label=_provider_label(provider),
        model=settings.model,
        success=True,
        elapsed_s=elapsed,
        final_text=result,
        model_output=model_output,
        model_outputs=model_outputs,
    )


def _display_run(run: ModelRunResult, *, mode: str) -> None:
    print(f"\n=== {run.label} ===")
    print(f"模型：{run.model}")
    model_outputs = run.model_outputs or (
        (run.model_output,) if run.model_output else ()
    )
    for attempt, model_output in enumerate(model_outputs, start=1):
        display_output = model_output.strip()
        if mode == INPUT_MODE_EDIT:
            try:
                display_output = json.dumps(
                    json.loads(display_output),
                    ensure_ascii=False,
                    indent=2,
                )
            except (TypeError, json.JSONDecodeError):
                pass
        if len(model_outputs) > 1:
            status = "失败，已重试" if attempt < len(model_outputs) else (
                "成功" if run.success else "失败"
            )
            if mode == INPUT_MODE_EDIT and run.provider == PROVIDER_LOCAL:
                label = "真正 tool_call arguments"
            elif mode == INPUT_MODE_EDIT:
                label = "function/tool arguments"
            else:
                label = "模型原始返回"
            print(f"[第 {attempt} 次 {label}：{status}]")
        elif mode == INPUT_MODE_EDIT and run.provider == PROVIDER_LOCAL:
            print("[真正 tool_call arguments]")
        elif mode == INPUT_MODE_EDIT:
            print("[function/tool arguments]")
        else:
            print("[模型原始返回]")
        print(display_output)
    if not run.success:
        print(f"[失败，{run.elapsed_s:.2f}s] {run.error}", file=sys.stderr)
        return
    output_label = "执行后的完整文本" if mode == INPUT_MODE_EDIT else "整理后的听写文本"
    print(f"[{output_label}，{run.elapsed_s:.2f}s]")
    print(run.final_text)


def _display_comparison(runs: list[ModelRunResult]) -> None:
    print(f"\n=== 最终结果集中比较（{len(runs)} 个模型） ===")
    for index, run in enumerate(runs, start=1):
        status = "成功" if run.success else "失败"
        print(
            f"\n[{index}] {run.label} / {run.model} | "
            f"{status} | {run.elapsed_s:.2f}s"
        )
        if run.success:
            print(run.final_text)
        else:
            print(f"错误：{run.error}")

    successful = [run for run in runs if run.success]
    if len(successful) >= 2 and len(
        {run.final_text for run in successful}
    ) == 1:
        print("\n所有成功模型的最终文本完全一致。")


def _run_models(
    processor: OpenAICompatibleTextProcessor,
    targets: list[tuple[str, LLMSettings]],
    *,
    mode: str,
    text: str,
    target_text: str = "",
    show_prompt: bool,
) -> bool:
    if show_prompt:
        print(f"\n--- {_mode_label(mode)}提示词（所有模型共用）---")
        print(_prompt_for(mode))

    print(f"\n[{_mode_label(mode)}] 将同一输入发送给 {len(targets)} 个模型。")
    runs = []
    for provider, settings in targets:
        print(f"正在请求 {_provider_label(provider)} / {settings.model} ...")
        run = _request_once(
            processor,
            settings,
            provider=provider,
            mode=mode,
            text=text,
            target_text=target_text,
        )
        runs.append(run)
        _display_run(run, mode=mode)
    if len(runs) > 1:
        _display_comparison(runs)
    return all(run.success for run in runs)


def _interactive(
    processor: OpenAICompatibleTextProcessor,
    targets: list[tuple[str, LLMSettings]],
    *,
    provider: str,
    show_prompt: bool,
) -> int:
    print("ProxiMic Voice LLM 独立测试")
    print(f"测试范围：{_provider_label(provider)}")
    for target_provider, settings in targets:
        print(
            f"- {_provider_label(target_provider)}：{settings.model} "
            f"({settings.base_url})"
        )
    print("每次选择听写或指令；指令模式会先询问待修改全文。输入 q 退出。")

    had_failure = False
    while True:
        try:
            choice = input("\n类型 [1=指令, 2=听写, q=退出]：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if choice.lower() in {"q", "quit", "exit"}:
            break
        try:
            mode = _parse_mode(choice)
        except argparse.ArgumentTypeError as exc:
            print(exc, file=sys.stderr)
            continue

        try:
            target_text = ""
            if mode == INPUT_MODE_EDIT:
                target_text = input("待修改全文：").strip()
                if not target_text:
                    print("指令模式的待修改全文不能为空。")
                    continue
                text = input("修改指令：").strip()
            else:
                text = input("听写文本：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            print("文本为空，本次跳过。")
            continue
        if not _run_models(
            processor,
            targets,
            mode=mode,
            text=text,
            target_text=target_text,
            show_prompt=show_prompt,
        ):
            had_failure = True

    return 1 if had_failure else 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    provider = _choose_provider(args)
    if provider is None:
        return 0
    providers = _providers_for(provider)
    targets = [(item, _settings_for(args, item)) for item in providers]
    for target_provider, settings in targets:
        try:
            settings.validate()
        except ValueError as exc:
            print(
                f"{_provider_label(target_provider)} 配置错误：{exc}",
                file=sys.stderr,
            )
            return 2

    missing_key_targets = [
        (target_provider, settings.api_key_env)
        for target_provider, settings in targets
        if settings.api_key_env
        and not os.environ.get(settings.api_key_env, "").strip()
    ]
    if missing_key_targets:
        missing = "、".join(
            f"{_provider_label(item)} 需要 {key_env}"
            for item, key_env in missing_key_targets
        )
        print(f"提示：{missing}；这些模型会显示为失败，其他模型仍会运行。")

    processor = OpenAICompatibleTextProcessor()
    if args.text is not None:
        mode = args.mode or INPUT_MODE_EDIT
        if not args.text.strip():
            print("测试文本不能为空。", file=sys.stderr)
            return 2
        if mode == INPUT_MODE_EDIT and not str(args.target_text or "").strip():
            print("指令模式需要 --target-text（待修改全文）。", file=sys.stderr)
            return 2
        return 0 if _run_models(
            processor,
            targets,
            mode=mode,
            text=args.text,
            target_text=args.target_text or "",
            show_prompt=args.show_prompt,
        ) else 1

    return _interactive(
        processor,
        targets,
        provider=provider,
        show_prompt=args.show_prompt,
    )


if __name__ == "__main__":
    raise SystemExit(main())
