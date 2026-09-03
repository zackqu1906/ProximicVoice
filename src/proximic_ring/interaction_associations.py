"""UI-independent association actions and recent-failure recommendations.

The coordinator deliberately knows nothing about Qt, buttons, or gestures.
Every input surface dispatches the same string actions to ``ActionRouter``;
future Ring gestures can therefore reuse the exact controller commands.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import time
import uuid
from typing import Callable


ASSOCIATION_ASR = "asr"
ASSOCIATION_LLM = "llm"
ASR_DICTATION_RETRY = "dictation_retry"
ASR_INSTRUCTION_RETRY = "instruction_retry"
_ASR_UNCLASSIFIED_RETRY = "unclassified_retry"


@dataclass(frozen=True)
class AssociationMember:
    interaction_id: str
    session_id: int
    request_id: int
    mode: str
    target_key: str
    asr_text: str = ""
    result_text: str = ""
    status: str = ""
    audio_path: str = ""
    created_at: str = ""
    result_id: str = ""
    target_x: int = 0
    target_y: int = 0
    target_width: int = 0
    target_height: int = 0
    occurred_monotonic: float = field(default_factory=time.monotonic)

    @property
    def has_llm_result(self) -> bool:
        return self.request_id > 0 and bool(self.result_text.strip())

    def ui_entry(self, role: str) -> dict[str, object]:
        return {
            "interactionId": self.interaction_id,
            "sessionId": self.session_id,
            "requestId": self.request_id,
            "resultId": self.result_id,
            "role": role,
            "roleLabel": "正例" if role == "chosen" else "反例",
            "mode": self.mode,
            "asrText": self.asr_text or "（未识别出文本）",
            "resultText": self.result_text,
            "status": self.status,
            "audioPath": self.audio_path,
            "createdAt": self.created_at,
        }


@dataclass(frozen=True)
class AssociationRecommendation:
    recommendation_id: str
    kind: str
    subtype: str
    chosen: AssociationMember
    rejected: tuple[AssociationMember, ...]
    relation_type: str = ""

    @property
    def title(self) -> str:
        count = len(self.rejected)
        if self.kind == ASSOCIATION_LLM:
            return f"发现 {count} 个可关联的失败编辑"
        noun = "听写" if self.subtype == ASR_DICTATION_RETRY else "指令重试"
        return f"发现 {count} 条可关联的{noun}"

    @property
    def positive_label(self) -> str:
        if self.kind == ASSOCIATION_LLM:
            return "正确结果"
        return "正确文本" if self.subtype == ASR_DICTATION_RETRY else "成功指令"

    @property
    def positive_text(self) -> str:
        if self.kind == ASSOCIATION_LLM:
            return self.chosen.result_text
        return self.chosen.asr_text or self.chosen.result_text

    def ui_entries(self) -> list[dict[str, object]]:
        return [self.chosen.ui_entry("chosen"), *(
            item.ui_entry("rejected") for item in self.rejected
        )]


class RecentFailureCoordinator:
    """Recommend failures since the previous success for one target/mode.

    A success is the only boundary.  The coordinator keeps at most ``limit``
    recent failures and discards stale or cross-target chains.
    """

    def __init__(self, *, limit: int = 5, max_age_s: float = 60.0) -> None:
        self.limit = max(1, int(limit))
        self.max_age_s = max(1.0, float(max_age_s))
        self._asr_failures: dict[tuple[str, str], deque[AssociationMember]] = {}
        self._llm_failures: dict[str, deque[AssociationMember]] = {}
        self._active_context: tuple[str, str] | None = None

    def clear(self) -> None:
        self._asr_failures.clear()
        self._llm_failures.clear()
        self._active_context = None

    def record_failure(self, member: AssociationMember) -> None:
        if not member.interaction_id or not member.target_key:
            return
        self._activate(member)
        subtype = (
            ASR_INSTRUCTION_RETRY
            if member.mode == "edit"
            else ASR_DICTATION_RETRY
        )
        asr_lane = subtype if member.asr_text.strip() else _ASR_UNCLASSIFIED_RETRY
        self._append(self._asr_failures, (member.target_key, asr_lane), member)
        if member.mode == "edit" and member.has_llm_result:
            self._append(self._llm_failures, member.target_key, member)

    def record_success(
        self, member: AssociationMember
    ) -> list[AssociationRecommendation]:
        if not member.interaction_id or not member.target_key:
            return []
        self._activate(member)
        recommendations: list[AssociationRecommendation] = []
        subtype = (
            ASR_INSTRUCTION_RETRY
            if member.mode == "edit"
            else ASR_DICTATION_RETRY
        )
        asr_failures = self._take_asr_failures(member, subtype)
        if asr_failures:
            exact = all(
                not item.asr_text.strip()
                or item.asr_text.strip() == member.asr_text.strip()
                for item in asr_failures
            )
            recommendations.append(
                AssociationRecommendation(
                    recommendation_id=f"recommend_{uuid.uuid4().hex}",
                    kind=ASSOCIATION_ASR,
                    subtype=subtype,
                    chosen=member,
                    rejected=tuple(asr_failures),
                    relation_type=(
                        "probable_exact_repeat" if exact else "same_intent_retry"
                    ),
                )
            )
        if member.mode == "edit" and member.has_llm_result:
            llm_failures = self._take(
                self._llm_failures, member.target_key, member
            )
            if llm_failures:
                recommendations.append(
                    AssociationRecommendation(
                        recommendation_id=f"recommend_{uuid.uuid4().hex}",
                        kind=ASSOCIATION_LLM,
                        subtype="edit_preference",
                        chosen=member,
                        rejected=tuple(llm_failures),
                    )
                )
        return recommendations

    def record_manual_success(
        self, member: AssociationMember
    ) -> list[AssociationRecommendation]:
        """Resolve only the lane for which typed text is a valid positive."""
        if not member.interaction_id or not member.target_key:
            return []
        self._activate(member)
        if member.mode == "edit":
            failures = self._take(self._llm_failures, member.target_key, member)
            if not failures:
                return []
            return [
                AssociationRecommendation(
                    recommendation_id=f"recommend_{uuid.uuid4().hex}",
                    kind=ASSOCIATION_LLM,
                    subtype="edit_preference",
                    chosen=member,
                    rejected=tuple(failures),
                )
            ]
        failures = self._take_asr_failures(member, ASR_DICTATION_RETRY)
        if not failures:
            return []
        return [
            AssociationRecommendation(
                recommendation_id=f"recommend_{uuid.uuid4().hex}",
                kind=ASSOCIATION_ASR,
                subtype=ASR_DICTATION_RETRY,
                chosen=member,
                rejected=tuple(failures),
                relation_type="same_intent_retry",
            )
        ]

    def restore_failures(
        self, recommendations: list[AssociationRecommendation]
    ) -> None:
        """Restore a provisional success chain when its result is undone."""
        by_interaction: dict[str, AssociationMember] = {}
        for recommendation in recommendations:
            for member in recommendation.rejected:
                by_interaction[member.interaction_id] = member
        for member in sorted(
            by_interaction.values(), key=lambda item: item.occurred_monotonic
        ):
            self.record_failure(member)

    def _append(self, mapping: dict, key, member: AssociationMember) -> None:
        queue = mapping.setdefault(key, deque(maxlen=self.limit))
        self._prune(queue, member.occurred_monotonic)
        if not queue or queue[-1].interaction_id != member.interaction_id:
            queue.append(member)

    def _activate(self, member: AssociationMember) -> None:
        context = (member.target_key, member.mode)
        if self._active_context is not None and self._active_context != context:
            preserve_unclassified = (
                self._active_context[0] == member.target_key
                and self._asr_failures.get(
                    (member.target_key, _ASR_UNCLASSIFIED_RETRY)
                )
            )
            preserved = deque(
                preserve_unclassified or (), maxlen=self.limit
            )
            self._asr_failures.clear()
            self._llm_failures.clear()
            if preserved:
                self._asr_failures[
                    (member.target_key, _ASR_UNCLASSIFIED_RETRY)
                ] = preserved
        self._active_context = context

    def _take_asr_failures(
        self, member: AssociationMember, subtype: str
    ) -> list[AssociationMember]:
        failures = [
            *self._take(
                self._asr_failures, (member.target_key, subtype), member
            ),
            *self._take(
                self._asr_failures,
                (member.target_key, _ASR_UNCLASSIFIED_RETRY),
                member,
            ),
        ]
        unique = {item.interaction_id: item for item in failures}
        return sorted(
            unique.values(), key=lambda item: item.occurred_monotonic
        )[-self.limit :]

    def _take(self, mapping: dict, key, success: AssociationMember) -> list:
        queue = mapping.pop(key, deque())
        self._prune(queue, success.occurred_monotonic)
        return list(queue)[-self.limit :]

    def _prune(self, queue: deque, now: float) -> None:
        while queue and now - queue[0].occurred_monotonic > self.max_age_s:
            queue.popleft()


class AssociationActionRouter:
    """Small command boundary shared by QML buttons and future gestures."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[str], None]] = {}

    def register(self, action: str, handler: Callable[[str], None]) -> None:
        self._handlers[str(action)] = handler

    def dispatch(self, action: str, payload: str = "") -> bool:
        handler = self._handlers.get(str(action))
        if handler is None:
            return False
        handler(str(payload))
        return True
