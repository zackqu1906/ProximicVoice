from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from ring_python_sdk.core.constants import (
    BLE_TEST_PACKET_HEADER_LEN,
    BLE_TEST_REPORT_PACKET_LEN,
    CMD_BLE_TEST,
    SUBCMD_BLE_TEST_PACKET,
    SUBCMD_BLE_TEST_REPORT,
)


@dataclass
class BleTestStats:
    wall_start_s: float = 0.0
    wall_end_s: float = 0.0
    notify_count: int = 0
    valid_packet_count: int = 0
    data_bytes_total: int = 0
    seq_gap_packets: int = 0
    duplicate_count: int = 0
    out_of_order_count: int = 0
    payload_error_count: int = 0
    short_packet_count: int = 0
    unknown_cmd_count: int = 0
    first_seq: int = -1
    last_seq: int = -1
    report_received: bool = False
    fw_total_sent: int = 0
    fw_duration_ms: int = 0
    fw_tx_dropped: int = 0
    latency_ms_sum: float = 0.0
    latency_ms_count: int = 0
    rx_packets_per_sec: list[int] = field(default_factory=list)
    fw_packets_per_sec: list[int] = field(default_factory=list)
    packet_lengths: list[int] = field(default_factory=list)


def _expected_payload_byte(seq: int, index: int) -> int:
    return (seq + index) & 0xFF


def _ensure_sec_bucket(buckets: list[int], sec: int) -> None:
    if sec < 0:
        sec = 0
    while len(buckets) <= sec:
        buckets.append(0)


