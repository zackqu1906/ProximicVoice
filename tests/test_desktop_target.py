from __future__ import annotations

import ctypes
import os
import sys
import time

import pytest

from proximic_ring.desktop_target import (
    DesktopTargetRef,
    DesktopTextSnapshot,
    MacOSDesktopTextTarget,
    WindowsDesktopTextTarget,
    _CFRange,
    _MacOSAccessibilityTextBridge,
    macos_texts_equivalent,
)
from proximic_ring.windows_uia import UIATextControlRef, WindowsUIATextBridge


def test_macos_target_reads_injects_and_replaces_external_text(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class Clipboard:
        def __init__(self) -> None:
            self.value = "用户原剪贴板"

        def snapshot(self):
            return self.value

        def restore(self, snapshot):
            self.value = snapshot
            calls.append(("restore", snapshot))

        def set_text(self, text):
            self.value = text

        def text(self):
            return self.value

    clipboard = Clipboard()

    class Injector:
        def is_trusted(self, *, prompt=False):
            calls.append(("trusted", prompt))
            return True

        def require_accessibility(self):
            calls.append("accessibility")

        def command_key(self, key):
            calls.append(("command", key))
            if key == MacOSDesktopTextTarget.KEY_C:
                clipboard.value = "外部文本框内容"

        def inject(self, text: str) -> None:
            calls.append(("inject", text))

        def press_key(self, key):
            calls.append(("press", key))

    class AccessibilityText:
        def focused_control_id(self, process_id):
            calls.append(("ax-id", process_id))
            return "ax:123"

        def focused_bounds(self, process_id):
            calls.append(("ax-bounds", process_id))
            return 120, 240, 500, 180

        def focused_caret_bounds(self, process_id):
            calls.append(("ax-caret", process_id))
            return 410, 286, 2, 19

        def focused_selected_range(self, process_id):
            calls.append(("ax-selection", process_id))
            return 3, 0

        def set_focused_selected_range(self, process_id, selection):
            calls.append(("ax-selection-restore", process_id, selection))
            return True

        def read_focused_value(self, process_id):
            calls.append(("ax-read", process_id))
            return None

        def set_focused_value(self, process_id, text):
            calls.append(("ax-set", process_id, text))
            return False

    adapter = MacOSDesktopTextTarget(
        clipboard,
        injector=Injector(),
        shortcut_settle_s=0.02,
        accessibility_text=AccessibilityText(),
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (4321, "TextEdit"))
    target = adapter.capture_reference()
    monkeypatch.setattr(adapter, "_activate", lambda value: calls.append(("activate", value)))

    assert adapter.request_accessibility(prompt=True) is True
    assert (
        target.screen_x,
        target.screen_y,
        target.screen_width,
        target.screen_height,
    ) == (120, 240, 500, 180)
    assert (
        target.caret_x,
        target.caret_y,
        target.caret_width,
        target.caret_height,
    ) == (410, 286, 2, 19)
    assert target.accessibility_id == "ax:123"
    snapshot = adapter.capture_text(target)
    adapter.inject(target, "听写内容")
    adapter.replace(snapshot, "修改后内容")
    adapter.replace(snapshot, "")

    assert snapshot == DesktopTextSnapshot(target, "外部文本框内容")
    assert clipboard.value == "修改后内容"
    assert ("command", MacOSDesktopTextTarget.KEY_V) in calls
    assert ("ax-selection-restore", 4321, (3, 0)) in calls
    assert ("press", MacOSDesktopTextTarget.KEY_RIGHT) not in calls
    assert calls.index(("ax-selection-restore", 4321, (3, 0))) < calls.index(
        ("command", MacOSDesktopTextTarget.KEY_V)
    )
    assert ("inject", "听写内容") not in calls
    assert ("inject", "修改后内容") not in calls
    assert calls.count(("command", MacOSDesktopTextTarget.KEY_V)) == 2
    assert ("press", MacOSDesktopTextTarget.KEY_DELETE) in calls


def test_macos_edit_falls_back_to_verified_paste_when_ax_lies(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class Clipboard:
        value = "用户原剪贴板"

        def set_text(self, text):
            self.value = text

        def text(self):
            return self.value

    clipboard = Clipboard()

    class Injector:
        def command_key(self, key):
            calls.append(("command", key, clipboard.value))

        def press_key(self, key):
            calls.append(("press", key))

    class AccessibilityText:
        def set_focused_value(self, process_id, text):
            calls.append(("ax-set", process_id, text))
            return True

        def read_focused_value(self, process_id):
            return "仍然是原文"

    adapter = MacOSDesktopTextTarget(
        clipboard,
        injector=Injector(),
        shortcut_settle_s=0.02,
        accessibility_text=AccessibilityText(),
    )
    target = DesktopTargetRef(0, 0, "微信", process_id=4321)
    monkeypatch.setattr(adapter, "_activate", lambda value: None)

    adapter.replace(DesktopTextSnapshot(target, "仍然是原文"), "修改后内容")

    assert ("command", MacOSDesktopTextTarget.KEY_A, "用户原剪贴板") in calls
    assert ("command", MacOSDesktopTextTarget.KEY_V, "修改后内容") in calls


def test_macos_edit_text_comparison_ignores_transport_normalization() -> None:
    assert macos_texts_equivalent("第一行\r\nCafe\u0301", "第一行\nCaf\u00e9")
    assert macos_texts_equivalent("第一段\u2028第二段", "第一段\n第二段")
    assert not macos_texts_equivalent("少了一个字", "没有少一个字")


def test_macos_target_prefers_accessibility_value_for_exact_replace(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class Clipboard:
        def snapshot(self):
            raise AssertionError("AXValue read should not use the clipboard")

    class Injector:
        def require_accessibility(self):
            calls.append("accessibility")

        def command_key(self, key):
            raise AssertionError("AXValue replace should not send Command+A")

    class AccessibilityText:
        value = "原文"

        def read_focused_value(self, process_id):
            calls.append(("ax-read", process_id))
            return self.value

        def set_focused_value(self, process_id, text):
            calls.append(("ax-set", process_id, text))
            self.value = text
            return True

    accessibility_text = AccessibilityText()
    adapter = MacOSDesktopTextTarget(
        Clipboard(),
        injector=Injector(),
        shortcut_settle_s=0.02,
        accessibility_text=accessibility_text,
    )
    target = DesktopTargetRef(0, 0, "TextEdit", process_id=4321)
    monkeypatch.setattr(adapter, "_activate", lambda value: calls.append(("activate", value)))

    snapshot = adapter.capture_text(target)
    adapter.replace(snapshot, "新文本")
    verified = adapter.capture_text(target)

    assert snapshot.text == "原文"
    assert verified.text == "新文本"
    assert ("ax-set", 4321, "新文本") in calls


def test_macos_manual_observation_never_activates_selects_or_copies(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class Clipboard:
        def snapshot(self):
            raise AssertionError("manual observation must not touch the clipboard")

    class Injector:
        def command_key(self, _key):
            raise AssertionError("manual observation must not send shortcuts")

    class AccessibilityText:
        def focused_bounds(self, process_id):
            calls.append(("bounds", process_id))
            return 100, 200, 500, 80

        def read_focused_value(self, process_id):
            calls.append(("read", process_id))
            return "用户正在手写"

    adapter = MacOSDesktopTextTarget(
        Clipboard(),
        injector=Injector(),
        accessibility_text=AccessibilityText(),
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (4321, "编辑器"))
    monkeypatch.setattr(
        adapter,
        "_activate",
        lambda _target: (_ for _ in ()).throw(
            AssertionError("manual observation must not activate the target")
        ),
    )
    target = DesktopTargetRef(
        0,
        0,
        "编辑器",
        process_id=4321,
        screen_x=100,
        screen_y=200,
        screen_width=500,
        screen_height=80,
    )

    assert adapter.observe_text(target).text == "用户正在手写"
    assert calls == [("bounds", 4321), ("read", 4321)]


def test_macos_context_observation_uses_only_bounded_ax_reader(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class Clipboard:
        def snapshot(self):
            raise AssertionError("context observation must not use clipboard")

    class Injector:
        def command_key(self, _key):
            raise AssertionError("context observation must not send shortcuts")

    class AccessibilityText:
        last_context_read_method = "system.parent1.AXStringForRange"
        last_context_source_char_count = 2460

        def read_focused_context(self, process_id, *, max_chars, anchor):
            calls.append(("context", process_id, max_chars, anchor))
            return "当前输入框的真实文本"

    adapter = MacOSDesktopTextTarget(
        Clipboard(),
        injector=Injector(),
        accessibility_text=AccessibilityText(),
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (4321, "微信"))
    target = DesktopTargetRef(
        0,
        0,
        "微信",
        process_id=4321,
        caret_x=320,
        caret_y=245,
    )

    snapshot = adapter.observe_context_text(target, max_chars=1600)

    assert snapshot.text == "当前输入框的真实文本"
    assert calls == [("context", 4321, 1600, (320, 245))]
    assert adapter.last_context_read_method == (
        "system.parent1.AXStringForRange"
    )
    assert adapter.last_context_source_char_count == 2460


def test_macos_wechat_context_temporarily_enables_lazy_accessibility(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    calls: list[object] = []

    class Clipboard:
        def snapshot(self):
            raise AssertionError("context observation must not use clipboard")

    class Injector:
        def command_key(self, _key):
            raise AssertionError("context observation must not send shortcuts")

    class AccessibilityText:
        def read_focused_context(self, process_id, *, max_chars, anchor):
            calls.append(("read", process_id, max_chars, anchor))
            return None if len(calls) == 1 else "延迟暴露的微信输入内容"

        def manual_accessibility_state(self, process_id):
            calls.append(("state", process_id))
            return False

        def set_manual_accessibility(self, process_id, *, enabled):
            calls.append(("set", process_id, enabled))
            return "enabled" if enabled else "disabled"

    adapter = MacOSDesktopTextTarget(
        Clipboard(),
        injector=Injector(),
        accessibility_text=AccessibilityText(),
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (506, "WeChat"))
    target = DesktopTargetRef(
        0,
        0,
        "WeChat",
        process_id=506,
        process_name="WeChat",
        caret_x=320,
        caret_y=245,
    )

    snapshot = adapter.observe_context_text(target, max_chars=1600)

    assert snapshot.text == "延迟暴露的微信输入内容"
    assert calls == [
        ("read", 506, 1600, (320, 245)),
        ("state", 506),
        ("set", 506, True),
        ("read", 506, 1600, (320, 245)),
        ("set", 506, False),
    ]
    assert adapter.last_context_accessibility_probe == (
        "enabled_readable;restore:disabled"
    )


def test_macos_wechat_context_reports_manual_accessibility_rejection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    class AccessibilityText:
        def read_focused_context(self, *_args, **_kwargs):
            return None

        def manual_accessibility_state(self, _process_id):
            return None

        def set_manual_accessibility(self, _process_id, *, enabled):
            assert enabled is True
            return "attribute_unsupported:-25205"

    adapter = MacOSDesktopTextTarget(
        object(),
        injector=object(),
        accessibility_text=AccessibilityText(),
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (506, "WeChat"))
    target = DesktopTargetRef(
        0,
        0,
        "WeChat",
        process_id=506,
        process_name="WeChat",
    )

    with pytest.raises(
        RuntimeError,
        match=r"AXManualAccessibility=attribute_unsupported:-25205",
    ):
        adapter.observe_context_text(target, max_chars=1600)

    assert adapter.last_context_accessibility_probe == (
        "attribute_unsupported:-25205"
    )


def test_macos_context_reader_walks_to_editable_parent_without_cache() -> None:
    bridge = object.__new__(_MacOSAccessibilityTextBridge)
    bridge._last_context_read_method = ""
    bridge._last_context_source_char_count = None
    released: list[int] = []
    bridge._copy_focused_elements = lambda _pid: [
        (1, 10, "application"),
        (2, 30, "system"),
    ]
    bridge._is_editable_text_element = lambda element: element == 20
    bridge._copy_text_attribute = lambda element, name: (
        "微信输入框里的真实上文"
        if element == 20 and name == "AXValue"
        else None
    )
    bridge._copy_attribute_value = lambda element, name: (
        20 if element == 10 and name == "AXParent" else None
    )
    bridge._read_parameterized_context = lambda *_args, **_kwargs: (
        None,
        "",
        None,
    )
    bridge._release = lambda *values: released.extend(
        int(value) for value in values if value
    )

    text = bridge.read_focused_context(475, max_chars=1600)

    assert text == "微信输入框里的真实上文"
    assert bridge.last_context_read_method == "application.parent1.AXValue"
    assert bridge.last_context_source_char_count == len(text)
    assert set((1, 2, 10, 20, 30)).issubset(released)


def test_macos_context_reader_uses_parameterized_text_on_system_focus() -> None:
    bridge = object.__new__(_MacOSAccessibilityTextBridge)
    bridge._last_context_read_method = ""
    bridge._last_context_source_char_count = None
    bridge._copy_focused_elements = lambda _pid: [(2, 30, "system")]
    bridge._is_editable_text_element = lambda _element: True
    bridge._copy_text_attribute = lambda *_args: None
    bridge._read_parameterized_context = lambda *_args, **_kwargs: (
        "范围读取的真实文本",
        "AXStringForRange",
        2400,
    )
    bridge._copy_attribute_value = lambda *_args: None
    bridge._release = lambda *_values: None

    text = bridge.read_focused_context(475, max_chars=1600)

    assert text == "范围读取的真实文本"
    assert bridge.last_context_read_method == (
        "system.focused.AXStringForRange"
    )
    assert bridge.last_context_source_char_count == 2400


def test_macos_context_reader_falls_back_to_verified_system_focus() -> None:
    bridge = object.__new__(_MacOSAccessibilityTextBridge)
    released: list[int] = []

    class Services:
        @staticmethod
        def AXUIElementCreateApplication(_process_id):
            return 1

        @staticmethod
        def AXUIElementCreateSystemWide():
            return 2

        @staticmethod
        def AXUIElementGetPid(_element, output):
            output._obj.value = 475
            return 0

    bridge._application_services = Services()
    bridge._copy_attribute_value = lambda element, name: (
        30 if element == 2 and name == "AXFocusedUIElement" else None
    )
    bridge._release = lambda *values: released.extend(
        int(value) for value in values if value
    )

    assert bridge._copy_focused_elements(475) == [(2, 30, "system")]
    assert 1 in released


def test_macos_context_reader_finds_focused_editor_below_window() -> None:
    bridge = object.__new__(_MacOSAccessibilityTextBridge)
    bridge._last_context_read_method = ""
    bridge._last_context_source_char_count = None
    released: list[int] = []
    bridge._copy_focused_elements = lambda _pid: []
    bridge._copy_application_element = lambda _pid, name: (
        (1, 10) if name == "AXFocusedWindow" else None
    )
    bridge._copy_element_at_position = lambda *_args: None
    bridge._copy_recent_children = lambda element: {
        10: ([20], 100),
        20: ([30], 101),
        30: ([], None),
    }[element]
    bridge._is_ax_focused = lambda element: element == 30
    bridge._is_editable_text_element = lambda element: element == 30
    bridge._copy_text_attribute = lambda element, name: (
        "微信窗口子树中的输入内容"
        if element == 30 and name == "AXValue"
        else None
    )
    bridge._copy_attribute_value = lambda *_args: None
    bridge._read_parameterized_context = lambda *_args, **_kwargs: (
        None,
        "",
        None,
    )
    bridge._release = lambda *values: released.extend(
        int(value) for value in values if value
    )

    text = bridge.read_focused_context(475, max_chars=1600)

    assert text == "微信窗口子树中的输入内容"
    assert bridge.last_context_read_method == (
        "window.descendant2.focused.AXValue"
    )
    assert {1, 10, 100, 101}.issubset(released)


def test_macos_point_context_requires_independent_ax_focus() -> None:
    bridge = object.__new__(_MacOSAccessibilityTextBridge)
    bridge._last_context_read_method = ""
    bridge._last_context_source_char_count = None
    bridge._copy_focused_elements = lambda _pid: []
    bridge._copy_application_element = lambda *_args: None
    bridge._copy_element_at_position = lambda _pid, _anchor: (2, 30)
    bridge._is_editable_text_element = lambda _element: True
    bridge._is_ax_focused = lambda _element: False
    bridge._copy_text_attribute = lambda *_args: "错误的搜索框内容"
    bridge._copy_attribute_value = lambda *_args: None
    bridge._read_context_from_focused_descendant = lambda *_args, **_kwargs: None
    bridge._release = lambda *_values: None

    text = bridge.read_focused_context(
        475, max_chars=1600, anchor=(320, 245)
    )

    assert text is None
    assert bridge.last_context_read_method == ""


def test_macos_context_child_scan_is_bounded_and_prefers_recent_children() -> None:
    bridge = object.__new__(_MacOSAccessibilityTextBridge)
    requests: list[tuple[int, int]] = []
    released: list[int] = []

    class Services:
        @staticmethod
        def AXUIElementGetAttributeValueCount(_element, _attribute, output):
            output._obj.value = 70
            return 0

        @staticmethod
        def AXUIElementCopyAttributeValues(
            _element, _attribute, start, count, output
        ):
            requests.append((start, count))
            output._obj.value = 500
            return 0

    class CoreFoundation:
        @staticmethod
        def CFArrayGetCount(_array):
            return 2

        @staticmethod
        def CFArrayGetValueAtIndex(_array, index):
            return (21, 22)[index]

    bridge._application_services = Services()
    bridge._core_foundation = CoreFoundation()
    bridge._cf_string = lambda name: 99 if name == "AXChildren" else 0
    bridge._release = lambda *values: released.extend(
        int(value) for value in values if value
    )

    children, owned_array = bridge._copy_recent_children(10)

    assert children == [21, 22]
    assert owned_array == 500
    assert requests == [(6, 64)]
    assert released == [99]


def test_macos_parameterized_context_reads_only_bounded_tail() -> None:
    bridge = object.__new__(_MacOSAccessibilityTextBridge)
    captured_ranges: list[tuple[int, int]] = []
    released: list[int] = []

    class Services:
        @staticmethod
        def AXValueCreate(value_type, pointer):
            assert value_type == 4
            value = ctypes.cast(pointer, ctypes.POINTER(_CFRange)).contents
            captured_ranges.append((int(value.location), int(value.length)))
            return 88

    bridge._application_services = Services()
    bridge._copy_attribute_value = lambda _element, name: (
        77 if name == "AXNumberOfCharacters" else None
    )
    bridge._cf_number = lambda value: 2400 if value == 77 else None
    bridge._copy_parameterized_text = lambda element, range_value: (
        "最后一段真实文本",
        "AXStringForRange",
    )
    bridge._release = lambda *values: released.extend(
        int(value) for value in values if value
    )

    result = bridge._read_parameterized_context(30, max_chars=1600)

    assert result == ("最后一段真实文本", "AXStringForRange", 2400)
    assert captured_ranges == [(800, 1600)]
    assert {77, 88}.issubset(released)


def test_macos_focused_observation_revalidates_a_rebuilt_web_field(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class Clipboard:
        def snapshot(self):
            raise AssertionError("focused observation must not touch the clipboard")

    class Injector:
        def command_key(self, _key):
            raise AssertionError("focused observation must not send shortcuts")

    class AccessibilityText:
        def focused_bounds(self, _process_id):
            raise AssertionError("stale geometry must not block revalidation")

        def read_focused_value(self, process_id):
            calls.append(("read", process_id))
            return "刚才应用后的文本"

    adapter = MacOSDesktopTextTarget(
        Clipboard(),
        injector=Injector(),
        accessibility_text=AccessibilityText(),
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (4321, "网页"))
    target = DesktopTargetRef(
        0,
        0,
        "网页",
        process_id=4321,
        accessibility_id="ax:stale-wrapper",
        screen_x=100,
        screen_y=200,
        screen_width=500,
        screen_height=80,
    )

    assert adapter.observe_focused_text(target).text == "刚才应用后的文本"
    assert calls == [("read", 4321)]


def test_macos_injection_waits_for_current_clipboard_before_paste(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class DelayedClipboard:
        def __init__(self) -> None:
            self.current = "上一条听写"
            self.pending = ""
            self.reads = 0

        def set_text(self, text):
            self.pending = text
            self.reads = 0
            calls.append(("set", text))

        def text(self):
            self.reads += 1
            if self.reads >= 2:
                self.current = self.pending
            calls.append(("read", self.current))
            return self.current

    clipboard = DelayedClipboard()

    class Injector:
        def command_key(self, key):
            calls.append(("paste", key, clipboard.current))

    class AccessibilityText:
        def read_focused_value(self, process_id):
            return None

    adapter = MacOSDesktopTextTarget(
        clipboard,
        injector=Injector(),
        copy_timeout_s=0.2,
        shortcut_settle_s=0.02,
        accessibility_text=AccessibilityText(),
    )
    target = DesktopTargetRef(0, 0, "微信", process_id=4321)
    monkeypatch.setattr(
        adapter, "_activate", lambda value: calls.append(("activate", value))
    )

    adapter.inject(target, "这一条听写")

    activate_index = next(i for i, item in enumerate(calls) if item[0] == "activate")
    set_index = next(i for i, item in enumerate(calls) if item[0] == "set")
    assert activate_index < set_index
    assert ("paste", MacOSDesktopTextTarget.KEY_V, "这一条听写") in calls


def test_macos_injection_never_pastes_when_clipboard_stays_stale(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class StaleClipboard:
        def set_text(self, text):
            calls.append(("set", text))

        def text(self):
            return "上一条听写"

    class Injector:
        def command_key(self, key):
            calls.append(("paste", key))

    class AccessibilityText:
        def read_focused_value(self, process_id):
            return None

    adapter = MacOSDesktopTextTarget(
        StaleClipboard(),
        injector=Injector(),
        copy_timeout_s=0.01,
        copy_attempts=1,
        shortcut_settle_s=0.02,
        accessibility_text=AccessibilityText(),
    )
    target = DesktopTargetRef(0, 0, "微信", process_id=4321)
    monkeypatch.setattr(adapter, "_activate", lambda value: None)

    with pytest.raises(RuntimeError, match="避免插入旧文字"):
        adapter.inject(target, "这一条听写")

    assert not any(item[0] == "paste" for item in calls)


def test_macos_target_rejects_own_process() -> None:
    adapter = object.__new__(MacOSDesktopTextTarget)
    target = DesktopTargetRef(0, 0, "Proximic Voice", process_id=os.getpid())
    with pytest.raises(RuntimeError, match="另一个应用"):
        adapter._activate(target)


def test_macos_target_reads_live_caret_bounds_without_moving_focus(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    class AccessibilityText:
        def focused_caret_bounds(self, process_id):
            assert process_id == 4321
            return 410, 242, 2, 19

    adapter = MacOSDesktopTextTarget(
        object(),
        injector=object(),
        accessibility_text=AccessibilityText(),
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (4321, "编辑器"))
    target = DesktopTargetRef(0, 0, "编辑器", process_id=4321)

    assert adapter.caret_bounds(target) == (410, 242, 2, 19)


def test_macos_foreground_requires_the_same_text_field_when_bounds_are_known(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    class AccessibilityText:
        def __init__(self):
            self.bounds = (100, 200, 500, 80)
            self.control_id = "ax:field-a"

        def focused_control_id(self, _process_id):
            return self.control_id

        def focused_bounds(self, _process_id):
            return self.bounds

    accessibility = AccessibilityText()
    adapter = MacOSDesktopTextTarget(
        object(),
        injector=object(),
        accessibility_text=accessibility,
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (4321, "编辑器"))
    target = DesktopTargetRef(
        0,
        0,
        "编辑器",
        process_id=4321,
        screen_x=100,
        screen_y=200,
        screen_width=500,
        screen_height=80,
        accessibility_id="ax:field-a",
    )

    assert adapter.is_foreground(target) is True
    # Resizing the same accessibility field does not break its identity.
    accessibility.bounds = (100, 400, 500, 80)
    assert adapter.is_foreground(target) is True
    accessibility.control_id = "ax:field-b"
    assert adapter.is_foreground(target) is False
    # Electron editors may recreate the AX wrapper and expand upward as text is
    # inserted. Matching top/bottom geometry keeps this as the same field.
    accessibility.bounds = (100, 160, 500, 120)
    assert adapter.is_foreground(target) is True
    accessibility.bounds = (100, 400, 500, 80)
    assert adapter.is_foreground(target) is False
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (9999, "其他"))
    assert adapter.is_foreground(target) is False


def test_macos_foreground_keeps_an_unresolved_web_editor_usable(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    adapter = MacOSDesktopTextTarget(
        object(),
        injector=object(),
        accessibility_text=object(),
    )
    target = DesktopTargetRef(0, 0, "ChatGPT", process_id=4321)

    monkeypatch.setattr(
        adapter, "_frontmost_application", lambda: (4321, "ChatGPT")
    )
    assert adapter.is_foreground(target) is True
    assert adapter.is_application_foreground(target) is True

    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (9999, "其他"))
    assert adapter.is_foreground(target) is False
    assert adapter.is_application_foreground(target) is False


def test_macos_activate_has_no_delay_when_target_is_already_frontmost(
    monkeypatch,
) -> None:
    adapter = object.__new__(MacOSDesktopTextTarget)
    target = DesktopTargetRef(0, 0, "微信", process_id=4321)
    adapter._frontmost_application = lambda: (4321, "微信")
    monkeypatch.setattr(
        time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(
            AssertionError("already-frontmost activation must not sleep")
        ),
    )

    adapter._activate(target)


def test_macos_target_uses_last_pointer_when_editor_hides_ax_caret(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    class AccessibilityText:
        def focused_bounds(self, _process_id):
            return 100, 200, 500, 100

        def focused_caret_bounds(self, _process_id):
            return 0, 0, 0, 0

    adapter = MacOSDesktopTextTarget(
        object(),
        injector=object(),
        accessibility_text=AccessibilityText(),
    )
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (4321, "编辑器"))
    monkeypatch.setattr(adapter, "_pointer_position", lambda: (320, 245))

    target = adapter.capture_reference()

    assert (target.caret_x, target.caret_y, target.caret_height) == (320, 245, 18)


def test_macos_caret_search_walks_from_web_child_to_editable_parent() -> None:
    bridge = object.__new__(_MacOSAccessibilityTextBridge)
    released: list[int] = []

    class Services:
        @staticmethod
        def AXUIElementCreateApplication(_process_id):
            return 1

        @staticmethod
        def AXUIElementCopyAttributeValue(element, attribute, output):
            if element == 1 and attribute == 101:
                output._obj.value = 10
                return 0
            if element == 10 and attribute == 102:
                output._obj.value = 20
                return 0
            return 1

    bridge._application_services = Services()
    bridge._cf_string = lambda value: {
        "AXFocusedUIElement": 101,
        "AXParent": 102,
    }[value]
    bridge._element_caret_bounds = lambda element: (
        (410, 242, 2, 19) if element == 20 else (0, 0, 0, 0)
    )
    bridge._release = lambda *values: released.extend(
        int(value) for value in values if value
    )

    assert bridge.focused_caret_bounds(4321) == (410, 242, 2, 19)
    assert 20 in released


def test_macos_undo_targets_the_locked_external_control() -> None:
    adapter = object.__new__(MacOSDesktopTextTarget)
    target = DesktopTargetRef(1, 2, "测试窗口", process_id=30)
    calls: list[object] = []

    adapter._activate = lambda value: calls.append(("activate", value))
    adapter._shortcut_settle_s = 0

    class Injector:
        def command_key(self, key):
            calls.append(("command", key))

    adapter._injector = Injector()

    adapter.undo(target)

    assert calls == [
        ("activate", target),
        ("command", MacOSDesktopTextTarget.KEY_Z),
    ]


def test_native_shortcuts_do_not_activate_copy_or_wait(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("native dispatch must not activate or wait")

    monkeypatch.setattr("proximic_ring.desktop_target.time.sleep", forbidden)
    target = DesktopTargetRef(1, 2, "测试窗口", process_id=30)
    calls = []
    mac = object.__new__(MacOSDesktopTextTarget)
    mac._activate = forbidden

    class Injector:
        def command_key(self, key):
            calls.append(("cmd", key))

    mac._injector = Injector()
    windows = object.__new__(WindowsDesktopTextTarget)
    windows._activate = forbidden
    windows._hotkey = lambda *keys: calls.append(("ctrl", keys))
    mac.send_native_undo(target)
    windows.send_native_undo(target)
    assert calls == [
        ("cmd", MacOSDesktopTextTarget.KEY_Z),
        ("ctrl", (windows.VK_CONTROL, 0x5A)),
    ]


def test_capture_retries_transient_empty_clipboard_and_restores_original() -> None:
    adapter = object.__new__(WindowsDesktopTextTarget)
    target = DesktopTargetRef(1, 2, "测试窗口")
    calls: list[object] = []

    class Clipboard:
        def __init__(self) -> None:
            self.value = "用户原剪贴板"
            self.copy_requested = False
            self.read_count = 0

        def snapshot(self):
            return self.value

        def restore(self, snapshot):
            self.value = snapshot
            calls.append(("restore", snapshot))

        def set_text(self, text):
            self.value = text
            self.copy_requested = False
            self.read_count = 0

        def text(self):
            if not self.copy_requested:
                return self.value
            self.read_count += 1
            if self.read_count == 1:
                return ""
            return "文本框内容"

    clipboard = Clipboard()
    adapter._clipboard = clipboard
    adapter._copy_timeout_s = 0.1
    adapter._copy_attempts = 2
    adapter._shortcut_settle_s = 0.01
    adapter._activate = lambda value: calls.append(("activate", value))

    def hotkey(*keys):
        calls.append(("hotkey", keys))
        if keys == (adapter.VK_CONTROL, adapter.VK_C):
            clipboard.copy_requested = True

    adapter._hotkey = hotkey

    snapshot = adapter.capture_text(target)

    assert snapshot == DesktopTextSnapshot(target, "文本框内容")
    assert clipboard.value == "用户原剪贴板"
    assert calls[-1] == ("restore", "用户原剪贴板")


def test_macos_explicit_clear_can_verify_an_empty_control(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[object] = []

    class Clipboard:
        value = "用户原剪贴板"

        def snapshot(self):
            return self.value

        def restore(self, snapshot):
            self.value = snapshot

        def set_text(self, text):
            self.value = text

        def text(self):
            return self.value

    class Injector:
        def require_accessibility(self):
            return None

        def command_key(self, key):
            calls.append(("command", key))

    class AccessibilityText:
        def read_focused_value(self, _process_id):
            return None

        def focused_selected_range(self, _process_id):
            return 0, 0

        def set_focused_selected_range(self, _process_id, _selection):
            return True

    adapter = MacOSDesktopTextTarget(
        Clipboard(),
        injector=Injector(),
        copy_timeout_s=0.2,
        accessibility_text=AccessibilityText(),
    )
    target = DesktopTargetRef(0, 0, "编辑器", process_id=4321)
    monkeypatch.setattr(adapter, "_activate", lambda _target: None)

    snapshot = adapter.capture_text_allowing_empty(target)

    assert snapshot.text == ""
    assert calls.count(("command", MacOSDesktopTextTarget.KEY_C)) == 1


def test_replace_empty_text_selects_all_and_clears_field() -> None:
    adapter = object.__new__(WindowsDesktopTextTarget)
    calls: list[object] = []

    adapter._activate = lambda target: calls.append(("activate", target))
    adapter._hotkey = lambda *keys: calls.append(("hotkey", keys))
    adapter._press_key = lambda key: calls.append(("press", key))
    adapter._shortcut_settle_s = 0

    class Injector:
        def inject(self, text: str) -> None:
            calls.append(("inject", text))

    adapter._injector = Injector()
    target = DesktopTargetRef(1, 2, "测试窗口")

    adapter.replace(DesktopTextSnapshot(target, "待删除文本"), "")

    assert calls == [
        ("activate", target),
        ("hotkey", (adapter.VK_CONTROL, adapter.VK_A)),
        ("press", adapter.VK_BACK),
    ]


def test_windows_undo_targets_the_locked_external_control() -> None:
    adapter = object.__new__(WindowsDesktopTextTarget)
    target = DesktopTargetRef(1, 2, "测试窗口")
    calls: list[object] = []

    adapter._activate = lambda value: calls.append(("activate", value))
    adapter._hotkey = lambda *keys: calls.append(("hotkey", keys))
    adapter._shortcut_settle_s = 0

    adapter.undo(target)

    assert calls == [
        ("activate", target),
        ("hotkey", (adapter.VK_CONTROL, 0x5A)),
    ]


def _codex_target() -> DesktopTargetRef:
    return DesktopTargetRef(
        10,
        20,
        "Codex task",
        process_id=30,
        process_name="ChatGPT.exe",
        uia_control=UIATextControlRef(
            process_id=30,
            runtime_id=(42, 1, 2),
            control_type_id=50004,
            name="随心输入",
            class_name="ProseMirror ProseMirror-focused",
        ),
    )


def test_codex_capture_reads_only_uia_composer_without_clipboard() -> None:
    adapter = object.__new__(WindowsDesktopTextTarget)
    calls: list[object] = []

    class UIA:
        def read_text(self, control, window_handle):
            calls.append(("read", control, window_handle))
            return "只读取当前提问框"

    class Clipboard:
        def snapshot(self):
            raise AssertionError("Codex composer must not use clipboard Select-All")

    adapter._uia = UIA()
    adapter._clipboard = Clipboard()
    target = _codex_target()

    snapshot = adapter.capture_text(target)

    assert snapshot == DesktopTextSnapshot(target, "只读取当前提问框")
    assert calls == [("read", target.uia_control, 10)]


def test_windows_manual_observation_uses_only_uia() -> None:
    adapter = object.__new__(WindowsDesktopTextTarget)
    calls: list[object] = []

    class UIA:
        def read_text(self, control, window_handle):
            calls.append(("read", control, window_handle))
            return "用户正在手写"

    adapter._uia = UIA()
    adapter._clipboard = object()
    target = _codex_target()

    assert adapter.observe_text(target).text == "用户正在手写"
    assert calls == [("read", target.uia_control, 10)]


def test_codex_uia_failure_never_falls_back_to_page_select_all() -> None:
    adapter = object.__new__(WindowsDesktopTextTarget)

    class UIA:
        def read_text(self, _control, _window_handle):
            raise RuntimeError("stale element")

    class Clipboard:
        def snapshot(self):
            raise AssertionError("unsafe clipboard fallback")

    adapter._uia = UIA()
    adapter._clipboard = Clipboard()

    try:
        adapter.capture_text(_codex_target())
    except RuntimeError as exc:
        assert "避免复制整页或历史对话" in str(exc)
    else:
        raise AssertionError("UIA failure must reject the edit")


def test_codex_replace_uses_exact_uia_value_pattern() -> None:
    adapter = object.__new__(WindowsDesktopTextTarget)
    calls: list[object] = []

    class UIA:
        def set_text(self, control, window_handle, text):
            calls.append(("set", control, window_handle, text))

    adapter._uia = UIA()
    adapter._activate = lambda target: calls.append(("activate", target))
    target = _codex_target()

    adapter.replace(DesktopTextSnapshot(target, "旧提问"), "新提问")

    assert calls == [
        ("activate", target),
        ("set", target.uia_control, 10, "新提问"),
    ]


def test_prosemirror_placeholder_is_not_part_of_captured_text() -> None:
    target = _codex_target().uia_control
    assert target is not None

    assert WindowsUIATextBridge._normalize_value(
        "\n随心输入", target
    ) == ""
    assert WindowsUIATextBridge._normalize_value(
        "当前草稿\n随心输入", target
    ) == "当前草稿"
