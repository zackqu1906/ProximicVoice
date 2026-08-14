from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Callable, Protocol

import numpy as np


class StreamingASRBackend(Protocol):
    """Backend contract for partial/final ASR without coupling session logic to a model.

    ``start`` begins a new utterance, ``feed`` consumes newly-arrived 16 kHz
    audio and may return a revised partial transcript, and ``finish`` returns
    the final transcript.  A backend can implement true cache-aware streaming,
    cumulative re-decoding, or a remote streaming API behind this same shape.
    """

    backend_name: str
    model_name: str

    def start(self) -> None: ...
    def feed(self, audio_16k: np.ndarray) -> str | None: ...
    def finish(self, final_audio_16k: np.ndarray) -> str: ...


@dataclass(frozen=True)
class StreamingASRUpdate:
    backend: str
    model: str
    text: str
    is_final: bool
    latency_s: float
    audio_duration_s: float
    sample_rate: int = 16_000
    error: str | None = None
    # perf_counter() value captured when this input block was fully prepared
    # by the producer.  The console uses it at the actual print site so the
    # displayed latency includes worker queueing and output-side processing.
    chunk_ready_time_s: float | None = None
    # Monotonic per-worker session identifier.  Consumers use this to revise
    # partial text and commit one final result without relying on text equality.
    session_id: int = 0


