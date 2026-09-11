#!/usr/bin/env python3
"""Offline A/B test for a transient-gated de-plosive preprocessor.

The utility deliberately does not alter the live detector/ASR path.  It selects
the newest completed InteractionRecord WAVs, renders an adaptive low-frequency
attenuation pass, writes listening pairs, and optionally re-runs both variants
through the same Doubao streaming ASR.

The algorithm is intentionally small and deterministic:

* a 64 ms STFT measures 20-250 Hz pressure energy against 300-3000 Hz speech;
* a local median distinguishes short low-frequency bursts from sustained voice;
* only detected bursts receive a smooth, frequency-dependent attenuation;
* a gentle 35 Hz high-pass removes subsonic diaphragm drift;
* overlap-add preserves the original length and avoids block-boundary clicks.

This is an offline quality experiment.  If it improves ASR, the same detector
and gain envelope can be converted to a stateful streaming implementation.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime
from difflib import SequenceMatcher
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
_DOUBAO_TOOL_SPEC = importlib.util.spec_from_file_location(
    "_proximic_test_doubao_audio_for_deplosive",
    PROJECT_ROOT / "tools/test_doubao_audio.py",
)
assert _DOUBAO_TOOL_SPEC and _DOUBAO_TOOL_SPEC.loader
_doubao_tool = importlib.util.module_from_spec(_DOUBAO_TOOL_SPEC)
sys.modules[_DOUBAO_TOOL_SPEC.name] = _doubao_tool
_DOUBAO_TOOL_SPEC.loader.exec_module(_doubao_tool)

read_wav = _doubao_tool.read_wav
resolve_api_key = _doubao_tool.resolve_api_key
transcribe = _doubao_tool.transcribe


SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class DePlosiveConfig:
    fft_size: int = 1024
    hop_size: int = 160
    detection_low_min_hz: float = 20.0
    detection_low_max_hz: float = 250.0
    detection_mid_min_hz: float = 300.0
    detection_mid_max_hz: float = 3000.0
    baseline_radius_frames: int = 24
    minimum_frame_dbfs: float = -45.0
    minimum_low_excess_db: float = 7.0
    minimum_low_to_mid_db: float = 3.0
    maximum_attenuation_db: float = 8.0
    full_attenuation_hz: float = 140.0
    attenuation_end_hz: float = 260.0
    subsonic_highpass_hz: float = 35.0
    pre_attack_frames: int = 2
    hold_frames: int = 2
    release_frames: int = 10


@dataclass(frozen=True)
class ProcessStats:
    detected_regions: int
    processed_duration_ms: float
    processed_audio_pct: float
    maximum_attenuation_db: float
    mean_active_attenuation_db: float
    sub_50hz_change_db: float
    low_20_250hz_change_db: float
    speech_300_3000hz_change_db: float
    peak_before_dbfs: float
    peak_after_dbfs: float
    clipped_before_pct: float
    clipped_after_pct: float


@dataclass(frozen=True)
class SessionCase:
    interaction_id: str
    path: Path
    prior_text: str
    record_path: Path


def _db(value: float) -> float:
    return 10.0 * math.log10(max(float(value), 1e-20))


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-10))


def _sliding_median(values: np.ndarray, radius: int) -> np.ndarray:
    """Return a centred rolling median without requiring SciPy."""

    values = np.asarray(values, dtype=np.float64).reshape(-1)
    radius = max(0, int(radius))
    if radius == 0 or values.size <= 1:
        return values.copy()
    result = np.empty_like(values)
    for index in range(values.size):
        start = max(0, index - radius)
        stop = min(values.size, index + radius + 1)
        result[index] = np.median(values[start:stop])
    return result


def _analysis_frames(
    audio: np.ndarray, config: DePlosiveConfig
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
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
    frames = np.stack(
        [padded[start : start + config.fft_size] for start in starts]
    )
    spectra = np.fft.rfft(frames * window, axis=1)
    return padded, starts, spectra, pad


def _detection_envelope(
    audio: np.ndarray,
    spectra: np.ndarray,
    config: DePlosiveConfig,
) -> np.ndarray:
    frequencies = np.fft.rfftfreq(config.fft_size, 1.0 / SAMPLE_RATE)
    low_mask = (frequencies >= config.detection_low_min_hz) & (
        frequencies < config.detection_low_max_hz
    )
    mid_mask = (frequencies >= config.detection_mid_min_hz) & (
        frequencies < config.detection_mid_max_hz
    )
    power = np.abs(spectra) ** 2
    low_db = 10.0 * np.log10(np.maximum(power[:, low_mask].sum(axis=1), 1e-20))
    mid_db = 10.0 * np.log10(np.maximum(power[:, mid_mask].sum(axis=1), 1e-20))
    low_to_mid_db = low_db - mid_db
    baseline = _sliding_median(low_db, config.baseline_radius_frames)
    low_excess_db = low_db - baseline

    # Match one RMS value to each centred STFT frame.  The analysis padding is
    # intentionally omitted here so padding cannot trigger the detector.
    half = config.fft_size // 2
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    frame_dbfs = np.empty(spectra.shape[0], dtype=np.float64)
    for index in range(spectra.shape[0]):
        centre = index * config.hop_size
        start = max(0, centre - half)
        stop = min(x.size, centre + half)
        segment = x[start:stop]
        rms = math.sqrt(float(np.mean(segment * segment))) if segment.size else 0.0
        frame_dbfs[index] = _dbfs(rms)

    valid = (
        (frame_dbfs >= config.minimum_frame_dbfs)
        & (low_excess_db >= config.minimum_low_excess_db)
        & (low_to_mid_db >= config.minimum_low_to_mid_db)
    )
    target = np.zeros_like(low_db)
    if np.any(valid):
        # The first term reacts to a sudden low-frequency rise.  The second is
        # deliberately weaker: voiced speech can naturally contain more low
        # than mid energy, but a large ratio strengthens an already transient
        # event.  Capping prevents over-processing severe/clipped samples.
        target[valid] = np.clip(
            3.0
            + 1.15 * (low_excess_db[valid] - config.minimum_low_excess_db)
            + 0.35 * (low_to_mid_db[valid] - config.minimum_low_to_mid_db),
            0.0,
            config.maximum_attenuation_db,
        )

    envelope = np.zeros_like(target)
    for index in np.flatnonzero(target > 0.0):
        amount = float(target[index])
        attack_start = max(0, index - config.pre_attack_frames)
        hold_stop = min(envelope.size, index + config.hold_frames + 1)
        envelope[attack_start:hold_stop] = np.maximum(
            envelope[attack_start:hold_stop], amount
        )
        for offset in range(1, config.release_frames + 1):
            position = index + config.hold_frames + offset
            if position >= envelope.size:
                break
            decay = math.exp(-3.0 * offset / max(1, config.release_frames))
            envelope[position] = max(envelope[position], amount * decay)
    return envelope


def _frequency_weight(config: DePlosiveConfig) -> tuple[np.ndarray, np.ndarray]:
    frequencies = np.fft.rfftfreq(config.fft_size, 1.0 / SAMPLE_RATE)
    weight = np.ones_like(frequencies)
    transition = (frequencies > config.full_attenuation_hz) & (
        frequencies < config.attenuation_end_hz
    )
    weight[frequencies >= config.attenuation_end_hz] = 0.0
    if np.any(transition):
        phase = (
            frequencies[transition] - config.full_attenuation_hz
        ) / (config.attenuation_end_hz - config.full_attenuation_hz)
        weight[transition] = 0.5 * (1.0 + np.cos(np.pi * phase))

    # A zero-phase, second-order Butterworth magnitude response.  This is only
    # appreciable below roughly 50 Hz; it removes diaphragm drift without
    # changing the speech bands used for consonant recognition.
    highpass = np.ones_like(frequencies)
    positive = frequencies > 0.0
    ratio = config.subsonic_highpass_hz / np.maximum(frequencies[positive], 1e-9)
    highpass[positive] = 1.0 / np.sqrt(1.0 + ratio**4)
    highpass[0] = 0.0
    return weight, highpass


def _region_count(envelope: np.ndarray, threshold_db: float = 1.0) -> int:
    active = np.asarray(envelope) >= threshold_db
    if not np.any(active):
        return 0
    return int(active[0]) + int(np.count_nonzero(active[1:] & ~active[:-1]))


def _band_power(audio: np.ndarray, low_hz: float, high_hz: float) -> float:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if not x.size:
        return 1e-20
    fft_size = 2048
    hop = 1024
    if x.size < fft_size:
        frames = np.pad(x, (0, fft_size - x.size)).reshape(1, -1)
    else:
        starts = np.arange(0, x.size - fft_size + 1, hop)
        frames = np.stack([x[start : start + fft_size] for start in starts])
    spectrum = np.abs(np.fft.rfft(frames * np.hanning(fft_size), axis=1)) ** 2
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / SAMPLE_RATE)
    mask = (frequencies >= low_hz) & (frequencies < high_hz)
    return float(np.mean(spectrum[:, mask])) + 1e-20


def process_deplosive(
    audio: np.ndarray,
    config: DePlosiveConfig = DePlosiveConfig(),
) -> tuple[np.ndarray, ProcessStats, np.ndarray]:
    """Render de-plosive audio and return samples, metrics, and dB envelope."""

    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if not x.size:
        raise ValueError("cannot process empty audio")
    padded, starts, spectra, pad = _analysis_frames(x, config)
    envelope = _detection_envelope(x, spectra, config)
    weight, highpass = _frequency_weight(config)
    gain = 10.0 ** (-(envelope[:, None] * weight[None, :]) / 20.0)
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
    rendered = output[pad : pad + x.size]
    rendered = np.clip(rendered, -1.0, 32767.0 / 32768.0).astype(np.float32)

    active = envelope >= 1.0
    processed_duration_ms = float(np.count_nonzero(active) * config.hop_size / 16.0)
    clip_level = 32767.0 / 32768.0
    stats = ProcessStats(
        detected_regions=_region_count(envelope),
        processed_duration_ms=processed_duration_ms,
        processed_audio_pct=float(
            min(100.0, processed_duration_ms / max(x.size / 16.0, 1e-9) * 100.0)
        ),
        maximum_attenuation_db=float(np.max(envelope)),
        mean_active_attenuation_db=(
            float(np.mean(envelope[active])) if np.any(active) else 0.0
        ),
        sub_50hz_change_db=_db(_band_power(rendered, 20.0, 50.0))
        - _db(_band_power(x, 20.0, 50.0)),
        low_20_250hz_change_db=_db(_band_power(rendered, 20.0, 250.0))
        - _db(_band_power(x, 20.0, 250.0)),
        speech_300_3000hz_change_db=_db(_band_power(rendered, 300.0, 3000.0))
        - _db(_band_power(x, 300.0, 3000.0)),
        peak_before_dbfs=_dbfs(np.max(np.abs(x))),
        peak_after_dbfs=_dbfs(np.max(np.abs(rendered))),
        clipped_before_pct=float(np.mean(np.abs(x) >= clip_level) * 100.0),
        clipped_after_pct=float(np.mean(np.abs(rendered) >= clip_level) * 100.0),
    )
    return rendered, stats, envelope.astype(np.float32)


def _write_wav(path: Path, audio: np.ndarray) -> None:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    pcm = np.rint(
        np.clip(x, -1.0, 32767.0 / 32768.0) * 32768.0
    ).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(pcm.tobytes())


def _list_recent_cases(root: Path, limit: int) -> list[SessionCase]:
    cases: list[SessionCase] = []
    records = sorted(
        root.glob("**/interactions/*/record.json"),
        key=lambda path: path.parent.name,
        reverse=True,
    )
    for record_path in records:
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        audio_data = record.get("audio") or {}
        audio_path = record_path.parent / str(audio_data.get("file") or "audio.wav")
        if not audio_path.is_file():
            continue
        asr = record.get("asr") or {}
        cases.append(
            SessionCase(
                interaction_id=record_path.parent.name,
                path=audio_path,
                prior_text=str(asr.get("final_text") or "").strip(),
                record_path=record_path,
            )
        )
        if len(cases) >= max(0, limit):
            break
    return cases


def _normalize_text(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u3400-\u9fff]+", "", str(text)).lower()


def _text_similarity(left: str, right: str) -> float | None:
    a, b = _normalize_text(left), _normalize_text(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return float(SequenceMatcher(None, a, b).ratio())


def _asr_assessment(raw_text: str, processed_text: str) -> str:
    raw = _normalize_text(raw_text)
    processed = _normalize_text(processed_text)
    if raw == processed:
        return "unchanged"
    if not raw and processed:
        return "potential_improvement_nonempty"
    if raw and not processed:
        return "potential_regression_empty"
    return "changed_needs_reference"


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _write_markdown(output: Path, report: dict) -> None:
    summary = report["summary"]
    lines = [
        "# De-plosive A/B test",
        "",
        "同一条已分段音频分别以原始版和动态 De-plosive 版重新送入同一 ASR。",
        "历史 ASR 文本不是人工标准答案，因此 `potential` 只表示候选改善，最终需结合试听确认。",
        "",
        "## Summary",
        "",
        f"- Sessions: {summary['sessions']}",
        f"- Detected regions: {summary['detected_regions']}",
        f"- Mean 20–250 Hz change: {summary['mean_low_20_250hz_change_db']:+.2f} dB",
        f"- Mean 300–3000 Hz change: {summary['mean_speech_300_3000hz_change_db']:+.3f} dB",
        f"- ASR unchanged: {summary['asr_unchanged']}",
        f"- ASR potential improvements: {summary['asr_potential_improvements']}",
        f"- ASR potential regressions: {summary['asr_potential_regressions']}",
        f"- ASR changed, needs reference: {summary['asr_changed_needs_reference']}",
        f"- Historical-text proxy better/same/worse: "
        f"{summary['historical_proxy_better']}/"
        f"{summary['historical_proxy_same']}/"
        f"{summary['historical_proxy_worse']}",
        "",
        "## Listening order",
        "",
        "每条目录中的 `ab_original_then_deplosive.wav` 都是原始版在前、处理版在后。",
        "`top_candidates_original_then_deplosive.wav` 按低频衰减幅度排列候选样本。",
        "",
        "## Per-session results",
        "",
        "| Session | Prior | Raw ASR | De-plosive ASR | Assessment | Regions | Low Δ | Speech Δ |",
        "|---|---|---|---|---|---:|---:|---:|",
    ]
    for item in report["sessions"]:
        stats = item["processing"]
        asr = item["asr"]
        def cell(value: object) -> str:
            return str(value or "∅").replace("|", "\\|").replace("\n", " ")
        lines.append(
            "| "
            + " | ".join(
                (
                    cell(item["interaction_id"]),
                    cell(item["prior_text"]),
                    cell(asr.get("raw_text")),
                    cell(asr.get("processed_text")),
                    cell(asr.get("assessment")),
                    str(stats["detected_regions"]),
                    f"{stats['low_20_250hz_change_db']:+.2f} dB",
                    f"{stats['speech_300_3000hz_change_db']:+.3f} dB",
                )
            )
            + " |"
        )
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_top_montage(output: Path, sessions: list[dict], limit: int = 8) -> None:
    ranked = sorted(
        sessions,
        key=lambda item: (
            item["processing"]["low_20_250hz_change_db"],
            -item["processing"]["maximum_attenuation_db"],
        ),
    )[:limit]
    silence_between_variants = np.zeros(int(0.4 * SAMPLE_RATE), dtype=np.float32)
    silence_between_sessions = np.zeros(int(1.0 * SAMPLE_RATE), dtype=np.float32)
    chunks: list[np.ndarray] = []
    order: list[str] = []
    for item in ranked:
        case_dir = output / item["output_directory"]
        raw = read_wav(case_dir / "original.wav")
        processed = read_wav(case_dir / "deplosive.wav")
        chunks.extend((raw, silence_between_variants, processed, silence_between_sessions))
        order.append(item["interaction_id"])
    if chunks:
        _write_wav(output / "top_candidates_original_then_deplosive.wav", np.concatenate(chunks))
        (output / "top_candidates_order.txt").write_text(
            "\n".join(f"{index + 1}. {name}" for index, name in enumerate(order)) + "\n",
            encoding="utf-8",
        )


def _load_sensevoice_runner():
    """Load the app's cached local SenseVoice model once for an offline run."""

    from proximic_ring.asr.backends.streaming_sensevoice import StreamingSenseVoiceASR

    model = (
        PROJECT_ROOT
        / ".cache/modelscope/models/iic--SenseVoiceSmall/snapshots/master"
    )
    if not (model / "model.pt").is_file():
        raise FileNotFoundError(
            f"SenseVoice cache missing: {model}; launch the app once with SenseVoice"
        )
    backend = StreamingSenseVoiceASR(
        model=str(model),
        device="cpu",
        language="zh",
        repo_path=PROJECT_ROOT / "third_party/streaming-sensevoice",
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interactions-root", type=Path, default=PROJECT_ROOT / "dataset"
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-seconds", type=float, default=15.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "experiments"
        / f"deplosive_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
    )
    parser.add_argument(
        "--analyse-only", action="store_true", help="render WAVs without calling cloud ASR"
    )
    parser.add_argument(
        "--backend",
        choices=("doubao", "sensevoice"),
        default="doubao",
        help="ASR used for the paired re-run (default: doubao)",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=1.0,
        help="ASR send pacing; 1 matches live capture",
    )
    parser.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases = _list_recent_cases(args.interactions_root.expanduser().resolve(), args.limit)
    if not cases:
        raise SystemExit("没有找到带 audio.wav 的 interaction sessions")

    config = DePlosiveConfig()
    backend_handle = None
    if args.analyse_only:
        run_asr = None
    elif args.backend == "doubao":
        api_key = resolve_api_key(args.api_key)

        def run_asr(audio: np.ndarray) -> str:
            return transcribe(
                audio,
                api_key=api_key,
                pace=max(0.0, args.pace),
                debug=args.debug,
            )
    else:
        backend_handle, run_asr = _load_sensevoice_runner()
    sessions: list[dict] = []
    errors = 0
    print(
        f"选择最近 {len(cases)} 条 session；"
        f"ASR={'不调用' if args.analyse_only else args.backend + ' 原始/处理各重跑一次'}"
    )
    for index, case in enumerate(cases, 1):
        audio = read_wav(case.path)
        if args.max_seconds > 0:
            audio = audio[: int(round(args.max_seconds * SAMPLE_RATE))]
        processed, stats, _envelope = process_deplosive(audio, config)
        case_dir_name = f"{index:02d}_{case.interaction_id}"
        case_dir = output / case_dir_name
        case_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case.path, case_dir / "original.wav")
        _write_wav(case_dir / "deplosive.wav", processed)
        pause = np.zeros(int(0.4 * SAMPLE_RATE), dtype=np.float32)
        _write_wav(
            case_dir / "ab_original_then_deplosive.wav",
            np.concatenate((audio, pause, processed)),
        )

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

        assessment = (
            "not_run"
            if args.analyse_only or raw_error or processed_error
            else _asr_assessment(raw_text or "", processed_text or "")
        )
        session = {
            "interaction_id": case.interaction_id,
            "source_audio": str(case.path.resolve()),
            "source_record": str(case.record_path.resolve()),
            "prior_text": case.prior_text,
            "output_directory": case_dir_name,
            "processing": asdict(stats),
            "asr": {
                "raw_text": raw_text,
                "processed_text": processed_text,
                "raw_error": raw_error,
                "processed_error": processed_error,
                "assessment": assessment,
                "raw_similarity_to_prior": (
                    _text_similarity(raw_text or "", case.prior_text)
                    if case.prior_text
                    else None
                ),
                "processed_similarity_to_prior": (
                    _text_similarity(processed_text or "", case.prior_text)
                    if case.prior_text
                    else None
                ),
            },
        }
        sessions.append(session)
        print(
            f"[{index:02d}/{len(cases):02d}] {case.interaction_id} "
            f"regions={stats.detected_regions} low={stats.low_20_250hz_change_db:+.2f}dB "
            f"speech={stats.speech_300_3000hz_change_db:+.3f}dB "
            f"ASR={assessment} raw={raw_text!r} processed={processed_text!r}"
        )

        # Keep a useful partial report even if a later network call is stopped.
        _write_json(
            output / "partial_sessions.json",
            {"config": asdict(config), "sessions": sessions},
        )

    assessments = [item["asr"]["assessment"] for item in sessions]
    proxy_pairs = [
        (
            item["asr"]["raw_similarity_to_prior"],
            item["asr"]["processed_similarity_to_prior"],
        )
        for item in sessions
        if item["asr"]["raw_similarity_to_prior"] is not None
        and item["asr"]["processed_similarity_to_prior"] is not None
    ]
    proxy_better = sum(after > before + 1e-9 for before, after in proxy_pairs)
    proxy_worse = sum(after < before - 1e-9 for before, after in proxy_pairs)
    summary = {
        "sessions": len(sessions),
        "detected_regions": int(
            sum(item["processing"]["detected_regions"] for item in sessions)
        ),
        "mean_low_20_250hz_change_db": float(
            np.mean(
                [item["processing"]["low_20_250hz_change_db"] for item in sessions]
            )
        ),
        "mean_speech_300_3000hz_change_db": float(
            np.mean(
                [
                    item["processing"]["speech_300_3000hz_change_db"]
                    for item in sessions
                ]
            )
        ),
        "asr_unchanged": assessments.count("unchanged"),
        "asr_potential_improvements": assessments.count(
            "potential_improvement_nonempty"
        ),
        "asr_potential_regressions": assessments.count("potential_regression_empty"),
        "asr_changed_needs_reference": assessments.count("changed_needs_reference"),
        "asr_not_run_or_error": assessments.count("not_run"),
        "historical_proxy_sessions": len(proxy_pairs),
        "historical_proxy_better": proxy_better,
        "historical_proxy_same": len(proxy_pairs) - proxy_better - proxy_worse,
        "historical_proxy_worse": proxy_worse,
        "mean_raw_similarity_to_historical_text": (
            float(np.mean([before for before, _after in proxy_pairs]))
            if proxy_pairs
            else None
        ),
        "mean_processed_similarity_to_historical_text": (
            float(np.mean([after for _before, after in proxy_pairs]))
            if proxy_pairs
            else None
        ),
    }
    report = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "algorithm": "adaptive_stft_deplosive_mild_v1",
        "asr_backend": None if args.analyse_only else args.backend,
        "config": asdict(config),
        "summary": summary,
        "sessions": sessions,
    }
    _write_json(output / "report.json", report)
    _write_markdown(output, report)
    _write_top_montage(output, sessions)
    print(f"\n完成：{output}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if backend_handle is not None:
        abort = getattr(backend_handle, "abort", None)
        if callable(abort):
            abort()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
