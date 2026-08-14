"""Compare WAV files with the native streaming Paraformer Chinese model."""

from __future__ import annotations

import argparse
import logging
import os
import time
import wave
from pathlib import Path

import numpy as np


DEFAULT_MODEL = "iic/speech_paraformer_asr_nat-zh-cn-16k-common-vocab8404-online"


def resolve_model(model: str) -> str:
    """Use an already downloaded ModelScope snapshot without a network probe."""
    explicit = Path(model).expanduser()
    if explicit.is_dir():
        return str(explicit.resolve())
    cache_root = Path(
        os.environ.get("MODELSCOPE_CACHE", str(Path.home() / ".cache" / "modelscope"))
    )
    cached = cache_root / "models" / model.replace("/", "--") / "snapshots" / "master"
    return str(cached.resolve()) if (cached / "config.yaml").is_file() else model


def read_mono_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != 16_000:
            raise ValueError(f"{path.name}: expected 16000 Hz, got {wav.getframerate()} Hz")
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected mono 16-bit PCM WAV")
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def transcribe(
    model,
    path: Path,
    *,
    chunk_ms: int,
    realtime: bool,
) -> None:
    audio = read_mono_16k(path)
    chunk_samples = 16_000 * chunk_ms // 1000
    # FunASR's chunk units are 60 ms.  The third value supplies the documented
    # half-chunk right context: [0, 8, 4] for the 480 ms configuration.
    chunk_units = chunk_ms // 60
    chunk_size = [0, chunk_units, chunk_units // 2]
    cache: dict = {}
    transcript = ""
    wall_start = time.perf_counter()

    print(f"\n========== {path.name} ==========", flush=True)
    for offset in range(0, len(audio), chunk_samples):
        chunk = audio[offset : offset + chunk_samples]
        is_final = offset + len(chunk) >= len(audio)
        audio_end_s = (offset + len(chunk)) / 16_000.0

        # In live capture this block only becomes available at audio_end_s.
        if realtime:
            wait_s = wall_start + audio_end_s - time.perf_counter()
            if wait_s > 0:
                time.sleep(wait_s)

        # Unified metric: completed audio block -> changed text print.
        chunk_ready_s = time.perf_counter()
        result = model.generate(
            input=chunk,
            cache=cache,
            is_final=is_final,
            disable_pbar=True,
            disable_log=True,
            chunk_size=chunk_size,
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        piece = ""
        if result and isinstance(result[0], dict):
            piece = str(result[0].get("text", "") or "").strip()

        if piece:
            transcript += piece
            label = "FINAL" if is_final else "PARTIAL"
            latency_ms = int(round((time.perf_counter() - chunk_ready_s) * 1000))
            print(
                f"{label} audio={audio_end_s:.2f}s latency={latency_ms}ms: {transcript}",
                flush=True,
            )
        elif is_final:
            latency_ms = int(round((time.perf_counter() - chunk_ready_s) * 1000))
            final_text = transcript or "[没有识别到文本]"
            print(
                f"FINAL audio={audio_end_s:.2f}s latency={latency_ms}ms: {final_text}",
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test native streaming Paraformer on one WAV or a WAV directory"
    )
    parser.add_argument("path", type=Path, help="16 kHz mono PCM WAV or directory")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--chunk-ms", type=int, default=480)
    parser.add_argument(
        "--no-realtime",
        action="store_true",
        help="process immediately instead of waiting for each audio boundary",
    )
    args = parser.parse_args()

    if args.chunk_ms <= 0 or args.chunk_ms % 120:
        parser.error("--chunk-ms must be a positive multiple of 120 (480 is recommended)")
    paths = sorted(args.path.glob("*.wav")) if args.path.is_dir() else [args.path]
    if not paths or any(not path.is_file() for path in paths):
        parser.error(f"no WAV files found: {args.path}")

    logging.getLogger().setLevel(logging.ERROR)
    from funasr import AutoModel

    resolved_model = resolve_model(args.model)
    print(f"Loading Paraformer streaming model on {args.device} ...", flush=True)
    load_started = time.perf_counter()
    model = AutoModel(
        model=resolved_model,
        device=args.device,
        disable_update=True,
        disable_pbar=True,
        disable_log=True,
    )
    print(f"Model ready ({time.perf_counter() - load_started:.1f}s)", flush=True)

    for path in paths:
        transcribe(
            model,
            path.resolve(),
            chunk_ms=args.chunk_ms,
            realtime=not args.no_realtime,
        )


if __name__ == "__main__":
    main()
