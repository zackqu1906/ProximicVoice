from __future__ import annotations

import numpy as np

from ..factory import ASRBackendSettings


class WhisperASR:
    """Local faster-whisper adapter using the same completed 16 kHz utterance."""

    backend_name = "whisper"
    sample_rate = 16_000

    def __init__(
        self,
        *,
        model: str = "small",
        device: str = "cuda:0",
        language: str = "auto",
        compute_type: str | None = None,
        beam_size: int = 5,
    ) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                'Whisper backend requires faster-whisper. Install: pip install -e ".[asr-whisper]"'
            ) from exc

        self.model_name = model
        self.language = language
        self.beam_size = int(beam_size)

        fw_device = "cuda" if str(device).lower().startswith("cuda") else "cpu"
        device_index = 0
        if fw_device == "cuda" and ":" in str(device):
            device_index = int(str(device).split(":", 1)[1])
        if compute_type is None:
            compute_type = "float16" if fw_device == "cuda" else "int8"

        self._model = WhisperModel(
            model,
            device=fw_device,
            device_index=device_index,
            compute_type=compute_type,
        )

    def transcribe(self, audio_16k: np.ndarray) -> str:
        x = np.asarray(audio_16k, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return ""
        segments, _info = self._model.transcribe(
            x,
            language=None if self.language == "auto" else self.language,
            beam_size=self.beam_size,
            vad_filter=False,
        )
        return "".join(seg.text for seg in segments).strip()


def create_backend(settings: ASRBackendSettings) -> WhisperASR:
    compute_type = settings.options.get("compute_type")
    beam_size = int(settings.options.get("beam_size", "5"))
    return WhisperASR(
        model=settings.model or "small",
        device=settings.device,
        language=settings.language,
        compute_type=compute_type,
        beam_size=beam_size,
    )
