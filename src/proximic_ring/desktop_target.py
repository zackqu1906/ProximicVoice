"""Platform adapter for reading and updating the focused desktop text field.

The voice interaction layer deals only in immutable target references and text
snapshots.  All Win32 focus, keyboard and clipboard details stay here so a
future macOS adapter, IME adapter, or accessibility implementation can replace
this module without changing ASR or LLM code.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import time
from typing import Protocol

from .desktop_output import WindowsUnicodeTextInjector


class ClipboardBridge(Protocol):
    """Small clipboard boundary supplied by the UI toolkit."""

    def snapshot(self) -> object: ...
    def restore(self, snapshot: object) -> None: ...
    def text(self) -> str: ...


@dataclass(frozen=True)
class DesktopTargetRef:
    window_handle: int
    control_handle: int
    window_title: str = ""


@dataclass(frozen=True)
class DesktopTextSnapshot:
    target: DesktopTargetRef
    text: str


class DesktopTextTarget(Protocol):
    def capture_reference(self) -> DesktopTargetRef: ...
    def capture_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot: ...
    def inject(self, target: DesktopTargetRef, text: str) -> None: ...
    def replace(self, snapshot: DesktopTextSnapshot, text: str) -> None: ...
    def release_selection(self, target: DesktopTargetRef) -> None: ...


if os.name == "nt":
    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        )


class WindowsDesktopTextTarget:
    """Best-effort adapter for ordinary Windows text controls.

    Reading uses the control's normal Select-All/Copy behavior and immediately
    restores every MIME payload exposed by Qt's clipboard bridge.  Applying a
    modification returns to the locked control and replaces its complete text
    only after the interaction layer receives explicit confirmation.
    """

    VK_CONTROL = 0x11
    VK_A = 0x41
    VK_C = 0x43
    VK_BACK = 0x08
    VK_END = 0x23
    KEYEVENTF_KEYUP = 0x0002

    def __init__(
        self,
        clipboard: ClipboardBridge,
        *,
        injector: WindowsUnicodeTextInjector | None = None,
        own_process_id: int | None = None,
        copy_timeout_s: float = 0.6,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("跨应用文本目标目前仅支持 Windows")
        self._clipboard = clipboard
        self._injector = injector or WindowsUnicodeTextInjector()
        self._own_process_id = int(own_process_id or os.getpid())
        self._copy_timeout_s = max(0.1, float(copy_timeout_s))
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_win32()

    def capture_reference(self) -> DesktopTargetRef:
        window = int(self._user32.GetForegroundWindow() or 0)
        if not window:
            raise RuntimeError("没有检测到前台窗口")
        process_id = wintypes.DWORD()
        thread_id = int(
            self._user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        )
        if int(process_id.value) == self._own_process_id:
            raise RuntimeError("请先把光标放到其他应用的文本框中")

        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        if (
            not thread_id
            or not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info))
            or not info.hwndFocus
        ):
            raise RuntimeError("无法锁定当前文本框，请重新点击文本框后重试")
        focus = int(info.hwndFocus)
        title_length = int(self._user32.GetWindowTextLengthW(window))
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        if title_length:
            self._user32.GetWindowTextW(window, title_buffer, title_length + 1)
        return DesktopTargetRef(window, focus, title_buffer.value.strip())

    def capture_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot:
        self._activate(target)
        clipboard_snapshot = self._clipboard.snapshot()
        sequence_before = int(self._user32.GetClipboardSequenceNumber())
        try:
            self._hotkey(self.VK_CONTROL, self.VK_A)
            self._hotkey(self.VK_CONTROL, self.VK_C)
            deadline = time.monotonic() + self._copy_timeout_s
            while time.monotonic() < deadline:
                if int(self._user32.GetClipboardSequenceNumber()) != sequence_before:
                    break
                time.sleep(0.01)
            text = str(self._clipboard.text() or "")
        finally:
            self._clipboard.restore(clipboard_snapshot)
        if not text:
            raise RuntimeError("当前文本框为空，或该控件不支持读取文本")
        return DesktopTextSnapshot(target=target, text=text)

    def inject(self, target: DesktopTargetRef, text: str) -> None:
        value = str(text or "")
        if not value:
            return
        self._activate(target)
        self._injector.inject(value)

    def replace(self, snapshot: DesktopTextSnapshot, text: str) -> None:
        replacement = str(text or "")
        self._activate(snapshot.target)
        # Re-select the complete field because clicking the background control
        # window may have collapsed the selection while the preview was open.
        # We intentionally do not copy/compare again: browser content-editable
        # controls often expose different clipboard representations after a
        # focus round trip even though their visible text did not change.
        self._hotkey(self.VK_CONTROL, self.VK_A)
        if replacement:
            self._injector.inject(replacement)
        else:
            self._press_key(self.VK_BACK)

    def release_selection(self, target: DesktopTargetRef) -> None:
        try:
            self._activate(target)
            self._press_key(self.VK_END)
        except BaseException:
            return

    def _activate(self, target: DesktopTargetRef) -> None:
        window = wintypes.HWND(int(target.window_handle))
        control = wintypes.HWND(int(target.control_handle))
        if not self._user32.IsWindow(window):
            raise RuntimeError("原文本窗口已经关闭")
        if control and not self._user32.IsWindow(control):
            raise RuntimeError("原文本框已经失效")

        self._user32.SetForegroundWindow(window)
        self._user32.BringWindowToTop(window)
        target_thread = int(self._user32.GetWindowThreadProcessId(window, None))
        current_thread = int(self._kernel32.GetCurrentThreadId())
        attached = False
        if target_thread and target_thread != current_thread:
            attached = bool(
                self._user32.AttachThreadInput(current_thread, target_thread, True)
            )
        try:
            if control:
                self._user32.SetFocus(control)
        finally:
            if attached:
                self._user32.AttachThreadInput(current_thread, target_thread, False)
        time.sleep(0.035)

    def _hotkey(self, *virtual_keys: int) -> None:
        for key in virtual_keys:
            self._user32.keybd_event(int(key), 0, 0, 0)
        for key in reversed(virtual_keys):
            self._user32.keybd_event(int(key), 0, self.KEYEVENTF_KEYUP, 0)

    def _press_key(self, virtual_key: int) -> None:
        self._user32.keybd_event(int(virtual_key), 0, 0, 0)
        self._user32.keybd_event(int(virtual_key), 0, self.KEYEVENTF_KEYUP, 0)

    def _configure_win32(self) -> None:
        self._user32.GetForegroundWindow.argtypes = ()
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetGUIThreadInfo.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(_GUITHREADINFO),
        )
        self._user32.GetGUIThreadInfo.restype = wintypes.BOOL
        self._user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = (
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        )
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.IsWindow.argtypes = (wintypes.HWND,)
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self._user32.SetForegroundWindow.restype = wintypes.BOOL
        self._user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        self._user32.BringWindowToTop.restype = wintypes.BOOL
        self._user32.AttachThreadInput.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        )
        self._user32.AttachThreadInput.restype = wintypes.BOOL
        self._user32.SetFocus.argtypes = (wintypes.HWND,)
        self._user32.SetFocus.restype = wintypes.HWND
        self._user32.GetClipboardSequenceNumber.argtypes = ()
        self._user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        self._user32.keybd_event.argtypes = (
            wintypes.BYTE,
            wintypes.BYTE,
            wintypes.DWORD,
            wintypes.WPARAM,
        )
        self._kernel32.GetCurrentThreadId.argtypes = ()
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
