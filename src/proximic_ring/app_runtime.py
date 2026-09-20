"""Reusable recognition runtime used by the customer UI.

The CLI remains the diagnostic entry point.  This module supplies the same
Ring/ProxiMic/ASR chain with cooperative start/stop and event callbacks so a UI
does not need to spawn or scrape a terminal process.
"""

from __future__ import annotations

from argparse import Namespace
from collections import deque
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Callable

import numpy as np

from .asr import ASRBackendCache
from .audio import MicrophoneSource, RingAudioSource
from .cli import _build_detector, _build_session_controller
from .events import Stage2Event
from .gesture_settings import BoundGestureEvent, GestureBindings
from .runner import format_event


# ``ctypes.wintypes`` is not portable: importing it on macOS raises
# ``ValueError: _type_ 'v' not supported`` before the UI can create a window.
# Keep every Win32-only import behind the same platform guard as its use.
if os.name == "nt":
    from ctypes import wintypes


WINDOWS_DESKTOP_INPUT_SUPPORTED = os.name == "nt"
DESKTOP_TEXT_INJECTION_SUPPORTED = (
    WINDOWS_DESKTOP_INPUT_SUPPORTED or sys.platform == "darwin"
)
ASR_GAIN_DB_MIN = 0.0
ASR_GAIN_DB_MAX = 12.0
ASR_GAIN_DB_DEFAULT = 0.0
IMU_SAMPLE_RATE_HZ = 50
GESTURE_SAMPLE_RATE_HZ = 200
GESTURE_STATUS_INTERVAL_S = 5.0
IMU_BUFFER_SECONDS = 45.0
IMU_LEAD_IN_MS = 300.0
_ASR_CONTEXT_CAPTURE_MAX_CHARS = 1600


def _configure_gesture_cpu_threads(recognizer: object, backend: str) -> int | None:
    torch = getattr(getattr(recognizer, "classifier", None), "_torch", None)
    if torch is None:
        return None
    # The online ASR path has only two small local CNNs: near-field detection
    # and gestures. Eight-way intra-op work on both stalled the gesture queue
    # for ~1 s. Match the SDK test's single-thread configuration for this path.
    # Local ASR models retain their own existing CPU-thread policy.
    if backend == "volcengine" and torch.get_num_threads() > 1:
        torch.set_num_threads(1)
    return int(torch.get_num_threads())


class _ReadOnlyContextClipboard:
    """Fail closed if a context read ever tries to disturb the clipboard."""

    @staticmethod
    def _unsupported(*_args, **_kwargs):
        raise RuntimeError("ASR context capture forbids clipboard access")

    snapshot = _unsupported
    restore = _unsupported
    set_text = _unsupported
    text = _unsupported


def _context_target_key(target: object) -> str:
    accessibility_id = str(getattr(target, "accessibility_id", "") or "")
    uia_control = getattr(target, "uia_control", None)
    control_handle = int(getattr(target, "control_handle", 0) or 0)
    if accessibility_id:
        control_identity = accessibility_id
    elif uia_control is not None:
        control_identity = "uia:" + ".".join(
            str(value) for value in getattr(uia_control, "runtime_id", ())
        )
    elif control_handle:
        control_identity = f"control:{control_handle}"
    elif (
        int(getattr(target, "screen_width", 0) or 0) > 0
        and int(getattr(target, "screen_height", 0) or 0) > 0
    ):
        control_identity = ":".join(
            (
                "bounds",
                str(int(getattr(target, "screen_x", 0) or 0)),
                str(int(getattr(target, "screen_y", 0) or 0)),
                str(int(getattr(target, "screen_width", 0) or 0)),
                str(int(getattr(target, "screen_height", 0) or 0)),
            )
        )
    else:
        control_identity = "unresolved"
    return ":".join(
        (
            str(int(getattr(target, "process_id", 0) or 0)),
            str(int(getattr(target, "window_handle", 0) or 0)),
            control_identity,
        )
    )


def _context_error_reason(prefix: str, exc: BaseException) -> str:
    detail = "_".join(str(exc).split())[:160]
    suffix = f":{detail}" if detail else ""
    return f"{prefix}:{type(exc).__name__}{suffix}"


