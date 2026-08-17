"""Reusable recognition runtime used by the customer UI.

The CLI remains the diagnostic entry point.  This module supplies the same
Ring/ProxiMic/ASR chain with cooperative start/stop and event callbacks so a UI
does not need to spawn or scrape a terminal process.
"""

from __future__ import annotations

from argparse import Namespace
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
from pathlib import Path
import threading
from typing import Callable

from .audio import RingAudioSource
from .cli import _build_detector, _build_session_controller


WINDOWS_DESKTOP_INPUT_SUPPORTED = os.name == "nt"


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
    ring_timeout_s: float = 8.0
    encoding: str = "pcm"
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

    asr_pre_roll_s: float = 1.0
    asr_end_rejects: int = 2
    asr_stage1_inactivity_s: float = 1.25
    asr_min_duration_s: float = 0.40
    asr_max_duration_s: float = 15.0

    desktop_output: bool = WINDOWS_DESKTOP_INPUT_SUPPORTED
    push_to_talk: bool = WINDOWS_DESKTOP_INPUT_SUPPORTED

    def to_namespace(self) -> Namespace:
        backend = self.asr_backend.strip().lower().replace("-", "_")
        model_entry = f"{backend}={self.asr_model}" if self.asr_model else None
        return Namespace(
            command="ring",
            name=self.ring_name,
            selector=self.ring_selector or None,
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
            asr_option=None,
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
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings

    def run(
        self,
        disconnect_event: threading.Event,
        recognition_event: threading.Event,
        *,
        on_update: Callable[[object], None],
        on_state: Callable[[str], None],
        on_started: Callable[[], None],
        on_push_to_talk: Callable[[bool], None] | None = None,
    ) -> None:
        args = self.settings.to_namespace()
        on_state("正在加载 ProxiMic 检测模型…")
        detector = _build_detector(args)
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
        )
        source = RingAudioSource(
            name_keyword=args.name,
            selector=args.selector,
            timeout_s=args.timeout,
            encoding=args.encoding,
            data_root=args.data_dir,
        )
        try:
            if disconnect_event.is_set():
                return
            on_state("正在连接 Ringo…")
            recognition_was_enabled = False
            with source:
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
                    controller.process(block, events)

                if recognition_was_enabled:
                    controller.flush()
        finally:
            if controller is not None:
                controller.close()
