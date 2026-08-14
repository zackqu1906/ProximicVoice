from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from proximic_ring.dataset import (
    LABEL_TO_TARGET,
    METADATA_FIELDS,
    ProximityFeatureDataset,
    append_metadata_row,
    load_metadata,
    make_split,
    make_window_specs,
    segment_records,
    write_pcm16_wav,
)
from proximic_ring.train import calibrate_threshold


def _add_clip(root: Path, *, label: str, idx: int, speaker: str = "p01") -> None:
    target = LABEL_TO_TARGET[label]
    t = np.arange(24_000, dtype=np.float32) / 16_000.0
    amp = 0.03 if label == "near" else (0.01 if label == "far" else 0.02)
    audio = (amp * np.sin(2 * np.pi * (180 + 10 * idx) * t)).astype(np.float32)
    rel = Path("raw") / label / f"{label}_{idx:03d}.wav"
    write_pcm16_wav(root / rel, audio)
    append_metadata_row(
        root / "metadata.csv",
        {
            "path": rel.as_posix(),
            "target": target,
            "class_name": label,
            "polarity": "positive" if label == "near" else "negative",
            "distance_cm": 2 if label == "near" else (50 if label == "far" else 0),
            "speaker_id": speaker,
            "speech_style": "normal",
            "angle_deg": 0,
            "session_id": "s1",
            "sample_index": idx,
            "duration_s": 1.5,
            "sample_rate": 16000,
            "channels": 1,
            "encoding": "PCM16LE",
            "timestamp_utc": "2026-08-10T00:00:00Z",
            "peak": amp,
            "rms": amp / np.sqrt(2),
            "phrase": "",
            "notes": "",
        },
    )


def test_label_mapping_matches_legacy_score_contract():
    assert LABEL_TO_TARGET == {"near": 0, "far": 1, "artifact": 1}


def test_dataset_feature_shape_and_target(tmp_path: Path):
    _add_clip(tmp_path, label="near", idx=1)
    _add_clip(tmp_path, label="far", idx=2)
    records = load_metadata(tmp_path)
    ds = ProximityFeatureDataset(records, [0, 1], cache_features=True)
    x0, y0 = ds[0]
    x1, y1 = ds[1]
    assert tuple(x0.shape) == (20, 201)
    assert tuple(x1.shape) == (20, 201)
    assert {int(y0), int(y1)} == {0, 1}


def test_file_split_is_stratified_by_collection_class(tmp_path: Path):
    for i in range(10):
        _add_clip(tmp_path, label="near", idx=i)
        _add_clip(tmp_path, label="far", idx=100 + i)
        _add_clip(tmp_path, label="artifact", idx=200 + i)
    records = load_metadata(tmp_path)
    split, mode = make_split(records, split_by="file", seed=1)
    assert mode == "file"
    for indices in split.values():
        classes = {records[i].class_name for i in indices}
        assert classes == {"near", "far", "artifact"}
        targets = {records[i].target for i in indices}
        assert targets == {0, 1}


def test_threshold_calibration_uses_near_as_score_positive():
    targets = np.array([0, 0, 0, 1, 1, 1])
    scores = np.array([4.0, 2.5, 1.5, -1.0, -2.0, -3.0])
    threshold, metrics = calibrate_threshold(targets, scores)
    assert -1.0 < threshold < 1.5
    assert metrics.balanced_accuracy == 1.0
    assert metrics.recall_near == 1.0


def test_long_take_creates_multiple_windows_without_cross_split_leakage(tmp_path: Path):
    from proximic_ring.dataset import WindowedProximityFeatureDataset, make_window_specs

    # Build 3 long takes per class so file split can create train/val/test takes.
    for i in range(3):
        for label in ("near", "far"):
            target = LABEL_TO_TARGET[label]
            t = np.arange(8 * 16_000, dtype=np.float32) / 16_000.0
            amp = 0.03 if label == "near" else (0.01 if label == "far" else 0.02)
            audio = (amp * np.sin(2 * np.pi * (180 + i * 20) * t)).astype(np.float32)
            rel = Path("raw") / label / f"long_{label}_{i}.wav"
            write_pcm16_wav(tmp_path / rel, audio)
            append_metadata_row(
                tmp_path / "metadata.csv",
                {
                    "path": rel.as_posix(),
                    "target": target,
                    "class_name": label,
                    "polarity": "positive" if label == "near" else "negative",
                    "distance_cm": 2 if label == "near" else (50 if label == "far" else 0),
                    "speaker_id": "p01",
                    "speech_style": "normal",
                    "angle_deg": 0,
                    "session_id": f"s_{label}_{i}",
                    "sample_index": i,
                    "duration_s": 8.0,
                    "sample_rate": 16000,
                    "channels": 1,
                    "encoding": "PCM16LE",
                    "timestamp_utc": "2026-08-10T00:00:00Z",
                    "peak": amp,
                    "rms": amp / np.sqrt(2),
                    "phrase": "",
                    "notes": "",
                },
            )

    records = load_metadata(tmp_path)
    split, _ = make_split(records, split_by="file", val_fraction=0.2, test_fraction=0.2, seed=2)
    owners: dict[int, str] = {}
    for split_name, indices in split.items():
        specs = make_window_specs(records, indices, hop_s=0.5, edge_margin_s=0.5)
        for spec in specs:
            assert spec.record_idx not in owners or owners[spec.record_idx] == split_name
            owners[spec.record_idx] = split_name

    # An 8 s take with 0.5 s margins and a 0.5 s hop gives 13 one-second windows.
    first_specs = make_window_specs(records, [0], hop_s=0.5, edge_margin_s=0.5)
    assert len(first_specs) == 13

    ds = WindowedProximityFeatureDataset(
        records,
        [0],
        hop_s=0.5,
        edge_margin_s=0.5,
        jitter_s=0.15,
        seed=3,
    )
    assert len(ds) == 13
    x, y = ds[0]
    assert tuple(x.shape) == (20, 201)
    assert int(y) in {0, 1}


