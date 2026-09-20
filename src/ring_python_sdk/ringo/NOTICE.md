# Upstream algorithm provenance

Source: [dBHz01/ai-ring, `streaming`](https://github.com/dBHz01/ai-ring/tree/a621886ba1d74eadd96b362aee82c190792484eb),
commit `a621886ba1d74eadd96b362aee82c190792484eb` (2026-09-14).
The requested source branch was fetched and verified on 2026-09-17. This
adaptation updates the SDK `ringo` branch from `dda9d69`.

| SDK module | Source algorithms and assets |
| --- | --- |
| `imu.py` | SDK `ImuSample`/chip-to-host physical frame; `core/ble_ring_v2.py` and stream clients define the required BCL host axes and rad/s input |
| `gestures/` | `swipe_density/`; `models/{swipe_cnn_recognizer,pinch_cnn_recognizer,swipe_gestures,cnn}.py`; `checkpoint/swipe/`; native window runtime from `apps/unified_gesture_app.py`; legacy gesture pipeline from `apps/features/gesture/runtime.py` and its `it-pinch`/`it-snap` checkpoints |
| `touchpad/` | `apps/features/touchpad_plus/{aggregation,runtime,cursor,models}/` and current Mamba2/contact packages; legacy `apps/features/touchpad/runtime.py`, `models/tcn_lstm.py`, `utils/{imu_processor,cd_ratio}.py`, `checkpoint/tcnlstm.pt` |
| `airmouse/` | `apps/features/airmouse/runtime.py` VQF/quaternion motion and filtering |
| `handwriting/` | `whisper/{whisper_pretrain,whisper_with_pretrain_recognizer}.py`; the handwriting demo/stream applications; digit/word checkpoints and configs from `checkpoint/whisper-{number,word}-exp/` |

The adaptation boundary is BLE protocol/data conversion, package imports and
resource paths, replacement of Qt thread/signal wiring by standard-library
threads/callbacks, and callback sinks in place of desktop OS input actions.
Model files, native recognizer interfaces, preprocessing and decoding follow the
upstream implementation. Original input scheduling, batching and clocks are
preserved. No common sample-gap reset or bounded queue dropping is added.
Per-domain UPSTREAM.md files list the concrete source mapping and integration
changes. The session API adds firmware/host selection and shared BLE acquisition.

Ordinary digit/word handwriting checkpoints are bundled; INPUT_END requires a
matching external word checkpoint. TypeRing and personalization are excluded.

Upstream's README declares **“License / 暂未声明。”** at this commit. This port
does not grant a new license to, or relicense, the upstream code and weights.
Original authors retain their rights. Dependencies retain their own licenses.

The earlier `ring_python_sdk.gestures` package remains associated with its
`feat/ringo` source revision and its separate notice; its 135-degree mounting
adapter is not the adapter for the algorithms in this package.
