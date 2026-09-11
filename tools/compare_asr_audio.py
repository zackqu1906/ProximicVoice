#!/usr/bin/env python3
"""Compare Doubao, streaming SenseVoice Small, and Fun-ASR Nano on WAVs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from difflib import SequenceMatcher
import gc
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DOUBAO_TOOL_SPEC = importlib.util.spec_from_file_location(
    "_proximic_test_doubao_audio", PROJECT_ROOT / "tools/test_doubao_audio.py"
)
assert _DOUBAO_TOOL_SPEC and _DOUBAO_TOOL_SPEC.loader
_doubao_tool = importlib.util.module_from_spec(_DOUBAO_TOOL_SPEC)
sys.modules[_DOUBAO_TOOL_SPEC.name] = _doubao_tool
_DOUBAO_TOOL_SPEC.loader.exec_module(_doubao_tool)
AudioCase = _doubao_tool.AudioCase
DEFAULT_DATASET = _doubao_tool.DEFAULT_DATASET
DEFAULT_INTERACTIONS = _doubao_tool.DEFAULT_INTERACTIONS
dataset_cases = _doubao_tool.dataset_cases
read_wav = _doubao_tool.read_wav
recent_cases = _doubao_tool.recent_cases
resolve_api_key = _doubao_tool.resolve_api_key
transcribe_doubao = _doubao_tool.transcribe


BACKENDS = ("doubao", "sensevoice", "funasr_nano")
SENSEVOICE_MODEL = (
    PROJECT_ROOT
    / ".cache/modelscope/models/iic--SenseVoiceSmall/snapshots/master"
)
SENSEVOICE_REPO = PROJECT_ROOT / "third_party/streaming-sensevoice"
FUNASR_REPO = PROJECT_ROOT / "third_party/Fun-ASR"
FUNASR_MODEL_ID = "FunAudioLLM/Fun-ASR-Nano-2512"


def _case_key(case: AudioCase) -> str:
    return str(case.path.resolve())


def _load_report(path: Path) -> dict:
    if not path.is_file():
        return {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cases": {},
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("cases"), dict):
        raise ValueError(f"invalid comparison report: {path}")
    return data


def _save_report(path: Path, report: dict) -> None:
    report["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    temporary = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _import_doubao_results(
    report: dict,
    cases: list[AudioCase],
    *,
    result_files: list[Path],
    reuse_interaction_records: bool,
) -> int:
    imported = 0
    by_path = {_case_key(case): case for case in cases}
    for result_file in result_files:
        try:
            items = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(Path(str(item.get("path") or "")).resolve())
            case = by_path.get(key)
            if case is None or item.get("error") is not None:
                continue
            entry = report["cases"].setdefault(
                key,
                {
                    "path": key,
                    "group": case.group,
                    "interaction_id": case.interaction_id,
                    "prior_doubao_text": case.prior_text,
                    "results": {},
                },
            )
            if "doubao" not in entry["results"]:
                entry["results"]["doubao"] = {
                    "text": str(item.get("new_text") or "").strip(),
                    "elapsed_s": None,
                    "error": None,
                    "source": str(result_file.resolve()),
                }
                imported += 1

    if reuse_interaction_records:
        for case in cases:
            if not case.interaction_id:
                continue
            key = _case_key(case)
            entry = report["cases"].setdefault(
                key,
                {
                    "path": key,
                    "group": case.group,
                    "interaction_id": case.interaction_id,
                    "prior_doubao_text": case.prior_text,
                    "results": {},
                },
            )
            if "doubao" not in entry["results"]:
                entry["results"]["doubao"] = {
                    "text": case.prior_text.strip(),
                    "elapsed_s": None,
                    "error": None,
                    "source": "interaction_record",
                }
                imported += 1
    return imported


def _prepare_cases(args: argparse.Namespace) -> list[AudioCase]:
    cases = dataset_cases(args.dataset, args.dataset_limit)
    cases.extend(
        recent_cases(
            args.interactions_root,
            empty_limit=args.recent_empty,
            recognized_limit=args.recent_recognized,
        )
    )
    seen: set[Path] = set()
    unique: list[AudioCase] = []
    for case in cases:
        resolved = case.path.resolve()
        if resolved not in seen:
            unique.append(case)
            seen.add(resolved)
    return unique


def _report_cases(report: dict) -> list[AudioCase]:
    """Restore the exact first-run manifest for a resumed backend run."""

    cases: list[AudioCase] = []
    for item in report.get("cases", {}).values():
        path = Path(str(item.get("path") or ""))
        if path.is_file():
            cases.append(
                AudioCase(
                    group=str(item.get("group") or "unknown"),
                    path=path,
                    prior_text=str(item.get("prior_doubao_text") or ""),
                    interaction_id=str(item.get("interaction_id") or ""),
                )
            )
    return cases


def _load_sensevoice() -> tuple[object, Callable[[np.ndarray], str]]:
    if not (SENSEVOICE_MODEL / "model.pt").is_file():
        raise FileNotFoundError(
            f"SenseVoice cache missing: {SENSEVOICE_MODEL}; launch the app once with SenseVoice"
        )
    from proximic_ring.asr.backends.streaming_sensevoice import StreamingSenseVoiceASR

    backend = StreamingSenseVoiceASR(
        model=str(SENSEVOICE_MODEL),
        device="cpu",
        language="zh",
        repo_path=SENSEVOICE_REPO,
        final_redecode=True,
    )

    def run(audio: np.ndarray) -> str:
        backend.start()
        try:
            return backend.finish(audio)
        except BaseException:
            backend.abort()
            raise

    return backend, run


def _load_funasr_nano() -> tuple[object, Callable[[np.ndarray], str]]:
    os.environ.setdefault(
        "MODELSCOPE_CACHE", str((PROJECT_ROOT / ".cache/modelscope").resolve())
    )
    from proximic_ring.asr.backends.funasr_nano import FunASRNanoStreamingASR

    local_model = FUNASR_REPO / "pretrained_models/Fun-ASR-Nano-2512"
    model = str(local_model) if local_model.is_dir() else FUNASR_MODEL_ID
    sys.path.insert(0, str(SENSEVOICE_REPO))
    try:
        backend = FunASRNanoStreamingASR(
            model=model,
            device="cpu",
            language="zh",
            repo_path=FUNASR_REPO,
            final_redecode=True,
        )
    finally:
        try:
            sys.path.remove(str(SENSEVOICE_REPO))
        except ValueError:
            pass

    def run(audio: np.ndarray) -> str:
        backend.start()
        try:
            return backend.finish(audio)
        except BaseException:
            backend.abort()
            raise

    return backend, run


def _load_doubao(pace: float) -> tuple[None, Callable[[np.ndarray], str]]:
    api_key = resolve_api_key()

    def run(audio: np.ndarray) -> str:
        return transcribe_doubao(audio, api_key=api_key, pace=pace, debug=False)

    return None, run


def _normalize_text(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u3400-\u9fff]+", "", str(text)).lower()


def _agreement(left: str, right: str) -> float | None:
    a, b = _normalize_text(left), _normalize_text(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _print_summary(report: dict, cases: list[AudioCase], backends: list[str]) -> None:
    print("\n识别出字率：")
    print("group                  backend          nonempty/total   median_s")
    groups = sorted({case.group for case in cases})
    for group in groups:
        group_cases = [case for case in cases if case.group == group]
        for backend in backends:
            results = [
                report["cases"].get(_case_key(case), {}).get("results", {}).get(backend)
                for case in group_cases
            ]
            completed = [item for item in results if isinstance(item, dict)]
            nonempty = sum(
                bool(_normalize_text(item.get("text", ""))) for item in completed
            )
            latencies = [
                float(item["elapsed_s"])
                for item in completed
                if item.get("error") is None and item.get("elapsed_s") is not None
            ]
            median = float(np.median(latencies)) if latencies else float("nan")
            print(
                f"{group:22s} {backend:16s} {nonempty:3d}/{len(completed):<3d}       {median:7.2f}"
            )

    print("\n模型文本相似度（无人工标注，只表示模型之间是否说得相近）：")
    for left, right in (
        ("doubao", "sensevoice"),
        ("doubao", "funasr_nano"),
        ("sensevoice", "funasr_nano"),
    ):
        values: list[float] = []
        for case in cases:
            results = report["cases"].get(_case_key(case), {}).get("results", {})
            lval, rval = results.get(left), results.get(right)
            if isinstance(lval, dict) and isinstance(rval, dict):
                values.append(
                    _agreement(lval.get("text", ""), rval.get("text", "")) or 0.0
                )
        if values:
            print(
                f"{left:12s} vs {right:12s}: mean={np.mean(values):.3f}, "
                f"median={np.median(values):.3f}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--dataset-limit", type=int, default=-1, help="-1 selects every plus24db WAV"
    )
    parser.add_argument(
        "--interactions-root", type=Path, default=DEFAULT_INTERACTIONS
    )
    parser.add_argument("--recent-empty", type=int, default=20)
    parser.add_argument("--recent-recognized", type=int, default=10)
    parser.add_argument(
        "--backend", action="append", choices=BACKENDS, dest="backends"
    )
    parser.add_argument("--max-seconds", type=float, default=15.0)
    parser.add_argument(
        "--doubao-pace",
        type=float,
        default=1.0,
        help="1 matches live capture; 0 sends as fast as possible",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "experiments/asr_three_model_many.json",
    )
    parser.add_argument(
        "--restart", action="store_true", help="discard completed results in output"
    )
    parser.add_argument(
        "--import-doubao-json",
        action="append",
        default=[],
        type=Path,
        help="reuse Doubao results from test_doubao_audio.py JSON output",
    )
    parser.add_argument(
        "--reuse-recorded-doubao",
        action="store_true",
        help="reuse the original Doubao final stored with each interaction WAV",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backends = list(dict.fromkeys(args.backends or BACKENDS))
    report = _load_report(Path("/nonexistent")) if args.restart else _load_report(args.output)
    cases = _report_cases(report) or _prepare_cases(args)
    if not cases:
        raise SystemExit("没有找到待测音频")
    report["configuration"] = {
        "backends": backends,
        "dataset": str(args.dataset.resolve()),
        "dataset_limit": args.dataset_limit,
        "recent_empty": args.recent_empty,
        "recent_recognized": args.recent_recognized,
        "max_seconds": args.max_seconds,
        "doubao_pace": args.doubao_pace,
    }
    imported = _import_doubao_results(
        report,
        cases,
        result_files=args.import_doubao_json,
        reuse_interaction_records=args.reuse_recorded_doubao,
    )
    if imported:
        _save_report(args.output, report)
        print(f"复用已有豆包结果：{imported} 条", flush=True)
    print(f"共 {len(cases)} 条音频，后端：{', '.join(backends)}", flush=True)

    loaders = {
        "doubao": lambda: _load_doubao(max(0.0, args.doubao_pace)),
        "sensevoice": _load_sensevoice,
        "funasr_nano": _load_funasr_nano,
    }
    failures = 0
    for backend_name in backends:
        pending = [
            case
            for case in cases
            if backend_name
            not in report["cases"].get(_case_key(case), {}).get("results", {})
        ]
        if not pending:
            print(f"\n{backend_name}: 已有全部 {len(cases)} 条结果，跳过", flush=True)
            continue
        print(f"\n正在加载 {backend_name}（待测 {len(pending)} 条）…", flush=True)
        load_started = time.perf_counter()
        owner, runner = loaders[backend_name]()
        print(f"{backend_name} 加载完成：{time.perf_counter() - load_started:.1f}s", flush=True)
        try:
            for index, case in enumerate(pending, 1):
                audio = read_wav(case.path)
                if args.max_seconds > 0:
                    audio = audio[: int(round(args.max_seconds * 16_000))]
                started = time.perf_counter()
                text = ""
                error = None
                try:
                    text = str(runner(audio) or "").strip()
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    failures += 1
                elapsed = time.perf_counter() - started
                key = _case_key(case)
                entry = report["cases"].setdefault(
                    key,
                    {
                        "path": key,
                        "group": case.group,
                        "interaction_id": case.interaction_id,
                        "prior_doubao_text": case.prior_text,
                        "audio_duration_s": len(audio) / 16_000,
                        "results": {},
                    },
                )
                entry["results"][backend_name] = {
                    "text": text,
                    "elapsed_s": round(elapsed, 3),
                    "error": error,
                }
                _save_report(args.output, report)
                outcome = repr(text) if error is None else f"ERROR {error}"
                print(
                    f"[{backend_name} {index:02d}/{len(pending):02d}] "
                    f"{case.group} {case.path.parent.name}/{case.path.name}: "
                    f"{outcome} ({elapsed:.2f}s)",
                    flush=True,
                )
        finally:
            del runner, owner
            gc.collect()

    _print_summary(report, cases, backends)
    print(f"\n完整逐条结果：{args.output.resolve()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
