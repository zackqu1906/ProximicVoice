import os
from pathlib import Path
import sys

import pytest


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
    assert "CPU 版 PyTorch" in message

    monkeypatch.setattr(sys, "platform", "darwin")
    devices, message = AppController._detect_compute_devices()
    assert devices == [{"label": "CPU（兼容性最佳）", "value": "cpu"}]
    assert "macOS" in message
    assert AppController._detect_nvidia_gpu_name() == ""


def test_qml_customer_window_loads(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QObject, QSettings, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    from proximic_ring.ui.controller import AppController

    app = QApplication.instance() or QApplication(["ui-test", "-platform", "offscreen"])
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path))
    controller = AppController()
    assert controller.computeDevices[0] == {
        "label": "CPU（兼容性最佳）",
        "value": "cpu",
    }
    assert controller.asrDevice in {
        item["value"] for item in controller.computeDevices
    }
    controller._apply_scan_finished(
        [
            {"name": "Ringo Test", "identifier": "RING-ID", "rssi": -40},
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
    if os.name != "nt":
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
    device_combo = window.findChild(QObject, "asrDeviceCombo")
    gpu_install_button = window.findChild(QObject, "gpuInstallButton")
    assert picker is not None and picker.property("visible") is True
    assert device_list is not None and device_list.property("count") == 1
    assert search_field is not None and search_field.property("text") == "Ringo"
    assert device_combo is not None
    assert device_combo.property("count") == len(controller.computeDevices)
    assert gpu_install_button is not None
    assert gpu_install_button.property("visible") is controller.gpuInstallerAvailable

    started = []
    controller._scan_busy = True
    controller._start_selected_device = lambda: started.append(controller.selector)
    controller.connectToDevice("RING-ID", "Ringo Test")
    assert started == []
    controller._apply_scan_finished([], "")
    assert started == ["RING-ID"]

    controller.asrModel = "iic/SenseVoiceSmall"
    controller.asrBackend = "funasr_nano"
    assert controller.asrModel == ""

    controller._apply_runtime_started()
    assert controller.connected is True
    assert controller.recognitionEnabled is False
    controller.startRecognition()
    assert controller.recognitionEnabled is True
    controller.pauseRecognition()
    assert controller.connected is True
    assert controller.recognitionEnabled is False

    controller._apply_runtime_update("第一段", True, "")
    controller._apply_runtime_update("第二段", True, "")
    assert controller.editorText == "第一段\n第二段"
    window.close()
