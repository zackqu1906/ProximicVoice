from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .alignment import align_take
from .capture import DualCapture
from .marker_player import MarkerPlayer
from .sync_markers import PROTOCOL
from .storage import list_takes, read_json, read_wav


class Bridge(QObject):
    event = Signal(str, object)


class Worker:
    def __init__(self, bridge):
        self.bridge = bridge
        self.closed = False
        self.loop = asyncio.new_event_loop()
        self.pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix="alignment")
        self.service = DualCapture(bridge.event.emit)
        self.thread = threading.Thread(target=self._run, daemon=True,name="dual-ring-ble")
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, name, coro):
        future=asyncio.run_coroutine_threadsafe(coro,self.loop)
        future.add_done_callback(lambda f:self._finished(name,f))

    def cpu(self, name, function, *args, **kwargs):
        future=self.pool.submit(function,*args,**kwargs)
        future.add_done_callback(lambda f:self._finished(name,f))

    def _finished(self,name,future):
        try:
            self.bridge.event.emit("done",(name,future.result()))
        except Exception as exc:
            self.bridge.event.emit("error",(name,f"{type(exc).__name__}: {exc}"))

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        future=asyncio.run_coroutine_threadsafe(self.service.disconnect(),self.loop)
        try:
            future.result(timeout=12)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.pool.shutdown(wait=False,cancel_futures=True)


