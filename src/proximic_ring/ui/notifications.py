"""Present the Ring disconnect notice independently of the main window."""
from __future__ import annotations


def install_ring_disconnect_notice(root, controller) -> None:
    from PySide6.QtGui import QCursor, QGuiApplication, QWindow

    notice = root.findChild(QWindow, "ringDisconnectNotice")
    if notice is None:
        raise RuntimeError("Ring disconnect notice window is missing")

    def present() -> None:
        if not controller.ringDisconnectNoticeVisible:
            return
        screen = QGuiApplication.screenAt(QCursor.pos()) or root.screen()
        if screen is not None:
            notice.setScreen(screen)
            area = screen.availableGeometry()
            notice.setPosition(
                area.x() + max(0, (area.width() - notice.width()) // 2),
                area.y() + max(0, (area.height() - notice.height()) // 3),
            )
        notice.show()
        notice.raise_()
        # Qt's topmost flag covers ordinary windows. macOS additionally needs
        # space membership to show above a different app's fullscreen Space.
        if QGuiApplication.platformName() == "cocoa":
            try:
                _show_on_macos_spaces(notice)
            except Exception as exc:
                controller._append_background_diagnostic(
                    f"[DISCONNECT NOTICE] macOS Space presentation failed: {exc}"
                )

    controller.ringDisconnectNoticeChanged.connect(present)


def _show_on_macos_spaces(window) -> None:
    import ctypes
    import objc
    from AppKit import (
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorMoveToActiveSpace,
    )

    # Qt on Cocoa exposes the window's NSView through winId().
    view = objc.objc_object(c_void_p=ctypes.c_void_p(int(window.winId())))
    native_window = view.window()
    if native_window is None:
        return
    behavior = int(native_window.collectionBehavior())
    native_window.setCollectionBehavior_(
        (behavior & ~NSWindowCollectionBehaviorMoveToActiveSpace)
        | NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    native_window.orderFrontRegardless()
