"""Frozen application entry point with persistent startup diagnostics."""

from datetime import datetime, timezone
import faulthandler
import multiprocessing
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import traceback


_SELF_CHECK_OPTIONS = {"--self-check-opus", "--self-check-package"}
_BUNDLED_QML_FILES = (
    "QtQml/qmldir",
    "QtQuick/qmldir",
    "QtQuick/Controls/qmldir",
    "QtQuick/Controls/Material/qmldir",
    "QtQuick/Layouts/qmldir",
    "QtQuick/Window/qmldir",
)


def _self_check_requested() -> bool:
    return bool(_SELF_CHECK_OPTIONS.intersection(sys.argv[1:]))


def _verify_bundled_qml_runtime(resource_root: Path) -> None:
    candidates = (
        resource_root / "PySide6" / "Qt" / "qml",
        resource_root / "PySide6" / "qml",
    )
    qml_root = next((item for item in candidates if item.is_dir()), candidates[0])
    missing = [name for name in _BUNDLED_QML_FILES if not (qml_root / name).is_file()]
    if missing:
        raise RuntimeError(
            "Bundled PySide6 QML runtime is incomplete: " + ", ".join(missing)
        )


def _open_startup_log() -> tuple[Path, object]:
    """Open a useful log even if the normal application directory is broken."""

    try:
        from proximic_ring.runtime_paths import app_data_root

        log_path = app_data_root() / "logs" / "startup.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return log_path, log_path.open("a", encoding="utf-8", buffering=1)
    except BaseException:
        log_path = Path(tempfile.gettempdir()) / "ProximicVoice-startup.log"
        return log_path, log_path.open("a", encoding="utf-8", buffering=1)


def _show_fatal_startup_error(error: BaseException, log_path: Path) -> None:
    detail = str(error).strip() or type(error).__name__
    message = (
        f"Proximic Voice 无法启动：\n{detail}\n\n"
        f"诊断日志：{log_path}"
    )
    try:
        if sys.platform == "darwin":
            script = (
                "on run argv\n"
                "display alert (item 1 of argv) message (item 2 of argv) "
                "as critical buttons {\"好\"} default button \"好\"\n"
                "end run"
            )
            subprocess.run(
                ["/usr/bin/osascript", "-e", script, "Proximic Voice", message],
                check=False,
                timeout=20,
            )
        elif sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, message, "Proximic Voice 启动失败", 0x10
            )
    except BaseException:
        # The persistent traceback remains available if even the native dialog
        # service is unavailable during early process startup.
        pass


def run() -> int:
    log_path, log_file = _open_startup_log()
    sys.stdout = log_file
    sys.stderr = log_file
    try:
        faulthandler.enable(log_file, all_threads=True)
    except BaseException:
        pass

    print("\n=== Proximic Voice startup ===")
    print(f"time_utc={datetime.now(timezone.utc).isoformat()}")
    print(f"platform={platform.platform()} machine={platform.machine()}")
    print(f"python={sys.version.split()[0]} frozen={bool(getattr(sys, 'frozen', False))}")
    print(f"executable={sys.executable}")
    try:
        from proximic_ring.runtime_paths import (
            configure_runtime_environment,
            is_frozen,
            resource_root,
        )

        configure_runtime_environment()
        print("[startup] runtime environment ready")
        package_self_check = "--self-check-package" in sys.argv[1:]
        if "--self-check-opus" in sys.argv[1:] or package_self_check:
            from ring_python_sdk.audio.opus_codec import OrderedOpusDecoder

            OrderedOpusDecoder(eager=True)
            print("[startup] bundled Opus decoder ready")
            if not package_self_check:
                return 0
        if package_self_check:
            if is_frozen():
                _verify_bundled_qml_runtime(resource_root())
            if sys.platform == "darwin":
                from proximic_ring.desktop_output import MacOSUnicodeTextInjector

                macos_injector = MacOSUnicodeTextInjector()
                print(
                    "[startup] macOS desktop injection bridge ready; "
                    f"accessibility_trusted={macos_injector.is_trusted(prompt=False)}"
                )
            os.environ["PROXIMIC_STARTUP_PROBE"] = "1"
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
            print("[startup] bundled QML files ready")
        from proximic_ring.ui.main import main

        print("[startup] UI modules imported")
        exit_code = int((main([sys.argv[0]]) if package_self_check else main()) or 0)
        print(f"[startup] Qt event loop exited with code {exit_code}")
        return exit_code
    except KeyboardInterrupt:
        print("[startup] interrupted")
        return 130
    except BaseException as exc:
        print("[startup] fatal error")
        traceback.print_exc(file=log_file)
        if not _self_check_requested():
            _show_fatal_startup_error(exc, log_path)
        return 1
    finally:
        try:
            log_file.flush()
        except BaseException:
            pass


def _entrypoint() -> int:
    multiprocessing.freeze_support()
    return run()


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
