# Ringo SDK integration

This project is no longer protocol-neutral: it is wired to the supplied
`ring-python-sdk` implementation.

## Verified SDK audio contract

From the supplied SDK source:

- transport: BLE Nordic UART Service (NUS), handled by `RingSession` / Bleak;
- MIC modes: `pcm`, `adpcm`, `opus`;
- default SDK audio format after decoding: **16,000 Hz, mono, signed PCM16LE**;
- real-time API: `await session.mic_on(..., on_pcm=callback)`;
- callback signature: `callback(frame_seq: int, pcm: bytes)`;
- the SDK simultaneously saves decoded audio to a WAV file;
- Opus blocks contain 5 x 320-sample frames (= 1600 decoded samples / 100 ms per block).

The ProxiMic adapter therefore does not need to know BLE UUIDs, packet headers,
fragment assembly, ADPCM, or Opus internals.  The SDK owns those concerns.

## Runtime path

```text
Ringo microphone
  -> ring-python-sdk / BLE NUS
  -> MIC packet assembly + codec decode
  -> on_pcm(frame_seq, PCM16LE bytes)
  -> RingAudioSource
  -> float32 mono samples [-1, 1)
  -> runner.py
  -> detector.feed()
  -> Stage 1 / delayed Stage 2 / CNN
```

The WAV written by the SDK is a useful experiment/debug recording, but it is
**not** read back into the real-time inference path.

The desktop runtime uses a two-phase startup. It first connects BLE, validates
the NUS service, starts the microphone, and requires a real PCM callback. Only
then does it load the ProxiMic and ASR models. PCM received while models load is
discarded instead of building an unbounded inference backlog; buffering starts
when recognition is ready.

The device picker retains Bleak's selected `BLEDevice` and passes it directly
to `BleakClient`. It does not discard the selection and run another fixed scan;
this is important for macOS identifiers and devices using rotating addresses.

The stream watchdog is fail-closed. If PCM stops, it closes and disconnects the
session immediately. It never restarts MIC or reconnects BLE in the background;
the user must explicitly choose **Reconnect device** in the UI.

After the initial real-PCM validation, the runtime keeps MIC active during
detector/ASR initialization but discards those callbacks instead of sending them
to inference. Once initialization finishes, it requires a fresh PCM callback
before buffering and arming the watchdog. This avoids an unreliable second MIC
ON command on firmware that cannot resume within the same BLE session.

## Codec choice

The customer desktop UI and the live proximity diagnostic default to `pcm` so
the detector receives the waveform distribution used for model training and
threshold calibration.

- `pcm`: recommended for proximity-model consistency, but creates more BLE traffic;
- `adpcm`: lower on-air bandwidth, but lossy compression can shift Stage2 scores;
- `opus`: efficient on-air, but requires `opuslib` and a native libopus runtime.

The detector receives PCM16 in all three cases, but lossy codecs need not preserve
the same waveform or score distribution as raw PCM.
