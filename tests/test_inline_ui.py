from pathlib import Path

import pytest

from proximic_ring.text_processing import TextProcessingResult


class FakeBridge:
    def __init__(self, callback):
        self.callback = callback
        self.messages = []
        self.epoch = "connection-one"

    def start(self):
        pass

    def send(self, message):
        self.messages.append({**message, "epoch": self.epoch})
        return True

    def close(self):
        pass

    def asr_context(self):
        return {"status": "unavailable", "source": "input_method"}


def test_app_gesture_settings_layout_and_shortcut_recorder(inline_ui, tmp_path, monkeypatch):
    from PySide6.QtCore import QObject, QMetaObject, QPointF, Qt
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    import proximic_ring.ui.app_gesture_controller as app_gestures

    controller, _, _, root, event = inline_ui
    dialog = root.findChild(QObject, "runtimeSettingsDialog")
    section = root.findChild(QQuickItem, "appGestureSettingsSection")
    picker = root.findChild(QObject, "appGestureAppPicker")
    root.show()
    QMetaObject.invokeMethod(dialog, "open")
    QTest.qWait(150)
    assert dialog.property("currentPage") == 0
    assert not section.isVisible()
    assert root.grabWindow().save("/private/tmp/proximic-settings-home.png")
    QMetaObject.invokeMethod(root.findChild(QObject, "settingsCategory2"), "click")
    QTest.qWait(30)
    jump = root.findChild(QQuickItem, "openAppGestureSettingsButton")
    assert jump is not None and jump.isVisible()
    QMetaObject.invokeMethod(jump, "click")
    QTest.qWait(30)
    assert dialog.property("currentPage") == 7
    section_pos = section.mapToScene(QPointF(0, 0))
    assert 0 <= section_pos.y() < root.height()
    def visual_child(item, name):
        if item.objectName() == name:
            return item
        for child in item.childItems():
            found = visual_child(child, name)
            if found is not None:
                return found
        return None
    # ComboBox.currentValue changes the actual repeater model for all three apps.
    for index, app in enumerate(["codex", "workbuddy", "wechat"]):
        picker.setProperty("currentIndex", index)
        QTest.qWait(30)
        assert section.property("selectedApp") == app
        for action in controller.appGestures._profiles[app]:
            field = visual_child(section, "appGestureShortcut_" + action)
            assert field is not None and field.width() >= 140
            pos = field.mapToItem(section, QPointF(0, 0))
            assert 0 <= pos.x() and pos.x() + field.width() <= section.width()
    assert root.grabWindow().save("/private/tmp/proximic-settings-shortcuts.png")
    QMetaObject.invokeMethod(root.findChild(QObject, "openWeChatGuideButton"), "click")
    QTest.qWait(30)
    guide = root.findChild(QQuickItem, "wechatShortcutGuide")
    assert dialog.property("currentPage") == 8 and guide.isVisible() and not section.isVisible()
    assert root.grabWindow().save("/private/tmp/proximic-settings-wechat.png")
    assert root.findChild(QObject, "openAppShortcutsButton") is not None
    calls = []
    monkeypatch.setattr(app_gestures.QDesktopServices, "openUrl", lambda url: calls.append(url.toString()) or True)
    QMetaObject.invokeMethod(root.findChild(QObject, "openAppShortcutsButton"), "click")
    assert calls == ["x-apple.systempreferences:com.apple.Keyboard-Settings.extension"]
    QTest.qWait(30)
    assert "App 快捷键" in controller.appGestures.systemSettingsStatus
    assert controller.appGestures.wechatInfo["language"] == "en"
    assert guide.property("previousMenu") == "Show Previous Chat"
    language_picker = root.findChild(QObject, "wechatMenuLanguagePicker")
    language_picker.setProperty("currentIndex", 1)
    assert guide.property("previousMenu") == "显示上一个聊天"
    language_picker.setProperty("currentIndex", 0)
    assert root.findChild(QObject, "wechatShortcutsVerified") is None
    QTest.qWait(30)
    assert "不能自行起名" in root.findChild(QObject, "wechatSetupInstructions").property("text")
    assert "Show Previous Chat" in visual_child(guide, "wechatMenuInstruction_previous").property("text")
    assert "Show Next Chat" in visual_child(guide, "wechatMenuInstruction_next").property("text")
    assert "⌘ Command + ⇧ Shift + [" in visual_child(guide, "wechatShortcutInstruction_previous").property("text")
    controller.appGestures.setBinding("wechat", "next", "circle-clockwise", "Cmd+Alt+]", True)
    QTest.qWait(30)
    assert "⌘ Command + ⌥ Option + ]" in visual_child(guide, "wechatShortcutInstruction_next").property("text")
    language_picker.setProperty("currentIndex", 1)
    QTest.qWait(30)
    assert "显示上一个聊天" in visual_child(guide, "wechatMenuInstruction_previous").property("text")
    language_picker.setProperty("currentIndex", 0)
    labels = [child.property("text") for child in section.findChildren(QObject) if child.property("text")]
    assert "测试上一个聊天" not in labels and "测试下一个聊天" not in labels
    value = controller.appGestures.shortcutFromKey(int(Qt.Key_BracketLeft), Qt.ControlModifier.value | Qt.ShiftModifier.value)
    assert value == "Cmd+Shift+["
    QMetaObject.invokeMethod(root.findChild(QObject, "settingsBackButton"), "click")
    QTest.qWait(30)
    assert dialog.property("currentPage") == 7
    field = visual_child(section, "appGestureShortcut_send")
    point = field.mapToScene(QPointF(field.width() / 2, field.height() / 2)).toPoint()
    QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, point)
    assert field.property("activeFocus") and controller.appGestures.recording
    QTest.keyClick(root, Qt.Key_Return, Qt.ControlModifier)
    QTest.qWait(30)
    assert controller.appGestures._profiles["wechat"]["send"].shortcut == "Cmd+Return"
    assert not controller.appGestures.recording
    # Render the QML component itself (not the user's desktop) for visual QA.
    grab = section.grabToImage()
    QTest.qWait(100)
    image = grab.image()
    assert not image.isNull()
    image.save("/private/tmp/proximic-app-gesture-settings.png")
    QMetaObject.invokeMethod(dialog, "close")


