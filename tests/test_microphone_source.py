import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from proximic_ring.audio import microphone as module


def test_dji_resolution_uses_identity_after_reorder_and_never_default_fallback(monkeypatch):
    rows = [
        {"name": "Built-in", "api": "Core Audio", "index": 0},
        {"name": "DJI Mic", "api": "Core Audio", "index": 4},
    ]
    monkeypatch.setattr(module, "input_device_choices", lambda: rows)
    assert module.resolve_input_device("")["index"] == 4
    saved = json.dumps({"name": "DJI Mic", "api": "Core Audio", "index": 2})
    assert module.resolve_input_device(saved)["index"] == 4
    rows.append({"name": "DJI Mic 2", "api": "Core Audio", "index": 5})
    with pytest.raises(RuntimeError, match="多个 DJI"):
        module.resolve_input_device("")
    del rows[1:]
    for selection in ("", saved):
        with pytest.raises(RuntimeError, match="不会改用其他"):
            module.resolve_input_device(selection)


@pytest.mark.parametrize("rate", [16000, 44100, 48000, 96000])
def test_resampling_has_no_drift_or_callback_boundary_discontinuities(rate):
    t = np.arange(rate * 2) / rate
    audio = (.3 * np.sin(2 * np.pi * 500 * t) + .1 * np.sin(2 * np.pi * 3000 * t)).astype(np.float32)
    reference = module._StreamingMonoResampler(rate).process(audio)
    stream = module._StreamingMonoResampler(rate)
    # Arbitrary callback lengths exercise fractional phase across boundaries.
    chunks = [stream.process(audio[index:index + 317]) for index in range(0, audio.size, 317)]
    actual = np.concatenate(chunks)
    assert len(actual) == 32000
    np.testing.assert_allclose(actual, reference, atol=1e-6)
    if rate == 16000:
        np.testing.assert_array_equal(actual, audio)


def test_downsampling_filters_out_of_band_noise():
    t = np.arange(48000) / 48000
    def rms_at(frequency):
        audio = np.sin(2 * np.pi * frequency * t).astype(np.float32)
        output = module._StreamingMonoResampler(48000).process(audio)[100:]
        return np.sqrt(np.mean(output ** 2))
    assert rms_at(12000) < rms_at(1000) * .01


def test_native_rate_fallback_and_mic_failure_are_interruptible(monkeypatch):
    calls = []
    class PortAudioError(Exception):
        pass

    def check(**kwargs):
        calls.append(kwargs["samplerate"])
        if kwargs["samplerate"] == 16000:
            raise PortAudioError("unsupported rate")

    class Stream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            self.kwargs["callback"](
                np.full((960, 1), .25, dtype=np.float32), 960,
                SimpleNamespace(currentTime=.02, inputBufferAdcTime=0), None,
            )

        def abort(self):
            calls.append("abort")
            self.kwargs["finished_callback"]()

        def close(self):
            calls.append("close")

    sd = SimpleNamespace(
        PortAudioError=PortAudioError, check_input_settings=check, InputStream=Stream,
        query_devices=lambda *args: {"name": "DJI Mic", "default_samplerate": 48000},
    )
    monkeypatch.setattr(module, "_sounddevice", lambda: sd)
    source = module.MicrophoneSource(device=3)
    source.open()
    assert calls == [16000, 48000]
    samples = source.read(320)
    assert samples.shape == (320,) and samples.dtype == np.float32
    np.testing.assert_allclose(samples[50:], .25, atol=1e-5)
    assert source.last_read_end_monotonic_ns > 0
    source._on_finished()
    with pytest.raises(RuntimeError, match="连接已中断"):
        source.read(320)
    source.close()
    assert calls[-2:] == ["abort", "close"]


def test_close_unblocks_read_without_submitting_partial_audio():
    source = module.MicrophoneSource()
    result = []
    thread = threading.Thread(target=lambda: result.append(source.read(320)))
    thread.start()
    source.close()
    thread.join(1)
    assert not thread.is_alive() and result == [None]


def test_overflow_is_reported_instead_of_silently_losing_speech():
    source = module.MicrophoneSource()
    source._on_audio(np.ones((320, 1)), 320, SimpleNamespace(), "input overflow")
    with pytest.raises(RuntimeError, match="overflow"):
        source.read(320)
