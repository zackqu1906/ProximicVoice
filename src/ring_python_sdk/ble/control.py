"""BLE scan, NUS helpers, and sensor control writes."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Any

from bleak import BleakClient, BleakError, BleakScanner

from ring_python_sdk.core.constants import (
    BLE_TEST_START_PACKET_LEN,
    CMD_BLE_TEST,
    CMD_IMU,
    CMD_LED,
    CMD_MIC,
    CMD_PPG,
    CMD_SWIPE,
    DEFAULT_IMU_ACCEL_FS_G,
    DEFAULT_IMU_ACCEL_HZ,
    DEFAULT_IMU_FRAMES_PER_PACKET,
    DEFAULT_IMU_GYRO_FS_DPS,
    DEFAULT_IMU_GYRO_HZ,
    DEFAULT_LED_BLINK_MS,
    DEFAULT_LED_BRIGHTNESS,
    IMU_ENCODE_RAW,
    IMU_ENCODE_TOKEN,
    IMU_START_PACKET_LEN_WITH_ENCODE,
    LED_BLINK_PACKET_LEN,
    LED_MODE_PACKET_LEN,
    LED_SET_PACKET_LEN,
    MIC_ENCODE_OPUS,
    MIC_START_PACKET_LEN,
    NUS_RX_CHAR_UUID,
    NUS_SERVICE_UUID,
    NUS_TX_CHAR_UUID,
    PPG_START_PACKET_LEN,
    SUBCMD_BLE_TEST_START,
    SUBCMD_BLE_TEST_STOP,
    SUBCMD_IMU_START,
    SUBCMD_IMU_STOP,
    SUBCMD_LED_BLINK,
    SUBCMD_LED_MODE,
    SUBCMD_LED_SET,
    SUBCMD_MIC_START,
    SUBCMD_MIC_STOP,
    SUBCMD_PPG_START,
    SUBCMD_PPG_STOP,
    SUBCMD_PPG_WEAR_CALIBRATE,
    SUBCMD_PPG_WEAR_CALIBRATION_GET,
    SUBCMD_SWIPE_START,
    SUBCMD_SWIPE_STOP,
    build_hid_get,
    build_hid_set,
    build_raise_to_wake_get,
    build_raise_to_wake_set,
    build_power_mode_set,
    build_power_mute_get,
    build_power_mute_set,
)
from ring_python_sdk.core.battery_status import build_battery_get
from ring_python_sdk.core.device_info import build_info_get
from ring_python_sdk.core.mac_status import build_mac_get
from ring_python_sdk.core.pcba_status import build_pcba_status_get
from ring_python_sdk.core.shipmode import build_shipmode_enter


@dataclass(frozen=True)
class DiscoveredBLEDevice:
    """A platform-neutral BLE scan result suitable for UI presentation."""

    device: Any
    name: str
    identifier: str
    rssi: int | None = None


async def scan_all_devices(timeout: float) -> list[DiscoveredBLEDevice]:
    """Return every nearby BLE device without applying a name filter.

    On Windows ``identifier`` is normally a MAC address.  On macOS Bleak uses
    CoreBluetooth identifiers, which are UUID strings and must be preserved as
    opaque values.
    """

    print(f"Scanning BLE devices for {timeout:.1f}s ...")
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    if isinstance(discovered, dict):
        items = discovered.values()
    else:
        items = [(device, None) for device in discovered]

    by_identifier: dict[str, DiscoveredBLEDevice] = {}
    for item in items:
        if isinstance(item, tuple):
            device, advertisement = item
        else:
            device, advertisement = item, None
        identifier = str(getattr(device, "address", "") or "").strip()
        if not identifier:
            continue
        name = str(
            getattr(device, "name", None)
            or (getattr(advertisement, "local_name", None) if advertisement else None)
            or ""
        ).strip()
        raw_rssi = getattr(advertisement, "rssi", None) if advertisement else None
        try:
            rssi = int(raw_rssi) if raw_rssi is not None else None
        except (TypeError, ValueError):
            rssi = None
        by_identifier[identifier.casefold()] = DiscoveredBLEDevice(
            device=device,
            name=name,
            identifier=identifier,
            rssi=rssi,
        )

    results = list(by_identifier.values())
    results.sort(
        key=lambda item: (
            not bool(item.name),
            item.name.casefold(),
            item.identifier.casefold(),
        )
    )
    return results


async def find_ring(name_keyword: str, timeout: float):
    matches = await scan_rings(name_keyword, timeout)
    return matches[0] if matches else None


async def scan_rings(name_keyword: str, timeout: float):
    """Scan and return all devices whose name contains name_keyword (case-insensitive)."""
    keyword = name_keyword.casefold()
    return [
        item.device
        for item in await scan_all_devices(timeout)
        if item.name and keyword in item.name.casefold()
    ]


def ensure_nus_characteristics(client: BleakClient) -> tuple[str, str]:
    if client.services is None:
        raise RuntimeError("BLE services not discovered.")

    service = client.services.get_service(NUS_SERVICE_UUID)
    if service is None:
        raise RuntimeError("NUS service not found on ring.")

    tx_uuid = NUS_TX_CHAR_UUID
    rx_uuid = NUS_RX_CHAR_UUID
    tx_char = client.services.get_characteristic(tx_uuid)
    rx_char = client.services.get_characteristic(rx_uuid)
    if tx_char is None or rx_char is None:
        raise RuntimeError(
            "NUS characteristics not found. "
            "Please confirm firmware enabled NUS service."
        )
    return tx_uuid, rx_uuid


async def send_mic_control(
    client: BleakClient,
    rx_uuid: str,
    on: bool,
    *,
    encode: int = MIC_ENCODE_OPUS,
) -> None:
    if not client.is_connected:
        print(f"mic {'ON' if on else 'OFF'} skipped (BLE disconnected)")
        return

    try:
        if on:
            packet = bytes([CMD_MIC, SUBCMD_MIC_START, encode & 0xFF])
            if len(packet) != MIC_START_PACKET_LEN:
                raise RuntimeError(
                    f"mic start packet length mismatch: {len(packet)}"
                )
            await client.write_gatt_char(rx_uuid, packet, response=False)
            print(f"mic ON command sent (encode={encode})")
        else:
            packet = bytes([CMD_MIC, SUBCMD_MIC_STOP])
            await client.write_gatt_char(rx_uuid, packet, response=False)
            print("mic OFF command sent")
    except BleakError as exc:
        print(f"mic {'ON' if on else 'OFF'} failed: {exc}")


async def send_imu_start(
    client: BleakClient,
    rx_uuid: str,
    gyro_hz: int = DEFAULT_IMU_GYRO_HZ,
    accel_hz: int = DEFAULT_IMU_ACCEL_HZ,
    gyro_fs_dps: int = DEFAULT_IMU_GYRO_FS_DPS,
    accel_fs_g: int = DEFAULT_IMU_ACCEL_FS_G,
    frames_per_packet: int = DEFAULT_IMU_FRAMES_PER_PACKET,
    encode_mode: int = IMU_ENCODE_RAW,
) -> None:
    payload = struct.pack(
        "<HHHBBB",
        gyro_hz & 0xFFFF,
        accel_hz & 0xFFFF,
        gyro_fs_dps & 0xFFFF,
        accel_fs_g & 0xFF,
        frames_per_packet & 0xFF,
        encode_mode & 0xFF,
    )
    packet = bytes([CMD_IMU, SUBCMD_IMU_START]) + payload
    if len(packet) != IMU_START_PACKET_LEN_WITH_ENCODE:
        raise RuntimeError(f"imu start packet length mismatch: {len(packet)}")
    await client.write_gatt_char(rx_uuid, packet, response=False)
    encode_name = "token" if encode_mode == IMU_ENCODE_TOKEN else "raw"
    print(
        "imu START sent: "
        f"gyro={gyro_hz}Hz accel={accel_hz}Hz "
        f"gyro_fs={gyro_fs_dps}dps accel_fs={accel_fs_g}g "
        f"frames_per_packet={frames_per_packet} encode={encode_name}"
    )


async def send_imu_stop(client: BleakClient, rx_uuid: str) -> None:
    packet = bytes([CMD_IMU, SUBCMD_IMU_STOP])
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print("imu STOP command sent")


async def send_ble_test_start(
    client: BleakClient,
    rx_uuid: str,
    payload_size: int,
    pps: int,
    duration_s: int,
    packet_count: int,
) -> None:
    payload = struct.pack(
        "<HHHI",
        payload_size & 0xFFFF,
        pps & 0xFFFF,
        duration_s & 0xFFFF,
        packet_count & 0xFFFFFFFF,
    )
    packet = bytes([CMD_BLE_TEST, SUBCMD_BLE_TEST_START]) + payload
    if len(packet) != BLE_TEST_START_PACKET_LEN:
        raise RuntimeError(f"ble test start packet length mismatch: {len(packet)}")
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print(
        "ble test START sent: "
        f"payload={payload_size} pps={pps} duration_s={duration_s} "
        f"packet_count={packet_count}"
    )


async def send_ble_test_stop(client: BleakClient, rx_uuid: str) -> None:
    packet = bytes([CMD_BLE_TEST, SUBCMD_BLE_TEST_STOP])
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print("ble test STOP command sent")


async def send_ppg_start(client: BleakClient, rx_uuid: str, mode: int) -> None:
    if not client.is_connected:
        print("ppg START skipped (BLE disconnected)")
        return

    packet = bytes([CMD_PPG, SUBCMD_PPG_START, mode & 0xFF])
    if len(packet) != PPG_START_PACKET_LEN:
        raise RuntimeError(f"ppg start packet length mismatch: {len(packet)}")
    await client.write_gatt_char(rx_uuid, packet, response=False)
    label = {0: "HRS", 1: "SpO2", 2: "Wear"}.get(mode, str(mode))
    print(f"ppg START sent (mode={label})")


async def send_ppg_stop(client: BleakClient, rx_uuid: str) -> None:
    packet = bytes([CMD_PPG, SUBCMD_PPG_STOP])
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print("ppg STOP command sent")


async def send_wear_calibrate(client: BleakClient, rx_uuid: str) -> None:
    packet = bytes([CMD_PPG, SUBCMD_PPG_WEAR_CALIBRATE])
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print("wear calibration command sent")


async def send_wear_calibration_get(client: BleakClient, rx_uuid: str) -> None:
    packet = bytes([CMD_PPG, SUBCMD_PPG_WEAR_CALIBRATION_GET])
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print("wear calibration query sent")


async def send_swipe_start(client: BleakClient, rx_uuid: str) -> None:
    if not client.is_connected:
        print("swipe START skipped (BLE disconnected)")
        return
    packet = bytes([CMD_SWIPE, SUBCMD_SWIPE_START])
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print("swipe START sent")


async def send_swipe_stop(client: BleakClient, rx_uuid: str) -> None:
    if not client.is_connected:
        print("swipe STOP skipped (BLE disconnected)")
        return
    packet = bytes([CMD_SWIPE, SUBCMD_SWIPE_STOP])
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print("swipe STOP sent")


async def send_pcba_status_get(client: BleakClient, rx_uuid: str) -> None:
    """Request PCBA STATUS (factory snapshot; still includes battery fields)."""
    if not client.is_connected:
        return
    await client.write_gatt_char(rx_uuid, build_pcba_status_get(), response=False)


async def send_battery_get(client: BleakClient, rx_uuid: str) -> None:
    """Request dedicated BATTERY STATUS (battery_mv / battery_pct / charge)."""
    if not client.is_connected:
        return
    await client.write_gatt_char(rx_uuid, build_battery_get(), response=False)


async def send_info_get(client: BleakClient, rx_uuid: str) -> None:
    """Request INFO STATUS (firmware version + hardware component table)."""
    if not client.is_connected:
        return
    await client.write_gatt_char(rx_uuid, build_info_get(), response=False)


async def send_mac_get(client: BleakClient, rx_uuid: str) -> None:
    """Request MAC STATUS (identity addr_type + 6-byte MAC MSB-first)."""
    if not client.is_connected:
        return
    await client.write_gatt_char(rx_uuid, build_mac_get(), response=False)


async def send_shipmode_enter(client: BleakClient, rx_uuid: str) -> None:
    """Request shipping mode enter (CM1126B). Success usually powers the ring off."""
    if not client.is_connected:
        print("shipmode ENTER skipped (BLE disconnected)")
        return
    await client.write_gatt_char(rx_uuid, build_shipmode_enter(), response=False)
    print("shipmode ENTER sent (expect power-off or RESULT fail)")


async def send_led_set(client: BleakClient, rx_uuid: str, on: bool) -> None:
    if not client.is_connected:
        print("led SET skipped (BLE disconnected)")
        return
    packet = build_led_set(on)
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print(f"led SET on={int(on)} sent")


def build_led_set(on: bool) -> bytes:
    packet = bytes([CMD_LED, SUBCMD_LED_SET, 1 if on else 0])
    if len(packet) != LED_SET_PACKET_LEN:
        raise RuntimeError(f"led SET packet length mismatch: {len(packet)}")
    return packet


def build_led_blink(duration_ms: int = DEFAULT_LED_BLINK_MS) -> bytes:
    packet = bytes([CMD_LED, SUBCMD_LED_BLINK]) + struct.pack(
        "<H", duration_ms & 0xFFFF
    )
    if len(packet) != LED_BLINK_PACKET_LEN:
        raise RuntimeError(f"led BLINK packet length mismatch: {len(packet)}")
    return packet


def build_led_mode(mode: int, brightness: int, period_ms: int) -> bytes:
    packet = bytes(
        [
            CMD_LED,
            SUBCMD_LED_MODE,
            mode & 0xFF,
            brightness & 0xFF,
        ]
    ) + struct.pack("<H", period_ms & 0xFFFF)
    if len(packet) != LED_MODE_PACKET_LEN:
        raise RuntimeError(f"led MODE packet length mismatch: {len(packet)}")
    return packet


async def send_led_blink(
    client: BleakClient,
    rx_uuid: str,
    duration_ms: int = DEFAULT_LED_BLINK_MS,
) -> None:
    if not client.is_connected:
        print("led BLINK skipped (BLE disconnected)")
        return
    packet = build_led_blink(duration_ms)
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print(f"led BLINK {duration_ms} ms sent")


async def send_led_mode(
    client: BleakClient,
    rx_uuid: str,
    mode: int,
    brightness: int = DEFAULT_LED_BRIGHTNESS,
    period_ms: int = 0,
) -> None:
    if not client.is_connected:
        print("led MODE skipped (BLE disconnected)")
        return
    packet = build_led_mode(mode, brightness, period_ms)
    await client.write_gatt_char(rx_uuid, packet, response=False)
    print(f"led MODE mode={mode} bri={brightness} period={period_ms} sent")


async def send_tx_mute(client: BleakClient, rx_uuid: str, muted: bool) -> None:
    """Mute/unmute business BLE notifies (POWER 0x2C SET)."""
    if not client.is_connected:
        print(f"tx mute skipped (BLE disconnected)")
        return
    try:
        await client.write_gatt_char(
            rx_uuid, build_power_mute_set(muted), response=False
        )
        print(f"tx mute {'ON' if muted else 'OFF'} sent")
    except BleakError as exc:
        print(f"tx mute failed: {exc}")
        raise


async def send_tx_mode(client: BleakClient, rx_uuid: str, mode: int) -> None:
    """Set TX mode: 0=normal, 1=sense, 2=encode_only."""
    if not client.is_connected:
        print("tx mode skipped (BLE disconnected)")
        return
    try:
        await client.write_gatt_char(
            rx_uuid, build_power_mode_set(mode), response=False
        )
        mode_name = {0: "NORMAL", 1: "SENSE", 2: "ENCODE_ONLY"}.get(mode, str(mode))
        print(f"tx mode {mode_name} sent")
    except BleakError as exc:
        print(f"tx mode failed: {exc}")
        raise


async def send_tx_mute_get(client: BleakClient, rx_uuid: str) -> None:
    if not client.is_connected:
        return
    await client.write_gatt_char(rx_uuid, build_power_mute_get(), response=False)


async def send_raise_to_wake_set(
    client: BleakClient, rx_uuid: str, enabled: bool
) -> None:
    """Enable/disable firmware Raise-to-Wake feature (ICM45686)."""
    if not client.is_connected:
        print("r2w set skipped (BLE disconnected)")
        return
    try:
        await client.write_gatt_char(
            rx_uuid, build_raise_to_wake_set(enabled), response=False
        )
        print(f"r2w {'ON' if enabled else 'OFF'} sent")
    except BleakError as exc:
        print(f"r2w set failed: {exc}")
        raise


async def send_raise_to_wake_get(client: BleakClient, rx_uuid: str) -> None:
    if not client.is_connected:
        print("r2w get skipped (BLE disconnected)")
        return
    await client.write_gatt_char(rx_uuid, build_raise_to_wake_get(), response=False)


async def send_hid_set(client: BleakClient, rx_uuid: str, enabled: bool) -> None:
    """Enable/disable firmware HID mapping (HOGP reports)."""
    if not client.is_connected:
        print("hid set skipped (BLE disconnected)")
        return
    try:
        await client.write_gatt_char(
            rx_uuid, build_hid_set(enabled), response=False
        )
        print(f"hid {'ON' if enabled else 'OFF'} sent")
    except BleakError as exc:
        print(f"hid set failed: {exc}")
        raise


async def send_hid_get(client: BleakClient, rx_uuid: str) -> None:
    if not client.is_connected:
        print("hid get skipped (BLE disconnected)")
        return
    await client.write_gatt_char(rx_uuid, build_hid_get(), response=False)
