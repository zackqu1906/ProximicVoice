from __future__ import annotations

import sys

import pytest

from proximic_ring.desktop_target import (
    DesktopTargetRef,
    DesktopTextSnapshot,
    MacOSDesktopTextTarget,
)


def _units(text):
    return len(text.encode("utf-16-le")) // 2


class Editor:
    """Model an editor whose AXValue setter preserves the old caret offset."""

    def __init__(self):
        self.text = "前缀后缀"
        self.selection = (2, 0)
        self.clipboard = ""
        self.ax_write = True
        self.range_write = True
        self.range_read = True
        self.value_read = True
        self.keys = []
        self.value_writes = 0

    def snapshot(self):
        return self.clipboard

    def restore(self, value):
        self.clipboard = value

    def set_text(self, text):
        self.clipboard = text

    def read_focused_value(self, _pid):
        return self.text if self.value_read else None

    def set_focused_value(self, _pid, text):
        if not self.ax_write:
            return False
        self.value_writes += 1
        self.text = text
        self.selection = (min(self.selection[0], _units(text)), 0)
        return True

    def focused_selected_range(self, _pid):
        return self.selection if self.range_read else None

    def set_focused_selected_range(self, _pid, selection):
        if not self.range_write:
            return False
        self.selection = selection
        return True

    def require_accessibility(self):
        pass

    def command_key(self, key):
        self.keys.append(("command", key))
        if key == MacOSDesktopTextTarget.KEY_A:
            self.selection = (0, _units(self.text))
        elif key == MacOSDesktopTextTarget.KEY_C:
            self.clipboard = self.text
        elif key == MacOSDesktopTextTarget.KEY_V:
            start, length = self.selection
            encoded = self.text.encode("utf-16-le")
            self.text = (
                encoded[:start * 2]
                + self.clipboard.encode("utf-16-le")
                + encoded[(start + length) * 2:]
            ).decode("utf-16-le")
            self.selection = (start + _units(self.clipboard), 0)
        elif key == MacOSDesktopTextTarget.KEY_DOWN:
            self.selection = (_units(self.text), 0)

    def press_key(self, key):
        self.keys.append(("press", key))
        if key == MacOSDesktopTextTarget.KEY_RIGHT:
            start, length = self.selection
            self.selection = (min(_units(self.text), start + (length or 1)), 0)
        elif key == MacOSDesktopTextTarget.KEY_DELETE:
            self.text = ""
            self.selection = (0, 0)


@pytest.fixture
def editor_target(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("proximic_ring.desktop_target.time.sleep", lambda _s: None)
    editor = Editor()

    class Clipboard:
        snapshot = editor.snapshot
        restore = editor.restore
        set_text = editor.set_text

        def text(self):
            return editor.clipboard

    adapter = MacOSDesktopTextTarget(
        Clipboard(), injector=editor, accessibility_text=editor,
    )
    target = DesktopTargetRef(0, 0, "编辑器", process_id=4321)
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (4321, "编辑器"))
    return editor, adapter, target


@pytest.mark.parametrize("ax_write", [True, False])
@pytest.mark.parametrize("range_write", [True, False])
@pytest.mark.parametrize("replacement", ["修改后的第一行\n第二行😀", ""])
def test_full_replace_and_readback_finish_at_document_end(
    editor_target, ax_write, range_write, replacement,
):
    editor, adapter, target = editor_target
    editor.ax_write = ax_write
    editor.range_write = range_write
    adapter.replace(DesktopTextSnapshot(target, editor.text), replacement)
    adapter.capture_text_allowing_empty(target)
    adapter.release_selection(target)
    assert editor.text == replacement
    assert editor.selection == (_units(replacement), 0)
    assert ("press", adapter.KEY_RIGHT) not in editor.keys
    assert editor.value_writes == int(ax_write)


@pytest.mark.parametrize("range_read", [True, False])
def test_dictation_in_middle_stays_after_inserted_text(editor_target, range_read):
    editor, adapter, target = editor_target
    editor.range_read = range_read
    adapter.inject(target, "插😀入")
    adapter.release_selection(target)
    assert editor.text == "前缀插😀入后缀"
    assert editor.selection == (_units("前缀插😀入"), 0)
    assert editor.keys == [("command", adapter.KEY_V)]


@pytest.mark.parametrize("range_write", [True, False])
def test_release_collapses_only_an_existing_selection(editor_target, range_write):
    editor, adapter, target = editor_target
    editor.range_write = range_write
    editor.selection = (1, 2)
    adapter.release_selection(target)
    adapter.release_selection(target)
    assert editor.selection == (3, 0)
    assert editor.keys == ([] if range_write else [("press", adapter.KEY_RIGHT)])


def test_release_never_activates_a_background_target(editor_target, monkeypatch):
    editor, adapter, target = editor_target
    editor.selection = (1, 2)
    monkeypatch.setattr(adapter, "_frontmost_application", lambda: (9999, "其他应用"))
    adapter.release_selection(target)
    assert editor.selection == (1, 2)
    assert not editor.keys


@pytest.mark.parametrize("range_read", [True, False])
def test_copy_fallback_releases_its_own_select_all(editor_target, range_read):
    editor, adapter, target = editor_target
    editor.value_read = False
    editor.range_read = range_read
    snapshot = adapter.capture_text(target)
    adapter.release_selection(target)
    assert snapshot.text == editor.text
    assert editor.selection == (2 if range_read else _units(editor.text), 0)


def test_caret_fallback_failure_does_not_repeat_successful_replacement(
    editor_target, monkeypatch,
):
    editor, adapter, target = editor_target
    editor.range_write = False

    def fail_key(_key):
        raise RuntimeError("keyboard dispatch unavailable")

    monkeypatch.setattr(editor, "command_key", fail_key)
    adapter.replace(DesktopTextSnapshot(target, editor.text), "修改后内容")
    assert editor.text == "修改后内容"
    assert editor.value_writes == 1
