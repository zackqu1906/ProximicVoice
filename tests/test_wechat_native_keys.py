from types import SimpleNamespace
import sys

import pytest

from proximic_ring.wechat_native_keys import send_wechat_key
from proximic_ring.wechat_native_keys import send_codex_key
from proximic_ring.wechat_native_keys import send_input_method_key


@pytest.fixture
def desktop(monkeypatch):
    state = SimpleNamespace(bundle="com.tencent.xinWeChat", pid=42, permission=True, sent=[])
    app = SimpleNamespace(bundleIdentifier=lambda: state.bundle, processIdentifier=lambda: state.pid)
    workspace = SimpleNamespace(frontmostApplication=lambda: app)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)))
    quartz = SimpleNamespace(
        kCGEventFlagMaskCommand=256, kCGEventFlagMaskShift=128, kCGEventSourceUserData=99,
        CGPreflightPostEventAccess=lambda: state.permission,
        CGEventCreateKeyboardEvent=lambda source, code, pressed: {"code": code, "pressed": pressed},
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        CGEventSetIntegerValueField=lambda event, field, value: event.update(tag=value),
        CGEventPostToPid=lambda pid, event: state.sent.append((pid, event)),
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    return state, quartz


@pytest.mark.parametrize("command,count", [("select_all", 0), ("select_previous", 3), ("delete", 0), ("caret_from_end", 2)])
def test_commands_are_tagged_and_target_wechat_pid(desktop, command, count):
    state, _ = desktop
    send_wechat_key(command, count, 456)
    assert all(pid == 42 and event["tag"] == 456 for pid, event in state.sent)
    assert all(event["code"] not in (36, 76) for _, event in state.sent)
    assert [event["pressed"] for _, event in state.sent] == [True, False] * (3 if count else 1)
    if command == "select_previous":
        assert all(event["code"] == 123 and event["flags"] == 128 for _, event in state.sent)


@pytest.mark.parametrize("bundle", ["com.openai.codex", "com.apple.TextEdit", ""])
def test_never_posts_to_other_applications(desktop, bundle):
    state, _ = desktop
    state.bundle = bundle
    with pytest.raises(RuntimeError):
        send_wechat_key("delete", 0, 456)
    assert state.sent == []


def test_permission_failure_does_not_post_or_prompt(desktop):
    state, _ = desktop
    state.permission = False
    with pytest.raises(RuntimeError, match="辅助功能"):
        send_wechat_key("select_all", 0, 456)
    assert state.sent == []


@pytest.mark.parametrize("bundle", ["com.tencent.xinWeChat", "com.microsoft.VSCode"])
def test_sentence_keys_recover_on_next_request_after_permission_grant(desktop, bundle):
    state, _ = desktop
    state.bundle = bundle
    def send():
        if bundle == "com.tencent.xinWeChat":
            send_wechat_key("delete", 0, 456)
        else:
            send_input_method_key(bundle, "delete", 0, 456)
    state.permission = False
    with pytest.raises(RuntimeError):
        send()
    assert not state.sent
    state.permission = True
    send()
    assert len(state.sent) == 2
    state.permission = False
    with pytest.raises(RuntimeError):
        send()
    assert len(state.sent) == 2


def test_changed_process_stops_remaining_motion(desktop):
    state, quartz = desktop
    def post(pid, event):
        state.sent.append((pid, event))
        if not event["pressed"]:
            state.pid = 100
    quartz.CGEventPostToPid = post
    with pytest.raises(RuntimeError):
        send_wechat_key("caret_from_end", 4, 456)
    assert len(state.sent) == 2


@pytest.mark.parametrize("command,count,tag", [("send", 0, 1), ("delete", 513, 1), ("delete", True, 1),
                                             ("delete", 0, 0), ("delete", 0, True),
                                             ("select_previous", 0, 1), ("undo", 0, 1)])
def test_malformed_requests_are_rejected(desktop, command, count, tag):
    state, _ = desktop
    with pytest.raises(ValueError):
        send_wechat_key(command, count, tag)
    assert state.sent == []


def test_codex_sentence_keys_do_not_send_messages(desktop):
    state, _ = desktop
    state.bundle = "com.openai.codex"
    send_codex_key("select_previous", 3, 456)
    send_codex_key("delete", 0, 456)
    assert [event["code"] for _, event in state.sent] == [123] * 6 + [51] * 2
    for command in ["undo", "send", "copy"]:
        with pytest.raises(ValueError):
            send_codex_key(command, 1, 456)


def test_codex_undo_never_posts_to_wechat_or_after_process_change(desktop):
    state, quartz = desktop
    with pytest.raises(RuntimeError):
        send_codex_key("delete", 0, 123)
    assert not state.sent
    state.bundle = "com.openai.codex"
    def post(pid, event):
        state.sent.append((pid, event))
        if not event["pressed"]:
            state.pid += 1
    quartz.CGEventPostToPid = post
    with pytest.raises(RuntimeError):
        send_codex_key("select_previous", 5, 123)
    assert len(state.sent) == 2


@pytest.mark.parametrize("command,count,codes", [("caret_to_end", 0, [125, 125]), ("caret_backward", 3, [123] * 6),
                                               ("caret_forward", 2, [124] * 4)])
def test_codex_caret_only_keys_are_tagged_and_targeted(desktop, command, count, codes):
    state, _ = desktop
    state.bundle = "com.openai.codex"
    send_codex_key(command, count, 789)
    assert [event["code"] for _, event in state.sent] == codes
    assert all(pid == 42 and event["tag"] == 789 for pid, event in state.sent)
    assert all(event["flags"] == (256 if command == "caret_to_end" else 0) for _, event in state.sent)


@pytest.mark.parametrize("command,count", [("caret_to_end", 1), ("caret_backward", 0), ("caret_backward", 513),
                                         ("caret_forward", 0), ("caret_forward", 513)])
def test_codex_rejects_invalid_caret_requests(desktop, command, count):
    state, _ = desktop
    state.bundle = "com.openai.codex"
    with pytest.raises(ValueError):
        send_codex_key(command, count, 789)
    assert not state.sent


def test_codex_caret_requests_require_permission_and_do_not_leak_to_wechat(desktop):
    state, _ = desktop
    with pytest.raises(RuntimeError):
        send_codex_key("caret_to_end", 0, 789)
    with pytest.raises(ValueError):
        send_wechat_key("caret_to_end", 0, 789)
    state.bundle = "com.openai.codex"
    state.permission = False
    with pytest.raises(RuntimeError):
        send_codex_key("caret_to_end", 0, 789)
    assert not state.sent


@pytest.mark.parametrize("application", ["com.microsoft.VSCode", "com.apple.TextEdit", "org.example.Editor"])
@pytest.mark.parametrize("command,count,codes", [("select_previous", 2, [123] * 4), ("delete", 0, [51] * 2),
                                                 ("caret_to_end", 0, [125] * 2), ("caret_backward", 1, [123] * 2),
                                                 ("select_all", 0, [0] * 2), ("select_to_start", 0, [126, 126]), ("caret_from_end", 1, [125, 125, 123, 123])])
def test_default_keys_work_for_any_matching_frontmost_client(desktop, application, command, count, codes):
    state, _ = desktop
    state.bundle = application
    send_input_method_key(application, command, count, 789)
    assert [event["code"] for _, event in state.sent] == codes
    assert all(pid == 42 and event["tag"] == 789 for pid, event in state.sent)


@pytest.mark.parametrize("application", [None, "", " ", "com.tencent.xinWeChat"])
def test_default_keys_require_an_explicit_non_wechat_target(desktop, application):
    state, _ = desktop
    with pytest.raises(ValueError):
        send_input_method_key(application, "delete", 0, 789)
    assert not state.sent


def test_default_keys_stop_on_target_change_and_cannot_copy_or_send(desktop):
    state, quartz = desktop
    state.bundle = "org.example.Editor"
    for command in ["copy", "send", "undo"]:
        with pytest.raises(ValueError):
            send_input_method_key(state.bundle, command, 1, 789)
    with pytest.raises(RuntimeError):
        send_input_method_key("com.microsoft.VSCode", "delete", 0, 789)
    assert not state.sent
    def post(pid, event):
        state.sent.append((pid, event))
        if not event["pressed"]:
            state.bundle = "com.microsoft.VSCode"
    quartz.CGEventPostToPid = post
    with pytest.raises(RuntimeError):
        send_input_method_key("org.example.Editor", "select_previous", 4, 789)
    assert len(state.sent) == 2
