"""Explicit gesture selection of our input mode, without key injection or AX."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from .input_method_install import BUNDLE_ID, payload_directory


def foreground_pid() -> int:
    if sys.platform != "darwin":
        raise RuntimeError("输入法切换仅支持 macOS")
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        raise RuntimeError("未找到前台应用，请点入文本框后重试")
    return int(app.processIdentifier())


def select_voice_input_source(pid: int, *, helper: Path | None = None) -> None:
    helper = helper if helper is not None else payload_directory() / "InputMethodAdmin"
    if not helper.is_file():
        raise RuntimeError("缺少输入法切换工具，请构建或更新完整应用")
    try:
        result = subprocess.run([str(helper), "select", str(pid)],
                                capture_output=True, text=True, timeout=3)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("切换输入法超时，请重新触发手势") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "切换输入法失败，请检查组件是否已安装")
    try:
        state = json.loads(result.stdout)
        selected = any(s.get("id") == BUNDLE_ID + ".dictation" and s.get("selected")
                       for s in state["sources"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("无法确认输入法切换结果，请更新完整应用") from exc
    if not selected:
        raise RuntimeError("系统未确认输入法切换，请点入文本框后重试")
