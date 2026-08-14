# Porting notes: WearOS -> Python

## 原始调用链

```text
MainActivity.java
  AudioRecord(16 kHz, mono, PCM16)
  read 640 bytes ~= 320 samples ~= 20 ms
      |
      v
native-lib.cpp::feed()
  PCM -> float
  circular buffer: 16000 samples
  Stage 1: max amplitude > threshold_1
  wait 0.5 s
      |
      v
second_stage()
  latest 1 s @16 kHz
  resample 16k -> 8k
      |
      v
prog.h::run()
  filter_bank_feature -> (20, 201)
  CNN -> 2 logits
  score = output[0] - output[1]
      |
      v
native-lib.cpp
  score > threshold_2 -> Java true
      |
      v
MainActivity.java
  vibration + 1 kHz beep
```

## Python mapping

| WearOS/C++ | Python |
|---|---|
| `AudioRecord` | `audio/microphone.py`, or a Ring transport adapter |
| `feed()` | `ProxiMicDetector.feed()` |
| `audio[16000]` | `_CircularAudioBuffer` |
| `first_stage()` | `detector.py` Stage 1 |
| `resample.h` | `resample.py` |
| `fft.h::filter_bank_feature` | `features.py` |
| `prog.h::run_model` | `model.py::CnnNet8` |
| `speech-xiaomi.model` | packaged unchanged in `assets/` |
| `threshold_2` | `DetectorConfig.stage2_threshold` |

## Why the assets are packaged

The original C++ deployment converts float coefficients into raw uint32 literals in `parameters.h`. This Python port takes the safer route:

- load the original PyTorch `speech-xiaomi.model` directly for network weights;
- export the exact original 20x81 filter bank to `filter_bank.npy`;
- export the exact original resampling interpolation table to `interp_win.npy`.

This avoids accidentally changing old inference behavior because of a newer librosa/resampy default.

## Training is not reproduced

The supplied project contains the network definition and a trained checkpoint, but no training dataset / dataloader / optimizer / loss / training loop. Therefore this repository only claims inference equivalence.
