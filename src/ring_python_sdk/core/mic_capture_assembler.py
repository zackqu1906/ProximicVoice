"""MIC frame assembler for capture: accepts out-of-order gap-fill."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ring_python_sdk.core.constants import (
    CMD_MIC,
    MIC_HEADER_SIZE,
    SUBCMD_MIC_PACKET,
    SUBCMD_MIC_PACKET_ADPCM,
    SUBCMD_MIC_PACKET_OPUS,
)


@dataclass
class MicFrame:
    subcmd: int
    frag_count: int
    frags: dict[int, bytes] = field(default_factory=dict)

    def add_fragment(self, frag_idx: int, payload: bytes) -> bool:
        self.frags[frag_idx] = payload
        if len(self.frags) < self.frag_count:
            return False
        return all(idx in self.frags for idx in range(self.frag_count))

    def build_payload(self) -> bytes:
        payload = bytearray()
        for idx in range(self.frag_count):
            frag = self.frags.get(idx)
            if frag is None:
                raise ValueError(f"missing fragment idx={idx}")
            payload.extend(frag)
        return bytes(payload)


class MicCaptureAssembler:
    """Assemble MIC notify packets; keep completed frames by seq for ordered flush."""

    def __init__(self) -> None:
        self._inflight: dict[int, MicFrame] = {}
        self._completed: dict[int, tuple[int, bytes]] = {}
        self.completed_frames: int = 0
        self.incomplete_notify_packets: int = 0
        self.repeated_completed_seq_packets: int = 0
        self.last_frame_seq: int | None = None
        self.last_frag_idx: int | None = None
        self.last_frag_count: int | None = None

    def add_packet(self, packet: bytes) -> Optional[tuple[int, bytes, int]]:
        """Return (subcmd, frame_payload, frame_seq) when a frame completes."""
        if len(packet) < MIC_HEADER_SIZE:
            return None
        if packet[0] != CMD_MIC:
            return None

        subcmd = packet[1]
        if subcmd not in {
            SUBCMD_MIC_PACKET,
            SUBCMD_MIC_PACKET_ADPCM,
            SUBCMD_MIC_PACKET_OPUS,
        }:
            return None

        frame_seq = packet[2] | (packet[3] << 8)
        frag_idx = packet[4] | (packet[5] << 8)
        frag_count = packet[6] | (packet[7] << 8)
        payload = packet[MIC_HEADER_SIZE:]
        self.last_frame_seq = frame_seq
        self.last_frag_idx = frag_idx
        self.last_frag_count = frag_count

        if frag_count == 0 or frag_idx >= frag_count:
            return None

        if frame_seq in self._completed:
            self.repeated_completed_seq_packets += 1
            return None

        frame = self._inflight.get(frame_seq)
        if frame is None:
            frame = MicFrame(subcmd=subcmd, frag_count=frag_count)
            self._inflight[frame_seq] = frame
        elif frame.frag_count != frag_count or frame.subcmd != subcmd:
            frame = MicFrame(subcmd=subcmd, frag_count=frag_count)
            self._inflight[frame_seq] = frame

        if not frame.add_fragment(frag_idx, payload):
            return None

        frame_payload = frame.build_payload()
        self._inflight.pop(frame_seq, None)
        self._completed[frame_seq] = (subcmd, frame_payload)
        self.completed_frames += 1
        return subcmd, frame_payload, frame_seq

    def note_incomplete_notify(self) -> None:
        self.incomplete_notify_packets += 1

    def completed_seqs(self) -> list[int]:
        return sorted(self._completed.keys())

    def pop_frame(self, frame_seq: int) -> tuple[int, bytes] | None:
        item = self._completed.pop(frame_seq, None)
        return item

    def has_frames(self) -> bool:
        return bool(self._completed) or bool(self._inflight)

    @property
    def completed_frame_count(self) -> int:
        return len(self._completed)

    @property
    def inflight_frame_count(self) -> int:
        return len(self._inflight)

    @property
    def inflight_seqs(self) -> set[int]:
        """Return the block sequences that still have missing fragments."""
        return set(self._inflight)
