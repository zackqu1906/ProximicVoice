"""Unified per-utterance interaction and association collection."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import threading
import wave

import numpy as np


SCHEMA_VERSION = 3
PROMPT_VERSION = "edit-race-v1"
_MAX_PENDING_SESSIONS = 8
_FEEDBACK_ACTIONS = {"cancel", "apply_failed", "abandoned"}
_CURRENT_INTERACTION_ID = re.compile(
    r"^interaction_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{3}(?:_\d{2})?$"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _local_interaction_stamp(occurred_at: str) -> str:
    """Format an ISO timestamp as a readable local wall-clock folder suffix."""
    try:
        instant = datetime.fromisoformat(str(occurred_at)).astimezone()
    except (TypeError, ValueError):
        instant = datetime.now().astimezone()
    return instant.strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]


def _json_value(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class ModificationDatasetCollector:
    """Thread-safe persistence for one InteractionRecord per ASR session."""

    def __init__(
        self,
        root: str | Path,
        user_id: str,
        *,
        on_saved=None,
    ) -> None:
        self.root = Path(root)
        self.user_id = str(user_id).strip()
        if not self.user_id:
            raise ValueError("anonymous user_id cannot be empty")
        self.user_root = self.root / self.user_id
        self.interactions_root = self.user_root / "interactions"
        self.association_index_path = self.user_root / "associations.jsonl"
        self._on_saved = on_saved
        self._lock = threading.RLock()
        self._pending_audio: dict[int, np.ndarray] = {}
        self._pending_asr: dict[int, list[dict]] = {}
        self._pending_runtime_events: list[dict] = []
        self._session_interactions: dict[int, str] = {}
        self._request_interactions: dict[int, str] = {}
        self._routing_interactions: dict[int, str] = {}
        self._published_interactions: set[str] = set()

    def reset_runtime(self) -> None:
        """Drop in-memory request/session bindings from an interrupted run."""
        with self._lock:
            self._pending_audio.clear()
            self._pending_asr.clear()
            self._pending_runtime_events.clear()
            self._session_interactions.clear()
            self._request_interactions.clear()
            self._routing_interactions.clear()

    def begin_session(self, session_id: int) -> str:
        """Create the interaction as soon as ProxiMic activates.

        This gives an early user cancellation a stable interaction to label,
        even when the ASR backend has not emitted its first partial yet.
        """
        session_id = int(session_id)
        if session_id <= 0:
            return ""
        with self._lock:
            return self._ensure_interaction_locked(session_id)

    def record_near_field_label(
        self,
        session_id: int,
        *,
        label: str,
        source: str,
    ) -> None:
        normalized = str(label).strip().lower()
        if normalized not in {"positive", "negative"}:
            raise ValueError("near-field label must be positive or negative")
        session_id = int(session_id)
        if session_id <= 0:
            return
        with self._lock:
            interaction_id = self._ensure_interaction_locked(session_id)
            record = self._interaction_data(interaction_id)
            occurred_at = _utc_now()
            record.setdefault("near_field", {}).update(
                {
                    "training_label": normalized,
                    "label_source": str(source),
                    "label_recorded_at": occurred_at,
                }
            )
            record["updated_at"] = occurred_at
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(
                interaction_id,
                {
                    "type": "model_label",
                    "model": "near_field",
                    "label": normalized,
                    "source": str(source),
                    "occurred_at": occurred_at,
                },
            )

    def record_asr_label(
        self,
        session_id: int,
        *,
        label: str,
        source: str,
    ) -> None:
        normalized = str(label).strip().lower()
        if normalized not in {"positive", "negative"}:
            raise ValueError("ASR label must be positive or negative")
        session_id = int(session_id)
        if session_id <= 0:
            return
        with self._lock:
            interaction_id = self._ensure_interaction_locked(session_id)
            record = self._interaction_data(interaction_id)
            occurred_at = _utc_now()
            record.setdefault("asr", {}).update(
                {
                    "training_label": normalized,
                    "label_source": str(source),
                    "label_recorded_at": occurred_at,
                }
            )
            record["updated_at"] = occurred_at
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(
                interaction_id,
                {
                    "type": "model_label",
                    "model": "asr",
                    "label": normalized,
                    "source": str(source),
                    "occurred_at": occurred_at,
                },
            )

    def record_mode_acceptance(self, session_id: int, *, mode: str) -> None:
        """Record the routed mode as an implicit positive after application."""
        session_id = int(session_id)
        if session_id <= 0:
            return
        with self._lock:
            interaction_id = self._ensure_interaction_locked(session_id)
            record = self._interaction_data(interaction_id)
            occurred_at = _utc_now()
            record.setdefault("mode", {}).update(
                {
                    "selected": str(mode),
                    "training_label": "positive",
                    "negative_mode": "",
                    "label_source": "implicit_application",
                    "label_recorded_at": occurred_at,
                }
            )
            record["updated_at"] = occurred_at
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(
                interaction_id,
                {
                    "type": "model_label",
                    "model": "input_mode",
                    "label": "positive",
                    "mode": str(mode),
                    "source": "implicit_application",
                    "occurred_at": occurred_at,
                },
            )

    def record_audio(self, session_id: int, audio_16k: np.ndarray) -> None:
        session_id = int(session_id)
        if session_id <= 0:
            return
        audio = np.asarray(audio_16k, dtype=np.float32).reshape(-1).copy()
        with self._lock:
            interaction_id = self._session_interactions.get(session_id)
            if interaction_id is None:
                self._pending_audio[session_id] = audio
            else:
                self._write_interaction_audio_locked(interaction_id, audio)
                self._publish_history_locked(interaction_id)
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
            interaction_id = self._session_interactions.get(session_id)
            if interaction_id is not None:
                self._write_asr_updates_locked(interaction_id, session_id)
                self._update_final_asr_locked(interaction_id, row)
            elif row["kind"] == "final":
                interaction_id = self._ensure_interaction_locked(session_id)
            if interaction_id is not None and row["kind"] == "final":
                self._publish_history_locked(interaction_id)
            self._prune_pending_locked()

    def record_final(self, update) -> None:
        """VoiceHistory-compatible entry point for a final ASR update."""
        if bool(getattr(update, "is_final", False)):
            self.record_asr_update(update)

    def record_runtime_event(self, message: str, session_id: int = 0) -> None:
        """Persist detector/timing evidence without parsing it into hard gates."""
        event = {
            "type": "runtime_evidence",
            "occurred_at": _utc_now(),
            "message": str(message),
        }
        event.update(self._runtime_evidence_fields(str(message)))
        with self._lock:
            interaction_id = self._session_interactions.get(int(session_id))
            if interaction_id is None:
                self._pending_runtime_events.append(event)
                del self._pending_runtime_events[:-64]
                return
            self._append_event_locked(interaction_id, event)
            self._apply_runtime_evidence_locked(interaction_id, event)

    def record_imu_samples(
        self,
        session_id: int,
        samples,
        *,
        sample_rate_hz: float | None = None,
        dropped_samples: int = 0,
        alignment_method: str = "",
    ) -> None:
        """Append audio-aligned IMU samples for future weighted fusion."""
        session_id = int(session_id)
        if session_id <= 0:
            return
        sample_values = tuple(samples) if samples is not None else ()
        rows = [_json_value(item) for item in sample_values]
        with self._lock:
            interaction_id = self._ensure_interaction_locked(session_id)
            path = self._interaction_dir(interaction_id) / "imu.jsonl"
            existing = self._read_jsonl(path)
            self._write_jsonl(path, existing + rows)
            record = self._interaction_data(interaction_id)
            record["imu"] = {
                "samples_file": path.name,
                "sample_count": len(existing) + len(rows),
                "sample_rate_hz": sample_rate_hz,
                "dropped_samples": max(0, int(dropped_samples)),
                "alignment_method": str(alignment_method),
            }
            record["updated_at"] = _utc_now()
            self._write_json(self._interaction_path(interaction_id), record)
            self._publish_history_locked(
                interaction_id,
                force=(
                    str(record.get("outcome", {}).get("status", ""))
                    == "cancelled"
                ),
            )

    def record_routing_request(self, request) -> None:
        from .text_processing.prompts import INPUT_MODE_ROUTER_PROMPT

        with self._lock:
            session_id = int(getattr(request, "session_id", 0))
            interaction_id = self._ensure_interaction_locked(session_id)
            request_id = int(getattr(request, "request_id", 0))
            settings = getattr(request, "settings", None)
            self._routing_interactions[request_id] = interaction_id
            record = self._interaction_data(interaction_id)
            record["mode"].update(
                {
                    "routing": "auto",
                    "fallback": str(getattr(request, "fallback_mode", "")),
                    "router_request_id": request_id,
                    "router_input": str(getattr(request, "raw_text", "")),
                    "router_system_prompt": INPUT_MODE_ROUTER_PROMPT,
                    "router_provider": str(getattr(settings, "provider", "")),
                    "router_model": str(getattr(settings, "model", "")),
                }
            )
            record["updated_at"] = _utc_now()
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(
                interaction_id,
                {
                    "type": "routing_started",
                    "occurred_at": _utc_now(),
                    "request_id": request_id,
                },
            )

    def record_routing_result(self, result) -> None:
        request_id = int(getattr(result, "request_id", 0))
        with self._lock:
            interaction_id = self._routing_interactions.get(request_id)
            if interaction_id is None:
                interaction_id = self._ensure_interaction_locked(
                    int(getattr(result, "session_id", 0))
                )
            record = self._interaction_data(interaction_id)
            record["mode"].update(
                {
                    "predicted": str(getattr(result, "mode", "")),
                    "router_output": str(getattr(result, "model_output", "") or ""),
                    "router_latency_ms": round(
                        float(getattr(result, "latency_s", 0.0) or 0.0) * 1000,
                        3,
                    ),
                    "router_error": getattr(result, "error", None),
                }
            )
            record["updated_at"] = _utc_now()
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(
                interaction_id,
                {
                    "type": "routing_completed",
                    "occurred_at": _utc_now(),
                    "request_id": request_id,
                    "predicted_mode": str(getattr(result, "mode", "")),
                    "error": getattr(result, "error", None),
                },
            )

    def record_text_request(self, request) -> None:
        """Bind every dictation/edit LLM call to the utterance record."""
        request_id = int(getattr(request, "request_id", 0))
        session_id = int(getattr(request, "session_id", 0))
        if request_id <= 0:
            return
        with self._lock:
            interaction_id = self._ensure_interaction_locked(session_id)
            self._request_interactions[request_id] = interaction_id
            settings = getattr(request, "settings", None)
            mode = str(getattr(request, "mode", ""))
            raw_text = str(getattr(request, "raw_text", ""))
            target_text = str(getattr(request, "target_text", ""))
            llm_request = {
                "request_id": request_id,
                "mode": mode,
                "provider": str(getattr(settings, "provider", "")),
                "model": str(getattr(settings, "model", "")),
                "base_url": str(getattr(settings, "base_url", "")),
                "prompt_version": PROMPT_VERSION,
                "input": self._llm_input(mode, raw_text, target_text),
                "started_at": _utc_now(),
                "status": "processing",
            }
            record = self._interaction_data(interaction_id)
            record["mode"]["selected"] = mode
            requests = record["llm"].setdefault("requests", [])
            requests[:] = [
                item for item in requests if int(item.get("request_id", 0)) != request_id
            ]
            requests.append(llm_request)
            record["updated_at"] = _utc_now()
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(
                interaction_id,
                {
                    "type": "llm_started",
                    "occurred_at": _utc_now(),
                    "request_id": request_id,
                    "mode": mode,
                },
            )

    def record_llm_result(self, request_id: int, result) -> None:
        with self._lock:
            branches = [_json_value(item) for item in getattr(result, "llm_branches", ())]
            interaction_id = self._request_interactions.get(int(request_id))
            if interaction_id is None:
                interaction_id = self._session_interactions.get(
                    int(getattr(result, "session_id", 0))
                )
            if interaction_id is None:
                return
            updates = {
                "completed_at": _utc_now(),
                "status": "failed" if getattr(result, "error", None) else "completed",
                "latency_ms": round(
                    float(getattr(result, "latency_s", 0.0) or 0.0) * 1000,
                    3,
                ),
                "used_llm": bool(getattr(result, "used_llm", False)),
                "raw_output": str(getattr(result, "model_output", "") or ""),
                "candidate_text": str(getattr(result, "final_text", "") or ""),
                "error": getattr(result, "error", None),
                "winner_branch": str(
                    getattr(result, "winner_branch", "") or ""
                ),
                "branches": branches,
            }
            self._update_llm_request_locked(interaction_id, int(request_id), updates)
            self._append_event_locked(
                interaction_id,
                {
                    "type": "llm_completed",
                    "occurred_at": _utc_now(),
                    "request_id": int(request_id),
                    "error": getattr(result, "error", None),
                },
            )

    def record_llm_branches(
        self,
        request_id: int,
        branches,
        winner_branch: str,
    ) -> None:
        """Finish the slow branch trace without changing the UI result."""
        with self._lock:
            rows = [_json_value(item) for item in tuple(branches or ())]
            if not rows:
                return
            interaction_id = self._request_interactions.get(int(request_id))
            if interaction_id is not None:
                self._update_llm_request_locked(
                    interaction_id,
                    int(request_id),
                    {
                        "branches": rows,
                        "winner_branch": str(winner_branch or ""),
                        "branches_collected_at": _utc_now(),
                    },
                )

    def record_llm_failure(self, request_id: int, error: str) -> None:
        """Persist a semantic failure discovered after a valid LLM response."""
        with self._lock:
            interaction_id = self._request_interactions.get(int(request_id))
            if interaction_id is not None:
                self._update_llm_request_locked(
                    interaction_id,
                    int(request_id),
                    {"status": "failed", "error": str(error)},
                )

    def feedback(
        self,
        request_id: int,
        action: str,
        *,
        error: str | None = None,
        final_text: str | None = None,
        manually_corrected: bool = False,
    ) -> None:
        normalized_action = str(action).strip().lower()
        if normalized_action not in _FEEDBACK_ACTIONS:
            raise ValueError(f"unsupported feedback action: {action}")
        with self._lock:
            event = {
                "action": normalized_action,
                "occurred_at": _utc_now(),
            }
            if error:
                event["error"] = str(error)
            interaction_id = self._request_interactions.get(int(request_id))
            if interaction_id is not None:
                record = self._interaction_data(interaction_id)
                unified_event = dict(event)
                unified_event["type"] = "feedback"
                unified_event["request_id"] = int(request_id)
                record["feedback"].append(unified_event)
                record["outcome"].update(
                    {
                        "status": normalized_action,
                        "final_text": (
                            str(final_text) if final_text is not None else None
                        ),
                        "manually_corrected": bool(manually_corrected),
                        # Undo is explicit negative evidence. Successful
                        # application is recorded separately by
                        # record_acceptance, without asking for feedback.
                        "accepted": (
                            False if normalized_action == "cancel" else None
                        ),
                        "acceptance_strength": (
                            "explicit"
                            if normalized_action == "cancel"
                            else "pending_undo"
                        ),
                    }
                )
                record["updated_at"] = _utc_now()
                self._write_json(self._interaction_path(interaction_id), record)
                self._append_event_locked(interaction_id, unified_event)

    def abandon_request(self, request_id: int, error: str) -> None:
        self.feedback(request_id, "abandoned", error=error)

    def record_application(
        self,
        *,
        action: str,
        session_id: int = 0,
        request_id: int = 0,
        mode: str = "",
        application: str = "",
        target_key: str = "",
        before_text: str | None = None,
        candidate_text: str | None = None,
        final_text: str | None = None,
        method: str = "automatic",
        error: str | None = None,
    ) -> str:
        """Append an application/cancel/undo event to one utterance."""
        with self._lock:
            interaction_id = self._request_interactions.get(int(request_id))
            if interaction_id is None:
                interaction_id = self._session_interactions.get(int(session_id))
            if interaction_id is None and int(session_id) > 0:
                interaction_id = self._ensure_interaction_locked(int(session_id))
            if interaction_id is None:
                return ""
            occurred_at = _utc_now()
            event = {
                "type": "application",
                "action": str(action),
                "occurred_at": occurred_at,
                "request_id": int(request_id),
                "mode": str(mode),
                "method": str(method),
            }
            if application:
                event["application"] = str(application)
            if target_key:
                event["target_key"] = str(target_key)
            if before_text is not None:
                event["before_text"] = str(before_text)
            if candidate_text is not None:
                event["candidate_text"] = str(candidate_text)
            if final_text is not None:
                event["final_text"] = str(final_text)
            if error:
                event["error"] = str(error)
            record = self._interaction_data(interaction_id)
            if mode:
                record["mode"]["selected"] = str(mode)
            if application:
                record.setdefault("target", {})["application"] = str(application)
            if target_key:
                record.setdefault("target", {})["key"] = str(target_key)
            record["outcome"].update(
                {
                    "status": str(action),
                    "application_method": str(method),
                    "final_text": (
                        str(final_text) if final_text is not None else None
                    ),
                    "accepted": (
                        False
                        if str(action) in {"cancelled", "undone", "apply_failed"}
                        else None
                        if str(action) == "applied"
                        else record["outcome"].get("accepted")
                    ),
                    "acceptance_strength": (
                        "explicit"
                        if str(action) in {"cancelled", "undone"}
                        else "pending_undo"
                        if str(action) == "applied"
                        else record["outcome"].get("acceptance_strength")
                    ),
                }
            )
            record["updated_at"] = occurred_at
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(interaction_id, event)
            self._publish_history_locked(
                interaction_id,
                force=str(action) == "cancelled",
            )
            return interaction_id

    def record_acceptance(
        self,
        *,
        accepted: bool,
        session_id: int = 0,
        request_id: int = 0,
        strength: str,
        reason: str,
    ) -> bool:
        with self._lock:
            interaction_id = self._request_interactions.get(int(request_id))
            if interaction_id is None:
                interaction_id = self._session_interactions.get(int(session_id))
            if interaction_id is None:
                return False
            record = self._interaction_data(interaction_id)
            # Never replace an explicit rejection with a weaker inference.
            if (
                record.get("outcome", {}).get("accepted") is False
                and record.get("outcome", {}).get("acceptance_strength")
                == "explicit"
            ):
                return False
            event = {
                "type": "acceptance",
                "occurred_at": _utc_now(),
                "accepted": bool(accepted),
                "strength": str(strength),
                "reason": str(reason),
            }
            record["outcome"].update(
                {
                    "accepted": bool(accepted),
                    "acceptance_strength": str(strength),
                    "acceptance_reason": str(reason),
                }
            )
            record["updated_at"] = _utc_now()
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(interaction_id, event)
            return True

    def record_mode_correction(
        self, session_id: int, *, previous_mode: str, corrected_mode: str
    ) -> None:
        session_id = int(session_id)
        if session_id <= 0:
            return
        with self._lock:
            interaction_id = self._ensure_interaction_locked(session_id)
            record = self._interaction_data(interaction_id)
            record["mode"].update(
                {
                    "selected": str(corrected_mode),
                    "user_corrected": True,
                    "training_label": "positive",
                    "negative_mode": str(previous_mode),
                    "label_source": "explicit_tab",
                    "label_recorded_at": _utc_now(),
                }
            )
            record["updated_at"] = _utc_now()
            self._write_json(self._interaction_path(interaction_id), record)
            self._append_event_locked(
                interaction_id,
                {
                    "type": "mode_corrected",
                    "occurred_at": _utc_now(),
                    "previous_mode": str(previous_mode),
                    "corrected_mode": str(corrected_mode),
                },
            )

    def interaction_id_for_session(self, session_id: int) -> str:
        with self._lock:
            return self._session_interactions.get(int(session_id), "")

    def association_member_for_session(
        self,
        session_id: int,
        *,
        target_key: str = "",
        mode: str = "",
        status: str = "",
    ) -> dict:
        """Return a lightweight pointer used by the recommendation engine."""
        with self._lock:
            interaction_id = self._session_interactions.get(int(session_id))
            if interaction_id is None:
                return {}
            record = self._interaction_data(interaction_id)
            return self._association_candidate(
                self._interaction_dir(interaction_id),
                record,
                target_key=target_key,
                mode=mode,
                status=status,
            )

    def record_manual_result(
        self,
        interaction_id: str,
        *,
        text: str,
        mode: str,
    ) -> str:
        """Attach a manual positive to an existing interaction without copying it."""
        value = str(text)
        if not value:
            raise ValueError("manual result cannot be empty")
        with self._lock:
            record = self._interaction_data(str(interaction_id))
            result_id = self._new_id("manual-result")
            record.setdefault("manual_results", []).append(
                {
                    "result_id": result_id,
                    "mode": str(mode),
                    "text": value,
                    "created_at": _utc_now(),
                }
            )
            record["outcome"]["manually_corrected"] = True
            record["updated_at"] = _utc_now()
            self._write_json(self._interaction_path(str(interaction_id)), record)
            return result_id

    def create_association(
        self,
        *,
        kind: str,
        subtype: str,
        chosen: dict,
        rejected: list[dict],
        source: str,
        relation_type: str = "",
    ) -> str:
        """Append one relationship row; original interaction data stays in place."""
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in {"asr", "llm"}:
            raise ValueError("association kind must be asr or llm")
        chosen_ref = self._association_reference(chosen)
        rejected_refs = [
            self._association_reference(item) for item in rejected
        ]
        if not rejected_refs:
            raise ValueError("association requires at least one rejected member")
        all_refs = [chosen_ref, *rejected_refs]
        interaction_ids = list(
            dict.fromkeys(
                str(item["interaction_id"]) for item in all_refs
            )
        )
        invalid_ids = [
            interaction_id
            for interaction_id in interaction_ids
            if _CURRENT_INTERACTION_ID.fullmatch(interaction_id) is None
        ]
        if invalid_ids:
            raise ValueError(
                "association member uses a legacy interaction id: "
                + ", ".join(invalid_ids)
            )
        with self._lock:
            records = {
                interaction_id: self._interaction_data(interaction_id)
                for interaction_id in interaction_ids
            }
            if normalized_kind == "llm":
                if not (
                    int(chosen_ref.get("request_id", 0)) > 0
                    or chosen_ref.get("result_id")
                ):
                    raise ValueError("LLM chosen member has no result reference")
                for reference in rejected_refs:
                    if int(reference.get("request_id", 0)) <= 0:
                        raise ValueError("LLM rejected member has no request reference")
            association_id = self._new_id(
                "asr-link" if normalized_kind == "asr" else "dpo-link"
            )
            row = {
                "schema_version": SCHEMA_VERSION,
                "association_id": association_id,
                "kind": normalized_kind,
                "subtype": str(subtype),
                "relation_type": str(relation_type),
                "source": str(source),
                "created_at": _utc_now(),
                "chosen": chosen_ref,
                "rejected": rejected_refs,
                "member_interaction_ids": interaction_ids,
            }
            rows = self._read_jsonl(self.association_index_path)
            rows.append(row)
            self._write_jsonl(self.association_index_path, rows)
            for interaction_id, record in records.items():
                ids = record.setdefault("association_ids", [])
                if association_id not in ids:
                    ids.append(association_id)
                memberships = record.setdefault("association_memberships", [])
                for role, references in (
                    ("chosen", [chosen_ref]),
                    ("rejected", rejected_refs),
                ):
                    for reference in references:
                        if reference["interaction_id"] != interaction_id:
                            continue
                        membership = {
                            "association_id": association_id,
                            "kind": normalized_kind,
                            "subtype": str(subtype),
                            "role": role,
                            "request_id": int(reference.get("request_id", 0)),
                            "result_id": str(reference.get("result_id", "")),
                        }
                        if membership not in memberships:
                            memberships.append(membership)
                record["updated_at"] = _utc_now()
                self._write_json(self._interaction_path(interaction_id), record)
            return association_id

    def load_associations(self) -> list[dict]:
        with self._lock:
            return self._read_jsonl(self.association_index_path)

    def load_association_candidates(
        self,
        kind: str,
        *,
        asr_subtype: str = "dictation_retry",
        limit: int = 40,
    ) -> list[dict]:
        """Load recent, unassociated records for the simple association center."""
        normalized_kind = str(kind).strip().lower()
        associations = self._read_jsonl(self.association_index_path)
        used = {
            interaction_id
            for association in associations
            if association.get("kind") == normalized_kind
            for interaction_id in association.get("member_interaction_ids", [])
        }
        rows: list[dict] = []
        paths = sorted(
            self.interactions_root.glob("*/record.json"),
            key=lambda path: path.parent.name,
            reverse=True,
        )
        for path in paths:
            if _CURRENT_INTERACTION_ID.fullmatch(path.parent.name) is None:
                continue
            if path.parent.name in used:
                continue
            try:
                record = self._read_json(path)
                candidate = self._association_candidate(path.parent, record)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            mode = str(candidate.get("mode", ""))
            if normalized_kind == "llm":
                if mode != "edit" or int(candidate.get("requestId", 0)) <= 0:
                    continue
            elif normalized_kind == "asr":
                expected_mode = (
                    "edit" if asr_subtype == "instruction_retry" else "dictation"
                )
                if mode != expected_mode:
                    continue
            else:
                continue
            rows.append(candidate)
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    @staticmethod
    def _association_reference(value: dict) -> dict:
        interaction_id = str(value.get("interaction_id", "")).strip()
        if not interaction_id:
            raise ValueError("association member has no interaction id")
        reference = {
            "interaction_id": interaction_id,
            "record_path": f"interactions/{interaction_id}/record.json",
        }
        request_id = int(value.get("request_id", 0) or 0)
        if request_id > 0:
            reference["request_id"] = request_id
        result_id = str(value.get("result_id", "")).strip()
        if result_id:
            reference["result_id"] = result_id
        return reference

    @staticmethod
    def _association_candidate(
        interaction_dir: Path,
        record: dict,
        *,
        target_key: str = "",
        mode: str = "",
        status: str = "",
    ) -> dict:
        asr = record.get("asr", {})
        outcome = record.get("outcome", {})
        selected_mode = str(mode or record.get("mode", {}).get("selected", ""))
        requests = record.get("llm", {}).get("requests", [])
        selected_request = next(
            (
                item
                for item in reversed(requests)
                if str(item.get("mode", "")) == selected_mode
                and str(item.get("candidate_text", "") or "").strip()
            ),
            next(
                (
                    item for item in reversed(requests)
                    if str(item.get("candidate_text", "") or "").strip()
                ),
                {},
            ),
        )
        audio_file = str(record.get("audio", {}).get("file", "audio.wav"))
        audio_path = interaction_dir / audio_file
        manual_result = next(
            reversed(record.get("manual_results", [])), {}
        )
        created_at = str(record.get("created_at", ""))
        try:
            display_time = datetime.fromisoformat(created_at).astimezone().strftime(
                "%m-%d %H:%M:%S"
            )
        except ValueError:
            display_time = created_at
        return {
            "interactionId": str(record.get("interaction_id", interaction_dir.name)),
            "interaction_id": str(record.get("interaction_id", interaction_dir.name)),
            "sessionId": int(record.get("asr_session_id", 0) or 0),
            "session_id": int(record.get("asr_session_id", 0) or 0),
            "requestId": int(selected_request.get("request_id", 0) or 0),
            "request_id": int(selected_request.get("request_id", 0) or 0),
            "mode": selected_mode,
            "targetKey": str(target_key or record.get("target", {}).get("key", "")),
            "target_key": str(target_key or record.get("target", {}).get("key", "")),
            "asrText": str(asr.get("final_text", "") or ""),
            "asr_text": str(asr.get("final_text", "") or ""),
            "resultId": str(manual_result.get("result_id", "")),
            "result_id": str(manual_result.get("result_id", "")),
            "resultText": str(manual_result.get("text", "") or selected_request.get("candidate_text", "") or outcome.get("final_text", "") or ""),
            "result_text": str(manual_result.get("text", "") or selected_request.get("candidate_text", "") or outcome.get("final_text", "") or ""),
            "status": str(status or outcome.get("status", "recognized")),
            "statusLabel": str(status or outcome.get("status", "recognized")),
            "audioPath": str(audio_path) if audio_path.is_file() else "",
            "audio_path": str(audio_path) if audio_path.is_file() else "",
            "createdAt": created_at,
            "created_at": created_at,
            "displayTime": display_time,
        }


    def load_entries(self, limit: int = 100) -> list[dict]:
        """Return the Voice History projection of unified records."""
        rows: list[dict] = []
        if self.interactions_root.is_dir():
            paths = sorted(
                self.interactions_root.glob("*/record.json"),
                key=lambda path: path.parent.name,
                reverse=True,
            )
            for path in paths:
                if _CURRENT_INTERACTION_ID.fullmatch(path.parent.name) is None:
                    continue
                try:
                    record = self._read_json(path)
                    entry = self._history_entry(path.parent, record)
                    if entry is not None:
                        rows.append(entry)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
        rows.sort(key=lambda item: str(item.get("createdAt", "")), reverse=True)
        return rows[: max(0, int(limit))]

    def clear(self) -> None:
        with self._lock:
            self.reset_runtime()
            if self.user_root.exists():
                shutil.rmtree(self.user_root)
            self.interactions_root.mkdir(parents=True, exist_ok=True)
            self._published_interactions.clear()

    def close(self, *, wait: bool = False) -> None:
        del wait

    def _interaction_dir(self, interaction_id: str) -> Path:
        return self.interactions_root / str(interaction_id)

    def _interaction_path(self, interaction_id: str) -> Path:
        return self._interaction_dir(interaction_id) / "record.json"

    def _interaction_data(self, interaction_id: str) -> dict:
        return self._read_json(self._interaction_path(interaction_id))

    def _ensure_interaction_locked(self, session_id: int) -> str:
        session_id = int(session_id)
        existing = self._session_interactions.get(session_id)
        if existing is not None:
            return existing
        updates = list(self._pending_asr.get(session_id, []))
        final_update = next(
            (row for row in reversed(updates) if row.get("kind") == "final"),
            {},
        )
        started_at = str(
            next(
                (
                    row.get("recorded_at")
                    for row in updates
                    if row.get("recorded_at")
                ),
                None,
            )
            or _utc_now()
        )
        base_interaction_id = (
            f"interaction_{_local_interaction_stamp(started_at)}"
        )
        interaction_id = base_interaction_id
        collision_index = 2
        while self._interaction_dir(interaction_id).exists():
            interaction_id = f"{base_interaction_id}_{collision_index:02d}"
            collision_index += 1
        interaction_dir = self._interaction_dir(interaction_id)
        interaction_dir.mkdir(parents=True, exist_ok=False)
        record = {
            "schema_version": SCHEMA_VERSION,
            "interaction_id": interaction_id,
            "anonymous_user_id": self.user_id,
            "asr_session_id": session_id,
            "created_at": started_at,
            "updated_at": _utc_now(),
            "audio": {
                "file": "audio.wav",
                "sample_rate_hz": 16_000,
                "duration_ms": final_update.get("audio_duration_ms"),
                "available": False,
            },
            "asr": {
                "updates_file": "asr_updates.jsonl",
                "backend": final_update.get("backend", ""),
                "model": final_update.get("model", ""),
                "final_text": final_update.get("text", ""),
                "latency_ms": final_update.get("latency_ms"),
                "audio_duration_ms": final_update.get("audio_duration_ms"),
                "error": final_update.get("error"),
                "final_recorded": bool(final_update),
                "training_label": None,
                "label_source": None,
                "label_recorded_at": None,
            },
            "imu": {
                "samples_file": None,
                "sample_count": 0,
                "sample_rate_hz": None,
                "dropped_samples": 0,
                "alignment_method": "",
            },
            "near_field": {
                "audio_score": None,
                "imu_evidence_score": None,
                "context_score": None,
                "fusion_score": None,
                "fusion_weights": None,
                "model_version": None,
                "training_label": None,
                "label_source": None,
                "label_recorded_at": None,
            },
            "mode": {
                "routing": "manual",
                "fallback": "",
                "predicted": "",
                "selected": "",
                "user_corrected": False,
                "label_source": "system",
                "training_label": None,
                "negative_mode": "",
                "label_recorded_at": None,
            },
            "target": {},
            "llm": {"requests": []},
            "feedback": [],
            "outcome": {
                "status": "recognized" if final_update else "collecting",
                "final_text": None,
                "accepted": None,
                "acceptance_strength": None,
                "manually_corrected": False,
            },
            "association_ids": [],
            "association_memberships": [],
            "events_file": "events.jsonl",
        }
        self._write_json(self._interaction_path(interaction_id), record)
        self._write_jsonl(interaction_dir / "events.jsonl", [])
        self._write_jsonl(interaction_dir / "asr_updates.jsonl", updates)
        self._session_interactions[session_id] = interaction_id
        if self._pending_runtime_events:
            for event in self._pending_runtime_events:
                self._append_event_locked(interaction_id, event)
                self._apply_runtime_evidence_locked(interaction_id, event)
            self._pending_runtime_events.clear()
        audio = self._pending_audio.pop(session_id, None)
        if audio is not None:
            self._write_interaction_audio_locked(interaction_id, audio)
        return interaction_id

    def _write_asr_updates_locked(
        self, interaction_id: str, session_id: int
    ) -> None:
        self._write_jsonl(
            self._interaction_dir(interaction_id) / "asr_updates.jsonl",
            list(self._pending_asr.get(int(session_id), [])),
        )

    def _update_final_asr_locked(self, interaction_id: str, row: dict) -> None:
        if row.get("kind") != "final":
            return
        record = self._interaction_data(interaction_id)
        record["asr"].update(
            {
                "backend": row.get("backend", ""),
                "model": row.get("model", ""),
                "final_text": row.get("text", ""),
                "latency_ms": row.get("latency_ms"),
                "audio_duration_ms": row.get("audio_duration_ms"),
                "error": row.get("error"),
                "final_recorded": True,
            }
        )
        record["outcome"]["status"] = "recognized"
        record["updated_at"] = _utc_now()
        self._write_json(self._interaction_path(interaction_id), record)

    def _write_interaction_audio_locked(
        self, interaction_id: str, audio: np.ndarray
    ) -> None:
        path = self._interaction_dir(interaction_id) / "audio.wav"
        self._write_wav(path, audio)
        record = self._interaction_data(interaction_id)
        record["audio"].update(
            {
                "available": True,
                "duration_ms": int(round(len(audio) * 1000 / 16_000)),
                "sample_count": int(len(audio)),
            }
        )
        record["updated_at"] = _utc_now()
        self._write_json(self._interaction_path(interaction_id), record)

    def _publish_history_locked(
        self, interaction_id: str, *, force: bool = False
    ) -> None:
        if interaction_id in self._published_interactions and not force:
            return
        record = self._interaction_data(interaction_id)
        has_final_asr = bool(record.get("asr", {}).get("final_recorded"))
        is_cancelled = str(record.get("outcome", {}).get("status", "")) == "cancelled"
        if not has_final_asr and not is_cancelled:
            return
        entry = self._history_entry(self._interaction_dir(interaction_id), record)
        if entry is None:
            return
        self._published_interactions.add(interaction_id)
        if self._on_saved is not None:
            self._on_saved(entry)

    @staticmethod
    def _history_entry(interaction_dir: Path, record: dict) -> dict | None:
        audio = record.get("audio", {})
        audio_path = interaction_dir / str(audio.get("file", "audio.wav"))
        if not audio_path.is_file():
            return None
        asr = record.get("asr", {})
        text = str(asr.get("final_text", "") or "").strip()
        candidate_text = ModificationDatasetCollector._display_candidate_text(
            record
        )
        preference_eligible = any(
            str(request.get("candidate_text", "") or "").strip()
            for request in record.get("llm", {}).get("requests", [])
        )
        recorded_at = str(record.get("created_at", "") or "")
        try:
            when = datetime.fromisoformat(recorded_at).astimezone()
            display_time = when.strftime("%m-%d %H:%M:%S")
        except ValueError:
            display_time = recorded_at
        duration_ms = max(0, int(float(audio.get("duration_ms", 0) or 0)))
        imu = record.get("imu", {})
        imu_path = interaction_dir / str(imu.get("samples_file") or "imu.jsonl")
        imu_sample_count = max(0, int(imu.get("sample_count", 0) or 0))
        return {
            "id": str(record.get("interaction_id", interaction_dir.name)),
            "interactionId": str(
                record.get("interaction_id", interaction_dir.name)
            ),
            "createdAt": recorded_at,
            "displayTime": display_time,
            "text": text or "（未识别出文字）",
            "recognized": bool(text),
            "backend": str(asr.get("backend", "") or ""),
            "model": str(asr.get("model", "") or ""),
            "durationSeconds": duration_ms / 1000,
            "durationLabel": f"{duration_ms / 1000:.1f} 秒",
            "audioPath": str(audio_path),
            "recordPath": str(interaction_dir / "record.json"),
            "hasImu": imu_path.is_file() and imu_sample_count > 0,
            "imuSampleCount": imu_sample_count,
            "dataSummary": (
                f"音频已保存 · IMU {imu_sample_count} 条"
                if imu_path.is_file() and imu_sample_count > 0
                else "音频已保存 · IMU 未采集"
            ),
            "error": str(asr.get("error", "") or ""),
            "mode": str(record.get("mode", {}).get("selected", "") or ""),
            "outcome": str(record.get("outcome", {}).get("status", "") or ""),
            "candidateText": candidate_text,
            "preferenceEligible": preference_eligible,
        }

    @staticmethod
    def _display_candidate_text(record: dict) -> str:
        outcome = record.get("outcome", {})
        final_text = str(outcome.get("final_text", "") or "").strip()
        if str(outcome.get("status", "")) in {"applied", "confirm"} and final_text:
            return final_text
        requests = record.get("llm", {}).get("requests", [])
        selected_mode = str(record.get("mode", {}).get("selected", ""))
        for request in reversed(requests):
            candidate = str(request.get("candidate_text", "") or "").strip()
            if candidate and str(request.get("mode", "")) == selected_mode:
                return candidate
        for request in reversed(requests):
            candidate = str(request.get("candidate_text", "") or "").strip()
            if candidate:
                return candidate
        if final_text:
            return final_text
        asr = record.get("asr", {})
        return str(
            asr.get("corrected_text") or asr.get("final_text") or ""
        ).strip()

    def _update_llm_request_locked(
        self, interaction_id: str, request_id: int, updates: dict
    ) -> None:
        record = self._interaction_data(interaction_id)
        requests = record["llm"].setdefault("requests", [])
        request = next(
            (
                item
                for item in requests
                if int(item.get("request_id", 0)) == int(request_id)
            ),
            None,
        )
        if request is None:
            request = {"request_id": int(request_id)}
            requests.append(request)
        request.update(_json_value(updates))
        record["updated_at"] = _utc_now()
        self._write_json(self._interaction_path(interaction_id), record)

    def _append_event_locked(self, interaction_id: str, event: dict) -> None:
        path = self._interaction_dir(interaction_id) / "events.jsonl"
        rows = self._read_jsonl(path)
        rows.append(_json_value(event))
        self._write_jsonl(path, rows)

    @staticmethod
    def _runtime_evidence_fields(message: str) -> dict:
        summary = str(message).splitlines()[0].strip()
        stage_match = re.match(r"^(STAGE[12])\b", summary)
        fields: dict[str, object] = {}
        if stage_match:
            fields["stage"] = stage_match.group(1).lower()
        for key in ("sample", "score", "probability", "threshold"):
            match = re.search(
                rf"\b{key}=([+-]?(?:\d+(?:\.\d*)?|\.\d+))",
                summary,
            )
            if match is None:
                continue
            fields[key] = (
                int(float(match.group(1)))
                if key == "sample"
                else float(match.group(1))
            )
        decision = re.search(
            r"\b(ACTIVATE|REJECT|ACCEPT|TRIGGER|PASS)\b", summary
        )
        if decision is not None:
            fields["decision"] = decision.group(1).lower()
        return fields

    def _apply_runtime_evidence_locked(
        self, interaction_id: str, event: dict
    ) -> None:
        stage = str(event.get("stage", ""))
        if not stage:
            return
        record = self._interaction_data(interaction_id)
        near_field = record["near_field"]
        if event.get("score") is not None:
            near_field[f"{stage}_score"] = float(event["score"])
            if stage == "stage2":
                near_field["audio_score"] = float(event["score"])
        if event.get("threshold") is not None:
            near_field[f"{stage}_threshold"] = float(event["threshold"])
        if event.get("decision"):
            near_field["detector_decision"] = str(event["decision"])
        record["updated_at"] = _utc_now()
        self._write_json(self._interaction_path(interaction_id), record)

    @staticmethod
    def _llm_input(mode: str, raw_text: str, target_text: str) -> dict:
        from .text_processing.prompts import (
            DICTATION_PROMPT,
            EDIT_FRAGMENT_PROMPT,
            EDIT_FULL_TEXT_PROMPT,
            EDIT_TOOL_REQUIRED_PROMPT,
        )

        if str(mode) == "edit":
            user_content = (
                "<待修改文本>\n"
                f"{target_text}\n"
                "</待修改文本>\n\n"
                "<修改要求>\n"
                f"{raw_text}\n"
                "</修改要求>"
            )
            return {
                "instruction": raw_text,
                "target_text": target_text,
                "user_content": user_content,
                "system_prompts": {
                    "fragment": EDIT_FRAGMENT_PROMPT + EDIT_TOOL_REQUIRED_PROMPT,
                    "full": EDIT_FULL_TEXT_PROMPT + EDIT_TOOL_REQUIRED_PROMPT,
                },
            }
        return {
            "instruction": raw_text,
            "target_text": "",
            "user_content": raw_text,
            "system_prompt": DICTATION_PROMPT,
        }

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        return rows

    @staticmethod
    def _new_id(prefix: str) -> str:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S-%f")
        return f"{prefix}_{stamp}"

    def _prune_pending_locked(self) -> None:
        session_ids = sorted(set(self._pending_audio) | set(self._pending_asr))
        for session_id in session_ids[:-_MAX_PENDING_SESSIONS]:
            self._pending_audio.pop(session_id, None)
            self._pending_asr.pop(session_id, None)

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