def test_shortcut_settings_reports_fallback_without_claiming_success(inline_ui, monkeypatch):
    import proximic_ring.ui.app_gesture_controller as module
    controller, *_ = inline_ui
    calls = []
    monkeypatch.setattr(module.QDesktopServices, "openUrl", lambda url: calls.append(url.toString()) or False)
    controller.appGestures.openSystemShortcuts()
    assert len(calls) == 1
    assert "未能打开" in controller.appGestures.systemSettingsStatus


def test_shortcut_click_capture_cancel_invalid_key_and_shortcut_override(inline_ui):
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
    from PySide6.QtTest import QTest
    c, _, _, root, _ = inline_ui
    root.show()
    dialog = root.findChild(QObject, "runtimeSettingsDialog")
    QMetaObject.invokeMethod(dialog, "open")
    QTest.qWait(150)
    for name in ("settingsCategory2", "openAppGestureSettingsButton"):
        QMetaObject.invokeMethod(root.findChild(QObject, name), "click")
        QTest.qWait(20)
    def field():
        section = root.findChild(QObject, "appGestureSettingsSection")
        def find(item):
            if item.objectName() == "appGestureShortcut_send":
                return item
            for child in item.childItems():
                result = find(child)
                if result is not None:
                    return result
        return find(section)
    def click_field():
        f = field()
        p = f.mapToScene(QPointF(f.width()/2, f.height()/2)).toPoint()
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier, p)
        assert f.property("activeFocus") and c.appGestures.recording
        return f
    click_field()
    QTest.keyClick(root, Qt.Key_A)
    assert c.appGestures.recording and c.appGestures.recordingError
    assert c.appGestures._profiles["codex"]["send"].shortcut == "Return"
    QTest.keyClick(root, Qt.Key_Escape)
    assert not c.appGestures.recording and dialog.property("opened")
    assert c.appGestures._profiles["codex"]["send"].shortcut == "Return"
    conflict = QShortcut(QKeySequence("Ctrl+N"), root)
    activated = []
    conflict.activated.connect(lambda: activated.append(True))
    click_field()
    QTest.keyClick(root, Qt.Key_N, Qt.ControlModifier)
    QTest.qWait(20)
    assert c.appGestures._profiles["codex"]["send"].shortcut == "Cmd+N"
    assert not activated and not c.appGestures.recording
    click_field()
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_BraceLeft, Qt.ControlModifier | Qt.ShiftModifier,
                      0, 33, 0x120000, "{")
    QCoreApplication.sendEvent(root, event)
    QTest.qWait(20)
    assert c.appGestures._profiles["codex"]["send"].shortcut == "Cmd+Shift+["
    click_field()
    QMetaObject.invokeMethod(root.findChild(QObject, "settingsBackButton"), "click")
    QTest.qWait(20)
    assert dialog.property("currentPage") == 2 and not c.appGestures.recording
    QMetaObject.invokeMethod(root.findChild(QObject, "settingsApplyButton"), "click")
    QTest.qWait(150)
    assert not dialog.property("visible") and not c.appGestures.recording
    conflict.deleteLater()


