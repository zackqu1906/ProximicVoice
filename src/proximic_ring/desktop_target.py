"""Platform adapter for reading and updating the focused desktop text field.

The voice interaction layer deals only in immutable target references and text
snapshots.  All Win32 focus, keyboard and clipboard details stay here so a
future macOS adapter, IME adapter, or accessibility implementation can replace
this module without changing ASR or LLM code.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
import os
import sys
import time
from typing import Protocol
import unicodedata
import uuid

from .desktop_output import MacOSUnicodeTextInjector, WindowsUnicodeTextInjector
from .windows_uia import UIATextControlRef, WindowsUIATextBridge


if os.name == "nt":
    from ctypes import wintypes


class _CGPoint(ctypes.Structure):
    _fields_ = (("x", ctypes.c_double), ("y", ctypes.c_double))


class _CGSize(ctypes.Structure):
    _fields_ = (("width", ctypes.c_double), ("height", ctypes.c_double))


class _CGRect(ctypes.Structure):
    _fields_ = (("origin", _CGPoint), ("size", _CGSize))


class _CFRange(ctypes.Structure):
    _fields_ = (("location", ctypes.c_long), ("length", ctypes.c_long))


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
    def release_selection(self, target: DesktopTargetRef) -> None: ...
    def is_foreground(self, target: DesktopTargetRef) -> bool: ...
    def caret_bounds(self, target: DesktopTargetRef) -> tuple[int, int, int, int]: ...


def macos_texts_equivalent(actual: str, expected: str) -> bool:
    """Compare user-visible macOS text without transport-only differences."""

    def canonical(value: str) -> str:
        normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.replace("\u2028", "\n").replace("\u2029", "\n")
        return unicodedata.normalize("NFC", normalized)

    return canonical(actual) == canonical(expected)


class _MacOSAccessibilityTextBridge:
    """Read or replace the focused AXValue without depending on key timing.

    PyObjC's Quartz module does not expose the Accessibility API on every
    supported build, so this deliberately uses the stable C API. Controls
    which do not expose a string AXValue return ``None``/``False`` and the
    caller falls back to Select-All plus keyboard events.
    """

    _UTF8 = 0x08000100

    def __init__(self) -> None:
        application_services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        application_services.AXUIElementCreateApplication.argtypes = (ctypes.c_int,)
        application_services.AXUIElementCreateApplication.restype = ctypes.c_void_p
        application_services.AXUIElementCopyAttributeValue.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        application_services.AXUIElementCopyAttributeValue.restype = ctypes.c_int
        application_services.AXUIElementIsAttributeSettable.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_bool),
        )
        application_services.AXUIElementIsAttributeSettable.restype = ctypes.c_int
        application_services.AXUIElementSetAttributeValue.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        application_services.AXUIElementSetAttributeValue.restype = ctypes.c_int
        application_services.AXUIElementCopyParameterizedAttributeValue.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        application_services.AXUIElementCopyParameterizedAttributeValue.restype = (
            ctypes.c_int
        )
        application_services.AXValueGetValue.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
        )
        application_services.AXValueGetValue.restype = ctypes.c_bool
        application_services.AXValueCreate.argtypes = (
            ctypes.c_int,
            ctypes.c_void_p,
        )
        application_services.AXValueCreate.restype = ctypes.c_void_p
        core_foundation.CFStringCreateWithCString.argtypes = (
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )
        core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
        core_foundation.CFStringGetLength.argtypes = (ctypes.c_void_p,)
        core_foundation.CFStringGetLength.restype = ctypes.c_long
        core_foundation.CFStringGetMaximumSizeForEncoding.argtypes = (
            ctypes.c_long,
            ctypes.c_uint32,
        )
        core_foundation.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        core_foundation.CFStringGetCString.argtypes = (
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        )
        core_foundation.CFStringGetCString.restype = ctypes.c_bool
        core_foundation.CFGetTypeID.argtypes = (ctypes.c_void_p,)
        core_foundation.CFGetTypeID.restype = ctypes.c_ulong
        core_foundation.CFStringGetTypeID.argtypes = ()
        core_foundation.CFStringGetTypeID.restype = ctypes.c_ulong
        core_foundation.CFRelease.argtypes = (ctypes.c_void_p,)
        self._application_services = application_services
        self._core_foundation = core_foundation

    def read_focused_value(self, process_id: int) -> str | None:
        application, focused, value = self._focused_value(int(process_id))
        try:
            if not value:
                return None
            cf = self._core_foundation
            if cf.CFGetTypeID(value) != cf.CFStringGetTypeID():
                return None
            length = int(cf.CFStringGetLength(value))
            size = int(cf.CFStringGetMaximumSizeForEncoding(length, self._UTF8)) + 1
            buffer = ctypes.create_string_buffer(max(1, size))
            if not cf.CFStringGetCString(value, buffer, len(buffer), self._UTF8):
                return None
            return buffer.value.decode("utf-8")
        finally:
            self._release(value, focused, application)

    def set_focused_value(self, process_id: int, text: str) -> bool:
        application, focused, current_value = self._focused_value(int(process_id))
        value = None
        attribute = None
        try:
            if not focused:
                return False
            attribute = self._cf_string("AXValue")
            settable = ctypes.c_bool(False)
            error = self._application_services.AXUIElementIsAttributeSettable(
                focused, attribute, ctypes.byref(settable)
            )
            if error or not settable.value:
                return False
            value = self._cf_string(str(text or ""))
            return not self._application_services.AXUIElementSetAttributeValue(
                focused, attribute, value
            )
        finally:
            self._release(value, attribute, current_value, focused, application)

    def focused_selected_range(self, process_id: int) -> tuple[int, int] | None:
        """Read the current AX text selection without changing it."""
        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        focused = ctypes.c_void_p()
        value = ctypes.c_void_p()
        focused_attribute = None
        selected_attribute = None
        try:
            if not application:
                return None
            focused_attribute = self._cf_string("AXFocusedUIElement")
            if self._application_services.AXUIElementCopyAttributeValue(
                application, focused_attribute, ctypes.byref(focused)
            ) or not focused.value:
                return None
            selected_attribute = self._cf_string("AXSelectedTextRange")
            if self._application_services.AXUIElementCopyAttributeValue(
                focused.value, selected_attribute, ctypes.byref(value)
            ) or not value.value:
                return None
            selected_range = _CFRange()
            if not self._application_services.AXValueGetValue(
                value.value, 4, ctypes.byref(selected_range)
            ):
                return None
            return int(selected_range.location), int(selected_range.length)
        finally:
            self._release(
                value.value,
                selected_attribute,
                focused.value,
                focused_attribute,
                application,
            )

    def set_focused_selected_range(
        self, process_id: int, selection: tuple[int, int]
    ) -> bool:
        """Restore a previously captured AX text selection/caret."""
        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        focused = ctypes.c_void_p()
        value = None
        focused_attribute = None
        selected_attribute = None
        try:
            if not application:
                return False
            focused_attribute = self._cf_string("AXFocusedUIElement")
            if self._application_services.AXUIElementCopyAttributeValue(
                application, focused_attribute, ctypes.byref(focused)
            ) or not focused.value:
                return False
            selected_attribute = self._cf_string("AXSelectedTextRange")
            selected_range = _CFRange(
                max(0, int(selection[0])), max(0, int(selection[1]))
            )
            value = self._application_services.AXValueCreate(
                4, ctypes.byref(selected_range)
            )
            if not value:
                return False
            return not self._application_services.AXUIElementSetAttributeValue(
                focused.value, selected_attribute, value
            )
        finally:
            self._release(
                value,
                selected_attribute,
                focused.value,
                focused_attribute,
                application,
            )

    def focused_bounds(self, process_id: int) -> tuple[int, int, int, int]:
        """Return the focused accessibility element's global screen bounds."""
        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        focused = ctypes.c_void_p()
        focused_attribute = None
        position_attribute = None
        size_attribute = None
        position_value = ctypes.c_void_p()
        size_value = ctypes.c_void_p()
        try:
            if not application:
                return 0, 0, 0, 0
            focused_attribute = self._cf_string("AXFocusedUIElement")
            if self._application_services.AXUIElementCopyAttributeValue(
                application, focused_attribute, ctypes.byref(focused)
            ) or not focused.value:
                return 0, 0, 0, 0
            position_attribute = self._cf_string("AXPosition")
            size_attribute = self._cf_string("AXSize")
            if self._application_services.AXUIElementCopyAttributeValue(
                focused.value, position_attribute, ctypes.byref(position_value)
            ):
                return 0, 0, 0, 0
            if self._application_services.AXUIElementCopyAttributeValue(
                focused.value, size_attribute, ctypes.byref(size_value)
            ):
                return 0, 0, 0, 0
            point = _CGPoint()
            size = _CGSize()
            if not self._application_services.AXValueGetValue(
                position_value.value, 1, ctypes.byref(point)
            ):
                return 0, 0, 0, 0
            if not self._application_services.AXValueGetValue(
                size_value.value, 2, ctypes.byref(size)
            ):
                return 0, 0, 0, 0
            return (
                int(round(point.x)),
                int(round(point.y)),
                max(0, int(round(size.width))),
                max(0, int(round(size.height))),
            )
        finally:
            self._release(
                size_value.value,
                position_value.value,
                size_attribute,
                position_attribute,
                focused.value,
                focused_attribute,
                application,
            )

    def focused_caret_bounds(self, process_id: int) -> tuple[int, int, int, int]:
        """Return the caret bounds, walking out of nested web-editor children."""
        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        focused = ctypes.c_void_p()
        focused_attribute = None
        parent_attribute = None
        owned_parents: list[int] = []
        try:
            if not application:
                return 0, 0, 0, 0
            focused_attribute = self._cf_string("AXFocusedUIElement")
            if self._application_services.AXUIElementCopyAttributeValue(
                application, focused_attribute, ctypes.byref(focused)
            ) or not focused.value:
                return 0, 0, 0, 0
            parent_attribute = self._cf_string("AXParent")
            element = int(focused.value)
            # Chromium, Electron and WebKit can expose the caret range on the
            # editor, a nested text child, or one of its accessibility parents.
            for _depth in range(7):
                bounds = self._element_caret_bounds(element)
                if bounds[3] > 0:
                    return bounds
                parent = ctypes.c_void_p()
                if self._application_services.AXUIElementCopyAttributeValue(
                    element, parent_attribute, ctypes.byref(parent)
                ) or not parent.value:
                    break
                element = int(parent.value)
                owned_parents.append(element)
            return 0, 0, 0, 0
        finally:
            self._release(
                *reversed(owned_parents),
                parent_attribute,
                focused.value,
                focused_attribute,
                application,
            )

    def _element_caret_bounds(self, element: int) -> tuple[int, int, int, int]:
        selected_attribute = None
        bounds_attribute = None
        selected_value = ctypes.c_void_p()
        try:
            selected_attribute = self._cf_string("AXSelectedTextRange")
            if self._application_services.AXUIElementCopyAttributeValue(
                element, selected_attribute, ctypes.byref(selected_value)
            ) or not selected_value.value:
                return self._text_marker_caret_bounds(element)
            selected_range = _CFRange()
            if not self._application_services.AXValueGetValue(
                selected_value.value, 4, ctypes.byref(selected_range)
            ):
                return self._text_marker_caret_bounds(element)
            bounds_attribute = self._cf_string("AXBoundsForRange")

            def bounds_for_range(
                location: int, length: int, *, use_right_edge: bool
            ) -> tuple[int, int, int, int]:
                range_value = None
                value = ctypes.c_void_p()
                try:
                    text_range = _CFRange(max(0, location), max(0, length))
                    range_value = self._application_services.AXValueCreate(
                        4, ctypes.byref(text_range)
                    )
                    if not range_value:
                        return 0, 0, 0, 0
                    error = self._application_services.AXUIElementCopyParameterizedAttributeValue(
                        element,
                        bounds_attribute,
                        range_value,
                        ctypes.byref(value),
                    )
                    if error or not value.value:
                        return 0, 0, 0, 0
                    rect = _CGRect()
                    if not self._application_services.AXValueGetValue(
                        value.value, 3, ctypes.byref(rect)
                    ):
                        return 0, 0, 0, 0
                    x = rect.origin.x + (rect.size.width if use_right_edge else 0)
                    return (
                        int(round(x)),
                        int(round(rect.origin.y)),
                        2,
                        max(1, int(round(rect.size.height))),
                    )
                finally:
                    self._release(value.value, range_value)

            caret_location = max(
                0, int(selected_range.location + selected_range.length)
            )
            exact = bounds_for_range(caret_location, 0, use_right_edge=False)
            if exact[3] > 0:
                return exact
            if caret_location > 0:
                trailing = bounds_for_range(
                    caret_location - 1, 1, use_right_edge=True
                )
                if trailing[3] > 0:
                    return trailing
            return self._text_marker_caret_bounds(element)
        finally:
            self._release(
                bounds_attribute, selected_value.value, selected_attribute
            )

    def _text_marker_caret_bounds(
        self, element: int
    ) -> tuple[int, int, int, int]:
        """Fallback used by WebKit/Chromium accessibility text markers."""
        selected_attribute = None
        bounds_attribute = None
        selected_value = ctypes.c_void_p()
        bounds_value = ctypes.c_void_p()
        try:
            selected_attribute = self._cf_string("AXSelectedTextMarkerRange")
            if self._application_services.AXUIElementCopyAttributeValue(
                element,
                selected_attribute,
                ctypes.byref(selected_value),
            ) or not selected_value.value:
                return 0, 0, 0, 0
            bounds_attribute = self._cf_string("AXBoundsForTextMarkerRange")
            if self._application_services.AXUIElementCopyParameterizedAttributeValue(
                element,
                bounds_attribute,
                selected_value.value,
                ctypes.byref(bounds_value),
            ) or not bounds_value.value:
                return 0, 0, 0, 0
            rect = _CGRect()
            if not self._application_services.AXValueGetValue(
                bounds_value.value, 3, ctypes.byref(rect)
            ):
                return 0, 0, 0, 0
            return (
                int(round(rect.origin.x + rect.size.width)),
                int(round(rect.origin.y)),
                2,
                max(1, int(round(rect.size.height))),
            )
        finally:
            self._release(
                bounds_value.value,
                bounds_attribute,
                selected_value.value,
                selected_attribute,
            )

    def _focused_value(
        self, process_id: int
    ) -> tuple[int | None, int | None, int | None]:
        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        focused = ctypes.c_void_p()
        value = ctypes.c_void_p()
        focused_attribute = None
        value_attribute = None
        try:
            if not application:
                return None, None, None
            focused_attribute = self._cf_string("AXFocusedUIElement")
            error = self._application_services.AXUIElementCopyAttributeValue(
                application, focused_attribute, ctypes.byref(focused)
            )
            if error or not focused.value:
                return application, None, None
            value_attribute = self._cf_string("AXValue")
            error = self._application_services.AXUIElementCopyAttributeValue(
                focused.value, value_attribute, ctypes.byref(value)
            )
            if error:
                return application, focused.value, None
            return application, focused.value, value.value
        finally:
            self._release(value_attribute, focused_attribute)

    def _cf_string(self, value: str) -> int:
        result = self._core_foundation.CFStringCreateWithCString(
            None, value.encode("utf-8"), self._UTF8
        )
        if not result:
            raise RuntimeError("macOS 无法创建辅助功能字符串")
        return int(result)

    def _release(self, *values: object) -> None:
        for value in values:
            pointer = int(value or 0)
            if pointer:
                self._core_foundation.CFRelease(pointer)


