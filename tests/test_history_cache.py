from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from proximic_ring.modification_dataset import ModificationDatasetCollector


def _add_record(collector, session_id, text):
    collector.record_audio(session_id, np.zeros(160, dtype=np.float32))
    collector.record_asr_update(SimpleNamespace(
        session_id=session_id, text=text, is_final=True, error=None,
        backend="test", model="test", latency_s=0.1, audio_duration_s=0.01,
    ))


def test_history_refresh_reads_only_changed_records(tmp_path, monkeypatch):
    store = ModificationDatasetCollector(tmp_path, "user_test")
    for session in range(1, 21):
        _add_record(store, session, f"语音 {session}")

    # A fresh instance performs the one startup scan.
    store = ModificationDatasetCollector(tmp_path, "user_test")
    reads = []
    read_json = store._read_json

    def tracked_read(path):
        reads.append(path)
        return read_json(path)

    monkeypatch.setattr(store, "_read_json", tracked_read)
    rows = store.load_entries()
    assert len(rows) == 20
    assert len(reads) == 20
    changed_id = rows[8]["interactionId"]

    def forbidden_scan(*_args, **_kwargs):
        raise AssertionError("a warm history refresh must not scan directories")

    monkeypatch.setattr(Path, "glob", forbidden_scan)
    reads.clear()
    assert store.load_entries(5) == rows[:5]
    assert reads == []
    # Mutating a returned row cannot corrupt the shared projection.
    store.load_entries()[0]["text"] = "caller mutation"
    assert store.load_entries()[0]["text"] == rows[0]["text"]

    store.record_application(
        interaction_id=changed_id, action="native_undo_sent",
        method="native_shortcut", mode="edit",
    )
    reads.clear()
    updated = store.load_entries()
    assert reads == [store._interaction_path(changed_id)]
    assert updated[8]["outcome"] == "native_undo_sent"
    assert updated[:8] == rows[:8]
    assert updated[9:] == rows[9:]
    reads.clear()
    assert store.load_entries() == updated
    assert reads == []


def test_history_cache_tracks_new_audio_reconnect_and_clear(tmp_path):
    store = ModificationDatasetCollector(tmp_path, "user_test")
    assert store.load_entries() == []
    first_id = store.begin_session(1)
    assert store.load_entries() == []  # No audio yet.
    _add_record(store, 1, "第一句")
    assert [row["text"] for row in store.load_entries()] == ["第一句"]
    store.reset_runtime()
    _add_record(store, 1, "重连后的第一句")
    assert store.interaction_id_for_session(1) != first_id
    rows = store.load_entries()
    assert [row["text"] for row in rows] == ["重连后的第一句", "第一句"]
    assert ModificationDatasetCollector(tmp_path, "user_test").load_entries() == rows
    store.clear()
    assert store.load_entries() == []
    _add_record(store, 1, "清空之后")
    assert [row["text"] for row in store.load_entries()] == ["清空之后"]
    assert store.load_entries(0) == []
