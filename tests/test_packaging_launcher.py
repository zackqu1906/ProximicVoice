from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
import types

import pytest


def _load_launcher():
    launcher_path = Path(__file__).parents[1] / "packaging" / "launcher.py"
    spec = importlib.util.spec_from_file_location(
        "proximic_packaging_launcher", launcher_path
    )
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    return launcher


def test_entrypoint_diverts_multiprocessing_before_starting_ui(monkeypatch) -> None:
    launcher = _load_launcher()

    calls: list[str] = []
    monkeypatch.setattr(
        launcher.multiprocessing,
        "freeze_support",
        lambda: calls.append("freeze_support"),
    )
    monkeypatch.setattr(launcher, "run", lambda: calls.append("run") or 0)

    assert launcher._entrypoint() == 0
    assert calls == ["freeze_support", "run"]


def test_packaged_qml_runtime_check_requires_every_imported_module(tmp_path) -> None:
    launcher = _load_launcher()
    qml_root = tmp_path / "PySide6" / "qml"
    for relative_path in launcher._BUNDLED_QML_FILES:
        target = qml_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()

    launcher._verify_bundled_qml_runtime(tmp_path)

    (qml_root / "QtQuick" / "Controls" / "Material" / "qmldir").unlink()
    with pytest.raises(RuntimeError, match="Controls/Material/qmldir"):
        launcher._verify_bundled_qml_runtime(tmp_path)


def test_package_self_check_configures_headless_ui(monkeypatch, tmp_path) -> None:
    launcher = _load_launcher()
    output = io.StringIO()
    log_path = tmp_path / "startup.log"
    monkeypatch.setattr(launcher, "_open_startup_log", lambda: (log_path, output))
    monkeypatch.setattr(
        launcher.sys,
        "argv",
        ["ProximicVoice", "--self-check-package"],
    )

    runtime_paths = types.ModuleType("proximic_ring.runtime_paths")
    runtime_paths.configure_runtime_environment = lambda: None
    runtime_paths.is_frozen = lambda: False
    runtime_paths.resource_root = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "proximic_ring.runtime_paths", runtime_paths)

    opus_codec = types.ModuleType("ring_python_sdk.audio.opus_codec")
    opus_codec.OrderedOpusDecoder = lambda **_kwargs: object()
    monkeypatch.setitem(sys.modules, "ring_python_sdk.audio.opus_codec", opus_codec)

    ui_main = types.ModuleType("proximic_ring.ui.main")

    def fake_main(argv):
        assert argv == ["ProximicVoice"]
        assert launcher.os.environ["PROXIMIC_STARTUP_PROBE"] == "1"
        assert launcher.os.environ["QT_QPA_PLATFORM"] == "offscreen"
        return 0

    ui_main.main = fake_main
    monkeypatch.setitem(sys.modules, "proximic_ring.ui.main", ui_main)

    original_stdout, original_stderr = sys.stdout, sys.stderr
    try:
        assert launcher.run() == 0
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    assert "bundled Opus decoder ready" in output.getvalue()
    assert "bundled QML files ready" in output.getvalue()
