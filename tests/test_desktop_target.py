from __future__ import annotations


import pytest

from proximic_ring.desktop_target import (
    DesktopTargetRef,
    DesktopTextSnapshot,
    WindowsDesktopTextTarget,
    macos_texts_equivalent,
)
from proximic_ring.windows_uia import UIATextControlRef, WindowsUIATextBridge


def test_macos_edit_text_comparison_ignores_transport_normalization() -> None:
    assert macos_texts_equivalent("第一行\r\nCafe\u0301", "第一行\nCaf\u00e9")
    assert macos_texts_equivalent("第一段\u2028第二段", "第一段\n第二段")
    assert not macos_texts_equivalent("少了一个字", "没有少一个字")


def test_native_shortcuts_do_not_activate_copy_or_wait(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("native dispatch must not activate or wait")

    monkeypatch.setattr("proximic_ring.desktop_target.time.sleep", forbidden)
    target = DesktopTargetRef(1, 2, "测试窗口", process_id=30)
    calls = []
    windows = object.__new__(WindowsDesktopTextTarget)
    windows._activate = forbidden
    windows._hotkey = lambda *keys: calls.append(("ctrl", keys))
    windows.send_native_undo(target)
    assert calls == [
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
