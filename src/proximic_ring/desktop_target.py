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


class _MacOSAccessibilityTextBridge:
    """Read or replace the focused AXValue without depending on key timing.

    PyObjC's Quartz module does not expose the Accessibility API on every
    supported build, so this deliberately uses the stable C API. Controls
    which do not expose a string AXValue return ``None``/``False`` and the
    caller falls back to Select-All plus keyboard events.
    """

    _UTF8 = 0x08000100
    _EDITABLE_TEXT_ROLES = frozenset(
        {"AXTextArea", "AXTextField", "AXSearchField", "AXComboBox"}
    )
    _MAX_TEXT_PARENT_DEPTH = 7
    _AX_ERROR_NAMES = {
        -25204: "cannot_complete",
        -25205: "attribute_unsupported",
        -25208: "not_implemented",
        -25211: "api_disabled",
    }

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
        application_services.AXUIElementCreateSystemWide.argtypes = ()
        application_services.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
        application_services.AXUIElementGetPid.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        )
        application_services.AXUIElementGetPid.restype = ctypes.c_int
        application_services.AXUIElementCopyElementAtPosition.argtypes = (
            ctypes.c_void_p,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.POINTER(ctypes.c_void_p),
        )
        application_services.AXUIElementCopyElementAtPosition.restype = ctypes.c_int
        application_services.AXUIElementCopyAttributeValue.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        application_services.AXUIElementCopyAttributeValue.restype = ctypes.c_int
        application_services.AXUIElementGetAttributeValueCount.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_long),
        )
        application_services.AXUIElementGetAttributeValueCount.restype = ctypes.c_int
        application_services.AXUIElementCopyAttributeValues.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.POINTER(ctypes.c_void_p),
        )
        application_services.AXUIElementCopyAttributeValues.restype = ctypes.c_int
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
        core_foundation.CFAttributedStringGetTypeID.argtypes = ()
        core_foundation.CFAttributedStringGetTypeID.restype = ctypes.c_ulong
        core_foundation.CFAttributedStringGetString.argtypes = (ctypes.c_void_p,)
        core_foundation.CFAttributedStringGetString.restype = ctypes.c_void_p
        core_foundation.CFNumberGetTypeID.argtypes = ()
        core_foundation.CFNumberGetTypeID.restype = ctypes.c_ulong
        core_foundation.CFNumberGetValue.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
        )
        core_foundation.CFNumberGetValue.restype = ctypes.c_bool
        core_foundation.CFBooleanGetTypeID.argtypes = ()
        core_foundation.CFBooleanGetTypeID.restype = ctypes.c_ulong
        core_foundation.CFBooleanGetValue.argtypes = (ctypes.c_void_p,)
        core_foundation.CFBooleanGetValue.restype = ctypes.c_bool
        core_foundation.CFArrayGetCount.argtypes = (ctypes.c_void_p,)
        core_foundation.CFArrayGetCount.restype = ctypes.c_long
        core_foundation.CFArrayGetValueAtIndex.argtypes = (
            ctypes.c_void_p,
            ctypes.c_long,
        )
        core_foundation.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        core_foundation.CFHash.argtypes = (ctypes.c_void_p,)
        core_foundation.CFHash.restype = ctypes.c_ulong
        core_foundation.CFRelease.argtypes = (ctypes.c_void_p,)
        self._application_services = application_services
        self._core_foundation = core_foundation
        try:
            self._cf_boolean_true = int(
                ctypes.c_void_p.in_dll(
                    core_foundation, "kCFBooleanTrue"
                ).value
                or 0
            )
            self._cf_boolean_false = int(
                ctypes.c_void_p.in_dll(
                    core_foundation, "kCFBooleanFalse"
                ).value
                or 0
            )
        except (TypeError, ValueError):
            self._cf_boolean_true = 0
            self._cf_boolean_false = 0
        self._last_context_read_method = ""
        self._last_context_source_char_count: int | None = None

    def manual_accessibility_state(self, process_id: int) -> bool | None:
        """Read an app's opt-in AX tree state when it exposes the switch."""

        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        value = None
        try:
            if not application:
                return None
            value = self._copy_attribute_value(
                int(application), "AXManualAccessibility"
            )
            if not value:
                return None
            cf = self._core_foundation
            if cf.CFGetTypeID(value) != cf.CFBooleanGetTypeID():
                return None
            return bool(cf.CFBooleanGetValue(value))
        finally:
            self._release(value, application)

    def set_manual_accessibility(
        self, process_id: int, *, enabled: bool
    ) -> str:
        """Temporarily request a lazily generated app accessibility tree.

        Chromium/Electron documents this attribute for third-party assistive
        software. Other applications reject it harmlessly with an AX error.
        The caller restores the previous value after its observation attempt.
        """

        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        attribute = None
        try:
            value = (
                self._cf_boolean_true if enabled else self._cf_boolean_false
            )
            if not application:
                return "application_unavailable"
            if not value:
                return "cf_boolean_unavailable"
            attribute = self._cf_string("AXManualAccessibility")
            error = int(
                self._application_services.AXUIElementSetAttributeValue(
                    application, attribute, value
                )
            )
            if error:
                name = self._AX_ERROR_NAMES.get(error, "ax_error")
                return f"{name}:{error}"
            return "enabled" if enabled else "disabled"
        except BaseException as exc:
            return f"exception:{type(exc).__name__}"
        finally:
            self._release(attribute, application)

    def focused_control_id(self, process_id: int) -> str:
        """Return a stable in-process identity for the focused AX element."""

        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        focused = ctypes.c_void_p()
        focused_attribute = None
        try:
            if not application:
                return ""
            focused_attribute = self._cf_string("AXFocusedUIElement")
            if self._application_services.AXUIElementCopyAttributeValue(
                application, focused_attribute, ctypes.byref(focused)
            ) or not focused.value:
                return ""
            return f"ax:{int(self._core_foundation.CFHash(focused.value))}"
        finally:
            self._release(focused.value, focused_attribute, application)

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

    def read_focused_context(
        self,
        process_id: int,
        *,
        max_chars: int,
        anchor: tuple[int, int] | None = None,
    ) -> str | None:
        """Read live focused text without selection, key, or clipboard changes.

        Context may use a standard accessibility character range when a custom
        editor does not expose AXValue. Unlike ``read_focused_value``, a tail or
        visible range is valid here because ASR context is intentionally
        bounded and is never used to replace or verify the field.
        """

        return self._read_focused_context(
            int(process_id),
            max_chars=max(1, int(max_chars)),
            anchor=anchor,
        )

    @property
    def last_context_read_method(self) -> str:
        return self._last_context_read_method

    @property
    def last_context_source_char_count(self) -> int | None:
        return self._last_context_source_char_count

    def _read_focused_context(
        self,
        process_id: int,
        *,
        max_chars: int,
        anchor: tuple[int, int] | None,
    ) -> str | None:
        candidates = self._copy_focused_elements(process_id)
        self._last_context_read_method = ""
        self._last_context_source_char_count = None
        try:
            for _owner, focused, focus_source in candidates:
                text = self._read_context_from_ancestry(
                    focused,
                    source=focus_source,
                    max_chars=max_chars,
                )
                if text is not None:
                    return text
                text = self._read_context_from_focused_descendant(
                    focused,
                    source=focus_source,
                    max_chars=max_chars,
                )
                if text is not None:
                    return text
        finally:
            for owner, focused, _source in candidates:
                self._release(focused, owner)

        focused_window = self._copy_application_element(
            process_id, "AXFocusedWindow"
        )
        if focused_window is not None:
            owner, element = focused_window
            try:
                text = self._read_context_from_focused_descendant(
                    element,
                    source="window",
                    max_chars=max_chars,
                )
                if text is not None:
                    return text
            finally:
                self._release(element, owner)

        if anchor is not None and anchor[0] > 0 and anchor[1] > 0:
            point_element = self._copy_element_at_position(
                process_id, anchor
            )
            if point_element is not None:
                owner, element = point_element
                try:
                    text = self._read_context_from_ancestry(
                        element,
                        source="point",
                        max_chars=max_chars,
                        require_ax_focused=True,
                    )
                    if text is not None:
                        return text
                    text = self._read_context_from_focused_descendant(
                        element,
                        source="point",
                        max_chars=max_chars,
                    )
                    if text is not None:
                        return text
                finally:
                    self._release(element, owner)
        return None

    def _read_context_from_ancestry(
        self,
        focused: int,
        *,
        source: str,
        max_chars: int,
        require_ax_focused: bool = False,
    ) -> str | None:
        owned_parents: list[int] = []
        seen = {int(focused)}
        element = int(focused)
        try:
            for depth in range(self._MAX_TEXT_PARENT_DEPTH):
                editable = self._is_editable_text_element(element)
                is_current = not require_ax_focused or self._is_ax_focused(
                    element
                )
                if editable and is_current:
                    location = "focused" if depth == 0 else f"parent{depth}"
                    text = self._read_context_from_element(
                        element,
                        method_prefix=f"{source}.{location}",
                        max_chars=max_chars,
                    )
                    if text is not None:
                        return text
                parent = self._copy_attribute_value(element, "AXParent")
                if not parent:
                    break
                if parent in seen:
                    self._release(parent)
                    break
                seen.add(parent)
                owned_parents.append(parent)
                element = parent
            return None
        finally:
            self._release(*reversed(owned_parents))

    def _read_context_from_element(
        self, element: int, *, method_prefix: str, max_chars: int
    ) -> str | None:
        text = self._copy_text_attribute(element, "AXValue")
        if text is not None:
            self._last_context_source_char_count = len(text)
            self._last_context_read_method = f"{method_prefix}.AXValue"
            return text[-max_chars:]
        text, method, source_char_count = self._read_parameterized_context(
            element, max_chars=max_chars
        )
        if text is None:
            return None
        self._last_context_source_char_count = (
            source_char_count
            if source_char_count is not None
            else len(text)
        )
        self._last_context_read_method = f"{method_prefix}.{method}"
        return text[-max_chars:]

    def _read_context_from_focused_descendant(
        self,
        root: int,
        *,
        source: str,
        max_chars: int,
    ) -> str | None:
        queue: list[tuple[int, int]] = [(int(root), 0)]
        seen = {int(root)}
        owned_arrays: list[int] = []
        examined = 0
        try:
            while queue and examined < 96:
                element, depth = queue.pop(0)
                examined += 1
                if depth > 0 and self._is_ax_focused(element):
                    text = self._read_context_from_ancestry(
                        element,
                        source=f"{source}.descendant{depth}",
                        max_chars=max_chars,
                    )
                    if text is not None:
                        return text
                if depth >= 6:
                    continue
                children, owned_array = self._copy_recent_children(element)
                if owned_array:
                    owned_arrays.append(owned_array)
                for child in children:
                    if child in seen:
                        continue
                    seen.add(child)
                    queue.append((child, depth + 1))
            return None
        finally:
            self._release(*reversed(owned_arrays))

    def _copy_focused_elements(
        self, process_id: int
    ) -> list[tuple[int, int, str]]:
        """Return app and system-wide focused elements owned by this caller."""

        services = self._application_services
        candidates: list[tuple[int, int, str]] = []
        application = services.AXUIElementCreateApplication(int(process_id))
        if application:
            focused = self._copy_attribute_value(
                int(application), "AXFocusedUIElement"
            )
            if focused:
                candidates.append((int(application), focused, "application"))
            else:
                self._release(application)

        system_wide = services.AXUIElementCreateSystemWide()
        if system_wide:
            focused = self._copy_attribute_value(
                int(system_wide), "AXFocusedUIElement"
            )
            focused_pid = ctypes.c_int(0)
            get_pid = getattr(services, "AXUIElementGetPid", None)
            valid_process = bool(
                focused
                and callable(get_pid)
                and not get_pid(focused, ctypes.byref(focused_pid))
                and focused_pid.value == int(process_id)
            )
            duplicate = any(
                candidate_focused == focused
                for _owner, candidate_focused, _source in candidates
            )
            if valid_process and not duplicate:
                candidates.append((int(system_wide), focused, "system"))
            else:
                self._release(focused, system_wide)
        return candidates

    def _copy_application_element(
        self, process_id: int, attribute_name: str
    ) -> tuple[int, int] | None:
        application = self._application_services.AXUIElementCreateApplication(
            int(process_id)
        )
        if not application:
            return None
        element = self._copy_attribute_value(
            int(application), attribute_name
        )
        if not element:
            self._release(application)
            return None
        return int(application), element

    def _copy_element_at_position(
        self, process_id: int, anchor: tuple[int, int]
    ) -> tuple[int, int] | None:
        services = self._application_services
        system_wide = services.AXUIElementCreateSystemWide()
        if not system_wide:
            return None
        element = ctypes.c_void_p()
        error = services.AXUIElementCopyElementAtPosition(
            system_wide,
            ctypes.c_float(float(anchor[0])),
            ctypes.c_float(float(anchor[1])),
            ctypes.byref(element),
        )
        actual_pid = ctypes.c_int(0)
        valid = bool(
            not error
            and element.value
            and not services.AXUIElementGetPid(
                element.value, ctypes.byref(actual_pid)
            )
            and actual_pid.value == int(process_id)
        )
        if not valid:
            self._release(element.value, system_wide)
            return None
        return int(system_wide), int(element.value)

    def _copy_recent_children(
        self, element: int, *, limit: int = 64
    ) -> tuple[list[int], int | None]:
        services = self._application_services
        attribute = self._cf_string("AXChildren")
        count = ctypes.c_long(0)
        values = ctypes.c_void_p()
        try:
            error = services.AXUIElementGetAttributeValueCount(
                element, attribute, ctypes.byref(count)
            )
            if error or count.value <= 0:
                return [], None
            request_count = min(max(1, int(limit)), int(count.value))
            start_index = max(0, int(count.value) - request_count)
            error = services.AXUIElementCopyAttributeValues(
                element,
                attribute,
                start_index,
                request_count,
                ctypes.byref(values),
            )
            if error or not values.value:
                self._release(values.value)
                return [], None
            cf = self._core_foundation
            array_count = min(
                request_count, int(cf.CFArrayGetCount(values.value))
            )
            children = [
                int(cf.CFArrayGetValueAtIndex(values.value, index) or 0)
                for index in range(array_count)
            ]
            return [child for child in children if child], int(values.value)
        finally:
            self._release(attribute)

    def _is_ax_focused(self, element: int) -> bool:
        value = self._copy_attribute_value(element, "AXFocused")
        try:
            if not value:
                return False
            cf = self._core_foundation
            if cf.CFGetTypeID(value) != cf.CFBooleanGetTypeID():
                return False
            return bool(cf.CFBooleanGetValue(value))
        finally:
            self._release(value)

    def _copy_attribute_value(self, element: int, name: str) -> int | None:
        attribute = self._cf_string(name)
        value = ctypes.c_void_p()
        try:
            error = self._application_services.AXUIElementCopyAttributeValue(
                element, attribute, ctypes.byref(value)
            )
            if error or not value.value:
                self._release(value.value)
                return None
            return int(value.value)
        finally:
            self._release(attribute)

    def _copy_text_attribute(self, element: int, name: str) -> str | None:
        value = self._copy_attribute_value(element, name)
        try:
            return self._cf_text(value)
        finally:
            self._release(value)

    def _is_editable_text_element(self, element: int) -> bool:
        role = self._copy_text_attribute(element, "AXRole")
        if role in self._EDITABLE_TEXT_ROLES:
            return True
        # Content-editable WebKit/Chromium wrappers are sometimes AXGroup or
        # AXWebArea. A settable text selection is a stronger and safer signal
        # of an editor than accepting arbitrary string-valued AX elements.
        attribute = self._cf_string("AXSelectedTextRange")
        settable = ctypes.c_bool(False)
        try:
            error = self._application_services.AXUIElementIsAttributeSettable(
                element, attribute, ctypes.byref(settable)
            )
            return not error and bool(settable.value)
        finally:
            self._release(attribute)

    def _read_parameterized_context(
        self, element: int, *, max_chars: int
    ) -> tuple[str | None, str, int | None]:
        character_count_value = self._copy_attribute_value(
            element, "AXNumberOfCharacters"
        )
        try:
            character_count = self._cf_number(character_count_value)
        finally:
            self._release(character_count_value)
        if character_count is not None:
            length = min(max_chars, max(0, character_count))
            location = max(0, character_count - length)
            text_range = _CFRange(location, length)
            range_value = self._application_services.AXValueCreate(
                4, ctypes.byref(text_range)
            )
            try:
                text, method = self._copy_parameterized_text(
                    element, range_value
                )
                if text is not None:
                    return text, method, character_count
            finally:
                self._release(range_value)

        visible_range = self._copy_attribute_value(
            element, "AXVisibleCharacterRange"
        )
        try:
            if visible_range:
                text, method = self._copy_parameterized_text(
                    element, visible_range
                )
                return text, method, None
        finally:
            self._release(visible_range)
        return None, "", None

    def _copy_parameterized_text(
        self, element: int, range_value: int | None
    ) -> tuple[str | None, str]:
        if not range_value:
            return None, ""
        for name in ("AXStringForRange", "AXAttributedStringForRange"):
            attribute = self._cf_string(name)
            value = ctypes.c_void_p()
            try:
                copy_parameterized = getattr(
                    self._application_services,
                    "AXUIElementCopyParameterizedAttributeValue",
                )
                error = copy_parameterized(
                    element, attribute, range_value, ctypes.byref(value)
                )
                if not error and value.value:
                    text = self._cf_text(value.value)
                    if text is not None:
                        return text, name
            finally:
                self._release(value.value, attribute)
        return None, ""

    def _cf_number(self, value: int | None) -> int | None:
        if not value:
            return None
        cf = self._core_foundation
        if cf.CFGetTypeID(value) != cf.CFNumberGetTypeID():
            return None
        result = ctypes.c_long(0)
        # kCFNumberCFIndexType is the native CFIndex-sized integer type.
        if not cf.CFNumberGetValue(value, 14, ctypes.byref(result)):
            return None
        return max(0, int(result.value))

    def _cf_text(self, value: int | None) -> str | None:
        if not value:
            return None
        cf = self._core_foundation
        type_id = cf.CFGetTypeID(value)
        string_value = int(value)
        if type_id == cf.CFAttributedStringGetTypeID():
            string_value = int(cf.CFAttributedStringGetString(value) or 0)
            if not string_value:
                return None
        elif type_id != cf.CFStringGetTypeID():
            return None
        length = int(cf.CFStringGetLength(string_value))
        size = int(
            cf.CFStringGetMaximumSizeForEncoding(length, self._UTF8)
        ) + 1
        buffer = ctypes.create_string_buffer(max(1, size))
        if not cf.CFStringGetCString(
            string_value, buffer, len(buffer), self._UTF8
        ):
            return None
        return buffer.value.decode("utf-8")

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
    _MANUAL_AX_APPLICATIONS = frozenset({"wechat", "weixin", "微信"})

    @staticmethod
    def _same_text_field_geometry(
        expected: tuple[int, int, int, int],
        current: tuple[int, int, int, int],
    ) -> bool:
        """Match one editor while allowing content-driven vertical resizing."""

        ex, ey, ew, eh = (int(value) for value in expected)
        cx, cy, cw, ch = (int(value) for value in current)
        if min(ew, eh, cw, ch) <= 0:
            return False
        horizontal_tolerance = max(12, min(48, round(max(ew, cw) * 0.08)))
        width_tolerance = max(18, min(80, round(max(ew, cw) * 0.15)))
        if abs(ex - cx) > horizontal_tolerance or abs(ew - cw) > width_tolerance:
            return False
        edge_tolerance = max(12, min(36, round(max(eh, ch) * 0.2)))
        same_top = abs(ey - cy) <= edge_tolerance
        same_bottom = abs((ey + eh) - (cy + ch)) <= edge_tolerance
        overlap = max(0, min(ey + eh, cy + ch) - max(ey, cy))
        enough_overlap = overlap >= min(eh, ch) * 0.5
        return bool(enough_overlap and (same_top or same_bottom))

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
        self._last_context_accessibility_probe = ""

    @classmethod
    def _allows_manual_accessibility_probe(cls, target: DesktopTargetRef) -> bool:
        application = str(
            target.process_name or target.window_title or ""
        ).strip().lower()
        return application in cls._MANUAL_AX_APPLICATIONS

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
        accessibility_id = ""
        focused_control_id = getattr(
            self._accessibility_text, "focused_control_id", None
        )
        if callable(focused_control_id):
            try:
                accessibility_id = str(focused_control_id(process_id) or "")
            except BaseException:
                accessibility_id = ""
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
            accessibility_id=accessibility_id,
        )

    def is_foreground(self, target: DesktopTargetRef) -> bool:
        process_id, _name = self._frontmost_application()
        if not process_id or process_id != int(target.process_id):
            return False
        focused_control_id = getattr(
            self._accessibility_text, "focused_control_id", None
        )
        if target.accessibility_id and callable(focused_control_id):
            try:
                current_id = str(focused_control_id(target.process_id) or "")
            except BaseException:
                current_id = ""
            if current_id:
                if current_id == target.accessibility_id:
                    return True
                # Some Electron/WebKit editors recreate their AX wrapper while
                # keeping the same focused field. Confirm with geometry before
                # treating a changed wrapper hash as a different text field.
        if target.screen_width <= 0 or target.screen_height <= 0:
            # Some macOS web editors expose neither AX bounds nor a persistent
            # focused-element identity. The owning application is still
            # reliable. Native user undo follows that application's current
            # focus/history; without AX metadata we cannot identify the field
            # more precisely, and must not select/copy text to do so.
            return True
        focused_bounds = getattr(self._accessibility_text, "focused_bounds", None)
        if not callable(focused_bounds):
            return True
        try:
            current_bounds = tuple(
                int(item) for item in focused_bounds(target.process_id)
            )
        except BaseException:
            return False
        expected_bounds = (
            target.screen_x,
            target.screen_y,
            target.screen_width,
            target.screen_height,
        )
        return self._same_text_field_geometry(expected_bounds, current_bounds)

    def is_application_foreground(self, target: DesktopTargetRef) -> bool:
        """Check the owning app without relying on a transient AX element."""

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

        # A process can contain several editable fields. Prefer the stable AX
        # element identity; bounds are a fallback for older/custom bridges.
        identity_verified = False
        focused_control_id = getattr(
            self._accessibility_text, "focused_control_id", None
        )
        if target.accessibility_id and callable(focused_control_id):
            try:
                current_id = str(focused_control_id(target.process_id) or "")
            except BaseException as exc:
                raise RuntimeError("无法无干扰定位目标文本框") from exc
            identity_verified = current_id == target.accessibility_id
        focused_bounds = getattr(self._accessibility_text, "focused_bounds", None)
        if (
            not identity_verified
            and target.screen_width > 0
            and target.screen_height > 0
        ):
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
            if not self._same_text_field_geometry(
                expected_bounds,
                current_bounds,
            ):
                raise RuntimeError("用户焦点已经离开原文本框")

        try:
            value = self._accessibility_text.read_focused_value(target.process_id)
        except BaseException as exc:
            raise RuntimeError("当前文本框不支持无干扰读取") from exc
        if value is None:
            raise RuntimeError("当前文本框不支持无干扰读取")
        return DesktopTextSnapshot(target=target, text=str(value))

    def observe_context_text(
        self, target: DesktopTargetRef, *, max_chars: int
    ) -> DesktopTextSnapshot:
        """Read bounded live ASR context without changing the target in any way."""

        process_id, _name = self._frontmost_application()
        if not process_id or process_id != int(target.process_id):
            raise RuntimeError("目标应用当前不在前台")
        reader = getattr(self._accessibility_text, "read_focused_context", None)
        if not callable(reader):
            raise RuntimeError("当前文本框不支持无干扰上下文读取")
        self._last_context_accessibility_probe = ""
        anchor = (
            (int(target.caret_x), int(target.caret_y))
            if target.caret_x > 0 and target.caret_y > 0
            else None
        )
        try:
            value = reader(
                target.process_id,
                max_chars=max_chars,
                anchor=anchor,
            )
        except BaseException as exc:
            raise RuntimeError(
                "无干扰上下文读取异常：" + type(exc).__name__
            ) from exc
        if value is None and self._allows_manual_accessibility_probe(target):
            state_reader = getattr(
                self._accessibility_text,
                "manual_accessibility_state",
                None,
            )
            state_setter = getattr(
                self._accessibility_text,
                "set_manual_accessibility",
                None,
            )
            if callable(state_setter):
                previous_state = None
                if callable(state_reader):
                    try:
                        previous_state = state_reader(target.process_id)
                    except BaseException:
                        previous_state = None
                try:
                    probe_result = str(
                        state_setter(target.process_id, enabled=True) or ""
                    )
                except BaseException as exc:
                    probe_result = f"exception:{type(exc).__name__}"
                self._last_context_accessibility_probe = probe_result
                if probe_result == "enabled":
                    try:
                        # Give a lazy web/custom accessibility tree a short,
                        # bounded opportunity to materialize. No input event is
                        # emitted during these retries.
                        for delay in (0.02, 0.05, 0.10):
                            time.sleep(delay)
                            value = reader(
                                target.process_id,
                                max_chars=max_chars,
                                anchor=anchor,
                            )
                            if value is not None:
                                self._last_context_accessibility_probe = (
                                    "enabled_readable"
                                )
                                break
                    finally:
                        if previous_state is not True:
                            try:
                                restore_result = str(
                                    state_setter(
                                        target.process_id,
                                        enabled=False,
                                    )
                                    or ""
                                )
                            except BaseException as exc:
                                restore_result = (
                                    f"exception:{type(exc).__name__}"
                                )
                            self._last_context_accessibility_probe += (
                                f";restore:{restore_result}"
                            )
        if value is None:
            probe_suffix = (
                "；AXManualAccessibility="
                + self._last_context_accessibility_probe
                if self._last_context_accessibility_probe
                else ""
            )
            raise RuntimeError(
                "应用级/系统级焦点、窗口焦点子树、坐标元素、"
                "可编辑父级、AXValue 和文本范围均不可读"
                + probe_suffix
            )
        return DesktopTextSnapshot(target=target, text=str(value))

    @property
    def last_context_accessibility_probe(self) -> str:
        return self._last_context_accessibility_probe

    @property
    def last_context_read_method(self) -> str:
        return str(
            getattr(self._accessibility_text, "last_context_read_method", "")
            or ""
        )

    @property
    def last_context_source_char_count(self) -> int | None:
        value = getattr(
            self._accessibility_text,
            "last_context_source_char_count",
            None,
        )
        return int(value) if isinstance(value, int) and value >= 0 else None

    def observe_focused_text(self, target: DesktopTargetRef) -> DesktopTextSnapshot:
        """Read the current field when a stale AX identity is being revalidated.

        This deliberately verifies only the owning foreground application.  The
        caller must compare the returned text with its saved applied state before
        performing any mutation.  Unlike ``capture_text`` this path never moves
        the selection or touches the clipboard.
        """

        process_id, _name = self._frontmost_application()
        if not process_id or process_id != int(target.process_id):
            raise RuntimeError("目标应用当前不在前台")
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
        """Internal rollback: activate, undo, then settle before readback."""
        self._activate(target)
        self.send_native_undo(target)
        time.sleep(max(self._shortcut_settle_s, 0.06))

    def send_native_undo(self, target: DesktopTargetRef) -> None:
        """Post one Cmd+Z after the caller checks focus; no activation/readback."""
        self._injector.command_key(self.KEY_Z)

    def release_selection(self, target: DesktopTargetRef) -> None:
        try:
            self._activate(target)
            self._injector.press_key(self.KEY_RIGHT)
        except BaseException:
            return

    def _activate(self, target: DesktopTargetRef) -> None:
        if not target.process_id or target.process_id == os.getpid():
            raise RuntimeError("请先把光标放入另一个应用的文本框，再开始听写")
        process_id, _name = self._frontmost_application()
        if process_id == int(target.process_id):
            return
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
