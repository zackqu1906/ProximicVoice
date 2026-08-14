from __future__ import annotations

import csv
import json
import random
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader

from .dataset import (
    LABEL_TO_TARGET,
    DatasetRecord,
    WindowedProximityFeatureDataset,
    discover_noise_wavs,
    load_metadata,
    make_split,
    segment_records,
    split_auxiliary_files,
    split_counts,
    window_split_counts,
)
from .model import ASSET_DIR, CnnNet8


@dataclass(frozen=True)
class Metrics:
    loss: float
    accuracy: float
    balanced_accuracy: float
    precision_near: float
    recall_near: float
    f1_near: float
    recall_non_target: float
    true_near: int
    true_non_target: int
    pred_near: int
    pred_non_target: int


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _resolve_device(name: str) -> torch.device:
    if name == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")
    return torch.device(name)


def _load_state_dict(path: Path, device: torch.device) -> dict[str, torch.Tensor]:
    try:
        state = torch.load(path, map_location=device, weights_only=True)
    except TypeError:
        state = torch.load(path, map_location=device)
    if not isinstance(state, dict):
        raise ValueError(f"Expected a PyTorch state_dict in {path}")
    return state


def _confusion_from_scores(targets: np.ndarray, scores: np.ndarray, threshold: float) -> tuple[int, int, int, int]:
    # near is the positive semantic class but has numeric target 0 for legacy compatibility.
    true_near = targets == LABEL_TO_TARGET["near"]
    pred_near = scores > threshold
    tp = int(np.sum(true_near & pred_near))
    fn = int(np.sum(true_near & ~pred_near))
    fp = int(np.sum(~true_near & pred_near))
    tn = int(np.sum(~true_near & ~pred_near))
    return tp, fn, fp, tn


def _metrics_from_scores(
    targets: np.ndarray,
    scores: np.ndarray,
    *,
    threshold: float,
    loss: float,
) -> Metrics:
    tp, fn, fp, tn = _confusion_from_scores(targets, scores, threshold)
    total = max(1, tp + fn + fp + tn)
    accuracy = (tp + tn) / total
    recall_near = tp / max(1, tp + fn)
    recall_non_target = tn / max(1, tn + fp)
    balanced = 0.5 * (recall_near + recall_non_target)
    precision_near = tp / max(1, tp + fp)
    f1 = 2 * precision_near * recall_near / max(1e-12, precision_near + recall_near)
    return Metrics(
        loss=float(loss),
        accuracy=float(accuracy),
        balanced_accuracy=float(balanced),
        precision_near=float(precision_near),
        recall_near=float(recall_near),
        f1_near=float(f1),
        recall_non_target=float(recall_non_target),
        true_near=int(tp + fn),
        true_non_target=int(tn + fp),
        pred_near=int(tp + fp),
        pred_non_target=int(tn + fn),
    )


def calibrate_threshold(targets: np.ndarray, scores: np.ndarray) -> tuple[float, Metrics]:
    targets = np.asarray(targets, dtype=np.int64)
    scores = np.asarray(scores, dtype=np.float64)
    if targets.size == 0 or scores.size != targets.size:
        raise ValueError("targets and scores must be non-empty and have the same length")
    if set(np.unique(targets).tolist()) != {0, 1}:
        raise ValueError("Threshold calibration requires both near and non-target binary classes")

    unique = np.unique(scores)
    if unique.size == 1:
        candidates = np.array([unique[0]], dtype=np.float64)
    else:
        mids = (unique[:-1] + unique[1:]) / 2.0
        eps = max(1e-6, float(np.ptp(unique)) * 1e-6)
        candidates = np.concatenate(([unique[0] - eps], mids, [unique[-1] + eps]))

    best_threshold = float(candidates[0])
    best_metrics = _metrics_from_scores(targets, scores, threshold=best_threshold, loss=0.0)
    best_key = (best_metrics.balanced_accuracy, best_metrics.f1_near, -abs(best_threshold))
    for threshold in candidates[1:]:
        metrics = _metrics_from_scores(targets, scores, threshold=float(threshold), loss=0.0)
        key = (metrics.balanced_accuracy, metrics.f1_near, -abs(float(threshold)))
        if key > best_key:
            best_threshold = float(threshold)
            best_metrics = metrics
            best_key = key
    return best_threshold, best_metrics


