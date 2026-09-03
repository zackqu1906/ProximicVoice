from __future__ import annotations

import os
from pathlib import Path
import signal
import sys


def main(argv: list[str] | None = None) -> int:
    from ..runtime_paths import configure_runtime_environment, resource_root

    configure_runtime_environment()
    try:
        from PySide6.QtCore import QTimer, QUrl
        from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    except ImportError as exc:
        raise SystemExit(
            'ProxiMic UI requires PySide6. Install with: pip install -e ".[ui]"'
        ) from exc

    from .controller import AppController

    startup_probe = os.environ.get("PROXIMIC_STARTUP_PROBE", "").strip() == "1"
    print("[startup] creating QApplication")
    app = QApplication(list(sys.argv if argv is None else argv))
    app.setApplicationName("ProxiMic Voice")
    app.setApplicationDisplayName("ProxiMic Voice")
    app.setOrganizationName("ProxiMic")

    base = Path(__file__).resolve().parent
    icon_path = base / "assets" / "proximic.svg"
    icon = QIcon(str(icon_path))
    if icon.isNull():
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor("#6C8CFF"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("white"))
        painter.drawText(pixmap.rect(), 0x0084, "P")  # AlignCenter
        painter.end()
        icon = QIcon(pixmap)
    app.setWindowIcon(icon)

    controller = AppController()
    print("[startup] controller ready")
    voice_action_hotkeys = None
    if sys.platform == "win32":
        try:
            from ..voice_actions import WindowsVoiceActionHotkeys

            voice_action_hotkeys = WindowsVoiceActionHotkeys(
                controller.dispatchVoiceAction,
                is_review_active=lambda: controller.reviewPending,
                is_interaction_active=lambda: controller.interactionCanCancel,
                is_mode_correction_active=lambda: controller.modeCorrectionAvailable,
            )
            app.aboutToQuit.connect(voice_action_hotkeys.close)
        except BaseException as exc:
            print(f"[voice-actions] 全局交互快捷键不可用：{exc}", file=sys.stderr)
    elif sys.platform == "darwin":
        mac_hotkeys: dict[str, object | None] = {"instance": None}

        def install_macos_hotkeys() -> None:
            if (
                mac_hotkeys["instance"] is not None
                or controller.macOSAccessibilityRequired
            ):
                return
            try:
                from ..voice_actions import MacOSVoiceActionHotkeys

                mac_hotkeys["instance"] = MacOSVoiceActionHotkeys(
                    controller.dispatchVoiceAction,
                    is_review_active=lambda: controller.reviewPending,
                    is_interaction_active=lambda: controller.interactionCanCancel,
                    is_mode_correction_active=lambda: controller.modeCorrectionAvailable,
                )
                print("[voice-actions] macOS 语音交互按键已就绪")
            except BaseException as exc:
                print(
                    f"[voice-actions] macOS 编辑确认键不可用：{exc}",
                    file=sys.stderr,
                )

        def close_macos_hotkeys() -> None:
            instance = mac_hotkeys["instance"]
            if instance is not None:
                instance.close()

        controller.accessibilityChanged.connect(install_macos_hotkeys)
        app.aboutToQuit.connect(close_macos_hotkeys)
        QTimer.singleShot(0, install_macos_hotkeys)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("appController", controller)
    qml_errors: list[str] = []
    engine.warnings.connect(
        lambda warnings: qml_errors.extend(str(item) for item in warnings)
    )
    engine.load(QUrl.fromLocalFile(str(base / "qml" / "Main.qml")))
    if not engine.rootObjects():
        detail = "\n".join(qml_errors) or "QML engine did not create a root window"
        raise RuntimeError(f"主界面加载失败：{detail}")
    window = engine.rootObjects()[0]
    print("[startup] QML root window ready")
    # Load model weights and seed both stable prompt prefixes after the first
    # frame instead of making the user's first utterance pay this cost.
    if startup_probe:
        # Packaging CI sets this flag to prove the frozen executable can import
        # the application, QML, and the dynamically loaded ASR modules.
        from ..asr.backends.funasr_nano import FunASRNanoStreamingASR
        from ..asr.backends.streaming_sensevoice import StreamingSenseVoiceASR

        StreamingSenseVoiceASR._load_external_class(
            resource_root() / "third_party" / "streaming-sensevoice"
        )
        nano_repo = resource_root() / "third_party" / "Fun-ASR"
        nano_probe = object.__new__(FunASRNanoStreamingASR)
        nano_probe.repo_path = nano_repo
        nano_probe._load_external_class(nano_repo / "model.py")
        __import__("transformers.models.qwen3.modeling_qwen3")
        print("[startup] packaged ASR imports ready")
        QTimer.singleShot(300, app.quit)
    else:
        QTimer.singleShot(600, controller.warmLocalModel)

    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    controller.setTrayAvailable(tray_available)
    # Closing the main window is an explicit application exit.  The tray is a
    # convenience controller, not a reason to leave an invisible process alive.
    app.setQuitOnLastWindowClosed(True)
    tray = None
    if tray_available:
        tray = QSystemTrayIcon(icon, app)
        tray.setToolTip("ProxiMic Voice · 准备就绪")
        menu = QMenu()
        show_action = QAction("显示主窗口", menu)
        connection_action = QAction("连接设备", menu)
        recognition_action = QAction("开启语音识别", menu)
        quit_action = QAction("退出", menu)
        menu.addAction(show_action)
        menu.addAction(connection_action)
        menu.addAction(recognition_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)

        def show_window() -> None:
            window.show()
            window.raise_()
            window.requestActivate()

        def toggle_connection() -> None:
            if controller.connected:
                controller.disconnectDevice()
            else:
                show_window()
                if controller.canReconnect:
                    controller.reconnectDevice()
                else:
                    controller.requestDevicePicker()

        def refresh_tray() -> None:
            connection_action.setText(
                "断开设备"
                if controller.connected
                else ("重新连接设备" if controller.canReconnect else "选择并连接设备")
            )
            connection_action.setEnabled(
                controller.statusKind != "stopping"
                if controller.connected
                else not controller.busy and not controller.scanBusy
            )
            recognition_action.setText(
                "暂停语音识别" if controller.recognitionEnabled else "开启语音识别"
            )
            recognition_action.setEnabled(controller.connected and not controller.busy)
            tray.setToolTip(f"ProxiMic Voice · {controller.statusTitle}")

        show_action.triggered.connect(show_window)
        connection_action.triggered.connect(toggle_connection)
        recognition_action.triggered.connect(controller.toggleRecognition)
        quit_action.triggered.connect(controller.requestQuit)
        controller.connectedChanged.connect(refresh_tray)
        controller.recognitionEnabledChanged.connect(refresh_tray)
        controller.busyChanged.connect(refresh_tray)
        controller.scanBusyChanged.connect(refresh_tray)
        controller.settingsChanged.connect(refresh_tray)
        controller.reconnectAvailabilityChanged.connect(refresh_tray)
        controller.statusChanged.connect(refresh_tray)
        tray.activated.connect(
            lambda reason: show_window()
            if reason in (
                QSystemTrayIcon.ActivationReason.Trigger,
                QSystemTrayIcon.ActivationReason.DoubleClick,
            )
            else None
        )
        app.aboutToQuit.connect(tray.hide)
        refresh_tray()
        tray.show()

    # A small Qt timer gives Python regular opportunities to dispatch console
    # signals while the native Qt event loop is running.
    heartbeat = QTimer(app)
    heartbeat.setInterval(200)
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start()

    def handle_console_signal(_signum, _frame) -> None:
        controller.requestQuit()

    signal.signal(signal.SIGINT, handle_console_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_console_signal)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
