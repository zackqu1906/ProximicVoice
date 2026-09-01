from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid

from PySide6.QtCore import QObject, Property, QCoreApplication, QSettings, QTimer, Signal, Slot

from ..asr import ASRBackendCache
from ..app_runtime import (
    WINDOWS_DESKTOP_INPUT_SUPPORTED,
    RecognitionRuntime,
    RuntimeSettings,
    normalize_funasr_nano_hotwords,
)
from ..desktop_target import DesktopTargetRef, DesktopTextSnapshot
from ..model_packages import install_default_local_model
from ..modification_dataset import (
    FEEDBACK_REASON_LABELS,
    ModificationDatasetCollector,
    PROMPT_VERSION,
)
from ..runtime_paths import app_data_root, is_frozen, resource_root
from ..text_processing import (
    DEFAULT_ARK_API_KEY_ENV,
    DEFAULT_ARK_BASE_URL,
    DEFAULT_ARK_MODEL,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_CONTEXT_SIZE,
    DEFAULT_LOCAL_MODEL,
    DEFAULT_LOCAL_MODEL_PATH,
    DEFAULT_LOCAL_REASONING,
    DEFAULT_LOCAL_SERVER_PATH,
    INPUT_MODE_DICTATION,
    INPUT_MODE_EDIT,
    INPUT_ROUTING_AUTO,
    INPUT_ROUTING_MANUAL,
    InputModeRoutingRequest,
    InputModeRoutingResult,
    LLM_PROVIDER_LOCAL,
    LLM_PROVIDER_OPENAI,
    LLM_PROVIDER_VOLCENGINE,
    LLMSettings,
    LLMTraceCollection,
    TextProcessingRequest,
    TextProcessingResult,
    TextProcessingWorker,
    normalize_input_mode,
    normalize_input_routing_mode,
    normalize_llm_provider,
    validate_edit_target_text,
)
from ..voice_actions import (
    ACTION_CANCEL,
    ACTION_CONFIRM,
    ACTION_EDIT,
    ACTION_INPUT,
    ACTION_REASON_ASR_ERROR,
    ACTION_REASON_LLM_ERROR,
    ACTION_REASON_OTHER,
    ACTION_RETRY,
)


@dataclass
class _PendingInteraction:
    target: DesktopTargetRef | None = None
    snapshot: DesktopTextSnapshot | None = None


@dataclass
class _PendingModeRoute:
    target: DesktopTargetRef | None = None


@dataclass
class _EditReview:
    request_id: int
    session_id: int
    instruction: str
    proposed_text: str
    snapshot: DesktopTextSnapshot
    failure_error: str = ""


@dataclass(frozen=True)
class _PendingFeedbackReason:
    request_id: int
    action: str


_EDIT_DIFF_COLOR = "#FF646F"


def _edit_preview_html(original_text: str, modified_text: str) -> str:
    """Render the proposed text, highlighting its changed parts for QML."""

    def render_text(value: str) -> str:
        return escape(value).replace("\n", "<br/>")

    original = str(original_text or "")
    modified = str(modified_text or "")
    if not modified:
        return (
            f'<span style="color:{_EDIT_DIFF_COLOR};">'
            "（修改后为空，将清空原文）</span>"
        )

    chunks: list[str] = []
    deleted_parts: list[str] = []
    matcher = SequenceMatcher(None, original, modified)
    for (
        tag,
        original_start,
        original_end,
        modified_start,
        modified_end,
    ) in matcher.get_opcodes():
        new_part = modified[modified_start:modified_end]
        if tag == "equal":
            chunks.append(render_text(new_part))
        elif new_part:
            chunks.append(
                f'<span style="color:{_EDIT_DIFF_COLOR};">'
                f"{render_text(new_part)}</span>"
            )
        if tag == "delete":
            deleted_parts.append(original[original_start:original_end])

    if deleted_parts:
        deleted = "…".join(deleted_parts)
        if len(deleted) > 120:
            deleted = f"{deleted[:120]}…"
        chunks.append(
            f'<br/><span style="color:{_EDIT_DIFF_COLOR};">'
            f"已删除：{render_text(deleted)}</span>"
        )
    return "".join(chunks)


def _is_explicit_emptying_edit_response(
    response: object,
    expected_original: str,
) -> bool:
    """Return whether either validated edit contract clears all text."""

    if not isinstance(response, dict) or response.get("modified_text") != "":
        return False
    # The full-text race contract intentionally has no original_text field.
    if set(response) == {"modified_text"}:
        return True
    return (
        isinstance(response.get("original_text"), str)
        and response["original_text"] == str(expected_original or "")
        and bool(response["original_text"])
    )