class Waveform(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(140)
        self.tracks=[]
        self.position=0
        self.duration=1

    def load(self, paths):
        self.tracks=[]
        for path in paths:
            x=read_wav(path)
            stride=max(1,int(np.ceil(len(x)/1800)))
            padded=np.pad(x,(0,(-len(x))%stride))
            frames=padded.reshape(-1,stride) if len(padded) else np.zeros((1,1))
            self.tracks.append((frames.min(axis=1),frames.max(axis=1)))
        self.duration=max(len(x)/16000, .01)
        self.update()

    def paintEvent(self,event):
        p=QPainter(self)
        p.fillRect(self.rect(),QColor("#101a26"))
        colors=["#65d7c0","#91b8ff"]
        for i,(lo,hi) in enumerate(self.tracks):
            middle=self.height()*(i+.5)/max(1,len(self.tracks))
            scale=self.height()/max(1,len(self.tracks))*.43
            p.setPen(QPen(QColor(colors[i%2]),1))
            for j in range(len(lo)):
                x=int(j/max(1,len(lo)-1)*(self.width()-1))
                p.drawLine(x,int(middle-hi[j]*scale),x,int(middle-lo[j]*scale))
        p.setPen(QColor("#ffc875"))
        x=int(self.position/max(self.duration,.01)*self.width())
        p.drawLine(x,0,x,self.height())


def combo(options):
    widget=QComboBox()
    for label,value in options:
        widget.addItem(label,value)
    return widget


class MainWindow(QMainWindow):
    """Capture and listen; alignment is automatic, never a review gate."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.current: Path | None = None
        self.busy = False  # BLE operation only; alignment has its own lifecycle.
        self.connected = False
        self.recording = False
        self.capture_phase = "idle"
        self.closing = False
        self.closed = False
        self.pending_seek = 0
        self.device_inventory = []
        self.alignment_jobs: dict[str, Path] = {}
        self.alignment_failures: dict[Path, str] = {}
        self.session_group = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.bridge = Bridge()
        self.bridge.event.connect(self._event)
        self.worker = Worker(self.bridge)
        self.setWindowTitle("Ring Whisper Lab · 提示音同步录音")
        self.resize(880, 700)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(.5)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.positionChanged.connect(self._position)
        self.player.durationChanged.connect(lambda n: self.seek.setRange(0, n))
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.errorOccurred.connect(
            lambda *args: self.status.setText("播放错误：" + self.player.errorString()))
        self.marker_player = MarkerPlayer(self)
        self.marker_player.finished.connect(self._marker_finished)
        self.marker_player.failed.connect(self._marker_failed)
        self._build()
        for selector in self.device_select.values():
            selector.currentIndexChanged.connect(self._buttons)
        self.refresh()
        self._buttons()
        QTimer.singleShot(0, self._queue_pending)

    def _build(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        title = QLabel("双 Ring 录音")
        title.setStyleSheet("font-size:24px;font-weight:600")
        layout.addWidget(title)
        layout.addWidget(QLabel("A 裸麦 / B 防喷罩 · 首尾提示音同步 · 自动裁掉提示音后试听"))

        devices = QGridLayout()
        self.device_select = {}
        for row, (role, label) in enumerate((("input", "A · 裸麦"), ("reference", "B · 防喷罩"))):
            devices.addWidget(QLabel(label), row, 0)
            selector = QComboBox()
            selector.setMinimumWidth(360)
            selector.addItem("请先扫描设备…", None)
            self.device_select[role] = selector
            devices.addWidget(selector, row, 1)
        self.scan_btn = QPushButton("扫描设备")
        self.scan_btn.clicked.connect(lambda: self._network("scan", self.worker.service.scan()))
        self.connect_btn = QPushButton("连接两只设备")
        self.connect_btn.clicked.connect(self.connect_devices)
        devices.addWidget(self.scan_btn, 0, 2)
        devices.addWidget(self.connect_btn, 1, 2)
        devices.setColumnStretch(1, 1)
        layout.addLayout(devices)
        self.connection_label = QLabel("未连接 · 请先在其他应用中断开这两只设备。")
        self.connection_label.setWordWrap(True)
        layout.addWidget(self.connection_label)

        capture = QHBoxLayout()
        self.record_btn = QPushButton("开始录音")
        self.record_btn.setMinimumHeight(40)
        self.record_btn.clicked.connect(self.toggle_capture)
        capture.addWidget(self.record_btn)
        self.level_label = QLabel("等界面显示“请说话”再开口；说完点结束，等待结束提示音播放完成。")
        self.level_label.setWordWrap(True)
        capture.addWidget(self.level_label, 1)
        layout.addLayout(capture)
        output = QLabel("提示音输出：" + self.marker_player.device.description()
                        + " · 请勿静音，保持两只 Ring 和扬声器位置固定、尽量等距。")
        output.setWordWrap(True)
        layout.addWidget(output)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["录音", "时长", "试听版本"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self.select_take)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table, 1)
        self.detail = QLabel("还没有录音。连接两只设备后即可开始。")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.waveform = Waveform()
        self.waveform.setMinimumHeight(100)
        self.waveform.setMaximumHeight(120)
        layout.addWidget(self.waveform)
        listen = QHBoxLayout()
        self.track = combo([("听 A · 裸麦", "input"), ("听 B · 防喷罩", "reference"),
                            ("左右对照 · 左 A / 右 B", "stereo")])
        self.track.currentIndexChanged.connect(self._switch_track)
        self.play_btn = QPushButton("播放 / 暂停")
        self.play_btn.clicked.connect(self.play)
        listen.addWidget(self.track)
        listen.addWidget(self.play_btn)
        listen.addWidget(QLabel("音量"))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(50)
        self.volume.setMaximumWidth(140)
        self.volume.valueChanged.connect(lambda x: self.audio.setVolume(x / 100))
        listen.addWidget(self.volume)
        listen.addStretch()
        layout.addLayout(listen)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.sliderMoved.connect(self.player.setPosition)
        layout.addWidget(self.seek)
        self.status = QLabel("自动保存原始音频和对齐版本，无需填写或复查。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        path = QLabel("保存位置：" + str(self.root))
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setWordWrap(True)
        path.setStyleSheet("color:#888;font-size:11px")
        layout.addWidget(path)
        self.setCentralWidget(central)

    def _buttons(self, *args):
        free = not self.busy and not self.recording and not self.closing
        selected = [c.currentData() for c in self.device_select.values()]
        self.scan_btn.setEnabled(free and not self.connected)
        self.connect_btn.setText("断开两只设备" if self.connected else "连接两只设备")
        self.connect_btn.setEnabled(
            free and (self.connected or (all(selected) and selected[0] != selected[1])))
        self.record_btn.setText({"idle": "开始录音", "starting": "准备录音…",
                                "start_marker": "开始提示音…", "speaking": "结束并保存",
                                "end_marker": "结束提示音…", "stopping": "保存中…"}[self.capture_phase])
        self.record_btn.setEnabled(
            not self.busy and not self.closing and self.capture_phase in ("idle", "speaking")
            and (self.recording or self.connected))
        for selector in self.device_select.values():
            selector.setEnabled(free and not self.connected)
        self.table.setEnabled(free)
        self.track.setEnabled(free and self.current is not None)
        self.seek.setEnabled(free and self.current is not None)
        try:
            playable = self._play_path().is_file()
        except (ValueError, OSError):
            playable = False
        self.play_btn.setEnabled(free and playable)

    def _network(self, name, coro):
        self.busy = True
        self.status.setText({"scan": "正在扫描设备…", "connect": "正在连接两只设备…",
                             "disconnect": "正在断开…", "start": "正在启动两路录音…",
                             "stop": "正在保存录音…"}.get(name, "正在处理…"))
        self._buttons()
        self.worker.submit(name, coro)

    def _queue_alignment(self, path):
        path = Path(path).resolve()
        name = "align:" + str(path)
        if name in self.alignment_jobs or path in self.alignment_failures:
            return
        record = read_json(path / "record.json")
        if record.get("alignment") or record.get("status") == "recording":
            return
        if not all((path / "raw" / f"{role}.wav").is_file() for role in ("input", "reference")):
            return
        self.alignment_jobs[name] = path
        self.worker.cpu(name, align_take, path)

    def _queue_pending(self):
        # Also finish saved takes left unaligned by an earlier application exit.
        if self.closing:
            return
        for path in list_takes(self.root):
            self._queue_alignment(path)
        self.refresh(self.current)

    def _event(self, kind, payload):
        if self.closed:
            return
        if kind == "devices":
            self.device_inventory = payload
            self._populate_devices()
            self.status.setText(f"发现 {len(payload)} 个设备，请选择两只不同的 Ring。")
        elif kind == "connected":
            self.connected = True
            self.connection_label.setText("两只设备已连接 · A 裸麦 / B 防喷罩")
            self.status.setText("可以开始录音。")
        elif kind == "disconnected":
            self.connected = False
            self.connection_label.setText("设备已断开，请重新连接两只设备。")
        elif kind == "recording":
            self.recording = True
            self.status.setText("两路已收到音频，准备播放开始提示音；请先不要说话。")
        elif kind == "saved":
            self.marker_player.cancel()
            self.capture_phase = "idle"
            self.recording = False
            path = Path(payload).resolve()
            self._queue_alignment(path)
            self.status.setText("录音已保存，正在自动对齐…")
            self.refresh(path)
        elif kind == "levels":
            self.level_label.setText("    ".join(
                f"{'A' if role == 'input' else 'B'}  {item['duration_s']:.1f} 秒"
                f" · {item['rms_dbfs']:.0f} dBFS" for role, item in payload.items()))
        elif kind == "log":
            self.status.setText(str(payload))
        elif kind in ("done", "error"):
            name, result = payload
            if name == "marker_note":
                if kind == "error" and self.recording:
                    self._marker_failed("无法保存提示音事件：" + str(result))
                return
            elif name.startswith("align:"):
                path = self.alignment_jobs.pop(name, None)
                if path is not None:
                    if kind == "error":
                        self.alignment_failures[path] = str(result)
                    # Do not interrupt playback of a different take.
                    self.refresh(self.current, reload_selected=path == self.current)
                if not self.recording and not self.busy:
                    self.status.setText(
                        "这条录音无法可靠对齐，已保留原音供试听。"
                        if kind == "error" else "自动对齐完成，选择录音即可试听。")
            else:
                self.busy = False
                if kind == "error":
                    self.status.setText("操作未完成：" + str(result))
                    if name == "start" and not self.recording:
                        self.capture_phase = "idle"
                elif name == "start" and self.recording and self.capture_phase == "starting":
                    self._begin_marker("start")
                elif name == "stop":
                    self.status.setText("已保存，正在自动对齐…" if self.alignment_jobs
                                        else "已保存。选择录音即可试听。")
                elif name == "disconnect":
                    self.status.setText("已断开两只设备。")
        self._buttons()
        if self.closing and not self.busy and not self.alignment_jobs:
            QTimer.singleShot(0, self.close)

    def _populate_devices(self):
        rows = sorted(self.device_inventory,
                      key=lambda d: ("ring" not in d["name"].lower(), d["name"], d["id"]))
        for selector in self.device_select.values():
            old = selector.currentData()
            selector.blockSignals(True)
            selector.clear()
            selector.addItem("请选择设备…", None)
            for device in rows:
                selector.addItem(f"{device['name']} · {device['id']}", device["id"])
            selector.setCurrentIndex(max(0, selector.findData(old)))
            selector.blockSignals(False)
        self._buttons()

    def connect_devices(self):
        if self.connected:
            self._network("disconnect", self.worker.service.disconnect())
        else:
            self._network("connect", self.worker.service.connect(
                self.device_select["input"].currentData(),
                self.device_select["reference"].currentData()))

    def toggle_capture(self):
        if self.recording:
            if self.capture_phase == "speaking":
                self._begin_marker("end")
        else:
            self.start_capture()

    def _begin_marker(self, kind):
        self.capture_phase = "start_marker" if kind == "start" else "end_marker"
        self.status.setText("正在播放开始提示音，请先不要说话…" if kind == "start"
                            else "正在播放结束提示音，请保持安静，稍后自动保存…")
        self.worker.submit("marker_note", self.worker.service.note_sync_event(
            kind, "requested", time.monotonic_ns(), self.marker_player.device.description()))
        self._buttons()
        self.marker_player.play(self.root, kind)

    def _marker_finished(self, kind):
        expected = "start_marker" if kind == "start" else "end_marker"
        if not self.recording or self.capture_phase != expected:
            return
        self.worker.submit("marker_note", self.worker.service.note_sync_event(
            kind, "playback_finished", time.monotonic_ns()))
        if kind == "start":
            self.capture_phase = "speaking"
            self.status.setText("请说话 · 说完点击“结束并保存”。中间的停顿和耳语会完整保留。")
            self._buttons()
            if self.closing:
                self._begin_marker("end")
        else:
            self.capture_phase = "stopping"
            self._network("stop", self.worker.service.stop())

    def _marker_failed(self, reason):
        self.marker_player.cancel()
        if not self.recording or self.capture_phase == "stopping":
            return
        self.worker.submit("marker_note", self.worker.service.note_sync_event(
            "playback", "failed", time.monotonic_ns(), reason))
        self.capture_phase = "stopping"
        self._network("stop", self.worker.service.stop("sync_playback_failed"))
        self.status.setText("提示音失败，正在保留原始录音：" + reason)

    def start_capture(self):
        self.player.stop()
        self.capture_phase = "starting"
        # Unknown speaker/style are deliberately not fabricated as training labels.
        metadata = {
            "speaker_id": "unknown", "session_group": self.session_group,
            "configuration": "bare_protected", "speech_style": "unspecified",
            "metadata_source": "simple_ui_defaults",
            "sync_protocol": PROTOCOL,
            "sync_output_device": self.marker_player.device.description(),
            "sync_playback_volume": self.marker_player.audio.volume(),
            "notes": "界面约定 A 裸麦 / B 防喷罩；说话人、语音类型和几何条件未标注。",
        }
        self._network("start", self.worker.service.start(str(self.root), metadata, "opus"))

    def _take_state(self, path, record):
        alignment = record.get("alignment")
        if alignment:
            label = "提示音对齐 · 已裁剪" if alignment.get("method") == PROTOCOL else "旧版语音对齐"
            return label if alignment.get("automatic_quality_pass") else label + " · 质量提示"
        if "align:" + str(path) in self.alignment_jobs:
            return "正在自动对齐…"
        if path in self.alignment_failures:
            return "原音 · 对齐失败"
        return "原音 · 未对齐"

    def refresh(self, select=None, *, reload_selected=False):
        paths = list_takes(self.root)
        self.table.blockSignals(True)
        self.table.setRowCount(len(paths))
        selection = -1
        for row, path in enumerate(paths):
            record = read_json(path / "record.json")
            duration = record.get("alignment", {}).get("duration_s",
                record.get("quality", {}).get("input", {}).get("duration_s", 0))
            label = ("[演示] " if record.get("demo") else "") + path.name
            values = [label, f"{duration:.1f} 秒", self._take_state(path, record)]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, str(path))
            if path == select:
                selection = row
        self.table.resizeColumnsToContents()
        if paths:
            self.table.selectRow(selection if selection >= 0 else 0)
        self.table.blockSignals(False)
        if paths:
            target = paths[selection if selection >= 0 else 0]
            if target != self.current or reload_selected:
                self.select_take()
            else:
                self._describe_take()
        else:
            self.current = None
            self.detail.setText("还没有录音。连接两只设备后即可开始。")
        self._buttons()

    def _describe_take(self):
        if not self.current:
            return
        record = read_json(self.current / "record.json")
        alignment = record.get("alignment")
        if alignment:
            if alignment.get("method") == PROTOCOL:
                text = "当前播放提示音对齐、裁剪后的说话区间 · 上轨 A，下轨 B；首尾标记不保证中段精度。"
            else:
                text = "旧录音：语音匹配对齐预览，未使用首尾提示音。"
            if not alignment.get("automatic_quality_pass"):
                text += " 质量提示：" + ("；".join(alignment.get("warnings", []))
                                         or "录音或对齐质量未通过自动检查。")
        elif self.current in self.alignment_failures:
            text = "无法自动对齐，当前播放原音：" + self.alignment_failures[self.current]
        else:
            text = "正在自动对齐；完成后切换到对齐版本。当前可分别试听原音。"
        if record.get("status") == "interrupted":
            text += " 本条录音中断，可能不完整。"
        self.detail.setText(text)

    def select_take(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self.pending_seek = 0
        self.current = Path(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        self._describe_take()
        stereo_available = (self._folder() / "stereo.wav").is_file()
        self.track.model().item(2).setEnabled(stereo_available)
        if not stereo_available and self.track.currentData() == "stereo":
            self.track.setCurrentIndex(0)
        try:
            self.waveform.load([self._folder() / "input.wav", self._folder() / "reference.wav"])
        except (ValueError, OSError):
            self.waveform.tracks = []
            self.waveform.update()
        self._buttons()

    def _folder(self):
        if not self.current:
            raise ValueError("请选择录音")
        alignment = read_json(self.current / "record.json").get("alignment")
        return self.current / alignment["revision"] if alignment else self.current / "raw"

    def _play_path(self):
        return self._folder() / f"{self.track.currentData()}.wav"

    def play(self):
        try:
            path = self._play_path()
            if not path.is_file():
                raise ValueError("录音文件尚未生成")
            url = QUrl.fromLocalFile(str(path))
            if self.player.source() != url:
                self.pending_seek = 0
                self.player.setSource(url)
                self.player.play()
            elif self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def _switch_track(self, *args):
        if not self.current or self.recording:
            return
        position = self.player.position()
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.pending_seek = position
        self.player.setSource(QUrl.fromLocalFile(str(self._play_path())))
        if playing:
            self.player.play()
        self._buttons()

    def _media_status(self, status):
        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            self.player.setPosition(self.pending_seek)
            self.pending_seek = 0

    def _position(self, value):
        if not self.seek.isSliderDown():
            self.seek.setValue(value)
        self.waveform.position = value / 1000
        self.waveform.update()

    def closeEvent(self, event):
        if self.closed:
            event.accept()
            return
        self.closing = True
        if self.busy or self.recording or self.alignment_jobs:
            event.ignore()
            if self.recording and not self.busy and self.capture_phase == "speaking":
                self._begin_marker("end")
            else:
                self.status.setText("正在保存或对齐，完成后会自动关闭。")
            self._buttons()
            return
        self.player.stop()
        self.marker_player.cancel()
        self.closed = True
        try:
            self.worker.shutdown()
        finally:
            event.accept()
