import json
from pathlib import Path
import plistlib
import subprocess

import pytest

from proximic_ring import input_method_install as module


def bundle(path, identifier, marker):
    (path / 'Contents/MacOS').mkdir(parents=True)
    with (path / 'Contents/Info.plist').open('wb') as stream:
        plistlib.dump({'CFBundleIdentifier': identifier}, stream)
    executable = path / 'Contents/MacOS/ProxiMicInput'
    executable.write_text(marker)
    executable.chmod(0o755)
    (path / 'marker').write_text(marker)


@pytest.fixture
def installer(tmp_path, monkeypatch):
    payload = tmp_path / 'App with spaces.app/Contents/Helpers'
    payload.mkdir(parents=True)
    helper = payload / 'InputMethodAdmin'
    helper.write_text('helper')
    helper.chmod(0o755)
    bundle(payload / 'ProxiMicInput.app', module.BUNDLE_ID, 'new')
    home = tmp_path / 'user'
    home.mkdir()
    monkeypatch.setattr(module.Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    value = module.InputMethodInstaller(payload)
    state = {'selected': False, 'enabled': True, 'fail': '', 'running': '', 'ps': ''}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        code, output, error = 0, '', ''
        if args[0] == str(helper):
            if args[-1] == 'status':
                output = json.dumps({'installed': value.destination.exists(),
                    'sources': [{'selected': state['selected'], 'enabled': state['enabled']}]})
            elif args[-1] == state['fail']:
                code, error = 1, 'test registration failure'
        elif args[0] == '/usr/bin/pgrep':
            code, output = (0, state['running']) if state['running'] else (1, '')
        elif args[0] == '/bin/ps':
            output = state['ps'] if args[-1] == 'uid=,command=' else state['running']
        elif args[0] == '/usr/bin/codesign' and state['fail'] == 'signature':
            code, error = 1, 'invalid signature'
        return subprocess.CompletedProcess(args, code, output, error)

    monkeypatch.setattr(module.subprocess, 'run', run)
    return value, state, calls


def test_unrelated_existing_bundle_is_never_overwritten(installer):
    value, _, calls = installer
    bundle(value.destination, 'other.product', 'keep')
    with pytest.raises(RuntimeError, match='不会覆盖'):
        value.install()
    assert (value.destination / 'marker').read_text() == 'keep'
    assert not any(c[-1] == 'register' for c in calls)


def test_install_does_not_replace_active_input_source(installer):
    value, state, calls = installer
    state['selected'] = True
    with pytest.raises(RuntimeError, match='切回拼音或 ABC'):
        value.install()
    assert not value.destination.exists()
    assert not any(c[0] == '/usr/bin/pgrep' for c in calls)


def test_failed_registration_restores_previous_product(installer):
    value, state, _ = installer
    bundle(value.destination, module.BUNDLE_ID, 'previous')
    state['fail'] = 'enable'
    with pytest.raises(RuntimeError, match='registration failure'):
        value.install()
    assert (value.destination / 'marker').read_text() == 'previous'
    assert not list(value.destination.parent.glob('.ProxiMic-install-*'))


def test_failed_fresh_install_does_not_leave_partial_bundle(installer):
    value, state, _ = installer
    state['fail'] = 'register'
    with pytest.raises(RuntimeError):
        value.install()
    assert not value.destination.exists()
    assert not list(value.destination.parent.glob('.ProxiMic-install-*'))


def test_success_installs_enables_but_never_selects(installer):
    value, _, calls = installer
    assert value.install()['installed']
    assert (value.destination / 'marker').read_text() == 'new'
    assert not list(value.destination.parent.glob('.ProxiMic-install-*'))
    commands = [call[-1] for call in calls]
    assert 'register' in commands and 'enable' in commands
    assert 'select' not in commands


def test_incomplete_payload_cannot_report_success(installer):
    value, _, calls = installer
    (value.source / 'Contents/MacOS/ProxiMicInput').unlink()
    with pytest.raises(RuntimeError, match='缺少'):
        value.install()
    assert calls == []


def test_invalid_signature_leaves_installed_bundle_untouched(installer):
    value, state, _ = installer
    bundle(value.destination, module.BUNDLE_ID, 'previous')
    state['fail'] = 'signature'
    with pytest.raises(RuntimeError, match='signature'):
        value.install()
    assert (value.destination / 'marker').read_text() == 'previous'


def test_system_must_confirm_enabled_source_or_install_rolls_back(installer):
    value, state, _ = installer
    state['enabled'] = False
    with pytest.raises(RuntimeError, match='尚未确认'):
        value.install()
    assert not value.destination.exists()


def test_only_our_inactive_current_user_process_can_be_stopped(installer, monkeypatch):
    value, state, _ = installer
    state['running'] = '42'
    state['ps'] = f'{module.os.getuid()} {value.destination}/Contents/MacOS/ProxiMicInput'
    stopped = []
    def kill(pid, sig):
        stopped.append(pid)
        state['running'] = ''
    monkeypatch.setattr(module.os, 'kill', kill)
    value.install()
    assert stopped == [42]


def test_different_executable_is_not_stopped(installer, monkeypatch):
    value, state, _ = installer
    state['running'] = '42'
    state['ps'] = f'{module.os.getuid()} /another/ProxiMicInput'
    monkeypatch.setattr(module.os, 'kill', lambda *_: pytest.fail('must not stop a different app'))
    with pytest.raises(RuntimeError, match='另一份'):
        value.install()
    assert not value.destination.exists()


def test_frozen_payload_is_located_inside_relocated_app_without_source_tree(tmp_path, monkeypatch):
    app = tmp_path / 'Another Mac/Proximic Voice.app'
    monkeypatch.setattr(module.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(module.sys, 'executable', str(app / 'Contents/MacOS/ProximicVoice'))
    assert module.payload_directory() == app / 'Contents/Helpers'


def test_source_entrypoint_uses_shared_installer(monkeypatch):
    monkeypatch.setattr(module.InputMethodInstaller, 'verify_payload', lambda _: None)
    assert module.main(['verify']) == 0
