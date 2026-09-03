from __future__ import annotations

import importlib
from pathlib import Path
import sys


def test_frozen_funasr_compat_reuses_legacy_modelscope_cache(
    monkeypatch, tmp_path: Path
):
    module_names = (
        "librosa",
        "soxr",
        "modelscope",
        "modelscope.hub",
        "modelscope.hub.check_model",
        "modelscope.hub.snapshot_download",
        "modelscope.utils",
        "modelscope.utils.constant",
    )
    for name in module_names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("MODELSCOPE_CACHE", str(tmp_path))

    cached = tmp_path / "models/iic--SenseVoiceSmall/snapshots/master"
    cached.mkdir(parents=True)

    from proximic_ring.asr.funasr_compat import prepare_funasr_runtime

    prepare_funasr_runtime()
    shim = importlib.import_module("modelscope.hub.snapshot_download")

    assert shim.snapshot_download("iic/SenseVoiceSmall", revision="master") == str(
        cached
    )
    assert sys.modules["librosa"].__spec__ is not None
    assert sys.modules["soxr"].__spec__ is not None
