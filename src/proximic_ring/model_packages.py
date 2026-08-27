"""Verified, resumable installation of the optional local text model."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
from typing import Callable
from urllib import request
import zipfile

from .text_processing.local_server import (
    default_local_llm_home,
    load_local_llm_catalog,
    local_llm_platform_key,
)


ProgressCallback = Callable[[str, int, int], None]


def local_model_is_installed() -> bool:
    resolved = resolve_default_local_llm()
    return Path(str(resolved["server_path"])).is_file() and Path(
        str(resolved["model_path"])
    ).is_file()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(
    urls: list[str], destination: Path, expected_sha256: str, label: str,
    progress: ProgressCallback | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    for url in urls:
        try:
            downloaded = partial.stat().st_size if partial.exists() else 0
            headers = {"User-Agent": "ProxiMic-Voice/0.6"}
            if downloaded:
                headers["Range"] = f"bytes={downloaded}-"
            with request.urlopen(request.Request(url, headers=headers), timeout=60) as response:
                if downloaded and getattr(response, "status", 200) != 206:
                    downloaded = 0
                    partial.unlink(missing_ok=True)
                total_header = int(response.headers.get("Content-Length", "0") or 0)
                total = downloaded + total_header
                mode = "ab" if downloaded else "wb"
                with partial.open(mode) as target:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            progress(label, downloaded, total)
            actual = _sha256(partial)
            if actual.lower() != expected_sha256.lower():
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"{label} 校验失败：{actual}")
            os.replace(partial, destination)
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"{label} 下载失败：{last_error}") from last_error


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            members = package.infolist()
            for member in members:
                if not (root / member.filename).resolve().is_relative_to(root):
                    raise RuntimeError("运行时压缩包包含不安全路径")
            package.extractall(destination)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as package:
            members = package.getmembers()
            for member in members:
                target = (root / member.name).resolve()
                if not target.is_relative_to(root) or member.issym() or member.islnk():
                    raise RuntimeError("运行时压缩包包含不安全路径或链接")
            package.extractall(destination, members=members, filter="data")
        return
    raise RuntimeError(f"不支持的运行时压缩包：{archive.name}")


def install_default_local_model(
    *, home: str | Path | None = None, catalog_path: str | Path | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, str | int]:
    """Install the host runtime and shared GGUF, then atomically select them."""

    target_home = Path(home) if home is not None else default_local_llm_home()
    catalog = load_local_llm_catalog(catalog_path)
    platform_key = local_llm_platform_key()
    try:
        runtime_id = str(catalog["defaultRuntimes"][platform_key])
    except KeyError as exc:
        raise RuntimeError(f"暂不支持此平台的本地模型运行时：{platform_key}") from exc
    model_id = str(catalog["defaultModel"])
    runtime = catalog["runtimes"][runtime_id]
    model = catalog["models"][model_id]
    downloads = target_home / "downloads"
    runtime_archive = downloads / runtime["archiveFilename"]
    model_path = target_home / "models" / model_id / model["filename"]
    runtime_path = target_home / "runtimes" / runtime_id

    if not model_path.is_file() or _sha256(model_path) != model["sha256"]:
        _download(
            list(model.get("downloadUrls") or [model["downloadUrl"]]),
            model_path, model["sha256"], "GGUF 模型", progress,
        )
    executable = runtime_path / runtime["executable"]
    if not executable.is_file():
        if not runtime_archive.is_file() or _sha256(runtime_archive) != runtime["sha256"]:
            _download(
                list(runtime.get("downloadUrls") or [runtime["downloadUrl"]]),
                runtime_archive, runtime["sha256"], "llama.cpp 运行时", progress,
            )
        target_home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="runtime-", dir=target_home) as tmp:
            extracted = Path(tmp)
            _safe_extract(runtime_archive, extracted)
            candidates = list(extracted.rglob(runtime["executable"]))
            if not candidates:
                raise RuntimeError("运行时压缩包中没有 llama-server")
            source_dir = candidates[0].parent
            staging = runtime_path.with_name(runtime_path.name + ".installing")
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(source_dir, staging)
            if runtime_path.exists():
                shutil.rmtree(runtime_path)
            os.replace(staging, runtime_path)
        if os.name != "nt":
            executable.chmod(executable.stat().st_mode | 0o111)

    target_home.mkdir(parents=True, exist_ok=True)
    catalog_target = target_home / "catalog.json"
    catalog_target.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    selection = {
        "schemaVersion": 1,
        "runtimeId": runtime_id,
        "modelId": model_id,
        "modelPath": str(model_path),
    }
    temp_selection = target_home / "installation.json.tmp"
    temp_selection.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp_selection, target_home / "installation.json")
    return {
        "home": str(target_home),
        "runtime_id": runtime_id,
        "model_id": model_id,
        "server_path": str(executable),
        "model_path": str(model_path),
        "api_model": str(model["apiModel"]),
        "context_size": int(model.get("contextSize", 8192)),
        "reasoning": str(model.get("reasoning", "off")),
    }
