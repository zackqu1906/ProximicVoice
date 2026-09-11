# ProxiMic session -> ASR integration

This document describes **when** a near-speech utterance starts and ends. The
choice of ASR model is intentionally separate; see `ASR_BACKENDS.md`.

The Ring input, ProxiMic feature pipeline, CNN architecture, and checkpoint are
unchanged. The post-activation cooldown remains removed so the existing detector
continues producing Stage2 `ACTIVATE` / `reject` evidence.

## Runtime state machine

```text
IDLE
  |
  | Stage1 -> wait 0.3 s -> Stage2 CNN on the rolling 1 s window
  | first ACTIVATE
  v
ACTIVE / recording original 16 kHz PCM
  |
  | rolling Stage2 every 0.20 s while active
  | later ACTIVATE     -> same session, reject counter = 0
  | Stage2 reject      -> reject counter += 1
  | N rejects          -> END
  | no Stage1 for T s  -> END fallback
  | max duration       -> END safety bound
  v
one complete 16 kHz near-speech utterance
  |
  v
UtteranceSink
  |
  +--> one ASRWorker
  +--> ASRFanout -> multiple ASRWorkers
  +--> future WAV saver / benchmark sink
```

`ProximitySessionController` does not import or instantiate any concrete ASR
model. It only creates an utterance and calls `sink.submit(audio)`.

## Why an inactivity fallback is required

Consecutive Stage2 rejects are the primary endpoint. But if the user truly stops
speaking, Stage1 may stop triggering as well. In that case Stage2 never runs and
therefore cannot emit a reject. A session based only on reject count could remain
open forever.

ACTIVE mode therefore also ends after a configurable interval with no Stage1
trigger. This is detector-event inactivity, not RMS silence and not a second VAD.

Defaults:

```text
pre-roll                 1.00 s
ASR input gain            0.00 dB
Active Stage2 interval   0.20 s
Stage2 reject count      5
Stage1 inactivity        1.25 s
minimum utterance        0.40 s
maximum utterance       15.00 s
```

The inactivity timeout must remain longer than the initial Stage2 delay
(0.30 s), so it does not fire while a valid first Stage2 result is still pending.

## Confirmation-tail trimming

When consecutive rejects confirm END, the later rejects are useful as evidence
but its whole audio tail is not useful to ASR. The session controller therefore
cuts the submitted waveform at the first reject's Stage2 endpoint. This reduces
far-speech / ambient tail contamination.

## Why the 1.0 s pre-roll stays

The first Stage2 decision arrives only after the 0.3 s delay. Starting waveform
capture at the ACTIVATE event would lose the beginning of the command. The
controller keeps the latest 1.0 s of the original 16 kHz Ring waveform and prepends
it when a new session starts.

This never changes ProxiMic input. ProxiMic still performs its own 16 kHz -> 8 kHz
path internally; ASR keeps the 16 kHz timing and receives only the optional
downstream gain described below.

## ASR-only input gain

The desktop app applies its adjustable ASR gain only after ProxiMic has evaluated
the original Ring waveform. The enhanced 16 kHz waveform is then shared by the
ASR backend and voice history, so replayed history matches what recognition heard.
The UI defaults to 0 dB and permits 0 through +12 dB. For quiet speech, +6 dB is
a useful first trial; excessive gain can clip loud speech and reduce recognition
quality.

## ASR workers are asynchronous

Each ASR backend gets an `ASRWorker`, so model inference / HTTP calls never block
the real-time Ring read loop. When several backends are selected, `ASRFanout`
submits the exact same utterance to each worker.

See `ASR_BACKENDS.md` for backend/model selection, comparison commands, cloud
integration, and the adapter template.

## Doubao semantic dialog context

Only the Volcengine/Doubao Seed-ASR backend reads the currently focused text
field before opening each utterance stream. The read uses macOS Accessibility or
Windows UI Automation only: it does not select text, send copy shortcuts, move
the caret, or touch the clipboard. If the control cannot be read safely, ASR
starts normally without context. It never substitutes an application-side text
cache, because a successful paste request is not proof that the target control
actually accepted the text.

On macOS the observation checks both the application and system-wide focused AX
element, walks editable text ancestors, and performs a bounded search for an
explicitly focused editor below the focused window. A coordinate lookup is a
last resort and is accepted only when that element or an ancestor independently
reports AX focus. The reader accepts plain or attributed AXValue and can read a
bounded standard character range when a custom editor omits AXValue. The
selected method is recorded as `read_method` in the interaction record and
compact diagnostic log.

The most recent text is split into sentence-like fragments and sent newest first
as the documented stringified `request.corpus.context` value with
`context_type=dialog_ctx`. The client caps the payload at 20 fragments and a
conservative 640 characters, below the service's 800-token limit. This path is
not wired to local or other cloud ASR backends.
