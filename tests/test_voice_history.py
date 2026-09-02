from __future__ import annotations

import json
from types import SimpleNamespace
import threading
import wave

import numpy as np

from proximic_ring.voice_history import VoiceHistoryStore


def _final(session_id: int, text: str):
    return SimpleNamespace(
        session_id=session_id,
        is_final=True,
        text=text,
        backend="streaming_sensevoice",
        model="iic/SenseVoiceSmall",
        error=None,
        latency_s=0.25,
        audio_duration_s=0.1,
    )


def test_voice_history_pairs_both_callback_orders_and_reloads(tmp_path):
    saved = []
    ready = threading.Event()

    def on_saved(entry):
        saved.append(entry)
        if len(saved) == 2:
            ready.set()

    root = tmp_path / "voice_history"
    store = VoiceHistoryStore(root, on_saved=on_saved)
    audio = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)

    store.record_audio(1, audio)
    store.record_final(_final(1, "第一句话"))
    store.record_final(_final(2, ""))
    store.record_audio(2, np.zeros(800, dtype=np.float32))

    assert ready.wait(3.0)
    store.close(wait=True)
    assert {entry["text"] for entry in saved} == {"第一句话", "（未识别出文字）"}
    assert all(entry["audioPath"].endswith("utterance.wav") for entry in saved)

    wav_path = next(root.glob("*/utterance.wav"))
    with wave.open(str(wav_path), "rb") as recording:
        assert recording.getframerate() == 16_000
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2

    metadata_rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in root.glob("*/metadata.json")
    ]
    assert {row["text"] for row in metadata_rows} == {"第一句话", ""}

    reloaded = VoiceHistoryStore(root)
    entries = reloaded.load_entries()
    reloaded.close(wait=True)
    assert len(entries) == 2
    assert all(entry["durationSeconds"] > 0 for entry in entries)


def test_nonfinal_update_is_not_recorded(tmp_path):
    store = VoiceHistoryStore(tmp_path / "voice_history")
    update = _final(1, "partial")
    update.is_final = False
    store.record_audio(1, np.zeros(160, dtype=np.float32))
    store.record_final(update)
    store.close(wait=True)
    assert store.load_entries() == []