class AppController(QObject):
    runningChanged = Signal()
    connectedChanged = Signal()
    recognitionEnabledChanged = Signal()
    busyChanged = Signal()
    statusChanged = Signal()
    transcriptChanged = Signal()
    sessionHistoryChanged = Signal()
    interactionChanged = Signal()
    logChanged = Signal()
    settingsChanged = Signal()
    trayAvailableChanged = Signal()
    devicesChanged = Signal()
    scanBusyChanged = Signal()
    deviceSearchChanged = Signal()
    devicePickerRequested = Signal()
    reconnectAvailabilityChanged = Signal()
    inputModeChanged = Signal()
    inputRoutingModeChanged = Signal()
    textProcessingChanged = Signal()
    localModelInstallationChanged = Signal()
    feedbackReasonChanged = Signal()

    _runtimeStatus = Signal(str)
    _runtimeConnected = Signal()
    _runtimeDisconnected = Signal()
    _runtimeStarted = Signal()
    _runtimeUpdate = Signal(str, bool, str, int)
    _runtimeFinished = Signal(str)
    _pushToTalkChanged = Signal(bool)
    _scanFinished = Signal(object, str)
    _textProcessed = Signal(object)
    _inputModeRouted = Signal(object)
    _llmTraceCollected = Signal(object)
    _llmWarmupFinished = Signal(str, float)
    _localModelInstallProgress = Signal(str, int, int)
    _localModelInstallFinished = Signal(object, str)
    _voiceActionRequested = Signal(str)

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
        self._transcript_mode = ""
        self._edit_preview_html = ""
        self._transcript_final = False
        self._transcript_visible = False
        self._session_history_lines: list[str] = []
        self._interaction_state = "idle"
        self._edit_review: _EditReview | None = None
        self._retry_snapshot: DesktopTextSnapshot | None = None
        self._retry_target_armed = False
        self._pending_feedback_reason: _PendingFeedbackReason | None = None
        self._feedback_reason_visible = False
        self._log_lines: list[str] = []
        self._ptt_active = False
        self._input_mode = normalize_input_mode(
            str(self._settings.value("input/mode", INPUT_MODE_DICTATION))
        )
        self._input_routing_mode = normalize_input_routing_mode(
            str(
                self._settings.value(
                    "input/routingMode", INPUT_ROUTING_MANUAL
                )
            )
        )
        # This switch controls only optional post-processing for dictation.
        # Edit mode always needs the selected text model.
        self._llm_enabled = self._bool_setting("llm/enabled", True)
        saved_llm_provider = str(self._settings.value("llm/provider", "")).strip()
        saved_llm_base_url = str(self._settings.value("llm/baseUrl", "")).strip()
        if saved_llm_provider:
            self._llm_provider = normalize_llm_provider(saved_llm_provider)
        elif saved_llm_base_url:
            local_hosts = ("http://127.0.0.1", "http://localhost", "http://[::1]")
            self._llm_provider = (
                LLM_PROVIDER_LOCAL
                if saved_llm_base_url.lower().startswith(local_hosts)
                else LLM_PROVIDER_OPENAI
            )
        else:
            self._llm_provider = LLM_PROVIDER_LOCAL
        self._llm_base_url = saved_llm_base_url or (
            DEFAULT_LOCAL_BASE_URL
            if self._llm_provider == LLM_PROVIDER_LOCAL
            else (
                DEFAULT_ARK_BASE_URL
                if self._llm_provider == LLM_PROVIDER_VOLCENGINE
                else "https://api.openai.com/v1"
            )
        )
        default_llm_model = (
            DEFAULT_LOCAL_MODEL
            if self._llm_provider == LLM_PROVIDER_LOCAL
            else (
                DEFAULT_ARK_MODEL
                if self._llm_provider == LLM_PROVIDER_VOLCENGINE
                else "gpt-5.6-luna"
            )
        )
        self._llm_model = str(
            self._settings.value("llm/model", default_llm_model)
        ).strip()
        default_key_env = ""
        if self._llm_provider == LLM_PROVIDER_VOLCENGINE:
            default_key_env = DEFAULT_ARK_API_KEY_ENV
        elif self._llm_provider != LLM_PROVIDER_LOCAL:
            default_key_env = "OPENAI_API_KEY"
        self._llm_api_key_env = str(
            self._settings.value("llm/apiKeyEnv", default_key_env)
        ).strip()
        self._llm_api_key = str(self._settings.value("llm/apiKey", "")).strip()
        self._llm_local_server_path = str(
            self._settings.value(
                "llm/localServerPath",
                os.environ.get("LOCAL_LLM_SERVER_PATH", DEFAULT_LOCAL_SERVER_PATH),
            )
        )
        self._llm_local_model_path = str(
            self._settings.value(
                "llm/localModelPath",
                os.environ.get("LOCAL_LLM_MODEL_PATH", DEFAULT_LOCAL_MODEL_PATH),
            )
        )
        self._local_model_installing = False
        self._local_model_install_status = ""
        try:
            saved_llm_timeout = float(
                self._settings.value("llm/timeoutSeconds", 30.0)
            )
        except (TypeError, ValueError):
            saved_llm_timeout = 30.0
        self._llm_timeout_s = max(1.0, min(saved_llm_timeout, 300.0))
        self._text_request_id = 0
        self._llm_warmup_requested = False
        self._pending_text_requests: set[int] = set()
        self._pending_interactions: dict[int, _PendingInteraction] = {}
        self._pending_mode_routes: set[int] = set()
        self._pending_mode_route_contexts: dict[int, _PendingModeRoute] = {}
        self._session_input_modes: dict[int, str] = {}
        self._session_routing_modes: dict[int, str] = {}
        self._session_targets: dict[int, DesktopTargetRef | None] = {}
        self._desktop_target = None
        self._worker: threading.Thread | None = None
        self._runtime_active = False
        self._runtime_had_connection = False
        self._asr_backend_cache = ASRBackendCache()
        self._scan_worker: threading.Thread | None = None
        self._available_devices: list[dict[str, object]] = []
        self._device_handles: dict[str, object] = {}
        self._selected_device: object | None = None
        self._device_search = "Ringo"
        self._discovery_active = False
        self._pending_device_connection = False
        # A persisted selector is only a convenience for settings.  "Reconnect"
        # becomes available after the user has selected/attempted a device in
        # this application session, never merely because the app just opened.
        self._can_reconnect = False
        self._scan_message = "打开设备列表后会扫描附近的蓝牙设备"
        self._disconnect_event = threading.Event()
        self._recognition_event = threading.Event()

        # Resolve bundled resources from the installed source tree instead of
        # depending on the terminal's current working directory.
        project_root = resource_root()
        self._project_root = project_root
        anonymous_user_id = str(
            self._settings.value("dataCollection/anonymousUserId", "")
        ).strip()
        if not anonymous_user_id:
            anonymous_user_id = f"user_{uuid.uuid4().hex}"
            self._settings.setValue(
                "dataCollection/anonymousUserId", anonymous_user_id
            )
        self._modification_dataset = ModificationDatasetCollector(
            app_data_root() / "dataset", anonymous_user_id
        )
        assets = Path(__file__).resolve().parents[1] / "assets"
        default_model = assets / "ringo-near-v1.model"
        default_repo = project_root / "third_party" / "streaming-sensevoice"
        default_funasr_repo = project_root / "third_party" / "Fun-ASR"
        self._device_name = str(self._settings.value("ring/name", "Ringo"))
        self._selector = str(self._settings.value("ring/selector", ""))
        try:
            encoding_default_version = int(
                self._settings.value("ring/audioEncodingDefaultVersion", 0)
            )
        except (TypeError, ValueError):
            encoding_default_version = 0
        if encoding_default_version < 2:
            # Move existing installations to the transport verified by the
            # firmware receiver.  Users can still explicitly select PCM/ADPCM
            # after this one-time reliability migration.
            saved_audio_encoding = "opus"
            self._settings.setValue("ring/audioEncoding", saved_audio_encoding)
            self._settings.setValue("ring/audioEncodingDefaultVersion", 2)
        else:
            saved_audio_encoding = str(
                self._settings.value("ring/audioEncoding", "opus")
            ).strip().lower()
        self._audio_encoding = (
            saved_audio_encoding
            if saved_audio_encoding in {"adpcm", "pcm", "opus"}
            else "opus"
        )
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
        self._asr_api_key = str(
            self._settings.value("asr/volcengineApiKey", "")
        ).strip()
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
        self._funasr_hotwords = "\n".join(
            normalize_funasr_nano_hotwords(
                str(self._settings.value("asr/funasrNanoHotwords", ""))
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
        self._feedback_reason_timer = QTimer(self)
        self._feedback_reason_timer.setSingleShot(True)
        self._feedback_reason_timer.setInterval(10_000)
        self._feedback_reason_timer.timeout.connect(self._clear_feedback_reason)
        self._feedback_reason_reveal_timer = QTimer(self)
        self._feedback_reason_reveal_timer.setSingleShot(True)
        self._feedback_reason_reveal_timer.setInterval(180)
        self._feedback_reason_reveal_timer.timeout.connect(
            self._reveal_feedback_reason
        )
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(100)
        self._quit_timer.timeout.connect(self._finish_quit)
        self._runtimeStatus.connect(self._apply_runtime_status)
        self._runtimeConnected.connect(self._apply_runtime_connected)
        self._runtimeDisconnected.connect(self._apply_runtime_disconnected)
        self._runtimeStarted.connect(self._apply_runtime_started)
        self._runtimeUpdate.connect(self._apply_runtime_update)
        self._runtimeFinished.connect(self._apply_runtime_finished)
        self._pushToTalkChanged.connect(self._apply_push_to_talk)
        self._scanFinished.connect(self._apply_scan_finished)
        self._textProcessed.connect(self._apply_text_processed)
        self._inputModeRouted.connect(self._apply_input_mode_routed)
        self._llmTraceCollected.connect(self._apply_llm_trace_collected)
        self._llmWarmupFinished.connect(self._apply_llm_warmup_finished)
        self._localModelInstallProgress.connect(
            self._apply_local_model_install_progress
        )
        self._localModelInstallFinished.connect(
            self._apply_local_model_install_finished
        )
        self._voiceActionRequested.connect(self._apply_voice_action)
        self._text_processing_worker = TextProcessingWorker(
            on_result=self._textProcessed.emit,
            on_routing_result=self._inputModeRouted.emit,
            on_trace=self._llmTraceCollected.emit,
            on_warmup=lambda error, latency: self._llmWarmupFinished.emit(
                error or "", latency
            ),
        )

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

    @Property(bool, notify=settingsChanged)
    def hasSelectedDevice(self) -> bool:
        return bool(self._selector.strip())

    @Property(bool, notify=reconnectAvailabilityChanged)
    def canReconnect(self) -> bool:
        return self._can_reconnect and bool(self._selector.strip())

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

    @Property(str, notify=transcriptChanged)
    def transcriptMode(self) -> str:
        """Resolved mode for the utterance currently shown in the overlay."""

        return self._transcript_mode

    @Property(str, notify=transcriptChanged)
    def editPreviewHtml(self) -> str:
        return self._edit_preview_html

    @Property(bool, notify=transcriptChanged)
    def transcriptFinal(self) -> bool:
        return self._transcript_final

    @Property(bool, notify=transcriptChanged)
    def transcriptVisible(self) -> bool:
        return self._transcript_visible

    @Property(str, notify=sessionHistoryChanged)
    def sessionHistoryText(self) -> str:
        return "\n\n".join(self._session_history_lines)

    @Property(str, notify=interactionChanged)
    def interactionState(self) -> str:
        return self._interaction_state

    @Property(bool, notify=interactionChanged)
    def reviewPending(self) -> bool:
        return self._edit_review is not None

    @Property(bool, notify=interactionChanged)
    def reviewFailed(self) -> bool:
        return bool(self._edit_review and self._edit_review.failure_error)

    @Property(bool, notify=interactionChanged)
    def reviewCanConfirm(self) -> bool:
        return bool(self._edit_review and not self._edit_review.failure_error)

    @Property(bool, notify=feedbackReasonChanged)
    def feedbackReasonVisible(self) -> bool:
        return self._feedback_reason_visible

    @Property(bool, notify=feedbackReasonChanged)
    def feedbackReasonAvailable(self) -> bool:
        """Whether a reason can bind, including the brief visual refresh gap."""
        return self._pending_feedback_reason is not None

    @Property(str, notify=feedbackReasonChanged)
    def feedbackReasonPrompt(self) -> str:
        pending = self._pending_feedback_reason
        if pending is not None and pending.action == "cancel":
            return "刚才为什么取消？"
        return "刚才为什么重说？"

    @Property(str, notify=logChanged)
    def logText(self) -> str:
        return "\n".join(self._log_lines)

    @Property(bool, notify=trayAvailableChanged)
    def trayAvailable(self) -> bool:
        return self._tray_available

    @Property(str, notify=inputModeChanged)
    def inputMode(self) -> str:
        return self._input_mode

    @inputMode.setter
    def inputMode(self, value: str) -> None:
        mode = normalize_input_mode(value)
        if mode == self._input_mode:
            return
        self._input_mode = mode
        self._settings.setValue("input/mode", mode)
        self.inputModeChanged.emit()
        label = "修改" if mode == INPUT_MODE_EDIT else "输入"
        self._append_log(f"输入模式已切换为：{label}")

    @Property(str, notify=inputRoutingModeChanged)
    def inputRoutingMode(self) -> str:
        return self._input_routing_mode

    @inputRoutingMode.setter
    def inputRoutingMode(self, value: str) -> None:
        mode = normalize_input_routing_mode(value)
        if mode == self._input_routing_mode:
            return
        self._input_routing_mode = mode
        self._settings.setValue("input/routingMode", mode)
        self.inputRoutingModeChanged.emit()
        label = "自动判断" if mode == INPUT_ROUTING_AUTO else "手动切换"
        self._append_log(f"听写/指令路由已切换为：{label}")

    @Property(bool, notify=textProcessingChanged)
    def textProcessing(self) -> bool:
        return bool(self._pending_text_requests or self._pending_mode_routes)

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
    def audioEncoding(self) -> str:
        return self._audio_encoding

    @audioEncoding.setter
    def audioEncoding(self, value: str) -> None:
        normalized = str(value).strip().lower()
        if normalized not in {"adpcm", "pcm", "opus"}:
            return
        self._set_setting("_audio_encoding", normalized, "ring/audioEncoding")

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
    def asrApiKey(self) -> str:
        return self._asr_api_key

    @asrApiKey.setter
    def asrApiKey(self, value: str) -> None:
        self._set_setting(
            "_asr_api_key",
            str(value).strip(),
            "asr/volcengineApiKey",
        )

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
        if is_frozen() or sys.platform != "win32" or not self._nvidia_gpu_name:
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

    @Property(str, notify=settingsChanged)
    def asrHotwords(self) -> str:
        return self._funasr_hotwords

    @asrHotwords.setter
    def asrHotwords(self, value: str) -> None:
        normalized = "\n".join(normalize_funasr_nano_hotwords(str(value)))
        self._set_setting(
            "_funasr_hotwords",
            normalized,
            "asr/funasrNanoHotwords",
        )

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

    @Property(bool, notify=settingsChanged)
    def llmEnabled(self) -> bool:
        return self._llm_enabled

    @llmEnabled.setter
    def llmEnabled(self, value: bool) -> None:
        self._set_setting("_llm_enabled", bool(value), "llm/enabled")

    @Slot()
    def toggleDictationLlm(self) -> None:
        """Toggle optional LLM post-processing for input mode only."""

        self.llmEnabled = not self._llm_enabled
        state = "开启" if self._llm_enabled else "关闭"
        self._append_log(f"输入模式文本 LLM 整理已{state}")

    @Property(str, notify=settingsChanged)
    def llmProvider(self) -> str:
        return self._llm_provider

    @llmProvider.setter
    def llmProvider(self, value: str) -> None:
        provider = normalize_llm_provider(value)
        if provider == self._llm_provider:
            return
        previous = self._llm_provider
        self._llm_provider = provider
        self._settings.setValue("llm/provider", provider)
        if provider == LLM_PROVIDER_LOCAL:
            # Keep the remote profile intact so switching back online restores
            # the user's Ark endpoint, model ID, key and compatibility fallback.
            pass
        elif previous == LLM_PROVIDER_LOCAL:
            if not self._llm_base_url.strip() or self._llm_base_url == DEFAULT_LOCAL_BASE_URL:
                self._llm_base_url = (
                    DEFAULT_ARK_BASE_URL
                    if provider == LLM_PROVIDER_VOLCENGINE
                    else "https://api.openai.com/v1"
                )
                self._settings.setValue("llm/baseUrl", self._llm_base_url)
            if not self._llm_model.strip() or self._llm_model == DEFAULT_LOCAL_MODEL:
                self._llm_model = (
                    DEFAULT_ARK_MODEL
                    if provider == LLM_PROVIDER_VOLCENGINE
                    else "gpt-5.6-luna"
                )
                self._settings.setValue("llm/model", self._llm_model)
            if not self._llm_api_key_env:
                self._llm_api_key_env = (
                    DEFAULT_ARK_API_KEY_ENV
                    if provider == LLM_PROVIDER_VOLCENGINE
                    else "OPENAI_API_KEY"
                )
                self._settings.setValue("llm/apiKeyEnv", self._llm_api_key_env)
        self.settingsChanged.emit()

    @Property(str, notify=settingsChanged)
    def llmBaseUrl(self) -> str:
        return self._llm_base_url

    @llmBaseUrl.setter
    def llmBaseUrl(self, value: str) -> None:
        self._set_setting("_llm_base_url", str(value), "llm/baseUrl")

    @Property(str, notify=settingsChanged)
    def llmModel(self) -> str:
        return self._llm_model

    @llmModel.setter
    def llmModel(self, value: str) -> None:
        self._set_setting("_llm_model", str(value), "llm/model")

    @Property(str, notify=settingsChanged)
    def llmApiKeyEnv(self) -> str:
        return self._llm_api_key_env

    @llmApiKeyEnv.setter
    def llmApiKeyEnv(self, value: str) -> None:
        self._set_setting("_llm_api_key_env", str(value), "llm/apiKeyEnv")

    @Property(str, notify=settingsChanged)
    def llmApiKey(self) -> str:
        return self._llm_api_key

    @llmApiKey.setter
    def llmApiKey(self, value: str) -> None:
        self._set_setting("_llm_api_key", str(value).strip(), "llm/apiKey")

    @Property(str, notify=settingsChanged)
    def llmLocalServerPath(self) -> str:
        return self._llm_local_server_path

    @llmLocalServerPath.setter
    def llmLocalServerPath(self, value: str) -> None:
        self._set_setting(
            "_llm_local_server_path", str(value), "llm/localServerPath"
        )
        self.localModelInstallationChanged.emit()

    @Property(str, notify=settingsChanged)
    def llmLocalModelPath(self) -> str:
        return self._llm_local_model_path

    @llmLocalModelPath.setter
    def llmLocalModelPath(self, value: str) -> None:
        self._set_setting("_llm_local_model_path", str(value), "llm/localModelPath")
        self.localModelInstallationChanged.emit()

    @Property(bool, notify=localModelInstallationChanged)
    def localModelInstalled(self) -> bool:
        return Path(self._llm_local_server_path).expanduser().is_file() and Path(
            self._llm_local_model_path
        ).expanduser().is_file()

    @Property(bool, notify=localModelInstallationChanged)
    def localModelInstalling(self) -> bool:
        return self._local_model_installing

    @Property(str, notify=localModelInstallationChanged)
    def localModelInstallStatus(self) -> str:
        if self.localModelInstalled:
            return "本地模型已安装，可离线使用"
        return self._local_model_install_status or "需要下载约 2.5 GB（仅首次）"

    @Property(float, notify=settingsChanged)
    def llmTimeoutSeconds(self) -> float:
        return self._llm_timeout_s

    @llmTimeoutSeconds.setter
    def llmTimeoutSeconds(self, value: float) -> None:
        timeout = max(1.0, min(float(value), 300.0))
        self._set_setting("_llm_timeout_s", timeout, "llm/timeoutSeconds")

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
    def reconnectDevice(self) -> None:
        """Reconnect only after an explicit user action."""
        if self._connected or self._busy:
            return
        if not self.canReconnect:
            self.requestDevicePicker()
            return
        self._start_selected_device()

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
        self._available_devices = []
        self._device_handles = {}
        self._scan_message = "正在扫描附近的蓝牙设备…"
        self.devicesChanged.emit()
        self._set_status("正在扫描", "请选择列表中的设备进行连接", "starting")
        self._append_log("开始扫描附近的蓝牙设备（不按名称过滤）")
        self._scan_devices_once()

    @Slot()
    def stopDeviceDiscovery(self) -> None:
        self._discovery_active = False

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

        timeout_s = 5.0

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
                        "_device": item.device,
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
        self._selected_device = self._device_handles.get(identifier.casefold())
        self._selector = identifier
        self._device_name = display_name
        self._settings.setValue("ring/selector", identifier)
        self._settings.setValue("ring/name", display_name)
        self.settingsChanged.emit()
        if not self._can_reconnect:
            self._can_reconnect = True
            self.reconnectAvailabilityChanged.emit()
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
        self._clear_feedback_reason()
        try:
            settings = self._runtime_settings()
        except BaseException as exc:
            self._set_status("配置有误", str(exc), "error")
            self._append_log(f"配置错误：{exc}")
            return
        try:
            self._modification_dataset.reset_runtime()
        except BaseException as exc:
            self._append_log(f"修改数据采集初始化失败：{exc}")

        self._disconnect_event = threading.Event()
        self._recognition_event = threading.Event()
        self._runtime_active = True
        self._runtime_had_connection = False
        self._busy = True
        self.busyChanged.emit()
        self._set_status(
            "正在连接设备",
            f"正在连接 {self._device_name}",
            "starting",
        )
        self._append_log(f"开始连接设备：{self._device_name} ({self._selector})")
        runtime = RecognitionRuntime(
            settings,
            asr_backend_cache=self._asr_backend_cache,
        )

        def worker_main() -> None:
            error = ""
            try:
                runtime.run(
                    self._disconnect_event,
                    self._recognition_event,
                    on_update=self._publish_update,
                    on_state=self._runtimeStatus.emit,
                    on_connected=self._runtimeConnected.emit,
                    on_disconnected=self._runtimeDisconnected.emit,
                    on_started=self._runtimeStarted.emit,
                    on_push_to_talk=self._pushToTalkChanged.emit,
                    on_raw_audio=self._record_raw_attempt_audio,
                )
            except BaseException as exc:
                error = str(exc)
                print(f"[runtime] {error}")
            self._runtimeFinished.emit(error)

        self._worker = threading.Thread(
            target=worker_main,
            name="ProxiMicUiRuntime",
            daemon=True,
        )
        self._worker.start()

    def _record_raw_attempt_audio(self, session_id: int, audio_16k) -> None:
        try:
            self._modification_dataset.record_audio(session_id, audio_16k)
        except BaseException as exc:
            print(f"[dataset] raw attempt audio was not saved: {exc}")

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
            detail += "，或按住右 Alt"
        self._set_status("自动监听中", detail, "running")
        self._append_log("语音识别已开启（设备保持连接）")

    @Slot()
    def pauseRecognition(self) -> None:
        if not self._connected or not self._recognition_enabled:
            return
        self._recognition_event.clear()
        self._session_input_modes.clear()
        self._session_routing_modes.clear()
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
        self._cancel_pending_text_processing()
        self._session_input_modes.clear()
        self._session_routing_modes.clear()
        self._recognition_event.clear()
        if self._recognition_enabled:
            self._recognition_enabled = False
            self.recognitionEnabledChanged.emit()
            self.runningChanged.emit()
        self._disconnect_event.set()
        self._busy = True
        self.busyChanged.emit()
        self._set_status("正在断开", "正在停止识别并优先断开设备", "stopping")
        self._append_log("已停止接收新的识别任务，正在优先断开语音设备")

    # Compatibility for older callers.  New UI code uses the explicit methods.
    @Slot()
    def start(self) -> None:
        self.connectDevice()

    @Slot()
    def stop(self) -> None:
        self.disconnectDevice()

    @Slot()
    def clearSessionHistory(self) -> None:
        if not self._session_history_lines:
            return
        self._session_history_lines.clear()
        self.sessionHistoryChanged.emit()

    @Slot()
    def clearLog(self) -> None:
        self._log_lines.clear()
        self.logChanged.emit()

    @Slot()
    def warmLocalModel(self) -> None:
        if self._llm_provider != LLM_PROVIDER_LOCAL:
            self._append_log("已选择火山方舟，收到语音结果后将按需调用线上模型")
            return
        if self._llm_warmup_requested or self._quitting:
            return
        if not self.localModelInstalled:
            self._append_log("本地文本模型尚未安装，可在设置中按需下载")
            return
        self._llm_warmup_requested = True
        self._append_log("正在后台加载并预热本地文本模型…")
        self._text_processing_worker.warmup(self._voice_llm_settings())

    @Slot(str, float)
    def _apply_llm_warmup_finished(self, error: str, latency_s: float) -> None:
        if error:
            self._llm_warmup_requested = False
            self._append_log(f"本地文本模型预热失败：{error}")
            return
        self._append_log(
            f"本地文本模型已加载，自动路由/输入/修改提示词预热完成（{latency_s:.2f}s）"
        )

    @Slot()
    def installLocalModel(self) -> None:
        if self._local_model_installing or self.localModelInstalled:
            return
        self._local_model_installing = True
        self._local_model_install_status = "正在准备下载…"
        self.localModelInstallationChanged.emit()
        self._append_log("开始下载本地文本模型；可以继续使用应用其他功能")

        def progress(label: str, current: int, total: int) -> None:
            self._localModelInstallProgress.emit(label, current, total)

        def worker() -> None:
            try:
                result = install_default_local_model(progress=progress)
                self._localModelInstallFinished.emit(result, "")
            except Exception as exc:
                self._localModelInstallFinished.emit({}, str(exc))

        threading.Thread(
            target=worker, name="local-model-installer", daemon=True
        ).start()

    @Slot(str, int, int)
    def _apply_local_model_install_progress(
        self, label: str, current: int, total: int
    ) -> None:
        if total > 0:
            percent = max(0, min(100, int(current * 100 / total)))
            self._local_model_install_status = f"{label}：{percent}%"
        else:
            self._local_model_install_status = (
                f"{label}：{current / (1024 * 1024):.0f} MB"
            )
        self.localModelInstallationChanged.emit()

    @Slot(object, str)
    def _apply_local_model_install_finished(self, result: object, error: str) -> None:
        self._local_model_installing = False
        if error:
            self._local_model_install_status = f"下载失败：{error}"
            self._append_log(self._local_model_install_status)
            self.localModelInstallationChanged.emit()
            return
        installed = dict(result) if isinstance(result, dict) else {}
        self._llm_local_server_path = str(installed.get("server_path", ""))
        self._llm_local_model_path = str(installed.get("model_path", ""))
        self._settings.setValue("llm/localServerPath", self._llm_local_server_path)
        self._settings.setValue("llm/localModelPath", self._llm_local_model_path)
        self._local_model_install_status = "本地模型已安装，可离线使用"
        self.settingsChanged.emit()
        self.localModelInstallationChanged.emit()
        self._append_log("本地文本模型下载并校验完成")
        self.warmLocalModel()

    @Slot()
    def requestQuit(self) -> None:
        self.stopDeviceDiscovery()
        if self._quitting:
            # A second close/Ctrl+C is an explicit request not to wait for
            # graceful device cleanup any longer.
            QCoreApplication.quit()
            return
        self._quitting = True
        self._clear_feedback_reason()
        self._cancel_pending_text_processing()
        self._text_processing_worker.close(wait=False)
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
        for row in rows:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get("identifier", "")).strip()
            device = row.get("_device")
            if identifier and device is not None:
                self._device_handles[identifier.casefold()] = device
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
            self._scan_message = "没有发现蓝牙设备，请确认蓝牙权限后重新扫描"
            self._set_status("未发现设备", self._scan_message, "idle")
        if added or not count:
            self.devicesChanged.emit()

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
        if self._scan_busy:
            message += "；正在扫描"
        self._scan_message = message

    # Worker event application --------------------------------------------------
    def _publish_update(self, update) -> None:
        try:
            self._modification_dataset.record_asr_update(update)
        except BaseException as exc:
            print(f"[dataset] ASR update was not saved: {exc}")
        self._runtimeUpdate.emit(
            str(update.text or ""),
            bool(update.is_final),
            str(update.error or ""),
            int(getattr(update, "session_id", 0)),
        )

    @Slot(str)
    def _apply_runtime_status(self, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        summary = text.splitlines()[0].strip()
        self._append_log(text)
        if summary.startswith("正在连接设备"):
            self._set_status("正在连接设备", summary, "starting")
        elif summary.startswith("正在验证 Ring"):
            self._set_status("正在验证设备音频", summary, "starting")
        elif summary.startswith("正在加载 ProxiMic"):
            self._set_status("正在加载检测模型", summary, "starting")
        elif summary.startswith("设备音频验证通过"):
            self._set_status("设备已连接", summary, "starting")
        elif summary.startswith("正在加载语音模型"):
            self._set_status("正在加载语音模型", summary, "starting")
        elif summary.startswith("正在复用已加载语音模型"):
            self._set_status("正在复用语音模型", summary, "starting")
        elif summary.startswith("正在准备实时识别"):
            self._set_status("正在准备识别", summary, "starting")
        elif summary.startswith("模型加载完成，正在确认实时音频"):
            self._set_status("正在确认实时音频", summary, "starting")
        elif summary.startswith(("STAGE2 ", "[ASR]", "[ASR TIMING]")):
            # Keep detector/ASR telemetry in the log without replacing the
            # user-facing status text at every diagnostic milestone.
            return
        elif summary.startswith("设备连接已中断"):
            self._set_status("设备连接异常", summary, "error")
        elif self._status_kind in {"running", "listening"}:
            self._status_detail = summary
            self.statusChanged.emit()

    @Slot()
    def _apply_runtime_connected(self) -> None:
        if self._disconnect_event.is_set():
            return
        self._connected = True
        self._runtime_had_connection = True
        self.connectedChanged.emit()
        self._set_status(
            "设备已连接",
            "蓝牙与设备服务验证通过，正在准备识别组件",
            "starting",
        )
        self._append_log("设备连接验证通过；不会在异常后自动重连")

    @Slot()
    def _apply_runtime_disconnected(self) -> None:
        if not self._runtime_active:
            return
        was_connected = self._connected
        was_recognizing = self._recognition_enabled
        self._connected = False
        self._recognition_enabled = False
        self._ptt_active = False
        if was_connected:
            self.connectedChanged.emit()
        if was_recognizing:
            self.recognitionEnabledChanged.emit()
            self.runningChanged.emit()

        if self._runtime_had_connection:
            self._set_status(
                "设备已断开",
                "蓝牙和麦克风已经释放，正在结束后台模型初始化或识别任务",
                "stopping",
            )
            self._append_log("物理设备已断开；后台任务正在安全结束")
        else:
            self._set_status(
                "连接未完成",
                "设备已自动断开，正在清理本次连接任务",
                "stopping",
            )
            self._append_log("设备连接未完成，已自动断开并开始清理")

    @Slot()
    def _apply_runtime_started(self) -> None:
        if self._disconnect_event.is_set():
            return
        if not self._connected:
            self._connected = True
            self.connectedChanged.emit()
        self._busy = False
        self.busyChanged.emit()
        self._set_status("准备就绪", "设备已连接，点击“开启语音识别”开始", "paused")
        self._append_log("设备和识别组件已就绪；识别当前暂停")

    @Slot(str, bool, str, int)
    def _apply_runtime_update(
        self,
        text: str,
        is_final: bool,
        error: str,
        session_id: int = 0,
    ) -> None:
        if self._status_kind == "stopping":
            return
        # A review is modal at the interaction layer even though its overlay
        # never steals focus.  Stray proximity activations must not replace the
        # preview or start another LLM edit before the user chooses an action.
        if self._edit_review is not None and not error:
            return
        if error:
            if session_id:
                self._session_input_modes.pop(int(session_id), None)
                self._session_routing_modes.pop(int(session_id), None)
                self._session_targets.pop(int(session_id), None)
            self._transcript_text = error
            self._transcript_mode = ""
            self._transcript_final = True
            self._transcript_visible = True
            self._set_interaction_state("error")
            self.transcriptChanged.emit()
            self._hide_overlay_timer.start(3500)
            self._append_log(f"ASR 错误：{error}")
            return
        text = text.strip()
        if not text:
            return
        normalized_session_id = int(session_id)
        if normalized_session_id:
            mode = self._session_input_modes.setdefault(
                normalized_session_id, self._input_mode
            )
            routing_mode = self._session_routing_modes.setdefault(
                normalized_session_id, self._input_routing_mode
            )
            if normalized_session_id not in self._session_targets:
                self._session_targets[normalized_session_id] = (
                    self._capture_desktop_reference()
                )
        else:
            mode = self._input_mode
            routing_mode = self._input_routing_mode
        self._transcript_mode = (
            mode if routing_mode == INPUT_ROUTING_MANUAL else ""
        )
        self._transcript_text = text
        self._transcript_final = is_final
        self._transcript_visible = True
        self._set_interaction_state("listening" if not is_final else "processing")
        self.transcriptChanged.emit()
        if is_final:
            if normalized_session_id:
                self._session_input_modes.pop(normalized_session_id, None)
                self._session_routing_modes.pop(normalized_session_id, None)
                target = self._session_targets.pop(normalized_session_id, None)
            else:
                target = self._capture_desktop_reference()
            self._append_log(f"识别完成：{text}")
            if routing_mode == INPUT_ROUTING_AUTO:
                self._submit_input_mode_routing(
                    text,
                    normalized_session_id,
                    fallback_mode=mode,
                    target=target,
                )
            else:
                self._dispatch_completed_text(
                    text, normalized_session_id, mode, target
                )
        else:
            self._hide_overlay_timer.stop()
            if self._recognition_enabled:
                self._set_status("正在识别", text, "listening")

    def _dispatch_completed_text(
        self,
        text: str,
        session_id: int,
        mode: str,
        target: DesktopTargetRef | None,
    ) -> None:
        mode = normalize_input_mode(mode)
        if mode != INPUT_MODE_EDIT:
            self._submit_text_processing(text, session_id, mode, target=target)
            return
        if self._edit_review is not None:
            self._reject_edit_request(
                "上一条修改仍在等待确认，请先确认、取消或选择重说"
            )
            return
        retrying_same_target = self._retry_target_armed
        snapshot = self._retry_snapshot if retrying_same_target else None
        if not retrying_same_target:
            self._retry_snapshot = None
        if snapshot is None:
            if target is None:
                self._reject_edit_request(
                    "没有锁定外部文本框，请先把光标放入要修改的文本框"
                )
                return
            try:
                snapshot = self._desktop_target_adapter().capture_text(target)
                validate_edit_target_text(snapshot.text)
                self._log_edit_target_snapshot(snapshot)
            except BaseException as exc:
                try:
                    self._desktop_target_adapter().release_selection(target)
                except BaseException:
                    pass
                self._reject_edit_request(str(exc))
                return
        else:
            self._retry_snapshot = None
            self._retry_target_armed = False
        self._submit_text_processing(
            text,
            session_id,
            mode,
            target_text=snapshot.text,
            target=target or snapshot.target,
            snapshot=snapshot,
        )

    def _submit_input_mode_routing(
        self,
        text: str,
        session_id: int,
        *,
        fallback_mode: str,
        target: DesktopTargetRef | None,
    ) -> None:
        self._text_request_id += 1
        request_id = self._text_request_id
        request = InputModeRoutingRequest(
            request_id=request_id,
            session_id=int(session_id),
            raw_text=text,
            settings=replace(self._llm_settings(), enabled=True),
            fallback_mode=normalize_input_mode(fallback_mode),
        )
        was_processing = self.textProcessing
        self._pending_mode_routes.add(request_id)
        self._pending_mode_route_contexts[request_id] = _PendingModeRoute(target)
        if not was_processing:
            self.textProcessingChanged.emit()
        self._transcript_text = f"正在自动判断听写或指令…\n{text}"
        self._transcript_final = False
        self._transcript_visible = True
        self._set_interaction_state("processing")
        self.transcriptChanged.emit()
        self._hide_overlay_timer.stop()
        self._append_log(
            f"自动路由判断开始：{text}（模型：{request.settings.provider}/"
            f"{request.settings.model}）"
        )
        if self._recognition_enabled:
            self._set_status("正在判断输入类型", "大模型正在区分听写或编辑指令", "starting")
        self._text_processing_worker.submit_routing(request)

    @Slot(object)
    def _apply_input_mode_routed(self, result: object) -> None:
        if not isinstance(result, InputModeRoutingResult):
            return
        if result.request_id not in self._pending_mode_routes:
            return
        self._pending_mode_routes.remove(result.request_id)
        context = self._pending_mode_route_contexts.pop(
            result.request_id, _PendingModeRoute()
        )
        if not self.textProcessing:
            self.textProcessingChanged.emit()
        if self._quitting or self._status_kind == "stopping":
            return
        label = "编辑指令" if result.mode == INPUT_MODE_EDIT else "听写"
        self._transcript_mode = result.mode
        self.transcriptChanged.emit()
        if result.model_output:
            self._append_log(f"自动路由模型原始返回：{result.model_output}")
        if result.error:
            self._append_log(
                f"自动路由判断失败（{result.latency_s:.3f}s）：{result.error}；"
                f"回退为当前手动模式“{label}”"
            )
        else:
            self._append_log(
                f"自动路由判断完成：{label}（耗时 {result.latency_s:.3f}s）"
            )
        self._dispatch_completed_text(
            result.raw_text,
            result.session_id,
            result.mode,
            context.target,
        )

    def _reject_edit_request(self, message: str) -> None:
        self._transcript_text = message
        self._transcript_final = True
        self._transcript_visible = True
        self._set_interaction_state("error")
        self.transcriptChanged.emit()
        self._hide_overlay_timer.start(3500)
        self._append_log(f"修改未执行：{message}")
        self._record_history("修改 · 未执行", detail=message)
        if self._recognition_enabled:
            self._set_status("自动监听中", message, "running")

    def _submit_text_processing(
        self,
        text: str,
        session_id: int,
        mode: str,
        *,
        target_text: str = "",
        target: DesktopTargetRef | None = None,
        snapshot: DesktopTextSnapshot | None = None,
    ) -> None:
        normalized_mode = normalize_input_mode(mode)
        if normalized_mode != INPUT_MODE_EDIT and not self._llm_enabled:
            self._append_log("输入模式已跳过文本大模型，直接采用 ASR 最终结果")
            self._commit_input_text(
                TextProcessingResult(
                    request_id=0,
                    session_id=int(session_id),
                    mode=normalized_mode,
                    raw_text=text,
                    final_text=text,
                    latency_s=0.0,
                    used_llm=False,
                ),
                target,
            )
            return
        self._text_request_id += 1
        request_id = self._text_request_id
        request = TextProcessingRequest(
            request_id=request_id,
            session_id=int(session_id),
            mode=normalized_mode,
            raw_text=text,
            settings=self._voice_llm_settings(normalized_mode),
            target_text=target_text,
        )
        if normalized_mode == INPUT_MODE_EDIT and snapshot is not None:
            try:
                episode_id, attempt_id = self._modification_dataset.begin_attempt(
                    request_id=request_id,
                    session_id=int(session_id),
                    target_text=target_text,
                    application=(
                        snapshot.target.process_name
                        or snapshot.target.window_title
                        or "unknown"
                    ),
                    provider=request.settings.provider,
                    model=request.settings.model,
                    prompt_version=PROMPT_VERSION,
                )
                request = replace(
                    request,
                    episode_id=episode_id,
                    attempt_id=attempt_id,
                )
            except BaseException as exc:
                self._append_log(f"修改数据 Attempt 保存失败：{exc}")
        was_processing = bool(self._pending_text_requests)
        self._pending_text_requests.add(request_id)
        self._pending_interactions[request_id] = _PendingInteraction(
            target=target,
            snapshot=snapshot,
        )
        if not was_processing:
            self.textProcessingChanged.emit()
        label = "修改" if normalized_mode == INPUT_MODE_EDIT else "输入"
        self._transcript_text = f"{label}处理中…\n{text}"
        self._transcript_final = False
        self._transcript_visible = True
        self._set_interaction_state("processing")
        self.transcriptChanged.emit()
        self._hide_overlay_timer.stop()
        self._append_log(f"已提交大模型{label}处理：{text}")
        if self._recognition_enabled:
            self._set_status("正在处理文本", f"大模型正在处理{label}内容", "starting")
        self._text_processing_worker.submit(request)

    @Slot(object)
    def _apply_text_processed(self, result: object) -> None:
        if not isinstance(result, TextProcessingResult):
            return
        if result.request_id not in self._pending_text_requests:
            return
        self._pending_text_requests.remove(result.request_id)
        context = self._pending_interactions.pop(
            result.request_id, _PendingInteraction()
        )
        if not self._pending_text_requests:
            self.textProcessingChanged.emit()
        if self._quitting or self._status_kind == "stopping":
            return
        if result.mode == INPUT_MODE_EDIT:
            try:
                self._modification_dataset.record_llm_result(
                    result.request_id, result
                )
            except BaseException as exc:
                self._append_log(f"修改数据 LLM 分支保存失败：{exc}")
        label = "修改" if result.mode == INPUT_MODE_EDIT else "输入"
        parsed_edit_response: object = None
        if result.model_output:
            model_output = result.model_output.strip()
            if result.mode == INPUT_MODE_EDIT:
                try:
                    parsed_output = json.loads(model_output)
                    if isinstance(parsed_output, dict):
                        parsed_edit_response = parsed_output
                    model_output = json.dumps(
                        parsed_output,
                        ensure_ascii=False,
                        indent=2,
                    )
                except (TypeError, json.JSONDecodeError):
                    pass
            self._append_log(f"大模型{label}原始返回：\n{model_output}")
        if result.error:
            self._append_log(
                f"大模型{label}处理失败：{result.error}"
            )
            if result.mode == INPUT_MODE_EDIT:
                if context.snapshot is None:
                    try:
                        self._modification_dataset.abandon_request(
                            result.request_id, "修改目标快照已经失效"
                        )
                    except BaseException:
                        pass
                    self._reject_edit_request("修改目标快照已经失效")
                    return
                self._begin_failed_edit_review(result, context.snapshot)
                return
        else:
            if result.mode == INPUT_MODE_EDIT:
                unchanged = result.final_text == result.target_text
                outcome = (
                    "目标保持不变"
                    if unchanged
                    else f"已生成候选（{len(result.final_text)} 个字符）"
                )
                self._append_log(
                    f"大模型修改处理完成（{result.latency_s:.2f}s）：{outcome}"
                )
            else:
                self._append_log(
                    f"大模型输入处理完成（{result.latency_s:.2f}s）："
                    f"{result.final_text}"
                )
        if result.mode == INPUT_MODE_EDIT:
            if context.snapshot is None:
                try:
                    self._modification_dataset.abandon_request(
                        result.request_id, "修改目标快照已经失效"
                    )
                except BaseException:
                    pass
                self._reject_edit_request("修改目标快照已经失效")
                return
            if result.final_text == result.target_text:
                self._begin_failed_edit_review(
                    replace(
                        result,
                        error="大模型未找到可可靠执行的修改",
                    ),
                    context.snapshot,
                )
                return
            self._begin_edit_review(
                result,
                context.snapshot,
                allow_empty=_is_explicit_emptying_edit_response(
                    parsed_edit_response,
                    result.target_text,
                ),
            )
            return
        self._commit_input_text(result, context.target)

    @Slot(object)
    def _apply_llm_trace_collected(self, trace: object) -> None:
        if not isinstance(trace, LLMTraceCollection):
            return
        try:
            self._modification_dataset.record_llm_branches(
                trace.request_id,
                trace.branches,
                trace.winner_branch,
            )
        except BaseException as exc:
            self._append_log(f"修改数据后台分支保存失败：{exc}")

    def _commit_input_text(
        self,
        result: TextProcessingResult,
        target: DesktopTargetRef | None,
    ) -> None:
        text = result.final_text
        final_text = str(text or "").strip()
        if not final_text:
            return
        self._transcript_text = final_text
        self._transcript_final = True
        self._transcript_visible = True
        self._set_interaction_state("applied")
        self.transcriptChanged.emit()
        self._hide_overlay_timer.start(1800)
        applied = False
        detail = ""
        if not self._desktop_output:
            detail = "跨应用注入已关闭"
        elif target is None:
            detail = "未锁定外部文本框，结果仅保存在后台记录"
        else:
            try:
                self._desktop_target_adapter().inject(target, final_text)
                applied = True
                detail = f"已注入 {target.window_title or '外部文本框'}"
            except BaseException as exc:
                detail = f"注入失败：{exc}"
                self._append_log(detail)
        if result.error:
            fallback = f"大模型处理失败，已回退 ASR 原文：{result.error}"
            detail = f"{detail}；{fallback}" if detail else fallback
        status = "已注入" if applied else "未注入"
        self._record_history(
            f"输入 · {status}",
            raw=result.raw_text,
            result=final_text if result.used_llm else "",
            detail=detail,
        )
        if self._recognition_enabled:
            self._set_status("自动监听中", "输入完成，等待下一段语音", "running")

    def _begin_edit_review(
        self,
        result: TextProcessingResult,
        snapshot: DesktopTextSnapshot,
        *,
        allow_empty: bool = False,
    ) -> None:
        proposed = str(result.final_text or "").strip()
        if not proposed and not allow_empty:
            self._begin_failed_edit_review(
                replace(result, error="大模型返回了空修改结果"),
                snapshot,
            )
            return
        if proposed == snapshot.text.strip():
            self._begin_failed_edit_review(
                replace(result, error="大模型未找到可可靠执行的修改"),
                snapshot,
            )
            return
        self._retry_snapshot = None
        self._edit_review = _EditReview(
            request_id=result.request_id,
            session_id=result.session_id,
            instruction=result.raw_text,
            proposed_text=proposed,
            snapshot=snapshot,
        )
        self._edit_preview_html = _edit_preview_html(
            snapshot.text.strip(),
            proposed,
        )
        review_lines = [f"修改指令：{result.raw_text}"]
        if not proposed:
            review_lines.append("⚠ 确认后将清空目标文本框")
        review_lines.append("Enter 确认  ·  Esc 取消  ·  按住右 Alt 重说")
        self._transcript_text = "\n".join(review_lines)
        self._transcript_final = True
        self._transcript_visible = True
        self._hide_overlay_timer.stop()
        self._set_interaction_state("review")
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._record_history(
            "修改 · 等待确认",
            raw=result.raw_text,
            result=proposed,
            detail=(
                f"目标：{snapshot.target.window_title or '外部文本框'}"
                + ("；确认后将清空目标文本框" if not proposed else "")
            ),
        )
        if proposed:
            self._append_log("修改预览已生成，等待确认、取消或重说指令")
        else:
            self._append_log("清空文本预览已生成，等待 Enter 确认或 Esc 取消")
        if self._recognition_enabled:
            detail = (
                "确认后将清空外部文本框"
                if not proposed
                else "确认后才会覆盖外部文本"
            )
            self._set_status("等待确认修改", detail, "manual")

    def _begin_failed_edit_review(
        self,
        result: TextProcessingResult,
        snapshot: DesktopTextSnapshot,
    ) -> None:
        error = str(result.error or "大模型没有返回可用的修改结果").strip()
        try:
            self._modification_dataset.record_llm_failure(
                result.request_id,
                error,
            )
        except BaseException as exc:
            self._append_log(f"修改数据 LLM 失败状态保存失败：{exc}")
        self._retry_snapshot = None
        self._retry_target_armed = False
        self._edit_review = _EditReview(
            request_id=result.request_id,
            session_id=result.session_id,
            instruction=result.raw_text,
            proposed_text="",
            snapshot=snapshot,
            failure_error=error,
        )
        self._edit_preview_html = ""
        self._transcript_text = "\n".join(
            (
                "大模型修改失败，原文本保持不变",
                error,
                "Esc 取消  ·  按住右 Alt 重说",
            )
        )
        self._transcript_final = True
        self._transcript_visible = True
        self._hide_overlay_timer.stop()
        self._set_interaction_state("review_error")
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._record_history(
            "修改 · 大模型失败",
            raw=result.raw_text,
            detail=error,
        )
        self._append_log("大模型修改失败，等待用户重说或取消")
        if self._recognition_enabled:
            self._set_status(
                "大模型修改失败",
                "原文本未改变；请重说修改要求或取消",
                "error",
            )

    @Slot()
    def confirmEdit(self) -> None:
        review = self._edit_review
        if review is None or review.failure_error:
            return
        self._clear_feedback_reason()
        try:
            self._desktop_target_adapter().replace(
                review.snapshot, review.proposed_text
            )
        except BaseException as exc:
            self._record_history("修改 · 应用失败", detail=str(exc))
            try:
                self._modification_dataset.feedback(
                    review.request_id,
                    "apply_failed",
                    error=str(exc),
                    final_text=review.snapshot.text,
                )
            except BaseException as collection_exc:
                self._append_log(f"修改数据反馈保存失败：{collection_exc}")
            self._finish_edit_review(
                message=f"修改未应用：{exc}", state="error", hide_ms=4000
            )
            return
        final_text, manually_corrected = self._read_back_edit_text(
            review, fallback=review.proposed_text
        )
        try:
            self._modification_dataset.feedback(
                review.request_id,
                "confirm",
                final_text=final_text,
                manually_corrected=manually_corrected,
            )
        except BaseException as exc:
            self._append_log(f"修改数据确认反馈保存失败：{exc}")
        self._record_history(
            "修改 · 已应用",
            raw=review.instruction,
            result=review.proposed_text,
            detail=f"已替换 {review.snapshot.target.window_title or '外部文本框'}",
        )
        self._finish_edit_review(
            message="修改已应用到原文本框", state="applied", hide_ms=2200
        )

    @Slot()
    def cancelEdit(self) -> None:
        review = self._edit_review
        if review is None:
            return
        self._clear_feedback_reason()
        feedback_saved = False
        final_text, manually_corrected = self._read_back_edit_text(
            review, fallback=review.snapshot.text
        )
        try:
            self._modification_dataset.feedback(
                review.request_id,
                "cancel",
                final_text=final_text,
                manually_corrected=manually_corrected,
            )
            feedback_saved = True
        except BaseException as exc:
            self._append_log(f"修改数据取消反馈保存失败：{exc}")
        self._desktop_target_adapter().release_selection(review.snapshot.target)
        self._record_history("修改 · 已取消", raw=review.instruction)
        self._finish_edit_review(
            message="已取消，本次修改没有执行", state="cancelled", hide_ms=2200
        )
        if feedback_saved:
            if review.failure_error:
                self._mark_automatic_llm_failure_reason(
                    review.request_id, "cancel"
                )
            else:
                self._offer_feedback_reason(review.request_id, "cancel")

    @Slot()
    def retryEdit(self) -> None:
        review = self._edit_review
        if review is None:
            return
        self._clear_feedback_reason()
        feedback_saved = False
        try:
            self._modification_dataset.feedback(review.request_id, "retry")
            feedback_saved = True
        except BaseException as exc:
            self._append_log(f"修改数据重说反馈保存失败：{exc}")
        self._retry_snapshot = review.snapshot
        self._retry_target_armed = True
        self._edit_review = None
        self._edit_preview_html = ""
        self.inputMode = INPUT_MODE_EDIT
        self._transcript_text = "请重新说修改要求\n原文本保持不变"
        self._transcript_final = False
        self._transcript_visible = True
        self._hide_overlay_timer.stop()
        self._set_interaction_state("retry")
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._record_history("修改 · 等待重说", raw=review.instruction)
        if self._recognition_enabled:
            self._set_status("请重说修改要求", "下一段语音仍然修改同一个文本框", "manual")
        if feedback_saved:
            if review.failure_error:
                self._mark_automatic_llm_failure_reason(
                    review.request_id, "retry"
                )
            else:
                self._offer_feedback_reason(review.request_id, "retry")

    def _mark_automatic_llm_failure_reason(
        self, request_id: int, action: str
    ) -> None:
        try:
            saved = self._modification_dataset.annotate_feedback_reason(
                request_id,
                action,
                "llm_error",
                input_method="automatic",
            )
        except BaseException as exc:
            self._append_log(f"大模型失败原因自动保存失败：{exc}")
            return
        if saved:
            action_label = "重说" if action == "retry" else "取消"
            self._append_log(
                f"已自动标记本次{action_label}原因："
                f"{FEEDBACK_REASON_LABELS['llm_error']}"
            )

    @Slot(str)
    def selectFeedbackReason(
        self, reason_code: str, input_method: str = "keyboard"
    ) -> None:
        pending = self._pending_feedback_reason
        if pending is None:
            return
        normalized_reason = str(reason_code).strip().lower()
        try:
            saved = self._modification_dataset.annotate_feedback_reason(
                pending.request_id,
                pending.action,
                normalized_reason,
                input_method=input_method,
            )
        except BaseException as exc:
            self._append_log(f"修改失败原因保存失败：{exc}")
            saved = False
        if saved:
            label = FEEDBACK_REASON_LABELS.get(normalized_reason, normalized_reason)
            action_label = "重说" if pending.action == "retry" else "取消"
            self._append_log(f"已标记本次{action_label}原因：{label}")
        self._clear_feedback_reason()

    def _offer_feedback_reason(self, request_id: int, action: str) -> None:
        # Always create a short, renderable gap before showing the next prompt.
        # A retry can replace an older unmarked retry in one Qt event-loop turn;
        # without this gap the Window never visibly closes and appears stale.
        self._pending_feedback_reason = _PendingFeedbackReason(
            request_id=int(request_id), action=str(action)
        )
        self._feedback_reason_timer.stop()
        self._feedback_reason_reveal_timer.stop()
        self._feedback_reason_visible = False
        self.feedbackReasonChanged.emit()
        self._feedback_reason_reveal_timer.start()

    @Slot()
    def _reveal_feedback_reason(self) -> None:
        if self._pending_feedback_reason is None:
            return
        self._feedback_reason_visible = True
        self._feedback_reason_timer.start()
        self.feedbackReasonChanged.emit()

    @Slot()
    def _clear_feedback_reason(self) -> None:
        had_feedback_reason = (
            self._pending_feedback_reason is not None
            or self._feedback_reason_visible
        )
        if not had_feedback_reason:
            self._feedback_reason_timer.stop()
            self._feedback_reason_reveal_timer.stop()
            return
        self._pending_feedback_reason = None
        self._feedback_reason_visible = False
        self._feedback_reason_timer.stop()
        self._feedback_reason_reveal_timer.stop()
        self.feedbackReasonChanged.emit()

    def _read_back_edit_text(
        self, review: _EditReview, *, fallback: str
    ) -> tuple[str, bool]:
        """Read the actual control text after an action or use a safe fallback."""
        try:
            snapshot = self._desktop_target_adapter().capture_text(
                review.snapshot.target
            )
            text = snapshot.text
            self._desktop_target_adapter().release_selection(
                review.snapshot.target
            )
            return text, text != fallback
        except BaseException as exc:
            self._append_log(f"最终文本回读失败，采用已知文本：{exc}")
            return fallback, False

    def dispatchVoiceAction(self, action: str) -> None:
        """Thread-safe entry used by keyboard hooks and future Ring gestures."""
        self._voiceActionRequested.emit(str(action))

    @Slot(str)
    def _apply_voice_action(self, action: str) -> None:
        action = str(action).strip().lower()
        if action == ACTION_INPUT and self._edit_review is None:
            self.inputMode = INPUT_MODE_DICTATION
        elif action == ACTION_EDIT and self._edit_review is None:
            self.inputMode = INPUT_MODE_EDIT
        elif action == ACTION_CONFIRM:
            self.confirmEdit()
        elif action == ACTION_CANCEL:
            self.cancelEdit()
        elif action == ACTION_RETRY:
            self.retryEdit()
        elif action == ACTION_REASON_ASR_ERROR:
            self.selectFeedbackReason("asr_error")
        elif action == ACTION_REASON_LLM_ERROR:
            self.selectFeedbackReason("llm_error")
        elif action == ACTION_REASON_OTHER:
            self.selectFeedbackReason("other")

    def _finish_edit_review(self, *, message: str, state: str, hide_ms: int) -> None:
        self._edit_review = None
        self._edit_preview_html = ""
        self._retry_snapshot = None
        self._retry_target_armed = False
        self._transcript_text = message
        self._transcript_final = True
        self._transcript_visible = True
        self._set_interaction_state(state)
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._hide_overlay_timer.start(hide_ms)
        self._append_log(message)
        if self._recognition_enabled:
            self._set_status("自动监听中", message, "running")

    def _desktop_target_adapter(self):
        if self._desktop_target is None:
            from ..desktop_target import WindowsDesktopTextTarget
            from .clipboard import QtClipboardBridge

            self._desktop_target = WindowsDesktopTextTarget(QtClipboardBridge())
        return self._desktop_target

    def _capture_desktop_reference(self) -> DesktopTargetRef | None:
        if not self._desktop_output or not WINDOWS_DESKTOP_INPUT_SUPPORTED:
            return None
        try:
            return self._desktop_target_adapter().capture_reference()
        except BaseException:
            return None

    def _set_interaction_state(self, state: str) -> None:
        state = str(state)
        if state == self._interaction_state:
            return
        self._interaction_state = state
        self.interactionChanged.emit()

    def _record_history(
        self,
        title: str,
        *,
        raw: str = "",
        result: str = "",
        detail: str = "",
    ) -> None:
        lines = [f"[{datetime.now().strftime('%H:%M:%S')}] {title}"]
        if raw:
            lines.append(f"识别/指令：{raw}")
        if result:
            lines.append(f"LLM：{result}")
        if detail:
            lines.append(detail)
        self._session_history_lines.append("\n".join(lines))
        if len(self._session_history_lines) > 80:
            del self._session_history_lines[:-80]
        self.sessionHistoryChanged.emit()

    def _log_edit_target_snapshot(self, snapshot: DesktopTextSnapshot) -> None:
        text = snapshot.text
        title = snapshot.target.window_title or "外部文本框"
        self._append_log(f"修改目标已读取：{title}（{len(text)} 个字符）")

    def _cancel_pending_text_processing(self) -> None:
        self._clear_feedback_reason()
        had_pending = bool(self._pending_text_requests or self._pending_mode_routes)
        for request_id in tuple(self._pending_text_requests):
            try:
                self._modification_dataset.abandon_request(
                    request_id,
                    "application stopped before text processing completed",
                )
            except BaseException:
                pass
        if self._edit_review is not None:
            try:
                self._modification_dataset.feedback(
                    self._edit_review.request_id,
                    "abandoned",
                    final_text=self._edit_review.snapshot.text,
                )
            except BaseException:
                pass
        self._pending_text_requests.clear()
        self._pending_interactions.clear()
        self._pending_mode_routes.clear()
        self._pending_mode_route_contexts.clear()
        self._session_routing_modes.clear()
        self._session_targets.clear()
        if had_pending:
            self.textProcessingChanged.emit()
        if self._edit_review is not None:
            try:
                self._desktop_target_adapter().release_selection(
                    self._edit_review.snapshot.target
                )
            except BaseException:
                pass
            self._edit_review = None
            self._edit_preview_html = ""
            self._retry_snapshot = None
            self._retry_target_armed = False
            self._set_interaction_state("idle")
            self.interactionChanged.emit()

    @Slot(str)
    def _apply_runtime_finished(self, error: str) -> None:
        was_connected = self._connected
        was_recognizing = self._recognition_enabled
        had_connection = self._runtime_had_connection
        self._runtime_active = False
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
            title = "设备已断开" if had_connection else "连接失败"
            retry = "请点击“重新连接设备”重试。" if self._selector else "请重新选择设备。"
            summary = str(error).splitlines()[0].strip()
            self._set_status(title, f"{summary} 设备已自动断开。{retry}", "error")
            self._append_log(f"{title}：{error}；设备已自动断开，等待用户手动重连")
        elif not self._quitting:
            self._set_status(
                "设备已断开",
                "设备资源已经释放；语音模型保留在内存中以便快速重连",
                "idle",
            )
            self._append_log("语音设备已断开；已加载模型保留到应用退出或配置切换")

    @Slot(bool)
    def _apply_push_to_talk(self, active: bool) -> None:
        if not self._recognition_enabled:
            self._ptt_active = False
            return
        self._ptt_active = bool(active)
        if active:
            if self._edit_review is not None:
                # Reusing the same hold-to-talk gesture is the simplest retry
                # interaction and maps directly to a future Ring gesture.
                self.retryEdit()
            self._set_status("按键监听中", "松开后恢复自动控制", "manual")
        elif self._recognition_enabled:
            self._set_status("自动监听中", "已恢复靠近检测", "running")

    def _hide_transcript(self) -> None:
        self._transcript_visible = False
        self._transcript_mode = ""
        if self._edit_review is None and self._interaction_state not in {"retry", "processing"}:
            self._set_interaction_state("idle")
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
        # Wall-clock time is useful for correlating device/model logs.  Actual
        # durations are measured separately with perf_counter in the ASR
        # worker so an OS clock adjustment cannot corrupt latency numbers.
        timestamp = datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        self._log_lines.append(f"[{timestamp}] {text}")
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
        # BLEDevice instances belong to their discovery loop. Resolve the
        # persisted MAC/opaque CoreBluetooth identifier again in the runtime's
        # long-lived loop on every platform.
        connection_device = None
        return RuntimeSettings(
            ring_name=self._device_name.strip(),
            ring_selector=self._selector.strip() or None,
            ring_device=connection_device,
            data_dir=app_data_root() / "data",
            encoding=self._audio_encoding,
            detector_model=model,
            stage1_threshold=self._stage1_threshold,
            asr_backend=self._asr_backend,
            asr_model=self._asr_model.strip(),
            asr_device=self._asr_device.strip(),
            asr_language=self._asr_language,
            streaming_sensevoice_repo=repo,
            funasr_nano_repo=funasr_repo,
            funasr_nano_hotwords=self._funasr_hotwords,
            asr_api_key=(
                self._asr_api_key.strip()
                if self._asr_backend == "volcengine"
                else ""
            ),
            # The UI commits either the LLM result or the raw fallback itself.
            # Feeding ASR finals into the legacy output here would inject the
            # unprocessed text once and then inject the processed text again.
            desktop_output=False,
            push_to_talk=self._push_to_talk,
        )

    def _llm_settings(self) -> LLMSettings:
        local_selected = self._llm_provider == LLM_PROVIDER_LOCAL
        return LLMSettings(
            enabled=self._llm_enabled,
            base_url=(
                DEFAULT_LOCAL_BASE_URL
                if local_selected
                else self._llm_base_url.strip()
            ),
            model=DEFAULT_LOCAL_MODEL if local_selected else self._llm_model.strip(),
            api_key_env=(
                ""
                if local_selected
                else self._llm_api_key_env.strip()
            ),
            timeout_s=self._llm_timeout_s,
            provider=self._llm_provider,
            local_server_path=self._llm_local_server_path.strip(),
            local_model_path=self._llm_local_model_path.strip(),
            local_auto_start=local_selected,
            local_context_size=DEFAULT_LOCAL_CONTEXT_SIZE,
            local_reasoning=DEFAULT_LOCAL_REASONING,
            api_key="" if local_selected else self._llm_api_key.strip(),
        )

    def _voice_llm_settings(
        self,
        mode: str = INPUT_MODE_EDIT,
    ) -> LLMSettings:
        """Use optional post-processing for input and mandatory LLM for edits."""

        enabled = (
            normalize_input_mode(mode) == INPUT_MODE_EDIT
            or self._llm_enabled
        )
        return replace(self._llm_settings(), enabled=enabled)

    @staticmethod
    def _path_or_none(value: str) -> Path | None:
        text = str(value).strip()
        if not text:
            return None
        path = Path(text).expanduser()
        return path if path.is_absolute() else Path.cwd() / path
