#!/usr/bin/env python3
"""Inspect firmware swipe EVENT/TRIGGER packets without application actions."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import csv
import json
import math
from pathlib import Path
import struct
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ring_python_sdk import RingSession  # noqa: E402
from ring_python_sdk.ble import scan_all_devices  # noqa: E402
from ring_python_sdk.core.constants import (  # noqa: E402
    INFO_COMP_SWIPE, INFO_SWIPE_MODEL_NONE,
    SWIPE_CLASS_LABELS_V2, SWIPE_GESTURE_IDS_V2,
)
from ring_python_sdk.swipe import SwipeProcessor, SwipeResult  # noqa: E402


GESTURE_LABELS = {
    "empty": "无手势", "swipe-up": "上滑", "swipe-down": "下滑",
    "swipe-left": "左滑", "swipe-right": "右滑", "tap": "点击",
    "swipe-tap": "点击", "snap": "响指", "clench": "握拳",
    "index-pinch": "食指捏合", "middle-pinch": "中指捏合",
    "circle-clockwise": "顺时针画圈", "circle-counterclockwise": "逆时针画圈",
}
CSV_FIELDS = [
    "source", "received_at_utc", "elapsed_s", "kind", "protocol_version",
    "seq", "class_id", "name", "name_zh", "confidence", "uptime_ms",
    "center_uptime_ms", "event_mass", "trigger_index",
    "trigger_interval_host_ms", "trigger_interval_device_ms",
    *(f"s{i}" for i in range(7)), *(f"p{i}" for i in SWIPE_GESTURE_IDS_V2),
]


def device_interval_ms(current: int, previous: int | None) -> int | None:
    """Support uptime rollover; a backward/reset clock is not a huge interval."""
    if previous is None:
        return None
    delta = (current - previous) & 0xFFFFFFFF
    return delta if delta < 0x80000000 else None


def firmware_swipe_capability(info) -> dict:
    """Trust an explicit INFO declaration, not a version-number assumption.

    Missing INFO or an older table without SWIPE is unknown and must not block
    testing legacy firmware. An explicit absent/none component cannot infer.
    """
    component = next(
        (item for item in getattr(info, "components", ()) if item.id == INFO_COMP_SWIPE),
        None,
    )
    if component is None:
        return {"status": "unknown"}
    return {
        "status": "available" if component.present and component.model != INFO_SWIPE_MODEL_NONE else "unavailable",
        "present": bool(component.present), "count": component.count,
        "model": component.model, "model_name": component.model_name,
        "flags": component.flags,
    }


class GestureRecorder:
    """Write all classifications, but count/print triggers separately."""

    def __init__(self, csv_path: Path, *, show_events: bool = False, demo: bool = False):
        self.csv_path = csv_path
        self.summary_path = csv_path.with_suffix(".summary.json")
        self.source = "demo" if demo else "firmware"
        self.show_events = show_events
        self.started = time.monotonic()
        self.received_count = 0
        self.event_count = 0
        self.nonempty_event_count = 0
        self.trigger_count = 0
        self.trigger_counts: Counter[str] = Counter()
        self.protocols: set[int] = set()
        self.last_trigger_host: float | None = None
        self.last_trigger_device: dict[int, int] = {}
        self.last_packet_host: float | None = None
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        if csv_path.exists() or self.summary_path.exists():
            raise FileExistsError(f"输出已存在，请换一个 --csv 路径：{csv_path}")
        self._file = csv_path.open("x", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._file, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        self._file.flush()

    def observe(self, result: SwipeResult) -> None:
        now = time.monotonic()
        elapsed = now - self.started
        self.received_count += 1
        self.last_packet_host = now
        if result.protocol_version not in self.protocols:
            self.protocols.add(result.protocol_version)
            detail = "12 类（11 个有效手势）的概率结果" if result.protocol_version == 2 else "7 类旧版分数，无概率置信度"
            print(f"[协议] 已收到 V{result.protocol_version}：{detail}", flush=True)
        row = {
            "source": self.source,
            "received_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_s": f"{elapsed:.6f}", "kind": result.kind,
            "protocol_version": result.protocol_version, "seq": result.seq,
            "class_id": result.class_id, "name": result.name,
            "name_zh": GESTURE_LABELS.get(result.name, result.name),
            "confidence": result.confidence, "uptime_ms": result.uptime_ms,
            "center_uptime_ms": result.center_uptime_ms, "event_mass": result.event_mass,
        }
        row.update({f"s{i}": value for i, value in enumerate(result.scores)})
        row.update({f"p{i}": value for i, value in zip(SWIPE_GESTURE_IDS_V2, result.probabilities)})
        interval = None
        if result.kind == "trigger":
            self.trigger_count += 1
            self.trigger_counts[result.name] += 1
            if self.last_trigger_host is not None:
                interval = (now - self.last_trigger_host) * 1000
            row.update({
                "trigger_index": self.trigger_count,
                "trigger_interval_host_ms": interval,
                "trigger_interval_device_ms": device_interval_ms(
                    result.uptime_ms, self.last_trigger_device.get(result.protocol_version)
                ),
            })
            self.last_trigger_host = now
            self.last_trigger_device[result.protocol_version] = result.uptime_ms
        else:
            self.event_count += 1
            self.nonempty_event_count += int(result.class_id != 0)
        self._writer.writerow(row)
        if result.kind == "trigger":
            self._file.flush()
        if result.kind == "trigger" or self.show_events:
            confidence = "N/A（旧版 scores）" if result.confidence is None else f"{result.confidence:.3f}"
            gap = "—" if interval is None else f"{interval:.0f}ms"
            count = f" #{self.trigger_count:04d}" if result.kind == "trigger" else ""
            print(
                f"[{elapsed:8.3f}s] {result.kind.upper()}{count} "
                f"{row['name_zh']} ({result.name})  V{result.protocol_version} "
                f"id={result.class_id} seq={result.seq} confidence={confidence} "
                f"uptime={result.uptime_ms}ms"
                + (f"  距上次触发={gap}" if result.kind == "trigger" else ""),
                flush=True,
            )

    def status(self, processor: SwipeProcessor | None) -> None:
        self._file.flush()
        stats = processor.stats if processor is not None else None
        age = "尚未收到分类/触发包" if self.last_packet_host is None else f"距最近结果 {time.monotonic() - self.last_packet_host:.1f}s"
        print(
            f"[状态] EVENT={self.event_count}（非空={self.nonempty_event_count}） "
            f"TRIGGER={self.trigger_count} | {age}"
            + (f" | 无效包={stats.invalid_packet_count} 序号缺口={stats.dropped_packet_count}"
               f" 重复包={stats.duplicate_packet_count} 乱序包={stats.out_of_order_packet_count}"
               if stats else ""),
            flush=True,
        )
        if self.received_count == 0:
            print("  START 已发送不等于固件已确认；请尝试手势，并核对固件是否支持 Swipe。", flush=True)

    def finish(self, *, metadata: dict, processor: SwipeProcessor | None, reason: str) -> None:
        self._file.close()
        summary = {
            "source": self.source, "stop_reason": reason, "device": metadata,
            "duration_s": time.monotonic() - self.started,
            "protocols_observed": sorted(self.protocols),
            "event_count": self.event_count, "nonempty_event_count": self.nonempty_event_count,
            "trigger_count": self.trigger_count,
            "trigger_counts": dict(self.trigger_counts),
            "sdk_stats": asdict(processor.stats) if processor else {},
            "csv_path": str(self.csv_path),
            "note": "Raw firmware trigger packets, including repeats. Counts are not accuracy; intervals are not end-to-end recognition latency.",
        }
        with self.summary_path.open("x", encoding="utf-8") as output:
            json.dump(summary, output, ensure_ascii=False, indent=2)
            output.write("\n")
        print(f"\n测试结束：{reason}，共 {self.trigger_count} 个 TRIGGER", flush=True)
        for name, count in self.trigger_counts.items():
            print(f"  {GESTURE_LABELS.get(name, name)} ({name}): {count}")
        print(f"完整记录：{self.csv_path}\n统计摘要：{self.summary_path}", flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="固件端手势测试：仅接收结果，无电脑模型、麦克风或按钮动作。")
    result.add_argument("--name", default="Ringo", help="设备名称关键词（默认 Ringo）")
    result.add_argument("--selector", default="", help="精确设备名、MAC 或 macOS 蓝牙 UUID")
    result.add_argument("--timeout", type=float, default=8.0, help="扫描/连接超时秒数")
    result.add_argument("--duration", type=float, default=0.0, help="测试秒数；0 表示直到 Ctrl+C")
    result.add_argument("--status-interval", type=float, default=5.0, help="状态汇总间隔秒数")
    result.add_argument("--csv", type=Path, help="完整 EVENT/TRIGGER CSV；不会覆盖已有文件")
    result.add_argument("--show-events", action="store_true", help="终端同时打印逐次分类（含 empty，输出较多）")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--scan", action="store_true", help="仅列出附近 BLE 设备，不连接")
    mode.add_argument("--demo", action="store_true", help="用合成协议包演示全部 11 个手势，不连接戒指")
    mode.add_argument("--dry-run", action="store_true", help="打印配置与手势清单，不连接、不写文件")
    return result


def demo_packet(class_id: int, *, seq: int, trigger: bool) -> bytearray:
    probabilities = [0.0] * len(SWIPE_GESTURE_IDS_V2)
    probabilities[SWIPE_GESTURE_IDS_V2.index(class_id)] = 1.0
    uptime = 2000 + seq * 500
    data = bytes([0x26, 6 if trigger else 5]) + struct.pack(
        "<HB12fI", seq, class_id, *probabilities, uptime
    )
    if trigger:
        data += struct.pack("<If", uptime - 150, 0.9)
    return bytearray(data)


async def run(args: argparse.Namespace) -> int:
    if args.scan:
        devices = await scan_all_devices(args.timeout)
        for device in devices:
            print(f"{device.name or '?'}  {device.identifier}  RSSI={device.rssi}")
        if not devices:
            print("未发现设备。请检查蓝牙与戒指状态。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    prefix = "demo_" if args.demo else ""
    csv_path = (args.csv or PROJECT_ROOT / "data" / "firmware_gestures" / f"{prefix}{stamp}" / "events.csv").expanduser().resolve()
    print("固件端手势测试 — 仅观察 EVENT / TRIGGER")
    print(f"设备：{args.selector or args.name} | 时长：{args.duration or '直到 Ctrl+C'} | CSV：{csv_path}")
    print("手势：" + "、".join(GESTURE_LABELS[name] for key, name in SWIPE_CLASS_LABELS_V2.items() if key))
    if args.dry_run:
        return 0
    if args.demo:
        print("[DEMO] 以下为合成协议包，只验证程序，不代表戒指识别效果。", flush=True)
    else:
        print("请先在主程序及其他 BLE 工具中断开这枚戒指；Ctrl+C 停止并保存。", flush=True)

    recorder = GestureRecorder(csv_path, show_events=args.show_events, demo=args.demo)
    session = None
    processor = None
    metadata: dict = {}
    reason = "completed"
    try:
        if args.demo:
            processor = SwipeProcessor(
                csv_path.parent / f"{csv_path.stem}.sdk.csv", log=lambda _line: None,
                print_events=False, print_triggers=False, print_profile=False,
                on_event=recorder.observe, on_trigger=recorder.observe,
            )
            for seq, class_id in enumerate(SWIPE_GESTURE_IDS_V2):
                processor.handle_notification(None, demo_packet(class_id, seq=seq, trigger=False))
                if class_id:
                    processor.handle_notification(None, demo_packet(class_id, seq=seq, trigger=True))
                await asyncio.sleep(0.05)
            recorder.status(processor)
            return 0

        session = RingSession(
            name_keyword=args.name, timeout_s=args.timeout,
            data_root=csv_path.parent / "sdk", auto_reconnect=False,
            battery_poll_enabled=False,
        )
        connected = await (session.connect_target(args.selector) if args.selector else session.connect())
        if not connected:
            raise RuntimeError("连接失败；可先用 --scan 查找设备，再用 --selector 指定 MAC/UUID。")
        # INFO is queried by connect; wait briefly for the asynchronous reply.
        info_deadline = time.monotonic() + 2.0
        while session.device_info is None and time.monotonic() < info_deadline:
            await asyncio.sleep(0.05)
        metadata = {
            "name": session.target_name, "address": session.target_address,
            "firmware_version": session.device_info.fw_version if session.device_info else None,
            "hardware_revision": session.device_info.hw_rev if session.device_info else None,
            "firmware_swipe": firmware_swipe_capability(session.device_info),
        }
        print(f"固件版本：{metadata['firmware_version'] or '未读到'}；实际协议以收到的数据包为准。", flush=True)
        capability = metadata["firmware_swipe"]
        if capability["status"] == "unavailable":
            raise RuntimeError(
                f"当前固件 {metadata['firmware_version']} 的 INFO 明确声明 "
                f"Swipe present={int(capability['present'])}, model={capability['model_name']}。"
                "固件未提供板端手势模型，本次不发送 Swipe START。"
                "请向设备提供方确认适配这枚戒指、包含 Swipe 模型的固件；"
                "仅更新电脑 SDK 不会给戒指安装模型。"
            )
        if capability["status"] == "available":
            print(f"固件 Swipe 能力：{capability['model_name']}（flags={capability['flags']}）", flush=True)
        else:
            print("固件未返回明确的 Swipe 能力声明，继续尝试接收，不能据此认定支持。", flush=True)
        await session.swipe_on(
            on_event=recorder.observe, on_trigger=recorder.observe,
            print_events=False, print_triggers=False, print_profile=False,
        )
        processor = session.swipe
        print("Swipe START 已发送，等待固件结果…", flush=True)
        capture_started = time.monotonic()
        next_status = capture_started + args.status_interval
        while not args.duration or time.monotonic() - capture_started < args.duration:
            if session.client is None or not session.client.is_connected:
                raise RuntimeError("蓝牙连接已断开；本次记录结束，请重新运行连接。")
            for line in session.drain_live_logs():
                if "callback failed" in line:
                    print(f"[SDK] {line}", flush=True)
            if processor is not None and processor.stats.callback_error_count:
                raise RuntimeError("手势记录回调失败，请检查输出路径/磁盘或终端。")
            if time.monotonic() >= next_status:
                recorder.status(processor)
                next_status = time.monotonic() + args.status_interval
            await asyncio.sleep(0.1)
        return 0
    except asyncio.CancelledError:
        reason = "interrupted"
        raise
    except Exception as exc:
        reason = f"error: {exc}"
        raise
    finally:
        try:
            if session is not None:
                await session.disconnect()
        except Exception as exc:
            reason += f"; cleanup error: {exc}"
            print(f"停止/断开时出错：{exc}", file=sys.stderr)
        finally:
            if processor is not None:
                processor.close()
            recorder.finish(metadata=metadata, processor=processor, reason=reason)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    cli = parser()
    args = cli.parse_args(argv)
    for name in ("timeout", "status_interval"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            cli.error(f"--{name.replace('_', '-')} 必须为正数")
    if not math.isfinite(args.duration) or args.duration < 0:
        cli.error("--duration 必须为有限非负数")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"测试失败：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
