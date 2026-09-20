"""Shared desktop target data and the Windows text-field adapter.

macOS text sessions are owned by the native input-method component. Immutable
target references and snapshots remain shared with the interaction layer;
Win32 focus, keyboard, clipboard and UI Automation details stay here.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
import os
import time
from typing import Protocol
import unicodedata
import uuid

from .desktop_output import WindowsUnicodeTextInjector
from .windows_uia import UIATextControlRef, WindowsUIATextBridge


if os.name == "nt":
    from ctypes import wintypes


class ClipboardBridge(Protocol):
    """Small clipboard boundary supplied by the UI toolkit."""

    def snapshot(self) -> object: ...
    def restore(self, snapshot: object) -> None: ...
    def set_text(self, text: str) -> None: ...
    def text(self) -> str: ...


@dataclass(frozen=True)
class DesktopTargetRef:
    window_handle: int
    control_handle: int
    window_title: str = ""
    process_id: int = 0
    process_name: str = ""
    uia_control: UIATextControlRef | None = None
    screen_x: int = 0
    screen_y: int = 0
    screen_width: int = 0
    screen_height: int = 0
    caret_x: int = 0
    caret_y: int = 0
    caret_width: int = 0
    caret_height: int = 0
    accessibility_id: str = ""


@dataclass(frozen=True)
class DesktopTextSnapshot:
    target: DesktopTargetRef
    text: str


class DesktopTextTarget(Protocol):
    def capture_reference(self) -> DesktopTargetRef: ...
    def capture_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot: ...
    def observe_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot: ...
    def inject(self, target: DesktopTargetRef, text: str) -> None: ...
    def replace(self, snapshot: DesktopTextSnapshot, text: str) -> None: ...
    def undo(self, target: DesktopTargetRef) -> None: ...
    def send_native_undo(self, target: DesktopTargetRef) -> None: ...
    def release_selection(self, target: DesktopTargetRef) -> None: ...
    def is_foreground(self, target: DesktopTargetRef) -> bool: ...
    def is_application_foreground(self, target: DesktopTargetRef) -> bool: ...
    def caret_bounds(self, target: DesktopTargetRef) -> tuple[int, int, int, int]: ...


def macos_texts_equivalent(actual: str, expected: str) -> bool:
    """Compare user-visible macOS text without transport-only differences."""

    def canonical(value: str) -> str:
        normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.replace("\u2028", "\n").replace("\u2029", "\n")
        return unicodedata.normalize("NFC", normalized)

    return canonical(actual) == canonical(expected)


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

    @staticmethod
    def _uia_control_id(control: UIATextControlRef | None) -> str:
        if control is None:
            return ""
        return "uia:" + ".".join(str(value) for value in control.runtime_id)

    def __init__(
        self,
        clipboard: ClipboardBridge,
        *,
        injector: WindowsUnicodeTextInjector | None = None,
        own_process_id: int | None = None,
        copy_timeout_s: float = 0.6,
        copy_attempts: int = 3,
        focus_settle_s: float = 0.08,
        shortcut_settle_s: float = 0.03,
        uia_bridge: WindowsUIATextBridge | None = None,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("跨应用文本目标目前仅支持 Windows")
        self._clipboard = clipboard
        self._injector = injector or WindowsUnicodeTextInjector()
        self._own_process_id = int(own_process_id or os.getpid())
        self._copy_timeout_s = max(0.1, float(copy_timeout_s))
        self._copy_attempts = max(1, int(copy_attempts))
        self._focus_settle_s = max(0.02, float(focus_settle_s))
        self._shortcut_settle_s = max(0.01, float(shortcut_settle_s))
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_win32()
        if uia_bridge is not None:
            self._uia = uia_bridge
        else:
            try:
                self._uia = WindowsUIATextBridge()
            except Exception:
                self._uia = None

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
        process_name = self._process_name(int(process_id.value))
        uia_control = None
        focused_uia_control = None
        if self._uia is not None:
            try:
                focused_uia_control = self._uia.capture_focused_text_control(
                    int(process_id.value)
                )
            except Exception:
                focused_uia_control = None
        # Preserve the established clipboard behavior for WeChat and browsers.
        # Codex needs UIA because its native focus HWND represents the entire
        # Chromium renderer rather than the ProseMirror composer.
        if (
            process_name.casefold() == "chatgpt.exe"
            and self._uia is not None
        ):
            uia_control = focused_uia_control
        title_length = int(self._user32.GetWindowTextLengthW(window))
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        if title_length:
            self._user32.GetWindowTextW(window, title_buffer, title_length + 1)
        bounds = wintypes.RECT()
        bounds_handle = focus if self._user32.GetWindowRect(focus, ctypes.byref(bounds)) else window
        if bounds_handle == window:
            self._user32.GetWindowRect(window, ctypes.byref(bounds))
        target = DesktopTargetRef(
            window,
            focus,
            title_buffer.value.strip(),
            int(process_id.value),
            process_name,
            uia_control,
            int(bounds.left),
            int(bounds.top),
            max(0, int(bounds.right - bounds.left)),
            max(0, int(bounds.bottom - bounds.top)),
            accessibility_id=self._uia_control_id(focused_uia_control),
        )
        caret = self.caret_bounds(target)
        if caret[3] > 0:
            target = replace(
                target,
                caret_x=caret[0],
                caret_y=caret[1],
                caret_width=max(2, caret[2]),
                caret_height=max(1, caret[3]),
            )
        return target

    def capture_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot:
        if target.uia_control is not None:
            if self._uia is None:
                raise RuntimeError(
                    "当前应用的精确文本框读取组件不可用；为避免复制整页内容，"
                    "本次修改已停止"
                )
            try:
                text = self._uia.read_text(
                    target.uia_control,
                    target.window_handle,
                )
            except Exception as exc:
                raise RuntimeError(
                    "无法读取锁定的应用输入框；为避免复制整页或历史对话，"
                    "本次修改已停止"
                ) from exc
            if not text:
                raise RuntimeError("当前输入框为空，没有可修改的文本")
            return DesktopTextSnapshot(target=target, text=text)

        if target.process_name.casefold() == "chatgpt.exe":
            raise RuntimeError(
                "Codex 提问框没有被精确锁定；为避免复制历史对话，本次修改已停止。"
                "请重新点击提问框后重试"
            )
        clipboard_snapshot = self._clipboard.snapshot()
        text = ""
        try:
            for attempt in range(self._copy_attempts):
                self._activate(target)
                sentinel = f"__PROXIMIC_COPY_{uuid.uuid4().hex}__"
                set_text = getattr(self._clipboard, "set_text", None)
                if callable(set_text):
                    set_text(sentinel)
                    sequence_before = None
                else:
                    sequence_before = int(
                        self._user32.GetClipboardSequenceNumber()
                    )
                self._hotkey(self.VK_CONTROL, self.VK_A)
                time.sleep(self._shortcut_settle_s)
                self._hotkey(self.VK_CONTROL, self.VK_C)
                deadline = time.monotonic() + self._copy_timeout_s
                while time.monotonic() < deadline:
                    candidate = str(self._clipboard.text() or "")
                    sequence_changed = (
                        sequence_before is None
                        or int(self._user32.GetClipboardSequenceNumber())
                        != sequence_before
                    )
                    # Some controls clear the clipboard first and publish the
                    # copied text asynchronously. Do not treat that transient
                    # empty state as the final copy result.
                    if candidate and candidate != sentinel and sequence_changed:
                        text = candidate
                        break
                    time.sleep(0.01)
                if text:
                    break
                if attempt + 1 < self._copy_attempts:
                    time.sleep(0.04)
        finally:
            self._clipboard.restore(clipboard_snapshot)
        if not text:
            raise RuntimeError(
                "多次复制后仍未读取到文本；当前文本框可能为空、尚未获得焦点，"
                "或该控件不支持读取文本"
            )
        return DesktopTextSnapshot(target=target, text=text)

    def observe_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot:
        """Read text through UI Automation without sending Ctrl+A/Ctrl+C."""
        if self._uia is None:
            raise RuntimeError("当前文本框不支持无干扰读取")

        control = target.uia_control
        if control is None:
            foreground = int(self._user32.GetForegroundWindow() or 0)
            if foreground != int(target.window_handle):
                raise RuntimeError("目标文本框当前不在前台")
            process_id = wintypes.DWORD()
            thread_id = int(
                self._user32.GetWindowThreadProcessId(
                    foreground, ctypes.byref(process_id)
                )
            )
            info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
            if (
                not thread_id
                or not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info))
                or int(info.hwndFocus or 0) != int(target.control_handle)
            ):
                raise RuntimeError("用户焦点已经离开原文本框")
            try:
                control = self._uia.capture_focused_text_control(
                    int(target.process_id)
                )
            except Exception as exc:
                raise RuntimeError("当前文本框不支持无干扰读取") from exc
            if control is None:
                raise RuntimeError("当前文本框不支持无干扰读取")

        try:
            text = self._uia.read_text(control, target.window_handle)
        except Exception as exc:
            raise RuntimeError("无法无干扰读取目标文本框") from exc
        return DesktopTextSnapshot(target=target, text=str(text or ""))

    def inject(self, target: DesktopTargetRef, text: str) -> None:
        value = str(text or "")
        if not value:
            return
        self._activate(target)
        self._injector.inject(value)

    def replace(self, snapshot: DesktopTextSnapshot, text: str) -> None:
        replacement = str(text or "")
        if snapshot.target.uia_control is not None:
            if self._uia is None:
                raise RuntimeError("锁定的 UI Automation 文本框不可用")
            try:
                self._activate(snapshot.target)
                self._uia.set_text(
                    snapshot.target.uia_control,
                    snapshot.target.window_handle,
                    replacement,
                )
                return
            except Exception as exc:
                raise RuntimeError(
                    "无法更新锁定的应用输入框；已禁止回退到整页 Ctrl+A"
                ) from exc
        self._activate(snapshot.target)
        # Re-select the complete field because clicking the background control
        # window may have collapsed the selection while the model was running.
        # We intentionally do not copy/compare again: browser content-editable
        # controls often expose different clipboard representations after a
        # focus round trip even though their visible text did not change.
        self._hotkey(self.VK_CONTROL, self.VK_A)
        time.sleep(getattr(self, "_shortcut_settle_s", 0.03))
        if replacement:
            self._injector.inject(replacement)
        else:
            self._press_key(self.VK_BACK)

    def undo(self, target: DesktopTargetRef) -> None:
        """Internal rollback: activate, undo, then settle before readback."""
        self._activate(target)
        self.send_native_undo(target)
        time.sleep(getattr(self, "_shortcut_settle_s", 0.03))

    def send_native_undo(self, target: DesktopTargetRef) -> None:
        """Post one Ctrl+Z after the caller checks focus; no activation/readback."""
        self._hotkey(self.VK_CONTROL, 0x5A)  # Z

    def release_selection(self, target: DesktopTargetRef) -> None:
        try:
            self._activate(target)
            self._press_key(self.VK_END)
        except BaseException:
            return

    def is_foreground(self, target: DesktopTargetRef) -> bool:
        foreground = int(self._user32.GetForegroundWindow() or 0)
        if foreground != int(target.window_handle):
            return False
        if target.accessibility_id.startswith("uia:") and self._uia is not None:
            try:
                focused = self._uia.capture_focused_text_control(
                    int(target.process_id)
                )
            except Exception:
                return False
            return self._uia_control_id(focused) == target.accessibility_id
        if target.uia_control is not None and self._uia is not None:
            try:
                focused = self._uia.capture_focused_text_control(
                    int(target.process_id)
                )
            except Exception:
                return False
            return bool(
                focused is not None
                and focused.runtime_id == target.uia_control.runtime_id
            )
        process_id = wintypes.DWORD()
        thread_id = int(
            self._user32.GetWindowThreadProcessId(
                foreground, ctypes.byref(process_id)
            )
        )
        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        if (
            not thread_id
            or not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info))
        ):
            return False
        return int(info.hwndFocus or 0) == int(target.control_handle)

    def is_application_foreground(self, target: DesktopTargetRef) -> bool:
        return int(self._user32.GetForegroundWindow() or 0) == int(
            target.window_handle
        )

    def caret_bounds(self, target: DesktopTargetRef) -> tuple[int, int, int, int]:
        """Return the current Win32 caret rectangle in global coordinates."""
        foreground = int(self._user32.GetForegroundWindow() or 0)
        if foreground != int(target.window_handle):
            return 0, 0, 0, 0
        process_id = wintypes.DWORD()
        thread_id = int(
            self._user32.GetWindowThreadProcessId(
                foreground, ctypes.byref(process_id)
            )
        )
        info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
        if (
            not thread_id
            or not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info))
        ):
            return 0, 0, 0, 0
        caret_window = int(info.hwndCaret or info.hwndFocus or 0)
        if not caret_window:
            return 0, 0, 0, 0
        top_left = wintypes.POINT(int(info.rcCaret.left), int(info.rcCaret.top))
        bottom_right = wintypes.POINT(
            int(info.rcCaret.right), int(info.rcCaret.bottom)
        )
        if not self._user32.ClientToScreen(
            caret_window, ctypes.byref(top_left)
        ) or not self._user32.ClientToScreen(
            caret_window, ctypes.byref(bottom_right)
        ):
            return 0, 0, 0, 0
        return (
            int(top_left.x),
            int(top_left.y),
            max(2, int(bottom_right.x - top_left.x)),
            max(1, int(bottom_right.y - top_left.y)),
        )

    def _activate(self, target: DesktopTargetRef) -> None:
        window = wintypes.HWND(int(target.window_handle))
        control = wintypes.HWND(int(target.control_handle))
        if not self._user32.IsWindow(window):
            raise RuntimeError("原文本窗口已经关闭")
        if control and not self._user32.IsWindow(control):
            raise RuntimeError("原文本框已经失效")

        self._user32.SetForegroundWindow(window)
        self._user32.BringWindowToTop(window)
        if target.uia_control is not None and self._uia is not None:
            try:
                self._uia.focus(target.uia_control, target.window_handle)
                time.sleep(getattr(self, "_focus_settle_s", 0.08))
                return
            except Exception as exc:
                if target.process_name.casefold() == "chatgpt.exe":
                    raise RuntimeError("无法重新聚焦 Codex 提问框") from exc
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
        # SetFocus and foreground activation are processed asynchronously by
        # the target GUI thread. Sending the first Unicode event immediately
        # can make some browsers/editors consume it during activation.
        time.sleep(getattr(self, "_focus_settle_s", 0.08))

    def _process_name(self, process_id: int) -> str:
        process = self._kernel32.OpenProcess(
            0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
            False,
            int(process_id),
        )
        if not process:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(int(size.value))
            if not self._kernel32.QueryFullProcessImageNameW(
                process,
                0,
                buffer,
                ctypes.byref(size),
            ):
                return ""
            return os.path.basename(buffer.value)
        finally:
            self._kernel32.CloseHandle(process)

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
        self._user32.GetWindowRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self._user32.GetWindowRect.restype = wintypes.BOOL
        self._user32.ClientToScreen.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        )
        self._user32.ClientToScreen.restype = wintypes.BOOL
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
        self._kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
