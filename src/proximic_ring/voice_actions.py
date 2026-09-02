"""Global keyboard adapters for voice interaction actions.

These action names are deliberately device-neutral.  Ring gestures can emit
the same values later without knowing anything about QML or desktop text APIs.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from typing import Callable


if os.name == "nt":
    from ctypes import wintypes


ACTION_INPUT = "input"
ACTION_EDIT = "edit"
ACTION_CONFIRM = "confirm"
ACTION_CANCEL = "cancel"
ACTION_REASON_ASR_ERROR = "reason_asr_error"
ACTION_REASON_LLM_ERROR = "reason_llm_error"
ACTION_REASON_OTHER = "reason_other"


if os.name == "nt":
    _ULONG_PTR = wintypes.WPARAM
    _LRESULT = ctypes.c_ssize_t

    class _KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = (
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    _HOOKPROC = ctypes.WINFUNCTYPE(
        _LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )


class WindowsVoiceActionHotkeys:
    """Map global shortcuts to the same actions future Ring gestures use."""

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_QUIT = 0x0012
    PM_NOREMOVE = 0x0000
    VK_MENU = 0x12
    _ALT_ACTIONS = {
        0x31: ACTION_INPUT,      # Alt+1
        0x32: ACTION_EDIT,       # Alt+2
    }
    _REVIEW_ACTIONS = {
        0x0D: ACTION_CONFIRM,    # Enter while a review is visible
        0x1B: ACTION_CANCEL,     # Escape while a review is visible
    }
    _FEEDBACK_REASON_ACTIONS = {
        0x41: ACTION_REASON_ASR_ERROR,  # Alt+A
        0x4C: ACTION_REASON_LLM_ERROR,  # Alt+L
        0x4F: ACTION_REASON_OTHER,      # Alt+O
    }

    def __init__(
        self,
        on_action: Callable[[str], None],
        *,
        is_review_active: Callable[[], bool] | None = None,
        is_feedback_reason_active: Callable[[], bool] | None = None,
        on_error: Callable[[str], None] = print,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("全局语音动作快捷键目前仅支持 Windows")
        self._on_action = on_action
        self._is_review_active = is_review_active or (lambda: False)
        self._is_feedback_reason_active = (
            is_feedback_reason_active or (lambda: False)
        )
        self._on_error = on_error
        self._ready = threading.Event()
        self._thread_id = 0
        self._failed: str | None = None
        self._pressed_actions: set[int] = set()
        self._thread = threading.Thread(
            target=self._run,
            name="ProxiMicVoiceActions",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=3.0):
            raise RuntimeError("安装全局语音动作快捷键超时")
        if self._failed:
            raise RuntimeError(self._failed)

    def close(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, self.WM_QUIT, 0, 0
            )
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        hook = None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._configure_win32(user32, kernel32)
            self._thread_id = int(kernel32.GetCurrentThreadId())

            def callback(n_code, w_param, l_param):
                if n_code >= 0:
                    data = ctypes.cast(
                        l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)
                    ).contents
                    key = int(data.vkCode)
                    message = int(w_param)
                    is_down = message in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
                    is_up = message in (self.WM_KEYUP, self.WM_SYSKEYUP)
                    action = self._action_for_key(
                        key,
                        alt_down=self._alt_down(user32),
                        review_active=self._is_review_active(),
                        feedback_reason_active=self._is_feedback_reason_active(),
                    )
                    if is_up:
                        was_consumed = key in self._pressed_actions
                        self._pressed_actions.discard(key)
                        if was_consumed:
                            return 1
                    if action and is_down:
                        if key not in self._pressed_actions:
                            self._pressed_actions.add(key)
                            try:
                                self._on_action(action)
                            except BaseException as exc:
                                self._on_error(f"[voice-actions] {exc}")
                        return 1
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            hook_proc = _HOOKPROC(callback)
            module = kernel32.GetModuleHandleW(None)
            hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, hook_proc, module, 0)
            if not hook:
                raise OSError(ctypes.get_last_error(), "SetWindowsHookExW failed")
            message = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, self.PM_NOREMOVE)
            self._ready.set()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._failed = f"global voice action hotkeys unavailable: {exc}"
            self._ready.set()
            self._on_error(f"[voice-actions] {self._failed}")
        finally:
            if hook:
                try:
                    user32.UnhookWindowsHookEx(hook)
                except BaseException:
                    pass
            self._thread_id = 0

    def _alt_down(self, user32) -> bool:
        return bool(user32.GetAsyncKeyState(self.VK_MENU) & 0x8000)

    @classmethod
    def _action_for_key(
        cls,
        key: int,
        *,
        alt_down: bool,
        review_active: bool,
        feedback_reason_active: bool,
    ) -> str | None:
        if alt_down and feedback_reason_active:
            reason_action = cls._FEEDBACK_REASON_ACTIONS.get(int(key))
            if reason_action is not None:
                return reason_action
        if alt_down:
            action = cls._ALT_ACTIONS.get(int(key))
            if action is not None:
                return action
        if review_active:
            return cls._REVIEW_ACTIONS.get(int(key))
        return None

    @staticmethod
    def _configure_win32(user32, kernel32) -> None:
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int,
            _HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        )
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = (
            wintypes.HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.CallNextHookEx.restype = _LRESULT
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        )
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        user32.DispatchMessageW.restype = _LRESULT
        kernel32.GetCurrentThreadId.argtypes = ()
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class MacOSVoiceActionHotkeys:
    """Consume edit confirmation keys while another macOS app has focus."""

    KEY_RETURN = 36
    KEY_ESCAPE = 53
    KEY_KEYPAD_ENTER = 76

    def __init__(
        self,
        on_action: Callable[[str], None],
        *,
        is_review_active: Callable[[], bool] | None = None,
        on_error: Callable[[str], None] = print,
        quartz: object | None = None,
        core_foundation: object | None = None,
    ) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("macOS 全局编辑确认键仅支持 macOS")
        if quartz is None:
            import Quartz as quartz_module

            quartz = quartz_module
        if core_foundation is None:
            import CoreFoundation as core_foundation_module

            core_foundation = core_foundation_module
        self._quartz = quartz
        self._core_foundation = core_foundation
        self._on_action = on_action
        self._is_review_active = is_review_active or (lambda: False)
        self._on_error = on_error
        self._ready = threading.Event()
        self._failed: str | None = None
        self._tap = None
        self._run_loop = None
        self._source = None
        self._callback = None
        self._thread = threading.Thread(
            target=self._run,
            name="ProxiMicMacVoiceActions",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=3.0):
            raise RuntimeError("安装 macOS 全局编辑确认键超时")
        if self._failed:
            raise RuntimeError(self._failed)

    @classmethod
    def _action_for_key(cls, key_code: int, *, review_active: bool) -> str | None:
        if not review_active:
            return None
        if int(key_code) in (cls.KEY_RETURN, cls.KEY_KEYPAD_ENTER):
            return ACTION_CONFIRM
        if int(key_code) == cls.KEY_ESCAPE:
            return ACTION_CANCEL
        return None

    def _run(self) -> None:
        quartz = self._quartz
        cf = self._core_foundation
        try:
            disabled_types = {
                int(quartz.kCGEventTapDisabledByTimeout),
                int(quartz.kCGEventTapDisabledByUserInput),
            }

            def callback(proxy, event_type, event, refcon):
                try:
                    if int(event_type) in disabled_types:
                        quartz.CGEventTapEnable(self._tap, True)
                        return event
                    if int(event_type) != int(quartz.kCGEventKeyDown):
                        return event
                    repeat_field = getattr(
                        quartz, "kCGKeyboardEventAutorepeat", None
                    )
                    if repeat_field is not None and quartz.CGEventGetIntegerValueField(
                        event, repeat_field
                    ):
                        return None if self._is_review_active() else event
                    key_code = quartz.CGEventGetIntegerValueField(
                        event, quartz.kCGKeyboardEventKeycode
                    )
                    action = self._action_for_key(
                        int(key_code),
                        review_active=self._is_review_active(),
                    )
                    if action is None:
                        return event
                    self._on_action(action)
                    # A session event tap can consume the key, preventing
                    # Return/Escape from also changing the external editor.
                    return None
                except BaseException as exc:
                    self._on_error(
                        f"[voice-actions] macOS 编辑确认键处理失败：{exc}"
                    )
                    return event

            self._callback = callback
            mask = quartz.CGEventMaskBit(quartz.kCGEventKeyDown)
            self._tap = quartz.CGEventTapCreate(
                quartz.kCGSessionEventTap,
                quartz.kCGHeadInsertEventTap,
                quartz.kCGEventTapOptionDefault,
                mask,
                callback,
                None,
            )
            if self._tap is None:
                raise RuntimeError(
                    "无法安装系统按键监听，请在系统设置中允许辅助功能权限"
                )
            self._source = quartz.CFMachPortCreateRunLoopSource(
                None, self._tap, 0
            )
            self._run_loop = cf.CFRunLoopGetCurrent()
            cf.CFRunLoopAddSource(
                self._run_loop, self._source, cf.kCFRunLoopCommonModes
            )
            quartz.CGEventTapEnable(self._tap, True)
            self._ready.set()
            cf.CFRunLoopRun()
        except BaseException as exc:
            self._failed = f"macOS global edit keys unavailable: {exc}"
            self._ready.set()
            self._on_error(f"[voice-actions] {self._failed}")
        finally:
            if self._tap is not None:
                try:
                    quartz.CFMachPortInvalidate(self._tap)
                except BaseException:
                    pass
            self._tap = None
            self._source = None
            self._run_loop = None

    def close(self) -> None:
        run_loop = self._run_loop
        if run_loop is not None:
            self._core_foundation.CFRunLoopStop(run_loop)
            self._core_foundation.CFRunLoopWakeUp(run_loop)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
