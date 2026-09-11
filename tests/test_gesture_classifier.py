"""Model goldens from ai-ring feat/ringo fa50547728967f1bfae7fb6dce53f89c98afb5b4."""

import subprocess
import sys

import numpy as np
import pytest

from ring_python_sdk.gestures import GESTURE_NAMES, GestureClassifier


def test_gesture_import_does_not_load_torch_and_missing_extra_is_actionable():
    subprocess.run([sys.executable, "-c", """
import importlib.abc
import sys
class NoTorch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'torch' or fullname.startswith('torch.'):
            raise ModuleNotFoundError('torch is not installed')
sys.meta_path.insert(0, NoTorch())
from ring_python_sdk.gestures import GestureClassifier, GestureRecognizer
assert 'torch' not in sys.modules
try:
    GestureClassifier()
except ImportError as exc:
    assert 'ring-python-sdk[gestures]' in str(exc)
else:
    raise AssertionError('missing optional extra was not reported')
"""], check=True)


def test_bundled_model_matches_upstream_probabilities_and_class_mapping():
    pytest.importorskip("torch")
    classifier = GestureClassifier()
    assert classifier.model.training is False
    # These synthetic inputs are output-parity checks, not an accuracy dataset.
    noise = np.random.default_rng(7).normal(size=(60, 6))
    cases = (
        (np.zeros((60, 6)), 0, (
            .9558679461, .0088534439, .0070343651, .0068135206,
            .0064869956, .0089318855, .0060119103,
        )),
        (noise, 0, (
            .9249013066, .0433661342, .0125643406, .0025865696,
            .0050059590, .0026549241, .0089207254,
        )),
        (noise * 50, 6, (
            .2002282143, 4.8406857e-8, 1.8388027e-7, 6.3722339e-10,
            4.1986237e-8, 4.3245800e-6, .7997671366,
        )),
    )
    for values, expected_id, probabilities in cases:
        original = values.copy()
        prediction = classifier.predict(values)
        assert prediction.class_id == expected_id
        assert prediction.name == GESTURE_NAMES[expected_id]
        assert prediction.confidence == prediction.probabilities[expected_id]
        np.testing.assert_allclose(prediction.probabilities, probabilities, atol=2e-6, rtol=2e-5)
        np.testing.assert_array_equal(values, original)
    for invalid in (np.zeros((59, 6)), np.zeros((60, 7)),
                    np.full((60, 6), np.nan), np.full((60, 6), np.inf)):
        with pytest.raises(ValueError):
            classifier.predict(invalid)
