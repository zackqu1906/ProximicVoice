"""Versioned acoustic bookends; fit only markers, never the intervening speech."""
from __future__ import annotations

import numpy as np
from scipy import signal

from .storage import SAMPLE_RATE as SR, write_wav

PROTOCOL = "chirp_bookends_v1"
PULSE_S = .16
GAP_S = .09
START_GUARD_S = .5
END_GUARD_S = .15
PATTERNS = {
    "start": ((900, 2700), (3100, 4800), (1800, 4200)),
    "end": ((4700, 2900), (2500, 800), (4100, 1500)),
}


def pulses(kind):
    t = np.arange(round(PULSE_S * SR)) / SR
    return [.35 * signal.chirp(t, f0=a, f1=b, t1=PULSE_S)
            * signal.windows.tukey(len(t), .25) for a, b in PATTERNS[kind]]


def marker_wave(kind):
    parts = pulses(kind)
    return np.concatenate([parts[0], np.zeros(round(GAP_S * SR)), parts[1],
                           np.zeros(round(GAP_S * SR)), parts[2]]).astype(np.float32)


def playback_file(root, kind):
    # Leading/trailing silence allows capture buffers and speaker reverberation to settle.
    path = root / "sync_signals" / f"{PROTOCOL}_{kind}.wav"
    if not path.exists():
        pre, post = (.25, .7) if kind == "start" else (.5, .5)
        write_wav(path, np.concatenate([np.zeros(round(pre * SR)), marker_wave(kind),
                                        np.zeros(round(post * SR))]))
    return path


def _scores(template, audio):
    template = np.asarray(template, dtype=np.float64)
    audio = np.asarray(audio, dtype=np.float64)
    n = len(template)
    if len(audio) < n:
        raise ValueError("录音太短，无法容纳同步提示音")
    corr = signal.correlate(audio, template, mode="valid", method="fft")
    energy = np.r_[0., np.cumsum(audio ** 2)]
    local_energy = np.maximum(energy[n:] - energy[:-n], 0)
    # FFT roundoff in a silent window must not become a giant normalized peak.
    scores = np.zeros_like(corr)
    audible = local_energy > n * 1e-12
    scores[audible] = corr[audible] / np.sqrt(local_energy[audible] * np.sum(template ** 2))
    return np.clip(scores, -1, 1)


def _peak(scores):
    scores = np.abs(scores)
    i = int(np.argmax(scores))
    fraction = 0.
    if 0 < i < len(scores) - 1:
        a, b, c = scores[i-1:i+2]
        denominator = a - 2*b + c
        if abs(denominator) > 1e-12:
            fraction = float(np.clip(.5 * (a-c) / denominator, -.5, .5))
    return i + fraction, float(scores[i])


def detect_marker(audio, kind):
    """Find distinct bookends near the first/last 12 s, then localize each pulse."""
    n = len(audio)
    lo, hi = (0, min(n, 12*SR)) if kind == "start" else (max(0, n-12*SR), n)
    template = marker_wave(kind)
    scores = _scores(template, audio[lo:hi])
    position, score = _peak(scores)
    if score < .25:
        raise ValueError(f"未可靠检出{'开始' if kind == 'start' else '结束'}提示音（相关性 {score:.2f}）；保留原音，不猜测裁剪")
    masked = np.abs(scores).copy()
    p = int(position)
    masked[max(0, p-round(.08*SR)):p+round(.08*SR)+1] = 0
    runner_up = float(masked.max())
    if runner_up > score / 1.35:
        raise ValueError("同步提示音存在多个相近匹配，无法确定边界；保留原音")
    start = position + lo
    anchors = []
    for index, pulse in enumerate(pulses(kind)):
        expected = start + index * round((PULSE_S + GAP_S) * SR)
        a = max(0, int(expected) - round(.025*SR))
        b = min(n, int(expected) + len(pulse) + round(.025*SR))
        point, confidence = _peak(_scores(pulse, audio[a:b]))
        if confidence < .3:
            raise ValueError("同步提示音子脉冲不完整或相关性不足；保留原音")
        anchors.append({"sample": a + point, "correlation": confidence})
    return {"start_sample": anchors[0]["sample"],
            "end_sample": anchors[-1]["sample"] + round(PULSE_S*SR),
            "correlation": score, "runner_up_correlation": runner_up, "pulses": anchors}


