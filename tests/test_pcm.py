import numpy as np

from proximic_ring.pcm import decode_pcm16le


def test_standard_pcm16le_decoder():
    data = bytes([0x00, 0x00, 0x00, 0x40, 0x00, 0xC0, 0xFF, 0x7F, 0x00, 0x80])
    x = decode_pcm16le(data)
    expected = np.array([0.0, 0.5, -0.5, 32767 / 32768, -1.0], dtype=np.float32)
    np.testing.assert_allclose(x, expected, atol=1e-7)


def test_pcm16le_rejects_odd_byte_count():
    try:
        decode_pcm16le(b"\x01")
    except ValueError as exc:
        assert "even number of bytes" in str(exc)
    else:
        raise AssertionError("expected ValueError")
