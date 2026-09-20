from __future__ import annotations

import gzip
import json
import struct
import threading
import queue
import time

import numpy as np
import pytest

from proximic_ring.asr.backends.volcengine import (
    DEFAULT_RESOURCE_ID,
    VolcengineStreamingASR,
    VolcengineASRCancelled,
    _dialog_context_data,
    _pcm16,
)
from proximic_ring.asr.factory import (
    ASRBackendSettings,
    asr_backend_kind,
    create_streaming_asr_backend,
)


def _response(text: str, *, final: bool, sequence: int | None = None) -> bytes:
    payload = gzip.compress(json.dumps({"result": {"text": text}}).encode())
    flags = (0x01 if sequence is not None else 0) | (0x02 if final else 0)
    prefix = struct.pack(">i", sequence) if sequence is not None else b""
    return bytes((0x11, 0x90 | flags, 0x01, 0)) + prefix + struct.pack(">I", len(payload)) + payload


class FakeWebSocket:
    def __init__(self):
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.responses = [_response("你好", final=False), _response("你好世界", final=True)]
        self.closed = False

    def send(self, data: bytes):
        self.sent.append(data)

    def settimeout(self, value: float):
        self.timeouts.append(value)

    def recv(self):
        if self.responses:
            return self.responses.pop(0)
        raise TimeoutError()

    def close(self):
        self.closed = True


class ClosingAfterFinalWebSocket(FakeWebSocket):
    def __init__(self):
        super().__init__()
        self.recv_calls = 0

    def recv(self):
        self.recv_calls += 1
        if self.responses:
            return self.responses.pop(0)
        raise ConnectionError("Connection to remote host was lost")


def _client_request(frame: bytes) -> dict:
    assert frame[0] == 0x11
    assert frame[1] >> 4 == 0x1
    payload_size = struct.unpack_from(">I", frame, 8)[0]
    payload = gzip.decompress(frame[12 : 12 + payload_size])
    return json.loads(payload)


