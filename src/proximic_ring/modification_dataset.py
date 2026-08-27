"""Persistent Episode/Attempt collection for user-confirmed voice edits."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import uuid
import wave

import numpy as np


SCHEMA_VERSION = 1
PROMPT_VERSION = "edit-race-v1"
_MAX_PENDING_SESSIONS = 8
FEEDBACK_REASON_LABELS = {
    "asr_error": "语音识别错误",
    "llm_error": "大模型理解错误",
    "other": "其他原因",
}
_REASONABLE_FEEDBACK_ACTIONS = {"retry", "cancel"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_value(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class ModificationDatasetCollector:
    """Thread-safe, best-effort persistence beside the product interaction path.

    Audio and ASR callbacks can arrive before the UI has captured the edit
    target. They are temporarily keyed by the ASR session id and moved into an
    Attempt as soon as the final transcript becomes an edit request.
    """

    def __init__(self, root: str | Path, user_id: str) -> None:
        self.root = Path(root)
        self.user_id = str(user_id).strip()
        if not self.user_id:
            raise ValueError("anonymous user_id cannot be empty")
        self.user_root = self.root / self.user_id
        self._lock = threading.RLock()
        self._pending_audio: dict[int, np.ndarray] = {}
        self._pending_asr: dict[int, list[dict]] = {}
        self._active_episode_id: str | None = None
        self._request_attempts: dict[int, tuple[str, str]] = {}
        self._preview_started: dict[tuple[str, str], float] = {}

    def reset_runtime(self) -> None:
        """Drop unbound callbacks and close an interrupted Episode."""
        with self._lock:
            self._pending_audio.clear()
            self._pending_asr.clear()
            if self._active_episode_id is not None:
                self._finalize_episode_locked(
                    self._active_episode_id,
                    status="abandoned",
                    final_text=self._episode_data(self._active_episode_id).get(
                        "final_user_text", ""
                    ),
                    manually_corrected=False,
                )

    def record_audio(self, session_id: int, audio_16k: np.ndarray) -> None:
        audio = np.asarray(audio_16k, dtype=np.float32).reshape(-1).copy()
        with self._lock:
            self._pending_audio[int(session_id)] = audio
            self._prune_pending_locked()

    def record_asr_update(self, update) -> None:
        session_id = int(getattr(update, "session_id", 0))
        if session_id <= 0:
            return
        row = {
            "recorded_at": _utc_now(),
            "kind": "final" if bool(getattr(update, "is_final", False)) else "partial",
            "backend": str(getattr(update, "backend", "") or ""),
            "model": str(getattr(update, "model", "") or ""),
            "text": str(getattr(update, "text", "") or ""),
            "latency_ms": round(float(getattr(update, "latency_s", 0.0)) * 1000, 3),
            "audio_duration_ms": round(
                float(getattr(update, "audio_duration_s", 0.0)) * 1000, 3
            ),
            "error": getattr(update, "error", None),
        }
        with self._lock:
            self._pending_asr.setdefault(session_id, []).append(row)
            self._prune_pending_locked()

    def begin_attempt(
        self,
        *,
        request_id: int,
        session_id: int,
        target_text: str,
        application: str,
        provider: str,
        model: str,
        prompt_version: str = PROMPT_VERSION,
    ) -> tuple[str, str]:
        with self._lock:
            episode_id = self._active_episode_id
            if episode_id is None:
                episode_id = self._new_id("ep")
                self._active_episode_id = episode_id
                self._write_json(
                    self._episode_path(episode_id),
                    {
                        "schema_version": SCHEMA_VERSION,
                        "episode_id": episode_id,
                        "anonymous_user_id": self.user_id,
                        "created_at": _utc_now(),
                        "updated_at": _utc_now(),
                        "attempt_ids": [],
                        "original_target_text": str(target_text),
                        "final_user_text": "",
                        "final_status": "active",
                        "manually_corrected": False,
                    },
                )
            episode = self._episode_data(episode_id)
            attempt_id = f"attempt_{len(episode['attempt_ids']) + 1:03d}"
            episode["attempt_ids"].append(attempt_id)
            episode["updated_at"] = _utc_now()
            self._write_json(self._episode_path(episode_id), episode)

            updates = self._pending_asr.pop(int(session_id), [])
            final_update = next(
                (row for row in reversed(updates) if row["kind"] == "final"), {}
            )
            attempt = {
                "schema_version": SCHEMA_VERSION,
                "episode_id": episode_id,
                "attempt_id": attempt_id,
                "request_id": int(request_id),
                "asr_session_id": int(session_id),
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "mode": "edit",
                "target_text": str(target_text),
                "application": str(application),
                "asr": {
                    "backend": final_update.get("backend", ""),
                    "model": final_update.get("model", ""),
                    "final_text": final_update.get("text", ""),
                    "latency_ms": final_update.get("latency_ms"),
                    "audio_duration_ms": final_update.get("audio_duration_ms"),
                    "error": final_update.get("error"),
                },
                "llm": {
                    "provider": str(provider),
                    "model": str(model),
                    "prompt_version": str(prompt_version),
                    "winner_branch": "",
                },
                "candidate_text": "",
                "feedback": [],
                "status": "processing",
            }
            attempt_dir = self._attempt_dir(episode_id, attempt_id)
            attempt_dir.mkdir(parents=True, exist_ok=True)
            self._write_json(attempt_dir / "attempt.json", attempt)
            self._write_jsonl(attempt_dir / "asr_updates.jsonl", updates)
            self._write_jsonl(attempt_dir / "llm_branches.jsonl", [])
            audio = self._pending_audio.pop(int(session_id), None)
            if audio is not None:
                self._write_wav(attempt_dir / "audio_raw.wav", audio)
            self._request_attempts[int(request_id)] = (episode_id, attempt_id)
            return episode_id, attempt_id

    def record_llm_result(self, request_id: int, result) -> None:
        with self._lock:
            reference = self._request_attempts.get(int(request_id))
            if reference is None:
                return
            episode_id, attempt_id = reference
            attempt_path = self._attempt_dir(episode_id, attempt_id) / "attempt.json"
            attempt = self._read_json(attempt_path)
            branches = [_json_value(item) for item in getattr(result, "llm_branches", ())]
            if branches:
                self._write_jsonl(
                    self._attempt_dir(episode_id, attempt_id) / "llm_branches.jsonl",
                    branches,
                )
                attempt["llm"]["branches_collected_at"] = _utc_now()
            attempt["llm"]["winner_branch"] = str(
                getattr(result, "winner_branch", "") or ""
            )
            attempt["llm"]["latency_ms"] = round(
                float(getattr(result, "latency_s", 0.0)) * 1000, 3
            )
            attempt["candidate_text"] = str(getattr(result, "final_text", "") or "")
            attempt["llm_error"] = getattr(result, "error", None)
            attempt["status"] = "preview" if not attempt["llm_error"] else "failed"
            attempt["updated_at"] = _utc_now()
            self._write_json(attempt_path, attempt)
            if not attempt["llm_error"]:
                self._preview_started[reference] = time.perf_counter()

    def record_llm_branches(
        self,
        request_id: int,
        branches,
        winner_branch: str,
    ) -> None:
        """Finish the slow branch trace without changing the UI result."""
        with self._lock:
            reference = self._request_attempts.get(int(request_id))
            if reference is None:
                return
            episode_id, attempt_id = reference
            rows = [_json_value(item) for item in tuple(branches or ())]
            if not rows:
                return
            self._write_jsonl(
                self._attempt_dir(episode_id, attempt_id) / "llm_branches.jsonl",
                rows,
            )
            attempt_path = self._attempt_dir(episode_id, attempt_id) / "attempt.json"
            attempt = self._read_json(attempt_path)
            attempt["llm"]["winner_branch"] = str(winner_branch or "")
            attempt["llm"]["branches_collected_at"] = _utc_now()
            attempt["updated_at"] = _utc_now()
            self._write_json(attempt_path, attempt)

    def record_llm_failure(self, request_id: int, error: str) -> None:
        """Persist a semantic failure discovered after a valid LLM response."""
        with self._lock:
            reference = self._request_attempts.get(int(request_id))
            if reference is None:
                return
            episode_id, attempt_id = reference
            attempt_path = self._attempt_dir(episode_id, attempt_id) / "attempt.json"
            attempt = self._read_json(attempt_path)
            attempt["llm_error"] = str(error)
            attempt["status"] = "failed"
            attempt["updated_at"] = _utc_now()
            self._preview_started.pop(reference, None)
            self._write_json(attempt_path, attempt)

    def feedback(
        self,
        request_id: int,
        action: str,
        *,
        error: str | None = None,
        final_text: str | None = None,
        manually_corrected: bool = False,
    ) -> None:
        with self._lock:
            reference = self._request_attempts.get(int(request_id))
            if reference is None:
                return
            episode_id, attempt_id = reference
            attempt_path = self._attempt_dir(episode_id, attempt_id) / "attempt.json"
            attempt = self._read_json(attempt_path)
            preview_started = self._preview_started.pop(reference, None)
            dwell_ms = None
            if preview_started is not None:
                dwell_ms = round(max(0.0, time.perf_counter() - preview_started) * 1000, 3)
            event = {
                "action": str(action),
                "occurred_at": _utc_now(),
                "preview_dwell_ms": dwell_ms,
            }
            if error:
                event["error"] = str(error)
            attempt["feedback"].append(event)
            attempt["status"] = str(action)
            attempt["updated_at"] = _utc_now()
            self._write_json(attempt_path, attempt)

            if action == "retry":
                return
            if action == "confirm":
                self._finalize_episode_locked(
                    episode_id,
                    status="completed",
                    final_text=str(final_text if final_text is not None else attempt["candidate_text"]),
                    manually_corrected=bool(manually_corrected),
                )
            elif action == "cancel":
                self._finalize_episode_locked(
                    episode_id,
                    status="cancelled",
                    final_text=str(final_text if final_text is not None else attempt["target_text"]),
                    manually_corrected=bool(manually_corrected),
                )
            elif action in {"apply_failed", "abandoned"}:
                self._finalize_episode_locked(
                    episode_id,
                    status="abandoned",
                    final_text=str(final_text if final_text is not None else attempt["target_text"]),
                    manually_corrected=bool(manually_corrected),
                )

    def abandon_request(self, request_id: int, error: str) -> None:
        self.feedback(request_id, "abandoned", error=error)

    def annotate_feedback_reason(
        self,
        request_id: int,
        action: str,
        reason_code: str,
        *,
        input_method: str = "keyboard",
    ) -> bool:
        """Attach an optional reason to the matching persisted feedback event.

        The retry/cancel event is written first so closing or crashing the app
        cannot lose the user's primary action.  This method performs a later
        atomic rewrite only if the user explicitly supplies a reason.
        """
        normalized_action = str(action).strip().lower()
        normalized_reason = str(reason_code).strip().lower()
        if normalized_action not in _REASONABLE_FEEDBACK_ACTIONS:
            raise ValueError(f"unsupported feedback reason action: {action}")
        if normalized_reason not in FEEDBACK_REASON_LABELS:
            raise ValueError(f"unsupported feedback reason: {reason_code}")

        with self._lock:
            reference = self._request_attempts.get(int(request_id))
            if reference is None:
                return False
            episode_id, attempt_id = reference
            attempt_path = self._attempt_dir(episode_id, attempt_id) / "attempt.json"
            attempt = self._read_json(attempt_path)
            event = next(
                (
                    item
                    for item in reversed(attempt.get("feedback", []))
                    if item.get("action") == normalized_action
                    and "failure_reason" not in item
                ),
                None,
            )
            if event is None:
                return False
            event["failure_reason"] = {
                "code": normalized_reason,
                "label": FEEDBACK_REASON_LABELS[normalized_reason],
                "selected_at": _utc_now(),
                "input_method": str(input_method).strip() or "unknown",
            }
            attempt["updated_at"] = _utc_now()
            self._write_json(attempt_path, attempt)
            return True

    @staticmethod
    def _new_id(prefix: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{prefix}_{stamp}_{uuid.uuid4().hex[:8]}"

    def _episode_dir(self, episode_id: str) -> Path:
        return self.user_root / episode_id

    def _episode_path(self, episode_id: str) -> Path:
        return self._episode_dir(episode_id) / "episode.json"

    def _attempt_dir(self, episode_id: str, attempt_id: str) -> Path:
        return self._episode_dir(episode_id) / attempt_id

    def _episode_data(self, episode_id: str) -> dict:
        return self._read_json(self._episode_path(episode_id))

    def _prune_pending_locked(self) -> None:
        session_ids = sorted(set(self._pending_audio) | set(self._pending_asr))
        for session_id in session_ids[:-_MAX_PENDING_SESSIONS]:
            self._pending_audio.pop(session_id, None)
            self._pending_asr.pop(session_id, None)

    def _finalize_episode_locked(
        self,
        episode_id: str,
        *,
        status: str,
        final_text: str,
        manually_corrected: bool,
    ) -> None:
        episode = self._episode_data(episode_id)
        episode["final_user_text"] = str(final_text)
        episode["final_status"] = str(status)
        episode["manually_corrected"] = bool(manually_corrected)
        episode["updated_at"] = _utc_now()
        self._write_json(self._episode_path(episode_id), episode)
        if self._active_episode_id == episode_id:
            self._active_episode_id = None

    @staticmethod
    def _read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _write_wav(path: Path, audio: np.ndarray) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        pcm = np.rint(np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
        temporary = path.with_suffix(path.suffix + ".tmp")
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(pcm.tobytes())
        temporary.replace(path)