def _volcengine_context_provider(
    ime_context_provider: Callable[[], dict[str, object]] | None = None,
) -> Callable[[], dict[str, object]]:
    """Read IME context on macOS; Windows retains its read-only UIA adapter."""
    if sys.platform == "darwin":
        if ime_context_provider is not None:
            return ime_context_provider
        return lambda: {
            "status": "unavailable", "source": "input_method",
            "reason": "input_method_not_connected",
        }

    adapter = None

    def capture() -> dict[str, object]:
        nonlocal adapter
        if not DESKTOP_TEXT_INJECTION_SUPPORTED:
            return {
                "status": "unavailable",
                "source": "focused_text",
                "reason": "platform_unsupported",
            }
        target = None
        try:
            if adapter is None:
                from .desktop_target import WindowsDesktopTextTarget

                adapter = WindowsDesktopTextTarget(_ReadOnlyContextClipboard())
            target = adapter.capture_reference()
            # Context capture is deliberately observation-only.  It never
            # falls back to Select-All/Copy, so it cannot move the caret,
            # replace a selection, or modify the user's clipboard.
            context_observer = getattr(adapter, "observe_context_text", None)
            if callable(context_observer):
                snapshot = context_observer(
                    target,
                    max_chars=_ASR_CONTEXT_CAPTURE_MAX_CHARS,
                )
            else:
                snapshot = adapter.observe_text(target)
            full_text = str(snapshot.text or "").rstrip()
        except BaseException as exc:
            reason = _context_error_reason("focused_text_unreadable", exc)
            result = {
                "status": "unavailable",
                "source": "focused_text",
                "reason": reason,
            }
            accessibility_probe = str(
                getattr(
                    adapter,
                    "last_context_accessibility_probe",
                    "",
                )
                or ""
            )
            if accessibility_probe:
                result["ax_manual_accessibility"] = accessibility_probe
            if target is not None:
                result["target_key"] = _context_target_key(target)
                result["application"] = str(
                    getattr(target, "process_name", "")
                    or getattr(target, "window_title", "")
                    or "unknown"
                )
            return result
        application = str(
            getattr(target, "process_name", "")
            or getattr(target, "window_title", "")
            or "unknown"
        )
        observed_source_char_count = getattr(
            adapter, "last_context_source_char_count", None
        )
        source_char_count = (
            max(len(full_text), int(observed_source_char_count))
            if isinstance(observed_source_char_count, int)
            and observed_source_char_count >= 0
            else len(full_text)
        )
        if not full_text:
            result = {
                "status": "empty",
                "source": "focused_text",
                "reason": "empty_focused_text",
                "source_char_count": 0,
                "target_key": _context_target_key(target),
                "application": application,
            }
        else:
            result = {
                "status": "captured",
                "source": "focused_text",
                "text": full_text[-_ASR_CONTEXT_CAPTURE_MAX_CHARS:],
                "source_char_count": source_char_count,
                "target_key": _context_target_key(target),
                "application": application,
            }
        read_method = str(getattr(adapter, "last_context_read_method", "") or "")
        if read_method:
            result["read_method"] = read_method
        accessibility_probe = str(
            getattr(
                adapter,
                "last_context_accessibility_probe",
                "",
            )
            or ""
        )
        if accessibility_probe:
            result["ax_manual_accessibility"] = accessibility_probe
        return result

    return capture


