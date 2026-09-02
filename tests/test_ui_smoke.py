import json
import os
from pathlib import Path
import re
import sys

import pytest


def test_edit_preview_html_highlights_changes_and_escapes_user_text():
    pytest.importorskip("PySide6")

    from proximic_ring.ui.controller import _edit_preview_html

    replaced = _edit_preview_html("今天下雨。", "今天下大雨。")
    assert 'style="color:#FF646F;">大</span>' in replaced

    deleted = _edit_preview_html("请删除这个词。", "请删除词。")
    assert "已删除：这个" in deleted
    assert "#FF646F" in deleted

    escaped = _edit_preview_html("", "<b>不是标签</b> & 安全")
    assert "&lt;b&gt;不是标签&lt;/b&gt; &amp; 安全" in escaped
    assert "<b>不是标签</b>" not in escaped

    cleared = _edit_preview_html("原文", "")
    assert "修改后为空" in cleared
    assert "#FF646F" in cleared


def test_compute_device_discovery_lists_cuda(monkeypatch):
    pytest.importorskip("PySide6")
    import torch

    from proximic_ring.ui.controller import AppController

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "device_count", lambda: 2)
    monkeypatch.setattr(
        torch.cuda,
        "get_device_name",
        lambda index: ["Example GPU A", "Example GPU B"][index],
    )

    devices, message = AppController._detect_compute_devices()

    assert [item["value"] for item in devices] == ["cpu", "cuda:0", "cuda:1"]
    assert devices[1]["label"] == "GPU 1 · Example GPU A"
    assert "2 张" in message

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    devices, message = AppController._detect_compute_devices("Example GPU A")
    assert devices == [{"label": "CPU（兼容性最佳）", "value": "cpu"}]
    assert ("macOS" if sys.platform == "darwin" else "CPU 版 PyTorch") in message

    monkeypatch.setattr(sys, "platform", "darwin")
    devices, message = AppController._detect_compute_devices()
    assert devices == [{"label": "CPU（兼容性最佳）", "value": "cpu"}]
    assert "macOS" in message
    assert AppController._detect_nvidia_gpu_name() == ""


def test_macos_desktop_output_migrates_old_forced_off_setting(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QSettings

    from proximic_ring.ui.controller import AppController

    _app = QCoreApplication.instance() or QCoreApplication(["mac-output-migration"])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    settings = QSettings("ProxiMic", "ProxiMic Voice")
    settings.setValue("input/desktopOutput", False)
    settings.remove("input/macosDesktopOutputMigrated")
    settings.sync()
    monkeypatch.setattr(sys, "platform", "darwin")

    controller = AppController()
    assert controller.desktopOutputEnabled is True
    assert controller._settings.value("input/macosDesktopOutputMigrated") is True
    controller.desktopOutputEnabled = False
    controller._text_processing_worker.close(wait=True)

    restarted = AppController()
    assert restarted.desktopOutputEnabled is False
    restarted._text_processing_worker.close(wait=True)


def test_macos_edit_does_not_report_success_without_verified_replacement(
    tmp_path, monkeypatch
):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QSettings

    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.ui.controller import AppController, _EditReview

    _app = QCoreApplication.instance() or QCoreApplication(["mac-edit-verification"])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    controller = AppController()
    controller._text_processing_worker.close(wait=True)
    controller._accessibility_timer.stop()
    target = DesktopTargetRef(0, 0, "测试编辑器", process_id=4321)

    class TargetThatIgnoresReplacement:
        def __init__(self):
            self.replace_calls = 0

        def replace(self, snapshot, text):
            self.replace_calls += 1

        def capture_text(self, captured_target):
            return DesktopTextSnapshot(captured_target, "仍然是原文")

        def release_selection(self, captured_target):
            return None

    desktop_target = TargetThatIgnoresReplacement()
    controller._desktop_target = desktop_target
    controller._edit_review = _EditReview(
        request_id=999,
        session_id=1,
        instruction="改得正式",
        proposed_text="正式的新文本",
        snapshot=DesktopTextSnapshot(target, "仍然是原文"),
    )
    controller._set_interaction_state("review")

    controller.confirmEdit()

    assert desktop_target.replace_calls == 2
    assert controller.reviewPending is False
    assert controller.interactionState == "error"
    assert "修改未应用" in controller.transcriptText
    assert "修改 · 应用失败" in controller.sessionHistoryText
    assert "修改 · 已应用" not in controller.sessionHistoryText


def test_auto_routing_dispatches_to_dictation_and_edit_with_timing_log(
    tmp_path,
):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QSettings

    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot
    from proximic_ring.text_processing import InputModeRoutingResult
    from proximic_ring.ui.controller import AppController

    _app = QCoreApplication.instance() or QCoreApplication(["routing-test"])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    controller = AppController()
    controller._llm_enabled = True
    controller._input_routing_mode = "manual"
    controller._text_processing_worker.close(wait=True)

    routed = []
    submitted = []

    class FakeWorker:
        def submit_routing(self, request):
            routed.append(request)

        def submit(self, request):
            submitted.append(request)

        def close(self, *, wait=False):
            return None

    target = DesktopTargetRef(1, 2, "测试编辑器")

    class FakeDesktopTarget:
        def __init__(self):
            self.injected = []

        def capture_reference(self):
            return target

        def capture_text(self, captured_target):
            assert captured_target == target
            return DesktopTextSnapshot(target, "已有文本。")

        def inject(self, captured_target, text):
            assert captured_target == target
            self.injected.append(text)

        def release_selection(self, _target):
            return None

    desktop_target = FakeDesktopTarget()
    controller._text_processing_worker = FakeWorker()
    controller._desktop_target = desktop_target
    controller._desktop_output = True
    controller.llmEnabled = False
    controller.inputRoutingMode = "auto"

    controller._apply_runtime_update("这是一段要输入的话。", True, "", 701)
    assert len(routed) == 1
    assert routed[0].settings.enabled is True
    assert controller.transcriptMode == ""
    assert "自动路由判断开始" in controller.logText
    controller._apply_input_mode_routed(
        InputModeRoutingResult(
            request_id=routed[0].request_id,
            session_id=701,
            raw_text=routed[0].raw_text,
            mode="dictation",
            latency_s=0.234,
            model_output="dictation",
        )
    )
    assert controller.transcriptMode == "dictation"
    assert desktop_target.injected == ["这是一段要输入的话。"]
    assert "自动路由判断完成：听写（耗时 0.234s）" in controller.logText

    controller._apply_runtime_update("把上一句改正式一点", True, "", 702)
    controller._apply_input_mode_routed(
        InputModeRoutingResult(
            request_id=routed[1].request_id,
            session_id=702,
            raw_text=routed[1].raw_text,
            mode="edit",
            latency_s=0.125,
            model_output="edit",
        )
    )
    assert controller.transcriptMode == "edit"
    assert len(submitted) == 1
    assert submitted[0].mode == "edit"
    assert submitted[0].target_text == "已有文本。"
    assert "自动路由判断完成：编辑指令（耗时 0.125s）" in controller.logText
    assert re.search(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] 自动路由判断完成",
        controller.logText,
    )
    controller._cancel_pending_text_processing()