def test_pcm16_quantization_matches_saved_voice_history_wav_samples():
    audio = np.array([-1.0, -0.25, 0.0, 0.25, 1.0], dtype=np.float32)
    saved_pcm = np.rint(np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")

    assert _pcm16(audio) == saved_pcm.tobytes()


def test_native_streaming_uses_new_console_headers_and_returns_partial(monkeypatch):
    monkeypatch.setenv("TEST_VOLC_KEY", "speech-app-key")
    ws = FakeWebSocket()
    calls = []

    def factory(url, *, header, timeout):
        calls.append((url, header, timeout))
        return ws

    backend = VolcengineStreamingASR(
        api_key_env="TEST_VOLC_KEY", ws_factory=factory, chunk_ms=200, partial_timeout_s=0.35
    )
    backend.start()
    partial = backend.feed(np.ones(3201, dtype=np.float32) * 0.1)
    if partial is None:
        time.sleep(0.01)
        partial = backend.feed(np.empty(0, dtype=np.float32))
    final = backend.finish(np.empty(0, dtype=np.float32))

    assert calls[0][1][0] == "X-Api-Key: speech-app-key"
    assert calls[0][1][1] == f"X-Api-Resource-Id: {DEFAULT_RESOURCE_ID}"
    assert calls[0][1][2].startswith("X-Api-Connect-Id: ")
    assert partial in {"你好", "你好世界"}
    assert final == "你好世界"
    # A dedicated receiver continuously reads the WebSocket while feed only
    # sends/drains updates, so slow cloud responses cannot backlog audio.
    assert ws.timeouts
    assert ws.closed
    # Every client frame carries the required sequence number.  The final
    # packet has the NEG_WITH_SEQUENCE flag and a negative sequence number.
    assert ws.sent[0][1] == 0x11
    assert struct.unpack_from(">i", ws.sent[0], 4)[0] == 1
    assert ws.sent[-1][1] == 0x23
    assert struct.unpack_from(">i", ws.sent[-1], 4)[0] < 0


def test_dialog_context_is_sent_as_stringified_corpus_newest_first(monkeypatch):
    monkeypatch.setenv("TEST_VOLC_KEY", "speech-app-key")
    ws = FakeWebSocket()
    backend = VolcengineStreamingASR(
        api_key_env="TEST_VOLC_KEY",
        ws_factory=lambda *_args, **_kwargs: ws,
    )
    source_text = "较早一句。\n更新一句！最新一句？"
    backend.set_session_context(
        {
            "status": "captured",
            "source": "focused_text",
            "text": source_text,
            "source_char_count": len(source_text),
            "target_key": "123:456:field",
            "application": "测试应用",
            "read_method": "system.parent1.AXStringForRange",
        }
    )

    backend.start()
    request = _client_request(ws.sent[0])
    serialized = request["request"]["corpus"]["context"]

    assert isinstance(serialized, str)
    assert json.loads(serialized) == {
        "context_type": "dialog_ctx",
        "context_data": [
            {"text": "最新一句？"},
            {"text": "更新一句！"},
            {"text": "较早一句。"},
        ],
    }
    assert backend.session_context_metadata == {
        "status": "sent",
        "source": "focused_text",
        "context_type": "dialog_ctx",
        "context_data": [
            {"text": "最新一句？"},
            {"text": "更新一句！"},
            {"text": "较早一句。"},
        ],
        "item_count": 3,
        "source_char_count": len(source_text),
        "sent_char_count": len(source_text.replace("\n", "")),
        "truncated": False,
        "reason": "",
        "target_key": "123:456:field",
        "application": "测试应用",
        "read_method": "system.parent1.AXStringForRange",
    }
    backend.abort()


def test_empty_context_is_omitted_and_old_context_does_not_leak(monkeypatch):
    monkeypatch.setenv("TEST_VOLC_KEY", "speech-app-key")
    sockets = [FakeWebSocket(), FakeWebSocket()]
    backend = VolcengineStreamingASR(
        api_key_env="TEST_VOLC_KEY",
        ws_factory=lambda *_args, **_kwargs: sockets.pop(0),
    )
    backend.set_session_context({"status": "captured", "text": "第一句。"})
    backend.start()
    first_socket = backend._ws
    backend.abort()

    backend.set_session_context(
        {
            "status": "unavailable",
            "source": "focused_text",
            "reason": "focused_text_unreadable:RuntimeError",
        }
    )
    backend.start()
    second_socket = backend._ws

    assert "corpus" in _client_request(first_socket.sent[0])["request"]
    assert "corpus" not in _client_request(second_socket.sent[0])["request"]
    assert backend.session_context_metadata["status"] == "unavailable"
    assert backend.session_context_metadata["context_data"] == []
    backend.abort()


def test_dialog_context_clips_items_and_character_budget_from_the_oldest_end():
    context_data, truncated = _dialog_context_data(
        "第一句。第二句。第三句。",
        max_items=2,
        max_chars=8,
    )

    assert context_data == [{"text": "第三句。"}, {"text": "第二句。"}]
    assert truncated


def test_server_close_after_final_is_not_reported_as_connection_loss(monkeypatch):
    monkeypatch.setenv("TEST_VOLC_KEY", "speech-app-key")
    ws = ClosingAfterFinalWebSocket()
    backend = VolcengineStreamingASR(
        api_key_env="TEST_VOLC_KEY",
        ws_factory=lambda *_args, **_kwargs: ws,
        chunk_ms=200,
    )

    backend.start()
    backend.feed(np.ones(3201, dtype=np.float32) * 0.1)
    final = backend.finish(np.empty(0, dtype=np.float32))

    assert final == "你好世界"
    assert ws.recv_calls == 2
    assert ws.closed


def test_response_summary_handles_empty_final_result(monkeypatch):
    monkeypatch.setenv("TEST_VOLC_KEY", "speech-app-key")
    backend = VolcengineStreamingASR(api_key_env="TEST_VOLC_KEY")
    empty = _response("", final=True)
    text, is_final, summary, sequence = backend._consume_frame(empty)

    assert text == ""
    assert is_final
    assert sequence is None
    assert "text_chars=0" in summary


def test_response_sequence_maps_a_partial_to_its_input_packet(monkeypatch):
    monkeypatch.setenv("TEST_VOLC_KEY", "speech-app-key")
    backend = VolcengineStreamingASR(api_key_env="TEST_VOLC_KEY")
    text, is_final, _, sequence = backend._consume_frame(
        _response("partial", final=False, sequence=7)
    )
    backend._packet_timings[7] = (12.5, 1.4)

    assert text == "partial"
    assert not is_final
    assert sequence == 7
    assert backend._timing_for_response(sequence) == (12.5, 1.4)


def test_backend_is_streaming_and_uses_new_console_key(monkeypatch):
    monkeypatch.setenv("TEST_VOLC_KEY", "speech-app-key")
    assert asr_backend_kind("volcengine") == "streaming"
    backend = create_streaming_asr_backend(
        "volcengine",
        ASRBackendSettings(options={"api_key_env": "TEST_VOLC_KEY"}),
    )
    assert backend.backend_name == "volcengine"
    assert backend.model_name == "seedasr-streaming"


def test_direct_api_key_from_ui_takes_precedence_over_environment(monkeypatch):
    monkeypatch.setenv("TEST_VOLC_KEY", "environment-key")
    backend = create_streaming_asr_backend(
        "volcengine",
        ASRBackendSettings(
            options={
                "api_key": "speech-ui-key",
                "api_key_env": "TEST_VOLC_KEY",
            }
        ),
    )

    assert backend.api_key == "speech-ui-key"


def test_api_key_is_required(monkeypatch):
    monkeypatch.delenv("MISSING_VOLC_KEY", raising=False)
    try:
        VolcengineStreamingASR(api_key_env="MISSING_VOLC_KEY")
    except RuntimeError as exc:
        assert "MISSING_VOLC_KEY" in str(exc)
    else:
        raise AssertionError("expected missing API key error")


class ControlledWebSocket(FakeWebSocket):
    """Socket whose responses arrive only when the test explicitly releases them."""

    def __init__(self):
        super().__init__()
        self.incoming: queue.Queue[bytes] = queue.Queue()
        self.receiving = threading.Event()
        self.final_sent = threading.Event()
        self.closed_event = threading.Event()

    def send(self, data: bytes):
        super().send(data)
        if data[1] == 0x23:
            self.final_sent.set()

    def recv(self):
        self.receiving.set()
        try:
            return self.incoming.get(timeout=0.01)
        except queue.Empty:
            raise TimeoutError() from None

    def close(self):
        self.closed = True
        self.closed_event.set()


def _invoke_in_thread(operation):
    values = []
    errors = []

    def run():
        try:
            values.append(operation())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, values, errors


def test_cancel_before_start_never_opens_connection():
    called = []
    backend = VolcengineStreamingASR(
        api_key="test", ws_factory=lambda *a, **k: called.append(True)
    )
    cancelled = threading.Event()
    cancelled.set()
    backend.set_cancel_event(cancelled)

    with pytest.raises(VolcengineASRCancelled):
        backend.start()

    assert called == []


def test_cancel_interrupts_connect_and_late_socket_cannot_replace_next_session():
    entered = threading.Event()
    release = threading.Event()
    first_socket = ControlledWebSocket()
    next_socket = FakeWebSocket()
    factory_calls = 0

    def factory(*_args, **_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            entered.set()
            release.wait(2)
            return first_socket
        return next_socket

    backend = VolcengineStreamingASR(api_key="test", ws_factory=factory, timeout_s=15)
    cancelled = threading.Event()
    backend.set_cancel_event(cancelled)
    thread, _, errors = _invoke_in_thread(backend.start)
    try:
        assert entered.wait(0.5)
        started = time.monotonic()
        cancelled.set()
        thread.join(timeout=0.4)
        assert not thread.is_alive(), "cancel waited for the 15-second connection"
        assert time.monotonic() - started < 0.4
        assert len(errors) == 1 and isinstance(errors[0], VolcengineASRCancelled)

        backend.set_cancel_event(threading.Event())
        backend.start()
        release.set()
        assert first_socket.closed_event.wait(0.5)
        assert first_socket.sent == []
        assert backend._ws is next_socket
        assert backend.finish(np.empty(0, dtype=np.float32)) == "你好世界"
    finally:
        release.set()
        backend.abort()


def test_connect_timeout_is_bounded_even_if_factory_ignores_its_timeout():
    release = threading.Event()
    late_socket = ControlledWebSocket()

    def factory(*_args, **_kwargs):
        release.wait(2)
        return late_socket

    backend = VolcengineStreamingASR(api_key="test", ws_factory=factory, timeout_s=0.05)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="connection timed out"):
            backend.start()
        assert time.monotonic() - started < 0.4
        release.set()
        assert late_socket.closed_event.wait(0.5)
        assert backend._ws is None
    finally:
        release.set()
        backend.abort()


def test_abort_interrupts_connect_without_needing_the_producer_event():
    entered = threading.Event()
    release = threading.Event()
    late_socket = ControlledWebSocket()

    def factory(*_args, **_kwargs):
        entered.set()
        release.wait(2)
        return late_socket

    backend = VolcengineStreamingASR(api_key="test", ws_factory=factory)
    thread, _, errors = _invoke_in_thread(backend.start)
    try:
        assert entered.wait(0.5)
        backend.abort()
        thread.join(timeout=0.4)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], VolcengineASRCancelled)
        release.set()
        assert late_socket.closed_event.wait(0.5)
    finally:
        release.set()
        backend.abort()


