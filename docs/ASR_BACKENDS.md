# ASR backend architecture

The ProxiMic detector and ASR implementations are intentionally decoupled.

```text
Ring / WAV / microphone
        |
        v
ProxiMicDetector
        |
        | Stage1 / Stage2 events
        v
ProximitySessionController
        |
        | one completed 16 kHz near-speech utterance
        v
UtteranceSink
        |
        +--> ASRWorker --> SenseVoice backend
        +--> ASRWorker --> Whisper backend
        +--> ASRWorker --> HTTP/cloud backend
```

`ProximitySessionController` does not import SenseVoice, Whisper, HTTP, or any
other ASR implementation. It only emits completed 16 kHz utterances to a sink.
The detector/model/input path is therefore unchanged when an ASR backend is
added or replaced.

## Backend contract

Every backend only needs two public attributes and one method:

```python
class MyASR:
    backend_name = "my_asr"
    model_name = "my-model"

    def transcribe(self, audio_16k: np.ndarray) -> str:
        ...
```

A built-in backend lives in:

```text
src/proximic_ring/asr/backends/<backend_name>.py
```

and exposes:

```python
def create_backend(settings):
    return MyASR(...)
```

The factory discovers backend modules by filename. Adding `my_asr.py` therefore
does not require edits to the detector, session controller, worker, or CLI
orchestration.

## CLI philosophy

`--asr` chooses the **backend**. `--asr-model` optionally pins the exact model.
For reproducible experiments it is recommended to specify both explicitly.

List adapters:

```powershell
python -m proximic_ring asr-backends
```

Disable ASR by omitting `--asr` (or using `--asr off`).

## Compare ASRs without ProxiMic

For a raw Ring-microphone ASR baseline, bypass the detector completely with
`--disable-proximic-detector` (short alias: `--disable-detector`). Audio starts
flowing to every selected backend immediately; there are no Stage1/Stage2
events, proximity activation, pre-roll, or detector-based endpointing. It rolls
audio into fixed 5-second ASR sessions by default so both streaming backends get
the same start, audio, and END event for each comparison. Ctrl+C flushes the
current partial session and prints its final result.

Compare the local streaming SenseVoice and native Doubao backend on the same
raw Ring stream:

```powershell
python -m proximic_ring ring --disable-proximic-detector --asr streaming_sensevoice --asr volcengine --asr-model streaming_sensevoice=iic/SenseVoiceSmall --asr-model volcengine=seedasr-streaming --asr-device cuda:0 --asr-language zh --streaming-sensevoice-repo ".\third_party\streaming-sensevoice"
```

When two streaming backends are selected, Seed-ASR uses a distinct `Seed-ASR`
label and all final results are always printed. Local SenseVoice partial text is
limited to one changed line per second by default, so it cannot flood the
terminal and hide sparse cloud results. To restore every local partial update,
add:

```powershell
--asr-partial-min-interval 0
```

Seed-ASR partials are also de-duplicated in every mode (with or without
ProxiMic): its service repeats the same cumulative text for many input packets,
so an unchanged partial is not printed. Its final result is always printed.

Set a different direct-session duration when needed (for example, 30 seconds):

```powershell
--direct-asr-session-duration 30
```

## Native Doubao streaming ASR (Volcengine)

`volcengine` is a native bidirectional streaming backend for
`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel`. It uses the new-console
WebSocket handshake documented by Doubao Speech:

```text
X-Api-Key: <Doubao Speech App Key>
X-Api-Resource-Id: volc.seedasr.sauc.duration
X-Api-Connect-Id: <new UUID for each connection>
```

It sends 16 kHz / 16-bit / mono PCM in 200 ms Seed-protocol packets, prints
partial text while Stage2 remains active, and requests final text when ProxiMic
ends the session. A dedicated receiver reads cloud responses continuously, so
slow network responses cannot queue up and delay later audio or final text. Set
the App Key before starting the program:

