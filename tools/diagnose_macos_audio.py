"""Inspect the latest installed-app Ring capture and relevant macOS logs."""

from __future__ import annotations

import argparse
from array import array
import math
from pathlib import Path
import sys
import wave


DEFAULT_DATA_ROOT = Path.home() / "Library/Application Support/ProxiMic Voice"
LOG_MARKERS = (
    "[DISCONNECT]",
    "Ring BLE disconnect",
    "Ring BLE connection",
    "PCM STREAM STALLED",
    "[ASR TIMING]",
    "mic capturing",
    "encoding=",
    "识别完成",
    "设备已断开",
)


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-12))


def _latest_wav(data_root: Path) -> Path | None:
    session_root = data_root / "data" / "session"
    candidates = (
        [path for path in session_root.rglob("ring_audio*.wav") if path.is_file()]
        if session_root.is_dir()
        else []
    )
    if not candidates:
        candidates = [path for path in data_root.rglob("*.wav") if path.is_file()]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def _analyze_wav(path: Path) -> dict[str, object]:
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        frame_count = reader.getnframes()
        payload = reader.readframes(frame_count)

    if sample_width != 2:
        return {
            "path": path,
            "channels": channels,
            "sample_width": sample_width,
            "sample_rate": sample_rate,
            "frames": frame_count,
            "duration_s": frame_count / sample_rate if sample_rate else 0.0,
            "error": "只支持分析 PCM16 WAV",
        }

    samples = array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    count = len(samples)
    square_sum = 0.0
    total = 0
    peak = 0
    near_zero = 0
    clipped = 0
    longest_near_zero = 0
    current_near_zero = 0
    large_jumps = 0
    previous: int | None = None
    for sample in samples:
        value = int(sample)
        magnitude = abs(value)
        total += value
        square_sum += float(value * value)
        peak = max(peak, magnitude)
        if magnitude < 64:
            near_zero += 1
            current_near_zero += 1
            longest_near_zero = max(longest_near_zero, current_near_zero)
        else:
            current_near_zero = 0
        if magnitude >= 32760:
            clipped += 1
        if previous is not None and abs(value - previous) >= 24000:
            large_jumps += 1
        previous = value

    normalized_rms = (
        math.sqrt(square_sum / count) / 32768.0 if count else 0.0
    )
    normalized_peak = peak / 32768.0
    return {
        "path": path,
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "frames": frame_count,
        "samples": count,
        "duration_s": frame_count / sample_rate if sample_rate else 0.0,
        "rms_dbfs": _dbfs(normalized_rms),
        "peak_dbfs": _dbfs(normalized_peak),
        "dc_offset": total / count / 32768.0 if count else 0.0,
        "near_zero_pct": 100.0 * near_zero / count if count else 0.0,
        "clipped_pct": 100.0 * clipped / count if count else 0.0,
        "longest_near_zero_s": (
            longest_near_zero / (sample_rate * max(channels, 1))
            if sample_rate
            else 0.0
        ),
        "large_jump_count": large_jumps,
    }


def _print_audio_report(report: dict[str, object]) -> None:
    print("=== Latest Ring WAV ===")
    for key, value in report.items():
        print(f"{key}: {value}")

    findings: list[str] = []
    if report.get("error"):
        findings.append(str(report["error"]))
    if (
        report.get("sample_rate") != 16_000
        or report.get("channels") != 1
        or report.get("sample_width") != 2
    ):
        findings.append("音频格式不是期望的 16 kHz / 单声道 / PCM16")
    duration = float(report.get("duration_s", 0.0))
    rms_dbfs = float(report.get("rms_dbfs", -240.0))
    near_zero = float(report.get("near_zero_pct", 100.0))
    clipped = float(report.get("clipped_pct", 0.0))
    if duration < 1.0:
        findings.append("录音不足 1 秒，优先排查 MIC 启动或 BLE 提前断开")
    if rms_dbfs < -45.0:
        findings.append("整体电平很低，ASR 容易漏字或产生短句误识别")
    if near_zero > 95.0:
        findings.append("录音绝大部分接近数字静音")
    if clipped > 0.1:
        findings.append("存在明显削波，需检查 Ring 麦克风增益")
    if not findings:
        findings.append("基础格式和幅度未见明显异常；下一步必须实际试听 WAV")

    print("\n=== Audio findings ===")
    for finding in findings:
        print("- " + finding)
    print(f"- 试听命令: open {report['path']!s}")


def _print_log_evidence(log_path: Path, tail_lines: int) -> None:
    print("\n=== Relevant startup.log evidence ===")
    print(f"log: {log_path}")
    if not log_path.is_file():
        print("startup.log 不存在")
        return
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = [
        line for line in lines[-tail_lines:] if any(marker in line for marker in LOG_MARKERS)
    ]
    if not selected:
        print(f"最近 {tail_lines} 行没有匹配到断链/ASR 诊断标记")
        return
    for line in selected:
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze the latest Proximic Voice macOS Ring capture and log."
    )
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--wav", type=Path, default=None)
    parser.add_argument("--log-lines", type=int, default=500)
    args = parser.parse_args()

    data_root = args.data_root.expanduser().resolve()
    wav_path = args.wav.expanduser().resolve() if args.wav else _latest_wav(data_root)
    if wav_path is None:
        print(f"没有在 {data_root} 下找到 WAV", file=sys.stderr)
        return 2
    _print_audio_report(_analyze_wav(wav_path))
    _print_log_evidence(data_root / "logs/startup.log", max(1, args.log_lines))
    print(
        "\n判断顺序：WAV 本身失真/截断 -> BLE、编码或固件；"
        "WAV 清晰但结果错误 -> ASR 后端、语言和分段；"
        "断开紧跟高耗时 ASR TIMING -> CPU 调度竞争。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
