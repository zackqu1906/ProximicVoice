from __future__ import annotations

import math
from typing import Protocol

import numpy as np


class SessionSink(Protocol):
    """Generic consumer of a detected near-speech session.

    This interface is intentionally not ASR-specific.  A sink can stream to an
    ASR backend, save audio, update a UI, or submit the completed utterance to a
    batch worker.
    """

    def start(self, initial_audio_16k: np.ndarray) -> None: ...
    def feed(self, audio_16k: np.ndarray) -> None: ...
    def end(self, final_audio_16k: np.ndarray) -> None: ...
    def abort(self) -> None: ...
    def close(self) -> None: ...


class CompletedUtteranceSessionSink:
    """Adapt the old submit(full_utterance) sink to the session interface."""

    def __init__(self, sink) -> None:
        self.sink = sink

    def start(self, initial_audio_16k: np.ndarray) -> None:
        return None

    def feed(self, audio_16k: np.ndarray) -> None:
        return None

    def end(self, final_audio_16k: np.ndarray) -> None:
        x = np.asarray(final_audio_16k, dtype=np.float32).reshape(-1)
        if x.size:
            self.sink.submit(x)

    def abort(self) -> None:
        abort = getattr(self.sink, "abort", None)
        if callable(abort):
            abort()

    def close(self) -> None:
        self.sink.close()


class ASRInputGainSessionSink:
    """Apply gain only to the audio copy consumed by an ASR session sink."""

    def __init__(self, sink: SessionSink, *, gain_db: float) -> None:
        gain_db = float(gain_db)
        if not math.isfinite(gain_db):
            raise ValueError("gain_db must be finite")
        self.sink = sink
        self.gain_db = gain_db
        self.linear_gain = float(10.0 ** (gain_db / 20.0))

    def _apply(self, audio_16k: np.ndarray) -> np.ndarray:
        x = np.asarray(audio_16k, dtype=np.float32).reshape(-1)
        return np.clip(x * self.linear_gain, -1.0, 1.0).astype(
            np.float32,
            copy=False,
        )

    def start(self, initial_audio_16k: np.ndarray) -> None:
        self.sink.start(self._apply(initial_audio_16k))

    def feed(self, audio_16k: np.ndarray) -> None:
        self.sink.feed(self._apply(audio_16k))

    def end(self, final_audio_16k: np.ndarray) -> None:
        self.sink.end(self._apply(final_audio_16k))

    def abort(self) -> None:
        abort = getattr(self.sink, "abort", None)
        if callable(abort):
            abort()

    def close(self) -> None:
        self.sink.close()


class SessionFanout:
    """Fan one ProxiMic session out to independent session consumers."""

    def __init__(self, sinks: list[SessionSink]) -> None:
        if not sinks:
            raise ValueError("SessionFanout requires at least one sink")
        self.sinks = list(sinks)

    def start(self, initial_audio_16k: np.ndarray) -> None:
        for sink in self.sinks:
            sink.start(initial_audio_16k)

    def feed(self, audio_16k: np.ndarray) -> None:
        for sink in self.sinks:
            sink.feed(audio_16k)

    def end(self, final_audio_16k: np.ndarray) -> None:
        for sink in self.sinks:
            sink.end(final_audio_16k)

    def abort(self) -> None:
        for sink in self.sinks:
            abort = getattr(sink, "abort", None)
            if callable(abort):
                abort()

    def close(self) -> None:
        for sink in self.sinks:
            sink.close()


def ensure_session_sink(sink) -> SessionSink:
    """Keep old submit/close sinks source-compatible with the new controller."""

    if all(hasattr(sink, name) for name in ("start", "feed", "end", "close")):
        return sink
    if hasattr(sink, "submit") and hasattr(sink, "close"):
        return CompletedUtteranceSessionSink(sink)
    raise TypeError("sink must implement either start/feed/end/close or submit/close")