```powershell
$env:VOLC_ASR_API_KEY = "your-doubao-speech-app-key"
```

> The two legacy paragraphs immediately below are obsolete historical text from
> the removed AI Gateway adapter.  Ignore them; this implementation uses the
> native new-console handshake documented above, not `Authorization: Bearer`.

<!-- Obsolete AI Gateway text retained only to avoid altering unrelated history.
`Authorization: Bearer <VOLCENGINE_GATEWAY_API_KEY>`—not an Ark console API key
and not the separate Volcengine Speech App-ID/Access-Token binary protocol. It emits partial text while the
near-speech session is active, then emits the service's final text at session
END. -->

Install the WebSocket dependency:

```powershell
python -m pip install -e ".[ring,asr-volcengine]"
```

Then select it with the existing backend switch:

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr volcengine `
  --asr-model seedasr-streaming
```

The default Resource ID is Seed-ASR 2.0 hourly:
`volc.seedasr.sauc.duration`. For an enabled concurrent plan, add
`--asr-option resource_id=volc.seedasr.sauc.concurrent`. Backend-local controls
are `chunk_ms` (100–200, default 200), `timeout_s` (default 15),
`partial_timeout_s` (default 0.8), `final_timeout_s` (default 8),
`api_key_env` (default
`VOLC_ASR_API_KEY`), and `url`.

If a session ends but no text is displayed, turn on protocol diagnostics (it
prints packet/frame counts and timings, never the App Key or audio):

```powershell
--asr-option debug=true
```

To compare the online and existing local streaming backend on the same detected
utterance, select both. Prefix the option with the target backend whenever
multiple backends are active:

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr streaming_sensevoice `
  --asr volcengine `
  --asr-model streaming_sensevoice=iic/SenseVoiceSmall `
  --asr-model volcengine=seedasr-streaming `
  --streaming-sensevoice-repo ".\third_party\streaming-sensevoice" `
  --asr-option volcengine.api_key_env=VOLC_ASR_API_KEY
```

For the current native backend, a `401` means the `X-Api-Key` is not a valid
new-console Doubao Speech App Key, while a `403` commonly means the selected
Resource ID is not enabled for that application. The key is never printed.

## SenseVoice

Install:

```powershell
python -m pip install -e ".[ring,asr]"
```

Use backend default (`iic/SenseVoiceSmall`):

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr sensevoice `
  --asr-device cuda:0 `
  --asr-language zh
```

Pin the model explicitly:

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr sensevoice `
  --asr-model iic/SenseVoiceSmall `
  --asr-device cuda:0 `
  --asr-language zh
```

Use a local SenseVoice source checkout:

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr sensevoice `
  --sensevoice-repo "C:\path\to\SenseVoice" `
  --asr-device cuda:0 `
  --asr-language zh
```

## Local Whisper (faster-whisper)

Install:

```powershell
python -m pip install -e ".[ring,asr-whisper]"
```

Run:

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr whisper `
  --asr-model small `
  --asr-device cuda:0 `
  --asr-language zh
```

Optional backend-specific settings use `--asr-option`:

```powershell
--asr-option compute_type=float16 --asr-option beam_size=5
```

## Generic HTTP/cloud ASR

The built-in `http` adapter supports services that accept a multipart WAV file
and return JSON. API-specific behavior stays in the backend adapter rather than
leaking into ProxiMic/session code.

Install:

```powershell
python -m pip install -e ".[ring,asr-http]"
```

Put credentials in an environment variable instead of the command line:

```powershell
$env:MY_ASR_KEY="your-key"
```

Example:

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr http `
  --asr-model remote-model-name `
  --asr-language zh `
  --asr-option url=https://your-asr-server.example/v1/transcribe `
  --asr-option api_key_env=MY_ASR_KEY `
  --asr-option text_field=text
```

If a cloud service does not use this multipart/JSON protocol, add a dedicated
backend module for that service. No detector/session changes are required.

## Compare multiple ASRs on the same utterance

