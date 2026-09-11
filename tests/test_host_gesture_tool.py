import asyncio
import csv
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from ring_python_sdk.core.device_info import InfoComponent
from ring_python_sdk.gestures import GesturePrediction, GestureRecognizer, GestureWorker


_SPEC = importlib.util.spec_from_file_location(
    "host_gesture_tool", Path(__file__).parents[1] / "tools/test_host_gestures.py"
)
tool = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tool
_SPEC.loader.exec_module(tool)


class TapClassifier:
    class_ids = tuple(range(7))
    window_size = 60

    def predict(self, _values):
        return GesturePrediction(5, "tap", 1.0, (0, 0, 0, 0, 0, 1, 0))


def make_recognizer(_args, recorder):
    return GestureRecognizer(classifier=TapClassifier(), on_prediction=recorder.on_prediction,
                             on_gesture=recorder.on_gesture)


def test_worker_keeps_inference_off_producer_and_exposes_overflow():
    entered = threading.Event()
    release = threading.Event()
    seen = []

    class Recognizer:
        prediction_count = 0
        reset_count = 1

        def on_sample(self, sample):
            seen.append((sample.sample_index, threading.current_thread().name))
            if sample.sample_index == 0:
                entered.set()
                assert release.wait(2)

    samples = list(tool.demo_samples())[:5]
    worker = GestureWorker(Recognizer(), queue_capacity=2)
    worker.start()
    try:
        worker.submit(samples[0])
        assert entered.wait(2)
        for sample in samples[1:]:
            worker.submit(sample)
    finally:
        release.set()
        worker.close()
    assert [i for i, _ in seen] == [0, 3, 4]
    assert all(name == "RingHostGestures" for _, name in seen)
    assert worker.snapshot()["dropped_samples"] == 2
    assert worker.snapshot()["received_samples"] == 5
    assert worker.snapshot()["processed_samples"] == 3
    worker.submit(samples[0])
    assert worker.snapshot()["received_samples"] == 5
    with pytest.raises(RuntimeError, match="single-use"):
        worker.start()


def test_worker_retains_failure_for_owner():
    class BrokenRecognizer:
        prediction_count = 0
        reset_count = 1

        def on_sample(self, _sample):
            raise ValueError("invalid IMU")

    worker = GestureWorker(BrokenRecognizer())
    worker.start()
    worker.submit(next(tool.demo_samples()))
    worker.close()
    assert isinstance(worker.error, ValueError)
    assert worker.snapshot()["error"] == "invalid IMU"


