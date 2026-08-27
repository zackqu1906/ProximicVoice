"""Resource and writable paths shared by source and packaged applications."""

from __future__ import annotations

import os
from pathlib import Path
import sys


APP_DIR_NAME = "ProxiMic Voice"
DATA_HOME_ENV = "PROXIMIC_DATA_HOME"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"))


def resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled).resolve()
    return Path(__file__).resolve().parents[2]


def app_data_root() -> Path:
    override = os.environ.get(DATA_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if not is_frozen():
        return resource_root()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return (base / APP_DIR_NAME).resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library/Application Support" / APP_DIR_NAME).resolve()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return (base / APP_DIR_NAME).resolve()


def cache_root() -> Path:
    if not is_frozen() and not os.environ.get(DATA_HOME_ENV, "").strip():
        return resource_root() / ".cache"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches" / APP_DIR_NAME
    return app_data_root() / "cache"


def configure_runtime_environment() -> None:
    """Point every mutable cache at a per-user directory before model imports."""

    data_home = app_data_root()
    cache_home = cache_root()
    for path in (data_home, cache_home):
        path.mkdir(parents=True, exist_ok=True)
    defaults = {
        "PROXIMIC_LLM_HOME": data_home / "local-llm",
        "MODELSCOPE_CACHE": cache_home / "modelscope",
        "HF_HOME": cache_home / "huggingface",
        "TORCH_HOME": cache_home / "torch",
    }
    for name, path in defaults.items():
        os.environ.setdefault(name, str(path))
        Path(os.environ[name]).expanduser().mkdir(parents=True, exist_ok=True)

    opus_dir = resource_root() / "opus"
    if opus_dir.is_dir():
        os.environ.setdefault("PROXIMIC_OPUS_DIR", str(opus_dir))
