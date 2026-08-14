import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import struct

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
from ring_python_sdk.core.seq_tracker import SeqTracker

PCM16_MIN = -32768
PCM16_MAX = 32767
AUDIO_GAIN = 1.0
FLAT_PCM_PEAK_THRESHOLD = 64


def _apply_gain_pcm16le(pcm: bytes, gain: float) -> bytes:
    if gain == 1.0 or not pcm:
        return pcm

    out = bytearray(len(pcm))
    write_off = 0
    for (sample,) in struct.iter_unpack("<h", pcm):
        boosted = int(sample * gain)
        if boosted > PCM16_MAX:
            boosted = PCM16_MAX
        elif boosted < PCM16_MIN:
            boosted = PCM16_MIN
        struct.pack_into("<h", out, write_off, boosted)
        write_off += 2
    return bytes(out)


@dataclass
class AudioStats:
    packet_count: int = 0
    frame_count: int = 0
    dropped_packet_count: int = 0
    dropped_frame_count: int = 0
    flat_frame_count: int = 0
    written_bytes: int = 0


class AudioProcessor:
    def __init__(
        self,
        output_path: Path,
        *,
        print_frames: bool = False,
        live_plot=None,
        encoding: str | None = None,
        on_pcm: Callable[[int, bytes], None] | None = None,
    ) -> None:
        self.output_path = output_path
        self.print_frames = print_frames
        self._live_plot = live_plot
        self.on_pcm = on_pcm
        self._assembler = MicCaptureAssembler()
        self._buffer = OrderedMicBuffer()
        self._frame_seq = SeqTracker(bits=16)
        self._opus = OrderedOpusDecoder(eager=encoding == "opus")
        self._closed = False
        self.stats = AudioStats()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._wave = wave.open(str(output_path), "wb")
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
        self._wave.close()
        self._closed = True

    def _accept_pcm(self, frame_seq: int, pcm: bytes) -> None:
        if not pcm or len(pcm) % DEFAULT_SAMPLE_WIDTH_BYTES != 0:
            self.stats.dropped_frame_count += 1
            self._buffer.mark_dropped(frame_seq)
            return

        self._buffer.store(frame_seq, pcm)
        self.stats.frame_count += 1
        if self.on_pcm is not None:
            self.on_pcm(frame_seq, pcm)
        if self._live_plot is not None:
            self._live_plot.add_pcm(pcm)
        if self.print_frames:
            print(f"mic frame={self.stats.frame_count} bytes={len(pcm)}")

    def _accept_opus_results(self, results: list[DecodedOpusBlock]) -> None:
        for result in results:
            if result.pcm is None:
                self.stats.dropped_frame_count += 1
                self._buffer.mark_dropped(result.frame_seq)
                if self.print_frames and result.error is not None:
                    print(f"mic opus frame={result.frame_seq} dropped: {result.error}")
                continue
            self._accept_pcm(result.frame_seq, result.pcm)

    def _write_pcm(self, pcm: bytes) -> None:
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        pcm_peak = max(samples) - min(samples) if samples else 0
        if pcm_peak < FLAT_PCM_PEAK_THRESHOLD:
            self.stats.flat_frame_count += 1

        pcm = _apply_gain_pcm16le(pcm, AUDIO_GAIN)
        self._wave.writeframesraw(pcm)
        self.stats.written_bytes += len(pcm)

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
        self._frame_seq.observe(frame_seq)

        if subcmd == SUBCMD_MIC_PACKET_OPUS:
            self._accept_opus_results(self._opus.push(frame_seq, frame_payload))
            return
        if subcmd == SUBCMD_MIC_PACKET_ADPCM:
            pcm = decode_ima_adpcm_frame(frame_payload)
        else:
            pcm = frame_payload

        # Count / plot on decode so TUI stats update live; WAV stays ordered.
        self._accept_pcm(frame_seq, pcm)
