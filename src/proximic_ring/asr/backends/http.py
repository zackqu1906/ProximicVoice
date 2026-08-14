from __future__ import annotations

import io
import os
import wave
from typing import Any

import numpy as np

from ..factory import ASRBackendSettings


def _wav_bytes(audio: np.ndarray, sample_rate: int = 16_000) -> bytes:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    x = np.clip(x, -1.0, 1.0)
    pcm = (x * 32767.0).astype("<i2", copy=False).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _extract_json_field(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


class HttpMultipartASR:
    """Generic cloud ASR adapter for multipart WAV endpoints.

    This intentionally knows nothing about ProxiMic. API-specific differences
    are isolated behind options; if a service uses a different protocol, add a
    new backend module rather than editing detector/session code.
    """

    backend_name = "http"
    sample_rate = 16_000

    def __init__(
        self,
        *,
        url: str,
        model: str = "remote",
        language: str = "auto",
        api_key_env: str | None = None,
        api_key_header: str = "Authorization",
        api_key_prefix: str = "Bearer ",
        file_field: str = "file",
        text_field: str = "text",
        timeout_s: float = 30.0,
    ) -> None:
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError(
                'HTTP ASR backend requires requests. Install: pip install -e ".[asr-http]"'
            ) from exc

        if not url:
            raise ValueError("HTTP ASR backend requires --asr-option url=https://...")
        self._requests = requests
        self.url = url
        self.model_name = model
        self.language = language
        self.file_field = file_field
        self.text_field = text_field
        self.timeout_s = float(timeout_s)
        self.headers: dict[str, str] = {}
        if api_key_env:
            key = os.environ.get(api_key_env)
            if not key:
                raise RuntimeError(f"Environment variable {api_key_env!r} is not set")
            self.headers[api_key_header] = f"{api_key_prefix}{key}"

    def transcribe(self, audio_16k: np.ndarray) -> str:
        files = {self.file_field: ("utterance.wav", _wav_bytes(audio_16k), "audio/wav")}
        data: dict[str, str] = {}
        if self.model_name and self.model_name != "remote":
            data["model"] = self.model_name
        if self.language != "auto":
            data["language"] = self.language

        response = self._requests.post(
            self.url,
            headers=self.headers,
            files=files,
            data=data,
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        return str(_extract_json_field(payload, self.text_field)).strip()


def create_backend(settings: ASRBackendSettings) -> HttpMultipartASR:
    o = settings.options
    return HttpMultipartASR(
        url=o.get("url", ""),
        model=settings.model or o.get("model", "remote"),
        language=settings.language,
        api_key_env=o.get("api_key_env"),
        api_key_header=o.get("api_key_header", "Authorization"),
        api_key_prefix=o.get("api_key_prefix", "Bearer "),
        file_field=o.get("file_field", "file"),
        text_field=o.get("text_field", "text"),
        timeout_s=float(o.get("timeout_s", "30")),
    )
