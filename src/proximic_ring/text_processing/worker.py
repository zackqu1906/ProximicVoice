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
    InputModeRoutingRequest,
    InputModeRoutingResult,
    LLMTraceCollection,
    LLMSettings,
    TextProcessingRequest,
    TextProcessingResult,
    normalize_input_mode,
)


@dataclass(frozen=True)
class _WarmupTask:
    settings: LLMSettings


class TextProcessingWorker:
    """Run independent routing/candidate requests away from real-time audio."""

    def __init__(
        self,
        *,
        on_result: Callable[[TextProcessingResult], None],
        on_trace: Callable[[LLMTraceCollection], None] | None = None,
        on_warmup: Callable[[str | None, float], None] | None = None,
        on_routing_result: Callable[[InputModeRoutingResult], None] | None = None,
        processor: OpenAICompatibleTextProcessor | None = None,
    ) -> None:
        self._on_result = on_result
        self._on_trace = on_trace
        self._on_warmup = on_warmup
        self._on_routing_result = on_routing_result
        self._processor = processor or OpenAICompatibleTextProcessor()
        self._queue: queue.SimpleQueue[
            TextProcessingRequest | InputModeRoutingRequest | _WarmupTask | None
        ] = queue.SimpleQueue()
        self._closed = threading.Event()
        self._cancelled_request_ids: set[int] = set()
        self._cancel_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._active_workers = 3
        self._threads = [
            threading.Thread(
                target=self._run,
                name=f"ProxiMicTextProcessing-{index + 1}",
                daemon=True,
            )
            for index in range(self._active_workers)
        ]
        # Kept for compatibility with diagnostics that inspect the old field.
        self._thread = self._threads[0]
        for thread in self._threads:
            thread.start()

    def submit(self, request: TextProcessingRequest) -> None:
        if self._closed.is_set():
            return
        self._queue.put(request)

    def warmup(self, settings: LLMSettings) -> None:
        if self._closed.is_set():
            return
        self._queue.put(_WarmupTask(settings))

    def submit_routing(self, request: InputModeRoutingRequest) -> None:
        if self._closed.is_set():
            return
        self._queue.put(request)

    def cancel_request(self, request_id: int) -> None:
        """Suppress a queued/running request's result.

        A blocking HTTP inference cannot always be force-killed safely, so the
        UI invalidates it immediately while the other workers remain free to
        process the next utterance.
        """
        with self._cancel_lock:
            self._cancelled_request_ids.add(int(request_id))

    def _is_cancelled(self, request_id: int) -> bool:
        with self._cancel_lock:
            return int(request_id) in self._cancelled_request_ids

    def _consume_cancelled(self, request_id: int) -> bool:
        """Return and forget cancellation once a request is fully discarded."""
        with self._cancel_lock:
            normalized = int(request_id)
            if normalized not in self._cancelled_request_ids:
                return False
            self._cancelled_request_ids.remove(normalized)
            return True

    def close(self, *, wait: bool = False) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for _thread in self._threads:
            self._queue.put(None)
        if wait:
            for thread in self._threads:
                if threading.current_thread() is not thread:
                    thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            request = self._queue.get()
            if request is None:
                close_processor = False
                with self._shutdown_lock:
                    self._active_workers -= 1
                    close_processor = self._active_workers == 0
                if close_processor:
                    close = getattr(self._processor, "close", None)
                    if callable(close):
                        try:
                            close()
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
            if isinstance(request, InputModeRoutingRequest):
                if self._consume_cancelled(request.request_id):
                    continue
                started = time.perf_counter()
                error = None
                model_output = ""
                mode = normalize_input_mode(request.fallback_mode)
                try:
                    classify_with_trace = getattr(
                        self._processor, "classify_input_mode_with_trace", None
                    )
                    if callable(classify_with_trace):
                        mode, model_output = classify_with_trace(
                            request.raw_text, request.settings
                        )
                    else:
                        mode = self._processor.classify_input_mode(
                            request.raw_text, request.settings
                        )
                except BaseException as exc:
                    error = str(exc)
                    model_output = str(getattr(exc, "model_output", "") or "")
                result = InputModeRoutingResult(
                    request_id=request.request_id,
                    session_id=request.session_id,
                    raw_text=request.raw_text,
                    mode=normalize_input_mode(mode),
                    latency_s=max(0.0, time.perf_counter() - started),
                    error=error,
                    model_output=model_output,
                )
                if self._on_routing_result is not None:
                    if self._consume_cancelled(request.request_id):
                        continue
                    try:
                        self._on_routing_result(result)
                    except BaseException:
                        if self._closed.is_set():
                            return
                continue
            if self._consume_cancelled(request.request_id):
                continue
            started = time.perf_counter()
            error = None
            model_output = ""
            llm_branches = ()
            winner_branch = ""
            used_llm = bool(request.settings.enabled)
            try:
                process_with_collection_trace = getattr(
                    self._processor, "process_with_collection_trace", None
                )
                process_with_trace = getattr(self._processor, "process_with_trace", None)
                if callable(process_with_collection_trace):
                    trace_request_id = request.request_id

                    def publish_trace(
                        branches,
                        winner_branch,
                        trace_request_id=trace_request_id,
                    ):
                        if (
                            self._on_trace is None
                            or self._closed.is_set()
                            or self._is_cancelled(trace_request_id)
                        ):
                            return
                        self._on_trace(
                            LLMTraceCollection(
                                request_id=trace_request_id,
                                branches=tuple(branches),
                                winner_branch=str(winner_branch),
                            )
                        )

                    (
                        final_text,
                        model_output,
                        llm_branches,
                        winner_branch,
                    ) = process_with_collection_trace(
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
                        on_collection_complete=publish_trace,
                    )
                elif callable(process_with_trace):
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
                llm_branches = tuple(getattr(exc, "branch_traces", ()) or ())
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
                llm_branches=tuple(llm_branches),
                winner_branch=winner_branch,
                episode_id=request.episode_id,
                attempt_id=request.attempt_id,
            )
            if self._consume_cancelled(request.request_id):
                continue
            try:
                self._on_result(result)
            except BaseException:
                # A UI teardown must not keep this daemon worker alive or turn
                # a completed request into an unhandled thread exception.
                if self._closed.is_set():
                    return
