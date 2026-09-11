"""Headless interactions; actual alignment worker, no BLE/microphone access."""
import os
import time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from ring_whisper_lab.demo import make_demo
from ring_whisper_lab.storage import read_json, write_json, write_wav
from ring_whisper_lab.ui import MainWindow


@pytest.fixture
def windows():
    app = QApplication.instance() or QApplication([])
    opened = []

    def create(root):
        window = MainWindow(root)
        opened.append(window)
        return window

    yield app, create
    for window in opened:
        window.close()
    app.processEvents()


def wait_for(app, predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert predicate()


def remove_alignment(take):
    record = read_json(take / "record.json")
    record.pop("alignment")
    write_json(take / "record.json", record)


def test_device_choices_and_aligned_playback_by_default(tmp_path, windows):
    app, create = windows
    take = make_demo(tmp_path)
    window = create(tmp_path)
    window._event("devices", [
        {"id": "a", "name": "RingoA", "rssi": -40},
        {"id": "b", "name": "RingoB", "rssi": -43},
        {"id": "c", "name": "Headphones", "rssi": -30},
    ])
    assert window.device_select["input"].count() == 4
    assert not window.connect_btn.isEnabled()
    window.device_select["input"].setCurrentIndex(1)
    window.device_select["reference"].setCurrentIndex(1)
    assert not window.connect_btn.isEnabled()
    window.device_select["reference"].setCurrentIndex(2)
    assert window.connect_btn.isEnabled()
    assert window.current == take
    assert window._play_path().parent.name.startswith("aligned_")
    window.track.setCurrentIndex(2)
    assert window._play_path().name == "stereo.wav"
    assert window._play_path().is_file()
    assert len(window.waveform.tracks) == 2
    assert not hasattr(window, "review_btn")
    assert not hasattr(window, "align_btn")
    app.processEvents()


def test_saved_take_auto_aligns_without_review_and_stop_race(tmp_path, windows):
    app, create = windows
    window = create(tmp_path)
    take = make_demo(tmp_path)
    remove_alignment(take)
    original = (take / "raw/input.wav").read_bytes()
    window.connected = True
    window.recording = True
    window.busy = True
    window._event("saved", str(take))
    assert not window.recording
    assert len(window.alignment_jobs) == 1
    window._event("done", ("stop", None))
    assert not window.busy
    assert len(window.alignment_jobs) == 1  # stop must not clear the CPU job
    assert window.record_btn.isEnabled()  # can record next take while aligning
    wait_for(app, lambda: not window.alignment_jobs)
    assert window._play_path().parent.name.startswith("aligned_")
    assert window._play_path().is_file()
    assert window.play_btn.isEnabled()
    assert read_json(take / "record.json")["review"]["decision"] == "pending"
    assert (take / "raw/input.wav").read_bytes() == original


def test_existing_unaligned_take_is_processed_on_startup(tmp_path, windows):
    app, create = windows
    take = make_demo(tmp_path)
    remove_alignment(take)
    window = create(tmp_path)
    wait_for(app, lambda: window._play_path().parent.name.startswith("aligned_")
             and not window.alignment_jobs)
    assert window._play_path().is_file()


def test_short_take_remains_listenable_with_clear_raw_fallback(tmp_path, windows):
    app, create = windows
    take = make_demo(tmp_path)
    remove_alignment(take)
    for role in ("input", "reference"):
        write_wav(take / "raw" / f"{role}.wav", np.zeros(1600))
    window = create(tmp_path)
    wait_for(app, lambda: take in window.alignment_failures)
    assert not window.alignment_jobs
    assert window._play_path().parent.name == "raw"
    assert window.play_btn.isEnabled()
    assert "无法自动对齐" in window.detail.text()
    assert not window.track.model().item(2).isEnabled()
    window._queue_pending()
    assert not window.alignment_jobs  # no infinite retry loop


def test_recording_waits_for_both_bookend_playbacks(monkeypatch, tmp_path, windows):
    app, create = windows
    take = make_demo(tmp_path)
    window = create(tmp_path)
    played, stopped, metadata_seen = [], [], []
    monkeypatch.setattr(window.marker_player, "play", lambda root, kind: played.append(kind))

    async def start(root, metadata, encoding):
        metadata_seen.append(metadata)
        window.bridge.event.emit("recording", str(take))

    async def stop(reason="manual"):
        stopped.append(reason)
        window.bridge.event.emit("saved", str(take))

    monkeypatch.setattr(window.worker.service, "start", start)
    monkeypatch.setattr(window.worker.service, "stop", stop)
    window.connected = True
    window.start_capture()
    wait_for(app, lambda: played == ["start"])
    assert window.capture_phase == "start_marker"
    assert not window.record_btn.isEnabled()
    assert metadata_seen[0]["sync_protocol"] == "chirp_bookends_v1"
    window._marker_finished("start")
    assert window.capture_phase == "speaking"
    assert window.record_btn.isEnabled()
    window.toggle_capture()
    assert played == ["start", "end"]
    assert not stopped  # end tone must be captured before MIC OFF
    assert not window.record_btn.isEnabled()
    window._marker_finished("end")
    wait_for(app, lambda: not window.busy and not window.recording)
    assert stopped == ["manual"]
    assert window.capture_phase == "idle"


def test_marker_playback_failure_saves_interrupted(monkeypatch, tmp_path, windows):
    app, create = windows
    window = create(tmp_path)
    stopped = []

    async def stop(reason):
        stopped.append(reason)

    monkeypatch.setattr(window.worker.service, "stop", stop)
    window.recording = True
    window.capture_phase = "start_marker"
    window._marker_failed("speaker unavailable")
    wait_for(app, lambda: bool(stopped) and not window.busy)
    assert stopped == ["sync_playback_failed"]
    window.recording = False
    window.capture_phase = "idle"


def test_close_during_speech_waits_for_end_marker(monkeypatch, tmp_path, windows):
    app, create = windows
    take = make_demo(tmp_path)
    window = create(tmp_path)
    played, stopped = [], []
    monkeypatch.setattr(window.marker_player, "play", lambda root, kind: played.append(kind))

    async def stop(reason="manual"):
        stopped.append(reason)
        window.bridge.event.emit("saved", str(take))

    monkeypatch.setattr(window.worker.service, "stop", stop)
    window.recording = True
    window.capture_phase = "speaking"
    window.close()
    assert played == ["end"]
    assert not stopped
    assert not window.closed
    window._marker_finished("end")
    wait_for(app, lambda: window.closed)
    assert stopped == ["manual"]


def test_interrupted_capture_ignores_late_marker_completion(tmp_path, windows):
    app, create = windows
    take = make_demo(tmp_path)
    window = create(tmp_path)
    window.recording = True
    window.capture_phase = "start_marker"
    window._event("saved", str(take))
    window._marker_finished("start")
    assert window.capture_phase == "idle"
    assert not window.recording
