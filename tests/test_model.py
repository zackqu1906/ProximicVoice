import numpy as np

from proximic_ring.features import LegacyFeatureExtractor
from proximic_ring.model import ProxiMicModel
from proximic_ring.pipeline import LegacyInferencePipeline
from proximic_ring.resample import LegacyDownsampler16kTo8k


def test_model_loads_and_shapes():
    model = ProxiMicModel()
    x = np.zeros((20, 201), dtype=np.float32)
    logits, score = model.infer(x)
    assert logits.shape == (2,)
    assert np.isfinite(score)


def test_zero_audio_matches_original_cpp_reference():
    """Original example.cpp/run_with_resample(zero) prints -1.10899."""
    pipeline = LegacyInferencePipeline()
    result = pipeline.infer_window(np.zeros(16000, dtype=np.float32))
    assert abs(result.score - (-1.10899)) < 2e-5


def test_feature_shape():
    features = LegacyFeatureExtractor().extract(np.zeros(8000, dtype=np.float32))
    assert features.shape == (20, 201)
    assert features.dtype == np.float32


def test_downsample_shape():
    y = LegacyDownsampler16kTo8k()(np.zeros(16000, dtype=np.float32))
    assert y.shape == (8000,)
    assert y.dtype == np.float32


def test_reference_waveform_matches_original_cpp():
    """C++ prog.h on tests/data/reference_audio.f32 returns -3.99417376518."""
    from pathlib import Path

    path = Path(__file__).parent / "data" / "reference_audio.f32"
    x = np.fromfile(path, dtype=np.float32)
    result = LegacyInferencePipeline().infer_window(x)
    assert abs(result.score - (-3.99417376518)) < 5e-5
