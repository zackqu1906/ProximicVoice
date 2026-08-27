"""Frozen application entry point with persistent startup diagnostics."""

from datetime import datetime, timezone
import faulthandler
import multiprocessing
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import traceback


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
        from proximic_ring.runtime_paths import configure_runtime_environment

        configure_runtime_environment()
        print("[startup] runtime environment ready")
        from proximic_ring.ui.main import main

        print("[startup] UI modules imported")
        exit_code = int(main() or 0)
        print(f"[startup] Qt event loop exited with code {exit_code}")
        return exit_code
    except KeyboardInterrupt:
        print("[startup] interrupted")
        return 130
    except BaseException as exc:
        print("[startup] fatal error")
        traceback.print_exc(file=log_file)
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
