from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import numpy as np

from ..factory import ASRBackendSettings


def _macos_cpu_chunk_size(device: str) -> int:
    """Decode less often on macOS CPU so CoreBluetooth keeps scheduling time."""

    normalized = str(device).strip().lower()
    return 8 if sys.platform == "darwin" and normalized == "cpu" else 4


def _limit_macos_cpu_inference_threads(device: str) -> None:
    """Keep local inference from saturating every macOS logical CPU.

    PyTorch inference runs away from the BLE thread, but both still compete for
    CPU and the Python runtime.  Leaving scheduling headroom is important for
    CoreBluetooth notification delivery during cumulative re-decodes.
    """

    if sys.platform != "darwin" or str(device).strip().lower() != "cpu":
        return
    try:
        import torch

        cpu_count = os.cpu_count() or 2
        inference_threads = max(1, min(4, cpu_count - 2))
        if int(torch.get_num_threads()) > inference_threads:
            torch.set_num_threads(inference_threads)
        try:
            interop_threads = max(1, min(2, inference_threads))
            if int(torch.get_num_interop_threads()) > interop_threads:
                torch.set_num_interop_threads(interop_threads)
        except RuntimeError:
            # PyTorch allows this setting only before inter-op work starts.
            pass
        print(
            "macOS SenseVoice CPU scheduling guard: "
            f"intra={int(torch.get_num_threads())}, "
            f"interop={int(torch.get_num_interop_threads())}, "
            f"system_cpus={cpu_count}"
        )
    except (ImportError, RuntimeError, TypeError, ValueError) as exc:
        print(f"macOS SenseVoice CPU scheduling guard unavailable: {exc}")


class StreamingSenseVoiceASR:
    """Adapter for pengzhendong/streaming-sensevoice.

    No code from that repository is copied into ProxiMic.  The adapter imports
    the external ``streaming_sensevoice.StreamingSenseVoice`` class and exposes
    it through ProxiMic's generic streaming-ASR contract.
    """

    backend_name = "streaming_sensevoice"
    sample_rate = 16_000

    def __init__(
        self,
        *,
        model: str = "iic/SenseVoiceSmall",
        device: str = "cuda:0",
        language: str = "zh",
        textnorm: bool = True,
        chunk_size: int = 4,
        padding: int = 8,
        beam_size: int = 1,
        contexts: list[str] | None = None,
        max_history: int = 0,
        repo_path: str | Path | None = None,
        final_redecode: bool = True,
    ) -> None:
        self.model_name = model
        self.device = device
        self.language = language
        self.final_redecode = bool(final_redecode)
        self.repo_path = Path(repo_path).expanduser().resolve() if repo_path else None

        _limit_macos_cpu_inference_threads(device)

        cls = self._load_external_class(self.repo_path)
        try:
            self._model = cls(
                chunk_size=int(chunk_size),
                padding=int(padding),
                beam_size=int(beam_size),
                contexts=contexts,
                language=language,
                textnorm=bool(textnorm),
                device=device,
                model=model,
                max_history=int(max_history),
            )
        except TypeError as exc:
            raise RuntimeError(
                "The installed streaming-sensevoice API is not compatible with this adapter. "
                "Use the current pengzhendong/streaming-sensevoice master branch or update the adapter."
            ) from exc
        self._last_text = ""

    @staticmethod
    def _load_external_class(repo_path: Path | None):
        if repo_path is not None:
            if not repo_path.is_dir():
                raise FileNotFoundError(f"streaming-sensevoice repo not found: {repo_path}")
            package_dir = repo_path / "streaming_sensevoice"
            if not package_dir.is_dir():
                raise FileNotFoundError(
                    f"Expected {package_dir}; --streaming-sensevoice-repo must point to the repository root"
                )
            sys.path.insert(0, str(repo_path))
            try:
                module = importlib.import_module("streaming_sensevoice")
            finally:
                try:
                    sys.path.remove(str(repo_path))
                except ValueError:
                    pass
        else:
            try:
                module = importlib.import_module("streaming_sensevoice")
            except ImportError as exc:
                raise RuntimeError(
                    "streaming_sensevoice is not importable. Clone pengzhendong/streaming-sensevoice "
                    "and pass --streaming-sensevoice-repo PATH (or add that repository to PYTHONPATH)."
                ) from exc

        cls = getattr(module, "StreamingSenseVoice", None)
        if cls is None:
            raise RuntimeError("streaming_sensevoice module has no StreamingSenseVoice class")
        return cls

    @staticmethod
    def _to_external_audio(audio_16k: np.ndarray) -> np.ndarray:
        x = np.asarray(audio_16k, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return x
        if not np.all(np.isfinite(x)):
            raise ValueError("ASR audio contains NaN or infinity")
        # Upstream realtime.py feeds float microphone samples scaled by 32768.
        return np.clip(x, -1.0, 1.0) * 32768.0

    def start(self) -> None:
        self._model.reset()
        self._last_text = ""

    def abort(self) -> None:
        self._model.reset()
        self._last_text = ""

    def _run(self, audio_16k: np.ndarray, *, is_last: bool) -> str | None:
        audio = self._to_external_audio(audio_16k)
        latest: str | None = None
        for result in self._model.streaming_inference(audio, is_last):
            text = str(result.get("text", "") or "").strip()
            if text:
                latest = text
        if latest is not None:
            self._last_text = latest
        return latest

    def feed(self, audio_16k: np.ndarray) -> str | None:
        if np.asarray(audio_16k).size == 0:
            return None
        return self._run(audio_16k, is_last=False)

    def finish(self, final_audio_16k: np.ndarray) -> str:
        final_audio = np.asarray(final_audio_16k, dtype=np.float32).reshape(-1)
        if final_audio.size == 0:
            self._model.reset()
            self._last_text = ""
            return ""

        if self.final_redecode:
            # Re-run the controller's trimmed final utterance from a clean state.
            # Partial text is provisional; this final pass corrects any reject
            # confirmation tail that may already have reached the live stream.
            self._model.reset()
            self._last_text = ""
            text = self._run(final_audio, is_last=True)
        else:
            # Upstream supports flushing with is_last=True.  An empty input may
            # internally add a zero frame; keep this option for lower latency.
            text = self._run(np.empty(0, dtype=np.float32), is_last=True)

        out = str(text or self._last_text or "")
        self._model.reset()
        self._last_text = ""
        return out


def _bool_option(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def create_streaming_backend(settings: ASRBackendSettings) -> StreamingSenseVoiceASR:
    o = settings.options
    contexts_raw = o.get("contexts", "").strip()
    contexts = [item.strip() for item in contexts_raw.split(",") if item.strip()] or None
    language = settings.language if settings.language != "auto" else "auto"
    return StreamingSenseVoiceASR(
        model=settings.model or "iic/SenseVoiceSmall",
        device=settings.device,
        language=language,
        textnorm=_bool_option(o.get("textnorm"), True),
        chunk_size=int(
            o.get("chunk_size", str(_macos_cpu_chunk_size(settings.device)))
        ),
        padding=int(o.get("padding", "8")),
        beam_size=int(o.get("beam_size", "1")),
        contexts=contexts,
        max_history=int(o.get("max_history", "0")),
        repo_path=o.get("repo"),
        final_redecode=_bool_option(o.get("final_redecode"), True),
    )
