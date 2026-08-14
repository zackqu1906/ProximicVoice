from __future__ import annotations

import ctypes
import os

from proximic_ring.asr.streaming import StreamingASRUpdate
from proximic_ring.cli import build_parser
from proximic_ring.desktop_output import DesktopTranscriptOutput


class FakeInjector:
    def __init__(self):
        self.texts = []

    def inject(self, text):
        self.texts.append(text)


class FakeOverlay:
    def __init__(self):
        self.events = []
        self.closed = False

    def show_partial(self, text):
        self.events.append(("partial", text))

    def show_final(self, text):
        self.events.append(("final", text))

    def show_error(self, message):
        self.events.append(("error", message))

    def close(self):
        self.closed = True


def update(text, *, final=False, session=1, backend="stream"):
    return StreamingASRUpdate(
        backend=backend,
        model="model",
        text=text,
        is_final=final,
        latency_s=0.01,
        audio_duration_s=1.0,
        session_id=session,
    )


def test_partial_is_previewed_and_only_final_is_injected():
    injector = FakeInjector()
    overlay = FakeOverlay()
    output = DesktopTranscriptOutput(
        backend="stream", injector=injector, overlay=overlay
    )

    output(update("今天"))
    output(update("今天下午"))
    output(update("今天下午开会", final=True))

    assert injector.texts == ["今天下午开会"]
    assert overlay.events == [
        ("partial", "今天"),
        ("partial", "今天下午"),
        ("final", "今天下午开会"),
    ]


def test_final_is_committed_once_per_backend_session():
    injector = FakeInjector()
    overlay = FakeOverlay()
    output = DesktopTranscriptOutput(
        backend="stream", injector=injector, overlay=overlay
    )

    output(update("第一句", final=True, session=7))
    output(update("第一句", final=True, session=7))
    output(update("第二句", final=True, session=8))
    output(update("其他后端", final=True, session=8, backend="other"))

    assert injector.texts == ["第一句", "第二句"]


def test_focused_customer_ui_can_suppress_keyboard_injection():
    injector = FakeInjector()
    overlay = FakeOverlay()
    output = DesktopTranscriptOutput(
        backend="stream",
        injector=injector,
        overlay=overlay,
        should_inject=lambda: False,
    )

    output(update("保留在编辑器", final=True))

    assert injector.texts == []
    assert overlay.events == [("final", "保留在编辑器")]


def test_empty_final_and_error_are_never_injected():
    injector = FakeInjector()
    overlay = FakeOverlay()
    output = DesktopTranscriptOutput(
        backend="stream", injector=injector, overlay=overlay
    )

    output(update("", final=True))
    failed = update("", session=2)
    failed = StreamingASRUpdate(**{**failed.__dict__, "error": "network failed"})
    output(failed)
    output.close()

    assert injector.texts == []
    assert overlay.events == [("error", "network failed")]
    assert overlay.closed


def test_desktop_output_cli_is_opt_in():
    parser = build_parser()
    default = parser.parse_args(["ring", "--asr", "streaming_sensevoice"])
    enabled = parser.parse_args(
        [
            "ring",
            "--asr",
            "streaming_sensevoice",
            "--desktop-output",
            "--desktop-output-backend",
            "streaming_sensevoice",
            "--push-to-talk",
        ]
    )

    assert default.desktop_output is False
    assert enabled.desktop_output is True
    assert enabled.desktop_output_backend == "streaming_sensevoice"
    assert enabled.push_to_talk is True


def test_win32_input_structure_has_native_size():
    if os.name != "nt":
        return
    from proximic_ring.desktop_output import _INPUT

    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(_INPUT) == expected
