"""Sensor start/stop controls (MIC/IMU/PPG/Swipe/Button/R2W/LED/BLE test)."""

from __future__ import annotations

from collections.abc import Callable

from ring_python_sdk.audio import AudioProcessor
from ring_python_sdk.audio.opus_codec import OpusUnavailableError
from ring_python_sdk.ble import (
    send_ble_test_start,
    send_ble_test_stop,
    send_imu_start,
    send_imu_stop,
    send_led_blink,
    send_led_mode,
    send_led_set,
    send_mic_control,
    send_ppg_start,
    send_ppg_stop,
    send_swipe_start,
    send_swipe_stop,
    send_wear_calibrate,
    send_wear_calibration_get,
)
from ring_python_sdk.ble_test.processor import BleTestProcessor
from ring_python_sdk.button.processor import ButtonProcessor
from ring_python_sdk.core.constants import (
    DEFAULT_BLE_TEST_DURATION_S,
    DEFAULT_BLE_TEST_PACKET_COUNT,
    DEFAULT_BLE_TEST_PAYLOAD_SIZE,
    DEFAULT_BLE_TEST_PPS,
    DEFAULT_BUTTON_OUTPUT,
    DEFAULT_IMU_ACCEL_FS_G,
    DEFAULT_IMU_ACCEL_HZ,
    DEFAULT_IMU_FRAMES_PER_PACKET,
    DEFAULT_IMU_GYRO_FS_DPS,
    DEFAULT_IMU_GYRO_HZ,
    DEFAULT_IMU_OUTPUT,
    DEFAULT_LED_BLINK_MS,
    DEFAULT_LED_BLINK_PERIOD_MS,
    DEFAULT_LED_BREATHE_PERIOD_MS,
    DEFAULT_LED_BRIGHTNESS,
    DEFAULT_OUTPUT,
    DEFAULT_PPG_OUTPUT,
    DEFAULT_RAISE_TO_WAKE_OUTPUT,
    DEFAULT_SWIPE_OUTPUT,
    DEFAULT_WEAR_OUTPUT,
    IMU_ENCODE_RAW,
    IMU_ENCODE_TOKEN,
    LED_MODE_BLINK,
    LED_MODE_BREATHE,
    LED_MODE_ON,
    LED_MODE_PULSE,
)
from ring_python_sdk.imu import ImuProcessor
from ring_python_sdk.imu.processor import ImuSample
from ring_python_sdk.plots import create_imu_plot
from ring_python_sdk.ppg.processor import PpgProcessor, PpgSample
from ring_python_sdk.raise_to_wake.processor import RaiseToWakeProcessor
from ring_python_sdk.session.types import MIC_ENCODE, PPG_MODE
from ring_python_sdk.swipe.processor import SwipeProcessor


