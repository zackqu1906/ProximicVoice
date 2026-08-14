from __future__ import annotations

import csv
import math
import re
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import torch
from torch.utils.data import Dataset

from .features import LegacyFeatureExtractor
from .resample import LegacyDownsampler16kTo8k

# Keep this mapping aligned with the legacy inference score:
#     score = logits[0] - logits[1]
# Therefore class 0 must be the near / positive class.
LABEL_TO_TARGET = {"near": 0, "far": 1, "artifact": 1}
TARGET_TO_LABEL = {0: "near", 1: "non_target"}
DATASET_SAMPLE_RATE = 16_000
MODEL_WINDOW_SAMPLES_16K = 16_000

METADATA_FIELDS = [
    "path",
    "target",
    "class_name",
    "polarity",
    "distance_cm",
    "speaker_id",
    "speech_style",
    "angle_deg",
    "session_id",
    "sample_index",
    "duration_s",
    "sample_rate",
    "channels",
    "encoding",
    "timestamp_utc",
    "peak",
    "rms",
    "phrase",
    "notes",
]


def sanitize_token(value: str) -> str:
    value = value.strip()
    if not value:
        return "na"
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return value.strip("-._") or "na"


def float32_to_pcm16le(samples: np.ndarray) -> bytes:
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not np.all(np.isfinite(x)):
        raise ValueError("Audio contains NaN or infinite values")
    scaled = np.rint(np.clip(x, -1.0, 32767.0 / 32768.0) * 32768.0)
    return scaled.astype("<i2", copy=False).tobytes()


def write_pcm16_wav(path: str | Path, samples: np.ndarray, sample_rate: int = DATASET_SAMPLE_RATE) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(float32_to_pcm16le(samples))


def read_pcm16_wav(path: str | Path) -> tuple[np.ndarray, int]:
    path = Path(path)
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if channels != 1:
        raise ValueError(f"{path}: expected mono WAV, got {channels} channels")
    if sample_width != 2:
        raise ValueError(f"{path}: expected PCM16 WAV, got {sample_width * 8}-bit samples")
    if sample_rate != DATASET_SAMPLE_RATE:
        raise ValueError(f"{path}: expected 16000 Hz WAV, got {sample_rate} Hz")
    if len(frames) % 2:
        raise ValueError(f"{path}: malformed PCM16 byte length")

    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / np.float32(32768.0)
    return samples, sample_rate


def center_crop_or_pad(samples: np.ndarray, size: int = MODEL_WINDOW_SAMPLES_16K) -> np.ndarray:
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size == size:
        return np.ascontiguousarray(x)
    if x.size > size:
        start = (x.size - size) // 2
        return np.ascontiguousarray(x[start : start + size])

    pad_total = size - x.size
    left = pad_total // 2
    right = pad_total - left
    return np.pad(x, (left, right), mode="constant").astype(np.float32, copy=False)


def audio_peak_rms(samples: np.ndarray) -> tuple[float, float]:
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return 0.0, 0.0
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return peak, rms


def append_metadata_row(csv_path: str | Path, row: dict[str, object]) -> None:
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.exists() and csv_path.stat().st_size > 0
    with csv_path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=METADATA_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in METADATA_FIELDS})


@dataclass(frozen=True)
class DatasetRecord:
    path: Path
    target: int
    class_name: str
    polarity: str
    distance_cm: float
    speaker_id: str
    speech_style: str
    angle_deg: float
    session_id: str
    sample_index: int
    # Optional logical sub-range of ``path``.  The raw WAV is never physically
    # rewritten: segment mode creates pseudo-takes that point into one long WAV.
    segment_index: int = 0
    segment_start_sample: int = 0
    segment_end_sample: int | None = None
    phrase: str = ""
    notes: str = ""