def test_settings_pages_and_back_navigation_fit_small_window(inline_ui):
    from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QPointF
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    _, _, _, root, _ = inline_ui
    root.resize(940, 700)
    root.show()
    dialog = root.findChild(QObject, "runtimeSettingsDialog")
    QMetaObject.invokeMethod(dialog, "open")
    QTest.qWait(150)
    for index in range(1, 7):
        QMetaObject.invokeMethod(root.findChild(QObject, f"settingsCategory{index}"), "click")
        QTest.qWait(25)
        assert dialog.property("currentPage") == index
        assert root.findChild(QQuickItem, f"settingsPage{index}").isVisible()
        assert not root.findChild(QQuickItem, "settingsPage0").isVisible()
        done = root.findChild(QQuickItem, "settingsApplyButton")
        p = done.mapToScene(QPointF(0, 0))
        assert 0 <= p.y() and p.y() + done.height() <= root.height()
        assert root.grabWindow().save(f"/private/tmp/proximic-settings-page-{index}.png")
        QMetaObject.invokeMethod(root.findChild(QObject, "settingsBackButton"), "click")
        assert dialog.property("currentPage") == 0
    QMetaObject.invokeMethod(dialog, "close")


@pytest.fixture
def inline_ui(tmp_path, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QSettings, QUrl, QEvent
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    import proximic_ring.ui.controller as module
    import proximic_ring.ui.inline_controller as inline
    import proximic_ring.ui.app_gesture_controller as gesture_setup
    from proximic_ring.wechat_setup import menu_details
    from types import SimpleNamespace
    monkeypatch.setattr(gesture_setup, "detect_wechat_menu", lambda: menu_details(
        ["Show Previous Chat", "Show Next Chat"], version="4.1.11"))
    monkeypatch.setattr(gesture_setup, "KeyboardShortcutsNavigator", lambda: SimpleNamespace(run=lambda cancel: "ready"))
    app = QCoreApplication.instance()
    if app is not None and not isinstance(app, QApplication):
        pytest.skip("QML requires a QApplication process")
    app = app or QApplication(["ime-tests", "-platform", "offscreen"])
    monkeypatch.setattr(module, "app_data_root", lambda: tmp_path)
    monkeypatch.setattr(module, "QSettings", lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.IniFormat))
    monkeypatch.setattr(inline, "IMEBridge", FakeBridge)
    monkeypatch.setattr(inline, "warm_input_method", lambda: None)
    monkeypatch.setattr(inline.InlineInputController, "_foreground_identity", lambda self: None)
    controller = module.AppController(inline_input_enabled=True)
    controller._text_processing_worker.close(wait=True)
    controller._desktop_output = True
    component = controller._inline_input
    component._accept({"type": "connected", "epoch": "connection-one"})
    component._accept({"type": "state", "epoch": "connection-one", "client_id": "client-one", "utterance_id": "", "phase": "idle", "ready": True, "application": "test.editor", "original": "明天三点开会", "selection": [6, 0], "context_complete": True})
    requests = []
    monkeypatch.setattr(controller._text_processing_worker, "submit", requests.append)
    monkeypatch.setattr(controller._text_processing_worker, "submit_routing", lambda _: pytest.fail("automatic routing must not run"))
    monkeypatch.setattr(controller, "_desktop_target_adapter", lambda: pytest.fail("IME must not access AX"))
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
    engine.rootContext().setContextProperty("appController", controller)
    engine.load(QUrl.fromLocalFile(str(Path(module.__file__).parent / "qml/Main.qml")))
    assert engine.rootObjects(), warnings
    root = engine.rootObjects()[0]
    root.hide()
    controller._recognition_enabled = True
    controller._apply_runtime_status("[ASR] START t=1.000s")

    def event(kind="state", **fields):
        component._accept({"type": kind, "epoch": component._epoch, "client_id": component._client_id, "utterance_id": component._utterance_id, **fields})

    event(phase="listening", ready=True, revision=0, original="明天三点开会", selection=[6, 0], context_complete=True)
    yield controller, component._bridge, requests, root, event
    component.close()
    controller._close_voice_history()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not warnings


