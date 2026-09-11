"""Serial host inference outside the BLE notification thread."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import queue
import threading
import time

from ring_python_sdk.imu.processor import ImuSample
from .runtime import GestureRecognizer
from .classifier import GESTURE_NAMES


@dataclass
class GestureWorkerStats:
    received_samples: int = 0
    processed_samples: int = 0
    dropped_samples: int = 0
    last_queue_wait_ms: float = 0.0
    max_queue_wait_ms: float = 0.0
    last_processing_ms: float = 0.0
    max_processing_ms: float = 0.0


class GestureWorker:
    """Bounded, nonblocking IMU producer with a single recognizer consumer.

    Start before imu_on(on_sample=worker.submit); stop IMU before close().
    Recognizer callbacks run on this worker, so GUI/IO consumers should enqueue
    their results. Overflow discards queued stale samples; the recognizer sees
    the resulting sample gap and resets its window. Counters expose all drops.
    A worker is single-use. Errors are retained in error and must be checked by
    the owner. No keyboard or application actions are dispatched here.
    """

    def __init__(self, recognizer: GestureRecognizer, *, queue_capacity: int = 200):
        if not isinstance(queue_capacity, int) or queue_capacity <= 0:
            raise ValueError("queue_capacity must be a positive integer")
        self.recognizer = recognizer
        self._queue: queue.Queue[tuple[ImuSample, float]] = queue.Queue(queue_capacity)
        self._stats = GestureWorkerStats()
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._started = False
        self._last_received_at: float | None = None
        self.error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="RingHostGestures", daemon=True)

    def start(self) -> None:
        if self._started or self._closing.is_set():
            raise RuntimeError("GestureWorker is single-use")
        self._started = True
        self._thread.start()

    def submit(self, sample: ImuSample) -> None:
        """Lightweight callback for RingSession.imu_on; never runs inference."""
        with self._lock:
            if not self._started or self._closing.is_set() or self.error is not None:
                return
            self._stats.received_samples += 1
            self._last_received_at = time.monotonic()
            if self._queue.full():
                self._discard_pending()
            self._queue.put_nowait((sample, time.monotonic()))

    def _discard_pending(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._stats.dropped_samples += 1
            except queue.Empty:
                return

    def _run(self) -> None:
        try:
            while not self._closing.is_set() or not self._queue.empty():
                try:
                    sample, received_at = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                started = time.monotonic()
                self.recognizer.on_sample(sample)
                elapsed = (time.monotonic() - started) * 1000
                wait_ms = (started - received_at) * 1000
                with self._lock:
                    self._stats.processed_samples += 1
                    self._stats.last_queue_wait_ms = wait_ms
                    self._stats.max_queue_wait_ms = max(self._stats.max_queue_wait_ms, wait_ms)
                    self._stats.last_processing_ms = elapsed
                    self._stats.max_processing_ms = max(self._stats.max_processing_ms, elapsed)
        except Exception as exc:
            with self._lock:
                self.error = exc
                self._discard_pending()

    def snapshot(self) -> dict:
        with self._lock:
            result = asdict(self._stats)
            last_received_at = self._last_received_at
        prediction = getattr(self.recognizer, "last_prediction", None)
        prediction_counts = list(getattr(self.recognizer, "prediction_counts", ()))
        gesture_counts = list(getattr(self.recognizer, "gesture_counts", ()))
        result.update(
            queue_depth=self._queue.qsize(),
            prediction_count=self.recognizer.prediction_count,
            window_resets=max(0, self.recognizer.reset_count - 1),
            timestamp_jitter_count=getattr(self.recognizer, "timestamp_jitter_count", 0),
            max_timestamp_jitter_ms=getattr(self.recognizer, "max_timestamp_jitter_ms", 0.0),
            nonempty_predictions=sum(prediction_counts[1:]),
            prediction_counts=dict(zip(GESTURE_NAMES, prediction_counts)),
            gesture_count=sum(gesture_counts),
            gesture_counts=dict(zip(GESTURE_NAMES, gesture_counts)),
            last_prediction=getattr(prediction, "name", None),
            last_confidence=getattr(prediction, "confidence", None),
            last_input_age_ms=(
                (time.monotonic() - last_received_at) * 1000
                if last_received_at is not None else None
            ),
            error=str(self.error) if self.error is not None else None,
        )
        return result

    def close(self, *, timeout_s: float = 5.0) -> None:
        """Refuse new samples, drain a bounded backlog, and join the consumer."""
        with self._lock:
            self._closing.set()
        if self._started:
            if threading.current_thread() is self._thread:
                raise RuntimeError("Close GestureWorker from its owner, not its callback")
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                raise TimeoutError("Gesture worker did not finish within the shutdown timeout")
