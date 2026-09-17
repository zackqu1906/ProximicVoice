from __future__ import annotations

import json
import queue
import threading
import time

import numpy as np

from .base import AudioSource


def _sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("电脑麦克风支持未安装，请安装项目的 mic 或 ui 依赖") from exc
    return sd


def input_device_choices() -> list[dict]:
    """Persist names/API, not a PortAudio index that changes after reconnect."""
    sd = _sounddevice()
    apis = sd.query_hostapis()
    rows = []
    for index, item in enumerate(sd.query_devices()):
        if int(item["max_input_channels"]) <= 0:
            continue
        name = str(item["name"])
        api = str(apis[int(item["hostapi"])]["name"])
        rows.append({
            "value": json.dumps({"name": name, "api": api, "index": index}, ensure_ascii=False),
            "label": f"{name} · {api}",
            "name": name, "api": api, "index": index,
            "sample_rate": float(item["default_samplerate"]),
        })
    return sorted(rows, key=lambda row: ("dji" not in row["name"].casefold(), row["index"]))


def resolve_input_device(selection: str) -> dict:
    rows = input_device_choices()
    if selection:
        try:
            saved = json.loads(selection)
            matches = [row for row in rows if row["name"] == saved["name"] and row["api"] == saved["api"]]
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("麦克风设置无效，请在设置中重新选择输入设备") from exc
        if len(matches) == 1:
            return matches[0]
        matches = [row for row in matches if row["index"] == saved.get("index")]
    else:
        matches = [row for row in rows if "dji" in row["name"].casefold()]
        preferred = [row for row in matches if row["api"] in {"Windows WASAPI", "Core Audio"}]
        if preferred:
            matches = preferred
    if not matches:
        raise RuntimeError("未找到选定的麦克风，请连接 DJI 并在设置中刷新输入设备；不会改用其他麦克风")
    if len(matches) != 1:
        raise RuntimeError("检测到多个 DJI 输入，请在设置中选择具体麦克风")
    return matches[0]


class _StreamingMonoResampler:
    """Continuous FIR low-pass + fractional sampling using only NumPy.

    Native 16 kHz is untouched. Other rates retain filter history and an
    absolute output clock across callbacks; the FIR adds about 1 ms delay.
    """

    def __init__(self, input_rate: int):
        self.input_rate = int(input_rate)
        self._input_count = self._output_count = 0
        self._last = np.zeros(1, dtype=np.float32)
        taps = 32 * max(1, int(np.ceil(input_rate / 16_000))) + 1
        cutoff = 0.45 * min(1.0, 16_000 / input_rate)
        positions = np.arange(taps) - (taps - 1) / 2
        kernel = 2 * cutoff * np.sinc(2 * cutoff * positions) * np.hamming(taps)
        self._kernel = kernel / kernel.sum()
        self._history = np.zeros(taps - 1, dtype=np.float32)

    def process(self, audio: np.ndarray) -> np.ndarray:
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if self.input_rate == 16_000 or not x.size:
            return x.copy()
        joined = np.concatenate((self._history, x))
        filtered = np.convolve(joined, self._kernel, mode="valid")
        self._history = joined[-self._history.size:]
        start = self._input_count
        self._input_count += x.size
        output_end = ((self._input_count - 1) * 16_000) // self.input_rate + 1
        positions = np.arange(self._output_count, output_end, dtype=np.float64) * (self.input_rate / 16_000)
        values = np.concatenate((self._last, filtered))
        result = np.interp(positions - start + 1, np.arange(values.size), values)
        self._last = filtered[-1:]
        self._output_count = output_end
        return result.astype(np.float32)


