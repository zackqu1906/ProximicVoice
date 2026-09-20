"""App shortcut profiles and phase policy, independent of Qt and the ring SDK."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json

from .gesture_settings import GESTURE_LABELS, GestureBindings

APP_LABELS = {"codex": "Codex", "workbuddy": "WorkBuddy", "wechat": "微信"}
ACTION_LABELS = {"send": "发送", "previous": "上一个任务／聊天",
                 "next": "下一个任务／聊天", "new": "新建任务"}
QUIET_PHASES = frozenset({"idle", "dictated", "edited", "undone", "interrupted", "error"})
KEY_CODES = dict(zip("ASDFHGZXCV", range(10)))
KEY_CODES.update(dict(zip("BQWERYT123465=97-80]OU[IP", range(11, 36))))
# macOS virtual key codes (not Unicode characters).
KEY_CODES.update({"Return": 36, "L": 37, "J": 38, "'": 39, "K": 40, ";": 41,
                  "\\": 42, ",": 43, "/": 44, "N": 45, "M": 46, ".": 47,
                  "Tab": 48, "Space": 49, "`": 50, "Backspace": 51,
                  "Escape": 53, "Home": 115, "End": 119, "PageUp": 116,
                  "PageDown": 121, "Delete": 117, "Left": 123, "Right": 124,
                  "Down": 125, "Up": 126})
KEY_CODES.update(dict(zip((f"F{i}" for i in range(1, 13)),
                         (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111))))
MODIFIERS = ("Cmd", "Ctrl", "Alt", "Shift")


def normalize_shortcut(value: str) -> str:
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("请输入一个快捷键组合")
    parts = [p.strip() for p in value.split("+")]
    aliases = {"command": "Cmd", "cmd": "Cmd", "⌘": "Cmd", "control": "Ctrl",
               "ctrl": "Ctrl", "option": "Alt", "alt": "Alt", "shift": "Shift",
               "enter": "Return", "esc": "Escape", **{k.lower(): k for k in KEY_CODES}}
    parts = [aliases.get(p.lower(), p) for p in parts]
    if (not parts or parts[-1] not in KEY_CODES or any(p not in MODIFIERS for p in parts[:-1])
            or len(set(parts[:-1])) != len(parts[:-1])):
        raise ValueError("请使用单个按键组合，例如 Cmd+Shift+[ 或 Enter")
    if len(parts) == 1 and len(parts[-1]) == 1:
        raise ValueError("字母、数字或标点需要搭配 Cmd、Ctrl 或 Alt，避免输入字符")
    if len(parts[-1]) == 1 and not set(parts[:-1]) & {"Cmd", "Ctrl", "Alt"}:
        raise ValueError("字符快捷键需要搭配 Cmd、Ctrl 或 Alt")
    return "+".join([m for m in MODIFIERS if m in parts[:-1]] + [parts[-1]])


@dataclass(frozen=True)
class AppBinding:
    gesture: str
    shortcut: str
    enabled: bool = True

    def __post_init__(self):
        if self.gesture not in GESTURE_LABELS or type(self.enabled) is not bool:
            raise ValueError("应用手势设置无效")
        object.__setattr__(self, "shortcut", normalize_shortcut(self.shortcut))


def default_profiles() -> dict[str, dict[str, AppBinding]]:
    profiles = {}
    for app in APP_LABELS:
        prefix = "Cmd" if app == "workbuddy" else "Cmd+Shift"
        profiles[app] = {
            "send": AppBinding("swipe-up", "Return"),
            "previous": AppBinding("circle-counterclockwise", prefix + "+["),
            "next": AppBinding("circle-clockwise", prefix + "+]"),
        }
        if app != "wechat":
            profiles[app]["new"] = AppBinding("index-pinch", "Cmd+N")
    return profiles


def validate_profiles(profiles, voice: GestureBindings) -> None:
    for app, actions in profiles.items():
        used = set()
        for binding in actions.values():
            if not binding.enabled:
                continue
            if binding.gesture in voice.confirm:
                raise ValueError("开始／结束听写的手势需独立保留，请选择其他手势")
            if binding.gesture in used:
                raise ValueError(f"{APP_LABELS[app]} 中同一阶段的应用操作不能重复分配手势")
            used.add(binding.gesture)


def profiles_to_json(profiles) -> str:
    return json.dumps({app: {action: vars(binding) for action, binding in actions.items()}
                       for app, actions in profiles.items()}, ensure_ascii=False)


def profiles_from_json(value: object):
    data = json.loads(str(value))
    defaults = default_profiles()
    if not isinstance(data, dict) or set(data) != set(defaults):
        raise ValueError("应用手势设置格式无效")
    for app, actions in data.items():
        if not isinstance(actions, dict) or set(actions) != set(defaults[app]):
            raise ValueError("应用手势设置格式无效")
        defaults[app] = {name: AppBinding(**fields) for name, fields in actions.items()}
    return defaults


def migrate_voice_defaults(bindings: GestureBindings) -> GestureBindings:
    # Only retire the two shipped aliases; preserve deliberate user assignments.
    return replace(bindings,
                   undo=("swipe-left", "") if bindings.undo == ("swipe-left", "swipe-down") else bindings.undo,
                   switch_mode=("swipe-right", "") if bindings.switch_mode == ("swipe-right", "swipe-up") else bindings.switch_mode)


def action_phase(action: str, *, phase: str, composing: bool = False,
                 writing: bool = False, edit_requested: bool = False) -> str:
    """Return dispatch, finish_then_send or a user-facing reason. Never queue navigation."""
    if action not in ACTION_LABELS:
        return "未分配操作"
    if phase == "editing" or edit_requested:
        return "正在编辑，完成或取消后再操作"
    if action == "send" and phase in {"starting", "listening", "finishing"}:
        return "finish_then_send"
    if writing:
        return "正在应用或撤销文字，请稍后再操作"
    if phase not in QUIET_PHASES or composing:
        return "请先结束本句听写，再切换或新建对话"
    return "dispatch"


def profile_for_application(bundle: str, name: str = "") -> str:
    if bundle == "com.openai.codex":
        return "codex"
    if bundle == "com.tencent.xinWeChat":
        return "wechat"
    # WorkBuddy distribution IDs differ between channels. Resolve the installed
    # native application's own name; the captured bundle and PID are still pinned.
    if name.casefold() == "workbuddy":
        return "workbuddy"
    return ""
