"""Buffer MIC PCM by frame_seq; write contiguous runs on flush."""

from __future__ import annotations

from collections.abc import Iterator


class OrderedMicBuffer:
    """Hold decoded PCM keyed by frame_seq; emit contiguous runs from the minimum seq."""

    def __init__(self) -> None:
        self._pcm_by_seq: dict[int, bytes] = {}
        self._dropped_seqs: set[int] = set()

    def __len__(self) -> int:
        return len(self._pcm_by_seq)

    def store(self, frame_seq: int, pcm: bytes) -> None:
        self._pcm_by_seq[frame_seq] = pcm
        self._dropped_seqs.discard(frame_seq)

    def mark_dropped(self, frame_seq: int) -> None:
        if frame_seq not in self._pcm_by_seq:
            self._dropped_seqs.add(frame_seq)

    def contiguous_from_min(self) -> Iterator[tuple[int, bytes]]:
        if not self._pcm_by_seq:
            return

        start = min(self._pcm_by_seq.keys() | self._dropped_seqs)
        seq = start
        while seq in self._pcm_by_seq or seq in self._dropped_seqs:
            if seq in self._dropped_seqs:
                self._dropped_seqs.remove(seq)
            else:
                yield seq, self._pcm_by_seq.pop(seq)
            seq = (seq + 1) & 0xFFFF

    def peek_contiguous_pcm(self) -> tuple[int | None, bytes]:
        """Return (first_seq, pcm_bytes) for the contiguous run from min seq without removing."""
        if not self._pcm_by_seq:
            return None, b""

        start = min(self._pcm_by_seq.keys() | self._dropped_seqs)
        seq = start
        chunks: list[bytes] = []
        while seq in self._pcm_by_seq or seq in self._dropped_seqs:
            if seq in self._pcm_by_seq:
                chunks.append(self._pcm_by_seq[seq])
            seq = (seq + 1) & 0xFFFF
        return start, b"".join(chunks)

    def pending_count(self) -> int:
        return len(self._pcm_by_seq)
