from pathlib import Path

import pytest


def test_notice_is_topmost_when_main_window_is_hidden_and_requires_acknowledgement(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QSettings, Qt, QUrl
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    import proximic_ring.ui.controller as module
    from proximic_ring.ui.notifications import install_ring_disconnect_notice

    app = QCoreApplication.instance()
    if app is not None and not isinstance(app, QApplication):
        pytest.skip("QML needs its own QApplication process")
    app = app or QApplication(["disconnect-notice", "-platform", "offscreen"])
    monkeypatch.setattr(module, "app_data_root", lambda: tmp_path)
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path))
    controller = module.AppController()
    controller._text_processing_worker.close(wait=True)
    controller._accessibility_timer.stop()
    engine = QQmlApplicationEngine()
    warnings = []
    engine.warnings.connect(lambda values: warnings.extend(str(value) for value in values))
    engine.rootContext().setContextProperty("appController", controller)
    engine.load(QUrl.fromLocalFile(str(Path(module.__file__).parent / "qml/Main.qml")))
    window = engine.rootObjects()[0]
    install_ring_disconnect_notice(window, controller)
    notice = window.findChild(QQuickWindow, "ringDisconnectNotice")
    dismiss = window.findChild(QObject, "dismissRingDisconnectNoticeButton")
    reconnect = window.findChild(QObject, "reconnectRingFromNoticeButton")
    try:
        window.hide()
        controller._runtime_active = controller._runtime_had_connection = controller._connected = True
        controller._selector = "RING-ID"
        controller._can_reconnect = True
        controller.reconnectAvailabilityChanged.emit()
        controller._apply_runtime_stopping(controller._disconnect_event)
        QTest.qWait(100)
        assert not window.isVisible()
        assert notice.isVisible()
        assert notice.transientParent() is None
        assert notice.flags() & Qt.WindowStaysOnTopHint
        assert notice.flags() & Qt.WindowDoesNotAcceptFocus
        assert notice.screen().availableGeometry().contains(notice.frameGeometry())
        assert not reconnect.property("enabled")
        controller._apply_runtime_finished("BLE lost")
        QTest.qWait(100)
        assert notice.isVisible()
        assert reconnect.property("enabled")
        QMetaObject.invokeMethod(dismiss, "click")
        app.processEvents()
        assert not notice.isVisible()
        controller._apply_runtime_finished("duplicate completion")
        app.processEvents()
        assert not notice.isVisible()
        requested = []
        monkeypatch.setattr(controller, "_start_selected_device", lambda: requested.append(controller._selector))
        controller._ring_disconnect_notice_visible = True
        controller.ringDisconnectNoticeChanged.emit()
        app.processEvents()
        QMetaObject.invokeMethod(reconnect, "click")
        app.processEvents()
        assert requested == ["RING-ID"]
        assert window.isVisible()
        assert not warnings
    finally:
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        controller._close_voice_history()
