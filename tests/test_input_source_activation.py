"""Exercise asynchronous source selection without touching any real app."""
import sys
import time

import pytest
from PySide6.QtCore import QCoreApplication, QTimer, QObject, Signal

from proximic_ring.ui.input_source_activation import InputSourceActivation
from test_inline_state import component, receive


def wait_until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.005)
    assert predicate()


def helper(tmp_path, body):
    path = tmp_path / 'helper.py'
    path.write_text('import sys,json,time\n' + body)
    return [sys.executable, '-u', str(path)]


@pytest.mark.parametrize('already_selected', [True, False])
def test_source_result_requires_no_focus_or_stdin_handshake(tmp_path, already_selected):
    app = QCoreApplication.instance() or QCoreApplication([])
    command = helper(tmp_path, f'''assert sys.argv[-3:] == ['ensure-selected', '123', 'test.editor']
print(json.dumps({{'event':'selected','already_selected':{already_selected!r}}}),flush=True)
''')
    source = InputSourceActivation(command=command)
    events = []
    source.event.connect(events.append)
    source.start(('test.editor', 123))
    wait_until(lambda: bool(events))
    assert events == [{'event':'selected', 'already_selected':already_selected}]
    source.cancel()
    app.processEvents()


def test_hung_selector_is_killed_without_stopping_gui_timers(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    source = InputSourceActivation(command=helper(tmp_path, 'time.sleep(10)\n'), timeout_ms=200)
    beats = []
    heartbeat = QTimer(); heartbeat.setInterval(10); heartbeat.timeout.connect(lambda: beats.append(1)); heartbeat.start()
    events = []; source.event.connect(events.append)
    started = time.monotonic(); source.start(('test.editor',123))
    assert time.monotonic() - started < .1
    wait_until(lambda: bool(events))
    heartbeat.stop()
    assert events[-1]['event'] == 'error' and '超时' in events[-1]['error']
    assert len(beats) >= 5
    assert time.monotonic() - started < 1
    app.processEvents()


def test_cancel_kills_pending_selection_without_delivering_a_result(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    source = InputSourceActivation(command=helper(tmp_path, '''time.sleep(10)
print(json.dumps({'event':'selected'}),flush=True)
'''))
    events = []; source.event.connect(events.append)
    process = source._process
    source.start(('test.editor',123))
    source.cancel()
    # Drain the process lifecycle before Qt deletes the wrapper.
    for _ in range(10):
        app.processEvents(); time.sleep(.005)
    assert not events


class FakeSource(QObject):
    event = Signal(object)
    def __init__(self,parent=None):
        super().__init__(parent); self.calls=[]
    def start(self,origin): self.calls.append(('start',origin))
    def recover(self,origin): self.calls.append(('recover',origin))
    def owns_foreground(self): return False
    def cancel(self): self.calls.append(('cancel',))


def prepare(c, monkeypatch):
    monkeypatch.setattr(c,'_foreground_identity',lambda:('test.editor',123))
    c._source_switch_factory=FakeSource
    assert c.begin(auto_select=True)
    source=c._source_activation
    c.bind_session(7); c.update('第一句完整的话',True,7)
    return source


@pytest.mark.parametrize('already_selected', [True, False])
def test_source_selection_releases_buffer_on_native_ack_without_focus_check(component,monkeypatch,already_selected):
    c,_=component; source=prepare(c,monkeypatch)
    receive(c,ready=True,phase='idle',utterance_id='',application='test.editor')
    assert not any(m['type']=='begin' for m in c._bridge.messages)
    source.event.emit({'event':'selected','already_selected':already_selected})
    # A fresh ping can describe the previous completed sentence in this client.
    receive(c,ready=True,phase='dictated',utterance_id='previous',application='test.editor')
    assert c._bridge.messages[-1]['type']=='begin'
    assert not any(m['type']=='update' for m in c._bridge.messages)
    receive(c,ready=True,phase='listening',application='test.editor')
    updates=[m for m in c._bridge.messages if m['type']=='update']
    assert len(updates)==1 and updates[0]['text']=='第一句完整的话'
    assert source.calls==[('start',('test.editor',123)), ('cancel',)]


@pytest.mark.parametrize('failure',['selection_failed','cancel','changed_app'])
def test_cancel_error_or_changed_app_discards_buffer(component,monkeypatch,failure):
    c,_=component; source=prepare(c,monkeypatch)
    if failure=='selection_failed': source.event.emit({'event':'error','error':'输入法未安装'})
    elif failure=='cancel': c.cancel()
    else:
        monkeypatch.setattr(c,'_foreground_identity',lambda:('other.editor',456))
        source.event.emit({'event':'selected'})
    source.event.emit({'event':'selected'})  # late helper result
    receive(c,ready=True,phase='listening',application='test.editor')
    assert not c._startup_buffering
    assert not any(m['type']=='update' for m in c._bridge.messages)


def test_old_helper_result_cannot_release_next_sentence(component,monkeypatch):
    c,_=component; old=prepare(c,monkeypatch);c.cancel()
    new=prepare(c,monkeypatch)
    old.event.emit({'event':'selected'})
    assert c._source_activation is new and c._source_pending
    assert not any(m['type']=='begin' for m in c._bridge.messages)


def test_new_source_activation_does_not_trigger_early_recovery(component, monkeypatch):
    c, events = component
    source = prepare(c, monkeypatch)
    source.event.emit({'event':'selected', 'already_selected':False, 'focus_restored':True})
    deadline = c._startup_deadline
    # Reproduce the real log: source selected, timer at ~70ms, then first ready.
    c._refresh_activation()
    receive(c, ready=False, phase='idle', client_id='', utterance_id='', lifecycle_event='inactive')
    c._refresh_activation()
    assert not any(m['type']=='recover_activation' for m in c._bridge.messages)
    receive(c, ready=True, phase='idle', client_id='activated', utterance_id='', application='test.editor')
    assert c._bridge.messages[-1]['type']=='begin'  # no 800ms wait on the normal path
    c._refresh_activation()
    assert c._bridge.messages[-1]['type']=='ping'  # query ACK, never reset/replay
    receive(c, ready=True, phase='listening', application='test.editor')
    updates=[m for m in c._bridge.messages if m['type']=='update']
    assert len(updates)==1 and updates[0]['text']=='第一句完整的话'
    assert not any(m['type']=='recover_activation' for m in c._bridge.messages)
    assert any(e['type']=='startup_state' and e.get('lifecycle_event')=='inactive' for e in events)


def test_missing_client_never_starts_extra_source_switch(component, monkeypatch):
    c, _ = component; source=prepare(c, monkeypatch)
    source.event.emit({'event':'selected', 'already_selected':False, 'focus_restored':True})
    deadline=c._startup_deadline
    for _ in range(6): c._refresh_activation()
    assert c._bridge.messages[-1]['type']=='ping'
    assert not any(m['type']=='recover_activation' for m in c._bridge.messages)
    assert c._startup_deadline==deadline

def test_already_selected_source_never_cycles_to_abc(component, monkeypatch):
    c, _ = component; source=prepare(c, monkeypatch)
    source.event.emit({'event':'selected', 'already_selected':True})
    c._refresh_activation()
    assert c._bridge.messages[-1]['type']=='ping'
    assert not any(m['type']=='recover_activation' for m in c._bridge.messages)

def test_lost_begin_ack_is_queried_without_duplicate_begin(component, monkeypatch):
    c, _ = component; source=prepare(c, monkeypatch)
    source.event.emit({'event':'selected', 'already_selected':True})
    receive(c, ready=True, phase='idle', utterance_id='', application='test.editor')
    for _ in range(3):c._refresh_activation()
    assert sum(m['type']=='begin' for m in c._bridge.messages)==1
    assert not any(m['type']=='recover_activation' for m in c._bridge.messages)
    # Real native ping includes the active session and can acknowledge BEGIN.
    receive(c, ready=True, phase='listening', application='test.editor')
    assert sum(m['type']=='update' for m in c._bridge.messages)==1


def test_cold_selection_leaves_budget_for_native_activation(component, monkeypatch):
    c, _ = component
    source = prepare(c, monkeypatch)
    utterance = c._utterance_id
    # TIS cold select finishes with only 0.8s left in the old shared budget.
    c._startup_deadline = time.monotonic() + .8
    source.event.emit({'event': 'selected', 'already_selected': False})
    assert c._startup_deadline - time.monotonic() > 2.9
    receive(c, ready=True, phase='idle', client_id='cold-client', utterance_id='', application='test.editor')
    receive(c, ready=True, phase='listening', application='test.editor')
    updates = [m for m in c._bridge.messages if m['type'] == 'update']
    assert len(updates) == 1 and updates[0]['utterance_id'] == utterance


def test_transient_activation_cannot_release_text_or_cancel_next_begin(component, monkeypatch):
    c, _ = component
    source = prepare(c, monkeypatch)
    source.event.emit({'event': 'selected', 'already_selected': True})
    c._refresh_activation()
    receive(c, ready=True, phase='idle', client_id='transient', utterance_id='', application='test.editor')
    receive(c, ready=False, phase='idle', client_id='', utterance_id='', lifecycle_event='deactivate')
    assert not any(m['type']=='update' for m in c._bridge.messages)
    receive(c, 'activation_recovery', request_id='obsolete', result='refreshed')
    receive(c, ready=True, phase='idle', client_id='stable', utterance_id='', application='test.editor')
    receive(c, ready=True, phase='listening', application='test.editor')
    updates = [m for m in c._bridge.messages if m['type'] == 'update']
    assert len(updates) == 1 and updates[0]['client_id'] == 'stable'
    assert not any(m['type']=='recover_activation' for m in c._bridge.messages)

def test_ime_reconnect_before_first_ack_keeps_same_audio_sentence(component, monkeypatch):
    c, _ = component
    source = prepare(c, monkeypatch)
    source.event.emit({'event': 'selected', 'already_selected': True})
    old_id = c._utterance_id
    deadline = c._startup_deadline
    receive(c, 'disconnected')
    assert c._startup_buffering and c._pending_update[0] == '第一句完整的话'
    c._accept({'type': 'connected', 'epoch': 'reconnected'})
    assert c._startup_deadline == deadline and c._utterance_id == old_id
    receive(c, ready=True, phase='idle', client_id='new-process', utterance_id='', application='test.editor')
    receive(c, ready=True, phase='listening', application='test.editor')
    updates = [m for m in c._bridge.messages if m['type'] == 'update']
    assert len(updates) == 1 and updates[0]['text'] == '第一句完整的话'
    assert c._epoch == 'reconnected' and updates[0]['client_id'] == 'new-process'


def test_ime_warmup_uses_background_launch_without_selecting_source(tmp_path, monkeypatch):
    from proximic_ring.ui import input_source_activation as module
    bundle = tmp_path / 'Library/Input Methods/ProxiMicInput.app'
    executable = bundle / 'Contents/MacOS/ProxiMicInput'
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    calls = []
    class Process:
        @staticmethod
        def startDetached(*args): calls.append(args); return (True, 123)
    monkeypatch.setattr(module, 'QProcess', Process)
    module.warm_input_method()
    assert calls == [('/usr/bin/open', ['-g', str(bundle)])]


def test_focus_interval_allows_only_owned_helper_and_buffers_native_states(component, monkeypatch):
    c, _ = component; source = prepare(c, monkeypatch)
    monkeypatch.setattr(c, '_foreground_identity', lambda: None)  # helper has no app bundle
    monkeypatch.setattr(source, 'owns_foreground', lambda: True)
    c._refresh_activation()
    receive(c, ready=False, phase='idle', client_id='', utterance_id='')
    assert c._source_pending and c._startup_buffering
    assert not any(m['type'] == 'begin' for m in c._bridge.messages)
    monkeypatch.setattr(source, 'owns_foreground', lambda: False)  # user switched elsewhere
    c._refresh_activation()
    assert not c._source_pending and not c._startup_buffering
    assert source.calls[-1] == ('cancel',)


def test_selected_but_inactive_recovers_once_before_begin(component, monkeypatch):
    c, _ = component; old = prepare(c, monkeypatch)
    old.event.emit({'event': 'selected', 'already_selected': True})
    receive(c, ready=False, phase='idle', client_id='', utterance_id='')
    source = c._source_activation
    assert source is not old and source.calls == [('recover', ('test.editor', 123))]
    assert c._source_pending
    old.event.emit({'event': 'selected'})  # obsolete result must not finish the recovery
    assert c._source_pending
    source.event.emit({'event': 'selected', 'already_selected': True, 'focus_restored': True})
    receive(c, ready=False, phase='idle', client_id='', utterance_id='')
    assert c._source_activation is source and not c._source_pending  # no loop
    receive(c, ready=True, phase='idle', client_id='recovered', utterance_id='', application='test.editor')
    receive(c, ready=True, phase='listening', application='test.editor')
    assert sum(m['type'] == 'begin' for m in c._bridge.messages) == 1
    assert sum(m['type'] == 'update' for m in c._bridge.messages) == 1


def test_focus_recovery_command_is_separate_and_cancellable(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    source = InputSourceActivation(command=helper(tmp_path, "assert sys.argv[-3:] == ['refresh-selected', '123', 'test.editor']\nprint(json.dumps({'event':'selected','focus_restored':True}),flush=True)\n"))
    events = []; source.event.connect(events.append)
    source.recover(('test.editor', 123))
    wait_until(lambda: bool(events))
    assert events[0]['focus_restored'] is True
    source.cancel(); app.processEvents()
