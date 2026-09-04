import struct
import threading
import time
from types import SimpleNamespace

import numpy as np

from proximic_ring.audio.ring import RingAudioSource


def test_imu_callback_is_normalized_and_failure_does_not_poison_audio():
    import asyncio

    rows = []
    source = RingAudioSource(imu_observer=rows.append, imu_hz=50)

    class WorkingSession:
        async def imu_on(self, **kwargs):
            assert kwargs["gyro_hz"] == 50
            assert kwargs["accel_hz"] == 50
            assert kwargs["frames_per_packet"] == 10
            kwargs["on_sample"](
                SimpleNamespace(
                    sample_index=12,
                    packet_seq=3,
                    uptime_ms=456.5,
                    accel_ms2=(1.0, 2.0, 3.0),
                    gyro_dps=(4.0, 5.0, 6.0),
                    raw=(1, 2, 3, 4, 5, 6),
                )
            )

    asyncio.run(source._start_imu_best_effort(WorkingSession()))

    assert source.error is None
    assert source.imu_error is None
    assert source.imu_samples_received == 1
    assert rows[0]["device_uptime_ms"] == 456.5
    assert rows[0]["accel_ms2"] == [1.0, 2.0, 3.0]
    assert rows[0]["gyro_dps"] == [4.0, 5.0, 6.0]
    assert isinstance(rows[0]["host_monotonic_ns"], int)

    class FailingSession:
        async def imu_on(self, **_kwargs):
            raise RuntimeError("unsupported firmware")

    failed = RingAudioSource(imu_observer=rows.append)
    asyncio.run(failed._start_imu_best_effort(FailingSession()))
    assert failed.error is None
    assert str(failed.imu_error) == "unsupported firmware"


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
    assert isinstance(source.last_read_end_monotonic_ns, int)

    diagnostics = source.diagnostic_summary()
    assert "encoding=opus" in diagnostics
    assert "callbacks=1" in diagnostics
    assert "samples=4" in diagnostics
    assert "last_frame_seq=7" in diagnostics
    assert "rms=" in diagnostics
    assert "capture=unavailable" in diagnostics


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
            assert kwargs["battery_poll_enabled"] is False
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


def test_stream_monitor_keeps_connected_session_alive_until_audio_resumes(
    monkeypatch, tmp_path, capsys
):
    import sys
    import types

    from proximic_ring.audio import ring as ring_module

    counters = {"connect": 0, "mic_on": 0, "disconnect": 0}
    callback_holder = {}

    class FakeClient:
        is_connected = True

    class FakeMic:
        def __init__(self):
            self.output_path = tmp_path / "stalled.wav"

    class FakeSession:
        latest = None

        def __init__(self, **kwargs):
            assert kwargs["auto_reconnect"] is False
            assert kwargs["battery_poll_enabled"] is False
            self.client = FakeClient()
            self.mic_active = False
            self.mic = None
            type(self).latest = self

        async def connect(self):
            counters["connect"] += 1
            return True

        async def mic_on(self, encoding, *, on_pcm=None):
            counters["mic_on"] += 1
            self.mic_active = True
            self.mic = FakeMic()
            assert on_pcm is not None
            callback_holder["on_pcm"] = on_pcm
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

    source = RingAudioSource(data_root=tmp_path, encoding="opus")
    source.open()
    try:
        assert source.read(320) is not None
        time.sleep(0.10)
        assert source.error is None
        assert counters["disconnect"] == 0

        callback_holder["on_pcm"](
            1, struct.pack("<320h", *([2048] * 320))
        )
        time.sleep(0.03)
        resumed = source.read(320)
        assert resumed is not None
        np.testing.assert_allclose(
            resumed, np.full(320, 0.0625, dtype=np.float32)
        )

        assert FakeSession.latest is not None
        FakeSession.latest.client.is_connected = False
        deadline = time.monotonic() + 1.0
        while source.error is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert source.error is not None
        assert "physically lost" in str(source.error)
    finally:
        source.close()

    output = capsys.readouterr().out
    assert "PCM STREAM STALLED" in output
    assert "Keeping the BLE session open" in output
    assert "PCM callbacks resumed" in output
    assert counters == {"connect": 1, "mic_on": 1, "disconnect": 1}


def test_stream_monitor_restarts_mic_once_for_early_startup_stall(
    monkeypatch, tmp_path, capsys
):
    import sys
    import types

    from proximic_ring.audio import ring as ring_module

    counters = {"connect": 0, "mic_on": 0, "mic_off": 0, "disconnect": 0}

    class FakeClient:
        is_connected = True

    class FakeMic:
        def __init__(self):
            self.output_path = tmp_path / f"segment-{counters['mic_on']}.wav"

    class FakeSession:
        def __init__(self, **kwargs):
            assert kwargs["auto_reconnect"] is False
            assert kwargs["battery_poll_enabled"] is False
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
            value = 1024 * counters["mic_on"]
            on_pcm(
                counters["mic_on"] - 1,
                struct.pack("<320h", *([value] * 320)),
            )

        async def mic_off(self):
            counters["mic_off"] += 1
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
    monkeypatch.setattr(ring_module, "_INITIAL_MIC_SETTLE_S", 0.0)
    monkeypatch.setattr(ring_module, "_EARLY_STARTUP_STALL_S", 0.04)
    monkeypatch.setattr(ring_module, "_MIC_RECOVERY_PAUSE_S", 0.01)
    monkeypatch.setattr(ring_module, "_WATCHDOG_POLL_S", 0.01)
    monkeypatch.setattr(ring_module, "_WATCHDOG_STALL_S", 1.0)

    source = RingAudioSource(data_root=tmp_path, encoding="opus")
    source.open()
    try:
        first = source.read(320)
        assert first is not None

        deadline = time.monotonic() + 1.0
        while counters["mic_on"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        assert counters["mic_on"] == 2
        assert counters["mic_off"] == 1
        assert counters["disconnect"] == 0
        assert source.error is None
        second = source.read(320)
        assert second is not None
        np.testing.assert_allclose(
            second, np.full(320, 0.0625, dtype=np.float32)
        )
    finally:
        source.close()

    output = capsys.readouterr().out
    assert "restarting MIC once" in output
    assert "MIC startup recovery succeeded" in output
    assert counters == {"connect": 1, "mic_on": 2, "mic_off": 2, "disconnect": 1}


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


def test_windows_cancel_during_cleanup_does_not_escape():
    import asyncio

    calls = {"disconnect": 0}

    class FakeSession:
        mic_active = True

        async def mic_off(self):
            exc = OSError("The I/O operation has been aborted")
            exc.winerror = 995
            raise exc

        async def disconnect(self):
            calls["disconnect"] += 1
            exc = OSError("The operation was canceled by the user")
            exc.winerror = 1223
            raise exc

    source = RingAudioSource()
    asyncio.run(source._shutdown_session(FakeSession()))

    assert calls["disconnect"] == 1
