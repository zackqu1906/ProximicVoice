from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _module():
    path = Path(__file__).parents[1] / "tools" / "diagnose_macos_undo.py"
    spec = importlib.util.spec_from_file_location("diagnose_macos_undo", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_undo_diagnostic_reads_only_focus_and_command_metadata():
    observer = object.__new__(_module().MetadataObserver)
    released = []

    def text_attribute(_element, name):
        assert name == "AXRole", "Diagnostic must never read input text"
        return "AXTextArea"

    def attribute(element, name):
        assert name == "AXEnabled", "Diagnostic must never copy AXValue"
        return {20: 1, 30: 2}[element]

    observer.bridge = SimpleNamespace(
        _copy_focused_elements=lambda pid: [(1, 2, "application")],
        _copy_text_attribute=text_attribute,
        _is_editable_text_element=lambda element: True,
        _copy_attribute_value=attribute,
        _release=lambda *values: released.extend(values),
        _core_foundation=SimpleNamespace(
            CFHash=lambda element: 123,
            CFBooleanGetValue=lambda value: value == 1,
            CFGetTypeID=lambda value: 10,
            CFBooleanGetTypeID=lambda: 10,
        ),
    )
    observer.pid = 42
    observer.commands = {"undo": 20, "redo": 30}
    assert observer.snapshot() == {
        "focus": [{"source": "application", "role": "AXTextArea", "editable": True, "id": 123}],
        "commands_enabled": {"undo": True, "redo": False},
    }
    assert released == [2, 1, 1, 2]


def test_undo_notification_is_not_reported_as_verified_success():
    observer = object.__new__(_module().MetadataObserver)
    events = []
    observer.emit = events.append
    observer.bridge = SimpleNamespace(
        _cf_text=lambda value: "AXValueChanged",
        _core_foundation=SimpleNamespace(CFHash=lambda element: 123),
    )
    observer._notification(None, 2, 3, None)
    assert events == [{"type": "notification", "name": "AXValueChanged", "element_id": 123}]
    assert "verified" not in events[0]
