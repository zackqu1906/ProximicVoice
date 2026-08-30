from __future__ import annotations

import gzip
import json
import struct
import time

import numpy as np

from proximic_ring.asr.backends.volcengine import (
    DEFAULT_RESOURCE_ID,
    VolcengineStreamingASR,
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
