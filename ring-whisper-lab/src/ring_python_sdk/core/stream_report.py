"""Format stream rate / loss evaluation lines."""

from __future__ import annotations

from ring_python_sdk.core.seq_tracker import SeqTrackerStats


def _loss_rate_percent(stats: SeqTrackerStats, received: int) -> float:
    missing = stats.missing_count
    denom = missing + received
    if denom <= 0:
        return 0.0
    return 100.0 * missing / denom


def _rate(count: int, duration_s: float) -> float:
    if duration_s <= 0.0:
        return 0.0
    return count / duration_s


def format_stream_line(
    label: str,
    *,
    received: int,
    duration_s: float,
    nominal_hz: float | None,
    seq_stats: SeqTrackerStats | None = None,
    extra: str = "",
) -> str:
    actual_hz = _rate(received, duration_s)
    parts = [
        f"{label}: received={received}",
        f"rate={actual_hz:.2f}Hz",
    ]
    if nominal_hz is not None and nominal_hz > 0:
        parts.append(f"nominal={nominal_hz:.2f}Hz")
        parts.append(f"rate_ratio={actual_hz / nominal_hz * 100:.1f}%")
    if seq_stats is not None and seq_stats.received_count > 0:
        parts.append(f"seq_gaps={seq_stats.gap_events}")
        parts.append(f"seq_missing={seq_stats.missing_count}")
        parts.append(f"seq_dup={seq_stats.duplicate_count}")
        parts.append(f"seq_ooo={seq_stats.out_of_order_count}")
        parts.append(f"loss_est={_loss_rate_percent(seq_stats, received):.2f}%")
    if extra:
        parts.append(extra)
    return ", ".join(parts)
