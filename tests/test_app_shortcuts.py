from dataclasses import replace
from types import SimpleNamespace
import sys

import pytest

from proximic_ring.app_shortcuts import MacAppShortcuts, ShortcutTarget, verify_native_api


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS bridge exports")
def test_installed_macos_bridge_exports_without_desktop_access():
    # Do not mock these exports: Quartz does not contain Accessibility functions.
    verify_native_api()


@pytest.fixture
def desktop(monkeypatch):
    target = ShortcutTarget("com.openai.codex", 42, "codex", "w", "editor", "AXTextArea")
    state = SimpleNamespace(target=target, permission=True, sent=[])
    backend = MacAppShortcuts()
    monkeypatch.setattr(backend, "capture", lambda: state.target)
    quartz = SimpleNamespace(
        kCGEventFlagMaskCommand=256, kCGEventFlagMaskShift=128,
        kCGEventFlagMaskControl=64, kCGEventFlagMaskAlternate=32, kCGEventSourceUserData=99,
        CGPreflightPostEventAccess=lambda: state.permission,
        CGEventCreateKeyboardEvent=lambda source, code, down: {"code": code, "down": down},
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        CGEventSetIntegerValueField=lambda event, field, value: event.update(tag=value),
        CGEventPostToPid=lambda pid, event: state.sent.append((pid, event)),
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    return backend, state, quartz


@pytest.mark.parametrize("shortcut,code,flags", [("Enter", 36, 0), ("Cmd+Shift+[", 33, 384),
    ("Cmd+]", 30, 256), ("Cmd+N", 45, 256), ("Ctrl+Alt+Return", 36, 96)])
def test_posts_one_balanced_chord_to_pinned_pid(desktop, shortcut, code, flags):
    backend, state, _ = desktop
    backend.post(state.target, shortcut)
    assert [(pid, e["code"], e["flags"], e["down"]) for pid, e in state.sent] == [
        (42, code, flags, True), (42, code, flags, False)]


@pytest.mark.parametrize("change", ["app", "pid", "window", "focus", "modal", "missing", "permission"])
def test_stale_target_or_permission_failure_posts_nothing(desktop, change):
    backend, state, _ = desktop
    original = state.target
    if change == "permission":
        state.permission = False
    elif change == "missing":
        state.target = None
    else:
        field, value = {"app": ("bundle", "com.apple.Terminal"), "pid": ("pid", 100),
            "window": ("window", "other"), "focus": ("focus", "search"), "modal": ("blocked", True)}[change]
        state.target = replace(state.target, **{field: value})
    with pytest.raises(RuntimeError):
        backend.post(original, "Enter", require_focus=True)
    assert not state.sent


def test_both_events_must_allocate_before_keydown(desktop):
    backend, state, quartz = desktop
    quartz.CGEventCreateKeyboardEvent = lambda _, code, down: {"code": code} if down else None
    with pytest.raises(RuntimeError):
        backend.post(state.target, "Enter")
    assert not state.sent


def test_existing_shortcut_backend_recovers_after_grant_and_stops_after_revocation(desktop):
    backend, state, _ = desktop
    state.permission = False
    with pytest.raises(RuntimeError):
        backend.post(state.target, "Enter")
    assert not state.sent
    state.permission = True
    # Grant does not replay the failed send; only a fresh action sends it.
    assert not state.sent
    backend.post(state.target, "Enter")
    assert len(state.sent) == 2
    state.permission = False
    with pytest.raises(RuntimeError):
        backend.post(state.target, "Enter")
    assert len(state.sent) == 2


def test_capture_is_bounded_metadata_only_no_text_or_clipboard(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    reads = []
    values = {("app", "AXFocusedWindow"): "window", ("app", "AXFocusedUIElement"): "focus",
              ("focus", "AXRole"): "AXTextArea", ("focus", "AXDescription"): "Message input"}
    def read(node, attribute, _):
        reads.append(attribute)
        assert attribute not in {"AXValue", "AXChildren", "AXSelectedText", "AXSelectedTextRange"}
        return 0, values.get((node, attribute))
    monkeypatch.setitem(sys.modules, "Quartz", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "ApplicationServices", SimpleNamespace(
        AXUIElementCreateApplication=lambda pid: "app",
        AXUIElementCopyAttributeValue=read,
        AXUIElementSetMessagingTimeout=lambda elem, timeout: None))
    app = SimpleNamespace(bundleIdentifier=lambda: "com.openai.codex", processIdentifier=lambda: 42,
                          localizedName=lambda: "Codex")
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSWorkspace=SimpleNamespace(
        sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: app))))
    target = MacAppShortcuts().capture()
    assert target.profile == "codex" and target.focus == "focus" and not target.blocked
    assert len(reads) == 6
