from __future__ import annotations

import asyncio
import ctypes
import math
import queue
import sys
import threading
import time
from typing import Callable
from pathlib import Path

import numpy as np

from ..pcm import decode_pcm16le
from .base import AudioSource

_STOP = object()

# Stream-monitor defaults.  These are intentionally kept inside ring.py so the
# existing CLI does not need to change.
_WATCHDOG_POLL_S = 0.25
_WATCHDOG_STALL_S = 5.0
_WATCHDOG_CONFIRM_S = 1.0
_INITIAL_PCM_TIMEOUT_S = 3.0
_FRESH_PCM_TIMEOUT_S = 3.0
_INITIAL_MIC_SETTLE_S = 1.0
_EARLY_STARTUP_STALL_S = 2.0
_EARLY_STARTUP_MAX_CALLBACKS = 5
_MIC_RECOVERY_PAUSE_S = 0.75
# The Ring MIC control command has no acknowledgement.  A warm model cache can
# otherwise make MIC OFF and the following MIC ON effectively back-to-back,
# before the firmware has finished closing the previous capture.
_MIC_RESTART_PAUSE_S = 0.25
_DEFAULT_IMU_HZ = 50
_DEFAULT_IMU_FRAMES_PER_PACKET = 10


class RingAudioSource(AudioSource):
    """Real-time Ringo microphone source backed by ``ring-python-sdk``.

    The SDK handles BLE NUS, MIC packet reassembly, and codec decoding.  Its
    ``on_pcm`` callback exposes decoded 16 kHz / mono / PCM16LE bytes.  This
    adapter converts those bytes to the float32 waveform expected by
    :class:`ProxiMicDetector` and presents the normal blocking ``AudioSource``
    interface to ``runner.py``.

    The SDK also writes the same decoded stream to a WAV file under ``data_root``.
    ProxiMic does *not* read that WAV back; inference uses the callback directly.

    BLE connection and microphone streaming are separate phases.  The customer
    UI connects and validates the selected device before loading detector/ASR
    models, then begins forwarding live audio when those models are ready.  A
    short startup stall gets one controlled MIC restart; a later PCM stall is
    reported without deliberately tearing down a healthy BLE link.  A physical
    disconnect still requires an explicit user reconnect.
    """

    sample_rate: int = 16_000

    def __init__(
        self,
        *,
        name_keyword: str = "Ringo",
        selector: str | None = None,
        device: object | None = None,
        timeout_s: float = 8.0,
        encoding: str = "opus",
        data_root: str | Path = "data",
        queue_blocks: int = 256,
        imu_observer: Callable[[dict], None] | None = None,
        imu_hz: int = _DEFAULT_IMU_HZ,
    ) -> None:
        encoding = encoding.lower()
        if encoding not in {"pcm", "adpcm", "opus"}:
            raise ValueError("encoding must be one of: pcm, adpcm, opus")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if queue_blocks <= 0:
            raise ValueError("queue_blocks must be > 0")
        if imu_hz <= 0:
            raise ValueError("imu_hz must be > 0")

        self.name_keyword = name_keyword
        self.selector = selector
        self.device = device
        self.timeout_s = float(timeout_s)
        self.encoding = encoding
        self.data_root = Path(data_root)
        self.imu_observer = imu_observer
        self.imu_hz = int(imu_hz)

        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_blocks)
        self._pending = np.empty(0, dtype=np.float32)
        self._stop = threading.Event()
        self._connected_ready = threading.Event()
        self._start_stream = threading.Event()
        self._buffer_audio = threading.Event()
        self._buffer_audio.set()
        self._watchdog_armed = threading.Event()
        self._fresh_pcm = threading.Event()
        self._pause_stream_requested = threading.Event()
        self._stream_paused = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

        self.capture_path: Path | None = None
        self.pcm_callbacks: int = 0
        self.samples_received: int = 0
        self._last_pcm_monotonic: float | None = None
        self._first_pcm_monotonic: float | None = None
        self._last_frame_seq: int | None = None
        self._sample_square_sum: float = 0.0
        self._near_zero_samples: int = 0
        self._clipped_samples: int = 0
        self._pcm_abs_peak: float = 0.0
        self.imu_samples_received: int = 0
        self.imu_error: BaseException | None = None

    def open(self) -> None:
        self.connect()
        self.start_stream(buffer_audio=True)

    def connect(self) -> None:
        """Connect and validate BLE/NUS without starting microphone audio."""
        if self._thread is not None:
            raise RuntimeError("RingAudioSource is already open")

        self._clear_queue()
        self._pending = np.empty(0, dtype=np.float32)
        self._error = None
        self.capture_path = None
        self.pcm_callbacks = 0
        self.samples_received = 0
        self._last_pcm_monotonic = None
        self._first_pcm_monotonic = None
        self._last_frame_seq = None
        self._sample_square_sum = 0.0
        self._near_zero_samples = 0
        self._clipped_samples = 0
        self._pcm_abs_peak = 0.0
        self.imu_samples_received = 0
        self.imu_error = None
        self._stop.clear()
        self._connected_ready.clear()
        self._start_stream.clear()
        self._buffer_audio.clear()
        self._watchdog_armed.clear()
        self._fresh_pcm.clear()
        self._pause_stream_requested.clear()
        self._stream_paused.clear()
        self._ready.clear()

        self._thread = threading.Thread(
            target=self._thread_main,
            name="RingAudioSource",
            daemon=True,
        )
        self._thread.start()

        # Scan + connection may each consume the configured BLE timeout.
        wait_s = max(15.0, self.timeout_s * 2.0 + 5.0)
        if not self._connected_ready.wait(timeout=wait_s):
            self.close()
            raise TimeoutError(
                f"Timed out connecting to the selected Ring after {wait_s:.1f}s"
            )
        if self._error is not None:
            err = self._error
            self.close()
            raise RuntimeError(f"Failed to connect to the selected Ring: {err}") from err

    def start_stream(self, *, buffer_audio: bool = True) -> None:
        """Start microphone audio and require the first usable PCM block.

        ``buffer_audio=False`` validates the physical stream without filling the
        inference queue while models are still loading.
        """
        if self._thread is None:
            self.connect()
        if buffer_audio:
            self._buffer_audio.set()
        else:
            self._buffer_audio.clear()
        self._start_stream.set()
        wait_s = max(6.0, _INITIAL_PCM_TIMEOUT_S + 2.0)
        if not self._ready.wait(timeout=wait_s):
            self.close()
            raise TimeoutError(
                f"Timed out waiting for Ring microphone audio after {wait_s:.1f}s"
            )
        if self._error is not None:
            err = self._error
            self.close()
            raise RuntimeError(f"Failed to start Ring microphone audio: {err}") from err
        if buffer_audio:
            self._watchdog_armed.set()

    def begin_buffering(self) -> None:
        """Resume if paused, require fresh PCM, then arm the watchdog."""
        if self._error is not None:
            raise RuntimeError(f"Ring audio stream failed: {self._error}") from self._error
        self._clear_queue()
        self._pending = np.empty(0, dtype=np.float32)
        if self._stream_paused.is_set():
            self._buffer_audio.set()
            self._ready.clear()
            self._pause_stream_requested.clear()
            wait_s = max(6.0, _INITIAL_PCM_TIMEOUT_S + 2.0)
            if not self._ready.wait(wait_s):
                raise RuntimeError(
                    "Ring microphone did not restart after model loading; "
                    "the device will be disconnected"
                )
            if self._error is not None:
                raise RuntimeError(
                    f"Ring audio stream failed: {self._error}"
                ) from self._error
            self._watchdog_armed.set()
            return

        baseline_callbacks = self.pcm_callbacks
        self._buffer_audio.set()
        deadline = time.monotonic() + _FRESH_PCM_TIMEOUT_S
        while self.pcm_callbacks <= baseline_callbacks:
            self._fresh_pcm.clear()
            if self.pcm_callbacks > baseline_callbacks:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._fresh_pcm.wait(remaining):
                raise RuntimeError(
                    "Ring microphone audio did not resume after model loading; "
                    "the device will be disconnected"
                )
            if self._error is not None:
                raise RuntimeError(
                    f"Ring audio stream failed: {self._error}"
                ) from self._error
        self._watchdog_armed.set()

    def pause_stream(self) -> None:
        """Stop MIC traffic while keeping BLE/NUS connected."""
        if self._thread is None:
            return
        self._watchdog_armed.clear()
        self._buffer_audio.clear()
        self._pause_stream_requested.set()
        wait_s = max(5.0, self.timeout_s + 1.0)
        if not self._stream_paused.wait(wait_s):
            if self._error is not None:
                raise RuntimeError(
                    f"Failed to pause Ring microphone: {self._error}"
                ) from self._error
            raise TimeoutError("Timed out pausing Ring microphone before model loading")
        if self._stop.is_set():
            return
        if self._error is not None:
            raise RuntimeError(
                f"Failed to pause Ring microphone: {self._error}"
            ) from self._error

    @property
    def error(self) -> BaseException | None:
        return self._error

    def close(self) -> None:
        self._stop.set()
        self._start_stream.set()
        self._pause_stream_requested.clear()
        self._stream_paused.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(5.0, self.timeout_s + 2.0))
        self._thread = None

    def read(self, frames: int) -> np.ndarray | None:
        if frames <= 0:
            raise ValueError("frames must be > 0")

        while self._pending.size < frames:
            if self._error is not None and self._queue.empty():
                raise RuntimeError(f"Ringo audio stream failed: {self._error}") from self._error

            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                thread = self._thread
                if thread is not None and not thread.is_alive():
                    if self._error is not None:
                        raise RuntimeError(
                            f"Ringo audio stream failed: {self._error}"
                        ) from self._error
                    if self._pending.size == 0:
                        return None
                    break
                continue

            if item is _STOP:
                if self._error is not None:
                    raise RuntimeError(
                        f"Ringo audio stream failed: {self._error}"
                    ) from self._error
                if self._pending.size == 0:
                    return None
                break

            if isinstance(item, BaseException):
                raise RuntimeError(f"Ringo audio stream failed: {item}") from item

            block = np.asarray(item, dtype=np.float32).reshape(-1)
            if block.size:
                self._pending = np.concatenate((self._pending, block))

        if self._pending.size == 0:
            return None

        take = min(frames, int(self._pending.size))
        out = self._pending[:take].copy()
        self._pending = self._pending[take:]
        return out

    def _on_pcm(self, frame_seq: int, pcm: bytes) -> None:
        """SDK real-time callback: decoded mono PCM16LE at 16 kHz."""
        try:
            block = decode_pcm16le(pcm)
        except BaseException as exc:
            self._signal_error(exc)
            return

        # A successful BLE connection and MIC ON acknowledgement do not prove
        # that the Ring audio path is usable.  Some firmware/SDK failures leave
        # Bleak reporting ``is_connected`` while no microphone samples arrive.
        # Only a real, non-empty PCM block may mark this source as ready.
        if block.size == 0:
            return

        now = time.monotonic()
        self.pcm_callbacks += 1
        self.samples_received += int(block.size)
        self._last_pcm_monotonic = now
        if self._first_pcm_monotonic is None:
            self._first_pcm_monotonic = now
        self._last_frame_seq = int(frame_seq) & 0xFFFF
        absolute = np.abs(block)
        self._sample_square_sum += float(np.dot(block, block))
        self._near_zero_samples += int(np.count_nonzero(absolute < (64.0 / 32768.0)))
        self._clipped_samples += int(np.count_nonzero(absolute >= (32760.0 / 32768.0)))
        self._pcm_abs_peak = max(self._pcm_abs_peak, float(np.max(absolute)))
        self._fresh_pcm.set()
        if self._buffer_audio.is_set():
            try:
                self._queue.put_nowait(block)
            except queue.Full:
                self._signal_error(
                    RuntimeError(
                        "Ringo PCM queue overflow: inference is not consuming audio fast enough"
                    )
                )
                return

        # The streaming phase is ready only after a usable audio frame reaches
        # the pipeline.  BLE connection readiness is tracked separately.
        self._ready.set()

    def _on_imu_sample(self, sample: object) -> None:
        """Forward one normalized IMU row without coupling it to audio health."""
        observer = self.imu_observer
        if observer is None or self.imu_error is not None:
            return
        try:
            accel = tuple(getattr(sample, "accel_ms2"))
            gyro = tuple(getattr(sample, "gyro_dps"))
            raw = getattr(sample, "raw", None)
            row = {
                "host_monotonic_ns": time.monotonic_ns(),
                "device_uptime_ms": float(getattr(sample, "uptime_ms")),
                "sample_index": int(getattr(sample, "sample_index")),
                "packet_seq": int(getattr(sample, "packet_seq")),
                "accel_ms2": [float(value) for value in accel],
                "gyro_dps": [float(value) for value in gyro],
                "raw": [int(value) for value in raw] if raw is not None else None,
            }
            observer(row)
            self.imu_samples_received += 1
        except BaseException as exc:
            # Dataset collection is deliberately a side channel.  A malformed
            # sample or consumer failure must never stop microphone delivery.
            self.imu_error = exc
            print(f"Ring IMU collection disabled for this connection: {exc}")

    def diagnostic_summary(self) -> str:
        """Return lightweight stream/audio evidence suitable for persistent logs."""

        samples = self.samples_received
        duration_s = samples / self.sample_rate
        rms = math.sqrt(self._sample_square_sum / samples) if samples else 0.0
        rms_dbfs = 20.0 * math.log10(max(rms, 1e-12))
        peak_dbfs = 20.0 * math.log10(max(self._pcm_abs_peak, 1e-12))
        near_zero_pct = 100.0 * self._near_zero_samples / samples if samples else 0.0
        clipped_pct = 100.0 * self._clipped_samples / samples if samples else 0.0
        last_pcm_age_s = (
            time.monotonic() - self._last_pcm_monotonic
            if self._last_pcm_monotonic is not None
            else -1.0
        )
        pcm_span_s = (
            self._last_pcm_monotonic - self._first_pcm_monotonic
            if self._last_pcm_monotonic is not None
            and self._first_pcm_monotonic is not None
            else 0.0
        )
        return (
            f"encoding={self.encoding}, callbacks={self.pcm_callbacks}, "
            f"samples={samples}, audio={duration_s:.3f}s, "
            f"pcm_span={pcm_span_s:.3f}s, "
            f"last_frame_seq={self._last_frame_seq}, "
            f"last_pcm_age={last_pcm_age_s:.3f}s, "
            f"rms={rms_dbfs:.1f}dBFS, peak={peak_dbfs:.1f}dBFS, "
            f"near_zero={near_zero_pct:.2f}%, clipped={clipped_pct:.3f}%, "
            f"capture={self.capture_path or 'unavailable'}"
        )

    def _signal_error(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = exc
        self._stop.set()
        self._connected_ready.set()
        self._fresh_pcm.set()
        self._stream_paused.set()
        try:
            self._queue.put_nowait(exc)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(exc)
            except queue.Full:
                pass
        self._ready.set()

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _thread_main(self) -> None:
        com_initialized = False
        try:
            if sys.platform == "win32":
                # The firmware receiver runs Bleak on its main asyncio thread,
                # which WinRT initializes as MTA.  The desktop app runs BLE on
                # a dedicated worker, so initialize that thread explicitly as
                # MTA before creating any scanner/client/notification objects.
                ole32 = ctypes.windll.ole32
                ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                ole32.CoInitializeEx.restype = ctypes.c_long
                result = int(ole32.CoInitializeEx(None, 0))
                if result not in {0, 1}:  # S_OK / S_FALSE
                    raise OSError(result, "CoInitializeEx(COINIT_MULTITHREADED) failed")
                com_initialized = True
            asyncio.run(self._run_async())
        except BaseException as exc:
            self._signal_error(exc)
        finally:
            if com_initialized:
                ctypes.windll.ole32.CoUninitialize()
            self._connected_ready.set()
            self._ready.set()
            try:
                self._queue.put_nowait(_STOP)
            except queue.Full:
                pass

    async def _wait_for_new_pcm(self, baseline_callbacks: int, timeout_s: float) -> bool:
        """Wait until at least one PCM callback newer than ``baseline_callbacks`` arrives."""
        deadline = time.monotonic() + timeout_s
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self.pcm_callbacks > baseline_callbacks:
                return True
            await asyncio.sleep(0.05)
        return self.pcm_callbacks > baseline_callbacks

    async def _start_mic_and_wait(self, session, *, timeout_s: float) -> bool:
        """Start the SDK microphone and require an actual PCM callback."""
        baseline_callbacks = self.pcm_callbacks
        await session.mic_on(self.encoding, on_pcm=self._on_pcm)
        if not session.mic_active or session.mic is None:
            return False

        self.capture_path = Path(session.mic.output_path)
        return await self._wait_for_new_pcm(baseline_callbacks, timeout_s)

    async def _print_battery_status(self, session, *, timeout_s: float = 1.5) -> None:
        """Query and print Ring battery after a BLE connection is established."""
        try:
            from ring_python_sdk.core.battery_status import format_battery

            # ConnectionMixin already starts an immediate battery poll, but its
            # reply is routed to the SDK live-log queue rather than stdout.
            # Query once more here and wait briefly for the notify reply so the
            # command-line Ring source always shows battery status on connect.
            await session.query_battery()
            deadline = time.monotonic() + timeout_s
            while (
                not self._stop.is_set()
                and session.battery_pct is None
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.05)

            if session.battery_pct is None or session.battery_mv is None:
                print("Ring battery: unavailable (no BATTERY STATUS reply)")
                return

            text = format_battery(
                battery_pct=session.battery_pct,
                battery_mv=session.battery_mv,
                charge_status=session.charge_status,
            )
            print(f"Ring battery: {text}")
        except Exception as exc:
            print(f"Ring battery query failed: {exc}")

    @staticmethod
    def _sdk_mic_diagnostics(session) -> str:
        """Return low-level SDK MIC counters for stall diagnosis.

        ``on_pcm`` only fires after a full MIC block has been reassembled and
        decoded.  These counters let us distinguish a true BLE/firmware stop
        from a case where MIC fragments still arrive but no complete block can
        be assembled.
        """
        mic = getattr(session, "mic", None)
        if mic is None:
            return "sdk_mic=none"

        stats = getattr(mic, "stats", None)
        packet_count = getattr(stats, "packet_count", -1)
        frame_count = getattr(stats, "frame_count", -1)
        dropped_packets = getattr(stats, "dropped_packet_count", -1)
        dropped_frames = getattr(stats, "dropped_frame_count", -1)

        assembler = getattr(mic, "_assembler", None)
        incomplete = getattr(assembler, "incomplete_notify_packets", -1)
        inflight = getattr(assembler, "inflight_frame_count", -1)
        assembled_frames = getattr(assembler, "completed_frames", -1)
        repeated_seq = getattr(assembler, "repeated_completed_seq_packets", -1)
        last_seq = getattr(assembler, "last_frame_seq", None)
        last_frag_idx = getattr(assembler, "last_frag_idx", None)
        last_frag_count = getattr(assembler, "last_frag_count", None)

        return (
            f"sdk_packets={packet_count}, decoded_blocks={frame_count}, "
            f"incomplete_notifies={incomplete}, inflight_frames={inflight}, "
            f"assembled_frames={assembled_frames}, "
            f"repeated_completed_seq_packets={repeated_seq}, "
            f"last_seq={last_seq}, last_frag={last_frag_idx}/{last_frag_count}, "
            f"dropped_packets={dropped_packets}, dropped_frames={dropped_frames}"
        )

    @staticmethod
    def _is_expected_windows_cancel(exc: BaseException) -> bool:
        """Return whether Bleak/WinRT cancelled pending I/O during teardown."""
        current: BaseException | None = exc
        while current is not None:
            if getattr(current, "winerror", None) in {
                995,
                1223,
                -2147023901,  # HRESULT_FROM_WIN32(ERROR_OPERATION_ABORTED)
                -2147023673,  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
            }:
                return True
            text = str(current).casefold()
            if (
                "i/o operation has been aborted" in text
                or "operation was canceled by the user" in text
                or "operation was cancelled by the user" in text
                or "由于线程退出或应用程序请求" in text
                or "操作已被用户取消" in text
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    async def _shutdown_session(self, session) -> None:
        """Best-effort BLE shutdown that never hides the stream failure."""
        if bool(getattr(session, "imu_active", False)):
            try:
                await session.imu_off()
            except Exception as exc:
                print(f"Ring cleanup: IMU OFF failed during shutdown: {exc}")
        if session.mic_active:
            try:
                await session.mic_off()
            except Exception as exc:
                label = (
                    "Windows cancelled pending BLE I/O during shutdown (expected)"
                    if self._is_expected_windows_cancel(exc)
                    else "MIC OFF failed during shutdown"
                )
                print(f"Ring cleanup: {label}: {exc}")
        try:
            await session.disconnect()
        except Exception as exc:
            label = (
                "Windows cancelled pending BLE I/O during disconnect (expected)"
                if self._is_expected_windows_cancel(exc)
                else "disconnect failed"
            )
            print(f"Ring cleanup: {label}: {exc}")

    async def _run_async(self) -> None:
        try:
            from ring_python_sdk import RingSession
            from ring_python_sdk.core.constants import (
                DEFAULT_CHANNELS,
                DEFAULT_SAMPLE_RATE,
                DEFAULT_SAMPLE_WIDTH_BYTES,
            )
        except ImportError as exc:
            raise RuntimeError(
                'Ringo support requires the bundled SDK dependencies. '
                'Install with: pip install -e ".[ring]"'
            ) from exc

        # Fail loudly if a future SDK changes the audio contract assumed by ProxiMic.
        contract = (
            int(DEFAULT_SAMPLE_RATE),
            int(DEFAULT_CHANNELS),
            int(DEFAULT_SAMPLE_WIDTH_BYTES),
        )
        if contract != (16_000, 1, 2):
            raise RuntimeError(
                "Unexpected ring SDK audio format: "
                f"sample_rate={contract[0]}, channels={contract[1]}, "
                f"sample_width={contract[2]} bytes; expected 16000/mono/PCM16"
            )

        session = RingSession(
            name_keyword=self.name_keyword,
            timeout_s=self.timeout_s,
            data_root=self.data_root,
            auto_reconnect=False,
            battery_poll_enabled=False,
        )

        try:
            if self.device is not None:
                connected = await session.connect_device(self.device)
            elif self.selector:
                print(
                    "Resolving selected Ring inside the BLE runtime loop: "
                    f"{self.selector}"
                )
                connected = await session.connect_target(self.selector)
            else:
                connected = await session.connect()
            if not connected:
                target = self.selector or self.name_keyword
                raise RuntimeError(f"Ringo device {target!r} not found or connection failed")
            connected_at = time.monotonic()

            # BLE and the required NUS service are now validated.  Release the
            # connection phase before doing battery queries or starting audio.
            self._connected_ready.set()
            await self._print_battery_status(session)

            while not self._stop.is_set() and not self._start_stream.is_set():
                client = getattr(session, "client", None)
                if client is not None and not bool(getattr(client, "is_connected", False)):
                    raise RuntimeError("Ring BLE connection was lost before audio started")
                await asyncio.sleep(0.05)

            if self._stop.is_set():
                return

            # The receiver is normally operated with a short human pause
            # between CONNECT and MIC ON.  Preserve at least a small quiet
            # window after NUS setup/control queries, especially when the ASR
            # backend is already cached and model startup finishes instantly.
            settle_remaining = _INITIAL_MIC_SETTLE_S - (
                time.monotonic() - connected_at
            )
            if settle_remaining > 0:
                print(
                    "Waiting briefly for the Ring link to settle before MIC ON "
                    f"({settle_remaining:.2f}s) ..."
                )
                await asyncio.sleep(settle_remaining)

            started = await self._start_mic_and_wait(
                session,
                timeout_s=_INITIAL_PCM_TIMEOUT_S,
            )
            if not started:
                hint = ""
                if self.encoding == "opus":
                    hint = (
                        " Opus mode additionally needs opuslib and a native libopus runtime; "
                        "try --encoding pcm to avoid Opus during initial testing."
                    )
                raise RuntimeError(
                    "Ring connected, but no microphone audio was received. "
                    f"The device was disconnected; reconnect manually.{hint}"
                )

            await self._start_imu_best_effort(session)

            # Start the stall timer from MIC ON.  If the Ring never sends the
            # first callback, that is treated exactly like a stream stall.
            last_seen_callbacks = self.pcm_callbacks
            last_progress_at = time.monotonic()
            stall_reported = False
            startup_recovery_attempted = False
            while not self._stop.is_set():
                await asyncio.sleep(_WATCHDOG_POLL_S)

                client = getattr(session, "client", None)
                if client is not None and not bool(getattr(client, "is_connected", False)):
                    stream_diag = self.diagnostic_summary()
                    sdk_diag = str(
                        getattr(session, "last_disconnect_diagnostics", "")
                    ).strip() or self._sdk_mic_diagnostics(session)
                    print("[DISCONNECT] STREAM DIAG: " + stream_diag)
                    print("[DISCONNECT] SDK DIAG: " + sdk_diag)
                    raise RuntimeError(
                        "Ring BLE connection was physically lost\n"
                        f"[DIAG] {stream_diag}\n"
                        f"[DIAG] {sdk_diag}"
                    )

                if self._pause_stream_requested.is_set():
                    self._watchdog_armed.clear()
                    if session.mic_active:
                        await session.mic_off()
                    mic_stopped_at = time.monotonic()
                    self._stream_paused.set()
                    while (
                        not self._stop.is_set()
                        and self._pause_stream_requested.is_set()
                    ):
                        await asyncio.sleep(0.05)
                    if self._stop.is_set():
                        return

                    remaining_pause = _MIC_RESTART_PAUSE_S - (
                        time.monotonic() - mic_stopped_at
                    )
                    if remaining_pause > 0:
                        await asyncio.sleep(remaining_pause)
                    self._stream_paused.clear()
                    started = await self._start_mic_and_wait(
                        session,
                        timeout_s=_INITIAL_PCM_TIMEOUT_S,
                    )
                    if not started:
                        raise RuntimeError(
                            "Ring microphone did not restart after model loading"
                        )
                    last_seen_callbacks = self.pcm_callbacks
                    last_progress_at = time.monotonic()
                    continue

                callbacks_now = self.pcm_callbacks
                now = time.monotonic()

                # Model construction/import can temporarily monopolize Python
                # execution even though BLE runs on its own thread.  The UI
                # deliberately leaves the watchdog disarmed until models are
                # ready and a fresh post-load PCM callback has been observed.
                if not self._watchdog_armed.is_set():
                    last_seen_callbacks = callbacks_now
                    last_progress_at = now
                    continue

                if callbacks_now > last_seen_callbacks:
                    if stall_reported:
                        print(
                            "[STREAM] PCM callbacks resumed after the temporary stall; "
                            "keeping the existing BLE session"
                        )
                        stall_reported = False
                    last_seen_callbacks = callbacks_now
                    last_progress_at = now
                    continue

                if stall_reported:
                    # Match the proven firmware receiver behavior: a decoded
                    # PCM gap is observable, but it is not a reason to tear down
                    # a still-connected Windows BLE session.  read() remains
                    # blocked until audio resumes or the user disconnects.
                    continue

                stalled_for = now - last_progress_at

                if (
                    not startup_recovery_attempted
                    and self.pcm_callbacks <= _EARLY_STARTUP_MAX_CALLBACKS
                    and stalled_for >= _EARLY_STARTUP_STALL_S
                ):
                    startup_recovery_attempted = True
                    print(
                        "[STREAM] Audio stopped during MIC startup after "
                        f"{self.pcm_callbacks} callback(s); restarting MIC once "
                        "before the firmware drops BLE"
                    )
                    if session.mic_active:
                        await session.mic_off()
                    await asyncio.sleep(_MIC_RECOVERY_PAUSE_S)
                    recovered = await self._start_mic_and_wait(
                        session,
                        timeout_s=_INITIAL_PCM_TIMEOUT_S,
                    )
                    if recovered:
                        last_seen_callbacks = self.pcm_callbacks
                        last_progress_at = time.monotonic()
                        print(
                            "[STREAM] MIC startup recovery succeeded; "
                            "continuing on the existing BLE connection"
                        )
                        continue
                    print(
                        "[STREAM] MIC startup recovery did not produce audio; "
                        "waiting for the BLE state to settle"
                    )

                if stalled_for < _WATCHDOG_STALL_S:
                    continue

                # Heavy local inference can briefly delay both this coroutine
                # and Bleak's notification callbacks.  Yield once so queued or
                # newly-arriving notifications are handled before declaring a
                # physical stream failure.  Outside the one-shot early-startup
                # recovery above, no MIC restart or BLE reconnect is attempted.
                await asyncio.sleep(_WATCHDOG_CONFIRM_S)
                callbacks_confirmed = self.pcm_callbacks
                if callbacks_confirmed > last_seen_callbacks:
                    last_seen_callbacks = callbacks_confirmed
                    last_progress_at = time.monotonic()
                    continue

                now = time.monotonic()
                stalled_for = now - last_progress_at

                print(
                    "[WATCHDOG] PCM STREAM STALLED: "
                    f"no new callback for {stalled_for:.1f}s; "
                    f"callbacks={self.pcm_callbacks}, "
                    f"samples={self.samples_received}."
                )
                print(
                    "[WATCHDOG] SDK MIC DIAG: "
                    + self._sdk_mic_diagnostics(session)
                )

                print(
                    "[STREAM] Keeping the BLE session open and waiting for audio "
                    "to resume; disconnect manually if the device does not recover"
                )
                stall_reported = True
        finally:
            await self._shutdown_session(session)

    async def _start_imu_best_effort(self, session) -> None:
        """Start low-bandwidth IMU capture, but never fail the audio session."""
        if self.imu_observer is None:
            return
        imu_on = getattr(session, "imu_on", None)
        if not callable(imu_on):
            self.imu_error = RuntimeError("Ring SDK does not expose imu_on")
            print(f"Ring IMU unavailable: {self.imu_error}")
            return
        try:
            await imu_on(
                gyro_hz=self.imu_hz,
                accel_hz=self.imu_hz,
                frames_per_packet=_DEFAULT_IMU_FRAMES_PER_PACKET,
                on_sample=self._on_imu_sample,
            )
            print(
                "Ring IMU collection enabled: "
                f"{self.imu_hz} Hz, {_DEFAULT_IMU_FRAMES_PER_PACKET} frames/packet"
            )
        except BaseException as exc:
            self.imu_error = exc
            print(f"Ring IMU unavailable; microphone audio will continue: {exc}")
