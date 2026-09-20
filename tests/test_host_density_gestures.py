"""Ringo density checkpoint parity and the application's host-only boundary."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from proximic_ring.host_gestures import (
    GESTURE_IDS, GESTURE_NAMES, MODEL_PATH, GestureClassifier, GestureRecognizer,
)
from ring_python_sdk.gestures import GestureWorker
from ring_python_sdk.imu.processor import ImuSample
from ring_python_sdk.ringo.gestures import GESTURE_LABELS


class ScriptedDensityModel:
    """Upstream decoder fixture: four known peaks, including sparse class IDs."""

    window_size = 60
    stride = 20
    sample_rate_hz = 200
    gesture_ids = GESTURE_IDS
    gesture_labels = GESTURE_LABELS

    def __init__(self):
        self.windows = []

    def predict(self, windows):
        outputs = []
        for window in windows:
            window = np.asarray(window)
            self.windows.append(window.copy())
            start = int(window[0, 0])
            peaks = np.array([39, 103, 141, 219])
            label = [6, 12, 5, 13][int(np.argmin(abs(peaks - (start + 29.5))))]
            positions = np.arange(start, start + 60)
            density = (np.exp(-((positions[:, None] - peaks) / 2.0) ** 2 / 2).sum(axis=1) * .3).astype(np.float32)
            probabilities = np.full(12, .01, np.float32)
            probabilities[GESTURE_IDS.index(label)] = .89
            outputs.append(SimpleNamespace(probabilities=tuple(probabilities), frame_density=tuple(density)))
        return tuple(outputs)


def sample(index, *, seq=None, timestamp=None):
    return ImuSample(index, index // 10 if seq is None else seq,
                     index * 5.0 if timestamp is None else timestamp,
                     (1., 2., 9.80665), (180., 0., -90.), (1,) * 6)


def test_density_import_is_lazy_and_missing_torch_does_not_select_another_model():
    subprocess.run([sys.executable, "-c", """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('torch', 'MNN', 'openvino'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Block())
from proximic_ring.host_gestures import GestureRecognizer
assert 'torch' not in sys.modules
try:
    GestureRecognizer()
except ImportError:
    pass
else:
    raise AssertionError('must report missing model dependency')
