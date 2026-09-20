# ai-ring gesture source mapping

Source: `dBHz01/ai-ring`, branch `streaming`, commit
`a621886ba1d74eadd96b362aee82c190792484eb` (checked 2026-09-17).
All paths below are relative to this package or that source repository.

| Packaged path | Upstream path | Changes |
| --- | --- | --- |
| `_upstream/models/cnn.py` | `models/cnn.py` | None |
| `_upstream/models/swipe_gestures.py` | `models/swipe_gestures.py` | None |
| `_upstream/models/swipe_cnn_recognizer.py` | `models/swipe_cnn_recognizer.py` | `models.*` imports changed to relative imports |
| `_upstream/models/pinch_cnn_recognizer.py` | `models/pinch_cnn_recognizer.py` | `models.*` imports changed to relative imports |
| `_upstream/swipe_density/{model,snapshot_model,stream,decoder}.py` | `swipe_density/` | None, including model normalization and decoder |
| `_upstream/swipe_density/assets/` | `swipe_density/assets/` | Original default density checkpoint, unchanged |
| `_upstream/checkpoint/swipe/cls/` | `checkpoint/swipe/cls/` | Original config, model source, PyTorch checkpoint and MNN FP16/FP32 files, unchanged |
| `_upstream/checkpoint/swipe/cls-pinch/` | `checkpoint/swipe/cls-pinch/` | Original config, PyTorch checkpoint and OpenVINO XML/BIN, unchanged |
| `_upstream/checkpoint/it-{pinch,snap}/` | `checkpoint/it-{pinch,snap}/` | Original OpenVINO XML/BIN, unchanged; no reconstruction from training checkpoints |
| `_upstream/window_runtime.py` | `apps/unified_gesture_app.py` | Verbatim definitions listed below; imports assembled for standalone use |
| `_upstream/legacy_runtime.py` | `apps/features/gesture/runtime.py` | Qt import replaced by thread/signal transport, constants import relocated, `PROJECT_ROOT` points at the packaged resource root |
| `_upstream/constants.py` | `apps/shared/constants.py` | None |
| `_upstream/ws_protocol.py` | `apps/features/gesture/ws_protocol.py` | Verbatim `RingTarget` definition only |

The window runtime definitions are `WindowClassifierConfig`, `InferenceJob`,
`InferenceOutput`, `ClassifierState`, `RateTracker`, `RecognizerBus`,
`InferenceWorker`, `RecognizerRuntimeBase` and `WindowClassifierRuntime`.
Their class ASTs match the source. The runtime still selects windows, calls
`recognizer.predict(job.values)`, votes, handles cooldown and invalidates stale
results itself. Swipe retains `latest_window_pipeline=True`; Pinch retains
`False`. No fixed-rate replay loop replaces either worker.

`_upstream/_qt.py` is the SDK's thread/signal transport, not upstream code.
A single dispatcher takes the place of the Qt UI event loop, while the original
worker runs on a separate Python thread. The original `submit`, `run`, `stop`
and scheduling method bodies are retained. SDK wrappers wait for initial model
readiness and close the worker before releasing the dispatcher. Legacy's worker
entry is wrapped only to report uncaught failures through `on_error`.

## Public entry points

Native classes retain their original constructor/method signatures:

- `SwipeCNNRecognizer(checkpoint_dir, device='cpu', backend='pytorch', precision='fp32')`:
  original `prepare` and `predict`, including configuration-derived mean/std,
  nonfinite-value replacement and 14-position canonical prediction vectors.
- `PinchCNNRecognizer(checkpoint_dir, device='cpu', backend='pytorch')`:
  original preparation, model call and visible-class probability folding.
- `SwipeDensityModel(model_dir=DEFAULT_MODEL_DIR)` and
  `SwipeDensityStream(model, decoder_config).push_frame(frame, timestamp_s)`:
  original classes and original 60-frame/20-stride processing.
- `InceptionGestureRecognizer(cfg).detect_window(address, window_data, event_ts)`:
  original `(6, 200)` OpenVINO input, masking, voting and duplicate suppression.
- `GestureInferenceWorker(cfg).enqueue_reading(address, data, event_ts)`:
  original pending-frame queue, processing modes and post-detection flattening.
- `WindowClassifierRuntime(...)` and `WindowClassifierConfig(...)` are available
  directly for native runtime integration.

The SDK factories `WindowGestureClassifier` and `PinchClassifier` only select a
bundled directory and backend. Window defaults to MNN FP16 and Pinch defaults to
OpenVINO CPU, as in the unified source app; `backend='pytorch'` explicitly selects
the source's existing PyTorch path. They return the native recognizer objects.
`LegacyGestureClassifier(config=None)` returns `InceptionGestureRecognizer`,
not the former reconstructed tsai model; use its native `detect_window` method.

`WindowGestureRecognizer`, `PinchRecognizer` and `LegacyGestureRecognizer` add
`feed`, `on_imu_message`, `on_event`, `on_error`, `error`, `reset`, `drain_events`
and `close` wiring. Inference remains asynchronous. With `on_event`, callbacks
run on the serial dispatcher and `feed` returns `()`; otherwise `feed` and
`drain_events` return already completed events. Reset delegates to native
lifecycle methods; it adds no timestamp-gap or missing-packet reset policy.
Window and Pinch events preserve the native emission wall clock. Legacy retains
its sample event time for merging and its wall clock for touch/pinch filtering.

SDK `GestureRecognizer.feed` remains a synchronous adapter to the original
Density `push_frame`. Its `raw` is the original `StreamingEvent`. Because the
original decoder does not expose a pooled probability vector, SDK density
events use `probabilities=()` and retain the original `class_confidence`.
Per-window probabilities remain at `last_inference.window.probabilities`.
The former decoder modification that exposed pooled probabilities is removed.

SDK event names/IDs are mapped only after the native result. Pinch native IDs
`0/1/2` remain in its prediction and raw result; SDK event IDs are `0/10/11`.
Legacy's 39-output taxonomy remains separate. The package itself and SDK
wrappers import without model libraries; explicitly requesting native model
classes loads their original dependencies.

## Input and dependency boundary

Callers deliver the source's six-axis array in BCL host coordinates:
`[ax, ay, az, gx, gy, gz]`, acceleration in m/s² and gyro in rad/s.
`on_imu_message` reproduces the source message-to-float32-array conversion and
timestamp fallback. BLE decoding, unit/axis adaptation and clock-field mapping
belong outside this package. There is no SDK rotation, resampling, extra
normalization or universal 15 ms reset before the native model. Density alone
retains its own upstream timestamp-gap reset.

The original native recognizers use PyTorch and PyYAML; MNN and OpenVINO are
required for their selected backends. Missing dependencies do not trigger a
backend/model substitution.

## Verification

`tests/test_ringo_gestures.py` covers original numerical PyTorch goldens,
source/SDK MNN and OpenVINO model calls, byte-identical resources and source
class ASTs, native array preparation, latest-window selection during slow
inference, vote/cooldown/refill, stale-result suppression, worker shutdown,
legacy `(6, 200)` windows and pending-frame flattening. Direct source comparison
uses a sibling `ai-ring-streaming` checkout when available.

On macOS Seatbelt the OpenVINO CPU JIT may abort while allocating executable
memory. The OpenVINO fixture probes that native backend in a subprocess and
skips only this known sandbox failure; outside that sandbox its tests run the
real original models. This does not change any runtime implementation/backend.
