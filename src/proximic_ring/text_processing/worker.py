from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .edit_tool import EDIT_MODE_RACE
from .llm import OpenAICompatibleTextProcessor
from .model import (
    INPUT_MODE_EDIT,
    LLMSettings,
    TextProcessingRequest,
    TextProcessingResult,
    normalize_input_mode,
)


@dataclass(frozen=True)
class _WarmupTask:
    settings: LLMSettings


class TextProcessingWorker:
    """Serialize LLM requests away from the real-time ASR and BLE threads."""

    def __init__(
        self,
        *,
        on_result: Callable[[TextProcessingResult], None],
        on_warmup: Callable[[str | None, float], None] | None = None,
        processor: OpenAICompatibleTextProcessor | None = None,
    ) -> None:
        self._on_result = on_result
        self._on_warmup = on_warmup
        self._processor = processor or OpenAICompatibleTextProcessor()
        self._queue: queue.SimpleQueue[
            TextProcessingRequest | _WarmupTask | None
        ] = queue.SimpleQueue()
        self._thread = threading.Thread(
            target=self._run,
            name="ProxiMicTextProcessing",
            daemon=True,
        )
        self._closed = threading.Event()
        self._thread.start()

    def submit(self, request: TextProcessingRequest) -> None:
        if self._closed.is_set():
            return
        self._queue.put(request)

    def warmup(self, settings: LLMSettings) -> None:
        if self._closed.is_set():
            return
        self._queue.put(_WarmupTask(settings))

    def close(self, *, wait: bool = False) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(None)
        if wait and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            if request is None:
                close_processor = getattr(self._processor, "close", None)
                if callable(close_processor):
                    try:
                        close_processor()
                    except BaseException:
                        pass
                return
            if isinstance(request, _WarmupTask):
                started = time.perf_counter()
                error = None
                try:
                    self._processor.warmup(request.settings)
                except BaseException as exc:
                    error = str(exc)
                if self._on_warmup is not None:
                    try:
                        self._on_warmup(
                            error,
                            max(0.0, time.perf_counter() - started),
                        )
                    except BaseException:
                        if self._closed.is_set():
                            return
                continue
            started = time.perf_counter()
            error = None
            model_output = ""
            used_llm = bool(request.settings.enabled)
            try:
                process_with_trace = getattr(self._processor, "process_with_trace", None)
                if callable(process_with_trace):
                    final_text, model_output = process_with_trace(
                        request.raw_text,
                        request.mode,
                        request.settings,
                        request.target_text,
                        (
                            EDIT_MODE_RACE
                            if normalize_input_mode(request.mode)
                            == INPUT_MODE_EDIT
                            else ""
                        ),
                    )
                else:
                    final_text = self._processor.process(
                        request.raw_text,
                        request.mode,
                        request.settings,
                        request.target_text,
                        (
                            EDIT_MODE_RACE
                            if normalize_input_mode(request.mode)
                            == INPUT_MODE_EDIT
                            else ""
                        ),
                    )
            except BaseException as exc:
                # The input method must remain usable when a cloud/local LLM is
                # unavailable.  Preserve the raw final ASR text as a fallback.
                error = str(exc)
                model_output = str(getattr(exc, "model_output", "") or "")
                final_text = request.target_text or request.raw_text
            result = TextProcessingResult(
                request_id=request.request_id,
                session_id=request.session_id,
                mode=normalize_input_mode(request.mode),
                raw_text=request.raw_text,
                final_text=str(final_text or "").strip(),
                latency_s=max(0.0, time.perf_counter() - started),
                used_llm=used_llm,
                target_text=request.target_text,
                error=error,
                model_output=model_output,
            )
            try:
                self._on_result(result)
            except BaseException:
                # A UI teardown must not keep this daemon worker alive or turn
                # a completed request into an unhandled thread exception.
                if self._closed.is_set():
                    return