"""], check=True)


def test_vendored_sources_and_checkpoint_match_pinned_sdk():
    root = Path(__file__).parents[1] / "src/ring_python_sdk/ringo"
    manifest = json.loads((root / "gestures/VENDORED.json").read_text())
    assert manifest["commit"] == "d7353047ad92006143c772e765c3f17a72c6290b"
    for relative, expected in manifest["files"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected, relative
    assert MODEL_PATH.is_file() and MODEL_PATH.stat().st_size > 300_000


def test_cpu_checkpoint_matches_upstream_probabilities_and_density():
    import torch
    from ring_python_sdk.ringo.gestures import SwipeDensityModel

    torch.set_num_threads(1)
    model = SwipeDensityModel()
    assert model._network.training is False
    assert {p.device.type for p in model._network.parameters()} == {"cpu"}
    noise = np.random.default_rng(291).normal(size=(60, 6)).astype(np.float32)
    values = np.stack([np.zeros((60, 6), np.float32), noise, noise * [15, 15, 15, 6, 6, 6]])
    original = values.copy()
    expected = json.loads((Path(__file__).parent / "fixtures/density_model_goldens.json").read_text())
    classifier = GestureClassifier(model)
    for window, actual, golden in zip(values, model.predict(values), expected):
        np.testing.assert_allclose(actual.probabilities, golden["probabilities"], rtol=2e-5, atol=2e-6)
        np.testing.assert_allclose(actual.frame_density, golden["frame_density"], rtol=2e-5, atol=2e-6)
        np.testing.assert_allclose(classifier.predict(window).probabilities, actual.probabilities, rtol=2e-5, atol=2e-6)
    np.testing.assert_array_equal(values, original)


def test_original_density_schedule_events_names_and_health_counts():
    model = ScriptedDensityModel()
    predictions, callbacks, events = [], [], []
    recognizer = GestureRecognizer(model=model, on_gesture=callbacks.append,
                                   on_prediction=lambda p, t: predictions.append((p, t)))
    for i in range(320):
        events.extend(recognizer.feed(np.full(6, i), timestamp_ms=i * 5))
    assert [(e.raw.center_frame, e.class_id, e.raw.emitted_after_frame) for e in events] == [
        (39, 6, 79), (103, 12, 139), (141, 5, 179), (219, 13, 259),
    ]
    assert [e.name for e in events] == ["snap", "circle-clockwise", "tap", "circle-counterclockwise"]
    assert [e.timestamp_ms for e in events] == [395., 695., 895., 1295.]
    assert callbacks == events
    assert all(e.probabilities == () and e.confidence == e.raw.raw.class_confidence for e in events)
    assert len(predictions) == recognizer.prediction_count == 14
    assert [t for _, t in predictions] == [i * 5 for i in range(59, 320, 20)]
    for index, window in enumerate(model.windows):
        np.testing.assert_array_equal(window[:, 0], np.arange(index * 20, index * 20 + 60))
    stats = GestureWorker(recognizer).snapshot()
    assert stats["gesture_count"] == 4
    assert stats["gesture_counts"]["circle-clockwise"] == 1
    assert stats["gesture_counts"]["tap"] == 1
    assert set(stats["prediction_counts"]) == set(GESTURE_NAMES)


def test_host_adapter_units_and_axes_without_old_mount_rotation():
    model = ScriptedDensityModel()
    recognizer = GestureRecognizer(model=model)
    for i in range(60):
        recognizer.on_sample(sample(i))
    expected = [9.8 / 9.80665, 2 * 9.8 / 9.80665, 9.8, np.pi, 0., -np.pi / 2]
    np.testing.assert_allclose(model.windows[0], np.tile(expected, (60, 1)), rtol=1e-6)


@pytest.mark.parametrize("wrapped", [False, True])
def test_mic_jitter_keeps_continuous_frames_and_original_diagnostic_clock(wrapped):
    predictions = []
    recognizer = GestureRecognizer(model=ScriptedDensityModel(),
                                   on_prediction=lambda _, t: predictions.append(t))
    origin = (1 << 32) - 200 if wrapped else 0
    for i in range(100):
        packet = i // 10
        timestamp = (origin + i * 5 + (10 if packet % 2 else 0)) % (1 << 32)
        recognizer.on_sample(sample(i, seq=(65530 + packet) & 0xFFFF, timestamp=timestamp))
    assert recognizer.reset_count == 1
    assert recognizer.prediction_count == 3
    assert recognizer.timestamp_jitter_count == 9
    assert recognizer.max_timestamp_jitter_ms == 10
    assert predictions == [origin + i * 5 + 10 for i in (59, 79, 99)]


@pytest.mark.parametrize("index,seq,elapsed", [
    (71, 7, 5), (70, 8, 5), (70, 5, 5), (70, 6, -5), (70, 7, 1005), (70, 7, -995),
])
def test_gaps_reorders_and_pauses_clear_pending_evidence(index, seq, elapsed):
    model = ScriptedDensityModel()
    recognizer = GestureRecognizer(model=model)
    for i in range(70):
        recognizer.on_sample(sample(i))
    assert recognizer.prediction_count == 1
    assert not recognizer.on_sample(sample(index, seq=seq, timestamp=345 + elapsed))
    assert recognizer.reset_count == 2
    assert recognizer.last_prediction is None
    assert recognizer.native.stream.inference_count == 0


def test_disable_jitter_and_direct_feed_gap_reset():
    recognizer = GestureRecognizer(model=ScriptedDensityModel(), packet_timestamp_tolerance_ms=0)
    for i in range(70):
        recognizer.on_sample(sample(i))
    recognizer.on_sample(sample(70, timestamp=340))
    assert recognizer.reset_count == 2 and recognizer.timestamp_jitter_count == 0
    for i in range(60):
        recognizer.feed([0] * 6, timestamp_ms=1000 + i * 5)
    previous = recognizer.reset_count
    recognizer.feed([0] * 6, timestamp_ms=2000)
    assert recognizer.reset_count == previous + 1
    assert recognizer.last_prediction is None


def test_reset_discards_state_but_retains_lifetime_counts():
    recognizer = GestureRecognizer(model=ScriptedDensityModel())
    for i in range(100):
        recognizer.feed([i] * 6)
    assert recognizer.prediction_count == 3
    recognizer.reset()
    assert recognizer.last_prediction is None and recognizer.reset_count == 2
    assert recognizer.prediction_count == 3
    assert not recognizer.feed([0] * 6)


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
def test_invalid_timestamp_tolerance_is_rejected(bad):
    with pytest.raises(ValueError):
        GestureRecognizer(model=ScriptedDensityModel(), packet_timestamp_tolerance_ms=bad)


def test_invalid_frames_and_tokens_fail_before_inference():
    model = ScriptedDensityModel()
    recognizer = GestureRecognizer(model=model)
    for invalid in (np.zeros(5), np.full(6, np.nan), np.full(6, np.inf)):
        with pytest.raises(ValueError):
            recognizer.feed(invalid)
    with pytest.raises(ValueError, match="token"):
        recognizer.on_sample(replace(sample(0), raw=None))
    with pytest.raises(ValueError):
        recognizer.on_sample(replace(sample(0), uptime_ms=float("nan")))
    assert model.windows == []
