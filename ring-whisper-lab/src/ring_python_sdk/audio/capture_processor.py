"""Audio processor for web capture: buffers frames by seq, flushes in order on close."""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from typing import IO

from ring_python_sdk.audio.adpcm import decode_ima_adpcm_frame
from ring_python_sdk.audio.opus_codec import DecodedOpusBlock, OrderedOpusDecoder
from ring_python_sdk.audio.ordered_mic_flush import OrderedMicBuffer
from ring_python_sdk.core.constants import (
    CMD_MIC,
    DEFAULT_CHANNELS,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SAMPLE_WIDTH_BYTES,
    SUBCMD_MIC_PACKET,
    SUBCMD_MIC_PACKET_ADPCM,
    SUBCMD_MIC_PACKET_OPUS,
)
from ring_python_sdk.core.mic_capture_assembler import MicCaptureAssembler

from ring_python_sdk.audio.processor import (
    AUDIO_GAIN,
    FLAT_PCM_PEAK_THRESHOLD,
    AudioStats,
    _apply_gain_pcm16le,
)

MIN_VALID_WAV_BYTES = 128
SAMPLES_PER_FRAME = 160
FRAME_BYTES = SAMPLES_PER_FRAME * DEFAULT_SAMPLE_WIDTH_BYTES


class CaptureAudioProcessor:
    def __init__(self, output_path: Path, *, encoding: str | None = None) -> None:
        self.output_path = output_path
        self._assembler = MicCaptureAssembler()
        self._buffer = OrderedMicBuffer()
        self._opus = OrderedOpusDecoder(eager=encoding == "opus")
        self.stats = AudioStats()
        self._wave: IO[bytes] | None = None
        self._closed = False

    @property
    def completed_frame_count(self) -> int:
        return self._assembler.completed_frame_count

    @property
    def pending_frame_count(self) -> int:
        return self._buffer.pending_count() + self._opus.pending_count

    def export_partial_pcm(self) -> tuple[int | None, int, bytes]:
        first_seq, pcm = self._buffer.peek_contiguous_pcm()
        if first_seq is None or not pcm:
            return None, 0, b""
        frame_count = len(pcm) // FRAME_BYTES
        return first_seq, frame_count, pcm

    def import_contiguous_pcm(self, pcm: bytes, first_seq: int) -> None:
        for offset in range(0, len(pcm), FRAME_BYTES):
            frame = pcm[offset : offset + FRAME_BYTES]
            if len(frame) != FRAME_BYTES:
                continue
            seq = (first_seq + offset // FRAME_BYTES) & 0xFFFF
            self._buffer.store(seq, frame)

    def _ensure_wave(self) -> None:
        if self._wave is not None:
            return
        self._wave = wave.open(str(self.output_path), "wb")
        self._wave.setnchannels(DEFAULT_CHANNELS)
        self._wave.setsampwidth(DEFAULT_SAMPLE_WIDTH_BYTES)
        self._wave.setframerate(DEFAULT_SAMPLE_RATE)

    def close(self) -> None:
        if self._closed:
            return
        decoded, blocked_count = self._opus.finish(self._assembler.inflight_seqs)
        self._accept_opus_results(decoded)
        self.stats.dropped_frame_count += blocked_count
        self._flush_buffer()
        if self._wave is not None:
            self._wave.close()
            self._wave = None
        self._closed = True

    def _decode_frame(self, subcmd: int, frame_payload: bytes) -> bytes | None:
        if subcmd == SUBCMD_MIC_PACKET_ADPCM:
            pcm = decode_ima_adpcm_frame(frame_payload)
        else:
            pcm = frame_payload

        if not pcm or len(pcm) % DEFAULT_SAMPLE_WIDTH_BYTES != 0:
            self.stats.dropped_frame_count += 1
            return None
        return pcm

    def _accept_opus_results(self, results: list[DecodedOpusBlock]) -> None:
        for result in results:
            if result.pcm is None:
                self.stats.dropped_frame_count += 1
                self._buffer.mark_dropped(result.frame_seq)
            else:
                self._buffer.store(result.frame_seq, result.pcm)

    def _write_pcm(self, pcm: bytes) -> None:
        self._ensure_wave()
        assert self._wave is not None

        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        pcm_peak = max(samples) - min(samples) if samples else 0
        if pcm_peak < FLAT_PCM_PEAK_THRESHOLD:
            self.stats.flat_frame_count += 1

        pcm = _apply_gain_pcm16le(pcm, AUDIO_GAIN)
        self._wave.writeframesraw(pcm)
        self.stats.written_bytes += len(pcm)
        self.stats.frame_count += 1

    def _flush_buffer(self) -> None:
        for _seq, pcm in self._buffer.contiguous_from_min():
            self._write_pcm(pcm)

    def handle_notification(self, _: int, data: bytearray) -> None:
        self.stats.packet_count += 1
        assembled = self._assembler.add_packet(bytes(data))
        if assembled is None:
            if (
                len(data) >= 2
                and data[0] == CMD_MIC
                and data[1]
                in {
                    SUBCMD_MIC_PACKET,
                    SUBCMD_MIC_PACKET_ADPCM,
                    SUBCMD_MIC_PACKET_OPUS,
                }
            ):
                self._assembler.note_incomplete_notify()
            else:
                self.stats.dropped_packet_count += 1
            return

        subcmd, frame_payload, frame_seq = assembled
        if subcmd == SUBCMD_MIC_PACKET_OPUS:
            self._accept_opus_results(self._opus.push(frame_seq, frame_payload))
            return
        pcm = self._decode_frame(subcmd, frame_payload)
        if pcm is not None:
            self._buffer.store(frame_seq, pcm)
        else:
            self._buffer.mark_dropped(frame_seq)
