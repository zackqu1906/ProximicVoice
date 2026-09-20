"""Lazy exports of the original density model and its deployment metadata."""
from pathlib import Path

DEFAULT_MODEL_DIR = Path(__file__).with_name('_upstream') / 'swipe_density' / 'assets' / 'density-pw64-dh96-swipe3-rot-0913'
GESTURE_IDS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13)
GESTURE_LABELS = (
    'empty', 'swipe-up', 'swipe-down', 'swipe-left', 'swipe-right', 'swipe-tap',
    'snap', 'clench', 'index-pinch', 'middle-pinch', 'circle-clockwise', 'circle-counterclockwise',
)


def __getattr__(name):
    if name not in ('SwipeDensityModel', 'WindowInference'):
        raise AttributeError(name)
    try:
        from ._upstream.swipe_density import model
    except ImportError as exc:
        raise ImportError("Install density inference with: pip install 'ring-python-sdk[ringo]'") from exc
    return getattr(model, name)
