"""Streaming behavior checked against ai-ring fa505477's desktop runtime.

The frame numbers below were obtained by executing the original window/vote/
cooldown methods with a synchronous fake worker, without loading its model/UI.
"""

from collections import deque

import numpy as np
import pytest

from ring_python_sdk.gestures.classifier import GesturePrediction
from ring_python_sdk.gestures.runtime import GestureRecognizer
from ring_python_sdk.imu.processor import ImuSample


NAMES = ("empty", "swipe-up", "swipe-down", "swipe-left", "swipe-right", "tap", "snap")


class FakeClassifier:
    window_size = 60
    class_ids = tuple(range(7))

    def __init__(self, predictions=(), *, default=1):
        self.predictions = deque(predictions)
        self.default = default
        self.windows = []

    def predict(self, values):
        self.windows.append(values.copy())
        class_id = self.predictions.popleft() if self.predictions else self.default
        probabilities = [0.02] * 7
        probabilities[class_id] = 0.88
        return GesturePrediction(class_id, NAMES[class_id], 0.88, tuple(probabilities))


class FakeAdapter:
    sample_hz = 200.0

    def __init__(self):
        self.reset_count = 0

    def reset(self):
        self.reset_count += 1

    def __call__(self, sample):
        return np.asarray((*sample.accel_ms2, *sample.gyro_dps), dtype=np.float32)


def feed_frames(recognizer, count, *, start=1):
    events = []
    for frame in range(start, start + count):
        event = recognizer.feed([frame] * 6, timestamp_ms=frame * 5)
        if event is not None:
            events.append(event)
    return events


def make_sample(index, seq, timestamp):
    return ImuSample(index, seq, timestamp, (1.0, 2.0, 3.0), (4.0, 5.0, 6.0))


def test_default_frame_schedule_and_exact_half_window_refill():
    classifier = FakeClassifier()
    callbacks = []
    recognizer = GestureRecognizer(classifier=classifier, on_gesture=callbacks.append)
    assert feed_frames(recognizer, 59) == []
    assert classifier.windows == []
    events = feed_frames(recognizer, 141, start=60)
    assert [event.timestamp_ms / 5 for event in events] == [75, 131, 187]
    assert callbacks == events
    assert [int(window[-1, 0]) for window in classifier.windows] == [
        60, 65, 70, 75, 116, 121, 126, 131, 172, 177, 182, 187
    ]
    np.testing.assert_array_equal(classifier.windows[0][:, 0], np.arange(1, 61))
    np.testing.assert_array_equal(
        classifier.windows[4][:, 0], [85] * 29 + list(range(86, 117))
    )


def test_zero_delay_keeps_existing_samples_and_repeats_every_four_predictions():
    classifier = FakeClassifier()
    recognizer = GestureRecognizer(classifier=classifier, reset_delay_frames=0)
    events = feed_frames(recognizer, 200)
    assert [event.timestamp_ms / 5 for event in events] == list(range(75, 200, 20))
    assert [int(window[-1, 0]) for window in classifier.windows] == list(range(60, 201, 5))
    np.testing.assert_array_equal(classifier.windows[4][:, 0], np.arange(21, 81))


@pytest.mark.parametrize("class_id", range(1, 7))
def test_all_swipe_directions_tap_and_snap(class_id):
    recognizer = GestureRecognizer(classifier=FakeClassifier(default=class_id))
    events = feed_frames(recognizer, 75)
    assert len(events) == 1
    assert (events[0].class_id, events[0].name) == (class_id, NAMES[class_id])
    assert events[0].confidence == pytest.approx(0.88)
    assert len(events[0].probabilities) == 7


def test_empty_and_unstable_classes_never_trigger():
    classifier = FakeClassifier([0, 1, 2, 3, 4, 5, 6, 0], default=0)
    recognizer = GestureRecognizer(classifier=classifier)
    assert feed_frames(recognizer, 200) == []
    assert recognizer.last_prediction.class_id == 0


