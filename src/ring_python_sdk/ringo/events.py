"""Common application event for firmware and host gesture recognition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GestureResult:
    """A confirmed action, with its source-specific result preserved in raw.

    timestamp_s is device uptime in seconds, unwrapped within a recognition run.
    Legacy firmware has no published score quantization, so confidence is None.
    """

    name: str
    source: Literal["firmware", "host"]
    timestamp_s: float
    confidence: float | None
    raw: object