def test_native_component_is_only_input_route_and_qml_has_no_overlay(inline_ui):
    from PySide6.QtQuick import QQuickWindow
    controller, bridge, requests, root, event = inline_ui
    controller._apply_runtime_update("你好", False, "", 1)
    assert bridge.messages[-1]["text"] == "你好"
    assert bridge.messages[-1]["type"] == "update"
    assert root.findChild(QQuickWindow, "inlineActionWindow") is None
    assert root.findChild(QQuickWindow, "inlineUnderlineWindow") is None
    assert not root.findChild(QQuickWindow, "transcriptOverlay").isVisible()
    assert not root.findChild(QQuickWindow, "appliedActionOverlay").isVisible()
    assert requests == []
    assert controller._speech_start_target.accessibility_id.startswith("ime:")
    assert controller._desktop_target is None
    controller._apply_runtime_status("[ASR] END user")
    assert bridge.messages[-1]["type"] == "finish"


def test_input_method_setup_installs_asynchronously_and_displays_result(inline_ui, monkeypatch):
    import threading
    from PySide6.QtCore import QObject, QMetaObject
    from PySide6.QtTest import QTest
    from proximic_ring.input_method_install import InputMethodInstaller
    controller, _, _, root, event = inline_ui
    component = controller._inline_input
    event(phase="idle", ready=False)
    released = threading.Event()
    calls = []
    def install(_):
        calls.append(True)
        assert released.wait(2)
        return {"installed": True}
    monkeypatch.setattr(InputMethodInstaller, "install", install)
    dialog = root.findChild(QObject, "inputMethodSetupDialog")
    button = root.findChild(QObject, "installInputMethodButton")
    QMetaObject.invokeMethod(dialog, "open")
    QTest.qWait(200)
    assert dialog.property("visible")
    QMetaObject.invokeMethod(button, "click")
    QTest.qWait(20)
    assert component.installing and not button.property("enabled")
    component.installInputMethod()  # Double click cannot launch a second installer.
    released.set()
    for _ in range(100):
        QTest.qWait(10)
        if not component.installing:
            break
    assert calls == [True]
    assert "已安装并启用" in component.installationMessage
    assert button.property("enabled")
    QMetaObject.invokeMethod(dialog, "close")


def test_input_method_setup_does_not_install_during_live_dictation(inline_ui, monkeypatch):
    from proximic_ring.input_method_install import InputMethodInstaller
    controller, _, _, _, _ = inline_ui
    monkeypatch.setattr(InputMethodInstaller, "install", lambda _: pytest.fail("live sentence must not be interrupted"))
    controller._inline_input.installInputMethod()
    assert not controller._inline_input.installing
    assert "先结束当前语音" in controller._inline_input.installationMessage


@pytest.mark.parametrize("size", [(1080, 820), (940, 700)])
@pytest.mark.parametrize("connected,recognizing", [(False, False), (True, False), (True, True)])
def test_connection_controls_stay_inside_card_and_history_remains_accessible(
    inline_ui, size, connected, recognizing
):
    from PySide6.QtCore import QPointF
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest

    controller, _, _, root, _ = inline_ui
    controller._connected = connected
    controller.connectedChanged.emit()
    controller._recognition_enabled = recognizing
    controller.recognitionEnabledChanged.emit()
    controller._apply_runtime_battery(71, 3900, 0)
    root.resize(*size)
    root.show()
    card = root.findChild(QQuickItem, "voiceInputCard")
    history = root.findChild(QQuickItem, "voiceHistoryCard")
    # Wrapped status text must grow the card without pushing actions out.
    for detail in ["本句已取消", "输入法已连接，请选择语音输入法并点入当前应用的文本框。" * 4]:
        controller._set_status("听写已停止", detail, "ready")
        QTest.qWait(60)
        for name in ["primaryConnectionButton", "secondaryConnectionButton"]:
            button = root.findChild(QQuickItem, name)
            if not button.isVisible():
                continue
            position = button.mapToItem(card, QPointF(0, 0))
            assert position.x() >= 20
            assert position.x() + button.width() <= card.width() - 20
            assert position.y() + button.height() <= card.height() - 20
            assert button.mapToScene(QPointF(0, button.height())).y() <= root.height()
            assert button.height() >= 44
        assert history.mapToScene(QPointF(0, 0)).y() >= (
            card.mapToScene(QPointF(0, card.height())).y() + 18
        )
        assert history.height() >= 250

    page = root.findChild(QQuickItem, "mainPageScroll")
    flickable = page.property("contentItem")
    flickable.setProperty("contentY", max(0, page.property("contentHeight") - page.property("availableHeight")))
    QTest.qWait(60)
    footer = root.findChild(QQuickItem, "openAssociationCenterButton")
    assert footer.mapToScene(QPointF(0, 0)).y() >= 78
    assert footer.mapToScene(QPointF(0, footer.height())).y() <= root.height() - 24


