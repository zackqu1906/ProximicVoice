"""Reusable recognition runtime used by the customer UI.

The CLI remains the diagnostic entry point.  This module supplies the same
Ring/ProxiMic/ASR chain with cooperative start/stop and event callbacks so a UI
does not need to spawn or scrape a terminal process.
"""

from __future__ import annotations

from argparse import Namespace
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
import threading
from typing import Callable

from .asr import ASRBackendCache
from .audio import RingAudioSource
from .cli import _build_detector, _build_session_controller
from .events import Stage2Event
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

    detector_model: Path | None = None
    stage1_threshold: float = 0.005
    stage2_threshold: float | None = None
    stage2_delay_s: float = 0.50

    asr_backend: str = "streaming_sensevoice"
    asr_model: str = "iic/SenseVoiceSmall"
    asr_device: str = "cpu"
    asr_language: str = "zh"
    streaming_sensevoice_repo: Path | None = None
    funasr_nano_repo: Path | None = None
    funasr_nano_hotwords: str = ""

    asr_pre_roll_s: float = 1.5
    asr_end_rejects: int = 2
    asr_stage1_inactivity_s: float = 1.25
    asr_min_duration_s: float = 0.40
    asr_max_duration_s: float = 15.0

    desktop_output: bool = WINDOWS_DESKTOP_INPUT_SUPPORTED
    push_to_talk: bool = WINDOWS_DESKTOP_INPUT_SUPPORTED
    # Passed only to the selected online ASR backend.  Environment-variable
    # lookup inside that backend remains available for CLI compatibility.
    asr_api_key: str = ""

    def to_namespace(self) -> Namespace:
        backend = self.asr_backend.strip().lower().replace("-", "_")
        model_entry = f"{backend}={self.asr_model}" if self.asr_model else None
        hotwords = normalize_funasr_nano_hotwords(self.funasr_nano_hotwords)
        if backend == "funasr_nano" and hotwords:
            asr_options = [f"funasr_nano.hotwords={','.join(hotwords)}"]
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
            disable_proximic_detector=False,
            direct_asr_session_duration=5.0,
            asr_partial_min_interval=0.0,
            desktop_output=self.desktop_output,
            desktop_output_backend=backend if self.desktop_output else None,
            push_to_talk=self.push_to_talk,
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
        on_update: Callable[[object], None],
        on_state: Callable[[str], None],
        on_connected: Callable[[], None],
        on_disconnected: Callable[[], None],
        on_started: Callable[[], None],
        on_push_to_talk: Callable[[bool], None] | None = None,
        on_raw_audio: Callable[[int, object], None] | None = None,
    ) -> None:
        args = self.settings.to_namespace()
        source = RingAudioSource(
            name_keyword=args.name,
            selector=args.selector,
            device=args.device,
            timeout_s=args.timeout,
            encoding=args.encoding,
            data_root=args.data_dir,
        )
        detector = None
        controller = None
        watcher_done = threading.Event()
        connection_attempted = threading.Event()
        source_disconnected = threading.Event()
        source_close_lock = threading.Lock()

        def close_source_and_report() -> None:
            """Close the physical device once and publish that independently."""
            with source_close_lock:
                source.close()
                if connection_attempted.is_set() and not source_disconnected.is_set():
                    source_disconnected.set()
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
                raw_audio_observer=on_raw_audio,
            )
            if source.error is not None:
                raise RuntimeError(str(source.error)) from source.error
            if disconnect_event.is_set():
                return

            on_state("模型加载完成，正在启动并确认实时音频…")
            source.start_stream(buffer_audio=True)
            recognition_was_enabled = False
            on_started()
            while not disconnect_event.is_set():
                block = source.read(320)
                if block is None:
                    break

                recognition_enabled = recognition_event.is_set()
                if not recognition_enabled:
                    if recognition_was_enabled:
                        # Finish the current utterance once, then discard
                        # detector/ASR history captured before the pause.
                        controller.reset()
                        detector.reset()
                    recognition_was_enabled = False
                    continue

                if not recognition_was_enabled:
                    # Detector and controller sample clocks must restart
                    # together because DetectionEvent uses sample indexes.
                    detector.reset()
                    controller.reset()
                recognition_was_enabled = True
                events = detector.feed(block)
                for event in events:
                    if isinstance(event, Stage2Event):
                        on_state(format_event(event))
                controller.process(block, events)

            if recognition_was_enabled and not disconnect_event.is_set():
                controller.flush()
        finally:
            watcher_done.set()
            close_source_and_report()
            if watcher is not threading.current_thread():
                watcher.join(timeout=1.0)
            if controller is not None:
                if disconnect_event.is_set():
                    abort = getattr(controller, "abort", None)
                    if callable(abort):
                        abort()
                controller.close()