`--asr` is repeatable. The completed near-speech utterance is fanned out to a
separate worker thread for each backend.

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr sensevoice `
  --asr whisper `
  --asr-model sensevoice=iic/SenseVoiceSmall `
  --asr-model whisper=small `
  --asr-device cuda:0 `
  --asr-language zh
```

Typical output:

```text
ASR[sensevoice/iic/SenseVoiceSmall] latency=180ms rtf=0.055: 帮我打开这个项目
ASR[whisper/small] latency=420ms rtf=0.129: 帮我打开这个项目
```

The reported `rtf` is `ASR latency / utterance duration`.

For a laptop GPU with limited VRAM, loading two local ASR models at the same
time may be too expensive. For rigorous comparisons, replay the same saved WAV
with one backend at a time:

```powershell
python -m proximic_ring wav .\test.wav --model runs\near_vs_nontarget_v1\best.model --asr sensevoice --asr-model iic/SenseVoiceSmall --asr-language zh
python -m proximic_ring wav .\test.wav --model runs\near_vs_nontarget_v1\best.model --asr whisper --asr-model small --asr-language zh
```

## Fun-ASR-Nano-2512 (local cumulative streaming)

`funasr_nano` integrates the local Fun-ASR checkout through the same streaming
worker used by the other live backends. It follows the repository demo's
cumulative pseudo-streaming algorithm: every 720 ms it re-decodes the audio
accumulated in the current session, carries stable text forward, and rolls back
five unstable tail tokens. On session END it re-decodes the controller's
trimmed final utterance once for the final result. It does not add a VAD or
change ProxiMic's START/END behavior.

The checkout passed to `--funasr-nano-repo` must contain `model.py`. When it
also contains `pretrained_models/Fun-ASR-Nano-2512`, that local checkpoint is
selected automatically, so no `--asr-model` argument is needed.

ProxiMic-gated Ring input, one line:

```powershell
python -m proximic_ring ring --model src\proximic_ring\assets\ringo-near-v1.model --stage1-threshold 0.005 --asr funasr_nano --funasr-nano-repo .\third_party\Fun-ASR --asr-device cuda:0 --asr-language auto
```

Direct Ring baseline without ProxiMic, one line:

```powershell
python -m proximic_ring ring --disable-proximic-detector --asr funasr_nano --funasr-nano-repo .\third_party\Fun-ASR --asr-device cuda:0 --asr-language auto
```

Optional controls use backend-local `--asr-option` values:

```text
chunk_ms=720
rollback_tokens=5
use_itn=true
hotwords=戒指,智能体
final_redecode=true
```

For example, with only Nano selected:

```powershell
--asr-option chunk_ms=1000 --asr-option rollback_tokens=5
```

Output is labelled independently from SenseVoice and Seed-ASR:

```text
ASR-PARTIAL[Fun-ASR-Nano/Fun-ASR-Nano-2512] latency=...ms: ...
ASR-FINAL[Fun-ASR-Nano/Fun-ASR-Nano-2512] latency=...ms: ...
```

## Add a new backend

Create `src/proximic_ring/asr/backends/my_asr.py`:

```python
from proximic_ring.asr.factory import ASRBackendSettings


class MyASR:
    backend_name = "my_asr"

    def __init__(self, model: str):
        self.model_name = model

    def transcribe(self, audio_16k):
        # Convert/call whatever this ASR requires here.
        return "..."


def create_backend(settings: ASRBackendSettings):
    return MyASR(settings.model or "default-model")
```

Then the existing CLI can immediately select it:

```powershell
python -m proximic_ring ring ... --asr my_asr --asr-model default-model
```

No changes are needed in `detector.py`, `pipeline.py`, `model.py`,
`controller.py`, or `runner.py`.

## Streaming SenseVoice (third-party cumulative/pseudo-streaming adapter)

This project can use the external `pengzhendong/streaming-sensevoice` repository
without copying or importing it from the ProxiMic detector path.