class SensorsMixin:
    async def mic_on(
        self,
        encode_name: str = "opus",
        *,
        on_pcm: Callable[[int, bytes], None] | None = None,
    ) -> None:
        assert self.client is not None
        if self.mic_active:
            print("mic already on")
            return
        encode = MIC_ENCODE.get(encode_name.lower())
        if encode is None:
            print("mic encode: pcm | adpcm | opus")
            return
        path = self._seg_path("mic", DEFAULT_OUTPUT)
        try:
            self.mic = AudioProcessor(
                path,
                print_frames=self.print_flags.mic,
                live_plot=self.audio_plot if self.audio_plot_enabled else None,
                encoding=encode_name.lower(),
                on_pcm=on_pcm,
            )
        except OpusUnavailableError as exc:
            print(str(exc))
            return
        await send_mic_control(self.client, self.rx_uuid, on=True, encode=encode)
        self.mic_active = True
        print(f"mic capturing -> {path}")

    async def mic_off(self) -> None:
        assert self.client is not None
        if not self.mic_active:
            print("mic already off")
            return
        await send_mic_control(self.client, self.rx_uuid, on=False)
        if self.mic is not None:
            self.mic.close()
            self.saved_paths.append(self.mic.output_path)
            print(f"mic saved: {self.mic.output_path}")
            self.mic = None
        self.mic_active = False

    # --- imu ---
    async def imu_on(
        self,
        gyro_hz: int = DEFAULT_IMU_GYRO_HZ,
        accel_hz: int = DEFAULT_IMU_ACCEL_HZ,
        gyro_fs: int = DEFAULT_IMU_GYRO_FS_DPS,
        accel_fs: int = DEFAULT_IMU_ACCEL_FS_G,
        frames_per_packet: int = DEFAULT_IMU_FRAMES_PER_PACKET,
        encode_mode: int = IMU_ENCODE_RAW,
        *,
        on_sample: Callable[[ImuSample], None] | None = None,
    ) -> None:
        assert self.client is not None
        if self.imu_active:
            print("imu already on")
            return
        path = self._seg_path("imu", DEFAULT_IMU_OUTPUT)
        encode_name = "token" if encode_mode == IMU_ENCODE_TOKEN else "raw"
        if self.imu_plot_enabled and self.imu_plot is None:
            self.imu_plot = create_imu_plot(
                window_seconds=self.imu_plot_window,
                expected_hz=max(gyro_hz, accel_hz),
            )
            self.imu_plot.setup()
        elif self.imu_plot is not None:
            self.imu_plot.set_expected_hz(max(gyro_hz, accel_hz))
        self.imu = ImuProcessor(
            path,
            None,
            print_samples=self.print_flags.imu,
            gyro_fs_dps=gyro_fs,
            accel_fs_g=accel_fs,
            gyro_hz=gyro_hz,
            accel_hz=accel_hz,
            frames_per_packet=frames_per_packet,
            live_plot=self.imu_plot if self.imu_plot_enabled else None,
            imu_chip=self.imu_chip,
            encode_mode=encode_name,
            on_sample=on_sample,
        )
        await send_imu_start(
            self.client,
            self.rx_uuid,
            gyro_hz,
            accel_hz,
            gyro_fs,
            accel_fs,
            frames_per_packet,
            encode_mode,
        )
        self.imu_active = True
        print(f"imu capturing ({encode_name}) -> {path}")

    async def imu_off(self) -> None:
        assert self.client is not None
        if not self.imu_active:
            print("imu already off")
            return
        await send_imu_stop(self.client, self.rx_uuid)
        if self.imu is not None:
            self.imu.close()
            if self.imu.csv_path is not None:
                self.saved_paths.append(self.imu.csv_path)
                print(f"imu saved: {self.imu.csv_path}")
            self.imu = None
        self.imu_active = False

    # --- ppg ---
    async def ppg_on(
        self,
        mode_name: str = "hrs",
        *,
        on_sample: Callable[[PpgSample], None] | None = None,
    ) -> None:
        assert self.client is not None
        mode = PPG_MODE.get(mode_name.lower())
        if mode is None:
            print("ppg mode: hrs | spo2 | wear")
            return
        mode_label = {0: "hrs", 1: "spo2", 2: "wear"}[mode]
        if self.ppg_active:
            if self.ppg_mode == mode_label:
                print(f"ppg already on ({mode_label})")
                return
            await self.ppg_off()
        default = DEFAULT_WEAR_OUTPUT if mode == 2 else DEFAULT_PPG_OUTPUT
        path = self._seg_path("ppg", default)
        # ppg on implies printing vitals (hr / spo2 / wear) to TUI Log.
        self.print_flags.ppg = True
        self.ppg = PpgProcessor(
            path,
            print_samples=True,
            wear_csv_path=path if mode == 2 else None,
            mode=mode_label,
            log=self.emit_live,
            on_sample=on_sample,
        )
        await send_ppg_start(self.client, self.rx_uuid, mode)
        self.ppg_active = True
        self.ppg_mode = mode_label
        print(f"ppg capturing ({mode_label}) -> {path}")

    async def ppg_off(self) -> None:
        assert self.client is not None
        if not self.ppg_active:
            print("ppg already off")
            return
        await send_ppg_stop(self.client, self.rx_uuid)
        if self.ppg is not None:
            self.ppg.close()
            self.saved_paths.append(self.ppg.csv_path)
            print(f"ppg saved: {self.ppg.csv_path}")
            self.ppg = None
        self.ppg_active = False
        self.ppg_mode = ""

    async def ppg_calibrate(self) -> None:
        assert self.client is not None
        await send_wear_calibrate(self.client, self.rx_uuid)

    async def ppg_calibration(self) -> None:
        assert self.client is not None
        await send_wear_calibration_get(self.client, self.rx_uuid)

    def emit_live(self, line: str) -> None:
        """Queue a line for TUI Log (BLE notify path; not stdout)."""
        self.live_logs.append(line)

    def drain_live_logs(self) -> list[str]:
        """Pop all pending live log lines (oldest first)."""
        out: list[str] = []
        while self.live_logs:
            out.append(self.live_logs.popleft())
        return out

    # --- swipe / button ---
    async def swipe_on(self) -> None:
        assert self.client is not None
        if self.swipe_active:
            print("swipe already on")
            return
        path = self._seg_path("swipe", DEFAULT_SWIPE_OUTPUT)
        # Temporarily hide every-inference EVENT (swipe infer); TRIGGER still logs.
        # Use `print swipe on` to show EVENT logits again.
        self.print_flags.swipe = False
        self.swipe = SwipeProcessor(
            path, print_events=False, log=self.emit_live
        )
        await send_swipe_start(self.client, self.rx_uuid)
        self.swipe_active = True
        print(f"swipe capturing -> {path}")

    async def swipe_off(self) -> None:
        assert self.client is not None
        if not self.swipe_active:
            print("swipe already off")
            return
        await send_swipe_stop(self.client, self.rx_uuid)
        if self.swipe is not None:
            self.swipe.close()
            self.saved_paths.append(self.swipe.csv_path)
            print(f"swipe saved: {self.swipe.csv_path}")
            self.swipe = None
        self.swipe_active = False

    async def _ensure_button_capture(self) -> None:
        """Button is always-on in firmware; start host capture whenever linked."""
        if self.button_active:
            return
        path = self._seg_path("button", DEFAULT_BUTTON_OUTPUT)
        self.print_flags.button = True
        self.button = ButtonProcessor(
            path, print_events=True, log=self.emit_live
        )
        self.button_active = True
        print(f"button capturing -> {path}")

    async def button_off(self) -> None:
        """Stop host capture (cleanup / disconnect only; no user command)."""
        if not self.button_active:
            return
        if self.button is not None:
            self.button.close()
            self.saved_paths.append(self.button.csv_path)
            print(f"button saved: {self.button.csv_path}")
            self.button = None
        self.button_active = False

    async def _ensure_raise_to_wake_capture(self) -> None:
        """R2W is always-on in ICM45686 firmware; capture whenever linked."""
        if self.raise_to_wake_active:
            return
        path = self._seg_path(
            "raise_to_wake", DEFAULT_RAISE_TO_WAKE_OUTPUT
        )
        self.raise_to_wake = RaiseToWakeProcessor(path, log=self.emit_live)
        self.raise_to_wake_active = True
        print(f"r2w capturing -> {path}")

    async def raise_to_wake_off(self) -> None:
        """Stop host capture during disconnect; firmware remains always-on."""
        if not self.raise_to_wake_active:
            return
        if self.raise_to_wake is not None:
            self.raise_to_wake.close()
            self.saved_paths.append(self.raise_to_wake.csv_path)
            print(f"r2w saved: {self.raise_to_wake.csv_path}")
            self.raise_to_wake = None
        self.raise_to_wake_active = False

    async def led_on(self, brightness: int = DEFAULT_LED_BRIGHTNESS) -> None:
        assert self.client is not None
        if brightness >= DEFAULT_LED_BRIGHTNESS:
            await send_led_set(self.client, self.rx_uuid, True)
        else:
            await send_led_mode(
                self.client,
                self.rx_uuid,
                LED_MODE_ON,
                brightness,
                0,
            )

    async def led_off(self) -> None:
        assert self.client is not None
        await send_led_set(self.client, self.rx_uuid, False)

    async def led_blink(self, ms: int = DEFAULT_LED_BLINK_MS) -> None:
        assert self.client is not None
        await send_led_blink(self.client, self.rx_uuid, ms)

    async def led_mode(
        self,
        mode: int,
        brightness: int = DEFAULT_LED_BRIGHTNESS,
        period_ms: int = 0,
    ) -> None:
        assert self.client is not None
        await send_led_mode(self.client, self.rx_uuid, mode, brightness, period_ms)

    async def led_breathe(
        self,
        period_ms: int = DEFAULT_LED_BREATHE_PERIOD_MS,
        brightness: int = DEFAULT_LED_BRIGHTNESS,
    ) -> None:
        await self.led_mode(LED_MODE_BREATHE, brightness, period_ms)

    async def led_blink_mode(
        self,
        period_ms: int = DEFAULT_LED_BLINK_PERIOD_MS,
        brightness: int = DEFAULT_LED_BRIGHTNESS,
    ) -> None:
        await self.led_mode(LED_MODE_BLINK, brightness, period_ms)

    async def led_pulse(
        self,
        ms: int = DEFAULT_LED_BLINK_MS,
        brightness: int = DEFAULT_LED_BRIGHTNESS,
    ) -> None:
        await self.led_mode(LED_MODE_PULSE, brightness, ms)

    # --- ble test ---
    async def bletest_on(
        self,
        payload: int = DEFAULT_BLE_TEST_PAYLOAD_SIZE,
        pps: int = DEFAULT_BLE_TEST_PPS,
        duration_s: int = DEFAULT_BLE_TEST_DURATION_S,
        packet_count: int = DEFAULT_BLE_TEST_PACKET_COUNT,
    ) -> None:
        assert self.client is not None
        if self.ble_test_active:
            print("bletest already on")
            return
        self.ble_test = BleTestProcessor()
        self.ble_test.mark_session_start()
        await send_ble_test_start(
            self.client, self.rx_uuid, payload, pps, duration_s, packet_count
        )
        self.ble_test_active = True

    async def bletest_off(self) -> None:
        assert self.client is not None
        if not self.ble_test_active:
            print("bletest already off")
            return
        await send_ble_test_stop(self.client, self.rx_uuid)
        if self.ble_test is not None:
            self.ble_test.mark_session_end()
            print(self.ble_test.format_summary())
            self.ble_test = None
        self.ble_test_active = False
