"""Backward-compatible import path for the SenseVoice backend."""

from .backends.sensevoice import SenseVoiceASR, create_backend

__all__ = ["SenseVoiceASR", "create_backend"]