def test_runtime_log_does_no_hidden_layout_and_coalesces_visible_bursts(inline_ui):
    from PySide6.QtCore import QObject, QMetaObject
    from PySide6.QtTest import QTest
    controller, _, _, root, _ = inline_ui
    dialog = root.findChild(QObject, "runtimeLogDialog")
    area = root.findChild(QObject, "logArea")
    before = area.property("text")
    for index in range(60):
        controller._append_log(f"hidden event {index}")
    QTest.qWait(200)
    assert area.property("text") == before
    QMetaObject.invokeMethod(dialog, "open")
    QTest.qWait(250)
    assert area.property("text") == controller.logText
    before = area.property("text")
    for index in range(60):
        controller._append_log(f"visible event {index}")
    assert area.property("text") == before
    QTest.qWait(200)
    assert area.property("text") == controller.logText
    QMetaObject.invokeMethod(dialog, "close")
    QTest.qWait(250)
    before = area.property("text")
    controller.clearLog()
    QTest.qWait(200)
    assert area.property("text") == before
    QMetaObject.invokeMethod(dialog, "open")
    QTest.qWait(250)
    assert area.property("text") == "尚未启动"


def test_partial_geometry_is_persisted_without_growing_live_log(inline_ui):
    controller, _, _, _, _ = inline_ui
    before = controller.logText
    controller._log_inline_diagnostic({"phase": "listening", "characters": 4321})
    assert controller.logText == before
    diagnostic = Path(controller.diagnosticLogPath).read_text()
    assert 'characters=4321' in diagnostic


def test_multi_undo_setting_is_visible_persisted_and_immediately_configures_native(inline_ui):
    from PySide6.QtCore import QObject
    controller, bridge, _, root, event = inline_ui
    control = root.findChild(QObject, "multiUndoSwitch")
    assert control is not None
    assert not controller.multiUndoEnabled
    controller.multiUndoEnabled = True
    assert control.property("checked") is True
    assert controller._settings.value("input/multiUndoEnabled", type=bool) is True
    assert bridge.messages[-1]["multi_undo_enabled"] is True
    event(phase="undone", ready=True, undo_history_depth=2)
    assert controller.nativeUndoAvailable
    controller.undoLastApplied()
    assert bridge.messages[-1]["type"] == "cancel"
    controller.multiUndoEnabled = False
    assert not controller.nativeUndoAvailable
    assert bridge.messages[-1]["multi_undo_enabled"] is False


def test_historical_undo_records_previous_session_without_reassigning_active_audio(inline_ui, monkeypatch):
    controller, _, _, _, event = inline_ui
    controller._latest_asr_session_id = 12
    controller._inline_input.settled_session_id = 11
    recorded = []
    monkeypatch.setattr(controller._modification_dataset, "record_application", lambda **kw: recorded.append(kw))
    controller._settle_inline_input("undone", "")
    assert recorded[-1]["session_id"] == 11
    assert controller._latest_asr_session_id == 12


def test_final_is_sent_once_and_native_settlement_resumes_audio(inline_ui):
    controller, bridge, _, _, event = inline_ui
    controller._connected = True
    controller._recognition_event.set()
    controller._apply_runtime_update("定稿", True, "", 1)
    assert not controller._recognition_event.is_set()
    count = len(bridge.messages)
    controller._apply_runtime_update("重复定稿", True, "", 1)
    assert len(bridge.messages) == count
    event(phase="dictated", ready=True, raw="定稿", revision=0)
    event("settled", phase="dictated", text="定稿")
    assert controller._recognition_event.is_set()
    assert controller.statusTitle == "已听写"
    controller._apply_runtime_update("重复定稿", True, "", 1)
    assert controller._recognition_event.is_set()


def test_old_result_cannot_rebind_new_sentence_or_close_its_gate(inline_ui):
    controller, bridge, _, _, event = inline_ui
    controller._connected = True
    controller._recognition_event.set()
    controller._apply_runtime_session_started(12)
    count = len(bridge.messages)
    controller._apply_runtime_update("旧句迟到结果", True, "", 11)
    assert controller._latest_asr_session_id == 12
    assert len(bridge.messages) == count
    assert controller._recognition_event.is_set()
    controller._apply_runtime_update("本句", False, "", 12)
    assert bridge.messages[-1]["text"] == "本句"


