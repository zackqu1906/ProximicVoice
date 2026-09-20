from pathlib import Path
import ctypes.util
import sys
import types

import pytest
from ring_python_sdk.audio import opus_codec as codec


@pytest.fixture
def bundled(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'Frameworks'), raising=False)
    path = tmp_path / 'Frameworks/opus/libopus.0.dylib'
    path.parent.mkdir(parents=True)
    path.touch()
    return path


def test_frozen_library_ignores_stale_external_environment(bundled, monkeypatch, tmp_path):
    other = tmp_path / 'homebrew'
    other.mkdir()
    (other / 'libopus.0.dylib').touch()
    monkeypatch.setenv('PROXIMIC_OPUS_DIR', str(other))
    assert codec._bundled_macos_opus() == bundled


def test_frozen_discovers_library_without_startup_environment(bundled, monkeypatch):
    monkeypatch.delenv('PROXIMIC_OPUS_DIR', raising=False)
    assert codec._bundled_macos_opus() == bundled


def test_missing_bundled_library_cannot_fall_back_to_installed_opus(bundled, monkeypatch):
    bundled.unlink()
    monkeypatch.setattr(ctypes.util, 'find_library', lambda _: '/opt/homebrew/lib/libopus.dylib')
    with pytest.raises(codec.OpusUnavailableError, match='无需另装 Opus'):
        codec._load_opuslib()


def test_external_symlink_not_accepted_as_bundled(bundled, tmp_path):
    external = tmp_path / 'external.dylib'
    external.touch()
    bundled.unlink()
    bundled.symlink_to(external)
    with pytest.raises(codec.OpusUnavailableError):
        codec._bundled_macos_opus()


def test_cached_external_library_is_rejected_and_resolver_restored(bundled, monkeypatch):
    original = ctypes.util.find_library
    fake = types.SimpleNamespace(api=types.SimpleNamespace(libopus=types.SimpleNamespace(_name='/opt/homebrew/lib/libopus.dylib')))
    monkeypatch.setitem(sys.modules, 'opuslib', fake)
    with pytest.raises(codec.OpusUnavailableError, match='实际加载路径'):
        codec._load_opuslib()
    assert ctypes.util.find_library is original


def test_source_library_discovery_needs_no_gui_init(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.delattr(sys, '_MEIPASS', raising=False)
    monkeypatch.delenv('PROXIMIC_OPUS_DIR', raising=False)
    monkeypatch.setattr(codec, '__file__', str(tmp_path / 'src/ring_python_sdk/audio/opus_codec.py'))
    path = tmp_path / '.runtime/opus/lib/libopus.0.dylib'
    path.parent.mkdir(parents=True)
    path.touch()
    assert codec._bundled_macos_opus() == path
