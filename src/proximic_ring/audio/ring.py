from __future__ import annotations

import asyncio
import queue
import threading
import time
from pathlib import Path

import numpy as np

from ..pcm import decode_pcm16le
from .base import AudioSource

_STOP = object()

# Watchdog defaults.  These are intentionally kept inside ring.py so the
# existing CLI does not need to change.
_WATCHDOG_POLL_S = 0.25
_WATCHDOG_STALL_S = 2.0
_WATCHDOG_RECOVERY_WAIT_S = 3.0
_WATCHDOG_MAX_CONSECUTIVE_FAILURES = 3
_WATCHDOG_RESTART_PAUSE_S = 0.25
_WATCHDOG_RECONNECT_PAUSE_S = 0.50


class RingAudioSource(AudioSource):
    """Real-time Ringo microphone source backed by ``ring-python-sdk``.

    The SDK handles BLE NUS, MIC packet reassembly, and codec decoding.  Its
    ``on_pcm`` callback exposes decoded 16 kHz / mono / PCM16LE bytes.  This
    adapter converts those bytes to the float32 waveform expected by
    :class:`ProxiMicDetector` and presents the normal blocking ``AudioSource``
    interface to ``runner.py``.

    The SDK also writes the same decoded stream to a WAV file under ``data_root``.
    ProxiMic does *not* read that WAV back; inference uses the callback directly.

    A PCM watchdog is included here because the Ring can occasionally remain BLE
    connected while the microphone stream itself stops producing callbacks.  If
    no new PCM callback arrives for two seconds, the watchdog first restarts the
    microphone.  If that does not recover PCM, it forces a BLE reconnect and
    starts the microphone again.  After three consecutive failed recoveries the
    source stops with an explicit error instead of silently recording an empty
    or 0.2-second WAV forever.
    """

    sample_rate: int = 16_000

    def __init__(
        self,
        *,
        name_keyword: str = "Ringo",
        selector: str | None = None,
        timeout_s: float = 8.0,
        encoding: str = "pcm",
        data_root: str | Path = "data",
        queue_blocks: int = 256,
    ) -> None:
        encoding = encoding.lower()
        if encoding not in {"pcm", "adpcm", "opus"}:
            raise ValueError("encoding must be one of: pcm, adpcm, opus")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if queue_blocks <= 0:
            raise ValueError("queue_blocks must be > 0")

        self.name_keyword = name_keyword
        self.selector = selector
        self.timeout_s = float(timeout_s)
        self.encoding = encoding
        self.data_root = Path(data_root)

        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_blocks)
        self._pending = np.empty(0, dtype=np.float32)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

        self.capture_path: Path | None = None
        self.pcm_callbacks: int = 0
        self.samples_received: int = 0
        self._last_pcm_monotonic: float | None = None

    def open(self) -> None:
        if self._thread is not None:
            raise RuntimeError("RingAudioSource is already open")

        self._clear_queue()
        self._pending = np.empty(0, dtype=np.float32)
        self._error = None
        self.capture_path = None
        self.pcm_callbacks = 0
        self.samples_received = 0
        self._last_pcm_monotonic = None
        self._stop.clear()
        self._ready.clear()

        self._thread = threading.Thread(
            target=self._thread_main,
            name="RingAudioSource",
            daemon=True,
        )
        self._thread.start()

        # Scan + connection may each consume the configured BLE timeout.
        wait_s = max(15.0, self.timeout_s * 2.0 + 5.0)
        if not self._ready.wait(timeout=wait_s):
            self.close()
            raise TimeoutError(
                f"Timed out waiting for Ringo microphone after {wait_s:.1f}s"
            )
        if self._error is not None:
            err = self._error
            self.close()
            raise RuntimeError(f"Failed to start Ringo microphone: {err}") from err

    def close(self) -> None:
        self._stop.set()
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
        del frame_seq  # Sequence tracking/reassembly is handled inside the SDK.
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

        self.pcm_callbacks += 1
        self.samples_received += int(block.size)
        self._last_pcm_monotonic = time.monotonic()
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            self._signal_error(
                RuntimeError(
                    "Ringo PCM queue overflow: inference is not consuming audio fast enough"
                )
            )
            return

        # RingAudioSource.open() and therefore the UI's "connected" state are
        # released only after the first usable audio frame reaches the pipeline.
        self._ready.set()

    def _signal_error(self, exc: BaseException) -> None:
        if self._error is None:
            self._error = exc
        self._stop.set()
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
        try:
            asyncio.run(self._run_async())
        except BaseException as exc:
            self._signal_error(exc)
        finally:
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

    async def _restart_mic(self, session) -> bool:
        """Restart MIC on the current BLE link and require fresh PCM."""
        try:
            if session.mic_active:
                await session.mic_off()
        except Exception as exc:
            print(f"[WATCHDOG] mic_off during recovery failed: {exc}")

        if self._stop.is_set():
            return False

        await asyncio.sleep(_WATCHDOG_RESTART_PAUSE_S)

        # If BLE itself dropped, let the SDK reconnect before restarting MIC.
        try:
            connected = await session.ensure_connected()
        except Exception as exc:
            print(f"[WATCHDOG] ensure_connected failed: {exc}")
            connected = False

        if not connected:
            return False

        try:
            return await self._start_mic_and_wait(
                session,
                timeout_s=_WATCHDOG_RECOVERY_WAIT_S,
            )
        except Exception as exc:
            print(f"[WATCHDOG] mic restart failed: {exc}")
            return False

    async def _force_reconnect_and_restart_mic(self, session) -> bool:
        """Force a fresh BLE connection, then start MIC and require fresh PCM."""
        print("[WATCHDOG] Forcing a fresh BLE reconnect ...")
        try:
            await session.disconnect_link()
        except Exception as exc:
            print(f"[WATCHDOG] disconnect_link failed: {exc}")

        if self._stop.is_set():
            return False

        await asyncio.sleep(_WATCHDOG_RECONNECT_PAUSE_S)

        try:
            if self.selector:
                # Force connect_target() to perform a fresh scan rather than reusing
                # a stale BLEDevice object from the previous connection.
                try:
                    session.scanned = []
                except Exception:
                    pass
                connected = await session.connect_target(self.selector)
            else:
                connected = await session.connect()
        except Exception as exc:
            print(f"[WATCHDOG] BLE reconnect failed: {exc}")
            return False

        if not connected:
            print("[WATCHDOG] BLE reconnect did not connect to a Ring.")
            return False

        await self._print_battery_status(session)

        try:
            return await self._start_mic_and_wait(
                session,
                timeout_s=_WATCHDOG_RECOVERY_WAIT_S,
            )
        except Exception as exc:
            print(f"[WATCHDOG] MIC start after BLE reconnect failed: {exc}")
            return False

    async def _recover_stalled_stream(self, session) -> bool:
        """Try MIC restart first; if that fails, rebuild the BLE link."""
        print(
            "[WATCHDOG] Restarting Ring microphone on the current BLE connection ..."
        )
        if await self._restart_mic(session):
            print(
                "[WATCHDOG] PCM stream recovered after MIC restart "
                f"(callbacks={self.pcm_callbacks}, samples={self.samples_received})."
            )
            return True

        if self._stop.is_set():
            return False

        print(
            f"[WATCHDOG] No PCM within {_WATCHDOG_RECOVERY_WAIT_S:.1f}s after MIC restart."
        )
        if await self._force_reconnect_and_restart_mic(session):
            print(
                "[WATCHDOG] PCM stream recovered after BLE reconnect "
                f"(callbacks={self.pcm_callbacks}, samples={self.samples_received})."
            )
            return True

        return False

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
        completed_waiting = getattr(assembler, "completed_frame_count", -1)

        return (
            f"sdk_packets={packet_count}, decoded_blocks={frame_count}, "
            f"incomplete_notifies={incomplete}, inflight_frames={inflight}, "
            f"completed_waiting={completed_waiting}, "
            f"dropped_packets={dropped_packets}, dropped_frames={dropped_frames}"
        )

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
        )

        try:
            if self.selector:
                connected = await session.connect_target(self.selector)
            else:
                connected = await session.connect()
            if not connected:
                target = self.selector or self.name_keyword
                raise RuntimeError(f"Ringo device {target!r} not found or connection failed")

            await self._print_battery_status(session)

            await session.mic_on(self.encoding, on_pcm=self._on_pcm)
            if not session.mic_active or session.mic is None:
                hint = ""
                if self.encoding == "opus":
                    hint = (
                        " Opus mode additionally needs opuslib and a native libopus runtime; "
                        "try --encoding pcm to avoid Opus during initial testing."
                    )
                raise RuntimeError(f"Ringo microphone did not start.{hint}")

            self.capture_path = Path(session.mic.output_path)

            # Start the stall timer from MIC ON.  If the Ring never sends the
            # first callback, that is treated exactly like a stream stall.
            last_seen_callbacks = self.pcm_callbacks
            last_progress_at = time.monotonic()
            consecutive_failures = 0

            while not self._stop.is_set():
                await asyncio.sleep(_WATCHDOG_POLL_S)

                callbacks_now = self.pcm_callbacks
                now = time.monotonic()

                if callbacks_now > last_seen_callbacks:
                    last_seen_callbacks = callbacks_now
                    last_progress_at = now
                    consecutive_failures = 0
                    continue

                stalled_for = now - last_progress_at
                if stalled_for < _WATCHDOG_STALL_S:
                    continue

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

                recovered = await self._recover_stalled_stream(session)
                if self._stop.is_set():
                    break

                if recovered:
                    last_seen_callbacks = self.pcm_callbacks
                    last_progress_at = time.monotonic()
                    consecutive_failures = 0
                    continue

                consecutive_failures += 1
                last_seen_callbacks = self.pcm_callbacks
                last_progress_at = time.monotonic()
                print(
                    "[WATCHDOG] Recovery failed "
                    f"({consecutive_failures}/{_WATCHDOG_MAX_CONSECUTIVE_FAILURES})."
                )

                if consecutive_failures >= _WATCHDOG_MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        "Ringo PCM stream repeatedly stalled and could not be recovered "
                        "after MIC restarts and BLE reconnects"
                    )
        finally:
            try:
                if session.mic_active:
                    await session.mic_off()
            finally:
                await session.disconnect()
