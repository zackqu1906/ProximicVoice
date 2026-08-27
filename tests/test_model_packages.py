import hashlib
import json
from pathlib import Path
import zipfile

from proximic_ring import model_packages
from proximic_ring.text_processing import local_server


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_platform_specific_runtime_selection(monkeypatch, tmp_path):
    catalog = {
        "schemaVersion": 1,
        "defaultRuntime": "win",
        "defaultRuntimes": {"windows-x86_64": "win", "macos-arm64": "mac"},
        "defaultModel": "model",
        "runtimes": {
            "win": {"executable": "llama-server.exe"},
            "mac": {"executable": "llama-server"},
        },
        "models": {
            "model": {
                "filename": "model.gguf",
                "apiModel": "local",
            }
        },
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setenv("PROXIMIC_LLM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(local_server, "LOCAL_LLM_CATALOG_PATH", path)
    monkeypatch.setattr(local_server, "local_llm_platform_key", lambda: "macos-arm64")

    resolved = local_server.resolve_default_local_llm()

    assert resolved["runtime_id"] == "mac"
    assert str(resolved["server_path"]).endswith("runtimes\\mac\\llama-server") or str(
        resolved["server_path"]
    ).endswith("runtimes/mac/llama-server")


def test_install_default_local_model_verifies_and_flattens_runtime(
    monkeypatch, tmp_path
):
    model_bytes = b"fake-gguf"
    runtime_archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(runtime_archive, "w") as package:
        package.writestr("llama-b1/bin/llama-server.exe", b"server")
        package.writestr("llama-b1/bin/backend.dll", b"backend")
    archive_bytes = runtime_archive.read_bytes()
    catalog = {
        "schemaVersion": 1,
        "defaultRuntime": "runtime",
        "defaultRuntimes": {"windows-x86_64": "runtime"},
        "defaultModel": "model",
        "runtimes": {
            "runtime": {
                "archiveFilename": "runtime.zip",
                "downloadUrl": "runtime",
                "sha256": _hash(archive_bytes),
                "executable": "llama-server.exe",
            }
        },
        "models": {
            "model": {
                "filename": "model.gguf",
                "downloadUrl": "model",
                "sha256": _hash(model_bytes),
                "apiModel": "local",
                "contextSize": 4096,
                "reasoning": "off",
            }
        },
    }
    catalog_path = tmp_path / "catalog-source.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(model_packages, "local_llm_platform_key", lambda: "windows-x86_64")

    def fake_download(_urls, destination, _sha, label, _progress):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            archive_bytes if "运行时" in label else model_bytes
        )

    monkeypatch.setattr(model_packages, "_download", fake_download)
    home = tmp_path / "installed"

    result = model_packages.install_default_local_model(
        home=home, catalog_path=catalog_path
    )

    assert Path(result["server_path"]).read_bytes() == b"server"
    assert (Path(result["server_path"]).parent / "backend.dll").is_file()
    assert Path(result["model_path"]).read_bytes() == model_bytes
    assert json.loads((home / "installation.json").read_text())["runtimeId"] == "runtime"
