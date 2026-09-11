"""One asyncio loop, two independent BLE clients and codec states.

Host receipt timestamps are diagnostic evidence, never claimed as ADC sync.
Original notification packets and decoded callback WAVs survive alignment.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import struct
import time
import wave
from typing import Callable

import numpy as np

from .storage import SAMPLE_RATE, audio_stats, new_take, read_json, read_wav, utc_now, write_json, write_wav

ROLES = ("input", "reference")


class FrameArchive:
    def __init__(self, directory: Path, role: str, encoding: str):
        from ring_python_sdk.core.mic_capture_assembler import MicCaptureAssembler
        from ring_python_sdk.audio.opus_codec import OrderedOpusDecoder

        self.directory, self.role, self.encoding = directory, role, encoding
        self.assembler = MicCaptureAssembler()
        self.decoder = OrderedOpusDecoder(eager=encoding == "opus")
        self.packets = (directory / f"{role}.notifications.bin").open("wb")
        self.log = (directory / f"{role}.frames.jsonl").open("w", encoding="utf-8")
        self.wave = wave.open(str(directory / f"{role}.capture.wav"), "wb")
        self.wave.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        self.frames: list[dict] = []
        self.frame_meta: dict[int, dict] = {}
        self.samples = 0
        self.packet_count = 0
        self.errors: list[str] = []
        self.last_pcm_ns = 0
        self.latest_level = -100.0
        self.closed = False
        self.first_ns: int | None = None

    def notify(self, packet: bytes) -> None:
        from ring_python_sdk.audio.adpcm import decode_ima_adpcm_frame
        from ring_python_sdk.core.constants import CMD_MIC, MIC_HEADER_SIZE, SUBCMD_MIC_PACKET_OPUS, SUBCMD_MIC_PACKET_ADPCM

        if self.closed or len(packet) < MIC_HEADER_SIZE or packet[0] != CMD_MIC or packet[1] not in (2, 3, 4):
            return
        now = time.monotonic_ns()
        self.packet_count += 1
        self.packets.write(struct.pack("<QI", now, len(packet)) + packet)
        self.packets.flush()
        _, _, seq, frag, count, uptime = struct.unpack_from("<BBHHHI", packet)
        self.frame_meta.setdefault(seq, {"first_host_ns": now, "device_uptime_ms": uptime})
        self.frame_meta[seq]["last_host_ns"] = now
        try:
            completed = self.assembler.add_packet(packet)
            if completed is None:
                return
            kind, payload, seq = completed
            if kind == SUBCMD_MIC_PACKET_OPUS:
                self._decoded(self.decoder.push(seq, payload))
            else:
                pcm = decode_ima_adpcm_frame(payload) if kind == SUBCMD_MIC_PACKET_ADPCM else payload
                self.accept_pcm(seq, pcm)
        except Exception as exc:
            self.errors.append(f"seq={seq}: {type(exc).__name__}: {exc}")

    def _decoded(self, results) -> None:
        for result in results:
            if result.pcm is None:
                self.errors.append(f"seq={result.frame_seq}: decode failed: {result.error}")
            else:
                self.accept_pcm(result.frame_seq, result.pcm)

    def accept_pcm(self, seq: int, pcm: bytes) -> None:
        if not pcm or len(pcm) % 2:
            raise ValueError("Invalid PCM block")
        now = time.monotonic_ns()
        self.first_ns = self.first_ns or now
        self.last_pcm_ns = now
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768
        self.latest_level = audio_stats(x)["rms_dbfs"]
        entry = {"frame_seq": seq, "callback_host_ns": now, "sample_offset": self.samples,
                 "sample_count": len(x), **self.frame_meta.get(seq, {})}
        self.wave.writeframes(pcm)  # update WAV header on each callback for crash recovery
        self.samples += len(x)
        self.frames.append(entry)
        self.log.write(json.dumps(entry) + "\n")
        self.log.flush()

    def finish(self) -> dict:
        if self.closed:
            raise RuntimeError("Archive already closed")
        try:
            decoded, blocked = self.decoder.finish(self.assembler.inflight_seqs)
            self._decoded(decoded)
            if blocked:
                self.errors.append(f"{blocked} incomplete/blocked codec frames")
        finally:
            self.closed = True
            self.wave.close()
            self.log.close()
            self.packets.close()

        source = read_wav(self.directory / f"{self.role}.capture.wav")
        gaps = []
        chunks = []
        timeline = []
        if self.frames:
            # Takes are capped at ten minutes, well below half a 16-bit sequence cycle.
            anchor = self.frames[0]["frame_seq"]
            ordered = sorted(self.frames, key=lambda r: ((r["frame_seq"]-anchor+32768) % 65536)-32768)
            block_samples = int(np.median([r["sample_count"] for r in ordered]))
            expected = None
            cursor = 0
            seen = set()
            for r in ordered:
                seq = r["frame_seq"]
                if seq in seen:
                    self.errors.append(f"duplicate decoded seq={seq}")
                    continue
                seen.add(seq)
                if expected is not None:
                    missing = (seq - expected) % 65536
                    if missing:
                        if missing > 6000:
                            self.errors.append("Unrecoverable sequence jump")
                            break
                        n = missing * block_samples
                        gaps.append({"start_sample": cursor, "end_sample": cursor+n,
                                     "missing_frames": missing, "length_estimated": True})
                        chunks.append(np.zeros(n, dtype=np.float32))
                        cursor += n
                segment = source[r["sample_offset"]:r["sample_offset"]+r["sample_count"]]
                timeline.append({**r, "ordered_sample_offset": cursor})
                chunks.append(segment)
                cursor += len(segment)
                expected = (seq + 1) % 65536
        audio = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32)
        write_wav(self.directory / f"{self.role}.wav", audio)
        report = {**audio_stats(audio), "packet_count": self.packet_count,
                  "decoded_frames": len(self.frames), "gaps": gaps, "errors": self.errors,
                  "incomplete_frames": self.assembler.inflight_frame_count,
                  "first_callback_host_ns": self.first_ns,
                  "last_callback_host_ns": self.last_pcm_ns,
                  "frame_timeline": timeline,
                  "clock_note": "device_uptime_ms is unsynchronized device time; host timestamps include BLE delay"}
        write_json(self.directory / f"{self.role}.quality.json", report)
        return {k: v for k, v in report.items() if k != "frame_timeline"}


class DualCapture:
    def __init__(self, emit: Callable[[str, object], None]):
        self.emit = emit
        self.discovered: dict[str, object] = {}
        self.clients: dict[str, object] = {}
        self.devices: dict[str, dict] = {}
        self.archives: dict[str, FrameArchive] = {}
        self.take: Path | None = None
        self.started = 0.0
        self.stopping = False
        self.setup_active = False
        self.capture_failure: str | None = None
        self.encoding = "opus"
        self.monitor_task: asyncio.Task | None = None
        self.sync_events: list[dict] = []

    async def scan(self):
        from ring_python_sdk.ble.control import scan_all_devices

        if self.take:
            raise RuntimeError("请先结束录音再扫描")
        devices = await scan_all_devices(5)
        self.discovered = {d.identifier: d.device for d in devices}
        rows = [{"id": d.identifier, "name": d.name or "未命名设备", "rssi": d.rssi} for d in devices]
        self.emit("devices", rows)
        return rows

    async def connect(self, input_id: str, reference_id: str):
        from bleak import BleakClient
        from ring_python_sdk.ble.control import ensure_nus_characteristics

        if not input_id or input_id == reference_id:
            raise ValueError("请选择两只不同的 ring")
        if input_id not in self.discovered or reference_id not in self.discovered:
            raise ValueError("设备不在扫描列表，请重新扫描")
        await self.disconnect()

        async def one(role, ident):
            device = self.discovered[ident]
            client = BleakClient(device, timeout=15, disconnected_callback=lambda _: self._lost(role))
            self.clients[role] = client
            await client.connect()
            tx, rx = ensure_nus_characteristics(client)
            self.devices[role] = {"id": ident, "name": device.name or "", "tx_uuid": tx, "rx_uuid": rx}
            await client.start_notify(tx, lambda _, data: self._notify(role, bytes(data)))

        outcomes = await asyncio.gather(one("input", input_id), one("reference", reference_id), return_exceptions=True)
        errors = [str(x) for x in outcomes if isinstance(x, BaseException)]
        if errors:
            await self.disconnect()
            raise RuntimeError("连接失败：" + "；".join(errors))
        self.emit("connected", self.devices)

    def _notify(self, role, data):
        archive = self.archives.get(role)
        if archive:
            archive.notify(data)

    def _lost(self, role):
        self.emit("log", f"{role} 蓝牙连接已断开")
        self.emit("disconnected", role)
        if self.take and not self.stopping:
            self.capture_failure = f"{role} disconnected"
            if not self.setup_active:
                asyncio.create_task(self.stop(self.capture_failure))

    async def start(self, root: str, metadata: dict, encoding: str = "opus"):
        from ring_python_sdk.ble.control import send_mic_control
        from ring_python_sdk.core.constants import MIC_ENCODE_OPUS, MIC_ENCODE_PCM, MIC_ENCODE_ADPCM

        if self.take or set(self.clients) != set(ROLES) or not all(c.is_connected for c in self.clients.values()):
            raise RuntimeError("两只设备必须先连接，且当前不能正在录音")
        if not metadata.get("speaker_id", "").strip() or not metadata.get("session_group", "").strip():
            raise ValueError("请填写说话人编号和采集场次")
        code = {"opus": MIC_ENCODE_OPUS, "pcm": MIC_ENCODE_PCM, "adpcm": MIC_ENCODE_ADPCM}[encoding]
        self.take = new_take(Path(root), metadata, self.devices)
        self.sync_events = []
        self.encoding = encoding
        self.started = time.monotonic()
        self.capture_failure = None
        self.setup_active = True
        try:
            commands = [send_mic_control(self.clients[r], self.devices[r]["rx_uuid"], False) for r in ROLES]
            await asyncio.gather(*commands)
            await asyncio.sleep(.3)
            # Drain an earlier stream before attaching fresh sequence/codec state.
            # Both decoders must be initialized before either MIC is enabled.
            for role in ROLES:
                self.archives[role] = FrameArchive(self.take / "raw", role, encoding)
            outcomes = await asyncio.gather(*[
                send_mic_control(self.clients[r], self.devices[r]["rx_uuid"], True, encode=code)
                for r in ROLES], return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            deadline = time.monotonic() + 8
            while not all(a.samples for a in self.archives.values()):
                if self.capture_failure:
                    raise RuntimeError(self.capture_failure)
                if time.monotonic() >= deadline:
                    raise TimeoutError("至少一路8秒内没有收到音频，请检查设备/编码并重试")
                await asyncio.sleep(.1)
            self.emit("recording", str(self.take))
            self.monitor_task = asyncio.create_task(self._monitor())
        except BaseException:
            await self.stop("start_failed")
            raise
        finally:
            self.setup_active = False

    async def _monitor(self):
        while self.take and not self.stopping:
            await asyncio.sleep(.25)
            now = time.monotonic_ns()
            levels = {r: {"rms_dbfs": a.latest_level, "duration_s": a.samples/SAMPLE_RATE}
                      for r, a in self.archives.items()}
            self.emit("levels", levels)
            if any(now-a.last_pcm_ns > 5_000_000_000 for a in self.archives.values()):
                await self.stop("audio_stall")
                return
            if time.monotonic() - self.started > 600:
                await self.stop("ten_minute_limit")
                return

    async def note_sync_event(self, kind: str, phase: str, host_ns: int, details: str = ""):
        if self.take and not self.stopping:
            event = {"kind": kind, "phase": phase, "host_monotonic_ns": host_ns,
                     "details": details, "note": "playback request/completion, not acoustic sample timestamp"}
            self.sync_events.append(event)
            # Persist immediately so failures/crashes retain the playback history.
            with (self.take / "markers.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    async def stop(self, reason="manual"):
        from ring_python_sdk.ble.control import send_mic_control

        if not self.take or self.stopping:
            return
        self.stopping = True
        path = self.take
        errors = []
        try:
            outcomes = await asyncio.gather(*[
                send_mic_control(self.clients[r], self.devices[r]["rx_uuid"], False)
                for r in ROLES if r in self.clients and self.clients[r].is_connected and r in self.devices
            ], return_exceptions=True)
            errors.extend(str(x) for x in outcomes if isinstance(x, BaseException))
            await asyncio.sleep(.35)  # drain in-flight microphone notifications
            quality = {}
            for role, archive in self.archives.items():
                try:
                    quality[role] = archive.finish()
                except Exception as exc:
                    errors.append(f"{role}: {exc}")
            record = read_json(path / "record.json")
            complete = reason in ("manual", "ten_minute_limit") and not errors and len(quality) == 2 and all(q["sample_count"] > 0 for q in quality.values())
            record.update(status="captured" if complete else "interrupted", ended_at=utc_now(),
                          stop_reason=reason, encoding=self.encoding, quality=quality, errors=errors,
                          device_gain="firmware_default_not_read_back", host_gain_db=0,
                          sync_events=self.sync_events)
            write_json(path / "record.json", record)
            self.emit("saved", str(path))
        finally:
            self.archives = {}
            self.take = None
            self.stopping = False
            if self.monitor_task and self.monitor_task is not asyncio.current_task():
                self.monitor_task.cancel()
            self.monitor_task = None

    async def disconnect(self):
        if self.take:
            await self.stop("user_disconnect")
        clients, self.clients = self.clients, {}
        await asyncio.gather(*[c.disconnect() for c in clients.values()], return_exceptions=True)
        self.devices = {}
        self.emit("disconnected", "all")
