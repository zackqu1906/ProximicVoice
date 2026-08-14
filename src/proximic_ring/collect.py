from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .audio.ring import RingAudioSource
from .dataset import (
    DATASET_SAMPLE_RATE,
    LABEL_TO_TARGET,
    append_metadata_row,
    audio_peak_rms,
    sanitize_token,
    write_pcm16_wav,
)


@dataclass(frozen=True)
class CollectionConfig:
    dataset_root: Path
    label: str
    distance_cm: float
    speaker_id: str
    speech_style: str
    angle_deg: float = 0.0
    count: int = 6
    duration_s: float = 8.0
    countdown_s: int = 3
    gap_s: float = 2.0
    manual_ready: bool = True
    near_max_cm: float = 5.0
    far_min_cm: float = 20.0
    allow_ambiguous_distance: bool = False
    notes: str = ""

    def validate(self) -> "CollectionConfig":
        label = self.label.lower()
        if label not in LABEL_TO_TARGET:
            raise ValueError("label must be near, far, or artifact")
        if label in {"near", "far"} and self.distance_cm <= 0:
            raise ValueError("distance_cm must be > 0 for near/far speech")
        if label == "artifact" and self.distance_cm < 0:
            raise ValueError("distance_cm cannot be negative")
        if self.count <= 0:
            raise ValueError("count must be > 0")
        if self.duration_s < 3.0:
            raise ValueError(
                "duration_s must be >= 3.0 for the long-take collector; "
                "8 s is recommended"
            )
        if self.countdown_s < 0 or self.gap_s < 0:
            raise ValueError("countdown_s and gap_s cannot be negative")
        if not self.speaker_id.strip():
            raise ValueError("speaker_id cannot be empty")
        if not self.speech_style.strip():
            raise ValueError("speech_style cannot be empty")
        if not self.allow_ambiguous_distance:
            if label == "near" and self.distance_cm > self.near_max_cm:
                raise ValueError(
                    f"near samples should be <= {self.near_max_cm:g} cm; got {self.distance_cm:g} cm. "
                    "Use a smaller distance or --allow-ambiguous-distance."
                )
            if label == "far" and self.distance_cm < self.far_min_cm:
                raise ValueError(
                    f"far samples should be >= {self.far_min_cm:g} cm; got {self.distance_cm:g} cm. "
                    "Use a larger distance or --allow-ambiguous-distance."
                )
        return self


def _read_exact(source: RingAudioSource, frames: int) -> np.ndarray:
    chunks: list[np.ndarray] = []
    remaining = int(frames)
    while remaining > 0:
        block = source.read(remaining)
        if block is None:
            raise RuntimeError("Ring audio stream ended during data collection")
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        if block.size == 0:
            continue
        chunks.append(block)
        remaining -= int(block.size)
    out = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    if out.size != frames:
        raise RuntimeError(f"Expected {frames} samples, collected {out.size}")
    return np.ascontiguousarray(out)


def _discard_for(source: RingAudioSource, seconds: float) -> None:
    frames = int(round(seconds * DATASET_SAMPLE_RATE))
    while frames > 0:
        take = min(frames, 1600)  # drain in <=100 ms chunks
        _read_exact(source, take)
        frames -= take


def _countdown_while_draining(source: RingAudioSource, seconds: int) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"  {remaining}...")
        _discard_for(source, 1.0)


def _wait_for_ready_while_draining(source: RingAudioSource) -> str:
    """Wait for Enter without allowing the live Ring PCM queue to fill up."""

    answer: dict[str, str] = {"value": ""}
    done = threading.Event()

    def _read_input() -> None:
        try:
            answer["value"] = input(
                "  Press Enter when ready for this take, or type q + Enter to stop: "
            ).strip().lower()
        except EOFError:
            answer["value"] = ""
        finally:
            done.set()

    threading.Thread(target=_read_input, name="collector-input", daemon=True).start()
    while not done.is_set():
        _discard_for(source, 0.10)
    return answer["value"]


def _load_phrases(path: Path | None) -> list[str]:
    if path is None:
        return []
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def _expected_windows(duration_s: float, *, margin_s: float = 0.5, hop_s: float = 0.5) -> int:
    usable = duration_s - 2.0 * margin_s
    if usable < 1.0:
        return 1
    return int(np.floor((usable - 1.0) / hop_s)) + 1


