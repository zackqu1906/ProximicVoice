"""Estimate reference_index = input_index * (1 + drift) + offset.

Band limiting is used ONLY for estimating alignment. Output retains original
levels and spectrum; reference is linearly resampled onto the input clock.
Low-confidence results are previews, never silently accepted training targets.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
from scipy import signal

from .storage import SAMPLE_RATE, audio_stats, read_json, read_wav, utc_now, write_json, write_wav


def _features(x):
    sos = signal.butter(4, [400, 6000], "bandpass", fs=SAMPLE_RATE, output="sos")
    return signal.sosfiltfilt(sos, np.asarray(x, dtype=np.float64))


def _match(template, region):
    """Normalized cross correlation for every valid template placement."""
    n = len(template)
    numerator = signal.correlate(region, template, mode="valid", method="fft")
    cumulative = np.concatenate(([0.], np.cumsum(region*region)))
    energy = cumulative[n:] - cumulative[:-n]
    scores = numerator / np.sqrt(np.maximum(energy * np.sum(template*template), 1e-20))
    index = int(np.argmax(np.abs(scores)))
    # Subsample peak interpolation; retain polarity for quality reporting.
    shift = 0.
    if 0 < index < len(scores)-1:
        a, b, c = np.abs(scores[index-1:index+2])
        denom = a - 2*b + c
        if abs(denom) > 1e-12:
            shift = float(np.clip(.5*(a-c)/denom, -.5, .5))
    return index + shift, float(scores[index])


def estimate_alignment(x, y, *, max_offset_s=5.0):
    if min(len(x), len(y)) < 2*SAMPLE_RATE:
        raise ValueError("对齐至少需要两路各2秒；建议录10–60秒，并在首尾加入同步声音")
    fx, fy = _features(x), _features(y)
    # Coarse offset on the first 30 seconds, decimated to reduce FFT memory.
    n = min(len(fx), len(fy), 30*SAMPLE_RATE)
    a = signal.resample_poly(fx[:n], 1, 4)
    b = signal.resample_poly(fy[:n], 1, 4)
    corr = signal.correlate(b, a, method="fft")
    lags = signal.correlation_lags(len(b), len(a))
    valid = np.abs(lags) <= max_offset_s*SAMPLE_RATE/4
    coarse = float(lags[valid][np.argmax(np.abs(corr[valid]))]*4)
    duration = min(len(fx), len(fy)) / SAMPLE_RATE
    # Short windows avoid decorrelation within an anchor from clock drift,
    # particularly for whisper-like aperiodic signals.
    window = min(.35, duration/3)*SAMPLE_RATE
    window = int(window)
    radius = int(.15*SAMPLE_RATE + duration*.002*SAMPLE_RATE)
    centers = np.linspace(window/2, len(fx)-window/2-1, min(80, max(4, int(duration))))
    anchors = []
    for center in centers:
        start = int(center-window/2)
        template = fx[start:start+window]
        if np.sqrt(np.mean(template*template)) < 1e-5:
            continue
        lo = max(0, int(start+coarse-radius))
        hi = min(len(fy), int(start+coarse+window+radius))
        if hi-lo < window:
            continue
        index, score = _match(template, fy[lo:hi])
        anchors.append({"input_sample": int(center), "offset_samples": float(lo+index-start),
                        "correlation": score})
    good = [p for p in anchors if abs(p["correlation"]) >= .3]
    warnings = []
    slope = 0.
    offset = coarse
    residual = None
    coverage = 0.
    fit_points = []
    if len(good) >= 3:
        tx = np.array([p["input_sample"] for p in good], dtype=float)
        ly = np.array([p["offset_samples"] for p in good])
        # Theil-Sen initialization, then reject outliers before least squares.
        slopes = [(ly[j]-ly[i])/(tx[j]-tx[i]) for i in range(len(tx)) for j in range(i+1,len(tx)) if tx[j]-tx[i] > SAMPLE_RATE*.5]
        slope = float(np.median(slopes)) if slopes else 0.
        offset = float(np.median(ly-slope*tx))
        err = ly-(offset+slope*tx)
        mad = float(np.median(np.abs(err-np.median(err))))
        keep = np.abs(err) <= max(8., 3*1.4826*mad)
        if np.sum(keep) >= 3:
            slope, offset = np.polyfit(tx[keep], ly[keep], 1)
            residual = float(np.sqrt(np.mean((ly[keep]-(offset+slope*tx[keep]))**2)))
            coverage = float(np.ptp(tx[keep])/len(x))
            fit_points = [p for p,k in zip(good, keep) if k]
    if len(fit_points) < 4 or coverage < .5:
        warnings.append("有效对齐锚点不足或覆盖不足，时钟漂移估计不可靠")
    if residual is None or residual > 16:
        warnings.append("对齐残差超过1ms或无法估计，需复核")
    if abs(slope*1e6) > 1500:
        warnings.append("估计漂移超过1500ppm，可能存在丢帧或错误匹配")
    median_corr = float(np.median([abs(p["correlation"]) for p in fit_points])) if fit_points else 0.
    if median_corr < .45:
        warnings.append("双路相关性偏低，请检查参考麦、同步声音和噪声")
    return {"offset_samples": float(offset), "offset_ms": float(offset/SAMPLE_RATE*1000),
            "drift_ppm": float(slope*1e6), "slope": float(slope),
            "residual_samples": residual, "anchor_coverage": coverage,
            "median_abs_correlation": median_corr, "anchor_count": len(fit_points),
            "anchors": anchors, "warnings": warnings, "automatic_quality_pass": not warnings,
            "mapping": "reference_sample = input_sample * (1 + slope) + offset_samples"}


def align_take(path: Path, *, manual_offset_ms=None, manual_drift_ppm=None) -> dict:
    record = read_json(path / "record.json")
    x, y = (read_wav(path / "raw" / f"{r}.wav") for r in ("input", "reference"))
    from .sync_markers import PROTOCOL, estimate_marker_alignment
    if record.get("metadata", {}).get("sync_protocol") == PROTOCOL:
        if manual_offset_ms is not None or manual_drift_ppm is not None:
            raise ValueError("提示音录音不使用手动覆盖；必须重新检出首尾标记")
        if any(q.get("gaps") or q.get("errors") or q.get("incomplete_frames")
               for q in record.get("quality", {}).values()):
            raise ValueError("采集存在丢帧或解码异常，无法保证首尾之间的时间轴；保留原音")
        result = estimate_marker_alignment(x, y)
    else:
        result = estimate_alignment(x, y)
        result["method"] = "bandlimited_normalized_xcorr_robust_affine"
    if manual_offset_ms is not None or manual_drift_ppm is not None:
        if manual_offset_ms is None or manual_drift_ppm is None:
            raise ValueError("Manual offset and drift must both be provided")
        if not np.isfinite([manual_offset_ms, manual_drift_ppm]).all():
            raise ValueError("Manual alignment must be finite")
        result["automatic_estimate"] = {k: result[k] for k in ("offset_samples", "drift_ppm", "slope")}
        result.update(offset_samples=float(manual_offset_ms)*SAMPLE_RATE/1000,
                      offset_ms=float(manual_offset_ms), drift_ppm=float(manual_drift_ppm),
                      slope=float(manual_drift_ppm)/1e6, method="manual_affine")
        # A manual transform does not inherit an automatic estimate's approval.
        result["automatic_quality_pass"] = False
        result["warnings"].append("手动对齐仅生成试听预览；需重新自动校验后才能导出训练集")
    slope, offset = result["slope"], result["offset_samples"]
    if abs(slope) > .005:
        raise ValueError("漂移超出±5000ppm，请检查录音完整性")
    start = max(0, int(np.ceil(-offset/(1+slope))))
    end = min(len(x), int(np.floor((len(y)-1-offset)/(1+slope)))+1)
    start = max(start, result.get("crop_input_start_sample", start))
    end = min(end, result.get("crop_input_end_sample", end))
    if end-start < SAMPLE_RATE*.5:
        raise ValueError("两路有效重叠不足0.5秒")
    positions = np.arange(start,end, dtype=np.float64)*(1+slope)+offset
    aligned_x = x[start:end]
    aligned_y = np.interp(positions, np.arange(len(y)), y).astype(np.float32)
    # New immutable processing revision; the original capture is never replaced.
    revision = "aligned_" + uuid.uuid4().hex[:10]
    out = path / revision
    write_wav(out / "input.wav", aligned_x)
    write_wav(out / "reference.wav", aligned_y)
    write_wav(out / "stereo.wav", np.column_stack((aligned_x,aligned_y)))
    result.update(created_at=utc_now(), revision=revision, input_start_sample=start,
                  input_end_sample=end, sample_count=len(aligned_x), duration_s=len(aligned_x)/SAMPLE_RATE,
                  output_gain_db=0, resampler="linear_interpolation_reference_only",
                  input_stats=audio_stats(aligned_x), reference_stats=audio_stats(aligned_y))
    for role, q in record.get("quality", {}).items():
        if q.get("gaps") or q.get("errors") or q.get("incomplete_frames"):
            result["warnings"].append(f"{role} 有丢帧/解码异常，此录音不自动进入训练集")
            result["automatic_quality_pass"] = False
    if record.get("status") != "captured":
        result["warnings"].append("采集状态异常或中断，不能自动导出")
        result["automatic_quality_pass"] = False
    if result["reference_stats"]["clipped_fraction"] > .001:
        result["warnings"].append("参考路削波超过0.1%，请检查增益和防喷罩")
        result["automatic_quality_pass"] = False
    write_json(out / "alignment.json", result)
    record["alignment"] = result
    record["review"] = {"decision": "pending", "transcript": record.get("review",{}).get("transcript", ""),
                        "notes": "自动处理完成；未进行人工标注，试听不需要复核"}
    write_json(path / "record.json", record)
    return result