def test_records_every_prediction_and_gesture_and_lossless_replay(tmp_path):
    recorder = tool.HostRecorder(tmp_path / "capture", source="test")
    worker = GestureWorker(make_recognizer(None, recorder))
    worker.start()
    samples = list(tool.demo_samples())[:75]
    for sample in samples:
        recorder.accept_sample(sample, worker)
    worker.close()
    recorder.finish(worker=worker, metadata={}, reason="test", imu_stats={})
    summary = json.loads((recorder.output_dir / "summary.json").read_text())
    assert summary["prediction_count"] == 4
    assert summary["gesture_count"] == 1
    with (recorder.output_dir / "results.csv").open(encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
    assert [r["kind"] for r in rows] == ["prediction"] * 4 + ["gesture"]
    assert rows[-1]["device_timestamp_ms"] == "370.0"
    assert rows[-1]["name"] == "tap" and float(rows[-1]["p5"]) == 1
    assert list(tool.replay_samples(recorder.output_dir / "samples.csv")) == samples
    with pytest.raises(FileExistsError):
        tool.HostRecorder(recorder.output_dir, source="test")


@pytest.mark.parametrize("outcome", ["complete", "disconnect", "cancel", "no_imu"])
def test_live_only_starts_imu_even_when_firmware_has_no_swipe(tmp_path, monkeypatch, outcome):
    instances = []
    target_device = object()

    async def scan(_timeout):
        return [SimpleNamespace(name="Ringo2CC7", identifier="TEST-ID", device=target_device)]

    @dataclass
    class Stats:
        sample_count: int = 0

    class Session:
        def __init__(self, **kwargs):
            self.client = SimpleNamespace(is_connected=True)
            self.device_info = SimpleNamespace(
                fw_version="1.1.186", hw_rev=1,
                components=(InfoComponent(1, 1, 1, 1, 3), InfoComponent(8, 0, 0, 0, 0)),
            )
            self.target_name, self.target_address = "Test Ring", "TEST-ID"
            self.imu = SimpleNamespace(stats=Stats())
            self.disconnected = False
            instances.append(self)

        async def connect_device(self, target):
            assert target is target_device
            return True

        async def imu_on(self, **kwargs):
            assert kwargs["gyro_hz"] == kwargs["accel_hz"] == 200
            assert kwargs["lp"] is False and kwargs["encode_mode"] == 0
            if outcome != "no_imu":
                for sample in list(tool.demo_samples())[:75]:
                    kwargs["on_sample"](sample)
                    self.imu.stats.sample_count += 1
            if outcome == "disconnect":
                self.client.is_connected = False
            if outcome == "cancel":
                raise asyncio.CancelledError()

        def drain_live_logs(self):
            return []

        async def disconnect(self):
            self.disconnected = True

    monkeypatch.setattr(tool, "RingSession", Session)
    monkeypatch.setattr(tool, "scan_all_devices", scan)
    monkeypatch.setattr(tool, "create_recognizer", make_recognizer)
    args = tool.parser().parse_args(["--ring-id", "2CC7", "--duration", "0.15", "--imu-timeout", "0.03",
                                     "--output-dir", str(tmp_path / "capture")])
    # A completed short fake capture has no ongoing producer; allow its normal
    # finite duration to expire. The no-data case exercises the actual timeout.
    if outcome != "no_imu":
        args.imu_timeout = 1
    if outcome == "complete":
        assert asyncio.run(tool.run(args)) == 0
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
            asyncio.run(tool.run(args))
    assert instances[0].disconnected
    summary = json.loads((tmp_path / "capture/summary.json").read_text())
    assert summary["received_samples"] == (0 if outcome == "no_imu" else 75)
    assert summary["gesture_count"] == (0 if outcome == "no_imu" else 1)
    assert summary["worker"]["queue_depth"] == 0


@pytest.mark.parametrize("number", ["2CC7", "2cc7", " Ringo2CC7 ", "0001"])
def test_ring_number_prompt_and_argument_resolve_the_same_target(monkeypatch, number):
    monkeypatch.setattr("builtins.input", lambda _prompt: number)
    target = tool.requested_target(tool.parser().parse_args([]))
    assert target == tool.requested_target(tool.parser().parse_args(["--ring-id", number]))
    assert target.casefold() == ("ringo0001" if number == "0001" else "ringo2cc7")


@pytest.mark.parametrize("number", ["", "Ringo", "2 CC7", "../2CC7"])
def test_empty_or_invalid_number_never_defaults_to_any_ring(number):
    with pytest.raises(ValueError):
        tool.requested_target(tool.parser().parse_args(["--ring-id", number]))


@pytest.mark.parametrize("target", ["ringo2cc7", "weak-id"])
def test_exact_selection_ignores_signal_strength_and_partial_names(monkeypatch, target):
    wanted = SimpleNamespace(name="Ringo2CC7", identifier="WEAK-ID", device=object(), rssi=-90)

    async def scan(_timeout):
        return [
            SimpleNamespace(name="RingoFFFF", identifier="OTHER", rssi=-10),
            SimpleNamespace(name="Ringo2CC70", identifier="PARTIAL", rssi=-15),
            wanted,
        ]

    monkeypatch.setattr(tool, "scan_all_devices", scan)
    assert asyncio.run(tool.find_requested_device(target, 1)) is wanted
    with pytest.raises(RuntimeError, match="未找到"):
        asyncio.run(tool.find_requested_device("Ringo2CC", 1))


def test_duplicate_ring_numbers_require_an_explicit_identifier(monkeypatch):
    async def scan(_timeout):
        return [SimpleNamespace(name="Ringo2CC7", identifier=value) for value in ("FIRST", "SECOND")]

    monkeypatch.setattr(tool, "scan_all_devices", scan)
    with pytest.raises(RuntimeError, match="匹配到多台设备"):
        asyncio.run(tool.find_requested_device("Ringo2CC7", 1))
    assert asyncio.run(tool.find_requested_device("SECOND", 1)).identifier == "SECOND"


def test_missing_weights_report_failure_and_close_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "MODEL_PATH", tmp_path / "missing.pt")
    assert tool.main(["--demo", "--output-dir", str(tmp_path / "capture")]) == 1
    summary = json.loads((tmp_path / "capture/summary.json").read_text())
    assert summary["stop_reason"].startswith("error:")
    assert summary["received_samples"] == 0


@pytest.mark.parametrize("args", [["--stable-window", "0"], ["--positive-ratio", "nan"],
                                    ["--duration", "-1"], ["--torch-threads", "0"]])
def test_invalid_configuration_rejected(args):
    with pytest.raises(SystemExit) as error:
        tool.main(args)
    assert error.value.code == 2
