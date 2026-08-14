from __future__ import annotations

import threading
from typing import Callable, Protocol

from .audio.base import AudioSource
from .detector import ProxiMicDetector
from .events import DetectionEvent, Stage1Event, Stage2Event


class AudioObserver(Protocol):
    def process(self, block, events: list[DetectionEvent]) -> None: ...
    def flush(self) -> None: ...


def format_event(event) -> str:
    if isinstance(event, Stage1Event):
        return f"STAGE1 t={event.time_s:8.3f}s max_amp={event.max_amplitude:.5f}"
    if isinstance(event, Stage2Event):
        label = "ACTIVATE" if event.activated else "reject"
        return (
            f"STAGE2 t={event.time_s:8.3f}s window=[{event.window_start_s:.3f}, {event.window_end_s:.3f}] "
            f"score={event.score:+.6f} logits=({event.logits[0]:+.6f},{event.logits[1]:+.6f}) {label}"
        )
    return repr(event)


def run_source(
    source: AudioSource,
    detector: ProxiMicDetector | None,
    *,
    read_frames: int = 320,
    show_stage1: bool = False,
    on_line: Callable[[str], None] = print,
    audio_observer: AudioObserver | None = None,
    stop_event: threading.Event | None = None,
    on_started: Callable[[], None] | None = None,
) -> None:
    with source:
        if on_started is not None:
            on_started()
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            block = source.read(read_frames)
            if block is None:
                break
            events = detector.feed(block) if detector is not None else []
            if audio_observer is not None:
                audio_observer.process(block, events)
            for event in events:
                if isinstance(event, Stage1Event) and not show_stage1:
                    continue
                on_line(format_event(event))
        if audio_observer is not None:
            audio_observer.flush()
