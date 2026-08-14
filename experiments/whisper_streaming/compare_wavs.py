"""Simulate streaming Whisper on the same low-volume WAV comparison set."""

from __future__ import annotations

import argparse
import logging
import time
import wave
from pathlib import Path

import numpy as np


def read_mono_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != 16_000:
            raise ValueError(f"{path.name}: expected 16000 Hz, got {wav.getframerate()} Hz")
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected mono 16-bit PCM WAV")
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def decode_prefix(model, audio: np.ndarray, *, language: str) -> str:
    # VAD is intentionally disabled: this experiment must expose Whisper to
    # quiet/whispered audio instead of filtering it before ASR.
    segments, _ = model.transcribe(
        audio,
        language=language,
        beam_size=1,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=False,
        without_timestamps=True,
        vad_filter=False,
    )
    return "".join(segment.text for segment in segments).strip()


def transcribe(
    model,
    path: Path,
    *,
    chunk_s: float,
    language: str,
    realtime: bool,
) -> None:
    audio = read_mono_16k(path)
    duration_s = len(audio) / 16_000.0
    boundaries = np.arange(chunk_s, duration_s + chunk_s, chunk_s)
    if not boundaries.size:
        boundaries = np.asarray([duration_s])

    last_text = ""
    wall_start = time.perf_counter()
    print(f"\n========== {path.name} ==========", flush=True)

    for index, boundary in enumerate(boundaries):
        audio_end_s = min(float(boundary), duration_s)
        end_sample = min(len(audio), int(round(audio_end_s * 16_000)))
        prefix = audio[:end_sample]
        is_final = index == len(boundaries) - 1

        if realtime:
            wait_s = wall_start + audio_end_s - time.perf_counter()
            if wait_s > 0:
                time.sleep(wait_s)

        # Unified metric: cumulative slice ready -> changed text print.
        chunk_ready_s = time.perf_counter()
        text = decode_prefix(model, prefix, language=language)
        if text and text != last_text:
            latency_ms = int(round((time.perf_counter() - chunk_ready_s) * 1000))
            label = "FINAL" if is_final else "PARTIAL"
            print(
                f"{label} audio={audio_end_s:.2f}s latency={latency_ms}ms: {text}",
                flush=True,
            )
            last_text = text
        elif is_final:
            latency_ms = int(round((time.perf_counter() - chunk_ready_s) * 1000))
            final_text = text or last_text or "[没有识别到文本]"
            print(
                f"FINAL audio={audio_end_s:.2f}s latency={latency_ms}ms: {final_text}",
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test Faster-Whisper with cumulative simulated streaming"
    )
    parser.add_argument("path", type=Path, help="16 kHz mono PCM WAV or directory")
    parser.add_argument(
        "--model",
        default=r"E:\AIModels\Whisper\whisper-large-v3-ct2-int8_float16-v2",
        help="Faster-Whisper model name or local CTranslate2 model directory",
    )
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--chunk-ms", type=int, default=1000)
    parser.add_argument(
        "--download-root",
        type=Path,
        default=Path(r"E:\AIModels\Whisper"),
    )
    parser.add_argument(
        "--no-realtime",
        action="store_true",
        help="process immediately instead of waiting for each audio boundary",
    )
    args = parser.parse_args()

    if args.chunk_ms <= 0:
        parser.error("--chunk-ms must be positive")
    paths = sorted(args.path.glob("*.wav")) if args.path.is_dir() else [args.path]
    if not paths or any(not path.is_file() for path in paths):
        parser.error(f"no WAV files found: {args.path}")

    logging.getLogger().setLevel(logging.ERROR)
    from faster_whisper import WhisperModel

    args.download_root.mkdir(parents=True, exist_ok=True)
    print(
        f"Loading Whisper {args.model} on {args.device} ({args.compute_type}) ...",
        flush=True,
    )
    load_started = time.perf_counter()
    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(args.download_root),
    )
    print(f"Model ready ({time.perf_counter() - load_started:.1f}s)", flush=True)

    for path in paths:
        transcribe(
            model,
            path.resolve(),
            chunk_s=args.chunk_ms / 1000.0,
            language=args.language,
            realtime=not args.no_realtime,
        )


if __name__ == "__main__":
    main()
