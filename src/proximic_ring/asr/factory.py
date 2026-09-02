from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

from .base import ASRBackend
from .streaming import StreamingASRBackend


@dataclass(frozen=True)
class ASRBackendSettings:
    """Backend-neutral settings passed to backend factories."""

    model: str | None = None
    device: str = "cuda:0"
    language: str = "auto"
    options: dict[str, str] = field(default_factory=dict)
    status_callback: Callable[[str], None] | None = field(
        default=None, compare=False, repr=False
    )


def _normalize_name(name: str) -> str:
    value = name.strip().lower().replace("-", "_")
    if not value or not value.replace("_", "").isalnum():
        raise ValueError(f"Invalid ASR backend name: {name!r}")
    return value




def _backend_module(name: str):
    normalized = _normalize_name(name)
    module_name = f"{__package__}.backends.{normalized}"
    try:
        return normalized, importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            available = ", ".join(available_asr_backends()) or "(none)"
            raise ValueError(
                f"Unknown ASR backend {name!r}. Available backends: {available}"
            ) from exc
        raise


def asr_backend_kind(name: str) -> str:
    """Return ``batch`` or ``streaming`` for a backend adapter module."""

    _normalized, module = _backend_module(name)
    if getattr(module, "create_streaming_backend", None) is not None:
        return "streaming"
    if getattr(module, "create_backend", None) is not None:
        return "batch"
    raise RuntimeError(
        f"ASR backend module {module.__name__} exposes neither "
        "create_backend(settings) nor create_streaming_backend(settings)"
    )


def create_streaming_asr_backend(
    name: str, settings: ASRBackendSettings
) -> StreamingASRBackend:
    normalized, module = _backend_module(name)
    creator = getattr(module, "create_streaming_backend", None)
    if creator is None:
        raise ValueError(f"ASR backend {normalized!r} is not a streaming backend")
    return creator(settings)


def create_asr_backend(name: str, settings: ASRBackendSettings) -> ASRBackend:
    """Create a completed-utterance backend by module name."""

    normalized, module = _backend_module(name)
    creator = getattr(module, "create_backend", None)
    if creator is None:
        if getattr(module, "create_streaming_backend", None) is not None:
            raise ValueError(
                f"ASR backend {normalized!r} is streaming; use create_streaming_asr_backend"
            )
        raise RuntimeError(f"ASR backend module {module.__name__} has no create_backend(settings)")
    return creator(settings)


def available_asr_backends() -> list[str]:
    from . import backends

    names: list[str] = []
    for item in pkgutil.iter_modules(backends.__path__):
        if item.name.startswith("_"):
            continue
        names.append(item.name)
    return sorted(names)


def parse_backend_options(values: list[str] | None, selected: list[str]) -> dict[str, dict[str, str]]:
    """Parse ``--asr-option`` values.

    Accepted forms:
      * ``key=value`` when exactly one backend is selected
      * ``backend.key=value`` for explicit per-backend configuration
    """

    out = {name: {} for name in selected}
    for raw in values or []:
        if "=" not in raw:
            raise ValueError(f"Invalid --asr-option {raw!r}; expected key=value")
        key, value = raw.split("=", 1)
        key = key.strip()
        if "." in key:
            prefix, option_key = key.split(".", 1)
            prefix = _normalize_name(prefix)
            if prefix not in out:
                raise ValueError(f"--asr-option targets unselected backend {prefix!r}")
            out[prefix][option_key] = value
        else:
            if len(selected) != 1:
                raise ValueError(
                    f"Unqualified --asr-option {raw!r} is ambiguous with multiple ASR backends"
                )
            out[selected[0]][key] = value
    return out


def parse_model_overrides(values: list[str] | None, selected: list[str]) -> dict[str, str]:
    """Parse optional ``--asr-model`` overrides.

    ``--asr-model MODEL`` is convenient for one backend. With multiple
    backends, use ``--asr-model backend=MODEL`` for each override.
    """

    out: dict[str, str] = {}
    for raw in values or []:
        if "=" in raw:
            name, model = raw.split("=", 1)
            name = _normalize_name(name)
            if name not in selected:
                raise ValueError(f"--asr-model targets unselected backend {name!r}")
            out[name] = model
        else:
            if len(selected) != 1:
                raise ValueError(
                    f"Unqualified --asr-model {raw!r} is ambiguous with multiple ASR backends; "
                    "use backend=MODEL"
                )
            out[selected[0]] = raw
    return out
