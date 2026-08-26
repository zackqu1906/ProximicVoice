"""Focused Windows UI Automation text controls.

Chromium/Electron applications expose DOM editors through UI Automation even
when their native HWND only represents the whole renderer.  Keeping this small
adapter separate lets the ordinary Win32/clipboard path remain a fallback for
applications that do not expose ValuePattern or TextPattern.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any


UIA_RUNTIME_ID_PROPERTY_ID = 30000
UIA_PROCESS_ID_PROPERTY_ID = 30002
UIA_CONTROL_TYPE_PROPERTY_ID = 30003
UIA_NAME_PROPERTY_ID = 30005
UIA_AUTOMATION_ID_PROPERTY_ID = 30011
UIA_CLASS_NAME_PROPERTY_ID = 30012
UIA_HAS_KEYBOARD_FOCUS_PROPERTY_ID = 30008
UIA_IS_OFFSCREEN_PROPERTY_ID = 30022
UIA_VALUE_PATTERN_ID = 10002
UIA_TEXT_PATTERN_ID = 10014
UIA_EDIT_CONTROL_TYPE_ID = 50004
UIA_DOCUMENT_CONTROL_TYPE_ID = 50030
TREE_SCOPE_DESCENDANTS = 4


@dataclass(frozen=True)
class UIATextControlRef:
    process_id: int
    runtime_id: tuple[int, ...]
    control_type_id: int
    name: str = ""
    automation_id: str = ""
    class_name: str = ""


class WindowsUIATextBridge:
    """Resolve, read, focus and update one UIA text control."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows UI Automation is only available on Windows")
        from comtypes.client import CreateObject, GetModule

        self._types = GetModule("UIAutomationCore.dll")
        self._automation = CreateObject(
            self._types.CUIAutomation,
            interface=self._types.IUIAutomation,
        )

    def capture_focused_text_control(
        self, process_id: int
    ) -> UIATextControlRef | None:
        element = self._automation.GetFocusedElement()
        if element is None:
            return None
        if int(self._property(element, UIA_PROCESS_ID_PROPERTY_ID, 0)) != int(
            process_id
        ):
            return None
        control_type = int(
            self._property(element, UIA_CONTROL_TYPE_PROPERTY_ID, 0)
        )
        if control_type not in {
            UIA_EDIT_CONTROL_TYPE_ID,
            UIA_DOCUMENT_CONTROL_TYPE_ID,
        }:
            return None
        if not self._supports_text(element):
            return None
        return self._reference(element, process_id, control_type)

    def read_text(self, target: UIATextControlRef, window_handle: int) -> str:
        element = self._resolve(target, window_handle)
        raw = self._read_value(element)
        return self._normalize_value(raw, target)

    def set_text(
        self,
        target: UIATextControlRef,
        window_handle: int,
        text: str,
    ) -> None:
        element = self._resolve(target, window_handle)
        pattern = element.GetCurrentPattern(UIA_VALUE_PATTERN_ID)
        value_pattern = pattern.QueryInterface(
            self._types.IUIAutomationValuePattern
        )
        value_pattern.SetValue(str(text or ""))

    def focus(self, target: UIATextControlRef, window_handle: int) -> None:
        self._resolve(target, window_handle).SetFocus()

    def _resolve(self, target: UIATextControlRef, window_handle: int) -> Any:
        root = self._automation.ElementFromHandle(int(window_handle))
        condition = self._automation.CreatePropertyCondition(
            UIA_CONTROL_TYPE_PROPERTY_ID,
            int(target.control_type_id),
        )
        matches = root.FindAll(TREE_SCOPE_DESCENDANTS, condition)
        best = None
        best_score = -1
        target_family = self._class_family(target.class_name)
        for index in range(int(matches.Length)):
            element = matches.GetElement(index)
            if int(self._property(element, UIA_PROCESS_ID_PROPERTY_ID, 0)) != int(
                target.process_id
            ):
                continue
            runtime_id = self._runtime_id(element)
            if target.runtime_id and runtime_id == target.runtime_id:
                return element
            if bool(self._property(element, UIA_IS_OFFSCREEN_PROPERTY_ID, False)):
                continue

            name = str(self._property(element, UIA_NAME_PROPERTY_ID, "") or "")
            automation_id = str(
                self._property(element, UIA_AUTOMATION_ID_PROPERTY_ID, "") or ""
            )
            class_name = str(
                self._property(element, UIA_CLASS_NAME_PROPERTY_ID, "") or ""
            )
            score = 0
            if target.automation_id and automation_id == target.automation_id:
                score += 20
            if target.class_name and class_name == target.class_name:
                score += 12
            elif target_family and self._class_family(class_name) == target_family:
                score += 9
            if target.name and name == target.name:
                score += 5
            if bool(
                self._property(
                    element,
                    UIA_HAS_KEYBOARD_FOCUS_PROPERTY_ID,
                    False,
                )
            ):
                score += 3
            if score > best_score and self._supports_text(element):
                best = element
                best_score = score

        # A runtime ID can change after a Chromium re-render. Require a strong
        # class/automation match before accepting the replacement element.
        if best is None or best_score < 9:
            raise RuntimeError("锁定的 UI Automation 文本框已经失效")
        return best

    def _supports_text(self, element: Any) -> bool:
        try:
            element.GetCurrentPattern(UIA_VALUE_PATTERN_ID)
            return True
        except Exception:
            try:
                element.GetCurrentPattern(UIA_TEXT_PATTERN_ID)
                return True
            except Exception:
                return False

    def _read_value(self, element: Any) -> str:
        try:
            pattern = element.GetCurrentPattern(UIA_VALUE_PATTERN_ID)
            value_pattern = pattern.QueryInterface(
                self._types.IUIAutomationValuePattern
            )
            return str(value_pattern.CurrentValue or "")
        except Exception:
            pattern = element.GetCurrentPattern(UIA_TEXT_PATTERN_ID)
            text_pattern = pattern.QueryInterface(
                self._types.IUIAutomationTextPattern
            )
            return str(text_pattern.DocumentRange.GetText(-1) or "")

    def _reference(
        self,
        element: Any,
        process_id: int,
        control_type: int,
    ) -> UIATextControlRef:
        return UIATextControlRef(
            process_id=int(process_id),
            runtime_id=self._runtime_id(element),
            control_type_id=int(control_type),
            name=str(self._property(element, UIA_NAME_PROPERTY_ID, "") or ""),
            automation_id=str(
                self._property(element, UIA_AUTOMATION_ID_PROPERTY_ID, "") or ""
            ),
            class_name=str(
                self._property(element, UIA_CLASS_NAME_PROPERTY_ID, "") or ""
            ),
        )

    @staticmethod
    def _runtime_id(element: Any) -> tuple[int, ...]:
        try:
            return tuple(int(value) for value in element.GetRuntimeId())
        except Exception:
            return ()

    @staticmethod
    def _property(element: Any, property_id: int, default: Any) -> Any:
        try:
            return element.GetCurrentPropertyValue(int(property_id))
        except Exception:
            return default

    @staticmethod
    def _class_family(class_name: str) -> str:
        return str(class_name or "").strip().split(" ", 1)[0].casefold()

    @staticmethod
    def _normalize_value(value: str, target: UIATextControlRef) -> str:
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        # Chromium contenteditable controls may expose their placeholder as a
        # final accessibility line. It is not part of the user's draft.
        if "prosemirror" in target.class_name.casefold() and target.name:
            lines = text.split("\n")
            if lines and lines[-1].strip() == target.name.strip():
                lines.pop()
            while lines and not lines[0]:
                lines.pop(0)
            text = "\n".join(lines)
        return text
