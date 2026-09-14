"""Measure Ring BLE audio clock drift against a CoreAudio reference microphone.

The tool emits a deterministic, short broadband probe every few seconds, records
the probes through both paths, and fits the change in their relative delay.  It
also records Ring frame sequence/arrival statistics so transport jitter and
packet loss are not confused with sampling-clock drift.
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
import wave
from pathlib import Path

import numpy as np
from scipy import signal

from proximic_ring.audio.ring import RingAudioSource


RING_RATE = 16_000
REFERENCE_RATE = 48_000


class TimedRingAudioSource(RingAudioSource):
    """Ring source that retains callback timing without changing the PCM path."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.arrivals: list[tuple[int, int, int]] = []

    def _on_pcm(self, frame_seq: int, pcm: bytes) -> None:
        self.arrivals.append(
            (int(frame_seq) & 0xFFFF, len(pcm) // 2, time.monotonic_ns())
        )
        super()._on_pcm(frame_seq, pcm)


def _device_index(sd, needle: str, *, input_device: bool) -> int:
    if needle.strip().isdigit():
        return int(needle)
    key = needle.casefold()
    channel_key = "max_input_channels" if input_device else "max_output_channels"
    matches = [
        index
        for index, item in enumerate(sd.query_devices())
        if int(item[channel_key]) > 0 and key in str(item["name"]).casefold()
    ]
    if len(matches) != 1:
        kind = "input" if input_device else "output"
        raise RuntimeError(
            f"Expected one {kind} device matching {needle!r}, found {matches}"
        )
    return matches[0]


def _probe(rate: int, duration_s: float = 0.18) -> np.ndarray:
    """Return a repeatable speech-band noise probe with click-free edges."""

    rng = np.random.default_rng(0x2CC7)
    count = int(round(rate * duration_s))
    noise = rng.standard_normal(count)
    sos = signal.butter(6, [700, 6_000], btype="bandpass", fs=rate, output="sos")
    shaped = signal.sosfiltfilt(sos, noise)
    shaped *= signal.windows.tukey(count, alpha=0.45)
    shaped /= max(float(np.max(np.abs(shaped))), 1e-12)
    return np.asarray(shaped * 0.22, dtype=np.float32)


def _stimulus(duration_s: float, interval_s: float) -> tuple[np.ndarray, list[float]]:
    probe = _probe(REFERENCE_RATE)
    out = np.zeros(int(round(duration_s * REFERENCE_RATE)), dtype=np.float32)
    times = list(np.arange(3.0, duration_s - 2.0, interval_s, dtype=float))
    for when in times:
        start = int(round(when * REFERENCE_RATE))
        out[start : start + probe.size] += probe
    return out, times


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    data = (pcm * 32767.0).astype("<i2", copy=False).tobytes()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(data)


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError(f"Expected mono PCM16 WAV: {path}")
        rate = handle.getframerate()
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    return samples.astype(np.float32) / 32768.0, rate


def _normalized_probe_score(samples: np.ndarray, probe: np.ndarray) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float64)
    probe = np.asarray(probe, dtype=np.float64)
    samples -= float(np.mean(samples))
    probe -= float(np.mean(probe))
    corr = signal.fftconvolve(samples, probe[::-1], mode="valid")
    energy = signal.fftconvolve(samples * samples, np.ones(probe.size), mode="valid")
    denom = np.sqrt(np.maximum(energy * float(np.dot(probe, probe)), 1e-18))
    return np.abs(corr / denom)


