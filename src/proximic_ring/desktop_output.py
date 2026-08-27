"""Safe first-stage desktop output for streaming ASR on Windows.

Partial transcripts are previewed in a small, non-activating overlay.  Only a
final transcript is sent to the application that currently owns keyboard
focus.  The injector uses Windows Unicode keyboard events, so it never replaces
the user's clipboard.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
from typing import Callable, Protocol


if os.name == "nt":
    from ctypes import wintypes


class TextInjector(Protocol):
    def inject(self, text: str) -> None: ...


class TranscriptOverlay(Protocol):
    def show_partial(self, text: str) -> None: ...
    def show_final(self, text: str) -> None: ...
    def show_error(self, message: str) -> None: ...
    def close(self) -> None: ...


class WindowsUnicodeTextInjector:
    """Insert Unicode text without touching the clipboard.

    ``SendInput`` is deliberately called only for final ASR results.  It works
    with ordinary Windows edit controls, browsers, chat applications and code
    editors.  Windows intentionally blocks a normal process from injecting
    into an elevated (administrator) application.
    """

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("desktop text injection is currently supported only on Windows")

    def inject(self, text: str) -> None:
        text = str(text or "")
        if not text:
            return

        # SendInput's Unicode mode accepts UTF-16 code units in ``wScan``.
        # Splitting the encoded bytes also handles characters outside the BMP.
        encoded = text.encode("utf-16-le", errors="surrogatepass")
        units = [int.from_bytes(encoded[i : i + 2], "little") for i in range(0, len(encoded), 2)]
        inputs = (_INPUT * (len(units) * 2))()
        for index, unit in enumerate(units):
            inputs[index * 2] = _keyboard_input(unit, self.KEYEVENTF_UNICODE)
            inputs[index * 2 + 1] = _keyboard_input(
                unit, self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP
            )

        # ``use_last_error`` is required for ctypes.get_last_error() to report
        # the Win32 failure instead of the stale/default value 0.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        )
        user32.SendInput.restype = wintypes.UINT
        ctypes.set_last_error(0)
        sent = user32.SendInput(len(inputs), inputs, ctypes.sizeof(_INPUT))
        if sent != len(inputs):
            error = ctypes.get_last_error()
            raise OSError(error, f"SendInput sent {sent}/{len(inputs)} keyboard events")


if os.name == "nt":
    _ULONG_PTR = wintypes.WPARAM

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = (
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        )

    class _INPUTUNION(ctypes.Union):
        # INPUT's size is determined by its largest union member (MOUSEINPUT),
        # even when only keyboard events are sent.  Omitting these native
        # branches makes cbSize too small and SendInput returns zero.
        _fields_ = (
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
            ("hi", _HARDWAREINPUT),
        )

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = (("type", wintypes.DWORD), ("union", _INPUTUNION))

    def _keyboard_input(scan: int, flags: int) -> _INPUT:
        return _INPUT(
            type=WindowsUnicodeTextInjector.INPUT_KEYBOARD,
            ki=_KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0),
        )
else:  # pragma: no cover - keeps the module importable for docs/tests elsewhere
    _INPUT = object  # type: ignore[assignment,misc]


class FloatingTranscriptOverlay:
    """A queue-driven Tk overlay owned entirely by its background UI thread."""

    _POLL_MS = 30
    _FINAL_HIDE_MS = 1800
    _ERROR_HIDE_MS = 3500

    def __init__(self, *, on_error: Callable[[str], None] = print) -> None:
        self._on_error = on_error
        self._events: queue.SimpleQueue[tuple[str, str] | None] = queue.SimpleQueue()
        self._ready = threading.Event()
        self._failed: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="ProxiMicTranscriptOverlay",
            daemon=True,
        )
        self._thread.start()
        # Do not hold up microphone/model startup if Tk is slow to initialize.
        self._ready.wait(timeout=2.0)

    def show_partial(self, text: str) -> None:
        self._publish("partial", text)

    def show_final(self, text: str) -> None:
        self._publish("final", text)

    def show_error(self, message: str) -> None:
        self._publish("error", message)

    def close(self) -> None:
        self._events.put(None)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    def _publish(self, kind: str, text: str) -> None:
        if self._failed is not None:
            return
        self._events.put((kind, str(text or "")))

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)
            root.configure(bg="#17191d")
            root.attributes("-topmost", True)
            try:
                root.attributes("-alpha", 0.94)
            except tk.TclError:
                pass

            label = tk.Label(
                root,
                text="",
                bg="#17191d",
                fg="#f1f3f5",
                font=("Microsoft YaHei UI", 15),
                padx=22,
                pady=14,
                justify="left",
                wraplength=760,
            )
            label.pack()
            root.update_idletasks()
            self._make_no_activate(root)
            hide_generation = [0]

            def hide_later(delay_ms: int) -> None:
                hide_generation[0] += 1
                generation = hide_generation[0]

                def hide() -> None:
                    if generation == hide_generation[0]:
                        root.withdraw()

                root.after(delay_ms, hide)

            def display(kind: str, text: str) -> None:
                hide_generation[0] += 1
                if kind == "partial":
                    label.configure(text=text or "正在聆听…", fg="#f1f3f5")
                elif kind == "final":
                    label.configure(text=text, fg="#8ce99a")
                else:
                    label.configure(text=f"桌面输入失败：{text}", fg="#ff8787")
                root.update_idletasks()
                width = min(max(label.winfo_reqwidth(), 320), 820)
                height = label.winfo_reqheight()
                x = max(12, (root.winfo_screenwidth() - width) // 2)
                y = max(12, root.winfo_screenheight() - height - 110)
                root.geometry(f"{width}x{height}+{x}+{y}")
                root.deiconify()
                root.lift()
                self._make_no_activate(root)
                if kind == "final":
                    hide_later(self._FINAL_HIDE_MS)
                elif kind == "error":
                    hide_later(self._ERROR_HIDE_MS)

            def poll() -> None:
                while True:
                    try:
                        event = self._events.get_nowait()
                    except queue.Empty:
                        break
                    if event is None:
                        root.destroy()
                        return
                    display(*event)
                root.after(self._POLL_MS, poll)

            self._ready.set()
            root.after(self._POLL_MS, poll)
            root.mainloop()
        except BaseException as exc:
            self._failed = str(exc)
            self._ready.set()
            self._on_error(f"[desktop-output] transcript overlay unavailable: {exc}")

    @staticmethod
    def _make_no_activate(root) -> None:
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = root.winfo_id()
            # Tk may wrap the widget HWND in a native top-level frame.  Window
            # styles must be applied to that outer frame to prevent activation.
            user32.GetParent.argtypes = (wintypes.HWND,)
            user32.GetParent.restype = wintypes.HWND
            user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowPos.argtypes = (
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            )
            user32.SetWindowPos.restype = wintypes.BOOL
            parent = user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            exstyle = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            user32.SetWindowLongW(hwnd, -20, exstyle | 0x08000000 | 0x00000080)
            user32.SetWindowPos(
                hwnd,
                wintypes.HWND(-1),  # HWND_TOPMOST
                0,
                0,
                0,
                0,
                0x0001 | 0x0002 | 0x0010,
            )
        except BaseException:
            # Preview is optional; injection still works if a window manager
            # rejects one of the non-activation style calls.
            return


class DesktopTranscriptOutput:
    """Consume normalized ASR updates and commit each final result exactly once."""

    def __init__(
        self,
        *,
        backend: str,
        injector: TextInjector | None = None,
        overlay: TranscriptOverlay | None = None,
        on_error: Callable[[str], None] = print,
        should_inject: Callable[[], bool] | None = None,
    ) -> None:
        self.backend = str(backend)
        self.injector = injector or WindowsUnicodeTextInjector()
        self.overlay = overlay or FloatingTranscriptOverlay(on_error=on_error)
        self.on_error = on_error
        self.should_inject = should_inject or (lambda: True)
        self._committed: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def __call__(self, update) -> None:
        if str(update.backend) != self.backend:
            return
        if update.error:
            self.overlay.show_error(str(update.error))
            return

        text = str(update.text or "").strip()
        if not update.is_final:
            if text:
                self.overlay.show_partial(text)
            return

        session_id = int(getattr(update, "session_id", 0))
        key = (str(update.backend), session_id)
        with self._lock:
            if key in self._committed:
                return
            self._committed.add(key)
            # A long-running dictation process should not accumulate one set
            # entry forever for every past utterance.
            if len(self._committed) > 2048:
                current = key
                self._committed.clear()
                self._committed.add(current)

        if not text:
            return
        try:
            should_inject = bool(self.should_inject())
        except BaseException as exc:
            self.on_error(f"[desktop-output] could not inspect foreground window: {exc}")
            should_inject = False
        if not should_inject:
            # The customer UI already appends this result to its own editor.
            # Avoid sending keystrokes back into a focused UI field and
            # duplicating or corrupting its contents.
            self.overlay.show_final(text)
            return
        try:
            self.injector.inject(text)
        except BaseException as exc:
            message = str(exc)
            self.overlay.show_error(message)
            self.on_error(f"[desktop-output] final text was not injected: {message}")
            return
        self.overlay.show_final(text)

    def close(self) -> None:
        self.overlay.close()


class DesktopOutputLifecycleSink:
    """Tie a desktop output object's lifetime to an ASR session fanout."""

    def __init__(self, output: DesktopTranscriptOutput) -> None:
        self.output = output

    def start(self, _initial_audio_16k) -> None:
        return None

    def feed(self, _audio_16k) -> None:
        return None

    def end(self, _final_audio_16k) -> None:
        return None

    def close(self) -> None:
        self.output.close()
