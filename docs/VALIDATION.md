# Validation against the supplied C++ implementation

The Python port was checked directly against the supplied C++ `prog.h` / `parameters.h` and the original `speech-xiaomi.model`.

## Reference 1: zero input

Original C++ `example.cpp`:

```text
run_with_resample(16000 zeros) = -1.10899
```

Python:

```text
-1.1089893579483032
```

## Reference 2: identical non-zero float32 input

A deterministic 16000-sample float32 signal was written to `tests/data/reference_audio.f32` and read by both implementations.

```text
C++ score    = -3.99417376518
Python score = -3.99417257309
absolute diff ~= 1.19e-6
```

The small floating-point difference is expected from FFT / arithmetic implementation details and is far below the Stage-2 threshold scale.

## Automated tests

At packaging time:

```text
11 passed
```

Tests cover model loading, feature shape, exact reference scores, detector timing, PCM compatibility behavior, and UDP framing.
