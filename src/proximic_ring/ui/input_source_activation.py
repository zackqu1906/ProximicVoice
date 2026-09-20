"""Check/select the system input source in a cancellable helper process.

No AX/text-field detection, keyboard injection or GUI-thread wait is performed.
The helper may briefly own focus to refresh a newly selected input context.
The UI waits for native BEGIN before starting gesture-controlled ASR.
"""
from __future__ import annotations

import json
from pathlib import Path
from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from ..input_method_install import payload_directory


def warm_input_method():
    """Launch our accessory IME in the background without hiding its palettes."""
    bundle = Path.home() / "Library/Input Methods/ProxiMicInput.app"
    if (bundle / "Contents/MacOS/ProxiMicInput").is_file():
        QProcess.startDetached("/usr/bin/open", ["-g", str(bundle)])


class InputSourceActivation(QObject):
    event = Signal(object)

    def __init__(self, parent=None, *, command=None, timeout_ms=3500):
        super().__init__(parent)
        self._command = command
        self._timeout_ms = timeout_ms
        self._process = QProcess(self)
        self._process.readyReadStandardOutput.connect(self._read)
        self._process.finished.connect(self._finished)
        self._process.errorOccurred.connect(self._process_error)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._fail("输入法切换超时，请稍后重试"))
        self._buffer = b""
        self._stage = "new"
        self._released = False

    def start(self, origin):
        self._start(origin, "ensure-selected")

    def recover(self, origin):
        self._start(origin, "refresh-selected")

    def owns_foreground(self):
        """Allow only this live helper during its intentional focus interval."""
        pid = int(self._process.processId())
        if self._stage != "probing" or pid <= 0:
            return False
        try:
            from AppKit import NSWorkspace
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            return front is not None and int(front.processIdentifier()) == pid
        except Exception:
            return False

    def _start(self, origin, operation):
        if self._stage != "new":
            return
        self._stage = "probing"
        command = self._command or [str(payload_directory() / "InputMethodAdmin")]
        self._timer.start(self._timeout_ms)
        self._process.start(command[0], [*command[1:], operation, str(origin[1]), origin[0]])

    def cancel(self):
        if self._released:
            return
        self._released = True
        self._stage = "cancelled"
        self._timer.stop()
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()  # asynchronous; never wait for the child
        else:
            self.deleteLater()

    def _fail(self, error):
        if self._stage in {"cancelled", "failed", "done"}:
            return
        self.cancel()
        self._stage = "failed"
        self.event.emit({"event": "error", "error": error})

    def _read(self):
        self._buffer += bytes(self._process.readAllStandardOutput())
        if len(self._buffer) > 8192:
            self._fail("输入法切换工具返回异常数据")
            return
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if self._stage in {"cancelled", "failed", "done"}:
                continue
            try:
                data = json.loads(line)
                kind = data["event"]
                if not isinstance(kind, str):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                self._fail("输入法切换工具返回异常数据")
                return
            if kind == "error":
                self._fail(str(data.get("error") or "无法切换输入法"))
                return
            if kind != "selected" or self._stage != "probing":
                self._fail("输入法切换工具状态异常")
                return
            self._stage = "done"
            self._timer.stop()
            self.event.emit(data)

    def _finished(self, code, status):
        self._read()
        if self._stage not in {"done", "cancelled", "failed"}:
            self._fail("输入法切换工具提前退出，请更新组件或重试")
        if self._released:
            self.deleteLater()

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._fail("无法启动输入法切换工具，请构建或更新完整应用")
