"""Verified, resumable installation of the optional local text model."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import time
from typing import Callable
from urllib import request
import zipfile

from .text_processing.local_server import (
    default_local_llm_home,
    load_local_llm_catalog,
    local_llm_platform_key,
)


ProgressCallback = Callable[[str, int, int], None]

_DOWNLOAD_TIMEOUT_S = 60
_DOWNLOAD_ROUNDS = 4
_DOWNLOAD_RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 10.0)
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


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
    progress: ProgressCallback | None, *, expected_size: int | None = None,
) -> None:
    """Download with checksum verification and automatic cross-mirror resume.

    A multi-gigabyte Hugging Face response can legitimately be interrupted by
    a proxy, VPN, CDN edge, or transient socket timeout.  Keep one ``.part``
    file for every mirror and retry it with a validated HTTP Range request.
    """

    if not urls:
        raise RuntimeError(f"{label} 没有可用的下载地址")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last_error: Exception | None = None
    attempts = urls * _DOWNLOAD_ROUNDS
    for attempt_index, url in enumerate(attempts, start=1):
        try:
            downloaded = partial.stat().st_size if partial.exists() else 0

            if expected_size is not None and downloaded > expected_size:
                partial.unlink(missing_ok=True)
                downloaded = 0
            if expected_size is not None and downloaded == expected_size:
                actual = _sha256(partial)
                if actual.lower() == expected_sha256.lower():
                    os.replace(partial, destination)
                    if progress:
                        progress(label, expected_size, expected_size)
                    return
                partial.unlink(missing_ok=True)
                downloaded = 0

            headers = {"User-Agent": "ProxiMic-Voice/0.6"}
            if downloaded:
                headers["Range"] = f"bytes={downloaded}-"
            with request.urlopen(
                request.Request(url, headers=headers), timeout=_DOWNLOAD_TIMEOUT_S
            ) as response:
                status = int(getattr(response, "status", 200) or 200)
                if downloaded and status == 206:
                    content_range = str(response.headers.get("Content-Range", ""))
                    match = _CONTENT_RANGE.fullmatch(content_range.strip())
                    if match is None or int(match.group(1)) != downloaded:
                        raise RuntimeError(
                            f"服务器返回了无效的断点范围：{content_range or '缺失'}"
                        )
                elif downloaded and status != 206:
                    # Some CDNs ignore Range and return a complete 200 body.
                    # The already-open response can safely replace the part.
                    downloaded = 0
                    partial.unlink(missing_ok=True)
                total_header = int(response.headers.get("Content-Length", "0") or 0)
                total = expected_size or (downloaded + total_header)
                mode = "ab" if downloaded else "wb"
                with partial.open(mode) as target:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                        downloaded += len(chunk)
                        if expected_size is not None and downloaded > expected_size:
                            raise RuntimeError(
                                f"服务器返回的数据超过预期大小 {expected_size} 字节"
                            )
                        if progress:
                            progress(label, downloaded, total)
            if expected_size is not None and downloaded != expected_size:
                raise RuntimeError(
                    f"连接提前结束：已下载 {downloaded} / {expected_size} 字节"
                )
            actual = _sha256(partial)
            if actual.lower() != expected_sha256.lower():
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"{label} 校验失败：{actual}")
            os.replace(partial, destination)
            return
        except Exception as exc:
            last_error = exc
            if attempt_index < len(attempts):
                downloaded = partial.stat().st_size if partial.exists() else 0
                if progress:
                    progress(
                        f"{label}连接中断，正在自动续传 "
                        f"({attempt_index + 1}/{len(attempts)})",
                        downloaded,
                        expected_size or 0,
                    )
                delay_index = min(
                    attempt_index - 1, len(_DOWNLOAD_RETRY_DELAYS_S) - 1
                )
                time.sleep(_DOWNLOAD_RETRY_DELAYS_S[delay_index])
    partial_note = f"；断点已保留在 {partial}" if partial.exists() else ""
    raise RuntimeError(
        f"{label} 下载失败：{last_error}{partial_note}。"
        "请检查代理/VPN、防火墙，或稍后重试。"
    ) from last_error


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
            expected_size=int(model.get("sizeBytes") or 0) or None,
        )
    executable = runtime_path / runtime["executable"]
    if not executable.is_file():
        if not runtime_archive.is_file() or _sha256(runtime_archive) != runtime["sha256"]:
            _download(
                list(runtime.get("downloadUrls") or [runtime["downloadUrl"]]),
                runtime_archive, runtime["sha256"], "llama.cpp 运行时", progress,
                expected_size=int(runtime.get("sizeBytes") or 0) or None,
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
