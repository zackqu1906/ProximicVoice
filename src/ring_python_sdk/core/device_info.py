"""Dedicated INFO GET / STATUS packet helpers (device + firmware model)."""

from __future__ import annotations

from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_INFO,
    INFO_COMPONENT_COUNT,
    INFO_COMPONENT_LEN,
    INFO_COMP_LABELS,
    INFO_FLAG_PROBE_OK,
    INFO_FLAG_PROBED,
    INFO_FORMAT_VER,
    INFO_HEADER_LEN,
    INFO_MODEL_LABELS,
    INFO_STATUS_PACKET_LEN,
    SUBCMD_INFO_GET,
    SUBCMD_INFO_STATUS,
)


@dataclass(frozen=True)
class InfoComponent:
    id: int
    present: int
    count: int
    model: int
    flags: int

    @property
    def probe_ok(self) -> bool:
        return bool(self.flags & INFO_FLAG_PROBE_OK)

    @property
    def probed(self) -> bool:
        return bool(self.flags & INFO_FLAG_PROBED)

    @property
    def label(self) -> str:
        return INFO_COMP_LABELS.get(self.id, f"comp{self.id}")

    @property
    def model_name(self) -> str:
        by_comp = INFO_MODEL_LABELS.get(self.id)
        if by_comp is None:
            return str(self.model)
        return by_comp.get(self.model, f"model{self.model}")

    @property
    def status_mark(self) -> str:
        """ok if probe passed, or design-only (not probed); ? if probe failed."""
        if self.probe_ok or not self.probed:
            return "ok"
        return "?"


@dataclass(frozen=True)
class DeviceInfo:
    format_ver: int
    hw_rev: int
    fw_major: int
    fw_minor: int
    fw_patch: int
    fw_tweak: int
    components: tuple[InfoComponent, ...]

    @property
    def fw_version(self) -> str:
        base = f"{self.fw_major}.{self.fw_minor}.{self.fw_patch}"
        if self.fw_tweak:
            return f"{base}.{self.fw_tweak}"
        return base

    def component(self, comp_id: int) -> InfoComponent | None:
        for c in self.components:
            if c.id == comp_id:
                return c
        return None


def build_info_get() -> bytes:
    return bytes([CMD_INFO, SUBCMD_INFO_GET])


def parse_info_status(packet: bytes | bytearray) -> DeviceInfo | None:
    if len(packet) < INFO_HEADER_LEN:
        return None
    if packet[0] != CMD_INFO or packet[1] != SUBCMD_INFO_STATUS:
        return None
    format_ver = int(packet[2])
    hw_rev = int(packet[3])
    fw_major = int(packet[4])
    fw_minor = int(packet[5])
    fw_patch = int(packet[6])
    fw_tweak = int(packet[7])
    count = int(packet[8])
    need = INFO_HEADER_LEN + count * INFO_COMPONENT_LEN
    if len(packet) < need:
        return None
    comps: list[InfoComponent] = []
    off = INFO_HEADER_LEN
    for _ in range(count):
        comps.append(
            InfoComponent(
                id=int(packet[off]),
                present=int(packet[off + 1]),
                count=int(packet[off + 2]),
                model=int(packet[off + 3]),
                flags=int(packet[off + 4]),
            )
        )
        off += INFO_COMPONENT_LEN
    return DeviceInfo(
        format_ver=format_ver,
        hw_rev=hw_rev,
        fw_major=fw_major,
        fw_minor=fw_minor,
        fw_patch=fw_patch,
        fw_tweak=fw_tweak,
        components=tuple(comps),
    )


def format_device_info(info: DeviceInfo | None) -> str:
    if info is None:
        return "INFO —"
    parts = [f"hw=v{info.hw_rev}", f"fw={info.fw_version}"]
    for c in info.components:
        if not c.present:
            continue
        parts.append(f"{c.label}:{c.model_name}/{c.status_mark}")
    return "INFO " + " ".join(parts)


def expected_info_status_len(component_count: int = INFO_COMPONENT_COUNT) -> int:
    return INFO_HEADER_LEN + component_count * INFO_COMPONENT_LEN


# Keep STATUS length constant for current firmware (8 components).
assert expected_info_status_len() == INFO_STATUS_PACKET_LEN
assert INFO_FORMAT_VER == 1
