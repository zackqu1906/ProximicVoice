from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit, urlunsplit


DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:11435/v1"
LOCAL_LLM_HOME_ENV = "PROXIMIC_LLM_HOME"
LOCAL_LLM_CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "local_llm_catalog.json"
)


def load_local_llm_catalog(path: str | Path | None = None) -> dict:
    catalog_path = Path(path) if path is not None else LOCAL_LLM_CATALOG_PATH
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取本地模型包清单：{catalog_path}") from exc
    if not isinstance(catalog, dict) or catalog.get("schemaVersion") != 1:
        raise RuntimeError(f"不支持的本地模型包清单：{catalog_path}")
    return catalog


def default_local_llm_home() -> Path:
    override = os.environ.get(LOCAL_LLM_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    project_root = Path(__file__).resolve().parents[3]
    return project_root / ".runtime" / "local-llm"


def resolve_default_local_llm() -> dict[str, str | int]:
    home = default_local_llm_home()
    installed_catalog_path = home / "catalog.json"
    catalog = load_local_llm_catalog(
        installed_catalog_path if installed_catalog_path.is_file() else None
    )
    runtime_id = str(catalog["defaultRuntime"])
    model_id = str(catalog["defaultModel"])
    model_path_override = ""
    selection_path = home / "installation.json"
    if selection_path.is_file():
        try:
            selection = json.loads(selection_path.read_text(encoding="utf-8-sig"))
            if selection.get("schemaVersion") == 1:
                runtime_id = str(selection.get("runtimeId") or runtime_id)
                model_id = str(selection.get("modelId") or model_id)
                model_path_override = str(selection.get("modelPath") or "").strip()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    if runtime_id not in catalog.get("runtimes", {}):
        raise RuntimeError(f"本地模型安装记录引用了未知运行时：{runtime_id}")
    if model_id not in catalog.get("models", {}):
        raise RuntimeError(f"本地模型安装记录引用了未知模型：{model_id}")
    runtime = catalog["runtimes"][runtime_id]
    model = catalog["models"][model_id]
    return {
        "home": str(home),
        "runtime_id": runtime_id,
        "model_id": model_id,
        "server_path": str(home / "runtimes" / runtime_id / runtime["executable"]),
        "model_path": model_path_override
        or str(home / "models" / model_id / model["filename"]),
        "api_model": str(model["apiModel"]),
        "context_size": int(model.get("contextSize", 8192)),
        "reasoning": str(model.get("reasoning", "off")),
    }


_DEFAULT_LOCAL_LLM = resolve_default_local_llm()
DEFAULT_LOCAL_MODEL = str(_DEFAULT_LOCAL_LLM["api_model"])
DEFAULT_LOCAL_SERVER_PATH = str(_DEFAULT_LOCAL_LLM["server_path"])
DEFAULT_LOCAL_MODEL_PATH = str(_DEFAULT_LOCAL_LLM["model_path"])
DEFAULT_LOCAL_CONTEXT_SIZE = int(_DEFAULT_LOCAL_LLM["context_size"])
DEFAULT_LOCAL_REASONING = str(_DEFAULT_LOCAL_LLM["reasoning"])


class LocalModelServer:
    """Start and reuse a local llama.cpp OpenAI-compatible model service."""

    def __init__(
        self,
        *,
        base_url: str,
        model_alias: str,
        server_path: str | Path,
        model_path: str | Path,
        context_size: int = 8192,
        reasoning: str = "off",
    ) -> None:
        self.base_url = str(base_url)
        self.model_alias = str(model_alias)
        self.server_path = Path(server_path).expanduser()
        self.model_path = Path(model_path).expanduser()
        self.context_size = max(512, int(context_size))
        self.reasoning = str(reasoning).strip().lower()
        self._process: subprocess.Popen[bytes] | None = None
        self._log = None

    def ensure_running(self) -> bool:
        """Ensure the endpoint is healthy; return True when this call started it."""

        if self._is_healthy():
            return False

        target = urlsplit(self.base_url)
        host = (target.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(f"本地模型地址不是本机地址：{self.base_url}")
        if not self.server_path.is_file():
            raise RuntimeError(f"找不到本地模型服务：{self.server_path}")
        if not self.model_path.is_file():
            raise RuntimeError(f"找不到本地 GGUF 模型：{self.model_path}")

        port = target.port or 80
        self._log = tempfile.TemporaryFile(mode="w+b")
        command = [
            str(self.server_path),
            "--model",
            str(self.model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--alias",
            self.model_alias,
            "--ctx-size",
            str(self.context_size),
            "--jinja",
            "--cache-prompt",
            "--cache-idle-slots",
            "--parallel",
            "2",
        ]
        if self.reasoning:
            command.extend(["--reasoning", self.reasoning])
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._process = subprocess.Popen(
            command,
            cwd=str(self.server_path.parent),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("本地模型服务启动失败：" + self._read_log_tail())
            if self._is_healthy():
                return True
            time.sleep(0.2)
        raise RuntimeError("本地模型加载超过 30 秒，请检查模型路径或内存")

    def stop_started_process(self) -> None:
        """Stop only a process started by this instance, primarily after startup failure."""

        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2.0)
        if self._log is not None:
            self._log.close()
            self._log = None

    def _is_healthy(self) -> bool:
        target = urlsplit(self.base_url)
        health_url = urlunsplit((target.scheme, target.netloc, "/health", "", ""))
        try:
            with urllib_request.urlopen(health_url, timeout=1.0) as response:
                return response.status == 200
        except (OSError, TimeoutError, urllib_error.URLError):
            return False

    def _read_log_tail(self) -> str:
        if self._log is None:
            return "没有日志"
        self._log.flush()
        self._log.seek(0)
        text = self._log.read().decode("utf-8", errors="replace").strip()
        return text[-1000:] or "没有日志"
