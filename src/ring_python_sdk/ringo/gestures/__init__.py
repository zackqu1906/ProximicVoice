"""Host density subset of the ringo SDK; see VENDORED.json for provenance."""

from .decoder import (
    DecodeResult, EvidencePoolDecoder, PeakEvidence, StreamingDecoderConfig, StreamingEvent,
)
from .model import GESTURE_IDS, GESTURE_LABELS
from .runtime import GestureEvent, GestureRecognizer


def __getattr__(name):
    if name in ("SwipeDensityModel", "WindowInference"):
        from . import model
        return getattr(model, name)
    if name in ("SwipeDensityStream", "StreamInference"):
        from . import stream
        return getattr(stream, name)
    raise AttributeError(name)


__all__ = [
    "DecodeResult", "EvidencePoolDecoder", "PeakEvidence", "StreamingDecoderConfig",
    "StreamingEvent", "GESTURE_IDS", "GESTURE_LABELS", "GestureEvent", "GestureRecognizer",
    "SwipeDensityModel", "WindowInference", "SwipeDensityStream", "StreamInference",
]
