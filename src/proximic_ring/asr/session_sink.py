from __future__ import annotations

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
