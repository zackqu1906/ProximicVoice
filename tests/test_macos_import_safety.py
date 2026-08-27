from __future__ import annotations

import ast
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WIN32_ADAPTERS = (
    "src/proximic_ring/app_runtime.py",
    "src/proximic_ring/desktop_output.py",
    "src/proximic_ring/desktop_target.py",
    "src/proximic_ring/push_to_talk.py",
    "src/proximic_ring/voice_actions.py",
)


@pytest.mark.parametrize("relative_path", WIN32_ADAPTERS)
def test_wintypes_is_not_imported_unconditionally(relative_path: str) -> None:
    """macOS cannot even import ctypes.wintypes; keep it in an OS guard."""

    source_path = PROJECT_ROOT / relative_path
    module = ast.parse(source_path.read_text(encoding="utf-8"), source_path.name)
    unconditional = [
        node
        for node in module.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "ctypes"
        and any(alias.name == "wintypes" for alias in node.names)
    ]
    assert not unconditional
