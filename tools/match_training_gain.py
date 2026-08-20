"""Create a gain-matched copy of a ProxiMic training dataset.

One global gain is applied to every WAV so relative near/far/artifact levels are
preserved.  Originals are never modified.  The automatic estimate compares the
90th percentile of 100 ms frame RMS in the reference recording with the median
per-file value from the near-speech training takes.  Far samples are deliberately
not used to estimate gain because doing so would confuse distance with gain.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import shutil
import sys
import wave

import numpy as np


PCM16_SCALE = 32768.0


def _read_pcm16(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"只支持 PCM16 WAV：{path}")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio / PCM16_SCALE, rate


def _write_pcm16(path: Path, audio: np.ndarray, rate: int) -> None:
    pcm = np.rint(np.clip(audio, -1.0, 32767 / PCM16_SCALE) * PCM16_SCALE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.astype("<i2").tobytes())


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-12))


def _frame_rms(audio: np.ndarray, rate: int, frame_ms: float = 100.0) -> np.ndarray:
    frame = max(1, int(round(rate * frame_ms / 1000.0)))
    usable = audio.size // frame * frame
    if usable == 0:
        return np.array([float(np.sqrt(np.mean(np.square(audio))))], dtype=np.float64)
    framed = audio[:usable].reshape(-1, frame).astype(np.float64)
    return np.sqrt(np.mean(np.square(framed), axis=1))


def _metrics(audio: np.ndarray, rate: int) -> dict[str, float]:
    frame_levels = _frame_rms(audio, rate)
    return {
        "duration_s": audio.size / rate,
        "peak": float(np.max(np.abs(audio))) if audio.size else 0.0,
        "rms": float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if audio.size else 0.0,
        "frame_rms_p50": float(np.percentile(frame_levels, 50)),
        "frame_rms_p75": float(np.percentile(frame_levels, 75)),
        "frame_rms_p90": float(np.percentile(frame_levels, 90)),
        "frame_rms_p95": float(np.percentile(frame_levels, 95)),
        "frame_rms_p99": float(np.percentile(frame_levels, 99)),
        "clip_fraction": float(np.mean(np.abs(audio) >= 32767 / PCM16_SCALE)) if audio.size else 0.0,
    }


def _load_metadata(dataset: Path) -> tuple[list[dict[str, str]], list[str]]:
    metadata = dataset / "metadata.csv"
    with metadata.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    if not rows or "path" not in fields:
        raise ValueError(f"训练集 metadata.csv 无有效记录：{metadata}")
    return rows, fields


def _estimate_gain(
    dataset: Path,
    rows: list[dict[str, str]],
    reference: Path,
) -> tuple[float, dict[str, object]]:
    ref_audio, ref_rate = _read_pcm16(reference)
    ref_metrics = _metrics(ref_audio, ref_rate)
    per_class: dict[str, list[float]] = {}
    per_group: dict[str, list[float]] = {}
    near_levels: list[float] = []
    near_audio: list[tuple[np.ndarray, int]] = []
    missing_source_files: list[str] = []
    for row in rows:
        wav_path = dataset / row["path"]
        if not wav_path.is_file():
            missing_source_files.append(row["path"])
            continue
        audio, rate = _read_pcm16(wav_path)
        level = _metrics(audio, rate)["frame_rms_p90"]
        label = row.get("class_name", "unknown")
        per_class.setdefault(label, []).append(level)
        group = f"{label}@{row.get('distance_cm', '?')}cm"
        per_group.setdefault(group, []).append(level)
        if label == "near":
            near_levels.append(level)
            near_audio.append((audio, rate))
    if not near_levels:
        raise ValueError("metadata.csv 中没有 near 语音样本")

    training_level = float(np.median(near_levels))
    reference_level = float(ref_metrics["frame_rms_p90"])
    linear_gain = reference_level / max(training_level, 1e-12)
    ceiling = float(ref_metrics["peak"])

    def rendered_near_level(candidate_gain: float) -> float:
        values = [
            _metrics(np.clip(audio * candidate_gain, -ceiling, ceiling), rate)[
                "frame_rms_p90"
            ]
            for audio, rate in near_audio
        ]
        return float(np.median(values))

    # Once the new microphone includes a limiter, the linear RMS ratio
    # underestimates the gain required after saturation.  Solve for the global
    # gain whose rendered near median actually matches the reference p90.
    low = 0.0
    high = max(1.0, linear_gain)
    while rendered_near_level(high) < reference_level and high < 1_000_000.0:
        high *= 2.0
    for _ in range(32):
        middle = (low + high) / 2.0
        if rendered_near_level(middle) < reference_level:
            low = middle
        else:
            high = middle
    gain = high
    rendered_level = rendered_near_level(gain)
    report = {
        "method": (
            "global gain solved so peak-limited near-training median frame-RMS p90 "
            "matches reference frame-RMS p90"
        ),
        "reference": str(reference),
        "reference_metrics": {
            **ref_metrics,
            "frame_rms_p90_dbfs": _dbfs(ref_metrics["frame_rms_p90"]),
        },
        "training_near_median_frame_rms_p90": training_level,
        "training_near_median_frame_rms_p90_dbfs": _dbfs(training_level),
        "linear_gain_before_ceiling": linear_gain,
        "linear_gain_before_ceiling_db": _dbfs(linear_gain),
        "rendered_near_median_frame_rms_p90": rendered_level,
        "rendered_near_median_frame_rms_p90_dbfs": _dbfs(rendered_level),
        "training_class_median_frame_rms_p90": {
            label: float(np.median(levels)) for label, levels in per_class.items()
        },
        "training_group_median_frame_rms_p90": {
            group: float(np.median(levels)) for group, levels in per_group.items()
        },
        "estimated_gain": gain,
        "estimated_gain_db": _dbfs(gain),
        "missing_source_files": missing_source_files,
    }
    return gain, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按参考录音估计统一增益，生成不覆盖原文件的新训练集。"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--gain-db",
        type=float,
        default=None,
        help="手动指定统一增益 dB；不填写则使用 100 ms frame-RMS p90 自动估计",
    )
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="只分析和打印建议增益，不创建新训练集",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dataset = args.dataset.expanduser().resolve()
    reference = args.reference.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not (dataset / "metadata.csv").is_file():
        raise SystemExit(f"找不到训练集 metadata.csv：{dataset}")
    if not reference.is_file():
        raise SystemExit(f"找不到参考音频：{reference}")
    if output.exists() and not args.analyze_only:
        raise SystemExit(f"输出目录已存在，为保护已有数据不会覆盖：{output}")

    rows, fields = _load_metadata(dataset)
    estimated_gain, report = _estimate_gain(dataset, rows, reference)
    gain = (
        10.0 ** (float(args.gain_db) / 20.0)
        if args.gain_db is not None
        else estimated_gain
    )
    report["applied_gain"] = gain
    report["applied_gain_db"] = _dbfs(gain)
    ceiling = float(report["reference_metrics"]["peak"])
    report["applied_ceiling"] = ceiling
    report["applied_ceiling_dbfs"] = _dbfs(ceiling)

    reference_metrics = report["reference_metrics"]
    print(
        "参考音频                  : "
        f"duration={reference_metrics['duration_s']:.2f}s, "
        f"peak={_dbfs(reference_metrics['peak']):+.2f} dBFS, "
        f"global RMS={_dbfs(reference_metrics['rms']):+.2f} dBFS, "
        f"clip={reference_metrics['clip_fraction']:.4%}"
    )
    print(
        "参考 100ms frame RMS      : "
        f"p50={_dbfs(reference_metrics['frame_rms_p50']):+.2f}, "
        f"p75={_dbfs(reference_metrics['frame_rms_p75']):+.2f}, "
        f"p90={_dbfs(reference_metrics['frame_rms_p90']):+.2f}, "
        f"p95={_dbfs(reference_metrics['frame_rms_p95']):+.2f}, "
        f"p99={_dbfs(reference_metrics['frame_rms_p99']):+.2f} dBFS"
    )
    print(f"训练 near 中位 p90 RMS : {report['training_near_median_frame_rms_p90_dbfs']:+.2f} dBFS")
    print(f"建议统一增益           : {report['estimated_gain_db']:+.2f} dB ({estimated_gain:.3f}x)")
    print(f"本次应用增益           : {report['applied_gain_db']:+.2f} dB ({gain:.3f}x)")
    print(f"参考峰值限幅           : {report['applied_ceiling_dbfs']:+.2f} dBFS")
    print("各类别原始中位 p90 RMS:")
    for label, value in report["training_class_median_frame_rms_p90"].items():
        print(f"  {label:8s}: {_dbfs(value):+.2f} dBFS")
    print("各距离原始中位 p90 RMS:")
    for group, value in report["training_group_median_frame_rms_p90"].items():
        print(f"  {group:16s}: {_dbfs(value):+.2f} dBFS")
    if report["missing_source_files"]:
        print(f"缺失 WAV（将跳过）      : {len(report['missing_source_files'])}")
        for missing in report["missing_source_files"]:
            print(f"  {missing}")
    if args.analyze_only:
        return 0

    output.mkdir(parents=True)
    output_rows: list[dict[str, str]] = []
    clipped_samples = 0
    total_samples = 0
    file_reports: list[dict[str, object]] = []
    near_candidates: list[tuple[float, Path, Path]] = []
    for row in rows:
        source_path = dataset / row["path"]
        if not source_path.is_file():
            continue
        target_path = output / row["path"]
        audio, rate = _read_pcm16(source_path)
        boosted = audio * gain
        clipped = int(np.count_nonzero(np.abs(boosted) > ceiling))
        clipped_samples += clipped
        total_samples += int(boosted.size)
        rendered = np.clip(boosted, -ceiling, ceiling)
        _write_pcm16(target_path, rendered, rate)
        original_metrics = _metrics(audio, rate)
        metrics = _metrics(rendered, rate)
        new_row = dict(row)
        if "peak" in new_row:
            new_row["peak"] = f"{metrics['peak']:.8f}"
        if "rms" in new_row:
            new_row["rms"] = f"{metrics['rms']:.8f}"
        output_rows.append(new_row)
        file_reports.append(
            {
                "path": row["path"],
                "class_name": row.get("class_name", ""),
                "clipped_samples": clipped,
                "clip_fraction": clipped / max(1, boosted.size),
                "input_frame_rms_p90": original_metrics["frame_rms_p90"],
                "output_peak": metrics["peak"],
                "output_rms": metrics["rms"],
            }
        )
        if row.get("class_name") == "near":
            near_candidates.append(
                (
                    abs(
                        original_metrics["frame_rms_p90"]
                        - report["training_near_median_frame_rms_p90"]
                    ),
                    source_path,
                    target_path,
                )
            )

    with (output / "metadata.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output_rows)

    listening_dir = output / "listening_comparison"
    listening_dir.mkdir()
    shutil.copy2(reference, listening_dir / "reference_new_gain.wav")
    if near_candidates:
        _, original_example, matched_example = min(near_candidates, key=lambda item: item[0])
        shutil.copy2(original_example, listening_dir / "training_near_original.wav")
        shutil.copy2(matched_example, listening_dir / "training_near_gain_matched.wav")

    report["output"] = str(output)
    report["files_processed"] = len(output_rows)
    report["total_samples"] = total_samples
    report["clipped_samples"] = clipped_samples
    report["clip_fraction"] = clipped_samples / max(1, total_samples)
    report["files"] = file_reports
    report["listening_comparison"] = str(listening_dir)
    (output / "gain_match_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已生成新训练集：{output}")
    print(f"处理 WAV 数量  ：{len(output_rows)}")
    print(f"削波样本比例    ：{report['clip_fraction']:.4%}")
    print(f"分析报告        ：{output / 'gain_match_report.json'}")
    print(f"试听对照        ：{listening_dir}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