def test_cancel_ack_reopens_gate_when_audio_end_races_ui_cancellation(inline_ui):
    controller, _, _, _, event = inline_ui
    controller._connected = True
    controller._recognition_event.set()
    controller._apply_runtime_session_started(2)
    event("interrupted", reason="已切换输入框")
    assert controller._cancel_utterance_event.is_set()
    controller._suspend_recognition_for_interaction()  # END already in flight.
    assert not controller._recognition_event.is_set()
    controller._apply_runtime_status("[ASR] END reason=gesture-tap")
    controller._apply_runtime_status("[ASR] CANCELLED reason=user-request")
    assert controller._recognition_event.is_set()


def test_final_asr_error_does_not_reclose_cancelled_recognition_gate(inline_ui):
    controller, _, _, _, _ = inline_ui
    controller._connected = True
    controller._recognition_event.set()
    controller._apply_runtime_session_started(2)
    controller._suspend_recognition_for_interaction()
    controller._apply_runtime_update("", True, "识别服务超时", 2)
    assert controller._recognition_event.is_set()


def test_begin_failed_before_session_id_still_retires_that_id(inline_ui):
    controller, _, _, _, event = inline_ui
    event(phase="idle", ready=False, original="", context_complete=False)
    controller._apply_runtime_status("[ASR] START gesture")
    controller._apply_runtime_session_started(12)
    assert 12 in controller._cancelled_asr_session_ids


def test_conversion_uses_sentence_snapshot_and_native_revision(inline_ui):
    controller, bridge, requests, _, event = inline_ui
    event(phase="listening", ready=True, has_composition=True, can_convert=True)
    controller.switchCurrentInputMode()
    assert bridge.messages[-1]["type"] == "convert"
    assert not controller._finish_utterance_event.is_set()
    event(phase="finishing", ready=True, revision=1, edit_requested=True)
    event("finish_audio")
    assert controller._finish_utterance_event.is_set()
    controller._apply_runtime_update("把三点改成四点", True, "", 1)
    assert not requests
    event(phase="editing", ready=True, revision=1, edit_requested=True)
    event("edit_requested", revision=1, instruction="把三点改成四点", original="明天三点开会")
    event("edit_requested", revision=1, instruction="把三点改成四点", original="明天三点开会")
    assert len(requests) == 1
    request = requests[0]
    assert request.target_text == "明天三点开会"
    assert request.raw_text == "把三点改成四点"
    controller._apply_text_processed(TextProcessingResult(request.request_id, 1, "edit", request.raw_text, "明天四点开会", .1, True))
    assert bridge.messages[-1]["type"] == "edit_result"
    assert bridge.messages[-1]["revision"] == 1
    assert bridge.messages[-1]["text"] == "明天四点开会"


def test_cancel_invalidates_late_model_and_old_connection_events(inline_ui):
    controller, bridge, requests, _, event = inline_ui
    event(phase="editing", ready=True, revision=1, edit_requested=True)
    event("edit_requested", revision=1, instruction="修改", original="明天三点开会")
    request = requests[0]
    controller.cancelCurrentUtterance()
    assert bridge.messages[-1]["type"] == "cancel"
    event(phase="dictated", ready=True, revision=2, edit_requested=False)
    event("settled", phase="dictated", revision=2, text="修改")
    count = len(bridge.messages)
    controller._apply_text_processed(TextProcessingResult(request.request_id, 1, "edit", "修改", "不能应用", .1, True))
    assert len(bridge.messages) == count
    component = controller._inline_input
    component._accept({"type": "state", "epoch": "old-connection", "ready": False, "phase": "error"})
    assert component.ready
    event("interrupted", reason="manual_input")
    assert controller._cancel_utterance_event.is_set()


def test_missing_input_method_stops_sentence_without_accessibility(inline_ui):
    controller, bridge, requests, _, event = inline_ui
    event(phase="idle", ready=False, original="", context_complete=False)
    count = len(bridge.messages)
    controller._apply_runtime_status("[ASR] START t=2.000s")
    assert controller._cancel_utterance_event.is_set()
    assert controller._inline_input._view["phase"] == "error"
    assert len(bridge.messages) == count


def test_cancel_live_dictation_discards_audio_and_late_final(inline_ui):
    controller, bridge, _, _, event = inline_ui
    controller._connected = True
    controller._recognition_event.set()
    controller._apply_runtime_update("正在说", False, "", 1)
    controller.cancelCurrentUtterance()
    event(phase="undone", ready=True, revision=1)
    event("finish_audio")
    event("settled", phase="undone", revision=1, text="")
    assert controller._cancel_utterance_event.is_set()
    assert not controller._finish_utterance_event.is_set()
    count = len(bridge.messages)
    controller._apply_runtime_update("取消后的结果", True, "", 1)
    assert len(bridge.messages) == count
    assert controller._recognition_event.is_set()


