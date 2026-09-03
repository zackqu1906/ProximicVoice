"""Clipboard bridge used by the desktop text adapters."""

from __future__ import annotations

from dataclasses import dataclass
import sys

from PySide6.QtCore import QCoreApplication, QMimeData
from PySide6.QtGui import QGuiApplication


@dataclass(frozen=True)
class QtClipboardSnapshot:
    payloads: tuple[tuple[str, bytes], ...]
    image: object | None = None


class QtClipboardBridge:
    """Clone all clipboard MIME payloads before temporary Select-All/Copy."""

    @staticmethod
    def _clipboard():
        application = QGuiApplication.instance()
        if application is None:
            raise RuntimeError("图形界面尚未初始化，无法读取文本框")
        return application.clipboard()

    def snapshot(self) -> QtClipboardSnapshot:
        mime = self._clipboard().mimeData()
        payloads = tuple(
            (str(fmt), bytes(mime.data(fmt))) for fmt in mime.formats()
        )
        image = mime.imageData() if mime.hasImage() else None
        return QtClipboardSnapshot(payloads, image)

    def restore(self, snapshot: object) -> None:
        if not isinstance(snapshot, QtClipboardSnapshot):
            return
        mime = QMimeData()
        for fmt, data in snapshot.payloads:
            mime.setData(fmt, data)
        if snapshot.image is not None:
            mime.setImageData(snapshot.image)
        self._clipboard().setMimeData(mime)
        QCoreApplication.processEvents()

    def set_text(self, text: str) -> None:
        value = str(text)
        # QClipboard can advertise data lazily.  On macOS that means changing
        # focus immediately after setText() may let the destination process
        # observe the previous pasteboard item.  Writing NSPasteboard directly
        # makes the new string available before Command+V is posted.
        if sys.platform == "darwin":
            try:
                from AppKit import NSPasteboard, NSPasteboardTypeString

                pasteboard = NSPasteboard.generalPasteboard()
                pasteboard.clearContents()
                if not pasteboard.setString_forType_(
                    value, NSPasteboardTypeString
                ):
                    raise RuntimeError("macOS 剪贴板拒绝写入听写内容")
                QCoreApplication.processEvents()
                return
            except ImportError:
                # Source-only environments may not bundle PyObjC.  Qt remains
                # a functional fallback and the adapter verifies the value
                # before it sends the paste shortcut.
                pass
        self._clipboard().setText(value)
        QCoreApplication.processEvents()

    def text(self) -> str:
        QCoreApplication.processEvents()
        if sys.platform == "darwin":
            try:
                from AppKit import NSPasteboard, NSPasteboardTypeString

                value = NSPasteboard.generalPasteboard().stringForType_(
                    NSPasteboardTypeString
                )
                return str(value or "")
            except ImportError:
                pass
        return str(self._clipboard().text() or "")
