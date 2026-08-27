"""Frozen application entry point with a persistent startup log."""

from pathlib import Path
import sys
import traceback

from proximic_ring.runtime_paths import app_data_root, configure_runtime_environment

configure_runtime_environment()

log_dir = app_data_root() / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = (log_dir / "startup.log").open("a", encoding="utf-8", buffering=1)
sys.stdout = log_file
sys.stderr = log_file

from proximic_ring.ui.main import main  # noqa: E402


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException:
        traceback.print_exc(file=log_file)
        raise