class MacOSDesktopTextTarget:
    """Read and update the locked macOS text control with native shortcuts."""

    KEY_A = 0
    KEY_C = 8
    KEY_V = 9
    KEY_Z = 6
    KEY_DELETE = 51
    KEY_RIGHT = 124

    def __init__(
        self,
        clipboard: ClipboardBridge,
        *,
        injector: MacOSUnicodeTextInjector | None = None,
        copy_timeout_s: float = 0.8,
        copy_attempts: int = 3,
        focus_settle_s: float = 0.12,
        shortcut_settle_s: float = 0.05,
        accessibility_text: object | None = None,
    ) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("macOS desktop target requires macOS")
        self._clipboard = clipboard
        self._injector = injector or MacOSUnicodeTextInjector()
        self._copy_timeout_s = max(0.2, float(copy_timeout_s))
        self._copy_attempts = max(1, int(copy_attempts))
        self._focus_settle_s = max(0.05, float(focus_settle_s))
        self._shortcut_settle_s = max(0.02, float(shortcut_settle_s))
        self._accessibility_text = (
            accessibility_text
            if accessibility_text is not None
            else _MacOSAccessibilityTextBridge()
        )

    @staticmethod
    def _frontmost_application() -> tuple[int, str]:
        try:
            from AppKit import NSWorkspace

            application = NSWorkspace.sharedWorkspace().frontmostApplication()
            if application is None:
                return 0, "当前光标"
            return int(application.processIdentifier()), str(
                application.localizedName() or "当前光标"
            )
        except BaseException:
            return 0, "当前光标"

    @staticmethod
    def _pointer_position() -> tuple[int, int]:
        """Last-resort anchor for editors that hide all AX text geometry."""
        try:
            import Quartz

            point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            return int(round(point.x)), int(round(point.y))
        except BaseException:
            return 0, 0

    def capture_reference(self) -> DesktopTargetRef:
        process_id, name = self._frontmost_application()
        bounds = (0, 0, 0, 0)
        caret = (0, 0, 0, 0)
        focused_bounds = getattr(self._accessibility_text, "focused_bounds", None)
        if callable(focused_bounds):
            try:
                bounds = tuple(int(item) for item in focused_bounds(process_id))
            except BaseException:
                bounds = (0, 0, 0, 0)
        focused_caret_bounds = getattr(
            self._accessibility_text, "focused_caret_bounds", None
        )
        if callable(focused_caret_bounds):
            try:
                caret = tuple(
                    int(item) for item in focused_caret_bounds(process_id)
                )
            except BaseException:
                caret = (0, 0, 0, 0)
        if caret[3] <= 0:
            pointer_x, pointer_y = self._pointer_position()
            pointer_is_plausible = pointer_x > 0 and pointer_y > 0
            if bounds[2] > 0 and bounds[3] > 0:
                pointer_is_plausible = pointer_is_plausible and (
                    bounds[0] <= pointer_x <= bounds[0] + bounds[2]
                    and bounds[1] <= pointer_y <= bounds[1] + bounds[3]
                )
            if pointer_is_plausible:
                caret = (pointer_x, pointer_y, 2, 18)
        return DesktopTargetRef(
            window_handle=0,
            control_handle=0,
            window_title=name,
            process_id=process_id,
            process_name=name,
            screen_x=bounds[0],
            screen_y=bounds[1],
            screen_width=bounds[2],
            screen_height=bounds[3],
            caret_x=caret[0],
            caret_y=caret[1],
            caret_width=max(0, caret[2]),
            caret_height=max(0, caret[3]),
        )

    def is_foreground(self, target: DesktopTargetRef) -> bool:
        process_id, _name = self._frontmost_application()
        return bool(process_id and process_id == int(target.process_id))

    def caret_bounds(self, target: DesktopTargetRef) -> tuple[int, int, int, int]:
        """Locate the live insertion caret without moving focus."""
        process_id, _name = self._frontmost_application()
        if not process_id or process_id != int(target.process_id):
            return 0, 0, 0, 0
        focused_caret_bounds = getattr(
            self._accessibility_text, "focused_caret_bounds", None
        )
        if not callable(focused_caret_bounds):
            return 0, 0, 0, 0
        try:
            return tuple(
                int(value) for value in focused_caret_bounds(target.process_id)
            )
        except BaseException:
            return 0, 0, 0, 0

    def request_accessibility(self, *, prompt: bool = True) -> bool:
        return self._injector.is_trusted(prompt=prompt)

    def capture_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot:
        return self._capture_text(target, allow_empty=False)

    def capture_text_allowing_empty(
        self, target: DesktopTargetRef
    ) -> DesktopTextSnapshot:
        """Read back a known clear operation where an empty value is success."""
        return self._capture_text(target, allow_empty=True)

    def _capture_text(
        self, target: DesktopTargetRef, *, allow_empty: bool
    ) -> DesktopTextSnapshot:
        self._injector.require_accessibility()
        self._activate(target)
        try:
            accessibility_value = self._accessibility_text.read_focused_value(
                target.process_id
            )
        except BaseException:
            accessibility_value = None
        if accessibility_value is not None:
            return DesktopTextSnapshot(target=target, text=accessibility_value)
        saved_selection = None
        selected_range = getattr(
            self._accessibility_text, "focused_selected_range", None
        )
        if callable(selected_range):
            try:
                saved_selection = selected_range(target.process_id)
            except BaseException:
                saved_selection = None
        clipboard_snapshot = self._clipboard.snapshot()
        text = ""
        try:
            for attempt in range(self._copy_attempts):
                self._activate(target)
                sentinel = f"__PROXIMIC_COPY_{uuid.uuid4().hex}__"
                self._clipboard.set_text(sentinel)
                self._injector.command_key(self.KEY_A)
                time.sleep(self._shortcut_settle_s)
                self._injector.command_key(self.KEY_C)
                deadline = time.monotonic() + self._copy_timeout_s
                while time.monotonic() < deadline:
                    candidate = str(self._clipboard.text() or "")
                    if candidate and candidate != sentinel:
                        text = candidate
                        break
                    time.sleep(0.01)
                if text:
                    break
                if allow_empty:
                    # The caller just issued an explicit, validated clear.
                    # A successful copy of an empty control leaves the sentinel
                    # untouched, so retrying cannot produce a non-empty value.
                    break
                if attempt + 1 < self._copy_attempts:
                    time.sleep(0.05)
        finally:
            self._clipboard.restore(clipboard_snapshot)
            restored = False
            restore_selection = getattr(
                self._accessibility_text, "set_focused_selected_range", None
            )
            if saved_selection is not None and callable(restore_selection):
                try:
                    self._activate(target)
                    restored = bool(
                        restore_selection(target.process_id, saved_selection)
                    )
                except BaseException:
                    restored = False
            if not restored:
                # Never leave Select-All active: a following dictation paste
                # must append at a caret rather than replace the entire field.
                try:
                    self._activate(target)
                    self._injector.press_key(self.KEY_RIGHT)
                    time.sleep(self._shortcut_settle_s)
                except BaseException:
                    pass
        if not text and not allow_empty:
            raise RuntimeError(
                "多次复制后仍未读取到文本；请确认光标位于可编辑文本框且内容非空"
            )
        return DesktopTextSnapshot(target=target, text=text)

    def observe_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot:
        """Read a manually edited field without focus, selection, or clipboard I/O."""
        process_id, _name = self._frontmost_application()
        if not process_id or process_id != int(target.process_id):
            raise RuntimeError("目标文本框当前不在前台")

        # A process can contain several editable fields.  Bounds are the only
        # stable identity available through the lightweight AX bridge; refuse
        # to observe a different field instead of recording the wrong result.
        focused_bounds = getattr(self._accessibility_text, "focused_bounds", None)
        if target.screen_width > 0 and target.screen_height > 0:
            if not callable(focused_bounds):
                raise RuntimeError("目标文本框不支持无干扰定位")
            try:
                current_bounds = tuple(
                    int(item) for item in focused_bounds(target.process_id)
                )
            except BaseException as exc:
                raise RuntimeError("无法无干扰定位目标文本框") from exc
            expected_bounds = (
                target.screen_x,
                target.screen_y,
                target.screen_width,
                target.screen_height,
            )
            if any(
                abs(current - expected) > 8
                for current, expected in zip(current_bounds, expected_bounds)
            ):
                raise RuntimeError("用户焦点已经离开原文本框")

        try:
            value = self._accessibility_text.read_focused_value(target.process_id)
        except BaseException as exc:
            raise RuntimeError("当前文本框不支持无干扰读取") from exc
        if value is None:
            raise RuntimeError("当前文本框不支持无干扰读取")
        return DesktopTextSnapshot(target=target, text=str(value))

    def inject(self, target: DesktopTargetRef, text: str) -> None:
        value = str(text or "")
        if not value:
            return
        # WeChat and several Chromium/Electron editors silently ignore
        # CGEventKeyboardSetUnicodeString even though posting the event reports
        # success. Clipboard paste is accepted consistently and intentionally
        # leaves the dictated text available to the user afterwards.
        self._activate(target)
        before = None
        try:
            before = self._accessibility_text.read_focused_value(
                target.process_id
            )
        except BaseException:
            pass
        self._set_clipboard_text_for_paste(value)
        self._injector.command_key(self.KEY_V)
        time.sleep(max(self._shortcut_settle_s, 0.12))
        if before is not None:
            try:
                after = self._accessibility_text.read_focused_value(
                    target.process_id
                )
            except BaseException:
                after = None
            if after is not None and after == before:
                raise RuntimeError("目标文本框没有接收剪贴板听写内容")

    def _set_clipboard_text_for_paste(self, value: str) -> None:
        """Publish and verify the exact string before sending Command+V."""
        for attempt in range(self._copy_attempts):
            self._clipboard.set_text(value)
            deadline = time.monotonic() + self._copy_timeout_s
            while time.monotonic() < deadline:
                if str(self._clipboard.text() or "") == value:
                    return
                time.sleep(0.01)
            if attempt + 1 < self._copy_attempts:
                time.sleep(0.03)
        raise RuntimeError("剪贴板未更新为本次听写内容，已停止粘贴以避免插入旧文字")

    def replace(self, snapshot: DesktopTextSnapshot, text: str) -> None:
        self._activate(snapshot.target)
        replacement = str(text or "")
        try:
            if self._accessibility_text.set_focused_value(
                snapshot.target.process_id, replacement
            ):
                # Some Chromium/Electron controls report AXValue success while
                # silently keeping the old value. Trust the setter only after
                # the focused control itself confirms the new text.
                time.sleep(max(self._shortcut_settle_s, 0.12))
                for attempt in range(2):
                    try:
                        observed = self._accessibility_text.read_focused_value(
                            snapshot.target.process_id
                        )
                    except BaseException:
                        observed = None
                    if observed is not None and macos_texts_equivalent(
                        observed, replacement
                    ):
                        return
                    if attempt == 0:
                        time.sleep(0.08)
        except BaseException:
            # Browser content-editables and custom editors often do not expose
            # a settable AXValue. Continue with the keyboard fallback.
            pass
        self._injector.command_key(self.KEY_A)
        time.sleep(self._shortcut_settle_s)
        if replacement:
            # WeChat and multiple Chromium/Electron editors ignore Quartz
            # Unicode events but reliably accept a verified clipboard paste.
            self._set_clipboard_text_for_paste(replacement)
            self._injector.command_key(self.KEY_V)
        else:
            self._injector.press_key(self.KEY_DELETE)
        # Posted Quartz events are asynchronous. Do not let immediate readback
        # steal the focus before the target app consumes the final chunk.
        time.sleep(max(self._shortcut_settle_s, 0.12))

    def undo(self, target: DesktopTargetRef) -> None:
        """Undo the most recent edit in the locked external text control."""
        self._activate(target)
        self._injector.command_key(self.KEY_Z)
        time.sleep(max(self._shortcut_settle_s, 0.12))

    def release_selection(self, target: DesktopTargetRef) -> None:
        try:
            self._activate(target)
            self._injector.press_key(self.KEY_RIGHT)
        except BaseException:
            return

    def _activate(self, target: DesktopTargetRef) -> None:
        if not target.process_id or target.process_id == os.getpid():
            raise RuntimeError("请先把光标放入另一个应用的文本框，再开始听写")
        try:
            from AppKit import (
                NSApplicationActivateAllWindows,
                NSApplicationActivateIgnoringOtherApps,
                NSRunningApplication,
            )

            application = NSRunningApplication.runningApplicationWithProcessIdentifier_(
                int(target.process_id)
            )
            if application is None:
                raise RuntimeError("原文本应用已经关闭")
            activated = application.activateWithOptions_(
                NSApplicationActivateAllWindows
                | NSApplicationActivateIgnoringOtherApps
            )
            if not activated:
                raise RuntimeError("无法重新激活原文本应用")
        except RuntimeError:
            raise
        except BaseException as exc:
            raise RuntimeError("无法重新激活原文本应用") from exc
        time.sleep(self._focus_settle_s)


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
        # Preserve the established clipboard behavior for WeChat and browsers.
        # Codex needs UIA because its native focus HWND represents the entire
        # Chromium renderer rather than the ProseMirror composer.
        if (
            process_name.casefold() == "chatgpt.exe"
            and self._uia is not None
        ):
            try:
                uia_control = self._uia.capture_focused_text_control(
                    int(process_id.value)
                )
            except Exception:
                uia_control = None
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
        """Undo the most recent edit in the locked external text control."""
        self._activate(target)
        self._hotkey(self.VK_CONTROL, 0x5A)  # Z
        time.sleep(getattr(self, "_shortcut_settle_s", 0.03))

    def release_selection(self, target: DesktopTargetRef) -> None:
        try:
            self._activate(target)
            self._press_key(self.VK_END)
        except BaseException:
            return

    def is_foreground(self, target: DesktopTargetRef) -> bool:
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
