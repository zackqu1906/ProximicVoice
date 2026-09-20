"""Native Doubao/Volcengine streaming ASR over the Seed WebSocket protocol.

This adapter implements the documented ``openspeech.bytedance.com`` protocol,
not Ark's HTTP API and not AI Gateway Realtime.  For a *new* Doubao Speech
console application, WebSocket authentication is:

``X-Api-Key`` + ``X-Api-Resource-Id`` + a per-connection UUID.

The selected Resource ID is what chooses Seed-ASR 2.0.  The protocol request
itself uses ``model_name: bigmodel`` as in the official API documentation.
"""

from __future__ import annotations

import gzip
import json
import os
import queue
import re
import struct
import threading
import time
import uuid
from collections.abc import Callable
from copy import deepcopy
from typing import Any

import numpy as np

from ..factory import ASRBackendSettings


DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"
DEFAULT_REQUEST_MODEL = "bigmodel"
_SAMPLE_RATE = 16_000
_CANCEL_POLL_S = 0.025
_CLOSE_JOIN_S = 0.025


class VolcengineASRCancelled(RuntimeError):
    """Internal terminal signal for a discarded utterance."""


# Seed binary protocol values.  The first byte encodes protocol v1 and a
# four-byte header; the other header nibbles describe message/payload format.
_FULL_CLIENT_REQUEST = 0x1
_AUDIO_ONLY_REQUEST = 0x2
_FULL_SERVER_RESPONSE = 0x9
_ERROR_RESPONSE = 0xF
_SERIALIZATION_JSON = 0x1
_COMPRESSION_GZIP = 0x1
_DIALOG_CONTEXT_TYPE = "dialog_ctx"
_DIALOG_CONTEXT_MAX_ITEMS = 20
# The service limit is 800 model tokens.  Its tokenizer is not available in
# the client, so keep a conservative character budget that is safe for Chinese
# (roughly one token per character) as well as mixed English text.
_DIALOG_CONTEXT_MAX_CHARS = 640


def _bool_option(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _pcm16(audio_16k: np.ndarray) -> bytes:
    x = np.asarray(audio_16k, dtype=np.float32).reshape(-1)
    if not x.size:
        return b""
    if not np.all(np.isfinite(x)):
        raise ValueError("ASR audio contains NaN or infinity")
    # Match VoiceHistoryStore's PCM16 quantization exactly so a saved WAV is a
    # byte-for-byte record of the samples submitted to the cloud ASR.
    return np.rint(np.clip(x, -1.0, 1.0) * 32767.0).astype(
        "<i2", copy=False
    ).tobytes()


def _packet(
    message_type: int,
    payload: bytes,
    *,
    sequence: int,
    is_last: bool = False,
    serialization: int = 0,
    compression: int = _COMPRESSION_GZIP,
) -> bytes:
    """Build one documented Seed binary-protocol client packet."""

    # The native SAUC protocol requires a signed sequence number on every
    # client request.  The final audio frame uses a negative sequence and the
    # NEG_WITH_SEQUENCE flag (0x3), exactly as in Volcengine's Python demo.
    # A server may tolerate sequence-less packets, but it is not the documented
    # streaming request shape and can lead to unreliable endpoint behaviour.
    flags = 0x3 if is_last else 0x1
    header = bytes(
        (
            0x11,  # protocol version 1; header size = 1 * 4 bytes
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0x00,
        )
    )
    body = gzip.compress(payload) if compression == _COMPRESSION_GZIP else payload
    signed_sequence = -sequence if is_last else sequence
    return header + struct.pack(">iI", signed_sequence, len(body)) + body


def _text_from_response(payload: Any) -> str | None:
    """Extract an accumulated transcript from known ASR response shapes."""

    if not isinstance(payload, dict):
        return None
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return None
    text = result.get("text")
    if text is not None:
        return str(text).strip()
    # Some response options return utterances rather than a top-level text.
    utterances = result.get("utterances")
    if isinstance(utterances, list):
        joined = "".join(
            str(item.get("text", ""))
            for item in utterances
            if isinstance(item, dict) and item.get("text")
        ).strip()
        return joined or None
    return None


def _response_summary(payload: Any) -> str:
    """Return safe schema diagnostics without printing recognized content."""

    if not isinstance(payload, dict):
        return f"payload_type={type(payload).__name__}"
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return f"top_keys={sorted(payload)[:8]} result_type={type(result).__name__}"
    text = result.get("text")
    utterances = result.get("utterances")
    return (
        f"top_keys={sorted(payload)[:8]} result_keys={sorted(result)[:12]} "
        f"text_chars={len(str(text or ''))} "
        f"utterances={len(utterances) if isinstance(utterances, list) else 'n/a'}"
    )


def _dialog_context_data(
    text: str,
    *,
    max_items: int = _DIALOG_CONTEXT_MAX_ITEMS,
    max_chars: int = _DIALOG_CONTEXT_MAX_CHARS,
) -> tuple[list[dict[str, str]], bool]:
    """Return recent text fragments in the newest-to-oldest service order."""

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    parts = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;])|\n+", normalized)
        if part.strip()
    ]
    if not parts:
        return [], False

    context_data: list[dict[str, str]] = []
    used_chars = 0
    truncated = False
    for part in reversed(parts):
        if len(context_data) >= max(1, int(max_items)):
            truncated = True
            break
        remaining = max(0, int(max_chars) - used_chars)
        if remaining <= 0:
            truncated = True
            break
        selected = part
        if len(selected) > remaining:
            selected = selected[-remaining:]
            truncated = True
        if selected:
            context_data.append({"text": selected})
            used_chars += len(selected)
        if selected != part:
            break
    if len(context_data) < len(parts):
        truncated = True
    return context_data, truncated


