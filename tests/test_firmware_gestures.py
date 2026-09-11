import asyncio
import csv
import importlib.util
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

from ring_python_sdk import RingSession
from ring_python_sdk.core.constants import SWIPE_CLASS_LABELS_V2, SWIPE_GESTURE_IDS_V2
from ring_python_sdk.core.device_info import InfoComponent, parse_info_status
from ring_python_sdk.session import sensors
from ring_python_sdk.swipe import (
    SwipeProcessor, SwipeResult, parse_swipe_event_v2, parse_swipe_trigger_v2,
)


_SPEC = importlib.util.spec_from_file_location(
    "firmware_gesture_tool", Path(__file__).parents[1] / "tools/test_firmware_gestures.py"
)
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


def v2_packet(class_id=12, *, trigger=True, seq=1, probabilities=None, mass=0.9):
    if probabilities is None:
        probabilities = [0.0] * 12
        probabilities[SWIPE_GESTURE_IDS_V2.index(class_id)] = 0.8
        probabilities[0] += 0.2
    data = bytes([0x26, 6 if trigger else 5]) + struct.pack(
        "<HB12fI", seq, class_id, *probabilities, 12345
    )
    return bytearray(data + struct.pack("<If", 12000, mass) if trigger else data)


def legacy_packet(*, trigger=True, seq=1):
    return bytearray(bytes([0x26, 3 if trigger else 2]) + struct.pack(
        "<HB7bI", seq, 5, -10, 0, 0, 0, 0, 100, 0, 13000
    ))


@pytest.mark.parametrize("class_id,name", SWIPE_CLASS_LABELS_V2.items())
def test_sparse_v2_ids_callbacks_and_csv(tmp_path, class_id, name):
    events, triggers, logs = [], [], []
    path = tmp_path / "swipe.csv"
    processor = SwipeProcessor(
        path, print_events=False, print_triggers=False, log=logs.append,
        on_event=events.append, on_trigger=triggers.append,
    )
    processor.handle_notification(None, v2_packet(class_id, trigger=False))
    if class_id:
        processor.handle_notification(None, v2_packet(class_id))
    processor.close()
    assert events[0].name == name
    assert events[0].confidence == pytest.approx(1.0 if class_id == 0 else 0.8)
    assert events[0].kind == "event"
    assert len(triggers) == int(class_id != 0)
    if triggers:
        assert triggers[0].name == name
        assert triggers[0].center_uptime_ms == 12000
        assert triggers[0].event_mass == pytest.approx(0.9)
    assert not logs
    with path.open() as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 1 + int(class_id != 0)
    assert None not in rows[0]  # Every row matches the expanded CSV header.
    assert float(rows[0][f"p{class_id}"]) == pytest.approx(events[0].confidence)


@pytest.mark.parametrize("data", [
    b"", b"\x26", v2_packet()[:-1], v2_packet() + b"\x00",
    v2_packet(10, probabilities=[1.0] + [0.0] * 11),
    v2_packet(11, probabilities=[1.0] + [0.0] * 11),
    v2_packet(0), v2_packet(mass=-1), v2_packet(mass=float("nan")),
    v2_packet(probabilities=[0.0] * 12),
    v2_packet(probabilities=[float("nan")] + [0.0] * 11),
    v2_packet(probabilities=[float("inf")] + [0.0] * 11),
    v2_packet(probabilities=[1.1, -0.1] + [0.0] * 10),
])
def test_malformed_v2_never_triggers(tmp_path, data):
    assert parse_swipe_trigger_v2(data) is None
    received = []
    processor = SwipeProcessor(tmp_path / "swipe.csv", on_trigger=received.append)
    try:
        processor.handle_notification(None, bytearray(data))
        assert not received
        assert processor.stats.trigger_count == 0
    finally:
        processor.close()


def test_event_parser_does_not_accept_trigger_and_legacy_has_no_probability(tmp_path):
    assert parse_swipe_event_v2(v2_packet()) is None
    assert parse_swipe_trigger_v2(v2_packet(trigger=False)) is None
    events, triggers, logs = [], [], []
    path = tmp_path / "swipe.csv"
    processor = SwipeProcessor(path, print_events=False, log=logs.append,
                               on_event=events.append, on_trigger=triggers.append)
    processor.handle_notification(None, legacy_packet(trigger=False))
    processor.handle_notification(None, legacy_packet())
    processor.close()
    assert events[0].name == triggers[0].name == "tap"
    assert triggers[0].protocol_version == 1
    assert triggers[0].confidence is None
    assert triggers[0].scores == (-10, 0, 0, 0, 0, 100, 0)
    assert len(logs) == 1 and "event=tap" in logs[0]
    with path.open() as source:
        rows = list(csv.DictReader(source))
    assert all(row["confidence"] == "" and row["p12"] == "" for row in rows)
    assert rows[1]["s5"] == "100"


