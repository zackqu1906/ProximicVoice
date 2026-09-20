"""Qt-facing transport for native input-method transactions.

All text, composition, geometry and undo decisions belong to the Swift helper.
Native keys handle sentence undo/caret positioning; WeChat has a separate full-field override.
"""
from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import uuid

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from ..desktop_target import DesktopTargetRef
from ..ime_bridge import IMEBridge
from .mac_permissions_controller import MacPermissionsController
from .input_source_activation import InputSourceActivation, warm_input_method


class InlineInputController(QObject):
    changed = Signal()
    began = Signal()
    diagnostic = Signal(object)
    _received = Signal(object)
    _installationFinished = Signal(str)
    editRequested = Signal(object, str, str, object)
    audioEndRequested = Signal()
    interrupted = Signal()
    settled = Signal(str, str)
    actionRequested = Signal(str)

    def __init__(self, parent=None, *, enabled=None, bridge_factory=None, source_switch_factory=None):
        super().__init__(parent)
        self._enabled = sys.platform == "darwin" if enabled is None else bool(enabled)
        self._permissions = MacPermissionsController(self, enabled=self._enabled)
        self._permissions.diagnostic.connect(self.diagnostic.emit)
        self._closed = False
        self._view = {"phase": "idle", "ready": False}
        self._epoch = ""
        self._client_id = ""
        self._utterance_id = ""
        self._session_id = 0
        self._seq = 0
        self._final_sent = False
        self._first_text_update_sent = False
        self._last_state_diagnostic = None
        self._requested = set()
        self._settled = set()
        self._compatibility_requests = set()
        self._shortcut = "F8"
        self._multi_undo_enabled = False
        self._utterance_sessions = {}
        self.settled_session_id = None
        self._bridge = None
        self._transport_error = ""
        self._installing = False
        self._installation_message = "安装后，点入文本框并 tap，即可自动切入语音输入法。"
        self._installationFinished.connect(self._installation_finished)
        self._reply_stage = ""
        self._source_switch_factory = source_switch_factory or InputSourceActivation
        self._warm_enabled = bridge_factory is None
        self._source_activation = None
        self._source_pending = False
        self._focus_recovery_attempted = False
        self._startup_probe = None
        self._startup_deadline = 0.0
        self._startup_clients = set()
        self._activation_timer = QTimer(self)
        self._activation_timer.setInterval(150)
        self._activation_timer.timeout.connect(self._refresh_activation)
        self._begin_origin = None
        self._startup_buffering = False
        self._pending_update = None
        self._pending_finish = False
        self._action_pending = None
        self._action_timer = QTimer(self)
        self._action_timer.setSingleShot(True)
        self._action_timer.timeout.connect(self._clear_action_pending)
        self._reply_timer = QTimer(self)
        self._reply_timer.setSingleShot(True)
        self._reply_timer.timeout.connect(self._reply_timed_out)
        self._received.connect(self._accept)
        if self._enabled:
            try:
                self._bridge = (bridge_factory or IMEBridge)(self._received.emit)
                self._bridge.start()
                if bridge_factory is None:
                    QTimer.singleShot(0, self._warm_input_method)
            except Exception as exc:
                self._transport_error = str(exc)

    def _warm_input_method(self):
        if not self._closed and self._enabled and self._warm_enabled:
            warm_input_method()

    @Property(bool, constant=True)
    def enabled(self):
        return self._enabled

    @Property(QObject, constant=True)
    def permissions(self):
        return self._permissions

    @Property(bool, notify=changed)
    def installing(self):
        return self._installing

    @Property(str, notify=changed)
    def installationMessage(self):
        return self._installation_message

    @Slot()
    def installInputMethod(self):
        if not self._enabled or self._installing or self._closed:
            return
        if self.active:
            self._installation_message = "请先结束当前语音，并手动切回拼音或 ABC，再安装／更新输入法。"
            self.changed.emit()
            return
        self._installing = True
        self._installation_message = "正在安装语音输入法…"
        self.changed.emit()

        def work():
            try:
                from ..input_method_install import InputMethodInstaller
                InputMethodInstaller().install()
                message = "已安装并启用。请点入目标文本框并 tap，程序会自动切入语音输入法。"
            except Exception as exc:
                message = "安装未完成：" + str(exc)
            try:
                self._installationFinished.emit(message)
            except RuntimeError:
                pass  # The window may have closed; the atomic installation is complete.

        # Finish the short atomic installation even if the app is closed.
        threading.Thread(target=work, name="ProxiMicIMEInstaller", daemon=False).start()

    @Slot(str)
    def _installation_finished(self, message):
        self._installing = False
        self._installation_message = message
        self.changed.emit()

    @Slot()
    def openInputSettings(self):
        if sys.platform == "darwin":
            QDesktopServices.openUrl(QUrl("x-apple.systempreferences:com.apple.Keyboard-Settings.extension"))

    @Slot()
    def openAccessibilitySettings(self):
        self._permissions.openSettings()

    @Property(bool, notify=changed)
    def ready(self):
        return bool(self._epoch and self._view.get("ready"))

    @Property(str, notify=changed)
    def connectionStatus(self):
        if self._transport_error:
            return "输入法连接失败：" + self._transport_error
        if self._epoch:
            return "输入法已连接 · 已就绪" if self.ready else "输入法已连接 · 点入文本框后 tap 自动开始"
        installed = [Path.home() / "Library/Input Methods/ProxiMicInput.app", Path("/Library/Input Methods/ProxiMicInput.app")]
        if not any(path.exists() for path in installed):
            return "语音输入法未安装"
        return "语音输入法待连接 · 点入文本框后 tap 自动切入"

    @Property(bool, notify=changed)
    def active(self):
        return (self.ready or self._begin_origin is not None) and self._view.get("phase") in {"starting", "listening", "finishing", "dictated", "editing", "edited"}

    @Property(bool, notify=changed)
    def visible(self):
        return self.active and bool(self._view.get("visible", True))

    @Property(bool, notify=changed)
    def canUndo(self):
        return bool(self.active or (self.ready and self._multi_undo_enabled
                    and self._view.get("phase") == "undone"
                    and self._view.get("undo_history_depth", 0) > 0))

    @Property(str, notify=changed)
    def status(self):
        return {"starting": "正在准备", "listening": "听写中", "finishing": "编辑 · 正在收尾" if self._view.get("edit_requested") else "正在定稿", "dictated": "已听写", "editing": "正在编辑", "edited": "已编辑", "undone": "已撤销", "interrupted": "已停止", "error": "输入停止"}.get(self._view.get("phase"), self.connectionStatus)

    @Property(str, notify=changed)
    def error(self):
        return str(self._view.get("error", "") or self._transport_error)

    @Property(bool, notify=changed)
    def canConvert(self):
        return bool(self.active and self._view.get("can_convert", False) and
                    self._view.get("phase") in {"listening", "finishing"})

    @Property(bool, notify=changed)
    def canRequestConversion(self):
        # A manual request must reach the IME so an unavailable capability can
        # produce a specific explanation instead of silently ignoring a swipe.
        return bool(self.active and self._view.get("has_composition") and
                    not self._view.get("edit_requested") and
                    self._view.get("phase") in {"listening", "finishing"})

    @Property(str, notify=changed)
    def cancelLabel(self):
        if self._view.get("phase") == "edited":
            return "还原听写"
        if self._view.get("phase") in {"starting", "listening", "finishing", "editing"}:
            return "取消"
        return "撤销"

    def target_reference(self):
        if not self.ready or not self._client_id:
            return None
        return DesktopTargetRef(0, 0, str(self._view.get("application", "")),
                                process_name=str(self._view.get("application", "")),
                                accessibility_id=f"ime:{self._epoch}:{self._client_id}")

    def context_provider(self):
        if self._bridge is None:
            return {"status": "unavailable", "source": "input_method", "reason": "bridge_unavailable"}
        return self._bridge.asr_context()

    def configure(self, shortcut, *, multi_undo_enabled=None):
        self._shortcut = str(shortcut or "F8")
        if multi_undo_enabled is not None:
            self._multi_undo_enabled = bool(multi_undo_enabled)
        if self._bridge is not None:
            self._bridge.send({"type": "configure", "mode_switch_shortcut": self._shortcut,
                               "cancel_shortcut": "Esc", "multi_undo_enabled": self._multi_undo_enabled})
        self.changed.emit()

    def _send(self, operation, **fields):
        if not self._bridge or not self._epoch or not self._client_id:
            return False
        self._seq += 1
        return self._bridge.send({"type": operation, "client_id": self._client_id,
                                  "utterance_id": self._utterance_id, "seq": self._seq, **fields})

    def _diagnose(self, kind, *, characters=None, error=None, reason=None):
        """Publish an explicit metadata allowlist, never editor or ASR text."""
        readback = self._view.get("readback_diagnostics", {})
        safe_readback = {}
        if isinstance(readback, dict):
            for key in ("expected_characters", "observed_characters"):
                if type(readback.get(key)) is int:
                    safe_readback[key] = readback[key]
            if type(readback.get("text_matches")) is bool:
                safe_readback["text_matches"] = readback["text_matches"]
            for key in ("operation_ms", "post_write_ms", "context_prepare_ms"):
                if type(readback.get(key)) in (int, float):
                    safe_readback[key] = readback[key]
            if readback.get("ack_source") in {"text", "selection_collapse", "native_selection", "committed_range", "composition_end"}:
                safe_readback["ack_source"] = readback["ack_source"]
            for key in ("expected_selection", "observed_selection", "expected_marked", "observed_marked"):
                value = readback.get(key)
                if isinstance(value, list) and len(value) == 2 and all(type(item) is int for item in value):
                    safe_readback[key] = value
        client_read = self._view.get("client_read_diagnostics", {})
        safe_client_read = {}
        if isinstance(client_read, dict):
            if client_read.get("query") in {"snapshot", "probe", "range"}:
                safe_client_read["query"] = client_read["query"]
            for key in ("document_length", "returned_characters", "context_returned_characters", "context_queries", "context_characters"):
                if type(client_read.get(key)) is int:
                    safe_client_read[key] = client_read[key]
            for key in ("document_access", "text_queried", "marked_text_queried", "composition_geometry_only"):
                if type(client_read.get(key)) is bool:
                    safe_client_read[key] = client_read[key]
            for key in ("selection", "reported_selection", "marked_range", "actual_range", "requested_range", "context_requested_range", "context_actual_range", "context_readable_range"):
                value = client_read.get(key)
                if isinstance(value, list) and len(value) == 2 and all(type(item) is int for item in value):
                    safe_client_read[key] = value
        handoff = self._view.get("handoff_diagnostics", {})
        safe_handoff = {}
        if isinstance(handoff, dict):
            if handoff.get("action") in {"waiting", "commit_previous", "reset_stale", "ready", "failed", "cancelled"}:
                safe_handoff["action"] = handoff["action"]
            if handoff.get("write_action") in {"", "commit_previous", "reset_stale"}:
                safe_handoff["write_action"] = handoff["write_action"]
            for key in ("writes", "marked_characters"):
                if type(handoff.get(key)) is int:
                    safe_handoff[key] = handoff[key]
            for key in ("document_access", "text_queried"):
                if type(handoff.get(key)) is bool:
                    safe_handoff[key] = handoff[key]
            for key in ("selection", "marked_range"):
                value = handoff.get(key)
                if isinstance(value, list) and len(value) == 2 and all(type(item) is int for item in value):
                    safe_handoff[key] = value
        event = {
            "type": kind,
            "phase": str(self._view.get("phase", "idle")),
            "ready": self.ready,
            "application": str(self._view.get("application", "")),
            "client_id": self._client_id,
            "utterance_id": self._utterance_id,
            "revision": self._view.get("revision", 0),
            "characters": len(str(self._view.get("raw", ""))) if characters is None else characters,
            "error": self.error if error is None else str(error),
            "reason": str(self._view.get("reason", "")) if reason is None else str(reason),
            "lifecycle_event": str(self._view.get("lifecycle_event", "")),
            "has_composition": bool(self._view.get("has_composition", False)),
            "awaiting_readback": bool(self._view.get("awaiting_readback", False)),
            "readback_stage": str(self._view.get("readback_stage", "")),
            "readback_diagnostics": safe_readback,
            "client_read_diagnostics": safe_client_read,
            "handoff_state": str(self._view.get("handoff_state", "")) if self._view.get("handoff_state", "") in {"pending", "ready", "failed"} else "",
            "handoff_diagnostics": safe_handoff,
        }
        if kind == "state":
            if event == self._last_state_diagnostic:
                return
            self._last_state_diagnostic = event.copy()
        self.diagnostic.emit(event)

    def _foreground_identity(self):
        if sys.platform != "darwin":
            return None
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app and app.bundleIdentifier():
                return (str(app.bundleIdentifier()), int(app.processIdentifier()))
        except Exception:
            pass
        return None

    def _startup_focus_matches(self):
        if self._foreground_identity() == self._begin_origin:
            return True
        source = self._source_activation
        return bool(self._source_pending and source is not None
                    and getattr(source, "owns_foreground", lambda: False)())

    def _recover_selected_source(self):
        if self._focus_recovery_attempted or self._source_activation is None:
            return
        self._focus_recovery_attempted = True
        self._source_activation.cancel()
        source = self._source_switch_factory(self)
        self._source_activation = source
        self._source_pending = True
        source.event.connect(lambda message: self._source_event(source, message))
        self._diagnose("auto_input_source", reason="refreshing_input_context")
        source.recover(self._begin_origin)

    def _refresh_activation(self):
        if self._closed or self._begin_origin is None:
            self._activation_timer.stop()
            return
        if not self._startup_focus_matches():
            self._transport_failed("已切换应用，本句已取消")
            return
        if time.monotonic() >= self._startup_deadline:
            self._reply_timed_out()
            return
        if self._source_pending:
            return
        if not self._bridge or not self._epoch:
            return
        if self._reply_stage == "begin":
            # Recover a delayed/lost BEGIN acknowledgment without replaying the
            # command or resetting the native input source.
            self._bridge.send({"type": "ping"})
            return
        if self._reply_stage != "ready":
            return
        # Observe readiness only. Never switch to ABC and back: that creates
        # another lifecycle transition which can outlive this Tap request.
        self._bridge.send({"type": "ping"})

    def _source_event(self, source, message):
        if self._closed or source is not self._source_activation or self._begin_origin is None:
            return
        if self._foreground_identity() != self._begin_origin:
            self._transport_failed("已切换应用，本句已取消")
            return
        kind = message.get("event")
        self._diagnose("auto_input_source", reason=str(kind), error=str(message.get("error", "")))
        if kind == "error":
            self._transport_failed(str(message.get("error") or "自动切换输入法失败"))
        elif kind == "selected":
            self._source_pending = False
            self._focus_recovery_attempted |= message.get("focus_restored") is True
            # Source selection and IMK activation are separate asynchronous
            # stages. A slow cold TIS select must not consume the client's
            # entire connection budget. The fast path still proceeds at once.
            self._startup_deadline = max(self._startup_deadline, time.monotonic() + 3.0)
            switched = message.get("already_selected") is not True
            self._diagnose("source_selection", reason="switched" if switched else "already_selected")
            self.diagnostic.emit({"type": "input_source_result",
                                  "focus_restored": message.get("focus_restored", False),
                                  "elapsed_ms": message.get("elapsed_ms")})
            # Request a fresh native state; never begin against the cached
            # ready flag from before the system-source check.
            self._wait_for_reply("ready")
            self._bridge.send({"type": "ping"})
            self.changed.emit()

    def _clear_startup(self):
        source, self._source_activation = self._source_activation, None
        self._source_pending = False
        self._focus_recovery_attempted = False
        self._startup_probe = None
        if source is not None:
            source.cancel()
        self._activation_timer.stop()
        self._startup_deadline = 0.0
        self._startup_clients.clear()
        self._begin_origin = None
        self._startup_buffering = False
        self._pending_update = None
        self._pending_finish = False

    def begin(self, target=None, *, auto_select=False):
        if not self._enabled or self._closed:
            return False
        self.actionRequested.emit("begin")
        self._clear_action_pending()
        self._stop_reply_wait()
        self._clear_startup()
        origin = self._foreground_identity()
        needs_activation = auto_select or not self.ready or bool(origin and self._view.get("application") != origin[0])
        if needs_activation and (origin is None or self._transport_error or self._bridge is None):
            self._view = {**self._view, "phase": "error", "error": self.error or self.connectionStatus}
            self._diagnose("begin", reason="input_method_not_ready")
            self.changed.emit()
            self.interrupted.emit()
            self.settled.emit("error", "")
            return False
        self._utterance_id = uuid.uuid4().hex
        self.settled_session_id = None
        self._session_id = 0
        self._seq = 0
        self._final_sent = False
        self._first_text_update_sent = False
        self._requested.clear()
        self._settled.clear()
        self._view = {**self._view, "phase": "starting", "error": "", "reason": "", "raw": "", "edit_requested": False}
        if origin is not None:
            # Fence startup through BEGIN acknowledgment, including a cached
            # ready state. No text goes to a client before this handshake.
            self._begin_origin = origin
            self._startup_buffering = True
            self._startup_deadline = time.monotonic() + (4.0 if auto_select else 3.0)
            self._activation_timer.start()
        if auto_select:
            if not self._epoch:
                self._warm_input_method()
            self._source_pending = True
            source = self._source_switch_factory(self)
            self._source_activation = source
            source.event.connect(lambda message: self._source_event(source, message))
            self._wait_for_reply("ready")
            self._diagnose("begin", reason="checking_input_source")
            self.changed.emit()
            source.start(origin)
            return True
        if needs_activation:
            # Source activation/IPC can trail the first tap. Keep this same
            # audio sentence for up to 3s; never switch the input source or app.
            self._begin_origin = origin
            self._startup_buffering = True
            self._wait_for_reply("ready")
            self._bridge.send({"type": "ping"})
            self._diagnose("begin", reason="waiting_for_input_method")
            self.changed.emit()
            return True
        self.changed.emit()
        self._startup_clients.add(self._client_id)
        sent = self._send("begin")
        if not sent:
            self._transport_failed("输入法连接已断开")
            self._diagnose("begin", reason="send_failed")
        else:
            self._diagnose("begin", reason="requested")
            self._wait_for_reply("begin")
        return sent

    def _wait_for_reply(self, stage):
        self._reply_stage = stage
        remaining = (max(1, int((self._startup_deadline - time.monotonic()) * 1000))
                     if self._begin_origin is not None else 3000)
        self._reply_timer.start(remaining)

    def _stop_reply_wait(self):
        self._reply_timer.stop()
        self._reply_stage = ""

    def _transport_failed(self, reason):
        if self._startup_buffering:
            self._send("reset")
        self._stop_reply_wait()
        self._clear_startup()
        self._view = {**self._view, "phase": "error", "error": reason}
        self._final_sent = True
        self._diagnose("interrupted", reason="input_method_reply_failed")
        self._utterance_id = ""
        self._session_id = 0
        self._requested.clear()
        self.changed.emit()
        self.interrupted.emit()
        self.settled.emit("error", "")

    def _reply_timed_out(self):
        stage = self._reply_stage
        if not stage or self._closed:
            return
        # Retire this exact transaction; never retry its text write.
        self._send("reset")
        self._utterance_id = ""
        self._transport_failed("输入法未连接到当前文本框，请重新点入文本框" if stage == "ready" else
                               "输入法未确认开始听写，请重新点入文本框" if stage == "begin"
                               else "输入法未确认定稿，已停止本句后续处理")

    def bind_session(self, session_id):
        """Bind at audio START, before an older worker can deliver a late result."""
        if self.active and self._utterance_id:
            self._session_id = int(session_id)
            self._remember_session()

    def _remember_session(self):
        if self._utterance_id and self._session_id:
            self._utterance_sessions[self._utterance_id] = self._session_id
            while len(self._utterance_sessions) > 256:
                self._utterance_sessions.pop(next(iter(self._utterance_sessions)))

    def update(self, text, final, session_id, error=""):
        if (self._closed or not self.active or self._final_sent or
                self._view.get("phase") not in {"starting", "listening", "finishing"}):
            return False
        if self._session_id and session_id and self._session_id != session_id:
            return False
        self._session_id = session_id or self._session_id
        self._remember_session()
        if self._startup_buffering:
            # Coalesce partials; a buffered final/error always wins.
            if self._pending_update and (self._pending_update[1] or self._pending_update[3]):
                return False
            self._pending_update = (str(text), bool(final), self._session_id, str(error))
            return True
        accepted = self._send("update", text=str(text), final=bool(final), error=str(error))
        if not accepted:
            self._transport_failed("输入法连接已断开，已停止本句")
            return False
        if accepted and (final or error or (text and not self._first_text_update_sent)):
            self._diagnose("update", characters=len(str(text)), error=error,
                           reason="error" if error else "final" if final else "first_partial")
        if accepted and text:
            self._first_text_update_sent = True
        if accepted and (final or error):
            self._final_sent = True
            self._wait_for_reply("final")
        return accepted

    def finish(self):
        if self._startup_buffering:
            self._pending_finish = True
            return
        if self.active and self._view.get("phase") in {"starting", "listening"}:
            self._send("finish")

    @Slot()
    def convert(self):
        if self.canRequestConversion:
            self._mark_action_pending("convert")
            self._send("convert")

    @Slot()
    def cancel(self):
        self.actionRequested.emit("cancel")
        if self._startup_buffering:
            self.reset()
            self._view = {**self._view, "phase": "undone", "error": ""}
            self.changed.emit()
            self.interrupted.emit()
            self.settled.emit("undone", "")
            return
        if self.canUndo:
            self._mark_action_pending("cancel")
            self._send("cancel")

    def _mark_action_pending(self, action):
        self.actionRequested.emit(action)
        self._action_pending = (self._utterance_id, self._view.get("revision", 0), self._view.get("phase"))
        self._action_timer.start(4000)
        self.changed.emit()

    def _clear_action_pending(self):
        self._action_timer.stop()
        if self._action_pending is not None:
            self._action_pending = None
            self.changed.emit()

    def apply_edit(self, key, text, error):
        current = (self._epoch, self._client_id, self._utterance_id, self._view.get("revision", 0))
        if tuple(key) == current and self._view.get("phase") == "editing":
            self._send("edit_result", revision=current[-1], text=str(text), error=str(error))

    @Slot(object)
    def _accept(self, message):
        if self._closed or not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "connected":
            self._utterance_sessions.clear()
            self.settled_session_id = None
            self._compatibility_requests.clear()
            if self._begin_origin is not None:
                self._epoch = str(message.get("epoch", ""))
                self._client_id = ""
                self._startup_clients.clear()
                self._wait_for_reply("ready")
                self._transport_error = ""
                self.configure(self._shortcut)
                self._diagnose("connected")
                self.changed.emit()
                return
            had_transaction = self.active or bool(self._reply_stage)
            self._clear_startup()
            self._stop_reply_wait()
            self._epoch = str(message.get("epoch", ""))
            self._client_id = self._utterance_id = ""
            self._view = {"phase": "idle", "ready": False}
            if had_transaction:
                self._view["error"] = "输入法连接已重新建立，请重新开始本句"
            self._transport_error = ""
            self._last_state_diagnostic = None
            self.configure(self._shortcut)
            self._diagnose("connected")
            self.changed.emit()
            if had_transaction:
                self.interrupted.emit()
                self.settled.emit("error", "")
            return
        if message.get("epoch") != self._epoch:
            return
        if kind == "activation_trace":
            allowed = {"type", "event", "controller", "lease", "client_id", "activated", "preparing",
                       "native_read", "state_read", "phase", "application", "front_application", "sender", "bound_sender"}
            self.diagnostic.emit({key: value for key, value in message.items()
                                  if key in allowed and isinstance(value, (str, int, bool))})
            return
        if kind == "disconnected":
            if self._begin_origin is not None and self._startup_buffering:
                # No ASR text has been released yet. Keep the same sentence
                # through a cold IME reconnect, bounded by its existing deadline.
                self._epoch = self._client_id = ""
                self._startup_clients.clear()
                self._view = {**self._view, "phase": "starting", "ready": False}
                self._wait_for_reply("ready")
                self._warm_input_method()
                self._diagnose("disconnected", reason="startup_reconnecting")
                self.changed.emit()
                return
            self._utterance_sessions.clear()
            self.settled_session_id = None
            self._stop_reply_wait()
            had_transaction = self.active
            self._clear_startup()
            self._epoch = self._client_id = self._utterance_id = ""
            self._view = {"phase": "idle", "ready": False, "error": "输入法连接已断开"}
            self._last_state_diagnostic = None
            self._diagnose("disconnected", reason="transport_disconnected")
            self.changed.emit()
            if had_transaction:
                self.interrupted.emit()
                self.settled.emit("error", "")
            return
        client_id = str(message.get("client_id", ""))
        utterance_id = str(message.get("utterance_id", ""))
        if kind == "activation_recovery":
            return  # Ignore obsolete native replies from the removed cycling path.
        if kind == "state":
            if self._begin_origin is not None:
                if not self._startup_focus_matches():
                    self._transport_failed("已切换应用，本句已取消")
                    return
                if self._source_pending:
                    return
                # Record transitions that startup intentionally buffers. The
                # ordinary state log only records accepted states, hiding the
                # activation/deactivation sequence behind a startup timeout.
                probe = {key: message.get(key) for key in (
                    "client_id", "utterance_id", "phase", "ready", "application",
                    "lifecycle_event", "handoff_state", "error")}
                if probe != self._startup_probe:
                    self._startup_probe = probe
                    self.diagnostic.emit({"type": "startup_state", **probe,
                                          "expected_utterance_id": self._utterance_id})
                acknowledged = (client_id == self._client_id and utterance_id == self._utterance_id
                                and message.get("ready") and message.get("phase") == "listening")
                if acknowledged:
                    # Only this exact BEGIN ack can release buffered ASR.
                    self._begin_origin = None
                    self._activation_timer.stop()
                elif utterance_id and not (
                        self._source_activation is not None and message.get("ready")
                        and message.get("phase") in {"dictated", "edited", "undone", "interrupted"}
                        and utterance_id != self._utterance_id and client_id not in self._startup_clients):
                    # An obsolete controller may report its empty BEGIN as
                    # interrupted during activation churn. It received no ASR.
                    if (client_id == self._client_id and utterance_id == self._utterance_id
                            and message.get("phase") == "error"):
                        self._transport_failed(str(message.get("error") or "输入法未能开始本句"))
                    return
                else:
                    if not message.get("ready"):
                        if self._reply_stage == "ready":
                            self._recover_selected_source()
                        if not client_id or client_id == self._client_id:
                            self._wait_for_reply("ready")
                        return
                    if message.get("application") != self._begin_origin[0] or not client_id:
                        return
                    if client_id in self._startup_clients:
                        return  # queued idle ping or retired activation
                    self._startup_clients.add(client_id)
                    self._client_id = client_id
                    self._view = {**message, "phase": "starting", "raw": "", "error": ""}
                    if self._send("begin"):
                        self._wait_for_reply("begin")
                        self._diagnose("begin", reason="ready_after_activation")
                        self.changed.emit()
                    else:
                        self._transport_failed("输入法连接已断开")
                    return
            # Idle activation/state messages may switch the active session.
            # Old non-empty transaction IDs can never replace the current one.
            if utterance_id and (client_id != self._client_id or utterance_id != self._utterance_id):
                return
            if (not utterance_id and self._utterance_id and (self.active or self._reply_stage)
                    and client_id == self._client_id
                    and message.get("ready") and message.get("phase") == "idle"):
                # An idle ping captured before BEGIN may arrive afterwards.
                # It cannot retire the new transaction or its acknowledgment timer.
                return
            begin_acknowledged = (self._reply_stage == "begin" and client_id == self._client_id
                                  and utterance_id == self._utterance_id and message.get("ready")
                                  and message.get("phase") == "listening")
            previous_active = self.active
            changed_client = client_id != self._client_id
            if changed_client:
                self._utterance_sessions.clear()
                self._client_id = client_id
                self._utterance_id = ""
                self._view = {}
            if not utterance_id and self._utterance_id and message.get("phase") == "idle":
                self._utterance_id = ""
            self._view = {**self._view, **message}
            if self._action_pending is not None:
                pending_id, pending_revision, pending_phase = self._action_pending
                if (utterance_id != pending_id or self._view.get("revision", 0) != pending_revision
                        or self._view.get("phase") != pending_phase
                        or self._view.get("error") or not self.ready):
                    self._clear_action_pending()
            if (changed_client or not self.ready
                    or (self._reply_stage == "begin" and self._view.get("phase") != "starting")
                    or self._view.get("phase") in {"dictated", "editing", "edited", "undone", "interrupted", "error"}):
                self._stop_reply_wait()
            if not message.get("ready"):
                self._view.pop("original", None)
                self._view["context_complete"] = False
                self._view["error"] = str(message.get("error") or message.get("reason") or self.connectionStatus)
            elif previous_active and changed_client:
                self._view["error"] = str(message.get("error") or message.get("reason") or "已切换输入框")
            self._diagnose("state")
            self.changed.emit()
            if previous_active and (changed_client or not self.ready or self._view.get("phase") == "error"):
                self._clear_startup()
                self.interrupted.emit()
            elif self._startup_buffering and utterance_id and self._view.get("phase") == "listening":
                pending, finish = self._pending_update, self._pending_finish
                self._clear_startup()
                if pending:
                    self.update(*pending)
                if finish and not self._final_sent:
                    self.finish()
            if begin_acknowledged and self.ready and self._view.get("phase") == "listening":
                self.began.emit()
            return
        if self._begin_origin is not None:
            if (kind == "interrupted" and client_id == self._client_id
                    and utterance_id == self._utterance_id):
                reason = str(message.get("reason") or message.get("error") or "")
                if reason and reason not in {"已切换输入框", "已切换输入法或输入框",
                                             "输入法已重新激活", "输入会话已关闭"}:
                    self._transport_failed(reason)
            return
        if client_id != self._client_id or utterance_id != self._utterance_id:
            return
        if kind == "wechat_key":
            request_id = message.get("request_id")
            key = (self._epoch, client_id, utterance_id, request_id)
            application = message.get("application")
            if (not isinstance(request_id, str) or not request_id or key in self._compatibility_requests
                    or not self.active or not self._utterance_id
                    or not isinstance(application, str) or not application
                    or self._view.get("application") != application
                    or (application != "com.tencent.xinWeChat"
                        and message.get("command") in {"select_all", "select_to_start", "caret_from_end", "caret_to_end", "caret_backward", "caret_forward"}
                        and self._view.get("phase") not in {"editing", "edited"})):
                return
            self._compatibility_requests.add(key)
            error = ""
            try:
                from ..wechat_native_keys import send_wechat_key, send_input_method_key
                args = (message.get("command"), message.get("count"), message.get("event_tag"))
                if application == "com.tencent.xinWeChat":
                    send_wechat_key(*args)
                else:
                    send_input_method_key(application, *args)
            except Exception as exc:
                error = str(exc)
                self._permissions.report_error(exc)
            self._send("wechat_key_result", request_id=request_id, error=error)
            self._diagnose("wechat_key", error=error, reason=str(message.get("command", "")))
        elif kind == "finish_audio":
            self.audioEndRequested.emit()
        elif kind == "interrupted":
            self._stop_reply_wait()
            reason = str(message.get("reason") or message.get("error") or self.error or "输入会话已结束")
            self._view = {**self._view, "error": reason, "reason": reason}
            if "lifecycle_event" in message:
                self._view["lifecycle_event"] = str(message["lifecycle_event"])
            if "has_composition" in message:
                self._view["has_composition"] = bool(message["has_composition"])
            self._diagnose("interrupted", reason=reason)
            self.changed.emit()
            self.interrupted.emit()
        elif kind == "edit_requested":
            revision = message.get("revision", 0)
            key = (self._epoch, client_id, utterance_id, revision)
            if (key not in self._requested and self._view.get("phase") == "editing"
                    and message.get("edit_context_available", self._view.get("context_complete"))):
                self._requested.add(key)
                original = message.get("original", self._view.get("original", ""))
                self.editRequested.emit(key, str(message.get("instruction", "")), str(original), self.target_reference())
        elif kind == "settled":
            self._stop_reply_wait()
            self._clear_action_pending()
            phase = str(message.get("phase", ""))
            key = (utterance_id, phase, message.get("revision", self._view.get("revision", 0)), str(message.get("text", "")))
            if key not in self._settled:
                self._settled.add(key)
                source = message.get("source_utterance_id", utterance_id)
                self.settled_session_id = (self._session_id if source == utterance_id
                                           else self._utterance_sessions.get(source, 0))
                self.settled.emit(phase, str(message.get("text", "")))

    def reset(self):
        self.actionRequested.emit("reset")
        self._clear_action_pending()
        self._stop_reply_wait()
        self._clear_startup()
        if self._utterance_id:
            self._send("reset")
        self._utterance_id = ""
        self._session_id = 0
        self._utterance_sessions.clear()
        self.settled_session_id = None
        self._view = {**self._view, "phase": "idle", "raw": ""}
        self._requested.clear()
        self.changed.emit()

    def release_completed_shortcut(self):
        """Retire host bookkeeping after native send/navigation takes over.

        The ordinary key already invalidates the IME's completed composition and
        undo history. Do not send a second reset or let its delayed lifecycle
        messages cancel the next sentence / flash 'input paused'.
        """
        if self._view.get("phase") in {"starting", "listening", "finishing", "editing"} or self._view.get("awaiting_readback"):
            return
        self._stop_reply_wait()
        self._clear_action_pending()
        self._utterance_id = ""
        self._session_id = 0
        self._utterance_sessions.clear()
        self._requested.clear()
        self._settled.clear()
        self._view = {**self._view, "phase": "idle", "raw": "", "error": "",
                      "has_composition": False, "edit_requested": False,
                      "undo_history_depth": 0, "can_cancel": False, "can_convert": False}
        self.changed.emit()

    def close(self):
        if self._closed:
            return
        self.reset()
        self._clear_startup()
        self._closed = True
        self._permissions.close()
        if self._bridge:
            self._bridge.close()
