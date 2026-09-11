"""Density model results carried by Swipe 0x26, separate from legacy logits."""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

from ring_python_sdk.core.constants import (
    CMD_SWIPE, SUBCMD_SWIPE_EVENT_V2, SUBCMD_SWIPE_TRIGGER_V2,
    SWIPE_EVENT_V2_PACKET_LEN, SWIPE_TRIGGER_V2_PACKET_LEN, SWIPE_GESTURE_IDS_V2,
)


@dataclass(frozen=True)
class SwipeEventV2:
    seq: int
    class_id: int
    probabilities: tuple[float, ...]
    uptime_ms: int

    @property
    def confidence(self) -> float:
        return self.probabilities[SWIPE_GESTURE_IDS_V2.index(self.class_id)]


@dataclass(frozen=True)
class SwipeTriggerV2(SwipeEventV2):
    center_uptime_ms: int
    event_mass: float


def _parse(data: bytes | bytearray, *, trigger: bool) -> SwipeEventV2 | SwipeTriggerV2 | None:
    size = SWIPE_TRIGGER_V2_PACKET_LEN if trigger else SWIPE_EVENT_V2_PACKET_LEN
    subcmd = SUBCMD_SWIPE_TRIGGER_V2 if trigger else SUBCMD_SWIPE_EVENT_V2
    if len(data) != size or data[0] != CMD_SWIPE or data[1] != subcmd:
        return None
    seq, class_id, *values = struct.unpack_from('<HB12fI', data, 2)
    probabilities = tuple(values[:12])
    if class_id not in SWIPE_GESTURE_IDS_V2 or any(
        not math.isfinite(p) or p < 0 or p > 1 for p in probabilities
    ) or abs(sum(probabilities) - 1) > 1e-4:
        return None
    args = (seq, class_id, probabilities, values[12])
    if trigger:
        center, mass = struct.unpack_from('<If', data, 57)
        if not math.isfinite(mass) or mass < 0 or class_id == 0:
            return None
        return SwipeTriggerV2(*args, center, mass)
    return SwipeEventV2(*args)


def parse_swipe_event_v2(data: bytes | bytearray) -> SwipeEventV2 | None:
    """Parse a 57-byte EVENT_V2; probabilities follow SWIPE_GESTURE_IDS_V2."""
    return _parse(data, trigger=False)


def parse_swipe_trigger_v2(data: bytes | bytearray) -> SwipeTriggerV2 | None:
    """Parse a 65-byte TRIGGER_V2 with fused probabilities and peak metadata."""
    return _parse(data, trigger=True)
