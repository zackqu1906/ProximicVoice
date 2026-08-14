# Supplied ring-python-sdk audit

The supplied `ring-python-sdk-main.zip` was inspected before this integration.
The important MIC findings are:

1. `RingSession.mic_on(encode_name, on_pcm=...)` is the intended high-level API.
2. BLE scanning/connection and Nordic UART Service characteristic discovery are
   already implemented by the SDK; ProxiMic should not duplicate them.
3. `AudioProcessor` assembles MIC fragments and supports raw PCM, IMA ADPCM, and
   Opus packets.
4. `AudioProcessor._accept_pcm(...)` invokes the real-time `on_pcm` callback and
   writes the same decoded stream to WAV.
5. SDK constants define 16 kHz, one channel, 2-byte samples.  This is exactly the
   ProxiMic detector's required front-end format.
6. Opus decoding produces 320 samples per Opus frame and 5 frames per block.

Because the SDK already solves the hardware protocol, the previous generic
`ble.py`, `serial.py`, and `udp.py` adapters were removed from ProxiMic.
