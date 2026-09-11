"""RingSession production identity request helpers."""

from __future__ import annotations

import asyncio
import secrets

from ring_python_sdk.ble import (
    send_identity_challenge,
    send_identity_get,
    send_identity_lock,
    send_identity_provision,
)
from ring_python_sdk.core.identity import (
    IDENTITY_CHALLENGE_LEN,
    IdentityChallengeResult,
    IdentityStatus,
    verify_identity_signature,
)


class IdentityMixin:
    async def _wait_identity_response(self, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(self._identity_event.wait(), timeout_s)
        except TimeoutError:
            print("identity request timed out")
            return False
        if self.identity_error is not None:
            error = self.identity_error
            raise RuntimeError(
                f"identity operation 0x{error.operation:02X} failed: "
                f"{error.error_code}"
            )
        return True

    def _prepare_identity_request(self) -> None:
        self._identity_event.clear()
        self.identity_error = None

    async def query_identity(self, timeout_s: float = 2.0) -> IdentityStatus | None:
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            print("identity status skipped (not connected)")
            return None
        self._prepare_identity_request()
        self.identity_status = None
        await send_identity_get(self.client, self.rx_uuid)
        if not await self._wait_identity_response(timeout_s):
            return None
        return self.identity_status
    async def provision_identity(
        self, sn: str, timeout_s: float = 5.0
    ) -> IdentityStatus | None:
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            print("identity provision skipped (not connected)")
            return None
        self._prepare_identity_request()
        self.identity_status = None
        await send_identity_provision(self.client, self.rx_uuid, sn)
        if not await self._wait_identity_response(timeout_s):
            return None
        return self.identity_status

    async def challenge_identity(
        self, timeout_s: float = 5.0
    ) -> IdentityChallengeResult | None:
        status = self.identity_status
        if status is None:
            status = await self.query_identity(timeout_s=min(timeout_s, 2.0))
        if status is None:
            return None
        if not status.provisioned or not status.key_present:
            raise RuntimeError("identity is not provisioned")
        assert self.client is not None

        challenge = secrets.token_bytes(IDENTITY_CHALLENGE_LEN)
        self._prepare_identity_request()
        self.identity_signature = None
        await send_identity_challenge(self.client, self.rx_uuid, challenge)
        if not await self._wait_identity_response(timeout_s):
            return None
        response = self.identity_signature
        if response is None:
            return None
        if response.challenge != challenge:
            raise RuntimeError("identity challenge echo mismatch")
        verify_identity_signature(status, challenge, response.signature)
        return IdentityChallengeResult(
            challenge=challenge,
            signature=response.signature,
            verified=True,
        )

    async def lock_identity(self, timeout_s: float = 5.0) -> IdentityStatus | None:
        if self.client is None or not self.client.is_connected or not self.rx_uuid:
            print("identity lock skipped (not connected)")
            return None
        self._prepare_identity_request()
        self.identity_status = None
        await send_identity_lock(self.client, self.rx_uuid)
        if not await self._wait_identity_response(timeout_s):
            return None
        return self.identity_status
