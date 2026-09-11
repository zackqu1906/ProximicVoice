"""Qt-owned speaker playback with completion and bounded failure handling."""
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from .sync_markers import playback_file


class MarkerPlayer(QObject):
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind = None
        outputs = QMediaDevices.audioOutputs()
        self.device = next((d for d in outputs if any(word in d.description().lower()
                           for word in ('macbook', 'built-in', '内置'))), QMediaDevices.defaultAudioOutput())
        self.audio = QAudioOutput(self.device, self)
        self.audio.setVolume(.7)  # independent of listening slider; never alters system volume
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.errorOccurred.connect(lambda *args: self._fail(self.player.errorString()))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self._fail('提示音播放超时'))

    def play(self, root, kind):
        self.cancel()
        self.kind = kind
        if self.device.isNull():
            self._fail('没有可用的扬声器输出设备')
            return
        try:
            path = playback_file(root, kind)
            self.timer.start(8000)
            self.player.setSource(QUrl.fromLocalFile(str(path)))
            if self.kind is not None:
                self.player.play()
        except Exception as exc:
            self._fail(str(exc))

    def _status(self, status):
        if self.kind is not None and status == QMediaPlayer.MediaStatus.EndOfMedia:
            kind = self.kind
            self.cancel()
            self.finished.emit(kind)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._fail('无法播放同步提示音')

    def _fail(self, message):
        if self.kind is not None:
            self.cancel()
            self.failed.emit(message)

    def cancel(self):
        self.kind = None
        self.timer.stop()
        self.player.stop()
