import numpy as np
import pytest
from scipy import signal

from ring_whisper_lab.alignment import align_take
from ring_whisper_lab.storage import SAMPLE_RATE as SR, audio_stats, new_take, read_json, read_wav, write_json, write_wav
from ring_whisper_lab.sync_markers import PROTOCOL, marker_wave, estimate_marker_alignment


def pair(delay=733.3, ppm=240):
    rng = np.random.default_rng(811)
    n = 15*SR
    clean = rng.normal(0, .006, n)
    for kind, start in (("start", round(.4*SR)), ("end", 13*SR)):
        marker = marker_wave(kind)
        clean[start:start+len(marker)] += marker
    # Intervening audio is deliberately unlike between channels; synchronization must use markers.
    x = clean.copy()
    x[2*SR:12*SR] += rng.normal(0, .025, 10*SR)
    y = .55*np.interp((np.arange(n)-delay)/(1+ppm/1e6), np.arange(n), clean, left=0, right=0)
    y = signal.lfilter([1, .12], [1], y) + rng.normal(0, .0005, n)
    return x, y


def take_from_pair(root, x, y):
    take = new_take(root, {"sync_protocol": PROTOCOL, "speaker_id": "test", "session_group": "test"}, {}, demo=True)
    record = read_json(take / "record.json")
    record.update(status="captured", quality={})
    for role, audio in (("input", x), ("reference", y)):
        write_wav(take / "raw" / f"{role}.wav", audio)
        record["quality"][role] = {**audio_stats(audio), "gaps": [], "errors": [], "incomplete_frames": 0}
    write_json(take / "record.json", record)
    return take


@pytest.mark.parametrize("delay,ppm", [(733.3, 240), (-410.7, -300), (0, 0)])
def test_bookends_recover_offset_and_drift_without_matching_speech(delay, ppm):
    x, y = pair(delay, ppm)
    result = estimate_marker_alignment(x, y)
    assert abs(result["offset_samples"]-delay) < 3
    assert abs(result["drift_ppm"]-ppm) < 15
    # Known simulation truth, including the unobserved middle of the recording.
    positions = np.linspace(2*SR, 12*SR, 30)
    error = result["offset_samples"] + result["slope"]*positions - (delay + ppm/1e6*positions)
    assert max(abs(error)) < 3
    assert result["automatic_quality_pass"], result["warnings"]


def test_output_crops_both_markers_preserves_pauses_and_raw(tmp_path):
    x, y = pair()
    x[5*SR:6*SR] = 0
    take = take_from_pair(tmp_path, x, y)
    before = (take / "raw/input.wav").read_bytes()
    result = align_take(take)
    assert result["method"] == PROTOCOL
    assert 1.5*SR < result["input_start_sample"] < 1.7*SR
    assert 12.7*SR < result["input_end_sample"] < 13*SR
    assert (take / "raw/input.wav").read_bytes() == before
    a = read_wav(take / result["revision"] / "input.wav")
    b = read_wav(take / result["revision"] / "reference.wav")
    assert len(a) == len(b) == result["sample_count"]
    start = result["input_start_sample"]
    np.testing.assert_equal(a[5*SR-start:6*SR-start], 0)  # no VAD or pause removal
    assert (take / result["revision"] / "stereo.wav").is_file()


@pytest.mark.parametrize("missing", ["start", "end", "all"])
def test_missing_markers_never_fall_back_to_speech(tmp_path, missing):
    x, y = pair()
    if missing in ("start", "all"):
        y[:2*SR] = 0
    if missing in ("end", "all"):
        y[12*SR:] = 0
    take = take_from_pair(tmp_path, x, y)
    with pytest.raises(ValueError):
        align_take(take)
    assert "alignment" not in read_json(take / "record.json")
    assert not list(take.glob("aligned_*"))


def test_transport_gap_blocks_affine_crop(tmp_path):
    take = take_from_pair(tmp_path, *pair())
    record = read_json(take / "record.json")
    record["quality"]["input"]["gaps"] = [{"start_sample": SR, "end_sample": 2*SR}]
    write_json(take / "record.json", record)
    with pytest.raises(ValueError, match="丢帧"):
        align_take(take)


def test_repeated_start_is_ambiguous():
    x, y = pair(0, 0)
    x[3*SR:3*SR+len(marker_wave("start"))] += marker_wave("start")
    with pytest.raises(ValueError, match="多个"):
        estimate_marker_alignment(x, y)


def test_marker_matching_survives_independent_opus_encoding(monkeypatch):
    import ctypes
    from ring_python_sdk.audio.opus_codec import _load_opuslib, OpusUnavailableError
    try:
        opus = _load_opuslib()
    except OpusUnavailableError:
        pytest.skip("optional native libopus unavailable")
    # Declare fixed arguments of the variadic ctl API (required by macOS ARM64 ABI).
    monkeypatch.setattr(opus.api.encoder.libopus_ctl, "argtypes",
                        [opus.api.encoder.EncoderPointer, ctypes.c_int])

    def roundtrip(audio):
        encoder = opus.Encoder(SR, 1, opus.APPLICATION_AUDIO)
        encoder.bitrate = 24000
        decoder = opus.Decoder(SR, 1)
        pcm = np.rint(np.clip(audio, -1, 32767/32768)*32768).astype('<i2')
        out = []
        for start in range(0, len(pcm), 320):
            packet = encoder.encode(pcm[start:start+320].tobytes(), 320)
            out.append(np.frombuffer(decoder.decode(packet, 320), '<i2').astype(float)/32768)
        return np.concatenate(out)

    result = estimate_marker_alignment(*(roundtrip(audio) for audio in pair()))
    assert abs(result["offset_samples"]-733.3) < 8
    assert abs(result["drift_ppm"]-240) < 30


def test_playback_files_have_guards_and_no_clipping(tmp_path):
    from ring_whisper_lab.sync_markers import playback_file
    for kind in ("start", "end"):
        audio = read_wav(playback_file(tmp_path, kind))
        assert max(abs(audio)) < .36
        np.testing.assert_equal(audio[:round(.2*SR)], 0)
        np.testing.assert_equal(audio[-round(.45*SR):], 0)
