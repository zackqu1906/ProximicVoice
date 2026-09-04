from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from difflib import SequenceMatcher
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from PySide6.QtCore import (
    QObject,
    Property,
    QCoreApplication,
    QSettings,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ..asr import ASRBackendCache
from ..app_runtime import (
    ASR_GAIN_DB_DEFAULT,
    ASR_GAIN_DB_MAX,
    ASR_GAIN_DB_MIN,
    DESKTOP_TEXT_INJECTION_SUPPORTED,
    WINDOWS_DESKTOP_INPUT_SUPPORTED,
    RecognitionRuntime,
    RuntimeSettings,
    normalize_funasr_nano_hotwords,
)
from ..desktop_target import (
    DesktopTargetRef,
    DesktopTextSnapshot,
    macos_texts_equivalent,
)
from ..model_packages import install_default_local_model
from ..interaction_associations import (
    ASSOCIATION_ASR,
    ASSOCIATION_LLM,
    ASR_DICTATION_RETRY,
    ASR_INSTRUCTION_RETRY,
    AssociationActionRouter,
    AssociationMember,
    AssociationRecommendation,
    RecentFailureCoordinator,
)
from ..modification_dataset import ModificationDatasetCollector
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
    ACTION_EDIT,
    ACTION_INPUT,
    ACTION_SWITCH_MODE,
    ACTION_UNDO,
)


def _resolve_voice_history_path(audio_path: str, history_root: Path) -> Path:
    resolved = Path(str(audio_path)).expanduser().resolve(strict=True)
    resolved.relative_to(Path(history_root).resolve())
    if not resolved.is_file():
        raise ValueError("not a file")
    return resolved


def _open_voice_history_location(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif os.name == "nt":
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent))):
        raise RuntimeError("系统文件管理器未能打开录音目录")


def _open_data_directory(path: Path) -> None:
    directory = Path(path).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("not a directory")
    if sys.platform == "darwin":
        # Reveal the directory from its parent. Opening it directly creates a
        # fresh Finder window with no navigation history, leaving Back disabled.
        subprocess.Popen(["open", "-R", str(directory)])
    elif os.name == "nt":
        subprocess.Popen(["explorer.exe", "/select,", str(directory)])
    elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.parent))):
        raise RuntimeError("系统文件管理器未能打开数据目录")


@dataclass
class _PendingInteraction:
    target: DesktopTargetRef | None = None
    snapshot: DesktopTextSnapshot | None = None
    auto_route_id: int = 0


@dataclass
class _PendingModeRoute:
    target: DesktopTargetRef | None = None


@dataclass
class _AutoInteraction:
    route_id: int
    session_id: int
    raw_text: str
    target: DesktopTargetRef | None
    selected_mode: str
    routed_at: float
    snapshot: DesktopTextSnapshot | None = None
    request_ids: dict[str, int] = field(default_factory=dict)
    results: dict[str, TextProcessingResult] = field(default_factory=dict)
    candidate_errors: dict[str, str] = field(default_factory=dict)
    preparing: bool = True
    classified: bool = False
    routed_by_model: bool = True
    prepared_at: float = field(default_factory=time.monotonic)


@dataclass
class _EditReview:
    request_id: int
    session_id: int
    instruction: str
    proposed_text: str
    snapshot: DesktopTextSnapshot


@dataclass
class _AppliedInteraction:
    mode: str
    target: DesktopTargetRef
    session_id: int
    request_id: int
    raw_text: str
    applied_text: str
    original_snapshot: DesktopTextSnapshot | None = None
    auto_context: _AutoInteraction | None = None
    summary: str = ""


