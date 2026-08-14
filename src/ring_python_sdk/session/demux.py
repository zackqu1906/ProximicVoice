"""Notify demux: route NUS TX packets to processors / status parsers."""

from __future__ import annotations

from ring_python_sdk.core.battery_status import format_battery, parse_battery_status
from ring_python_sdk.core.constants import (
    CMD_BATTERY,
    CMD_BLE_TEST,
    CMD_BUTTON,
    CMD_HID,
    CMD_IMU,
    CMD_INFO,
    CMD_MAC,
    CMD_MIC,
    CMD_PCBA,
    CMD_PPG,
    CMD_RAISE_TO_WAKE,
    CMD_SHIPMODE,
    CMD_SWIPE,
    SUBCMD_HID_STATUS,
    SUBCMD_RAISE_TO_WAKE_STATUS,
)
from ring_python_sdk.core.device_info import format_device_info, parse_info_status
from ring_python_sdk.core.mac_status import format_mac, parse_mac_status
from ring_python_sdk.core.pcba_status import format_pcba_status_line, parse_pcba_status
from ring_python_sdk.core.shipmode import format_shipmode_result, parse_shipmode_result


class DemuxMixin:
    def _demux(self, sender: int, data: bytearray) -> None:
        if len(data) < 1:
            return
        cmd = data[0]
        if cmd == CMD_MIC and self.mic is not None:
            before = self.mic.stats.frame_count
            self.mic.handle_notification(sender, data)
            delta = self.mic.stats.frame_count - before
            if delta:
                self.rates.record("mic", delta)
        elif cmd == CMD_IMU and self.imu is not None:
            before = self.imu.stats.sample_count
            self.imu.handle_notification(sender, data)
            delta = self.imu.stats.sample_count - before
            if delta:
                self.rates.record("imu", delta)
        elif cmd == CMD_PPG and self.ppg is not None:
            before = self.ppg.stats.sample_count
            self.ppg.handle_notification(sender, data)
            delta = self.ppg.stats.sample_count - before
            if delta:
                self.rates.record("ppg", delta)
        elif cmd == CMD_SWIPE and self.swipe is not None and self.swipe_active:
            before = (
                self.swipe.stats.event_count + self.swipe.stats.trigger_count
            )
            self.swipe.handle_notification(sender, data)
            after = (
                self.swipe.stats.event_count + self.swipe.stats.trigger_count
            )
            delta = after - before
            if delta:
                self.rates.record("swipe", delta)
        elif cmd == CMD_BUTTON and self.button is not None and self.button_active:
            before = self.button.stats.event_count
            self.button.handle_notification(sender, data)
            delta = self.button.stats.event_count - before
            if delta:
                self.rates.record("button", delta)
        elif (
            cmd == CMD_RAISE_TO_WAKE
            and self.raise_to_wake is not None
            and self.raise_to_wake_active
            and len(data) >= 2
            and data[1] != SUBCMD_RAISE_TO_WAKE_STATUS
        ):
            before = self.raise_to_wake.stats.event_count
            self.raise_to_wake.handle_notification(sender, data)
            delta = self.raise_to_wake.stats.event_count - before
            if delta:
                self.rates.record("r2w", delta)
        elif cmd == CMD_RAISE_TO_WAKE and len(data) >= 3:
            if data[1] == SUBCMD_RAISE_TO_WAKE_STATUS:
                self.raise_to_wake_enabled = data[2] != 0
                self.emit_live(
                    f"r2w status: {'on' if self.raise_to_wake_enabled else 'off'}"
                )
        elif cmd == CMD_HID and len(data) >= 3:
            if data[1] == SUBCMD_HID_STATUS:
                self.hid_enabled = data[2] != 0
                self.emit_live(
                    f"hid status: {'on' if self.hid_enabled else 'off'}"
                )
        elif cmd == CMD_BLE_TEST and self.ble_test is not None:
            before = self.ble_test.stats.valid_packet_count
            self.ble_test.handle_notification(sender, data)
            delta = self.ble_test.stats.valid_packet_count - before
            if delta:
                self.rates.record("ble_test", delta)
        elif cmd == CMD_BATTERY:
            st = parse_battery_status(data)
            if st is not None:
                self.battery_pct = st.battery_pct
                self.battery_mv = st.battery_mv
                self.charge_status = st.charge_status
                self.emit_live(format_battery(st))
        elif cmd == CMD_INFO:
            info = parse_info_status(data)
            if info is not None:
                self.device_info = info
                self.emit_live(format_device_info(info))
        elif cmd == CMD_MAC:
            st = parse_mac_status(data)
            if st is not None:
                self.mac_status = st
                self.mac_str = st.mac_str
                self.mac_addr_type = st.addr_type
                self.emit_live(format_mac(st))
        elif cmd == CMD_SHIPMODE:
            st = parse_shipmode_result(data)
            if st is not None:
                self.shipmode_last_ok = st.ok
                self.shipmode_last_err = st.err_code
                self.emit_live(format_shipmode_result(st))
        elif cmd == CMD_PCBA:
            st = parse_pcba_status(data)
            if st is not None:
                self.emit_live(format_pcba_status_line(st))
