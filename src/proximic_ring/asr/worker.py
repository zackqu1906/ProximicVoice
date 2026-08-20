from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Protocol

import numpy as np

from .base import ASRBackend, ASRResult


class UtteranceSink(Protocol):
    """Consumer for a completed 16 kHz utterance."""

    def submit(self, audio_16k: np.ndarray) -> None: ...
    def close(self) -> None: ...


class ASRWorker:
    """Run one ASR backend away from the real-time Ring/detector loop."""

    sample_rate = 16_000

    def __init__(
        self,
        backend: ASRBackend,
        *,
        on_result: Callable[[ASRResult], None] | None = None,
        on_text: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] = print,
        max_pending: int = 4,
    ) -> None:
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self.backend = backend
        self.on_result = on_result
        self.on_text = on_text
        self.on_error = on_error
        self._queue: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=max_pending)
        self._abort_requested = threading.Event()
        thread_name = f"ASR-{getattr(backend, 'backend_name', type(backend).__name__)}"
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)
        self._thread.start()

    def submit(self, audio_16k: np.ndarray) -> None:
        if self._abort_requested.is_set():
            return
        x = np.asarray(audio_16k, dtype=np.float32).reshape(-1).copy()
        try:
            self._queue.put_nowait(x)
        except queue.Full:
            name = getattr(self.backend, "backend_name", type(self.backend).__name__)
            self.on_error(f"[ASR:{name}] queue full; dropped one completed utterance")

    def abort(self) -> None:
        self._abort_requested.set()

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join()

    def _run(self) -> None:
        while True:
            audio = self._queue.get()
            try:
                if audio is None:
                    return
                if self._abort_requested.is_set():
                    continue

                backend_name = str(getattr(self.backend, "backend_name", type(self.backend).__name__))
                model_name = str(getattr(self.backend, "model_name", "unknown"))
                duration_s = audio.size / self.sample_rate
                start = time.perf_counter()
                try:
                    text = str(self.backend.transcribe(audio) or "")
                    latency_s = time.perf_counter() - start
                    result = ASRResult(
                        backend=backend_name,
                        model=model_name,
                        text=text,
                        latency_s=latency_s,
                        audio_duration_s=duration_s,
                        sample_rate=self.sample_rate,
                    )
                    if self._abort_requested.is_set():
                        continue
                    if self.on_result is not None:
                        self.on_result(result)
                    if text and self.on_text is not None:
                        self.on_text(text)
                except BaseException as exc:
                    if self._abort_requested.is_set():
                        continue
                    latency_s = time.perf_counter() - start
                    result = ASRResult(
                        backend=backend_name,
                        model=model_name,
                        text="",
                        latency_s=latency_s,
                        audio_duration_s=duration_s,
                        sample_rate=self.sample_rate,
                        error=str(exc),
                    )
                    if self.on_result is not None:
                        self.on_result(result)
                    self.on_error(f"[ASR:{backend_name}] inference failed: {exc}")
            finally:
                self._queue.task_done()


class ASRFanout:
    """Send the exact same completed utterance to multiple independent workers.

    Each ASRWorker owns its own queue/thread, so a slow cloud backend does not
    block the Ring loop or another backend. This is useful for fair A/B testing.
    """

    def __init__(self, workers: list[ASRWorker]) -> None:
        if not workers:
            raise ValueError("ASRFanout requires at least one worker")
        self.workers = list(workers)

    def submit(self, audio_16k: np.ndarray) -> None:
        for worker in self.workers:
            worker.submit(audio_16k)

    def abort(self) -> None:
        for worker in self.workers:
            worker.abort()

    def close(self) -> None:
        for worker in self.workers:
            worker.close()
