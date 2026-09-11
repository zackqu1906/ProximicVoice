#!/usr/bin/env python3
"""Send WAV files to the app's Doubao ASR and compare their acoustics.

The tool is intentionally independent of the Ring/detector path: it lets us
answer whether a saved waveform is recognisable by the exact cloud backend.
It can also discover the newest interaction records whose final transcript was
empty and compare/replay those byte-for-byte recordings.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import plistlib
import sys
import time
import wave

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = (
    PROJECT_ROOT.parent
    / "ProximicAsr_decoupled"
    / "datasets"
    / "ring_proximity_plus24db_20260826"
    / "raw"
    / "near"
)
DEFAULT_INTERACTIONS = PROJECT_ROOT / "dataset"


@dataclass(frozen=True)
class AudioMetrics:
    duration_s: float
    rms_dbfs: float
    peak_dbfs: float
    frame_p10_dbfs: float
    frame_p50_dbfs: float
    frame_p90_dbfs: float
    quiet_frames_pct: float
    active_frames_pct: float
    clipped_samples_pct: float
    exact_zero_samples_pct: float
    low_band_pct: float
    speech_band_pct: float


@dataclass(frozen=True)
class AudioCase:
    group: str
    path: Path
    prior_text: str = ""
    interaction_id: str = ""


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-10))


def read_wav(path: Path) -> np.ndarray:
    """Read PCM16 mono WAV as float32, resampling to 16 kHz when necessary."""

    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM WAV is supported (got {width * 8}-bit)")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    if rate <= 0:
        raise ValueError(f"{path}: invalid sample rate {rate}")
    if rate != 16_000 and audio.size:
        output_count = int(round(audio.size * 16_000 / rate))
        old_positions = np.arange(audio.size, dtype=np.float64)
        new_positions = np.arange(output_count, dtype=np.float64) * rate / 16_000
        audio = np.interp(new_positions, old_positions, audio).astype(np.float32)
    return audio.reshape(-1)


def acoustic_metrics(audio: np.ndarray, sample_rate: int = 16_000) -> AudioMetrics:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if not x.size:
        raise ValueError("cannot analyse empty audio")
    frame_size = max(1, int(round(0.02 * sample_rate)))
    usable = x[: x.size // frame_size * frame_size]
    if usable.size:
        frames = usable.reshape(-1, frame_size)
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-20)
        frame_db = 20.0 * np.log10(np.maximum(frame_rms, 1e-10))
    else:
        frame_db = np.asarray([_dbfs(np.sqrt(np.mean(x * x)))])

    # Welch-like averaging keeps long recordings cheap and avoids a single
    # start/end transient dominating the spectral comparison.
    fft_size = 2048
    hop = 1024
    if x.size < fft_size:
        padded = np.pad(x, (0, fft_size - x.size))
        chunks = padded.reshape(1, -1)
    else:
        starts = np.arange(0, x.size - fft_size + 1, hop)
        # Bound work for very long raw takes while sampling the whole file.
        if starts.size > 512:
            starts = starts[np.linspace(0, starts.size - 1, 512).astype(int)]
        chunks = np.stack([x[start : start + fft_size] for start in starts])
    spectrum = np.abs(np.fft.rfft(chunks * np.hanning(fft_size), axis=1)) ** 2
    power = spectrum.mean(axis=0)
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
    total = float(power[frequencies >= 20].sum()) + 1e-20
    low = float(power[(frequencies >= 20) & (frequencies < 300)].sum()) / total
    speech = float(power[(frequencies >= 300) & (frequencies < 4000)].sum()) / total

    return AudioMetrics(
        duration_s=x.size / sample_rate,
        rms_dbfs=_dbfs(np.sqrt(np.mean(x * x))),
        peak_dbfs=_dbfs(np.max(np.abs(x))),
        frame_p10_dbfs=float(np.percentile(frame_db, 10)),
        frame_p50_dbfs=float(np.percentile(frame_db, 50)),
        frame_p90_dbfs=float(np.percentile(frame_db, 90)),
        quiet_frames_pct=float(np.mean(frame_db < -50.0) * 100.0),
        active_frames_pct=float(np.mean(frame_db > -40.0) * 100.0),
        clipped_samples_pct=float(np.mean(np.abs(x) >= 32767.0 / 32768.0) * 100.0),
        exact_zero_samples_pct=float(np.mean(x == 0.0) * 100.0),
        low_band_pct=low * 100.0,
        speech_band_pct=speech * 100.0,
    )


def resolve_api_key(explicit: str = "") -> str:
    """Resolve the speech App Key without ever logging it."""

    key = explicit.strip() or os.environ.get("VOLC_ASR_API_KEY", "").strip()
    if key:
        return key
    try:
        from PySide6.QtCore import QSettings

        key = str(QSettings("ProxiMic", "ProxiMic Voice").value(
            "asr/volcengineApiKey", ""
        )).strip()
    except ImportError:
        pass
    if key:
        return key
    if sys.platform == "darwin":
        preference = Path.home() / "Library/Preferences/com.proximic.ProxiMic Voice.plist"
        try:
            with preference.open("rb") as source:
                values = plistlib.load(source)
            key = str(
                values.get("asr.volcengineApiKey")
                or values.get("asr/volcengineApiKey")
                or ""
            ).strip()
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
    if not key:
        raise RuntimeError(
            "找不到豆包语音 App Key：请先在应用设置中填写，或设置 VOLC_ASR_API_KEY"
        )
    return key


def _evenly_spaced(paths: list[Path], limit: int) -> list[Path]:
    if limit == 0:
        return []
    if limit < 0 or len(paths) <= limit:
        return paths
    indexes = np.linspace(0, len(paths) - 1, limit).round().astype(int)
    return [paths[int(index)] for index in indexes]


def dataset_cases(
    root: Path, limit: int, *, excluded: set[Path] | None = None
) -> list[AudioCase]:
    excluded = excluded or set()
    paths = [
        path
        for path in sorted(root.glob("*.wav"))
        if path.resolve() not in excluded
    ]
    return [AudioCase("plus24db-near", path) for path in _evenly_spaced(paths, limit)]


def recent_cases(
    root: Path,
    *,
    empty_limit: int,
    recognized_limit: int,
    excluded: set[Path] | None = None,
) -> list[AudioCase]:
    excluded = excluded or set()
    records: list[tuple[Path, dict]] = []
    for record_path in root.glob("**/interactions/*/record.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        asr = record.get("asr") or {}
        if asr.get("backend") == "volcengine" and bool(asr.get("final_recorded")):
            records.append((record_path, record))
    records.sort(key=lambda item: item[0].parent.name, reverse=True)
    empty: list[AudioCase] = []
    recognized: list[AudioCase] = []
    for record_path, record in records:
        text = str((record.get("asr") or {}).get("final_text") or "").strip()
        audio_name = str((record.get("audio") or {}).get("file") or "audio.wav")
        path = record_path.parent / audio_name
        if not path.is_file() or path.resolve() in excluded:
            continue
        case = AudioCase(
            "recent-recognized" if text else "recent-empty",
            path,
            prior_text=text,
            interaction_id=record_path.parent.name,
        )
        target = recognized if text else empty
        target_limit = recognized_limit if text else empty_limit
        if len(target) < max(0, target_limit):
            target.append(case)
        if len(empty) >= max(0, empty_limit) and len(recognized) >= max(0, recognized_limit):
            break
    return empty + recognized


def excluded_paths(result_files: list[Path]) -> set[Path]:
    """Load audio paths from earlier JSON reports so follow-up runs are new."""

    paths: set[Path] = set()
    for result_file in result_files:
        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法读取历史结果 {result_file}: {exc}") from exc
        if not isinstance(payload, list):
            raise ValueError(f"历史结果必须是 JSON 数组：{result_file}")
        for item in payload:
            if isinstance(item, dict) and item.get("path"):
                paths.add(Path(str(item["path"])).expanduser().resolve())
    return paths


def transcribe(audio: np.ndarray, *, api_key: str, pace: float, debug: bool) -> str:
    from proximic_ring.asr.backends.volcengine import VolcengineStreamingASR

    backend = VolcengineStreamingASR(
        api_key=api_key,
        language="zh",
        debug=debug,
        final_timeout_s=12.0,
    )
    backend.start()
    block_samples = 320  # exactly the same 20 ms block size used by the app
    started = time.perf_counter()
    try:
        for offset in range(0, audio.size, block_samples):
            backend.feed(audio[offset : offset + block_samples])
            if pace > 0:
                target = started + (offset + block_samples) / 16_000 * pace
                delay = target - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
        return backend.finish(audio)
    except BaseException:
        backend.abort()
        raise


def _mean_metrics(items: list[dict]) -> dict[str, float]:
    keys = asdict(items[0]["metrics"]).keys() if items else ()
    return {key: float(np.mean([getattr(item["metrics"], key) for item in items])) for key in keys}


def _print_comparison(results: list[dict]) -> None:
    print("\n声学分组均值（dBFS 越接近 0 越响）：")
    print("group                 n   rms     peak   active% quiet%  clip%  low<300% speech%")
    groups = sorted({item["case"].group for item in results})
    for group in groups:
        items = [item for item in results if item["case"].group == group]
        mean = _mean_metrics(items)
        print(
            f"{group:21s} {len(items):2d} {mean['rms_dbfs']:7.1f} {mean['peak_dbfs']:7.1f} "
            f"{mean['active_frames_pct']:7.1f} {mean['quiet_frames_pct']:6.1f} "
            f"{mean['clipped_samples_pct']:6.2f} {mean['low_band_pct']:9.1f} "
            f"{mean['speech_band_pct']:7.1f}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="additional WAV files")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--dataset-limit", type=int, default=4, help="0 skips the dataset; -1 selects all"
    )
    parser.add_argument("--interactions-root", type=Path, default=DEFAULT_INTERACTIONS)
    parser.add_argument("--recent-empty", type=int, default=6)
    parser.add_argument("--recent-recognized", type=int, default=3)
    parser.add_argument(
        "--replay-recent-empty",
        action="store_true",
        help="also resend recent empty-result WAVs to determine reproducibility",
    )
    parser.add_argument(
        "--analyse-only", action="store_true", help="do not call the cloud ASR"
    )
    parser.add_argument("--max-seconds", type=float, default=15.0)
    parser.add_argument(
        "--gain-db",
        type=float,
        default=0.0,
        help="diagnostic gain applied before submission (for example -6)",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=1.0,
        help="send pacing relative to real time (1 matches the app; 0 is fastest)",
    )
    parser.add_argument("--api-key", default="", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument(
        "--exclude-results",
        action="append",
        default=[],
        type=Path,
        help="exclude WAV paths already present in an earlier JSON report",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    excluded = excluded_paths(args.exclude_results)
    cases = dataset_cases(args.dataset, args.dataset_limit, excluded=excluded)
    cases.extend(recent_cases(
        args.interactions_root,
        empty_limit=args.recent_empty,
        recognized_limit=args.recent_recognized,
        excluded=excluded,
    ))
    cases.extend(AudioCase("explicit", path) for path in args.paths)
    if not cases:
        raise SystemExit("没有找到 WAV 文件或豆包交互记录")
    api_key = "" if args.analyse_only else resolve_api_key(args.api_key)
    results: list[dict] = []
    print(f"找到 {len(cases)} 条音频；豆包密钥：{'不调用' if args.analyse_only else '已加载（不显示）'}")
    for index, case in enumerate(cases, 1):
        audio = read_wav(case.path)
        if args.max_seconds > 0:
            audio = audio[: int(round(args.max_seconds * 16_000))]
        if args.gain_db:
            audio = np.clip(audio * (10.0 ** (args.gain_db / 20.0)), -1.0, 1.0)
        metrics = acoustic_metrics(audio)
        should_transcribe = (
            not args.analyse_only
            and case.group not in {"recent-recognized"}
            and (case.group != "recent-empty" or args.replay_recent_empty)
        )
        text = None
        error = None
        if should_transcribe:
            try:
                text = transcribe(audio, api_key=api_key, pace=max(0.0, args.pace), debug=args.debug)
            except BaseException as exc:
                error = f"{type(exc).__name__}: {exc}"
        results.append({"case": case, "metrics": metrics, "new_text": text, "error": error})
        outcome = "仅分析"
        if should_transcribe:
            outcome = f"→ {text!r}" if error is None else f"→ ERROR {error}"
        prior = f"，原结果={case.prior_text!r}" if case.prior_text else ""
        print(
            f"[{index:02d}/{len(cases):02d}] {case.group} {case.path.name}{prior}\n"
            f"    {metrics.duration_s:.2f}s rms={metrics.rms_dbfs:.1f}dBFS "
            f"peak={metrics.peak_dbfs:.1f}dBFS active={metrics.active_frames_pct:.1f}% "
            f"quiet={metrics.quiet_frames_pct:.1f}% clip={metrics.clipped_samples_pct:.2f}% {outcome}"
        )
    _print_comparison(results)
    if args.json_output:
        payload = [
            {
                "group": item["case"].group,
                "path": str(item["case"].path.resolve()),
                "interaction_id": item["case"].interaction_id,
                "prior_text": item["case"].prior_text,
                "new_text": item["new_text"],
                "error": item["error"],
                "metrics": asdict(item["metrics"]),
            }
            for item in results
        ]
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nJSON 已写入：{args.json_output}")
    return 1 if any(item["error"] for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