def test_packet_repeats_are_observable_without_false_65535_packet_loss(tmp_path):
    received = []
    processor = SwipeProcessor(tmp_path / "swipe.csv", print_triggers=False,
                               on_trigger=received.append)
    for seq in (65535, 0, 0, 65535, 1, 3):
        processor.handle_notification(None, v2_packet(seq=seq))
    assert len(received) == 6  # Diagnostics must not silently filter duplicates.
    assert processor.stats.duplicate_packet_count == 1
    assert processor.stats.out_of_order_packet_count == 1
    assert processor.stats.dropped_packet_count == 1
    processor.close()
    processor.handle_notification(None, v2_packet(seq=4))
    assert len(received) == 6  # Closed captures no longer dispatch callbacks.


def test_callback_failure_does_not_break_next_packet_or_csv(tmp_path):
    def fail(_event):
        raise RuntimeError("consumer failed")

    path = tmp_path / "swipe.csv"
    logs, received = [], []
    processor = SwipeProcessor(path, on_trigger=fail, log=logs.append)
    processor.handle_notification(None, v2_packet())
    processor.on_trigger = received.append
    processor.handle_notification(None, v2_packet(seq=2))
    processor.close()
    assert len(received) == 1
    assert processor.stats.callback_error_count == 1
    assert any("callback failed" in line for line in logs)
    with path.open() as source:
        assert len(list(csv.DictReader(source))) == 2


def test_session_dispatches_start_race_and_closes_even_when_stop_fails(tmp_path, monkeypatch):
    session = RingSession("Ringo", 1, data_root=tmp_path)
    session.session_dir = tmp_path
    session.client = SimpleNamespace(is_connected=True)
    received = []

    async def start(_client, _uuid):
        session._demux(None, v2_packet())

    async def stop(_client, _uuid):
        raise RuntimeError("link lost")

    monkeypatch.setattr(sensors, "send_swipe_start", start)
    monkeypatch.setattr(sensors, "send_swipe_stop", stop)

    async def run():
        await session.swipe_on(on_trigger=received.append, print_triggers=False)
        processor = session.swipe
        await session.swipe_on(on_trigger=lambda _r: pytest.fail("callback replaced"))
        assert session.swipe is processor
        with pytest.raises(RuntimeError, match="link lost"):
            await session.swipe_off()
        assert processor._file is None
        assert session.swipe is None and not session.swipe_active

    asyncio.run(run())
    assert received[0].name == "circle-clockwise"
    assert len(session.saved_paths) == 1


@pytest.mark.parametrize("error", [RuntimeError("start failed"), asyncio.CancelledError()])
def test_session_start_failure_and_cancellation_close_capture(tmp_path, monkeypatch, error):
    session = RingSession("Ringo", 1, data_root=tmp_path)
    session.session_dir = tmp_path
    session.client = SimpleNamespace(is_connected=True)
    processors = []

    async def start(_client, _uuid):
        processors.append(session.swipe)
        raise error

    monkeypatch.setattr(sensors, "send_swipe_start", start)
    with pytest.raises(type(error)):
        asyncio.run(session.swipe_on())
    assert processors[0]._file is None
    assert not session.swipe_active and session.swipe is None


