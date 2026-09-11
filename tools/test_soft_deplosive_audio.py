#!/usr/bin/env python3
"""Offline A/B test for a soft, low-band-only de-plosive processor.

Unlike the rejected v2 experiment, this version never ducks the full-band
signal.  It derives a complementary low band with a linear-phase FIR filter and
applies one continuous sample-rate gain envelope only to that band.  Therefore
the original waveform is bit-equivalent (up to float conversion) wherever the
detector is inactive, and frequencies above the crossover are preserved during
an event.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import importlib.util
import json
import math
from pathlib import Path
import shutil
import sys

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_tool(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_v1 = _load_tool(
    "_proximic_deplosive_v1_for_soft_v3",
    PROJECT_ROOT / "tools/test_deplosive_audio.py",
)
_v2 = _load_tool(
    "_proximic_blast_v2_for_soft_v3",
    PROJECT_ROOT / "tools/test_blast_repair_audio.py",
)

read_wav = _v1.read_wav
SAMPLE_RATE = _v1.SAMPLE_RATE


@dataclass(frozen=True)
class SoftDePlosiveConfig:
    analysis_fft_size: int = 256
    analysis_hop_size: int = 64
    baseline_radius_frames: int = 32
    minimum_frame_dbfs: float = -40.0
    minimum_low_excess_db: float = 4.5
    minimum_low_to_mid_db: float = -1.0
    minimum_peak: float = 0.28
    crossover_hz: float = 130.0
    fir_taps: int = 513
    minimum_attenuation_db: float = 4.0
    maximum_attenuation_db: float = 7.0
    lookahead_ms: float = 18.0
    hold_ms: float = 20.0
    release_ms: float = 120.0


@dataclass(frozen=True)
class SoftDePlosiveStats:
    detected_events: int
    trigger_frames: int
    processed_duration_ms: float
    processed_audio_pct: float
    maximum_attenuation_db: float
    mean_active_attenuation_db: float
    sub_50hz_change_db: float
    low_20_300hz_change_db: float
    speech_300_3000hz_change_db: float
    high_3000_7500hz_change_db: float
    peak_before_dbfs: float
    peak_after_dbfs: float
    clipped_before_pct: float
    clipped_after_pct: float


def config_for_strength(strength: str) -> SoftDePlosiveConfig:
    base = SoftDePlosiveConfig()
    if strength == "gentle":
        return base
    if strength == "stronger":
        return replace(
            base,
            crossover_hz=170.0,
            minimum_attenuation_db=7.0,
            maximum_attenuation_db=12.0,
        )
    raise ValueError(f"unknown strength: {strength}")


def _db(value: float) -> float:
    return 10.0 * math.log10(max(float(value), 1e-20))


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-10))


def _lowpass_kernel(config: SoftDePlosiveConfig) -> np.ndarray:
    taps = int(config.fir_taps)
    if taps < 3 or taps % 2 == 0:
        raise ValueError("fir_taps must be an odd integer >= 3")
    if not 0.0 < config.crossover_hz < SAMPLE_RATE / 2.0:
        raise ValueError("crossover_hz must be between 0 and Nyquist")
    centre = (taps - 1) / 2.0
    positions = np.arange(taps, dtype=np.float64) - centre
    normalized = config.crossover_hz / SAMPLE_RATE
    kernel = 2.0 * normalized * np.sinc(2.0 * normalized * positions)
    # Blackman gives a smooth, low-ripple crossover without adding a runtime
    # SciPy dependency to this test utility.
    kernel *= np.blackman(taps)
    kernel /= np.sum(kernel)
    return kernel


def _detector_targets(
    audio: np.ndarray,
    config: SoftDePlosiveConfig,
) -> tuple[np.ndarray, np.ndarray]:
    detector_config = _v2.BlastRepairConfig(
        fft_size=config.analysis_fft_size,
        hop_size=config.analysis_hop_size,
        baseline_radius_frames=config.baseline_radius_frames,
        minimum_frame_dbfs=config.minimum_frame_dbfs,
        pressure_low_excess_db=config.minimum_low_excess_db,
        pressure_low_to_mid_db=config.minimum_low_to_mid_db,
        pressure_minimum_peak=config.minimum_peak,
    )
    _padded, _starts, frames, spectra, _pad = _v2._analysis_frames(
        audio, detector_config
    )
    features = _v2._frame_features(frames, spectra, detector_config)
    triggers = (
        (features.frame_dbfs >= config.minimum_frame_dbfs)
        & (features.low_excess_db >= config.minimum_low_excess_db)
        & (features.low_to_mid_db >= config.minimum_low_to_mid_db)
        & (features.peak >= config.minimum_peak)
    )
    targets = np.zeros(features.peak.size, dtype=np.float64)
    if np.any(triggers):
        targets[triggers] = np.clip(
            config.minimum_attenuation_db
            + 0.8
            * (features.low_excess_db[triggers] - config.minimum_low_excess_db)
            + 0.2
            * (features.low_to_mid_db[triggers] - config.minimum_low_to_mid_db),
            config.minimum_attenuation_db,
            config.maximum_attenuation_db,
        )
    return targets, triggers


def _sample_envelope(
    sample_count: int,
    frame_targets: np.ndarray,
    config: SoftDePlosiveConfig,
) -> np.ndarray:
    envelope = np.zeros(sample_count, dtype=np.float64)
    lookahead = max(1, int(round(config.lookahead_ms * SAMPLE_RATE / 1000.0)))
    hold = max(1, int(round(config.hold_ms * SAMPLE_RATE / 1000.0)))
    release = max(1, int(round(config.release_ms * SAMPLE_RATE / 1000.0)))
    for frame_index in np.flatnonzero(frame_targets > 0.0):
        amount = float(frame_targets[frame_index])
        centre = frame_index * config.analysis_hop_size
        attack_start = max(0, centre - lookahead)
        attack_stop = min(sample_count, centre + 1)
        if attack_stop > attack_start:
            phase = np.linspace(0.0, 1.0, attack_stop - attack_start)
            curve = amount * 0.5 * (1.0 - np.cos(np.pi * phase))
            envelope[attack_start:attack_stop] = np.maximum(
                envelope[attack_start:attack_stop], curve
            )
        hold_stop = min(sample_count, centre + hold)
        if hold_stop > centre:
            envelope[centre:hold_stop] = np.maximum(
                envelope[centre:hold_stop], amount
            )
        release_stop = min(sample_count, centre + hold + release)
        if release_stop > hold_stop:
            phase = np.linspace(0.0, 1.0, release_stop - hold_stop)
            curve = amount * 0.5 * (1.0 + np.cos(np.pi * phase))
            envelope[hold_stop:release_stop] = np.maximum(
                envelope[hold_stop:release_stop], curve
            )
    return envelope


def _event_ranges(envelope: np.ndarray) -> list[dict[str, float]]:
    active = np.asarray(envelope) >= 0.5
    events: list[dict[str, float]] = []
    start = None
    for index, value in enumerate(np.append(active, False)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            stop = index
            events.append(
                {
                    "start_seconds": round(start / SAMPLE_RATE, 3),
                    "end_seconds": round(stop / SAMPLE_RATE, 3),
                    "maximum_attenuation_db": round(
                        float(np.max(envelope[start:stop])), 2
                    ),
                }
            )
            start = None
    return events


def process_soft_deplosive(
    audio: np.ndarray,
    config: SoftDePlosiveConfig = SoftDePlosiveConfig(),
) -> tuple[np.ndarray, SoftDePlosiveStats, list[dict[str, float]], np.ndarray]:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if not x.size:
        raise ValueError("cannot process empty audio")
    frame_targets, triggers = _detector_targets(x, config)
    envelope = _sample_envelope(x.size, frame_targets, config)

    # Complementary split: high = original - low.  With gain=1, recombination
    # is exactly the original; during a trigger only the low component changes.
    low = np.convolve(x, _lowpass_kernel(config), mode="same")
    gain = 10.0 ** (-envelope / 20.0)
    rendered = x + (gain - 1.0) * low
    rendered = np.clip(
        rendered, -1.0, 32767.0 / 32768.0
    ).astype(np.float32)

    active = envelope >= 0.5
    clip_level = 32767.0 / 32768.0
    band = _v1._band_power
    stats = SoftDePlosiveStats(
        detected_events=len(_event_ranges(envelope)),
        trigger_frames=int(np.count_nonzero(triggers)),
        processed_duration_ms=float(np.count_nonzero(active) / 16.0),
        processed_audio_pct=float(np.mean(active) * 100.0),
        maximum_attenuation_db=float(np.max(envelope)),
        mean_active_attenuation_db=(
            float(np.mean(envelope[active])) if np.any(active) else 0.0
        ),
        sub_50hz_change_db=_db(band(rendered, 20.0, 50.0))
        - _db(band(x, 20.0, 50.0)),
        low_20_300hz_change_db=_db(band(rendered, 20.0, 300.0))
        - _db(band(x, 20.0, 300.0)),
        speech_300_3000hz_change_db=_db(band(rendered, 300.0, 3000.0))
        - _db(band(x, 300.0, 3000.0)),
        high_3000_7500hz_change_db=_db(band(rendered, 3000.0, 7500.0))
        - _db(band(x, 3000.0, 7500.0)),
        peak_before_dbfs=_dbfs(np.max(np.abs(x))),
        peak_after_dbfs=_dbfs(np.max(np.abs(rendered))),
        clipped_before_pct=float(np.mean(np.abs(x) >= clip_level) * 100.0),
        clipped_after_pct=float(np.mean(np.abs(rendered) >= clip_level) * 100.0),
    )
    return rendered, stats, _event_ranges(envelope), envelope.astype(np.float32)


def _write_report(output: Path, report: dict) -> None:
    config = report["config"]
    lines = [
        "# Soft de-plosive v3 A/B test",
        "",
        "每条 A/B 音频均为原始版在前、0.5 秒静音、柔和修复版在后。",
        "",
        "## Algorithm",
        "",
        "- 16 ms / 4 ms hop 检测 20–300 Hz 相对局部基线的瞬时上升。",
        f"- {config['fir_taps']}-tap Blackman 窗 FIR 在 "
        f"{config['crossover_hz']:.0f} Hz 附近做互补分频。",
        f"- 只衰减低频支路，{config['minimum_attenuation_db']:.0f}–"
        f"{config['maximum_attenuation_db']:.0f} dB；高频支路始终保留原始信号。",
        f"- {config['lookahead_ms']:.0f} ms 预启动、"
        f"{config['hold_ms']:.0f} ms 保持、{config['release_ms']:.0f} ms "
        "余弦恢复，无全频压缩、无固定高通。",
        "",
        "## Results",
        "",
        "| # | Session | Events | Coverage | Low Δ | Speech Δ | Original ASR | Soft-v3 ASR |",
        "|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for item in report["sessions"]:
        stats = item["processing"]
        asr = item["asr"]

        def cell(value: object) -> str:
            return str(value or "∅").replace("|", "\\|").replace("\n", " ")

        lines.append(
            f"| {item['index']} | {item['interaction_id']} | "
            f"{stats['detected_events']} | {stats['processed_audio_pct']:.1f}% | "
            f"{stats['low_20_300hz_change_db']:+.2f} dB | "
            f"{stats['speech_300_3000hz_change_db']:+.3f} dB | "
            f"{cell(asr.get('raw_text'))} | {cell(asr.get('processed_text'))} |"
        )
    lines.extend(
        (
            "",
            "豆包输出只有在人工目标文本已知时才能计算真实准确率；文本变化本身不等于改善。",
        )
    )
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interactions-root", type=Path, default=PROJECT_ROOT / "dataset"
    )
    parser.add_argument("--recent-limit", type=int, default=20)
    parser.add_argument("--indices", default="3-7")
    parser.add_argument("--max-seconds", type=float, default=15.0)
    parser.add_argument(
        "--backend", choices=("none", "sensevoice", "doubao"), default="none"
    )
    parser.add_argument("--pace", type=float, default=1.0)
    parser.add_argument(
        "--strength",
        choices=("gentle", "stronger"),
        default="gentle",
        help="gentle: 130 Hz/4-7 dB; stronger: 170 Hz/7-12 dB",
    )
    parser.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "experiments"
        / f"soft_deplosive_v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        indices = _v2._parse_indices(args.indices)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    cases = _v1._list_recent_cases(
        args.interactions_root.expanduser().resolve(), args.recent_limit
    )
    if not indices or max(indices) > len(cases):
        raise SystemExit("--indices is empty or exceeds available recent sessions")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = config_for_strength(args.strength)

    backend_handle = None
    if args.backend == "none":
        run_asr = None
    elif args.backend == "sensevoice":
        backend_handle, run_asr = _v1._load_sensevoice_runner()
    else:
        api_key = _v1.resolve_api_key(args.api_key)

        def run_asr(audio: np.ndarray) -> str:
            return _v1.transcribe(
                audio,
                api_key=api_key,
                pace=max(0.0, args.pace),
                debug=args.debug,
            )

    sessions: list[dict] = []
    montage: list[np.ndarray] = []
    removed_montage: list[np.ndarray] = []
    order: list[str] = []
    errors = 0
    for index in indices:
        case = cases[index - 1]
        audio = read_wav(case.path)
        if args.max_seconds > 0:
            audio = audio[: int(round(args.max_seconds * SAMPLE_RATE))]
        processed, stats, events, _envelope = process_soft_deplosive(audio, config)
        directory_name = f"{index:02d}_{case.interaction_id}"
        case_dir = output / directory_name
        case_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case.path, case_dir / "original.wav")
        _v2._write_wav(case_dir / "soft_deplosive.wav", processed)
        removed = audio - processed
        removed_peak = float(np.max(np.abs(removed)))
        if removed_peak > 1e-9:
            removed = removed * (0.707 / removed_peak)
        _v2._write_wav(case_dir / "removed_low_band_normalized.wav", removed)
        pause = np.zeros(int(0.5 * SAMPLE_RATE), dtype=np.float32)
        ab = np.concatenate((audio, pause, processed))
        _v2._write_wav(case_dir / "ab_original_then_soft.wav", ab)
        montage.extend((ab, np.zeros(SAMPLE_RATE, dtype=np.float32)))
        removed_montage.extend((removed, np.zeros(SAMPLE_RATE, dtype=np.float32)))
        order.append(f"{index}. {case.interaction_id}")

        raw_text = None
        processed_text = None
        raw_error = None
        processed_error = None
        if run_asr is not None:
            try:
                raw_text = run_asr(audio)
            except BaseException as exc:
                raw_error = f"{type(exc).__name__}: {exc}"
                errors += 1
            try:
                processed_text = run_asr(processed)
            except BaseException as exc:
                processed_error = f"{type(exc).__name__}: {exc}"
                errors += 1
        sessions.append(
            {
                "index": index,
                "interaction_id": case.interaction_id,
                "source_audio": str(case.path.resolve()),
                "output_directory": directory_name,
                "prior_text": case.prior_text,
                "processing": asdict(stats),
                "events": events,
                "asr": {
                    "backend": None if run_asr is None else args.backend,
                    "raw_text": raw_text,
                    "processed_text": processed_text,
                    "raw_error": raw_error,
                    "processed_error": processed_error,
                    "assessment": (
                        "not_run"
                        if run_asr is None or raw_error or processed_error
                        else _v1._asr_assessment(raw_text or "", processed_text or "")
                    ),
                },
            }
        )
        print(
            f"[{index}] {case.interaction_id} events={stats.detected_events} "
            f"coverage={stats.processed_audio_pct:.1f}% "
            f"low={stats.low_20_300hz_change_db:+.2f}dB "
            f"speech={stats.speech_300_3000hz_change_db:+.3f}dB "
            f"ASR={raw_text!r} -> {processed_text!r}"
        )

    if montage:
        _v2._write_wav(
            output / "sessions_03_to_07_original_then_soft_v3.wav",
            np.concatenate(montage),
        )
        _v2._write_wav(
            output / "sessions_03_to_07_removed_low_band_normalized.wav",
            np.concatenate(removed_montage),
        )
    (output / "ORDER.txt").write_text("\n".join(order) + "\n", encoding="utf-8")
    report = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "algorithm": f"complementary_fir_soft_low_band_deplosive_v3_{args.strength}",
        "asr_backend": None if run_asr is None else args.backend,
        "config": asdict(config),
        "sessions": sessions,
    }
    temporary = output / "report.json.tmp"
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output / "report.json")
    _write_report(output, report)
    if backend_handle is not None:
        abort = getattr(backend_handle, "abort", None)
        if callable(abort):
            abort()
    print(f"输出: {output}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
