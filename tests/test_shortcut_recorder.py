import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent

from proximic_ring.ui.shortcut_recorder import shortcut_from_event, shortcut_from_key


@pytest.mark.parametrize("key,modifiers,expected", [
    (Qt.Key_BraceLeft, Qt.ControlModifier | Qt.ShiftModifier, "Cmd+Shift+["),
    (Qt.Key_BraceRight, Qt.ControlModifier | Qt.ShiftModifier, "Cmd+Shift+]"),
    (Qt.Key_Exclam, Qt.ControlModifier | Qt.ShiftModifier, "Cmd+Shift+1"),
    (Qt.Key_Return, Qt.ControlModifier, "Cmd+Return"),
    (Qt.Key_Tab, Qt.MetaModifier, "Ctrl+Tab"),
    (Qt.Key_F8, Qt.NoModifier, "F8"),
])
def test_recording_normalizes_qt_key_symbols_to_injection_keys(key, modifiers, expected, monkeypatch):
    monkeypatch.setattr("proximic_ring.ui.shortcut_recorder.sys.platform", "darwin")
    assert shortcut_from_key(int(key), modifiers.value) == expected


@pytest.mark.parametrize("key", [Qt.Key_Shift, Qt.Key_Meta, Qt.Key_Control, Qt.Key_Alt])
def test_modifier_alone_does_not_finish_recording(key):
    assert shortcut_from_key(int(key), Qt.ControlModifier.value) == ""


def test_native_key_keeps_physical_key_despite_option_character(monkeypatch):
    monkeypatch.setattr("proximic_ring.ui.shortcut_recorder.sys.platform", "darwin")
    event = QKeyEvent(QEvent.KeyPress, 0x201C, Qt.ControlModifier | Qt.AltModifier,
                      0, 33, 0x180000, "“")
    assert shortcut_from_event(event) == "Cmd+Alt+["


def test_synthetic_zero_native_key_does_not_become_a():
    assert shortcut_from_event(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)) == "Return"


def test_fn_navigation_keeps_semantic_key(monkeypatch):
    monkeypatch.setattr("proximic_ring.ui.shortcut_recorder.sys.platform", "darwin")
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_PageDown, Qt.NoModifier, 0, 125, 0x800000)
    assert shortcut_from_event(event) == "PageDown"


def test_bare_text_is_not_saved_as_a_command():
    with pytest.raises(ValueError):
        shortcut_from_key(int(Qt.Key_A), 0)
