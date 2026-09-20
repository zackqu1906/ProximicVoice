"""Translate an in-app key event to the same physical key used for injection."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from ..app_gestures import KEY_CODES, normalize_shortcut

MODIFIER_KEYS = {int(key) for key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Meta, Qt.Key_Alt, Qt.Key_AltGr)}
SHIFTED_KEYS = dict(zip('~!@#$%^&*()_+{}|:"<>?', '`1234567890-=[]\\;\',./'))


def shortcut_from_key(key, modifiers, *, native_key=None):
    if key in MODIFIER_KEYS:
        return ""
    # Qt swaps Command and Control on macOS. The stored shortcut names do not.
    names = [(Qt.ControlModifier, "Cmd" if sys.platform == "darwin" else "Ctrl"),
             (Qt.MetaModifier, "Ctrl" if sys.platform == "darwin" else "Cmd"),
             (Qt.AltModifier, "Alt"), (Qt.ShiftModifier, "Shift")]
    # Keep semantic navigation/function keys (e.g. Fn+Down -> PageDown).
    # Physical-key recovery is needed for printable Shift/Option characters.
    base = next((name for name, code in KEY_CODES.items() if code == native_key), None) if key < int(Qt.Key_Escape) else None
    if base is None:
        base = QKeySequence(key).toString(QKeySequence.PortableText)
        base = {"PgUp": "PageUp", "PgDown": "PageDown", "Del": "Delete", "Backtab": "Tab", "Enter": "Return"}.get(base, base)
        if modifiers & Qt.ShiftModifier.value:
            base = SHIFTED_KEYS.get(base, base)
    return normalize_shortcut("+".join([name for flag, name in names if modifiers & flag.value] + [base]))


def shortcut_from_event(event):
    native = None
    if sys.platform == "darwin" and any((event.nativeVirtualKey(), event.nativeScanCode(), event.nativeModifiers())):
        native = event.nativeVirtualKey()
    return shortcut_from_key(event.key(), event.modifiers().value, native_key=native)