def test_cancel_interrupts_waiting_final_and_does_not_wait_eight_seconds():
    socket = ControlledWebSocket()
    cancelled = threading.Event()
    backend = VolcengineStreamingASR(
        api_key="test", ws_factory=lambda *a, **k: socket, final_timeout_s=8
    )
    backend.set_cancel_event(cancelled)
    backend.start()
    thread, _, errors = _invoke_in_thread(lambda: backend.finish(np.empty(0, dtype=np.float32)))
    assert socket.final_sent.wait(0.5)

    started = time.monotonic()
    cancelled.set()
    thread.join(timeout=0.4)

    assert not thread.is_alive()
    assert time.monotonic() - started < 0.4
    assert len(errors) == 1 and isinstance(errors[0], VolcengineASRCancelled)
    assert socket.closed
    assert backend._ws is None


def test_late_receiver_result_and_error_cannot_pollute_new_session():
    release = threading.Event()

    class LateReceiver(ControlledWebSocket):
        def recv(self):
            self.receiving.set()
            release.wait(2)
            return _response("旧句迟到结果", final=True)

    previous = LateReceiver()
    current = ControlledWebSocket()
    sockets = iter([previous, current])
    old_updates = []
    new_updates = []
    backend = VolcengineStreamingASR(api_key="test", ws_factory=lambda *a, **k: next(sockets))
    backend.set_partial_callback(lambda *update: old_updates.append(update))
    backend.start()
    assert previous.receiving.wait(0.5)
    backend.feed(np.ones(3201, dtype=np.float32) * 0.1)
    backend.abort()
    backend.set_cancel_event(threading.Event())
    backend.set_partial_callback(lambda *update: new_updates.append(update))
    backend.start()
    backend.feed(np.ones(3201, dtype=np.float32) * 0.1)
    try:
        release.set()
        current.incoming.put(_response("本句", final=False, sequence=2))
        deadline = time.monotonic() + 0.5
        while not new_updates and time.monotonic() < deadline:
            time.sleep(0.005)
        assert old_updates == []
        assert new_updates and new_updates[0][0] == "本句"
        assert backend._last_text == "本句"
        assert not backend._final_seen
        assert backend._receiver_error is None
        current.incoming.put(_response("本句完成", final=True))
        assert backend.finish(np.empty(0, dtype=np.float32)) == "本句完成"
    finally:
        release.set()
        backend.abort()


