import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from proximic_ring.text_processing import (
    load_local_llm_catalog,
    resolve_default_local_llm,
)


def test_default_local_llm_catalog_is_pinned_and_relocatable(tmp_path, monkeypatch):
    catalog = load_local_llm_catalog()
    runtime = catalog["runtimes"][catalog["defaultRuntime"]]
    model = catalog["models"][catalog["defaultModel"]]

    assert runtime["downloadUrl"].startswith(
        "https://github.com/ggml-org/llama.cpp/releases/"
    )
    assert catalog["defaultModel"] == "qwen3-4b-instruct-2507-q4-k-m"
    assert model["source"] == "bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF"
    assert "Qwen3-4B-Instruct-2507" in model["filename"]
    assert model["apiModel"] == "qwen3-4b-instruct-2507-local"
    assert model["reasoning"] == "off"
    assert model["downloadUrl"].startswith("https://huggingface.co/bartowski/")
    assert len(runtime["sha256"]) == 64
    assert len(model["sha256"]) == 64
    assert model["sizeBytes"] > 2_000_000_000

    custom_home = tmp_path / "models-on-another-drive"
    monkeypatch.setenv("PROXIMIC_LLM_HOME", str(custom_home))
    resolved = resolve_default_local_llm()
    assert Path(resolved["server_path"]).is_relative_to(custom_home)
    assert Path(resolved["model_path"]).is_relative_to(custom_home)
    assert resolved["api_model"] == model["apiModel"]
    assert resolved["context_size"] == model["contextSize"]


@pytest.mark.skipif(os.name != "nt", reason="Windows installer smoke test")
def test_local_llm_installer_uses_catalog_without_hardcoded_model(
    tmp_path, monkeypatch
):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")

    source = tmp_path / "source"
    source.mkdir()
    runtime_archive = source / "runtime.zip"
    with zipfile.ZipFile(runtime_archive, "w") as archive:
        archive.writestr("llama-server.exe", b"fake-server")
        archive.writestr("ggml.dll", b"fake-dll")
    model_file = source / "alternate-model.gguf"
    model_file.write_bytes(b"fake-gguf-model")

    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    catalog = {
        "schemaVersion": 1,
        "defaultRuntime": "fake-runtime",
        "defaultModel": "alternate-model",
        "runtimes": {
            "fake-runtime": {
                "displayName": "Fake runtime",
                "archiveFilename": runtime_archive.name,
                "downloadUrl": runtime_archive.as_uri(),
                "sha256": sha256(runtime_archive),
                "executable": "llama-server.exe",
            }
        },
        "models": {
            "alternate-model": {
                "displayName": "Alternate model",
                "apiModel": "alternate-local",
                "filename": model_file.name,
                "downloadUrl": model_file.as_uri(),
                "sha256": sha256(model_file),
            }
        },
    }
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    install_root = tmp_path / "installed"
    download_root = tmp_path / "downloads"
    script = Path(__file__).parents[1] / "scripts" / "install-local-llm.ps1"
    command = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-CatalogPath",
        str(catalog_path),
        "-InstallRoot",
        str(install_root),
        "-DownloadRoot",
        str(download_root),
    ]

    completed = subprocess.run(command, capture_output=True, text=True, timeout=30)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (
        install_root / "runtimes" / "fake-runtime" / "llama-server.exe"
    ).read_bytes() == b"fake-server"
    assert (
        install_root / "models" / "alternate-model" / "alternate-model.gguf"
    ).read_bytes() == b"fake-gguf-model"
    assert (install_root / "catalog.json").is_file()
    installation = json.loads(
        (install_root / "installation.json").read_text(encoding="utf-8")
    )
    assert installation["runtimeId"] == "fake-runtime"
    assert installation["modelId"] == "alternate-model"
    assert Path(installation["modelPath"]).is_file()
    monkeypatch.setenv("PROXIMIC_LLM_HOME", str(install_root))
    resolved = resolve_default_local_llm()
    assert resolved["runtime_id"] == "fake-runtime"
    assert resolved["model_id"] == "alternate-model"
    assert resolved["api_model"] == "alternate-local"
