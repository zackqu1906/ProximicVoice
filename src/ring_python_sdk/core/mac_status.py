"""Dedicated MAC GET / STATUS packet helpers."""

from __future__ import annotations

from dataclasses import dataclass

from ring_python_sdk.core.constants import (
    CMD_MAC,
    MAC_ADDR_TYPE_LABELS,
    MAC_STATUS_PACKET_LEN,
    SUBCMD_MAC_GET,
    SUBCMD_MAC_STATUS,
)


@dataclass(frozen=True)
class MacStatus:
    addr_type: int
    mac: bytes

    @property
    def mac_str(self) -> str:
        return ":".join(f"{b:02X}" for b in self.mac)


def build_mac_get() -> bytes:
    return bytes([CMD_MAC, SUBCMD_MAC_GET])


def parse_mac_status(packet: bytes | bytearray) -> MacStatus | None:
    if len(packet) < MAC_STATUS_PACKET_LEN:
        return None
    if packet[0] != CMD_MAC or packet[1] != SUBCMD_MAC_STATUS:
        return None
    return MacStatus(addr_type=int(packet[2]), mac=bytes(packet[3:9]))


def format_mac(status: MacStatus | None) -> str:
    """Compact MAC line for TUI / status, e.g. ``MAC C6:A2:FF:13:8A:92 type=random``."""
    if status is None:
        return "MAC —"
    type_label = MAC_ADDR_TYPE_LABELS.get(status.addr_type, f"0x{status.addr_type:02x}")
    return f"MAC {status.mac_str} type={type_label}"
