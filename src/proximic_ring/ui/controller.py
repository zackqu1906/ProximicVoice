from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, Property, QCoreApplication, QSettings, QTimer, Signal, Slot

from ..app_runtime import (
    WINDOWS_DESKTOP_INPUT_SUPPORTED,
    RecognitionRuntime,
    RuntimeSettings,
)


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
    devicesChanged = Signal()
    scanBusyChanged = Signal()
    deviceSearchChanged = Signal()
    devicePickerRequested = Signal()

    _runtimeStatus = Signal(str)
    _runtimeStarted = Signal()
    _runtimeUpdate = Signal(str, bool, str)
    _runtimeFinished = Signal(str)
    _pushToTalkChanged = Signal(bool)
    _scanFinished = Signal(object, str)

    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings("ProxiMic", "ProxiMic Voice")
        self._connected = False
        self._recognition_enabled = False
        self._busy = False
        self._scan_busy = False
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
        self._scan_worker: threading.Thread | None = None
        self._available_devices: list[dict[str, object]] = []
        self._device_search = "Ringo"
        self._discovery_active = False
        self._pending_device_connection = False
        self._scan_message = "打开设备列表后会实时发现附近的蓝牙设备"
        self._disconnect_event = threading.Event()
        self._recognition_event = threading.Event()

        # Resolve bundled resources from the installed source tree instead of
        # depending on the terminal's current working directory.
        project_root = Path(__file__).resolve().parents[3]
        self._project_root = project_root
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
        self._nvidia_gpu_name = self._detect_nvidia_gpu_name()
        self._compute_devices, self._gpu_status_text = self._detect_compute_devices(
            self._nvidia_gpu_name
        )
        default_asr_device = next(
            (
                str(item["value"])
                for item in self._compute_devices
                if str(item["value"]).startswith("cuda:")
            ),
            "cpu",
        )
        self._asr_device = str(
            self._settings.value("asr/device", default_asr_device)
        )
        available_device_values = {
            str(item["value"]) for item in self._compute_devices
        }
        if self._asr_device not in available_device_values:
            self._asr_device = "cpu"
            self._settings.setValue("asr/device", self._asr_device)
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
        self._desktop_output = (
            WINDOWS_DESKTOP_INPUT_SUPPORTED
            and self._bool_setting("input/desktopOutput", True)
        )
        self._push_to_talk = (
            WINDOWS_DESKTOP_INPUT_SUPPORTED
            and self._bool_setting("input/pushToTalk", True)
        )

        self._hide_overlay_timer = QTimer(self)
        self._hide_overlay_timer.setSingleShot(True)
        self._hide_overlay_timer.timeout.connect(self._hide_transcript)
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(100)
        self._quit_timer.timeout.connect(self._finish_quit)
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(600)
        self._rescan_timer.timeout.connect(self._scan_devices_once)

        self._runtimeStatus.connect(self._apply_runtime_status)
        self._runtimeStarted.connect(self._apply_runtime_started)
        self._runtimeUpdate.connect(self._apply_runtime_update)
        self._runtimeFinished.connect(self._apply_runtime_finished)
        self._pushToTalkChanged.connect(self._apply_push_to_talk)
        self._scanFinished.connect(self._apply_scan_finished)

    @staticmethod
    def _detect_compute_devices(
        nvidia_gpu_name: str = "",
    ) -> tuple[list[dict[str, str]], str]:
        devices = [{"label": "CPU（兼容性最佳）", "value": "cpu"}]
        try:
            import torch

            if torch.cuda.is_available():
                count = int(torch.cuda.device_count())
                for index in range(count):
                    try:
                        name = str(torch.cuda.get_device_name(index)).strip()
                    except BaseException:
                        name = "NVIDIA GPU"
                    devices.append(
                        {
                            "label": f"GPU {index + 1} · {name}",
                            "value": f"cuda:{index}",
                        }
                    )
                return devices, f"检测到 {count} 张可用的 NVIDIA GPU，切换后下次连接生效。"
        except BaseException:
            pass

        if sys.platform == "darwin":
            message = "macOS 当前使用 CPU；ASR 的 Apple GPU 加速尚未开放。"
        elif nvidia_gpu_name:
            message = (
                f"检测到 {nvidia_gpu_name}，但当前是 CPU 版 PyTorch。"
                "可以在下方安装 NVIDIA GPU 加速。"
            )
        else:
            message = (
                "未检测到可用的 NVIDIA GPU；如本机有独立显卡，请安装 CUDA 版 "
                "PyTorch 后重启应用。"
            )
        return devices, message

    @staticmethod
    def _detect_nvidia_gpu_name() -> str:
        if sys.platform != "win32":
            return ""
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                ["nvidia-smi.exe", "--query-gpu=name", "--format=csv,noheader"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if result.returncode != 0:
            return ""
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return ", ".join(names)

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

    @Property(bool, notify=scanBusyChanged)
    def scanBusy(self) -> bool:
        return self._scan_busy

    @Property("QVariantList", notify=devicesChanged)
    def availableDevices(self) -> list[dict[str, object]]:
        query = self._device_search.strip().casefold()
        if not query:
            return list(self._available_devices)
        return [
            item
            for item in self._available_devices
            if query in str(item.get("name", "")).casefold()
            or query in str(item.get("identifier", "")).casefold()
        ]

    @Property(str, notify=deviceSearchChanged)
    def deviceSearch(self) -> str:
        return self._device_search

    @deviceSearch.setter
    def deviceSearch(self, value: str) -> None:
        value = str(value)
        if value == self._device_search:
            return
        self._device_search = value
        self.deviceSearchChanged.emit()
        self._refresh_scan_message()
        self.devicesChanged.emit()

    @Property(str, notify=devicesChanged)
    def scanMessage(self) -> str:
        return self._scan_message

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
        known_defaults = {
            "",
            "iic/SenseVoiceSmall",
            "seedasr-streaming",
            "FunAudioLLM/Fun-ASR-Nano-2512",
        }
        backend_changed = value != self._asr_backend
        model_changed = False
        if self._asr_model in known_defaults:
            model = {
                "streaming_sensevoice": "iic/SenseVoiceSmall",
                "volcengine": "seedasr-streaming",
                # Empty lets the backend prefer
                # repo/pretrained_models/Fun-ASR-Nano-2512.
                "funasr_nano": "",
            }.get(value, self._asr_model)
            model_changed = model != self._asr_model
            self._asr_model = model

        if not backend_changed and not model_changed:
            return
        self._asr_backend = value
        self._settings.setValue("asr/backend", value)
        if model_changed:
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

    @Property("QVariantList", constant=True)
    def computeDevices(self) -> list[dict[str, str]]:
        return list(self._compute_devices)

    @Property(str, constant=True)
    def gpuStatusText(self) -> str:
        return self._gpu_status_text

    @Property(bool, constant=True)
    def gpuInstallerAvailable(self) -> bool:
        if sys.platform != "win32" or not self._nvidia_gpu_name:
            return False
        if any(
            str(item["value"]).startswith("cuda:")
            for item in self._compute_devices
        ):
            return False
        return (self._project_root / "scripts" / "install-gpu.ps1").is_file()

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
        self._set_setting(
            "_desktop_output",
            bool(value) and WINDOWS_DESKTOP_INPUT_SUPPORTED,
            "input/desktopOutput",
        )

    @Property(bool, notify=settingsChanged)
    def pushToTalkEnabled(self) -> bool:
        return self._push_to_talk

    @pushToTalkEnabled.setter
    def pushToTalkEnabled(self, value: bool) -> None:
        self._set_setting(
            "_push_to_talk",
            bool(value) and WINDOWS_DESKTOP_INPUT_SUPPORTED,
            "input/pushToTalk",
        )

    def _set_setting(self, attr: str, value, key: str) -> None:
        if getattr(self, attr) == value:
            return
        setattr(self, attr, value)
        self._settings.setValue(key, value)
        self.settingsChanged.emit()

    # Commands ------------------------------------------------------------------
    @Slot()
    def installGpuSupport(self) -> None:
        if not self.gpuInstallerAvailable:
            self._set_status(
                "无法安装 GPU 加速",
                "未检测到可升级的 NVIDIA GPU 环境",
                "error",
            )
            return
        if self._connected or self._busy:
            self._set_status(
                "请先断开设备",
                "安装 GPU 运行库前需要停止当前识别和设备连接",
                "error",
            )
            return

        script = self._project_root / "scripts" / "install-gpu.ps1"
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-WaitForProcessId",
            str(QCoreApplication.applicationPid()),
            "-Restart",
            "-Interactive",
        ]
        try:
            subprocess.Popen(
                command,
                cwd=str(self._project_root),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        except OSError as exc:
            self._set_status("无法启动安装程序", str(exc), "error")
            self._append_log(f"GPU 安装程序启动失败：{exc}")
            return

        self._append_log("已启动 NVIDIA GPU 安装程序，应用即将退出")
        self._set_status(
            "正在切换到 GPU 版本",
            "请在安装窗口中查看进度，完成后应用会自动重启",
            "stopping",
        )
        QTimer.singleShot(250, self.requestQuit)

    @Slot()
    def connectDevice(self) -> None:
        """Open the device picker instead of connecting by a name heuristic."""
        self.requestDevicePicker()

    @Slot()
    def requestDevicePicker(self) -> None:
        if self._connected or self._busy:
            return
        self.devicePickerRequested.emit()
        self.scanDevices()

    @Slot()
    def scanDevices(self) -> None:
        if self._connected or self._busy or self._scan_busy:
            return
        self._discovery_active = True
        self._pending_device_connection = False
        self._rescan_timer.stop()
        self._available_devices = []
        self._scan_message = "正在实时发现附近的蓝牙设备…"
        self.devicesChanged.emit()
        self._set_status("正在扫描", "请选择列表中的设备进行连接", "starting")
        self._append_log("开始实时发现附近的蓝牙设备（不按名称过滤）")
        self._scan_devices_once()

    @Slot()
    def stopDeviceDiscovery(self) -> None:
        self._discovery_active = False
        self._rescan_timer.stop()

    @Slot()
    def _scan_devices_once(self) -> None:
        if (
            not self._discovery_active
            or self._connected
            or self._busy
            or self._scan_busy
        ):
            return
        worker = self._scan_worker
        if worker is not None and worker.is_alive():
            return

        self._scan_busy = True
        self.scanBusyChanged.emit()
        self._refresh_scan_message()

        timeout_s = 3.0

        def scan_main() -> None:
            rows: list[dict[str, object]] = []
            error = ""
            try:
                from ring_python_sdk.ble import scan_all_devices

                discovered = asyncio.run(scan_all_devices(timeout_s))
                rows = [
                    {
                        "name": item.name or "未命名设备",
                        "identifier": item.identifier,
                        "rssi": item.rssi if item.rssi is not None else "",
                    }
                    for item in discovered
                ]
            except BaseException as exc:
                error = str(exc)
            self._scanFinished.emit(rows, error)

        self._scan_worker = threading.Thread(
            target=scan_main,
            name="ProxiMicBleScan",
            daemon=True,
        )
        self._scan_worker.start()

    @Slot(str, str)
    def connectToDevice(self, identifier: str, name: str) -> None:
        if self._connected or self._busy:
            return
        identifier = str(identifier).strip()
        if not identifier:
            self._set_status("无法连接", "设备标识为空，请重新扫描", "error")
            return
        display_name = str(name).strip() or "未命名设备"
        self._selector = identifier
        self._device_name = display_name
        self._settings.setValue("ring/selector", identifier)
        self._settings.setValue("ring/name", display_name)
        self.settingsChanged.emit()
        self.stopDeviceDiscovery()
        if self._scan_busy:
            self._pending_device_connection = True
            self._set_status(
                "正在连接",
                f"正在结束扫描并连接 {self._device_name}",
                "starting",
            )
            return
        self._start_selected_device()

    def _start_selected_device(self) -> None:
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
        self._set_status(
            "正在连接",
            f"正在准备模型并连接 {self._device_name}",
            "starting",
        )
        self._append_log(f"开始连接设备：{self._device_name} ({self._selector})")
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
        detail = "靠近说话"
        if self._push_to_talk:
            detail += "，或按住 Ctrl+Alt+Space"
        self._set_status("自动监听中", detail, "running")
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
        self.stopDeviceDiscovery()
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

    @Slot(object, str)
    def _apply_scan_finished(self, devices: object, error: str) -> None:
        self._scan_worker = None
        self._scan_busy = False
        self.scanBusyChanged.emit()

        rows = list(devices) if isinstance(devices, list) else []
        added = self._merge_discovered_devices(rows)

        if self._pending_device_connection:
            self._pending_device_connection = False
            self._start_selected_device()
            return

        # The picker may have been cancelled while the platform scan was still
        # finishing.  Do not change the main-window status after it is closed.
        if not self._discovery_active:
            return

        if error:
            if self._discovery_active:
                self._scan_message = f"本轮扫描失败，稍后自动重试：{error}"
                self.devicesChanged.emit()
                self._append_log(f"蓝牙扫描本轮失败，将自动重试：{error}")
                self._rescan_timer.start(1200)
                return
            self._scan_message = f"扫描失败：{error}"
            self.devicesChanged.emit()
            self._set_status("扫描失败", error, "error")
            self._append_log(f"蓝牙扫描失败：{error}")
            return

        count = len(self._available_devices)
        if count:
            self._refresh_scan_message()
            self._set_status("请选择设备", self._scan_message, "idle")
            if added:
                self._append_log(f"发现 {added} 个新设备，当前共 {count} 个")
        else:
            self._scan_message = (
                "正在实时发现设备，请确认设备可被发现以及系统蓝牙权限"
                if self._discovery_active
                else "没有发现蓝牙设备，请确认蓝牙权限后重新扫描"
            )
            self._set_status("未发现设备", self._scan_message, "idle")
        if added or not count:
            self.devicesChanged.emit()
        if self._discovery_active and not self._connected and not self._busy:
            self._rescan_timer.start()

    def _merge_discovered_devices(self, rows: list[object]) -> int:
        """Merge scan results without moving rows already visible to the user."""
        existing = {
            str(item.get("identifier", "")).casefold(): item
            for item in self._available_devices
        }
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get("identifier", "")).strip()
            if not identifier:
                continue
            key = identifier.casefold()
            current = existing.get(key)
            if current is None:
                current = {
                    "name": str(row.get("name", "")).strip() or "未命名设备",
                    "identifier": identifier,
                    "rssi": row.get("rssi", ""),
                }
                self._available_devices.append(current)
                existing[key] = current
                added += 1
            else:
                current["name"] = (
                    str(row.get("name", "")).strip()
                    or str(current.get("name", ""))
                    or "未命名设备"
                )
                current["rssi"] = row.get("rssi", current.get("rssi", ""))
        return added

    def _refresh_scan_message(self) -> None:
        total = len(self._available_devices)
        visible = len(self.availableDevices)
        query = self._device_search.strip()
        if query:
            message = f"已发现 {total} 个设备，显示 {visible} 个匹配“{query}”的设备"
        else:
            message = f"已发现 {total} 个设备"
        if self._discovery_active:
            message += "；列表正在实时更新"
        self._scan_message = message

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
        if not self._selector.strip():
            raise ValueError("请先扫描并选择要连接的设备")
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
