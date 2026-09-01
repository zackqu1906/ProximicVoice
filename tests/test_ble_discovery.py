import asyncio
from types import SimpleNamespace

from ring_python_sdk.ble import control
from ring_python_sdk import RingSession
from ring_python_sdk.session import connection


def _device(name: str | None, identifier: str):
    return SimpleNamespace(name=name, address=identifier)


def _advertisement(*, local_name: str | None = None, rssi: int | None = None):
    return SimpleNamespace(local_name=local_name, rssi=rssi)


def test_scan_all_devices_keeps_non_ringo_and_macos_identifiers(monkeypatch):
    printer = _device("Office Printer", "AA:BB:CC:DD:EE:01")
    unnamed_ring = _device(None, "57A74F5D-18EA-4C3B-83B2-987ED0512456")

    class FakeScanner:
        def __init__(self, *, detection_callback):
            self.callback = detection_callback

        async def start(self):
            self.callback(printer, _advertisement(rssi=-70))
            self.callback(
                unnamed_ring,
                _advertisement(local_name="My Voice Ring", rssi=-42),
            )

        async def stop(self):
            return None

    async def no_wait(_timeout):
        return None

    monkeypatch.setattr(control, "BleakScanner", FakeScanner)
    monkeypatch.setattr(control.asyncio, "sleep", no_wait)

    found = asyncio.run(control.scan_all_devices(0.01))

    assert [item.name for item in found] == ["My Voice Ring", "Office Printer"]
    assert found[0].identifier == "57A74F5D-18EA-4C3B-83B2-987ED0512456"
    assert found[0].rssi == -42


def test_legacy_scan_rings_still_supports_cli_name_filter(monkeypatch):
    async def scan_all(_timeout):
        return [
            control.DiscoveredBLEDevice(_device("Ringo One", "1"), "Ringo One", "1"),
            control.DiscoveredBLEDevice(_device("Other Device", "2"), "Other Device", "2"),
        ]

    monkeypatch.setattr(control, "scan_all_devices", scan_all)

    found = asyncio.run(control.scan_rings("ringo", 0.01))

    assert [item.address for item in found] == ["1"]


def test_connect_target_rescans_without_name_filter(monkeypatch):
    selected = _device("Custom Device", "MACOS-OPAQUE-UUID")

    async def scan_all(_timeout):
        return [
            control.DiscoveredBLEDevice(
                selected,
                "Custom Device",
                "MACOS-OPAQUE-UUID",
            )
        ]

    monkeypatch.setattr(connection, "scan_all_devices", scan_all)

    class FakeConnection(connection.ConnectionMixin):
        name_keyword = "Ringo"
        timeout_s = 0.01
        scanned = []

        async def _connect_device(self, target, *, new_session):
            self.connected_target = target
            self.new_session = new_session
            return True

    session = FakeConnection()
    connected = asyncio.run(session.connect_target("MACOS-OPAQUE-UUID"))

    assert connected is True
    assert session.connected_target is selected
    assert session.new_session is True


def test_connect_target_exact_mac_uses_short_targeted_scan(monkeypatch):
    address = "CC:01:8C:31:2C:C7"
    selected = _device("Ringo2CC7", address)
    calls = []

    class FakeScanner:
        @classmethod
        async def find_device_by_address(cls, target, timeout):
            calls.append((target, timeout))
            return selected

    async def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("an exact Windows MAC should use targeted discovery")

    monkeypatch.setattr(connection, "BleakScanner", FakeScanner)
    monkeypatch.setattr(connection, "scan_all_devices", unexpected_full_scan)

    class FakeConnection(connection.ConnectionMixin):
        name_keyword = "Ringo"
        timeout_s = 8.0

        def __init__(self):
            self.scanned = []

        async def _connect_device(self, target, *, new_session):
            self.connected_target = target
            self.new_session = new_session
            return True

    session = FakeConnection()
    connected = asyncio.run(session.connect_target(address))

    assert connected is True
    assert calls == [(address, 3.0)]
    assert session.connected_target is selected
    assert session.new_session is True