class MicrophoneSource(AudioSource):
    """Explicit OS input, interruptible reads, and continuous mono 16 kHz PCM."""

    def __init__(self, device: str | int | None = None, *, selection: str | None = None):
        self.device = device
        self.selection = selection
        self.device_name = ""
        self._stream = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=100)
        self._pending = np.empty(0, dtype=np.float32)
        self._pending_start_ns = 0
        self._error: BaseException | None = None
        self.last_read_end_monotonic_ns: int | None = None
        self.native_sample_rate = 16_000
        self._resampler = _StreamingMonoResampler(16_000)

    @property
    def error(self) -> BaseException | None:
        return self._error

    def open(self) -> None:
        sd = _sounddevice()
        with self._lock:
            if self._stopped.is_set():
                raise RuntimeError("麦克风启动已取消")
            if self.selection is not None:
                selected = resolve_input_device(self.selection)
                self.device = selected["index"]
                self.device_name = selected["name"]
            info = sd.query_devices(self.device, "input")
            self.device_name = str(info["name"])
            rate = 16_000
            try:
                sd.check_input_settings(device=self.device, samplerate=rate, channels=1, dtype="float32")
            except sd.PortAudioError:
                rate = int(round(info["default_samplerate"]))
                sd.check_input_settings(device=self.device, samplerate=rate, channels=1, dtype="float32")
            self.native_sample_rate = rate
            self._resampler = _StreamingMonoResampler(rate)
            self._stream = sd.InputStream(
                device=self.device, samplerate=rate, channels=1, dtype="float32",
                blocksize=max(1, rate // 50),
                callback=self._on_audio, finished_callback=self._on_finished,
            )
            try:
                self._stream.start()
            except BaseException:
                self._stream.close()
                self._stream = None
                raise

    def _on_audio(self, data, frames, timing, status) -> None:
        if self._stopped.is_set() or self._error is not None:
            return
        try:
            if status:
                raise RuntimeError(f"麦克风采集中断：{status}")
            audio = self._resampler.process(np.asarray(data[:, 0], dtype=np.float32))
            if not np.all(np.isfinite(audio)):
                raise RuntimeError("麦克风返回了无效音频")
            age_s = max(0.0, float(timing.currentTime - timing.inputBufferAdcTime) - frames / self.native_sample_rate)
            end_ns = time.monotonic_ns() - int(age_s * 1_000_000_000)
            if audio.size:
                self._queue.put_nowait((audio, end_ns))
        except BaseException as exc:
            self._error = RuntimeError("麦克风处理积压，已停止本次连接") if isinstance(exc, queue.Full) else exc

    def _on_finished(self) -> None:
        if not self._stopped.is_set() and self._error is None:
            self._error = RuntimeError("麦克风连接已中断，请检查 DJI 接收器并重新连接")

    def close(self) -> None:
        self._stopped.set()
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            finally:
                stream.close()

    def read(self, frames: int) -> np.ndarray | None:
        if frames <= 0:
            raise ValueError("frames must be positive")
        deadline = time.monotonic() + 3.0
        chunks = []
        remaining = frames
        while remaining:
            if self._stopped.is_set():
                return None
            if self._error is not None:
                raise RuntimeError(f"电脑麦克风不可用：{self._error}") from self._error
            if not self._pending.size:
                try:
                    self._pending, end_ns = self._queue.get(timeout=0.05)
                    self._pending_start_ns = end_ns - int(self._pending.size * 1_000_000_000 / self.sample_rate)
                except queue.Empty:
                    if time.monotonic() >= deadline:
                        self._error = RuntimeError("电脑麦克风连续 3 秒没有音频，请检查连接和麦克风权限")
                    continue
            take = min(remaining, self._pending.size)
            chunks.append(self._pending[:take])
            self._pending = self._pending[take:]
            remaining -= take
            self._pending_start_ns += take * 1_000_000_000 // self.sample_rate
            self.last_read_end_monotonic_ns = self._pending_start_ns
        return np.concatenate(chunks).astype(np.float32, copy=False)


def list_input_devices() -> list[tuple[int, str, float, int]]:
    sd = _sounddevice()
    return [(i, d["name"], float(d["default_samplerate"]), int(d["max_input_channels"]))
            for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
