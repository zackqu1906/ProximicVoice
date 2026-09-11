#!/usr/bin/env python3
"""Offline A/B test for plosive pressure plus broadband overload repair.

This second experiment is intentionally separate from the mild v1 filter.  It
targets two artefacts found together in the labelled recordings:

* a short 20-300 Hz pressure rise (the classic close-mic plosive); and
* a near-clipped, high-derivative broadband burst (microphone/ADC overload).

The detector uses 16 ms frames at a 4 ms hop.  A local 260 ms median provides a
level-independent baseline.  Detected pressure receives up to 18 dB of smooth
low-band attenuation; detected overload receives up to 4 dB of short full-band
attenuation.  A 45 Hz zero-phase high-pass removes diaphragm drift.  Processing
is offline and length preserving, so it can be evaluated before touching the
live ASR path.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import sys
import wave

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_V1_SPEC = importlib.util.spec_from_file_location(
    "_proximic_deplosive_v1_for_blast_repair",
    PROJECT_ROOT / "tools/test_deplosive_audio.py",
)
assert _V1_SPEC and _V1_SPEC.loader
_v1 = importlib.util.module_from_spec(_V1_SPEC)
sys.modules[_V1_SPEC.name] = _v1
_V1_SPEC.loader.exec_module(_v1)

read_wav = _v1.read_wav
SAMPLE_RATE = _v1.SAMPLE_RATE


@dataclass(frozen=True)
class BlastRepairConfig:
    fft_size: int = 256
    hop_size: int = 64
    baseline_radius_frames: int = 32
    minimum_frame_dbfs: float = -40.0
    pressure_low_excess_db: float = 4.5
    pressure_low_to_mid_db: float = -1.0
    pressure_minimum_peak: float = 0.28
    overload_minimum_peak: float = 0.82
    overload_minimum_jump: float = 0.34
    overload_energy_excess_db: float = 4.5
    clip_level: float = 0.995
    maximum_low_attenuation_db: float = 18.0
    maximum_broadband_attenuation_db: float = 4.0
    low_full_attenuation_hz: float = 170.0
    low_attenuation_end_hz: float = 340.0
    subsonic_highpass_hz: float = 45.0
    attack_frames: int = 3
    hold_frames: int = 3
    release_frames: int = 14
    broadband_attack_frames: int = 1
    broadband_hold_frames: int = 1
    broadband_release_frames: int = 5


@dataclass(frozen=True)
class BlastRepairStats:
    detected_events: int
    pressure_trigger_frames: int
    overload_trigger_frames: int
    processed_duration_ms: float
    processed_audio_pct: float
    maximum_low_attenuation_db: float
    maximum_broadband_attenuation_db: float
    sub_50hz_change_db: float
    low_20_300hz_change_db: float
    speech_300_3000hz_change_db: float
    high_3000_7500hz_change_db: float
    peak_before_dbfs: float
    peak_after_dbfs: float
    clipped_before_pct: float
    clipped_after_pct: float


@dataclass(frozen=True)
class FrameFeatures:
    frame_dbfs: np.ndarray
    energy_excess_db: np.ndarray
    low_excess_db: np.ndarray
    low_to_mid_db: np.ndarray
    peak: np.ndarray
    jump: np.ndarray
    clip_fraction: np.ndarray


def _db(value: float) -> float:
    return 10.0 * math.log10(max(float(value), 1e-20))


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-10))


def _sliding_median(values: np.ndarray, radius: int) -> np.ndarray:
    x = np.asarray(values, dtype=np.float64).reshape(-1)
    result = np.empty_like(x)
    for index in range(x.size):
        start = max(0, index - radius)
        stop = min(x.size, index + radius + 1)
        result[index] = np.median(x[start:stop])
    return result


def _analysis_frames(
    audio: np.ndarray, config: BlastRepairConfig
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
    window = np.sqrt(np.hanning(config.fft_size).astype(np.float64))
    frames = np.stack([padded[start : start + config.fft_size] for start in starts])
    spectra = np.fft.rfft(frames * window, axis=1)
    return padded, starts, frames, spectra, pad


def _frame_features(
    frames: np.ndarray,
    spectra: np.ndarray,
    config: BlastRepairConfig,
) -> FrameFeatures:
    frequencies = np.fft.rfftfreq(config.fft_size, 1.0 / SAMPLE_RATE)
    power = np.abs(spectra) ** 2

    def band_db(low: float, high: float) -> np.ndarray:
        mask = (frequencies >= low) & (frequencies < high)
        return 10.0 * np.log10(np.maximum(power[:, mask].sum(axis=1), 1e-20))

    total_db = band_db(30.0, 7500.0)
    low_db = band_db(20.0, 300.0)
    mid_db = band_db(300.0, 3000.0)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    peak = np.max(np.abs(frames), axis=1)
    jump = np.max(np.abs(np.diff(frames, axis=1)), axis=1)
    clip_fraction = np.mean(np.abs(frames) >= config.clip_level, axis=1)
    return FrameFeatures(
        frame_dbfs=20.0 * np.log10(np.maximum(rms, 1e-10)),
        energy_excess_db=total_db
        - _sliding_median(total_db, config.baseline_radius_frames),
        low_excess_db=low_db - _sliding_median(low_db, config.baseline_radius_frames),
        low_to_mid_db=low_db - mid_db,
        peak=peak,
        jump=jump,
        clip_fraction=clip_fraction,
    )


def _expand_envelope(
    target: np.ndarray,
    attack_frames: int,
    hold_frames: int,
    release_frames: int,
) -> np.ndarray:
    envelope = np.zeros_like(target, dtype=np.float64)
    for index in np.flatnonzero(target > 0.0):
        amount = float(target[index])
        attack_start = max(0, index - attack_frames)
        hold_stop = min(envelope.size, index + hold_frames + 1)
        envelope[attack_start:hold_stop] = np.maximum(
            envelope[attack_start:hold_stop], amount
        )
        for offset in range(1, release_frames + 1):
            position = index + hold_frames + offset
            if position >= envelope.size:
                break
            # Reaches roughly 5% at the end of the release.
            decay = math.exp(-3.0 * offset / max(1, release_frames))
            envelope[position] = max(envelope[position], amount * decay)
    return envelope


def _detection_envelopes(
    features: FrameFeatures,
    config: BlastRepairConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    active = features.frame_dbfs >= config.minimum_frame_dbfs
    pressure = (
        active
        & (features.low_excess_db >= config.pressure_low_excess_db)
        & (features.low_to_mid_db >= config.pressure_low_to_mid_db)
        & (features.peak >= config.pressure_minimum_peak)
    )
    clipped = features.clip_fraction > 0.0
    fast_overload = (
        (features.peak >= config.overload_minimum_peak)
        & (features.jump >= config.overload_minimum_jump)
        & (features.energy_excess_db >= config.overload_energy_excess_db)
    )
    # A clipped frame is only treated as overload when it also contains a large
    # derivative.  This avoids ducking a merely loud, smooth vowel.
    overload = active & (fast_overload | (clipped & (features.jump >= 0.28)))

    low_target = np.zeros(features.peak.size, dtype=np.float64)
    if np.any(pressure):
        low_target[pressure] = np.clip(
            10.0
            + 1.4
            * (features.low_excess_db[pressure] - config.pressure_low_excess_db)
            + 0.4
            * (features.low_to_mid_db[pressure] - config.pressure_low_to_mid_db),
            0.0,
            config.maximum_low_attenuation_db,
        )
    # Broadband overload also needs low-frequency repair even when low/mid
    # ratio is hidden by clipping harmonics.
    low_target[overload] = np.maximum(low_target[overload], 12.0)

    broad_target = np.zeros(features.peak.size, dtype=np.float64)
    if np.any(overload):
        peak_drive = np.maximum(0.0, features.peak[overload] - 0.72) * 18.0
        jump_drive = np.maximum(0.0, features.jump[overload] - 0.24) * 7.0
        transient_drive = np.maximum(
            0.0, features.energy_excess_db[overload] - 2.5
        )
        clip_boost = np.where(features.clip_fraction[overload] > 0.0, 2.0, 0.0)
        broad_target[overload] = np.clip(
            2.0
            + 0.45 * peak_drive
            + 0.25 * jump_drive
            + 0.30 * transient_drive
            + clip_boost,
            0.0,
            config.maximum_broadband_attenuation_db,
        )
    return (
        _expand_envelope(
            low_target,
            config.attack_frames,
            config.hold_frames,
            config.release_frames,
        ),
        _expand_envelope(
            broad_target,
            config.broadband_attack_frames,
            config.broadband_hold_frames,
            config.broadband_release_frames,
        ),
        pressure,
        overload,
    )


def _frequency_weights(config: BlastRepairConfig) -> tuple[np.ndarray, np.ndarray]:
    frequencies = np.fft.rfftfreq(config.fft_size, 1.0 / SAMPLE_RATE)
    low_weight = np.ones_like(frequencies)
    transition = (frequencies > config.low_full_attenuation_hz) & (
        frequencies < config.low_attenuation_end_hz
    )
    low_weight[frequencies >= config.low_attenuation_end_hz] = 0.0
    if np.any(transition):
        phase = (
            frequencies[transition] - config.low_full_attenuation_hz
        ) / (config.low_attenuation_end_hz - config.low_full_attenuation_hz)
        low_weight[transition] = 0.5 * (1.0 + np.cos(np.pi * phase))

    highpass = np.ones_like(frequencies)
    positive = frequencies > 0.0
    ratio = config.subsonic_highpass_hz / np.maximum(frequencies[positive], 1e-9)
    # Fourth-order Butterworth magnitude, applied once in zero-phase STFT form.
    highpass[positive] = 1.0 / np.sqrt(1.0 + ratio**8)
    highpass[0] = 0.0
    return low_weight, highpass


def _band_power(audio: np.ndarray, low_hz: float, high_hz: float) -> float:
    return _v1._band_power(audio, low_hz, high_hz)


def _region_count(envelope: np.ndarray, threshold_db: float = 1.0) -> int:
    active = np.asarray(envelope) >= threshold_db
    if not np.any(active):
        return 0
    return int(active[0]) + int(np.count_nonzero(active[1:] & ~active[:-1]))


def _event_ranges(
    low_envelope: np.ndarray,
    broad_envelope: np.ndarray,
    config: BlastRepairConfig,
) -> list[dict[str, float]]:
    combined = np.maximum(low_envelope, broad_envelope)
    active = combined >= 1.0
    events: list[dict[str, float]] = []
    start = None
    for index, value in enumerate(np.append(active, False)):
        if value and start is None:
            start = index
        elif not value and start is not None:
            stop = index
            events.append(
                {
                    "start_seconds": round(start * config.hop_size / SAMPLE_RATE, 3),
                    "end_seconds": round(stop * config.hop_size / SAMPLE_RATE, 3),
                    "maximum_low_attenuation_db": round(
                        float(np.max(low_envelope[start:stop])), 2
                    ),
                    "maximum_broadband_attenuation_db": round(
                        float(np.max(broad_envelope[start:stop])), 2
                    ),
                }
            )
            start = None
    return events


def process_blast_repair(
    audio: np.ndarray,
    config: BlastRepairConfig = BlastRepairConfig(),
) -> tuple[np.ndarray, BlastRepairStats, list[dict[str, float]]]:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if not x.size:
        raise ValueError("cannot process empty audio")
    padded, starts, frames, spectra, pad = _analysis_frames(x, config)
    features = _frame_features(frames, spectra, config)
    low_envelope, broad_envelope, pressure, overload = _detection_envelopes(
        features, config
    )
    low_weight, highpass = _frequency_weights(config)
    attenuation_db = broad_envelope[:, None] + low_envelope[:, None] * low_weight
    gain = 10.0 ** (-attenuation_db / 20.0)
    processed_spectra = spectra * gain * highpass[None, :]

    window = np.sqrt(np.hanning(config.fft_size).astype(np.float64))
    output = np.zeros_like(padded)
    normalization = np.zeros_like(padded)
    for index, start in enumerate(starts):
        frame = np.fft.irfft(processed_spectra[index], n=config.fft_size)
        output[start : start + config.fft_size] += frame * window
        normalization[start : start + config.fft_size] += window * window
    covered = normalization > 1e-10
    output[covered] /= normalization[covered]
    rendered = np.clip(
        output[pad : pad + x.size], -1.0, 32767.0 / 32768.0
    ).astype(np.float32)

    combined = np.maximum(low_envelope, broad_envelope)
    active_frames = combined >= 1.0
    processed_ms = float(np.count_nonzero(active_frames) * config.hop_size / 16.0)
    pcm_clip = 32767.0 / 32768.0
    stats = BlastRepairStats(
        detected_events=_region_count(combined),
        pressure_trigger_frames=int(np.count_nonzero(pressure)),
        overload_trigger_frames=int(np.count_nonzero(overload)),
        processed_duration_ms=processed_ms,
        processed_audio_pct=float(
            min(100.0, processed_ms / max(x.size / 16.0, 1e-9) * 100.0)
        ),
        maximum_low_attenuation_db=float(np.max(low_envelope)),
        maximum_broadband_attenuation_db=float(np.max(broad_envelope)),
        sub_50hz_change_db=_db(_band_power(rendered, 20.0, 50.0))
        - _db(_band_power(x, 20.0, 50.0)),
        low_20_300hz_change_db=_db(_band_power(rendered, 20.0, 300.0))
        - _db(_band_power(x, 20.0, 300.0)),
        speech_300_3000hz_change_db=_db(_band_power(rendered, 300.0, 3000.0))
        - _db(_band_power(x, 300.0, 3000.0)),
        high_3000_7500hz_change_db=_db(_band_power(rendered, 3000.0, 7500.0))
        - _db(_band_power(x, 3000.0, 7500.0)),
        peak_before_dbfs=_dbfs(np.max(np.abs(x))),
        peak_after_dbfs=_dbfs(np.max(np.abs(rendered))),
        clipped_before_pct=float(np.mean(np.abs(x) >= pcm_clip) * 100.0),
        clipped_after_pct=float(np.mean(np.abs(rendered) >= pcm_clip) * 100.0),
    )
    return rendered, stats, _event_ranges(low_envelope, broad_envelope, config)


def _write_wav(path: Path, audio: np.ndarray) -> None:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    pcm = np.rint(np.clip(x, -1.0, 32767.0 / 32768.0) * 32768.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(pcm.tobytes())


def _parse_indices(value: str) -> list[int]:
    result: list[int] = []
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        match = re.fullmatch(r"(\d+)-(\d+)", token)
        if match:
            start, stop = int(match.group(1)), int(match.group(2))
            if start > stop:
                raise ValueError(f"invalid descending range: {token}")
            result.extend(range(start, stop + 1))
        elif token.isdigit():
            result.append(int(token))
        else:
            raise ValueError(f"invalid index: {token}")
    return sorted(set(result))


def _write_report(output: Path, report: dict) -> None:
    lines = [
        "# Plosive + overload repair v2",
        "",
        "试听文件均为原始版在前、0.5 秒静音、修复版在后。",
        "",
        "## Algorithm",
        "",
        "- 16 ms STFT，4 ms hop，以局部约 260 ms 中位数作为自适应基线。",
        "- 短时 20–300 Hz 突增：仅对低频动态衰减，最大 18 dB。",
        "- 近削顶且有大幅样本跳变：仅在约 4–20 ms 内做全频衰减，最大 4 dB，并叠加低频修复。",
        "- 全程 45 Hz 四阶零相位高通，最后 overlap-add，保持原采样数不变。",
        "",
        "## Results",
        "",
        "| # | Session | Events | Coverage | Low Δ | Speech Δ | Peak before/after | Local ASR raw → repaired |",
        "|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in report["sessions"]:
        stats = item["processing"]
        asr = item["asr"]
        raw_text = str(asr.get("raw_text") or "∅").replace("|", "\\|")
        repaired_text = str(asr.get("repaired_text") or "∅").replace("|", "\\|")
        lines.append(
            f"| {item['index']} | {item['interaction_id']} | "
            f"{stats['detected_events']} | {stats['processed_audio_pct']:.1f}% | "
            f"{stats['low_20_300hz_change_db']:+.2f} dB | "
            f"{stats['speech_300_3000hz_change_db']:+.2f} dB | "
            f"{stats['peak_before_dbfs']:+.1f}/{stats['peak_after_dbfs']:+.1f} dBFS | "
            f"{raw_text} → {repaired_text} |"
        )
    lines.extend(
        (
            "",
            "本报告的本地 SenseVoice 结果只是稳定性检查，不是人工标注的准确率。",
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
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "experiments"
        / f"blast_repair_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )
    parser.add_argument(
        "--analyse-only", action="store_true", help="skip local SenseVoice"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        selected_indices = _parse_indices(args.indices)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not selected_indices:
        raise SystemExit("--indices must select at least one session")

    cases = _v1._list_recent_cases(
        args.interactions_root.expanduser().resolve(), args.recent_limit
    )
    if max(selected_indices) > len(cases):
        raise SystemExit(
            f"index {max(selected_indices)} exceeds {len(cases)} available sessions"
        )
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = BlastRepairConfig()

    backend_handle = None
    run_asr = None
    if not args.analyse_only:
        backend_handle, run_asr = _v1._load_sensevoice_runner()

    sessions: list[dict] = []
    montage: list[np.ndarray] = []
    order: list[str] = []
    for index in selected_indices:
        case = cases[index - 1]
        audio = read_wav(case.path)
        if args.max_seconds > 0:
            audio = audio[: int(round(args.max_seconds * SAMPLE_RATE))]
        repaired, stats, events = process_blast_repair(audio, config)
        directory_name = f"{index:02d}_{case.interaction_id}"
        case_dir = output / directory_name
        case_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case.path, case_dir / "original.wav")
        _write_wav(case_dir / "repaired.wav", repaired)
        pause = np.zeros(int(0.5 * SAMPLE_RATE), dtype=np.float32)
        ab = np.concatenate((audio, pause, repaired))
        _write_wav(case_dir / "ab_original_then_repaired.wav", ab)
        montage.extend((ab, np.zeros(SAMPLE_RATE, dtype=np.float32)))
        order.append(f"{index}. {case.interaction_id}")

        raw_text = None
        repaired_text = None
        raw_error = None
        repaired_error = None
        if run_asr is not None:
            try:
                raw_text = run_asr(audio)
            except BaseException as exc:
                raw_error = f"{type(exc).__name__}: {exc}"
            try:
                repaired_text = run_asr(repaired)
            except BaseException as exc:
                repaired_error = f"{type(exc).__name__}: {exc}"
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
                    "raw_text": raw_text,
                    "repaired_text": repaired_text,
                    "raw_error": raw_error,
                    "repaired_error": repaired_error,
                    "assessment": (
                        "not_run"
                        if run_asr is None or raw_error or repaired_error
                        else _v1._asr_assessment(raw_text or "", repaired_text or "")
                    ),
                },
            }
        )
        print(
            f"[{index}] {case.interaction_id} events={stats.detected_events} "
            f"coverage={stats.processed_audio_pct:.1f}% "
            f"low={stats.low_20_300hz_change_db:+.2f}dB "
            f"speech={stats.speech_300_3000hz_change_db:+.2f}dB "
            f"ASR={raw_text!r} -> {repaired_text!r}"
        )

    if montage:
        _write_wav(output / "sessions_03_to_07_original_then_repaired.wav", np.concatenate(montage))
    (output / "ORDER.txt").write_text("\n".join(order) + "\n", encoding="utf-8")
    report = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "algorithm": "adaptive_plosive_and_broadband_overload_repair_v2",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
