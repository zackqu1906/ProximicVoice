from __future__ import annotations

import gc
import sys
import threading
from typing import Any

from .factory import ASRBackendSettings


class ASRBackendCache:
    """Keep one heavyweight ASR model resident for the desktop app session.

    Device sessions, worker threads and UI callbacks are deliberately not
    cached.  A changed backend/model/device/language/options identity replaces
    the cached object so incompatible settings are never silently reused.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[object, ...] | None = None
        self._backend: Any | None = None

    @staticmethod
    def key(
        name: str,
        kind: str,
        settings: ASRBackendSettings,
    ) -> tuple[object, ...]:
        return (
            str(name).strip().lower().replace("-", "_"),
            str(kind),
            settings.model,
            settings.device,
            settings.language,
            tuple(sorted((str(k), str(v)) for k, v in settings.options.items())),
        )

    def get(
        self,
        name: str,
        kind: str,
        settings: ASRBackendSettings,
    ) -> Any | None:
        key = self.key(name, kind, settings)
        with self._lock:
            if self._key == key:
                return self._backend
            old = self._backend
            self._key = None
            self._backend = None
        # Release the previous heavyweight model before constructing a model
        # for incompatible settings, avoiding a temporary double allocation.
        if old is not None:
            del old
            self._release_unused_memory()
        return None

    def put(
        self,
        name: str,
        kind: str,
        settings: ASRBackendSettings,
        backend: Any,
    ) -> None:
        key = self.key(name, kind, settings)
        with self._lock:
            old = self._backend if self._key != key else None
            self._key = key
            self._backend = backend
        if old is not None:
            del old
            self._release_unused_memory()

    def clear(self) -> None:
        with self._lock:
            old = self._backend
            self._key = None
            self._backend = None
        if old is not None:
            del old
            self._release_unused_memory()

    @staticmethod
    def _release_unused_memory() -> None:
        gc.collect()
        torch = sys.modules.get("torch")
        cuda = getattr(torch, "cuda", None) if torch is not None else None
        empty_cache = getattr(cuda, "empty_cache", None)
        if callable(empty_cache):
            try:
                empty_cache()
            except BaseException:
                pass