class _ImuSampleBuffer:
    """Bounded, thread-safe bridge from BLE callbacks to utterance records."""

    def __init__(
        self,
        *,
        sample_rate_hz: int = IMU_SAMPLE_RATE_HZ,
        buffer_seconds: float = IMU_BUFFER_SECONDS,
    ) -> None:
        self.sample_rate_hz = int(sample_rate_hz)
        self._max_age_ns = int(float(buffer_seconds) * 1_000_000_000)
        self._rows: deque[dict] = deque()
        self._lock = threading.Lock()

    def append(self, row: dict) -> None:
        received_ns = int(row["host_monotonic_ns"])
        cutoff_ns = received_ns - self._max_age_ns
        with self._lock:
            self._rows.append(dict(row))
            while self._rows and int(
                self._rows[0]["host_monotonic_ns"]
            ) < cutoff_ns:
                self._rows.popleft()

    @staticmethod
    def _device_clock_offset_ms(rows: list[dict]) -> float | None:
        """Map Ring uptime to host time using the last sample of each BLE packet."""
        packet_tails: dict[int, dict] = {}
        fallback: list[dict] = []
        for row in rows:
            if "device_uptime_ms" not in row or "host_monotonic_ns" not in row:
                continue
            fallback.append(row)
            packet_seq = row.get("packet_seq")
            if packet_seq is None:
                continue
            key = int(packet_seq)
            current = packet_tails.get(key)
            if current is None or float(row["device_uptime_ms"]) > float(
                current["device_uptime_ms"]
            ):
                packet_tails[key] = row
        anchors = list(packet_tails.values()) or fallback
        if not anchors:
            return None
        offsets = [
            int(row["host_monotonic_ns"]) / 1_000_000
            - float(row["device_uptime_ms"])
            for row in anchors
        ]
        return float(np.median(offsets))

    def slice_for_audio(
        self,
        *,
        audio_start_monotonic_ns: int,
        audio_end_monotonic_ns: int,
    ) -> tuple[list[dict], dict]:
        start_ms = int(audio_start_monotonic_ns) / 1_000_000
        end_ms = int(audio_end_monotonic_ns) / 1_000_000
        lower_ms = start_ms - IMU_LEAD_IN_MS
        with self._lock:
            buffered = [dict(row) for row in self._rows]
        clock_offset_ms = self._device_clock_offset_ms(buffered)
        alignment_method = (
            "device_uptime_packet_tail_v2"
            if clock_offset_ms is not None
            else "host_receive_fallback_v2" if buffered else "unavailable"
        )
        aligned: list[tuple[dict, float]] = []
        for row in buffered:
            if clock_offset_ms is not None and row.get("device_uptime_ms") is not None:
                sample_host_ms = float(row["device_uptime_ms"]) + clock_offset_ms
            else:
                alignment_method = "host_receive_fallback_v2"
                sample_host_ms = int(row["host_monotonic_ns"]) / 1_000_000
            if lower_ms <= sample_host_ms <= end_ms:
                aligned.append((row, sample_host_ms))

        selected: list[dict] = []
        for row, sample_host_ms in aligned:
            compact = {
                "relative_to_audio_start_ms": round(sample_host_ms - start_ms, 3),
            }
            if row.get("accel_ms2") is not None:
                compact["accel_ms2"] = row["accel_ms2"]
            if row.get("gyro_dps") is not None:
                compact["gyro_dps"] = row["gyro_dps"]
            selected.append(compact)

        sample_indexes = sorted(
            {
                int(row["sample_index"])
                for row, _sample_host_ms in aligned
                if row.get("sample_index") is not None
            }
        )
        dropped_samples = 0
        if len(sample_indexes) >= 2:
            dropped_samples = max(
                0,
                sample_indexes[-1] - sample_indexes[0] + 1 - len(sample_indexes),
            )
        return selected, {
            "sample_rate_hz": self.sample_rate_hz,
            "dropped_samples": dropped_samples,
            "alignment_method": alignment_method,
        }


def apply_asr_gain(audio_16k: object, gain_db: float) -> np.ndarray:
    """Apply bounded ASR-only gain without changing detector input or timing."""

    gain_db = float(gain_db)
    if not np.isfinite(gain_db):
        raise ValueError("ASR gain must be finite")
    if not ASR_GAIN_DB_MIN <= gain_db <= ASR_GAIN_DB_MAX:
        raise ValueError(
            f"ASR gain must be between {ASR_GAIN_DB_MIN:g} and "
            f"{ASR_GAIN_DB_MAX:g} dB"
        )
    audio = np.asarray(audio_16k, dtype=np.float32).reshape(-1)
    if gain_db == 0.0 or not audio.size:
        return audio
    scale = np.float32(10.0 ** (gain_db / 20.0))
    return np.clip(audio * scale, -1.0, 1.0).astype(np.float32, copy=False)


def normalize_funasr_nano_hotwords(value: str) -> tuple[str, ...]:
    """Normalize UI-friendly separators and remove duplicate Nano hotwords."""

    words: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,，;；\r\n]+", str(value or "")):
        word = item.strip()
        identity = word.casefold()
        if not word or identity in seen:
            continue
        seen.add(identity)
        words.append(word)
    return tuple(words)


class SilentTranscriptOverlay:
    """Disable the legacy Tk preview when QML owns the transcript overlay."""

    def show_partial(self, _text: str) -> None:
        return None

    def show_final(self, _text: str) -> None:
        return None

    def show_error(self, _message: str) -> None:
        return None

    def close(self) -> None:
        return None


def external_window_has_focus() -> bool:
    """Return false when the foreground window belongs to this UI process."""
    if os.name != "nt":
        return True
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    foreground = user32.GetForegroundWindow()
    if not foreground:
        return False
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id))
    return process_id.value != os.getpid()


