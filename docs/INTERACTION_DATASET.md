# Unified interaction dataset

The desktop application writes every completed utterance to one source of
truth under:

```text
<app-data>/dataset/<anonymous-user-id>/interactions/<interaction-id>/
```

新记录的目录名直接使用语音会话开始时的本地系统时间，例如
`interaction_2026-09-04_16-42-09-381`。如果同一毫秒内恰好创建多条记录，
只在后面增加 `_02`、`_03` 这样的顺序号。

Each directory contains `record.json`, `events.jsonl`, `asr_updates.jsonl`,
`audio.wav` when an activated session captured samples, and `imu.jsonl` when
synchronized IMU samples are supplied. This includes a session explicitly
cancelled before text application: its captured audio/IMU is retained for
near-field negative training, but the audio is not submitted as an ASR final.
API keys are never persisted.

`record.json` keeps the final queryable state while `events.jsonl` preserves
the action timeline. It includes ASR metadata, exact non-secret LLM inputs and
outputs, routing results, target snapshots for edit requests, application
events, objective actions, and acceptance strength. A successful application is
immediately stored as weak implicit acceptance without asking the user a
question; undo changes it to explicit negative evidence. Cancelling before any
text is applied is the supported explicit negative label for the near-field
detector. An empty/error ASR result labels ASR only and never labels the
near-field detector.

The Voice History UI is a projection of InteractionRecords. New utterances are
written only to this unified representation; no Episode/Attempt compatibility
directories or duplicate edit records are created.

## Association index

Association does not copy audio, rewrite interactions, or generate processed
training JSON. Both ASR and LLM relationships are appended to one lightweight
index:

```text
<app-data>/dataset/<anonymous-user-id>/associations.jsonl
```

Each row has a stable association ID, kind/subtype, one chosen reference, one
or more rejected references, and the complete member interaction-ID list. A
reference points directly to `interactions/<id>/record.json` and, for LLM
results, the exact request ID. Manual positives are stored once in the source
interaction and referenced by result ID. Every member interaction also keeps a
reverse `association_ids` link plus its chosen/rejected membership. A later ASR/DPO exporter can therefore resolve
one complete group without matching timestamps or duplicating source data.

The manual association center treats a selection as one draft, not as global
labels on history rows. The user explicitly starts one association, chooses
ASR or LLM, selects exactly one chosen member and one or more rejected members,
reviews the complete draft, and confirms it. Nothing is persisted before that
final confirmation; one confirmation calls the collector once and creates one
association ID.

Automatic recommendations are off by default. When enabled, they use a deliberately simple boundary: for the same
target and mode, take at most the five failures after the previous success and
before the current applied/manual result, within 60 seconds. The recommendation
appears as soon as the result is applied. Undo remains available as an operation
stack with no countdown; accepting an association is an explicit commit boundary.
Undo retracts the provisional recommendation and restores its failure chain.
Failure alone never opens a prompt. Empty ASR is kept as an unclassified failure
so the next successful route can resolve it to dictation or instruction mode.
Undo/cancel reasons are not requested. ASR associations distinguish
`dictation_retry` and `instruction_retry`; LLM associations include only
rejected members that have a concrete LLM request/result reference.

Manual-result observation is strictly non-invasive: it reads through macOS AX
or Windows UI Automation without focusing the target, selecting text, sending
copy shortcuts, touching the clipboard, or moving the caret. If a control does
not expose a safe accessibility value, automatic manual-result detection is
skipped and the association center remains the explicit fallback.

## IMU and near-field evidence

The desktop runtime starts the Ring SDK microphone and a 50 Hz IMU stream in
the same BLE session. Audio sample zero is anchored from the PCM callback clock
and the exact number of 16 kHz samples, before detector inference, ASR, or disk
writing can add delay. Ring uptime is mapped to that same host clock using the
last sample in each IMU BLE packet. The saved `relative_to_audio_start_ms` is
therefore directly comparable with `audio_sample_index / 16`.

The in-memory buffer keeps the transport fields needed to calculate alignment,
but `imu.jsonl` stores only `relative_to_audio_start_ms`, calibrated
`accel_ms2`, and calibrated `gyro_dps`. Host/device absolute clocks, packet
sequence values, sample indexes, and duplicate raw sensor units are not written.
Each interaction retains only the sample count/rate, dropped-sample count, and
alignment method needed to validate training data. The saved window starts up
to 300 ms before WAV sample zero and ends at the last sample covered by the WAV;
there is no fictitious post-roll margin.

IMU start, callback, or dataset-write failures are isolated from the microphone
path. Detector status lines
are stored as events; Stage 1/2 score and threshold values are also extracted
into `near_field`. These are evidence fields only. They do not introduce a hard
motion gate or change the current near-field recognition path.
