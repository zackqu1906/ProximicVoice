"""Notify demux: route NUS TX packets to processors / status parsers."""

from __future__ import annotations

from ring_python_sdk.core.battery_status import format_battery, parse_battery_status
from ring_python_sdk.core.temperature import parse_temperature_status
from ring_python_sdk.core.time_sync import format_time_status, parse_time_status
from ring_python_sdk.core.constants import (
    CMD_BATTERY,
    CMD_BLE_TEST,
    CMD_BUTTON,
    CMD_HID,
    CMD_IMU,
    CMD_INFO,
    CMD_IDENTITY,
    CMD_MAC,
    CMD_MIC,
    CMD_PCBA,
    CMD_PPG,
    CMD_RAISE_TO_WAKE,
    CMD_REBOOT,
    CMD_SHIPMODE,
    CMD_HEALTH,
    CMD_SWIPE,
    CMD_TEMPERATURE,
    CMD_TIME,
    MIC_RECORD_ID_LATEST,
    SUBCMD_HID_STATUS,
    SUBCMD_IMU_CALIBRATION_STATUS,
    SUBCMD_PPG_WEAR_CALIBRATION_STATUS,
    SUBCMD_RAISE_TO_WAKE_STATUS,
    SUBCMD_HEALTH_DATA,
    SUBCMD_HEALTH_LIST_END,
    SUBCMD_HEALTH_LIST_ITEM,
    SUBCMD_HEALTH_READ_END,
    SUBCMD_HEALTH_STATUS,
    SUBCMD_MIC_RECORD_DATA,
    SUBCMD_MIC_RECORD_LIST_END,
    SUBCMD_MIC_RECORD_LIST_ITEM,
    SUBCMD_MIC_RECORD_READ_END,
    SUBCMD_MIC_RECORD_STATUS,
    SUBCMD_TIME_STATUS,
)
from ring_python_sdk.core.device_info import format_device_info, parse_info_status
from ring_python_sdk.core.identity import (
    format_identity_status,
    parse_identity_error,
    parse_identity_signature,
    parse_identity_status,
)
from ring_python_sdk.core.mac_status import format_mac, parse_mac_status
from ring_python_sdk.core.mic_recording import (
    MicRecordingDataChunk,
    parse_mic_record_data,
    parse_mic_record_list_end,
    parse_mic_record_list_item,
    parse_mic_record_read_end,
    parse_mic_record_status,
)
from ring_python_sdk.core.pcba_status import format_pcba_status_line, parse_pcba_status
from ring_python_sdk.core.reboot import format_reboot_result, parse_reboot_result
from ring_python_sdk.core.shipmode import format_shipmode_result, parse_shipmode_result
from ring_python_sdk.core.health import (
    HealthDataChunk,
    format_health_status,
    parse_health_data,
    parse_health_list_end,
    parse_health_list_item,
    parse_health_read_end,
    parse_health_status,
)
from ring_python_sdk.ppg.calibration import (
    format_ppg_wear_calibration_status,
    parse_ppg_wear_calibration_status,
)
from ring_python_sdk.imu.calibration import (
    format_imu_calibration_status,
    parse_imu_calibration_status,
)