def collect_ring_dataset(
    *,
    cfg: CollectionConfig,
    name_keyword: str = "Ringo",
    selector: str | None = None,
    timeout_s: float = 8.0,
    encoding: str = "pcm",
    phrases_path: Path | None = None,
) -> int:
    cfg = cfg.validate()
    root = cfg.dataset_root
    raw_dir = root / "raw" / cfg.label.lower()
    metadata_path = root / "metadata.csv"
    sdk_dir = root / "sdk_sessions"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sdk_dir.mkdir(parents=True, exist_ok=True)

    phrases = _load_phrases(phrases_path)
    target = LABEL_TO_TARGET[cfg.label.lower()]
    polarity = "positive" if cfg.label.lower() == "near" else "negative"
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    windows_per_take = _expected_windows(cfg.duration_s)

    print("\nCollection configuration")
    print(f"  class          : {cfg.label.lower()} ({polarity}, target={target})")
    distance_text = f"{cfg.distance_cm:g} cm" if cfg.distance_cm > 0 else "n/a"
    print(f"  distance       : {distance_text}")
    print(f"  subject        : {cfg.speaker_id}")
    print(f"  subtype/style  : {cfg.speech_style}")
    print(f"  angle          : {cfg.angle_deg:g} deg")
    print(f"  long takes     : {cfg.count}")
    print(f"  take duration  : {cfg.duration_s:g} s")
    print(f"  est. windows   : ~{windows_per_take} x 1-s windows/take at 0.5-s hop")
    print(f"  dataset        : {root}")
    print("\nProtocol")
    print("  - Keep the Ring position/distance fixed for this run.")
    print("  - Speak naturally and continuously during RECORD; short pauses are okay.")
    print("  - Do not deliberately blow into the microphone unless collecting that condition.")
    print("  - Each WAV is saved as one long take; training creates many 1-s windows later.")
    input("Press Enter once to connect the Ring and start this collection session... ")

    source = RingAudioSource(
        name_keyword=name_keyword,
        selector=selector,
        timeout_s=timeout_s,
        encoding=encoding,
        data_root=sdk_dir,
        queue_blocks=4096,
    )

    saved = 0
    try:
        source.open()
        print("Ring connected. Warming up audio stream...")
        _discard_for(source, 0.5)

        for take_idx in range(1, cfg.count + 1):
            phrase = phrases[(take_idx - 1) % len(phrases)] if phrases else ""
            print(
                f"\n[{take_idx:02d}/{cfg.count:02d}] "
                f"{cfg.label.upper()} @ {cfg.distance_cm:g} cm | {cfg.speech_style}"
            )
            if phrase:
                print(f"  PROMPT: {phrase}")
                print("  You may continue naturally if the prompt ends before the take stops.")
            else:
                print("  PROMPT: free speech for the full take")

            if cfg.manual_ready:
                answer = _wait_for_ready_while_draining(source)
                if answer in {"q", "quit", "stop", "exit"}:
                    print("Collection stopped before the next take.")
                    break
            elif cfg.gap_s > 0:
                print(f"  Rest/preparation: {cfg.gap_s:g} s")
                _discard_for(source, cfg.gap_s)

            _countdown_while_draining(source, cfg.countdown_s)
            print(f"  >>> RECORD {cfg.duration_s:g} s <<<")
            frames = int(round(cfg.duration_s * DATASET_SAMPLE_RATE))
            audio = _read_exact(source, frames)
            print("  <<< STOP >>>")

            peak, rms = audio_peak_rms(audio)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            filename = (
                f"class-{cfg.label.lower()}__target-{target}__"
                f"speaker-{sanitize_token(cfg.speaker_id)}__"
                f"dist-{int(round(cfg.distance_cm)):03d}cm__"
                f"style-{sanitize_token(cfg.speech_style)}__"
                f"angle-{int(round(cfg.angle_deg)):03d}deg__"
                f"session-{session_id}__take-{take_idx:03d}__{stamp}.wav"
            )
            wav_path = raw_dir / filename
            write_pcm16_wav(wav_path, audio, DATASET_SAMPLE_RATE)

            rel_path = wav_path.relative_to(root).as_posix()
            append_metadata_row(
                metadata_path,
                {
                    "path": rel_path,
                    "target": target,
                    "class_name": cfg.label.lower(),
                    "polarity": polarity,
                    "distance_cm": f"{cfg.distance_cm:g}",
                    "speaker_id": cfg.speaker_id,
                    "speech_style": cfg.speech_style,
                    "angle_deg": f"{cfg.angle_deg:g}",
                    "session_id": session_id,
                    "sample_index": take_idx,
                    "duration_s": f"{cfg.duration_s:.4f}",
                    "sample_rate": DATASET_SAMPLE_RATE,
                    "channels": 1,
                    "encoding": "PCM16LE",
                    "timestamp_utc": stamp,
                    "peak": f"{peak:.8f}",
                    "rms": f"{rms:.8f}",
                    "phrase": phrase,
                    "notes": cfg.notes,
                },
            )
            saved += 1
            print(f"  saved: {rel_path}")
            print(f"  level: peak={peak:.5f} rms={rms:.5f}")
            print(f"  training yield: ~{windows_per_take} base windows before jitter")
            if peak < 0.003:
                print("  WARNING: very low peak; listen to this WAV before training.")

            if cfg.manual_ready and cfg.gap_s > 0 and take_idx < cfg.count:
                print(f"  Minimum rest: {cfg.gap_s:g} s")
                _discard_for(source, cfg.gap_s)
    except KeyboardInterrupt:
        print("\nCollection stopped by user.")
    finally:
        source.close()

    print(f"\nSaved {saved} long labeled takes.")
    print(f"Estimated base windows at default training settings: {saved * windows_per_take}")
    print(f"Metadata: {metadata_path}")
    if source.capture_path is not None:
        print(f"SDK full-session WAV: {source.capture_path}")
    return 0
