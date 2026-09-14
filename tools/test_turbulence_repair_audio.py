#!/usr/bin/env python3
"""Offline A/B test for high-frequency microphone turbulence/overload bursts.

This processor is deliberately separate from the low-frequency de-plosive
experiment.  It detects short, spectrally flat high-band outliers, caps only
the excessive time-frequency bins toward a local reference, and protects
periodic harmonics.  It never applies a fixed low-pass or full-band duck.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import importlib.util
import json
import math
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_tool(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_soft = _load_tool(
    "_proximic_soft_deplosive_for_turbulence_v4",
    PROJECT_ROOT / "tools/test_soft_deplosive_audio.py",
)
_v1 = _soft._v1
_v2 = _soft._v2
read_wav = _soft.read_wav
SAMPLE_RATE = _soft.SAMPLE_RATE


@dataclass(frozen=True)
class TurbulenceRepairConfig:
    fft_size: int = 512
    hop_size: int = 80
    baseline_radius_frames: int = 20
    high_min_hz: float = 1500.0
    high_max_hz: float = 7500.0
    attenuation_start_hz: float = 900.0
    full_attenuation_hz: float = 1500.0
    minimum_frame_dbfs: float = -42.0
    minimum_high_excess_db: float = 8.0
    minimum_high_flatness: float = 0.12
    target_margin_db: float = 2.5
    maximum_attenuation_db: float = 10.0
    harmonic_periodicity_threshold: float = 0.35
    harmonic_protection_gain: float = 0.15
    attack_frames: int = 1
    hold_frames: int = 1
    release_frames: int = 8


@dataclass(frozen=True)
class TurbulenceRepairStats:
    detected_events: int
    trigger_frames: int
    processed_duration_ms: float
    processed_audio_pct: float
    maximum_attenuation_db: float
    low_20_300hz_change_db: float
    voice_300_1500hz_change_db: float
    presence_1500_4000hz_change_db: float
    high_4000_7500hz_change_db: float
    peak_before_dbfs: float
    peak_after_dbfs: float
    clipped_before_pct: float
    clipped_after_pct: float


def _config_for_profile(profile: str) -> TurbulenceRepairConfig:
    if profile == "balanced":
        return TurbulenceRepairConfig()
    if profile == "aggressive":
        return TurbulenceRepairConfig(
            baseline_radius_frames=24,
            high_min_hz=1200.0,
            high_max_hz=7800.0,
            attenuation_start_hz=1000.0,
            full_attenuation_hz=1500.0,
            minimum_frame_dbfs=-48.0,
            minimum_high_excess_db=4.0,
            minimum_high_flatness=0.065,
            target_margin_db=-1.5,
            maximum_attenuation_db=20.0,
            harmonic_periodicity_threshold=0.28,
            harmonic_protection_gain=0.20,
            attack_frames=2,
            hold_frames=2,
            release_frames=12,
        )
    raise ValueError(f"unknown profile: {profile}")


def _db(value: float) -> float:
    return 10.0 * math.log10(max(float(value), 1e-20))


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-10))


def _analysis_frames(
    audio: np.ndarray, config: TurbulenceRepairConfig
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    pad = config.fft_size // 2
    padded = np.pad(x, (pad, pad))
    remainder = (padded.size - config.fft_size) % config.hop_size
    if remainder:
        padded = np.pad(padded, (0, config.hop_size - remainder))
    starts = np.arange(
        0, padded.size - config.fft_size + 1, config.hop_size, dtype=np.int64
    )
    raw_frames = np.stack(
        [padded[start : start + config.fft_size] for start in starts]
    )
    window = np.sqrt(np.hanning(config.fft_size).astype(np.float64))
    spectra = np.fft.rfft(raw_frames * window, axis=1)
    return padded, starts, raw_frames, spectra, pad


def _local_median(values: np.ndarray, radius: int) -> np.ndarray:
    result = np.empty_like(values, dtype=np.float64)
    for index in range(values.shape[0]):
        start = max(0, index - radius)
        stop = min(values.shape[0], index + radius + 1)
        result[index] = np.median(values[start:stop], axis=0)
    return result


def _periodicity_and_f0(frame: np.ndarray) -> tuple[float, float]:
    x = np.asarray(frame, dtype=np.float64) - float(np.mean(frame))
    if float(np.dot(x, x)) < 1e-10:
        return 0.0, 0.0
    fft_size = 1 << int(math.ceil(math.log2(max(2, x.size * 2 - 1))))
    spectrum = np.fft.rfft(x * np.hanning(x.size), n=fft_size)
    correlation = np.fft.irfft(np.abs(spectrum) ** 2, n=fft_size)[: x.size]
    correlation /= max(float(correlation[0]), 1e-20)
    min_lag = max(1, int(SAMPLE_RATE / 350.0))
    max_lag = min(x.size - 1, int(SAMPLE_RATE / 80.0))
    if max_lag <= min_lag:
        return 0.0, 0.0
    relative = int(np.argmax(correlation[min_lag : max_lag + 1]))
    lag = min_lag + relative
    return float(correlation[lag]), float(SAMPLE_RATE / lag)


def _harmonic_protection(
    frame: np.ndarray,
    frequencies: np.ndarray,
    config: TurbulenceRepairConfig,
) -> np.ndarray:
    periodicity, f0 = _periodicity_and_f0(frame)
    protection = np.ones(frequencies.size, dtype=np.float64)
    if periodicity < config.harmonic_periodicity_threshold or f0 <= 0.0:
        return protection
    bin_width = SAMPLE_RATE / config.fft_size
    harmonic = f0
    while harmonic <= min(4000.0, frequencies[-1]):
        distance = np.abs(frequencies - harmonic)
        protection[distance <= 1.25 * bin_width] = (
            config.harmonic_protection_gain
        )
        harmonic += f0
    return protection


def _frequency_focus(
    frequencies: np.ndarray, config: TurbulenceRepairConfig
) -> np.ndarray:
    focus = np.zeros_like(frequencies)
    focus[frequencies >= config.full_attenuation_hz] = 1.0
    transition = (frequencies > config.attenuation_start_hz) & (
        frequencies < config.full_attenuation_hz
    )
    if np.any(transition):
        phase = (
            frequencies[transition] - config.attenuation_start_hz
        ) / (config.full_attenuation_hz - config.attenuation_start_hz)
        focus[transition] = 0.5 * (1.0 - np.cos(np.pi * phase))
    return focus


def _attenuation_masks(
    raw_frames: np.ndarray,
    spectra: np.ndarray,
    config: TurbulenceRepairConfig,
) -> tuple[np.ndarray, np.ndarray]:
    frequencies = np.fft.rfftfreq(config.fft_size, 1.0 / SAMPLE_RATE)
    magnitude = np.abs(spectra)
    power = magnitude * magnitude
    high_mask = (frequencies >= config.high_min_hz) & (
        frequencies < config.high_max_hz
    )
    high_power = np.sum(power[:, high_mask], axis=1)
    high_baseline = _local_median(high_power[:, None], config.baseline_radius_frames)[
        :, 0
    ]
    high_excess_db = 10.0 * np.log10(
        np.maximum(high_power, 1e-20) / np.maximum(high_baseline, 1e-20)
    )
    high_flatness = np.exp(
        np.mean(np.log(np.maximum(power[:, high_mask], 1e-20)), axis=1)
    ) / np.maximum(np.mean(power[:, high_mask], axis=1), 1e-20)
    frame_rms = np.sqrt(np.mean(raw_frames * raw_frames, axis=1))
    frame_dbfs = 20.0 * np.log10(np.maximum(frame_rms, 1e-10))
    triggers = (
        (frame_dbfs >= config.minimum_frame_dbfs)
        & (high_excess_db >= config.minimum_high_excess_db)
        & (high_flatness >= config.minimum_high_flatness)
    )
    # The centered STFT uses zero padding at both ends.  Do not mistake that
    # artificial boundary for a broadband microphone transient.
    edge_guard = int(math.ceil((config.fft_size / 2) / config.hop_size))
    triggers[:edge_guard] = False
    triggers[-edge_guard:] = False

    baseline_magnitude = _local_median(magnitude, config.baseline_radius_frames)
    allowed = baseline_magnitude * (10.0 ** (config.target_margin_db / 20.0))
    raw_attenuation = np.clip(
        20.0
        * np.log10(
            np.maximum(magnitude, 1e-12) / np.maximum(allowed, 1e-12)
        ),
        0.0,
        config.maximum_attenuation_db,
    )
    raw_attenuation[~triggers] = 0.0
    focus = _frequency_focus(frequencies, config)
    raw_attenuation *= focus[None, :]
    for index in np.flatnonzero(triggers):
        raw_attenuation[index] *= _harmonic_protection(
            raw_frames[index], frequencies, config
        )

    # Smooth each attenuation curve across neighboring bins so it behaves like
    # a dynamic spectral envelope rather than a collection of narrow notches.
    frequency_kernel = np.asarray([1.0, 2.0, 3.0, 2.0, 1.0]) / 9.0
    smoothed = np.stack(
        [np.convolve(row, frequency_kernel, mode="same") for row in raw_attenuation]
    )
    smoothed *= focus[None, :]

    masks = np.zeros_like(smoothed)
    for index in np.flatnonzero(triggers):
        start = max(0, index - config.attack_frames)
        stop = min(masks.shape[0], index + config.hold_frames + 1)
        masks[start:stop] = np.maximum(masks[start:stop], smoothed[index])
        for offset in range(1, config.release_frames + 1):
            position = index + config.hold_frames + offset
            if position >= masks.shape[0]:
                break
            decay = 0.5 * (
                1.0 + math.cos(math.pi * offset / config.release_frames)
            )
            masks[position] = np.maximum(
                masks[position], smoothed[index] * decay
            )
    return masks, triggers


def _event_ranges(active_frames: np.ndarray, hop_size: int) -> list[dict[str, float]]:
    events: list[dict[str, float]] = []
    start = None
    for index, active in enumerate(np.append(active_frames, False)):
        if active and start is None:
            start = index
        elif not active and start is not None:
            events.append(
                {
                    "start_seconds": round(start * hop_size / SAMPLE_RATE, 3),
                    "end_seconds": round(index * hop_size / SAMPLE_RATE, 3),
                }
            )
            start = None
    return events


def process_turbulence(
    audio: np.ndarray,
    config: TurbulenceRepairConfig = TurbulenceRepairConfig(),
) -> tuple[np.ndarray, TurbulenceRepairStats, list[dict[str, float]]]:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if not x.size:
        raise ValueError("cannot process empty audio")
    padded, starts, raw_frames, spectra, pad = _analysis_frames(x, config)
    masks_db, triggers = _attenuation_masks(raw_frames, spectra, config)
    gain = 10.0 ** (-masks_db / 20.0)
    delta_spectra = spectra * (gain - 1.0)
    window = np.sqrt(np.hanning(config.fft_size).astype(np.float64))
    delta = np.zeros_like(padded)
    normalization = np.zeros_like(padded)
    for index, start in enumerate(starts):
        frame = np.fft.irfft(delta_spectra[index], n=config.fft_size)
        delta[start : start + config.fft_size] += frame * window
        normalization[start : start + config.fft_size] += window * window
    covered = normalization > 1e-10
    delta[covered] /= normalization[covered]
    rendered = np.clip(
        x + delta[pad : pad + x.size], -1.0, 32767.0 / 32768.0
    ).astype(np.float32)

    active_frames = np.max(masks_db, axis=1) >= 0.5
    events = _event_ranges(active_frames, config.hop_size)
    clip_level = 32767.0 / 32768.0
    band = _v1._band_power
    stats = TurbulenceRepairStats(
        detected_events=len(events),
        trigger_frames=int(np.count_nonzero(triggers)),
        processed_duration_ms=float(
            np.count_nonzero(active_frames) * config.hop_size / 16.0
        ),
        processed_audio_pct=float(np.mean(active_frames) * 100.0),
        maximum_attenuation_db=float(np.max(masks_db)),
        low_20_300hz_change_db=_db(band(rendered, 20.0, 300.0))
        - _db(band(x, 20.0, 300.0)),
        voice_300_1500hz_change_db=_db(band(rendered, 300.0, 1500.0))
        - _db(band(x, 300.0, 1500.0)),
        presence_1500_4000hz_change_db=_db(band(rendered, 1500.0, 4000.0))
        - _db(band(x, 1500.0, 4000.0)),
        high_4000_7500hz_change_db=_db(band(rendered, 4000.0, 7500.0))
        - _db(band(x, 4000.0, 7500.0)),
        peak_before_dbfs=_dbfs(np.max(np.abs(x))),
        peak_after_dbfs=_dbfs(np.max(np.abs(rendered))),
        clipped_before_pct=float(np.mean(np.abs(x) >= clip_level) * 100.0),
        clipped_after_pct=float(np.mean(np.abs(rendered) >= clip_level) * 100.0),
    )
    return rendered, stats, events


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interactions-root", type=Path, default=PROJECT_ROOT / "dataset"
    )
    parser.add_argument("--recent-limit", type=int, default=20)
    parser.add_argument("--indices", default="3-7")
    parser.add_argument(
        "--source-report",
        type=Path,
        help="lock source paths and indices to a previous report for a fair A/B comparison",
    )
    parser.add_argument(
        "--profile",
        choices=("balanced", "aggressive"),
        default="balanced",
        help="aggressive lowers the detector threshold and permits up to 20 dB attenuation",
    )
    parser.add_argument(
        "--low-profile",
        choices=("none", "aggressive-300"),
        default="none",
        help="optionally run transient-only 20-300 Hz repair before high-band repair",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "experiments"
        / f"turbulence_repair_v4_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    indices = _v2._parse_indices(args.indices)
    locked_cases = None
    if args.source_report:
        source_report = json.loads(
            args.source_report.expanduser().resolve().read_text(encoding="utf-8")
        )
        locked_cases = {
            int(item["index"]): SimpleNamespace(
                interaction_id=item["interaction_id"],
                path=Path(item["source_audio"]),
            )
            for item in source_report["sessions"]
        }
        missing = [index for index in indices if index not in locked_cases]
        if missing:
            raise SystemExit(f"indices missing from --source-report: {missing}")
        cases = []
    else:
        cases = _v1._list_recent_cases(
            args.interactions_root.expanduser().resolve(), args.recent_limit
        )
        if not indices or max(indices) > len(cases):
            raise SystemExit("--indices is empty or exceeds available recent sessions")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = _config_for_profile(args.profile)
    if args.low_profile == "aggressive-300":
        version_label = "v6_hybrid_aggressive"
    else:
        version_label = "v5_aggressive" if args.profile == "aggressive" else "v4"
    sessions: list[dict] = []
    ab_montage: list[np.ndarray] = []
    processed_montage: list[np.ndarray] = []
    removed_montage: list[np.ndarray] = []
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
    pause = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
    for index in indices:
        case = locked_cases[index] if locked_cases is not None else cases[index - 1]
        audio = read_wav(case.path)
        low_stats = None
        low_events: list[dict[str, float]] = []
        high_input = audio
        if args.low_profile == "aggressive-300":
            high_input, low_stats, low_events, _low_envelope = (
                _soft.process_soft_deplosive(
                    audio, _soft.config_for_strength(args.low_profile)
                )
            )
        processed, stats, events = process_turbulence(high_input, config)
        directory_name = f"{index:02d}_{case.interaction_id}"
        case_dir = output / directory_name
        case_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case.path, case_dir / "original.wav")
        _v2._write_wav(case_dir / "turbulence_repaired.wav", processed)
        removed = audio - processed
        removed_peak = float(np.max(np.abs(removed)))
        if removed_peak > 1e-9:
            removed = removed * (0.707 / removed_peak)
        _v2._write_wav(case_dir / "removed_high_noise_normalized.wav", removed)
        ab = np.concatenate((audio, pause, processed))
        _v2._write_wav(case_dir / "ab_original_then_repaired.wav", ab)
        ab_montage.extend((ab, silence))
        processed_montage.extend((processed, silence))
        removed_montage.extend((removed, silence))
        sessions.append(
            {
                "index": index,
                "interaction_id": case.interaction_id,
                "source_audio": str(case.path.resolve()),
                "output_directory": directory_name,
                "processing": asdict(stats),
                "low_processing": asdict(low_stats) if low_stats else None,
                "events": events,
                "low_events": low_events,
            }
        )
        low_summary = (
            f"20-300={low_stats.low_20_300hz_change_db:+.2f}dB "
            if low_stats
            else ""
        )
        print(
            f"[{index}] {case.interaction_id} events={stats.detected_events} "
            f"coverage={stats.processed_audio_pct:.1f}% "
            f"{low_summary}"
            f"1.5-4k={stats.presence_1500_4000hz_change_db:+.2f}dB "
            f"4-7.5k={stats.high_4000_7500hz_change_db:+.2f}dB"
        )

    _v2._write_wav(
        output / f"sessions_03_to_07_original_then_turbulence_{version_label}.wav",
        np.concatenate(ab_montage),
    )
    _v2._write_wav(
        output / f"sessions_03_to_07_turbulence_{version_label}_only.wav",
        np.concatenate(processed_montage),
    )
    _v2._write_wav(
        output / "sessions_03_to_07_removed_high_noise_normalized.wav",
        np.concatenate(removed_montage),
    )
    report = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "algorithm": f"harmonic_protected_spectral_outlier_turbulence_{version_label}",
        "profile": args.profile,
        "low_profile": args.low_profile,
        "config": asdict(config),
        "low_config": (
            asdict(_soft.config_for_strength(args.low_profile))
            if args.low_profile != "none"
            else None
        ),
        "sessions": sessions,
    }
    temporary = output / "report.json.tmp"
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output / "report.json")
    print(f"输出: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
