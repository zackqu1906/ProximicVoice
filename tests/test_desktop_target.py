from __future__ import annotations

from proximic_ring.desktop_target import (
    DesktopTargetRef,
    DesktopTextSnapshot,
    WindowsDesktopTextTarget,
)


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
