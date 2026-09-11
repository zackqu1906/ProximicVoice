"""Production identity (0x34) packet builders, parsers, and verification."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from ring_python_sdk.core.constants import (
    CMD_IDENTITY,
    SUBCMD_IDENTITY_CHALLENGE,
    SUBCMD_IDENTITY_ERROR,
    SUBCMD_IDENTITY_GET,
    SUBCMD_IDENTITY_LOCK,
    SUBCMD_IDENTITY_PROVISION,
    SUBCMD_IDENTITY_SIGNATURE,
    SUBCMD_IDENTITY_STATUS,
)

FLAG_IDENTITY_PROVISIONED = 1 << 0
FLAG_IDENTITY_LOCKED = 1 << 1
FLAG_IDENTITY_KEY_PRESENT = 1 << 2

IDENTITY_SN_MAX_LEN = 31
IDENTITY_UID_MAX_LEN = 16
IDENTITY_CHALLENGE_LEN = 32
IDENTITY_SIGNATURE_LEN = 64
IDENTITY_PUBLIC_KEY_LEN = 65
IDENTITY_STATUS_PACKET_LEN = 126
IDENTITY_SIGNATURE_PACKET_LEN = 98
IDENTITY_ERROR_PACKET_LEN = 5
IDENTITY_CHALLENGE_DOMAIN = b"RINGO-ID-V1"


@dataclass(frozen=True)
class IdentityStatus:
    format_version: int
    provisioned: bool
    locked: bool
    key_present: bool
    hardware_revision: int
    firmware_parts: tuple[int, int, int, int]
    sn: str
    chip_uid: bytes
    public_key: bytes
    algorithm_id: int

    @property
    def hardware_version(self) -> str:
        return f"v{self.hardware_revision}"

    @property
    def firmware_version(self) -> str:
        parts = self.firmware_parts
        if parts[3]:
            return ".".join(str(part) for part in parts)
        return ".".join(str(part) for part in parts[:3])

    @property
    def chip_uid_hex(self) -> str:
        return self.chip_uid.hex().upper()

    @property
    def key_fingerprint(self) -> str:
        return hashlib.sha256(self.public_key).hexdigest().upper()


@dataclass(frozen=True)
class IdentitySignature:
    challenge: bytes
    signature: bytes


@dataclass(frozen=True)
class IdentityError:
    operation: int
    error_code: int


@dataclass(frozen=True)
class IdentityChallengeResult:
    challenge: bytes
    signature: bytes
    verified: bool


def normalize_identity_sn(sn: str) -> str:
    normalized = sn.strip().upper()
    if not 4 <= len(normalized) <= IDENTITY_SN_MAX_LEN:
        raise ValueError(f"SN length must be 4..{IDENTITY_SN_MAX_LEN}")
    if any(
        not (ch.isascii() and (ch.isupper() or ch.isdigit() or ch == "-"))
        for ch in normalized
    ):
        raise ValueError("SN may only contain uppercase letters, digits, and hyphens")
    return normalized


def build_identity_get() -> bytes:
    return bytes([CMD_IDENTITY, SUBCMD_IDENTITY_GET])


def build_identity_lock() -> bytes:
    return bytes([CMD_IDENTITY, SUBCMD_IDENTITY_LOCK])


def build_identity_provision(sn: str) -> bytes:
    encoded = normalize_identity_sn(sn).encode("ascii")
    return bytes([CMD_IDENTITY, SUBCMD_IDENTITY_PROVISION, len(encoded)]) + encoded


def build_identity_challenge(challenge: bytes) -> bytes:
    if len(challenge) != IDENTITY_CHALLENGE_LEN:
        raise ValueError(f"identity challenge must be {IDENTITY_CHALLENGE_LEN} bytes")
    return bytes([CMD_IDENTITY, SUBCMD_IDENTITY_CHALLENGE]) + challenge


def parse_identity_status(packet: bytes | bytearray) -> IdentityStatus | None:
    data = bytes(packet)
    if len(data) != IDENTITY_STATUS_PACKET_LEN:
        return None
    if data[0:2] != bytes([CMD_IDENTITY, SUBCMD_IDENTITY_STATUS]):
        return None
    if data[2] != 1:
        return None

    sn_len = data[9]
    uid_len = data[42]
    public_key_len = data[59]
    if sn_len > IDENTITY_SN_MAX_LEN or uid_len > IDENTITY_UID_MAX_LEN:
        return None
    if public_key_len not in (0, IDENTITY_PUBLIC_KEY_LEN):
        return None

    try:
        sn = data[10 : 10 + sn_len].decode("ascii")
        if sn:
            sn = normalize_identity_sn(sn)
    except (UnicodeDecodeError, ValueError):
        return None
    public_key = data[60 : 60 + public_key_len]
    if public_key and public_key[0] != 0x04:
        return None

    flags = data[3]
    return IdentityStatus(
        format_version=data[2],
        provisioned=bool(flags & FLAG_IDENTITY_PROVISIONED),
        locked=bool(flags & FLAG_IDENTITY_LOCKED),
        key_present=bool(flags & FLAG_IDENTITY_KEY_PRESENT),
        hardware_revision=data[4],
        firmware_parts=tuple(data[5:9]),
        sn=sn,
        chip_uid=data[43 : 43 + uid_len],
        public_key=public_key,
        algorithm_id=data[125],
    )

def parse_identity_signature(
    packet: bytes | bytearray,
) -> IdentitySignature | None:
    data = bytes(packet)
    if len(data) != IDENTITY_SIGNATURE_PACKET_LEN:
        return None
    if data[0:2] != bytes([CMD_IDENTITY, SUBCMD_IDENTITY_SIGNATURE]):
        return None
    return IdentitySignature(
        challenge=data[2:34],
        signature=data[34:98],
    )


def parse_identity_error(packet: bytes | bytearray) -> IdentityError | None:
    data = bytes(packet)
    if len(data) != IDENTITY_ERROR_PACKET_LEN:
        return None
    if data[0:2] != bytes([CMD_IDENTITY, SUBCMD_IDENTITY_ERROR]):
        return None
    return IdentityError(
        operation=data[2],
        error_code=int.from_bytes(data[3:5], "little", signed=True),
    )


def build_identity_challenge_message(
    status: IdentityStatus, challenge: bytes
) -> bytes:
    if len(challenge) != IDENTITY_CHALLENGE_LEN:
        raise ValueError(f"identity challenge must be {IDENTITY_CHALLENGE_LEN} bytes")
    if not status.sn or not status.chip_uid:
        raise ValueError("identity status is missing SN or chip UID")
    return (
        IDENTITY_CHALLENGE_DOMAIN
        + b"\x00"
        + status.sn.encode("ascii")
        + b"\x00"
        + status.chip_uid
        + challenge
    )


def verify_identity_signature(
    status: IdentityStatus, challenge: bytes, signature: bytes
) -> None:
    if len(status.public_key) != IDENTITY_PUBLIC_KEY_LEN or status.algorithm_id != 1:
        raise ValueError("identity does not contain a supported P-256 public key")
    if len(signature) != IDENTITY_SIGNATURE_LEN:
        raise ValueError(f"identity signature must be {IDENTITY_SIGNATURE_LEN} bytes")

    public_key = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), status.public_key
    )
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    try:
        public_key.verify(
            encode_dss_signature(r, s),
            build_identity_challenge_message(status, challenge),
            ec.ECDSA(hashes.SHA256()),
        )
    except InvalidSignature as exc:
        raise ValueError("identity challenge signature verification failed") from exc


def format_identity_status(status: IdentityStatus) -> str:
    sn = status.sn or "-"
    uid = status.chip_uid_hex or "-"
    fingerprint = status.key_fingerprint if status.public_key else "-"
    return (
        f"IDENTITY sn={sn} provisioned={'yes' if status.provisioned else 'no'} "
        f"locked={'yes' if status.locked else 'no'} "
        f"key={'yes' if status.key_present else 'no'} "
        f"hw={status.hardware_version} fw={status.firmware_version} "
        f"uid={uid} key_fp={fingerprint}"
    )
