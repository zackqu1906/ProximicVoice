import struct

_STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]

_INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
_HEADER_SIZE = 6


def _clamp_predictor(value: int) -> int:
    return max(-32768, min(32767, value))


def _clamp_index(value: int) -> int:
    return max(0, min(88, value))


def decode_ima_adpcm_frame(frame: bytes) -> bytes:
    if len(frame) < _HEADER_SIZE:
        return b""

    predictor, index, _reserved, sample_count = struct.unpack_from("<hBBH", frame, 0)
    if sample_count == 0:
        return b""

    pcm = bytearray()
    pcm.extend(struct.pack("<h", predictor))

    expected_nibbles = sample_count - 1
    nibble_idx = 0
    payload = frame[_HEADER_SIZE:]

    for byte in payload:
        for shift in (0, 4):
            if nibble_idx >= expected_nibbles:
                break

            code = (byte >> shift) & 0x0F
            step = _STEP_TABLE[index]
            diffq = step >> 3
            if code & 4:
                diffq += step
            if code & 2:
                diffq += step >> 1
            if code & 1:
                diffq += step >> 2

            if code & 8:
                predictor -= diffq
            else:
                predictor += diffq

            predictor = _clamp_predictor(predictor)
            index = _clamp_index(index + _INDEX_TABLE[code])
            pcm.extend(struct.pack("<h", predictor))
            nibble_idx += 1

    return bytes(pcm)
