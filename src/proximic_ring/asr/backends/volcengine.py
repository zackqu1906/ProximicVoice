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
import struct
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import numpy as np

from ..factory import ASRBackendSettings


DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"
DEFAULT_REQUEST_MODEL = "bigmodel"
_SAMPLE_RATE = 16_000

# Seed binary protocol values.  The first byte encodes protocol v1 and a
# four-byte header; the other header nibbles describe message/payload format.
_FULL_CLIENT_REQUEST = 0x1
_AUDIO_ONLY_REQUEST = 0x2
_FULL_SERVER_RESPONSE = 0x9
_ERROR_RESPONSE = 0xF
_SERIALIZATION_JSON = 0x1
_COMPRESSION_GZIP = 0x1


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
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2", copy=False).tobytes()


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


class VolcengineStreamingASR:
    """Bidirectional Seed-ASR 2.0 stream used by :class:`StreamingASRWorker`."""

    backend_name = "volcengine"
    sample_rate = _SAMPLE_RATE

    def __init__(
        self,
        *,
        model: str = "seedasr-streaming",
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
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Environment variable {api_key_env!r} is not set. Set it to the App Key "
                "from the new Doubao Speech console; do not pass API keys with --asr-option."
            )
        self.model_name = model
        self.api_key = api_key
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

    def set_partial_callback(
        self,
        callback: Callable[[str, float, float], None] | None,
    ) -> None:
        """Deliver native receiver-thread partials without a later feed poll."""

        self._partial_callback = callback

    def mark_chunk_ready(self, chunk_ready_time_s: float) -> None:
        """Associate subsequently completed packets with their input-ready time."""

        self._current_chunk_ready_time_s = float(chunk_ready_time_s)

    def _log(self, message: str) -> None:
        if self.debug:
            print(f"[ASR:volcengine] {message}")

    def _connect(self):
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
        return factory(self.url, header=headers, timeout=self.timeout_s)

    def start(self) -> None:
        self._close()
        self._ws = self._connect()
        self._pending_pcm.clear()
        self._last_text = ""
        self._final_seen = False
        self._updates = queue.SimpleQueue()
        self._receiver_stop.clear()
        self._final_response.clear()
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
        self._ws.send(
            _packet(
                _FULL_CLIENT_REQUEST,
                json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                sequence=self._next_sequence,
                serialization=_SERIALIZATION_JSON,
            )
        )
        self._next_sequence += 1
        # Audio sending must never wait for a cloud response.  The old design
        # waited up to ``partial_timeout_s`` after *every* audio callback. A
        # 200 ms stream therefore accumulated a growing worker queue, which
        # delayed END/final output by many seconds.  One receiver owns recv()
        # and stores responses for the next feed/finish call to publish.
        self._receiver_thread = threading.Thread(
            target=self._receive_loop,
            name="VolcengineASRReceiver",
            daemon=True,
        )
        self._receiver_thread.start()
        self._log("connected; sent request config; receiver started")

    def abort(self) -> None:
        self._pending_pcm.clear()
        self._close()

    def _send_audio(self, pcm: bytes, *, is_last: bool) -> None:
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

    def _receive_loop(self) -> None:
        """Continuously read cloud results without delaying audio production."""

        try:
            while not self._receiver_stop.is_set() and self._ws is not None:
                try:
                    # This is a polling interval for clean shutdown, not a
                    # per-audio latency budget. Keep it modest even when the
                    # user selects a larger partial_timeout_s compatibility
                    # setting from an earlier version of this adapter.
                    self._ws.settimeout(min(max(self.partial_timeout_s, 0.01), 0.2))
                    frame = self._ws.recv()
                except Exception as exc:
                    if isinstance(exc, TimeoutError) or exc.__class__.__name__ == "WebSocketTimeoutException":
                        continue
                    raise
                if frame is None:
                    if not self._receiver_stop.is_set():
                        raise RuntimeError("Volcengine ASR WebSocket closed before a final response")
                    return
                if isinstance(frame, str):
                    frame = frame.encode("latin1")
                text, is_final, summary, response_sequence = self._consume_frame(bytes(frame))
                self._received_frames += 1
                if text:
                    self._last_text = text
                    callback = self._partial_callback
                    timing = self._timing_for_response(response_sequence)
                    if callback is not None and not is_final and timing is not None:
                        callback(text, timing[0], timing[1])
                    elif callback is None:
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
                    self._final_response.set()
        except BaseException as exc:
            if not self._receiver_stop.is_set():
                self._receiver_error = exc
                self._final_response.set()
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
        try:
            self._log(
                f"finish entered; queued_pcm_bytes={len(self._pending_pcm)} "
                f"sent_packets={self._sent_audio_packets} received_frames={self._received_frames}"
            )
            self._send_audio(bytes(self._pending_pcm), is_last=True)
            self._pending_pcm.clear()
            if not self._final_response.wait(self.final_timeout_s):
                self._log(
                    f"final timeout after {self.final_timeout_s:.1f}s; "
                    f"sent_packets={self._sent_audio_packets} received_frames={self._received_frames}"
                )
                raise RuntimeError("Volcengine ASR did not return a final response before timeout")
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
            self._close()

    def _close(self) -> None:
        self._receiver_stop.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        receiver, self._receiver_thread = self._receiver_thread, None
        if receiver is not None and receiver is not threading.current_thread():
            receiver.join(timeout=max(1.0, min(self.partial_timeout_s, 2.0)))


def create_streaming_backend(settings: ASRBackendSettings) -> VolcengineStreamingASR:
    o = settings.options
    return VolcengineStreamingASR(
        # This is display/experiment metadata.  Resource ID selects the actual
        # Seed-ASR model on the native speech service.
        model=settings.model or "seedasr-streaming",
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
