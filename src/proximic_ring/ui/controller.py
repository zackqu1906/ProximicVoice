from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import QObject, Property, QCoreApplication, QSettings, QTimer, Signal, Slot

from ..app_runtime import RecognitionRuntime, RuntimeSettings


class AppController(QObject):
    runningChanged = Signal()
    connectedChanged = Signal()
    recognitionEnabledChanged = Signal()
    busyChanged = Signal()
    statusChanged = Signal()
    transcriptChanged = Signal()
    editorChanged = Signal()
    logChanged = Signal()
    settingsChanged = Signal()
    trayAvailableChanged = Signal()

    _runtimeStatus = Signal(str)
    _runtimeStarted = Signal()
    _runtimeUpdate = Signal(str, bool, str)
    _runtimeFinished = Signal(str)
    _pushToTalkChanged = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings("ProxiMic", "ProxiMic Voice")
        self._connected = False
        self._recognition_enabled = False
        self._busy = False
        self._quitting = False
        self._quit_wait_ticks = 0
        self._tray_available = False
        self._status_title = "准备就绪"
        self._status_detail = "配置设备后即可开始自动语音输入"
        self._status_kind = "idle"
        self._transcript_text = ""
        self._transcript_final = False
        self._transcript_visible = False
        self._editor_text = ""
        self._log_lines: list[str] = []
        self._ptt_active = False
        self._worker: threading.Thread | None = None
        self._disconnect_event = threading.Event()
        self._recognition_event = threading.Event()

        # Resolve bundled resources from the installed source tree instead of
        # depending on the terminal's current working directory.
        project_root = Path(__file__).resolve().parents[3]
        assets = Path(__file__).resolve().parents[1] / "assets"
        default_model = assets / "ringo-near-v1.model"
        default_repo = project_root / "third_party" / "streaming-sensevoice"
        default_funasr_repo = project_root / "third_party" / "Fun-ASR"
        self._device_name = str(self._settings.value("ring/name", "Ringo"))
        self._selector = str(self._settings.value("ring/selector", ""))
        self._model_path = str(
            self._settings.value(
                "detector/model",
                str(default_model) if default_model.exists() else "",
            )
        )
        self._stage1_threshold = float(
            self._settings.value("detector/stage1Threshold", 0.005)
        )
        self._asr_backend = str(
            self._settings.value("asr/backend", "streaming_sensevoice")
        )
        self._asr_model = str(
            self._settings.value("asr/model", "iic/SenseVoiceSmall")
        )
        # CPU is the safest first-run default for a cloned project. Users with
        # a compatible CUDA environment can select cuda:0 in the UI.
        self._asr_device = str(self._settings.value("asr/device", "cpu"))
        self._asr_language = str(self._settings.value("asr/language", "zh"))
        self._streaming_repo = str(
            self._settings.value(
                "asr/streamingRepo",
                str(default_repo) if default_repo.exists() else "",
            )
        )
        self._funasr_repo = str(
            self._settings.value(
                "asr/funasrRepo",
                str(default_funasr_repo) if default_funasr_repo.exists() else "",
            )
        )
        self._desktop_output = self._bool_setting("input/desktopOutput", True)
        self._push_to_talk = self._bool_setting("input/pushToTalk", True)

        self._hide_overlay_timer = QTimer(self)
        self._hide_overlay_timer.setSingleShot(True)
        self._hide_overlay_timer.timeout.connect(self._hide_transcript)
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(100)
        self._quit_timer.timeout.connect(self._finish_quit)

        self._runtimeStatus.connect(self._apply_runtime_status)
        self._runtimeStarted.connect(self._apply_runtime_started)
        self._runtimeUpdate.connect(self._apply_runtime_update)
        self._runtimeFinished.connect(self._apply_runtime_finished)
        self._pushToTalkChanged.connect(self._apply_push_to_talk)

    def _bool_setting(self, key: str, default: bool) -> bool:
        value = self._settings.value(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    # Runtime state -------------------------------------------------------------
    @Property(bool, notify=runningChanged)
    def running(self) -> bool:
        """Backward-compatible alias for recognitionEnabled."""
        return self._recognition_enabled

    @Property(bool, notify=connectedChanged)
    def connected(self) -> bool:
        return self._connected

    @Property(bool, notify=recognitionEnabledChanged)
    def recognitionEnabled(self) -> bool:
        return self._recognition_enabled

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=statusChanged)
    def statusTitle(self) -> str:
        return self._status_title

    @Property(str, notify=statusChanged)
    def statusDetail(self) -> str:
        return self._status_detail

    @Property(str, notify=statusChanged)
    def statusKind(self) -> str:
        return self._status_kind

    @Property(str, notify=transcriptChanged)
    def transcriptText(self) -> str:
        return self._transcript_text

    @Property(bool, notify=transcriptChanged)
    def transcriptFinal(self) -> bool:
        return self._transcript_final

    @Property(bool, notify=transcriptChanged)
    def transcriptVisible(self) -> bool:
        return self._transcript_visible

    @Property(str, notify=editorChanged)
    def editorText(self) -> str:
        return self._editor_text

    @editorText.setter
    def editorText(self, value: str) -> None:
        value = str(value)
        if value == self._editor_text:
            return
        self._editor_text = value
        self.editorChanged.emit()

    @Property(str, notify=logChanged)
    def logText(self) -> str:
        return "\n".join(self._log_lines)

    @Property(bool, notify=trayAvailableChanged)
    def trayAvailable(self) -> bool:
        return self._tray_available

    # Editable settings ---------------------------------------------------------
    @Property(str, notify=settingsChanged)
    def deviceName(self) -> str:
        return self._device_name

    @deviceName.setter
    def deviceName(self, value: str) -> None:
        self._set_setting("_device_name", str(value), "ring/name")

    @Property(str, notify=settingsChanged)
    def selector(self) -> str:
        return self._selector

    @selector.setter
    def selector(self, value: str) -> None:
        self._set_setting("_selector", str(value), "ring/selector")

    @Property(str, notify=settingsChanged)
    def modelPath(self) -> str:
        return self._model_path

    @modelPath.setter
    def modelPath(self, value: str) -> None:
        self._set_setting("_model_path", str(value), "detector/model")

    @Property(float, notify=settingsChanged)
    def stage1Threshold(self) -> float:
        return self._stage1_threshold

    @stage1Threshold.setter
    def stage1Threshold(self, value: float) -> None:
        self._set_setting(
            "_stage1_threshold", float(value), "detector/stage1Threshold"
        )

    @Property(str, notify=settingsChanged)
    def asrBackend(self) -> str:
        return self._asr_backend

    @asrBackend.setter
    def asrBackend(self, value: str) -> None:
        value = str(value)
        if value == self._asr_backend:
            return
        known_defaults = {
            "",
            "iic/SenseVoiceSmall",
            "seedasr-streaming",
            "FunAudioLLM/Fun-ASR-Nano-2512",
        }
        self._asr_backend = value
        self._settings.setValue("asr/backend", value)
        if self._asr_model in known_defaults:
            self._asr_model = {
                "streaming_sensevoice": "iic/SenseVoiceSmall",
                "volcengine": "seedasr-streaming",
                # Empty lets the backend prefer
                # repo/pretrained_models/Fun-ASR-Nano-2512.
                "funasr_nano": "",
            }.get(value, self._asr_model)
            self._settings.setValue("asr/model", self._asr_model)
        self.settingsChanged.emit()

    @Property(str, notify=settingsChanged)
    def asrModel(self) -> str:
        return self._asr_model

    @asrModel.setter
    def asrModel(self, value: str) -> None:
        self._set_setting("_asr_model", str(value), "asr/model")

    @Property(str, notify=settingsChanged)
    def asrDevice(self) -> str:
        return self._asr_device

    @asrDevice.setter
    def asrDevice(self, value: str) -> None:
        self._set_setting("_asr_device", str(value), "asr/device")

    @Property(str, notify=settingsChanged)
    def asrLanguage(self) -> str:
        return self._asr_language

    @asrLanguage.setter
    def asrLanguage(self, value: str) -> None:
        self._set_setting("_asr_language", str(value), "asr/language")

    @Property(str, notify=settingsChanged)
    def streamingRepo(self) -> str:
        return self._streaming_repo

    @streamingRepo.setter
    def streamingRepo(self, value: str) -> None:
        self._set_setting("_streaming_repo", str(value), "asr/streamingRepo")

    @Property(str, notify=settingsChanged)
    def funasrRepo(self) -> str:
        return self._funasr_repo

    @funasrRepo.setter
    def funasrRepo(self, value: str) -> None:
        self._set_setting("_funasr_repo", str(value), "asr/funasrRepo")

    @Property(bool, notify=settingsChanged)
    def desktopOutputEnabled(self) -> bool:
        return self._desktop_output

    @desktopOutputEnabled.setter
    def desktopOutputEnabled(self, value: bool) -> None:
        self._set_setting("_desktop_output", bool(value), "input/desktopOutput")

    @Property(bool, notify=settingsChanged)
    def pushToTalkEnabled(self) -> bool:
        return self._push_to_talk

    @pushToTalkEnabled.setter
    def pushToTalkEnabled(self, value: bool) -> None:
        self._set_setting("_push_to_talk", bool(value), "input/pushToTalk")

    def _set_setting(self, attr: str, value, key: str) -> None:
        if getattr(self, attr) == value:
            return
        setattr(self, attr, value)
        self._settings.setValue(key, value)
        self.settingsChanged.emit()

    # Commands ------------------------------------------------------------------
    @Slot()
    def connectDevice(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        try:
            settings = self._runtime_settings()
        except BaseException as exc:
            self._set_status("配置有误", str(exc), "error")
            self._append_log(f"配置错误：{exc}")
            return

        self._disconnect_event = threading.Event()
        self._recognition_event = threading.Event()
        self._busy = True
        self.busyChanged.emit()
        self._set_status("正在连接", "正在准备检测模型、语音模型和 Ring 设备", "starting")
        self._append_log("开始连接语音设备")
        runtime = RecognitionRuntime(settings)

        def worker_main() -> None:
            error = ""
            try:
                runtime.run(
                    self._disconnect_event,
                    self._recognition_event,
                    on_update=self._publish_update,
                    on_state=self._runtimeStatus.emit,
                    on_started=self._runtimeStarted.emit,
                    on_push_to_talk=self._pushToTalkChanged.emit,
                )
            except BaseException as exc:
                error = str(exc)
            self._runtimeFinished.emit(error)

        self._worker = threading.Thread(
            target=worker_main,
            name="ProxiMicUiRuntime",
            daemon=True,
        )
        self._worker.start()

    @Slot()
    def startRecognition(self) -> None:
        if not self._connected or self._busy or self._recognition_enabled:
            return
        self._recognition_event.set()
        self._recognition_enabled = True
        self.recognitionEnabledChanged.emit()
        self.runningChanged.emit()
        self._set_status("自动监听中", "靠近说话，或按住 Ctrl+Alt+Space", "running")
        self._append_log("语音识别已开启（设备保持连接）")

    @Slot()
    def pauseRecognition(self) -> None:
        if not self._connected or not self._recognition_enabled:
            return
        self._recognition_event.clear()
        self._recognition_enabled = False
        self._ptt_active = False
        self.recognitionEnabledChanged.emit()
        self.runningChanged.emit()
        self._set_status("识别已暂停", "设备仍保持连接，不会处理语音", "paused")
        self._append_log("语音识别已暂停（设备保持连接）")

    @Slot()
    def toggleRecognition(self) -> None:
        if self._recognition_enabled:
            self.pauseRecognition()
        else:
            self.startRecognition()

    @Slot()
    def disconnectDevice(self) -> None:
        worker = self._worker
        if worker is None or not worker.is_alive():
            return
        self._recognition_event.clear()
        if self._recognition_enabled:
            self._recognition_enabled = False
            self.recognitionEnabledChanged.emit()
            self.runningChanged.emit()
        self._disconnect_event.set()
        self._busy = True
        self.busyChanged.emit()
        self._set_status("正在断开", "正在完成当前识别并释放设备", "stopping")
        self._append_log("正在断开语音设备")

    # Compatibility for older callers.  New UI code uses the explicit methods.
    @Slot()
    def start(self) -> None:
        self.connectDevice()

    @Slot()
    def stop(self) -> None:
        self.disconnectDevice()

    @Slot()
    def clearEditor(self) -> None:
        if not self._editor_text:
            return
        self._editor_text = ""
        self.editorChanged.emit()

    @Slot()
    def clearLog(self) -> None:
        self._log_lines.clear()
        self.logChanged.emit()

    @Slot()
    def requestQuit(self) -> None:
        if self._quitting:
            # A second close/Ctrl+C is an explicit request not to wait for
            # graceful device cleanup any longer.
            QCoreApplication.quit()
            return
        self._quitting = True
        self._quit_wait_ticks = 0
        self.disconnectDevice()
        if self._worker is not None and self._worker.is_alive():
            self._quit_timer.start()
        else:
            QCoreApplication.quit()

    @Slot(bool)
    def setTrayAvailable(self, available: bool) -> None:
        available = bool(available)
        if available == self._tray_available:
            return
        self._tray_available = available
        self.trayAvailableChanged.emit()

    def _finish_quit(self) -> None:
        self._quit_wait_ticks += 1
        if (
            self._worker is None
            or not self._worker.is_alive()
            or self._quit_wait_ticks >= 50
        ):
            self._quit_timer.stop()
            QCoreApplication.quit()

    # Worker event application --------------------------------------------------
    def _publish_update(self, update) -> None:
        self._runtimeUpdate.emit(
            str(update.text or ""), bool(update.is_final), str(update.error or "")
        )

    @Slot(str)
    def _apply_runtime_status(self, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        self._append_log(text)
        if self._status_kind in {"starting", "running", "listening", "stopping"}:
            self._status_detail = text
            self.statusChanged.emit()

    @Slot()
    def _apply_runtime_started(self) -> None:
        self._connected = True
        self._busy = False
        self.connectedChanged.emit()
        self.busyChanged.emit()
        self._set_status("设备已连接", "识别当前暂停，点击“开启语音识别”开始", "paused")
        self._append_log("Ringo 已连接；设备会保持连接直到主动断开")

    @Slot(str, bool, str)
    def _apply_runtime_update(self, text: str, is_final: bool, error: str) -> None:
        if error:
            self._transcript_text = error
            self._transcript_final = True
            self._transcript_visible = True
            self.transcriptChanged.emit()
            self._hide_overlay_timer.start(3500)
            self._append_log(f"ASR 错误：{error}")
            return
        text = text.strip()
        if not text:
            return
        self._transcript_text = text
        self._transcript_final = is_final
        self._transcript_visible = True
        self.transcriptChanged.emit()
        if is_final:
            separator = "" if not self._editor_text or self._editor_text[-1:].isspace() else "\n"
            self._editor_text += separator + text
            self.editorChanged.emit()
            self._hide_overlay_timer.start(1800)
            self._append_log(f"识别完成：{text}")
            if self._recognition_enabled:
                self._set_status("自动监听中", "等待下一段语音", "running")
        else:
            self._hide_overlay_timer.stop()
            if self._recognition_enabled:
                self._set_status("正在识别", text, "listening")

    @Slot(str)
    def _apply_runtime_finished(self, error: str) -> None:
        was_connected = self._connected
        was_recognizing = self._recognition_enabled
        self._connected = False
        self._recognition_enabled = False
        self._busy = False
        self._ptt_active = False
        self._worker = None
        if was_connected:
            self.connectedChanged.emit()
        if was_recognizing:
            self.recognitionEnabledChanged.emit()
            self.runningChanged.emit()
        self.busyChanged.emit()
        if error:
            self._set_status("运行失败", error, "error")
            self._append_log(f"运行失败：{error}")
        elif not self._quitting:
            self._set_status("设备已断开", "设备和模型资源已经释放", "idle")
            self._append_log("语音设备已断开")

    @Slot(bool)
    def _apply_push_to_talk(self, active: bool) -> None:
        if not self._recognition_enabled:
            self._ptt_active = False
            return
        self._ptt_active = bool(active)
        if active:
            self._set_status("按键监听中", "松开后恢复自动控制", "manual")
        elif self._recognition_enabled:
            self._set_status("自动监听中", "已恢复靠近检测", "running")

    def _hide_transcript(self) -> None:
        self._transcript_visible = False
        self.transcriptChanged.emit()

    def _set_status(self, title: str, detail: str, kind: str) -> None:
        self._status_title = title
        self._status_detail = detail
        self._status_kind = kind
        self.statusChanged.emit()

    def _append_log(self, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        self._log_lines.append(text)
        del self._log_lines[:-200]
        self.logChanged.emit()

    def _runtime_settings(self) -> RuntimeSettings:
        model = self._path_or_none(self._model_path)
        if model is not None and not model.is_file():
            raise ValueError(f"检测模型不存在：{model}")
        repo = self._path_or_none(self._streaming_repo)
        if (
            self._asr_backend == "streaming_sensevoice"
            and repo is not None
            and not repo.is_dir()
        ):
            raise ValueError(f"streaming-sensevoice 目录不存在：{repo}")
        funasr_repo = self._path_or_none(self._funasr_repo)
        if self._asr_backend == "funasr_nano":
            if funasr_repo is None:
                raise ValueError("Fun-ASR-Nano 必须配置 Fun-ASR-main 目录")
            if not (funasr_repo / "model.py").is_file():
                raise ValueError(f"Fun-ASR 目录中没有 model.py：{funasr_repo}")
        if not self._device_name.strip():
            raise ValueError("设备名称不能为空")
        if self._stage1_threshold <= 0:
            raise ValueError("Stage1 threshold 必须大于 0")
        return RuntimeSettings(
            ring_name=self._device_name.strip(),
            ring_selector=self._selector.strip() or None,
            detector_model=model,
            stage1_threshold=self._stage1_threshold,
            asr_backend=self._asr_backend,
            asr_model=self._asr_model.strip(),
            asr_device=self._asr_device.strip(),
            asr_language=self._asr_language,
            streaming_sensevoice_repo=repo,
            funasr_nano_repo=funasr_repo,
            desktop_output=self._desktop_output,
            push_to_talk=self._push_to_talk,
        )

    @staticmethod
    def _path_or_none(value: str) -> Path | None:
        text = str(value).strip()
        if not text:
            return None
        path = Path(text).expanduser()
        return path if path.is_absolute() else Path.cwd() / path