def test_connect_target_macos_uuid_uses_targeted_scan_in_runtime_loop(monkeypatch):
    identifier = "57A74F5D-18EA-4C3B-83B2-987ED0512456"
    selected = _device("Ringo Mac", identifier)
    calls = []

    class FakeScanner:
        @classmethod
        async def find_device_by_address(cls, target, timeout):
            calls.append((target, timeout))
            return selected

    async def unexpected_full_scan(*_args, **_kwargs):
        raise AssertionError("an exact CoreBluetooth UUID should use targeted discovery")

    monkeypatch.setattr(connection, "BleakScanner", FakeScanner)
    monkeypatch.setattr(connection, "scan_all_devices", unexpected_full_scan)

    class FakeConnection(connection.ConnectionMixin):
        name_keyword = "Ringo"
        timeout_s = 8.0

        def __init__(self):
            self.scanned = []

        async def _connect_device(self, target, *, new_session):
            self.connected_target = target
            return True

    session = FakeConnection()
    connected = asyncio.run(session.connect_target(identifier))

    assert connected is True
    assert calls == [(identifier, 3.0)]
    assert session.connected_target is selected


def test_connect_device_uses_selected_scan_result_without_rescanning(monkeypatch):
    selected = _device("New Ringo", "ROTATING-IDENTIFIER")

    async def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("selected BLEDevice must connect without a second scan")

    monkeypatch.setattr(connection, "scan_all_devices", unexpected_scan)

    class FakeConnection(connection.ConnectionMixin):
        async def _connect_device(self, target, *, new_session):
            self.connected_target = target
            self.new_session = new_session
            return True

    session = FakeConnection()
    connected = asyncio.run(session.connect_device(selected))

    assert connected is True
    assert session.connected_target is selected
    assert session.new_session is True


def test_ring_session_does_not_auto_reconnect_by_default():
    session = RingSession(name_keyword="Ringo", timeout_s=1.0)

    assert session.auto_reconnect is False
    assert session.battery_poll_enabled is True


def test_streaming_session_can_disable_battery_control_writes(monkeypatch):
    session = RingSession(
        name_keyword="Ringo",
        timeout_s=1.0,
        battery_poll_enabled=False,
    )
    queries = []

    async def query_once():
        queries.append(True)

    monkeypatch.setattr(session, "query_battery", query_once)

    asyncio.run(session._start_battery_poll())

    assert queries == []
    assert session._battery_task is None


def test_unexpected_disconnect_snapshots_mic_stats_before_cleanup(monkeypatch):
    session = RingSession(name_keyword="Ringo", timeout_s=1.0)
    session.target_name = "Ringo Mac"
    session.target_address = "COREBLUETOOTH-ID"
    session.mic_active = True
    session.battery_pct = 72
    session.battery_mv = 3910
    session.device_info = SimpleNamespace(fw_version="1.2.3", hw_rev=4)
    session.mic = SimpleNamespace(
        output_path="/tmp/ring_audio.wav",
        stats=SimpleNamespace(
            packet_count=123,
            frame_count=45,
            dropped_packet_count=2,
            dropped_frame_count=3,
            flat_frame_count=4,
        ),
        _buffer=[b"a", b"b"],
        _assembler=SimpleNamespace(
            incomplete_notify_packets=5,
            inflight_frame_count=1,
            completed_frames=45,
            repeated_completed_seq_packets=0,
            last_frame_seq=44,
            last_frag_idx=2,
            last_frag_count=3,
        ),
        _frame_seq=SimpleNamespace(
            stats=SimpleNamespace(
                gap_events=1,
                missing_count=2,
                duplicate_count=0,
                out_of_order_count=0,
            )
        ),
    )
    cleanup_saw_snapshot = []
    monkeypatch.setattr(
        session,
        "_drop_local_streams",
        lambda: cleanup_saw_snapshot.append(session.last_disconnect_diagnostics),
    )

    session._on_ble_disconnected(SimpleNamespace(mtu_size=185))

    snapshot = session.last_disconnect_diagnostics
    assert cleanup_saw_snapshot == [snapshot]
    assert "mtu=185" in snapshot
    assert "fw=1.2.3" in snapshot
    assert "sdk_packets=123" in snapshot
    assert "missing_blocks=2" in snapshot
    assert "last_frag=2/3" in snapshot
    assert "capture=/tmp/ring_audio.wav" in snapshot


def test_macos_12_uses_ring_service_filter_for_corebluetooth(monkeypatch):
    options = {}

    class FakeScanner:
        def __init__(self, **kwargs):
            options.update(kwargs)

        async def start(self):
            return None

        async def stop(self):
            return None

    async def no_wait(_timeout):
        return None

    monkeypatch.setattr(control, "BleakScanner", FakeScanner)
    monkeypatch.setattr(control.asyncio, "sleep", no_wait)
    monkeypatch.setattr(control.sys, "platform", "darwin")
    monkeypatch.setattr(
        control.platform,
        "mac_ver",
        lambda: ("12.2.1", ("", "", ""), ""),
    )

    asyncio.run(control.scan_all_devices(0.01))

    assert options["service_uuids"] == [control.NUS_SERVICE_UUID]
