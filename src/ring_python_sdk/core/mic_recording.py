"""Flash-backed MIC recording packet and stored Opus-block helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_MIC,
    MIC_RECORD_DATA_HEADER_LEN,
    MIC_RECORD_FLAG_INTERRUPTED,
    MIC_RECORD_LIST_END_PACKET_LEN,
    MIC_RECORD_LIST_ITEM_PACKET_LEN,
    MIC_RECORD_READ_END_PACKET_LEN,
    MIC_RECORD_STATUS_PACKET_LEN,
    SUBCMD_MIC_RECORD_DATA,
    SUBCMD_MIC_RECORD_LIST_END,
    SUBCMD_MIC_RECORD_LIST_ITEM,
    SUBCMD_MIC_RECORD_READ_END,
    SUBCMD_MIC_RECORD_STATUS,
)


@dataclass(frozen=True)
class MicRecordingStatus:
    recording: bool
    recording_id: int
    bytes: int
    err_code: int
    start_uptime_ms: int
    start_unix_ms: int


@dataclass(frozen=True)
class MicRecordingListItem:
    recording_id: int
    bytes: int
    duration_ms: int
    start_uptime_ms: int
    start_unix_ms: int
    flags: int

    @property
    def interrupted(self) -> bool:
        return bool(self.flags & MIC_RECORD_FLAG_INTERRUPTED)


@dataclass(frozen=True)
class MicRecordingDataChunk:
    recording_id: int
    offset: int
    payload: bytes


@dataclass(frozen=True)
class MicRecordingReadEnd:
    recording_id: int
    next_offset: int
    done: bool
    err_code: int


@dataclass(frozen=True)
class MicRecordingBlock:
    seq: int
    uptime_ms: int
    opus: bytes


def parse_mic_record_status(packet: bytes | bytearray) -> MicRecordingStatus | None:
    if len(packet) < MIC_RECORD_STATUS_PACKET_LEN:
        return None
    if packet[0] != CMD_MIC or packet[1] != SUBCMD_MIC_RECORD_STATUS:
        return None
    recording_id, nbytes, err, start_uptime_ms, start_unix_ms = struct.unpack_from(
        "<IIhIq", packet, 3
    )
    return MicRecordingStatus(
        bool(packet[2]),
        recording_id,
        nbytes,
        err,
        start_uptime_ms,
        start_unix_ms,
    )


def parse_mic_record_list_item(
    packet: bytes | bytearray,
) -> MicRecordingListItem | None:
    if len(packet) < MIC_RECORD_LIST_ITEM_PACKET_LEN:
        return None
    if packet[0] != CMD_MIC or packet[1] != SUBCMD_MIC_RECORD_LIST_ITEM:
        return None
    recording_id, nbytes, duration_ms, start_uptime_ms, start_unix_ms, flags = (
        struct.unpack_from(
            "<IIIIqB", packet, 2
        )
    )
    return MicRecordingListItem(
        recording_id,
        nbytes,
        duration_ms,
        start_uptime_ms,
        start_unix_ms,
        flags,
    )


def parse_mic_record_list_end(packet: bytes | bytearray) -> int | None:
    if len(packet) < MIC_RECORD_LIST_END_PACKET_LEN:
        return None
    if packet[0] != CMD_MIC or packet[1] != SUBCMD_MIC_RECORD_LIST_END:
        return None
    return struct.unpack_from("<I", packet, 2)[0]


def parse_mic_record_data(
    packet: bytes | bytearray,
) -> MicRecordingDataChunk | None:
    if len(packet) < MIC_RECORD_DATA_HEADER_LEN:
        return None
    if packet[0] != CMD_MIC or packet[1] != SUBCMD_MIC_RECORD_DATA:
        return None
    recording_id, offset, length = struct.unpack_from("<IIH", packet, 2)
    if len(packet) < MIC_RECORD_DATA_HEADER_LEN + length:
        return None
    payload = bytes(packet[MIC_RECORD_DATA_HEADER_LEN : MIC_RECORD_DATA_HEADER_LEN + length])
    return MicRecordingDataChunk(recording_id, offset, payload)


def parse_mic_record_read_end(
    packet: bytes | bytearray,
) -> MicRecordingReadEnd | None:
    if len(packet) < MIC_RECORD_READ_END_PACKET_LEN:
        return None
    if packet[0] != CMD_MIC or packet[1] != SUBCMD_MIC_RECORD_READ_END:
        return None
    recording_id, next_offset, done, err = struct.unpack_from("<IIBh", packet, 2)
    return MicRecordingReadEnd(recording_id, next_offset, bool(done), err)


def iter_mic_recording_blocks(data: bytes | bytearray):
    """Iterate `[seq:u32, uptime_ms:u32, len:u16, opus...]` records."""
    offset = 0
    while offset < len(data):
        if len(data) - offset < 10:
            raise ValueError("truncated MIC recording block header")
        seq, uptime_ms, length = struct.unpack_from("<IIH", data, offset)
        offset += 10
        if len(data) - offset < length:
            raise ValueError("truncated MIC recording Opus block")
        yield MicRecordingBlock(seq, uptime_ms, bytes(data[offset : offset + length]))
        offset += length