def test_publish_duplicate_final_does_not_suspend_recognition_again(inline_ui):
    from types import SimpleNamespace
    controller, _, _, _, event = inline_ui
    controller._connected = True
    controller._recognition_event.set()
    controller._apply_runtime_update("定稿", True, "", 1)
    event(phase="dictated", ready=True, raw="定稿", revision=0)
    event("settled", phase="dictated", text="定稿")
    controller._publish_update(SimpleNamespace(session_id=1, text="重复", is_final=True, error=""))
    assert controller._recognition_event.is_set()


def test_listening_feedback_waits_for_input_method_confirmation(inline_ui, monkeypatch):
    from test_input_source_activation import FakeSource
    controller, _, _, _, event = inline_ui
    inline = controller._inline_input
    monkeypatch.setattr(inline, '_foreground_identity', lambda: ('test.editor', 123))
    inline._source_switch_factory = FakeSource
    controller._apply_runtime_status("[ASR] START gesture t=2.000s")
    assert controller.statusTitle == "正在准备"
    inline._source_activation.event.emit({'event': 'selected', 'already_selected': True})
    event(phase='idle', ready=True, utterance_id='', application='test.editor')
    event(phase="listening", ready=True, raw="", revision=0)
    assert controller.statusTitle == "听写 · 可以说话"
    event(phase="finishing", ready=True, raw="", revision=0)
    assert controller.statusTitle == "正在定稿"
    assert not controller._cancel_utterance_event.is_set()


def test_native_interrupt_reports_reason_and_discards_late_asr(inline_ui):
    controller, bridge, _, _, event = inline_ui
    reason = "应用结束了当前输入组合"
    event("interrupted", reason=reason, lifecycle_event="commit_composition")
    assert controller._cancel_utterance_event.is_set()
    assert controller.statusTitle == "听写已停止"
    assert reason in controller.statusDetail
    assert "INPUT_METHOD_CANCEL" in controller.logText
    assert reason in controller.logText
    count = len(bridge.messages)
    controller._apply_runtime_update("迟到的语音不能输入", False, "", 1)
    assert len(bridge.messages) == count
    controller._interrupt_inline_audio()
    assert controller.logText.count("[EVENT INPUT_METHOD_CANCEL]") == 1


def test_context_reply_does_not_cancel_confirmed_listening(inline_ui):
    controller, _, _, _, event = inline_ui
    event(phase="listening", ready=True, raw="", revision=0,
          request_id="asr-context", original="明天三点开会", selection=[6, 0], context_complete=True)
    assert not controller._cancel_utterance_event.is_set()
    assert controller.statusTitle == "听写 · 可以说话"


def test_missing_input_method_reports_blocked_status(inline_ui):
    controller, _, _, _, event = inline_ui
    event(phase="idle", ready=False, original="", context_complete=False)
    controller._apply_runtime_status("[ASR] START gesture t=2.000s")
    assert controller.statusKind == "error"
    assert "文本框" in controller.statusDetail
    assert "INPUT_METHOD_CANCEL" in controller.logText


