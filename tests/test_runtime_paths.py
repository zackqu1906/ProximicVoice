from pathlib import Path

from proximic_ring import runtime_paths


def test_data_home_override_is_shared_by_packaged_runtime(monkeypatch, tmp_path):
    target = tmp_path / "userdata"
    monkeypatch.setenv(runtime_paths.DATA_HOME_ENV, str(target))

    runtime_paths.configure_runtime_environment()

    assert runtime_paths.app_data_root() == target.resolve()
    assert Path(runtime_paths.os.environ["PROXIMIC_LLM_HOME"]).is_dir()
    assert Path(runtime_paths.os.environ["MODELSCOPE_CACHE"]).is_dir()


def test_frozen_macos_uses_application_support(monkeypatch, tmp_path):
    monkeypatch.delenv(runtime_paths.DATA_HOME_ENV, raising=False)
    monkeypatch.setattr(runtime_paths, "is_frozen", lambda: True)
    monkeypatch.setattr(runtime_paths.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert runtime_paths.app_data_root() == (
        tmp_path / "Library/Application Support/ProxiMic Voice"
    ).resolve()
