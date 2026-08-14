"""Decode HX3918 PPG BLE packets."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ring_python_sdk.core.constants import (
    CMD_PPG,
    PPG_PACKET_HRV_LEN,
    PPG_PACKET_LEN,
    PPG_WEAR_BYTES_PER_SAMPLE,
    PPG_WEAR_FLAG_CALIBRATED,
    PPG_WEAR_FLAG_SAR_VALID,
    PPG_WEAR_HEADER_LEN,
    SUBCMD_PPG_PACKET,
    SUBCMD_PPG_WEAR_CALIBRATION_STATUS,
    SUBCMD_PPG_WEAR_PACKET,
)
from ring_python_sdk.ppg.io import PpgCsvWriter, WearCsvWriter


def format_ppg_sample_line(
    mode: str,
    seq: int,
    hr: int,
    spo2: int,
    wear: int,
    uptime_ms: int,
    hrv_rri_ms: int = 0,
    hrv_stress: int = 0,
    hrv_rri_num: int = 0,
) -> str:
    """Human-readable PPG vitals for Log / stdout (HRS or SpO2)."""
    if mode == "hrs":
        return (
            f"ppg hrs seq={seq} hr={hr} spo2={spo2}% wear={wear} "
            f"hrv_rri_ms={hrv_rri_ms} hrv_stress={hrv_stress} "
            f"hrv_rri_num={hrv_rri_num} uptime_ms={uptime_ms}"
        )
    if mode == "spo2":
        return (
            f"ppg spo2 seq={seq} hr={hr} spo2={spo2}% wear={wear} "
            f"uptime_ms={uptime_ms}"
        )
    return (
        f"ppg {mode} seq={seq} hr={hr} spo2={spo2}% wear={wear} "
        f"uptime_ms={uptime_ms}"
    )


def format_ppg_vitals_short(mode: str, hr: int, spo2: int, wear: int) -> str:
    """Compact vitals for SensorCard extras line."""
    if mode == "spo2":
        return f"{mode}  HR {hr}  SpO2 {spo2}%  wear={wear}"
    if mode == "hrs":
        return f"{mode}  HR {hr}  SpO2 {spo2}%  wear={wear}"
    if mode == "wear":
        return f"{mode}  wear={wear}"
    return f"{mode}  HR {hr}  SpO2 {spo2}%  wear={wear}"


@dataclass
class PpgSample:
    """One PPG vitals or wear packet for real-time callbacks."""

    mode: str
    seq: int
    hr: int = 0
    spo2: int = 0
    wear: int = 0
    uptime_ms: int = 0
    hrv_rri_ms: int = 0
    hrv_stress: int = 0
    hrv_rri_num: int = 0
    ir_samples: tuple[int, ...] | None = None


@dataclass
class PpgStats:
    sample_count: int = 0
    packet_count: int = 0
    dropped_packet_count: int = 0


@dataclass
class PpgProcessor:
    csv_path: Path
    print_samples: bool = True
    wear_csv_path: Path | None = None
    mode: str = "hrs"
    log: Callable[[str], None] | None = None
    on_sample: Callable[[PpgSample], None] | None = None
    stats: PpgStats = field(default_factory=PpgStats)
    latest_hr: int | None = field(default=None, repr=False)
    latest_spo2: int | None = field(default=None, repr=False)
    latest_wear: int | None = field(default=None, repr=False)
    _writer: PpgCsvWriter | None = field(default=None, repr=False)
    _wear_writer: WearCsvWriter | None = field(default=None, repr=False)
    _last_seq: int | None = field(default=None, repr=False)
    _wear_sample_index: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self.wear_csv_path is None:
            self.wear_csv_path = self.csv_path

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._wear_writer is not None:
            self._wear_writer.close()
            self._wear_writer = None

    def _emit(self, line: str) -> None:
        if self.log is not None:
            self.log(line)
        else:
            print(line)

    def handle_notification(self, _sender, data: bytearray) -> None:
        if len(data) < 2:
            return
        if data[0] != CMD_PPG:
            return
        if data[1] == SUBCMD_PPG_WEAR_PACKET:
            self._handle_wear(data)
            return
        if data[1] == SUBCMD_PPG_WEAR_CALIBRATION_STATUS:
            if self.print_samples and len(data) >= 14:
                op, result, valid = data[2], data[3], bool(data[4])
                self._emit(f"wear calibration op={op} result={result} valid={valid}")
            return
        if data[1] != SUBCMD_PPG_PACKET or len(data) < PPG_PACKET_LEN:
            return

        seq, hr, spo2, wear, uptime_ms = struct.unpack_from("<HHBHI", data, 2)
        hrv_rri_ms = 0
        hrv_stress = 0
        hrv_rri_num = 0
        if len(data) >= PPG_PACKET_HRV_LEN:
            hrv_rri_ms, hrv_stress, hrv_rri_num = struct.unpack_from("<HBB", data, 13)

        if self._last_seq is not None:
            expected = (self._last_seq + 1) & 0xFFFF
            if seq != expected:
                gap = (seq - expected) & 0xFFFF
                self.stats.dropped_packet_count += max(gap - 1, 0)
        self._last_seq = seq
        self.stats.packet_count += 1
        self.stats.sample_count += 1
        self.latest_hr = hr
        self.latest_spo2 = spo2
        self.latest_wear = wear

        if self._writer is not None:
            self._writer.write_row(
                seq, hr, spo2, wear, uptime_ms, hrv_rri_ms, hrv_stress, hrv_rri_num
            )
        else:
            self._writer = PpgCsvWriter(self.csv_path)
            self._writer.write_row(
                seq, hr, spo2, wear, uptime_ms, hrv_rri_ms, hrv_stress, hrv_rri_num
            )

        sample = PpgSample(
            mode=self.mode,
            seq=seq,
            hr=hr,
            spo2=spo2,
            wear=wear,
            uptime_ms=uptime_ms,
            hrv_rri_ms=hrv_rri_ms,
            hrv_stress=hrv_stress,
            hrv_rri_num=hrv_rri_num,
        )
        if self.on_sample is not None:
            self.on_sample(sample)

        if self.print_samples:
            self._emit(
                format_ppg_sample_line(
                    mode=self.mode,
                    seq=seq,
                    hr=hr,
                    spo2=spo2,
                    wear=wear,
                    uptime_ms=uptime_ms,
                    hrv_rri_ms=hrv_rri_ms,
                    hrv_stress=hrv_stress,
                    hrv_rri_num=hrv_rri_num,
                )
            )

    def _handle_wear(self, data: bytearray) -> None:
        if len(data) < PPG_WEAR_HEADER_LEN:
            return
        seq, = struct.unpack_from("<H", data, 2)
        wear, flags, count = data[4], data[5], data[6]
        expected = PPG_WEAR_HEADER_LEN + count * PPG_WEAR_BYTES_PER_SAMPLE
        if count == 0 or len(data) < expected:
            return
        sar_lp, sar_bl, sar_diff, uptime_ms = struct.unpack_from("<hhiI", data, 8)
        ir_samples = list(struct.unpack_from(f"<{count}i", data, PPG_WEAR_HEADER_LEN))

        if self._last_seq is not None:
            expected_seq = (self._last_seq + 1) & 0xFFFF
            if seq != expected_seq:
                self.stats.dropped_packet_count += (seq - expected_seq) & 0xFFFF
        self._last_seq = seq
        self.stats.packet_count += 1
        self.stats.sample_count += count
        self.latest_wear = wear
        calibrated = bool(flags & PPG_WEAR_FLAG_CALIBRATED)
        sar_valid = bool(flags & PPG_WEAR_FLAG_SAR_VALID)

        if self._wear_writer is None:
            self._wear_writer = WearCsvWriter(self.wear_csv_path or self.csv_path)
        self._wear_writer.write_rows(
            seq, wear, calibrated, sar_valid, sar_lp, sar_bl, sar_diff,
            ir_samples, uptime_ms, self._wear_sample_index,
        )
        self._wear_sample_index += count
        if self.on_sample is not None:
            self.on_sample(
                PpgSample(
                    mode="wear",
                    seq=seq,
                    wear=wear,
                    uptime_ms=uptime_ms,
                    ir_samples=tuple(ir_samples),
                )
            )
        if self.print_samples:
            self._emit(
                f"ppg wear pkt_seq={seq} status={wear} ir={ir_samples} "
                f"sar_diff={sar_diff} calibrated={calibrated} valid={sar_valid}"
            )
