"""Low-latency persistent history for completed voice utterances."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import shutil
import threading
import uuid
import wave

import numpy as np


SCHEMA_VERSION = 1
_MAX_PENDING_SESSIONS = 16


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class VoiceHistoryStore:
    """Pair final ASR updates with raw audio and persist them off the hot path."""

    def __init__(self, root: str | Path, *, on_saved=None) -> None:
        self.root = Path(root)
        self._on_saved = on_saved
        self._lock = threading.RLock()
        self._disk_lock = threading.Lock()
        self._pending_audio: dict[int, np.ndarray] = {}
        self._pending_final: dict[int, dict] = {}
        self._generation = 0
        self._jobs: queue.Queue[tuple[int, int, np.ndarray, dict] | None] = (
            queue.Queue()
        )
        self._worker = threading.Thread(
            target=self._run,
            name="ProxiMicVoiceHistory",
            daemon=True,
        )
        self._worker.start()

    def record_audio(self, session_id: int, audio_16k) -> None:
        session_id = int(session_id)
        if session_id <= 0:
            return
        audio = np.asarray(audio_16k, dtype=np.float32).reshape(-1).copy()
        with self._lock:
            metadata = self._pending_final.pop(session_id, None)
            if metadata is None:
                self._pending_audio[session_id] = audio
                self._prune_locked()
                return
            self._jobs.put((self._generation, session_id, audio, metadata))

    def record_final(self, update) -> None:
        if not bool(getattr(update, "is_final", False)):
            return
        session_id = int(getattr(update, "session_id", 0))
        if session_id <= 0:
            return
        metadata = {
            "recorded_at": _utc_now(),
            "text": str(getattr(update, "text", "") or ""),
            "backend": str(getattr(update, "backend", "") or ""),
            "model": str(getattr(update, "model", "") or ""),
            "error": str(getattr(update, "error", "") or ""),
            "latency_ms": round(
                float(getattr(update, "latency_s", 0.0) or 0.0) * 1000, 3
            ),
            "audio_duration_ms": round(
                float(getattr(update, "audio_duration_s", 0.0) or 0.0) * 1000,
                3,
            ),
        }
        with self._lock:
            audio = self._pending_audio.pop(session_id, None)
            if audio is None:
                self._pending_final[session_id] = metadata
                self._prune_locked()
                return
            self._jobs.put((self._generation, session_id, audio, metadata))

    def reset_runtime(self) -> None:
        with self._lock:
            self._pending_audio.clear()
            self._pending_final.clear()

    def load_entries(self, limit: int = 100) -> list[dict]:
        rows: list[dict] = []
        if not self.root.is_dir():
            return rows
        paths = sorted(
            self.root.glob("*/metadata.json"),
            key=lambda path: path.parent.name,
            reverse=True,
        )
        for path in paths[: max(0, int(limit))]:
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                rows.append(self._entry(path.parent, metadata))
            except (OSError, ValueError, TypeError):
                continue
        return rows

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._pending_audio.clear()
            self._pending_final.clear()
            generation = self._generation
        with self._disk_lock:
            if self.root.exists():
                shutil.rmtree(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
        # Jobs already dequeued check the generation once more before publish.
        with self._lock:
            self._generation = generation

    def close(self, *, wait: bool = False) -> None:
        self._jobs.put(None)
        if wait and threading.current_thread() is not self._worker:
            self._worker.join(timeout=3.0)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            generation, session_id, audio, metadata = job
            with self._lock:
                if generation != self._generation:
                    continue
            try:
                with self._disk_lock:
                    with self._lock:
                        if generation != self._generation:
                            continue
                    entry = self._write_entry(session_id, audio, metadata)
                with self._lock:
                    publish = generation == self._generation
                if publish and self._on_saved is not None:
                    self._on_saved(entry)
            except BaseException as exc:
                print(f"[voice-history] utterance was not saved: {exc}")

    def _write_entry(
        self, session_id: int, audio: np.ndarray, metadata: dict
    ) -> dict:
        recorded_at = str(metadata.get("recorded_at") or _utc_now())
        stamp = recorded_at.replace("-", "").replace(":", "").replace(".", "")
        stamp = stamp.replace("+0000", "Z")
        entry_id = f"{stamp}_{int(session_id):06d}_{uuid.uuid4().hex[:8]}"
        entry_dir = self.root / entry_id
        entry_dir.mkdir(parents=True, exist_ok=False)
        audio_path = entry_dir / "utterance.wav"
        self._write_wav(audio_path, audio)
        duration_ms = int(round(len(audio) * 1000 / 16_000))
        data = dict(metadata)
        data.update(
            {
                "schema_version": SCHEMA_VERSION,
                "id": entry_id,
                "session_id": int(session_id),
                "audio_file": audio_path.name,
                "audio_duration_ms": duration_ms,
            }
        )
        temporary = entry_dir / "metadata.json.tmp"
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(entry_dir / "metadata.json")
        return self._entry(entry_dir, data)

    @staticmethod
    def _entry(entry_dir: Path, metadata: dict) -> dict:
        text = str(metadata.get("text", "") or "").strip()
        recorded_at = str(metadata.get("recorded_at", "") or "")
        try:
            when = datetime.fromisoformat(recorded_at).astimezone()
            display_time = when.strftime("%m-%d %H:%M:%S")
        except ValueError:
            display_time = recorded_at
        duration_ms = max(0, int(float(metadata.get("audio_duration_ms", 0) or 0)))
        duration_label = f"{duration_ms / 1000:.1f} 秒"
        return {
            "id": str(metadata.get("id", entry_dir.name)),
            "createdAt": recorded_at,
            "displayTime": display_time,
            "text": text or "（未识别出文字）",
            "recognized": bool(text),
            "backend": str(metadata.get("backend", "") or ""),
            "model": str(metadata.get("model", "") or ""),
            "durationSeconds": duration_ms / 1000,
            "durationLabel": duration_label,
            "audioPath": str(entry_dir / str(metadata.get("audio_file", "utterance.wav"))),
            "error": str(metadata.get("error", "") or ""),
        }

    def _prune_locked(self) -> None:
        session_ids = sorted(set(self._pending_audio) | set(self._pending_final))
        for session_id in session_ids[:-_MAX_PENDING_SESSIONS]:
            self._pending_audio.pop(session_id, None)
            self._pending_final.pop(session_id, None)

    @staticmethod
    def _write_wav(path: Path, audio: np.ndarray) -> None:
        pcm = np.rint(np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
        temporary = path.with_suffix(".wav.tmp")
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(pcm.tobytes())
        temporary.replace(path)