class BleTestProcessor:
    def __init__(self) -> None:
        self.stats = BleTestStats()
        self._last_seq = -1

    def mark_session_start(self) -> None:
        self.stats.wall_start_s = time.monotonic()
        self.stats.rx_packets_per_sec = []
        self.stats.fw_packets_per_sec = []
        self.stats.packet_lengths = []

    def mark_session_end(self) -> None:
        self.stats.wall_end_s = time.monotonic()

    def _rx_second_index(self) -> int:
        return int(time.monotonic() - self.stats.wall_start_s)

    def _record_rx_packet(self, packet_len: int) -> None:
        sec = self._rx_second_index()
        _ensure_sec_bucket(self.stats.rx_packets_per_sec, sec)
        self.stats.rx_packets_per_sec[sec] += 1
        self.stats.packet_lengths.append(packet_len)

    def handle_notification(self, _sender: int, packet: bytearray) -> None:
        self.stats.notify_count += 1

        if len(packet) < 2:
            self.stats.short_packet_count += 1
            return

        if packet[0] != CMD_BLE_TEST:
            self.stats.unknown_cmd_count += 1
            return

        if packet[1] == SUBCMD_BLE_TEST_REPORT:
            self.stats.data_bytes_total += len(packet)
            self._handle_report(packet)
            return

        if packet[1] != SUBCMD_BLE_TEST_PACKET:
            self.stats.unknown_cmd_count += 1
            return

        if len(packet) < BLE_TEST_PACKET_HEADER_LEN:
            self.stats.short_packet_count += 1
            return

        self.stats.data_bytes_total += len(packet)
        self._record_rx_packet(len(packet))

        seq = struct.unpack_from("<I", packet, 2)[0]
        tx_ms = struct.unpack_from("<I", packet, 6)[0]
        payload_len = len(packet) - BLE_TEST_PACKET_HEADER_LEN

        for i in range(payload_len):
            expected = _expected_payload_byte(seq, i)
            if packet[BLE_TEST_PACKET_HEADER_LEN + i] != expected:
                self.stats.payload_error_count += 1
                break

        if self.stats.first_seq < 0:
            self.stats.first_seq = seq

        if self._last_seq >= 0:
            if seq == self._last_seq:
                self.stats.duplicate_count += 1
            elif seq < self._last_seq:
                self.stats.out_of_order_count += 1
            elif seq > self._last_seq + 1:
                self.stats.seq_gap_packets += seq - self._last_seq - 1

        self._last_seq = seq
        self.stats.last_seq = seq
        self.stats.valid_packet_count += 1

        rx_ms = time.monotonic() * 1000.0
        self.stats.latency_ms_sum += rx_ms - float(tx_ms)
        self.stats.latency_ms_count += 1

    def _handle_report(self, packet: bytearray) -> None:
        if len(packet) < BLE_TEST_REPORT_PACKET_LEN:
            self.stats.short_packet_count += 1
            return

        total_sent, duration_ms, tx_dropped = struct.unpack_from("<III", packet, 2)
        self.stats.report_received = True
        self.stats.fw_total_sent = total_sent
        self.stats.fw_duration_ms = duration_ms
        self.stats.fw_tx_dropped = tx_dropped

        if len(packet) >= BLE_TEST_REPORT_PACKET_LEN + 2:
            (seconds_count,) = struct.unpack_from("<H", packet, 14)
            hist_len = BLE_TEST_REPORT_PACKET_LEN + 2 + seconds_count * 2
            if seconds_count > 0 and len(packet) >= hist_len:
                self.stats.fw_packets_per_sec = list(
                    struct.unpack_from(f"<{seconds_count}H", packet, 16)
                )

    def _build_per_second_rows(self) -> list[dict[str, int | float]]:
        rx = self.stats.rx_packets_per_sec
        fw = self.stats.fw_packets_per_sec
        n = max(len(rx), len(fw))
        rows: list[dict[str, int | float]] = []

        for sec in range(n):
            fw_n = fw[sec] if sec < len(fw) else 0
            rx_n = rx[sec] if sec < len(rx) else 0
            rows.append(
                {
                    "sec": sec,
                    "fw_sent": fw_n,
                    "rx_received": rx_n,
                    "delta": fw_n - rx_n,
                }
            )
        return rows

    def _packet_size_stats(self) -> dict[str, int | float]:
        lengths = self.stats.packet_lengths
        if not lengths:
            return {
                "packet_size_min": 0,
                "packet_size_max": 0,
                "packet_size_avg": 0.0,
                "payload_size_min": 0,
                "payload_size_max": 0,
                "payload_size_avg": 0.0,
            }

        payloads = [ln - BLE_TEST_PACKET_HEADER_LEN for ln in lengths]
        return {
            "packet_size_min": min(lengths),
            "packet_size_max": max(lengths),
            "packet_size_avg": round(sum(lengths) / len(lengths), 2),
            "payload_size_min": min(payloads),
            "payload_size_max": max(payloads),
            "payload_size_avg": round(sum(payloads) / len(payloads), 2),
        }

    def compute_metrics(self) -> dict[str, float | int | bool | list]:
        wall_s = max(self.stats.wall_end_s - self.stats.wall_start_s, 1e-6)
        valid = self.stats.valid_packet_count
        gap = self.stats.seq_gap_packets
        fw_sent = self.stats.fw_total_sent
        fw_dropped = self.stats.fw_tx_dropped
        fw_duration_s = max(self.stats.fw_duration_ms / 1000.0, 1e-6)

        if self.stats.report_received and fw_sent > 0:
            air_loss_packets = max(fw_sent - valid, 0)
            air_loss_pct = 100.0 * air_loss_packets / fw_sent
            delivery_pct = 100.0 * valid / fw_sent
        else:
            air_loss_packets = gap
            air_loss_pct = (100.0 * gap / (gap + valid)) if (gap + valid) > 0 else 0.0
            delivery_pct = 0.0

        enqueue_attempts = fw_sent + fw_dropped
        enqueue_drop_pct = (
            100.0 * fw_dropped / enqueue_attempts if enqueue_attempts > 0 else 0.0
        )

        ble_seq_loss_pct = (100.0 * gap / (gap + valid)) if (gap + valid) > 0 else 0.0

        send_pps = 0.0
        throughput_kbs = 0.0
        if self.stats.report_received and fw_sent > 0:
            send_pps = fw_sent / fw_duration_s
            if valid > 0:
                avg_packet_bytes = self.stats.data_bytes_total / valid
                throughput_kbs = (fw_sent * avg_packet_bytes) / 1024.0 / fw_duration_s

        mean_latency = (
            self.stats.latency_ms_sum / self.stats.latency_ms_count
            if self.stats.latency_ms_count > 0
            else 0.0
        )

        per_second = self._build_per_second_rows()
        size_stats = self._packet_size_stats()

        metrics: dict[str, float | int | bool | list] = {
            "wall_time_s": round(wall_s, 3),
            "notify_count": self.stats.notify_count,
            "valid_packet_count": valid,
            "data_bytes_total": self.stats.data_bytes_total,
            "seq_gap_packets": gap,
            "ble_seq_loss_pct": round(ble_seq_loss_pct, 4),
            "air_loss_packets": air_loss_packets,
            "air_loss_pct": round(air_loss_pct, 4),
            "delivery_pct": round(delivery_pct, 4),
            "enqueue_drop_packets": fw_dropped,
            "enqueue_drop_pct": round(enqueue_drop_pct, 4),
            "enqueue_attempts": enqueue_attempts,
            "send_pps": round(send_pps, 2),
            "throughput_kbs": round(throughput_kbs, 3),
            "fw_duration_s": round(fw_duration_s, 3),
            "duplicate_count": self.stats.duplicate_count,
            "out_of_order_count": self.stats.out_of_order_count,
            "payload_error_count": self.stats.payload_error_count,
            "short_packet_count": self.stats.short_packet_count,
            "unknown_cmd_count": self.stats.unknown_cmd_count,
            "first_seq": self.stats.first_seq,
            "last_seq": self.stats.last_seq,
            "mean_latency_ms": round(mean_latency, 2),
            "report_received": self.stats.report_received,
            "fw_total_sent": fw_sent,
            "fw_duration_ms": self.stats.fw_duration_ms,
            "fw_tx_dropped": fw_dropped,
            "per_second": per_second,
            "histogram_seconds": len(per_second),
        }
        metrics.update(size_stats)
        return metrics

    def format_summary(self) -> str:
        m = self.compute_metrics()
        lines = [
            f"session_wall={m['wall_time_s']}s fw_duration={m['fw_duration_s']}s "
            f"valid={m['valid_packet_count']} notify={m['notify_count']}",
            f"air_loss={m['air_loss_packets']} ({m['air_loss_pct']}%) "
            f"delivery={m['delivery_pct']}% seq_gap={m['seq_gap_packets']} "
            f"({m['ble_seq_loss_pct']}%)",
            f"enqueue_drop={m['enqueue_drop_packets']} ({m['enqueue_drop_pct']}%) "
            f"attempts={m['enqueue_attempts']}",
            f"send_throughput={m['throughput_kbs']} KB/s send_pps={m['send_pps']}",
            f"packet_size B: min={m['packet_size_min']} max={m['packet_size_max']} "
            f"avg={m['packet_size_avg']} "
            f"(payload avg={m['payload_size_avg']})",
            f"bytes={m['data_bytes_total']} duplicate={m['duplicate_count']} "
            f"out_of_order={m['out_of_order_count']} payload_err={m['payload_error_count']}",
            f"seq_range=[{m['first_seq']}, {m['last_seq']}]",
        ]

        if m["report_received"]:
            lines.append(
                "firmware_report: "
                f"total_sent={m['fw_total_sent']} "
                f"duration_ms={m['fw_duration_ms']} "
                f"tx_dropped_enqueue={m['fw_tx_dropped']} "
                f"hist_seconds={m['histogram_seconds']}"
            )
        else:
            lines.append("firmware_report: not received")

        return "\n".join(lines)
