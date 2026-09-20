"""Output-only SDK event mapping around the original density stream."""
from dataclasses import dataclass

from .decoder import StreamingDecoderConfig


@dataclass(frozen=True)
class GestureEvent:
    """SDK event retaining the original result in raw.

    Density's original decoder provides confidence but no probability vector;
    probabilities is empty for these events. Window probabilities remain in
    last_inference.window. Window/pinch events use their native result clock.
    """
    class_id: int
    name: str
    confidence: float
    timestamp_s: float
    probabilities: tuple[float, ...]
    class_ids: tuple[int, ...]
    center_frame: int | None = None
    emitted_after_frame: int | None = None
    event_mass: float | None = None
    segment_id: int = 0
    raw: object | None = None

    @property
    def latency_frames(self):
        if self.center_frame is None or self.emitted_after_frame is None:
            return None
        return self.emitted_after_frame - self.center_frame


class GestureRecognizer:
    def __init__(self, *, model=None, decoder_config=None, on_gesture=None):
        from .model import SwipeDensityModel
        from .stream import SwipeDensityStream
        self.stream = SwipeDensityStream(model if model is not None else SwipeDensityModel(),
                                         decoder_config or StreamingDecoderConfig())
        self.on_gesture = on_gesture
        self.last_inference = None

    def reset(self):
        self.stream.reset()
        self.last_inference = None

    def feed(self, frame, timestamp_s):
        inference = self.stream.push_frame(frame, timestamp_s)
        if inference is None:
            if self.stream.inference_count == 0:
                self.last_inference = None
            return ()
        self.last_inference = inference
        model = self.stream.model
        labels = dict(zip(model.gesture_ids, model.gesture_labels))
        events = tuple(
            GestureEvent(event.label, labels[event.label], event.class_confidence,
                         timestamp_s, (), model.gesture_ids, event.center,
                         event.emitted_after_frame, event.event_mass, inference.segment_id, event)
            for event in inference.decode.events
        )
        if self.on_gesture is not None:
            for event in events:
                self.on_gesture(event)
        return events