@dataclass(frozen=True)
class RuntimeSettings:
    ring_name: str = "Ringo"
    ring_selector: str | None = None
    ring_device: object | None = None
    ring_timeout_s: float = 8.0
    # Opus is the production default because it keeps BLE traffic low enough
    # for reliable continuous streaming on Windows.  The SDK still exposes
    # decoded 16 kHz PCM16 to the detector, regardless of transport codec.
    encoding: str = "opus"
    data_dir: Path = Path("data")
    audio_source: str = "ring"
    microphone_device: str = ""  # Empty means explicitly auto-detect DJI only.
    speech_control_mode: str = "proximity"

    detector_model: Path | None = None
    stage1_threshold: float = 0.005
    stage2_threshold: float | None = None
    stage2_delay_s: float = 0.30
    stage2_active_interval_s: float = 0.20

    asr_backend: str = "streaming_sensevoice"
    asr_model: str = "iic/SenseVoiceSmall"
    asr_device: str = "cpu"
    asr_language: str = "zh"
    streaming_sensevoice_repo: Path | None = None
    funasr_nano_repo: Path | None = None
    funasr_nano_hotwords: str = ""
    asr_gain_db: float = ASR_GAIN_DB_DEFAULT

    asr_pre_roll_s: float = 1.0
    asr_end_rejects: int = 5
    asr_stage1_inactivity_s: float = 1.25
    asr_min_duration_s: float = 0.40
    asr_max_duration_s: float = 15.0
    # The desktop UI opts into tap; headless/offline callers keep auto END.
    asr_end_on_tap: bool = False
    gesture_bindings: GestureBindings = GestureBindings()

    desktop_output: bool = WINDOWS_DESKTOP_INPUT_SUPPORTED
    push_to_talk: bool = WINDOWS_DESKTOP_INPUT_SUPPORTED
    # Passed only to the selected online ASR backend.  Environment-variable
    # lookup inside that backend remains available for CLI compatibility.
    asr_api_key: str = ""
    # Dataset capture is optional. Gesture consumers independently request IMU.
    collect_imu: bool = True
    imu_sample_rate_hz: int = IMU_SAMPLE_RATE_HZ

    def to_namespace(self) -> Namespace:
        if self.audio_source not in {"ring", "microphone"}:
            raise ValueError("未知音频来源")
        if self.speech_control_mode not in {"proximity", "gesture"}:
            raise ValueError("未知语音启停方式")
        backend = self.asr_backend.strip().lower().replace("-", "_")
        model_entry = f"{backend}={self.asr_model}" if self.asr_model else None
        hotwords = normalize_funasr_nano_hotwords(self.funasr_nano_hotwords)
        if backend == "streaming_sensevoice":
            # The QML flow already receives stable streaming text. Flushing
            # that session is much faster than decoding the whole utterance a
            # second time after the detector has ended it.
            asr_options = ["streaming_sensevoice.final_redecode=false"]
        elif backend == "funasr_nano":
            asr_options = ["funasr_nano.final_redecode=false"]
            if hotwords:
                asr_options.append(
                    f"funasr_nano.hotwords={','.join(hotwords)}"
                )
        elif backend == "volcengine" and self.asr_api_key.strip():
            asr_options = [f"volcengine.api_key={self.asr_api_key.strip()}"]
        else:
            asr_options = None
        return Namespace(
            command="ring",
            name=self.ring_name,
            selector=self.ring_selector or None,
            device=self.ring_device,
            timeout=self.ring_timeout_s,
            encoding=self.encoding,
            data_dir=self.data_dir,
            model=self.detector_model,
            stage1_threshold=self.stage1_threshold,
            stage2_threshold=self.stage2_threshold,
            stage2_delay=self.stage2_delay_s,
            stage2_active_interval=self.stage2_active_interval_s,
            show_stage1=False,
            asr=[backend],
            asr_model=[model_entry] if model_entry else None,
            asr_device=self.asr_device,
            asr_language=self.asr_language,
            asr_option=asr_options,
            sensevoice_repo=None,
            streaming_sensevoice_repo=(
                self.streaming_sensevoice_repo
                if backend == "streaming_sensevoice"
                else None
            ),
            funasr_nano_repo=(
                self.funasr_nano_repo if backend == "funasr_nano" else None
            ),
            asr_pre_roll=self.asr_pre_roll_s,
            asr_end_rejects=self.asr_end_rejects,
            asr_stage1_inactivity=self.asr_stage1_inactivity_s,
            asr_min_duration=self.asr_min_duration_s,
            asr_max_duration=self.asr_max_duration_s,
            asr_end_on_tap=self.asr_end_on_tap or self.speech_control_mode == "gesture",
            asr_start_on_gesture=self.speech_control_mode == "gesture",
            disable_proximic_detector=False,
            direct_asr_session_duration=5.0,
            asr_partial_min_interval=0.0,
            desktop_output=self.desktop_output,
            desktop_output_backend=backend if self.desktop_output else None,
            push_to_talk=self.push_to_talk and self.speech_control_mode != "gesture",
        )


