"""Run the official Fun-ASR-Nano cumulative streaming pattern on WAV files."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import numpy as np
import torch
import soundfile as sf

from model import FunASRNano
from tools.utils import load_audio


def stable_partial(tokenizer, text: str, rollback_tokens: int) -> str:
    """Roll back the unstable tail exactly as the upstream streaming demo does."""
    if rollback_tokens <= 0 or not text:
        return text
    token_ids = tokenizer.encode(text)
    if len(token_ids) <= rollback_tokens:
        return ""
    return tokenizer.decode(token_ids[:-rollback_tokens]).replace("�", "").strip()


def transcribe_streaming(
    model: FunASRNano,
    model_kwargs: dict,
    tokenizer,
    path: Path,
    *,
    chunk_s: float,
    rollback_tokens: int,
    realtime: bool,
) -> None:
    duration_s = float(sf.info(path).duration)
    boundaries = np.arange(chunk_s, duration_s + chunk_s, chunk_s)
    if boundaries.size == 0:
        boundaries = np.asarray([duration_s])

    previous_context = ""
    last_printed = ""
    wall_start = time.perf_counter()
    print(f"\n========== {path.name} ==========", flush=True)

    for index, boundary in enumerate(boundaries):
        audio_end_s = min(float(boundary), duration_s)
        is_last = index == len(boundaries) - 1

        # In real capture this prefix would only be available at audio_end_s.
        if realtime:
            wait_s = wall_start + audio_end_s - time.perf_counter()
            if wait_s > 0:
                time.sleep(wait_s)

        audio, _ = load_audio(
            str(path),
            16_000,
            duration=round(audio_end_s, 3),
        )
        chunk_ready_s = time.perf_counter()
        result = model.inference(
            [audio.clone().detach()],
            prev_text=previous_context,
            **model_kwargs,
        )[0][0]
        raw_text = str(result.get("text", "") or "").strip()

        if is_last:
            display_text = raw_text
            previous_context = raw_text
        else:
            previous_context = stable_partial(tokenizer, raw_text, rollback_tokens)
            display_text = previous_context

        if display_text and display_text != last_printed:
            label = "FINAL" if is_last else "PARTIAL"
            latency_ms = int(round((time.perf_counter() - chunk_ready_s) * 1000))
            print(
                f"{label} audio={audio_end_s:.2f}s latency={latency_ms}ms: {display_text}",
                flush=True,
            )
            last_printed = display_text

    if not last_printed:
        print("FINAL: [没有识别到文本]", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test local Fun-ASR-Nano with cumulative streaming input"
    )
    parser.add_argument("path", type=Path, help="WAV file or directory containing WAVs")
    parser.add_argument(
        "--model",
        type=Path,
        default=Path(__file__).parent / "pretrained_models" / "Fun-ASR-Nano-2512",
    )
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--chunk-ms", type=int, default=720)
    parser.add_argument("--rollback-tokens", type=int, default=5)
    parser.add_argument(
        "--no-realtime",
        action="store_true",
        help="do not wait for each audio boundary; useful for fast batch checks",
    )
    args = parser.parse_args()

    if args.chunk_ms <= 0:
        parser.error("--chunk-ms must be positive")
    paths = sorted(args.path.glob("*.wav")) if args.path.is_dir() else [args.path]
    if not paths:
        parser.error(f"no WAV files found: {args.path}")
    if not args.model.is_dir():
        parser.error(f"model directory not found: {args.model}")

    logging.getLogger().setLevel(logging.ERROR)
    print(f"Loading Fun-ASR-Nano-2512 on {args.device} ...", flush=True)
    load_started = time.perf_counter()
    model, model_kwargs = FunASRNano.from_pretrained(
        model=str(args.model.resolve()),
        device=args.device,
    )
    model.eval()
    tokenizer = model_kwargs.get("tokenizer")
    if tokenizer is None:
        raise RuntimeError("Fun-ASR model did not provide a tokenizer")
    print(f"Model ready ({time.perf_counter() - load_started:.1f}s)", flush=True)

    with torch.inference_mode():
        for path in paths:
            transcribe_streaming(
                model,
                model_kwargs,
                tokenizer,
                path.resolve(),
                chunk_s=args.chunk_ms / 1000.0,
                rollback_tokens=args.rollback_tokens,
                realtime=not args.no_realtime,
            )


if __name__ == "__main__":
    main()
