import struct
import threading
import time

import numpy as np
import pytest

from proximic_ring.audio.ring import RingAudioSource


def test_ring_pcm_callback_becomes_float32_audio():
    source = RingAudioSource(queue_blocks=4)
    pcm = struct.pack("<4h", 0, 16384, -16384, 32767)

    assert source._ready.is_set() is False
    source._on_pcm(7, pcm)
    assert source._ready.is_set() is True
    out = source.read(4)

    assert out is not None
    assert out.dtype == np.float32
    np.testing.assert_allclose(
        out,
        np.array([0.0, 0.5, -0.5, 32767 / 32768], dtype=np.float32),
        atol=1e-7,
    )
    assert source.pcm_callbacks == 1
    assert source.samples_received == 4


def test_empty_pcm_callback_does_not_mark_ring_ready():
    source = RingAudioSource(queue_blocks=4)

    source._on_pcm(7, b"")

    assert source._ready.is_set() is False
    assert source.pcm_callbacks == 0
    assert source.samples_received == 0


def test_ring_source_can_rechunk_sdk_callbacks():
    source = RingAudioSource(queue_blocks=4)
    source._on_pcm(1, struct.pack("<3h", 100, 200, 300))
    source._on_pcm(2, struct.pack("<3h", 400, 500, 600))

    first = source.read(4)
    second = source.read(2)

    assert first is not None and second is not None
    scale = np.float32(32768.0)
    np.testing.assert_allclose(first, np.array([100, 200, 300, 400], np.float32) / scale)
    np.testing.assert_allclose(second, np.array([500, 600], np.float32) / scale)


def test_ring_source_thread_uses_sdk_live_callback(monkeypatch, tmp_path):
    import sys
    import types
    from proximic_ring.audio import ring as ring_module

    monkeypatch.setattr(ring_module, "_MIC_RESTART_PAUSE_S", 0.05)

    class FakeMic:
        def __init__(self):
            self.output_path = tmp_path / "fake_ring.wav"

    class FakeSession:
        mic_on_calls = 0
        mic_off_at = None
        restart_gap_s = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.mic_active = False
            self.mic = None

        async def connect(self):
            return True

        async def connect_target(self, selector):
            return selector == "0"

        async def mic_on(self, encoding, *, on_pcm=None):
            assert encoding == "pcm"
            type(self).mic_on_calls += 1
            if type(self).mic_off_at is not None:
                type(self).restart_gap_s = time.monotonic() - type(self).mic_off_at
            self.mic_active = True
            self.mic = FakeMic()
            assert on_pcm is not None
            on_pcm(0, struct.pack("<320h", *([4096] * 320)))

        async def mic_off(self):
            self.mic_active = False
            type(self).mic_off_at = time.monotonic()

        async def disconnect(self):
            return None

    sdk = types.ModuleType("ring_python_sdk")
    sdk.RingSession = FakeSession
    core = types.ModuleType("ring_python_sdk.core")
    constants = types.ModuleType("ring_python_sdk.core.constants")
    constants.DEFAULT_SAMPLE_RATE = 16_000
    constants.DEFAULT_CHANNELS = 1
    constants.DEFAULT_SAMPLE_WIDTH_BYTES = 2

    monkeypatch.setitem(sys.modules, "ring_python_sdk", sdk)
    monkeypatch.setitem(sys.modules, "ring_python_sdk.core", core)
    monkeypatch.setitem(sys.modules, "ring_python_sdk.core.constants", constants)

    source = RingAudioSource(data_root=tmp_path, encoding="pcm")
    source.connect()
    try:
        assert FakeSession.mic_on_calls == 0
        source.start_stream(buffer_audio=False)
        assert FakeSession.mic_on_calls == 1
        source.pause_stream()
        assert source._stream_paused.is_set()
        source.begin_buffering()
        assert FakeSession.mic_on_calls == 2
        assert FakeSession.restart_gap_s is not None
        assert FakeSession.restart_gap_s >= 0.045
        block = source.read(320)
        assert block is not None and block.shape == (320,)
        np.testing.assert_allclose(block, np.full(320, 0.125, dtype=np.float32))
        assert source.capture_path == tmp_path / "fake_ring.wav"
    finally:
        source.close()


def test_watchdog_disconnects_once_without_recovery(monkeypatch, tmp_path):
    import sys
    import types

    from proximic_ring.audio import ring as ring_module

    counters = {"connect": 0, "mic_on": 0, "disconnect": 0}

    class FakeClient:
        is_connected = True

    class FakeMic:
        def __init__(self):
            self.output_path = tmp_path / "stalled.wav"

    class FakeSession:
        def __init__(self, **kwargs):
            assert kwargs["auto_reconnect"] is False
            self.client = FakeClient()
            self.mic_active = False
            self.mic = None

        async def connect(self):
            counters["connect"] += 1
            return True

        async def mic_on(self, encoding, *, on_pcm=None):
            counters["mic_on"] += 1
            self.mic_active = True
            self.mic = FakeMic()
            assert on_pcm is not None
            on_pcm(0, struct.pack("<320h", *([1024] * 320)))

        async def mic_off(self):
            self.mic_active = False

        async def disconnect(self):
            counters["disconnect"] += 1
            self.client.is_connected = False

    sdk = types.ModuleType("ring_python_sdk")
    sdk.RingSession = FakeSession
    core = types.ModuleType("ring_python_sdk.core")
    constants = types.ModuleType("ring_python_sdk.core.constants")
    constants.DEFAULT_SAMPLE_RATE = 16_000
    constants.DEFAULT_CHANNELS = 1
    constants.DEFAULT_SAMPLE_WIDTH_BYTES = 2
    monkeypatch.setitem(sys.modules, "ring_python_sdk", sdk)
    monkeypatch.setitem(sys.modules, "ring_python_sdk.core", core)
    monkeypatch.setitem(sys.modules, "ring_python_sdk.core.constants", constants)
    monkeypatch.setattr(ring_module, "_WATCHDOG_STALL_S", 0.05)
    monkeypatch.setattr(ring_module, "_WATCHDOG_POLL_S", 0.01)
    monkeypatch.setattr(ring_module, "_WATCHDOG_CONFIRM_S", 0.01)

    # UI model loading starts the stream only as a physical validation probe.
    # A long model import must not be mistaken for a device stall.
    probe = RingAudioSource(data_root=tmp_path, encoding="pcm")
    probe.connect()
    probe.start_stream(buffer_audio=False)
    time.sleep(0.08)
    assert probe.error is None
    probe.close()

    source = RingAudioSource(data_root=tmp_path, encoding="pcm")
    source.open()
    try:
        assert source.read(320) is not None
        with pytest.raises(RuntimeError, match="reconnect manually"):
            source.read(320)
    finally:
        source.close()

    assert counters == {"connect": 2, "mic_on": 2, "disconnect": 2}


def test_begin_buffering_requires_fresh_post_model_pcm(tmp_path):
    source = RingAudioSource(data_root=tmp_path, encoding="pcm")
    source._buffer_audio.clear()
    source.pcm_callbacks = 1
    pcm = struct.pack("<320h", *([2048] * 320))
    callback = threading.Timer(0.02, lambda: source._on_pcm(2, pcm))
    callback.start()
    try:
        source.begin_buffering()
        assert source._watchdog_armed.is_set()
        block = source.read(320)
        assert block is not None and block.shape == (320,)
    finally:
        callback.join()
        source.close()
