"""Process-targeted sentence undo/caret keys, with a WeChat edit override.

Text is read and written by the input method. No clipboard, Return, source
switching or window activation is exposed. Every request pins its target app.
"""
from __future__ import annotations

from .mac_permissions import require_post_event_access


def send_wechat_key(command: str, count: int, event_tag: int) -> None:
    if command not in {"select_all", "select_previous", "delete", "caret_from_end"}:
        raise ValueError("微信兼容命令无效")
    _send_key(command, count, event_tag, "com.tencent.xinWeChat")


def send_codex_key(command: str, count: int, event_tag: int) -> None:
    """Compatibility entry point for existing callers."""
    send_input_method_key("com.openai.codex", command, count, event_tag)


def send_input_method_key(application: str, command: str, count: int, event_tag: int) -> None:
    if not isinstance(application, str) or not application.strip():
        raise ValueError("输入法操作缺少目标应用")
    if application == "com.tencent.xinWeChat":
        raise ValueError("微信需要使用专用兼容入口")
    if command not in {"select_all", "select_to_start", "select_previous", "delete", "caret_from_end", "caret_to_end", "caret_backward", "caret_forward"}:
        raise ValueError("默认输入法操作只允许原生选取、删除和光标定位")
    if command == "caret_to_end" and count != 0:
        raise ValueError("文末定位不接受移动次数")
    _send_key(command, count, event_tag, application)


def _send_key(command: str, count: int, event_tag: int, bundle: str) -> None:
    label = "微信" if bundle == "com.tencent.xinWeChat" else "输入法"
    if command not in {"select_all", "select_to_start", "select_previous", "delete", "caret_from_end", "caret_to_end", "caret_backward", "caret_forward"}:
        raise ValueError(f"{label}兼容命令无效")
    if type(count) is not int or not 0 <= count <= 512:
        raise ValueError(f"{label}光标移动范围无效")
    if command in {"select_previous", "caret_backward", "caret_forward"} and count == 0:
        raise ValueError(f"{label}本句选取范围为空")
    if type(event_tag) is not int or not 0 < event_tag <= 1 << 50:
        raise ValueError(f"{label}兼容事件标记无效")
    import AppKit
    import Quartz

    workspace = AppKit.NSWorkspace.sharedWorkspace()
    application = workspace.frontmostApplication()
    if application is None or application.bundleIdentifier() != bundle:
        raise RuntimeError(f"{label}已不在前台，未发送快捷键")
    pid = application.processIdentifier()
    require_post_event_access()
    command_mask = Quartz.kCGEventFlagMaskCommand
    keys = {"select_all": [(0, command_mask)],
            "select_to_start": [(126, command_mask | Quartz.kCGEventFlagMaskShift)],
            "select_previous": [(123, Quartz.kCGEventFlagMaskShift)] * count,
            "delete": [(51, 0)], "caret_from_end": [(125, command_mask)] + [(123, 0)] * count,
            "caret_to_end": [(125, command_mask)], "caret_backward": [(123, 0)] * count,
            "caret_forward": [(124, 0)] * count}[command]
    for code, flags in keys:
        current = workspace.frontmostApplication()
        if (current is None or current.bundleIdentifier() != bundle
                or current.processIdentifier() != pid):
            raise RuntimeError(f"{label}输入目标已切换，已停止快捷键")
        for pressed in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, code, pressed)
            if event is None:
                raise RuntimeError(f"无法创建{label}快捷键")
            Quartz.CGEventSetFlags(event, flags)
            Quartz.CGEventSetIntegerValueField(event, Quartz.kCGEventSourceUserData, event_tag)
            Quartz.CGEventPostToPid(pid, event)
