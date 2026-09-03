# Unified interaction dataset

The desktop application writes every completed utterance to one source of
truth under:

```text
<app-data>/dataset/<anonymous-user-id>/interactions/<interaction-id>/
```

Each directory contains `record.json`, `events.jsonl`, `asr_updates.jsonl`,
`audio.wav` when audio is available, and `imu.jsonl` when synchronized IMU
samples are supplied. API keys are never persisted.

`record.json` keeps the final queryable state while `events.jsonl` preserves
the action timeline. It includes ASR metadata, exact non-secret LLM inputs and
outputs, routing results, target snapshots for edit requests, application
events, feedback, and acceptance strength. Applying text is initially marked
`pending_undo`; starting the next utterance without undo is only a weak,
implicit acceptance. Cancel and undo are explicit negative signals.

## Compatibility

`ModificationDatasetCollector` retains its historical name and still creates
Episode/Attempt views for existing edit tools. Their `interaction_id` points
to the unified source record. `audio_raw.wav` is a hard link to the unified
`audio.wav` on supported filesystems, so the recording is not stored twice.

The Voice History UI is now a projection of InteractionRecords. Existing
files in the old `voice_history` directory remain readable but new utterances
are written only to the unified dataset.

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
reverse `association_ids` link. A later ASR/DPO exporter can therefore resolve
one complete group without matching timestamps or duplicating source data.

The manual association center treats a selection as one draft, not as global
labels on history rows. The user explicitly starts one association, chooses
ASR or LLM, selects exactly one chosen member and one or more rejected members,
reviews the complete draft, and confirms it. Nothing is persisted before that
final confirmation; one confirmation calls the collector once and creates one
association ID.

Automatic recommendations use a deliberately simple boundary: for the same
target and mode, take at most the five failures after the previous success and
before the current applied/manual result, within 60 seconds. The recommendation
appears as soon as the result is applied. The user has five seconds to undo;
accepting the association or letting that window expire confirms the result.
Undo retracts the provisional recommendation and restores its failure chain.
Failure alone never opens a prompt. Empty ASR is kept as an unclassified failure
so the next successful route can resolve it to dictation or instruction mode.
Undo/cancel reasons are not requested. ASR associations distinguish
`dictation_retry` and `instruction_retry`; LLM associations include only
rejected members that have a concrete LLM request/result reference.

## IMU and near-field evidence

The desktop runtime starts the Ring SDK microphone and a 50 Hz IMU stream in
the same BLE session. It keeps a bounded in-memory sensor buffer, slices it by
the final utterance audio interval (with a small packet-arrival margin), and
writes the raw accelerometer/gyroscope rows to the same interaction. Both the
device uptime and host monotonic timestamp are retained for later alignment.

IMU start, callback, or dataset-write failures are isolated from the microphone
path and stored as collection diagnostics when possible. Detector status lines
are stored as events; Stage 1/2 score and threshold values are also extracted
into `near_field`. These are evidence fields only. They do not introduce a hard
motion gate or change the current near-field recognition path.
