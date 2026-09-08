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


def test_first_stage2_occurs_300ms_after_stage1():
    pipe = FakePipeline([0.0])
    d = ProxiMicDetector(DetectorConfig(), pipe)

    first = d.feed(np.full(320, 0.5, np.float32))
    s1 = next(e for e in first if isinstance(e, Stage1Event))
    assert np.isclose(s1.time_s, 0.02)

    # 14 more chunks takes us to 0.30 s; due is at 0.32 s.
    events = feed_chunks(d, 14)
    assert not any(isinstance(e, Stage2Event) for e in events)
    events = d.feed(np.full(320, 0.5, np.float32))
    s2 = next(e for e in events if isinstance(e, Stage2Event))
    assert np.isclose(s2.time_s, 0.32)
    assert np.isclose(s2.window_start_s, -0.68)


def test_retrigger_after_rejection_is_about_320ms():
    pipe = FakePipeline([0.0, 0.0])
    d = ProxiMicDetector(DetectorConfig(), pipe)
    events = feed_chunks(d, 33, amp=0.5)
    s2 = [e for e in events if isinstance(e, Stage2Event)]
    assert len(s2) >= 2
    assert np.isclose(s2[1].time_s - s2[0].time_s, 0.32)


def test_active_session_runs_stage2_every_200ms_then_returns_to_initial_delay():
    pipe = FakePipeline([2.0] * 8)
    d = ProxiMicDetector(DetectorConfig(), pipe)
    events = feed_chunks(d, 16, amp=0.5)
    s2 = [e for e in events if isinstance(e, Stage2Event)]
    assert len(s2) == 1
    assert np.isclose(s2[0].time_s, 0.32)

    d.set_continuation_mode(True)
    active_events = feed_chunks(d, 20, amp=0.5)
    active_s2 = [e for e in active_events if isinstance(e, Stage2Event)]
    assert [round(e.time_s, 2) for e in active_s2] == [0.52, 0.72]
    assert any(isinstance(e, Stage1Event) for e in active_events)

    d.set_continuation_mode(False)
    idle_events = feed_chunks(d, 16, amp=0.5)
    idle_s2 = [e for e in idle_events if isinstance(e, Stage2Event)]
    assert len(idle_s2) == 1
    assert np.isclose(idle_s2[0].time_s - active_s2[-1].time_s, 0.32)
