from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
import wave

import numpy as np

SAMPLE_RATE = 16_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(audio)
    if not np.isfinite(x).all():
        raise ValueError("Audio contains NaN or infinity")
    channels = 1 if x.ndim == 1 else x.shape[1]
    pcm = np.rint(np.clip(x, -1, 32767 / 32768) * 32768).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setparams((channels, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        stream.writeframes(pcm.tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as stream:
        if (stream.getframerate(), stream.getsampwidth(), stream.getnchannels()) != (SAMPLE_RATE, 2, 1):
            raise ValueError(f"Expected 16 kHz PCM16 mono: {path}")
        return np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2").astype(np.float32) / 32768


def audio_stats(x: np.ndarray) -> dict:
    x = np.asarray(x, dtype=np.float64)
    return {
        "duration_s": len(x) / SAMPLE_RATE,
        "sample_count": len(x),
        "rms_dbfs": float(20 * np.log10(max(float(np.sqrt(np.mean(x*x))) if x.size else 0, 1e-10))),
        "peak": float(np.max(np.abs(x))) if x.size else 0,
        "clipped_fraction": float(np.mean(np.abs(x) >= 32767/32768)) if x.size else 0,
    }


def new_take(root: Path, metadata: dict, devices: dict, *, demo: bool = False) -> Path:
    ident = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    path = root / ident
    (path / "raw").mkdir(parents=True, exist_ok=False)
    write_json(path / "record.json", {
        "schema_version": 1, "take_id": ident, "created_at": utc_now(),
        "status": "recording", "sample_rate": SAMPLE_RATE,
        "metadata": metadata, "devices": devices, "demo": demo,
        "review": {"decision": "pending", "transcript": "", "notes": ""},
        "processing": {"gain_applied_db": 0, "endpointing": False,
                       "reference_is_ground_truth": False},
    })
    return path


def list_takes(root: Path) -> list[Path]:
    return sorted((p.parent for p in root.glob("*/record.json")), reverse=True)
