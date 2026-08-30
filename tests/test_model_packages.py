import hashlib
import json
from pathlib import Path
import zipfile

from proximic_ring import model_packages
from proximic_ring.text_processing import local_server


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _FakeResponse:
    def __init__(self, chunks, *, status=200, headers=None):
        self._chunks = iter(chunks)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        item = next(self._chunks, b"")
        if isinstance(item, BaseException):
            raise item
        return item


def test_download_retries_and_resumes_after_socket_timeout(monkeypatch, tmp_path):
    payload = b"abcdefgh"
    requests = []
    responses = iter(
        [
            _FakeResponse(
                [payload[:4], TimeoutError("WinError 10060")],
                headers={"Content-Length": str(len(payload))},
            ),
            _FakeResponse(
                [payload[4:], b""],
                status=206,
                headers={
                    "Content-Length": str(len(payload) - 4),
                    "Content-Range": "bytes 4-7/8",
                },
            ),
        ]
    )

    def fake_urlopen(req, timeout):
        requests.append((req, timeout))
        return next(responses)

    monkeypatch.setattr(model_packages.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(model_packages.time, "sleep", lambda _seconds: None)
    progress = []
    destination = tmp_path / "model.gguf"

    model_packages._download(
        ["https://example.invalid/model.gguf"],
        destination,
        _hash(payload),
        "GGUF 模型",
        lambda *args: progress.append(args),
        expected_size=len(payload),
    )

    assert destination.read_bytes() == payload
    assert requests[0][0].get_header("Range") is None
    assert requests[1][0].get_header("Range") == "bytes=4-"
    assert any("自动续传" in row[0] for row in progress)


def test_download_promotes_complete_verified_part_without_network(
    monkeypatch, tmp_path
):
    payload = b"already complete"
    destination = tmp_path / "model.gguf"
    partial = destination.with_suffix(".gguf.part")
    partial.write_bytes(payload)
    monkeypatch.setattr(
        model_packages.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete part must not contact the network")
        ),
    )

    model_packages._download(
        ["https://example.invalid/model.gguf"],
        destination,
        _hash(payload),
        "GGUF 模型",
        None,
        expected_size=len(payload),
    )

    assert destination.read_bytes() == payload
    assert not partial.exists()


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

    def fake_download(
        _urls, destination, _sha, label, _progress, *, expected_size=None
    ):
        assert expected_size is None
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