def _find_probe_positions(
    samples: np.ndarray,
    probe: np.ndarray,
    *,
    interval_s: float,
    expected_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    score = _normalized_probe_score(samples, probe)
    peaks, properties = signal.find_peaks(
        score,
        height=0.045,
        distance=int(round(interval_s * RING_RATE * 0.72)),
    )
    heights = np.asarray(properties.get("peak_heights", []), dtype=float)
    if peaks.size > expected_count:
        keep = np.argsort(heights)[-expected_count:]
        peaks = peaks[keep]
        heights = heights[keep]
    order = np.argsort(peaks)
    return peaks[order], heights[order]


def _percentile(values: np.ndarray, q: float) -> float | None:
    if values.size == 0:
        return None
    return float(np.percentile(values, q))


def _arrival_report(rows: list[tuple[int, int, int]]) -> dict[str, object]:
    if len(rows) < 3:
        return {"callbacks": len(rows), "usable": False}

    seq = np.asarray([row[0] for row in rows], dtype=np.int64)
    sizes = np.asarray([row[1] for row in rows], dtype=np.int64)
    host_s = np.asarray([row[2] for row in rows], dtype=np.float64) / 1e9
    host_s -= host_s[0]
    sample_end = np.cumsum(sizes, dtype=np.int64).astype(np.float64)
    sample_end -= sample_end[0]

    slope, intercept = np.polyfit(host_s, sample_end, 1)
    predicted = slope * host_s + intercept
    residual_ms = (sample_end - predicted) * 1_000.0 / RING_RATE
    gaps: list[int] = []
    gap_events = 0
    out_of_order = 0
    duplicates = 0
    for before, after in zip(seq[:-1], seq[1:]):
        delta = int((after - before) & 0xFFFF)
        if delta == 0:
            duplicates += 1
        elif delta == 1:
            continue
        elif delta < 0x8000:
            gap_events += 1
            gaps.append(delta - 1)
        else:
            out_of_order += 1

    arrival_ms = np.diff(host_s) * 1_000.0
    return {
        "usable": True,
        "callbacks": int(len(rows)),
        "samples": int(np.sum(sizes)),
        "host_span_s": float(host_s[-1]),
        "fitted_delivery_rate_samples_per_s": float(slope),
        "fitted_delivery_rate_error_ppm": float((slope / RING_RATE - 1.0) * 1e6),
        "arrival_interval_ms_p50": _percentile(arrival_ms, 50),
        "arrival_interval_ms_p95": _percentile(arrival_ms, 95),
        "arrival_interval_ms_p99": _percentile(arrival_ms, 99),
        "arrival_interval_ms_max": float(np.max(arrival_ms)),
        "delivery_timing_residual_ms_p95": _percentile(np.abs(residual_ms), 95),
        "sequence_gap_events": gap_events,
        "missing_frames": int(sum(gaps)),
        "duplicates": duplicates,
        "out_of_order": out_of_order,
    }


def _acoustic_drift_report(
    ring_wav: Path,
    reference: np.ndarray,
    probe_48k: np.ndarray,
    *,
    interval_s: float,
    expected_count: int,
) -> dict[str, object]:
    ring, ring_rate = _read_wav(ring_wav)
    if ring_rate != RING_RATE:
        raise RuntimeError(f"Unexpected Ring rate {ring_rate}")
    reference_16k = signal.resample_poly(reference, 1, 3).astype(np.float32)
    probe_16k = signal.resample_poly(probe_48k, 1, 3).astype(np.float32)

    ring_peaks, ring_scores = _find_probe_positions(
        ring, probe_16k, interval_s=interval_s, expected_count=expected_count
    )
    ref_peaks, ref_scores = _find_probe_positions(
        reference_16k,
        probe_16k,
        interval_s=interval_s,
        expected_count=expected_count,
    )
    pair_count = min(ring_peaks.size, ref_peaks.size, expected_count)
    report: dict[str, object] = {
        "expected_probes": expected_count,
        "ring_probes_found": int(ring_peaks.size),
        "reference_probes_found": int(ref_peaks.size),
        "paired_probes": int(pair_count),
        "ring_probe_scores": ring_scores.tolist(),
        "reference_probe_scores": ref_scores.tolist(),
    }
    if pair_count < 4:
        report["usable"] = False
        report["reason"] = "Fewer than four acoustic probes were detected in both paths"
        return report

    ring_peaks = ring_peaks[:pair_count].astype(np.float64)
    ref_peaks = ref_peaks[:pair_count].astype(np.float64)
    elapsed_s = (ref_peaks - ref_peaks[0]) / RING_RATE
    relative_samples = ring_peaks - ref_peaks
    slope, intercept = np.polyfit(elapsed_s, relative_samples, 1)
    fitted = slope * elapsed_s + intercept
    residual = relative_samples - fitted
    drift_ppm = slope / RING_RATE * 1e6
    report.update(
        {
            "usable": True,
            "relative_delay_samples": relative_samples.tolist(),
            "relative_delay_ms": (relative_samples * 1_000.0 / RING_RATE).tolist(),
            "relative_drift_ppm_ring_vs_reference": float(drift_ppm),
            "effective_ring_rate_if_reference_is_48khz": float(
                RING_RATE * (1.0 + drift_ppm / 1e6)
            ),
            "delay_change_ms_over_test": float(
                slope * elapsed_s[-1] * 1_000.0 / RING_RATE
            ),
            "fit_residual_ms_rms": float(
                math.sqrt(float(np.mean(residual * residual))) * 1_000.0 / RING_RATE
            ),
        }
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selector", default="2cc7")
    parser.add_argument("--duration", type=float, default=125.0)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--reference-device", default="MacBook Pro Microphone")
    parser.add_argument("--output-device", default="MacBook Pro Speakers")
    parser.add_argument("--output", type=Path, default=Path("experiments/ring_drift_2cc7"))
    args = parser.parse_args()

    import sounddevice as sd

    args.output.mkdir(parents=True, exist_ok=True)
    reference_device = _device_index(sd, args.reference_device, input_device=True)
    output_device = _device_index(sd, args.output_device, input_device=False)
    stimulus, probe_times = _stimulus(args.duration, args.interval)
    probe_48k = _probe(REFERENCE_RATE)
    _write_wav(args.output / "stimulus.wav", stimulus, REFERENCE_RATE)

    reference_chunks: list[np.ndarray] = []
    reference_overflows = 0

    def on_reference(indata, frames, time_info, status) -> None:
        nonlocal reference_overflows
        if status:
            reference_overflows += 1
        reference_chunks.append(np.asarray(indata[:, 0], dtype=np.float32).copy())

    ring = TimedRingAudioSource(
        name_keyword=args.selector,
        selector=args.selector,
        timeout_s=12.0,
        encoding="pcm",
        data_root=args.output / "ring_sdk",
        queue_blocks=1024,
    )
    drain_stop = threading.Event()

    def drain_ring() -> None:
        while not drain_stop.is_set():
            block = ring.read(320)
            if block is None:
                return

    started_ns = 0
    ended_ns = 0
    with ring:
        drain_thread = threading.Thread(target=drain_ring, daemon=True)
        drain_thread.start()
        # Let the known early-startup recovery finish before emitting probes.
        time.sleep(5.0)
        arrival_start = len(ring.arrivals)
        stream = sd.InputStream(
            device=reference_device,
            samplerate=REFERENCE_RATE,
            channels=1,
            dtype="float32",
            blocksize=480,
            callback=on_reference,
        )
        stream.start()
        time.sleep(0.5)
        started_ns = time.monotonic_ns()
        sd.play(stimulus, samplerate=REFERENCE_RATE, device=output_device, blocking=False)
        sd.wait()
        time.sleep(0.75)
        ended_ns = time.monotonic_ns()
        stream.stop()
        stream.close()
        arrival_end = len(ring.arrivals)
        drain_stop.set()

    reference = (
        np.concatenate(reference_chunks)
        if reference_chunks
        else np.empty(0, dtype=np.float32)
    )
    reference_path = args.output / "reference.wav"
    _write_wav(reference_path, reference, REFERENCE_RATE)
    ring_wav = ring.capture_path
    if ring_wav is None:
        raise RuntimeError("Ring SDK did not produce a capture WAV")

    arrival_rows = ring.arrivals[arrival_start:arrival_end]
    acoustic = _acoustic_drift_report(
        ring_wav,
        reference,
        probe_48k,
        interval_s=args.interval,
        expected_count=len(probe_times),
    )
    report = {
        "ring_selector": args.selector,
        "ring_capture": str(ring_wav.resolve()),
        "reference_capture": str(reference_path.resolve()),
        "reference_device": str(sd.query_devices(reference_device)["name"]),
        "output_device": str(sd.query_devices(output_device)["name"]),
        "wall_test_s": (ended_ns - started_ns) / 1e9,
        "reference_overflow_callbacks": reference_overflows,
        "ring_diagnostic": ring.diagnostic_summary(),
        "ring_transport": _arrival_report(arrival_rows),
        "acoustic_alignment": acoustic,
    }
    report_path = args.output / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report: {report_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
