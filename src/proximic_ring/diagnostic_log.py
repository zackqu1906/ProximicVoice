"""Small, failure-isolated rotating logs for local diagnostics."""

from __future__ import annotations

from pathlib import Path
import threading


DIAGNOSTIC_LOG_MAX_BYTES = 2 * 1024 * 1024
DIAGNOSTIC_LOG_BACKUP_COUNT = 3
STARTUP_LOG_MAX_BYTES = 2 * 1024 * 1024
STARTUP_LOG_BACKUP_COUNT = 2


def rotate_existing_log(
    path: Path,
    *,
    max_bytes: int,
    backup_count: int,
    incoming_bytes: int = 0,
) -> bool:
    """Rotate ``path`` when the next write would exceed its byte budget.

    Rotation is deliberately best-effort. Diagnostics must never prevent the
    application from starting or accepting voice input.
    """

    log_path = Path(path)
    try:
        limit = max(1, int(max_bytes))
        backups = max(0, int(backup_count))
        current_size = log_path.stat().st_size if log_path.exists() else 0
        if (
            current_size <= 0
            or current_size + max(0, int(incoming_bytes)) <= limit
        ):
            return False
        if backups <= 0:
            log_path.unlink(missing_ok=True)
            return True
        oldest = log_path.with_name(f"{log_path.name}.{backups}")
        oldest.unlink(missing_ok=True)
        for index in range(backups - 1, 0, -1):
            source = log_path.with_name(f"{log_path.name}.{index}")
            if source.exists():
                source.replace(log_path.with_name(f"{log_path.name}.{index + 1}"))
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))
        return True
    except OSError:
        return False


class RotatingDiagnosticLog:
    """Append UTF-8 lines to a bounded local file without surfacing I/O errors."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DIAGNOSTIC_LOG_MAX_BYTES,
        backup_count: int = DIAGNOSTIC_LOG_BACKUP_COUNT,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max(1, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self._lock = threading.RLock()

    def append(self, line: str) -> bool:
        payload = (str(line).rstrip("\r\n") + "\n").encode(
            "utf-8", errors="replace"
        )
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                rotate_existing_log(
                    self.path,
                    max_bytes=self.max_bytes,
                    backup_count=self.backup_count,
                    incoming_bytes=len(payload),
                )
                with self.path.open("ab") as handle:
                    handle.write(payload)
                return True
            except OSError:
                return False
