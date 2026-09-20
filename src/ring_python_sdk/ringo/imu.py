"""Ringo BLE samples in ai-ring stream_hub's original input format."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import numpy as np

from ring_python_sdk.imu.processor import ImuSample
from ring_python_sdk.imu.units import GRAVITY_MS2


class ImuAdapter:
    """Convert units and unwrap the wire clock; leave model preprocessing upstream.

    Ringo's decoder already applies the chip-to-BCL host rotation. ai-ring uses
    9.8 m/s² per g and rad/s. No mounting transform, calibration, filtering,
    finite-value replacement or model reset belongs in this transport adapter.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._previous_uptime_ms: float | None = None
        self._elapsed_ms = 0.0

    def __call__(self, sample: ImuSample) -> np.ndarray:
        if sample.raw is None:
            raise ValueError("ringo algorithms require six-axis raw/delta IMU; token frames are unsupported")
        acc = np.asarray(sample.accel_ms2) * (9.8 / GRAVITY_MS2)
        gyr = np.deg2rad(sample.gyro_dps)
        return np.concatenate((acc, gyr)).astype(np.float32)

    def to_message(
        self, sample: ImuSample, *, address: str = "", name: str = "",
        host_timestamp: float | None = None,
    ) -> dict[str, Any]:
        """Return the original ``ImuStreamHub.publish_imu`` message schema.

        host_timestamp is the host receipt time unless a caller supplies a
        synchronized host time. local_timestamp preserves device sample timing,
        including gaps and repeats, while unwrapping the uint32 millisecond clock.
        """
        values = self(sample)
        current = sample.uptime_ms
        if self._previous_uptime_ms is None:
            self._elapsed_ms = current
        else:
            delta = (current - self._previous_uptime_ms + (1 << 31)) % (1 << 32) - (1 << 31)
            self._elapsed_ms += delta
        self._previous_uptime_ms = current
        timestamp = self._elapsed_ms / 1000.0
        return {
            "type": "imu", "address": address, "name": name,
            "timestamp": timestamp, "local_timestamp": timestamp,
            "host_timestamp": time.time() if host_timestamp is None else host_timestamp,
            "acc": values[:3].tolist(), "gyr": values[3:].tolist(),
        }


def button_to_touch_message(event, *, address: str = "", name: str = "",
                            host_timestamp: float | None = None) -> dict[str, Any] | None:
    """Map Ringo PRESS/RELEASE to the original handwriting touch codes 1/5."""
    if event.event not in (0, 1):
        return None
    timestamp = event.uptime_ms / 1000.0
    return {
        "type": "touch", "address": address, "name": name,
        "host_timestamp": time.time() if host_timestamp is None else host_timestamp,
        "action_code": 1 if event.event == 0 else 5,
        "tap_timestamp": timestamp if event.event == 0 else None,
        "release_timestamp": timestamp if event.event == 1 else None,
    }


class ImuPipeline:
    """Forward BLE messages to upstream wrappers without changing their buffers.

    Native message wrappers expose ``on_imu_message``; simple consumers may use
    ``feed(values, local_timestamp)``. Async recognizers deliver via their own
    on_event callback. Only synchronous return values go to on_result. Call
    reset explicitly for a new capture; each upstream runtime owns gap handling.
    """

    def __init__(
        self, *processors: Any, adapter: ImuAdapter | None = None,
        on_result: Callable[[Any, Any], None] | None = None,
        address: str = "sdk", name: str = "",
    ) -> None:
        self.processors = processors
        self.adapter = adapter if adapter is not None else ImuAdapter()
        self.on_result = on_result
        self.address, self.name = address, name

    def reset(self) -> None:
        self.adapter.reset()
        for processor in self.processors:
            processor.reset()

    def on_sample(self, sample: ImuSample) -> tuple[Any, ...]:
        message = self.adapter.to_message(sample, address=self.address, name=self.name)
        values = np.asarray(message["acc"] + message["gyr"], dtype=np.float32)
        results = []
        for processor in self.processors:
            handler = getattr(processor, "on_imu_message", None)
            result = (handler(message) if handler is not None
                      else processor.feed(values, message["local_timestamp"]))
            if result is not None and not (isinstance(result, (tuple, list)) and not result):
                results.append(result)
                if self.on_result is not None:
                    self.on_result(processor, result)
        return tuple(results)

    def close(self) -> None:
        for processor in self.processors:
            close = getattr(processor, "close", None)
            if close is not None:
                close()
