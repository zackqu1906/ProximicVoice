"""Read WeChat menu labels and navigate to macOS App Shortcuts on explicit request."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import plistlib
import time


MENU_TITLES = {
    "en": ("Show Previous Chat", "Show Next Chat"),
    "zh-Hans": ("显示上一个聊天", "显示下一个聊天"),
    "zh-Hant": ("顯示上一個聊天", "顯示下一個聊天"),
}
LANGUAGE_LABELS = {"en": "English", "zh-Hans": "简体中文", "zh-Hant": "繁體中文"}
KEYBOARD_SETTINGS_URL = "x-apple.systempreferences:com.apple.Keyboard-Settings.extension"


def _normal(value):
    return str(value or "").replace("…", "").rstrip(".").strip().casefold()


class _Accessibility:
    def __init__(self):
        import ApplicationServices as AX
        self.api = AX

    def application(self, pid):
        return self.api.AXUIElementCreateApplication(pid)

    def get(self, node, name):
        if node is None:
            return None
        self.api.AXUIElementSetMessagingTimeout(node, 0.05)
        error, value = self.api.AXUIElementCopyAttributeValue(node, name, None)
        return value if error == 0 else None

    def nodes(self, roots, *, maximum=250):
        queue = deque((node, None) for node in roots)
        deadline = time.monotonic() + 1.0
        for _ in range(maximum):
            if not queue or time.monotonic() >= deadline:
                break
            node, row = queue.popleft()
            role = self.get(node, "AXRole")
            if role == "AXRow":
                row = node
            yield node, row, role
            queue.extend((child, row) for child in (self.get(node, "AXChildren") or ()))

    def labels(self, node):
        return [value for name in ("AXTitle", "AXDescription", "AXValue")
                if isinstance(value := self.get(node, name), str)]

    def select(self, node):
        return self.api.AXUIElementSetAttributeValue(node, "AXSelected", True) == 0

    def press(self, node):
        return self.api.AXUIElementPerformAction(node, "AXPress") == 0


def menu_details(titles, *, version=""):
    """Match complete pairs from the live menu; don't infer from system language."""
    names = {_normal(title): title for title in titles}
    for language, pair in MENU_TITLES.items():
        if all(_normal(title) in names for title in pair):
            return dict(version=version, language=language, languageLabel=LANGUAGE_LABELS[language],
                        previous=names[_normal(pair[0])], next=names[_normal(pair[1])],
                        detected=True, message="已读取微信当前菜单名称")
    return dict(version=version, language="", languageLabel="未识别", previous="", next="",
                detected=False, message="未能读取聊天切换菜单。可展开微信的「显示 / Show」菜单后点重新识别，或手动选择菜单语言。")


def detect_wechat_menu():
    import AppKit
    running = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.tencent.xinWeChat")
    version = ""
    url = running[0].bundleURL() if running else AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_("com.tencent.xinWeChat")
    if url is not None:
        try:
            info = plistlib.loads((Path(str(url.path())) / "Contents/Info.plist").read_bytes())
            version = str(info.get("CFBundleShortVersionString", ""))
        except (OSError, ValueError):
            pass
    if not running:
        return {**menu_details([], version=version), "message": "请打开微信后点重新识别"}
    ax = _Accessibility()
    app = ax.application(int(running[0].processIdentifier()))
    menu = ax.get(app, "AXMenuBar")
    # Only menu labels, never windows, conversations, message text or clipboard.
    titles = []
    if menu is not None:
        for node, _, _ in ax.nodes([menu], maximum=350):
            titles.extend(ax.labels(node))
    return menu_details(titles, version=version)


class KeyboardShortcutsNavigator:
    """Select a settings category. Never toggle a preference or add a shortcut."""
    _app_labels = frozenset(map(_normal, (
        "App Shortcuts", "Application shortcuts", "应用快捷键", "App 快捷键",
        "应用程序快捷键", "App 快速鍵", "應用程式快速鍵", "應用程式快捷鍵",
    )))
    _keyboard_labels = frozenset(map(_normal, ("Keyboard Shortcuts", "键盘快捷键", "鍵盤快速鍵", "鍵盤快捷鍵")))

    def __init__(self, *, accessibility=None, foreground=None):
        self.ax = accessibility or _Accessibility()
        self._foreground = foreground or self._front
        self._seen_settings = False

    @staticmethod
    def _front():
        import AppKit
        return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()

    def step(self):
        app = self._foreground()
        if app is None or app.bundleIdentifier() != "com.apple.systempreferences":
            return "cancelled" if self._seen_settings else "waiting"
        self._seen_settings = True
        element = self.ax.application(int(app.processIdentifier()))
        windows = self.ax.get(element, "AXWindows") or ()
        sheets = [sheet for window in windows for sheet in (self.ax.get(window, "AXSheets") or ())]
        roots = sheets or windows
        button = None
        for node, row, role in self.ax.nodes(roots):
            labels = {_normal(label) for label in self.ax.labels(node)}
            if row is not None and labels & self._app_labels:
                if self.ax.get(row, "AXSelected"):
                    return "ready"
                if not self.ax.select(row):
                    self.ax.press(row)
                return "waiting"
            if role == "AXButton" and labels & self._keyboard_labels:
                button = node
        if button is not None and not sheets:
            self.ax.press(button)
        return "waiting"

    def run(self, cancel, *, timeout=7.0):
        deadline = time.monotonic() + timeout
        while not cancel.is_set() and time.monotonic() < deadline:
            result = self.step()
            if result != "waiting":
                return result
            cancel.wait(0.2)
        return "cancelled" if cancel.is_set() else "manual"
