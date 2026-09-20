"""Strict, per-recording Opus decoding for MIC blocks."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes.util
import os
from pathlib import Path
import struct
import sys
import threading
from collections.abc import Iterable
from typing import Any

from ring_python_sdk.core.constants import DEFAULT_SAMPLE_RATE

OPUS_FRAME_SAMPLES = 320
OPUS_FRAMES_PER_BLOCK = 5
OPUS_BLOCK_SAMPLES = OPUS_FRAME_SAMPLES * OPUS_FRAMES_PER_BLOCK
OPUS_MAX_FRAME_BYTES = 256
PCM_SAMPLE_BYTES = 2
_WINDOWS_DLL_HANDLES: list[object] = []
_OPUS_IMPORT_LOCK = threading.RLock()


class OpusCodecError(RuntimeError):
    """An Opus MIC block is malformed or cannot be decoded."""


class OpusUnavailableError(OpusCodecError):
    """The Python binding or native libopus runtime is unavailable."""


def _load_opuslib() -> Any:
    # Import temporarily redirects ctypes discovery; concurrent recordings must
    # not restore each other's resolver while opuslib is still importing.
    with _OPUS_IMPORT_LOCK:
        return _load_opuslib_locked()


def _runtime_unavailable_message() -> str:
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        return "应用内置 Opus 解码组件缺失或无法加载，请重新安装完整应用；无需另装 Opus。"
    if sys.platform == "darwin":
        return "项目内 Opus 解码组件不可用，请运行 scripts/install-opus-macos.sh，并安装 requirements.txt 中的依赖。"
    return "Opus 解码不可用，需要 opuslib 和原生 libopus 运行库。"


def _load_opuslib_locked() -> Any:
    _prepare_windows_opus_runtime()
    bundled_opus = _bundled_macos_opus()
    original_find_library = ctypes.util.find_library
    if bundled_opus is not None:
        ctypes.util.find_library = lambda name: (
            str(bundled_opus) if name == "opus" else original_find_library(name)
        )
    try:
        import opuslib
        if bundled_opus is not None and (getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")):
            loaded = getattr(opuslib.api.libopus, "_name", "")
            if not loaded or Path(loaded).resolve() != bundled_opus:
                raise RuntimeError("Opus 实际加载路径不是应用内置组件")
    except Exception as exc:
        raise OpusUnavailableError(
            _runtime_unavailable_message() + f"（{exc}）"
        ) from exc
    finally:
        ctypes.util.find_library = original_find_library
    return opuslib


def _bundled_macos_opus() -> Path | None:
    if sys.platform != "darwin":
        return None
    frozen = bool(getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"))
    if frozen:
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent.parent / "Frameworks")).resolve()
        directories = [root / "opus"]
    else:
        configured = str(os.environ.get("PROXIMIC_OPUS_DIR", "")).strip()
        project = Path(__file__).resolve().parents[3]
        directories = ([Path(configured).expanduser()] if configured else []) + [
            project / ".runtime" / "opus" / "lib", project / "opus"]
    for directory in directories:
        for filename in ("libopus.0.dylib", "libopus.dylib"):
            candidate = directory / filename
            if candidate.is_file():
                resolved = candidate.resolve()
                if frozen and not resolved.is_relative_to(root):
                    raise OpusUnavailableError(_runtime_unavailable_message())
                return resolved
    if frozen:
        # Never let a builder's Homebrew install hide a broken application.
        raise OpusUnavailableError(_runtime_unavailable_message())
    return None


def _prepare_windows_opus_runtime() -> None:
    """Make the project-local libopus visible to opuslib on Windows."""
    if os.name != "nt":
        return

    candidates: list[Path] = []
    configured = str(os.environ.get("PROXIMIC_OPUS_DIR", "")).strip()
    if configured:
        candidates.append(Path(configured).expanduser())

    # Editable/source installation: <project>/src/ring_python_sdk/audio/...
    source_file = Path(__file__).resolve()
    if len(source_file.parents) > 3:
        candidates.append(source_file.parents[3] / ".runtime" / "opus")
    # Conda environments commonly install libopus under Library/bin.
    candidates.append(Path(sys.prefix) / "Library" / "bin")

    for directory in candidates:
        resolved = directory.resolve()
        if not any(
            (resolved / filename).is_file()
            for filename in ("opus.dll", "libopus-0.dll")
        ):
            continue
        current_path = os.environ.get("PATH", "")
        path_entries = current_path.split(os.pathsep) if current_path else []
        if str(resolved).casefold() not in {
            entry.casefold() for entry in path_entries
        }:
            os.environ["PATH"] = str(resolved) + os.pathsep + current_path
        add_directory = getattr(os, "add_dll_directory", None)
        if callable(add_directory):
            try:
                _WINDOWS_DLL_HANDLES.append(add_directory(str(resolved)))
            except OSError:
                pass
        return


def _parse_block(payload: bytes) -> list[bytes]:
    if len(payload) < 3:
        raise OpusCodecError("Opus block header is truncated")

    sample_count, frame_count = struct.unpack_from("<HB", payload, 0)
    if sample_count != OPUS_BLOCK_SAMPLES:
        raise OpusCodecError(
            f"invalid Opus sample_count={sample_count}, expected {OPUS_BLOCK_SAMPLES}"
        )
    if frame_count != OPUS_FRAMES_PER_BLOCK:
        raise OpusCodecError(
            f"invalid Opus frame_count={frame_count}, expected {OPUS_FRAMES_PER_BLOCK}"
        )

    frames: list[bytes] = []
    offset = 3
    for frame_index in range(frame_count):
        if offset + 2 > len(payload):
            raise OpusCodecError(f"Opus frame {frame_index} length is truncated")
        (frame_len,) = struct.unpack_from("<H", payload, offset)
        offset += 2
        if frame_len == 0 or frame_len > OPUS_MAX_FRAME_BYTES:
            raise OpusCodecError(
                f"invalid Opus frame {frame_index} length={frame_len}"
            )
        if offset + frame_len > len(payload):
            raise OpusCodecError(f"Opus frame {frame_index} data is truncated")
        frames.append(payload[offset : offset + frame_len])
        offset += frame_len

    if offset != len(payload):
        raise OpusCodecError(
            f"Opus block has {len(payload) - offset} trailing byte(s)"
        )
    return frames


class OpusBlockDecoder:
    """Own one native decoder for one MIC recording."""

    def __init__(self) -> None:
        self._opuslib = _load_opuslib()
        self._decoder = self._create_decoder()

    def _create_decoder(self):
        try:
            return self._opuslib.Decoder(DEFAULT_SAMPLE_RATE, 1)
        except (OSError, RuntimeError) as exc:
            raise OpusUnavailableError(
                _runtime_unavailable_message() + f"（{exc}）"
            ) from exc

    def reset(self) -> None:
        self._decoder = self._create_decoder()

    def decode_block(self, payload: bytes) -> bytes:
        try:
            frames = _parse_block(payload)
        except OpusCodecError:
            self.reset()
            raise
        pcm = bytearray()
        expected_frame_bytes = OPUS_FRAME_SAMPLES * PCM_SAMPLE_BYTES

        try:
            for frame_index, frame in enumerate(frames):
                decoded = bytes(
                    self._decoder.decode(
                        frame, OPUS_FRAME_SAMPLES, decode_fec=False
                    )
                )
                if len(decoded) != expected_frame_bytes:
                    raise OpusCodecError(
                        f"Opus frame {frame_index} decoded {len(decoded) // 2} "
                        f"samples, expected {OPUS_FRAME_SAMPLES}"
                    )
                pcm.extend(decoded)
        except OpusCodecError:
            self.reset()
            raise
        except Exception as exc:
            self.reset()
            raise OpusCodecError(f"Opus decode failed: {exc}") from exc

        expected_block_bytes = OPUS_BLOCK_SAMPLES * PCM_SAMPLE_BYTES
        if len(pcm) != expected_block_bytes:
            self.reset()
            raise OpusCodecError(
                f"Opus block decoded {len(pcm)} bytes, "
                f"expected {expected_block_bytes}"
            )
        return bytes(pcm)


@dataclass(frozen=True)
class DecodedOpusBlock:
    frame_seq: int
    pcm: bytes | None
    error: OpusCodecError | None = None


class OrderedOpusDecoder:
    """Buffer Opus blocks and mutate decoder state strictly by block sequence."""

    def __init__(self, *, eager: bool = False) -> None:
        self._decoder = OpusBlockDecoder() if eager else None
        self._pending: dict[int, bytes] = {}
        self._known_dropped: set[int] = set()
        self._seen: set[int] = set()
        self._next_seq = 0
        self._started = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _ensure_decoder(self) -> OpusBlockDecoder:
        if self._decoder is None:
            self._decoder = OpusBlockDecoder()
        return self._decoder

    def push(self, frame_seq: int, payload: bytes) -> list[DecodedOpusBlock]:
        frame_seq &= 0xFFFF
        if frame_seq in self._seen or frame_seq in self._pending:
            return []

        if not self._started and not self._pending and not self._known_dropped:
            # A capture may attach after the firmware sequence has already
            # advanced.  Decode from the first block actually observed instead
            # of waiting forever for sequence zero.
            self._next_seq = frame_seq
        else:
            forward_gap = (frame_seq - self._next_seq) & 0xFFFF
            if forward_gap >= 0x8000:
                # A delayed/duplicate block behind the decoder cursor cannot be
                # applied without corrupting Opus state.
                return []
            # BLE notifications are ordered.  Once a later complete Opus block
            # arrives, any skipped block has permanently lost at least one
            # fragment.  Mark the gap so one packet loss cannot stall all later
            # audio for the rest of the session (seen most often on macOS).
            for offset in range(forward_gap):
                missing = (self._next_seq + offset) & 0xFFFF
                if missing not in self._pending:
                    self._known_dropped.add(missing)
        self._pending[frame_seq] = payload
        return self._drain()

    def _drain(self) -> list[DecodedOpusBlock]:
        decoded: list[DecodedOpusBlock] = []
        while (
            self._next_seq in self._pending
            or self._next_seq in self._known_dropped
        ):
            frame_seq = self._next_seq
            self._seen.add(frame_seq)
            self._started = True
            if frame_seq in self._known_dropped:
                self._known_dropped.remove(frame_seq)
                if self._decoder is not None:
                    self._decoder.reset()
                decoded.append(
                    DecodedOpusBlock(
                        frame_seq,
                        None,
                        OpusCodecError(f"Opus block {frame_seq} is incomplete"),
                    )
                )
            else:
                payload = self._pending.pop(frame_seq)
                try:
                    pcm = self._ensure_decoder().decode_block(payload)
                    decoded.append(DecodedOpusBlock(frame_seq, pcm))
                except OpusCodecError as exc:
                    decoded.append(DecodedOpusBlock(frame_seq, None, exc))
            self._next_seq = (self._next_seq + 1) & 0xFFFF
        return decoded

    def finish(
        self, incomplete_seqs: Iterable[int] = ()
    ) -> tuple[list[DecodedOpusBlock], int]:
        """Decode every complete block in order and report gaps as dropped.

        Missing blocks reset decoder state and never create placeholder PCM.
        A capture attached mid-stream starts at its lowest observed sequence.
        """
        for frame_seq in incomplete_seqs:
            frame_seq &= 0xFFFF
            if frame_seq not in self._seen and frame_seq not in self._pending:
                self._known_dropped.add(frame_seq)

        observed = self._pending.keys() | self._known_dropped
        if not self._started and observed and self._next_seq not in observed:
            self._next_seq = min(observed)

        decoded: list[DecodedOpusBlock] = []
        while self._pending or self._known_dropped:
            decoded.extend(self._drain())
            if not self._pending and not self._known_dropped:
                break

            observed = self._pending.keys() | self._known_dropped
            next_observed = min(
                observed,
                key=lambda seq: (seq - self._next_seq) & 0xFFFF,
            )
            gap = (next_observed - self._next_seq) & 0xFFFF
            for offset in range(gap):
                self._known_dropped.add((self._next_seq + offset) & 0xFFFF)

        return decoded, 0


def decode_opus_block(payload: bytes) -> bytes:
    """Decode one standalone block with a fresh decoder (primarily for tests)."""

    return OpusBlockDecoder().decode_block(payload)