Architecture:

```text
Ring 16 kHz PCM
      |
      v
ProxiMicDetector                     unchanged
      |
      | Stage2 ACTIVATE / reject
      v
ProximitySessionController           model-agnostic
      |
      +-- START(pre-roll) -------------------------------+
      +-- AUDIO(new 16 kHz blocks) -------------------+   |
      +-- END(trimmed full utterance) --------------+ |   |
                                                   | |   |
                                                   v v   v
                                      StreamingASRWorker
                                                   |
                                                   v
                                      StreamingASRBackend
                                                   |
                                                   v
                         adapters/streaming_sensevoice.py
                                                   |
                                                   v
                         external streaming_sensevoice package
```

The detector, ProxiMic model, 8 kHz feature path, Stage1/Stage2 logic, and Ring
source do not import the third-party project.  The external dependency exists
only inside the backend adapter.

### Prepare the external repository

Clone it somewhere outside this project, for example:

```powershell
# The verified source snapshot is already included at:
# .\third_party\streaming-sensevoice
```

This adapter uses only the repository's core `StreamingSenseVoice` class; it
does not use the repository's Silero VAD or microphone demo because ProxiMic
already owns START/END gating.

Install the ProxiMic-side runtime requirements:

```powershell
python -m pip install -e ".[ring,asr-streaming-sensevoice]"
```

The upstream repository's full `requirements.txt` currently contains its own
Torch/NumPy pins and demo packages.  If your existing CUDA environment is
already working, do not blindly reinstall its full requirements just to use
this adapter.  Start with the ProxiMic extra above; if the external repository
reports an incompatibility, resolve it in a separate environment or match the
upstream versions deliberately.

### Run real-time partial output

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --stage1-threshold 0.005 `
  --asr streaming_sensevoice `
  --asr-model iic/SenseVoiceSmall `
  --asr-device cuda:0 `
  --asr-language zh `
  --streaming-sensevoice-repo ".\third_party\streaming-sensevoice"
```

Typical terminal output:

```text
[ASR] START t=12.540s (pre-roll=1.00s)
ASR-PARTIAL[streaming_sensevoice/iic/SenseVoiceSmall] latency=...ms: 帮我
ASR-PARTIAL[streaming_sensevoice/iic/SenseVoiceSmall] latency=...ms: 帮我打开这个
ASR-PARTIAL[streaming_sensevoice/iic/SenseVoiceSmall] latency=...ms: 帮我打开这个项目
[ASR] END reason=stage1-inactivity duration=3.12s rejects=0
ASR-FINAL[streaming_sensevoice/iic/SenseVoiceSmall] latency=...ms: 帮我打开这个项目
```

Partial results are provisional display text.  By default the adapter resets the
third-party model at END and re-decodes the controller's final **trimmed**
utterance once (`final_redecode=true`).  This prevents the final transcript from
being permanently affected by reject-confirmation tail audio that may already
have reached the live partial stream.

Backend-specific options:

```powershell
--asr-option chunk_size=4
--asr-option beam_size=1
--asr-option max_history=0
--asr-option textnorm=true
--asr-option final_redecode=true
--asr-option contexts=清华大学,ProxiMic,Ringo
```

`max_history=0` leaves the third-party implementation's history unlimited.  A
positive value bounds its accumulated feature history and compute cost, at the
cost of less context.

### Why this remains decoupled

Streaming and batch backends use different generic contracts:

```text
Batch backend:
    transcribe(full_utterance)

Streaming backend:
    start()
    feed(new_audio) -> optional revised partial text
    finish(final_utterance) -> final text
```

The session controller talks only to a generic `SessionSink` with
`start/feed/end/close`.  `CompletedUtteranceSessionSink` adapts old batch
workers, while `StreamingASRWorker` adapts streaming backends.  Therefore a
future native streaming backend (for example a cloud WebSocket API) can be
added as another module under `asr/backends/` without changing ProxiMic.
