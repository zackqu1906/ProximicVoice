"""PPG capture CSV output."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from ring_python_sdk.core.constants import DEFAULT_PPG_OUTPUT, DEFAULT_WEAR_OUTPUT
from ring_python_sdk.core.data_paths import MODE_PPG, resolve_capture_path, new_session_dir

PPG_SAMPLE_COLUMNS = (
    "seq",
    "heart_rate_bpm",
    "spo2_pct",
    "wear_status",
    "uptime_ms",
    "hrv_rri_ms",
    "hrv_stress",
    "hrv_rri_num",
)

WEAR_SAMPLE_COLUMNS = (
    "packet_seq",
    "sample_index",
    "wear_status",
    "sar_calibrated",
    "sar_valid",
    "sar_lp",
    "sar_bl",
    "sar_diff",
    "ir_raw",
    "uptime_ms",
)

PPG_RAW_SAMPLE_COLUMNS = (
    "packet_seq",
    "sample_index",
    "mode",
    "channels_mask",
    "green",
    "red",
    "ir",
    "uptime_ms",
)


def resolve_ppg_output_path(args: argparse.Namespace) -> Path:
    session = new_session_dir(MODE_PPG)
    default_output = DEFAULT_WEAR_OUTPUT if args.ppg_mode == "wear" else DEFAULT_PPG_OUTPUT
    output = args.output or default_output
    return resolve_capture_path(
        MODE_PPG, output, default_output, session_dir=session
    )


class PpgCsvWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(PPG_SAMPLE_COLUMNS)

    def write_row(
        self,
        seq: int,
        hr: int,
        spo2: int,
        wear: int,
        uptime_ms: int,
        hrv_rri_ms: int = 0,
        hrv_stress: int = 0,
        hrv_rri_num: int = 0,
    ) -> None:
        self._writer.writerow(
            [seq, hr, spo2, wear, uptime_ms, hrv_rri_ms, hrv_stress, hrv_rri_num]
        )
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class WearCsvWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(WEAR_SAMPLE_COLUMNS)

    def write_rows(
        self,
        seq: int,
        wear: int,
        calibrated: bool,
        sar_valid: bool,
        sar_lp: int,
        sar_bl: int,
        sar_diff: int,
        ir_samples: list[int],
        uptime_ms: int,
        start_index: int,
    ) -> None:
        for offset, ir_raw in enumerate(ir_samples):
            self._writer.writerow(
                [
                    seq,
                    start_index + offset,
                    wear,
                    int(calibrated),
                    int(sar_valid),
                    sar_lp,
                    sar_bl,
                    sar_diff,
                    ir_raw,
                    uptime_ms,
                ]
            )
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class PpgRawCsvWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(PPG_RAW_SAMPLE_COLUMNS)

    def write_rows(
        self,
        seq: int,
        mode: str,
        channels_mask: int,
        green: list[int],
        red: list[int],
        ir: list[int],
        uptime_ms: int,
        start_index: int,
    ) -> None:
        count = max(len(green), len(red), len(ir))
        for offset in range(count):
            self._writer.writerow(
                [
                    seq,
                    start_index + offset,
                    mode,
                    channels_mask,
                    green[offset] if offset < len(green) else "",
                    red[offset] if offset < len(red) else "",
                    ir[offset] if offset < len(ir) else "",
                    uptime_ms,
                ]
            )
        self._file.flush()

    def close(self) -> None:
        self._file.close()
