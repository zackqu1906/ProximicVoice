# Ring dataset collection and training

The current binary task is:

```text
target 0 = near speech
target 1 = realistic non-target audio
```

Collection keeps three source labels so the negative class remains interpretable:

```text
near      -> target 0
far       -> target 1
artifact  -> target 1
```

`artifact` is for non-speech hard negatives such as airflow, hand/ring motion, fabric rubbing, or object contact. The CNN still has only two outputs.

## 1. Collect near speech

```powershell
python -m proximic_ring collect `
  --label near --distance-cm 2 --speaker p01 --style normal `
  --count 8 --duration 8 --prompts docs\phrases_example.txt
```

## 2. Collect far speech

```powershell
python -m proximic_ring collect `
  --label far --distance-cm 30 --speaker p01 --style normal `
  --count 8 --duration 8 --prompts docs\phrases_example.txt
```

## 3. Collect artifact hard negatives

Airflow example:

```powershell
python -m proximic_ring collect `
  --label artifact --speaker p01 --style airflow `
  --count 8 --duration 8 --prompts docs\artifact_prompts\airflow.txt
```

Hand-motion example:

```powershell
python -m proximic_ring collect `
  --label artifact --speaker p01 --style hand_motion `
  --count 8 --duration 8 --prompts docs\artifact_prompts\hand_motion.txt
```

Other prompt files are provided for `fabric_rub` and `contact`.

For artifact recordings, distance is optional and defaults to 0 / not applicable. `speaker_id` is retained as provenance (the subject/operator who produced the recording), but the default split does not use it.

## 4. Dataset layout

```text
datasets/ring_proximity/
  metadata.csv
  raw/
    near/
    far/
    artifact/
```

The metadata CSV is the training index. `class_name` preserves near/far/artifact, while `target` is the binary target 0/1.

## 5. Split policy

Three useful split modes are available.

### File split (strict recording-level split)

```text
--split-by file
```

Whole WAV takes are stratified separately by `class_name`, so near, far, and artifact each appear in train/validation/test when at least three takes of each type exist. All 1-second windows from one original WAV remain in one split.

### Segment split (efficient long-recording workflow)

```text
--split-by segment --split-segment-duration 8
```

In this mode a long WAV is **not physically rewritten**. During training it is represented as consecutive non-overlapping 8-second pseudo-takes, for example:

```text
120-s WAV
 -> [0,8) [8,16) [16,24) ...
 -> pseudo-takes are stratified into train/val/test
 -> each pseudo-take is then converted to 1-s model windows
```

The final remainder shorter than 8 seconds is dropped for a long WAV, so all pseudo-takes used for splitting have the same duration. Existing recordings shorter than 8 seconds are retained as one legacy take.

This lets collection use much longer recordings, for example:

```powershell
python -m proximic_ring collect `
  --label artifact --speaker p01 --style airflow `
  --count 1 --duration 120 --prompts docs\artifact_prompts\airflow.txt
```

Then train with:

```powershell
python -m proximic_ring train `
  --dataset datasets\ring_proximity `
  --run-dir runs\near_vs_nontarget_segment_v1 `
  --epochs 30 --batch-size 32 --init scratch `
  --split-by segment --split-segment-duration 8
```

A 1-second model window and its training jitter are constrained to its own pseudo-take, so no model window crosses an 8-second train/val/test boundary.

**Important:** segment split intentionally allows different 8-second chunks from the same original recording to appear in different splits. This is convenient for rapid engineering iteration, but it is not as strict as holding out whole recordings/sessions.

### Speaker split (strongest unseen-user evaluation)

```text
--split-by speaker
```

A large sample count does not by itself remove speaker/session leakage. Use file or speaker split for stricter final reporting; use segment split when efficient collection and development are the priority.

## 6. Background-noise mixing

Do not destructively overwrite the raw Ring recordings. Put ambient non-speech WAV files in a separate directory, for example:

```text
datasets/background_noise/
  office_01.wav
  office_02.wav
  cafe_01.wav
  fan_01.wav
  ...
```

Background files may be mono or multi-channel PCM16 WAVs and may use a different sample rate; the training loader downmixes and resamples them to 16 kHz.

When `--noise-dir` is supplied, noise is mixed on-the-fly into each 1-second model window before feature extraction. With the default `--noise-prob 1.0`, every window receives noise.

Noise WAV files themselves are split into disjoint train/validation/test pools, so the exact same background recording is not reused across the three model splits.

Training noise is randomized each epoch. Validation/test noise is deterministic for reproducible metrics.

## 7. Train a new model from scratch

Example with background noise:

```powershell
python -m proximic_ring train `
  --dataset datasets\ring_proximity `
  --run-dir runs\near_vs_nontarget_v1 `
  --epochs 30 --batch-size 32 --init scratch `
  --split-by file `
  --noise-dir datasets\background_noise `
  --noise-prob 1.0 `
  --noise-snr-min-db 12 `
  --noise-snr-max-db 25
```

The background-noise parameters mean that each window gets a randomly chosen ambient-noise segment at roughly 12–25 dB SNR. Adjust the SNR range after listening to augmented examples or measuring realistic deployment noise.

If you want noise augmentation only in training and clean validation/test, add:

```text
--no-noise-eval
```

## 8. Model input pipeline

File mode:

```text
raw Ring WAV
 -> whole-WAV train/val/test split
 -> overlapping 1-s window
 -> optional background-noise mixing
 -> 16 kHz to 8 kHz legacy downsampler
 -> legacy STFT/filter-bank features (20, 201)
 -> CnnNet8
 -> logits[0] - logits[1]
```

Segment mode:

```text
long raw Ring WAV
 -> logical non-overlapping 8-s pseudo-takes
 -> pseudo-take train/val/test split
 -> overlapping 1-s window inside each pseudo-take
 -> optional background-noise mixing
 -> unchanged 16 kHz to 8 kHz + feature + CnnNet8 pipeline
```

Training windows keep the existing start-time jitter. Validation/test have zero time jitter.

## 9. Outputs

```text
runs/near_vs_nontarget_v1/
  best.model
  best.model.json
  last.model
  metrics.json
  history.csv
  split_manifest.csv
  window_manifest.csv
  validation_outputs.npz
  test_outputs.npz
  validation_take_outputs.npz
  test_take_outputs.npz
```

`metrics.json` records the binary metrics plus take-level behavior broken down by `class_name` and `speech_style`, making it possible to inspect far-speech rejection separately from airflow/hand-motion/etc. artifact rejection.

## 10. Runtime

```powershell
python -m proximic_ring ring `
  --model runs\near_vs_nontarget_v1\best.model `
  --show-stage1 --stage1-threshold 0.005
```

The calibrated Stage-2 threshold is read from `best.model.json` when no explicit Stage-2 threshold is given.
