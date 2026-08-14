"""Run Peng streaming-sensevoice on one WAV or a directory of WAV files."""

from __future__ import annotations

import argparse
import time
import wave
from pathlib import Path

import numpy as np

from streaming_sensevoice import StreamingSenseVoice


def _bytes_to_unicode() -> dict[int, str]:
    visible = list(range(ord("!"), ord("~") + 1))
    visible += list(range(ord("¡"), ord("¬") + 1))
    visible += list(range(ord("®"), ord("ÿ") + 1))
    chars = visible[:]
    extra = 0
    for value in range(256):
        if value not in visible:
            visible.append(value)
            chars.append(256 + extra)
            extra += 1
    return dict(zip(visible, map(chr, chars)))


_UNICODE_TO_BYTE = {char: value for value, char in _bytes_to_unicode().items()}


def readable_text(text: str) -> str:
    """Decode byte-level BPE output while leaving ordinary Unicode untouched."""
    if not text or any(char not in _UNICODE_TO_BYTE for char in text):
        return text
    try:
        return bytes(_UNICODE_TO_BYTE[char] for char in text).decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return text


def read_mono_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != 16_000:
            raise ValueError(
                f"{path.name}: expected 16000 Hz, got {wav.getframerate()} Hz"
            )
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"{path.name}: expected mono 16-bit PCM WAV")
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def transcribe(
    model: StreamingSenseVoice,
    path: Path,
    *,
    chunk_ms: int,
    realtime: bool,
) -> None:
    audio = read_mono_16k(path)
    chunk_samples = 16_000 * chunk_ms // 1000
    model.reset()
    last_text = ""
    wall_start = time.perf_counter()

    print(f"\n========== {path.name} ==========", flush=True)
    for offset in range(0, len(audio), chunk_samples):
        chunk = audio[offset : offset + chunk_samples]
        is_last = offset + chunk_samples >= len(audio)
        audio_end_s = min((offset + len(chunk)) / 16_000.0, len(audio) / 16_000.0)
        if realtime:
            wait_s = wall_start + audio_end_s - time.perf_counter()
            if wait_s > 0:
                time.sleep(wait_s)
        chunk_ready_s = time.perf_counter()
        for result in model.streaming_inference(chunk * 32768.0, is_last):
            text = readable_text(str(result.get("text", "") or "").strip())
            if text and text != last_text:
                label = "FINAL" if is_last else "PARTIAL"
                latency_ms = int(round((time.perf_counter() - chunk_ready_s) * 1000))
                print(
                    f"{label} audio={audio_end_s:.2f}s latency={latency_ms}ms: {text}",
                    flush=True,
                )
                last_text = text

    if not last_text:
        print("FINAL: [没有识别到文本]", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare WAV files with Peng streaming-sensevoice"
    )
    parser.add_argument("path", type=Path, help="A WAV file or directory")
    parser.add_argument("--chunk-ms", type=int, default=200)
    parser.add_argument("--realtime", action="store_true", help="sleep between chunks")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument(
        "--model",
        default=str(
            Path.home()
            / ".cache/modelscope/models/iic--SenseVoiceSmall/snapshots/master"
        ),
        help="local SenseVoiceSmall model directory or model hub name",
    )
    args = parser.parse_args()

    if args.chunk_ms <= 0:
        parser.error("--chunk-ms must be positive")
    paths = (
        sorted(args.path.glob("*.wav")) if args.path.is_dir() else [args.path]
    )
    if not paths:
        parser.error(f"no WAV files found: {args.path}")

    model = StreamingSenseVoice(
        model=args.model,
        device=args.device,
        language="zh",
        textnorm=True,
        chunk_size=4,
        padding=8,
        beam_size=1,
        max_history=0,
    )
    for path in paths:
        transcribe(
            model,
            path,
            chunk_ms=args.chunk_ms,
            realtime=args.realtime,
        )


if __name__ == "__main__":
    main()
