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
import traceback
import uuid

from PySide6.QtCore import (
    QAbstractListModel,
    QObject,
    Property,
    QCoreApplication,
    QModelIndex,
    QSettings,
    QTimer,
    QUrl,
    Signal,
    Slot,
    Qt,
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
from ..diagnostic_log import RotatingDiagnosticLog
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
    session_id: int = 0


@dataclass
class _PendingModeRoute:
    target: DesktopTargetRef | None = None
    session_id: int = 0


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
    mode_switch_error: str = ""


@dataclass
class _ModeSwitchApplication:
    """One atomic replacement of an already-applied voice operation."""

    source_key: str
    source_operation: _AppliedInteraction
    expected_mode: str
    interaction: _AutoInteraction
    replacement: _AppliedInteraction | None = None
    error: str = ""


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


class _VoiceHistoryListModel(QAbstractListModel):
    """Stable QML model for in-place Interaction history updates."""

    ENTRY_ROLE = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, entries: list[dict], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries = [dict(entry) for entry in entries]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    def data(
        self,
        index: QModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        if role in {self.ENTRY_ROLE, int(Qt.ItemDataRole.DisplayRole)}:
            return dict(self._entries[index.row()])
        return None

    def roleNames(self) -> dict[int, bytes]:
        return {self.ENTRY_ROLE: b"entry"}

    @staticmethod
    def _entry_id(entry: dict) -> str:
        return str(entry.get("interactionId", "") or entry.get("id", ""))

    def replace_entries(
        self,
        entries: list[dict],
        *,
        force_reset: bool = False,
    ) -> None:
        updated = [dict(entry) for entry in entries]
        if force_reset:
            # QML delegates keep the whole row in one QVariantMap role. Some
            # Qt/macOS combinations retain that map across dataChanged even
            # though the Python model already contains the new mode. A reset is
            # reserved for explicit mode corrections so the visible row cannot
            # remain stuck on its pre-F8 label.
            self.beginResetModel()
            self._entries = updated
            self.endResetModel()
            return
        current_ids = [self._entry_id(entry) for entry in self._entries]
        updated_ids = [self._entry_id(entry) for entry in updated]
        if current_ids == updated_ids:
            for row, entry in enumerate(updated):
                if entry == self._entries[row]:
                    continue
                self._entries[row] = entry
                model_index = self.index(row, 0)
                self.dataChanged.emit(
                    model_index,
                    model_index,
                    [self.ENTRY_ROLE],
                )
            return
        self.beginResetModel()
        self._entries = updated
        self.endResetModel()


class AppController(QObject):
    runningChanged = Signal()
    connectedChanged = Signal()
    batteryChanged = Signal()
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
    _runtimeBatteryChanged = Signal(int, int, int)
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
        self._battery_percentage = -1
        self._battery_millivolts = -1
        self._battery_charge_status = -1
        self._battery_query_complete = False
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
        self._transcript_primary_text = ""
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
        self._operation_stacks: dict[str, list[_AppliedInteraction]] = {}
        self._active_operation_target_key = ""
        self._applied_action_visible = False
        self._applied_target_foreground = True
        self._applied_target_mismatch_count = 0
        self._applied_overlay_drag_active = False
        self._applied_overlay_foreground_grace_until = 0.0
        self._pending_applied_mode_switches: dict[str, tuple[int, str]] = {}
        self._mode_switch_application: _ModeSwitchApplication | None = None
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
        self._provisional_association_recommendations: dict[
            str, list[AssociationRecommendation]
        ] = {}
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
        self._diagnostic_run_id = uuid.uuid4().hex[:8]
        self._diagnostic_log = RotatingDiagnosticLog(
            app_data_root() / "logs" / "diagnostic.log"
        )
        self._diagnostic_session_started_at: dict[int, float] = {}
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
        saved_applied_overlay_style = str(
            self._settings.value("ui/appliedOverlayStyle", "normal")
        ).strip().lower()
        self._applied_overlay_style = (
            saved_applied_overlay_style
            if saved_applied_overlay_style in {"normal", "compact"}
            else "normal"
        )
        try:
            saved_applied_overlay_duration = int(
                round(
                    float(
                        self._settings.value(
                            "ui/appliedOverlayDurationSeconds", 3
                        )
                    )
                )
            )
        except (TypeError, ValueError):
            saved_applied_overlay_duration = 3
        self._applied_overlay_duration_seconds = max(
            1, min(saved_applied_overlay_duration, 10)
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
        self._voice_history_model = _VoiceHistoryListModel(
            self._voice_history_entries, self
        )
        self.voiceHistoryChanged.connect(self._sync_voice_history_model)
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
        self._applied_action_hide_timer = QTimer(self)
        self._applied_action_hide_timer.setSingleShot(True)
        self._applied_action_hide_timer.setInterval(
            self._applied_overlay_duration_seconds * 1000
        )
        self._applied_action_hide_timer.timeout.connect(
            self._hide_applied_action_overlay
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
        self._runtimeBatteryChanged.connect(self._apply_runtime_battery)
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
        self._event_log(
            "APP_READY",
            platform=sys.platform,
            packaged=is_frozen(),
            asr_backend=self._asr_backend,
            asr_device=self._asr_device,
            routing=self._input_routing_mode,
            default_mode=self._input_mode,
            llm_provider=self._llm_provider,
            llm_model=self._llm_model,
            desktop_output=self._desktop_output,
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

    @Property(bool, notify=batteryChanged)
    def batteryAvailable(self) -> bool:
        return self._battery_percentage >= 0

    @Property(bool, notify=batteryChanged)
    def batteryQueryComplete(self) -> bool:
        return self._battery_query_complete

    @Property(int, notify=batteryChanged)
    def batteryPercentage(self) -> int:
        return self._battery_percentage

    @Property(bool, notify=batteryChanged)
    def batteryCharging(self) -> bool:
        return self._battery_charge_status == 1

    @Property(bool, notify=batteryChanged)
    def batteryFull(self) -> bool:
        return self._battery_charge_status == 2

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
    def transcriptPrimaryText(self) -> str:
        """Latest ASR text shown as the overlay's primary content."""

        return self._transcript_primary_text

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

    @Property(QObject, constant=True)
    def voiceHistoryModel(self) -> QObject:
        return self._voice_history_model

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
        applied_match = next(
            (
                (key, applied)
                for key, applied in self._all_operations()
                if applied.session_id == recommendation.chosen.session_id
                and applied.mode == recommendation.chosen.mode
            ),
            None,
        )
        if applied_match is not None:
            self._commit_associated_result(*applied_match)
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
        chosen_interaction_id = str(chosen.get("interactionId", ""))
        applied_match = next(
            (
                (key, applied)
                for key, applied in self._all_operations()
                if self._modification_dataset.interaction_id_for_session(
                    applied.session_id
                )
                == chosen_interaction_id
            ),
            None,
        )
        if applied_match is not None:
            self._commit_associated_result(*applied_match)
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
        # Association recommendations must obey the same text-field boundary as
        # undo. Two editors inside one application are not interchangeable.
        return AppController._operation_target_key(target)

    @staticmethod
    def _operation_target_key(target: DesktopTargetRef | None) -> str:
        """Identify one runtime text field, not merely its owning application."""

        if target is None:
            return ""
        if target.accessibility_id:
            control_identity = target.accessibility_id
        elif target.uia_control is not None:
            control_identity = "uia:" + ".".join(
                str(value) for value in target.uia_control.runtime_id
            )
        elif target.control_handle:
            control_identity = f"control:{int(target.control_handle)}"
        elif target.screen_width > 0 and target.screen_height > 0:
            control_identity = ":".join(
                (
                    "bounds",
                    str(int(target.screen_x)),
                    str(int(target.screen_y)),
                    str(int(target.screen_width)),
                    str(int(target.screen_height)),
                )
            )
        else:
            # This identity is deliberately application-local and ambiguous.
            # The foreground poll will refuse to expose it unless the platform
            # adapter can later prove that this exact field is still focused.
            control_identity = "unresolved"
        return ":".join(
            (
                str(int(target.process_id)),
                str(int(target.window_handle)),
                control_identity,
            )
        )

    @staticmethod
    def _target_has_text_field_identity(target: DesktopTargetRef) -> bool:
        """Return whether one focused field can be distinguished from its app."""

        return bool(
            target.accessibility_id
            or target.uia_control is not None
            or target.control_handle
            or (target.screen_width > 0 and target.screen_height > 0)
        )

    @classmethod
    def _operation_can_switch_mode(cls, operation: _AppliedInteraction) -> bool:
        """Reject only an unverifiable conversion away from a full clear.

        A non-empty applied value can be compared with the focused field before
        it is replaced. An empty field carries no such evidence, so a process-
        only target cannot safely be distinguished from another empty field in
        the same application.
        """

        if operation.mode != INPUT_MODE_EDIT or operation.applied_text:
            return True
        target = (
            operation.original_snapshot.target
            if operation.original_snapshot is not None
            else operation.target
        )
        return cls._target_has_text_field_identity(target)

    @staticmethod
    def _operation_mode_correction_failure(
        operation: _AppliedInteraction,
    ) -> str:
        """Return a terminal error for the operation's alternate mode."""

        if operation.mode_switch_error:
            return str(operation.mode_switch_error)
        interaction = operation.auto_context
        if interaction is None:
            return ""
        alternate_mode = (
            INPUT_MODE_DICTATION
            if operation.mode == INPUT_MODE_EDIT
            else INPUT_MODE_EDIT
        )
        candidate_error = str(
            interaction.candidate_errors.get(alternate_mode, "") or ""
        ).strip()
        if candidate_error:
            return candidate_error
        result = interaction.results.get(alternate_mode)
        if result is None:
            return ""
        if result.error:
            return str(result.error).strip() or "另一种处理方式不可用"
        if alternate_mode == INPUT_MODE_EDIT and (
            result.final_text == result.target_text
            or not str(result.final_text or "").strip()
        ):
            return "没有可应用的编辑结果"
        if alternate_mode == INPUT_MODE_DICTATION and not str(
            result.final_text or result.raw_text or ""
        ).strip():
            return "没有可应用的听写结果"
        return ""

    def _failed_edit_fallback_interaction(self) -> _AutoInteraction | None:
        """Return the failed edit whose raw dictation can still be applied."""

        interaction = self._active_auto_interaction
        if (
            not self._transcript_visible
            or self._interaction_state != "error"
            or interaction is None
            or not interaction.classified
            or interaction.selected_mode != INPUT_MODE_EDIT
        ):
            return None
        result = interaction.results.get(INPUT_MODE_DICTATION)
        if (
            result is None
            or result.error
            or not str(result.final_text or result.raw_text or "").strip()
        ):
            return None
        return interaction

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
        target_key = self._operation_target_key(interaction.target)
        self._provisional_association_recommendations.pop(target_key, None)
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
        self._provisional_association_recommendations[target_key] = list(
            recommendations
        )
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

    def _active_operation_stack(self) -> list[_AppliedInteraction]:
        if not self._active_operation_target_key:
            return []
        return self._operation_stacks.get(self._active_operation_target_key, [])

    def _operation_stack_for_target(
        self,
        target: DesktopTargetRef,
        *,
        create: bool = False,
    ) -> tuple[str, list[_AppliedInteraction]]:
        key = self._operation_target_key(target)
        if not key:
            return "", []
        if create:
            return key, self._operation_stacks.setdefault(key, [])
        return key, self._operation_stacks.get(key, [])

    def _all_operations(self) -> list[tuple[str, _AppliedInteraction]]:
        return [
            (key, operation)
            for key, stack in self._operation_stacks.items()
            for operation in reversed(stack)
        ]

    def _find_operation_for_route(
        self, route_id: int
    ) -> tuple[str, _AppliedInteraction] | None:
        for key, operation in self._all_operations():
            if (
                operation.auto_context is not None
                and operation.auto_context.route_id == int(route_id)
            ):
                return key, operation
        return None

    def _latest_operation(self) -> _AppliedInteraction | None:
        stack = self._active_operation_stack()
        return stack[-1] if stack else None

    def _target_is_focused(self, target: DesktopTargetRef) -> bool:
        checker = getattr(self._desktop_target_adapter(), "is_foreground", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(target))
        except BaseException:
            return False

    def _operation_target_is_focused(
        self, operation: _AppliedInteraction
    ) -> bool:
        """Validate a field even when its accessibility wrapper was rebuilt."""

        if self._target_is_focused(operation.target):
            return True
        adapter = self._desktop_target_adapter()
        application_checker = getattr(adapter, "is_application_foreground", None)
        if not callable(application_checker):
            return False
        try:
            if not application_checker(operation.target):
                return False
        except BaseException:
            return False
        original = operation.original_snapshot
        if original is None:
            return False
        # A changed AX/UIA wrapper is accepted only when the currently focused
        # field still contains the exact state created by this operation. This
        # keeps the green button actionable while refusing another field in the
        # same application when its contents differ.
        try:
            # macOS web editors can change both AX identity and geometry when
            # their content grows.  Read the *current* focused AXValue without
            # applying the stale identity/geometry filters; the exact saved-text
            # comparison below is what authorizes the conversion.
            observe_focused = getattr(adapter, "observe_focused_text", None)
            if callable(observe_focused):
                current_text = str(observe_focused(original.target).text)
            else:
                current_text = self._read_target_text_for_undo(
                    original.target,
                    allow_focused_fallback=True,
                    allow_empty=not bool(operation.applied_text),
                )
        except BaseException as exc:
            self._append_log(f"类型转换目标校验失败：{exc}")
            return False
        if current_text is None:
            return False
        if operation.mode == INPUT_MODE_EDIT:
            return macos_texts_equivalent(current_text, operation.applied_text)
        return self._dictation_state_matches_snapshot(
            current_text,
            original.text,
            operation.applied_text,
        )

    def _hide_applied_action_for_focus_mismatch(self) -> None:
        if self._applied_target_foreground:
            self._applied_target_foreground = False
            self.interactionChanged.emit()

    @Slot()
    def _hide_applied_action_overlay(self) -> None:
        """Hide only the action window; keep its undo operation intact."""
        self._applied_action_hide_timer.stop()
        if not self._applied_action_visible:
            return
        self._applied_action_visible = False
        self._applied_target_mismatch_count = 0
        # Presentation timeout must not make F8 stale. Keep tracking the target
        # while the operation remains current, even though its window is hidden.
        self.interactionChanged.emit()

    def _restore_applied_action_overlay(self) -> None:
        """Restore the undo entry after an utterance ends without an application."""
        if not self._operation_stacks:
            return
        self._applied_action_visible = True
        self._applied_target_foreground = True
        self._applied_target_mismatch_count = 0
        if not self._applied_target_timer.isActive():
            self._applied_target_timer.start()
        self._applied_action_hide_timer.start()
        self._poll_applied_target_foreground()

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
        return bool(self._active_operation_stack())

    @Property(bool, notify=interactionChanged)
    def interactionCanCancel(self) -> bool:
        # Once text has been applied, any remaining request belongs only to the
        # optional alternate-mode cache. Escape must undo the applied operation
        # instead of cancelling that background work first.
        if self._interaction_state == "applied" and self._active_operation_stack():
            return False
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
            self._applied_action_visible
            and self._applied_target_foreground
            and operation is not None
            and operation.auto_context is not None
            and self._operation_can_switch_mode(operation)
            and not self._operation_mode_correction_failure(operation)
        )

    @Property(bool, notify=interactionChanged)
    def modeCorrectionHotkeyAvailable(self) -> bool:
        """Keep F8 functional after timeout and after a speculative failure.

        A failed alternate candidate was produced in the background before the
        user asked for a conversion.  It must not prevent the explicit F8
        correction from reaching ``switchCurrentInputMode``, which can retry
        that candidate with the user's now-unambiguous intent.
        """

        operation = self._latest_operation()
        return bool(
            self._applied_target_foreground
            and self._interaction_state not in {"listening", "processing", "review"}
            and operation is not None
            and operation.auto_context is not None
            and self._operation_can_switch_mode(operation)
        )

    @Property(bool, notify=interactionChanged)
    def modeCorrectionFailed(self) -> bool:
        """Keep a failed conversion visible, but make it non-actionable."""

        operation = self._latest_operation()
        return bool(
            self._applied_action_visible
            and self._applied_target_foreground
            and operation is not None
            and operation.auto_context is not None
            and self._operation_can_switch_mode(operation)
            and self._operation_mode_correction_failure(operation)
        )

    @Property(bool, notify=interactionChanged)
    def processingModeCorrectionAvailable(self) -> bool:
        interaction = self._active_auto_interaction
        failed_edit_fallback = self._failed_edit_fallback_interaction()
        return bool(
            failed_edit_fallback is not None
            or (
                self._processing_mode_correction_revealed
                and self._transcript_visible
                and self._interaction_state == "processing"
                and interaction is not None
                and interaction.classified
                and interaction.selected_mode == INPUT_MODE_EDIT
                and INPUT_MODE_DICTATION in getattr(interaction, "results", {})
            )
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
    def modeCorrectionPending(self) -> bool:
        pending = self._pending_applied_mode_switches.get(
            self._active_operation_target_key
        )
        operation = self._latest_operation()
        return bool(
            pending is not None
            and operation is not None
            and operation.auto_context is not None
            and pending[0] == operation.auto_context.route_id
        )

    @Property(bool, notify=interactionChanged)
    def appliedActionVisible(self) -> bool:
        return bool(
            self._applied_action_visible
            and self._applied_target_foreground
            and self._active_operation_stack()
        )

    @Property(str, notify=interactionChanged)
    def appliedActionText(self) -> str:
        operation = self._latest_operation()
        if operation is None:
            return ""
        summary = " ".join(str(operation.summary or "").split())
        limit = 42
        if len(summary) <= limit:
            return summary
        return summary[: limit - 3].rstrip() + "..."

    @Property(str, notify=interactionChanged)
    def appliedActionTitle(self) -> str:
        operation = self._latest_operation()
        if operation is None:
            return ""
        if operation.mode == INPUT_MODE_EDIT:
            return "已应用上一次修改"
        return "已输入文本"

    @Property(int, notify=interactionChanged)
    def undoDepth(self) -> int:
        return len(self._active_operation_stack())

    @Property(str, notify=interactionChanged)
    def appliedPopupPlacementKey(self) -> str:
        """Stable for one utterance, including its mode reinterpretations."""

        operation = self._latest_operation()
        return str(operation.session_id) if operation is not None else ""

    @Property(str, notify=interactionChanged)
    def appliedPopupApplicationKey(self) -> str:
        """Identify the application that owns the active undo overlay."""

        operation = self._latest_operation()
        if operation is None:
            return ""
        target = operation.target
        application_name = str(
            target.process_name or target.window_title or ""
        ).strip().casefold()
        if application_name:
            return f"application:{application_name}"
        if target.process_id:
            return f"process:{int(target.process_id)}"
        if target.window_handle:
            return f"window:{int(target.window_handle)}"
        return ""

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

    @Property(str, constant=True)
    def diagnosticLogPath(self) -> str:
        return str(self._diagnostic_log.path)

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
        previous_mode = self._input_mode
        self._input_mode = mode
        self._settings.setValue("input/mode", mode)
        self.inputModeChanged.emit()
        self._event_log(
            "USER_SETTING",
            setting="input_mode",
            previous=previous_mode,
            value=mode,
        )
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
        previous_mode = self._input_routing_mode
        self._input_routing_mode = mode
        self._settings.setValue("input/routingMode", mode)
        self.inputRoutingModeChanged.emit()
        self._event_log(
            "USER_SETTING",
            setting="input_routing_mode",
            previous=previous_mode,
            value=mode,
        )
        label = "自动判断" if mode == INPUT_ROUTING_AUTO else "手动切换"
        self._append_log(f"听写/指令路由已切换为：{label}")

    @Property(str, notify=settingsChanged)
    def appliedOverlayStyle(self) -> str:
        return self._applied_overlay_style

    @appliedOverlayStyle.setter
    def appliedOverlayStyle(self, value: str) -> None:
        style = str(value).strip().lower()
        if style not in {"normal", "compact"}:
            return
        self._set_setting(
            "_applied_overlay_style", style, "ui/appliedOverlayStyle"
        )

    @Property(int, notify=settingsChanged)
    def appliedOverlayDurationSeconds(self) -> int:
        return self._applied_overlay_duration_seconds

    @appliedOverlayDurationSeconds.setter
    def appliedOverlayDurationSeconds(self, value: int) -> None:
        try:
            duration = int(round(float(value)))
        except (TypeError, ValueError):
            return
        duration = max(1, min(duration, 10))
        if duration == self._applied_overlay_duration_seconds:
            return
        self._applied_overlay_duration_seconds = duration
        self._settings.setValue("ui/appliedOverlayDurationSeconds", duration)
        self._applied_action_hide_timer.setInterval(duration * 1000)
        if self._applied_action_visible:
            # Treat a live adjustment as a fresh display interval. This keeps
            # the setting predictable without touching the retained undo op.
            self._applied_action_hide_timer.start()
        self.settingsChanged.emit()
        self._event_log(
            "USER_SETTING",
            setting="undo_overlay_duration_seconds",
            value=duration,
        )

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
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(threshold) or threshold <= 0:
            return
        previous = self._stage1_threshold
        self._set_setting(
            "_stage1_threshold", threshold, "detector/stage1Threshold"
        )
        if threshold != previous:
            suffix = "，已实时生效" if self._connected else ""
            self._append_log(f"Stage1 threshold 已设为 {threshold:g}{suffix}")

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
        suffix = "，已实时生效" if self._connected else ""
        self._append_log(f"ASR 输入增益已设为 {gain_db:+.1f} dB{suffix}")

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
        self._event_log(
            "USER_ACTION",
            action="reconnect_device",
            accepted=bool(not self._connected and not self._busy),
            has_previous_device=self.canReconnect,
        )
        if self._connected or self._busy:
            return
        if not self.canReconnect:
            self.requestDevicePicker()
            return
        self._start_selected_device()

    @Slot()
    def requestDevicePicker(self) -> None:
        self._event_log(
            "USER_ACTION",
            action="open_device_picker",
            accepted=bool(not self._connected and not self._busy),
        )
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
        self._event_log(
            "USER_ACTION",
            action="connect_device",
            device_name=str(name).strip() or "未命名设备",
            accepted=not (self._connected or self._busy),
        )
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
        self._clear_undo_stack_for_device_boundary()
        self._close_floating_overlays_for_device_boundary()
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
        self._reset_battery_state()

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
        self._event_log(
            "RUNTIME_START",
            device_name=self._device_name,
            audio_encoding=self._audio_encoding,
            asr_backend=self._asr_backend,
            asr_device=self._asr_device,
            recognition_enabled=self._recognition_enabled,
        )
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
                    on_session_ended=self._suspend_recognition_for_interaction,
                    on_push_to_talk=self._pushToTalkChanged.emit,
                    on_asr_context=self._record_asr_context,
                    on_raw_audio=self._record_raw_interaction_audio,
                    on_raw_imu=self._record_raw_imu_samples,
                    on_battery=self._publish_battery_status,
                    asr_gain_db_provider=lambda: self._asr_gain_db,
                    stage1_threshold_provider=lambda: self._stage1_threshold,
                )
            except BaseException as exc:
                error = str(exc)
                self._append_background_diagnostic(
                    "[ERROR RUNTIME_THREAD] "
                    f"{type(exc).__name__}: {error}\n{traceback.format_exc()}"
                )
                print(f"[runtime] {error}")
                traceback.print_exc()
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
            self._append_background_diagnostic(
                "[ERROR DATASET_AUDIO] "
                f"session={int(session_id)} {type(exc).__name__}: {exc}"
            )
            print(f"[dataset] raw interaction audio was not saved: {exc}")

    def _record_asr_context(
        self,
        session_id: int,
        context: dict[str, object],
    ) -> None:
        """Persist the exact Doubao context without touching Qt UI state."""

        try:
            self._modification_dataset.record_asr_context(session_id, context)
        except BaseException as exc:
            self._append_background_diagnostic(
                "[ERROR DATASET_ASR_CONTEXT] "
                f"session={int(session_id)} {type(exc).__name__}: {exc}"
            )
            print(f"[dataset] ASR context was not saved: {exc}")

    @Slot(int)
    def _apply_runtime_session_started(self, session_id: int) -> None:
        """Bind UI cancellation and detector evidence before ASR emits text."""
        normalized = int(session_id)
        if normalized <= 0:
            return
        self._latest_asr_session_id = normalized
        self._diagnostic_session_started_at[normalized] = time.monotonic()
        while len(self._diagnostic_session_started_at) > 128:
            oldest_session = next(iter(self._diagnostic_session_started_at))
            self._diagnostic_session_started_at.pop(oldest_session, None)
        self._pipeline_log(
            "会话已建立；此前的 STAGE2 ACTIVATE 已绑定到本句",
            session_id=normalized,
        )
        self._event_log(
            "SESSION_START",
            session=normalized,
            routing=self._input_routing_mode,
            requested_mode=self._input_mode,
            **self._diagnostic_target_fields(self._speech_start_target),
        )
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
            self._append_background_diagnostic(
                "[ERROR DATASET_IMU] "
                f"session={int(session_id)} {type(exc).__name__}: {exc}"
            )
            print(f"[dataset] utterance IMU was not saved: {exc}")

    @Slot()
    def startRecognition(self) -> None:
        accepted = bool(
            self._connected and not self._busy and not self._recognition_enabled
        )
        self._event_log(
            "USER_ACTION",
            action="start_recognition",
            accepted=accepted,
            connected=self._connected,
            busy=self._busy,
        )
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
        accepted = bool(self._connected and self._recognition_enabled)
        self._event_log(
            "USER_ACTION",
            action="pause_recognition",
            accepted=accepted,
            connected=self._connected,
        )
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
        self._event_log(
            "USER_ACTION",
            action="disconnect_device",
            source="quit_cleanup" if self._quitting else "user",
            accepted=bool(worker is not None and worker.is_alive()),
            connected=self._connected,
        )
        if worker is None or not worker.is_alive():
            return
        self._clear_undo_stack_for_device_boundary()
        self._cancel_pending_text_processing()
        self._close_floating_overlays_for_device_boundary()
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
        self._event_log(
            "USER_ACTION",
            action="clear_voice_history",
            existing_entries=len(self._voice_history_entries),
        )
        if self._voice_player is not None:
            self._voice_player.stop()
        self._playing_voice_path = ""
        self.playingVoiceChanged.emit()
        try:
            self._voice_history.clear()
        except BaseException as exc:
            self._append_log(f"逐句语音记录清空失败：{exc}")
            self._event_log(
                "VOICE_HISTORY_CLEAR_RESULT", status="failed", reason=exc
            )
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
        self._event_log("VOICE_HISTORY_CLEAR_RESULT", status="applied")

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
        if self._mode_switch_application is not None:
            # record_application() publishes synchronously, before the same
            # conversion has atomically replaced its undo-stack slot. The
            # successful completion path reloads and resets the model once;
            # a failed path republishes the restored source outcome instead.
            return
        # This signal can cross from the runtime/ASR thread to Qt's UI thread.
        # Its payload is therefore only a notification: by the time it is
        # delivered, routing or application may already have updated the same
        # interaction.  Reload the authoritative projection instead of letting
        # a delayed pre-routing snapshot overwrite the final applied mode.
        self._refresh_voice_history_entries()

    @Slot()
    def _sync_voice_history_model(self) -> None:
        self._voice_history_model.replace_entries(self._voice_history_entries)

    def _refresh_voice_history_entries(
        self,
        *,
        force_model_reset: bool = False,
    ) -> None:
        try:
            entries = self._voice_history.load_entries()
        except BaseException as exc:
            self._append_log(f"刷新逐句语音记录失败：{exc}")
            return
        changed = entries != self._voice_history_entries
        if changed:
            self._voice_history_entries = entries
        if force_model_reset:
            self._voice_history_model.replace_entries(
                self._voice_history_entries,
                force_reset=True,
            )
        if changed:
            self.voiceHistoryChanged.emit()

    def _finalize_mode_switch_history(
        self,
        *,
        session_id: int,
        previous_mode: str,
        corrected_mode: str,
        record_correction: bool,
    ) -> None:
        """Publish one successful F8 conversion as a single visible outcome."""

        if record_correction:
            try:
                self._modification_dataset.record_mode_correction(
                    int(session_id),
                    previous_mode=previous_mode,
                    corrected_mode=corrected_mode,
                )
            except BaseException as exc:
                self._append_log(f"输入类型纠正数据保存失败：{exc}")

        # record_application() has already persisted the replacement that was
        # actually observed in the target field. Reload only after the optional
        # correction label is also committed, then force the QVariantMap-backed
        # QML delegate to rebuild. This is the sole completion point for F8.
        self._refresh_voice_history_entries(force_model_reset=True)
        interaction_id = self._modification_dataset.interaction_id_for_session(
            int(session_id)
        )
        entry = next(
            (
                item
                for item in self._voice_history_entries
                if str(item.get("interactionId", "")) == interaction_id
            ),
            None,
        )
        if entry is not None and str(entry.get("mode", "")) != corrected_mode:
            self._append_log(
                "语音记录同步异常：目标模式为"
                f" {corrected_mode}，实际投影为 {entry.get('mode', '') or '空'}"
            )

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
        self._append_background_diagnostic(
            f"[EVENT USER_ACTION] run={self._diagnostic_run_id} "
            'action="clear_live_log"'
        )
        self._log_lines.clear()
        self.logChanged.emit()

    @Slot()
    def openDiagnosticLogDirectory(self) -> None:
        try:
            self._diagnostic_log.path.parent.mkdir(parents=True, exist_ok=True)
            _open_data_directory(self._diagnostic_log.path.parent)
            self._event_log("USER_ACTION", action="open_diagnostic_log_directory")
        except BaseException as exc:
            self._append_log(f"打开诊断日志目录失败：{exc}")

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
        self._event_log(
            "USER_ACTION",
            action="quit_application",
            repeated=self._quitting,
            connected=self._connected,
            recognition_enabled=self._recognition_enabled,
        )
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
            # The producer-side endpoint callback normally closed the gate
            # before final ASR was queued. Keep this worker-thread check as an
            # idempotent fallback for nonstandard sinks/callers.
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
        active_session_id = (
            self._latest_asr_session_id
            if self._utterance_active and self._latest_asr_session_id > 0
            else 0
        )
        # Idle Stage2 rejects are the detector's normal background result and
        # quickly drown the useful pipeline evidence. Keep rejects only after
        # an ACTIVATE has established a real utterance session.
        suppress_idle_stage2_reject = bool(
            summary.startswith("STAGE2 ")
            and summary.upper().endswith("REJECT")
            and active_session_id <= 0
        )
        log_session_id = 0
        if (
            summary.startswith(("STAGE1 ", "STAGE2 "))
            and active_session_id > 0
        ):
            log_session_id = active_session_id
        elif summary.startswith(("[ASR] END", "[ASR] ABORT", "[ASR] CANCEL")):
            log_session_id = self._latest_asr_session_id
        if suppress_idle_stage2_reject:
            pass
        elif log_session_id > 0:
            self._pipeline_log(text, session_id=log_session_id)
        else:
            self._append_log(text)
        if summary.startswith(("STAGE1 ", "STAGE2 ")):
            try:
                if self._utterance_active and self._latest_asr_session_id > 0:
                    # Repeated detector evidence belongs to the open audio
                    # session only. Never fall back to the previous session.
                    self._modification_dataset.record_runtime_event(
                        text, self._latest_asr_session_id
                    )
                elif summary.startswith("STAGE2 ") and summary.endswith(
                    "ACTIVATE"
                ):
                    # The activating Stage2 event is emitted immediately before
                    # the ASR sink allocates its session id. Keep this one event
                    # for that next session and discard any stale idle window.
                    self._modification_dataset.begin_runtime_evidence_window()
                    self._modification_dataset.record_runtime_event(text)
            except BaseException as exc:
                self._append_log(f"检测证据保存失败：{exc}")
        elif summary.startswith("[ASR] START"):
            try:
                if summary.startswith(("[ASR] START manual", "[ASR] START direct")):
                    self._modification_dataset.begin_runtime_evidence_window()
                # START is emitted before the raw-audio observer allocates the
                # session id, so keep it beside the activating Stage2 event and
                # bind both when _apply_runtime_session_started arrives.
                self._modification_dataset.record_runtime_event(text)
            except BaseException as exc:
                self._append_log(f"ASR 开始证据保存失败：{exc}")
        elif summary.startswith(("[ASR] END", "[ASR] ABORT", "[ASR] CANCEL")):
            if self._latest_asr_session_id > 0:
                try:
                    self._modification_dataset.record_runtime_event(
                        text, self._latest_asr_session_id
                    )
                except BaseException as exc:
                    self._append_log(f"ASR 端点证据保存失败：{exc}")
        elif summary.startswith("[ASR TIMING]"):
            # Timing messages carry their originating session explicitly and
            # may arrive after detector endpointing. Bind by that id instead of
            # whichever utterance happened to be latest in the UI.
            timing_session_id = 0
            for token in summary.split():
                if not token.startswith("session="):
                    continue
                try:
                    timing_session_id = int(token.partition("=")[2])
                except ValueError:
                    timing_session_id = 0
                break
            if timing_session_id > 0:
                try:
                    self._modification_dataset.record_runtime_event(
                        text, timing_session_id
                    )
                except BaseException as exc:
                    self._append_log(f"ASR 时序证据保存失败：{exc}")
        if summary.startswith("[ASR] START"):
            # Stage2 activation is the authoritative start of a detected voice
            # session.  Show the overlay now, before ASR has any text to emit.
            self._stop_manual_association_watch()
            self._applied_action_visible = False
            self._applied_action_hide_timer.stop()
            self._ignore_asr_updates_until_next_start = False
            self._latest_asr_session_id = 0
            self._utterance_active = True
            self._speech_start_target = self._capture_desktop_reference()
            speech_target_key = self._operation_target_key(
                self._speech_start_target
            )
            if speech_target_key in self._operation_stacks:
                self._active_operation_target_key = speech_target_key
            self._transcript_primary_text = ""
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
        self._event_log("RUNTIME_CONNECTED", device_name=self._device_name)

    def _publish_battery_status(
        self,
        percentage: int | None,
        millivolts: int | None,
        charge_status: int | None,
    ) -> None:
        self._runtimeBatteryChanged.emit(
            int(percentage) if percentage is not None else -1,
            int(millivolts) if millivolts is not None else -1,
            int(charge_status) if charge_status is not None else -1,
        )

    @Slot(int, int, int)
    def _apply_runtime_battery(
        self,
        percentage: int,
        millivolts: int,
        charge_status: int,
    ) -> None:
        # Do not turn firmware sentinel/corrupt values such as 255 into a
        # convincing 100% reading.
        normalized_percentage = percentage if 0 <= percentage <= 100 else -1
        normalized_millivolts = (
            max(0, millivolts) if millivolts >= 0 else -1
        )
        normalized_charge_status = (
            charge_status if charge_status in {0, 1, 2} else -1
        )
        updated = (
            normalized_percentage,
            normalized_millivolts,
            normalized_charge_status,
        )
        current = (
            self._battery_percentage,
            self._battery_millivolts,
            self._battery_charge_status,
        )
        if updated == current and self._battery_query_complete:
            return
        self._battery_percentage = normalized_percentage
        self._battery_millivolts = normalized_millivolts
        self._battery_charge_status = normalized_charge_status
        self._battery_query_complete = True
        self.batteryChanged.emit()
        if percentage > 100:
            self._append_log(
                f"设备返回了无效电量值：{percentage}%"
                + (
                    f"（{normalized_millivolts} mV）"
                    if normalized_millivolts >= 0
                    else ""
                )
            )
            return
        if normalized_percentage >= 0:
            suffix = (
                "，充电中"
                if normalized_charge_status == 1
                else ("，已充满" if normalized_charge_status == 2 else "")
            )
            self._append_log(f"设备电量：{normalized_percentage}%{suffix}")

    def _reset_battery_state(self) -> None:
        if (
            self._battery_percentage,
            self._battery_millivolts,
            self._battery_charge_status,
        ) == (-1, -1, -1) and not self._battery_query_complete:
            return
        self._battery_percentage = -1
        self._battery_millivolts = -1
        self._battery_charge_status = -1
        self._battery_query_complete = False
        self.batteryChanged.emit()

    def _clear_undo_stack_for_device_boundary(self) -> None:
        """Retire text-operation undo state when a device session ends."""
        depth = sum(len(stack) for stack in self._operation_stacks.values())
        had_action_state = bool(
            depth
            or self._applied_action_visible
            or self._pending_applied_mode_switches
        )
        if not had_action_state:
            return
        self._operation_stacks.clear()
        self._active_operation_target_key = ""
        self._applied_action_visible = False
        self._applied_target_foreground = True
        self._applied_target_mismatch_count = 0
        self._applied_overlay_drag_active = False
        self._applied_overlay_foreground_grace_until = 0.0
        self._pending_applied_mode_switches.clear()
        self._mode_switch_application = None
        self._applied_action_hide_timer.stop()
        self._applied_target_timer.stop()
        self._provisional_association_recommendations.clear()
        if self._interaction_state == "applied":
            self._set_interaction_state("idle")
        self.interactionChanged.emit()
        if depth:
            self._append_log(
                f"设备会话结束，已清空 {depth} 条跨应用撤销记录"
            )

    def _close_floating_overlays_for_device_boundary(self) -> None:
        """Close every voice/result overlay when the Ring session ends."""
        transcript_changed = bool(
            self._transcript_visible or self._transcript_mode
        )
        association_changed = bool(
            self._association_recommendation is not None
            or self._association_queue
            or self._association_detail_visible
            or self._association_center_visible
        )
        self._hide_overlay_timer.stop()
        self._processing_mode_correction_timer.stop()
        self._transcript_visible = False
        self._transcript_mode = ""
        self._association_queue.clear()
        self._association_recommendation = None
        self._association_detail_visible = False
        self._association_center_visible = False
        self._association_center_stage = "home"
        self._association_center_entries = []
        self._clear_association_center_selection(emit=False)
        if transcript_changed:
            self.transcriptChanged.emit()
        if association_changed:
            self.associationChanged.emit()

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
        self._reset_battery_state()
        self._clear_undo_stack_for_device_boundary()
        self._cancel_pending_text_processing()
        self._close_floating_overlays_for_device_boundary()
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
        self._event_log(
            "RUNTIME_DISCONNECTED",
            had_connection=self._runtime_had_connection,
            was_recognizing=was_recognizing,
            quitting=self._quitting,
        )

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
        self._event_log("RUNTIME_READY", device_name=self._device_name)

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
            self._restore_applied_action_overlay()
            self._set_interaction_state("error")
            self.transcriptChanged.emit()
            self._hide_overlay_timer.start(3500)
            elapsed_ms = self._session_elapsed_ms(int(session_id))
            elapsed_detail = (
                f"；会话耗时 {elapsed_ms / 1000:.3f}s"
                if elapsed_ms is not None
                else ""
            )
            self._pipeline_log(
                f"ASR 错误{elapsed_detail}：{error}",
                session_id=int(session_id),
            )
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
                elapsed_ms = self._session_elapsed_ms(int(session_id))
                elapsed_detail = (
                    f"（会话耗时 {elapsed_ms / 1000:.3f}s）"
                    if elapsed_ms is not None
                    else ""
                )
                self._pipeline_log(
                    f"本段语音已结束，但没有识别出文字{elapsed_detail}",
                    session_id=int(session_id),
                )
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
                # This utterance did not apply anything, so resurfacing an
                # older undo action would misleadingly associate it with the
                # empty result. Keep the operation available in its stack, but
                # leave its presentation hidden until a later real operation.
                self._applied_action_visible = False
                self._applied_action_hide_timer.stop()
                self._applied_target_timer.stop()
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
        self._transcript_primary_text = text
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
        if routing_mode == INPUT_ROUTING_AUTO:
            self._transcript_text = "正在判断听写或指令"
        elif mode == INPUT_MODE_EDIT or self._llm_enabled:
            self._transcript_text = self._processing_overlay_text(mode, text)
        else:
            self._transcript_text = "正在输入文字"
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
            elapsed_ms = self._session_elapsed_ms(normalized_session_id)
            elapsed_detail = (
                f"；会话耗时 {elapsed_ms / 1000:.3f}s"
                if elapsed_ms is not None
                else ""
            )
            self._pipeline_log(
                f"ASR 识别完成（{len(text)} 个字符{elapsed_detail}）：{text}",
                session_id=normalized_session_id,
            )
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
        self._pending_mode_route_contexts[request_id] = _PendingModeRoute(
            target=target,
            session_id=int(session_id),
        )
        try:
            self._modification_dataset.record_routing_request(request)
        except BaseException as exc:
            self._append_log(f"输入类型路由数据保存失败：{exc}")
        if not was_processing:
            self.textProcessingChanged.emit()
        self._transcript_text = "正在判断听写或指令"
        self._transcript_final = False
        self._transcript_visible = True
        self._set_interaction_state("processing")
        self.transcriptChanged.emit()
        self._hide_overlay_timer.stop()
        self._pipeline_log(
            f"自动路由判断开始：{text}（模型：{request.settings.provider}/"
            f"{request.settings.model}）",
            session_id=request.session_id,
            route_id=request.request_id,
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
            self._pipeline_log(
                f"自动路由模型原始返回：{result.model_output}",
                session_id=result.session_id,
                route_id=result.request_id,
            )
        if result.error:
            self._pipeline_log(
                f"自动路由判断失败（{result.latency_s:.3f}s）：{result.error}；"
                f"回退为当前手动模式“{label}”",
                session_id=result.session_id,
                route_id=result.request_id,
            )
        else:
            self._pipeline_log(
                f"自动路由判断完成：{label}（耗时 {result.latency_s:.3f}s）",
                session_id=result.session_id,
                route_id=result.request_id,
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
                adapter = self._desktop_target_adapter()
                capture = getattr(adapter, "capture_text_allowing_empty", None)
                if not callable(capture):
                    capture = adapter.capture_text
                # Dictation needs a pre-application snapshot even when the
                # field is empty. Without that empty snapshot the bottom stack
                # item falls back to the application's unrelated native undo
                # history and can reinsert a sentence removed moments earlier.
                interaction.snapshot = capture(target)
                validate_edit_target_text(interaction.snapshot.text)
                self._log_edit_target_snapshot(interaction.snapshot)
            except BaseException as exc:
                interaction.candidate_errors[INPUT_MODE_EDIT] = str(exc)

        for mode in (INPUT_MODE_DICTATION, INPUT_MODE_EDIT):
            if mode == INPUT_MODE_DICTATION and not self._llm_enabled:
                continue
            if mode == INPUT_MODE_EDIT and (
                interaction.snapshot is None
                or INPUT_MODE_EDIT in interaction.candidate_errors
            ):
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
        if selected == INPUT_MODE_EDIT or self._llm_enabled:
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
            session_id=int(session_id),
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
        self._pipeline_log(
            f"LLM {label}处理已提交：{text}（模型："
            f"{request.settings.provider}/{request.settings.model}）",
            session_id=request.session_id,
            request_id=request.request_id,
            mode=normalized_mode,
        )
        if self._recognition_enabled and update_overlay:
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
            self._pipeline_log(
                f"LLM {label}原始返回：\n{model_output}",
                session_id=result.session_id,
                request_id=result.request_id,
                mode=result.mode,
            )
        if result.error:
            self._pipeline_log(
                f"LLM {label}处理失败：{result.error}",
                session_id=result.session_id,
                request_id=result.request_id,
                mode=result.mode,
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
                self._pipeline_log(
                    f"LLM 修改处理完成（{result.latency_s:.2f}s）：{outcome}",
                    session_id=result.session_id,
                    request_id=result.request_id,
                    mode=result.mode,
                )
            else:
                self._pipeline_log(
                    f"LLM 输入处理完成（{result.latency_s:.2f}s）："
                    f"{result.final_text}",
                    session_id=result.session_id,
                    request_id=result.request_id,
                    mode=result.mode,
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
        expected_switch = (int(route_id), result.mode)
        pending_switch_keys = [
            key
            for key, pending in self._pending_applied_mode_switches.items()
            if pending == expected_switch
        ]
        interaction = self._active_auto_interaction
        if interaction is None or interaction.route_id != int(route_id):
            matched = self._find_operation_for_route(route_id)
            interaction = matched[1].auto_context if matched is not None else None
        if interaction is None:
            # A late worker result must never leave the mode control spinning
            # forever after its operation was replaced or removed.
            if pending_switch_keys:
                for key in pending_switch_keys:
                    self._pending_applied_mode_switches.pop(key, None)
                self.interactionChanged.emit()
            return
        interaction.results[result.mode] = result
        matched = self._find_operation_for_route(route_id)
        matched_key = matched[0] if matched is not None else ""
        if pending_switch_keys:
            if result.error:
                # Both edit contracts can fail together. Resolve that terminal
                # result immediately instead of relying on a later QTimer to
                # clear the busy marker.
                for key in pending_switch_keys:
                    self._pending_applied_mode_switches.pop(key, None)
                operation = matched[1] if matched is not None else None
                if operation is not None:
                    reason = self._short_text(str(result.error), limit=34)
                    operation.mode_switch_error = reason
                    operation.summary = f"另一种处理方式不可用：{reason}"
                feedback_key = matched_key or pending_switch_keys[0]
                if (
                    operation is not None
                    and self._operation_target_is_focused(operation)
                ):
                    self._show_mode_correction_feedback(
                        feedback_key,
                        pending=False,
                    )
                else:
                    self.interactionChanged.emit()
                return
            self._schedule_alternate_result(
                result.mode,
                target_key=matched_key or pending_switch_keys[0],
                route_id=route_id,
            )
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
            self._transcript_text = (
                f"修改失败：{self._short_text(error, limit=48)}"
                if mode == INPUT_MODE_EDIT
                else "未能处理文本"
            )
            self._transcript_final = True
            self._transcript_visible = True
            self._set_interaction_state("error")
            self.transcriptChanged.emit()
            self.interactionChanged.emit()
            self._append_log(f"文本处理失败：{error}")
            self._hide_overlay_timer.start(3500)
            if self._recognition_enabled:
                if self._failed_edit_fallback_interaction() is not None:
                    self._set_status(
                        "修改未完成",
                        "原文本保持不变；按 F8 可改为听写输入",
                        "error",
                    )
                else:
                    self._set_status("自动监听中", error, "running")
            # A usable dictation fallback owns this short error window. This
            # prevents the next utterance from replacing its state before the
            # user has had a chance to press F8.
            if self._failed_edit_fallback_interaction() is None:
                self._resume_recognition_after_interaction()
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
            if self._recognition_enabled:
                label = "修改" if mode == INPUT_MODE_EDIT else "输入"
                self._set_status(
                    "正在处理文本",
                    f"大模型正在处理{label}内容",
                    "starting",
                )
            return
        if mode == INPUT_MODE_DICTATION:
            commit_target = target if target is not None else interaction.target
            if not self._llm_enabled:
                # The raw ASR final is already complete.  With dictation LLM
                # disabled there is no correction candidate to wait for.
                self._dictation_commit_timer.stop()
                self._pending_dictation_result = None
                self._commit_input_text(result, commit_target)
                return
            self._pending_dictation_result = (
                result,
                commit_target,
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
            self._reject_edit_request("修改目标快照已经失效")
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
        operation_for_log = self._latest_operation()
        self._event_log(
            "MODE_SWITCH_REQUEST",
            session=(
                processing.session_id
                if processing is not None
                else (
                    operation_for_log.session_id
                    if operation_for_log is not None
                    else self._latest_asr_session_id
                )
            ),
            interaction_state=self._interaction_state,
            processing=processing is not None,
            applied_mode=(
                operation_for_log.mode if operation_for_log is not None else ""
            ),
            target_key=self._active_operation_target_key,
            target_undo_depth=len(self._active_operation_stack()),
            total_undo_depth=sum(
                len(stack) for stack in self._operation_stacks.values()
            ),
            pending=bool(
                self._pending_applied_mode_switches.get(
                    self._active_operation_target_key
                )
            ),
        )
        if (
            processing is not None
            and processing.classified
            and processing.selected_mode == INPUT_MODE_EDIT
        ):
            result = processing.results.get(INPUT_MODE_DICTATION)
            if result is None or result.error:
                self._event_log(
                    "MODE_SWITCH_RESULT",
                    status="unavailable",
                    session=processing.session_id,
                    from_mode=INPUT_MODE_EDIT,
                    to_mode=INPUT_MODE_DICTATION,
                    reason=(
                        result.error
                        if result is not None
                        else "candidate_pending"
                    ),
                )
                return
            was_model_routed = processing.routed_by_model
            processing.selected_mode = INPUT_MODE_DICTATION
            self._processing_mode_correction_timer.stop()
            self._processing_mode_correction_revealed = False
            self._pending_dictation_result = None
            self._dictation_commit_timer.stop()
            self._stop_manual_association_watch()
            self.interactionChanged.emit()
            applied = self._commit_input_text(
                result,
                processing.target,
            )
            if not applied:
                self._event_log(
                    "MODE_SWITCH_RESULT",
                    status="failed",
                    session=processing.session_id,
                    from_mode=INPUT_MODE_EDIT,
                    to_mode=INPUT_MODE_DICTATION,
                    reason="dictation_not_applied",
                )
                return
            self._finalize_mode_switch_history(
                session_id=processing.session_id,
                previous_mode=INPUT_MODE_EDIT,
                corrected_mode=INPUT_MODE_DICTATION,
                record_correction=was_model_routed,
            )
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="applied",
                session=processing.session_id,
                from_mode=INPUT_MODE_EDIT,
                to_mode=INPUT_MODE_DICTATION,
            )
            self._append_log("用户已指明“刚刚是输入内容”，改为听写输入")
            return

        operation = self._latest_operation()
        if operation is None or operation.auto_context is None:
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="unavailable",
                reason="no_convertible_result",
            )
            self._append_log("收到类型转换操作，但当前没有可转换的语音结果")
            return
        if not self._operation_can_switch_mode(operation):
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="unavailable",
                session=operation.session_id,
                reason="unsafe_result",
            )
            self._append_log("收到类型转换操作，但当前结果无法安全转换")
            return
        self._append_log(
            "收到类型转换操作："
            + ("听写改为指令" if operation.mode == INPUT_MODE_DICTATION else "指令改为听写")
        )
        if not self._operation_target_is_focused(operation):
            operation.mode_switch_error = "无法确认仍是原文本框"
            operation.summary = "无法转换：请把光标放回原文本框后重试"
            self._append_log("类型转换未执行：无法确认当前焦点仍是原文本框")
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                session=operation.session_id,
                reason="target_not_focused",
            )
            self._show_mode_correction_feedback(
                self._active_operation_target_key,
                pending=False,
            )
            return
        target_key = self._active_operation_target_key
        if target_key in self._pending_applied_mode_switches:
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="pending",
                session=operation.session_id,
                reason="already_processing",
            )
            self._show_mode_correction_feedback(target_key, pending=True)
            return
        operation.mode_switch_error = ""
        interaction = operation.auto_context
        alternate_mode = (
            INPUT_MODE_DICTATION
            if operation.mode == INPUT_MODE_EDIT
            else INPUT_MODE_EDIT
        )
        if alternate_mode in interaction.candidate_errors:
            operation.mode_switch_error = str(
                interaction.candidate_errors[alternate_mode]
            )
            operation.summary = (
                "另一种处理方式不可用："
                + self._short_text(
                    interaction.candidate_errors[alternate_mode], limit=34
                )
            )
            self._show_mode_correction_feedback(target_key, pending=False)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                session=operation.session_id,
                from_mode=operation.mode,
                to_mode=alternate_mode,
                reason=interaction.candidate_errors[alternate_mode],
            )
            return
        result = interaction.results.get(alternate_mode)
        if result is None:
            self._pending_applied_mode_switches[target_key] = (
                interaction.route_id,
                alternate_mode,
            )
            operation.summary = (
                "正在准备指令结果…"
                if alternate_mode == INPUT_MODE_EDIT
                else "正在准备输入结果…"
            )
            self._show_mode_correction_feedback(target_key, pending=True)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="pending",
                session=operation.session_id,
                from_mode=operation.mode,
                to_mode=alternate_mode,
                reason="candidate_pending",
            )
            return
        if result.error:
            if alternate_mode == INPUT_MODE_EDIT:
                self._event_log(
                    "MODE_SWITCH_RESULT",
                    status="retrying",
                    session=operation.session_id,
                    from_mode=operation.mode,
                    to_mode=alternate_mode,
                    reason=result.error,
                )
                self._retry_explicit_edit_conversion(
                    operation,
                    target_key=target_key,
                )
                return
            reason = self._short_text(str(result.error), limit=34)
            operation.summary = f"另一种处理方式不可用：{reason}"
            self._show_mode_correction_feedback(target_key, pending=False)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                session=operation.session_id,
                from_mode=operation.mode,
                to_mode=alternate_mode,
                reason=result.error,
            )
            return
        if alternate_mode == INPUT_MODE_EDIT and (
            interaction.snapshot is None
            or result.final_text == result.target_text
            or not str(result.final_text or "").strip()
        ):
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="retrying",
                session=operation.session_id,
                from_mode=operation.mode,
                to_mode=alternate_mode,
                reason="invalid_edit_candidate",
            )
            self._retry_explicit_edit_conversion(
                operation,
                target_key=target_key,
            )
            return
        if (
            alternate_mode == INPUT_MODE_DICTATION
            and not str(result.final_text or result.raw_text or "").strip()
        ):
            operation.summary = "没有可应用的输入结果"
            self._show_mode_correction_feedback(target_key, pending=False)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                session=operation.session_id,
                from_mode=operation.mode,
                to_mode=alternate_mode,
                reason="empty_dictation_candidate",
            )
            return
        self._pending_applied_mode_switches[target_key] = (
            interaction.route_id,
            alternate_mode,
        )
        operation.summary = (
            "正在按指令重新处理…"
            if alternate_mode == INPUT_MODE_EDIT
            else "正在改为输入内容…"
        )
        self.interactionChanged.emit()
        self._event_log(
            "MODE_SWITCH_RESULT",
            status="applying",
            session=operation.session_id,
            from_mode=operation.mode,
            to_mode=alternate_mode,
        )
        self._schedule_alternate_result(
            alternate_mode,
            target_key=self._active_operation_target_key,
            route_id=interaction.route_id,
        )

    def _retry_explicit_edit_conversion(
        self,
        operation: _AppliedInteraction,
        *,
        target_key: str,
    ) -> None:
        """Retry a speculative edit failure after the user explicitly presses F8."""

        interaction = operation.auto_context
        if interaction is None:
            return
        snapshot = interaction.snapshot or operation.original_snapshot
        if snapshot is None:
            operation.summary = "无法转换：缺少编辑前文本快照"
            self._show_mode_correction_feedback(target_key, pending=False)
            return

        # The failed candidate was speculative.  Once the user explicitly says
        # the utterance was an instruction, submit a fresh edit request instead
        # of treating that cached failure as the final outcome.
        interaction.snapshot = snapshot
        interaction.results.pop(INPUT_MODE_EDIT, None)
        interaction.candidate_errors.pop(INPUT_MODE_EDIT, None)
        operation.mode_switch_error = ""
        self._pending_applied_mode_switches[target_key] = (
            interaction.route_id,
            INPUT_MODE_EDIT,
        )
        operation.summary = "正在重新处理指令…"
        self._show_mode_correction_feedback(target_key, pending=True)
        self._append_log("用户已指明“刚刚是指令”，重新提交编辑处理")
        self._submit_text_processing(
            interaction.raw_text,
            interaction.session_id,
            INPUT_MODE_EDIT,
            target_text=snapshot.text,
            target=operation.target,
            snapshot=snapshot,
            auto_route_id=interaction.route_id,
            update_overlay=False,
        )

    def _show_mode_correction_feedback(
        self,
        target_key: str,
        *,
        pending: bool,
    ) -> None:
        """Show conversion feedback without changing the undo stack."""

        stack = self._operation_stacks.get(target_key, [])
        if not stack:
            return
        self._active_operation_target_key = target_key
        self._applied_action_visible = True
        self._applied_target_foreground = True
        self._applied_target_mismatch_count = 0
        if not self._applied_target_timer.isActive():
            self._applied_target_timer.start()
        if pending:
            # A real LLM retry may outlive the configured presentation timeout.
            # Keep its progress visible until it resolves.
            self._applied_action_hide_timer.stop()
        else:
            self._applied_action_hide_timer.start()
        self.interactionChanged.emit()

    def _schedule_alternate_result(
        self,
        alternate_mode: str,
        *,
        target_key: str,
        route_id: int | None = None,
    ) -> None:
        """Render pending feedback before a potentially blocking desktop edit."""

        stack = self._operation_stacks.get(target_key, [])
        operation = stack[-1] if stack else None
        if operation is None or operation.auto_context is None:
            self._pending_applied_mode_switches.pop(target_key, None)
            self.interactionChanged.emit()
            return
        expected_route_id = int(
            operation.auto_context.route_id
            if route_id is None
            else route_id
        )
        expected = (expected_route_id, alternate_mode)
        if operation.auto_context.route_id != expected_route_id:
            if self._pending_applied_mode_switches.get(target_key) == expected:
                self._pending_applied_mode_switches.pop(target_key, None)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="cancelled",
                session=operation.session_id,
                to_mode=alternate_mode,
                reason="superseded_by_newer_operation",
                expected_route=expected_route_id,
                current_route=operation.auto_context.route_id,
            )
            self.interactionChanged.emit()
            return
        self._pending_applied_mode_switches[target_key] = expected
        operation.summary = (
            "正在按指令重新处理…"
            if alternate_mode == INPUT_MODE_EDIT
            else "正在改为输入内容…"
        )
        operation.mode_switch_error = ""
        # A late alternate result stays bound to its original field. Do not
        # pull the visible action window away from a newer active field.
        if target_key == self._active_operation_target_key:
            self._show_mode_correction_feedback(target_key, pending=True)
        else:
            self.interactionChanged.emit()

        def apply_after_feedback() -> None:
            if self._pending_applied_mode_switches.get(target_key) != expected:
                return
            try:
                self._apply_alternate_result(
                    alternate_mode,
                    target_key=target_key,
                    route_id=expected_route_id,
                )
            finally:
                for key, pending in tuple(
                    self._pending_applied_mode_switches.items()
                ):
                    if pending == expected:
                        self._pending_applied_mode_switches.pop(key, None)
                self.interactionChanged.emit()

        QTimer.singleShot(40, apply_after_feedback)

    def _apply_alternate_result(
        self,
        alternate_mode: str,
        *,
        target_key: str = "",
        route_id: int | None = None,
    ) -> None:
        selected_key = target_key or self._active_operation_target_key
        stack = self._operation_stacks.get(selected_key, [])
        operation = stack[-1] if stack else None
        if operation is None or operation.auto_context is None:
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                to_mode=alternate_mode,
                reason="operation_expired_before_apply",
            )
            return
        if (
            route_id is not None
            and operation.auto_context.route_id != int(route_id)
        ):
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="cancelled",
                session=operation.session_id,
                to_mode=alternate_mode,
                reason="superseded_before_apply",
                expected_route=int(route_id),
                current_route=operation.auto_context.route_id,
            )
            return
        if not self._operation_target_is_focused(operation):
            operation.mode_switch_error = "无法确认仍是原文本框"
            operation.summary = "无法转换：请把光标放回原文本框后重试"
            if selected_key == self._active_operation_target_key:
                self._show_mode_correction_feedback(selected_key, pending=False)
            else:
                # A late result for an older field must not pull the floating
                # controls away from the field the user is working in now.
                self.interactionChanged.emit()
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                session=operation.session_id,
                from_mode=operation.mode,
                to_mode=alternate_mode,
                reason="target_not_focused_before_apply",
            )
            return
        self._active_operation_target_key = selected_key
        interaction = operation.auto_context
        result = interaction.results.get(alternate_mode)
        if result is None or result.error:
            operation.mode_switch_error = (
                str(result.error).strip()
                if result is not None and result.error
                else "另一种处理方式不可用"
            )
            operation.summary = "另一种处理方式不可用"
            self._show_mode_correction_feedback(selected_key, pending=False)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                session=operation.session_id,
                from_mode=operation.mode,
                to_mode=alternate_mode,
                reason=operation.mode_switch_error,
            )
            return
        if alternate_mode == INPUT_MODE_EDIT:
            if (
                interaction.snapshot is None
                or result.final_text == result.target_text
                or not str(result.final_text or "").strip()
            ):
                operation.mode_switch_error = "没有可应用的编辑结果"
                operation.summary = "没有可应用的编辑结果"
                self._show_mode_correction_feedback(selected_key, pending=False)
                self._event_log(
                    "MODE_SWITCH_RESULT",
                    status="failed",
                    session=operation.session_id,
                    from_mode=operation.mode,
                    to_mode=alternate_mode,
                    reason="invalid_edit_candidate",
                )
                return
        elif not str(result.final_text or result.raw_text or "").strip():
            operation.mode_switch_error = "没有可应用的听写结果"
            operation.summary = "没有可应用的听写结果"
            self._show_mode_correction_feedback(selected_key, pending=False)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                session=operation.session_id,
                from_mode=operation.mode,
                to_mode=alternate_mode,
                reason="empty_dictation_candidate",
            )
            return

        previous_mode = operation.mode
        if self._mode_switch_application is not None:
            operation.mode_switch_error = "另一项类型转换仍在应用"
            operation.summary = "正在完成上一次类型转换"
            self._show_mode_correction_feedback(selected_key, pending=True)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="pending",
                session=operation.session_id,
                from_mode=previous_mode,
                to_mode=alternate_mode,
                reason="application_in_progress",
            )
            return
        previous_applied_state: str | None = None
        try:
            previous_applied_state = self._read_target_text_for_undo(
                (
                    operation.original_snapshot.target
                    if operation.original_snapshot is not None
                    else operation.target
                ),
                allow_focused_fallback=True,
                allow_empty=True,
            )
        except BaseException as exc:
            self._append_log(f"切换前完整文本回读不可用：{exc}")
        try:
            self._undo_applied_operation(operation)
        except BaseException as exc:
            operation.mode_switch_error = str(exc)
            operation.summary = f"无法切换：{exc}"
            self._append_log(f"类型转换未执行：{exc}")
            self._show_mode_correction_feedback(selected_key, pending=False)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed",
                session=operation.session_id,
                from_mode=previous_mode,
                to_mode=alternate_mode,
                reason=exc,
            )
            return

        # The alternate result is a new representation of the same utterance,
        # not a new user operation. Bind the generic application path to this
        # exact stack slot so _show_applied_interaction() replaces it in place.
        # This remains stable even if the target app rebuilds its accessibility
        # object after a paste.
        mode_switch = _ModeSwitchApplication(
            source_key=selected_key,
            source_operation=operation,
            expected_mode=alternate_mode,
            interaction=interaction,
        )
        self._mode_switch_application = mode_switch
        try:
            self._active_auto_interaction = interaction
            interaction.selected_mode = alternate_mode
            if alternate_mode == INPUT_MODE_DICTATION:
                self._commit_input_text(result, operation.target)
            else:
                self._begin_edit_review(result, interaction.snapshot)
        except BaseException as exc:
            mode_switch.error = str(exc)
            self._append_log(f"替代结果应用过程异常：{exc}")
        finally:
            if self._mode_switch_application is mode_switch:
                self._mode_switch_application = None

        replacement = mode_switch.replacement
        succeeded = replacement is not None and not mode_switch.error
        if not succeeded:
            operation.mode_switch_error = (
                mode_switch.error or "替代结果没有成功应用"
            )
            operation.summary = "转换未完成，已恢复原结果"
            # If an exception happened after the atomic slot replacement, put
            # the source operation back before restoring the external text.
            if replacement is not None:
                source_stack = self._operation_stacks.get(selected_key, [])
                for index, candidate in enumerate(source_stack):
                    if candidate is replacement:
                        source_stack[index] = operation
                        break
            try:
                self._restore_applied_operation(
                    operation,
                    full_text=previous_applied_state,
                )
            except BaseException as exc:
                self._append_log(f"切换失败且原结果恢复失败：{exc}")
            else:
                self._active_operation_target_key = selected_key
                self._applied_action_visible = True
                self._applied_action_hide_timer.start()
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
            self._show_mode_correction_feedback(selected_key, pending=False)
            self._event_log(
                "MODE_SWITCH_RESULT",
                status="failed_restored_original",
                session=operation.session_id,
                from_mode=previous_mode,
                to_mode=alternate_mode,
                reason=operation.mode_switch_error,
            )
            return
        self._active_operation_target_key = selected_key
        self._finalize_mode_switch_history(
            session_id=interaction.session_id,
            previous_mode=previous_mode,
            corrected_mode=alternate_mode,
            record_correction=interaction.routed_by_model,
        )
        self._event_log(
            "MODE_SWITCH_RESULT",
            status="applied",
            session=interaction.session_id,
            from_mode=previous_mode,
            to_mode=alternate_mode,
            target_key=selected_key,
            target_undo_depth=len(self._operation_stacks.get(selected_key, [])),
            total_undo_depth=sum(
                len(operation_stack)
                for operation_stack in self._operation_stacks.values()
            ),
        )
        self._append_log(
            "已按另一种方式重新处理："
            + ("听写内容" if alternate_mode == INPUT_MODE_DICTATION else "编辑指令")
        )

    def _undo_applied_operation(self, operation: _AppliedInteraction) -> None:
        if operation.original_snapshot is not None:
            self._restore_snapshot_for_undo(operation)
        else:
            self._undo_native_dictation(operation)

    def _restore_applied_operation(
        self,
        operation: _AppliedInteraction,
        *,
        full_text: str | None = None,
    ) -> None:
        adapter = self._desktop_target_adapter()
        if full_text is not None:
            target = (
                operation.original_snapshot.target
                if operation.original_snapshot is not None
                else operation.target
            )
            adapter.replace(DesktopTextSnapshot(target, ""), full_text)
            return
        if (
            operation.mode == INPUT_MODE_EDIT
            and operation.original_snapshot is not None
        ):
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
    ) -> bool:
        auto_context = self._active_auto_interaction
        text = result.final_text
        final_text = str(text or "").strip()
        if not final_text:
            self._append_log("文本处理完成，但没有可注入的文字")
            self._finish_auto_interaction(INPUT_MODE_DICTATION, retain=False)
            self._resume_recognition_after_interaction()
            return False
        if normalize_input_mode(result.mode) == INPUT_MODE_DICTATION:
            self._copy_text_to_clipboard(final_text)
        # Stage2 normally locks the target before the overlay appears. If that
        # first capture raced a focus transition, make one last attempt at the
        # actual commit point instead of silently discarding valid dictation.
        if self._desktop_output and target is None:
            target = self._capture_desktop_reference()
        if result.used_llm:
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
        elapsed_ms = self._session_elapsed_ms(int(result.session_id))
        elapsed_detail = (
            f"；会话总耗时 {elapsed_ms / 1000:.3f}s"
            if elapsed_ms is not None
            else ""
        )
        self._pipeline_log(
            f"应用结果：{'applied' if applied else 'apply_failed'}；"
            f"{detail}{elapsed_detail}",
            session_id=result.session_id,
            request_id=result.request_id,
            mode=INPUT_MODE_DICTATION,
        )
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
            return True
        if self._recognition_enabled:
            self._set_status("自动监听中", "输入完成，等待下一段语音", "running")
        self._resume_recognition_after_interaction()
        return False

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
        failed_result = replace(result, error=error)
        interaction = self._active_auto_interaction
        if (
            interaction is not None
            and interaction.session_id == int(result.session_id)
            and interaction.selected_mode == INPUT_MODE_EDIT
        ):
            interaction.results[INPUT_MODE_EDIT] = failed_result
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
        self._transcript_text = f"修改失败：{self._short_text(error, limit=48)}"
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
        self._pipeline_log(
            "应用结果：apply_failed；LLM 修改失败，原文本保持不变："
            f"{error}"
            + (
                f"；会话总耗时 {elapsed_ms / 1000:.3f}s"
                if (elapsed_ms := self._session_elapsed_ms(int(result.session_id)))
                is not None
                else ""
            ),
            session_id=result.session_id,
            request_id=result.request_id,
            mode=INPUT_MODE_EDIT,
        )
        has_dictation_fallback = (
            self._failed_edit_fallback_interaction() is not None
        )
        if not has_dictation_fallback:
            self._finish_auto_interaction(INPUT_MODE_EDIT, retain=False)
        self._hide_overlay_timer.start(2200)
        if self._recognition_enabled:
            detail = "原文本保持不变"
            if has_dictation_fallback:
                detail += "；按 F8 可改为听写输入"
            self._set_status("修改未完成", detail, "error")
        if not has_dictation_fallback:
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
        elapsed_ms = self._session_elapsed_ms(int(review.session_id))
        elapsed_detail = (
            f"；会话总耗时 {elapsed_ms / 1000:.3f}s"
            if elapsed_ms is not None
            else ""
        )
        self._pipeline_log(
            "应用结果：applied；修改已写入并完成回读校验"
            + elapsed_detail,
            session_id=review.session_id,
            request_id=review.request_id,
            mode=INPUT_MODE_EDIT,
        )
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
        self._event_log(
            "GLOBAL_ACTION",
            action=action,
            session=self._latest_asr_session_id,
            interaction_state=self._interaction_state,
        )
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
        """Register one application, atomically replacing an F8 source item."""
        self._edit_review = None
        interaction = replace(
            interaction,
            target=self._target_with_live_caret(interaction.target),
        )
        mode_switch = self._mode_switch_application
        is_mode_switch = bool(
            mode_switch is not None
            and interaction.auto_context is mode_switch.interaction
            and interaction.session_id == mode_switch.source_operation.session_id
            and interaction.mode == mode_switch.expected_mode
        )
        if is_mode_switch:
            assert mode_switch is not None
            target_key = mode_switch.source_key
            stack = self._operation_stacks.get(target_key, [])
            try:
                source_index = next(
                    index
                    for index, candidate in enumerate(stack)
                    if candidate is mode_switch.source_operation
                )
            except StopIteration:
                mode_switch.error = "原撤销项在转换完成前已经失效"
                self._append_log(
                    "转换结果已写入，但原撤销项已经失效；正在恢复转换前文本"
                )
                self._resume_recognition_after_interaction()
                return
            # A mode correction is another representation of the same spoken
            # interaction. Replace that exact stack slot before notifying QML;
            # never expose a transient extra undo item or split the stack when
            # an Electron/WebKit accessibility identity refreshes after paste.
            stack[source_index] = interaction
            mode_switch.replacement = interaction
        else:
            target_key, stack = self._operation_stack_for_target(
                interaction.target,
                create=True,
            )
            if not target_key:
                self._append_log("无法识别目标文本框，未创建撤销记录")
                if self._recognition_enabled:
                    self._set_status(
                        "自动监听中", "结果已应用，但无法创建撤销记录", "running"
                    )
                self._resume_recognition_after_interaction()
                return
            stack.append(interaction)
        self._active_operation_target_key = target_key
        self._applied_action_visible = True
        self._applied_target_foreground = True
        self._applied_target_mismatch_count = 0
        if not self._applied_target_timer.isActive():
            self._applied_target_timer.start()
        self._applied_action_hide_timer.start()
        if not is_mode_switch:
            self._pending_applied_mode_switches.pop(target_key, None)
        self._transcript_mode = ""
        self._transcript_text = ""
        self._transcript_final = True
        self._transcript_visible = False
        self._set_interaction_state("applied")
        self._hide_overlay_timer.stop()
        if not is_mode_switch:
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
        if not is_mode_switch:
            self._refresh_voice_history_entries()
        self._event_log(
            "UNDO_STACK_CHANGE",
            action="replace" if is_mode_switch else "push",
            session=interaction.session_id,
            mode=interaction.mode,
            target_key=target_key,
            target_undo_depth=len(stack),
            total_undo_depth=sum(
                len(operation_stack)
                for operation_stack in self._operation_stacks.values()
            ),
        )
        self._append_log(
            message
            + (
                "；已替换当前撤销项"
                if is_mode_switch
                else "；已加入撤销栈"
            )
        )
        if self._recognition_enabled:
            self._set_status("自动监听中", "结果已应用，可在光标旁撤销", "running")
        self._resume_recognition_after_interaction()

    def _target_with_live_caret(self, target: DesktopTargetRef) -> DesktopTargetRef:
        """Refresh the post-application field identity and compact-pill anchor."""
        adapter = self._desktop_target_adapter()
        capture = getattr(adapter, "capture_reference", None)
        if callable(capture):
            try:
                live_target = capture()
            except BaseException as exc:
                self._append_log(f"应用后文本框定位刷新失败：{exc}")
            else:
                same_application = bool(
                    live_target is not None
                    and (
                        int(live_target.process_id or 0)
                        == int(target.process_id or 0)
                        or (
                            target.window_handle
                            and live_target.window_handle == target.window_handle
                        )
                    )
                )
                if same_application:
                    live_has_bounds = bool(
                        live_target.screen_width > 0
                        and live_target.screen_height > 0
                    )
                    live_has_caret = bool(live_target.caret_height > 0)
                    # Electron/WebKit can replace the focused AX object as a
                    # paste lands. Store the object and geometry *after* the
                    # application, otherwise every following poll compares
                    # against a reference that no longer exists.
                    target = replace(
                        target,
                        window_handle=(
                            live_target.window_handle or target.window_handle
                        ),
                        control_handle=(
                            live_target.control_handle or target.control_handle
                        ),
                        window_title=(
                            live_target.window_title or target.window_title
                        ),
                        process_id=live_target.process_id or target.process_id,
                        process_name=(
                            live_target.process_name or target.process_name
                        ),
                        uia_control=(live_target.uia_control or target.uia_control),
                        screen_x=(
                            live_target.screen_x
                            if live_has_bounds
                            else target.screen_x
                        ),
                        screen_y=(
                            live_target.screen_y
                            if live_has_bounds
                            else target.screen_y
                        ),
                        screen_width=(
                            live_target.screen_width
                            if live_has_bounds
                            else target.screen_width
                        ),
                        screen_height=(
                            live_target.screen_height
                            if live_has_bounds
                            else target.screen_height
                        ),
                        caret_x=(
                            live_target.caret_x
                            if live_has_caret
                            else target.caret_x
                        ),
                        caret_y=(
                            live_target.caret_y
                            if live_has_caret
                            else target.caret_y
                        ),
                        caret_width=(
                            live_target.caret_width
                            if live_has_caret
                            else target.caret_width
                        ),
                        caret_height=(
                            live_target.caret_height
                            if live_has_caret
                            else target.caret_height
                        ),
                        accessibility_id=live_target.accessibility_id,
                    )

        locator = getattr(adapter, "caret_bounds", None)
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

    def _commit_associated_result(
        self,
        target_key: str,
        interaction: _AppliedInteraction,
    ) -> None:
        """Commit one target stack without touching other text fields."""

        stack = self._operation_stacks.get(target_key, [])
        if not stack or interaction not in stack:
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
        # Older operations in this field cannot be popped safely underneath a
        # committed edit. Other applications and text fields stay independent.
        self._operation_stacks.pop(target_key, None)
        self._pending_applied_mode_switches.pop(target_key, None)
        if self._active_operation_target_key == target_key:
            self._active_operation_target_key = ""
        self._applied_action_visible = bool(self._operation_stacks)
        if not self._operation_stacks:
            self._applied_target_timer.stop()
        self._provisional_association_recommendations.pop(target_key, None)
        self.interactionChanged.emit()
        if self._recognition_enabled:
            self._set_status("自动监听中", "结果已确认，等待下一段语音", "running")

    def _retract_applied_recommendations(
        self, interaction: _AppliedInteraction
    ) -> None:
        target_key = self._operation_target_key(interaction.target)
        recommendations = list(
            self._provisional_association_recommendations.pop(target_key, [])
        )
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
        self.associationChanged.emit()

    def _read_target_text_for_undo(
        self,
        target: DesktopTargetRef,
        *,
        allow_focused_fallback: bool = False,
        allow_empty: bool = False,
    ) -> str | None:
        """Read the locked field, optionally falling back to an explicit copy.

        The normal path never disturbs selection. During an explicit undo we
        may use Select-All/Copy as a last-resort verifier; proving that this
        works also makes a later full-snapshot replacement safe.
        """
        adapter = self._desktop_target_adapter()
        observe = getattr(adapter, "observe_text", None)
        if callable(observe):
            try:
                return str(observe(target).text)
            except RuntimeError as exc:
                if allow_focused_fallback:
                    observe_focused = getattr(
                        adapter, "observe_focused_text", None
                    )
                    if callable(observe_focused):
                        try:
                            return str(observe_focused(target).text)
                        except RuntimeError as focused_exc:
                            if "不支持无干扰" not in str(focused_exc):
                                raise
                if "不支持无干扰" not in str(exc):
                    raise
        if not allow_focused_fallback:
            return None
        capture = (
            getattr(adapter, "capture_text_allowing_empty", None)
            if allow_empty
            else None
        )
        if not callable(capture):
            capture = getattr(adapter, "capture_text", None)
        if not callable(capture):
            return None
        try:
            return str(capture(target).text)
        except BaseException as exc:
            self._append_log(f"撤销文本回读不可用，将只执行一次原生撤销：{exc}")
            return None

    @staticmethod
    def _dictation_state_matches_snapshot(
        current_text: str,
        original_text: str,
        inserted_text: str,
    ) -> bool:
        """Return whether removing one dictated fragment restores the snapshot."""
        if not inserted_text:
            return False
        start = 0
        while True:
            index = current_text.find(inserted_text, start)
            if index < 0:
                return False
            without_insertion = (
                current_text[:index] + current_text[index + len(inserted_text) :]
            )
            if macos_texts_equivalent(without_insertion, original_text):
                return True
            start = index + 1

    def _restore_snapshot_for_undo(
        self,
        interaction: _AppliedInteraction,
    ) -> None:
        """Restore and verify a snapshot before allowing its stack item to pop."""
        original = interaction.original_snapshot
        if original is None:
            raise RuntimeError("本次操作没有可恢复的文本快照")
        expected_text = str(original.text)
        # The stack's target may contain a refreshed post-application caret
        # used only to place the action pill.  Text I/O must keep using the
        # original captured control identity.
        target = original.target
        current_text = self._read_target_text_for_undo(
            target,
            allow_focused_fallback=True,
            allow_empty=not bool(interaction.applied_text),
        )
        if current_text is not None:
            if macos_texts_equivalent(current_text, expected_text):
                # The target was already restored outside this process.  The
                # stack item can be retired without writing the field again.
                return
            if interaction.mode == INPUT_MODE_EDIT:
                state_matches = macos_texts_equivalent(
                    current_text, interaction.applied_text
                )
            else:
                state_matches = self._dictation_state_matches_snapshot(
                    current_text,
                    expected_text,
                    interaction.applied_text,
                )
            if not state_matches:
                raise RuntimeError(
                    "当前文本已在语音结果之后发生变化；为避免覆盖新内容，"
                    "已保留撤销记录"
                )

        adapter = self._desktop_target_adapter()
        # The first dictated sentence commonly has an empty pre-snapshot. When
        # the field is readable, clear it from that snapshot instead of trusting
        # older host history. If the field is completely unobservable, one native
        # Undo is safer than an unverified Select-All/Delete fallback.
        native_undo = (
            getattr(adapter, "undo", None)
            if expected_text or current_text is None
            else None
        )
        if callable(native_undo):
            try:
                native_undo(target)
                actual = self._read_target_text_for_undo(
                    target,
                    allow_focused_fallback=True,
                    allow_empty=not bool(expected_text),
                )
                if actual is None:
                    # This control cannot prove whether Command/Ctrl+Z worked.
                    # Never follow that unverified mutation with a full-snapshot
                    # paste: editors that ignore Select-All append the snapshot
                    # and create exponentially duplicated text across undos.
                    self._append_log(
                        "目标文本框无法静默验证；已只执行一次原生撤销，"
                        "未追加整段快照"
                    )
                    return
                # When readback is available, require proof that native Undo
                # reached the saved snapshot; otherwise restore it precisely.
                if macos_texts_equivalent(actual, expected_text):
                    return
            except BaseException as exc:
                self._append_log(f"目标应用原生撤销未完成，改用精确恢复：{exc}")

        # Native Undo is the fast path.  A full snapshot write is reserved for
        # applications whose own undo stack did not reach the expected state.
        adapter.replace(
            DesktopTextSnapshot(target, interaction.applied_text),
            expected_text,
        )
        actual = self._read_target_text_for_undo(
            target,
            allow_focused_fallback=True,
            allow_empty=not bool(expected_text),
        )
        # When AXValue cannot be read, this is still a deterministic write from
        # the saved pre-application snapshot.  Unlike native Undo, it does not
        # depend on an external application's private history.
        if actual is None or macos_texts_equivalent(actual, expected_text):
            return
        preview = self._short_text(actual, limit=36)
        detail = f"，当前仍为“{preview}”" if preview else ""
        raise RuntimeError(f"目标文本没有恢复{detail}；已保留撤销记录")

    def _undo_native_dictation(self, interaction: _AppliedInteraction) -> None:
        """Safely remove a legacy dictation that lacks a pre-snapshot."""
        before = self._read_target_text_for_undo(
            interaction.target,
            allow_focused_fallback=True,
            allow_empty=not bool(interaction.applied_text),
        )
        if before is None:
            raise RuntimeError(
                "本次听写没有保存原文本且当前文本框无法安全回读；"
                "为避免调用错误的应用撤销记录，已保留撤销操作"
            )
        inserted = str(interaction.applied_text or "")
        positions: list[int] = []
        start = 0
        while inserted:
            index = before.find(inserted, start)
            if index < 0:
                break
            positions.append(index)
            start = index + 1
        if len(positions) != 1:
            raise RuntimeError(
                "本次听写缺少原文本快照，且无法唯一定位写入内容；"
                "为避免删除错误文字，已保留撤销操作"
            )
        index = positions[0]
        expected = before[:index] + before[index + len(inserted) :]
        adapter = self._desktop_target_adapter()
        adapter.replace(
            DesktopTextSnapshot(interaction.target, before),
            expected,
        )
        after = self._read_target_text_for_undo(
            interaction.target,
            allow_focused_fallback=True,
            allow_empty=not bool(expected),
        )
        if after is not None and not macos_texts_equivalent(after, expected):
            raise RuntimeError("目标文本没有恢复；已保留撤销记录")

    @Slot()
    def undoLastApplied(self) -> None:
        """Pop and undo the latest dictation or edit operation."""
        target_key = self._active_operation_target_key
        stack = self._operation_stacks.get(target_key, [])
        interaction = stack[-1] if stack else None
        self._event_log(
            "UNDO_REQUEST",
            accepted=interaction is not None,
            session=interaction.session_id if interaction is not None else 0,
            mode=interaction.mode if interaction is not None else "",
            target_key=target_key,
            target_undo_depth=len(stack),
        )
        if interaction is None:
            return
        if not self._operation_target_is_focused(interaction):
            self._event_log(
                "UNDO_RESULT",
                status="failed",
                session=interaction.session_id,
                reason="target_not_focused",
            )
            self._hide_applied_action_for_focus_mismatch()
            return
        self._hide_overlay_timer.stop()
        try:
            if interaction.original_snapshot is not None:
                self._restore_snapshot_for_undo(interaction)
            else:
                self._undo_native_dictation(interaction)
        except BaseException as exc:
            self._transcript_text = f"撤回失败：{exc}"
            self._transcript_final = True
            self._transcript_visible = True
            self._set_interaction_state("error")
            self.transcriptChanged.emit()
            self.interactionChanged.emit()
            self._append_log(f"撤回上次结果失败：{exc}")
            self._event_log(
                "UNDO_RESULT",
                status="failed",
                session=interaction.session_id,
                mode=interaction.mode,
                reason=exc,
            )
            self._set_status(
                "撤销未完成",
                "没有确认原文本恢复；撤销记录仍然保留",
                "error",
            )
            return

        self._retract_applied_recommendations(interaction)
        stack.pop()
        if not stack:
            self._operation_stacks.pop(target_key, None)
            self._active_operation_target_key = ""
        self._applied_action_visible = bool(self._operation_stacks)
        self._applied_target_foreground = True
        self._applied_target_mismatch_count = 0
        if not self._operation_stacks:
            self._applied_target_timer.stop()
        self._pending_applied_mode_switches.pop(target_key, None)
        mode_label = "修改" if interaction.mode == INPUT_MODE_EDIT else "听写"
        self._transcript_mode = ""
        self._transcript_text = ""
        self._transcript_final = True
        self._transcript_visible = False
        self._set_interaction_state(
            "applied" if stack else "idle"
        )
        self.transcriptChanged.emit()
        self.interactionChanged.emit()
        self._record_history(
            f"{mode_label} · 已撤回",
            raw=interaction.raw_text,
        )
        self._append_log(f"用户已撤回上次{mode_label}结果")
        self._event_log(
            "UNDO_RESULT",
            status="applied",
            session=interaction.session_id,
            mode=interaction.mode,
            remaining_target_undo_depth=len(stack),
        )
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
            remaining = len(stack)
            detail = (
                f"已撤回，仍可继续撤回 {remaining} 次"
                if remaining
                else "已撤回，等待下一段语音"
            )
            self._set_status("自动监听中", detail, "running")

    @Slot()
    def cancelCurrentUtterance(self) -> None:
        """Cancel one utterance at any stage without stopping recognition."""
        self._event_log(
            "CANCEL_REQUEST",
            accepted=self.interactionCanCancel,
            session=self._latest_asr_session_id,
            interaction_state=self._interaction_state,
            utterance_active=self._utterance_active,
            text_processing=self.textProcessing,
        )
        if not self.interactionCanCancel:
            return
        cancelled_session_id = self._latest_asr_session_id
        had_active_audio = self._utterance_active
        cancelled_mode = self._transcript_mode or self._input_mode
        cancelled_target = self._session_targets.get(
            int(cancelled_session_id), self._speech_start_target
        )
        if cancelled_session_id > 0:
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
        cancelled_text_requests = {
            request_id
            for request_id, context in self._pending_interactions.items()
            if context.session_id == int(cancelled_session_id)
        }
        cancelled_mode_routes = {
            request_id
            for request_id, context in self._pending_mode_route_contexts.items()
            if context.session_id == int(cancelled_session_id)
        }
        interaction = self._active_auto_interaction
        interaction_is_current = bool(
            interaction is not None
            and (
                (
                    cancelled_session_id > 0
                    and interaction.session_id == int(cancelled_session_id)
                )
                or (
                    cancelled_session_id <= 0
                    and not had_active_audio
                    and interaction.session_id <= 0
                )
            )
        )
        for request_id in tuple(
            cancelled_text_requests | cancelled_mode_routes
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
        if (
            interaction_is_current
            and interaction is not None
            and interaction.snapshot is not None
        ):
            try:
                self._desktop_target_adapter().release_selection(
                    interaction.snapshot.target
                )
            except BaseException:
                pass
        self._pending_text_requests.difference_update(cancelled_text_requests)
        for request_id in cancelled_text_requests:
            self._pending_interactions.pop(request_id, None)
        self._pending_mode_routes.difference_update(cancelled_mode_routes)
        for request_id in cancelled_mode_routes:
            self._pending_mode_route_contexts.pop(request_id, None)
        if interaction_is_current:
            self._active_auto_interaction = None
        if cancelled_session_id > 0:
            self._session_input_modes.pop(int(cancelled_session_id), None)
            self._session_routing_modes.pop(int(cancelled_session_id), None)
            self._session_targets.pop(int(cancelled_session_id), None)
        self._speech_start_target = None
        if was_processing != self.textProcessing:
            self.textProcessingChanged.emit()
        self._restore_applied_action_overlay()
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
        self._event_log(
            "CANCEL_RESULT",
            status="applied",
            session=cancelled_session_id,
            mode=cancelled_mode,
            had_active_audio=had_active_audio,
            cancelled_llm_requests=len(cancelled_text_requests),
            cancelled_route_requests=len(cancelled_mode_routes),
        )
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
        if not self._operation_stacks:
            if self._applied_target_timer.isActive():
                self._applied_target_timer.stop()
            self._applied_target_mismatch_count = 0
            if self._applied_target_foreground:
                self._applied_target_foreground = False
                self.interactionChanged.emit()
            return
        if (
            self._applied_overlay_drag_active
            or time.monotonic() < self._applied_overlay_foreground_grace_until
        ):
            if (
                self._active_operation_stack()
                and not self._applied_target_foreground
            ):
                self._applied_target_foreground = True
                self.interactionChanged.emit()
            return
        adapter = self._desktop_target_adapter()
        is_foreground = getattr(adapter, "is_foreground", None)
        matched_key = ""
        if callable(is_foreground):
            ordered_keys = list(self._operation_stacks)
            active_key = self._active_operation_target_key
            if active_key in self._operation_stacks:
                ordered_keys.remove(active_key)
                ordered_keys.insert(0, active_key)
            for key in ordered_keys:
                stack = self._operation_stacks.get(key, [])
                if not stack:
                    continue
                try:
                    if is_foreground(stack[-1].target):
                        matched_key = key
                        break
                except BaseException:
                    continue
        if not matched_key:
            # Exact AX/UIA field identity is preferred, but macOS web editors
            # sometimes expose a fresh or incomplete accessibility element
            # after every paste. Keep the current field's actions visible
            # while its owning application is still frontmost. If focus moved
            # to another application with exactly one known stack, activate
            # that stack. Undo/mode conversion still revalidate the exact
            # field synchronously before changing any text.
            is_application_foreground = getattr(
                adapter, "is_application_foreground", None
            )
            if callable(is_application_foreground):
                active_stack = self._active_operation_stack()
                if active_stack:
                    try:
                        if is_application_foreground(active_stack[-1].target):
                            matched_key = self._active_operation_target_key
                    except BaseException:
                        pass
                if not matched_key:
                    application_keys: list[str] = []
                    for key, stack in self._operation_stacks.items():
                        if not stack:
                            continue
                        try:
                            if is_application_foreground(stack[-1].target):
                                application_keys.append(key)
                        except BaseException:
                            continue
                    if len(application_keys) == 1:
                        matched_key = application_keys[0]
        visible = bool(matched_key)
        if visible:
            self._applied_target_mismatch_count = 0
        elif self._applied_target_foreground:
            # Pasting into Electron/WebKit editors can briefly rebuild the AX
            # focus object (or report no focused object at all).  A single
            # 300 ms miss must not make the freshly shown action flash away.
            # Mutating actions still perform their own synchronous exact-field
            # check, so this visual grace period cannot undo another editor.
            self._applied_target_mismatch_count += 1
            if self._applied_target_mismatch_count < 3:
                return
        changed = visible != self._applied_target_foreground
        if matched_key and matched_key != self._active_operation_target_key:
            self._active_operation_target_key = matched_key
            changed = True
        if changed:
            self._applied_target_foreground = bool(visible)
            self.interactionChanged.emit()

    @Slot()
    def beginAppliedOverlayDrag(self) -> None:
        self._applied_overlay_drag_active = True
        self._applied_overlay_foreground_grace_until = float("inf")
        self._applied_target_mismatch_count = 0
        if not self._applied_target_foreground:
            self._applied_target_foreground = True
            self.interactionChanged.emit()

    @Slot()
    def endAppliedOverlayDrag(self) -> None:
        self._applied_overlay_drag_active = False
        # macOS can briefly report the overlay process/focused element while a
        # non-activating window finishes its native move. Ignore that transient
        # state so the result actions do not disappear under the target app.
        self._applied_overlay_foreground_grace_until = time.monotonic() + 1.0

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
        self._reset_battery_state()
        self._clear_undo_stack_for_device_boundary()
        self._close_floating_overlays_for_device_boundary()
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
        self._event_log(
            "RUNTIME_FINISHED",
            status="error" if error else "clean",
            error=error,
            had_connection=had_connection,
            quitting=self._quitting,
        )

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
        failed_edit_fallback = self._failed_edit_fallback_interaction()
        self._transcript_visible = False
        self._transcript_mode = ""
        if self._edit_review is None and self._interaction_state != "processing":
            self._set_interaction_state("idle")
        self.transcriptChanged.emit()
        if failed_edit_fallback is not None:
            # The user did not choose the raw dictation during the error
            # window. Retire only this interaction, then reopen recognition.
            self._finish_auto_interaction(INPUT_MODE_EDIT, retain=False)
            self._resume_recognition_after_interaction()

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
        line = f"[{timestamp}] {text}"
        self._log_lines.append(line)
        # Keep enough context in the live viewer for several full utterances;
        # the separate diagnostic file preserves the same lines across runs.
        del self._log_lines[:-1000]
        try:
            self._diagnostic_log.append(line)
        except BaseException:
            # Diagnostics are strictly observational and may not interrupt UI,
            # ASR, desktop injection, undo, or mode conversion.
            pass
        try:
            print(line, flush=True)
        except BaseException:
            # A closed diagnostic stream must never affect voice input.
            pass
        self.logChanged.emit()

    def _append_background_diagnostic(self, message: str) -> None:
        """Persist worker-thread failures without mutating Qt-facing UI state."""

        text = str(message).strip()
        if not text:
            return
        timestamp = datetime.now().astimezone().strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        try:
            self._diagnostic_log.append(f"[{timestamp}] {text}")
        except BaseException:
            pass

    @staticmethod
    def _diagnostic_field(value: object, *, limit: int = 240) -> str:
        normalized = " ".join(str(value).split())
        if len(normalized) > limit:
            normalized = normalized[: max(0, limit - 1)] + "…"
        return json.dumps(normalized, ensure_ascii=False)

    def _event_log(self, event: str, **fields: object) -> None:
        """Write one compact, searchable semantic event without changing state."""

        parts = [
            f"[EVENT {str(event).strip().upper() or 'UNKNOWN'}]",
            f"run={self._diagnostic_run_id}",
        ]
        for key, value in fields.items():
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                rendered = self._diagnostic_field(value)
            parts.append(f"{key}={rendered}")
        self._append_log(" ".join(parts))

    def _session_elapsed_ms(self, session_id: int) -> int | None:
        started_at = self._diagnostic_session_started_at.get(int(session_id))
        if started_at is None:
            return None
        return max(0, int(round((time.monotonic() - started_at) * 1000)))

    @staticmethod
    def _diagnostic_target_fields(
        target: DesktopTargetRef | None,
    ) -> dict[str, object]:
        if target is None:
            return {"target": "unavailable"}
        return {
            "application": target.process_name or target.window_title or "unknown",
            "process_id": int(target.process_id or 0),
            "target_key": AppController._operation_target_key(target),
        }

    def _pipeline_log(
        self,
        message: str,
        *,
        session_id: int = 0,
        route_id: int = 0,
        request_id: int = 0,
        mode: str = "",
    ) -> None:
        """Write one searchable line with stable utterance/request identity."""
        context = []
        if int(session_id) > 0:
            context.append(f"session={int(session_id)}")
        if int(route_id) > 0:
            context.append(f"route={int(route_id)}")
        if int(request_id) > 0:
            context.append(f"request={int(request_id)}")
        normalized_mode = str(mode).strip()
        if normalized_mode:
            context.append(f"mode={normalized_mode}")
        prefix = f"[{' '.join(context)}] " if context else ""
        self._append_log(prefix + str(message))

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
            # Keep RuntimeSettings/CLI reusable default at 1.0 s; the desktop
            # product intentionally retains a little more leading context.
            asr_pre_roll_s=1.2,
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