def test_recorder_separates_classification_trigger_and_device_wrap(tmp_path, capsys):
    recorder = tool.GestureRecorder(tmp_path / "events.csv")
    recorder.observe(SwipeResult(1, "event", 1, 0, 1))
    recorder.observe(SwipeResult(1, "event", 2, 5, 2))
    assert "TRIGGER" not in capsys.readouterr().out
    recorder.observe(SwipeResult(1, "trigger", 1, 5, 0xFFFFFFF0, scores=(0,) * 7))
    recorder.observe(SwipeResult(1, "trigger", 2, 6, 20, scores=(0,) * 7))
    recorder.finish(metadata={}, processor=None, reason="test")
    with recorder.csv_path.open(encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 4
    assert rows[0]["trigger_index"] == ""
    assert rows[-1]["trigger_interval_device_ms"] == "36"
    assert rows[-1]["confidence"] == ""
    summary = json.loads(recorder.summary_path.read_text())
    assert summary["event_count"] == 2
    assert summary["nonempty_event_count"] == 1
    assert summary["trigger_count"] == 2
    assert summary["trigger_counts"] == {"tap": 1, "snap": 1}
    assert tool.device_interval_ms(10, 20) is None
    with pytest.raises(FileExistsError):
        tool.GestureRecorder(recorder.csv_path)


@pytest.mark.parametrize("outcome", ["completed", "disconnect", "cancel"])
def test_tool_only_starts_swipe_and_finalizes_on_disconnect_or_cancel(tmp_path, monkeypatch, outcome):
    instances = []
    class FakeSession:
        def __init__(self, **kwargs):
            self.client = SimpleNamespace(is_connected=True)
            self.device_info = SimpleNamespace(fw_version="1.2.68", hw_rev=1)
            self.target_name, self.target_address = "Test Ring", "TEST-ID"
            self.swipe = None
            self.disconnected = False
            instances.append(self)

        async def connect(self):
            return True

        async def swipe_on(self, **kwargs):
            self.swipe = SwipeProcessor(tmp_path / "sdk.csv", **kwargs)
            self.swipe.handle_notification(None, v2_packet())
            if outcome == "disconnect":
                self.client.is_connected = False
            elif outcome == "cancel":
                raise asyncio.CancelledError()

        def drain_live_logs(self):
            return []

        async def disconnect(self):
            self.disconnected = True
            if self.swipe:
                self.swipe.close()

    monkeypatch.setattr(tool, "RingSession", FakeSession)
    args = tool.parser().parse_args(["--duration", "0.01", "--csv", str(tmp_path / "events.csv")])
    if outcome == "completed":
        assert asyncio.run(tool.run(args)) == 0
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
            asyncio.run(tool.run(args))
    assert instances[0].disconnected
    summary = json.loads((tmp_path / "events.summary.json").read_text())
    assert summary["trigger_count"] == 1
    assert (summary["stop_reason"] == "completed") == (outcome == "completed")


def test_demo_exercises_all_eleven_triggers_without_bluetooth(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "RingSession", lambda **_kw: pytest.fail("demo touched BLE"))
    assert tool.main(["--demo", "--csv", str(tmp_path / "demo.csv")]) == 0
    summary = json.loads((tmp_path / "demo.summary.json").read_text())
    assert summary["source"] == "demo"
    assert summary["protocols_observed"] == [2]
    assert summary["event_count"] == 12
    assert summary["trigger_count"] == 11
    assert set(summary["trigger_counts"]) == set(SWIPE_CLASS_LABELS_V2.values()) - {"empty"}


@pytest.mark.parametrize("args", [["--duration", "nan"], ["--timeout", "0"], ["--status-interval", "-1"]])
def test_invalid_cli_timing_rejected(args):
    with pytest.raises(SystemExit) as error:
        tool.main(args)
    assert error.value.code == 2


def test_explicit_firmware_without_swipe_exits_before_start(tmp_path, monkeypatch):
    # Actual INFO response read from Ringo2CC7 running 1.1.186. SWIPE id=8
    # reports present=0,count=0,model=none; no identifier is stored in this packet.
    info = parse_info_status(bytes.fromhex(
        "2a0201010101ba000801010101030201010103030101010004010101030501010103"
        "060101020007010101000800000000"
    ))
    assert info.fw_version == "1.1.186"
    instances = []

    class FakeSession:
        def __init__(self, **_kwargs):
            self.device_info = info
            self.target_name, self.target_address = "Test Ring", "TEST-ID"
            self.disconnected = False
            instances.append(self)

        async def connect(self):
            return True

        async def swipe_on(self, **_kwargs):
            pytest.fail("firmware explicitly declares no swipe model")

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(tool, "RingSession", FakeSession)
    args = tool.parser().parse_args(["--csv", str(tmp_path / "events.csv")])
    with pytest.raises(RuntimeError, match="present=0, model=none"):
        asyncio.run(tool.run(args))
    assert instances[0].disconnected
    summary = json.loads((tmp_path / "events.summary.json").read_text())
    assert summary["device"]["firmware_swipe"]["status"] == "unavailable"
    assert summary["trigger_count"] == 0


@pytest.mark.parametrize("info,expected", [
    (None, "unknown"),
    (SimpleNamespace(components=()), "unknown"),
    (SimpleNamespace(components=(InfoComponent(1, 1, 1, 1, 3),)), "unknown"),
    (SimpleNamespace(fw_version="1.1.186", components=(InfoComponent(8, 1, 1, 2, 3),)), "available"),
    (SimpleNamespace(components=(InfoComponent(8, 1, 1, 0, 0),)), "unavailable"),
])
def test_swipe_capability_uses_explicit_component_not_version(info, expected):
    assert tool.firmware_swipe_capability(info)["status"] == expected
