"""Resolve capture output paths under a configurable data root."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

# None → Path.cwd() / "data" at call time.
_DATA_DIR: Path | None = None

MODE_AUDIO = "audio"
MODE_IMU = "imu"
MODE_COMBO = "combo"
MODE_PPG = "ppg"
MODE_SWIPE = "swipe"
MODE_BUTTON = "button"
MODE_SESSION = "session"


def set_data_dir(path: Path | str) -> None:
    """Set the root directory for session captures (CSV/WAV/…)."""
    global _DATA_DIR
    _DATA_DIR = Path(path).expanduser().resolve()


def get_data_dir() -> Path:
    if _DATA_DIR is None:
        return Path.cwd() / "data"
    return _DATA_DIR


def receiver_data_dir() -> Path:
    """Alias for get_data_dir() (legacy name)."""
    return get_data_dir()


def _basename_only(path: str | Path) -> str:
    return Path(path).name


def new_session_dir(mode: str, when: datetime | None = None) -> Path:
    """Create and return {data_dir}/{mode}/YYYYMMDD_HHMMSS/."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    session = get_data_dir() / mode / stamp
    session.mkdir(parents=True, exist_ok=True)
    return session


def resolve_capture_path(
    mode: str,
    user_output: str | None,
    default_filename: str,
    *,
    session_dir: Path | None = None,
) -> Path:
    """
    Place file under {data_dir}/{mode}/<session>/<filename>.

    user_output may be a stem (ring_combo) or filename (ring_combo.wav); only the
    basename is used. Extension comes from default_filename if user_output has none.
    """
    session = session_dir or new_session_dir(mode)
    default_path = Path(default_filename)
    user = Path(user_output) if user_output else default_path

    if user.suffix:
        name = user.name
    else:
        name = user.name + default_path.suffix

    return (session / name).resolve()


def resolve_combo_capture_paths(
    args: argparse.Namespace,
    *,
    default_audio: str = "ring_combo.wav",
    default_imu_csv: str = "ring_combo_imu.csv",
) -> tuple[Path, Path, Path | None, Path]:
    """Return (audio_wav, imu_csv, imu_npy|None, session_dir)."""
    session = new_session_dir(MODE_COMBO)

    raw = args.output or default_audio.replace(".wav", "")
    stem = Path(_basename_only(raw)).stem

    audio_path = resolve_capture_path(
        MODE_COMBO, f"{stem}.wav", default_audio, session_dir=session
    )
    csv_path = resolve_capture_path(
        MODE_COMBO, f"{stem}_imu.csv", default_imu_csv, session_dir=session
    )

    npy_path: Path | None = None
    if args.imu_npy is not None:
        if args.imu_npy:
            npy_path = resolve_capture_path(
                MODE_COMBO, _basename_only(args.imu_npy), "ring_combo_imu.npy",
                session_dir=session,
            )
        else:
            npy_path = csv_path.with_suffix(".npy")

    return audio_path, csv_path, npy_path, session


def resolve_imu_capture_paths(
    args: argparse.Namespace,
    *,
    default_csv: str = "ring_imu.csv",
) -> tuple[Path | None, Path | None, Path]:
    """Return (csv_path, npy_path, session_dir). At least one output path is set."""
    session = new_session_dir(MODE_IMU)
    output = args.output or default_csv
    user = Path(_basename_only(output))
    suffix = user.suffix.lower()

    csv_path: Path | None = None
    npy_path: Path | None = None

    if suffix == ".npy":
        npy_path = resolve_capture_path(MODE_IMU, user.name, default_csv, session_dir=session)
        npy_path = npy_path if npy_path.suffix == ".npy" else npy_path.with_suffix(".npy")
    elif suffix == ".npz":
        npy_path = resolve_capture_path(MODE_IMU, user.name, "ring_imu.npz", session_dir=session)
    elif suffix == ".csv" or suffix == "":
        csv_name = user.name if suffix == ".csv" else f"{user.name}.csv"
        csv_path = session / csv_name
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path = csv_path.resolve()
    else:
        csv_path = resolve_capture_path(MODE_IMU, user.name, default_csv, session_dir=session)

    if args.imu_npy is not None:
        if args.imu_npy:
            npy_path = resolve_capture_path(
                MODE_IMU, _basename_only(args.imu_npy), "ring_imu.npy", session_dir=session
            )
        elif csv_path is not None:
            npy_path = csv_path.with_suffix(".npy")
        else:
            npy_path = resolve_capture_path(
                MODE_IMU, "ring_imu.npy", "ring_imu.npy", session_dir=session
            )

    if csv_path is None and npy_path is None:
        csv_path = resolve_capture_path(MODE_IMU, default_csv, default_csv, session_dir=session)

    return csv_path, npy_path, session


def find_data_file(path: str | Path, mode: str | None = None) -> Path:
    """
    Resolve a user path for reading: as-is, then data/{mode}/, then latest session.
    """
    candidate = Path(path).expanduser()
    if candidate.is_file():
        return candidate.resolve()

    name = candidate.name
    modes = [mode] if mode else [MODE_IMU, MODE_COMBO, MODE_AUDIO]
    data_dir = get_data_dir()

    for m in modes:
        flat = data_dir / m / name
        if flat.is_file():
            return flat.resolve()

        mode_dir = data_dir / m
        if not mode_dir.is_dir():
            continue
        sessions = sorted(
            (p for p in mode_dir.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        for session in sessions:
            hit = session / name
            if hit.is_file():
                return hit.resolve()

    return candidate.resolve()
