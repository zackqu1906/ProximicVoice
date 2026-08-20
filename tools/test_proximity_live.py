"""Live Stage-1/Stage-2 diagnostic using the same settings as the customer UI.

This tool deliberately does not construct ASR or LLM components.  It connects
to the Ring, runs only the ProxiMic detector, prints every detector event, and
writes the same observations to CSV for later near/far comparison.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys
import time


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from proximic_ring.audio import RingAudioSource  # noqa: E402
from proximic_ring.config import DetectorConfig  # noqa: E402
from proximic_ring.detector import ProxiMicDetector  # noqa: E402
from proximic_ring.events import Stage1Event, Stage2Event  # noqa: E402
from proximic_ring.model import ProxiMicModel  # noqa: E402
from proximic_ring.pipeline import LegacyInferencePipeline  # noqa: E402


DEFAULT_MODEL_PATH = SRC_ROOT / "proximic_ring" / "assets" / "ringo-near-v1.model"
DEFAULT_STAGE1_THRESHOLD = 0.005
DEFAULT_STAGE2_DELAY_S = 0.50
DEFAULT_STAGE2_THRESHOLD = 1.0


def _ui_settings() -> dict[str, object]:
    """Read the persisted customer-UI settings when PySide6 is available."""

    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return {}

    settings = QSettings("ProxiMic", "ProxiMic Voice")
    return {
        "name": str(settings.value("ring/name", "Ringo")),
        "selector": str(settings.value("ring/selector", "")),
        "encoding": str(settings.value("ring/audioEncoding", "pcm")),
        "model": str(settings.value("detector/model", "")),
        "stage1_threshold": settings.value(
            "detector/stage1Threshold", DEFAULT_STAGE1_THRESHOLD
        ),
    }


def _model_stage2_threshold(model_path: Path) -> float:
    sidecar = model_path.with_name(model_path.name + ".json")
    if not sidecar.is_file():
        return DEFAULT_STAGE2_THRESHOLD
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        return float(payload["recommended_stage2_threshold"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
        return DEFAULT_STAGE2_THRESHOLD


def _parser() -> argparse.ArgumentParser:
    ui = _ui_settings()
    saved_model = str(ui.get("model", "")).strip()
    # Diagnostics intentionally default to the PCM distribution used by the
    # bundled model, independently of an older persisted UI codec choice.
    saved_encoding = "pcm"
    try:
        saved_stage1 = float(
            ui.get("stage1_threshold", DEFAULT_STAGE1_THRESHOLD)
        )
    except (TypeError, ValueError):
        saved_stage1 = DEFAULT_STAGE1_THRESHOLD

    parser = argparse.ArgumentParser(
        description=(
            "连接 Ring，使用主界面当前参数实时输出 ProxiMic Stage1/Stage2；"
            "不会启动 ASR 或 LLM。"
        )
    )
    parser.add_argument("--name", default=str(ui.get("name", "Ringo")))
    parser.add_argument("--selector", default=str(ui.get("selector", "")))
    parser.add_argument(
        "--encoding",
        choices=["pcm", "adpcm", "opus"],
        default=saved_encoding,
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--model",
        type=Path,
        default=Path(saved_model) if saved_model else DEFAULT_MODEL_PATH,
    )
    parser.add_argument("--stage1-threshold", type=float, default=saved_stage1)
    parser.add_argument("--stage2-threshold", type=float, default=None)
    parser.add_argument("--stage2-delay", type=float, default=DEFAULT_STAGE2_DELAY_S)
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="测试秒数；0 表示一直运行到 Ctrl+C（默认 60）",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="CSV 输出路径；默认写入 data/proximity_diagnostics/",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "data",
        help="Ring SDK WAV 保存根目录",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印最终生效参数，不连接设备",
    )
    return parser


def _print_config(args: argparse.Namespace, stage2_threshold: float) -> None:
    print("\nProxiMic 实时诊断参数")
    print(f"  Ring name       : {args.name}")
    print(f"  Ring selector   : {args.selector or '(按名称自动扫描)'}")
    print(f"  Encoding        : {args.encoding}")
    print(f"  Model           : {args.model.resolve()}")
    print(f"  Stage1 threshold: {args.stage1_threshold:.6f}")
    print(f"  Stage2 delay    : {args.stage2_delay:.3f} s")
    print(f"  Stage2 threshold: {stage2_threshold:+.6f}")
    print("  Stage2 decision : score > threshold => ACTIVATE")
    print(f"  Duration        : {args.duration:g} s" if args.duration else "  Duration        : Ctrl+C 结束")


def _csv_path(args: argparse.Namespace) -> Path:
    if args.csv is not None:
        return args.csv.expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "data" / "proximity_diagnostics" / f"stage_events_{stamp}.csv"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.model = args.model.expanduser().resolve()
    if args.stage1_threshold <= 0:
        raise SystemExit("--stage1-threshold 必须大于 0")
    if args.stage2_delay < 0:
        raise SystemExit("--stage2-delay 不能小于 0")
    if args.duration < 0:
        raise SystemExit("--duration 不能小于 0")
    if not args.model.is_file():
        raise SystemExit(f"找不到检测模型：{args.model}")

    stage2_threshold = (
        float(args.stage2_threshold)
        if args.stage2_threshold is not None
        else _model_stage2_threshold(args.model)
    )
    _print_config(args, stage2_threshold)
    if args.dry_run:
        return 0

    config = DetectorConfig(
        stage1_threshold=float(args.stage1_threshold),
        stage2_threshold=stage2_threshold,
        stage2_delay_s=float(args.stage2_delay),
    ).validate()
    detector = ProxiMicDetector(
        config,
        LegacyInferencePipeline(model=ProxiMicModel(args.model)),
    )
    source = RingAudioSource(
        name_keyword=str(args.name),
        selector=str(args.selector).strip() or None,
        timeout_s=float(args.timeout),
        encoding=str(args.encoding),
        data_root=args.data_dir.expanduser().resolve(),
    )
    output_path = _csv_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage2_scores: list[float] = []
    started_at = time.monotonic()

    print(f"\n事件 CSV        : {output_path}")
    print("请先关闭主界面中的 Ring 连接。测试开始后分别在近处、远处说话。")
    print("按 Ctrl+C 可提前结束。\n")

    fieldnames = [
        "event",
        "time_s",
        "max_amplitude",
        "stage1_threshold",
        "score",
        "stage2_threshold",
        "margin",
        "logit_near",
        "logit_far",
        "activated",
    ]
    try:
        with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            with source:
                while args.duration == 0 or time.monotonic() - started_at < args.duration:
                    block = source.read(320)
                    if block is None:
                        break
                    for event in detector.feed(block):
                        if isinstance(event, Stage1Event):
                            excess = event.max_amplitude - config.stage1_threshold
                            print(
                                f"[STAGE1] t={event.time_s:8.3f}s "
                                f"max_amp={event.max_amplitude:.6f} "
                                f"threshold={config.stage1_threshold:.6f} "
                                f"excess={excess:+.6f}"
                            )
                            writer.writerow(
                                {
                                    "event": "stage1",
                                    "time_s": f"{event.time_s:.6f}",
                                    "max_amplitude": f"{event.max_amplitude:.8f}",
                                    "stage1_threshold": f"{config.stage1_threshold:.8f}",
                                }
                            )
                        elif isinstance(event, Stage2Event):
                            margin = event.score - config.stage2_threshold
                            decision = "ACTIVATE" if event.activated else "reject"
                            stage2_scores.append(event.score)
                            print(
                                f"[STAGE2] t={event.time_s:8.3f}s "
                                f"window=[{event.window_start_s:.3f},{event.window_end_s:.3f}] "
                                f"score={event.score:+.6f} "
                                f"threshold={config.stage2_threshold:+.6f} "
                                f"margin={margin:+.6f} "
                                f"logits=({event.logits[0]:+.6f},{event.logits[1]:+.6f}) "
                                f"{decision}"
                            )
                            writer.writerow(
                                {
                                    "event": "stage2",
                                    "time_s": f"{event.time_s:.6f}",
                                    "score": f"{event.score:.8f}",
                                    "stage2_threshold": f"{config.stage2_threshold:.8f}",
                                    "margin": f"{margin:.8f}",
                                    "logit_near": f"{event.logits[0]:.8f}",
                                    "logit_far": f"{event.logits[1]:.8f}",
                                    "activated": int(event.activated),
                                }
                            )
                        csv_file.flush()
    except KeyboardInterrupt:
        print("\n用户结束测试。")
    except Exception as exc:
        print(f"\n[失败] {exc}", file=sys.stderr)
        print("如果主界面仍连接着 Ring，请先完全断开或关闭主界面。", file=sys.stderr)
        return 1

    stats = detector.stats
    elapsed = time.monotonic() - started_at
    activation_rate = stats.activations / stats.stage2_runs if stats.stage2_runs else 0.0
    print("\n诊断汇总")
    print(f"  实际运行       : {elapsed:.2f} s")
    print(f"  Stage1 triggers: {stats.stage1_triggers}")
    print(f"  Stage2 runs    : {stats.stage2_runs}")
    print(f"  Activations    : {stats.activations}")
    print(f"  Activation rate: {activation_rate:.1%}")
    if stage2_scores:
        print(
            "  Stage2 scores  : "
            f"min={min(stage2_scores):+.6f}, "
            f"median={statistics.median(stage2_scores):+.6f}, "
            f"max={max(stage2_scores):+.6f}"
        )
    print(f"  Event CSV      : {output_path}")
    print(f"  Ring WAV       : {source.capture_path or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