def test_permission_warning_updates_and_settings_remain_usable(inline_ui, monkeypatch):
    from PySide6.QtCore import QObject, QMetaObject, QPointF
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from proximic_ring.mac_permissions import MacPermissionError, PermissionState
    import proximic_ring.ui.mac_permissions_controller as permission_module
    controller, _, _, root, _ = inline_ui
    monitor = controller.inlineInput.permissions
    state = PermissionState(True, False)
    monitor._reader = lambda: state
    monitor.report_error(MacPermissionError(state))
    root.resize(940, 700)
    root.show()
    QTest.qWait(80)
    banner = root.findChild(QQuickItem, 'accessibilityPermissionWarning')
    card = root.findChild(QQuickItem, 'voiceInputCard')
    assert banner.isVisible() and banner.height() >= 60
    assert banner.mapToItem(card, QPointF()).y() + banner.height() < card.height()
    assert root.grabWindow().save('/private/tmp/proximic-permission-warning.png')
    QMetaObject.invokeMethod(root.findChild(QObject, 'permissionWarningSettingsButton'), 'click')
    QTest.qWait(80)
    dialog = root.findChild(QObject, 'inputMethodSetupDialog')
    assert dialog.property('visible')
    assert '按键控制' in root.findChild(QObject, 'accessibilityPermissionStatus').property('text')
    scroll = root.findChild(QQuickItem, 'inputMethodSetupScroll')
    assert scroll.height() <= root.height() - 200
    pos = scroll.mapToScene(QPointF())
    assert pos.y() >= 0 and pos.y() + scroll.height() < root.height()
    calls = []
    monkeypatch.setattr(permission_module, 'request_post_event_access', lambda: calls.append('request'))
    monkeypatch.setattr(permission_module.QDesktopServices, 'openUrl', lambda url: calls.append(url.toString()) or True)
    assert not calls
    QMetaObject.invokeMethod(root.findChild(QObject, 'openAccessibilityPermissionsButton'), 'click')
    QTest.qWait(30)
    assert calls == ['request', 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility']
    assert root.grabWindow().save('/private/tmp/proximic-permission-settings.png')
    # The permission reader runs on a worker. Wait for its prior generation
    # before requesting a new value; a fixed sleep races refresh coalescing.
    for _ in range(200):
        if not monitor.checking:
            break
        QTest.qWait(5)
    state = PermissionState(True, True)
    monitor.refresh()
    for _ in range(200):
        if not monitor.checking:
            break
        QTest.qWait(5)
    assert not monitor.checking
    assert not banner.isVisible()
    assert '已生效' in root.findChild(QObject, 'accessibilityPermissionStatus').property('text')
    QMetaObject.invokeMethod(dialog, 'close')


def test_gesture_asr_starts_only_after_native_begin_ack(inline_ui, monkeypatch):
    from test_input_source_activation import FakeSource
    from proximic_ring.asr.controller import ProximityASRController
    from test_asr_controller import StreamingRecorder
    import numpy as np
    controller, bridge, _, _, event = inline_ui
    inline = controller._inline_input
    controller._recognition_event.set()
    monkeypatch.setattr(inline, '_foreground_identity', lambda: ('test.editor', 123))
    inline._source_switch_factory = FakeSource
    controller._gesture_input_preparation_enabled = True
    sink = StreamingRecorder()
    gate = ProximityASRController(sink, start_on_gesture=True, on_state=controller._apply_runtime_status)
    prepare = lambda callback: controller._prepare_inline_gesture_start(callback, controller._disconnect_event)
    assert gate.request_gesture_toggle(prepare) == 'start'
    gate.process(np.ones(320, dtype=np.float32), [])
    assert not sink.started and controller.statusTitle == '正在准备'
    inline._source_activation.event.emit({'event': 'selected', 'already_selected': False})
    gate.process(np.ones(320, dtype=np.float32), [])
    assert not sink.started
    event(phase='idle', ready=True, utterance_id='', application='test.editor')
    gate.process(np.ones(320, dtype=np.float32), [])
    assert not sink.started
    event(phase='listening', ready=True, raw='', revision=0)
    utterance = inline._utterance_id
    begins = sum(m['type'] == 'begin' for m in bridge.messages)
    gate.process(np.ones(320, dtype=np.float32), [])
    assert len(sink.started) == 1 and inline._utterance_id == utterance
    assert sum(m['type'] == 'begin' for m in bridge.messages) == begins
    assert controller.statusTitle == '听写 · 可以说话'


@pytest.mark.parametrize('reason', ['cancel', 'timeout', 'old_connection'])
def test_preparing_gesture_rejects_cancelled_or_stale_start(inline_ui, monkeypatch, reason):
    from test_input_source_activation import FakeSource
    import threading
    controller, _, _, _, _ = inline_ui
    inline = controller._inline_input
    controller._recognition_event.set()
    monkeypatch.setattr(inline, '_foreground_identity', lambda: ('test.editor', 123))
    inline._source_switch_factory = FakeSource
    answers = []
    connection = threading.Event() if reason == 'old_connection' else controller._disconnect_event
    controller._prepare_inline_gesture_start(answers.append, connection)
    if reason == 'cancel':
        controller._prepare_inline_gesture_start(None, connection)
    elif reason == 'timeout':
        inline._reply_timed_out()
    assert answers == [False]
    controller._inline_gesture_start_ready()
    assert answers == [False] and not controller._inline_audio_prepared


def test_gesture_audio_start_cannot_restart_a_retired_native_sentence(inline_ui):
    controller, bridge, _, _, _ = inline_ui
    controller._gesture_input_preparation_enabled = True
    controller._inline_audio_prepared = "obsolete-utterance"
    count = len(bridge.messages)
    controller._apply_runtime_status("[ASR] START gesture t=2.000s")
    assert controller._cancel_utterance_event.is_set()
    assert len(bridge.messages) == count
    assert "输入法连接已变化" in controller.statusDetail