def test_artifact_maps_to_binary_non_target(tmp_path: Path):
    _add_clip(tmp_path, label="artifact", idx=1)
    records = load_metadata(tmp_path)
    assert records[0].class_name == "artifact"
    assert records[0].target == 1


def test_noise_files_are_split_disjointly(tmp_path: Path):
    from proximic_ring.dataset import split_auxiliary_files

    paths = [tmp_path / f"noise_{i}.wav" for i in range(9)]
    split = split_auxiliary_files(paths, val_fraction=0.2, test_fraction=0.2, seed=7)
    train = set(split["train"])
    val = set(split["val"])
    test = set(split["test"])
    assert train and val and test
    assert train.isdisjoint(val)
    assert train.isdisjoint(test)
    assert val.isdisjoint(test)
    assert train | val | test == set(paths)


def test_segment_split_cuts_long_wavs_into_8s_pseudotakes_before_windowing(tmp_path: Path):
    # One long recording per collection class is enough here because each 24-s
    # WAV becomes three independent 8-s split units.
    duration_s = 24
    n = duration_s * 16_000
    t = np.arange(n, dtype=np.float32) / 16_000.0
    for class_offset, label in enumerate(("near", "far", "artifact")):
        target = LABEL_TO_TARGET[label]
        amp = 0.03 if label == "near" else (0.01 if label == "far" else 0.02)
        audio = (amp * np.sin(2 * np.pi * (180 + class_offset * 50) * t)).astype(np.float32)
        rel = Path("raw") / label / f"long_{label}.wav"
        write_pcm16_wav(tmp_path / rel, audio)
        append_metadata_row(
            tmp_path / "metadata.csv",
            {
                "path": rel.as_posix(),
                "target": target,
                "class_name": label,
                "polarity": "positive" if label == "near" else "negative",
                "distance_cm": 2 if label == "near" else (50 if label == "far" else 0),
                "speaker_id": "p01",
                "speech_style": "normal",
                "angle_deg": 0,
                "session_id": f"long_{label}",
                "sample_index": class_offset,
                "duration_s": duration_s,
                "sample_rate": 16000,
                "channels": 1,
                "encoding": "PCM16LE",
                "timestamp_utc": "2026-08-12T00:00:00Z",
                "peak": amp,
                "rms": amp / np.sqrt(2),
                "phrase": "",
                "notes": "",
            },
        )

    source_records = load_metadata(tmp_path)
    assert len(source_records) == 3

    records = segment_records(source_records, segment_duration_s=8.0)
    assert len(records) == 9
    assert sorted({r.segment_index for r in records}) == [0, 1, 2]
    assert all(
        r.segment_end_sample - r.segment_start_sample == 8 * 16_000
        for r in records
    )

    split, mode = make_split(
        records,
        split_by="segment",
        val_fraction=0.2,
        test_fraction=0.2,
        seed=7,
    )
    assert mode == "segment"
    assert {name: len(indices) for name, indices in split.items()} == {
        "train": 3,
        "val": 3,
        "test": 3,
    }
    for indices in split.values():
        assert {records[i].class_name for i in indices} == {"near", "far", "artifact"}

    # With exactly three segments per class, each original long WAV contributes
    # one 8-s pseudo-take to each split. This is the intended segment mode.
    path_to_splits: dict[Path, set[str]] = {}
    for split_name, indices in split.items():
        for i in indices:
            path_to_splits.setdefault(records[i].path, set()).add(split_name)
    assert all(names == {"train", "val", "test"} for names in path_to_splits.values())

    # 1-s windows are generated only inside each 8-s pseudo-take. With 0.5-s
    # margins and 0.5-s hop, each pseudo-take yields 13 windows.
    for indices in split.values():
        specs = make_window_specs(records, indices, hop_s=0.5, edge_margin_s=0.5)
        assert len(specs) == 3 * 13
        for spec in specs:
            record = records[spec.record_idx]
            assert spec.start_sample >= record.segment_start_sample + int(0.5 * 16_000)
            assert spec.start_sample + 16_000 <= record.segment_end_sample - int(0.5 * 16_000)


def test_segment_records_drops_short_tail_from_long_recording(tmp_path: Path):
    duration_s = 20
    n = duration_s * 16_000
    audio = np.zeros(n, dtype=np.float32)
    rel = Path("raw") / "near" / "twenty_seconds.wav"
    write_pcm16_wav(tmp_path / rel, audio)
    append_metadata_row(
        tmp_path / "metadata.csv",
        {
            "path": rel.as_posix(),
            "target": 0,
            "class_name": "near",
            "polarity": "positive",
            "distance_cm": 2,
            "speaker_id": "p01",
            "speech_style": "normal",
            "angle_deg": 0,
            "session_id": "s20",
            "sample_index": 0,
            "duration_s": duration_s,
            "sample_rate": 16000,
            "channels": 1,
            "encoding": "PCM16LE",
            "timestamp_utc": "2026-08-12T00:00:00Z",
            "peak": 0,
            "rms": 0,
            "phrase": "",
            "notes": "",
        },
    )
    records = segment_records(load_metadata(tmp_path), segment_duration_s=8.0)
    assert len(records) == 2
    assert [(r.segment_start_sample, r.segment_end_sample) for r in records] == [
        (0, 8 * 16_000),
        (8 * 16_000, 16 * 16_000),
    ]
