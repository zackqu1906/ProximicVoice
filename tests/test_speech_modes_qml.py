from pathlib import Path

import pytest


def test_independent_mode_controls_and_gesture_hint_render_and_lock(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QSettings, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import proximic_ring.ui.controller as module

    app = QCoreApplication.instance()
    if app is not None and not isinstance(app, QApplication):
        pytest.skip("QML needs its own QApplication process")
    app = app or QApplication(["speech-modes", "-platform", "offscreen"])
    monkeypatch.setattr(module, "app_data_root", lambda: tmp_path)
    monkeypatch.setattr(module.AppController, "_request_macos_accessibility", lambda self: None)
    monkeypatch.setattr(module, "input_device_choices", lambda: [])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    controller = module.AppController()
    controller._text_processing_worker.close(wait=True)
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda values: warnings.extend(str(value) for value in values))
    engine.rootContext().setContextProperty("appController", controller)
    engine.load(QUrl.fromLocalFile(str(Path(module.__file__).parent / "qml/Main.qml")))
    assert engine.rootObjects()
    window = engine.rootObjects()[0]
    assert isinstance(window, QQuickWindow)
    controls = {name: window.findChild(QObject, name) for name in (
        "runtimeSettingsDialog", "audioSourceCombo", "speechControlModeCombo",
        "microphoneDeviceCombo", "speechControlModeHint", "stage1SensitivitySlider",
    )}
    assert all(controls.values())
    try:
        window.show()
        QMetaObject.invokeMethod(controls["runtimeSettingsDialog"], "open")
        QTest.qWait(100)
        assert controls["audioSourceCombo"].property("currentIndex") == 0
        assert controls["speechControlModeCombo"].property("currentIndex") == 0
        assert not controls["microphoneDeviceCombo"].property("visible")
        controller.audioSource = "microphone"
        controller.speechControlMode = "gesture"
        assert controller.setGestureBinding("confirm", 0, "snap")
        QTest.qWait(100)
        assert controls["microphoneDeviceCombo"].property("visible")
        assert controls["audioSourceCombo"].property("currentIndex") == 1
        assert controls["speechControlModeCombo"].property("currentIndex") == 1
        assert "弹指" in controls["speechControlModeHint"].property("text")
        assert not controls["stage1SensitivitySlider"].property("enabled")
        controller._connected = True
        controller.connectedChanged.emit()
        app.processEvents()
        for name in ("audioSourceCombo", "speechControlModeCombo", "microphoneDeviceCombo"):
            assert not controls[name].property("enabled")
        assert window.grabWindow().save(str(tmp_path / "speech-mode-settings.png"))
        assert not warnings
    finally:
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        controller._close_voice_history()
