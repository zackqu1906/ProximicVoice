"""Compare Stage-2 scores for aligned original and gain-matched datasets."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import statistics
import sys

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from proximic_ring.dataset import read_pcm16_wav  # noqa: E402
from proximic_ring.model import ProxiMicModel  # noqa: E402
from proximic_ring.pipeline import LegacyInferencePipeline  # noqa: E402


def _rows(root: Path) -> list[dict[str, str]]:
    with (root / "metadata.csv").open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _highest_rms_second(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    frame = sample_rate
    if audio.size < frame:
        return np.pad(audio, (0, frame - audio.size))
    count = audio.size // frame
    windows = audio[: count * frame].reshape(count, frame)
    rms = np.sqrt(np.mean(np.square(windows, dtype=np.float64), axis=1))
    return windows[int(np.argmax(rms))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--matched", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args(argv)
    original = args.original.resolve()
    matched = args.matched.resolve()
    model_path = args.model.resolve()
    threshold = args.threshold
    if threshold is None:
        sidecar = model_path.with_name(model_path.name + ".json")
        threshold = float(
            json.loads(sidecar.read_text(encoding="utf-8-sig"))[
                "recommended_stage2_threshold"
            ]
        )

    pipeline = LegacyInferencePipeline(model=ProxiMicModel(model_path))
    comparisons: list[dict[str, object]] = []
    for row in _rows(matched):
        relative = Path(row["path"])
        original_path = original / relative
        matched_path = matched / relative
        if not original_path.is_file() or not matched_path.is_file():
            continue
        audio_before, rate_before = read_pcm16_wav(original_path)
        audio_after, rate_after = read_pcm16_wav(matched_path)
        before = pipeline.infer_window(_highest_rms_second(audio_before, rate_before)).score
        after = pipeline.infer_window(_highest_rms_second(audio_after, rate_after)).score
        comparisons.append(
            {
                "path": relative.as_posix(),
                "class_name": row.get("class_name", ""),
                "score_before": before,
                "score_after": after,
                "delta": after - before,
                "activate_before": before > threshold,
                "activate_after": after > threshold,
            }
        )

    deltas = [float(row["delta"]) for row in comparisons]
    flips = [row for row in comparisons if row["activate_before"] != row["activate_after"]]
    print(f"Threshold     : {threshold:+.6f}")
    print(f"Files compared: {len(comparisons)}")
    print(
        "Score delta   : "
        f"median={statistics.median(deltas):+.6f}, "
        f"mean={statistics.fmean(deltas):+.6f}, "
        f"min={min(deltas):+.6f}, max={max(deltas):+.6f}"
    )
    print(f"Decision flips: {len(flips)}")
    for label in ("near", "far", "artifact"):
        group = [row for row in comparisons if row["class_name"] == label]
        if not group:
            continue
        before_rate = sum(bool(row["activate_before"]) for row in group) / len(group)
        after_rate = sum(bool(row["activate_after"]) for row in group) / len(group)
        print(
            f"  {label:8s}: n={len(group):2d}, "
            f"activate {before_rate:.1%} -> {after_rate:.1%}"
        )

    output = args.csv.resolve() if args.csv else matched / "gain_model_score_comparison.csv"
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(comparisons[0]))
        writer.writeheader()
        writer.writerows(comparisons)
    print(f"CSV           : {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
