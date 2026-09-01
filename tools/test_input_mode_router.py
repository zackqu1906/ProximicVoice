"""Test automatic instruction-versus-dictation routing with three LLM choices."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
import sys
import time

try:
    from tools.test_llm import (
        COMPARE_PROVIDERS,
        DEFAULT_LOCAL_MODEL_PATH,
        DEFAULT_LOCAL_SERVER_PATH,
        PROVIDER_COMPARE,
        PROVIDER_DEEPSEEK,
        PROVIDER_DOUBAO,
        PROVIDER_LOCAL,
        _provider_label,
        _settings_for,
    )
except ModuleNotFoundError:
    # Direct execution puts ``tools`` itself, rather than the repository root,
    # first on sys.path.
    from test_llm import (
        COMPARE_PROVIDERS,
        DEFAULT_LOCAL_MODEL_PATH,
        DEFAULT_LOCAL_SERVER_PATH,
        PROVIDER_COMPARE,
        PROVIDER_DEEPSEEK,
        PROVIDER_DOUBAO,
        PROVIDER_LOCAL,
        _provider_label,
        _settings_for,
    )
from proximic_ring.text_processing import OpenAICompatibleTextProcessor
from proximic_ring.text_processing.prompts import INPUT_MODE_ROUTER_PROMPT


PROVIDER_ALIASES = {
    "1": PROVIDER_LOCAL,
    "local": PROVIDER_LOCAL,
    "本地": PROVIDER_LOCAL,
    "2": PROVIDER_DOUBAO,
    "doubao": PROVIDER_DOUBAO,
    "豆包": PROVIDER_DOUBAO,
    "3": PROVIDER_DEEPSEEK,
    "deepseek": PROVIDER_DEEPSEEK,
    "深度求索": PROVIDER_DEEPSEEK,
    "4": PROVIDER_COMPARE,
    "compare": PROVIDER_COMPARE,
    "all": PROVIDER_COMPARE,
    "对比": PROVIDER_COMPARE,
}


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _parse_provider(value: str) -> str:
    provider = PROVIDER_ALIASES.get(str(value).strip().lower())
    if provider is None:
        raise argparse.ArgumentTypeError(
            "可选：1/local、2/doubao、3/deepseek、4/compare"
        )
    return provider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="测试 LLM 自动区分听写（dictation）和编辑指令（edit）。"
    )
    parser.add_argument(
        "--provider",
        type=_parse_provider,
        help="local、doubao、deepseek 或 compare；不填则交互选择",
    )
    parser.add_argument("--text", help="要分类的一整段用户语音文本")
    parser.add_argument(
        "--expected",
        choices=("dictation", "edit"),
        help="可选预期分类；不一致时返回非零退出码",
    )
    parser.add_argument("--show-prompt", action="store_true")
    parser.add_argument("--model", help="覆盖所选模型名称")
    parser.add_argument("--base-url", help="覆盖 API Base URL")
    parser.add_argument("--api-key-env", help="覆盖 API Key 环境变量名")
    parser.add_argument("--timeout", type=float, help="请求超时秒数")
    parser.add_argument(
        "--local-server-path",
        default=os.environ.get("LOCAL_LLM_SERVER_PATH", DEFAULT_LOCAL_SERVER_PATH),
    )
    parser.add_argument(
        "--local-model-path",
        default=os.environ.get(
            "LOCAL_LLM_MODEL_PATH", str(DEFAULT_LOCAL_MODEL_PATH)
        ),
    )
    return parser


def _choose_provider(provider: str | None) -> str | None:
    if provider:
        return provider
    try:
        choice = input(
            "模型 [1=本地 Qwen, 2=豆包, 3=DeepSeek, 4=三者对比, q=退出]："
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if choice.lower() in {"q", "quit", "exit"}:
        return None
    return _parse_provider(choice or "1")


def _run_once(processor, provider: str, settings, text: str) -> tuple[bool, str]:
    label = _provider_label(provider)
    started_at = _timestamp()
    started = time.perf_counter()
    print(f"[{started_at}] 开始判断 | {label} | {settings.model}")
    try:
        mode, model_output = processor.classify_input_mode_with_trace(
            text, settings
        )
    except (RuntimeError, ValueError) as exc:
        elapsed = max(0.0, time.perf_counter() - started)
        print(
            f"[{_timestamp()}] 判断失败 | {label} | 耗时 {elapsed:.3f}s | {exc}",
            file=sys.stderr,
        )
        raw = str(getattr(exc, "model_output", "") or "").strip()
        if raw:
            print(f"模型原始返回：{raw}", file=sys.stderr)
        return False, ""
    elapsed = max(0.0, time.perf_counter() - started)
    chinese_mode = "编辑指令" if mode == "edit" else "听写"
    print(
        f"[{_timestamp()}] 判断完成 | {label} | {chinese_mode} ({mode}) | "
        f"耗时 {elapsed:.3f}s"
    )
    print(f"模型原始返回：{model_output}")
    return True, mode


def _run_text(processor, args, provider: str, text: str) -> bool:
    providers = COMPARE_PROVIDERS if provider == PROVIDER_COMPARE else (provider,)
    success = True
    for item in providers:
        settings = _settings_for(args, item)
        try:
            settings.validate()
        except ValueError as exc:
            print(f"{_provider_label(item)} 配置错误：{exc}", file=sys.stderr)
            success = False
            continue
        ok, mode = _run_once(processor, item, settings, text)
        if args.expected and ok and mode != args.expected:
            print(
                f"预期 {args.expected}，实际 {mode}",
                file=sys.stderr,
            )
            ok = False
        success = success and ok
    return success


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        provider = _choose_provider(args.provider)
    except argparse.ArgumentTypeError as exc:
        print(exc, file=sys.stderr)
        return 2
    if provider is None:
        return 0
    if args.show_prompt:
        print("--- 自动路由 system prompt ---")
        print(INPUT_MODE_ROUTER_PROMPT.strip())

    processor = OpenAICompatibleTextProcessor()
    had_failure = False
    try:
        if args.text is not None:
            if not args.text.strip():
                print("测试文本不能为空。", file=sys.stderr)
                return 2
            return 0 if _run_text(processor, args, provider, args.text) else 1

        print("逐段输入已经说完的话，程序会自动分类；输入 q 退出。")
        while True:
            try:
                text = input("\n用户语音：").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if text.lower() in {"q", "quit", "exit"}:
                break
            if not text:
                continue
            if not _run_text(processor, args, provider, text):
                had_failure = True
    finally:
        processor.close()
    return 1 if had_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
