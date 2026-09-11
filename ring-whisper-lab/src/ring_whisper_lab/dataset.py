"""Portable paired WAV manifest export; model-independent and no torch import."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from .storage import SAMPLE_RATE, list_takes, read_json, utc_now, write_json


def export_dataset(root: Path, *, segment_s: float = 4.0) -> Path:
    if not .5 <= segment_s <= 60:
        raise ValueError("segment_s must be between 0.5 and 60")
    output = root / "exports" / (utc_now().replace(":", "-") + "_" + uuid.uuid4().hex[:6])
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    skipped = []
    group_splits = {}
    for take in list_takes(root):
        record = read_json(take / "record.json")
        alignment = record.get("alignment", {})
        review = record.get("review", {})
        metadata = record.get("metadata", {})
        if (record.get("demo") or record.get("status") != "captured"
                or metadata.get("configuration") != "bare_protected"
                or metadata.get("speech_style") not in ("whisper", "soft_voiced", "normal")
                or review.get("decision") != "accepted" or not alignment.get("automatic_quality_pass")):
            skipped.append({"take_id": record["take_id"], "reason": "demo / calibration / unreviewed / rejected / failed QA / nonstandard roles"})
            continue
        speaker = record["metadata"]["speaker_id"]
        # Keep ALL sessions for a speaker together to avoid overlapping speaker leakage.
        bucket = int(hashlib.sha256(speaker.encode()).hexdigest()[:8],16) % 100
        split = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
        group_splits[speaker] = split
        folder = take / alignment["revision"]
        for name in ("input.wav", "reference.wav"):
            if not (folder/name).is_file():
                raise FileNotFoundError(folder/name)
        n = alignment["sample_count"]
        start = max(0,int(float(review.get("usable_start_s",0))*SAMPLE_RATE))
        end_value = review.get("usable_end_s")
        end = min(n,int(float(end_value)*SAMPLE_RATE)) if end_value is not None else n
        if end <= start:
            raise ValueError(f"Invalid usable interval: {take.name}")
        step = int(segment_s*SAMPLE_RATE)
        for offset in range(start,end,step):
            length = min(step,end-offset)
            if length < SAMPLE_RATE*.5:
                continue
            rows.append({"schema_version":1,"take_id":take.name,"split":split,
                         "speaker_id":speaker,"session_group":record["metadata"]["session_group"],
                         "input_path":str((folder/"input.wav").relative_to(root)),
                         "reference_path":str((folder/"reference.wav").relative_to(root)),
                         "start_sample":offset,"num_samples":length,"sample_rate":SAMPLE_RATE,
                         "take_transcript":review.get("transcript", ""),
                         "transcript_scope":"entire_take_not_segment",
                         "reference_type":"protected_ring_approximate_reference",
                         "alignment_revision":alignment["revision"]})
    for split in ("train","validation","test"):
        with (output/f"{split}.jsonl").open("w",encoding="utf-8") as f:
            for row in rows:
                if row["split"] == split:
                    f.write(json.dumps(row,ensure_ascii=False)+"\n")
    write_json(output/"export.json", {"schema_version":1,"data_root":"../..","segments":len(rows),
                                     "speaker_splits":group_splits,"skipped":skipped,
                                     "warning":"Small speaker counts may leave validation/test empty; gather held-out speakers before model selection."})
    return output


class PairedAudioDataset:
    """A NumPy dataset; future PyTorch training can wrap it or use DataLoader."""
    def __init__(self, manifest: Path, data_root: Path):
        self.root = data_root
        self.rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        import wave
        import numpy as np

        row = self.rows[index]
        def load(key):
            path = (self.root/row[key]).resolve()
            if not path.is_relative_to(self.root.resolve()):
                raise ValueError("Manifest path escapes dataset root")
            with wave.open(str(path),"rb") as f:
                if (f.getframerate(),f.getnchannels(),f.getsampwidth()) != (SAMPLE_RATE,1,2):
                    raise ValueError("Invalid training WAV format")
                f.setpos(row["start_sample"])
                x = np.frombuffer(f.readframes(row["num_samples"]),"<i2").astype(np.float32)/32768
                if len(x) != row["num_samples"]:
                    raise ValueError("Training segment extends beyond WAV")
                return x
        return {"input":load("input_path"),"reference":load("reference_path"),"metadata":row}
