"""Thread-safe terminal formatting for concurrent streaming ASR comparisons."""

from __future__ import annotations

import threading
import time


class StreamingASRConsole:
    """Keep sparse cloud ASR output visible beside high-rate local partials."""

    def __init__(self, *, selected: list[str], local_partial_interval_s: float) -> None:
        self._comparing = len(selected) > 1
        self._local_partial_interval_s = max(0.0, float(local_partial_interval_s))
        self._last_text: dict[str, str] = {}
        self._last_print_s: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(self, update) -> None:
        label = "FINAL" if update.is_final else "PARTIAL"
        key = str(update.backend)
        with self._lock:
            if update.error:
                latency_s = self._print_latency_s(update)
                print(
                    f"\n!!! ASR-{label} ERROR [{update.backend}/{update.model}] "
                    f"latency={latency_s * 1000:.0f}ms\n{update.error}\n",
                    flush=True,
                )
                return

            now = time.perf_counter()
            # Cumulative streaming backends can return the same transcript for
            # many consecutive audio packets. Display a partial only when its
            # text changes; final results always remain visible. This applies
            # equally to ProxiMic-gated and direct baseline sessions.
            if (
                not update.is_final
                and update.text == self._last_text.get(key)
            ):
                return
            if (
                self._comparing
                and not update.is_final
                and update.backend == "streaming_sensevoice"
                and update.text == self._last_text.get(key)
            ):
                return
            if (
                self._comparing
                and not update.is_final
                and update.backend == "streaming_sensevoice"
                and now - self._last_print_s.get(key, 0.0) < self._local_partial_interval_s
            ):
                self._last_text[key] = update.text
                return

            self._last_text[key] = update.text
            self._last_print_s[key] = now
            # Evaluate this only after de-duplication/throttling, immediately
            # before the line is emitted.  Suppressed partials therefore do
            # not manufacture latency samples.
            latency_s = self._print_latency_s(update)
            if update.backend != "streaming_sensevoice" or update.is_final:
                display_name = {
                    "volcengine": "Seed-ASR",
                    "funasr_nano": "Fun-ASR-Nano",
                }.get(update.backend, update.backend)
                print(
                    f"ASR-{label}[{display_name}/{update.model}] "
                    f"latency={latency_s * 1000:.0f}ms: {update.text}",
                    flush=True,
                )
                return
            print(
                f"ASR-PARTIAL[local SenseVoice] "
                f"latency={latency_s * 1000:.0f}ms: {update.text}",
                flush=True,
            )

    @staticmethod
    def _print_latency_s(update) -> float:
        ready_s = getattr(update, "chunk_ready_time_s", None)
        if ready_s is None:
            return max(0.0, float(update.latency_s))
        return max(0.0, time.perf_counter() - float(ready_s))
