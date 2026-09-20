from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Any, Callable, Protocol

import numpy as np


_NO_ITEM = object()


@dataclass(frozen=True)
class _ASRSession:
    identifier: int
    cancelled: threading.Event


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
        on_state: Callable[[str], None] | None = None,
        context_provider: Callable[[], object | None] | None = None,
        on_context: Callable[[int, dict[str, Any]], None] | None = None,
    ) -> None:
        self.backend = backend
        self.on_update = on_update
        self.on_error = on_error
        self.on_state = on_state
        self.context_provider = context_provider
        self.on_context = on_context
        # Session length is bounded by the controller.  SimpleQueue keeps the
        # real-time producer non-blocking and preserves every audio block.
        self._queue: queue.SimpleQueue[tuple[str, np.ndarray, float, _ASRSession] | None] = queue.SimpleQueue()
        self._producer_lock = threading.Lock()
        self._producer_session: _ASRSession | None = None
        self._producer_session_id = 0
        self._current_session: _ASRSession | None = None
        self._backend_active = False
        self._session_samples = 0
        self._session_id = 0
        self._session_model_started_time_s: float | None = None
        self._session_failed = False
        self._session_error: str | None = None
        self._abort_requested = threading.Event()
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
        with self._producer_lock:
            self._producer_session_id += 1
            session = _ASRSession(self._producer_session_id, threading.Event())
            self._producer_session = session
            self._queue.put(("start", x, time.perf_counter(), session))

    def _enqueue(self, kind: str, audio: np.ndarray) -> None:
        with self._producer_lock:
            if self._producer_session is not None:
                self._queue.put((kind, audio, time.perf_counter(), self._producer_session))

    def feed(self, audio_16k: np.ndarray) -> None:
        x = np.asarray(audio_16k, dtype=np.float32).reshape(-1).copy()
        if x.size:
            self._enqueue("feed", x)

    def end(self, final_audio_16k: np.ndarray) -> None:
        x = np.asarray(final_audio_16k, dtype=np.float32).reshape(-1).copy()
        self._enqueue("end", x)

    def discard(self, captured_audio_16k: np.ndarray) -> None:
        """End only the live backend session without emitting a final result."""
        del captured_audio_16k
        self.cancel_pending()

    def cancel_pending(self) -> None:
        """Cancel the latest sentence even after its audio endpoint was queued."""
        with self._producer_lock:
            session = self._producer_session
            if session is None:
                return
            # Invalidate on the producer thread, not behind a slow connection
            # or inference. Every queued item carries this same event.
            session.cancelled.set()
            self._queue.put(("discard", np.empty(0, dtype=np.float32), time.perf_counter(), session))

    def abort(self) -> None:
        self._abort_requested.set()
        with self._producer_lock:
            if self._producer_session is not None:
                self._producer_session.cancelled.set()
        if self._current_session is not None:
            self._current_session.cancelled.set()

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
        session: _ASRSession | None = None,
    ) -> None:
        session = session or self._current_session
        if (self._abort_requested.is_set() or self.on_update is None
                or (session is not None and session.cancelled.is_set())):
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
                session_id=session.identifier if session else self._session_id,
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
        if self._abort_requested.is_set() or (self._current_session is not None and self._current_session.cancelled.is_set()):
            return
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
        session: _ASRSession | None = None,
    ) -> None:
        """Publish a receiver-thread partial against its originating packet."""

        emitted_at = time.perf_counter()
        self._emit(
            text,
            is_final=False,
            latency_s=max(0.0, emitted_at - chunk_ready_time_s),
            duration_s=audio_duration_s,
            chunk_ready_time_s=chunk_ready_time_s,
            session=session,
        )

    def _mark_backend_chunk_ready(self, chunk_ready_time_s: float) -> None:
        marker = getattr(self.backend, "mark_chunk_ready", None)
        if callable(marker):
            marker(chunk_ready_time_s)

    def _report_timing(self, message: str) -> None:
        """Publish diagnostics without allowing log failures to stop ASR."""

        if self.on_state is None:
            return
        try:
            self.on_state(message)
        except Exception:
            return

    def _prepare_session_context(self) -> None:
        """Bind optional per-utterance context without making ASR depend on it."""

        setter = getattr(self.backend, "set_session_context", None)
        if not callable(setter):
            return
        context: object | None = None
        if self.context_provider is not None:
            try:
                context = self.context_provider()
            except BaseException as exc:
                context = {
                    "status": "unavailable",
                    "source": "focused_text",
                    "reason": f"provider_error:{type(exc).__name__}",
                }
        try:
            setter(context)
        except BaseException as exc:
            # Context is an accuracy hint.  A malformed or unreadable context
            # must never prevent the audio stream from starting.
            try:
                setter(
                    {
                        "status": "unavailable",
                        "source": "focused_text",
                        "reason": f"prepare_error:{type(exc).__name__}",
                    }
                )
            except BaseException:
                # A third-party backend may expose an incompatible method.
                # Keep recognition functional and omit context diagnostics.
                return

    def _publish_session_context(self) -> None:
        metadata = getattr(self.backend, "session_context_metadata", None)
        if callable(metadata):
            metadata = metadata()
        if not isinstance(metadata, dict):
            return
        snapshot = dict(metadata)
        status = str(snapshot.get("status", "unknown") or "unknown")
        source = str(snapshot.get("source", "unknown") or "unknown")
        fields = (
            f"[ASR CONTEXT] session={self._session_id} "
            f"backend={getattr(self.backend, 'backend_name', type(self.backend).__name__)} "
            f"status={status} source={source} "
            f"items={int(snapshot.get('item_count', 0) or 0)} "
            f"chars={int(snapshot.get('sent_char_count', 0) or 0)} "
            f"source_chars={int(snapshot.get('source_char_count', 0) or 0)} "
            f"truncated={'true' if bool(snapshot.get('truncated')) else 'false'}"
        )
        reason = str(snapshot.get("reason", "") or "")
        if reason:
            fields += f" reason={reason}"
        read_method = str(snapshot.get("read_method", "") or "")
        if read_method:
            fields += f" method={read_method}"
        application = str(snapshot.get("application", "") or "")
        if application:
            fields += f" app={application!r}"
        target_key = str(snapshot.get("target_key", "") or "")
        if target_key:
            fields += f" target={target_key}"
        self._report_timing(fields)
        if self.on_context is not None:
            try:
                self.on_context(self._session_id, snapshot)
            except Exception:
                # Dataset/log observers cannot break live recognition.
                pass

    def _run(self) -> None:
        deferred_item: tuple[str, np.ndarray, float, _ASRSession] | None | object = _NO_ITEM
        while True:
            if deferred_item is _NO_ITEM:
                item = self._queue.get()
            else:
                item = deferred_item
                deferred_item = _NO_ITEM
            if item is None:
                if self._abort_requested.is_set():
                    self._abort_backend_session()
                return
            if self._abort_requested.is_set():
                continue
            kind, audio, chunk_ready_time_s, session = item
            if session.cancelled.is_set():
                if self._current_session is session:
                    self._abort_backend_session()
                if kind == "start":
                    self._report_timing(f"[ASR TIMING] session={session.identifier} 已取消，跳过尚未开始的识别")
                continue

            # A cumulative local model can take longer than real time once an
            # utterance grows.  In that case many tiny 20 ms feed messages may
            # queue up while one partial is being decoded.  Collapse the
            # already-pending consecutive feeds so the next inference jumps to
            # the newest audio instead of re-decoding every stale 720 ms
            # checkpoint.  START / END boundaries remain strictly ordered.
            if kind == "feed":
                pending_audio = [audio]
                while True:
                    try:
                        next_item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if next_item is None or next_item[0] != "feed" or next_item[3] is not session:
                        deferred_item = next_item
                        break
                    pending_audio.append(next_item[1])
                    chunk_ready_time_s = next_item[2]
                if len(pending_audio) > 1:
                    audio = np.concatenate(pending_audio).astype(
                        np.float32, copy=False
                    )
            try:
                if kind == "start":
                    self._current_session = session
                    # Pair with abort() reading _current_session: whichever
                    # thread wins, an in-flight start receives cancellation.
                    if self._abort_requested.is_set():
                        session.cancelled.set()
                        continue
                    self._session_id = session.identifier
                    self._session_failed = False
                    self._session_error = None
                    set_cancel_event = getattr(self.backend, "set_cancel_event", None)
                    if callable(set_cancel_event):
                        set_cancel_event(session.cancelled)
                    set_partial_callback = getattr(self.backend, "set_partial_callback", None)
                    if callable(set_partial_callback):
                        set_partial_callback(lambda text, ready, duration, origin=session:
                                             self._on_async_partial(text, ready, duration, origin))
                    self._prepare_session_context()
                    if session.cancelled.is_set():
                        continue
                    model_started_time_s = time.perf_counter()
                    self._session_model_started_time_s = model_started_time_s
                    self._report_timing(
                        f"[ASR TIMING] session={self._session_id} "
                        f"ASR模型开始接收音频 queue="
                        f"{max(0.0, model_started_time_s - chunk_ready_time_s) * 1000:.1f}ms "
                        f"initial_audio={audio.size / self.sample_rate:.3f}s"
                    )
                    try:
                        self._backend_active = True
                        self.backend.start()
                    finally:
                        self._publish_session_context()
                    if session.cancelled.is_set():
                        self._abort_backend_session()
                        continue
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
                    if session.cancelled.is_set():
                        self._abort_backend_session()
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
                        # The producer closes recognition at the utterance
                        # endpoint. Publish a terminal update even though the
                        # backend failed earlier, otherwise the UI never gets
                        # the completion signal that reopens that gate.
                        self._emit_error(
                            RuntimeError(
                                self._session_error
                                or "streaming ASR session failed before finalization"
                            ),
                            is_final=True,
                            latency_s=max(
                                0.0, time.perf_counter() - chunk_ready_time_s
                            ),
                            chunk_ready_time_s=chunk_ready_time_s,
                        )
                        self._session_samples = 0
                        self._session_failed = False
                        self._session_error = None
                        self._session_model_started_time_s = None
                        continue
                    # The controller passes its trimmed final utterance here.
                    # A backend may re-decode it to correct provisional text
                    # that saw reject-confirmation tail audio.
                    self._session_samples = int(audio.size)
                    self._mark_backend_chunk_ready(chunk_ready_time_s)
                    final_started_time_s = time.perf_counter()
                    self._report_timing(
                        f"[ASR TIMING] session={self._session_id} "
                        f"ASR最终推理开始 queue="
                        f"{max(0.0, final_started_time_s - chunk_ready_time_s) * 1000:.1f}ms "
                        f"audio={self._session_samples / self.sample_rate:.3f}s"
                    )
                    text = self.backend.finish(audio)
                    self._backend_active = False
                    finished_time_s = time.perf_counter()
                    latency = finished_time_s - chunk_ready_time_s
                    total_s = (
                        0.0
                        if self._session_model_started_time_s is None
                        else max(
                            0.0,
                            finished_time_s - self._session_model_started_time_s,
                        )
                    )
                    self._report_timing(
                        f"[ASR TIMING] session={self._session_id} ASR模型结束 "
                        f"final_inference="
                        f"{max(0.0, finished_time_s - final_started_time_s) * 1000:.1f}ms "
                        f"since_model_start={total_s:.3f}s"
                    )
                    self._emit(
                        text,
                        is_final=True,
                        latency_s=latency,
                        duration_s=self._session_samples / self.sample_rate,
                        chunk_ready_time_s=chunk_ready_time_s,
                    )
                    self._session_samples = 0
                    self._session_model_started_time_s = None
                    self._session_error = None
                elif kind == "discard":
                    self._abort_backend_session()
                else:  # pragma: no cover - internal invariant
                    raise RuntimeError(f"Unknown streaming ASR worker message: {kind}")
            except BaseException as exc:
                if session.cancelled.is_set() or self._abort_requested.is_set():
                    self._abort_backend_session()
                    continue
                latency = time.perf_counter() - chunk_ready_time_s
                self._emit_error(
                    exc,
                    is_final=(kind == "end"),
                    latency_s=latency,
                    chunk_ready_time_s=chunk_ready_time_s,
                )
                if kind in {"start", "feed"}:
                    # A failed connect or a lost live stream cannot recover
                    # from later feed/end messages.  Close the broken backend,
                    # keep the first useful error, and drop the rest of this
                    # session.  The next START will establish a fresh stream.
                    self._session_failed = True
                    self._session_error = str(exc)
                    abort_backend = getattr(self.backend, "abort", None)
                    if callable(abort_backend):
                        try:
                            abort_backend()
                        except Exception:
                            # Cleanup must not replace the original connection
                            # error that was already reported above.
                            pass
                    self._backend_active = False
                elif kind == "end":
                    self._session_samples = 0
                    self._session_failed = False
                    self._session_error = None
                    self._session_model_started_time_s = None

    def _abort_backend_session(self) -> None:
        if self._backend_active:
            self._backend_active = False
            abort_backend = getattr(self.backend, "abort", None)
            if callable(abort_backend):
                try:
                    abort_backend()
                except Exception:
                    pass
        self._session_samples = 0
        self._session_failed = False
        self._session_error = None
        self._session_model_started_time_s = None
