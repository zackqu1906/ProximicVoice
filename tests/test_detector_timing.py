import numpy as np

from proximic_ring.config import DetectorConfig
from proximic_ring.detector import ProxiMicDetector
from proximic_ring.events import Stage1Event, Stage2Event
from proximic_ring.pipeline import InferenceResult


class FakePipeline:
    def __init__(self, scores):
        self.scores = iter(scores)
        self.windows = []

    def infer_window(self, audio_16k):
        self.windows.append(audio_16k.copy())
        score = next(self.scores)
        return InferenceResult(logits=(score, 0.0), score=score)


def feed_chunks(detector, count, amp=0.5):
    events = []
    block = np.full(320, amp, dtype=np.float32)
    for _ in range(count):
        events.extend(detector.feed(block))
    return events


def test_stage2_occurs_half_second_after_stage1():
    pipe = FakePipeline([0.0])
    d = ProxiMicDetector(DetectorConfig(), pipe)

    first = d.feed(np.full(320, 0.5, np.float32))
    s1 = next(e for e in first if isinstance(e, Stage1Event))
    assert np.isclose(s1.time_s, 0.02)

    # 24 more chunks takes us to 0.50 s; due is at 0.52 s.
    events = feed_chunks(d, 24)
    assert not any(isinstance(e, Stage2Event) for e in events)
    events = d.feed(np.full(320, 0.5, np.float32))
    s2 = next(e for e in events if isinstance(e, Stage2Event))
    assert np.isclose(s2.time_s, 0.52)
    assert np.isclose(s2.window_start_s, -0.48)


def test_retrigger_after_rejection_is_about_520ms():
    pipe = FakePipeline([0.0, 0.0])
    d = ProxiMicDetector(DetectorConfig(), pipe)
    events = feed_chunks(d, 60, amp=0.5)
    s2 = [e for e in events if isinstance(e, Stage2Event)]
    assert len(s2) >= 2
    assert np.isclose(s2[1].time_s - s2[0].time_s, 0.52)


def test_success_has_no_cooldown_and_retriggers_about_520ms_later():
    pipe = FakePipeline([2.0] + [0.0] * 8)
    d = ProxiMicDetector(DetectorConfig(), pipe)
    events = feed_chunks(d, 120, amp=0.5)
    s2 = [e for e in events if isinstance(e, Stage2Event)]
    assert len(s2) >= 2
    # Stage2 #1 at 0.52 s.  With cooldown removed, the next 20 ms block can
    # trigger Stage1 immediately and Stage2 #2 arrives another ~0.52 s later.
    assert np.isclose(s2[0].time_s, 0.52)
    assert np.isclose(s2[1].time_s, 1.04)