def test_receiver_callback_is_captured_for_its_own_session():
    socket = ControlledWebSocket()
    previous = []
    later = []
    backend = VolcengineStreamingASR(api_key="test", ws_factory=lambda *a, **k: socket)
    backend.set_partial_callback(lambda *update: previous.append(update))
    backend.start()
    backend.feed(np.ones(3201, dtype=np.float32) * 0.1)
    # Preparing a callback for another session must not relabel this receiver.
    backend.set_partial_callback(lambda *update: later.append(update))
    socket.incoming.put(_response("本句", final=False, sequence=2))
    deadline = time.monotonic() + 0.5
    while not previous and time.monotonic() < deadline:
        time.sleep(0.005)
    backend.abort()
    assert previous and previous[0][0] == "本句"
    assert later == []


def test_abort_does_not_wait_for_a_slow_custom_close_handshake():
    release = threading.Event()
    closing = threading.Event()

    class SlowClose(ControlledWebSocket):
        def close(self):
            closing.set()
            release.wait(2)
            super().close()

    socket = SlowClose()
    backend = VolcengineStreamingASR(api_key="test", ws_factory=lambda *a, **k: socket)
    backend.start()
    started = time.monotonic()
    try:
        backend.abort()
        assert time.monotonic() - started < 0.2
        assert closing.wait(0.5)
        assert backend._ws is None
    finally:
        release.set()
        assert socket.closed_event.wait(0.5)


def test_empty_final_completes_successfully_without_inventing_text():
    socket = FakeWebSocket()
    socket.responses = [_response("", final=True)]
    backend = VolcengineStreamingASR(api_key="test", ws_factory=lambda *a, **k: socket)
    backend.start()

    assert backend.finish(np.empty(0, dtype=np.float32)) == ""
    assert socket.closed


def test_no_response_final_times_out_and_next_utterance_recovers():
    silent = ControlledWebSocket()
    next_socket = FakeWebSocket()
    sockets = iter([silent, next_socket])
    backend = VolcengineStreamingASR(
        api_key="test", ws_factory=lambda *a, **k: next(sockets), final_timeout_s=0.05
    )
    backend.start()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="final response before timeout"):
        backend.finish(np.empty(0, dtype=np.float32))
    assert time.monotonic() - started < 0.4
    assert silent.closed
    backend.set_cancel_event(threading.Event())
    backend.start()
    assert backend.finish(np.empty(0, dtype=np.float32)) == "你好世界"


def test_configuration_send_failure_closes_the_accepted_connection():
    class FailedConfiguration(ControlledWebSocket):
        def send(self, data):
            raise ConnectionError("configuration send failed")

    socket = FailedConfiguration()
    backend = VolcengineStreamingASR(api_key="test", ws_factory=lambda *a, **k: socket)
    with pytest.raises(ConnectionError, match="configuration send failed"):
        backend.start()
    assert socket.closed
    assert backend._ws is None
