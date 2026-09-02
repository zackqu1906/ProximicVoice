from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Callable

from ..factory import ASRBackendSettings

import numpy as np

_TAG_RE = re.compile(r"<\|.*?\|>")


class SenseVoiceASR:
    backend_name = "sensevoice"
    """Small adapter from 16 kHz float32 NumPy audio to SenseVoice text.

    This adapter deliberately accepts the *same* 16 kHz waveform consumed by
    ProxiMic before ProxiMic internally downsamples to 8 kHz.  Nothing in the
    proximity model or feature pipeline is changed.

    If ``repo_path`` is supplied, the local SenseVoice ``model.py`` is loaded
    directly (matching the repository's api.py path).  Otherwise FunASR's
    built-in SenseVoice integration is used.
    """

    sample_rate = 16_000

    def __init__(
        self,
        *,
        model: str = "iic/SenseVoiceSmall",
        device: str = "cuda:0",
        language: str = "auto",
        use_itn: bool = True,
        repo_path: str | Path | None = None,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.model_name = model
        self.device = device
        self.language = language
        self.use_itn = bool(use_itn)
        self.repo_path = Path(repo_path).resolve() if repo_path is not None else None
        self._status_callback = status_callback

        try:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
        except ImportError as exc:
            raise RuntimeError(
                "SenseVoice ASR requires FunASR. Install the ASR extras first."
            ) from exc
        self._postprocess = rich_transcription_postprocess

        if self.repo_path is not None:
            self._mode = "local"
            self._model, self._kwargs = self._load_local_model(self.repo_path)
        else:
            self._mode = "automodel"
            self._model, self._kwargs = self._load_automodel()
        if self._status_callback is not None:
            destination = "显存" if str(device).lower().startswith("cuda") else "内存"
            self._status_callback(f"ASR 模型参数已载入{destination}：SenseVoiceSmall")

    def _load_local_model(self, repo_path: Path) -> tuple[Any, dict[str, Any]]:
        if self._status_callback is not None:
            if Path(self.model_name).expanduser().is_dir():
                self._status_callback("正在读取本地 ASR 模型参数：SenseVoiceSmall…")
            else:
                self._status_callback(
                    "正在检查并下载 ASR 模型参数：SenseVoiceSmall（已有磁盘缓存将直接复用）…"
                )
        model_py = repo_path / "model.py"
        if not model_py.is_file():
            raise FileNotFoundError(f"SenseVoice model.py not found: {model_py}")

        # SenseVoice model.py imports its sibling ``utils`` package.  Put only
        # this repository root at the front while loading it.
        sys.path.insert(0, str(repo_path))
        try:
            spec = importlib.util.spec_from_file_location(
                "_proximic_external_sensevoice_model", model_py
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Could not import SenseVoice model from {model_py}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls = module.SenseVoiceSmall
            model, kwargs = cls.from_pretrained(model=self.model_name, device=self.device)
            model.eval()
            return model, kwargs
        finally:
            try:
                sys.path.remove(str(repo_path))
            except ValueError:
                pass

    def _load_automodel(self) -> tuple[Any, dict[str, Any]]:
        if self._status_callback is not None:
            self._status_callback(
                "正在检查并下载 ASR 模型参数：SenseVoiceSmall（已有磁盘缓存将直接复用）…"
            )
        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise RuntimeError(
                "SenseVoice ASR requires FunASR. Install the ASR extras first."
            ) from exc

        # FunASR already contains an integrated SenseVoice implementation.  We
        # intentionally do not attach a second VAD model here: ProxiMic gates
        # which utterances are allowed into ASR, and the controller handles the
        # endpoint.  This keeps latency and duplicate segmentation down.
        model = AutoModel(
            model=self.model_name,
            trust_remote_code=False,
            device=self.device,
        )
        return model, {}

    def transcribe(self, audio_16k: np.ndarray) -> str:
        x = np.asarray(audio_16k, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return ""
        if not np.all(np.isfinite(x)):
            raise ValueError("ASR audio contains NaN or infinity")

        if self._mode == "local":
            # This follows SenseVoice's own api.py: inference(data_in=<audio>, fs=16000).
            try:
                import torch
            except ImportError as exc:
                raise RuntimeError("SenseVoice requires PyTorch") from exc
            res = self._model.inference(
                data_in=[torch.from_numpy(np.ascontiguousarray(x))],
                language=self.language,
                use_itn=self.use_itn,
                ban_emo_unk=False,
                key=["live"],
                fs=self.sample_rate,
                **self._kwargs,
            )
            if not res or not res[0]:
                return ""
            raw = str(res[0][0].get("text", ""))
        else:
            # AutoModel supports raw waveform input.  Explicit fs keeps the
            # contract identical to the Ring/ProxiMic 16 kHz source.
            res = self._model.generate(
                input=x,
                cache={},
                language=self.language,
                use_itn=self.use_itn,
                fs=self.sample_rate,
            )
            if not res:
                return ""
            raw = str(res[0].get("text", ""))

        text = self._postprocess(raw)
        # rich_transcription_postprocess normally removes/expands the rich tags;
        # this fallback keeps CLI output clean if a version leaves any tags.
        return _TAG_RE.sub("", text).strip()


def create_backend(settings: ASRBackendSettings) -> SenseVoiceASR:
    repo_path = settings.options.get("repo")
    use_itn = settings.options.get("use_itn", "true").strip().lower() not in {"0", "false", "no", "off"}
    return SenseVoiceASR(
        model=settings.model or "iic/SenseVoiceSmall",
        device=settings.device,
        language=settings.language,
        use_itn=use_itn,
        repo_path=repo_path,
        status_callback=settings.status_callback,
    )
