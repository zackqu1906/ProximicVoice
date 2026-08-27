from __future__ import annotations

import importlib.util
from pathlib import Path


def test_entrypoint_diverts_multiprocessing_before_starting_ui(monkeypatch) -> None:
    launcher_path = Path(__file__).parents[1] / "packaging" / "launcher.py"
    spec = importlib.util.spec_from_file_location(
        "proximic_packaging_launcher", launcher_path
    )
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    calls: list[str] = []
    monkeypatch.setattr(
        launcher.multiprocessing,
        "freeze_support",
        lambda: calls.append("freeze_support"),
    )
    monkeypatch.setattr(launcher, "run", lambda: calls.append("run") or 0)

    assert launcher._entrypoint() == 0
    assert calls == ["freeze_support", "run"]