@torch.inference_mode()
def _evaluate_raw(
    model: CnnNet8,
    loader: DataLoader,
    criterion: nn.Module,
    device: torch.device,
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    model.eval()
    losses = 0.0
    total = 0
    all_logits: list[np.ndarray] = []
    all_targets: list[np.ndarray] = []
    for x, y in loader:
        x = x.to(device)
        y = y.to(device)
        logits = model(x)
        loss = criterion(logits, y)
        n = int(y.numel())
        losses += float(loss.item()) * n
        total += n
        all_logits.append(logits.detach().cpu().numpy())
        all_targets.append(y.detach().cpu().numpy())
    if total == 0:
        raise ValueError("Cannot evaluate an empty dataset split")
    logits_np = np.concatenate(all_logits, axis=0).astype(np.float32)
    targets_np = np.concatenate(all_targets, axis=0).astype(np.int64)
    scores_np = (logits_np[:, 0] - logits_np[:, 1]).astype(np.float32)
    return losses / total, targets_np, scores_np, logits_np


def _class_weights_windows(
    records: Sequence[DatasetRecord],
    dataset: WindowedProximityFeatureDataset,
    device: torch.device,
) -> torch.Tensor:
    counts = np.zeros(2, dtype=np.float64)
    for spec in dataset.specs:
        counts[records[spec.record_idx].target] += 1
    if np.any(counts == 0):
        raise ValueError(f"Training windows are missing a class: counts={counts.tolist()}")
    total = float(np.sum(counts))
    weights = total / (2.0 * counts)
    return torch.tensor(weights, dtype=torch.float32, device=device)


def _write_split_manifest(
    path: Path,
    dataset_root: Path,
    records: Sequence[DatasetRecord],
    split: dict[str, list[int]],
) -> None:
    reverse = {idx: split_name for split_name, indices in split.items() for idx in indices}
    fields = [
        "split",
        "path",
        "target",
        "class_name",
        "distance_cm",
        "speaker_id",
        "speech_style",
        "angle_deg",
        "session_id",
        "sample_index",
        "segment_index",
        "segment_start_s",
        "segment_end_s",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for idx, record in enumerate(records):
            writer.writerow(
                {
                    "split": reverse[idx],
                    "path": record.path.relative_to(dataset_root).as_posix(),
                    "target": record.target,
                    "class_name": record.class_name,
                    "distance_cm": record.distance_cm,
                    "speaker_id": record.speaker_id,
                    "speech_style": record.speech_style,
                    "angle_deg": record.angle_deg,
                    "session_id": record.session_id,
                    "sample_index": record.sample_index,
                    "segment_index": record.segment_index,
                    "segment_start_s": f"{record.segment_start_sample / 16000.0:.6f}",
                    "segment_end_s": (
                        f"{record.segment_end_sample / 16000.0:.6f}"
                        if record.segment_end_sample is not None
                        else ""
                    ),
                }
            )


def _write_window_manifest(
    path: Path,
    dataset_root: Path,
    records: Sequence[DatasetRecord],
    datasets: dict[str, WindowedProximityFeatureDataset],
) -> None:
    fields = [
        "split",
        "path",
        "target",
        "class_name",
        "window_index",
        "start_s",
        "end_s",
        "speaker_id",
        "distance_cm",
        "speech_style",
        "session_id",
        "take_index",
        "segment_index",
        "segment_start_s",
        "segment_end_s",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for split_name, ds in datasets.items():
            for window_idx, spec in enumerate(ds.specs):
                record = records[spec.record_idx]
                start_s = spec.start_sample / 16000.0
                writer.writerow(
                    {
                        "split": split_name,
                        "path": record.path.relative_to(dataset_root).as_posix(),
                        "target": record.target,
                        "class_name": record.class_name,
                        "window_index": window_idx,
                        "start_s": f"{start_s:.6f}",
                        "end_s": f"{start_s + 1.0:.6f}",
                        "speaker_id": record.speaker_id,
                        "distance_cm": record.distance_cm,
                        "speech_style": record.speech_style,
                        "session_id": record.session_id,
                        "take_index": record.sample_index,
                        "segment_index": record.segment_index,
                        "segment_start_s": f"{record.segment_start_sample / 16000.0:.6f}",
                        "segment_end_s": (
                            f"{record.segment_end_sample / 16000.0:.6f}"
                            if record.segment_end_sample is not None
                            else ""
                        ),
                    }
                )


def _aggregate_scores_by_take(
    records: Sequence[DatasetRecord],
    dataset: WindowedProximityFeatureDataset,
    scores: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if len(dataset.specs) != len(scores):
        raise ValueError("Window score count does not match dataset specs")
    grouped: dict[int, list[float]] = {}
    for spec, score in zip(dataset.specs, scores, strict=True):
        grouped.setdefault(spec.record_idx, []).append(float(score))
    record_indices = np.array(sorted(grouped), dtype=np.int64)
    targets = np.array([records[i].target for i in record_indices], dtype=np.int64)
    mean_scores = np.array([np.mean(grouped[i]) for i in record_indices], dtype=np.float32)
    return record_indices, targets, mean_scores


def _take_behavior_by_group(
    records: Sequence[DatasetRecord],
    record_indices: np.ndarray,
    scores: np.ndarray,
    *,
    threshold: float,
    group_by: str,
) -> dict[str, dict[str, float | int]]:
    """Summarize take-level acceptance/rejection by class or artifact subtype."""

    grouped: dict[str, list[tuple[int, float]]] = {}
    for record_idx, score in zip(record_indices.tolist(), scores.tolist(), strict=True):
        record = records[int(record_idx)]
        key = str(getattr(record, group_by))
        grouped.setdefault(key, []).append((record.target, float(score)))

    out: dict[str, dict[str, float | int]] = {}
    for key, values in sorted(grouped.items()):
        targets = np.array([v[0] for v in values], dtype=np.int64)
        group_scores = np.array([v[1] for v in values], dtype=np.float64)
        pred_near = group_scores > threshold
        if np.all(targets == 0):
            out[key] = {
                "count": int(len(values)),
                "near_accept_rate": float(np.mean(pred_near)),
            }
        elif np.all(targets == 1):
            out[key] = {
                "count": int(len(values)),
                "non_target_rejection_rate": float(np.mean(~pred_near)),
            }
        else:
            out[key] = {
                "count": int(len(values)),
                "accuracy": float(np.mean(pred_near == (targets == 0))),
            }
    return out


def _save_state_dict_cpu(model: CnnNet8, path: Path) -> None:
    state = {k: v.detach().cpu() for k, v in model.state_dict().items()}
    torch.save(state, path)


def train_proximity_model(
    *,
    dataset_root: Path,
    run_dir: Path,
    epochs: int = 30,
    batch_size: int = 32,
    lr: float | None = None,
    weight_decay: float = 1e-4,
    init: str = "scratch",
    init_checkpoint: Path | None = None,
    device_name: str = "auto",
    seed: int = 42,
    split_by: str = "file",
    split_segment_duration_s: float = 8.0,
    val_fraction: float = 0.15,
    test_fraction: float = 0.15,
    patience: int = 8,
    num_workers: int = 0,
    window_hop_s: float = 0.50,
    edge_margin_s: float = 0.50,
    train_jitter_s: float = 0.15,
    noise_dir: Path | None = None,
    noise_probability: float = 1.0,
    noise_snr_min_db: float = 12.0,
    noise_snr_max_db: float = 25.0,
    noise_eval: bool = True,
) -> int:
    if epochs <= 0 or batch_size <= 0 or patience <= 0:
        raise ValueError("epochs, batch_size, and patience must be positive")
    if num_workers < 0:
        raise ValueError("num_workers cannot be negative")
    if window_hop_s <= 0:
        raise ValueError("window_hop_s must be > 0")
    if split_segment_duration_s < 1.0:
        raise ValueError("split_segment_duration_s must be at least 1.0 second")
    if edge_margin_s < 0 or train_jitter_s < 0:
        raise ValueError("edge_margin_s and train_jitter_s must be >= 0")
    if not 0.0 <= noise_probability <= 1.0:
        raise ValueError("noise_probability must be between 0 and 1")
    if noise_snr_min_db > noise_snr_max_db:
        raise ValueError("noise_snr_min_db cannot exceed noise_snr_max_db")

    _seed_everything(seed)
    device = _resolve_device(device_name)
    source_records = load_metadata(dataset_root)
    records = (
        segment_records(source_records, segment_duration_s=split_segment_duration_s)
        if split_by.lower() == "segment"
        else source_records
    )
    split, actual_split_by = make_split(
        records,
        split_by=split_by,
        val_fraction=val_fraction,
        test_fraction=test_fraction,
        seed=seed,
    )

    noise_split: dict[str, list[Path]] = {"train": [], "val": [], "test": []}
    if noise_dir is not None:
        noise_paths = discover_noise_wavs(noise_dir)
        noise_split = split_auxiliary_files(
            noise_paths,
            val_fraction=val_fraction,
            test_fraction=test_fraction,
            seed=seed + 71_011,
        )

    run_dir.mkdir(parents=True, exist_ok=True)
    _write_split_manifest(run_dir / "split_manifest.csv", dataset_root, records, split)

    train_ds = WindowedProximityFeatureDataset(
        records,
        split["train"],
        hop_s=window_hop_s,
        edge_margin_s=edge_margin_s,
        jitter_s=train_jitter_s,
        seed=seed,
        cache_audio=True,
        cache_features=False,
        noise_paths=noise_split["train"],
        noise_probability=noise_probability,
        noise_snr_min_db=noise_snr_min_db,
        noise_snr_max_db=noise_snr_max_db,
        noise_randomize_per_epoch=bool(noise_split["train"]),
    )
    val_ds = WindowedProximityFeatureDataset(
        records,
        split["val"],
        hop_s=window_hop_s,
        edge_margin_s=edge_margin_s,
        jitter_s=0.0,
        seed=seed,
        cache_audio=True,
        cache_features=True,
        noise_paths=noise_split["val"] if noise_eval else [],
        noise_probability=noise_probability if noise_eval else 0.0,
        noise_snr_min_db=noise_snr_min_db,
        noise_snr_max_db=noise_snr_max_db,
        noise_randomize_per_epoch=False,
    )
    test_ds = WindowedProximityFeatureDataset(
        records,
        split["test"],
        hop_s=window_hop_s,
        edge_margin_s=edge_margin_s,
        jitter_s=0.0,
        seed=seed,
        cache_audio=True,
        cache_features=True,
        noise_paths=noise_split["test"] if noise_eval else [],
        noise_probability=noise_probability if noise_eval else 0.0,
        noise_snr_min_db=noise_snr_min_db,
        noise_snr_max_db=noise_snr_max_db,
        noise_randomize_per_epoch=False,
    )
    _write_window_manifest(
        run_dir / "window_manifest.csv",
        dataset_root,
        records,
        {"train": train_ds, "val": val_ds, "test": test_ds},
    )

    generator = torch.Generator().manual_seed(seed)
    train_loader = DataLoader(
        train_ds,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        generator=generator,
    )
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers)
    test_loader = DataLoader(test_ds, batch_size=batch_size, shuffle=False, num_workers=num_workers)

    model = CnnNet8(20, 2).to(device)
    init = init.lower()
    if init not in {"pretrained", "scratch"}:
        raise ValueError("init must be pretrained or scratch")
    resolved_init_checkpoint: Path | None = None
    if init == "pretrained":
        resolved_init_checkpoint = init_checkpoint or (ASSET_DIR / "speech-xiaomi.model")
        model.load_state_dict(_load_state_dict(resolved_init_checkpoint, device), strict=True)

    resolved_lr = float(lr if lr is not None else (1e-4 if init == "pretrained" else 1e-3))
    criterion = nn.CrossEntropyLoss(weight=_class_weights_windows(records, train_ds, device))
    optimizer = torch.optim.AdamW(model.parameters(), lr=resolved_lr, weight_decay=weight_decay)

    print("\nTraining configuration")
    print(f"  dataset        : {dataset_root}")
    print(f"  run_dir        : {run_dir}")
    print(f"  device         : {device}")
    print(f"  init           : {init}")
    if resolved_init_checkpoint is not None:
        print(f"  init checkpoint: {resolved_init_checkpoint}")
    print(f"  learning rate  : {resolved_lr:g}")
    if actual_split_by == "segment":
        print(
            f"  split strategy : segment ({split_segment_duration_s:g} s pseudo-takes; "
            "raw WAVs stay unchanged)"
        )
    else:
        print(f"  split strategy : {actual_split_by} (split long takes before making windows)")
    print(f"  window          : 1.0 s")
    print(f"  window hop      : {window_hop_s:g} s")
    print(f"  edge margin     : {edge_margin_s:g} s")
    print(f"  train jitter    : +/- {train_jitter_s:g} s per epoch")
    if noise_dir is not None:
        print(f"  noise dir       : {noise_dir}")
        print(
            f"  noise mixing    : p={noise_probability:g}, SNR={noise_snr_min_db:g}..{noise_snr_max_db:g} dB, "
            f"eval={'on' if noise_eval else 'off'}"
        )
        print(
            f"  noise WAV split : train={len(noise_split['train'])}, "
            f"val={len(noise_split['val'])}, test={len(noise_split['test'])}"
        )
    for name, ds in (("train", train_ds), ("val", val_ds), ("test", test_ds)):
        takes = split_counts(records, split[name])
        wins = window_split_counts(records, ds.specs)
        print(
            f"  {name:5s} takes    : {takes['total']} "
            f"(near={takes['near']}, far={takes['far']}, artifact={takes['artifact']}; "
            f"binary neg={takes['negative']})"
        )
        print(
            f"  {name:5s} windows  : {wins['total']} "
            f"(near={wins['near']}, far={wins['far']}, artifact={wins['artifact']}; "
            f"binary neg={wins['negative']})"
        )
    if actual_split_by == "file":
        print("  NOTE           : file-level split ignores speaker_id; use --split-by speaker for unseen-speaker evaluation.")
    elif actual_split_by == "segment":
        print(
            "  NOTE           : segment split may place different 8-s chunks from the same original WAV "
            "into different splits; useful for efficient development, but file/speaker split is stricter evaluation."
        )

    history_path = run_dir / "history.csv"
    with history_path.open("w", newline="", encoding="utf-8") as hf:
        writer = csv.writer(hf)
        writer.writerow(["epoch", "train_loss", "val_loss", "val_bal_acc_at_0", "val_acc_at_0"])

    best_model_path = run_dir / "best.model"
    last_model_path = run_dir / "last.model"
    best_val_loss = float("inf")
    stale_epochs = 0

    for epoch in range(1, epochs + 1):
        train_ds.set_epoch(epoch)
        model.train()
        total_loss = 0.0
        total_samples = 0
        for x, y in train_loader:
            x = x.to(device)
            y = y.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(x)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()
            n = int(y.numel())
            total_loss += float(loss.item()) * n
            total_samples += n

        train_loss = total_loss / max(1, total_samples)
        val_loss, val_targets, val_scores, _ = _evaluate_raw(model, val_loader, criterion, device)
        val_metrics_at_zero = _metrics_from_scores(
            val_targets, val_scores, threshold=0.0, loss=val_loss
        )
        print(
            f"Epoch {epoch:03d}/{epochs:03d} | "
            f"train_loss={train_loss:.5f} | val_loss={val_loss:.5f} | "
            f"val_bal_acc(score>0)={val_metrics_at_zero.balanced_accuracy:.4f}"
        )

        with history_path.open("a", newline="", encoding="utf-8") as hf:
            csv.writer(hf).writerow(
                [
                    epoch,
                    f"{train_loss:.8f}",
                    f"{val_loss:.8f}",
                    f"{val_metrics_at_zero.balanced_accuracy:.8f}",
                    f"{val_metrics_at_zero.accuracy:.8f}",
                ]
            )

        _save_state_dict_cpu(model, last_model_path)
        if val_loss < best_val_loss - 1e-6:
            best_val_loss = val_loss
            stale_epochs = 0
            _save_state_dict_cpu(model, best_model_path)
        else:
            stale_epochs += 1
            if stale_epochs >= patience:
                print(f"Early stopping after {epoch} epochs (patience={patience}).")
                break

    model.load_state_dict(_load_state_dict(best_model_path, device), strict=True)
    val_loss, val_targets, val_scores, val_logits = _evaluate_raw(model, val_loader, criterion, device)
    threshold, calibrated_val_metrics = calibrate_threshold(val_targets, val_scores)
    calibrated_val_metrics = _metrics_from_scores(
        val_targets, val_scores, threshold=threshold, loss=val_loss
    )

    test_loss, test_targets, test_scores, test_logits = _evaluate_raw(model, test_loader, criterion, device)
    test_metrics = _metrics_from_scores(test_targets, test_scores, threshold=threshold, loss=test_loss)

    val_take_idx, val_take_targets, val_take_scores = _aggregate_scores_by_take(
        records, val_ds, val_scores
    )
    test_take_idx, test_take_targets, test_take_scores = _aggregate_scores_by_take(
        records, test_ds, test_scores
    )
    val_take_metrics = _metrics_from_scores(
        val_take_targets, val_take_scores, threshold=threshold, loss=val_loss
    )
    test_take_metrics = _metrics_from_scores(
        test_take_targets, test_take_scores, threshold=threshold, loss=test_loss
    )
    val_take_by_class = _take_behavior_by_group(
        records, val_take_idx, val_take_scores, threshold=threshold, group_by="class_name"
    )
    test_take_by_class = _take_behavior_by_group(
        records, test_take_idx, test_take_scores, threshold=threshold, group_by="class_name"
    )
    test_take_by_style = _take_behavior_by_group(
        records, test_take_idx, test_take_scores, threshold=threshold, group_by="speech_style"
    )

    np.savez_compressed(
        run_dir / "validation_outputs.npz",
        targets=val_targets,
        scores=val_scores,
        logits=val_logits,
    )
    np.savez_compressed(
        run_dir / "test_outputs.npz",
        targets=test_targets,
        scores=test_scores,
        logits=test_logits,
    )
    np.savez_compressed(
        run_dir / "validation_take_outputs.npz",
        record_indices=val_take_idx,
        targets=val_take_targets,
        mean_scores=val_take_scores,
    )
    np.savez_compressed(
        run_dir / "test_take_outputs.npz",
        record_indices=test_take_idx,
        targets=test_take_targets,
        mean_scores=test_take_scores,
    )

    timestamp = datetime.now(timezone.utc).isoformat()
    report = {
        "created_utc": timestamp,
        "architecture": "CnnNet8",
        "input_feature_shape": [20, 201],
        "input_audio": {
            "sample_rate": 16000,
            "window_s": 1.0,
            "window_hop_s": window_hop_s,
            "edge_margin_s": edge_margin_s,
            "train_jitter_s": train_jitter_s,
        },
        "preprocessing": "LegacyDownsampler16kTo8k + LegacyFeatureExtractor",
        "label_mapping": {"near": 0, "far": 1, "artifact": 1},
        "binary_task": {"0": "near_speech", "1": "realistic_non_target_audio"},
        "score_definition": "logits[0] - logits[1]",
        "recommended_stage2_threshold": threshold,
        "split_strategy": actual_split_by,
        "split_segment_duration_s": (
            split_segment_duration_s if actual_split_by == "segment" else None
        ),
        "source_record_count": len(source_records),
        "split_unit_count": len(records),
        "seed": seed,
        "init": init,
        "init_checkpoint": str(resolved_init_checkpoint) if resolved_init_checkpoint else None,
        "learning_rate": resolved_lr,
        "best_val_loss": best_val_loss,
        "background_noise": {
            "enabled": noise_dir is not None,
            "noise_dir": str(noise_dir) if noise_dir is not None else None,
            "probability": noise_probability if noise_dir is not None else 0.0,
            "snr_min_db": noise_snr_min_db,
            "snr_max_db": noise_snr_max_db,
            "evaluation_mixed": bool(noise_dir is not None and noise_eval),
            "file_counts": {name: len(noise_split[name]) for name in ("train", "val", "test")},
        },
        "take_counts": {name: split_counts(records, split[name]) for name in ("train", "val", "test")},
        "window_counts": {
            "train": window_split_counts(records, train_ds.specs),
            "val": window_split_counts(records, val_ds.specs),
            "test": window_split_counts(records, test_ds.specs),
        },
        "validation_window_level": asdict(calibrated_val_metrics),
        "test_window_level": asdict(test_metrics),
        "validation_take_level_mean_score": asdict(val_take_metrics),
        "test_take_level_mean_score": asdict(test_take_metrics),
        "validation_take_behavior_by_class": val_take_by_class,
        "test_take_behavior_by_class": test_take_by_class,
        "test_take_behavior_by_style": test_take_by_style,
    }
    (run_dir / "metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Sidecar is deliberately named after the checkpoint so runtime can discover it.
    sidecar_path = best_model_path.with_name(best_model_path.name + ".json")
    sidecar_path.write_text(
        json.dumps(
            {
                "recommended_stage2_threshold": threshold,
                "label_mapping": {"near": 0, "far": 1, "artifact": 1},
                "binary_task": {"0": "near_speech", "1": "realistic_non_target_audio"},
                "score_definition": "logits[0] - logits[1]",
                "metrics_file": "metrics.json",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\nBest model saved:")
    print(f"  {best_model_path}")
    print(f"Recommended Stage-2 threshold: {threshold:+.6f}")
    print(
        f"Validation balanced accuracy={calibrated_val_metrics.balanced_accuracy:.4f}, "
        f"near recall={calibrated_val_metrics.recall_near:.4f}, "
        f"non-target recall={calibrated_val_metrics.recall_non_target:.4f}"
    )
    print(
        f"Test balanced accuracy={test_metrics.balanced_accuracy:.4f}, "
        f"near recall={test_metrics.recall_near:.4f}, "
        f"non-target recall={test_metrics.recall_non_target:.4f}"
    )
    print("\nRuntime example:")
    print(
        "  python -m proximic_ring ring "
        f'--model "{best_model_path}" --show-stage1 --stage1-threshold 0.01'
    )
    print("  (The custom model sidecar will auto-load the calibrated Stage-2 threshold.)")
    return 0
