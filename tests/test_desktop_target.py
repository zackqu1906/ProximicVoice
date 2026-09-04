from __future__ import annotations

import os
import sys

import pytest

from proximic_ring.desktop_target import (
    DesktopTargetRef,
    DesktopTextSnapshot,
    MacOSDesktopTextTarget,
    WindowsDesktopTextTarget,
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
