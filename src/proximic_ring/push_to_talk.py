"""Windows global hold-to-talk control for ProxiMic's existing ASR session."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
from typing import Callable


class PushToTalkState:
    """Small thread-safe state shared by the keyboard hook and audio loop."""

    def __init__(self, *, on_change: Callable[[bool], None] | None = None) -> None:
        self._active = threading.Event()
        self._on_change = on_change
        self._lock = threading.Lock()

    def is_active(self) -> bool:
        return self._active.is_set()

    def set_active(self, active: bool) -> None:
        active = bool(active)
        callback = None
        with self._lock:
            if active == self._active.is_set():
                return
            if active:
                self._active.set()
            else:
                self._active.clear()
            callback = self._on_change
        if callback is not None:
            callback(active)


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
        _LRESULT,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )


class WindowsPushToTalkHotkey:
    """Hold Ctrl+Alt+Space globally to force the current ASR session active.

    Only the Space key event is consumed while the chord is active.  Ctrl and
    Alt continue through the normal Windows input path.  Releasing Space or
    either modifier immediately releases the manual hold.
    """

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_QUIT = 0x0012
    PM_NOREMOVE = 0x0000

    VK_SPACE = 0x20
    VK_CONTROL = 0x11
    VK_MENU = 0x12
    _CTRL_KEYS = frozenset((0x11, 0xA2, 0xA3))
    _ALT_KEYS = frozenset((0x12, 0xA4, 0xA5))

    display_name = "Ctrl+Alt+Space"

    def __init__(
        self,
        *,
        on_error: Callable[[str], None] = print,
        on_change: Callable[[bool], None] | None = None,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("push-to-talk is currently supported only on Windows")
        self._on_error = on_error
        self._on_change = on_change
        self.state = PushToTalkState(on_change=self._report_change)
        self._ready = threading.Event()
        self._thread_id = 0
        self._failed: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="ProxiMicPushToTalk",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=3.0):
            raise RuntimeError("timed out while installing the global push-to-talk hotkey")
        if self._failed is not None:
            raise RuntimeError(self._failed)

    def is_active(self) -> bool:
        return self.state.is_active()

    def close(self) -> None:
        self.state.set_active(False)
        thread_id = self._thread_id
        if thread_id:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)

    # SessionSink-compatible no-ops let the ASR fanout own this resource and
    # close the global hook after all queued final results have completed.
    def start(self, _initial_audio_16k) -> None:
        return None

    def feed(self, _audio_16k) -> None:
        return None

    def end(self, _final_audio_16k) -> None:
        return None

    def _report_change(self, active: bool) -> None:
        action = "HOLD -> manual listening" if active else "RELEASE -> automatic control"
        print(f"[PTT] {self.display_name} {action}", flush=True)
        if self._on_change is not None:
            self._on_change(active)

    def _run(self) -> None:
        hook = None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._configure_win32(user32, kernel32)
            self._thread_id = int(kernel32.GetCurrentThreadId())

            def callback(n_code, w_param, l_param):
                try:
                    if n_code >= 0:
                        data = ctypes.cast(
                            l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)
                        ).contents
                        vk = int(data.vkCode)
                        message = int(w_param)
                        is_down = message in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
                        is_up = message in (self.WM_KEYUP, self.WM_SYSKEYUP)

                        if vk == self.VK_SPACE:
                            modifiers_down = self._key_down(
                                user32, self.VK_CONTROL
                            ) and self._key_down(user32, self.VK_MENU)
                            if is_down and modifiers_down:
                                self.state.set_active(True)
                                return 1
                            if is_up and self.state.is_active():
                                self.state.set_active(False)
                                return 1
                        elif is_up and self.state.is_active() and (
                            vk in self._CTRL_KEYS or vk in self._ALT_KEYS
                        ):
                            self.state.set_active(False)
                except BaseException as exc:
                    self._on_error(f"[PTT] keyboard hook event failed: {exc}")
                return user32.CallNextHookEx(None, n_code, w_param, l_param)

            hook_proc = _HOOKPROC(callback)
            ctypes.set_last_error(0)
            module = kernel32.GetModuleHandleW(None)
            hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, hook_proc, module, 0)
            if not hook:
                error = ctypes.get_last_error()
                raise OSError(error, "SetWindowsHookExW failed")

            # Ensure this thread owns a message queue before close() may post
            # WM_QUIT, then publish readiness to the caller.
            message = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(message), None, 0, 0, self.PM_NOREMOVE)
            self._ready.set()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    error = ctypes.get_last_error()
                    raise OSError(error, "GetMessageW failed")
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._failed = f"global push-to-talk unavailable: {exc}"
            self._ready.set()
            self._on_error(f"[PTT] {self._failed}")
        finally:
            self.state.set_active(False)
            if hook:
                try:
                    user32.UnhookWindowsHookEx(hook)
                except BaseException:
                    pass
            self._thread_id = 0

    @staticmethod
    def _key_down(user32, vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

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
