"""Plots, print flags, TUI snapshot, status/stats text, save."""

from __future__ import annotations

from ring_python_sdk.core.battery_status import format_battery
from ring_python_sdk.core.constants import (
    DEFAULT_BLE_TEST_PPS,
    DEFAULT_IMU_GYRO_HZ,
)
from ring_python_sdk.core.device_info import format_device_info
from ring_python_sdk.plots import create_audio_plot, create_imu_plot
from ring_python_sdk.ppg.processor import format_ppg_vitals_short
from ring_python_sdk.session.types import SensorSnap, SessionSnapshot


class StatusMixin:
    def _ppg_vitals_extras(self) -> str:
        if self.ppg is None or not self.ppg_active:
            return ""
        hr = self.ppg.latest_hr
        spo2 = self.ppg.latest_spo2
        wear = self.ppg.latest_wear
        if hr is None and spo2 is None and wear is None:
            return self.ppg_mode or ""
        return format_ppg_vitals_short(
            self.ppg.mode,
            hr=hr if hr is not None else 0,
            spo2=spo2 if spo2 is not None else 0,
            wear=wear if wear is not None else 0,
        )

    def plot_imu(self, on: bool) -> None:
        if on:
            if self.imu_plot is None:
                hz = DEFAULT_IMU_GYRO_HZ
                if self.imu is not None:
                    hz = max(self.imu.gyro_hz, self.imu.accel_hz)
                self.imu_plot = create_imu_plot(
                    window_seconds=self.imu_plot_window, expected_hz=hz
                )
                self.imu_plot.setup()
            if self.imu is not None:
                self.imu._live_plot = self.imu_plot
            self.imu_plot_enabled = True
            print("plot imu on")
        else:
            self.imu_plot_enabled = False
            if self.imu is not None:
                self.imu._live_plot = None
            if self.imu_plot is not None:
                self.imu_plot.close()
                self.imu_plot = None
            print("plot imu off")

    def plot_audio(self, on: bool) -> None:
        if on:
            if self.audio_plot is None:
                self.audio_plot = create_audio_plot()
                self.audio_plot.setup()
            if self.mic is not None:
                self.mic._live_plot = self.audio_plot
            self.audio_plot_enabled = True
            print("plot audio on")
        else:
            self.audio_plot_enabled = False
            if self.mic is not None:
                self.mic._live_plot = None
            if self.audio_plot is not None:
                self.audio_plot.close()
                self.audio_plot = None
            print("plot audio off")

    def set_print(self, channel: str | None, on: bool) -> None:
        channels = ("mic", "imu", "ppg", "swipe", "button")
        targets = channels if channel is None else (channel,)
        for ch in targets:
            if ch not in channels:
                print(f"unknown print channel: {ch}")
                continue
            setattr(self.print_flags, ch, on)
            if ch == "mic" and self.mic is not None:
                self.mic.print_frames = on
            if ch == "imu" and self.imu is not None:
                self.imu.print_samples = on
            if ch == "ppg" and self.ppg is not None:
                self.ppg.print_samples = on
                if on and self.ppg.log is None:
                    self.ppg.log = self.emit_live
            if ch == "swipe" and self.swipe is not None:
                self.swipe.print_events = on
                if on and self.swipe.log is None:
                    self.swipe.log = self.emit_live
            if ch == "button":
                # Always-on Log; keep status flag true regardless of print off.
                self.print_flags.button = True
        if channel is None:
            print(f"print {'on' if on else 'off'} (all)")
        else:
            print(f"print {channel} {'on' if on else 'off'}")

    def snapshot(self) -> SessionSnapshot:
        rates = self.rates.fps()
        sensors: list[SensorSnap] = [
            SensorSnap(
                name="MIC",
                active=self.mic_active,
                rate=rates["mic"],
                rate_unit="fps",
                count_label="frames",
                count=self.mic.stats.frame_count if self.mic else 0,
                packets=self.mic.stats.packet_count if self.mic else 0,
                dropped=self.mic.stats.dropped_packet_count if self.mic else 0,
                nominal_rate=10.0,
            ),
            SensorSnap(
                name="IMU",
                active=self.imu_active,
                rate=rates["imu"],
                rate_unit="Hz",
                count_label="samples",
                count=self.imu.stats.sample_count if self.imu else 0,
                packets=self.imu.stats.packet_count if self.imu else 0,
                dropped=self.imu.stats.dropped_packet_count if self.imu else 0,
                nominal_rate=float(
                    max(self.imu.gyro_hz, self.imu.accel_hz)
                    if self.imu is not None
                    else DEFAULT_IMU_GYRO_HZ
                ),
            ),
            SensorSnap(
                name="PPG",
                active=self.ppg_active,
                rate=rates["ppg"],
                rate_unit="/s",
                count_label="samples",
                count=self.ppg.stats.sample_count if self.ppg else 0,
                packets=self.ppg.stats.packet_count if self.ppg else 0,
                dropped=self.ppg.stats.dropped_packet_count if self.ppg else 0,
                nominal_rate=2.0,
                extras=self._ppg_vitals_extras(),
            ),
            SensorSnap(
                name="SWIPE",
                active=self.swipe_active,
                rate=rates["swipe"],
                rate_unit="/s",
                count_label="infer/trig",
                count=(
                    (self.swipe.stats.event_count + self.swipe.stats.trigger_count)
                    if self.swipe
                    else 0
                ),
                packets=self.swipe.stats.packet_count if self.swipe else 0,
                dropped=self.swipe.stats.dropped_packet_count if self.swipe else 0,
                nominal_rate=5.0,
            ),
            SensorSnap(
                name="BUTTON",
                active=self.button_active,
                rate=rates["button"],
                rate_unit="/s",
                count_label="events",
                count=self.button.stats.event_count if self.button else 0,
                packets=self.button.stats.packet_count if self.button else 0,
                dropped=(
                    self.button.stats.dropped_packet_count if self.button else 0
                ),
                nominal_rate=5.0,
            ),
            SensorSnap(
                name="R2W",
                active=self.raise_to_wake_active,
                rate=rates["r2w"],
                rate_unit="/s",
                count_label="events",
                count=(
                    self.raise_to_wake.stats.event_count
                    if self.raise_to_wake
                    else 0
                ),
                packets=(
                    self.raise_to_wake.stats.packet_count
                    if self.raise_to_wake
                    else 0
                ),
                dropped=(
                    self.raise_to_wake.stats.dropped_packet_count
                    if self.raise_to_wake
                    else 0
                ),
                nominal_rate=1.0,
                extras=(
                    f"last {self.raise_to_wake.last_event}"
                    if self.raise_to_wake and self.raise_to_wake.last_event
                    else "waiting"
                ),
            ),
            SensorSnap(
                name="BLETEST",
                active=self.ble_test_active,
                rate=rates["ble_test"],
                rate_unit="pps",
                count_label="packets",
                count=(
                    self.ble_test.stats.valid_packet_count if self.ble_test else 0
                ),
                packets=(
                    self.ble_test.stats.notify_count if self.ble_test else 0
                ),
                dropped=0,
                nominal_rate=float(DEFAULT_BLE_TEST_PPS),
            ),
        ]
        extras = (
            f"plot imu={'on' if self.imu_plot_enabled else 'off'} "
            f"audio={'on' if self.audio_plot_enabled else 'off'}  "
            f"chip={self.imu_chip}  "
            f"auto-reconnect={'on' if self.auto_reconnect else 'off'}"
        )
        connected = bool(self.client is not None and self.client.is_connected)
        scanned = [
            (d.name or "?", d.address) for d in self.scanned
        ]
        battery_text = format_battery(
            battery_pct=self.battery_pct,
            battery_mv=self.battery_mv,
            charge_status=self.charge_status,
        )
        info_text = format_device_info(self.device_info)
        return SessionSnapshot(
            device_name=self.target_name,
            device_address=self.target_address,
            session_dir=str(self.session_dir) if self.session_dir else "",
            connected=connected,
            reconnecting=self.reconnecting and not connected,
            auto_reconnect=self.auto_reconnect,
            imu_chip=self.imu_chip,
            plot_imu=self.imu_plot_enabled,
            plot_audio=self.audio_plot_enabled,
            sensors=sensors,
            extras=extras,
            scanned=scanned,
            battery_text=battery_text,
            battery_pct=self.battery_pct,
            battery_mv=self.battery_mv,
            charge_status=self.charge_status,
            info_text=info_text,
            hw_rev=self.device_info.hw_rev if self.device_info else None,
            fw_version=self.device_info.fw_version if self.device_info else None,
        )

    def status_text(self) -> str:
        rates = self.rates.fps()
        lines = [
            f"device: {self.target_name} ({self.target_address})",
            f"session: {self.session_dir}",
            format_battery(
                battery_pct=self.battery_pct,
                battery_mv=self.battery_mv,
                charge_status=self.charge_status,
            ),
            f"mic={'on' if self.mic_active else 'off'} "
            f"imu={'on' if self.imu_active else 'off'} "
            f"ppg={'on' if self.ppg_active else 'off'}"
            f"{('('+self.ppg_mode+')') if self.ppg_active and self.ppg_mode else ''} "
            f"swipe={'on' if self.swipe_active else 'off'} "
            f"button={'on' if self.button_active else 'off'} "
            f"r2w={'on' if self.raise_to_wake_active else 'off'} "
            f"bletest={'on' if self.ble_test_active else 'off'}",
            f"plot imu={'on' if self.imu_plot_enabled else 'off'} "
            f"audio={'on' if self.audio_plot_enabled else 'off'}",
            f"print mic={int(self.print_flags.mic)} imu={int(self.print_flags.imu)} "
            f"ppg={int(self.print_flags.ppg)} swipe={int(self.print_flags.swipe)} "
            f"button={int(self.print_flags.button)}",
            f"rates mic={rates['mic']:.1f}/s imu={rates['imu']:.1f}Hz "
            f"ppg={rates['ppg']:.1f}/s r2w={rates['r2w']:.1f}/s",
        ]
        return "\n".join(lines)

    def stats_text(self) -> str:
        rates = self.rates.fps()
        lines: list[str] = []
        if self.mic is not None:
            s = self.mic.stats
            lines.append(
                f"mic   frames={s.frame_count} packets={s.packet_count} "
                f"dropped={s.dropped_packet_count} rate={rates['mic']:.1f}/s"
            )
        elif self._seg.get("mic"):
            lines.append("mic   (idle, prior segments saved)")
        if self.imu is not None:
            s = self.imu.stats
            lines.append(
                f"imu   samples={s.sample_count} packets={s.packet_count} "
                f"raw={s.raw_packet_count} delta={s.delta_packet_count} "
                f"token={s.token_packet_count} "
                f"wire={s.wire_byte_count}B dropped={s.dropped_packet_count} "
                f"rate={rates['imu']:.1f}Hz"
            )
        if self.ppg is not None:
            s = self.ppg.stats
            vitals = self._ppg_vitals_extras()
            extra = f" {vitals}" if vitals else ""
            lines.append(
                f"ppg   mode={self.ppg.mode} samples={s.sample_count} "
                f"packets={s.packet_count} dropped={s.dropped_packet_count} "
                f"rate={rates['ppg']:.1f}/s{extra}"
            )
        if self.swipe is not None:
            s = self.swipe.stats
            lines.append(
                f"swipe infer={s.event_count} triggers={s.trigger_count} "
                f"profile={s.profile_count} packets={s.packet_count} "
                f"dropped={s.dropped_packet_count} rate={rates['swipe']:.1f}/s"
            )
        if self.button is not None:
            s = self.button.stats
            lines.append(
                f"button events={s.event_count} packets={s.packet_count} "
                f"dropped={s.dropped_packet_count} rate={rates['button']:.1f}/s"
            )
        if self.raise_to_wake is not None:
            s = self.raise_to_wake.stats
            last = self.raise_to_wake.last_event or "—"
            lines.append(
                f"r2w   events={s.event_count} packets={s.packet_count} "
                f"dropped={s.dropped_packet_count} rate={rates['r2w']:.1f}/s "
                f"last={last}"
            )
        if self.ble_test is not None:
            s = self.ble_test.stats
            lines.append(
                f"bletest packets={s.valid_packet_count} "
                f"notify={s.notify_count} rate={rates['ble_test']:.1f}/s"
            )
        if not lines:
            lines.append("(no active streams)")
        return "\n".join(lines)

    def stats_reset(self) -> None:
        self.rates.reset()
        if self.mic is not None:
            self.mic.stats = type(self.mic.stats)()
        if self.imu is not None:
            self.imu.stats = type(self.imu.stats)()
        if self.ppg is not None:
            self.ppg.stats = type(self.ppg.stats)()
        if self.swipe is not None:
            self.swipe.stats = type(self.swipe.stats)()
        if self.button is not None:
            self.button.stats = type(self.button.stats)()
        if self.raise_to_wake is not None:
            self.raise_to_wake.stats = type(self.raise_to_wake.stats)()
        print("stats reset")

    def save(self, channel: str | None = None) -> None:
        """Flush active writers; data is already on disk while streaming."""
        targets = []
        if channel is None or channel == "mic":
            if self.mic is not None:
                self.mic._flush_buffer()
                targets.append(self.mic.output_path)
        if channel is None or channel == "imu":
            if self.imu is not None and self.imu.csv_path is not None:
                if self.imu._csv_file is not None:
                    self.imu._csv_file.flush()
                targets.append(self.imu.csv_path)
        if channel is None or channel == "ppg":
            if self.ppg is not None:
                targets.append(self.ppg.csv_path)
        if channel is None or channel == "swipe":
            if self.swipe is not None:
                if self.swipe._file is not None:
                    self.swipe._file.flush()  # type: ignore[union-attr]
                if self.swipe._profile_file is not None:
                    self.swipe._profile_file.flush()  # type: ignore[union-attr]
                targets.append(self.swipe.csv_path)
                if self.swipe.profile_csv_path is not None:
                    targets.append(self.swipe.profile_csv_path)
        if channel is None or channel == "button":
            if self.button is not None:
                if self.button._file is not None:
                    self.button._file.flush()  # type: ignore[union-attr]
                targets.append(self.button.csv_path)
        if channel is None or channel in {"r2w", "raise_to_wake"}:
            if self.raise_to_wake is not None:
                if self.raise_to_wake._file is not None:
                    self.raise_to_wake._file.flush()  # type: ignore[union-attr]
                targets.append(self.raise_to_wake.csv_path)
        if not targets and self.saved_paths:
            targets = list(self.saved_paths)
        if not targets:
            print("nothing to save")
            return
        for p in targets:
            print(f"saved: {p}")
