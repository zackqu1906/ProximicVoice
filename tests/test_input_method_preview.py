"""Preview diagnostics are tested without selecting an input source or opening apps."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "input_method_preview", Path(__file__).resolve().parents[1] / "scripts/input-method-preview.py"
)
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


def evaluate(waiter, now, **overrides):
    values = dict(connected=False, ready=False, application="", phase="idle",
                  connection_status="语音输入法未连接", transport_error="", error="")
    values.update(overrides)
    return waiter.evaluate(now, **values)


def test_existing_host_lock_error_fails_before_preparation_delay():
    waiter = preview.PreviewWaitState(100, 10)
    action, stage, detail = evaluate(waiter, 100, transport_error="[Errno 35] Resource temporarily unavailable")
    assert (action, stage) == ("fail", "bridge_error")
    assert "Errno 35" in detail
    assert "主程序或其他体验脚本" in detail
    assert "尚未开始输入" in detail


@pytest.mark.parametrize("values, stage, reason", [
    ({}, "waiting_connection", "尚未收到输入法组件连接"),
    ({"connected": True}, "waiting_client", "没有就绪的输入会话"),
    ({"connected": True, "ready": True, "application": "com.apple.TextEdit"}, "wrong_application", "com.apple.TextEdit"),
])
def test_waiting_reason_and_timeout_stay_specific(values, stage, reason):
    waiter = preview.PreviewWaitState(100, 10)
    action, actual, detail = evaluate(waiter, 120, **values)
    assert (action, actual) == ("wait", stage)
    assert reason in detail
    action, actual, detail = evaluate(waiter, 155, **values)
    assert (action, actual) == ("fail", stage + "_timeout")
    assert reason in detail
    assert "没有发送任何文本" in detail


@pytest.mark.parametrize("application", sorted(preview.TARGET_APPLICATIONS))
def test_only_authorized_targets_begin_after_delay(application):
    waiter = preview.PreviewWaitState(100, 10)
    target = {"connected": True, "ready": True, "application": application}
    assert evaluate(waiter, 109, **target)[:2] == ("wait", "preparing")
    assert evaluate(waiter, 110, **target)[:2] == ("begin", "ready")


def test_begin_requires_confirmation_and_times_out_before_streaming():
    waiter = preview.PreviewWaitState(100, 10)
    waiter.mark_begin(110)
    assert evaluate(waiter, 114, connected=True, ready=True, phase="starting")[:2] == ("wait", "waiting_begin")
    action, stage, detail = evaluate(waiter, 115, connected=True, ready=True, phase="starting")
    assert (action, stage) == ("fail", "begin_timeout")
    assert "未收到 listening／finishing 确认" in detail
    assert "尚未开始逐段模拟输入" in detail


def test_confirmation_occurs_once_and_is_not_subject_to_ready_timeout():
    waiter = preview.PreviewWaitState(100, 10)
    waiter.mark_begin(110)
    assert evaluate(waiter, 111, connected=True, ready=True, phase="listening")[:2] == ("confirm", "active")
    assert evaluate(waiter, 200, connected=True, ready=True, phase="editing")[:2] == ("active", "active")


def test_disconnect_and_component_error_stop_after_begin():
    waiter = preview.PreviewWaitState(100, 10)
    waiter.mark_begin(110)
    assert evaluate(waiter, 111)[:2] == ("fail", "connection_lost")
    action, stage, detail = evaluate(waiter, 111, connected=True, phase="error", error="客户端拒绝组合")
    assert (action, stage, detail) == ("fail", "begin_error", "客户端拒绝组合")


def test_user_cancel_ends_simulation_without_restarting_or_error():
    waiter = preview.PreviewWaitState(100, 10)
    waiter.mark_begin(110)
    evaluate(waiter, 111, connected=True, ready=True, phase="listening")
    assert evaluate(waiter, 112, connected=True, ready=True, phase="undone")[:2] == ("stopped", "undone")


@pytest.fixture
def run_preview(tmp_path, monkeypatch):
    """Run the actual preview main with real Qt timers and an in-memory host."""
    from types import SimpleNamespace
    from PySide6 import QtCore, QtWidgets
    import proximic_ring.runtime_paths as runtime_paths
    import proximic_ring.ui.controller as controller_module

    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication(["preview-timer-test"])
    monkeypatch.setattr(preview, "PROJECT", tmp_path)
    monkeypatch.setattr(runtime_paths, "configure_runtime_environment", lambda: None)
    monkeypatch.setattr(preview.sys, "platform", "darwin")

    class ApplicationFacade:
        def setQuitOnLastWindowClosed(self, _value):
            pass

        def exec(self):
            return app.exec()

        def exit(self, code=0):
            app.exit(code)

        def quit(self):
            app.quit()

    monkeypatch.setattr(QtWidgets, "QApplication", lambda _args: ApplicationFacade())
    instances = []
    closes = []

    class FakeInline(QtCore.QObject):
        changed = QtCore.Signal()
        audioEndRequested = QtCore.Signal()
        interrupted = QtCore.Signal()

        def __init__(self, error):
            super().__init__()
            self._view = {"phase": "idle", "ready": False}
            self._epoch = ""
            self._transport_error = error
            self.error = error
            self.connectionStatus = "输入法连接失败：" + error if error else "语音输入法未连接"

        def activate(self):
            self._epoch = "fake-epoch"
            self.connectionStatus = "输入法已连接 · 已就绪"
            self._view.update(ready=True, application="com.openai.codex")
            self.changed.emit()

        def close(self):
            closes.append("inline")

    def run(*, transport_error="", activate=False, confirm=False, shorten_wait=False, shorten_begin=False):
        class FakeHost:
            def __init__(self, **_kwargs):
                self.inlineInput = FakeInline(transport_error)
                self._text_processing_worker = SimpleNamespace(close=lambda **_kwargs: closes.append("worker"))
                self.starts = []
                self.updates = []
                instances.append(self)
                if activate:
                    QtCore.QTimer.singleShot(10, self.inlineInput.activate)

            def _apply_runtime_status(self, message):
                self.starts.append(message)
                self.inlineInput._view["phase"] = "starting"
                self.inlineInput.changed.emit()
                if confirm:
                    def acknowledge():
                        self.inlineInput._view["phase"] = "listening"
                        self.inlineInput.changed.emit()
                    QtCore.QTimer.singleShot(30, acknowledge)

            def _apply_runtime_update(self, text, final, error, session):
                self.updates.append((text, final, error, session))
                # End after proving the real repeating ASR timer fired. No UI is touched.
                QtCore.QTimer.singleShot(0, app.quit)

            def _close_voice_history(self):
                closes.append("history")

        monkeypatch.setattr(controller_module, "AppController", FakeHost)
        monkeypatch.setattr(preview.sys, "argv", ["input-method-preview.py", "--delay", "1"])
        if shorten_wait or shorten_begin:
            original = preview.PreviewWaitState
            class ShortWaitState(original):
                def __init__(self, now, delay):
                    super().__init__(now, delay)
                    if shorten_wait:
                        self.ready_deadline = now + .1
                    if shorten_begin:
                        self.begin_timeout = .1
            monkeypatch.setattr(preview, "PreviewWaitState", ShortWaitState)
        guard = QtCore.QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(lambda: app.exit(99))
        guard.start(4000)
        try:
            result = preview.main()
        finally:
            guard.stop()
        assert result != 99, "Qt timer callbacks were not connected or did not finish"
        return result, instances[-1], list(closes), tmp_path / ".build/input-method/preview.json"

    return run


def test_actual_qt_timer_starts_exactly_one_transaction_after_ready_confirmation(run_preview, capsys):
    import json
    result, host, closes, report = run_preview(activate=True, confirm=True)
    assert result == 0
    assert host.starts == ["[ASR] START input_method_preview"]
    assert len(host.updates) == 1
    assert host.updates[0] == ("今天", False, "", 1)
    assert closes == ["inline", "worker", "history"]
    output = capsys.readouterr().out
    assert "[初始连接状态] 语音输入法未连接" in output
    assert "已收到真实输入法事务确认" in output
    events = json.loads(report.read_text())["events"]
    assert events[0]["connected"] is False
    assert any(event["phase"] == "listening" for event in events)


def test_actual_qt_loop_reports_startup_lock_error_and_cleans_up(run_preview, capsys):
    import json
    result, host, closes, report = run_preview(transport_error="[Errno 35] Resource temporarily unavailable")
    assert result == 1
    assert not host.starts and not host.updates
    assert closes == ["inline", "worker", "history"]
    output = capsys.readouterr().out
    assert "[初始错误] [Errno 35]" in output
    assert "主机启动失败" in output
    assert json.loads(report.read_text())["result"]["status"] == "failed"


def test_actual_qt_repeating_timer_reports_connection_timeout_without_writing(run_preview, capsys):
    import json
    result, host, closes, report = run_preview(shorten_wait=True)
    assert result == 1
    assert not host.starts and not host.updates
    assert closes == ["inline", "worker", "history"]
    assert "等待超时：主机通信已启动，但尚未收到输入法组件连接" in capsys.readouterr().out
    assert json.loads(report.read_text())["events"][-1]["progress_stage"] == "waiting_connection_timeout"


def test_actual_qt_timer_does_not_stream_without_begin_acknowledgement(run_preview, capsys):
    result, host, closes, _ = run_preview(activate=True, shorten_begin=True)
    assert result == 1
    assert len(host.starts) == 1
    assert not host.updates
    assert closes == ["inline", "worker", "history"]
    assert "未收到 listening／finishing 确认" in capsys.readouterr().out
