"""Device-side gesture results; no host inference or application actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ring_python_sdk.core.constants import (
    SWIPE_CLASS_LABELS_V2,
    SWIPE_EVENT_MAP,
    SWIPE_GESTURE_IDS_V2,
)


@dataclass(frozen=True)
class SwipeResult:
    """One firmware EVENT (classification) or TRIGGER (recognized action).

    Legacy V1 carries signed model scores, not probabilities: confidence is
    deliberately None. V2 probability positions follow SWIPE_GESTURE_IDS_V2,
    not class_id. Device timestamps are uint32 milliseconds and can wrap.
    Callbacks receive every valid packet, including duplicates; no host
    threshold, debounce, or filtering changes the firmware's observations.
    """

    protocol_version: int
    kind: Literal["event", "trigger"]
    seq: int
    class_id: int
    uptime_ms: int
    scores: tuple[int, ...] = ()
    probabilities: tuple[float, ...] = ()
    center_uptime_ms: int | None = None
    event_mass: float | None = None

    @property
    def name(self) -> str:
        if self.protocol_version == 2:
            return SWIPE_CLASS_LABELS_V2.get(self.class_id, f"class-{self.class_id}")
        if self.class_id == 0:
            return "empty"
        return SWIPE_EVENT_MAP.get(self.class_id, f"class-{self.class_id}")

    @property
    def confidence(self) -> float | None:
        if not self.probabilities:
            return None
        return self.probabilities[SWIPE_GESTURE_IDS_V2.index(self.class_id)]