def test_device_scan_keeps_one_snapshot_until_manual_rescan(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QSettings

    from proximic_ring.ui.controller import AppController

    _app = QCoreApplication.instance() or QCoreApplication(["device-scan-test"])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    controller = AppController()
    scans = []
    monkeypatch.setattr(controller, "_scan_devices_once", lambda: scans.append(True))

    controller.scanDevices()
    assert scans == [True]

    controller._scan_busy = True
    controller._apply_scan_finished(
        [{"name": "Ringo Test", "identifier": "RING-ID", "rssi": -40}],
        "",
    )
    assert scans == [True]
    assert controller.availableDevices == [
        {"name": "Ringo Test", "identifier": "RING-ID", "rssi": -40}
    ]
    assert controller.scanMessage == "已发现 1 个设备，显示 1 个匹配“Ringo”的设备"

    controller.scanDevices()
    assert scans == [True, True]
    assert controller.availableDevices == []


def test_qml_customer_window_loads(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import (
        QObject,
        QMetaObject,
        QPoint,
        QPointF,
        QSettings,
        Qt,
        QUrl,
    )
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from proximic_ring.ui.controller import AppController

    app = QApplication.instance() or QApplication(["ui-test", "-platform", "offscreen"])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    controller = AppController()
    controller._llm_enabled = True
    controller._input_routing_mode = "manual"
    controller._llm_provider = "local"
    controller._llm_model = "qwen3-4b-instruct-2507-local"
    controller._llm_base_url = "http://127.0.0.1:11435/v1"
    # Simulate a selector persisted by an earlier application run.  It must not
    # make the freshly opened UI claim that this is a reconnect operation.
    controller._selector = "SAVED-RING-ID"
    controller._device_name = "Previously used Ring"
    assert controller.hasSelectedDevice is True
    assert controller.canReconnect is False
    assert controller.computeDevices[0] == {
        "label": "CPU（兼容性最佳）",
        "value": "cpu",
    }
    assert controller.asrDevice in {
        item["value"] for item in controller.computeDevices
    }
    selected_handle = object()
    controller._apply_scan_finished(
        [
            {
                "name": "Ringo Test",
                "identifier": "RING-ID",
                "rssi": -40,
                "_device": selected_handle,
            },
            {"name": "Keyboard", "identifier": "KEYBOARD-ID", "rssi": -55},
        ],
        "",
    )
    assert controller.deviceSearch == "Ringo"
    assert [item["identifier"] for item in controller.availableDevices] == ["RING-ID"]
    controller.deviceSearch = ""
    assert [item["identifier"] for item in controller.availableDevices] == [
        "RING-ID",
        "KEYBOARD-ID",
    ]
    controller._apply_scan_finished(
        [
            {"name": "Keyboard", "identifier": "KEYBOARD-ID", "rssi": -42},
            {"name": "Ringo Test", "identifier": "RING-ID", "rssi": -38},
            {"name": "Mouse", "identifier": "MOUSE-ID", "rssi": -60},
        ],
        "",
    )
    assert [item["identifier"] for item in controller.availableDevices] == [
        "RING-ID",
        "KEYBOARD-ID",
        "MOUSE-ID",
    ]
    controller.deviceSearch = "Ringo"
    if sys.platform == "darwin":
        controller.desktopOutputEnabled = True
        assert controller.desktopOutputEnabled is True
        controller.desktopOutputEnabled = False
        assert controller.desktopOutputEnabled is False
        controller.desktopOutputEnabled = True
        assert controller.desktopOutputEnabled is True
        assert controller.pushToTalkEnabled is False
        controller.pushToTalkEnabled = True
        assert controller.pushToTalkEnabled is False
    elif os.name != "nt":
        assert controller.desktopOutputEnabled is False
        assert controller.pushToTalkEnabled is False
        controller.desktopOutputEnabled = True
        controller.pushToTalkEnabled = True
        assert controller.desktopOutputEnabled is False
        assert controller.pushToTalkEnabled is False
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("appController", controller)
    qml = (
        Path(__file__).parents[1]
        / "src"
        / "proximic_ring"
        / "ui"
        / "qml"
        / "Main.qml"
    )
    engine.load(QUrl.fromLocalFile(str(qml)))
    app.processEvents()

    assert len(engine.rootObjects()) == 1
    window = engine.rootObjects()[0]
    assert window.title() == "ProxiMic Voice"
    controller.devicePickerRequested.emit()
    app.processEvents()
    picker = window.findChild(QObject, "devicePicker")
    device_list = window.findChild(QObject, "deviceList")
    search_field = window.findChild(QObject, "deviceSearchField")
    audio_encoding_combo = window.findChild(QObject, "audioEncodingCombo")
    device_combo = window.findChild(QObject, "asrDeviceCombo")
    asr_api_key_field = window.findChild(QObject, "asrApiKeyField")
    asr_hotwords_field = window.findChild(QObject, "asrHotwordsField")
    gpu_install_button = window.findChild(QObject, "gpuInstallButton")
    dictation_mode_button = window.findChild(QObject, "dictationModeButton")
    edit_mode_button = window.findChild(QObject, "editModeButton")
    input_routing_mode_combo = window.findChild(QObject, "inputRoutingModeCombo")
    auto_mode_badge = window.findChild(QObject, "autoModeBadge")
    dictation_llm_button = window.findChild(QQuickItem, "dictationLlmButton")
    voice_input_card = window.findChild(QQuickItem, "voiceInputCard")
    llm_local_server_field = window.findChild(QObject, "llmLocalServerField")
    llm_local_model_field = window.findChild(QObject, "llmLocalModelField")
    llm_provider_combo = window.findChild(QObject, "llmProviderCombo")
    llm_base_url_field = window.findChild(QObject, "llmBaseUrlField")
    llm_model_combo = window.findChild(QObject, "llmModelCombo")
    llm_model_field = window.findChild(QObject, "llmModelField")
    llm_api_key_field = window.findChild(QObject, "llmApiKeyField")
    voice_history_list = window.findChild(QObject, "voiceHistoryList")
    confirm_edit_button = window.findChild(QObject, "confirmEditButton")
    cancel_edit_button = window.findChild(QObject, "cancelEditButton")
    edit_preview_text = window.findChild(QObject, "editPreviewText")
    feedback_reason_overlay = window.findChild(QObject, "feedbackReasonOverlay")
    log_area = window.findChild(QObject, "logArea")
    primary_connection_button = window.findChild(QObject, "primaryConnectionButton")
    secondary_connection_button = window.findChild(QObject, "secondaryConnectionButton")
    assert picker is not None and picker.property("visible") is True
    assert device_list is not None and device_list.property("count") == 1
    assert search_field is not None and search_field.property("text") == "Ringo"
    assert audio_encoding_combo is not None
    assert controller.audioEncoding == "opus"
    controller.audioEncoding = "pcm"
    assert controller.audioEncoding == "pcm"
    controller.audioEncoding = "adpcm"
    assert controller.audioEncoding == "adpcm"
    assert device_combo is not None
    assert asr_api_key_field is not None
    assert device_combo.property("count") == len(controller.computeDevices)
    assert asr_hotwords_field is not None
    assert gpu_install_button is not None
    assert gpu_install_button.property("visible") is controller.gpuInstallerAvailable
    assert dictation_mode_button is not None
    assert edit_mode_button is not None
    assert dictation_llm_button is not None
    assert voice_input_card is not None
    assert feedback_reason_overlay is not None
    assert controller.feedbackReasonVisible is False
    assert controller.feedbackReasonAvailable is False
    assert controller.llmEnabled is True
    QMetaObject.invokeMethod(picker, "close")
    QTest.qWait(300)
    llm_button_center = dictation_llm_button.mapToScene(
        QPointF(
            dictation_llm_button.property("width") / 2,
            dictation_llm_button.property("height") / 2,
        )
    )
    QTest.mouseClick(
        window,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        QPoint(round(llm_button_center.x()), round(llm_button_center.y())),
    )
    app.processEvents()
    assert controller.llmEnabled is False
    QTest.mouseClick(
        window,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        QPoint(round(llm_button_center.x()), round(llm_button_center.y())),
    )
    app.processEvents()
    assert controller.llmEnabled is True
    controller._set_status(
        "设备已断开",
        "Ringo audio stream failed: [WinError -2147023673] 操作已被用户取消。"
        "设备已自动断开。请点击“重新连接设备”重试。" * 3,
        "error",
    )
    app.processEvents()
    primary_connection_item = window.findChild(
        QQuickItem, "primaryConnectionButton"
    )
    card_top = voice_input_card.mapToScene(QPointF(0, 0)).y()
    button_top = primary_connection_item.mapToScene(QPointF(0, 0)).y()
    assert button_top + primary_connection_item.property("height") <= (
        card_top + voice_input_card.property("height")
    )
    assert llm_local_server_field is not None
    assert llm_provider_combo is not None
    assert llm_base_url_field is not None
    assert llm_model_combo is not None
    assert llm_model_field is not None
    assert llm_api_key_field is not None
    assert voice_history_list is not None
    controller._voice_history_entries = [
        {
            "displayTime": "16:30:00",
            "durationLabel": "1.2 秒",
            "backend": "SenseVoice",
            "text": "测试语音记录",
            "recognized": True,
            "audioPath": str(tmp_path / "voice.wav"),
        }
    ]
    controller.voiceHistoryChanged.emit()
    app.processEvents()
    assert voice_history_list.property("count") == 1
    voice_history_list.setProperty("currentIndex", 0)
    QMetaObject.invokeMethod(voice_history_list, "forceLayout")
    QTest.qWait(50)
    current_voice_item = voice_history_list.property("currentItem")
    assert current_voice_item is not None
    voice_play_button = current_voice_item.findChild(
        QQuickItem, "voiceHistoryPlayButton"
    )
    assert voice_play_button is not None
    assert voice_play_button.property("text") == "播放录音"
    assert voice_play_button.property("width") >= 88
    assert voice_play_button.property("contentItem").property("text") == "播放录音"
    assert confirm_edit_button is not None
    assert cancel_edit_button is not None
    assert edit_preview_text is not None
    assert log_area is not None
    assert log_area.property("font").pixelSize() == 14
    controller._apply_runtime_status("最新日志")
    app.processEvents()
    assert re.search(
        r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] 最新日志",
        controller.logText,
    )
    assert log_area.property("text") == controller.logText
    assert log_area.property("cursorPosition") == len(log_area.property("text"))
    assert llm_local_model_field is not None
    assert controller.inputMode == "dictation"
    assert input_routing_mode_combo is not None
    assert auto_mode_badge is not None
    assert controller.inputRoutingMode == "manual"
    controller.inputRoutingMode = "auto"
    app.processEvents()
    assert input_routing_mode_combo.property("currentIndex") == 0
    assert dictation_mode_button.property("enabled") is False
    assert edit_mode_button.property("enabled") is False
    assert dictation_mode_button.property("visible") is False
    assert edit_mode_button.property("visible") is False
    assert auto_mode_badge.property("visible") is False
    controller._transcript_mode = "edit"
    controller.transcriptChanged.emit()
    app.processEvents()
    assert auto_mode_badge.property("visible") is True
    controller._transcript_mode = ""
    controller.transcriptChanged.emit()
    controller.inputRoutingMode = "manual"
    app.processEvents()
    # Persisted callers using the former name migrate to the edit lane.
    controller.inputMode = "instruction"
    app.processEvents()
    assert controller.inputMode == "edit"
    assert edit_mode_button.property("checked") is True
    assert dictation_llm_button.property("visible") is True
    controller.llmApiKey = "ark-ui-key"
    controller.asrApiKey = "speech-ui-key"
    app.processEvents()
    assert llm_api_key_field.property("text") == "ark-ui-key"
    assert asr_api_key_field.property("text") == "speech-ui-key"
    assert controller._settings.value("llm/apiKey") == "ark-ui-key"
    assert controller._settings.value("asr/volcengineApiKey") == "speech-ui-key"
    controller.llmProvider = "volcengine"
    app.processEvents()
    assert llm_api_key_field.property("visible") is True
    voice_settings = controller._voice_llm_settings()
    assert voice_settings.provider == "volcengine"
    assert voice_settings.enabled is True
    assert voice_settings.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert voice_settings.model == "doubao-seed-2-0-lite-260215"
    assert voice_settings.api_key == "ark-ui-key"
    assert voice_settings.api_key_env == "ARK_API_KEY"
    assert voice_settings.local_auto_start is False
    controller.llmModel = "deepseek-v4-flash-260425"
    assert controller._voice_llm_settings().model == "deepseek-v4-flash-260425"
    controller.llmProvider = "local"
    local_settings = controller._voice_llm_settings()
    assert local_settings.provider == "local"
    assert local_settings.api_key == ""
    assert local_settings.api_key_env == ""
    assert local_settings.local_auto_start is True
    controller.llmProvider = "volcengine"
    assert controller._voice_llm_settings().model == "deepseek-v4-flash-260425"
    controller.llmProvider = "local"
    assert primary_connection_button.property("text") == "选择并连接设备"
    assert secondary_connection_button.property("visible") is False

    started = []
    controller._scan_busy = True
    controller._start_selected_device = lambda: started.append(controller.selector)
    controller.connectToDevice("RING-ID", "Ringo Test")
    assert controller._selected_device is selected_handle
    runtime_settings = controller._runtime_settings()
    assert runtime_settings.encoding == "adpcm"
    assert runtime_settings.desktop_output is False
    assert runtime_settings.ring_device is None
    assert controller.canReconnect is True
    assert started == []
    controller._apply_scan_finished([], "")
    assert started == ["RING-ID"]
    app.processEvents()
    assert primary_connection_button.property("text") == "重新连接设备"
    assert secondary_connection_button.property("visible") is True

    controller.asrBackend = "volcengine"
    app.processEvents()
    assert asr_api_key_field.property("visible") is True
    runtime_settings = controller._runtime_settings()
    assert runtime_settings.asr_api_key == "speech-ui-key"
    assert runtime_settings.to_namespace().asr_option == [
        "volcengine.api_key=speech-ui-key"
    ]

    controller.asrModel = "iic/SenseVoiceSmall"
    controller.asrBackend = "funasr_nano"
    assert controller.asrModel == ""

    class SettingsRecorder:
        def __init__(self):
            self.values = {}

        def setValue(self, key, value):
            self.values[key] = value

    settings_recorder = SettingsRecorder()
    controller._settings = settings_recorder
    controller.asrHotwords = " ProxiMic，豆包\n瑞幸,豆包；张三 "
    app.processEvents()
    assert controller.asrHotwords == "ProxiMic\n豆包\n瑞幸\n张三"
    assert asr_hotwords_field.property("text") == controller.asrHotwords
    assert asr_hotwords_field.property("visible") is True
    runtime_settings = controller._runtime_settings()
    assert runtime_settings.funasr_nano_hotwords == controller.asrHotwords
    assert runtime_settings.to_namespace().asr_option == [
        "funasr_nano.hotwords=ProxiMic,豆包,瑞幸,张三"
    ]
    assert settings_recorder.values["asr/funasrNanoHotwords"] == (
        controller.asrHotwords
    )

    controller._runtime_active = True
    controller._busy = True
    controller._apply_runtime_status("正在连接设备 Ringo Test…")
    assert controller.statusTitle == "正在连接设备"
    controller._apply_runtime_status("正在验证 Ring 麦克风音频…")
    assert controller.statusTitle == "正在验证设备音频"
    controller._apply_runtime_connected()
    assert controller.connected is True
    assert controller.busy is True
    controller._apply_runtime_status("正在加载 ProxiMic 检测模型…")
    assert controller.statusTitle == "正在加载检测模型"
    controller._apply_runtime_status("正在加载语音模型 funasr_nano…")
    assert controller.statusTitle == "正在加载语音模型"
    controller._apply_runtime_status(
        "正在检查并下载 ASR 模型参数：FunASR-Nano（已有磁盘缓存将直接复用）…"
    )
    assert controller.statusTitle == "正在下载模型参数"
    controller._apply_runtime_status("ASR 模型参数已载入内存：FunASR-Nano")
    assert controller.statusTitle == "模型参数已载入"
    controller._apply_runtime_disconnected()
    assert controller.connected is False
    assert controller.busy is True
    assert controller.statusTitle == "设备已断开"
    assert "后台" in controller.statusDetail
    controller._apply_runtime_connected()
    controller._apply_runtime_status("模型加载完成，正在确认实时音频…")
    assert controller.statusTitle == "正在确认实时音频"
    controller._apply_runtime_status("STAGE2 sample=100 score=+0.900 ACTIVATE")
    assert controller.statusTitle == "正在确认实时音频"
    assert "STAGE2 sample=100 score=+0.900 ACTIVATE" in controller.logText
    controller._apply_runtime_started()
    assert controller.connected is True
    assert controller.statusTitle == "准备就绪"
    assert controller.recognitionEnabled is False
    controller.startRecognition()
    assert controller.recognitionEnabled is True
    controller.pauseRecognition()
    assert controller.connected is True
    assert controller.recognitionEnabled is False

    from proximic_ring.text_processing import MAX_EDIT_TARGET_CHARS, TextProcessingResult
    from proximic_ring.desktop_target import DesktopTargetRef, DesktopTextSnapshot

    submitted = []
    target_ref = DesktopTargetRef(100, 101, "测试编辑器")

    class FakeDesktopTarget:
        def __init__(self):
            self.current_text = "原始外部文本。"
            self.injected = []
            self.replaced = []
            self.released = []

        def capture_reference(self):
            return target_ref

        def capture_text(self, target):
            assert target == target_ref
            return DesktopTextSnapshot(target, self.current_text)

        def inject(self, target, text):
            assert target == target_ref
            self.injected.append(text)

        def replace(self, snapshot, text):
            assert snapshot.text == self.current_text
            self.current_text = text
            self.replaced.append(text)

        def release_selection(self, target):
            self.released.append(target)

    desktop_target = FakeDesktopTarget()

    class FakeTextWorker:
        def submit(self, request):
            submitted.append(request)

        def close(self, *, wait=False):
            return None

    controller._text_processing_worker.close(wait=True)
    controller._text_processing_worker = FakeTextWorker()
    controller._desktop_target = desktop_target
    controller._desktop_output = True
    controller.inputMode = "dictation"
    controller._apply_runtime_update("实时片段", False, "", 42)
    controller.inputMode = "edit"
    controller._apply_runtime_update("原始文本", True, "", 42)
    assert len(submitted) == 1
    assert submitted[0].mode == "dictation"
    assert submitted[0].settings.provider == "local"
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=submitted[0].request_id,
            session_id=42,
            mode=submitted[0].mode,
            raw_text="原始文本",
            final_text="整理后的文本。",
            latency_s=0.1,
            used_llm=True,
        )
    )
    assert desktop_target.injected == ["整理后的文本。"]
    assert "输入 · 已注入" in controller.sessionHistoryText
    assert controller.transcriptFinal is True

    controller.inputMode = "dictation"
    controller.llmEnabled = False
    app.processEvents()
    assert "关" in dictation_llm_button.property("text")
    submitted_before_direct_input = len(submitted)
    controller._apply_runtime_update("Nano 已整理的文本。", True, "", 100)
    assert len(submitted) == submitted_before_direct_input
    assert desktop_target.injected[-1] == "Nano 已整理的文本。"
    assert "直接采用 ASR 最终结果" in controller.logText

    controller.inputMode = "edit"
    controller._apply_runtime_update("改得更正式一点", True, "", 43)
    assert len(submitted) == 2
    edit_request = submitted[1]
    assert edit_request.mode == "edit"
    assert edit_request.settings.enabled is True
    assert edit_request.target_text == "原始外部文本。"
    assert "修改目标已读取：测试编辑器（7 个字符）" in controller.logText
    assert "目标文本开始" not in controller.logText
    assert "隐藏字符检查" not in controller.logText
    assert controller.inputMode == "edit"
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=edit_request.request_id,
            session_id=43,
            mode=edit_request.mode,
            raw_text=edit_request.raw_text,
            final_text="第一次修改预览。",
            latency_s=0.2,
            used_llm=True,
            target_text=edit_request.target_text,
            model_output=(
                '{"original_text":"原始外部文本。",'
                '"modified_text":"第一次修改预览。"}'
            ),
        )
    )
    assert "大模型修改原始返回" in controller.logText
    assert '"original_text": "原始外部文本。"' in controller.logText
    assert controller.reviewPending is True
    assert "第一次修改预览" in controller.editPreviewHtml
    assert "#FF646F" in controller.editPreviewHtml
    assert desktop_target.replaced == []
    app.processEvents()
    assert confirm_edit_button.property("visible") is True
    assert edit_preview_text.property("visible") is True
    assert edit_preview_text.property("text") == controller.editPreviewHtml
    controller._recognition_enabled = True
    controller._apply_push_to_talk(True)
    controller._apply_push_to_talk(False)
    assert controller.reviewPending is True
    controller.cancelEdit()
    assert controller.reviewPending is False
    assert controller.editPreviewHtml == ""
    assert controller.interactionState == "cancelled"
    QTest.qWait(220)
    assert confirm_edit_button.property("visible") is False
    assert controller.feedbackReasonVisible is True
    assert controller.feedbackReasonAvailable is True
    assert feedback_reason_overlay.property("visible") is True
    assert controller.feedbackReasonPrompt == "刚才为什么取消？"
    controller._apply_voice_action("reason_asr_error")
    app.processEvents()
    assert controller.feedbackReasonVisible is False
    assert feedback_reason_overlay.property("visible") is False
    assert "已标记本次取消原因：语音识别错误" in controller.logText

    # Replacing an unanswered cancellation prompt must have a rendered gap.
    controller._offer_feedback_reason(900, "cancel")
    QTest.qWait(220)
    assert controller.feedbackReasonVisible is True
    controller._offer_feedback_reason(901, "cancel")
    assert controller.feedbackReasonAvailable is True
    assert controller.feedbackReasonVisible is False
    assert feedback_reason_overlay.property("visible") is False
    QTest.qWait(220)
    assert controller.feedbackReasonVisible is True
    assert controller._pending_feedback_reason.request_id == 901
    controller._clear_feedback_reason()

    controller._apply_runtime_update("改得简洁正式", True, "", 44)
    next_request = submitted[2]
    assert next_request.target_text == "原始外部文本。"
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=next_request.request_id,
            session_id=44,
            mode=next_request.mode,
            raw_text=next_request.raw_text,
            final_text="正式的新文本。",
            latency_s=0.2,
            used_llm=True,
            target_text=next_request.target_text,
        )
    )
    assert controller.reviewPending is True
    controller.confirmEdit()
    assert desktop_target.replaced == ["正式的新文本。"]
    assert controller.reviewPending is False
    assert controller.inputMode == "edit"
    assert "修改 · 已应用" in controller.sessionHistoryText

    controller.inputMode = "edit"
    controller._apply_runtime_update("删除最后一句", True, "", 45)
    cancel_request = submitted[3]
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=cancel_request.request_id,
            session_id=45,
            mode=cancel_request.mode,
            raw_text=cancel_request.raw_text,
            final_text="不应应用的预览。",
            latency_s=0.2,
            used_llm=True,
            target_text=cancel_request.target_text,
        )
    )
    controller.cancelEdit()
    QTest.qWait(220)
    assert controller.feedbackReasonVisible is True
    assert controller.feedbackReasonPrompt == "刚才为什么取消？"
    assert desktop_target.current_text == "正式的新文本。"
    assert desktop_target.released[-1] == target_ref
    assert "修改 · 已取消" in controller.sessionHistoryText

    controller._apply_runtime_update("把这段话删除", True, "", 46)
    clear_request = submitted[4]
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=clear_request.request_id,
            session_id=46,
            mode=clear_request.mode,
            raw_text=clear_request.raw_text,
            final_text="",
            latency_s=0.2,
            used_llm=True,
            target_text=clear_request.target_text,
            model_output=(
                '{"original_text":"正式的新文本。",'
                '"modified_text":""}'
            ),
        )
    )
    assert controller.reviewPending is True
    assert "确认后将清空目标文本框" in controller.transcriptText
    assert desktop_target.current_text == "正式的新文本。"
    controller.confirmEdit()
    assert controller.feedbackReasonVisible is False
    assert desktop_target.current_text == ""
    assert desktop_target.replaced[-1] == ""
    assert controller.reviewPending is False

    # A validated full-text response can explicitly request a clear without
    # carrying original_text, but it still requires confirmation.
    desktop_target.current_text = "不得意外清空。"
    controller._apply_runtime_update("润色一下", True, "", 47)
    unexplained_empty_request = submitted[5]
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=unexplained_empty_request.request_id,
            session_id=47,
            mode=unexplained_empty_request.mode,
            raw_text=unexplained_empty_request.raw_text,
            final_text="",
            latency_s=0.2,
            used_llm=True,
            target_text=unexplained_empty_request.target_text,
            model_output='{"modified_text":""}',
        )
    )
    assert controller.reviewPending is True
    assert desktop_target.current_text == "不得意外清空。"
    controller.cancelEdit()
    assert controller.reviewPending is False
    assert desktop_target.current_text == "不得意外清空。"

    # An LLM edit failure remains actionable instead of briefly flashing and
    # abandoning the Episode. Retrying automatically records the known cause
    # and creates the next Attempt in the same Episode.
    desktop_target.current_text = "大模型失败时保留的原文。"
    controller._apply_runtime_update("把原文改得更清楚", True, "", 60)
    failed_request = submitted[6]
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=failed_request.request_id,
            session_id=60,
            mode=failed_request.mode,
            raw_text=failed_request.raw_text,
            final_text=failed_request.target_text,
            latency_s=0.3,
            used_llm=True,
            target_text=failed_request.target_text,
            error="两种编辑协议均失败（返回格式无效）",
        )
    )
    app.processEvents()
    assert controller.reviewPending is True
    assert controller.reviewFailed is True
    assert controller.reviewCanConfirm is False
    assert controller.interactionState == "review_error"
    assert "两种编辑协议均失败" in controller.transcriptText
    assert controller._hide_overlay_timer.isActive() is False
    assert confirm_edit_button.property("visible") is False
    assert cancel_edit_button.property("visible") is True
    assert window.findChild(QObject, "retryEditButton") is None
    controller.confirmEdit()
    assert controller.reviewPending is True

    failed_attempt_path = (
        controller._modification_dataset.user_root
        / failed_request.episode_id
        / failed_request.attempt_id
        / "attempt.json"
    )
    failed_attempt = json.loads(failed_attempt_path.read_text(encoding="utf-8"))
    assert failed_attempt["status"] == "failed"
    assert failed_attempt["llm_error"] == "两种编辑协议均失败（返回格式无效）"
    episode_path = failed_attempt_path.parents[1] / "episode.json"
    episode = json.loads(episode_path.read_text(encoding="utf-8"))
    assert episode["final_status"] == "active"

    controller.cancelEdit()
    assert controller.reviewPending is False
    assert controller.interactionState == "cancelled"
    failed_attempt = json.loads(failed_attempt_path.read_text(encoding="utf-8"))
    cancel_reason = failed_attempt["feedback"][-1]["failure_reason"]
    assert cancel_reason["code"] == "llm_error"
    assert cancel_reason["input_method"] == "automatic"
    assert "已自动标记本次取消原因：大模型理解错误" in controller.logText

    controller._apply_runtime_update("改得简洁清楚", True, "", 61)
    recovered_request = submitted[7]
    assert recovered_request.episode_id != failed_request.episode_id
    assert recovered_request.target_text == "大模型失败时保留的原文。"
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=recovered_request.request_id,
            session_id=61,
            mode=recovered_request.mode,
            raw_text=recovered_request.raw_text,
            final_text="恢复后的清楚文本。",
            latency_s=0.2,
            used_llm=True,
            target_text=recovered_request.target_text,
        )
    )
    controller.confirmEdit()
    assert desktop_target.current_text == "恢复后的清楚文本。"
    episode = json.loads(episode_path.read_text(encoding="utf-8"))
    assert episode["attempt_ids"] == [failed_request.attempt_id]
    assert episode["final_status"] == "cancelled"

    # A syntactically valid response that leaves the target unchanged is also
    # a persistent LLM failure, not a transient notification.
    desktop_target.current_text = "不能原样返回的文本。"
    controller._apply_runtime_update("请修改这句话", True, "", 62)
    unchanged_request = submitted[8]
    controller._apply_text_processed(
        TextProcessingResult(
            request_id=unchanged_request.request_id,
            session_id=62,
            mode=unchanged_request.mode,
            raw_text=unchanged_request.raw_text,
            final_text=unchanged_request.target_text,
            latency_s=0.2,
            used_llm=True,
            target_text=unchanged_request.target_text,
        )
    )
    assert controller.reviewFailed is True
    assert controller.reviewCanConfirm is False
    assert "大模型未找到可可靠执行的修改" in controller.transcriptText
    unchanged_attempt_path = (
        controller._modification_dataset.user_root
        / unchanged_request.episode_id
        / unchanged_request.attempt_id
        / "attempt.json"
    )
    unchanged_attempt = json.loads(
        unchanged_attempt_path.read_text(encoding="utf-8")
    )
    assert unchanged_attempt["status"] == "failed"
    assert unchanged_attempt["llm_error"] == "大模型未找到可可靠执行的修改"
    controller.cancelEdit()
    unchanged_attempt = json.loads(
        unchanged_attempt_path.read_text(encoding="utf-8")
    )
    cancel_reason = unchanged_attempt["feedback"][-1]["failure_reason"]
    assert cancel_reason["code"] == "llm_error"
    assert cancel_reason["input_method"] == "automatic"

    submitted_before_oversized_capture = len(submitted)
    desktop_target.current_text = "页面内容" * (MAX_EDIT_TARGET_CHARS // 4 + 1)
    controller._apply_runtime_update("润色一下", True, "", 48)
    assert len(submitted) == submitted_before_oversized_capture
    assert desktop_target.released[-1] == target_ref
    assert "超过单次修改上限" in controller.logText

    # A failed/noop edit must not pin an unrelated future edit to old text.
    desktop_target.current_text = "新的目标文本。"
    controller._set_interaction_state("error")
    controller._apply_runtime_update("把新的改成更新的", True, "", 49)
    assert submitted[-1].target_text == "新的目标文本。"

    controller._apply_runtime_finished(
        "Ring BLE connection was physically lost\n"
        "[DIAG] encoding=opus, audio=12.3s\n"
        "[DIAG] mtu=185, missing_blocks=2"
    )
    assert controller.connected is False
    assert controller.statusTitle == "设备已断开"
    assert "自动断开" in controller.statusDetail
    assert "[DIAG]" not in controller.statusDetail
    assert "missing_blocks=2" in controller.logText
    assert controller.hasSelectedDevice is True
    assert controller.canReconnect is True
    controller.reconnectDevice()
    assert started == ["RING-ID", "RING-ID"]
    window.close()
