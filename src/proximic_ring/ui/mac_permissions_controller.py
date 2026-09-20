"""Live permission status, including while the user is in System Settings."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import platform
import sys
import threading

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication

from ..mac_permissions import (MacPermissionError, PermissionState, read_permission_state,
                               request_post_event_access, running_identity)


class MacPermissionsController(QObject):
    WAITING_INTERVAL_MS = 1000
    READY_INTERVAL_MS = 10000

    changed = Signal()
    diagnostic = Signal(object)
    _result = Signal(int, object)

    def __init__(self, parent=None, *, enabled=True, reader=None):
        super().__init__(parent)
        self._enabled = enabled and sys.platform == "darwin"
        self._reader = reader or read_permission_state
        self._state = PermissionState()
        self._checked = False
        self._checking = False
        self._closed = False
        self._generation = 0
        self._identity = running_identity()
        self._action_message = ""
        self._result.connect(self._apply)
        self._timer = QTimer(self)
        self._timer.setInterval(self.WAITING_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        app = QGuiApplication.instance()
        if self._enabled and app is not None:
            if isinstance(app, QGuiApplication):
                app.applicationStateChanged.connect(self._on_application_state)
            # Voice input normally runs with another app in front. Waiting for
            # our own window to activate leaves permission feedback stale.
            self._timer.start()
        if self._enabled:
            QTimer.singleShot(0, self.refresh)

    @Property(bool, notify=changed)
    def warning(self):
        return self._enabled and self._checked and not self._state.ready

    @Property(bool, notify=changed)
    def checking(self):
        return self._checking

    @Property(str, notify=changed)
    def title(self):
        return self._state.title if self._checked else "正在检查辅助功能权限…"

    @Property(str, notify=changed)
    def detail(self):
        return self._state.guidance if self._checked else "检查当前进程的系统权限。"

    @Property(str, constant=True)
    def location(self):
        return self._identity["app_path"] or self._identity["executable"]

    @Property(bool, constant=True)
    def canReveal(self):
        return bool(self._identity["app_path"])

    @Property(str, constant=True)
    def instructions(self):
        if not self._identity["frozen"]:
            return "源码运行：请为启动源码的终端授权。程序会在后台自动检测，权限生效后即可继续使用。"
        prefix = "请先将 App 拖入“应用程序”，退出当前副本，再从“应用程序”打开。" if self._identity["temporary_location"] else ""
        return prefix + ("请为下方路径的 Proximic Voice 授权，程序会自动检测。"
                         "更新后若已勾选但持续未生效，请核对是否授权了当前副本；必要时重新添加新版 App，"
                         "系统仍未放行时再退出重开。输入法更新与此权限独立。")

    @Property(str, notify=changed)
    def actionMessage(self):
        return self._action_message

    def _on_application_state(self, state):
        if state == Qt.ApplicationActive:
            self.refresh()

    @Slot()
    def refresh(self):
        if not self._enabled or self._closed or self._checking:
            return
        self._checking = True
        self._generation += 1
        generation = self._generation
        self.changed.emit()

        def work():
            try:
                value = self._reader()
            except Exception as exc:
                value = PermissionState(error=type(exc).__name__)
            try:
                self._result.emit(generation, value)
            except RuntimeError:
                pass

        threading.Thread(target=work, name="ProxiMicPermissions", daemon=True).start()

    @Slot(int, object)
    def _apply(self, generation, state):
        if self._closed or generation != self._generation:
            return
        self._checking = False
        changed = not self._checked or state != self._state
        was_waiting = self._checked and not self._state.ready
        was_ready = self._checked and self._state.ready
        self._checked, self._state = True, state
        interval = self.READY_INTERVAL_MS if state.ready else self.WAITING_INTERVAL_MS
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        if state.ready and was_waiting:
            # Recovery only updates readiness. A failed send/edit may now have
            # a stale target, so never replay it when permission arrives.
            self._action_message = "权限已生效，可以继续使用；刚才未完成的操作请重新触发。"
        elif was_ready and not state.ready:
            self._action_message = "权限已失效，重新授权后会自动检测。"
        if changed:
            self.diagnostic.emit({"kind": "permissions", **asdict(state), **self._identity, "pid": os.getpid()})
        self.changed.emit()

    def report_error(self, error):
        if not isinstance(error, MacPermissionError) or self._closed:
            return
        # A key failure is newer than any pending passive check.
        self._generation += 1
        self._apply(self._generation, error.state)

    @Slot()
    def openSettings(self):
        if not self._enabled or self._closed:
            return
        try:
            request_post_event_access()
            self._action_message = "授权后会在后台自动检测，权限生效后即可继续使用。"
        except Exception as exc:
            self._action_message = "系统权限请求未完成：" + type(exc).__name__
        if not QDesktopServices.openUrl(QUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")):
            self._action_message = "请手动打开系统设置 → 隐私与安全性 → 辅助功能。"
        self.refresh()
        self.changed.emit()

    @Slot()
    def revealApplication(self):
        if self.canReveal:
            import AppKit
            AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_(
                [AppKit.NSURL.fileURLWithPath_(self._identity["app_path"])])

    @Slot()
    def copyDiagnostics(self):
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            data = {"checked": self._checked, **asdict(self._state), **self._identity,
                    "pid": os.getpid(), "macos": platform.mac_ver()[0]}
            app.clipboard().setText(json.dumps(data, ensure_ascii=False, indent=2))
            self._action_message = "已复制权限状态和运行路径，不包含听写或聊天内容。"
            self.changed.emit()

    def close(self):
        self._closed = True
        self._timer.stop()
        app = QGuiApplication.instance()
        if self._enabled and isinstance(app, QGuiApplication):
            app.applicationStateChanged.disconnect(self._on_application_state)
