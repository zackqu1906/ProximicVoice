#!/usr/bin/env python3
"""Standalone 200 Hz IMU -> host gesture model test; no application actions."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import queue
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ring_python_sdk import RingSession  # noqa: E402
from ring_python_sdk.ble import scan_all_devices  # noqa: E402
from ring_python_sdk.core.constants import IMU_ENCODE_RAW, INFO_COMP_IMU  # noqa: E402
from ring_python_sdk.gestures import (  # noqa: E402
    GESTURE_NAMES, GestureClassifier, GestureRecognizer, GestureWorker, ImuGestureAdapter,
)
from ring_python_sdk.imu.frame import apply_chip_to_host_physical  # noqa: E402
from ring_python_sdk.imu.processor import ImuSample  # noqa: E402


LABELS = ("无手势", "上滑", "下滑", "左滑", "右滑", "点击/捏合", "响指")
MODEL_PATH = PROJECT_ROOT / "src/ring_python_sdk/gestures/assets/swipe.pt"
SAMPLE_FIELDS = [
    "sample_index", "packet_seq", "uptime_ms", "received_at_utc", "elapsed_s",
    "ax_ms2", "ay_ms2", "az_ms2", "gx_dps", "gy_dps", "gz_dps",
    *(f"raw_{i}" for i in range(6)),
]
RESULT_FIELDS = [
    "source", "kind", "processed_at_utc", "elapsed_s", "device_timestamp_ms",
    "class_id", "name", "name_zh", "confidence", "gesture_index",
    "gesture_interval_device_ms", *(f"p{i}" for i in range(7)),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class HostRecorder:
    """Main-thread CSV writer; model callbacks only put immutable results in a queue."""

    def __init__(self, output_dir: Path, *, source: str, show_predictions: bool = False):
        self.output_dir = output_dir
        self.source = source
        self.show_predictions = show_predictions
        self.started = time.monotonic()
        self.results: queue.SimpleQueue = queue.SimpleQueue()
        self.received_samples = 0
        self.last_sample_at: float | None = None
        self.last_device_timestamp: float | None = None
        self.last_gesture_timestamp: float | None = None
        self.sample_steps: deque[float] = deque(maxlen=200)
        self.prediction_count = 0
        self.gesture_count = 0
        self.gesture_counts: Counter[str] = Counter()
        self.latest_prediction = None
        self.sample_error: Exception | None = None
        output_dir.mkdir(parents=True, exist_ok=False)
        self._sample_file = (output_dir / "samples.csv").open("x", newline="", encoding="utf-8-sig")
        try:
            self._result_file = (output_dir / "results.csv").open("x", newline="", encoding="utf-8-sig")
        except BaseException:
            self._sample_file.close()
            raise
        self._samples = csv.DictWriter(self._sample_file, fieldnames=SAMPLE_FIELDS)
        self._results = csv.DictWriter(self._result_file, fieldnames=RESULT_FIELDS)
        self._samples.writeheader()
        self._results.writeheader()
        self.flush()

    def on_prediction(self, prediction, timestamp_ms: float) -> None:
        self.results.put(("prediction", prediction, timestamp_ms, time.monotonic(), utc_now()))

    def on_gesture(self, event) -> None:
        self.results.put(("gesture", event, event.timestamp_ms, time.monotonic(), utc_now()))

    def record_sample(self, sample: ImuSample) -> None:
        now = time.monotonic()
        self.received_samples += 1
        self.last_sample_at = now
        if self.last_device_timestamp is not None:
            delta = (sample.uptime_ms - self.last_device_timestamp) % (1 << 32)
            if 0 < delta < 1000:
                self.sample_steps.append(delta)
        self.last_device_timestamp = sample.uptime_ms
        row = dict(zip(SAMPLE_FIELDS[5:11], (*sample.accel_ms2, *sample.gyro_dps)))
        row.update(sample_index=sample.sample_index, packet_seq=sample.packet_seq,
                   uptime_ms=sample.uptime_ms, received_at_utc=utc_now(), elapsed_s=now - self.started)
        if sample.raw is not None:
            row.update({f"raw_{i}": value for i, value in enumerate(sample.raw)})
        self._samples.writerow(row)

    def accept_sample(self, sample: ImuSample, worker: GestureWorker) -> None:
        # ImuProcessor isolates callback exceptions. Retain recording failures
        # here so the owner can stop instead of silently losing samples.
        if self.sample_error is not None:
            return
        try:
            self.record_sample(sample)
            worker.submit(sample)
        except Exception as exc:
            self.sample_error = exc

    def drain(self) -> None:
        while True:
            try:
                kind, result, device_ms, now, utc = self.results.get_nowait()
            except queue.Empty:
                break
            row = dict(source=self.source, kind=kind, processed_at_utc=utc,
                       elapsed_s=now - self.started, device_timestamp_ms=device_ms,
                       class_id=result.class_id, name=result.name,
                       name_zh=LABELS[result.class_id], confidence=result.confidence)
            row.update({f"p{i}": value for i, value in enumerate(result.probabilities)})
            if kind == "gesture":
                self.gesture_count += 1
                self.gesture_counts[result.name] += 1
                row["gesture_index"] = self.gesture_count
                if self.last_gesture_timestamp is not None:
                    delta = (device_ms - self.last_gesture_timestamp) % (1 << 32)
                    if delta < (1 << 31):
                        row["gesture_interval_device_ms"] = delta
                self.last_gesture_timestamp = device_ms
            else:
                self.prediction_count += 1
                self.latest_prediction = result
            self._results.writerow(row)
            if kind == "gesture" or self.show_predictions:
                count = f" #{self.gesture_count:04d}" if kind == "gesture" else ""
                print(
                    f"[{now - self.started:8.3f}s] {kind.upper()}{count} "
                    f"{LABELS[result.class_id]} ({result.name}) "
                    f"confidence={result.confidence:.3f} device={device_ms:.1f}ms",
                    flush=True,
                )
            if kind == "gesture":
                self._result_file.flush()

    @property
    def device_rate_hz(self) -> float | None:
        if not self.sample_steps:
            return None
        return 1000 * len(self.sample_steps) / sum(self.sample_steps)

    def status(self, worker: GestureWorker) -> None:
        self.drain()
        self.flush()
        stats = worker.snapshot()
        rate = "—" if self.device_rate_hz is None else f"{self.device_rate_hz:.1f}Hz"
        last = self.latest_prediction
        prediction = "尚未形成窗口" if last is None else f"{last.name} ({last.confidence:.3f})"
        print(
            f"[状态] IMU={self.received_samples} 设备采样≈{rate} "
            f"分类={self.prediction_count} 触发={self.gesture_count} 最近分类={prediction}\n"
            f"       队列={stats['queue_depth']} 丢弃样本={stats['dropped_samples']} "
            f"窗口重置={stats['window_resets']} 最大排队={stats['max_queue_wait_ms']:.1f}ms",
            flush=True,
        )
        if self.device_rate_hz is not None and not 180 <= self.device_rate_hz <= 220:
            print("[提示] 模型需要 200Hz 连续六轴数据；当前采样时间间隔异常，请检查采样设置/丢包。", flush=True)

    def flush(self) -> None:
        self._sample_file.flush()
        self._result_file.flush()

    def finish(self, *, worker, metadata: dict, reason: str, imu_stats: dict) -> None:
        try:
            self.drain()
        finally:
            self._sample_file.close()
            self._result_file.close()
        summary = dict(
            source=self.source, stop_reason=reason, device=metadata,
            duration_s=time.monotonic() - self.started,
            received_samples=self.received_samples, device_rate_hz=self.device_rate_hz,
            prediction_count=self.prediction_count, gesture_count=self.gesture_count,
            gesture_counts=dict(self.gesture_counts), worker=worker.snapshot() if worker else {},
            imu_stats=imu_stats,
            note="Host model outputs; counts are not accuracy and device intervals are not end-to-end latency.",
        )
        (self.output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n测试结束：{reason} | IMU={self.received_samples} 分类={self.prediction_count} 手势={self.gesture_count}")
        for name, count in self.gesture_counts.items():
            print(f"  {LABELS[GESTURE_NAMES.index(name)]} ({name}): {count}")
        print(f"记录目录：{self.output_dir}", flush=True)


def create_recognizer(args, recorder: HostRecorder) -> GestureRecognizer:
    # Load and warm up before touching BLE; no network or ASR dependencies.
    import numpy as np
    import torch

    torch.set_num_threads(args.torch_threads)
    classifier = GestureClassifier()
    classifier.predict(np.zeros((60, 6), dtype=np.float32))
    return GestureRecognizer(
        classifier=classifier,
        adapter=ImuGestureAdapter(mount_angle_deg=args.mount_angle, mount_radius_m=args.mount_radius),
        on_prediction=recorder.on_prediction, on_gesture=recorder.on_gesture,
        step_frames=args.step_frames, stable_window_seconds=args.stable_window,
        positive_ratio=args.positive_ratio, reset_delay_frames=args.cooldown_frames,
    )


def replay_samples(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not set(SAMPLE_FIELDS).issubset(reader.fieldnames or ()):
            raise ValueError("--replay 需要本程序保存的 samples.csv（含物理量和原始六轴值）")
        for row in reader:
            yield ImuSample(
                sample_index=int(row["sample_index"]), packet_seq=int(row["packet_seq"]),
                uptime_ms=float(row["uptime_ms"]),
                accel_ms2=tuple(float(row[key]) for key in ("ax_ms2", "ay_ms2", "az_ms2")),
                gyro_dps=tuple(float(row[key]) for key in ("gx_dps", "gy_dps", "gz_dps")),
                raw=tuple(int(row[f"raw_{i}"]) for i in range(6)),
            )


def demo_samples():
    # Stationary physical samples, fed through the real adapter/model. This is
    # a pipeline check; do not invent six successful gestures for a demo.
    physical = apply_chip_to_host_physical(0, 0, 9.80665, 0, 0, 0)
    for index in range(600):
        yield ImuSample(index, index // 10, index * 5.0, physical[:3], physical[3:], (0, 0, 2048, 0, 0, 0))


def requested_target(args) -> str:
    """Require a ring suffix (or an explicit exact name/platform identifier)."""
    if args.selector is not None:
        target = args.selector.strip()
        if not target:
            raise ValueError("--selector 不能为空")
        return target
    prefix = args.name.strip()
    if not prefix:
        raise ValueError("--name 名称前缀不能为空")
    number = args.ring_id
    if number is None:
        try:
            number = input(f"请输入 {prefix} 后面的编号（例如 2CC7）：")
        except (EOFError, OSError) as exc:
            raise ValueError("未输入戒指编号；请使用 --ring-id 2CC7 或 --selector 指定设备") from exc
    number = number.strip()
    if number.casefold().startswith(prefix.casefold()):
        number = number[len(prefix):].strip()
    if not number or not number.isascii() or not number.isalnum():
        raise ValueError("戒指编号不能为空，且只能包含字母和数字，例如 2CC7")
    return prefix + number


async def find_requested_device(target: str, timeout: float):
    devices = await scan_all_devices(timeout)
    matches = [item for item in devices if target.casefold() in {
        item.name.strip().casefold(), item.identifier.strip().casefold()
    }]
    if not matches:
        raise RuntimeError(f"未找到 {target}；请核对编号或用 --scan 查看完整名称。不会改连其他戒指。")
    if len(matches) > 1:
        identifiers = ", ".join(item.identifier for item in matches)
        raise RuntimeError(f"{target} 匹配到多台设备，请用 --selector 指定其中一个 MAC/UUID：{identifiers}")
    return matches[0]


async def run(args) -> int:
    if args.scan:
        for device in await scan_all_devices(args.timeout):
            print(f"{device.name or '?'}  {device.identifier}  RSSI={device.rssi}")
        return 0
    source = "replay" if args.replay else "synthetic_host" if args.demo else "live_host"
    output_dir = (args.output_dir or PROJECT_ROOT / "data/host_gestures" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")).expanduser().resolve()
    print("电脑端手势识别：Ring IMU → 200Hz 六轴 → 电脑模型 → 分类/触发")
    print("有效手势：上、下、左、右、点击/捏合、响指；empty 为无手势。")
    print(f"模式：{source} | 模型：{MODEL_PATH}\n记录目录：{output_dir}", flush=True)
    print(
        f"判定参数：每 {args.step_frames} 帧推理，稳定窗 {args.stable_window:g}s，"
        f"同类投票比例 {args.positive_ratio:g}，冷却 {args.cooldown_frames} 帧。",
        flush=True,
    )
    if args.dry_run:
        return 0
    target = requested_target(args) if source == "live_host" else None
    if target is not None:
        print(f"目标设备：{target}（完整名称/标识精确匹配，不区分大小写）", flush=True)
    recorder = HostRecorder(output_dir, source=source, show_predictions=args.show_predictions)
    session = None
    imu = None
    worker = None
    metadata = {"sample_hz": 200, "source": source,
                "settings": {key: getattr(args, key) for key in ("mount_angle", "mount_radius", "step_frames", "stable_window", "positive_ratio", "cooldown_frames", "torch_threads")}}
    reason = "completed"
    try:
        metadata["model_sha256"] = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
        print("正在加载并预热电脑端模型…", flush=True)
        recognizer = create_recognizer(args, recorder)
        worker = GestureWorker(recognizer)
        worker.start()
        print("模型已就绪。", flush=True)
        if args.replay or args.demo:
            print("[离线] 使用真实模型；合成/重放结果不代表本轮真人手势测试。", flush=True)
            samples = replay_samples(args.replay) if args.replay else demo_samples()
            for sample in samples:
                # Replay is faster than real time, but never overflows/drops
                # the recorded samples merely because disk reading is fast.
                while worker.snapshot()["queue_depth"] >= 30:
                    recorder.drain()
                    if worker.error is not None:
                        raise worker.error
                    await asyncio.sleep(0.005)
                recorder.accept_sample(sample, worker)
                if recorder.sample_error is not None:
                    raise recorder.sample_error
            return 0

        print("请在主程序/其他 BLE 工具中断开这枚戒指；Ctrl+C 停止。", flush=True)
        session = RingSession(name_keyword=args.name, timeout_s=args.timeout,
                              data_root=output_dir / "sdk", auto_reconnect=False, battery_poll_enabled=False)
        selected = await find_requested_device(target, args.timeout)
        metadata["requested_target"] = target
        print(f"已匹配：{selected.name}  {selected.identifier}", flush=True)
        connected = await session.connect_device(selected.device)
        if not connected:
            raise RuntimeError(f"未连接到 {target}；不会改连其他戒指，请检查设备后重试。")
        deadline = time.monotonic() + 2.0
        while session.device_info is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        info = session.device_info
        metadata.update(name=session.target_name, address=session.target_address,
                        firmware_version=info.fw_version if info else None,
                        hardware_revision=info.hw_rev if info else None)
        print(f"戒指：{session.target_name} 固件={metadata['firmware_version']}；电脑端识别不要求固件包含 Swipe 模型。", flush=True)
        component = next((c for c in getattr(info, "components", ()) if c.id == INFO_COMP_IMU), None)
        if component is not None and not component.present:
            raise RuntimeError("固件 INFO 声明没有 IMU，无法运行电脑端手势模型。")
        await session.imu_on(gyro_hz=200, accel_hz=200, gyro_fs=2000, accel_fs=16,
                             frames_per_packet=10, encode_mode=IMU_ENCODE_RAW, lp=False,
                             on_sample=lambda sample: recorder.accept_sample(sample, worker))
        imu = session.imu
        started = time.monotonic()
        next_status = started + args.status_interval
        print("IMU START 已发送，等待六轴样本…", flush=True)
        while not args.duration or time.monotonic() - started < args.duration:
            recorder.drain()
            if worker.error is not None:
                raise RuntimeError(f"电脑端识别线程失败：{worker.error}") from worker.error
            if recorder.sample_error is not None:
                raise RuntimeError(f"IMU 记录失败：{recorder.sample_error}") from recorder.sample_error
            if session.client is None or not session.client.is_connected:
                raise RuntimeError("蓝牙连接已断开，本次记录结束；请重新运行。")
            now = time.monotonic()
            last_sample = recorder.last_sample_at if recorder.last_sample_at is not None else started
            if now - last_sample > args.imu_timeout:
                raise RuntimeError(f"连续 {args.imu_timeout:g} 秒未收到 IMU 样本；请检查 IMU 数据流及固件上报状态。")
            if now >= next_status:
                recorder.status(worker)
                next_status = now + args.status_interval
            session.drain_live_logs()
            await asyncio.sleep(0.05)
        return 0
    except asyncio.CancelledError:
        reason = "interrupted"
        raise
    except Exception as exc:
        reason = f"error: {exc}"
        raise
    finally:
        cleanup_error = None
        try:
            if session is not None:
                await session.disconnect()
        except Exception as exc:
            cleanup_error = exc
        try:
            if worker is not None:
                worker.close()
                if worker.error is not None and cleanup_error is None:
                    cleanup_error = worker.error
        except Exception as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            reason += f"; cleanup/worker error: {cleanup_error}"
        recorder.finish(worker=worker, metadata=metadata, reason=reason,
                        imu_stats=asdict(imu.stats) if imu is not None else {})
        if cleanup_error is not None:
            raise RuntimeError(str(cleanup_error)) from cleanup_error


def parser():
    p = argparse.ArgumentParser(description="电脑端 GestureRecognizer 独立测试，不接按钮、不启动麦克风/ASR/LLM。")
    p.add_argument("--name", default="Ringo", help="广播名称前缀，默认 Ringo；仍需输入编号")
    target = p.add_mutually_exclusive_group()
    target.add_argument("--ring-id", help="Ringo 后面的编号，例如 2CC7；不填时启动后询问")
    target.add_argument("--selector", help="完整设备名、MAC 或 macOS UUID，精确匹配")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--duration", type=float, default=0.0, help="实时测试秒数；0 为直到 Ctrl+C")
    p.add_argument("--imu-timeout", type=float, default=10.0, help="IMU 连续无数据超时秒数")
    p.add_argument("--status-interval", type=float, default=5.0)
    p.add_argument("--output-dir", type=Path, help="新建记录目录；不会覆盖已有目录")
    p.add_argument("--show-predictions", action="store_true", help="同时打印每次分类，含 empty")
    p.add_argument("--mount-angle", type=float, default=135.0)
    p.add_argument("--mount-radius", type=float, default=0.01, help="安装杆臂长度，单位米")
    p.add_argument("--step-frames", type=int, default=5)
    p.add_argument("--stable-window", type=float, default=0.10)
    p.add_argument("--positive-ratio", type=float, default=1.0, help="稳定窗同类投票比例，不是置信度阈值")
    p.add_argument("--cooldown-frames", type=int, default=10)
    p.add_argument("--torch-threads", type=int, default=1)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--scan", action="store_true")
    mode.add_argument("--dry-run", action="store_true", help="只显示配置，不连接、不加载模型、不写文件")
    mode.add_argument("--demo", action="store_true", help="合成静止 IMU 经过真实模型；无需戒指")
    mode.add_argument("--replay", type=Path, help="重放本程序保存的 samples.csv；无需戒指")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = parser()
    args = p.parse_args(argv)
    for key in ("timeout", "imu_timeout", "status_interval", "stable_window"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            p.error(f"--{key.replace('_', '-')} 必须为有限正数")
    for key in ("duration", "mount_radius"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) < 0:
            p.error(f"--{key.replace('_', '-')} 必须为有限非负数")
    if not math.isfinite(args.mount_angle) or not 0 < args.positive_ratio <= 1:
        p.error("安装角度必须为有限数，投票比例必须在 (0,1] 内")
    if args.step_frames <= 0 or args.torch_threads <= 0 or args.cooldown_frames < 0:
        p.error("推理步长/线程数必须为正数，冷却帧数不能为负")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"测试失败：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
