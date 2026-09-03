"""Compatibility helpers for the deliberately pruned FunASR runtime."""

from __future__ import annotations

import sys
import os
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType


def prepare_funasr_runtime() -> None:
    """Satisfy FunASR's unused librosa import in a frozen application.

    ``funasr.utils.load_utils`` imports librosa unconditionally, although the
    audio paths used by Proximic Voice use torch, torchaudio and NumPy only.
    Keeping librosa would also retain numba, llvmlite, scipy and scikit-learn.
    Source installations keep using a real librosa when one is available; a
    frozen build uses this empty module because those APIs are never called.
    """

    if not getattr(sys, "frozen", False):
        return
    if "librosa" not in sys.modules:
        placeholder = ModuleType("librosa")
        placeholder.__doc__ = "Unused FunASR compatibility placeholder."
        placeholder.__spec__ = ModuleSpec("librosa", loader=None)
        sys.modules["librosa"] = placeholder
    # Transformers imports soxr when it detects the FunASR librosa placeholder.
    # Qwen3 text generation never calls either audio loading API.
    if "soxr" not in sys.modules:
        soxr_placeholder = ModuleType("soxr")
        soxr_placeholder.__doc__ = "Unused Transformers audio placeholder."
        soxr_placeholder.__spec__ = ModuleSpec("soxr", loader=None)
        sys.modules["soxr"] = soxr_placeholder
    _install_modelscope_download_shim()


def _install_modelscope_download_shim() -> None:
    """Expose the three ModelScope names FunASR uses without its model SDK.

    Current ModelScope delegates downloads to the much smaller
    ``modelscope_hub`` package.  Importing the legacy top-level SDK makes
    PyInstaller follow type-checking imports for thousands of unrelated CV,
    NLP, training and export modules, so the frozen app provides this narrow
    compatibility surface instead.
    """

    if "modelscope.hub.snapshot_download" in sys.modules:
        return

    from modelscope_hub.compat.snapshot_download import (
        snapshot_download as hub_snapshot_download,
    )

    def snapshot_download(
        model_id: str,
        revision: str | None = None,
        user_agent: object | None = None,
        **kwargs: object,
    ) -> str:
        del user_agent  # The lightweight hub supplies its own user agent.
        revision_name = str(revision or "master")
        cache_root = Path(
            os.environ.get("MODELSCOPE_CACHE", Path.home() / ".cache/modelscope")
        ).expanduser()
        # Reuse the legacy cache written by earlier Proximic Voice releases.
        cached_snapshot = (
            cache_root
            / "models"
            / model_id.replace("/", "--")
            / "snapshots"
            / revision_name
        )
        if cached_snapshot.is_dir():
            return str(cached_snapshot)
        return str(
            hub_snapshot_download(
                model_id=model_id,
                revision=revision,
                cache_dir=str(cache_root),
                **kwargs,
            )
        )

    class Invoke:
        KEY = "invoke"
        PIPELINE = "pipeline"
        LOCAL_TRAINER = "local_trainer"

    class ThirdParty:
        KEY = "third_party"

    packages = {
        "modelscope": ModuleType("modelscope"),
        "modelscope.hub": ModuleType("modelscope.hub"),
        "modelscope.utils": ModuleType("modelscope.utils"),
    }
    for name, module in packages.items():
        module.__path__ = []  # type: ignore[attr-defined]
        sys.modules.setdefault(name, module)

    snapshot_module = ModuleType("modelscope.hub.snapshot_download")
    snapshot_module.snapshot_download = snapshot_download  # type: ignore[attr-defined]
    sys.modules["modelscope.hub.snapshot_download"] = snapshot_module

    check_module = ModuleType("modelscope.hub.check_model")
    check_module.check_local_model_is_latest = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    sys.modules["modelscope.hub.check_model"] = check_module

    constant_module = ModuleType("modelscope.utils.constant")
    constant_module.Invoke = Invoke  # type: ignore[attr-defined]
    constant_module.ThirdParty = ThirdParty  # type: ignore[attr-defined]
    sys.modules["modelscope.utils.constant"] = constant_module
