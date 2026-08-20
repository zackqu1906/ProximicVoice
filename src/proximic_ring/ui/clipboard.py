"""Qt clipboard bridge used by the Windows desktop text adapter."""

from __future__ import annotations

from dataclasses import dataclass

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

    def text(self) -> str:
        QCoreApplication.processEvents()
        return str(self._clipboard().text() or "")
