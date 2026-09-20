"""User-started ASR simulation through the real IME and production controller."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
TARGET_APPLICATIONS = frozenset({"com.openai.codex", "com.tencent.xinWeChat"})


class PreviewWaitState:
    """Pure timing/diagnostic policy; it neither reads nor controls any app."""

    def __init__(self, now: float, delay: float, *, ready_timeout=45, begin_timeout=5):
        self.prepared_at = now + max(1, delay)
        self.ready_deadline = self.prepared_at + ready_timeout
        self.begin_timeout = begin_timeout
        self.begin_at = None
        self.confirmed = False

    def mark_begin(self, now: float) -> None:
        self.begin_at = now

    def evaluate(self, now: float, *, connected: bool, ready: bool, application: str,
                 phase: str, connection_status: str, transport_error: str = "",
                 error: str = "") -> tuple[str, str, str]:
        """Return action, stable stage, and an accurate user-facing explanation."""
        if transport_error:
            occupied = any(marker in transport_error for marker in ("Errno 35", "Errno 11", "Resource temporarily unavailable"))
            advice = ("请先退出正在运行的主程序或其他体验脚本" if occupied else
                      "请按上述错误检查主机启动条件")
            return ("fail", "bridge_error", "输入法主机启动失败：" + transport_error
                    + "。" + advice + "；本次尚未开始输入。")
        if self.begin_at is not None:
            if not connected:
                return "fail", "connection_lost", "起句后输入法组件已断开连接，已停止继续输入。"
            if phase == "error":
                return "fail", "begin_error" if not self.confirmed else "session_error", error or "输入法组件报告输入失败。"
            if phase in {"interrupted", "undone"}:
                return "stopped", phase, "本句已撤销或由用户接管，已停止模拟语音。"
            if phase in {"listening", "finishing", "dictated", "editing", "edited"}:
                first_confirmation = not self.confirmed
                self.confirmed = True
                return ("confirm" if first_confirmation else "active", "active", "已收到真实输入法事务确认。")
            if self.confirmed:
                return "active", "active", "输入法事务正在运行。"
            if now >= self.begin_at + self.begin_timeout:
                return ("fail", "begin_timeout",
                        f"已向目标组件请求开始，但 {self.begin_timeout:g} 秒内未收到 listening／finishing 确认"
                        f"（当前状态：{phase or '未知'}）。尚未开始逐段模拟输入。")
            return "wait", "waiting_begin", "已请求开始，正在等待输入法确认本句；确认后才逐段输入。"
        if not connected:
            stage = "waiting_connection"
            reason = "主机通信已启动，但尚未收到输入法组件连接。" + connection_status
        elif not ready:
            stage = "waiting_client"
            reason = "输入法组件已连接，但没有就绪的输入会话；请手动选中 ProxiMic 语音并点入目标文本框。"
        elif application not in TARGET_APPLICATIONS:
            stage = "wrong_application"
            reason = f"输入法会话已就绪，但当前应用是 {application or '未知应用'}；正在等待 Codex 或微信输入框。"
        elif now < self.prepared_at:
            return "wait", "preparing", "目标输入法会话已就绪，正在等待准备倒计时结束。"
        else:
            return "begin", "ready", "目标输入法会话已就绪，开始请求本句。"
        if now >= self.ready_deadline:
            return "fail", stage + "_timeout", "等待超时：" + reason + " 本次没有发送任何文本。"
        return "wait", stage, reason


def main() -> int:
    parser = argparse.ArgumentParser(description="真实输入法体验：只模拟 ASR，不连接戒指，不自动发送消息")
    parser.add_argument("--edit", action="store_true", help="模拟编辑指令；主动转换时使用当前模型")
    parser.add_argument("--delay", type=int, default=10, help="手动选择输入法并切到输入框的准备时间")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("此体验仅支持 macOS")
    print("[启动] 正在建立输入法体验主机；将立即报告连接状态。", flush=True)
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from proximic_ring.runtime_paths import configure_runtime_environment
    configure_runtime_environment()
    from proximic_ring.ui.controller import AppController

    app = QApplication(["ProxiMic 输入法体验"])
    app.setQuitOnLastWindowClosed(False)
    host = AppController(inline_input_enabled=True)
    host._desktop_output = True
    inline = host.inlineInput
    partials = (["把", "把三点", "把三点改成", "把三点改成四点。"] if args.edit else
                ["今天", "今天下雨", "今天下午", "今天下午三点", "今天下午三点开会，",
                 "今天下午三点开会，讨论", "今天下午三点开会，讨论下一阶段的安排。"])
    simulation = {"index": 0, "final": False, "started": False, "closed": False, "exiting": False}
    log = {"simulation": "ASR only; real IME", "events": [], "started_at": time.time()}
    report_path = PROJECT / ".build/input-method/preview.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    stream = QTimer()
    stream.setInterval(700)
    preparation = QTimer()
    preparation.setInterval(250)
    watchdog = PreviewWaitState(time.monotonic(), args.delay)
    progress = {"stage": "startup", "detail": "读取主机初始状态", "last_print": 0.0}

    def save() -> None:
        report_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    def record() -> None:
        view = inline._view
        event = {"phase": view.get("phase"), "error": inline.error,
                 "characters": len(view.get("raw", "")), "ready": bool(view.get("ready")),
                 "connected": bool(inline._epoch), "connection_status": inline.connectionStatus,
                 "application": view.get("application", ""),
                 "capabilities": view.get("capabilities", {}),
                 "progress_stage": progress["stage"], "progress_detail": progress["detail"]}
        if not log["events"] or log["events"][-1] != event:
            log["events"].append(event)
            save()
            print(json.dumps(event, ensure_ascii=False), flush=True)
        if view.get("phase") in {"error", "interrupted", "undone"}:
            stream.stop()

    inline.changed.connect(record)

    def finish() -> None:
        if simulation["final"] or simulation["closed"] or simulation["exiting"] or not simulation["started"]:
            return
        if inline._view.get("phase") not in {"listening", "finishing"}:
            return
        simulation["final"] = True
        stream.stop()
        host._apply_runtime_update(partials[-1], True, "", 1)

    def partial() -> None:
        if inline._view.get("phase") not in {"listening", "finishing"}:
            return
        index = simulation["index"]
        if index == len(partials):
            finish()
        else:
            host._apply_runtime_update(partials[index], False, "", 1)
            simulation["index"] += 1

    stream.timeout.connect(partial)
    inline.audioEndRequested.connect(lambda: QTimer.singleShot(80, finish))
    inline.interrupted.connect(stream.stop)

    def stop(reason: str, *, failed: bool) -> None:
        if simulation["exiting"]:
            return
        simulation["exiting"] = True
        preparation.stop()
        stream.stop()
        log["result"] = {"status": "failed" if failed else "finished", "reason": reason}
        save()
        print(reason, flush=True)
        app.exit(1 if failed else 0)

    def poll() -> None:
        if simulation["exiting"]:
            return
        now = time.monotonic()
        view = inline._view
        action, stage, detail = watchdog.evaluate(
            now, connected=bool(inline._epoch), ready=bool(view.get("ready")),
            application=str(view.get("application", "")), phase=str(view.get("phase", "")),
            connection_status=inline.connectionStatus,
            transport_error=str(inline._transport_error), error=inline.error,
        )
        if stage != progress["stage"] or detail != progress["detail"]:
            progress.update(stage=stage, detail=detail, last_print=now)
            print("[体验状态] " + detail, flush=True)
            record()
        elif action == "wait" and now - progress["last_print"] >= 5:
            progress["last_print"] = now
            print("[仍在等待] " + detail, flush=True)
        if action == "fail":
            stop(detail, failed=True)
        elif action == "begin":
            simulation["started"] = True
            watchdog.mark_begin(now)
            host._apply_runtime_status("[ASR] START input_method_preview")
        elif action == "confirm":
            if not simulation["final"] and view.get("phase") in {"listening", "finishing"}:
                stream.start()
        elif action == "stopped":
            stop(detail, failed=False)

    preparation.timeout.connect(poll)
    print("请先退出正在运行的主程序，避免同一输入法连接两个 ASR 主机。", flush=True)
    print(f"准备时间 {max(1, args.delay)} 秒；手动选择“ProxiMic 语音”，切到 Codex 普通消息框或微信文件传输助手输入框。", flush=True)
    print("下划线、操作条和文字读写均来自真实输入法组件；仅 ASR 由脚本模拟。", flush=True)
    print("有下划线时可转编辑；F8 / 已配置转换快捷键确认本句为指令，Esc 可取消。定稿后只可撤销。", flush=True)
    print("脚本不会选择输入法、模拟按键、按回车或点发送。", flush=True)
    if args.edit:
        print("请预先写入“明天三点开会”，将光标放在末尾，再观察“把三点改成四点。”；主动转换才调用模型。", flush=True)
    print(f"仅记录状态、字符数和能力：{report_path}", flush=True)
    print("[初始连接状态] " + inline.connectionStatus, flush=True)
    if inline.error:
        print("[初始错误] " + inline.error, flush=True)
    record()
    preparation.start()
    QTimer.singleShot(0, poll)
    QTimer.singleShot((max(1, args.delay) + 150) * 1000,
                      lambda: stop("体验时间结束；已停止模拟语音，已经定稿的文字保留在目标输入框中。", failed=False))
    try:
        return app.exec()
    finally:
        simulation["closed"] = True
        preparation.stop()
        stream.stop()
        inline.close()
        host._text_processing_worker.close(wait=False)
        host._close_voice_history()


if __name__ == "__main__":
    raise SystemExit(main())
