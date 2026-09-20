"""Small macOS keyboard boundary. No activation, text reads, clipboard or clicks."""
from __future__ import annotations

from dataclasses import dataclass, field
import sys

from .app_gestures import KEY_CODES, normalize_shortcut, profile_for_application
from .mac_permissions import require_post_event_access


def verify_native_api() -> None:
    """Validate the installed bridges without reading the desktop or posting keys."""
    import AppKit
    import ApplicationServices
    import Quartz

    for module, names in (
        (AppKit, ("NSWorkspace",)),
        (ApplicationServices, ("AXUIElementCreateApplication", "AXUIElementCopyAttributeValue",
                               "AXUIElementSetMessagingTimeout")),
        (Quartz, ("CGPreflightPostEventAccess", "CGEventCreateKeyboardEvent",
                  "CGEventSetFlags", "CGEventSetIntegerValueField", "CGEventPostToPid")),
    ):
        for name in names:
            if not callable(getattr(module, name, None)):
                raise RuntimeError(f"macOS 应用手势接口不可用：{module.__name__}.{name}")


@dataclass(frozen=True)
class ShortcutTarget:
    bundle: str
    pid: int
    profile: str
    window: object = field(default=None, repr=False)
    focus: object = field(default=None, repr=False)
    role: str = ""
    blocked: bool = False


class MacAppShortcuts:
    def capture(self) -> ShortcutTarget | None:
        if sys.platform != "darwin":
            return None
        import AppKit

        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        bundle, pid = str(app.bundleIdentifier() or ""), int(app.processIdentifier())
        profile = profile_for_application(bundle, str(app.localizedName() or ""))
        if not profile:
            return None
        import ApplicationServices as AX
        element = AX.AXUIElementCreateApplication(pid)
        # These are tiny metadata reads. Never fetch AXValue, selection, full text,
        # or the accessibility tree in the speech/gesture path.
        try:
            AX.AXUIElementSetMessagingTimeout(element, 0.08)
        except (AttributeError, TypeError):
            pass
        def attr(node, name):
            if node is None:
                return None
            error, value = AX.AXUIElementCopyAttributeValue(node, name, None)
            return value if error == 0 else None
        window = attr(element, "AXFocusedWindow")
        focus = attr(element, "AXFocusedUIElement")
        role = str(attr(focus, "AXRole") or "")
        subrole = str(attr(window, "AXSubrole") or "")
        # Do not turn a chat shortcut into a dialog confirmation or terminal input.
        description = str(attr(focus, "AXDescription") or "").casefold()
        blocked = (bool(attr(window, "AXModal")) or subrole in {"AXDialog", "AXSystemDialog"}
                   or role in {"AXMenu", "AXMenuItem", "AXComboBox", "AXSearchField"}
                   or any(word in description for word in ("terminal", "终端", "search", "搜索")))
        return ShortcutTarget(bundle, pid, profile, window, focus, role, blocked)

    def same_target(self, target: ShortcutTarget, *, require_focus: bool = False) -> bool:
        current = self.capture()
        if current is None or (current.bundle, current.pid) != (target.bundle, target.pid) or current.blocked:
            return False
        if target.window is not None and current.window != target.window:
            return False
        if require_focus and target.focus is not None and current.focus != target.focus:
            return False
        return True

    def post(self, target: ShortcutTarget, shortcut: str, *, require_focus: bool = False) -> None:
        import Quartz
        parts = normalize_shortcut(shortcut).split("+")
        if not self.same_target(target, require_focus=require_focus):
            raise RuntimeError("目标窗口已变化，请在目标对话中重新操作")
        require_post_event_access()
        masks = {"Cmd": Quartz.kCGEventFlagMaskCommand, "Ctrl": Quartz.kCGEventFlagMaskControl,
                 "Alt": Quartz.kCGEventFlagMaskAlternate, "Shift": Quartz.kCGEventFlagMaskShift}
        flags = sum(masks[part] for part in parts[:-1])
        # Precreate the down/up pair, so an allocation failure cannot leave a key held.
        events = [Quartz.CGEventCreateKeyboardEvent(None, KEY_CODES[parts[-1]], down)
                  for down in (True, False)]
        if any(event is None for event in events):
            raise RuntimeError("无法创建应用快捷键")
        for event in events:
            Quartz.CGEventSetFlags(event, flags)
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, 0x50524F584147)
        for event in events:
            Quartz.CGEventPostToPid(target.pid, event)
