# ai-ring gesture source

The model and gesture algorithms in this package originate from
[dBHz01/ai-ring](https://github.com/dBHz01/ai-ring/tree/fa50547728967f1bfae7fb6dce53f89c98afb5b4),
branch `feat/ringo`, commit `fa50547728967f1bfae7fb6dce53f89c98afb5b4`.

| SDK file | Upstream source |
| --- | --- |
| `_model.py` | `checkpoint/swipe/cls/model.py` |
| `assets/swipe.pt` | `checkpoint/swipe/cls/swipe_cnn_classification_best.pt` (unchanged weights) |
| `assets/swipe.json` | Inference settings from `checkpoint/swipe/cls/config.yaml`; training paths removed |
| `classifier.py` | CPU model loading, normalization and prediction from `models/swipe_cnn_recognizer.py`; seven-class names from `models/swipe_gestures.py` |
| `preprocess.py` | `core/ringo.py:RingXRotationAugment` and Ringo IMU processing, `core/ringo_protocol.py` chip mapping; adapted to SDK physical samples |
| `runtime.py` | Window, voting and cooldown behavior from `desktop/swipe_runtime.py` and `apps/features/window_classifier/swipe.py` |

Upstream's README states **“License / 暂未声明。”** at this revision and
contains no license file. This notice records provenance and does not grant
or relicense upstream code or model weights. Their original authors retain
their rights.

Local Proximic Voice additions: `worker.py` provides bounded serial background
inference and health snapshots; `runtime.py` adds an optional per-prediction
observer, lifetime counters, and bounded packet-tail clock jitter tolerance for
continuous SDK sample/packet sequences when microphone traffic is active.
Bundled weights, preprocessing and classification/voting rules remain
those of the supplied SDK snapshot.