@pytest.mark.parametrize(
    "votes,ratio,expected",
    [
        ([1, 1, 2, 1], 0.75, 1),
        ([1, 1, 1, 2], 0.75, 1),
        ([1, 2, 2, 1], 0.75, None),
        ([1, 1, 2, 1], 0.750001, None),
        ([2, 2, 1, 1], 0.5, 1),
    ],
)
def test_vote_threshold_is_inclusive_and_confidence_belongs_to_winner(votes, ratio, expected):
    recognizer = GestureRecognizer(
        classifier=FakeClassifier(votes), positive_ratio=ratio
    )
    # A majority cannot trigger before all four prediction slots are filled.
    assert feed_frames(recognizer, 74) == []
    events = feed_frames(recognizer, 1, start=75)
    assert [event.class_id for event in events] == ([] if expected is None else [expected])
    if events:
        assert events[0].name == NAMES[expected]
        assert events[0].confidence == events[0].probabilities[expected]
        assert events[0].confidence == pytest.approx(0.88 if votes[-1] == expected else 0.02)


def test_streams_and_reset_do_not_share_votes_samples_or_cooldown():
    adapter = FakeAdapter()
    first = GestureRecognizer(classifier=FakeClassifier(default=5), adapter=adapter)
    second = GestureRecognizer(classifier=FakeClassifier(default=6))
    assert feed_frames(first, 70) == []
    assert feed_frames(second, 59) == []
    first.reset()
    assert first.last_prediction is None
    assert adapter.reset_count == 2
    assert feed_frames(first, 74) == []
    assert feed_frames(first, 1, start=75)[0].name == "tap"
    assert feed_frames(second, 16, start=60)[0].name == "snap"
    first.reset()  # Reset also discards the just-started cooldown.
    assert [event.name for event in feed_frames(first, 75)] == ["tap"]


@pytest.mark.parametrize("timestamp", [350.0, 345.0, 365.0])
def test_duplicate_backward_and_missing_sample_times_reset_pending_votes(timestamp):
    recognizer = GestureRecognizer(classifier=FakeClassifier())
    assert feed_frames(recognizer, 70) == []
    assert recognizer.feed([0] * 6, timestamp_ms=timestamp) is None
    assert recognizer.last_prediction is None
    for offset in range(1, 74):
        assert recognizer.feed([0] * 6, timestamp_ms=timestamp + offset * 5) is None
    event = recognizer.feed([0] * 6, timestamp_ms=timestamp + 74 * 5)
    assert event is not None