class RecognitionRuntime:
    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        asr_backend_cache: ASRBackendCache | None = None,
    ) -> None:
        self.settings = settings
        self.asr_backend_cache = asr_backend_cache

    def run(
        self,
        disconnect_event: threading.Event,
        recognition_event: threading.Event,
        *,
        cancel_utterance_event: threading.Event | None = None,
        finish_utterance_event: threading.Event | None = None,
        on_update: Callable[[object], None],
        on_state: Callable[[str], None],
        on_connected: Callable[[], None],
        on_disconnected: Callable[[], None],
        on_started: Callable[[], None],
        on_stopping: Callable[[], None] | None = None,
        on_push_to_talk: Callable[[bool], None] | None = None,
        on_session_started: Callable[[int], None] | None = None,
        on_session_ended: Callable[[], None] | None = None,
        on_asr_context: Callable[[int, dict[str, object]], None] | None = None,
        on_raw_audio: Callable[[int, object], None] | None = None,
        on_raw_imu: Callable[[int, object, dict], None] | None = None,
        on_gesture: Callable[[object], None] | None = None,
        on_battery: Callable[
            [int | None, int | None, int | None], None
        ]
        | None = None,
        ime_context_provider: Callable[[], dict[str, object]] | None = None,
        prepare_gesture_start: Callable[[Callable[[bool], None] | None], None] | None = None,
        asr_gain_db_provider: Callable[[], float] | None = None,
        stage1_threshold_provider: Callable[[], float] | None = None,
        gesture_bindings_provider: Callable[[], GestureBindings] | None = None,
    ) -> None:
        args = self.settings.to_namespace()
        gesture_control = self.settings.speech_control_mode == "gesture"
        end_on_gesture = bool(args.asr_end_on_tap)
        selected_backend = self.settings.asr_backend.strip().lower().replace(
            "-", "_"
        )
        asr_context_provider = (
            _volcengine_context_provider(ime_context_provider)
            if selected_backend == "volcengine"
            else None
        )
        # One physical stream serves both consumers. Record its actual rate in
        # dataset metadata; the supplied gesture model requires exactly 200 Hz.
        imu_hz = (
            GESTURE_SAMPLE_RATE_HZ
            if on_gesture is not None or end_on_gesture
            else self.settings.imu_sample_rate_hz
        )
        imu_buffer = (
            _ImuSampleBuffer(sample_rate_hz=imu_hz)
            if self.settings.collect_imu and on_raw_imu is not None
            else None
        )
        source = RingAudioSource(
            name_keyword=args.name,
            selector=args.selector,
            device=args.device,
            timeout_s=args.timeout,
            encoding=args.encoding,
            data_root=args.data_dir,
            imu_observer=imu_buffer.append if imu_buffer is not None else None,
            battery_observer=on_battery,
            imu_hz=imu_hz,
            **({"audio_enabled": False} if self.settings.audio_source == "microphone" else {}),
        )
        microphone = (
            MicrophoneSource(selection=self.settings.microphone_device)
            if self.settings.audio_source == "microphone" else None
        )
        audio_source = microphone if microphone is not None else source
        gesture_worker = None
        gestures_enabled = threading.Event()
        gesture_error_reported = False
        next_gesture_status_at = 0.0
        detector = None
        controller = None
        watcher_done = threading.Event()
        connection_attempted = threading.Event()
        source_disconnected = threading.Event()
        stopping_reported = False
        stopping_lock = threading.Lock()
        source_close_lock = threading.Lock()

        def publish_raw_utterance(session_id: int, audio_16k: object) -> None:
            audio = np.asarray(audio_16k, dtype=np.float32).reshape(-1)
            duration_ns = int(audio.size * 1_000_000_000 / 16_000)
            controller_start_ns = getattr(
                controller, "audio_start_monotonic_ns", None
            )
            audio_start_ns = int(
                controller_start_ns
                if controller_start_ns is not None
                else time.monotonic_ns() - duration_ns
            )
            audio_end_ns = audio_start_ns + duration_ns
            if on_raw_audio is not None:
                on_raw_audio(session_id, audio_16k)
            if on_raw_imu is None or imu_buffer is None:
                return
            rows, metadata = imu_buffer.slice_for_audio(
                audio_start_monotonic_ns=audio_start_ns,
                audio_end_monotonic_ns=audio_end_ns,
            )
            imu_error = getattr(source, "imu_error", None)
            metadata["collection_error"] = (
                str(imu_error) if imu_error is not None else None
            )
            on_raw_imu(session_id, rows, metadata)

        def close_source_and_report() -> None:
            """Close the physical device once and publish that independently."""
            nonlocal stopping_reported
            # Stop interaction and notify before BLE/model cleanup, which can
            # take seconds. The stop event is also set on unexpected EOF/errors.
            disconnect_event.set()
            recognition_event.clear()
            gestures_enabled.clear()
            with stopping_lock:
                if not stopping_reported:
                    stopping_reported = True
                    if on_stopping is not None:
                        on_stopping()
            with source_close_lock:
                if not source_disconnected.is_set():
                    try:
                        if microphone is not None:
                            microphone.close()
                    finally:
                        try:
                            source.close()
                        finally:
                            source_disconnected.set()
                            if connection_attempted.is_set():
                                on_disconnected()

        def stop_source_when_requested() -> None:
            while not watcher_done.wait(0.1):
                if disconnect_event.is_set():
                    close_source_and_report()
                    return
                if source.error is not None:
                    on_state(f"设备连接已中断：{source.error}")
                    disconnect_event.set()
                    close_source_and_report()
                    return
                if microphone is not None and microphone.error is not None:
                    on_state(f"[AUDIO_INPUT_ERROR] {microphone.error}")
                    close_source_and_report()
                    return

        watcher = threading.Thread(
            target=stop_source_when_requested,
            name="ProxiMicDisconnectWatcher",
            daemon=True,
        )
        watcher.start()
        try:
            if disconnect_event.is_set():
                return
            on_state(f"正在连接设备 {self.settings.ring_name}…")
            connection_attempted.set()
            source.connect()
            if disconnect_event.is_set():
                return
            on_connected()

            # Keep MIC OFF while importing and constructing the detector/ASR.
            # Bleak notifications are Python callbacks; heavy model startup can
            # starve that callback path and the Ring stream was observed to stop
            # after only 2-3 blocks.  The firmware receiver remains reliable
            # because it starts MIC only after its lightweight UI is ready.
            on_state("设备已连接，保持麦克风关闭并加载模型…")

            if disconnect_event.is_set():
                return
            if gesture_control:
                on_state("纯手势启停：已跳过近点检测模型")
            else:
                on_state("正在加载 ProxiMic 检测模型…")
                detector = _build_detector(args)
            if source.error is not None:
                raise RuntimeError(str(source.error)) from source.error
            if disconnect_event.is_set():
                return

            on_state(f"正在加载语音模型 {args.asr[0]}…")
            controller = _build_session_controller(
                args,
                detector,
                streaming_observer=on_update,
                desktop_overlay=SilentTranscriptOverlay(),
                on_state=on_state,
                show_streaming_console=False,
                push_to_talk_observer=on_push_to_talk,
                desktop_should_inject=external_window_has_focus,
                backend_cache=self.asr_backend_cache,
                raw_audio_observer=(
                    publish_raw_utterance
                    if on_raw_audio is not None or on_raw_imu is not None
                    else None
                ),
                raw_session_start_observer=on_session_started,
                session_end_observer=on_session_ended,
                asr_context_provider=asr_context_provider,
                asr_context_observer=on_asr_context,
            )
            if source.error is not None:
                raise RuntimeError(str(source.error)) from source.error
            if disconnect_event.is_set():
                return

            if on_gesture is not None or end_on_gesture:
                on_state("正在加载电脑端手势模型…")
                try:
                    from ring_python_sdk.gestures import GestureWorker
                    from proximic_ring.host_gestures import (
                        GestureRecognizer, MODEL_PATH, MODEL_NAME, SDK_REVISION,
                    )

                    def publish_gesture(event: object) -> None:
                        bindings = (
                            gesture_bindings_provider() if gesture_bindings_provider
                            else self.settings.gesture_bindings
                        )
                        name = str(getattr(event, "name", ""))
                        accepted = (
                            gestures_enabled.is_set()
                            and not disconnect_event.is_set()
                            and source.error is None
                            and (microphone is None or microphone.error is None)
                        )
                        is_confirm_endpoint = (
                            end_on_gesture
                            and bool(name) and name in bindings.confirm
                        )
                        confirm_requested = False
                        requested_action = "end"
                        blocked_reason = ""
                        if accepted and is_confirm_endpoint:
                            # Do this before logging or queuing any GUI work.
                            # The audio producer owns END and closes the normal
                            # interaction gate before submitting final ASR.
                            can_request = (
                                recognition_event.is_set()
                                and not (
                                    cancel_utterance_event is not None
                                    and cancel_utterance_event.is_set()
                                )
                            )
                            if can_request:
                                if gesture_control:
                                    requested_action = controller.request_gesture_toggle(prepare_gesture_start)
                                    confirm_requested = requested_action is not None
                                else:
                                    confirm_requested = controller.request_tap_end()
                            else:
                                blocked_reason = (
                                    "cancellation_pending"
                                    if cancel_utterance_event is not None and cancel_utterance_event.is_set()
                                    else "recognition_gate_closed"
                                )
                        # Record model output before application state gating.
                        on_state("[GESTURE_RECOGNIZED] " + json.dumps({
                            "name": getattr(event, "name", ""),
                            "confidence": getattr(event, "confidence", None),
                            "device_timestamp_ms": getattr(event, "timestamp_ms", None),
                            "forwarded": accepted,
                            "reason": (
                                "runtime_stopped" if not accepted else
                                f"confirm_{requested_action}_requested" if confirm_requested else
                                blocked_reason if blocked_reason else
                                "no_active_utterance_or_duplicate" if is_confirm_endpoint else
                                "ui_dispatch"
                            ),
                        }, ensure_ascii=False))
                        if confirm_requested:
                            action_label = {"start": "准备本句" if prepare_gesture_start else "开始本句",
                                            "cancel_start": "取消准备", "end": "结束本句"}[requested_action]
                            on_state(f"[手势] {name} → {action_label}")
                        if accepted and not is_confirm_endpoint and on_gesture is not None:
                            on_gesture(
                                BoundGestureEvent(event, bindings)
                                if gesture_bindings_provider else event
                            )

                    recognizer = GestureRecognizer(on_gesture=publish_gesture)
                    gesture_threads = _configure_gesture_cpu_threads(recognizer, selected_backend)
                    # Warm up before starting MIC/IMU.
                    recognizer.classifier.predict(
                        np.zeros((recognizer.classifier.window_size, 6), dtype=np.float32)
                    )
                    gesture_worker = GestureWorker(recognizer)
                    gesture_worker.start()
                    source.imu_sample_observer = gesture_worker.submit
                    gestures_enabled.set()
                    model_path = MODEL_PATH
                    on_state("[GESTURE_MODEL] " + json.dumps({
                        "model": MODEL_NAME,
                        "backend": "pytorch-cpu",
                        "source": "host",
                        "sdk_revision": SDK_REVISION,
                        "path": str(model_path),
                        "sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
                        "sample_hz": GESTURE_SAMPLE_RATE_HZ,
                        "cpu_intra_threads": gesture_threads,
                        "packet_timestamp_tolerance_ms": getattr(
                            recognizer, "packet_timestamp_tolerance_ms", None
                        ),
                    }, ensure_ascii=False))
                    on_state(
                        "电脑端手势已就绪，使用设置中的手势分配"
                    )
                except Exception as exc:
                    gestures_enabled.clear()
                    gesture_error_reported = True
                    if gesture_control:
                        raise RuntimeError(f"纯手势模式无法启动：手势模型不可用：{exc}") from exc
                    on_state(
                        f"电脑端手势不可用，确认手势无法结束语音，请用 Esc 取消本句并重连：{exc}"
                        if end_on_gesture else
                        f"电脑端手势不可用，按键和语音继续工作：{exc}"
                    )
            if disconnect_event.is_set():
                return

            on_state("模型加载完成，正在启动并确认实时音频…")
            source.start_stream(buffer_audio=True)
            if disconnect_event.is_set():
                return
            if microphone is not None:
                try:
                    microphone.open()
                    if microphone.read(320) is None or disconnect_event.is_set():
                        return
                except Exception as exc:
                    on_state(f"[AUDIO_INPUT_ERROR] {exc}")
                    raise
                on_state(f"音频来源：{microphone.device_name}（电脑麦克风）；Ring 仅提供手势")
            else:
                on_state("音频来源：Ring 麦克风")
            next_gesture_status_at = time.monotonic() + GESTURE_STATUS_INTERVAL_S
            recognition_was_enabled = False
            on_started()
            while not disconnect_event.is_set():
                try:
                    block = audio_source.read(320)
                except Exception as exc:
                    if microphone is not None:
                        on_state(f"[AUDIO_INPUT_ERROR] {exc}")
                    raise
                if block is None:
                    break
                if disconnect_event.is_set():
                    break
                if gesture_worker is not None and not gesture_error_reported:
                    gesture_error = gesture_worker.error or getattr(
                        source, "imu_sample_error", None
                    ) or getattr(
                        source, "imu_stream_error", None
                    )
                    if gesture_error is not None:
                        gesture_error_reported = True
                        gestures_enabled.clear()
                        if gesture_control:
                            raise RuntimeError(f"纯手势模式已停止：{gesture_error}") from gesture_error
                        on_state(
                            f"电脑端手势已停止，确认手势无法结束语音，请用 Esc 取消本句并重连：{gesture_error}"
                            if end_on_gesture else
                            f"电脑端手势已停止，按键和语音继续工作：{gesture_error}"
                        )
                if gesture_worker is not None and time.monotonic() >= next_gesture_status_at:
                    stats = gesture_worker.snapshot()
                    stats["status"] = (
                        "error" if gesture_error_reported else
                        "no_imu" if not stats["received_samples"] else
                        "imu_stalled" if (stats["last_input_age_ms"] or 0) > 2000 else
                        "inference_backlog" if stats["last_queue_wait_ms"] > 250 else
                        "running"
                    )
                    on_state("[GESTURE_STATUS] " + json.dumps(stats, ensure_ascii=False))
                    next_gesture_status_at = time.monotonic() + GESTURE_STATUS_INTERVAL_S
                block_end_monotonic_ns = int(
                    getattr(audio_source, "last_read_end_monotonic_ns", 0)
                    or time.monotonic_ns()
                )

                if (
                    cancel_utterance_event is not None
                    and cancel_utterance_event.is_set()
                ):
                    discard_current = getattr(controller, "discard_current", None)
                    if callable(discard_current):
                        discard_current()
                    cancel_pending = getattr(controller, "cancel_pending", None)
                    if callable(cancel_pending):
                        cancel_pending()
                    if detector is not None:
                        detector.reset()
                    # Keep confirm gestures gated until the controller's old
                    # tap flags are cleared; otherwise a new START can be lost.
                    cancel_utterance_event.clear()
                    # UI cancellation can race with END closing the gate.
                    # Acknowledge only after retiring all pending ASR work so
                    # the Qt owner can reopen that gate even without a final.
                    on_state("[ASR] CANCELLED reason=user-request")
                    # Keep the user's recognition on/off choice unchanged.
                    # The next block begins with clean detector/session clocks.
                    recognition_was_enabled = recognition_event.is_set()
                    continue

                if finish_utterance_event is not None and finish_utterance_event.is_set():
                    finish_utterance_event.clear()
                    request_end = getattr(controller, "request_user_end", None)
                    if callable(request_end):
                        request_end()
                    else:
                        controller.flush()  # direct-ASR baseline has no tap gate

                recognition_enabled = recognition_event.is_set()
                if not recognition_enabled:
                    if recognition_was_enabled:
                        # Finish the current utterance once, then discard
                        # detector/ASR history captured before the pause.
                        controller.reset()
                        if detector is not None:
                            detector.reset()
                    recognition_was_enabled = False
                    continue

                if not recognition_was_enabled:
                    # Detector and controller sample clocks must restart
                    # together because DetectionEvent uses sample indexes.
                    if detector is not None:
                        detector.reset()
                    # A gesture can be queued between enable and this first
                    # audio block. Gesture sessions have no pre-roll to reset.
                    if not gesture_control:
                        controller.reset()
                recognition_was_enabled = True
                if detector is not None and stage1_threshold_provider is not None:
                    try:
                        live_threshold = float(stage1_threshold_provider())
                        if (
                            np.isfinite(live_threshold)
                            and live_threshold > 0
                            and live_threshold != detector.config.stage1_threshold
                        ):
                            detector.config = detector.config.with_overrides(
                                stage1_threshold=live_threshold
                            )
                    except (TypeError, ValueError):
                        pass
                # ProxiMic evaluates the selected source's untouched waveform. Gain
                # is applied only after detection, so both ASR and the raw
                # utterance observer/history receive the same enhanced audio.
                was_active = bool(getattr(controller, "active", False))
                # ACTIVATE is needed only to start. Once listening for tap,
                # skip Stage2 inference entirely, keeping audio and gestures
                # free from reject inference and its CPU/GIL scheduling cost.
                events = (
                    [] if gesture_control or (end_on_gesture and was_active)
                    else detector.feed(block)
                )
                for event in events:
                    if isinstance(event, Stage2Event):
                        on_state(format_event(event))
                live_asr_gain_db = self.settings.asr_gain_db
                if asr_gain_db_provider is not None:
                    try:
                        candidate_gain_db = float(asr_gain_db_provider())
                        if np.isfinite(candidate_gain_db):
                            live_asr_gain_db = candidate_gain_db
                    except (TypeError, ValueError):
                        pass
                controller.process(
                    apply_asr_gain(block, live_asr_gain_db),
                    events,
                    block_end_monotonic_ns=block_end_monotonic_ns,
                )
                if end_on_gesture and was_active and not controller.active:
                    # Reset even if a very fast final/application already
                    # reopened recognition before this loop saw the pause.
                    controller.reset()
                    if detector is not None:
                        detector.reset()
                    recognition_was_enabled = False
                set_continuation_mode = getattr(
                    detector, "set_continuation_mode", None
                )
                if callable(set_continuation_mode):
                    set_continuation_mode(
                        bool(getattr(controller, "active", False))
                    )

            # Ring EOF ends the device session; it must not submit a partial
            # utterance as if the user had performed the confirmation gesture.
        finally:
            watcher_done.set()
            try:
                close_source_and_report()
            finally:
                if gesture_worker is not None:
                    try:
                        gesture_worker.close()
                        on_state(f"电脑端手势统计：{gesture_worker.snapshot()}")
                    except Exception as exc:
                        on_state(f"电脑端手势清理异常：{exc}")
                if watcher is not threading.current_thread():
                    watcher.join(timeout=1.0)
                if controller is not None:
                    abort = getattr(controller, "abort", None)
                    if callable(abort):
                        abort()
                    controller.close()
