from dataclasses import dataclass, field
from typing import Optional

from ring_python_sdk.core.seq_tracker import SeqTracker
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


class MicPacketAssembler:
    def __init__(self) -> None:
        self._frames: dict[int, MicFrame] = {}
        self._last_completed_seq = -1
        self._frame_seq = SeqTracker(bits=16)
        self.completed_frames: int = 0
        self.incomplete_notify_packets: int = 0

    def add_packet(self, packet: bytes) -> Optional[tuple[int, bytes]]:
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

        if frag_count == 0 or frag_idx >= frag_count:
            return None
        if frame_seq <= self._last_completed_seq:
            return None

        frame = self._frames.get(frame_seq)
        if frame is None:
            frame = MicFrame(subcmd=subcmd, frag_count=frag_count)
            self._frames[frame_seq] = frame
        elif frame.frag_count != frag_count or frame.subcmd != subcmd:
            frame = MicFrame(subcmd=subcmd, frag_count=frag_count)
            self._frames[frame_seq] = frame

        if not frame.add_fragment(frag_idx, payload):
            return None

        frame_payload = frame.build_payload()
        self._last_completed_seq = frame_seq
        self._frame_seq.observe(frame_seq)
        self.completed_frames += 1
        self._frames.pop(frame_seq, None)

        stale = [seq for seq in self._frames.keys() if seq < frame_seq]
        for seq in stale:
            self._frames.pop(seq, None)

        return frame.subcmd, frame_payload

    def note_incomplete_notify(self) -> None:
        self.incomplete_notify_packets += 1