def load_metadata(dataset_root: str | Path) -> list[DatasetRecord]:
    root = Path(dataset_root)
    metadata_path = root / "metadata.csv"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Dataset metadata not found: {metadata_path}")

    records: list[DatasetRecord] = []
    with metadata_path.open("r", newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            try:
                class_name = str(row["class_name"]).strip().lower()
                target = int(row["target"])
                expected = LABEL_TO_TARGET[class_name]
                if target != expected:
                    raise ValueError(
                        f"target={target} disagrees with class_name={class_name!r}; expected {expected}"
                    )
                wav_path = root / str(row["path"])
                if not wav_path.exists():
                    raise FileNotFoundError(wav_path)
                records.append(
                    DatasetRecord(
                        path=wav_path,
                        target=target,
                        class_name=class_name,
                        polarity=str(row.get("polarity", "")),
                        distance_cm=float(row.get("distance_cm", 0.0)),
                        speaker_id=str(row.get("speaker_id", "unknown")),
                        speech_style=str(row.get("speech_style", "unknown")),
                        angle_deg=float(row.get("angle_deg", 0.0) or 0.0),
                        session_id=str(row.get("session_id", "unknown")),
                        sample_index=int(row.get("sample_index", 0) or 0),
                        phrase=str(row.get("phrase", "")),
                        notes=str(row.get("notes", "")),
                    )
                )
            except Exception as exc:
                raise ValueError(f"Invalid metadata row {line_no} in {metadata_path}: {exc}") from exc

    if not records:
        raise ValueError(f"No samples found in {metadata_path}")
    return records


def _stratified_file_split(
    records: Sequence[DatasetRecord],
    *,
    val_fraction: float,
    test_fraction: float,
    seed: int,
) -> dict[str, list[int]]:
    """Split whole WAV takes while preserving each collection subtype.

    We stratify on ``class_name`` rather than only the binary target so that
    near speech, far speech, and artifact hard negatives are each represented
    in train/validation/test.  All windows from one long take remain together.
    """

    rng = np.random.default_rng(seed)
    split = {"train": [], "val": [], "test": []}
    class_names = sorted({r.class_name for r in records})
    for class_name in class_names:
        idx = np.array(
            [i for i, r in enumerate(records) if r.class_name == class_name],
            dtype=np.int64,
        )
        if idx.size < 3:
            raise ValueError(
                f"Need at least 3 long takes for collection class {class_name!r}; "
                f"got {idx.size}. At least one take is needed in train/val/test."
            )
        rng.shuffle(idx)
        n_test = max(1, int(round(idx.size * test_fraction)))
        n_val = max(1, int(round(idx.size * val_fraction)))
        if n_test + n_val >= idx.size:
            n_test = 1
            n_val = 1
        split["test"].extend(idx[:n_test].tolist())
        split["val"].extend(idx[n_test : n_test + n_val].tolist())
        split["train"].extend(idx[n_test + n_val :].tolist())

    for indices in split.values():
        rng.shuffle(indices)
    return split


def _speaker_group_split(
    records: Sequence[DatasetRecord],
    *,
    val_fraction: float,
    test_fraction: float,
    seed: int,
) -> dict[str, list[int]]:
    speakers = sorted({r.speaker_id for r in records})
    if len(speakers) < 3:
        raise ValueError("Speaker-disjoint split requires at least 3 distinct speaker_id values")

    # Require each speaker to contain both classes; otherwise a held-out split can
    # accidentally contain only one class and yield meaningless metrics.
    for speaker in speakers:
        classes = {r.target for r in records if r.speaker_id == speaker}
        if classes != {0, 1}:
            raise ValueError(
                f"Speaker {speaker!r} does not contain both near and far samples; "
                "speaker-disjoint evaluation requires both classes per speaker"
            )

    rng = np.random.default_rng(seed)
    speakers_arr = np.array(speakers, dtype=object)
    rng.shuffle(speakers_arr)

    n = len(speakers_arr)
    n_test = max(1, int(round(n * test_fraction)))
    n_val = max(1, int(round(n * val_fraction)))
    if n_test + n_val >= n:
        n_test = 1
        n_val = 1
    test_speakers = set(speakers_arr[:n_test].tolist())
    val_speakers = set(speakers_arr[n_test : n_test + n_val].tolist())
    train_speakers = set(speakers_arr[n_test + n_val :].tolist())

    split = {"train": [], "val": [], "test": []}
    for i, r in enumerate(records):
        if r.speaker_id in test_speakers:
            split["test"].append(i)
        elif r.speaker_id in val_speakers:
            split["val"].append(i)
        elif r.speaker_id in train_speakers:
            split["train"].append(i)
        else:
            raise AssertionError(r.speaker_id)
    return split


def make_split(
    records: Sequence[DatasetRecord],
    *,
    split_by: str = "file",
    val_fraction: float = 0.15,
    test_fraction: float = 0.15,
    seed: int = 42,
) -> tuple[dict[str, list[int]], str]:
    if not 0 < val_fraction < 0.5 or not 0 < test_fraction < 0.5:
        raise ValueError("val_fraction and test_fraction must both be between 0 and 0.5")
    if val_fraction + test_fraction >= 0.8:
        raise ValueError("Validation + test fractions leave too little training data")

    split_by = split_by.lower()
    if split_by not in {"auto", "speaker", "file", "segment"}:
        raise ValueError("split_by must be auto, speaker, file, or segment")

    if split_by == "segment":
        # ``train.py`` first expands long WAVs with ``segment_records``.  At
        # this point each DatasetRecord is one pseudo-take, so the same
        # stratified record split can be reused.
        if not any(r.segment_end_sample is not None for r in records):
            raise ValueError(
                "segment split requires records created by segment_records()"
            )
        split = _stratified_file_split(
            records,
            val_fraction=val_fraction,
            test_fraction=test_fraction,
            seed=seed,
        )
        return split, "segment"

    if split_by in {"auto", "speaker"}:
        try:
            split = _speaker_group_split(
                records,
                val_fraction=val_fraction,
                test_fraction=test_fraction,
                seed=seed,
            )
            return split, "speaker"
        except ValueError:
            if split_by == "speaker":
                raise

    split = _stratified_file_split(
        records,
        val_fraction=val_fraction,
        test_fraction=test_fraction,
        seed=seed,
    )
    return split, "file"


class ProximityFeatureDataset(Dataset[tuple[torch.Tensor, torch.Tensor]]):
    """WAV -> exact legacy ProxiMic features.

    No per-clip amplitude normalization is applied. This is intentional: the
    inference path also sees the Ring waveform at its native gain.
    """

    def __init__(
        self,
        records: Sequence[DatasetRecord],
        indices: Sequence[int],
        *,
        cache_features: bool = True,
    ) -> None:
        self.records = records
        self.indices = list(indices)
        self.cache_features = bool(cache_features)
        self._cache: dict[int, np.ndarray] = {}
        self._downsampler = LegacyDownsampler16kTo8k()
        self._features = LegacyFeatureExtractor()

    def __len__(self) -> int:
        return len(self.indices)

    def _feature_for_record(self, record_idx: int) -> np.ndarray:
        if self.cache_features and record_idx in self._cache:
            return self._cache[record_idx]

        record = self.records[record_idx]
        audio_16k, _ = read_pcm16_wav(record.path)
        segment_end = record.segment_end_sample if record.segment_end_sample is not None else audio_16k.size
        clip_16k = audio_16k[record.segment_start_sample:segment_end]
        window_16k = center_crop_or_pad(clip_16k, MODEL_WINDOW_SAMPLES_16K)
        audio_8k = self._downsampler(window_16k)
        feat = self._features.extract(audio_8k)
        if self.cache_features:
            self._cache[record_idx] = feat
        return feat

    def __getitem__(self, item: int) -> tuple[torch.Tensor, torch.Tensor]:
        record_idx = self.indices[item]
        record = self.records[record_idx]
        feat = self._feature_for_record(record_idx)
        x = torch.from_numpy(np.ascontiguousarray(feat)).float()
        y = torch.tensor(record.target, dtype=torch.long)
        return x, y



@dataclass(frozen=True)
class WindowSpec:
    """One 1-second training/evaluation window derived from a longer take."""

    record_idx: int
    start_sample: int
    min_start_sample: int
    max_start_sample: int


def _wav_frame_count(path: Path) -> int:
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono WAV")
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected PCM16 WAV")
        if wf.getframerate() != DATASET_SAMPLE_RATE:
            raise ValueError(f"{path}: expected {DATASET_SAMPLE_RATE} Hz WAV")
        return int(wf.getnframes())


def segment_records(
    records: Sequence[DatasetRecord],
    *,
    segment_duration_s: float = 8.0,
) -> list[DatasetRecord]:
    """Turn long WAVs into fixed-duration logical pseudo-takes.

    No audio files are copied or rewritten.  Each returned ``DatasetRecord``
    points to the original WAV plus a non-overlapping sample range.  For a WAV
    longer than ``segment_duration_s``, only complete fixed-length segments are
    emitted; a final shorter remainder is intentionally dropped so train/val/test
    units all have the same duration.  Legacy WAVs shorter than one segment are
    kept as a single record for backward compatibility.
    """

    if segment_duration_s <= 0:
        raise ValueError("segment_duration_s must be > 0")
    segment_samples = int(round(segment_duration_s * DATASET_SAMPLE_RATE))
    if segment_samples < MODEL_WINDOW_SAMPLES_16K:
        raise ValueError("segment_duration_s must be at least 1.0 second")

    segmented: list[DatasetRecord] = []
    for record in records:
        total_samples = _wav_frame_count(record.path)
        if total_samples <= segment_samples:
            segmented.append(
                replace(
                    record,
                    segment_index=0,
                    segment_start_sample=0,
                    segment_end_sample=total_samples,
                )
            )
            continue

        full_segments = total_samples // segment_samples
        for segment_index in range(full_segments):
            start = segment_index * segment_samples
            end = start + segment_samples
            segmented.append(
                replace(
                    record,
                    segment_index=segment_index,
                    segment_start_sample=start,
                    segment_end_sample=end,
                )
            )

    if not segmented:
        raise ValueError("No pseudo-takes could be created from the dataset")
    return segmented


def make_window_specs(
    records: Sequence[DatasetRecord],
    indices: Sequence[int],
    *,
    hop_s: float = 0.50,
    edge_margin_s: float = 0.50,
) -> list[WindowSpec]:
    """Create overlapping 1-second windows after the split unit is chosen.

    In normal file mode the split unit is the whole WAV.  In segment mode it is
    the fixed-duration pseudo-take described by ``segment_start_sample`` /
    ``segment_end_sample``.  Window starts and training jitter are constrained to
    that unit, so a 1-second model window never crosses a train/val/test boundary.
    """

    if hop_s <= 0:
        raise ValueError("hop_s must be > 0")
    if edge_margin_s < 0:
        raise ValueError("edge_margin_s must be >= 0")

    hop = max(1, int(round(hop_s * DATASET_SAMPLE_RATE)))
    margin = int(round(edge_margin_s * DATASET_SAMPLE_RATE))
    window = MODEL_WINDOW_SAMPLES_16K
    specs: list[WindowSpec] = []

    for record_idx in indices:
        record = records[record_idx]
        wav_n = _wav_frame_count(record.path)
        segment_start = int(record.segment_start_sample)
        segment_end = int(record.segment_end_sample) if record.segment_end_sample is not None else wav_n
        if segment_start < 0 or segment_end > wav_n or segment_end <= segment_start:
            raise ValueError(
                f"Invalid segment bounds for {record.path}: "
                f"[{segment_start}, {segment_end}) with WAV length {wav_n}"
            )
        n = segment_end - segment_start
        if n < window:
            # Backward compatibility for old short clips: use one centered,
            # zero-padded window. Segment mode itself emits >=1-s pseudo-takes.
            center = segment_start + max(0, (n - window) // 2)
            specs.append(WindowSpec(record_idx, center, center, center))
            continue

        local_min_start = min(margin, max(0, n - window))
        local_max_start = max(0, n - margin - window)
        if local_max_start < local_min_start:
            center = segment_start + max(0, (n - window) // 2)
            specs.append(WindowSpec(record_idx, center, center, center))
            continue

        min_start = segment_start + local_min_start
        max_start = segment_start + local_max_start
        starts = list(range(min_start, max_start + 1, hop))
        if not starts:
            starts = [min_start]
        # Always include the latest legal window even when hop does not land on it.
        if starts[-1] != max_start:
            starts.append(max_start)

        for start in starts:
            specs.append(WindowSpec(record_idx, start, min_start, max_start))

    if not specs:
        raise ValueError("No training windows could be created")
    return specs


def discover_noise_wavs(noise_dir: str | Path) -> list[Path]:
    """Return background-noise WAV files below ``noise_dir`` recursively."""

    root = Path(noise_dir)
    if not root.exists():
        raise FileNotFoundError(f"Noise directory not found: {root}")
    paths = sorted(p for p in root.rglob("*.wav") if p.is_file())
    if not paths:
        raise ValueError(f"No .wav background-noise files found under {root}")
    return paths


def split_auxiliary_files(
    paths: Sequence[Path],
    *,
    val_fraction: float,
    test_fraction: float,
    seed: int,
) -> dict[str, list[Path]]:
    """Create disjoint train/val/test pools for auxiliary noise recordings."""

    if len(paths) < 3:
        raise ValueError(
            "At least 3 background-noise WAV files are required so train, val, and test "
            "can use disjoint noise files"
        )
    rng = np.random.default_rng(seed)
    order = np.arange(len(paths), dtype=np.int64)
    rng.shuffle(order)
    n_test = max(1, int(round(len(paths) * test_fraction)))
    n_val = max(1, int(round(len(paths) * val_fraction)))
    if n_test + n_val >= len(paths):
        n_test = 1
        n_val = 1
    return {
        "test": [Path(paths[i]) for i in order[:n_test]],
        "val": [Path(paths[i]) for i in order[n_test : n_test + n_val]],
        "train": [Path(paths[i]) for i in order[n_test + n_val :]],
    }


def read_background_wav(path: str | Path) -> np.ndarray:
    """Read a PCM16 WAV, downmix to mono, and linearly resample to 16 kHz.

    Collected Ring WAVs remain strict mono/16-kHz PCM16.  Background files are
    allowed to be mono or multi-channel and may use a different sample rate.
    """

    path = Path(path)
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sample_width != 2:
        raise ValueError(f"{path}: background noise must be PCM16 WAV")
    if channels <= 0 or sample_rate <= 0:
        raise ValueError(f"{path}: invalid WAV header")

    x = np.frombuffer(frames, dtype="<i2").astype(np.float32) / np.float32(32768.0)
    if x.size % channels:
        raise ValueError(f"{path}: malformed interleaved PCM")
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    if x.size == 0:
        raise ValueError(f"{path}: empty background-noise WAV")
    if sample_rate != DATASET_SAMPLE_RATE:
        out_n = max(1, int(round(x.size * DATASET_SAMPLE_RATE / sample_rate)))
        old_pos = np.arange(x.size, dtype=np.float64)
        new_pos = np.linspace(0.0, max(0.0, x.size - 1.0), out_n, dtype=np.float64)
        x = np.interp(new_pos, old_pos, x).astype(np.float32)
    return np.ascontiguousarray(x, dtype=np.float32)


class WindowedProximityFeatureDataset(Dataset[tuple[torch.Tensor, torch.Tensor]]):
    """Long WAV take -> many legacy ProxiMic 1-second feature windows.

    Training may apply a small deterministic start-time jitter around each base
    window on every epoch. Validation/test use zero jitter. No amplitude
    normalization is applied, matching the real-time Ring inference path.
    """

    def __init__(
        self,
        records: Sequence[DatasetRecord],
        indices: Sequence[int],
        *,
        hop_s: float = 0.50,
        edge_margin_s: float = 0.50,
        jitter_s: float = 0.0,
        seed: int = 42,
        cache_audio: bool = True,
        cache_features: bool = False,
        noise_paths: Sequence[Path] | None = None,
        noise_probability: float = 0.0,
        noise_snr_min_db: float = 12.0,
        noise_snr_max_db: float = 25.0,
        noise_randomize_per_epoch: bool = False,
    ) -> None:
        if jitter_s < 0:
            raise ValueError("jitter_s must be >= 0")
        if not 0.0 <= noise_probability <= 1.0:
            raise ValueError("noise_probability must be between 0 and 1")
        if noise_snr_min_db > noise_snr_max_db:
            raise ValueError("noise_snr_min_db cannot exceed noise_snr_max_db")
        self.records = records
        self.indices = list(indices)
        self.specs = make_window_specs(
            records,
            self.indices,
            hop_s=hop_s,
            edge_margin_s=edge_margin_s,
        )
        self.jitter_samples = int(round(jitter_s * DATASET_SAMPLE_RATE))
        self.seed = int(seed)
        self.epoch = 0
        self.cache_audio = bool(cache_audio)
        self.noise_paths = [Path(p) for p in (noise_paths or [])]
        self.noise_probability = float(noise_probability if self.noise_paths else 0.0)
        self.noise_snr_min_db = float(noise_snr_min_db)
        self.noise_snr_max_db = float(noise_snr_max_db)
        self.noise_randomize_per_epoch = bool(noise_randomize_per_epoch)
        self.cache_features = (
            bool(cache_features)
            and self.jitter_samples == 0
            and not self.noise_randomize_per_epoch
        )
        self._audio_cache: dict[Path, np.ndarray] = {}
        self._noise_cache: dict[Path, np.ndarray] = {}
        self._feature_cache: dict[tuple[int, int, int], np.ndarray] = {}

        # Save a few training-time noise mixtures so they can be listened to.
        # Validation/test datasets do not save previews because
        # noise_randomize_per_epoch=False for those splits.
        self._noise_preview_saved = 0
        self._noise_preview_limit = 5 if self.noise_randomize_per_epoch else 0
        self._noise_preview_dir = Path("noise_preview")

        self._downsampler = LegacyDownsampler16kTo8k()
        self._features = LegacyFeatureExtractor()

    def __len__(self) -> int:
        return len(self.specs)

    def set_epoch(self, epoch: int) -> None:
        self.epoch = int(epoch)

    def _audio_for_record(self, record_idx: int) -> np.ndarray:
        path = self.records[record_idx].path
        if self.cache_audio and path in self._audio_cache:
            return self._audio_cache[path]
        audio, _ = read_pcm16_wav(path)
        if self.cache_audio:
            self._audio_cache[path] = audio
        return audio

    def _start_for_item(self, item: int, spec: WindowSpec) -> int:
        if self.jitter_samples <= 0 or spec.max_start_sample <= spec.min_start_sample:
            return spec.start_sample
        # Deterministic per (epoch, item), so rerunning with the same seed is stable.
        rng = np.random.default_rng(self.seed + self.epoch * 1_000_003 + item * 9_176)
        delta = int(rng.integers(-self.jitter_samples, self.jitter_samples + 1))
        return int(np.clip(spec.start_sample + delta, spec.min_start_sample, spec.max_start_sample))

    def _noise_for_path(self, path: Path) -> np.ndarray:
        if path not in self._noise_cache:
            self._noise_cache[path] = read_background_wav(path)
        return self._noise_cache[path]

    def _mix_background_noise(
        self,
        window: np.ndarray,
        item: int,
        record_idx: int,
    ) -> np.ndarray:
        if not self.noise_paths or self.noise_probability <= 0.0:
            return window

        epoch_component = self.epoch if self.noise_randomize_per_epoch else 0
        rng = np.random.default_rng(
            self.seed + 37_919 + epoch_component * 1_000_003 + item * 9_176
        )
        if float(rng.random()) > self.noise_probability:
            return window

        noise_path = self.noise_paths[int(rng.integers(0, len(self.noise_paths)))]
        noise = self._noise_for_path(noise_path)
        size = MODEL_WINDOW_SAMPLES_16K
        if noise.size < size:
            repeats = int(math.ceil(size / max(1, noise.size)))
            noise = np.tile(noise, repeats)

        max_start = max(0, noise.size - size)
        noise_start = int(rng.integers(0, max_start + 1)) if max_start > 0 else 0
        noise_window = np.ascontiguousarray(noise[noise_start : noise_start + size])

        signal_rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
        noise_rms = float(np.sqrt(np.mean(np.square(noise_window, dtype=np.float64))))

        # If either side is effectively silent, leave the Ring sample unchanged.
        if signal_rms < 1e-8 or noise_rms < 1e-8:
            return window

        snr_db = float(rng.uniform(self.noise_snr_min_db, self.noise_snr_max_db))
        desired_noise_rms = signal_rms / (10.0 ** (snr_db / 20.0))
        scale = np.float32(desired_noise_rms / noise_rms)
        scaled_noise = noise_window * scale

        mixed = window + scaled_noise
        mixed = np.clip(
            mixed,
            -1.0,
            32767.0 / 32768.0,
        ).astype(np.float32, copy=False)

        # Save a few 1-second training examples for listening.  Each group has:
        # clean Ring audio, the SNR-scaled noise alone, and the actual mixed input.
        if self._noise_preview_saved < self._noise_preview_limit:
            self._noise_preview_dir.mkdir(parents=True, exist_ok=True)
            preview_idx = self._noise_preview_saved + 1
            record = self.records[record_idx]
            source_name = sanitize_token(record.path.stem)[:80]
            prefix = f"{preview_idx:02d}_{record.class_name}_{source_name}"

            write_pcm16_wav(
                self._noise_preview_dir / f"{prefix}__clean.wav",
                window,
                DATASET_SAMPLE_RATE,
            )
            write_pcm16_wav(
                self._noise_preview_dir
                / f"{prefix}__noise_snr_{snr_db:.1f}dB.wav",
                scaled_noise,
                DATASET_SAMPLE_RATE,
            )
            write_pcm16_wav(
                self._noise_preview_dir
                / f"{prefix}__mixed_snr_{snr_db:.1f}dB.wav",
                mixed,
                DATASET_SAMPLE_RATE,
            )
            print(
                f"[noise preview] saved sample {preview_idx}: "
                f"class={record.class_name}, SNR={snr_db:.1f} dB"
            )
            self._noise_preview_saved += 1

        return mixed

    def _feature(self, record_idx: int, start: int, item: int) -> np.ndarray:
        key = (record_idx, start, item)
        if self.cache_features and key in self._feature_cache:
            return self._feature_cache[key]

        audio = self._audio_for_record(record_idx)
        end = start + MODEL_WINDOW_SAMPLES_16K
        if audio.size >= end:
            window_16k = np.ascontiguousarray(audio[start:end])
        else:
            # Only expected for legacy clips shorter than one second.
            window_16k = center_crop_or_pad(audio, MODEL_WINDOW_SAMPLES_16K)
        # Artifact hard negatives (airflow, hand motion, rubbing, contact, ...)
        # stay clean so their weak acoustic signatures are not masked by added
        # background noise. Near/far speech may still receive augmentation.
        record = self.records[record_idx]
        if record.class_name != "artifact":
            window_16k = self._mix_background_noise(
                window_16k,
                item,
                record_idx,
            )

        audio_8k = self._downsampler(window_16k)
        feat = self._features.extract(audio_8k)
        if self.cache_features:
            self._feature_cache[key] = feat
        return feat

    def __getitem__(self, item: int) -> tuple[torch.Tensor, torch.Tensor]:
        spec = self.specs[item]
        start = self._start_for_item(item, spec)
        feat = self._feature(spec.record_idx, start, item)
        target = self.records[spec.record_idx].target
        x = torch.from_numpy(np.ascontiguousarray(feat)).float()
        y = torch.tensor(target, dtype=torch.long)
        return x, y


def _empty_counts() -> dict[str, int]:
    return {"near": 0, "far": 0, "artifact": 0, "positive": 0, "negative": 0, "total": 0}


def window_split_counts(
    records: Sequence[DatasetRecord],
    specs: Sequence[WindowSpec],
) -> dict[str, int]:
    counts = _empty_counts()
    for spec in specs:
        record = records[spec.record_idx]
        counts.setdefault(record.class_name, 0)
        counts[record.class_name] += 1
        counts["positive" if record.target == 0 else "negative"] += 1
        counts["total"] += 1
    return counts


def split_counts(records: Sequence[DatasetRecord], indices: Iterable[int]) -> dict[str, int]:
    counts = _empty_counts()
    for i in indices:
        record = records[i]
        counts.setdefault(record.class_name, 0)
        counts[record.class_name] += 1
        counts["positive" if record.target == 0 else "negative"] += 1
        counts["total"] += 1
    return counts
