"""BLE scan / connect / disconnect / reconnect / device queries."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from bleak import BleakClient, BleakError, BleakScanner

from ring_python_sdk.ble import (
    ensure_nus_characteristics,
    find_ring,
    scan_all_devices,
    scan_rings,
    send_battery_get,
    send_hid_get,
    send_hid_set,
    send_info_get,
    send_mac_get,
    send_raise_to_wake_get,
    send_raise_to_wake_set,
    send_reboot,
    send_shipmode_enter,
    send_temperature_get,
    send_time_get,
    send_time_set,
)
from ring_python_sdk.core.constants import BATTERY_POLL_INTERVAL_S
from ring_python_sdk.core.data_paths import MODE_SESSION, new_session_dir
from ring_python_sdk.core.temperature import TemperatureStatus
from ring_python_sdk.core.time_sync import TimeStatus


class ConnectionMixin:
    async def connect(self) -> bool:
        """Try connect by name keyword (first match). Does not refresh scan list."""
        target = await find_ring(self.name_keyword, self.timeout_s)
        if target is None:
            print(f"No device matching {self.name_keyword!r}.")
            return False
        return await self._connect_device(target, new_session=True)

    async def scan(self, *, quiet: bool = False, all_devices: bool = False) -> list[Any]:
        if all_devices:
            self.scanned = [
                item.device for item in await scan_all_devices(self.timeout_s)
            ]
        else:
            self.scanned = await scan_rings(self.name_keyword, self.timeout_s)
        if not self.scanned:
            if not quiet:
                if all_devices:
                    print("No BLE devices found.")
                else:
                    print(f"No devices matching {self.name_keyword!r}.")
            return []
        if not quiet:
            print(f"Found {len(self.scanned)} device(s):")
            for i, dev in enumerate(self.scanned):
                print(f"  [{i}] {dev.name or '?'}  {dev.address}")
            print("Use: connect <index|name|address>")
        return list(self.scanned)

    def list_scanned(self) -> None:
        if not self.scanned:
            print("No scan results. Run: scan")
            return
        for i, dev in enumerate(self.scanned):
            mark = "*" if dev.address == self.target_address else " "
            print(f" {mark}[{i}] {dev.name or '?'}  {dev.address}")

    async def connect_target(self, selector: str) -> bool:
        """Switch to one ring (disconnects current). selector: index / name / address."""
        sel = selector.strip()
        # The Windows product UI already has an exact MAC address.  Stop as
        # soon as that advertiser is seen instead of collecting every nearby
        # BLE device for the full general-purpose scan timeout.  Scan and
        # connect still happen in this same asyncio/WinRT thread.
        if not self.scanned and ":" in sel:
            targeted_timeout = min(self.timeout_s, 3.0)
            print(
                f"Scanning for selected device {sel} "
                f"(up to {targeted_timeout:.1f}s) ..."
            )
            target = await BleakScanner.find_device_by_address(
                sel, timeout=targeted_timeout
            )
            if target is None:
                print(f"No match for {selector!r}. Try scanning again.")
                return False
            self.scanned = [target]
            print(f"Selected device found: {target.name!r} ({target.address})")
            return await self._connect_device(target, new_session=True)

        if not self.scanned:
            await self.scan(all_devices=True)
        if not self.scanned:
            return False

        target = None
        if sel.isdigit():
            idx = int(sel)
            if idx < 0 or idx >= len(self.scanned):
                print(f"index out of range 0..{len(self.scanned) - 1}")
                return False
            target = self.scanned[idx]
        else:
            low = sel.lower()
            for dev in self.scanned:
                name = (dev.name or "").lower()
                if low == dev.address.lower() or low in name:
                    target = dev
                    break
            if target is None:
                # Refresh without a name filter. macOS identifiers are opaque
                # UUID strings and custom firmware may use a non-Ringo name.
                matches = [
                    item.device for item in await scan_all_devices(self.timeout_s)
                ]
                for dev in matches:
                    if sel.lower() == dev.address.lower() or (
                        dev.name and sel.lower() in dev.name.lower()
                    ):
                        target = dev
                        self.scanned = matches
                        break
        if target is None:
            print(f"No match for {selector!r}. Run: scan")
            return False

        print(f"Switching to {target.name!r} ({target.address}) ...")
        return await self._connect_device(target, new_session=True)

    async def connect_device(self, target: Any) -> bool:
        """Connect a BLEDevice returned by discovery without scanning again."""
        if target is None:
            return False
        print(
            "Using selected scan result: "
            f"{getattr(target, 'name', None)!r} "
            f"({getattr(target, 'address', '?')})"
        )
        return await self._connect_device(target, new_session=True)

    async def _connect_device(self, target: Any, *, new_session: bool) -> bool:
        """Connect to exactly one device; drop any previous link first."""
        async with self._connect_lock:
            await self._detach_client(send_stops=True)

            self.target_name = target.name or ""
            self.target_address = target.address
            if new_session or self.session_dir is None:
                self.session_dir = new_session_dir(MODE_SESSION)
                self._seg.clear()
                print(f"Session dir: {self.session_dir}")

            print(f"Connecting {self.target_name!r} ({self.target_address}) ...")
            # Windows occasionally returns a transient WinRT E_FAIL while
            # opening GATT immediately after discovery. Retry this user-
            # initiated handshake once; this is not background auto-reconnect.
            for attempt in range(2):
                self.client = BleakClient(
                    target,
                    timeout=self.timeout_s,
                    disconnected_callback=self._on_ble_disconnected,
                )
                try:
                    await self.client.connect()
                    break
                except Exception as exc:
                    self.client = None
                    if attempt == 0:
                        print(f"Connect attempt failed: {exc}; retrying once ...")
                        await asyncio.sleep(0.75)
                        continue
                    print(f"Connect failed after retry: {exc}")
                    return False

            if not self.client.is_connected:
                print("Connect failed.")
                self.client = None
                return False

            print("Connected successfully.")
            print(f"MTU: {self.client.mtu_size}")
            try:
                self.tx_uuid, self.rx_uuid = ensure_nus_characteristics(self.client)
            except Exception as exc:
                print(f"NUS setup failed: {exc}")
                await self._detach_client(send_stops=False)
                return False

            await self.client.start_notify(self.tx_uuid, self._demux)
            self.reconnecting = False
            self._was_connected = True
            await self.sync_time(timeout_s=min(self.timeout_s, 2.0))
            await self._ensure_button_capture()
            await self._ensure_raise_to_wake_capture()
            await self._start_battery_poll()
            await self.query_info()
            print(f"IMU chip: {self.imu_chip}")
            return True

    async def ensure_connected(self) -> bool:
        """If auto-reconnect enabled and link is down, try to reconnect to preferred address."""
        if self._user_closing:
            return False
        if self.client is not None and self.client.is_connected:
            self.reconnecting = False
            return True
        if not self.auto_reconnect:
            return False
        if not self.target_address:
            return False

        async with self._connect_lock:
            if self.client is not None and self.client.is_connected:
                self.reconnecting = False
                return True
            self.reconnecting = True
            try:
                client = BleakClient(
                    self.target_address,
                    timeout=self.timeout_s,
                    disconnected_callback=self._on_ble_disconnected,
                )
                await client.connect()
                if not client.is_connected:
                    return False
                self.client = client
                self.tx_uuid, self.rx_uuid = ensure_nus_characteristics(self.client)
                await self.client.start_notify(self.tx_uuid, self._demux)
                self.reconnecting = False
                self._was_connected = True
                await self.sync_time(timeout_s=min(self.timeout_s, 2.0))
                await self._ensure_button_capture()
                await self._ensure_raise_to_wake_capture()
                await self._start_battery_poll()
                await self.query_info()
                print(f"Reconnected: {self.target_name} ({self.target_address})")
                return True
            except Exception:
                pass

            try:
                matches = [
                    item.device
                    for item in await scan_all_devices(min(self.timeout_s, 3.0))
                ]
                for dev in matches:
                    if dev.address.lower() == self.target_address.lower() or (
                        self.target_name
                        and dev.name
                        and self.target_name.lower() in (dev.name or "").lower()
                    ):
                        # nested connect without re-entering lock: inline attach
                        await self._detach_client(send_stops=False)
                        self.target_name = dev.name or self.target_name
                        self.target_address = dev.address
                        self.client = BleakClient(
                            dev,
                            timeout=self.timeout_s,
                            disconnected_callback=self._on_ble_disconnected,
                        )
                        await self.client.connect()
                        if not self.client.is_connected:
                            self.client = None
                            return False
                        self.tx_uuid, self.rx_uuid = ensure_nus_characteristics(
                            self.client
                        )
                        await self.client.start_notify(self.tx_uuid, self._demux)
                        self.reconnecting = False
                        self._was_connected = True
                        await self.sync_time(timeout_s=min(self.timeout_s, 2.0))
                        await self._ensure_button_capture()
                        await self._ensure_raise_to_wake_capture()
                        await self._start_battery_poll()
                        await self.query_info()
                        print(
                            f"Reconnected: {self.target_name} ({self.target_address})"
                        )
                        return True
            except Exception as exc:
                print(f"reconnect scan failed: {exc}")
            return False

    def _on_ble_disconnected(self, _client: BleakClient) -> None:
        self._was_connected = False
        self._stop_battery_poll()
        if self._user_closing:
            return
        self._drop_local_streams()
        if self.auto_reconnect and self.target_address:
            self.reconnecting = True

    def _drop_local_streams(self) -> None:
        """Close processors after unexpected link loss (no BLE STOP)."""
        if self.mic is not None:
            try:
                self.mic.close()
            except Exception:
                pass
            self.mic = None
        if self.imu is not None:
            try:
                self.imu.close()
            except Exception:
                pass
            self.imu = None
        if self.ppg is not None:
            try:
                self.ppg.close()
            except Exception:
                pass
            self.ppg = None
        if self.swipe is not None:
            try:
                self.swipe.close()
            except Exception:
                pass
            self.swipe = None
        if self.button is not None:
            try:
                self.button.close()
            except Exception:
                pass
            self.button = None
        if self.raise_to_wake is not None:
            try:
                self.raise_to_wake.close()
            except Exception:
                pass
            self.raise_to_wake = None
        self.ble_test = None
        self.mic_active = False
        self.imu_active = False
        self.ppg_active = False
        self.ppg_mode = ""
        self.ppg_send_raw = False
        self.swipe_active = False
        self.button_active = False
        self.raise_to_wake_active = False
        self.ble_test_active = False

    async def query_battery(self) -> None:
        """Send BATTERY GET once (reply updates battery_* via _demux)."""
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return
        try:
            await send_battery_get(self.client, self.rx_uuid)
        except Exception as exc:
            print(f"battery query failed: {exc}")

    async def query_temperature(
        self, timeout_s: float = 2.0
    ) -> TemperatureStatus | None:
        """Read one GXT310W0 sample and return its STATUS, or ``None`` on timeout."""
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return None
        self._temperature_event.clear()
        self.temperature_status = None
        self.temperature_mc = None
        self.temperature_c = None
        try:
            await send_temperature_get(self.client, self.rx_uuid)
            await asyncio.wait_for(self._temperature_event.wait(), timeout_s)
        except TimeoutError:
            print("temperature query timed out")
            return None
        except Exception as exc:
            print(f"temperature query failed: {exc}")
            return None
        return self.temperature_status

    async def sync_time(
        self, unix_ms: int | None = None, timeout_s: float = 2.0
    ) -> TimeStatus | None:
        """Set UTC milliseconds and return the ring's resulting TIME STATUS."""
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return None
        if unix_ms is None:
            unix_ms = time.time_ns() // 1_000_000
        self._time_event.clear()
        self.time_status = None
        try:
            await send_time_set(self.client, self.rx_uuid, unix_ms)
            await asyncio.wait_for(self._time_event.wait(), timeout_s)
        except TimeoutError:
            print("time sync timed out")
            return None
        except Exception as exc:
            print(f"time sync failed: {exc}")
            return None
        return self.time_status

    async def query_time(self, timeout_s: float = 2.0) -> TimeStatus | None:
        """Query the current UTC/uptime anchor from the ring."""
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return None
        self._time_event.clear()
        self.time_status = None
        try:
            await send_time_get(self.client, self.rx_uuid)
            await asyncio.wait_for(self._time_event.wait(), timeout_s)
        except TimeoutError:
            print("time query timed out")
            return None
        except Exception as exc:
            print(f"time query failed: {exc}")
            return None
        return self.time_status

    async def query_info(self) -> None:
        """Send INFO GET once (reply updates device_info via _demux)."""
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return
        try:
            await send_info_get(self.client, self.rx_uuid)
        except Exception as exc:
            print(f"info query failed: {exc}")

    async def query_mac(self) -> None:
        """Send MAC GET once (reply updates mac_* via _demux)."""
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return
        try:
            await send_mac_get(self.client, self.rx_uuid)
        except Exception as exc:
            print(f"mac query failed: {exc}")

    async def enter_shipmode(self) -> None:
        """Send SHIPMODE ENTER (success usually powers the ring off)."""
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            print("shipmode skipped (not connected)")
            return
        try:
            await send_shipmode_enter(self.client, self.rx_uuid)
        except Exception as exc:
            print(f"shipmode enter failed: {exc}")

    async def reboot(self) -> None:
        """Send REBOOT ENTER (success usually disconnects BLE)."""
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            print("reboot skipped (not connected)")
            return
        try:
            await send_reboot(self.client, self.rx_uuid)
        except Exception as exc:
            print(f"reboot enter failed: {exc}")

    async def set_raise_to_wake(self, enabled: bool) -> None:
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return
        try:
            await send_raise_to_wake_set(self.client, self.rx_uuid, enabled)
        except Exception as exc:
            print(f"r2w set failed: {exc}")

    async def query_raise_to_wake(self) -> None:
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return
        try:
            await send_raise_to_wake_get(self.client, self.rx_uuid)
        except Exception as exc:
            print(f"r2w query failed: {exc}")

    async def set_hid(self, enabled: bool) -> None:
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return
        try:
            await send_hid_set(self.client, self.rx_uuid, enabled)
        except Exception as exc:
            print(f"hid set failed: {exc}")

    async def query_hid(self) -> None:
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            return
        try:
            await send_hid_get(self.client, self.rx_uuid)
        except Exception as exc:
            print(f"hid query failed: {exc}")

    def _stop_battery_poll(self) -> None:
        task = self._battery_task
        self._battery_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _start_battery_poll(self) -> None:
        """Query immediately, then every BATTERY_POLL_INTERVAL_S while connected."""
        self._stop_battery_poll()
        await self.query_battery()

        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(BATTERY_POLL_INTERVAL_S)
                    await self.query_battery()
            except asyncio.CancelledError:
                raise

        self._battery_task = asyncio.create_task(_loop())

    async def _detach_client(self, *, send_stops: bool) -> None:
        self._stop_battery_poll()
        if send_stops and self.client is not None and self.client.is_connected:
            try:
                await self.stop_all()
            except Exception as exc:
                print(f"stop_all during detach: {exc}")
                self._drop_local_streams()
        else:
            self._drop_local_streams()

        if self.client is not None:
            if self.client.is_connected:
                try:
                    if self.tx_uuid:
                        await self.client.stop_notify(self.tx_uuid)
                except BleakError:
                    pass
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.client = None
        self.tx_uuid = ""
        self.rx_uuid = ""

    def set_auto_reconnect(self, on: bool) -> None:
        self.auto_reconnect = on
        print(f"auto-reconnect {'on' if on else 'off'}")
        if not on:
            self.reconnecting = False

    async def disconnect_link(self) -> None:
        """User-requested disconnect; no auto-reconnect until next connect."""
        self._user_closing = True
        self.reconnecting = False
        try:
            await self._detach_client(send_stops=True)
            self.target_address = ""
            self.target_name = ""
            self.battery_pct = None
            self.battery_mv = None
            self.charge_status = None
            self.device_info = None
            self.mac_status = None
            self.mac_str = None
            self.mac_addr_type = None
            self.shipmode_last_ok = None
            self.shipmode_last_err = None
            self.reboot_last_ok = None
            self.reboot_last_err = None
            print("Disconnected.")
        finally:
            self._user_closing = False

    async def disconnect(self) -> None:
        """App shutdown: stop sensors, drop link, disable reconnect."""
        self._user_closing = True
        self.auto_reconnect = False
        self.reconnecting = False
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        await self._detach_client(send_stops=True)
        self._close_plots()
        print("Disconnected.")

    def _close_plots(self) -> None:
        if self.imu_plot is not None:
            self.imu_plot.close()
            self.imu_plot = None
        if self.audio_plot is not None:
            self.audio_plot.close()
            self.audio_plot = None
        self.imu_plot_enabled = False
        self.audio_plot_enabled = False

    async def stop_all(self) -> None:
        if self.mic_active:
            await self.mic_off()
        if self.imu_active:
            await self.imu_off()
        if self.ppg_active:
            await self.ppg_off()
        if self.swipe_active:
            await self.swipe_off()
        if self.button_active:
            await self.button_off()
        if self.raise_to_wake_active:
            await self.raise_to_wake_off()
        if self.ble_test_active:
            await self.bletest_off()

    def refresh_plots(self) -> None:
        if self.imu_plot_enabled and self.imu_plot is not None:
            if not self.imu_plot.refresh():
                self.imu_plot_enabled = False
        if self.audio_plot_enabled and self.audio_plot is not None:
            if not self.audio_plot.refresh():
                self.audio_plot_enabled = False
