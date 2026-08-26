import numpy as np

from proximic_ring.asr.session_sink import ASRInputGainSessionSink


class RecordingSink:
    def __init__(self):
        self.calls = []
        self.aborted = False
        self.closed = False

    def start(self, audio):
        self.calls.append(("start", np.asarray(audio).copy()))

    def feed(self, audio):
        self.calls.append(("feed", np.asarray(audio).copy()))

    def end(self, audio):
        self.calls.append(("end", np.asarray(audio).copy()))

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True


def test_asr_input_gain_applies_24_db_to_every_session_phase_and_clips():
    target = RecordingSink()
    sink = ASRInputGainSessionSink(target, gain_db=24.0)
    original = np.asarray([-0.1, 0.0, 0.01, 0.1], dtype=np.float32)
    original_snapshot = original.copy()
    expected = np.clip(original * (10.0 ** (24.0 / 20.0)), -1.0, 1.0)

    sink.start(original)
    sink.feed(original)
    sink.end(original)
    sink.abort()
    sink.close()

    assert [name for name, _audio in target.calls] == ["start", "feed", "end"]
    for _name, audio in target.calls:
        np.testing.assert_allclose(audio, expected, rtol=1e-6, atol=1e-7)
        assert audio.dtype == np.float32
    np.testing.assert_array_equal(original, original_snapshot)
    assert target.aborted is True
    assert target.closed is True
