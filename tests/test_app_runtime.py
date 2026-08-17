import os
from pathlib import Path

from proximic_ring.app_runtime import RuntimeSettings, SilentTranscriptOverlay


def test_runtime_defaults_only_enable_windows_desktop_features():
    args = RuntimeSettings().to_namespace()

    expected = os.name == "nt"
    assert args.desktop_output is expected
    assert args.push_to_talk is expected


def test_runtime_settings_preserve_working_detector_and_asr_values():
    settings = RuntimeSettings(
        detector_model=Path("runs/model.model"),
        stage1_threshold=0.005,
        asr_backend="streaming_sensevoice",
        asr_model="iic/SenseVoiceSmall",
        streaming_sensevoice_repo=Path("../streaming-sensevoice"),
        desktop_output=True,
        push_to_talk=True,
    )
    args = settings.to_namespace()

    assert args.stage1_threshold == 0.005
    assert args.asr == ["streaming_sensevoice"]
    assert args.asr_model == ["streaming_sensevoice=iic/SenseVoiceSmall"]
    assert args.streaming_sensevoice_repo == Path("../streaming-sensevoice")
    assert args.desktop_output_backend == "streaming_sensevoice"
    assert args.push_to_talk is True


def test_qml_runtime_overlay_adapter_is_deliberately_silent():
    overlay = SilentTranscriptOverlay()
    overlay.show_partial("partial")
    overlay.show_final("final")
    overlay.show_error("error")
    overlay.close()


def test_non_local_backend_does_not_receive_streaming_sensevoice_repo():
    args = RuntimeSettings(
        asr_backend="volcengine",
        asr_model="seedasr-streaming",
        streaming_sensevoice_repo=Path("../streaming-sensevoice"),
    ).to_namespace()

    assert args.streaming_sensevoice_repo is None


def test_funasr_backend_receives_its_repo_and_can_auto_select_local_model():
    repo = Path("../Fun-ASR-main")
    args = RuntimeSettings(
        asr_backend="funasr_nano",
        asr_model="",
        streaming_sensevoice_repo=Path("../streaming-sensevoice"),
        funasr_nano_repo=repo,
    ).to_namespace()

    assert args.asr == ["funasr_nano"]
    assert args.asr_model is None
    assert args.funasr_nano_repo == repo
    assert args.streaming_sensevoice_repo is None