@pytest.mark.parametrize("index,seq,timestamp", [(72, 7, 355), (71, 8, 355), (0, 0, 0)])
def test_sample_index_packet_gaps_and_reconnect_reset_adapter_and_votes(index, seq, timestamp):
    adapter = FakeAdapter()
    recognizer = GestureRecognizer(classifier=FakeClassifier(), adapter=adapter)
    for frame in range(1, 71):
        assert recognizer.on_sample(make_sample(frame, (frame - 1) // 10, frame * 5)) is None
    assert recognizer.on_sample(make_sample(index, seq, timestamp)) is None
    assert adapter.reset_count == 2
    assert recognizer.last_prediction is None
    for offset in range(1, 74):
        assert recognizer.on_sample(
            make_sample(index + offset, seq + offset // 10, timestamp + offset * 5)
        ) is None
    assert recognizer.on_sample(make_sample(index + 74, seq + 7, timestamp + 370)) is not None


def test_uint16_packet_and_uint32_uptime_wrap_preserve_continuous_stream():
    adapter = FakeAdapter()
    recognizer = GestureRecognizer(classifier=FakeClassifier(), adapter=adapter)
    events = []
    for frame in range(75):
        timestamp = ((1 << 32) - 200 + frame * 5) % (1 << 32)
        seq = (65530 + frame // 10) & 0xFFFF
        event = recognizer.on_sample(make_sample(frame, seq, timestamp))
        if event is not None:
            events.append(event)
    assert [event.timestamp_ms for event in events] == [170.0]
    assert adapter.reset_count == 1


def test_mic_packet_timestamp_jitter_does_not_erase_continuous_motion():
    # Reproduce alternating overlapping/delayed packet tails observed with MIC
    # active. Sample indexes and packet sequence prove that no frames were lost.
    recognizer = GestureRecognizer(classifier=FakeClassifier(), adapter=FakeAdapter())
    events = []
    for frame in range(75):
        packet = frame // 10
        timestamp = frame * 5 + (10 if packet % 2 else 0)
        event = recognizer.on_sample(make_sample(frame, packet, timestamp))
        if event is not None:
            events.append(event)
    assert [event.name for event in events] == ["swipe-up"]
    assert recognizer.reset_count == 1
    assert recognizer.timestamp_jitter_count == 7
    assert recognizer.max_timestamp_jitter_ms == 10
    assert recognizer.prediction_count == 4
    assert recognizer.gesture_counts[1] == 1


@pytest.mark.parametrize("index,seq,dt", [
    (71, 7, -5),   # missing sample at a jittered boundary
    (70, 8, -5),   # missing packet
    (70, 5, -5),   # out-of-order packet
    (70, 6, -5),   # overlapping timestamp inside a packet
    (70, 7, 1005), # actual pause, with otherwise continuous sequence
    (70, 7, -995), # device clock restart, not bounded jitter
])
def test_jitter_tolerance_does_not_hide_real_discontinuities(index, seq, dt):
    recognizer = GestureRecognizer(classifier=FakeClassifier(), adapter=FakeAdapter())
    for frame in range(70):
        recognizer.on_sample(make_sample(frame, frame // 10, frame * 5))
    assert recognizer.on_sample(make_sample(index, seq, 345 + dt)) is None
    assert recognizer.reset_count == 2
    assert recognizer.last_prediction is None


def test_packet_clock_jitter_remains_bounded_across_uptime_and_sequence_wrap():
    recognizer = GestureRecognizer(classifier=FakeClassifier(), adapter=FakeAdapter())
    for frame in range(75):
        packet = frame // 10
        timestamp = ((1 << 32) - 200 + frame * 5 + (10 if packet % 2 else 0)) % (1 << 32)
        event = recognizer.on_sample(make_sample(frame, (65530 + packet) & 0xFFFF, timestamp))
    assert event.name == "swipe-up"
    assert recognizer.reset_count == 1
    assert recognizer.timestamp_jitter_count == 7


@pytest.mark.parametrize("tolerance", [-1, float("nan"), float("inf")])
def test_packet_timestamp_tolerance_validation(tolerance):
    with pytest.raises(ValueError, match="packet_timestamp_tolerance_ms"):
        GestureRecognizer(classifier=FakeClassifier(), packet_timestamp_tolerance_ms=tolerance)


def test_automatic_timestamps_and_input_array_ownership():
    classifier = FakeClassifier()
    recognizer = GestureRecognizer(classifier=classifier)
    row = np.zeros(6, dtype=np.float32)
    events = []
    for frame in range(75):
        row[:] = frame
        event = recognizer.feed(row)
        if event is not None:
            events.append(event)
    assert [event.timestamp_ms for event in events] == [370.0]
    np.testing.assert_array_equal(classifier.windows[0][:, 0], np.arange(60))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_hz": 0}, {"sample_hz": -1}, {"sample_hz": float("nan")},
        {"sample_hz": float("inf")}, {"step_frames": 0}, {"step_frames": -1},
        {"step_frames": 1.5}, {"stable_window_seconds": 0},
        {"stable_window_seconds": -1}, {"stable_window_seconds": float("nan")},
        {"stable_window_seconds": float("inf")}, {"positive_ratio": 0},
        {"positive_ratio": -0.1}, {"positive_ratio": 1.1},
        {"positive_ratio": float("nan")}, {"positive_ratio": float("inf")},
        {"reset_delay_frames": -1}, {"reset_delay_frames": 1.5},
    ],
)
def test_invalid_stream_parameters(kwargs):
    with pytest.raises(ValueError):
        GestureRecognizer(classifier=FakeClassifier(), **kwargs)


@pytest.mark.parametrize(
    "values",
    [[], [0] * 5, [0] * 7, [[0] * 6], [float("nan")] * 6, [float("inf")] * 6],
)
def test_invalid_rows_are_rejected_before_inference(values):
    classifier = FakeClassifier()
    recognizer = GestureRecognizer(classifier=classifier)
    with pytest.raises(ValueError):
        recognizer.feed(values)
    assert classifier.windows == []


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_timestamp_is_rejected(timestamp):
    recognizer = GestureRecognizer(classifier=FakeClassifier())
    with pytest.raises(ValueError):
        recognizer.feed([0] * 6, timestamp_ms=timestamp)
