from pathlib import Path

import pytest


def test_qml_customer_window_loads(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtWidgets import QApplication

    from proximic_ring.ui.controller import AppController

    app = QApplication.instance() or QApplication(["ui-test", "-platform", "offscreen"])
    QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, str(tmp_path))
    controller = AppController()
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
