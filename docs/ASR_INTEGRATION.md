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
  | original Stage1 -> wait 0.5 s -> original Stage2 CNN
  | first ACTIVATE
  v
ACTIVE / recording original 16 kHz PCM
  |
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
pre-roll                 1.50 s
Stage2 reject count      2
Stage1 inactivity        1.25 s
minimum utterance        0.40 s
maximum utterance       15.00 s
```

The inactivity timeout must remain longer than the Stage2 delay (0.50 s), so it
does not fire while a valid Stage2 result is still pending.

## Confirmation-tail trimming

When consecutive rejects confirm END, the second reject is useful as evidence
but its whole audio tail is not useful to ASR. The session controller therefore
cuts the submitted waveform at the first reject's Stage2 endpoint. This reduces
far-speech / ambient tail contamination.

## Why the 1.5 s pre-roll stays

The first Stage2 decision arrives only after the 0.5 s delay. Starting waveform
capture at the ACTIVATE event would lose the beginning of the command. The
controller keeps the latest 1.5 s of the original 16 kHz Ring waveform and prepends
it when a new session starts.

This never changes ProxiMic input. ProxiMic still performs its own 16 kHz -> 8 kHz
path internally; ASR receives the untouched 16 kHz utterance.

## ASR workers are asynchronous

Each ASR backend gets an `ASRWorker`, so model inference / HTTP calls never block
the real-time Ring read loop. When several backends are selected, `ASRFanout`
submits the exact same utterance to each worker.

See `ASR_BACKENDS.md` for backend/model selection, comparison commands, cloud
integration, and the adapter template.