class StreamingASRWorker:
    """Run a streaming ASR backend away from the Ring/detector callback thread.

    The session controller only emits START / new-audio / END messages.  All
    model inference happens on this worker thread, so a slow partial decode does
    not block BLE reads or ProxiMic inference.
    """

    sample_rate = 16_000

    def __init__(
        self,
        backend: StreamingASRBackend,
        *,
        on_update: Callable[[StreamingASRUpdate], None] | None = None,
        on_error: Callable[[str], None] = print,
    ) -> None:
        self.backend = backend
        self.on_update = on_update
        self.on_error = on_error
        # Session length is bounded by the controller.  SimpleQueue keeps the
        # real-time producer non-blocking and preserves every audio block.
        self._queue: queue.SimpleQueue[tuple[str, np.ndarray, float] | None] = queue.SimpleQueue()
        self._session_samples = 0
        self._session_id = 0
        self._session_failed = False
        name = getattr(backend, "backend_name", type(backend).__name__)
        set_partial_callback = getattr(backend, "set_partial_callback", None)
        if callable(set_partial_callback):
            # A native remote stream receives transcripts on its receiver
            # thread.  Let it publish there instead of waiting for a later
            # feed() call merely to drain a result queue.
            set_partial_callback(self._on_async_partial)
        self._thread = threading.Thread(target=self._run, name=f"StreamingASR-{name}", daemon=True)
        self._thread.start()

    # SessionSink-compatible API -------------------------------------------------
    def start(self, initial_audio_16k: np.ndarray) -> None:
        x = np.asarray(initial_audio_16k, dtype=np.float32).reshape(-1).copy()
        self._queue.put(("start", x, time.perf_counter()))

    def feed(self, audio_16k: np.ndarray) -> None:
        x = np.asarray(audio_16k, dtype=np.float32).reshape(-1).copy()
        if x.size:
            self._queue.put(("feed", x, time.perf_counter()))

    def end(self, final_audio_16k: np.ndarray) -> None:
        x = np.asarray(final_audio_16k, dtype=np.float32).reshape(-1).copy()
        self._queue.put(("end", x, time.perf_counter()))

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join()

    # Worker ---------------------------------------------------------------------
    def _emit(
        self,
        text: str,
        *,
        is_final: bool,
        latency_s: float,
        duration_s: float,
        chunk_ready_time_s: float,
    ) -> None:
        if self.on_update is None:
            return
        self.on_update(
            StreamingASRUpdate(
                backend=str(getattr(self.backend, "backend_name", type(self.backend).__name__)),
                model=str(getattr(self.backend, "model_name", "unknown")),
                text=str(text or ""),
                is_final=is_final,
                latency_s=latency_s,
                audio_duration_s=duration_s,
                sample_rate=self.sample_rate,
                chunk_ready_time_s=chunk_ready_time_s,
                session_id=self._session_id,
            )
        )

    def _emit_error(
        self,
        exc: BaseException,
        *,
        is_final: bool,
        latency_s: float,
        chunk_ready_time_s: float,
    ) -> None:
        name = str(getattr(self.backend, "backend_name", type(self.backend).__name__))
        message = str(exc)
        if self.on_update is not None:
            self.on_update(
                StreamingASRUpdate(
                    backend=name,
                    model=str(getattr(self.backend, "model_name", "unknown")),
                    text="",
                    is_final=is_final,
                    latency_s=latency_s,
                    audio_duration_s=self._session_samples / self.sample_rate,
                    sample_rate=self.sample_rate,
                    error=message,
                    chunk_ready_time_s=chunk_ready_time_s,
                    session_id=self._session_id,
                )
            )
        self.on_error(f"[ASR:{name}] streaming inference failed: {message}")

    def _on_async_partial(
        self,
        text: str,
        chunk_ready_time_s: float,
        audio_duration_s: float,
    ) -> None:
        """Publish a receiver-thread partial against its originating packet."""

        emitted_at = time.perf_counter()
        self._emit(
            text,
            is_final=False,
            latency_s=max(0.0, emitted_at - chunk_ready_time_s),
            duration_s=audio_duration_s,
            chunk_ready_time_s=chunk_ready_time_s,
        )

    def _mark_backend_chunk_ready(self, chunk_ready_time_s: float) -> None:
        marker = getattr(self.backend, "mark_chunk_ready", None)
        if callable(marker):
            marker(chunk_ready_time_s)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            kind, audio, chunk_ready_time_s = item
            try:
                if kind == "start":
                    self._session_id += 1
                    self._session_failed = False
                    self.backend.start()
                    self._mark_backend_chunk_ready(chunk_ready_time_s)
                    self._session_samples = int(audio.size)
                    text = self.backend.feed(audio) if audio.size else None
                    latency = time.perf_counter() - chunk_ready_time_s
                    if text:
                        self._emit(
                            text,
                            is_final=False,
                            latency_s=latency,
                            duration_s=self._session_samples / self.sample_rate,
                            chunk_ready_time_s=chunk_ready_time_s,
                        )
                elif kind == "feed":
                    if self._session_failed:
                        continue
                    self._session_samples += int(audio.size)
                    self._mark_backend_chunk_ready(chunk_ready_time_s)
                    text = self.backend.feed(audio)
                    latency = time.perf_counter() - chunk_ready_time_s
                    if text:
                        self._emit(
                            text,
                            is_final=False,
                            latency_s=latency,
                            duration_s=self._session_samples / self.sample_rate,
                            chunk_ready_time_s=chunk_ready_time_s,
                        )
                elif kind == "end":
                    if self._session_failed:
                        self._session_samples = 0
                        self._session_failed = False
                        continue
                    # The controller passes its trimmed final utterance here.
                    # A backend may re-decode it to correct provisional text
                    # that saw reject-confirmation tail audio.
                    self._session_samples = int(audio.size)
                    self._mark_backend_chunk_ready(chunk_ready_time_s)
                    text = self.backend.finish(audio)
                    latency = time.perf_counter() - chunk_ready_time_s
                    self._emit(
                        text,
                        is_final=True,
                        latency_s=latency,
                        duration_s=self._session_samples / self.sample_rate,
                        chunk_ready_time_s=chunk_ready_time_s,
                    )
                    self._session_samples = 0
                else:  # pragma: no cover - internal invariant
                    raise RuntimeError(f"Unknown streaming ASR worker message: {kind}")
            except BaseException as exc:
                latency = time.perf_counter() - chunk_ready_time_s
                self._emit_error(
                    exc,
                    is_final=(kind == "end"),
                    latency_s=latency,
                    chunk_ready_time_s=chunk_ready_time_s,
                )
                if kind == "start":
                    # A failed connect/session initialization cannot recover
                    # from feed/end.  Keep the first, useful error and drop
                    # this session's later events rather than emitting a
                    # misleading "session was not started" for every block.
                    self._session_failed = True
                elif kind == "end":
                    self._session_samples = 0
                    self._session_failed = False
