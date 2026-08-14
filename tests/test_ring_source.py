import struct

import numpy as np

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

    class FakeMic:
        def __init__(self):
            self.output_path = tmp_path / "fake_ring.wav"

    class FakeSession:
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
            self.mic_active = True
            self.mic = FakeMic()
            assert on_pcm is not None
            on_pcm(0, struct.pack("<320h", *([4096] * 320)))

        async def mic_off(self):
            self.mic_active = False

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
    source.open()
    try:
        block = source.read(320)
        assert block is not None and block.shape == (320,)
        np.testing.assert_allclose(block, np.full(320, 0.125, dtype=np.float32))
        assert source.capture_path == tmp_path / "fake_ring.wav"
    finally:
        source.close()