def estimate_marker_alignment(x, y):
    if min(len(x), len(y)) < 3*SR:
        raise ValueError("首尾提示音之间需要留出说话时间；本条太短，保留原音")
    detections = {role: {kind: detect_marker(audio, kind) for kind in PATTERNS}
                  for role, audio in (("input", x), ("reference", y))}
    for role, marks in detections.items():
        if marks["end"]["start_sample"] - marks["start"]["end_sample"] < SR:
            raise ValueError(f"{role} 首尾提示音顺序或间距异常；保留原音")
    tx, ty, correlations, anchors = [], [], [], []
    for kind in PATTERNS:
        for a, b in zip(detections["input"][kind]["pulses"], detections["reference"][kind]["pulses"]):
            tx.append(a["sample"]); ty.append(b["sample"])
            correlations.append(min(a["correlation"], b["correlation"]))
            anchors.append({"marker": kind, "input_sample": a["sample"],
                            "offset_samples": b["sample"]-a["sample"],
                            "correlation": min(a["correlation"], b["correlation"])})
    tx, ty = np.array(tx), np.array(ty)
    slope, offset = np.polyfit(tx, ty-tx, 1)
    error = ty - (tx*(1+slope) + offset)
    residual = float(np.sqrt(np.mean(error**2)))
    if abs(slope) > .005 or np.max(np.abs(error)) > 32:
        raise ValueError("提示音时差无法用稳定的线性漂移解释；保留原音，不生成裁剪版本")
    warnings = []
    if residual > 8:
        warnings.append("提示音拟合残差超过0.5ms；这不是整段真实误差的测量")
    if abs(slope) > .0015:
        warnings.append("估计时钟漂移超过1500ppm，请检查采样连续性")
    for role, audio in (("input", x), ("reference", y)):
        for kind, marker in detections[role].items():
            segment = audio[max(0, int(marker["start_sample"])):int(np.ceil(marker["end_sample"]))]
            if np.mean(np.abs(segment) >= 32767/32768) > .001:
                warnings.append(f"{role} {kind} 提示音削波，请降低扬声器音量")
    a, b = detections["input"], detections["reference"]
    begin = max(a["start"]["end_sample"] + START_GUARD_S*SR,
                (b["start"]["end_sample"] + START_GUARD_S*SR - offset)/(1+slope))
    end = min(a["end"]["start_sample"] - END_GUARD_S*SR,
              (b["end"]["start_sample"] - END_GUARD_S*SR - offset)/(1+slope))
    if end - begin < .5*SR:
        raise ValueError("去掉首尾提示音后不足0.5秒；保留原音")
    return {
        "method": PROTOCOL, "offset_samples": float(offset), "offset_ms": float(offset/SR*1000),
        "slope": float(slope), "drift_ppm": float(slope*1e6), "residual_samples": residual,
        "anchor_count": len(tx), "anchor_coverage": float(np.ptp(tx)/len(x)),
        "median_abs_correlation": float(np.median(correlations)), "anchors": anchors,
        "warnings": warnings, "automatic_quality_pass": not warnings,
        "mapping": "reference_sample = input_sample * (1 + slope) + offset_samples",
        "marker_detections": detections,
        "crop_input_start_sample": int(np.ceil(begin)), "crop_input_end_sample": int(np.floor(end)),
        "crop_policy": "between_bookends_keep_all_speech_and_internal_pauses_no_VAD",
        "start_guard_s": START_GUARD_S, "end_guard_s": END_GUARD_S,
        "accuracy_note": "首尾子脉冲拟合残差不是独立验证；没有测量整段0.5ms精度，不校正中途非线性跳变。",
    }
