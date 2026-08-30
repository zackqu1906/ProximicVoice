from ring_python_sdk.audio.opus_codec import OrderedOpusDecoder


class _IdentityDecoder:
    def decode_block(self, payload: bytes) -> bytes:
        return payload

    def reset(self) -> None:
        return None


def test_live_opus_decoder_starts_at_first_observed_sequence(monkeypatch):
    decoder = OrderedOpusDecoder()
    monkeypatch.setattr(decoder, "_ensure_decoder", lambda: _IdentityDecoder())

    decoded = decoder.push(37, b"first")

    assert [(item.frame_seq, item.pcm) for item in decoded] == [(37, b"first")]


def test_live_opus_decoder_drops_gap_and_continues(monkeypatch):
    decoder = OrderedOpusDecoder()
    codec = _IdentityDecoder()
    monkeypatch.setattr(decoder, "_ensure_decoder", lambda: codec)
    decoder.push(10, b"ten")

    decoded = decoder.push(12, b"twelve")

    assert [item.frame_seq for item in decoded] == [11, 12]
    assert decoded[0].pcm is None
    assert "incomplete" in str(decoded[0].error)
    assert decoded[1].pcm == b"twelve"