class DemuxMixin:
    def _demux(self, sender: int, data: bytearray) -> None:
        if len(data) < 1:
            return
        cmd = data[0]
        if (
            cmd == CMD_MIC
            and len(data) >= 2
            and data[1] == SUBCMD_MIC_RECORD_STATUS
        ):
            status = parse_mic_record_status(data)
            if status is not None:
                self.mic_recording_status = status
                self._mic_record_status_event.set()
        elif (
            cmd == CMD_MIC
            and len(data) >= 2
            and data[1] == SUBCMD_MIC_RECORD_LIST_ITEM
        ):
            item = parse_mic_record_list_item(data)
            if item is not None:
                self.mic_recording_latest_info = item
        elif (
            cmd == CMD_MIC
            and len(data) >= 2
            and data[1] == SUBCMD_MIC_RECORD_LIST_END
        ):
            latest_id = parse_mic_record_list_end(data)
            if latest_id is not None:
                self.mic_recording_latest_id = (
                    None if latest_id == MIC_RECORD_ID_LATEST else latest_id
                )
                if (
                    self.mic_recording_latest_info is not None
                    and self.mic_recording_latest_info.recording_id != latest_id
                ):
                    self.mic_recording_latest_info = None
                self._mic_record_list_event.set()
        elif (
            cmd == CMD_MIC
            and len(data) >= 2
            and data[1] == SUBCMD_MIC_RECORD_DATA
        ):
            chunk = parse_mic_record_data(data)
            if chunk is not None:
                if not self._mic_record_read_buf:
                    self._mic_record_read_off = chunk.offset
                self._mic_record_read_buf.extend(chunk.payload)
                self.mic_recording_last_data = MicRecordingDataChunk(
                    chunk.recording_id,
                    self._mic_record_read_off,
                    bytes(self._mic_record_read_buf),
                )
        elif (
            cmd == CMD_MIC
            and len(data) >= 2
            and data[1] == SUBCMD_MIC_RECORD_READ_END
        ):
            end = parse_mic_record_read_end(data)
            if end is not None:
                self.mic_recording_last_read_end = end
                self._mic_record_read_event.set()
        elif cmd == CMD_MIC and self.mic is not None:
            before = self.mic.stats.frame_count
            self.mic.handle_notification(sender, data)
            delta = self.mic.stats.frame_count - before
            if delta:
                self.rates.record("mic", delta)
        elif (
            cmd == CMD_IMU
            and len(data) >= 2
            and data[1] == SUBCMD_IMU_CALIBRATION_STATUS
        ):
            status = parse_imu_calibration_status(data)
            if status is not None:
                self.imu_calibration_status = status
                self.emit_live(format_imu_calibration_status(status))
        elif cmd == CMD_IMU and self.imu is not None:
            before = self.imu.stats.sample_count
            self.imu.handle_notification(sender, data)
            delta = self.imu.stats.sample_count - before
            if delta:
                self.rates.record("imu", delta)
        elif (
            cmd == CMD_PPG
            and len(data) >= 2
            and data[1] == SUBCMD_PPG_WEAR_CALIBRATION_STATUS
        ):
            status = parse_ppg_wear_calibration_status(data)
            if status is not None:
                self.ppg_calibration_status = status
                self.emit_live(format_ppg_wear_calibration_status(status))
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
        elif cmd == CMD_TEMPERATURE:
            st = parse_temperature_status(data)
            if st is not None:
                self.temperature_status = st
                self.temperature_mc = st.temperature_mc if st.ok else None
                self.temperature_c = st.temperature_c if st.ok else None
                self._temperature_event.set()
        elif cmd == CMD_TIME and len(data) >= 2 and data[1] == SUBCMD_TIME_STATUS:
            st = parse_time_status(data)
            if st is not None:
                self.time_status = st
                self.emit_live(format_time_status(st))
                self._time_event.set()
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
        elif cmd == CMD_IDENTITY and len(data) >= 2:
            status = parse_identity_status(data)
            signature = parse_identity_signature(data)
            error = parse_identity_error(data)
            if status is not None:
                self.identity_status = status
                self.emit_live(format_identity_status(status))
                self._identity_event.set()
            elif signature is not None:
                self.identity_signature = signature
                self._identity_event.set()
            elif error is not None:
                self.identity_error = error
                self.emit_live(
                    f"identity operation 0x{error.operation:02X} failed: "
                    f"{error.error_code}"
                )
                self._identity_event.set()
        elif cmd == CMD_SHIPMODE:
            st = parse_shipmode_result(data)
            if st is not None:
                self.shipmode_last_ok = st.ok
                self.shipmode_last_err = st.err_code
                self.emit_live(format_shipmode_result(st))
        elif cmd == CMD_REBOOT:
            st = parse_reboot_result(data)
            if st is not None:
                self.reboot_last_ok = st.ok
                self.reboot_last_err = st.err_code
                self.emit_live(format_reboot_result(st))
        elif cmd == CMD_HEALTH and len(data) >= 2:
            sub = data[1]
            if sub == SUBCMD_HEALTH_STATUS:
                st = parse_health_status(data)
                if st is not None:
                    self.health_status = st
                    self.emit_live(format_health_status(st))
            elif sub == SUBCMD_HEALTH_LIST_ITEM:
                item = parse_health_list_item(data)
                if item is not None:
                    self.health_sessions.append(item)
            elif sub == SUBCMD_HEALTH_LIST_END:
                count = parse_health_list_end(data)
                if count is not None:
                    self.health_sessions = self.health_sessions[:count]
                    self._health_list_event.set()
            elif sub == SUBCMD_HEALTH_DATA:
                chunk = parse_health_data(data)
                if chunk is not None:
                    if not self._health_read_buf:
                        self._health_read_off = chunk.offset
                    self._health_read_buf.extend(chunk.payload)
                    self.health_last_data = HealthDataChunk(
                        offset=self._health_read_off,
                        payload=bytes(self._health_read_buf),
                    )
            elif sub == SUBCMD_HEALTH_READ_END:
                end = parse_health_read_end(data)
                if end is not None:
                    self.health_last_read_end = end
                    self._health_read_event.set()
        elif cmd == CMD_PCBA:
            st = parse_pcba_status(data)
            if st is not None:
                self.emit_live(format_pcba_status_line(st))
