from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

from ..factory import ASRBackendSettings


DEFAULT_MODEL = "FunAudioLLM/Fun-ASR-Nano-2512"
LOCAL_MODEL_NAME = "Fun-ASR-Nano-2512"


class FunASRNanoStreamingASR:
    """Cumulative pseudo-streaming adapter for Fun-ASR-Nano-2512.

    Fun-ASR-Nano's regular PyTorch API decodes a complete waveform rather than
    consuming cache-aware audio packets. This adapter follows the repository's
    streaming demo: it re-decodes the accumulated utterance at a fixed interval,
    carries stable text forward as context, and rolls back a few unstable tail
    tokens before publishing a partial result.
    """

    backend_name = "funasr_nano"
    sample_rate = 16_000

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        device: str = "cuda:0",
        language: str = "auto",
        repo_path: str | Path,
        chunk_ms: int = 720,
        rollback_tokens: int = 5,
        use_itn: bool = True,
        hotwords: list[str] | None = None,
        final_redecode: bool = True,
    ) -> None:
        if chunk_ms <= 0:
            raise ValueError("Fun-ASR-Nano chunk_ms must be positive")
        if rollback_tokens < 0:
            raise ValueError("Fun-ASR-Nano rollback_tokens cannot be negative")

        self.repo_path = Path(repo_path).expanduser().resolve()
        model_py = self.repo_path / "model.py"
        if not model_py.is_file():
            raise FileNotFoundError(
                f"Fun-ASR-Nano model.py not found: {model_py}; "
                "repo must point to the Fun-ASR repository root"
            )

        model_path = Path(model).expanduser()
        if model_path.is_dir():
            resolved_model = str(model_path.resolve())
            self.model_name = model_path.name
        else:
            resolved_model = str(model)
            self.model_name = str(model)

        self.device = str(device)
        self.language = self._language_prompt(language)
        self.chunk_samples = int(round(chunk_ms * self.sample_rate / 1000.0))
        self.rollback_tokens = int(rollback_tokens)
        self.use_itn = bool(use_itn)
        self.hotwords = list(hotwords or [])
        self.final_redecode = bool(final_redecode)

        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("Fun-ASR-Nano requires PyTorch") from exc
        self._torch = torch

        # Local cumulative decoding is intentionally kept below full CPU
        # saturation.  BLE notification delivery and the customer UI must keep
        # receiving scheduling time even when a long partial is being decoded.
        try:
            if torch.device(self.device).type == "cpu":
                cpu_count = os.cpu_count() or 2
                inference_threads = max(1, min(4, cpu_count // 2))
                if int(torch.get_num_threads()) > inference_threads:
                    torch.set_num_threads(inference_threads)
                print(
                    "Fun-ASR CPU inference threads: "
                    f"{int(torch.get_num_threads())} (system CPUs: {cpu_count})"
                )
        except (RuntimeError, TypeError, ValueError):
            # Thread-pool configuration is a responsiveness optimization; it
            # must not make an otherwise usable backend fail to load.
            pass

        cls = self._load_external_class(model_py)
        # FunASR may inspect configuration.json and try to import the checkpoint's
        # remote_code entry ("model") while building the already-registered class.
        # Keep the checkout importable for that build step as the official demo does.
        sys.path.insert(0, str(self.repo_path))
        try:
            try:
                self._model, self._model_kwargs = cls.from_pretrained(
                    model=resolved_model,
                    device=self.device,
                )
            except ImportError as exc:
                raise RuntimeError(
                    "Fun-ASR-Nano dependencies are incomplete. Install the Fun-ASR "
                    "repository requirements in the active Python environment."
                ) from exc
        finally:
            try:
                sys.path.remove(str(self.repo_path))
            except ValueError:
                pass
        self._model.eval()
        self._tokenizer = self._model_kwargs.get("tokenizer")
        if self._tokenizer is None:
            raise RuntimeError("Fun-ASR-Nano did not provide a tokenizer")

        self._parts: list[np.ndarray] = []
        self._samples = 0
        self._last_decode_samples = 0
        self._previous_context = ""
        self._last_text = ""

    def _load_external_class(self, model_py: Path):
        # model.py imports its sibling tools package, so the repository root
        # must be first on sys.path while the external implementation loads.
        module_name = "_proximic_external_funasr_nano_model"
        sys.path.insert(0, str(self.repo_path))
        try:
            spec = importlib.util.spec_from_file_location(module_name, model_py)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Could not import Fun-ASR-Nano model from {model_py}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        finally:
            try:
                sys.path.remove(str(self.repo_path))
            except ValueError:
                pass

        cls = getattr(module, "FunASRNano", None)
        if cls is None:
            raise RuntimeError(f"{model_py} has no FunASRNano class")
        return cls

    @staticmethod
    def _language_prompt(language: str) -> str | None:
        value = str(language or "auto").strip().lower()
        if value == "auto":
            return None
        return {
            "zh": "中文",
            "yue": "中文",
            "en": "英文",
            "ja": "日文",
            "ko": "韩文",
        }.get(value, language)

    def _reset(self) -> None:
        self._parts = []
        self._samples = 0
        self._last_decode_samples = 0
        self._previous_context = ""
        self._last_text = ""

    def start(self) -> None:
        self._reset()

    def abort(self) -> None:
        self._reset()

    @staticmethod
    def _validate_audio(audio_16k: np.ndarray) -> np.ndarray:
        x = np.asarray(audio_16k, dtype=np.float32).reshape(-1)
        if x.size and not np.all(np.isfinite(x)):
            raise ValueError("ASR audio contains NaN or infinity")
        return np.ascontiguousarray(x)

    def _decode(self, audio: np.ndarray, *, previous_context: str) -> str:
        tensor = self._torch.from_numpy(np.ascontiguousarray(audio))
        kwargs: dict[str, Any] = dict(self._model_kwargs)
        kwargs.update(
            prev_text=previous_context,
            language=self.language,
            itn=self.use_itn,
            hotwords=self.hotwords,
        )
        with self._torch.inference_mode():
            result = self._model.inference([tensor], **kwargs)
        if not result or not result[0]:
            return ""
        text = str(result[0][0].get("text", "") or "").strip()
        return self._merge_context_boundary(previous_context, text)

    @staticmethod
    def _merge_context_boundary(previous_context: str, text: str) -> str:
        """Remove overlap where the generated continuation repeats context tail.

        The upstream model prepends ``prev_text`` to its decoded continuation.
        The continuation can begin by revising the last one or two characters,
        producing partials such as ``心心情``. Keep the longest exact suffix /
        prefix overlap once, without touching repetitions spoken in the audio.
        """

        if not previous_context or not text.startswith(previous_context):
            return text
        continuation = text[len(previous_context) :]
        max_overlap = min(len(previous_context), len(continuation))
        for size in range(max_overlap, 0, -1):
            if previous_context[-size:] == continuation[:size]:
                return previous_context + continuation[size:]
        return text

    def _stable_partial(self, text: str) -> str:
        if not text or self.rollback_tokens <= 0:
            return text
        token_ids = self._tokenizer.encode(text)
        if len(token_ids) <= self.rollback_tokens:
            return ""
        return (
            self._tokenizer.decode(token_ids[: -self.rollback_tokens])
            .replace("\ufffd", "")
            .strip()
        )

    def feed(self, audio_16k: np.ndarray) -> str | None:
        x = self._validate_audio(audio_16k)
        if x.size == 0:
            return None
        self._parts.append(x.copy())
        self._samples += int(x.size)
        if self._samples - self._last_decode_samples < self.chunk_samples:
            return None

        whole = np.concatenate(self._parts).astype(np.float32, copy=False)
        raw = self._decode(whole, previous_context=self._previous_context)
        self._last_decode_samples = self._samples
        stable = self._stable_partial(raw)
        self._previous_context = stable
        if not stable:
            return None
        if stable == self._last_text:
            return None
        self._last_text = stable
        return stable

    def finish(self, final_audio_16k: np.ndarray) -> str:
        final_audio = self._validate_audio(final_audio_16k)
        if final_audio.size == 0:
            self._reset()
            return ""

        if self.final_redecode:
            text = self._decode(final_audio, previous_context="")
        elif not self._last_text:
            # The controller supplies the complete, trimmed utterance. Decode
            # it once if no partial has yet crossed the configured interval.
            text = self._decode(final_audio, previous_context="")
        else:
            text = self._last_text
        out = str(text or self._last_text or "").strip()
        self._reset()
        return out


def _bool_option(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _resolve_model(model: str | None, repo_path: Path) -> str:
    if model:
        return model
    local = repo_path / "pretrained_models" / LOCAL_MODEL_NAME
    return str(local) if local.is_dir() else DEFAULT_MODEL


def create_streaming_backend(settings: ASRBackendSettings) -> FunASRNanoStreamingASR:
    options = settings.options
    repo_raw = options.get("repo")
    if not repo_raw:
        raise ValueError(
            "Fun-ASR-Nano requires its source repository. Pass "
            "--funasr-nano-repo PATH or --asr-option repo=PATH."
        )
    repo_path = Path(repo_raw).expanduser().resolve()
    hotwords = [item.strip() for item in options.get("hotwords", "").split(",") if item.strip()]
    return FunASRNanoStreamingASR(
        model=_resolve_model(settings.model, repo_path),
        device=settings.device,
        language=settings.language,
        repo_path=repo_path,
        chunk_ms=int(options.get("chunk_ms", "720")),
        rollback_tokens=int(options.get("rollback_tokens", "5")),
        use_itn=_bool_option(options.get("use_itn"), True),
        hotwords=hotwords,
        final_redecode=_bool_option(options.get("final_redecode"), True),
    )
