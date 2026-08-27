"""Public API with lazy imports so desktop startup can initialize Qt first."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "DetectorConfig",
    "ProxiMicDetector",
    "Stage1Event",
    "Stage2Event",
    "CnnNet8",
    "ProxiMicModel",
    "LegacyInferencePipeline",
]

_EXPORTS = {
    "DetectorConfig": (".config", "DetectorConfig"),
    "ProxiMicDetector": (".detector", "ProxiMicDetector"),
    "Stage1Event": (".events", "Stage1Event"),
    "Stage2Event": (".events", "Stage2Event"),
    "CnnNet8": (".model", "CnnNet8"),
    "ProxiMicModel": (".model", "ProxiMicModel"),
    "LegacyInferencePipeline": (".pipeline", "LegacyInferencePipeline"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
