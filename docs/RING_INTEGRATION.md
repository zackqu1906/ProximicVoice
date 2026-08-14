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

## Codec choice

`proximic-ring ring` defaults to `--encoding pcm` for the first hardware
integration because it avoids a native Opus runtime and gives the detector the
simplest possible signal path.

- `pcm`: no audio codec dependency beyond the SDK/BLE path;
- `adpcm`: compressed on-air, decoded by the SDK;
- `opus`: efficient on-air, but requires `opuslib` and a native libopus runtime.

The detector sees the same decoded PCM16 stream in all three cases.