_DICTATION_CORRECTION_GRACE_MS = 700
_PROCESSING_MODE_CORRECTION_DELAY_MS = 3000


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
    voiceHistoryChanged = Signal()
    playingVoiceChanged = Signal()
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
    associationChanged = Signal()
    accessibilityChanged = Signal()

    _runtimeStatus = Signal(str)
    _runtimeConnected = Signal()
    _runtimeDisconnected = Signal()
    _runtimeStarted = Signal()
    _runtimeSessionStarted = Signal(int)
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
    _associationActionRequested = Signal(str, str)
    _voiceHistorySaved = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings("ProxiMic", "ProxiMic Voice")
        self._connected = False
        self._recognition_enabled = False
        self._interaction_recognition_suspended = False
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
        self._transcript_final = False
        self._transcript_visible = False
        self._session_history_lines: list[str] = []
        self._voice_history_entries: list[dict[str, object]] = []
        self._playing_voice_path = ""
        self._voice_history_closed = False
        self._interaction_state = "idle"
        self._utterance_active = False
        self._latest_asr_session_id = 0
        self._cancelled_asr_session_ids: set[int] = set()
        self._ignore_asr_updates_until_next_start = False
        self._speech_start_target: DesktopTargetRef | None = None
        self._edit_review: _EditReview | None = None
        self._operation_stack: list[_AppliedInteraction] = []
        self._applied_action_visible = False
        self._applied_target_foreground = True
        self._pending_applied_mode_switch: tuple[int, str] | None = None
        self._active_auto_interaction: _AutoInteraction | None = None
        self._processing_mode_correction_revealed = False
        self._pending_dictation_result: tuple[
            TextProcessingResult, DesktopTargetRef | None
        ] | None = None
        self._smart_association_enabled = self._bool_setting(
            "dataCollection/smartAssociationEnabled", False
        )
        self._association_coordinator = RecentFailureCoordinator(
            limit=5, max_age_s=60.0
        )
        self._association_actions = AssociationActionRouter()
        self._association_queue: deque[AssociationRecommendation] = deque()
        self._association_recommendation: AssociationRecommendation | None = None
        self._provisional_association_recommendations: list[
            AssociationRecommendation
        ] = []
        self._association_detail_visible = False
        self._association_center_visible = False
        self._association_center_stage = "home"
        self._association_center_last_created_id = ""
        self._association_center_kind = ""
        self._association_center_asr_subtype = ASR_DICTATION_RETRY
        self._association_center_entries: list[dict[str, object]] = []
        self._association_center_limit = 40
        self._association_center_chosen_id = ""
        self._association_center_rejected_ids: set[str] = set()
        self._manual_association_watch: tuple[
            DesktopTargetRef, str, AssociationMember
        ] | None = None
        self._manual_association_watch_deadline = 0.0
        self._manual_association_candidate_text = ""
        self._manual_association_candidate_since = 0.0
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
        self._macos_accessibility_trusted = sys.platform != "darwin"
        self._macos_accessibility_last_reported: bool | None = None
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
        self._cancel_utterance_event = threading.Event()

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
            app_data_root() / "dataset",
            anonymous_user_id,
            on_saved=self._voiceHistorySaved.emit,
        )
        # Voice History is now a projection of the same InteractionRecords
        # used for ASR/LLM/feedback training data. Keep the old attribute so
        # the QML and playback code remain stable.
        self._voice_history = self._modification_dataset
        self._voice_history_entries = self._voice_history.load_entries()
        self._register_association_actions()
        self._voice_audio_output: QAudioOutput | None = None
        self._voice_player: QMediaPlayer | None = None
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
        try:
            saved_asr_gain_db = float(
                self._settings.value("asr/gainDb", ASR_GAIN_DB_DEFAULT)
            )
        except (TypeError, ValueError):
            saved_asr_gain_db = ASR_GAIN_DB_DEFAULT
        if not math.isfinite(saved_asr_gain_db):
            saved_asr_gain_db = ASR_GAIN_DB_DEFAULT
        self._asr_gain_db = max(
            ASR_GAIN_DB_MIN, min(saved_asr_gain_db, ASR_GAIN_DB_MAX)
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
        self._funasr_hotwords = "\n".join(
            normalize_funasr_nano_hotwords(
                str(self._settings.value("asr/funasrNanoHotwords", ""))
            )
        )
        macos_output_migrated = self._bool_setting(
            "input/macosDesktopOutputMigrated", False
        )
        if sys.platform == "darwin" and not macos_output_migrated:
            # Older macOS builds forced this setting off because no adapter
            # existed. Enable the new native injector once, while preserving
            # the user's choice after this migration marker is stored.
            self._desktop_output = True
            self._settings.setValue("input/desktopOutput", True)
            self._settings.setValue("input/macosDesktopOutputMigrated", True)
        else:
            self._desktop_output = (
                DESKTOP_TEXT_INJECTION_SUPPORTED
                and self._bool_setting("input/desktopOutput", True)
            )
        self._push_to_talk = (
            WINDOWS_DESKTOP_INPUT_SUPPORTED
            and self._bool_setting("input/pushToTalk", True)
        )

        self._hide_overlay_timer = QTimer(self)
        self._hide_overlay_timer.setSingleShot(True)
        self._hide_overlay_timer.timeout.connect(self._hide_transcript)
        self._dictation_commit_timer = QTimer(self)
        self._dictation_commit_timer.setSingleShot(True)
        self._dictation_commit_timer.timeout.connect(
            self._commit_pending_dictation
        )
        self._processing_mode_correction_timer = QTimer(self)
        self._processing_mode_correction_timer.setSingleShot(True)
        self._processing_mode_correction_timer.timeout.connect(
            self._reveal_processing_mode_correction
        )
        self._applied_target_timer = QTimer(self)
        self._applied_target_timer.setInterval(300)
        self._applied_target_timer.timeout.connect(
            self._poll_applied_target_foreground
        )
        self._manual_association_timer = QTimer(self)
        self._manual_association_timer.setInterval(750)
        self._manual_association_timer.timeout.connect(
            self._poll_manual_association_result
        )
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(100)
        self._quit_timer.timeout.connect(self._finish_quit)
        self._accessibility_timer = QTimer(self)
        self._accessibility_timer.setInterval(1000)
        self._accessibility_timer.timeout.connect(
            self._poll_macos_accessibility
        )
        self._runtimeStatus.connect(self._apply_runtime_status)
        self._runtimeConnected.connect(self._apply_runtime_connected)
        self._runtimeDisconnected.connect(self._apply_runtime_disconnected)
        self._runtimeStarted.connect(self._apply_runtime_started)
        self._runtimeSessionStarted.connect(self._apply_runtime_session_started)
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
        self._associationActionRequested.connect(self._apply_association_action)
        self._voiceHistorySaved.connect(self._apply_voice_history_saved)
        self._text_processing_worker = TextProcessingWorker(
            on_result=self._textProcessed.emit,
            on_routing_result=self._inputModeRouted.emit,
            on_trace=self._llmTraceCollected.emit,
            on_warmup=lambda error, latency: self._llmWarmupFinished.emit(
                error or "", latency
            ),
        )
        if sys.platform == "darwin" and self._desktop_output:
            QTimer.singleShot(1000, self._request_macos_accessibility)

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

    @Property(bool, notify=transcriptChanged)
    def transcriptFinal(self) -> bool:
        return self._transcript_final

    @Property(bool, notify=transcriptChanged)
    def transcriptVisible(self) -> bool:
        return self._transcript_visible

    @Property(str, notify=sessionHistoryChanged)
    def sessionHistoryText(self) -> str:
        return "\n\n".join(self._session_history_lines)

    @Property("QVariantList", notify=voiceHistoryChanged)
    def voiceHistoryEntries(self) -> list[dict[str, object]]:
        return list(self._voice_history_entries)

    @Property(str, notify=playingVoiceChanged)
    def playingVoicePath(self) -> str:
        return self._playing_voice_path

    @Property(bool, notify=associationChanged)
    def smartAssociationEnabled(self) -> bool:
        return self._smart_association_enabled

    @smartAssociationEnabled.setter
    def smartAssociationEnabled(self, value: bool) -> None:
        enabled = bool(value)
        if enabled == self._smart_association_enabled:
            return
        self._smart_association_enabled = enabled
        self._settings.setValue(
            "dataCollection/smartAssociationEnabled", enabled
        )
        if not enabled:
            self._association_coordinator.clear()
            self._association_queue.clear()
            self._association_recommendation = None
            self._provisional_association_recommendations.clear()
            self._association_detail_visible = False
            self._stop_manual_association_watch()
        self.associationChanged.emit()

    @Property(bool, notify=associationChanged)
    def associationRecommendationVisible(self) -> bool:
        return self._association_recommendation is not None

    @Property(str, notify=associationChanged)
    def associationRecommendationTitle(self) -> str:
        recommendation = self._association_recommendation
        return recommendation.title if recommendation is not None else ""

    @Property(str, notify=associationChanged)
    def associationRecommendationPositiveLabel(self) -> str:
        recommendation = self._association_recommendation
        return recommendation.positive_label if recommendation is not None else ""

    @Property(str, notify=associationChanged)
    def associationRecommendationPositiveText(self) -> str:
        recommendation = self._association_recommendation
        return recommendation.positive_text if recommendation is not None else ""

    @Property(int, notify=associationChanged)
    def associationPopupTargetX(self) -> int:
        recommendation = self._association_recommendation
        return recommendation.chosen.target_x if recommendation is not None else 0

    @Property(int, notify=associationChanged)
    def associationPopupTargetY(self) -> int:
        recommendation = self._association_recommendation
        return recommendation.chosen.target_y if recommendation is not None else 0

    @Property(int, notify=associationChanged)
    def associationPopupTargetWidth(self) -> int:
        recommendation = self._association_recommendation
        return recommendation.chosen.target_width if recommendation is not None else 0

    @Property(int, notify=associationChanged)
    def associationPopupTargetHeight(self) -> int:
        recommendation = self._association_recommendation
        return recommendation.chosen.target_height if recommendation is not None else 0

    @Property(bool, notify=associationChanged)
    def associationDetailVisible(self) -> bool:
        return self._association_detail_visible

    @Property("QVariantList", notify=associationChanged)
    def associationDetailEntries(self) -> list[dict[str, object]]:
        recommendation = self._association_recommendation
        return recommendation.ui_entries() if recommendation is not None else []

    @Property(bool, notify=associationChanged)
    def associationCenterVisible(self) -> bool:
        return self._association_center_visible

    @Property(str, notify=associationChanged)
    def associationCenterStage(self) -> str:
        return self._association_center_stage

    @Property(str, notify=associationChanged)
    def associationCenterLastCreatedId(self) -> str:
        return self._association_center_last_created_id

    @Property(str, notify=associationChanged)
    def associationCenterKind(self) -> str:
        return self._association_center_kind

    @Property(str, notify=associationChanged)
    def associationCenterAsrSubtype(self) -> str:
        return self._association_center_asr_subtype

    @Property("QVariantList", notify=associationChanged)
    def associationCenterEntries(self) -> list[dict[str, object]]:
        return list(self._association_center_entries)

    @Property("QVariantList", notify=associationChanged)
    def associationCenterConfirmationEntries(self) -> list[dict[str, object]]:
        chosen, rejected = self._association_center_selected_entries()
        rows: list[dict[str, object]] = []
        if chosen is not None:
            rows.append({**chosen, "role": "chosen", "roleLabel": "正例"})
        rows.extend(
            {**item, "role": "rejected", "roleLabel": "反例"}
            for item in rejected
        )
        return rows

    @Property(str, notify=associationChanged)
    def associationCenterSelectionSummary(self) -> str:
        chosen = 1 if self._association_center_chosen_id else 0
        rejected = len(self._association_center_rejected_ids)
        return f"已选择：{chosen} 个正例 · {rejected} 个反例"

    @Property(bool, notify=associationChanged)
    def associationCenterCanSave(self) -> bool:
        return bool(
            self._association_center_kind
            and self._association_center_chosen_id
            and self._association_center_rejected_ids
        )

    @Slot(str, result=str)
    def associationCenterRole(self, interaction_id: str) -> str:
        value = str(interaction_id)
        if value == self._association_center_chosen_id:
            return "chosen"
        if value in self._association_center_rejected_ids:
            return "rejected"
        return ""

    @Slot(str, str)
    def performAssociationAction(self, action: str, payload: str = "") -> None:
        """Thread-safe command entry used by QML and future Ring gestures."""
        self._associationActionRequested.emit(str(action), str(payload))

    @Slot(str, str)
    def _apply_association_action(self, action: str, payload: str) -> None:
        if not self._association_actions.dispatch(action, payload):
            self._append_log(f"忽略未知关联动作：{action}")

    def _register_association_actions(self) -> None:
        handlers = {
            "recommendation.accept": lambda _payload: self._accept_recommendation(),
            "recommendation.reject": lambda _payload: self._reject_recommendation(),
            "recommendation.details.open": lambda _payload: self._set_association_details(True),
            "recommendation.details.close": lambda _payload: self._set_association_details(False),
            "center.open": lambda _payload: self._open_association_center(),
            "center.close": lambda _payload: self._close_association_center(),
            "center.create": lambda _payload: self._begin_association_center_draft(),
            "center.back": lambda _payload: self._back_association_center(),
            "center.kind": self._select_association_center_kind,
            "center.asrSubtype": self._select_association_center_asr_subtype,
            "center.chosen": lambda payload: self._set_association_center_role(payload, "chosen"),
            "center.rejected": lambda payload: self._set_association_center_role(payload, "rejected"),
            "center.clear": lambda _payload: self._clear_association_center_selection(),
            "center.confirm": lambda _payload: self._confirm_association_center_draft(),
            "center.commit": lambda _payload: self._save_association_center(),
            "center.loadMore": lambda _payload: self._load_more_association_candidates(),
        }
        for action, handler in handlers.items():
            self._association_actions.register(action, handler)

    def _set_association_details(self, visible: bool) -> None:
        self._association_detail_visible = bool(
            visible and self._association_recommendation is not None
        )
        if self._association_detail_visible:
            self._association_center_visible = False
        self.associationChanged.emit()

    def _accept_recommendation(self) -> None:
        recommendation = self._association_recommendation
        if recommendation is None:
            return
        try:
            association_id = self._modification_dataset.create_association(
                kind=recommendation.kind,
                subtype=recommendation.subtype,
                chosen=self._association_member_reference(recommendation.chosen),
                rejected=[
                    self._association_member_reference(item)
                    for item in recommendation.rejected
                ],
                source="auto_recommended",
                relation_type=recommendation.relation_type,
            )
        except BaseException as exc:
            self._append_log(f"关联推荐保存失败：{exc}")
            return
        self._append_log(
            f"已保存关联 {association_id}：1 个正例、"
            f"{len(recommendation.rejected)} 个反例"
        )
        applied = self._latest_operation()
        if (
            applied is not None
            and applied.session_id == recommendation.chosen.session_id
            and applied.mode == recommendation.chosen.mode
        ):
            self._commit_associated_result()
        self._advance_association_recommendation()

    def _reject_recommendation(self) -> None:
        if self._association_recommendation is None:
            return
        self._append_log("已忽略本次关联推荐")
        self._advance_association_recommendation()

    def _advance_association_recommendation(self) -> None:
        self._association_detail_visible = False
        self._association_recommendation = (
            self._association_queue.popleft() if self._association_queue else None
        )
        self.associationChanged.emit()

    def _open_association_center(self) -> None:
        self._association_detail_visible = False
        self._association_center_visible = True
        self._association_center_stage = "home"
        self._association_center_last_created_id = ""
        self._association_center_kind = ""
        self._association_center_entries = []
        self._clear_association_center_selection(emit=False)
        self.associationChanged.emit()

    def _close_association_center(self) -> None:
        self._association_center_visible = False
        self._association_center_stage = "home"
        self._association_center_last_created_id = ""
        self._association_center_kind = ""
        self._association_center_entries = []
        self._clear_association_center_selection(emit=False)
        self.associationChanged.emit()

    def _begin_association_center_draft(self) -> None:
        self._association_center_stage = "type"
        self._association_center_last_created_id = ""
        self._association_center_kind = ""
        self._association_center_entries = []
        self._clear_association_center_selection(emit=False)
        self.associationChanged.emit()

    def _back_association_center(self) -> None:
        if self._association_center_stage == "confirm":
            self._association_center_stage = "select"
        elif self._association_center_stage == "select":
            self._association_center_stage = "type"
            self._association_center_kind = ""
            self._association_center_entries = []
            self._clear_association_center_selection(emit=False)
        elif self._association_center_stage == "type":
            self._association_center_stage = "home"
        self.associationChanged.emit()

    def _select_association_center_kind(self, kind: str) -> None:
        normalized = str(kind).strip().lower()
        if normalized not in {ASSOCIATION_ASR, ASSOCIATION_LLM}:
            return
        if self._association_center_stage != "type":
            return
        self._association_center_kind = normalized
        self._association_center_stage = "select"
        self._association_center_limit = 40
        self._clear_association_center_selection(emit=False)
        self._reload_association_center()

    def _select_association_center_asr_subtype(self, subtype: str) -> None:
        normalized = str(subtype).strip()
        if normalized not in {ASR_DICTATION_RETRY, ASR_INSTRUCTION_RETRY}:
            return
        self._association_center_asr_subtype = normalized
        self._clear_association_center_selection(emit=False)
        if self._association_center_kind == ASSOCIATION_ASR:
            self._reload_association_center()

    def _reload_association_center(self) -> None:
        try:
            self._association_center_entries = (
                self._modification_dataset.load_association_candidates(
                    self._association_center_kind,
                    asr_subtype=self._association_center_asr_subtype,
                    limit=self._association_center_limit,
                )
            )
        except BaseException as exc:
            self._association_center_entries = []
            self._append_log(f"读取关联候选失败：{exc}")
        self.associationChanged.emit()

    def _load_more_association_candidates(self) -> None:
        if (
            self._association_center_stage != "select"
            or not self._association_center_kind
        ):
            return
        self._association_center_limit += 40
        self._reload_association_center()

    def _set_association_center_role(self, interaction_id: str, role: str) -> None:
        if self._association_center_stage != "select":
            return
        value = str(interaction_id).strip()
        candidate = next(
            (
                item for item in self._association_center_entries
                if str(item.get("interactionId", "")) == value
            ),
            None,
        )
        if not value or candidate is None:
            return
        if role == "chosen":
            positive = (
                str(
                    candidate.get("asrText", "")
                    or candidate.get("resultText", "")
                ).strip()
                if self._association_center_kind == ASSOCIATION_ASR
                else str(candidate.get("resultText", "")).strip()
            )
            if not positive:
                self._append_log("空结果不能设为正例")
                return
            self._association_center_chosen_id = (
                "" if self._association_center_chosen_id == value else value
            )
            self._association_center_rejected_ids.discard(value)
        else:
            if value in self._association_center_rejected_ids:
                self._association_center_rejected_ids.remove(value)
            else:
                self._association_center_rejected_ids.add(value)
                if self._association_center_chosen_id == value:
                    self._association_center_chosen_id = ""
        self.associationChanged.emit()

    def _clear_association_center_selection(self, *, emit: bool = True) -> None:
        self._association_center_chosen_id = ""
        self._association_center_rejected_ids.clear()
        if emit:
            self.associationChanged.emit()

    def _association_center_selected_entries(
        self,
    ) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
        chosen = next(
            (
                item
                for item in self._association_center_entries
                if str(item.get("interactionId", ""))
                == self._association_center_chosen_id
            ),
            None,
        )
        rejected = [
            item
            for item in self._association_center_entries
            if str(item.get("interactionId", ""))
            in self._association_center_rejected_ids
        ]
        return chosen, rejected

    def _confirm_association_center_draft(self) -> None:
        if self._association_center_stage != "select":
            return
        if not self.associationCenterCanSave:
            return
        self._association_center_stage = "confirm"
        self.associationChanged.emit()

    def _save_association_center(self) -> None:
        if (
            self._association_center_stage != "confirm"
            or not self.associationCenterCanSave
        ):
            return
        chosen, rejected = self._association_center_selected_entries()
        if chosen is None or not rejected:
            return
        subtype = (
            self._association_center_asr_subtype
            if self._association_center_kind == ASSOCIATION_ASR
            else "edit_preference"
        )
        try:
            association_id = self._modification_dataset.create_association(
                kind=self._association_center_kind,
                subtype=subtype,
                chosen=self._association_candidate_reference(chosen, chosen=True),
                rejected=[
                    self._association_candidate_reference(item, chosen=False)
                    for item in rejected
                ],
                source="manual_association_center",
                relation_type=(
                    "same_intent_retry"
                    if self._association_center_kind == ASSOCIATION_ASR
                    else ""
                ),
            )
        except BaseException as exc:
            self._append_log(f"手动关联保存失败：{exc}")
            return
        self._append_log(
            f"已保存关联 {association_id}：1 个正例、{len(rejected)} 个反例"
        )
        applied = self._latest_operation()
        if (
            applied is not None
            and self._modification_dataset.interaction_id_for_session(
                applied.session_id
            )
            == str(chosen.get("interactionId", ""))
        ):
            self._commit_associated_result()
        self._association_center_last_created_id = association_id
        self._association_center_stage = "home"
        self._association_center_kind = ""
        self._association_center_entries = []
        self._clear_association_center_selection(emit=False)
        self.associationChanged.emit()

    @staticmethod
    def _association_candidate_reference(
        candidate: dict, *, chosen: bool
    ) -> dict:
        reference = {
            "interaction_id": str(candidate.get("interactionId", "")),
            "request_id": int(candidate.get("requestId", 0) or 0),
        }
        if chosen and candidate.get("resultId"):
            reference["result_id"] = str(candidate["resultId"])
        return reference

    @staticmethod
    def _association_member_reference(member: AssociationMember) -> dict:
        return {
            "interaction_id": member.interaction_id,
            "request_id": member.request_id,
            "result_id": member.result_id,
        }

    @staticmethod
    def _association_target_key(target: DesktopTargetRef | None) -> str:
        if target is None:
            return ""
        return ":".join(
            (
                str(int(target.process_id)),
                str(int(target.window_handle)),
                str(int(target.control_handle)),
                str(target.process_name or ""),
                str(target.window_title or ""),
            )
        )

    def _association_member(
        self,
        *,
        session_id: int,
        target: DesktopTargetRef | None,
        mode: str,
        status: str,
    ) -> AssociationMember | None:
        target_key = self._association_target_key(target)
        if not target_key:
            return None
        try:
            value = self._modification_dataset.association_member_for_session(
                int(session_id),
                target_key=target_key,
                mode=normalize_input_mode(mode),
                status=status,
            )
        except BaseException as exc:
            self._append_log(f"读取关联记录失败：{exc}")
            return None
        if not value:
            return None
        return AssociationMember(
            interaction_id=str(value.get("interaction_id", "")),
            session_id=int(value.get("session_id", 0) or 0),
            request_id=int(value.get("request_id", 0) or 0),
            mode=str(value.get("mode", "")),
            target_key=str(value.get("target_key", "")),
            asr_text=str(value.get("asr_text", "")),
            result_text=str(value.get("result_text", "")),
            status=str(value.get("status", "")),
            audio_path=str(value.get("audio_path", "")),
            created_at=str(value.get("created_at", "")),
            target_x=int(target.screen_x),
            target_y=int(target.screen_y),
            target_width=int(target.screen_width),
            target_height=int(target.screen_height),
        )

    def _record_association_failure(
        self,
        *,
        session_id: int,
        target: DesktopTargetRef | None,
        mode: str,
        status: str,
    ) -> AssociationMember | None:
        if not self._smart_association_enabled:
            return None
        member = self._association_member(
            session_id=session_id,
            target=target,
            mode=mode,
            status=status,
        )
        if member is not None:
            self._association_coordinator.record_failure(member)
        return member

    def _record_association_success(
        self, interaction: _AppliedInteraction
    ) -> list[AssociationRecommendation]:
        self._provisional_association_recommendations = []
        if not self._smart_association_enabled:
            return []
        member = self._association_member(
            session_id=interaction.session_id,
            target=interaction.target,
            mode=interaction.mode,
            status="accepted",
        )
        if member is None:
            return []
        recommendations = self._association_coordinator.record_success(member)
        if not recommendations:
            return []
        self._provisional_association_recommendations = list(recommendations)
        self._association_queue.extend(recommendations)
        if self._association_recommendation is None:
            self._association_recommendation = self._association_queue.popleft()
        self.associationChanged.emit()
        return recommendations

    def _start_manual_association_watch(
        self,
        target: DesktopTargetRef | None,
        member: AssociationMember | None,
        *,
        baseline: str | None = None,
    ) -> None:
        if (
            not self._smart_association_enabled
            or target is None
            or member is None
        ):
            return
        adapter = self._desktop_target_adapter()
        observer = getattr(adapter, "observe_text", None)
        if not callable(observer):
            self._append_log("当前文本框不支持无干扰观察，已跳过手写结果自动关联")
            return
        if baseline is None:
            try:
                snapshot = observer(target)
                baseline = snapshot.text
            except BaseException:
                return
        self._manual_association_watch = (target, str(baseline), member)
        self._manual_association_watch_deadline = time.monotonic() + 60.0
        self._manual_association_candidate_text = ""
        self._manual_association_candidate_since = 0.0
        self._manual_association_timer.start()

    def _stop_manual_association_watch(self) -> None:
        self._manual_association_watch = None
        self._manual_association_watch_deadline = 0.0
        self._manual_association_candidate_text = ""
        self._manual_association_candidate_since = 0.0
        self._manual_association_timer.stop()

    @Slot()
    def _poll_manual_association_result(self) -> None:
        watched = self._manual_association_watch
        if watched is None or not self._smart_association_enabled:
            self._stop_manual_association_watch()
            return
        if time.monotonic() >= self._manual_association_watch_deadline:
            self._stop_manual_association_watch()
            return
        target, baseline, failed_member = watched
        try:
            observer = getattr(
                self._desktop_target_adapter(), "observe_text", None
            )
            if not callable(observer):
                self._stop_manual_association_watch()
                return
            snapshot = observer(target)
            current = snapshot.text
        except BaseException:
            return
        if current == baseline:
            self._manual_association_candidate_text = ""
            self._manual_association_candidate_since = 0.0
            return
        positive_text = (
            self._inserted_text(baseline, current)
            if failed_member.mode == INPUT_MODE_DICTATION
            else current
        )
        if not positive_text:
            return
        now = time.monotonic()
        if positive_text != self._manual_association_candidate_text:
            self._manual_association_candidate_text = positive_text
            self._manual_association_candidate_since = now
            return
        if now - self._manual_association_candidate_since < 1.5:
            return
        try:
            result_id = self._modification_dataset.record_manual_result(
                failed_member.interaction_id,
                text=positive_text,
                mode=failed_member.mode,
            )
        except BaseException as exc:
            self._append_log(f"人工结果保存失败：{exc}")
            return
        chosen = replace(
            failed_member,
            request_id=0,
            result_id=result_id,
            asr_text=(
                positive_text
                if failed_member.mode == INPUT_MODE_DICTATION
                else failed_member.asr_text
            ),
            result_text=positive_text,
            status="手动修改正例",
            occurred_monotonic=time.monotonic(),
        )
        recommendations = self._association_coordinator.record_manual_success(chosen)
        self._stop_manual_association_watch()
        if not recommendations:
            return
        self._association_queue.extend(recommendations)
        if self._association_recommendation is None:
            self._association_recommendation = self._association_queue.popleft()
        self.associationChanged.emit()

    @staticmethod
    def _inserted_text(before: str, after: str) -> str:
        chunks: list[str] = []
        for tag, _i1, _i2, j1, j2 in SequenceMatcher(
            None, str(before), str(after)
        ).get_opcodes():
            if tag in {"insert", "replace"}:
                chunks.append(str(after)[j1:j2])
        return "".join(chunks).strip()

    def _latest_operation(self) -> _AppliedInteraction | None:
        return self._operation_stack[-1] if self._operation_stack else None

    @staticmethod
    def _short_text(value: str, limit: int = 28) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else f"{text[:limit]}…"

    @classmethod
    def _processing_overlay_text(cls, mode: str, raw_text: str) -> str:
        """Show the recognized command only once an utterance is known as edit."""
        if normalize_input_mode(mode) != INPUT_MODE_EDIT:
            return "正在处理文本"
        instruction = cls._short_text(raw_text, limit=64)
        return (
            f"正在处理文本 · 指令：{instruction}"
            if instruction
            else "正在处理文本"
        )

    @classmethod
    def _edit_result_summary(cls, before: str, after: str) -> str:
        if not after:
            return "已清空当前文本"
        matcher = SequenceMatcher(None, str(before), str(after))
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            old = cls._short_text(str(before)[i1:i2])
            new = cls._short_text(str(after)[j1:j2])
            if tag == "insert":
                return f"已添加：“{new}”"
            if tag == "delete":
                return f"已删除：“{old}”"
            return f"已将“{old}”改为“{new}”"
        return "修改已应用"

    @Property(str, notify=interactionChanged)
    def interactionState(self) -> str:
        return self._interaction_state

    @Property(bool, notify=interactionChanged)
    def undoAvailable(self) -> bool:
        return bool(self._operation_stack)

    @Property(bool, notify=interactionChanged)
    def interactionCanCancel(self) -> bool:
        return bool(
            self._utterance_active
            or self._active_auto_interaction is not None
            or self._pending_text_requests
            or self._pending_mode_routes
            or self._pending_dictation_result is not None
            or self._interaction_state
            in {"listening", "processing"}
        )

    @Property(bool, notify=interactionChanged)
    def modeCorrectionAvailable(self) -> bool:
        operation = self._latest_operation()
        return bool(
            self._interaction_state == "applied"
            and operation is not None
            and operation.auto_context is not None
        )

    @Property(bool, notify=interactionChanged)
    def processingModeCorrectionAvailable(self) -> bool:
        interaction = self._active_auto_interaction
        return bool(
            self._processing_mode_correction_revealed
            and self._transcript_visible
            and self._interaction_state == "processing"
            and interaction is not None
            and interaction.classified
            and interaction.selected_mode == INPUT_MODE_EDIT
            and INPUT_MODE_DICTATION in getattr(interaction, "results", {})
        )

    @Property(str, notify=interactionChanged)
    def modeCorrectionLabel(self) -> str:
        operation = self._latest_operation()
        if operation is None:
            return ""
        return (
            "刚刚是输入内容"
            if operation.mode == INPUT_MODE_EDIT
            else "刚刚是指令"
        )

    @Property(bool, notify=interactionChanged)
    def appliedActionVisible(self) -> bool:
        return bool(
            self._applied_action_visible
            and self._applied_target_foreground
            and self._operation_stack
        )

    @Property(str, notify=interactionChanged)
    def appliedActionText(self) -> str:
        operation = self._latest_operation()
        return operation.summary if operation is not None else ""

    @Property(int, notify=interactionChanged)
    def undoDepth(self) -> int:
        return len(self._operation_stack)

    @Property(int, notify=interactionChanged)
    def appliedPopupTargetX(self) -> int:
        operation = self._latest_operation()
        return operation.target.screen_x if operation is not None else 0

    @Property(int, notify=interactionChanged)
    def appliedPopupTargetY(self) -> int:
        operation = self._latest_operation()
        return operation.target.screen_y if operation is not None else 0

    @Property(int, notify=interactionChanged)
    def appliedPopupTargetWidth(self) -> int:
        operation = self._latest_operation()
        return operation.target.screen_width if operation is not None else 0

    @Property(int, notify=interactionChanged)
    def appliedPopupTargetHeight(self) -> int:
        operation = self._latest_operation()
        return operation.target.screen_height if operation is not None else 0

    @Property(int, notify=interactionChanged)
    def appliedPopupCaretX(self) -> int:
        operation = self._latest_operation()
        if operation is None:
            return 0
        target = operation.target
        if target.caret_height > 0:
            return target.caret_x
        if target.screen_width > 0 and target.screen_height > 0:
            return target.screen_x + target.screen_width
        return 0

    @Property(int, notify=interactionChanged)
    def appliedPopupCaretY(self) -> int:
        operation = self._latest_operation()
        if operation is None:
            return 0
        target = operation.target
        if target.caret_height > 0:
            return target.caret_y
        if target.screen_width > 0 and target.screen_height > 0:
            return target.screen_y
        return 0

    @Property(int, notify=interactionChanged)
    def appliedPopupCaretWidth(self) -> int:
        operation = self._latest_operation()
        if operation is None:
            return 0
        target = operation.target
        if target.caret_height > 0:
            return target.caret_width
        return 2 if target.screen_width > 0 and target.screen_height > 0 else 0

    @Property(int, notify=interactionChanged)
    def appliedPopupCaretHeight(self) -> int:
        operation = self._latest_operation()
        if operation is None:
            return 0
        target = operation.target
        if target.caret_height > 0:
            return target.caret_height
        if target.screen_width > 0 and target.screen_height > 0:
            return min(24, target.screen_height)
        return 0

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

    @Property(float, notify=settingsChanged)
    def asrGainDb(self) -> float:
        return self._asr_gain_db

    @asrGainDb.setter
    def asrGainDb(self, value: float) -> None:
        try:
            gain_db = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(gain_db):
            return
        gain_db = round(
            max(ASR_GAIN_DB_MIN, min(gain_db, ASR_GAIN_DB_MAX)), 1
        )
        if gain_db == self._asr_gain_db:
            return
        self._set_setting("_asr_gain_db", gain_db, "asr/gainDb")
        self._append_log(f"ASR 输入增益已设为 {gain_db:+.1f} dB，重新连接后生效")

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
        enabled = bool(value) and DESKTOP_TEXT_INJECTION_SUPPORTED
        if enabled == self._desktop_output:
            return
        self._desktop_output = enabled
        self._settings.setValue("input/desktopOutput", enabled)
        self.settingsChanged.emit()
        self.accessibilityChanged.emit()
        if sys.platform == "darwin" and enabled:
            QTimer.singleShot(0, self._request_macos_accessibility)
        elif not enabled:
            self._accessibility_timer.stop()

    @Property(bool, notify=accessibilityChanged)
    def macOSAccessibilityRequired(self) -> bool:
        return (
            sys.platform == "darwin"
            and self._desktop_output
            and not self._macos_accessibility_trusted
        )

    @Slot()
    def openMacOSAccessibilitySettings(self) -> None:
        if sys.platform != "darwin":
            return
        self._request_macos_accessibility()
        if not self._macos_accessibility_trusted:
            QDesktopServices.openUrl(
                QUrl(
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Accessibility"
                )
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
        self._interaction_recognition_suspended = False
        self._cancelled_asr_session_ids.clear()
        self._ignore_asr_updates_until_next_start = False

        self._disconnect_event = threading.Event()
        self._recognition_event = threading.Event()
        self._cancel_utterance_event = threading.Event()
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
                    cancel_utterance_event=self._cancel_utterance_event,
                    on_update=self._publish_update,
                    on_state=self._runtimeStatus.emit,
                    on_connected=self._runtimeConnected.emit,
                    on_disconnected=self._runtimeDisconnected.emit,
                    on_started=self._runtimeStarted.emit,
                    on_session_started=self._runtimeSessionStarted.emit,
                    on_push_to_talk=self._pushToTalkChanged.emit,
                    on_raw_audio=self._record_raw_interaction_audio,
                    on_raw_imu=self._record_raw_imu_samples,
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

    def _record_raw_interaction_audio(self, session_id: int, audio_16k) -> None:
        try:
            self._modification_dataset.record_audio(session_id, audio_16k)
        except BaseException as exc:
            print(f"[dataset] raw interaction audio was not saved: {exc}")

    @Slot(int)
    def _apply_runtime_session_started(self, session_id: int) -> None:
        """Bind UI cancellation and detector evidence before ASR emits text."""
        normalized = int(session_id)
        if normalized <= 0:
            return
        self._latest_asr_session_id = normalized
        try:
            self._modification_dataset.begin_session(normalized)
        except BaseException as exc:
            self._append_log(f"语音会话记录初始化失败：{exc}")

    def _record_raw_imu_samples(
        self,
        session_id: int,
        samples,
        metadata: dict,
    ) -> None:
        try:
            self._modification_dataset.record_imu_samples(
                session_id,
                samples,
                sample_rate_hz=metadata.get("sample_rate_hz"),
                dropped_samples=int(metadata.get("dropped_samples", 0)),
                alignment_method=metadata.get("alignment_method", ""),
            )
        except BaseException as exc:
            # IMU is evidence collection only; do not disturb recognition.
            print(f"[dataset] utterance IMU was not saved: {exc}")

    @Slot()
    def startRecognition(self) -> None:
        if not self._connected or self._busy or self._recognition_enabled:
            return
        self._interaction_recognition_suspended = False
        self._recognition_event.set()
        self._recognition_enabled = True
        self.recognitionEnabledChanged.emit()
        self.runningChanged.emit()
        detail = "靠近说话"
        if self._push_to_talk:
            detail += "，或按住右 Alt"
        self._set_status("自动监听中", detail, "running")
        self._append_log("语音识别已开启（设备保持连接）")

    def _suspend_recognition_for_interaction(self) -> None:
        """Close the runtime gate without changing the user's on/off choice."""
        if self._recognition_enabled and self._recognition_event.is_set():
            self._interaction_recognition_suspended = True
            self._recognition_event.clear()

    def _resume_recognition_after_interaction(self) -> None:
        if not self._interaction_recognition_suspended:
            return
        self._interaction_recognition_suspended = False
        if (
            self._connected
            and self._recognition_enabled
            and not self._quitting
            and not self._disconnect_event.is_set()
        ):
            self._recognition_event.set()
            self._append_log("当前语句处理完成，已恢复下一段语音识别")

    @Slot()
    def pauseRecognition(self) -> None:
        if not self._connected or not self._recognition_enabled:
            return
        self._recognition_event.clear()
        self._interaction_recognition_suspended = False
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
        self._interaction_recognition_suspended = False
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
    def clearVoiceHistory(self) -> None:
        if self._voice_player is not None:
            self._voice_player.stop()
        self._playing_voice_path = ""
        self.playingVoiceChanged.emit()
        try:
            self._voice_history.clear()
        except BaseException as exc:
            self._append_log(f"逐句语音记录清空失败：{exc}")
            return
        self._voice_history_entries.clear()
        self._association_coordinator.clear()
        self._association_queue.clear()
        self._association_recommendation = None
        self._association_detail_visible = False
        self._association_center_entries = []
        self._clear_association_center_selection(emit=False)
        self.voiceHistoryChanged.emit()
        self.associationChanged.emit()
        self._append_log("逐句语音记录已清空")

    @Slot()
    def openDataDirectory(self) -> None:
        directory = self._modification_dataset.user_root
        try:
            directory.mkdir(parents=True, exist_ok=True)
            _open_data_directory(directory)
        except (OSError, RuntimeError, ValueError) as exc:
            self._append_log(f"无法打开数据目录：{exc}")
            return
        self._append_log(
            "已在上级目录中显示数据文件夹；逐句主记录位于 interactions，"
            "关联索引为 associations.jsonl"
        )

    @Slot(str)
    def playVoiceHistory(self, audio_path: str) -> None:
        resolved = self._validated_voice_history_path(audio_path, "播放")
        if resolved is None:
            return
        player = self._ensure_voice_player()
        if (
            self._playing_voice_path == str(resolved)
            and player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        ):
            player.stop()
            return
        player.setSource(QUrl.fromLocalFile(str(resolved)))
        self._playing_voice_path = str(resolved)
        self.playingVoiceChanged.emit()
        player.play()

    @Slot(str)
    def openVoiceHistoryLocation(self, record_path: str) -> None:
        resolved = self._validated_voice_history_path(record_path, "打开本条记录")
        if resolved is None:
            return
        try:
            _open_voice_history_location(resolved)
        except (OSError, RuntimeError) as exc:
            self._append_log(f"无法打开语音文件位置：{exc}")
            return
        self._append_log(f"已打开语音文件位置：{resolved.name}")

    def _validated_voice_history_path(
        self, audio_path: str, action: str
    ) -> Path | None:
        try:
            return _resolve_voice_history_path(
                audio_path, self._modification_dataset.user_root
            )
        except (OSError, ValueError):
            pass
        self._append_log(f"无法{action}：语音记录文件不存在或路径无效")
        return None

    def _ensure_voice_player(self) -> QMediaPlayer:
        if self._voice_player is not None:
            return self._voice_player
        self._voice_audio_output = QAudioOutput(self)
        self._voice_player = QMediaPlayer(self)
        self._voice_player.setAudioOutput(self._voice_audio_output)
        self._voice_player.playbackStateChanged.connect(
            self._apply_voice_playback_state
        )
        self._voice_player.errorOccurred.connect(self._apply_voice_playback_error)
        return self._voice_player

    @Slot(object)
    def _apply_voice_history_saved(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        saved = dict(entry)
        interaction_id = str(saved.get("interactionId", "") or saved.get("id", ""))
        existing_index = next(
            (
                index
                for index, item in enumerate(self._voice_history_entries)
                if str(item.get("interactionId", "") or item.get("id", ""))
                == interaction_id
            ),
            None,
        )
        if existing_index is None:
            self._voice_history_entries.insert(0, saved)
        else:
            self._voice_history_entries[existing_index] = saved
        del self._voice_history_entries[100:]
        self.voiceHistoryChanged.emit()

    def _refresh_voice_history_entries(self) -> None:
        try:
            entries = self._voice_history.load_entries()
        except BaseException as exc:
            self._append_log(f"刷新逐句语音记录失败：{exc}")
            return
        if entries == self._voice_history_entries:
            return
        self._voice_history_entries = entries
        self.voiceHistoryChanged.emit()

    def _apply_voice_playback_state(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.StoppedState and self._playing_voice_path:
            self._playing_voice_path = ""
            self.playingVoiceChanged.emit()

    def _apply_voice_playback_error(self, error, error_string: str = "") -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        player_error = self._voice_player.errorString() if self._voice_player else ""
        detail = str(error_string or player_error).strip()
        self._append_log(f"语音记录播放失败：{detail or error}")

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
            self._close_voice_history()
            QCoreApplication.quit()
            return
        self._quitting = True
        self._cancel_pending_text_processing()
        self._text_processing_worker.close(wait=False)
        self._quit_wait_ticks = 0
        self.disconnectDevice()
        if self._worker is not None and self._worker.is_alive():
            self._quit_timer.start()
        else:
            self._close_voice_history()
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
            self._close_voice_history()
            QCoreApplication.quit()

    def _close_voice_history(self) -> None:
        if self._voice_history_closed:
            return
        self._voice_history_closed = True
        self._applied_target_timer.stop()
        self._stop_manual_association_watch()
        if self._voice_player is not None:
            self._voice_player.stop()
        self._voice_history.close(wait=True)

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
        session_id = int(getattr(update, "session_id", 0))
        if (
            self._ignore_asr_updates_until_next_start
            or session_id in self._cancelled_asr_session_ids
        ):
            return
        if bool(getattr(update, "is_final", False)):
            # This callback runs in the recognition thread. Clear the physical
            # gate before posting the final into Qt so another utterance cannot
            # begin while routing, text processing, injection, or review runs.
            self._suspend_recognition_for_interaction()
        try:
            self._modification_dataset.record_asr_update(update)
        except BaseException as exc:
            print(f"[dataset] ASR update was not saved: {exc}")
        self._runtimeUpdate.emit(
            str(update.text or ""),
            bool(update.is_final),
            str(update.error or ""),
            session_id,
        )

    @Slot(str)
    def _apply_runtime_status(self, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        summary = text.splitlines()[0].strip()
        self._append_log(text)
        if summary.startswith(("STAGE1 ", "STAGE2 ", "[ASR TIMING]")):
            try:
                self._modification_dataset.record_runtime_event(
                    text, self._latest_asr_session_id
                )
            except BaseException as exc:
                self._append_log(f"检测证据保存失败：{exc}")
        if summary.startswith("[ASR] START"):
            # Stage2 activation is the authoritative start of a detected voice
            # session.  Show the overlay now, before ASR has any text to emit.
            self._stop_manual_association_watch()
            self._applied_action_visible = False
            self._ignore_asr_updates_until_next_start = False
            self._latest_asr_session_id = 0
            self._utterance_active = True
            self._speech_start_target = self._capture_desktop_reference()
            self._transcript_text = "正在收听语音"
            self._transcript_mode = ""
            self._transcript_final = False
            self._transcript_visible = True
            self._hide_overlay_timer.stop()
            self._set_interaction_state("listening")
            self.transcriptChanged.emit()
            self.interactionChanged.emit()
            if self._recognition_enabled:
                self._set_status("正在聆听", "Esc 可随时取消本句", "listening")
            return
        if summary.startswith("[ASR] END"):
            self._utterance_active = False
            if self._interaction_state == "listening":
                self._transcript_text = "正在处理语音"
                self._set_interaction_state("processing")
                self.transcriptChanged.emit()
                self.interactionChanged.emit()
            return
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
        elif summary.startswith("正在检查并下载 ASR 模型参数"):
            self._set_status("正在下载模型参数", summary, "starting")
        elif summary.startswith("正在读取本地 ASR 模型参数"):
            self._set_status("正在读取模型参数", summary, "starting")
        elif summary.startswith("ASR 模型参数已载入"):
            self._set_status("模型参数已载入", summary, "starting")
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
        self._interaction_recognition_suspended = False
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
        if int(session_id) in self._cancelled_asr_session_ids:
            return
        if self._ignore_asr_updates_until_next_start:
            if session_id:
                self._cancelled_asr_session_ids.add(int(session_id))
            return
        if session_id:
            self._latest_asr_session_id = int(session_id)
        if is_final:
            self._utterance_active = False
            self._suspend_recognition_for_interaction()
        if error:
            if session_id:
                self._session_input_modes.pop(int(session_id), None)
                self._session_routing_modes.pop(int(session_id), None)
                self._session_targets.pop(int(session_id), None)
            self._transcript_text = "语音识别失败"
            self._transcript_mode = ""
            self._transcript_final = True
            self._transcript_visible = True
            self._set_interaction_state("error")
            self.transcriptChanged.emit()
            self._hide_overlay_timer.start(3500)
            self._append_log(f"ASR 错误：{error}")
            if session_id:
                try:
                    self._modification_dataset.record_asr_label(
                        int(session_id), label="negative", source="asr_error"
                    )
                except BaseException as exc:
                    self._append_log(f"ASR 错误标签保存失败：{exc}")
            self._resume_recognition_after_interaction()
            return
        text = text.strip()
        if not text:
            if is_final:
                self._append_log("本段语音已结束，但没有识别出文字")
                failed_mode = self._session_input_modes.get(
                    int(session_id), self._input_mode
                )
                failed_target = self._session_targets.get(
                    int(session_id), self._speech_start_target
                )
                try:
                    self._modification_dataset.record_asr_label(
                        int(session_id), label="negative", source="empty_final"
                    )
                except BaseException as exc:
                    self._append_log(f"ASR 空结果标签保存失败：{exc}")
                try:
                    self._modification_dataset.record_application(
                        action="no_result",
                        session_id=int(session_id),
                        mode=failed_mode,
                        application=(
                            failed_target.process_name or failed_target.window_title
                            if failed_target is not None
                            else ""
                        ),
                        target_key=self._association_target_key(failed_target),
                    )
                except BaseException as exc:
                    self._append_log(f"ASR 空结果状态保存失败：{exc}")
                failed_member = self._record_association_failure(
                    session_id=int(session_id),
                    target=failed_target,
                    mode=failed_mode,
                    status="未识别",
                )
                self._start_manual_association_watch(
                    failed_target, failed_member
                )
                if session_id:
                    self._session_input_modes.pop(int(session_id), None)
                    self._session_routing_modes.pop(int(session_id), None)
                    self._session_targets.pop(int(session_id), None)
                self._speech_start_target = None
                self._transcript_text = "未识别到语音"
                self._transcript_mode = ""
                self._transcript_final = True
                self._transcript_visible = True
                self._set_interaction_state("no_result")
                self.transcriptChanged.emit()
                self.interactionChanged.emit()
                self._hide_overlay_timer.start(1500)
                if self._recognition_enabled:
                    self._set_status(
                        "自动监听中", "未识别到文字，等待下一段语音", "running"
                    )
                self._resume_recognition_after_interaction()
            return
        if not is_final:
            self._transcript_mode = ""
            self._transcript_text = "正在收听语音"
            self._transcript_final = False
            self._transcript_visible = True
            self._set_interaction_state("listening")
            self.transcriptChanged.emit()
            self._hide_overlay_timer.stop()
            if self._recognition_enabled:
                self._set_status("正在识别", "正在接收语音", "listening")
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
                    self._speech_start_target
                    if self._speech_start_target is not None
                    else self._capture_desktop_reference()
                )
        else:
            mode = self._input_mode
            routing_mode = self._input_routing_mode
        self._transcript_mode = (
            mode if routing_mode == INPUT_ROUTING_MANUAL else ""
        )
        self._transcript_text = (
            self._processing_overlay_text(mode, text)
            if is_final and routing_mode == INPUT_ROUTING_MANUAL
            else ("正在处理文本" if is_final else "正在收听语音")
        )
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
                self._prepare_manual_candidates(
                    text, normalized_session_id, mode, target
                )

    def _prepare_manual_candidates(
        self,
        text: str,
        session_id: int,
        mode: str,
        target: DesktopTargetRef | None,
    ) -> None:
        """Use the same two-candidate pipeline without invoking the router."""
        self._text_request_id += 1
        route_id = self._text_request_id
        selected = normalize_input_mode(mode)
        self._prepare_auto_candidates(
            route_id,
            int(session_id),
            str(text),
            target,
            selected,
        )
        interaction = self._active_auto_interaction
        if interaction is None:
            return
        interaction.selected_mode = selected
        interaction.classified = True
        interaction.routed_by_model = False
        interaction.routed_at = time.monotonic()
        self._present_auto_selection(target)

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
        try:
            self._modification_dataset.record_routing_request(request)
        except BaseException as exc:
            self._append_log(f"输入类型路由数据保存失败：{exc}")
        if not was_processing:
            self.textProcessingChanged.emit()
        self._transcript_text = "正在处理文本"
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
        self._prepare_auto_candidates(
            request_id,
            int(session_id),
            text,
            target,
            normalize_input_mode(fallback_mode),
        )

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
        if self._quitting or self._status_kind == "stopping":
            return
        try:
            self._modification_dataset.record_routing_result(result)
        except BaseException as exc:
            self._append_log(f"输入类型路由结果保存失败：{exc}")
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
        self._start_auto_candidates(result, context.target)

    def _prepare_auto_candidates(
        self,
        route_id: int,
        session_id: int,
        raw_text: str,
        target: DesktopTargetRef | None,
        fallback_mode: str,
    ) -> None:
        """Start routing-independent candidates while classification runs."""
        interaction = _AutoInteraction(
            route_id=route_id,
            session_id=session_id,
            raw_text=raw_text,
            target=target,
            selected_mode=normalize_input_mode(fallback_mode),
            routed_at=0.0,
        )
        self._active_auto_interaction = interaction
        # The ASR final is always a complete, immediately usable dictation
        # candidate. Optional LLM cleanup may replace it if it finishes inside
        # the short correction window, but must never delay injection.
        interaction.results[INPUT_MODE_DICTATION] = TextProcessingResult(
            request_id=0,
            session_id=session_id,
            mode=INPUT_MODE_DICTATION,
            raw_text=raw_text,
            final_text=raw_text,
            latency_s=0.0,
            used_llm=False,
        )
        if target is None:
            interaction.candidate_errors[INPUT_MODE_EDIT] = (
                "没有锁定外部文本框，无法按编辑指令执行"
            )
        else:
            try:
                interaction.snapshot = self._desktop_target_adapter().capture_text(
                    target
                )
                validate_edit_target_text(interaction.snapshot.text)
                self._log_edit_target_snapshot(interaction.snapshot)
            except BaseException as exc:
                interaction.candidate_errors[INPUT_MODE_EDIT] = str(exc)

        for mode in (INPUT_MODE_DICTATION, INPUT_MODE_EDIT):
            if mode == INPUT_MODE_DICTATION and not self._llm_enabled:
                continue
            if mode == INPUT_MODE_EDIT and interaction.snapshot is None:
                continue
            self._submit_text_processing(
                raw_text,
                session_id,
                mode,
                target_text=(
                    interaction.snapshot.text
                    if mode == INPUT_MODE_EDIT and interaction.snapshot is not None
                    else ""
                ),
                target=target,
                snapshot=(interaction.snapshot if mode == INPUT_MODE_EDIT else None),
                auto_route_id=route_id,
                update_overlay=False,
            )

        interaction.preparing = False

    def _start_auto_candidates(
        self,
        route: InputModeRoutingResult,
        target: DesktopTargetRef | None,
    ) -> None:
        """Select one of the two candidates already running for this utterance."""
        interaction = self._active_auto_interaction
        if interaction is None or interaction.route_id != route.request_id:
            # Compatibility for callers that deliver a route result directly.
            self._prepare_auto_candidates(
                route.request_id,
                route.session_id,
                route.raw_text,
                target,
                route.mode,
            )
            interaction = self._active_auto_interaction
        if interaction is None:
            return
        selected = normalize_input_mode(route.mode)
        interaction.selected_mode = selected
        interaction.classified = True
        interaction.routed_by_model = not bool(route.error)
        interaction.routed_at = time.monotonic()

        self._transcript_mode = ""
        self._transcript_text = self._processing_overlay_text(
            selected, interaction.raw_text
        )
        self._transcript_final = False
        self._transcript_visible = True
        self._set_interaction_state("processing")
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._hide_overlay_timer.stop()
        self._schedule_processing_mode_correction(selected, interaction)
        if selected in interaction.results or selected in interaction.candidate_errors:
            self._present_auto_selection(target)
        if not self.textProcessing:
            self.textProcessingChanged.emit()

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
        self._resume_recognition_after_interaction()

    def _submit_text_processing(
        self,
        text: str,
        session_id: int,
        mode: str,
        *,
        target_text: str = "",
        target: DesktopTargetRef | None = None,
        snapshot: DesktopTextSnapshot | None = None,
        auto_route_id: int = 0,
        update_overlay: bool = True,
    ) -> None:
        normalized_mode = normalize_input_mode(mode)
        if normalized_mode != INPUT_MODE_EDIT and not self._llm_enabled:
            self._append_log("输入模式已跳过文本大模型，直接采用 ASR 最终结果")
            immediate_result = TextProcessingResult(
                    request_id=0,
                    session_id=int(session_id),
                    mode=normalized_mode,
                    raw_text=text,
                    final_text=text,
                    latency_s=0.0,
                    used_llm=False,
                )
            if auto_route_id:
                self._accept_auto_candidate(auto_route_id, immediate_result, target)
            else:
                self._commit_input_text(immediate_result, target)
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
        was_processing = bool(self._pending_text_requests)
        self._pending_text_requests.add(request_id)
        self._pending_interactions[request_id] = _PendingInteraction(
            target=target,
            snapshot=snapshot,
            auto_route_id=int(auto_route_id),
        )
        try:
            self._modification_dataset.record_text_request(request)
        except BaseException as exc:
            self._append_log(f"统一交互 LLM 输入保存失败：{exc}")
        if auto_route_id and self._active_auto_interaction is not None:
            self._active_auto_interaction.request_ids[normalized_mode] = request_id
        if not was_processing:
            self.textProcessingChanged.emit()
        label = "修改" if normalized_mode == INPUT_MODE_EDIT else "输入"
        if update_overlay:
            self._transcript_text = self._processing_overlay_text(
                normalized_mode, text
            )
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
        try:
            self._modification_dataset.record_llm_result(
                result.request_id, result
            )
        except BaseException as exc:
            self._append_log(f"统一交互 LLM 结果保存失败：{exc}")
        else:
            self._refresh_voice_history_entries()
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
            # Auto routing deliberately computes both candidates before the
            # classifier finishes.  Cache failures just like successes; an
            # unselected edit failure must never open a review (or expose the
            # correction control) before classification has completed.
            if result.mode == INPUT_MODE_EDIT and not context.auto_route_id:
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
        if context.auto_route_id:
            self._accept_auto_candidate(
                context.auto_route_id, result, context.target
            )
            return
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

    def _accept_auto_candidate(
        self,
        route_id: int,
        result: TextProcessingResult,
        target: DesktopTargetRef | None,
    ) -> None:
        interaction = self._active_auto_interaction
        if interaction is None or interaction.route_id != int(route_id):
            interaction = next(
                (
                    item.auto_context
                    for item in reversed(self._operation_stack)
                    if item.auto_context is not None
                    and item.auto_context.route_id == int(route_id)
                ),
                None,
            )
        if interaction is None:
            return
        interaction.results[result.mode] = result
        if self._pending_applied_mode_switch == (int(route_id), result.mode):
            self._pending_applied_mode_switch = None
            self._apply_alternate_result(result.mode)
            return
        if (
            interaction is self._active_auto_interaction
            and
            result.mode == interaction.selected_mode
            and not interaction.preparing
            and interaction.classified
        ):
            self._present_auto_selection(target)

    def _present_auto_selection(
        self, target: DesktopTargetRef | None = None
    ) -> None:
        interaction = self._active_auto_interaction
        if interaction is None:
            return
        mode = interaction.selected_mode
        error = interaction.candidate_errors.get(mode, "")
        if error:
            self._transcript_mode = ""
            self._transcript_text = "未能处理文本"
            self._transcript_final = True
            self._set_interaction_state("error")
            self.transcriptChanged.emit()
            self.interactionChanged.emit()
            self._append_log(f"文本处理失败：{error}")
            return
        result = interaction.results.get(mode)
        if result is None:
            self._transcript_mode = ""
            self._transcript_text = self._processing_overlay_text(
                mode, interaction.raw_text
            )
            self._transcript_final = False
            self._set_interaction_state("processing")
            self.transcriptChanged.emit()
            self.interactionChanged.emit()
            return
        if mode == INPUT_MODE_DICTATION:
            self._pending_dictation_result = (
                result,
                target if target is not None else interaction.target,
            )
            self._transcript_mode = ""
            self._transcript_text = "正在处理文本"
            self._transcript_final = True
            self._set_interaction_state("processing")
            self.transcriptChanged.emit()
            self.interactionChanged.emit()
            elapsed_ms = int(
                max(0.0, time.monotonic() - interaction.routed_at) * 1000
            )
            remaining_ms = max(0, _DICTATION_CORRECTION_GRACE_MS - elapsed_ms)
            if remaining_ms:
                self._dictation_commit_timer.start(remaining_ms)
            else:
                self._commit_pending_dictation()
            return

        snapshot = interaction.snapshot
        if snapshot is None:
            return
        parsed: object = None
        if result.model_output:
            try:
                parsed = json.loads(result.model_output)
            except (TypeError, json.JSONDecodeError):
                pass
        if result.error or result.final_text == result.target_text:
            self._begin_failed_edit_review(
                replace(
                    result,
                    error=result.error or "大模型未找到可可靠执行的修改",
                ),
                snapshot,
            )
            return
        self._begin_edit_review(
            result,
            snapshot,
            allow_empty=_is_explicit_emptying_edit_response(
                parsed, result.target_text
            ),
        )

    @Slot()
    def _commit_pending_dictation(self) -> None:
        pending = self._pending_dictation_result
        if pending is None:
            return
        self._pending_dictation_result = None
        result, target = pending
        self._commit_input_text(result, target)

    @Slot()
    def switchCurrentInputMode(self) -> None:
        """Switch a processing or applied utterance to its other interpretation."""
        processing = self._active_auto_interaction
        if (
            processing is not None
            and processing.classified
            and processing.selected_mode == INPUT_MODE_EDIT
        ):
            result = processing.results.get(INPUT_MODE_DICTATION)
            if result is None or result.error:
                return
            was_model_routed = processing.routed_by_model
            processing.selected_mode = INPUT_MODE_DICTATION
            self._processing_mode_correction_timer.stop()
            self._processing_mode_correction_revealed = False
            self._pending_dictation_result = None
            self._dictation_commit_timer.stop()
            self.interactionChanged.emit()
            self._commit_input_text(
                result,
                processing.target,
            )
            if was_model_routed:
                try:
                    self._modification_dataset.record_mode_correction(
                        processing.session_id,
                        previous_mode=INPUT_MODE_EDIT,
                        corrected_mode=INPUT_MODE_DICTATION,
                    )
                except BaseException as exc:
                    self._append_log(f"输入类型纠正数据保存失败：{exc}")
            self._append_log("用户已指明“刚刚是输入内容”，改为听写输入")
            return

        operation = self._latest_operation()
        if operation is None or operation.auto_context is None:
            return
        interaction = operation.auto_context
        alternate_mode = (
            INPUT_MODE_DICTATION
            if operation.mode == INPUT_MODE_EDIT
            else INPUT_MODE_EDIT
        )
        if alternate_mode in interaction.candidate_errors:
            operation.summary = "另一种处理方式不可用"
            self.interactionChanged.emit()
            return
        result = interaction.results.get(alternate_mode)
        if result is None:
            self._pending_applied_mode_switch = (
                interaction.route_id,
                alternate_mode,
            )
            operation.summary = "正在准备另一种处理结果…"
            self.interactionChanged.emit()
            return
        self._apply_alternate_result(alternate_mode)

    def _apply_alternate_result(self, alternate_mode: str) -> None:
        operation = self._latest_operation()
        if operation is None or operation.auto_context is None:
            return
        interaction = operation.auto_context
        result = interaction.results.get(alternate_mode)
        if result is None or result.error:
            operation.summary = "另一种处理方式不可用"
            self.interactionChanged.emit()
            return
        if alternate_mode == INPUT_MODE_EDIT:
            if (
                interaction.snapshot is None
                or result.final_text == result.target_text
                or not str(result.final_text or "").strip()
            ):
                operation.summary = "没有可应用的编辑结果"
                self.interactionChanged.emit()
                return
        elif not str(result.final_text or result.raw_text or "").strip():
            operation.summary = "没有可应用的听写结果"
            self.interactionChanged.emit()
            return

        previous_mode = operation.mode
        try:
            self._undo_applied_operation(operation)
        except BaseException as exc:
            operation.summary = f"无法切换：{exc}"
            self.interactionChanged.emit()
            return

        self._operation_stack.pop()
        self._active_auto_interaction = interaction
        interaction.selected_mode = alternate_mode
        before_depth = len(self._operation_stack)
        if alternate_mode == INPUT_MODE_DICTATION:
            self._commit_input_text(result, operation.target)
        else:
            self._begin_edit_review(result, interaction.snapshot)

        replacement = self._latest_operation()
        succeeded = (
            len(self._operation_stack) == before_depth + 1
            and replacement is not None
            and replacement.session_id == operation.session_id
            and replacement.mode == alternate_mode
        )
        if not succeeded:
            try:
                self._restore_applied_operation(operation)
            except BaseException as exc:
                self._append_log(f"切换失败且原结果恢复失败：{exc}")
            else:
                self._operation_stack.append(operation)
                self._applied_action_visible = True
                try:
                    self._modification_dataset.record_application(
                        action="applied",
                        session_id=operation.session_id,
                        request_id=operation.request_id,
                        mode=operation.mode,
                        application=(
                            operation.target.process_name
                            or operation.target.window_title
                        ),
                        target_key=self._association_target_key(operation.target),
                        before_text=(
                            operation.original_snapshot.text
                            if operation.original_snapshot is not None
                            else None
                        ),
                        candidate_text=operation.applied_text,
                        final_text=operation.applied_text,
                        method="alternate_failed_restore",
                    )
                    self._modification_dataset.record_acceptance(
                        accepted=True,
                        session_id=operation.session_id,
                        request_id=operation.request_id,
                        strength="implicit",
                        reason="alternate_failed_restored",
                    )
                except BaseException as exc:
                    self._append_log(f"切换恢复状态保存失败：{exc}")
            self.interactionChanged.emit()
            return
        if interaction.routed_by_model:
            try:
                self._modification_dataset.record_mode_correction(
                    interaction.session_id,
                    previous_mode=previous_mode,
                    corrected_mode=alternate_mode,
                )
            except BaseException as exc:
                self._append_log(f"输入类型纠正数据保存失败：{exc}")
        self._append_log(
            "已按另一种方式重新处理："
            + ("听写内容" if alternate_mode == INPUT_MODE_DICTATION else "编辑指令")
        )

    def _undo_applied_operation(self, operation: _AppliedInteraction) -> None:
        adapter = self._desktop_target_adapter()
        if operation.original_snapshot is not None:
            adapter.replace(
                DesktopTextSnapshot(operation.target, operation.applied_text),
                operation.original_snapshot.text,
            )
        else:
            adapter.undo(operation.target)

    def _restore_applied_operation(self, operation: _AppliedInteraction) -> None:
        adapter = self._desktop_target_adapter()
        if operation.mode == INPUT_MODE_EDIT and operation.original_snapshot is not None:
            adapter.replace(operation.original_snapshot, operation.applied_text)
        else:
            adapter.inject(operation.target, operation.applied_text)

    def _finish_auto_interaction(self, used_mode: str, *, retain: bool = False) -> None:
        interaction = self._active_auto_interaction
        if interaction is None:
            return
        if retain:
            self._active_auto_interaction = None
            self.interactionChanged.emit()
            return
        for mode, request_id in tuple(interaction.request_ids.items()):
            if request_id in self._pending_text_requests:
                self._pending_text_requests.discard(request_id)
                self._pending_interactions.pop(request_id, None)
                cancel_request = getattr(
                    self._text_processing_worker, "cancel_request", None
                )
                if callable(cancel_request):
                    cancel_request(request_id)
            if mode == INPUT_MODE_EDIT and mode != used_mode:
                try:
                    self._modification_dataset.abandon_request(
                        request_id, "alternate mode was not selected"
                    )
                except BaseException:
                    pass
        if used_mode != INPUT_MODE_EDIT and interaction.snapshot is not None:
            try:
                self._desktop_target_adapter().release_selection(
                    interaction.snapshot.target
                )
            except BaseException:
                pass
        self._active_auto_interaction = None
        self.interactionChanged.emit()
        self.textProcessingChanged.emit()

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
        auto_context = self._active_auto_interaction
        text = result.final_text
        final_text = str(text or "").strip()
        if not final_text:
            self._append_log("文本处理完成，但没有可注入的文字")
            self._finish_auto_interaction(INPUT_MODE_DICTATION, retain=False)
            self._resume_recognition_after_interaction()
            return
        if normalize_input_mode(result.mode) == INPUT_MODE_DICTATION:
            self._copy_text_to_clipboard(final_text)
        # Stage2 normally locks the target before the overlay appears. If that
        # first capture raced a focus transition, make one last attempt at the
        # actual commit point instead of silently discarding valid dictation.
        if self._desktop_output and target is None:
            target = self._capture_desktop_reference()
        self._transcript_text = "正在处理文本"
        self._transcript_final = False
        self._transcript_visible = True
        self._set_interaction_state("processing")
        self.transcriptChanged.emit()
        self._hide_overlay_timer.stop()
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
        try:
            self._modification_dataset.record_application(
                action="applied" if applied else "apply_failed",
                session_id=int(result.session_id),
                request_id=int(result.request_id),
                mode=INPUT_MODE_DICTATION,
                application=(
                    target.process_name or target.window_title
                    if target is not None
                    else ""
                ),
                target_key=self._association_target_key(target),
                before_text=(
                    auto_context.snapshot.text
                    if auto_context is not None
                    and auto_context.snapshot is not None
                    else None
                ),
                candidate_text=final_text,
                final_text=final_text if applied else None,
                method="automatic",
                error=None if applied else detail,
            )
        except BaseException as exc:
            self._append_log(f"听写应用事件保存失败：{exc}")
        if applied:
            try:
                self._modification_dataset.record_asr_label(
                    int(result.session_id),
                    label="positive",
                    source="successful_application",
                )
                self._modification_dataset.record_near_field_label(
                    int(result.session_id),
                    label="positive",
                    source="successful_application",
                )
                if auto_context is not None and auto_context.routed_by_model:
                    self._modification_dataset.record_mode_acceptance(
                        int(result.session_id), mode=INPUT_MODE_DICTATION
                    )
            except BaseException as exc:
                self._append_log(f"听写模型标签保存失败：{exc}")
        applied_interaction = None
        if applied and target is not None:
            applied_interaction = _AppliedInteraction(
                mode=INPUT_MODE_DICTATION,
                target=target,
                session_id=int(result.session_id),
                request_id=int(result.request_id),
                raw_text=str(result.raw_text or ""),
                applied_text=final_text,
                original_snapshot=(
                    auto_context.snapshot
                    if auto_context is not None
                    and auto_context.snapshot is not None
                    else None
                ),
                auto_context=auto_context,
                summary=f"已输入：“{self._short_text(final_text)}”",
            )
        if not applied:
            self._finish_auto_interaction(INPUT_MODE_DICTATION, retain=False)
            self._transcript_text = "未能输入文本"
            self._set_interaction_state("error")
            self.transcriptChanged.emit()
            self.interactionChanged.emit()
            self._hide_overlay_timer.start(3500)
        self._record_history(
            f"输入 · {status}",
            raw=result.raw_text,
            result=final_text if result.used_llm else "",
            detail=detail,
        )
        if applied_interaction is not None:
            self._finish_auto_interaction(INPUT_MODE_DICTATION, retain=True)
            self._show_applied_interaction(
                applied_interaction,
                message="听写已应用到原文本框",
            )
            return
        if self._recognition_enabled:
            self._set_status("自动监听中", "输入完成，等待下一段语音", "running")
        self._resume_recognition_after_interaction()

    def _copy_text_to_clipboard(self, text: str) -> None:
        """Keep every committed dictation available for manual paste."""
        value = str(text or "")
        try:
            from .clipboard import QtClipboardBridge

            clipboard = QtClipboardBridge()
            last_error: BaseException | None = None
            for attempt in range(3):
                try:
                    clipboard.set_text(value)
                    deadline = time.monotonic() + 0.25
                    while time.monotonic() < deadline:
                        if clipboard.text() == value:
                            self._append_log("听写结果已复制到剪贴板")
                            return
                        time.sleep(0.01)
                except BaseException as exc:
                    last_error = exc
                if attempt < 2:
                    time.sleep(0.03)
            if last_error is not None:
                raise RuntimeError(str(last_error)) from last_error
            raise RuntimeError("剪贴板读回内容与本次听写不一致")
        except BaseException as exc:
            # Clipboard failure must not prevent the independent desktop
            # injection attempt.
            self._append_log(f"听写结果复制到剪贴板失败：{exc}")

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
        self._edit_review = _EditReview(
            request_id=result.request_id,
            session_id=result.session_id,
            instruction=result.raw_text,
            proposed_text=proposed,
            snapshot=snapshot,
        )
        self._transcript_text = self._processing_overlay_text(
            INPUT_MODE_EDIT, result.raw_text
        )
        self._transcript_final = False
        self._transcript_visible = True
        self._set_interaction_state("processing")
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._apply_edit_result()

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
        self._edit_review = None
        try:
            self._modification_dataset.record_application(
                action="apply_failed",
                session_id=int(result.session_id),
                request_id=int(result.request_id),
                mode=INPUT_MODE_EDIT,
                application=(
                    snapshot.target.process_name or snapshot.target.window_title
                ),
                target_key=self._association_target_key(snapshot.target),
                before_text=snapshot.text,
                candidate_text=str(result.final_text or ""),
                final_text=snapshot.text,
                method="automatic",
                error=error,
            )
        except BaseException as exc:
            self._append_log(f"修改失败事件保存失败：{exc}")
        failed_member = self._record_association_failure(
            session_id=int(result.session_id),
            target=snapshot.target,
            mode=INPUT_MODE_EDIT,
            status="处理失败",
        )
        self._start_manual_association_watch(
            snapshot.target, failed_member, baseline=snapshot.text
        )
        self._transcript_text = "未能完成修改"
        self._transcript_final = True
        self._transcript_visible = True
        self._set_interaction_state("error")
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._record_history(
            "修改 · 大模型失败",
            raw=result.raw_text,
            detail=error,
        )
        self._append_log(f"大模型修改失败，原文本保持不变：{error}")
        self._finish_auto_interaction(INPUT_MODE_EDIT, retain=False)
        self._hide_overlay_timer.start(2200)
        if self._recognition_enabled:
            self._set_status("修改未完成", "原文本保持不变", "error")
        self._resume_recognition_after_interaction()

    def _apply_edit_result(self) -> None:
        review = self._edit_review
        if review is None:
            return
        auto_context = self._active_auto_interaction
        try:
            adapter = self._desktop_target_adapter()
            adapter.replace(review.snapshot, review.proposed_text)
            if sys.platform == "darwin":
                try:
                    final_text = self._verify_macos_edit_text(review)
                except BaseException as first_error:
                    self._append_log(
                        f"macOS 首次修改回读未通过，正在重新聚焦后重试：{first_error}"
                    )
                    adapter.replace(review.snapshot, review.proposed_text)
                    final_text = self._verify_macos_edit_text(review)
            else:
                final_text, _was_normalized = self._read_back_edit_text(
                    review, fallback=review.proposed_text
                )
        except BaseException as exc:
            self._finish_auto_interaction(INPUT_MODE_EDIT)
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
            self._finish_edit_application(
                message=f"修改未应用：{exc}", state="error", hide_ms=4000
            )
            return
        try:
            self._modification_dataset.record_application(
                action="applied",
                session_id=int(review.session_id),
                request_id=int(review.request_id),
                mode=INPUT_MODE_EDIT,
                application=(
                    review.snapshot.target.process_name
                    or review.snapshot.target.window_title
                ),
                target_key=self._association_target_key(review.snapshot.target),
                before_text=review.snapshot.text,
                candidate_text=review.proposed_text,
                final_text=final_text,
                method="automatic",
            )
        except BaseException as exc:
            self._append_log(f"修改应用事件保存失败：{exc}")
        try:
            self._modification_dataset.record_asr_label(
                int(review.session_id),
                label="positive",
                source="successful_application",
            )
            self._modification_dataset.record_near_field_label(
                int(review.session_id),
                label="positive",
                source="successful_application",
            )
            if auto_context is not None and auto_context.routed_by_model:
                self._modification_dataset.record_mode_acceptance(
                    int(review.session_id), mode=INPUT_MODE_EDIT
                )
        except BaseException as exc:
            self._append_log(f"修改模型标签保存失败：{exc}")
        self._record_history(
            "修改 · 已应用",
            raw=review.instruction,
            result=review.proposed_text,
            detail=f"已替换 {review.snapshot.target.window_title or '外部文本框'}",
        )
        applied_interaction = _AppliedInteraction(
            mode=INPUT_MODE_EDIT,
            target=review.snapshot.target,
            session_id=int(review.session_id),
            request_id=int(review.request_id),
            raw_text=str(review.instruction or ""),
            applied_text=str(final_text),
            original_snapshot=review.snapshot,
            auto_context=auto_context,
            summary=self._edit_result_summary(review.snapshot.text, final_text),
        )
        self._finish_auto_interaction(INPUT_MODE_EDIT, retain=True)
        self._show_applied_interaction(
            applied_interaction,
            message="修改已应用到原文本框",
        )

    def _verify_macos_edit_text(self, review: _EditReview) -> str:
        """Require actual external-control readback before reporting success."""
        adapter = self._desktop_target_adapter()
        capture = adapter.capture_text
        if not review.proposed_text:
            capture_empty = getattr(adapter, "capture_text_allowing_empty", None)
            if callable(capture_empty):
                capture = capture_empty
        snapshot = capture(review.snapshot.target)
        try:
            actual = snapshot.text
        finally:
            adapter.release_selection(review.snapshot.target)
        if not macos_texts_equivalent(actual, review.proposed_text):
            raise RuntimeError(
                "外部文本框回读结果与预期修改不一致，系统没有确认替换成功"
            )
        return actual

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

    @Slot(str)
    def dispatchVoiceAction(self, action: str) -> None:
        """Thread-safe entry used by keyboard hooks and future Ring gestures."""
        self._voiceActionRequested.emit(str(action))

    @Slot(str)
    def _apply_voice_action(self, action: str) -> None:
        action = str(action).strip().lower()
        if action == ACTION_INPUT:
            self.inputMode = INPUT_MODE_DICTATION
        elif action == ACTION_EDIT:
            self.inputMode = INPUT_MODE_EDIT
        elif action == ACTION_CANCEL:
            self.cancelCurrentUtterance()
        elif action == ACTION_SWITCH_MODE:
            self.switchCurrentInputMode()
        elif action == ACTION_UNDO:
            self.undoLastApplied()

    def _finish_edit_application(
        self, *, message: str, state: str, hide_ms: int
    ) -> None:
        self._edit_review = None
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
        self._resume_recognition_after_interaction()

    def _show_applied_interaction(
        self,
        interaction: _AppliedInteraction,
        *,
        message: str,
    ) -> None:
        """Push one applied operation and show its target-anchored actions."""
        self._edit_review = None
        interaction = replace(
            interaction,
            target=self._target_with_live_caret(interaction.target),
        )
        self._operation_stack.append(interaction)
        self._applied_action_visible = True
        self._applied_target_foreground = True
        if not self._applied_target_timer.isActive():
            self._applied_target_timer.start()
        self._pending_applied_mode_switch = None
        self._transcript_mode = ""
        self._transcript_text = ""
        self._transcript_final = True
        self._transcript_visible = False
        self._set_interaction_state("applied")
        self._hide_overlay_timer.stop()
        self._record_association_success(interaction)
        try:
            self._modification_dataset.record_acceptance(
                accepted=True,
                session_id=interaction.session_id,
                request_id=interaction.request_id,
                strength="implicit",
                reason="successful_application",
            )
        except BaseException as exc:
            self._append_log(f"成功应用状态保存失败：{exc}")
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._append_log(message + "；已加入撤销栈")
        if self._recognition_enabled:
            self._set_status("自动监听中", "结果已应用，可在光标旁撤销", "running")
        self._resume_recognition_after_interaction()

    def _target_with_live_caret(self, target: DesktopTargetRef) -> DesktopTargetRef:
        """Snapshot the post-application caret used by the compact action pill."""
        locator = getattr(self._desktop_target_adapter(), "caret_bounds", None)
        if not callable(locator):
            return target
        try:
            bounds = tuple(int(value) for value in locator(target))
        except BaseException as exc:
            self._append_log(f"输入光标定位失败，改用文本框边缘：{exc}")
            return target
        if len(bounds) != 4 or bounds[3] <= 0:
            if target.screen_width <= 0 or target.screen_height <= 0:
                self._append_log(
                    "当前应用未提供输入光标或文本框坐标；操作条改用安全的底部位置"
                )
            return target
        return replace(
            target,
            caret_x=bounds[0],
            caret_y=bounds[1],
            caret_width=max(2, bounds[2]),
            caret_height=max(1, bounds[3]),
        )

    def _commit_associated_result(self) -> None:
        """Make the latest association positive a non-undoable boundary."""
        interaction = self._latest_operation()
        if interaction is None:
            return
        try:
            self._modification_dataset.record_acceptance(
                accepted=True,
                session_id=interaction.session_id,
                request_id=interaction.request_id,
                strength="explicit",
                reason="association_accepted",
            )
        except BaseException as exc:
            self._append_log(f"成功确认状态保存失败：{exc}")
        # Older operations cannot be popped safely underneath a committed edit.
        self._operation_stack.clear()
        self._applied_action_visible = False
        self._applied_target_timer.stop()
        self._provisional_association_recommendations.clear()
        self.interactionChanged.emit()
        if self._recognition_enabled:
            self._set_status("自动监听中", "结果已确认，等待下一段语音", "running")

    def _retract_applied_recommendations(self) -> None:
        recommendations = list(self._provisional_association_recommendations)
        if not recommendations:
            return
        recommendation_ids = {
            item.recommendation_id for item in recommendations
        }
        if (
            self._association_recommendation is not None
            and self._association_recommendation.recommendation_id
            in recommendation_ids
        ):
            self._association_recommendation = None
            self._association_detail_visible = False
        self._association_queue = deque(
            item
            for item in self._association_queue
            if item.recommendation_id not in recommendation_ids
        )
        if self._association_recommendation is None and self._association_queue:
            self._association_recommendation = self._association_queue.popleft()
        if self._smart_association_enabled:
            self._association_coordinator.restore_failures(recommendations)
        self._provisional_association_recommendations.clear()
        self.associationChanged.emit()

    @Slot()
    def undoLastApplied(self) -> None:
        """Pop and undo the latest dictation or edit operation."""
        interaction = self._latest_operation()
        if interaction is None:
            return
        self._hide_overlay_timer.stop()
        try:
            adapter = self._desktop_target_adapter()
            if interaction.original_snapshot is not None:
                # Edit mode replaces the whole field, so restoring the captured
                # pre-edit snapshot is deterministic and does not depend on an
                # application's undo-stack implementation.
                adapter.replace(
                    DesktopTextSnapshot(
                        interaction.target,
                        interaction.applied_text,
                    ),
                    interaction.original_snapshot.text,
                )
            else:
                # Dictation inserts at the active caret. Native Undo is the only
                # general cross-application way to remove that exact insertion.
                adapter.undo(interaction.target)
        except BaseException as exc:
            self._transcript_text = f"撤回失败：{exc}"
            self._transcript_final = True
            self._transcript_visible = True
            self._set_interaction_state("error")
            self.transcriptChanged.emit()
            self.interactionChanged.emit()
            self._append_log(f"撤回上次结果失败：{exc}")
            return

        self._retract_applied_recommendations()
        self._operation_stack.pop()
        self._applied_action_visible = bool(self._operation_stack)
        self._applied_target_foreground = True
        if not self._operation_stack:
            self._applied_target_timer.stop()
        self._pending_applied_mode_switch = None
        mode_label = "修改" if interaction.mode == INPUT_MODE_EDIT else "听写"
        self._transcript_mode = ""
        self._transcript_text = ""
        self._transcript_final = True
        self._transcript_visible = False
        self._set_interaction_state(
            "applied" if self._operation_stack else "idle"
        )
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._record_history(
            f"{mode_label} · 已撤回",
            raw=interaction.raw_text,
        )
        self._append_log(f"用户已撤回上次{mode_label}结果")
        if interaction.request_id > 0:
            try:
                self._modification_dataset.feedback(
                    interaction.request_id,
                    "cancel",
                    final_text=(
                        interaction.original_snapshot.text
                        if interaction.original_snapshot is not None
                        else ""
                    ),
                )
            except BaseException as exc:
                self._append_log(f"撤回反馈保存失败：{exc}")
        try:
            self._modification_dataset.record_application(
                action="undone",
                session_id=int(interaction.session_id),
                request_id=int(interaction.request_id),
                mode=interaction.mode,
                application=(
                    interaction.target.process_name
                    or interaction.target.window_title
                ),
                target_key=self._association_target_key(interaction.target),
                before_text=(
                    interaction.applied_text
                    if interaction.mode == INPUT_MODE_EDIT
                    else None
                ),
                candidate_text=interaction.applied_text,
                final_text=(
                    interaction.original_snapshot.text
                    if interaction.original_snapshot is not None
                    else None
                ),
                method="explicit_user",
            )
        except BaseException as exc:
            self._append_log(f"撤回事件保存失败：{exc}")
        failed_member = self._record_association_failure(
            session_id=interaction.session_id,
            target=interaction.target,
            mode=interaction.mode,
            status="已撤回",
        )
        self._start_manual_association_watch(
            interaction.target,
            failed_member,
            baseline=(
                interaction.original_snapshot.text
                if interaction.original_snapshot is not None
                else None
            ),
        )
        if self._recognition_enabled:
            remaining = len(self._operation_stack)
            detail = (
                f"已撤回，仍可继续撤回 {remaining} 次"
                if remaining
                else "已撤回，等待下一段语音"
            )
            self._set_status("自动监听中", detail, "running")

    @Slot()
    def cancelCurrentUtterance(self) -> None:
        """Cancel one utterance at any stage without stopping recognition."""
        if not self.interactionCanCancel:
            return
        cancelled_session_id = self._latest_asr_session_id
        cancelled_mode = self._transcript_mode or self._input_mode
        cancelled_target = self._session_targets.get(
            int(cancelled_session_id), self._speech_start_target
        )
        if cancelled_session_id > 0:
            try:
                self._modification_dataset.record_near_field_label(
                    int(cancelled_session_id),
                    label="negative",
                    source="pre_application_cancel",
                )
            except BaseException as exc:
                self._append_log(f"近点取消标签保存失败：{exc}")
            try:
                self._modification_dataset.record_application(
                    action="cancelled",
                    session_id=int(cancelled_session_id),
                    mode=cancelled_mode,
                    application=(
                        cancelled_target.process_name or cancelled_target.window_title
                        if cancelled_target is not None
                        else ""
                    ),
                    target_key=self._association_target_key(cancelled_target),
                    method="explicit_user",
                )
            except BaseException as exc:
                self._append_log(f"本句取消事件保存失败：{exc}")
            failed_member = self._record_association_failure(
                session_id=int(cancelled_session_id),
                target=cancelled_target,
                mode=cancelled_mode,
                status="已取消",
            )
            self._start_manual_association_watch(
                cancelled_target, failed_member
            )
        self._cancel_utterance_event.set()
        self._ignore_asr_updates_until_next_start = True
        if self._latest_asr_session_id:
            self._cancelled_asr_session_ids.add(self._latest_asr_session_id)
        self._utterance_active = False
        self._dictation_commit_timer.stop()
        self._pending_dictation_result = None

        was_processing = self.textProcessing
        for request_id in tuple(
            self._pending_text_requests | self._pending_mode_routes
        ):
            cancel_request = getattr(
                self._text_processing_worker, "cancel_request", None
            )
            if callable(cancel_request):
                cancel_request(request_id)
            try:
                self._modification_dataset.abandon_request(
                    request_id, "utterance cancelled by user"
                )
            except BaseException:
                pass
        interaction = self._active_auto_interaction
        if interaction is not None and interaction.snapshot is not None:
            try:
                self._desktop_target_adapter().release_selection(
                    interaction.snapshot.target
                )
            except BaseException:
                pass
        self._pending_text_requests.clear()
        self._pending_interactions.clear()
        self._pending_mode_routes.clear()
        self._pending_mode_route_contexts.clear()
        self._active_auto_interaction = None
        self._session_input_modes.clear()
        self._session_routing_modes.clear()
        self._session_targets.clear()
        self._speech_start_target = None
        if was_processing:
            self.textProcessingChanged.emit()
        self._transcript_mode = ""
        self._transcript_text = "已取消，等待下一句话"
        self._transcript_final = True
        self._transcript_visible = True
        self._set_interaction_state("cancelled")
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._hide_overlay_timer.start(1200)
        self._record_history("本句 · 已取消")
        self._append_log("用户已取消当前语句；旧识别和文本处理结果将被忽略")
        if self._recognition_enabled:
            self._set_status("自动监听中", "已取消，等待下一段语音", "running")
        self._resume_recognition_after_interaction()

    def _desktop_target_adapter(self):
        if self._desktop_target is None:
            if sys.platform == "darwin":
                from ..desktop_target import MacOSDesktopTextTarget
                from .clipboard import QtClipboardBridge

                self._desktop_target = MacOSDesktopTextTarget(QtClipboardBridge())
            else:
                from ..desktop_target import WindowsDesktopTextTarget
                from .clipboard import QtClipboardBridge

                self._desktop_target = WindowsDesktopTextTarget(QtClipboardBridge())
        return self._desktop_target

    @Slot()
    def _poll_applied_target_foreground(self) -> None:
        operation = self._latest_operation()
        if operation is None or not self._applied_action_visible:
            if self._applied_target_timer.isActive():
                self._applied_target_timer.stop()
            if not self._applied_target_foreground:
                self._applied_target_foreground = True
                self.interactionChanged.emit()
            return
        is_foreground = getattr(self._desktop_target_adapter(), "is_foreground", None)
        visible = True
        if callable(is_foreground):
            try:
                visible = bool(is_foreground(operation.target))
            except BaseException:
                visible = False
        if visible != self._applied_target_foreground:
            self._applied_target_foreground = visible
            self.interactionChanged.emit()

    def _request_macos_accessibility(self) -> None:
        self._check_macos_accessibility(prompt=True)

    @Slot()
    def _poll_macos_accessibility(self) -> None:
        self._check_macos_accessibility(prompt=False)

    def _check_macos_accessibility(self, *, prompt: bool) -> None:
        if sys.platform != "darwin" or not self._desktop_output:
            self._accessibility_timer.stop()
            return
        try:
            trusted = self._desktop_target_adapter().request_accessibility(
                prompt=prompt
            )
        except BaseException as exc:
            self._append_log(f"macOS 辅助功能权限检查失败：{exc}")
            if not self._accessibility_timer.isActive():
                self._accessibility_timer.start()
            return
        changed = trusted != self._macos_accessibility_trusted
        self._macos_accessibility_trusted = trusted
        if changed:
            self.accessibilityChanged.emit()
        if trusted:
            self._accessibility_timer.stop()
        elif not self._accessibility_timer.isActive():
            self._accessibility_timer.start()
        if trusted == self._macos_accessibility_last_reported:
            return
        self._macos_accessibility_last_reported = trusted
        if trusted:
            self._append_log("macOS 辅助功能权限已就绪，可听写和编辑当前文本框")
        else:
            self._append_log(
                "macOS 尚未授予辅助功能权限；请在系统设置的“隐私与安全性 → "
                "辅助功能”中允许当前安装的 Proximic Voice"
            )

    def _capture_desktop_reference(self) -> DesktopTargetRef | None:
        if not self._desktop_output or not DESKTOP_TEXT_INJECTION_SUPPORTED:
            return None
        try:
            return self._desktop_target_adapter().capture_reference()
        except BaseException as exc:
            self._append_log(f"锁定外部文本框失败：{exc}")
            return None

    def _set_interaction_state(self, state: str) -> None:
        state = str(state)
        correction_changed = False
        if state != "processing":
            self._processing_mode_correction_timer.stop()
            correction_changed = self._processing_mode_correction_revealed
            self._processing_mode_correction_revealed = False
        if state == self._interaction_state:
            if correction_changed:
                self.interactionChanged.emit()
            return
        self._interaction_state = state
        self.interactionChanged.emit()

    def _schedule_processing_mode_correction(
        self,
        selected_mode: str,
        interaction: _AutoInteraction,
    ) -> None:
        self._processing_mode_correction_timer.stop()
        was_revealed = self._processing_mode_correction_revealed
        self._processing_mode_correction_revealed = False
        if normalize_input_mode(selected_mode) == INPUT_MODE_EDIT:
            elapsed_ms = int(
                max(0.0, time.monotonic() - interaction.prepared_at) * 1000
            )
            remaining_ms = max(
                0, _PROCESSING_MODE_CORRECTION_DELAY_MS - elapsed_ms
            )
            if remaining_ms:
                self._processing_mode_correction_timer.start(remaining_ms)
            else:
                self._reveal_processing_mode_correction()
                return
        if was_revealed:
            self.interactionChanged.emit()

    @Slot()
    def _reveal_processing_mode_correction(self) -> None:
        interaction = self._active_auto_interaction
        should_reveal = bool(
            self._transcript_visible
            and self._interaction_state == "processing"
            and interaction is not None
            and interaction.classified
            and interaction.selected_mode == INPUT_MODE_EDIT
            and INPUT_MODE_DICTATION in getattr(interaction, "results", {})
        )
        if should_reveal == self._processing_mode_correction_revealed:
            return
        self._processing_mode_correction_revealed = should_reveal
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
        self._dictation_commit_timer.stop()
        self._processing_mode_correction_timer.stop()
        self._processing_mode_correction_revealed = False
        self._pending_dictation_result = None
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
        self._active_auto_interaction = None
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
        self._interaction_recognition_suspended = False
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
            self._set_status("按键监听中", "松开后恢复自动控制", "manual")
        elif self._recognition_enabled:
            self._set_status("自动监听中", "已恢复靠近检测", "running")

    def _hide_transcript(self) -> None:
        self._transcript_visible = False
        self._transcript_mode = ""
        if self._edit_review is None and self._interaction_state != "processing":
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
            asr_gain_db=self._asr_gain_db,
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