class VolcengineStreamingASR:
    """Bidirectional Seed-ASR 2.0 stream used by :class:`StreamingASRWorker`."""

    backend_name = "volcengine"
    sample_rate = _SAMPLE_RATE

    def __init__(
        self,
        *,
        model: str = "seedasr-streaming",
        api_key: str = "",
        api_key_env: str = "VOLC_ASR_API_KEY",
        resource_id: str = DEFAULT_RESOURCE_ID,
        url: str = DEFAULT_URL,
        request_model: str = DEFAULT_REQUEST_MODEL,
        language: str = "auto",
        chunk_ms: int = 200,
        timeout_s: float = 15.0,
        partial_timeout_s: float = 0.8,
        final_timeout_s: float = 8.0,
        debug: bool = False,
        ws_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not 100 <= int(chunk_ms) <= 200:
            raise ValueError("chunk_ms must be between 100 and 200 for Volcengine streaming ASR")
        resolved_api_key = str(api_key).strip()
        key_env = str(api_key_env).strip()
        if not resolved_api_key and key_env:
            resolved_api_key = os.environ.get(key_env, "").strip()
        if not resolved_api_key:
            raise RuntimeError(
                "尚未配置线上语音模型 API Key；请在应用设置中填写"
                + (f"（兼容环境变量：{key_env}）" if key_env else "")
            )
        self.model_name = model
        self.api_key = resolved_api_key
        self.resource_id = resource_id
        self.url = url
        self.request_model = request_model
        self.language = language
        self.chunk_bytes = _SAMPLE_RATE * 2 * int(chunk_ms) // 1000
        self.timeout_s = float(timeout_s)
        self.partial_timeout_s = float(partial_timeout_s)
        self.final_timeout_s = float(final_timeout_s)
        self.debug = bool(debug)
        self._ws_factory = ws_factory
        self._ws: Any | None = None
        self._session_lock = threading.RLock()
        self._session_generation = 0
        self._cancel_event = threading.Event()
        self._active_cancel_event = self._cancel_event
        self._pending_pcm = bytearray()
        self._last_text = ""
        self._final_seen = False
        self._updates: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._receiver_stop = threading.Event()
        self._final_response = threading.Event()
        self._receiver_error: BaseException | None = None
        self._receiver_thread: threading.Thread | None = None
        self._sent_audio_packets = 0
        self._sent_audio_bytes = 0
        self._audio_square_sum = 0.0
        self._audio_peak = 0.0
        self._audio_samples = 0
        self._received_frames = 0
        self._next_sequence = 1
        self._current_chunk_ready_time_s: float | None = None
        self._packet_timings: dict[int, tuple[float, float]] = {}
        self._latest_packet_timing: tuple[float, float] | None = None
        self._timing_lock = threading.Lock()
        self._partial_callback: Callable[[str, float, float], None] | None = None
        self._pending_session_context: dict[str, Any] | None = None
        self._session_context_metadata: dict[str, Any] = {
            "status": "empty",
            "source": "focused_text",
            "context_type": _DIALOG_CONTEXT_TYPE,
            "context_data": [],
            "item_count": 0,
            "source_char_count": 0,
            "sent_char_count": 0,
            "truncated": False,
            "reason": "not_provided",
        }

    def set_cancel_event(self, event: threading.Event) -> None:
        """Bind the producer's cancellation event to the next utterance.

        Do not clear this event: discard can arrive before start/connect runs.
        Every receiver captures its own event rather than following this mutable
        next-session slot.
        """
        self._cancel_event = event

    def _check_cancelled(
        self,
        generation: int,
        cancel_event: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        if (cancel_event.is_set() or stop_event.is_set()
                or generation != self._session_generation):
            raise VolcengineASRCancelled("Volcengine ASR session was cancelled")

    def set_partial_callback(
        self,
        callback: Callable[[str, float, float], None] | None,
    ) -> None:
        """Deliver native receiver-thread partials without a later feed poll."""

        self._partial_callback = callback

    def set_session_context(self, context: object | None) -> None:
        """Prepare one utterance's semantic context without retaining stale text."""

        supplied = dict(context) if isinstance(context, dict) else {}
        status = str(supplied.get("status", "captured") or "captured")
        source = str(supplied.get("source", "focused_text") or "focused_text")
        raw_text = str(supplied.get("text", "") or "")
        context_data, truncated = _dialog_context_data(raw_text)
        source_char_count = int(
            supplied.get("source_char_count", len(raw_text)) or 0
        )
        sent_char_count = sum(len(item["text"]) for item in context_data)

        if status != "captured":
            context_data = []
            sent_char_count = 0
        final_status = "prepared" if context_data else (
            "empty" if status in {"captured", "empty"} else "unavailable"
        )
        reason = str(supplied.get("reason", "") or "")
        if not reason and not context_data:
            reason = "empty_focused_text"

        metadata: dict[str, Any] = {
            "status": final_status,
            "source": source,
            "context_type": _DIALOG_CONTEXT_TYPE,
            "context_data": context_data,
            "item_count": len(context_data),
            "source_char_count": max(0, source_char_count),
            "sent_char_count": sent_char_count,
            "truncated": bool(truncated or source_char_count > len(raw_text)),
            "reason": reason,
        }
        for key in ("target_key", "application", "read_method"):
            value = str(supplied.get(key, "") or "")
            if value:
                metadata[key] = value
        self._pending_session_context = metadata
        self._session_context_metadata = deepcopy(metadata)

    @property
    def session_context_metadata(self) -> dict[str, Any]:
        """Describe exactly what this session sends, for logs and datasets."""

        return deepcopy(self._session_context_metadata)

    def mark_chunk_ready(self, chunk_ready_time_s: float) -> None:
        """Associate subsequently completed packets with their input-ready time."""

        self._current_chunk_ready_time_s = float(chunk_ready_time_s)

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[ASR:volcengine] {message}")

    def _connect(
        self,
        generation: int,
        cancel_event: threading.Event,
        stop_event: threading.Event,
    ):
        if self._ws_factory is not None:
            factory = self._ws_factory
        else:
            try:
                from websocket import create_connection
            except ImportError as exc:
                raise RuntimeError(
                    'Volcengine streaming ASR requires websocket-client. '
                    'Install: pip install -e ".[asr-volcengine]"'
                ) from exc
            factory = create_connection
        headers = [
            f"X-Api-Key: {self.api_key}",
            f"X-Api-Resource-Id: {self.resource_id}",
            f"X-Api-Connect-Id: {uuid.uuid4()}",
        ]
        self._log(
            f"connecting resource_id={self.resource_id} timeout={self.timeout_s:.1f}s"
        )
        self._check_cancelled(generation, cancel_event, stop_event)
        completed = threading.Event()
        result_lock = threading.Lock()
        result: list[Any] = []
        failures: list[BaseException] = []
        abandoned = False

        def connect() -> None:
            nonlocal abandoned
            try:
                ws = factory(self.url, header=headers, timeout=self.timeout_s)
            except BaseException as exc:
                with result_lock:
                    if not abandoned:
                        failures.append(exc)
                    completed.set()
                return
            with result_lock:
                late = abandoned or cancel_event.is_set() or stop_event.is_set()
                if not late:
                    result.append(ws)
                completed.set()
            if late:
                self._dispose_socket(ws)

        threading.Thread(target=connect, name="VolcengineASRConnect", daemon=True).start()
        deadline = time.monotonic() + max(0.0, self.timeout_s)
        try:
            while True:
                self._check_cancelled(generation, cancel_event, stop_event)
                if completed.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Volcengine ASR connection timed out")
                completed.wait(min(_CANCEL_POLL_S, remaining))
            self._check_cancelled(generation, cancel_event, stop_event)
            with result_lock:
                if failures:
                    raise failures[0]
                if not result:
                    raise VolcengineASRCancelled("Volcengine ASR session was cancelled")
                return result.pop()
        finally:
            with result_lock:
                abandoned = True
                late_socket = result.pop() if result else None
            if late_socket is not None:
                self._dispose_socket(late_socket)

    def start(self) -> None:
        self._close()
        generation = self._session_generation
        try:
            self._start_session(generation)
        except BaseException:
            self._close(expected_generation=generation)
            raise

    def _start_session(self, generation: int) -> None:
        with self._session_lock:
            cancel_event = self._cancel_event
            self._active_cancel_event = cancel_event
            # Never clear/reuse an old receiver's events. A slow old recv may
            # still return after the next utterance is already established.
            stop_event = self._receiver_stop = threading.Event()
            final_response = self._final_response = threading.Event()
        self._check_cancelled(generation, cancel_event, stop_event)
        context_metadata = self._pending_session_context
        self._pending_session_context = None
        if context_metadata is None:
            self.set_session_context(None)
            context_metadata = self._pending_session_context
            self._pending_session_context = None
        if context_metadata is None:  # pragma: no cover - defensive invariant
            context_metadata = {}
        self._session_context_metadata = deepcopy(context_metadata)
        ws = self._connect(generation, cancel_event, stop_event)
        with self._session_lock:
            try:
                self._check_cancelled(generation, cancel_event, stop_event)
            except VolcengineASRCancelled:
                self._dispose_socket(ws)
                raise
            self._ws = ws
        self._pending_pcm.clear()
        self._last_text = ""
        self._final_seen = False
        self._updates = queue.SimpleQueue()
        self._receiver_error = None
        self._sent_audio_packets = 0
        self._sent_audio_bytes = 0
        self._audio_square_sum = 0.0
        self._audio_peak = 0.0
        self._audio_samples = 0
        self._received_frames = 0
        self._next_sequence = 1
        with self._timing_lock:
            self._packet_timings.clear()
            self._latest_packet_timing = None
        request: dict[str, Any] = {
            "user": {"uid": "proximic-ring"},
            "audio": {"format": "pcm", "rate": _SAMPLE_RATE, "bits": 16, "channel": 1},
            "request": {
                "model_name": self.request_model,
                "enable_itn": True,
                "enable_punc": True,
                # These are the documented bidirectional-streaming settings.
                # Without enable_nonstream=False, service defaults are allowed
                # to select a less eager endpoint mode.
                "enable_nonstream": False,
                "show_utterances": True,
            },
        }
        if self.language != "auto":
            request["request"]["language"] = self.language
        context_data = context_metadata.get("context_data")
        if isinstance(context_data, list) and context_data:
            request["request"]["corpus"] = {
                "context": json.dumps(
                    {
                        "context_type": _DIALOG_CONTEXT_TYPE,
                        "context_data": context_data,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            }
        # Bound socket writes as well as receiver polls. This does not wait
        # for a response and avoids the connection timeout carrying into send.
        ws.settimeout(min(max(self.partial_timeout_s, 0.01), 0.2))
        self._check_cancelled(generation, cancel_event, stop_event)
        ws.send(
            _packet(
                _FULL_CLIENT_REQUEST,
                json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                sequence=self._next_sequence,
                serialization=_SERIALIZATION_JSON,
            )
        )
        self._check_cancelled(generation, cancel_event, stop_event)
        if context_data:
            self._session_context_metadata["status"] = "sent"
        self._next_sequence += 1
        # Audio sending must never wait for a cloud response.  The old design
        # waited up to ``partial_timeout_s`` after *every* audio callback. A
        # 200 ms stream therefore accumulated a growing worker queue, which
        # delayed END/final output by many seconds.  One receiver owns recv()
        # and stores responses for the next feed/finish call to publish.
        self._receiver_thread = threading.Thread(
            target=self._receive_loop,
            args=(ws, generation, cancel_event, stop_event, final_response, self._partial_callback),
            name="VolcengineASRReceiver",
            daemon=True,
        )
        self._receiver_thread.start()
        self._log("connected; sent request config; receiver started")

    def abort(self) -> None:
        self._pending_pcm.clear()
        self._close()

    def _send_audio(self, pcm: bytes, *, is_last: bool) -> None:
        self._check_cancelled(self._session_generation, self._active_cancel_event, self._receiver_stop)
        if self._ws is None:
            raise RuntimeError("Volcengine streaming ASR session was not started")
        sequence = self._next_sequence
        ready_s = self._current_chunk_ready_time_s
        if ready_s is None:
            ready_s = time.perf_counter()
        audio_end_s = (self._sent_audio_bytes + len(pcm)) / (_SAMPLE_RATE * 2)
        timing = (ready_s, audio_end_s)
        # Publish the mapping before send(): a very fast/fake WebSocket can
        # make its response visible to the receiver as soon as send returns.
        with self._timing_lock:
            self._packet_timings[sequence] = timing
            self._latest_packet_timing = timing
        self._ws.send(
            _packet(
                _AUDIO_ONLY_REQUEST,
                pcm,
                sequence=sequence,
                is_last=is_last,
            )
        )
        self._next_sequence += 1
        self._sent_audio_packets += 1
        self._sent_audio_bytes += len(pcm)
        if pcm:
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            self._audio_samples += samples.size
            self._audio_square_sum += float(np.dot(samples, samples))
            self._audio_peak = max(self._audio_peak, float(np.max(np.abs(samples))))
        if is_last or self._sent_audio_packets == 1:
            self._log(
                f"sent audio packet={self._sent_audio_packets} pcm_bytes={len(pcm)} "
                f"total_audio_s={self._sent_audio_bytes / (_SAMPLE_RATE * 2):.2f} last={is_last}"
            )

    def _consume_frame(self, frame: bytes) -> tuple[str | None, bool, str, int | None]:
        if len(frame) < 4:
            return None, False, "frame_too_short", None
        message_type = frame[1] >> 4
        flags = frame[1] & 0x0F
        compression = frame[2] & 0x0F
        offset = 4

        if message_type == _ERROR_RESPONSE:
            if len(frame) < offset + 8:
                raise RuntimeError("Volcengine ASR returned a malformed error response")
            code, size = struct.unpack_from(">II", frame, offset)
            message = frame[offset + 8 : offset + 8 + size].decode("utf-8", errors="replace")
            raise RuntimeError(f"Volcengine ASR error {code}: {message}")
        if message_type != _FULL_SERVER_RESPONSE:
            return None, False, f"message_type={message_type}", None
        response_sequence: int | None = None
        if flags & 0x1:  # response includes a sequence number before payload size
            if len(frame) < offset + 4:
                return None, bool(flags & 0x2), "response_missing_sequence", None
            (response_sequence,) = struct.unpack_from(">i", frame, offset)
            offset += 4
        if len(frame) < offset + 4:
            return None, bool(flags & 0x2), "response_missing_payload_size", response_sequence
        (size,) = struct.unpack_from(">I", frame, offset)
        payload = frame[offset + 4 : offset + 4 + size]
        if len(payload) != size:
            raise RuntimeError("Volcengine ASR returned a truncated response payload")
        if compression == _COMPRESSION_GZIP:
            payload = gzip.decompress(payload)
        try:
            decoded = json.loads(payload.decode("utf-8"))
            text = _text_from_response(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("Volcengine ASR returned an invalid JSON response") from exc
        return text, bool(flags & 0x2), _response_summary(decoded), response_sequence

    def _timing_for_response(self, response_sequence: int | None) -> tuple[float, float] | None:
        with self._timing_lock:
            timing = None
            if response_sequence is not None:
                timing = self._packet_timings.get(abs(response_sequence))
            # Some endpoint responses omit the optional sequence field.  They
            # are cumulative results, so the newest fully sent packet is the
            # best available attribution in that case.
            return timing or self._latest_packet_timing

    def _receive_loop(
        self,
        ws: Any,
        generation: int,
        cancel_event: threading.Event,
        stop_event: threading.Event,
        final_response: threading.Event,
        callback: Callable[[str, float, float], None] | None,
    ) -> None:
        """Read only this utterance's socket and publish only while it owns state."""
        try:
            while True:
                self._check_cancelled(generation, cancel_event, stop_event)
                try:
                    ws.settimeout(min(max(self.partial_timeout_s, 0.01), 0.2))
                    frame = ws.recv()
                except Exception as exc:
                    if isinstance(exc, TimeoutError) or exc.__class__.__name__ == "WebSocketTimeoutException":
                        continue
                    raise
                self._check_cancelled(generation, cancel_event, stop_event)
                if frame is None:
                    raise RuntimeError("Volcengine ASR WebSocket closed before a final response")
                if isinstance(frame, str):
                    frame = frame.encode("latin1")
                text, is_final, summary, response_sequence = self._consume_frame(bytes(frame))
                timing = None
                with self._session_lock:
                    self._check_cancelled(generation, cancel_event, stop_event)
                    self._received_frames += 1
                    if text:
                        self._last_text = text
                        timing = self._timing_for_response(response_sequence)
                        if callback is None:
                            self._updates.put(text)
                        self._log(
                            f"received transcript frame={self._received_frames} "
                            f"chars={len(text)} final={is_final}"
                        )
                    elif is_final:
                        self._log(
                            f"received final response frame={self._received_frames} without text; {summary}"
                        )
                    if is_final:
                        self._final_seen = True
                        final_response.set()
                if text and callback is not None and not is_final and timing is not None:
                    # The callback itself is permanently bound by the worker to
                    # this session, so even a concurrent cancellation cannot
                    # relabel a last in-flight callback as the next utterance.
                    self._check_cancelled(generation, cancel_event, stop_event)
                    callback(text, timing[0], timing[1])
                if is_final:
                    return
        except BaseException as exc:
            with self._session_lock:
                if (generation == self._session_generation and not stop_event.is_set()
                        and not cancel_event.is_set() and not self._final_seen):
                    self._receiver_error = exc
                    final_response.set()
                    self._log(f"receiver failed: {exc}")

    def _drain_updates(self) -> str | None:
        if self._receiver_error is not None:
            raise RuntimeError(f"Volcengine ASR receive failed: {self._receiver_error}") from self._receiver_error
        latest: str | None = None
        while True:
            try:
                latest = self._updates.get_nowait()
            except queue.Empty:
                return latest

    def feed(self, audio_16k: np.ndarray) -> str | None:
        self._check_cancelled(self._session_generation, self._active_cancel_event, self._receiver_stop)
        self._pending_pcm.extend(_pcm16(audio_16k))
        # Keep one complete packet queued.  ``finish`` marks that actual final
        # packet with the required last-packet flag instead of sending audio
        # twice or inventing a second VAD layer.
        while len(self._pending_pcm) > self.chunk_bytes:
            packet = bytes(self._pending_pcm[: self.chunk_bytes])
            del self._pending_pcm[: self.chunk_bytes]
            self._send_audio(packet, is_last=False)
        return self._drain_updates()

    def finish(self, final_audio_16k: np.ndarray) -> str:
        del final_audio_16k  # Already streamed by start/feed; do not duplicate it.
        generation = self._session_generation
        cancel_event = self._active_cancel_event
        stop_event = self._receiver_stop
        final_response = self._final_response
        try:
            self._check_cancelled(generation, cancel_event, stop_event)
            self._log(
                f"finish entered; queued_pcm_bytes={len(self._pending_pcm)} "
                f"sent_packets={self._sent_audio_packets} received_frames={self._received_frames}"
            )
            self._send_audio(bytes(self._pending_pcm), is_last=True)
            self._pending_pcm.clear()
            deadline = time.monotonic() + max(0.0, self.final_timeout_s)
            while not final_response.is_set():
                self._check_cancelled(generation, cancel_event, stop_event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._log(
                        f"final timeout after {self.final_timeout_s:.1f}s; "
                        f"sent_packets={self._sent_audio_packets} received_frames={self._received_frames}"
                    )
                    raise RuntimeError("Volcengine ASR did not return a final response before timeout")
                final_response.wait(min(_CANCEL_POLL_S, remaining))
            self._check_cancelled(generation, cancel_event, stop_event)
            self._drain_updates()
            if self._receiver_error is not None:
                raise RuntimeError(f"Volcengine ASR receive failed: {self._receiver_error}") from self._receiver_error
            if not self._final_seen:
                raise RuntimeError("Volcengine ASR stream ended without a final response")
            rms = (self._audio_square_sum / self._audio_samples) ** 0.5 if self._audio_samples else 0.0
            rms_dbfs = 20.0 * np.log10(max(rms, 1e-12))
            self._log(
                f"finish complete; received_frames={self._received_frames} chars={len(self._last_text)} "
                f"audio_rms_dbfs={rms_dbfs:.1f} peak={self._audio_peak:.3f}"
            )
            return self._last_text
        finally:
            self._close(expected_generation=generation)

    @staticmethod
    def _dispose_socket(ws: Any) -> None:
        """Avoid a graceful close handshake blocking cancellation/new audio."""
        def close() -> None:
            try:
                shutdown = getattr(ws, "shutdown", None)
                if callable(shutdown):
                    shutdown()
                else:
                    try:
                        ws.close(timeout=0)
                    except TypeError:
                        ws.close()
            except Exception:
                pass

        closer = threading.Thread(target=close, name="VolcengineASRClose", daemon=True)
        closer.start()
        # Usually immediate; retain deterministic cleanup for ordinary sockets
        # while bounding an injected/custom close implementation that blocks.
        closer.join(timeout=_CLOSE_JOIN_S)

    def _close(self, *, expected_generation: int | None = None) -> None:
        with self._session_lock:
            if expected_generation is not None and expected_generation != self._session_generation:
                return
            self._session_generation += 1
            self._receiver_stop.set()
            self._final_response.set()
            ws, self._ws = self._ws, None
            self._receiver_thread = None
        if ws is not None:
            self._dispose_socket(ws)


def create_streaming_backend(settings: ASRBackendSettings) -> VolcengineStreamingASR:
    o = settings.options
    return VolcengineStreamingASR(
        # This is display/experiment metadata.  Resource ID selects the actual
        # Seed-ASR model on the native speech service.
        model=settings.model or "seedasr-streaming",
        api_key=o.get("api_key", ""),
        api_key_env=o.get("api_key_env", "VOLC_ASR_API_KEY"),
        resource_id=o.get("resource_id", DEFAULT_RESOURCE_ID),
        url=o.get("url", DEFAULT_URL),
        request_model=o.get("request_model", DEFAULT_REQUEST_MODEL),
        language=settings.language,
        chunk_ms=int(o.get("chunk_ms", "200")),
        timeout_s=float(o.get("timeout_s", "15")),
        partial_timeout_s=float(o.get("partial_timeout_s", "0.8")),
        final_timeout_s=float(o.get("final_timeout_s", "8")),
        debug=_bool_option(o.get("debug"), False),
    )
