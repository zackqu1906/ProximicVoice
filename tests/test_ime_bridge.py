import json
import os
import socket
import stat
import threading
import tempfile
from pathlib import Path
import time

import pytest

from proximic_ring.ime_bridge import IMEBridge


def receive(stream):
    return json.loads(stream.readline())


def send(stream, message):
    stream.write(json.dumps(message).encode() + b"\n")
    stream.flush()


def connect(bridge, token=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    sock.connect(str(bridge.path))
    stream = sock.makefile("rwb")
    send(stream, {"type": "hello", "protocol": 1, "token": token or (bridge.path.parent / "bridge.token").read_text()})
    return sock, stream


@pytest.fixture
def bridge(tmp_path):
    messages = []
    with tempfile.TemporaryDirectory(prefix="ime-test-", dir="/private/tmp") as directory:
        value = IMEBridge(messages.append, path=Path(directory) / "bridge.sock")
        value.start()
        yield value, messages
        value.close()


def wait_until(predicate):
    for _ in range(100):
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_authenticated_epoch_and_private_files(bridge):
    server, messages = bridge
    assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((server.path.parent / "bridge.token").stat().st_mode) == 0o600
    bad, bad_stream = connect(server, token="invalid")
    assert bad_stream.readline() == b""
    bad_stream.close(); bad.close()
    sock, stream = connect(server)
    welcome = receive(stream)
    epoch = welcome["epoch"]
    assert welcome["protocol"] == 1
    send(stream, {"type": "state", "epoch": "old", "client_id": "wrong"})
    send(stream, {"type": "state", "epoch": epoch, "client_id": "real", "ready": True})
    wait_until(lambda: any(m.get("client_id") == "real" for m in messages))
    assert not any(m.get("client_id") == "wrong" for m in messages)
    assert server.send({"type": "begin", "client_id": "real"})
    assert receive(stream)["epoch"] == epoch
    stream.close(); sock.close()
    wait_until(lambda: not server.connected)
    newer, newer_stream = connect(server)
    assert receive(newer_stream)["epoch"] != epoch
    newer_stream.close(); newer.close()


def test_second_listener_does_not_remove_live_endpoint_or_rotate_token(bridge):
    first, _ = bridge
    token = (first.path.parent / "bridge.token").read_text()
    inode = first.path.stat().st_ino
    other = IMEBridge(lambda _: None, path=first.path)
    with pytest.raises(BlockingIOError):
        other.start()
    assert first.path.stat().st_ino == inode
    assert (first.path.parent / "bridge.token").read_text() == token
    sock, stream = connect(first)
    assert receive(stream)["type"] == "welcome"
    stream.close(); sock.close()


def test_wechat_command_survives_transport_with_epoch_fencing(bridge):
    server, messages = bridge
    sock, stream = connect(server)
    epoch = receive(stream)["epoch"]
    command = dict(type="wechat_key", epoch=epoch, client_id="client", utterance_id="utterance",
                   request_id="request", application="com.tencent.xinWeChat", command="select_all",
                   count=0, event_tag=123)
    send(stream, {**command, "epoch": "stale", "request_id": "old"})
    send(stream, command)
    wait_until(lambda: any(m.get("request_id") == "request" for m in messages))
    assert not any(m.get("request_id") == "old" for m in messages)
    server.send(dict(type="wechat_key_result", request_id="request", error=""))
    assert receive(stream)["epoch"] == epoch
    stream.close(); sock.close()


@pytest.mark.parametrize("name", ["bridge.sock", "bridge.token", "bridge.lock"])
def test_reject_symlink_files(tmp_path, name):
    directory = tmp_path / "ime"
    directory.mkdir(mode=0o700)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("untouched")
    (directory / name).symlink_to(sentinel)
    bridge = IMEBridge(lambda _: None, path=directory / "bridge.sock")
    with pytest.raises((RuntimeError, OSError)):
        bridge.start()
    assert sentinel.read_text() == "untouched"


def test_context_is_fresh_and_truncated_only_for_asr(bridge):
    server, messages = bridge
    sock, stream = connect(server)
    epoch = receive(stream)["epoch"]
    original = "完整原文" * 700
    result = []
    worker = threading.Thread(target=lambda: result.append(server.asr_context(timeout=1)))
    worker.start()
    ping = receive(stream)
    assert ping["capture_context"] is True
    send(stream, {"type": "state", "epoch": epoch, "request_id": ping["request_id"], "client_id": "a", "phase": "idle", "ready": True, "original": original, "selection": [0, 0], "context_complete": True, "application": "example"})
    worker.join(2)
    assert result[0]["text"] == original[-1600:]
    assert result[0]["source_char_count"] == len(original)
    assert server._state["original"] == original
    # Cached full context must never stand in for a new authoritative response.
    assert server.asr_context(timeout=.02)["status"] == "unavailable"
    receive(stream)
    stream.close(); sock.close()


def test_unavailable_context_does_not_reuse_previous_field(bridge):
    server, _ = bridge
    sock, stream = connect(server)
    epoch = receive(stream)["epoch"]
    result = []
    worker = threading.Thread(target=lambda: result.append(server.asr_context(timeout=1)))
    worker.start()
    ping = receive(stream)
    send(stream, {"type": "state", "epoch": epoch, "request_id": ping["request_id"], "client_id": "b", "ready": True, "context_complete": False, "original": "partial"})
    worker.join(2)
    assert result[0]["status"] == "unavailable"
    assert "text" not in result[0]
    stream.close(); sock.close()


def test_activation_recovery_reply_reaches_host_and_does_not_replace_context(bridge):
    server, messages = bridge
    sock, stream = connect(server)
    epoch = receive(stream)['epoch']
    send(stream, dict(type='activation_recovery', epoch='old', request_id='old', result='refreshed'))
    send(stream, dict(type='activation_recovery', epoch=epoch, request_id='tap', result='not_selected'))
    wait_until(lambda: any(m.get('request_id') == 'tap' for m in messages))
    assert not any(m.get('request_id') == 'old' for m in messages)
    assert not server._state
    stream.close(); sock.close()
