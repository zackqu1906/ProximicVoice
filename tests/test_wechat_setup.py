import threading
from types import SimpleNamespace

import pytest

from proximic_ring.wechat_setup import KeyboardShortcutsNavigator, MENU_TITLES, menu_details


@pytest.mark.parametrize("language,pair", MENU_TITLES.items())
def test_language_comes_from_actual_menu_pair(language, pair):
    result = menu_details(["File", *pair, "Show Next Unread Chat"], version="4.1.11")
    assert result["language"] == language and result["detected"]
    assert (result["previous"], result["next"]) == pair
    assert result["version"] == "4.1.11"


@pytest.mark.parametrize("titles", [[], ["Show Previous Chat"], ["Show Next Unread Chat"], ["显示上一个聊天", "Show Next Chat"]])
def test_incomplete_or_mixed_menus_do_not_guess_from_system_language(titles):
    result = menu_details(titles)
    assert not result["detected"] and not result["previous"] and not result["next"]


class FakeAccessibility:
    def __init__(self):
        self.sheet = False
        self.selected = False
        self.permission_dialog = False
        self.pressed = []
        self.selections = []

    def application(self, pid):
        assert pid == 42
        return "app"

    def get(self, node, name):
        if (node, name) == ("app", "AXWindows"):
            return ["window"]
        if (node, name) == ("window", "AXSheets"):
            return ["permission"] if self.permission_dialog else ["sheet"] if self.sheet else []
        if name == "AXSelected":
            return self.selected

    def nodes(self, roots):
        if roots == ["window"]:
            yield "keyboard-button", None, "AXButton"
        elif roots == ["sheet"]:
            yield "app-label", "app-row", "AXStaticText"
            yield "function-switch", None, "AXCheckBox"
        elif roots == ["permission"]:
            yield "allow-button", None, "AXButton"
        else:
            raise AssertionError(roots)

    def labels(self, node):
        return [{"keyboard-button": "Keyboard Shortcuts…", "app-label": "Application shortcuts",
                 "function-switch": "Use F1, F2, etc. keys as standard function keys", "allow-button": "Allow"}[node]]

    def select(self, node):
        self.selections.append(node)
        assert node == "app-row"
        self.selected = True
        return True

    def press(self, node):
        self.pressed.append(node)
        assert node == "keyboard-button"
        self.sheet = True
        return True


def test_settings_navigation_opens_sheet_selects_app_category_and_checks_result():
    ax = FakeAccessibility()
    app = SimpleNamespace(bundleIdentifier=lambda: "com.apple.systempreferences", processIdentifier=lambda: 42)
    navigator = KeyboardShortcutsNavigator(accessibility=ax, foreground=lambda: app)
    assert navigator.step() == "waiting"
    assert navigator.step() == "waiting"
    assert navigator.step() == "ready"
    assert ax.pressed == ["keyboard-button"] and ax.selections == ["app-row"]
    app.bundleIdentifier = lambda: "com.tencent.xinWeChat"
    assert navigator.step() == "cancelled"


def test_settings_navigation_never_handles_a_permission_dialog():
    ax = FakeAccessibility()
    ax.permission_dialog = True
    app = SimpleNamespace(bundleIdentifier=lambda: "com.apple.systempreferences", processIdentifier=lambda: 42)
    navigator = KeyboardShortcutsNavigator(accessibility=ax, foreground=lambda: app)
    assert navigator.step() == "waiting"
    assert not ax.pressed and not ax.selections
    cancel = threading.Event()
    cancel.set()
    assert navigator.run(cancel) == "cancelled"
