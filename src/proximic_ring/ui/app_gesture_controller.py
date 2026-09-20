"""App-level gesture routing, settings, and one-shot commit-then-send."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sys
import threading
import time

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Property, QTimer, QUrl, Signal, Slot, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication

from ..app_gestures import (ACTION_LABELS, APP_LABELS, AppBinding, action_phase,
                            default_profiles, profiles_from_json,
                            profiles_to_json, validate_profiles)
from ..app_shortcuts import MacAppShortcuts
from ..gesture_settings import BoundGestureEvent, GESTURE_LABELS
from ..input_source_switch import foreground_pid, select_voice_input_source
from ..wechat_setup import (KEYBOARD_SETTINGS_URL, MENU_TITLES,
                            KeyboardShortcutsNavigator, detect_wechat_menu)
from .shortcut_recorder import shortcut_from_event, shortcut_from_key


@dataclass(frozen=True)
class AppGestureEvent:
    source: object
    target: object
    generation: int
    created: float
    phase: str
    composing: bool
    writing: bool
    edit_requested: bool
    sentence: tuple
    capture_error: str = ""


@dataclass(frozen=True)
class InputSourceGestureEvent:
    source: object
    generation: int
    created: float
    pid: int
    error: str = ""


class AppGestureController(QObject):
    changed = Signal()
    noticeChanged = Signal()
    recordingChanged = Signal()
    shortcutCaptured = Signal(str)
    setupChanged = Signal()
    _setupResult = Signal(str, int, object)
    _inputSourceResult = Signal(str)

    def __init__(self, owner, *, backend=None):
        super().__init__(owner)
        self.owner = owner
        self.backend = backend or MacAppShortcuts()
        self._generation = 0
        self._error = ""
        self._notice = ""
        self._recording = False
        self._recording_error = ""
        self._pending = None
        self._last_dispatch = None
        self._wechat_info = {"detected": False, "message": "选择微信后自动识别菜单语言", "version": ""}
        self._menu_generation = 0
        self._navigation_generation = 0
        self._navigation_cancel = threading.Event()
        self._setup_closed = False
        self._settings_status = ""
        self._setupResult.connect(self._apply_setup_result)
        self._inputSourceResult.connect(self._input_source_result)
        self._source_busy = False
        self._source_last = -float("inf")
        try:
            self._profiles = profiles_from_json(owner._settings.value(
                "gestures/appProfiles", profiles_to_json(default_profiles())))
            validate_profiles(self._profiles, owner._gesture_bindings)
        except (ValueError, TypeError, KeyError):
            self._profiles = default_profiles()
            # Preserve a user's custom start/end gestures, disabling colliding
            # defaults instead of silently rebinding the voice controls.
            self._profiles = {app: {action: AppBinding(b.gesture, b.shortcut,
                              b.gesture not in owner._gesture_bindings.confirm)
                              for action, b in actions.items()}
                              for app, actions in self._profiles.items()}
            self._error = "应用手势已恢复默认；与开始／结束听写冲突的项已关闭"
        available = self._unused_source_gestures()
        saved = owner._settings.value("gestures/inputSourceGesture", None)
        if sys.platform != "darwin":
            self._source_gesture = ""
        elif saved is None:
            # Standalone selection is opt-in; tap handles automatic activation.
            self._source_gesture = ""
        else:
            self._source_gesture = str(saved) if str(saved) in available else ""
        owner._settings.setValue("gestures/inputSourceGesture", self._source_gesture)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self.dismissNotice)
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.timeout.connect(lambda: self.cancel_pending("等待定稿超时，请定稿后重新上滑发送"))

    @Property(bool, constant=True)
    def supported(self):
        return sys.platform == "darwin"

    @Property("QVariantList", constant=True)
    def apps(self):
        return [{"value": key, "label": value} for key, value in APP_LABELS.items()]

    @Property("QVariantList", constant=True)
    def gestures(self):
        return [{"value": key, "label": value} for key, value in GESTURE_LABELS.items()]

    @Property("QVariantMap", notify=changed)
    def profiles(self):
        return {app: [{"action": action, "label": ACTION_LABELS[action], **vars(binding)}
                       for action, binding in actions.items()] for app, actions in self._profiles.items()}

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property("QVariantMap", notify=setupChanged)
    def wechatInfo(self):
        return self._wechat_info

    @Property(str, notify=setupChanged)
    def systemSettingsStatus(self):
        return self._settings_status

    @Property(str, notify=noticeChanged)
    def notice(self):
        return self._notice

    @Property(bool, notify=recordingChanged)
    def recording(self):
        return self._recording

    @recording.setter
    def recording(self, value):
        app = QCoreApplication.instance()
        if app is not None and bool(value) != self._recording:
            if value:
                app.installEventFilter(self)
            else:
                app.removeEventFilter(self)
        self._recording = bool(value)
        self._recording_error = ""
        self.recordingChanged.emit()

    @Property(str, notify=recordingChanged)
    def recordingError(self):
        return self._recording_error

    def eventFilter(self, watched, event):
        if event.type() not in {QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.KeyRelease} or not self._recording:
            return False
        field = QGuiApplication.focusObject()
        if field is None or not field.property("shortcutRecorder") or not field.property("visible"):
            return False
        event.accept()
        if event.type() == QEvent.KeyPress and not event.isAutoRepeat():
            if event.key() == Qt.Key_Escape:
                field.setProperty("focus", False)
                self.recording = False
            else:
                try:
                    value = shortcut_from_event(event)
                except ValueError as exc:
                    self._recording_error = str(exc)
                    self.recordingChanged.emit()
                else:
                    if value:
                        self.shortcutCaptured.emit(value)
        return True

    def notify(self, text, *, duration=2200):
        self._notice = str(text)
        self.noticeChanged.emit()
        self._notice_timer.start(duration)

    @Slot()
    def dismissNotice(self):
        self._notice = ""
        self.noticeChanged.emit()

    def _save(self):
        self._generation += 1
        self.cancel_pending("手势设置已变化，请重新上滑发送")
        self.owner._settings.setValue("gestures/appProfiles", profiles_to_json(self._profiles))
        self._error = ""
        self.changed.emit()

    def validate_voice(self, bindings):
        validate_profiles(self._profiles, bindings)
        self._validate_source_conflict(self._profiles, bindings)

    def _unused_source_gestures(self):
        used = {g for pair in self.owner._gesture_bindings.as_dict().values() for g in pair}
        used.update(b.gesture for actions in self._profiles.values() for b in actions.values() if b.enabled)
        return [g for g in GESTURE_LABELS if g not in used]

    def _validate_source_conflict(self, profiles, voice):
        gesture = self._source_gesture
        if gesture and (any(gesture in pair for pair in voice.as_dict().values()) or
                        any(b.enabled and b.gesture == gesture for actions in profiles.values() for b in actions.values())):
            raise ValueError("该手势用于切换语音输入法，请先修改或关闭输入法切换手势")

    @Property(str, notify=changed)
    def inputSourceGesture(self):
        return self._source_gesture

    @Property(str, notify=changed)
    def inputSourceHint(self):
        return (GESTURE_LABELS[self._source_gesture] + " 切入 ProxiMic Voice"
                if self._source_gesture else "从菜单栏选择 ProxiMic Voice")

    @Property("QVariantList", notify=changed)
    def inputSourceOptions(self):
        return [{"value": "", "label": "未绑定（默认关闭）"}] + [
            {"value": g, "label": GESTURE_LABELS[g]} for g in self._unused_source_gestures()]

    @Slot(str, result=bool)
    def setInputSourceGesture(self, gesture):
        if gesture and gesture not in self._unused_source_gestures():
            self._error = "该手势已分配给听写或应用操作，请选择未占用的手势"
            self.changed.emit()
            return False
        self._source_gesture = gesture
        self.owner._settings.setValue("gestures/inputSourceGesture", gesture)
        self._generation += 1
        self._error = ""
        self.changed.emit()
        self.owner.gestureSettingsChanged.emit()
        return True

    def handle_input_source(self, event):
        if (self._recording or self._source_busy or self._setup_closed
                or event.generation != self._generation
                or time.monotonic() - event.created > 1.0):
            return
        if event.error:
            self.notify(event.error)
            return
        # Explicit input-source selection must not depend on cached sentence
        # state. TIS selection is idempotent when our IME is already selected;
        # real activation/deactivation callbacks own any session transition.
        if time.monotonic() - self._source_last < 0.6:
            return
        self._source_busy = True
        self._source_last = time.monotonic()
        connection = self.owner._disconnect_event
        def work():
            error = ""
            try:
                if (self._setup_closed or connection.is_set()
                        or connection is not self.owner._disconnect_event
                        or event.generation != self._generation):
                    error = "连接或设置已变化，请重新触发手势"
                    return
                select_voice_input_source(event.pid)
            except Exception as exc:
                error = str(exc)
            finally:
                if not self._setup_closed:
                    self._inputSourceResult.emit(error)
        threading.Thread(target=work, name="SelectVoiceInputSource", daemon=True).start()

    @Slot(str)
    def _input_source_result(self, error):
        self._source_busy = False
        if self._setup_closed:
            return
        self.notify(error or "已切换到 ProxiMic Voice")
        self.owner._event_log("INPUT_SOURCE_GESTURE", result="failed" if error else "selected", reason=error)

    @Slot(str, str, str, str, bool, result=bool)
    def setBinding(self, app, action, gesture, shortcut, enabled):
        try:
            if app not in self._profiles or action not in self._profiles[app]:
                raise ValueError("应用操作不存在")
            binding = AppBinding(gesture, shortcut, enabled)
            profiles = {key: dict(value) for key, value in self._profiles.items()}
            profiles[app][action] = binding
            validate_profiles(profiles, self.owner._gesture_bindings)
            self._validate_source_conflict(profiles, self.owner._gesture_bindings)
        except (ValueError, TypeError) as exc:
            self._error = str(exc)
            self.changed.emit()
            return False
        self._profiles = profiles
        self._save()
        return True

    @Slot(str)
    def resetApp(self, app):
        if app not in self._profiles:
            return
        defaults = default_profiles()[app]
        self._profiles = {**self._profiles, app: {
            action: AppBinding(b.gesture, b.shortcut, b.gesture not in self.owner._gesture_bindings.confirm
                              and b.gesture != self._source_gesture)
            for action, b in defaults.items()}}
        self._save()

    @Slot(int, int, result=str)
    def shortcutFromKey(self, key, modifiers):
        try:
            return shortcut_from_key(key, modifiers)
        except ValueError as exc:
            self._recording_error = str(exc)
            self.recordingChanged.emit()
            return ""

    @Slot(str, str, result=str)
    def wechatMenuTitle(self, action, language):
        if action not in {"previous", "next"}:
            return ""
        if language == "auto":
            return str(self._wechat_info.get(action, ""))
        pair = MENU_TITLES.get(language)
        return pair[action == "next"] if pair else ""

    @Slot(str, str)
    def copyWeChatMenu(self, action, language):
        title = self.wechatMenuTitle(action, language)
        if not title:
            self.notify("请先重新识别微信菜单，或手动选择菜单语言")
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard:
            clipboard.setText(title)
            self.notify("菜单名称已复制")

    @Slot()
    def refreshWeChatMenu(self):
        if not self.supported or self._setup_closed:
            return
        self._menu_generation += 1
        generation = self._menu_generation
        self._wechat_info = {**self._wechat_info, "message": "正在识别微信版本和菜单语言…"}
        self.setupChanged.emit()
        def work():
            try:
                result = detect_wechat_menu()
            except Exception as exc:
                result = {"detected": False, "message": "无法读取微信菜单：" + str(exc), "version": ""}
            if not self._setup_closed:
                self._setupResult.emit("menu", generation, result)
        threading.Thread(target=work, name="WeChatMenuSetup", daemon=True).start()

    @Slot(str, int, object)
    def _apply_setup_result(self, kind, generation, result):
        if self._setup_closed:
            return
        if kind == "menu" and generation == self._menu_generation:
            self._wechat_info = result
            self.owner._event_log("WECHAT_SETUP", operation="detect_menu",
                                  detected=bool(result.get("detected")),
                                  version=result.get("version", ""), language=result.get("language", ""))
            if result.get("detected"):
                signature = json.dumps([result["previous"], result["next"]], ensure_ascii=False)
                previous = self.owner._settings.value("gestures/wechatMenuTitles", "")
                if previous and previous != signature:
                    self._wechat_info["message"] = "微信菜单语言已变化，请同步更新系统 App 快捷键中的菜单标题"
                self.owner._settings.setValue("gestures/wechatMenuTitles", signature)
        elif kind == "settings" and generation == self._navigation_generation:
            self.owner._event_log("WECHAT_SETUP", operation="open_app_shortcuts", result=result)
            self._settings_status = {
                "ready": "已打开 App 快捷键。点击「＋」，应用选择微信，再添加下面两个菜单及对应按键。",
                "cancelled": "已停止定位。需要时可再次打开 App 快捷键。",
            }.get(result, "请在键盘设置中点击「键盘快捷键」，再在左侧选择「App 快捷键」。无需开启「功能键」开关。")
        else:
            return
        self.setupChanged.emit()

    @Slot()
    def openSystemShortcuts(self):
        if not self.supported:
            return
        self._navigation_cancel.set()
        self._navigation_cancel = cancel = threading.Event()
        self._navigation_generation += 1
        generation = self._navigation_generation
        opened = QDesktopServices.openUrl(QUrl(KEYBOARD_SETTINGS_URL))
        self._settings_status = ("正在打开 App 快捷键…" if opened
                                 else "未能打开系统设置，请进入「系统设置 → 键盘 → 键盘快捷键 → App 快捷键」。")
        self.setupChanged.emit()
        if not opened:
            return
        def navigate():
            try:
                result = KeyboardShortcutsNavigator().run(cancel)
            except Exception:
                result = "manual"
            if not self._setup_closed:
                self._setupResult.emit("settings", generation, result)
        threading.Thread(target=navigate, name="AppShortcutSettings", daemon=True).start()

    def sentence(self):
        inline = self.owner._inline_input
        return (inline._epoch, inline._client_id, inline._utterance_id)

    def phase_state(self):
        owner = self.owner
        inline = getattr(owner, "_inline_input", None)
        view = inline._view if inline is not None and inline.enabled else {}
        phase = str(view.get("phase", "idle"))
        if getattr(owner, "_utterance_active", False) and phase in {"idle", "dictated", "edited", "undone", "interrupted", "error"}:
            phase = "listening"
        if owner._inline_requests:
            phase = "editing"
        if inline is not None and inline._action_pending:
            phase = "applying"
        writing = bool(view.get("awaiting_readback") or (inline and inline._reply_stage == "final")
                       or (inline and inline._action_pending) or view.get("handoff_state") == "pending"
                       or getattr(owner, "_undo_running", False))
        return {"phase": phase, "composing": bool(view.get("has_composition")),
                "writing": writing, "edit_requested": bool(view.get("edit_requested")) and phase not in {"edited", "dictated", "undone", "idle"}}

    def envelope(self, event):
        """Capture the destination and phase at recognition, before Qt queues it."""
        source = event.event if isinstance(event, BoundGestureEvent) else event
        name = str(getattr(source, "name", ""))
        if name and name == self._source_gesture:
            created, generation = time.monotonic(), self._generation
            try:
                pid, error = foreground_pid(), ""
            except Exception as exc:
                pid, error = 0, str(exc)
            return InputSourceGestureEvent(event, generation, created, pid, error)
        if not any(b.enabled and b.gesture == name for actions in self._profiles.values() for b in actions.values()):
            return event
        created = time.monotonic()
        state, sentence, generation = self.phase_state(), self.sentence(), self._generation
        capture_error = ""
        try:
            target = self.backend.capture()
        except Exception as exc:
            target = None
            capture_error = f"{type(exc).__name__}: {exc}"
        return AppGestureEvent(event, target, generation, created, sentence=sentence,
                               capture_error=capture_error, **state)

    def handle(self, event: AppGestureEvent) -> bool:
        source = event.source.event if isinstance(event.source, BoundGestureEvent) else event.source
        name = str(getattr(source, "name", ""))
        target = event.target
        if event.capture_error:
            self.notify("应用手势无法读取前台应用，请查看运行日志")
            self.owner._event_log("APP_GESTURE", gesture=name, result="blocked",
                                  reason="target_capture_failed", error=event.capture_error)
            return True
        if target is None:
            return False
        actions = self._profiles[target.profile]
        action = next((key for key, b in actions.items() if b.enabled and b.gesture == name), None)
        if action is None:
            return False
        if self._recording:
            return True
        if event.generation != self._generation or time.monotonic() - event.created > 1.0:
            self.notify("操作已过期，请重新触发手势")
            return True
        if target.blocked:
            self.notify("请回到主聊天输入区域操作")
            return True
        original = action_phase(action, phase=event.phase, composing=event.composing,
                                writing=event.writing, edit_requested=event.edit_requested)
        current = action_phase(action, **self.phase_state())
        if original not in {"dispatch", "finish_then_send"}:
            self.notify(original)
            return True
        if current not in {"dispatch", "finish_then_send"}:
            self.notify(current)
            return True
        just_committed = (original == "finish_then_send" and current == "dispatch"
                          and event.sentence == self.sentence()
                          and self.owner._inline_input._view.get("phase") == "dictated"
                          and bool(str(self.owner._inline_input._view.get("raw", "")).strip())
                          and not self.owner._inline_input.error)
        if (original != current and not just_committed) or (original == "finish_then_send" and event.sentence != self.sentence()):
            self.notify("本句状态已变化，请重新上滑发送")
            return True
        if self._pending is not None:
            if action != "send":
                self.notify("正在定稿并准备发送，请稍后再切换对话")
            return True
        if current == "finish_then_send":
            inline = self.owner._inline_input
            if not inline.enabled or not inline.ready or not inline._utterance_id:
                self.notify("听写输入框尚未就绪，请稍后上滑发送")
                return True
            try:
                same_target = self.backend.same_target(target, require_focus=True)
            except Exception:
                same_target = False
            if inline._view.get("application") != target.bundle or not same_target:
                self.notify("输入目标已变化，请在原对话定稿后发送")
                return True
            self._pending = (event, actions[action])
            self._pending_timer.start(12000)
            self.owner._finish_utterance_event.set()
            inline.finish()
            self.notify("正在定稿，完成后发送；左滑可取消", duration=12000)
            return True
        self._dispatch(action, actions[action], target)
        return True

    def _dispatch(self, action, binding, target):
        signature = (target.bundle, target.pid, action)
        now = time.monotonic()
        if self._last_dispatch and self._last_dispatch[0] == signature and now - self._last_dispatch[1] < 0.35:
            return
        try:
            if action == "send" and target.role and target.role not in {"AXTextArea", "AXTextField"}:
                raise RuntimeError("请先点入消息输入框，再上滑发送")
            self.backend.post(target, binding.shortcut, require_focus=action == "send")
            self._last_dispatch = (signature, now)
            self._retire_sentence(target)
            self.owner._event_log("APP_GESTURE", app=target.profile, action=action,
                                  shortcut=binding.shortcut, result="posted")
            self.dismissNotice()
        except Exception as exc:
            permissions = getattr(self.owner._inline_input, "permissions", None)
            if permissions is not None:
                permissions.report_error(exc)
            self.notify(str(exc))
            self.owner._event_log("APP_GESTURE", app=target.profile, action=action,
                                  result="blocked", reason=str(exc))

    def _retire_sentence(self, target):
        inline = self.owner._inline_input
        if inline.enabled and inline._view.get("application") == target.bundle:
            inline.release_completed_shortcut()

    def settled(self, phase, text):
        if self._pending is None:
            return
        event, binding = self._pending
        if event.sentence != self.sentence() or phase != "dictated" or not str(text).strip():
            self.cancel_pending("本句没有成功定稿，未发送")
            return
        if (self._generation != event.generation or self.owner._disconnect_event.is_set()
                or not self.owner._connected or not self.owner._runtime_active):
            self.cancel_pending("连接或设置已变化，未发送")
            return
        if action_phase("send", **self.phase_state()) != "dispatch":
            self.cancel_pending("文字尚未完成应用，请完成后重新上滑发送")
            return
        self._pending = None
        self._pending_timer.stop()
        self._dispatch("send", binding, event.target)

    def state_changed(self):
        if self._pending is None:
            return
        event, _ = self._pending
        inline = self.owner._inline_input
        if (event.sentence != self.sentence() or not inline.ready
                or inline._view.get("phase") in {"editing", "edited", "undone", "interrupted", "error"}
                or inline._view.get("edit_requested")):
            self.cancel_pending("本句已取消、转编辑或切换输入框，未发送")

    def cancel_pending(self, reason=""):
        if self._pending is not None:
            self._pending = None
            self._pending_timer.stop()
            if reason:
                self.notify(reason)
            else:
                self.dismissNotice()

    def close(self):
        app = QCoreApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.recording = False
        self._setup_closed = True
        self._navigation_cancel.set()
        self.cancel_pending()
        self._notice_timer.stop()
