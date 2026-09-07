"""Native-v2 custody gate: verified metadata and fresh grant checks around unwrap."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_server.coding_hosted_authoring_evidence import canonical
from ditto.api_server.coding_hosted_runtime_io import read_private
from ditto.api_server.coding_private_v2_retrieval import (
    PrivateV2GrantStore,
    PrivateV2InputAuthority,
    PrivateV2UnwrapRequest,
    PrivateV2UnwrapResult,
)


class PrivateV2CustodyError(ValueError):
    """Fixed private-process diagnostic; never returns a key or object locator."""


class PrivateV2KeyBackend(Protocol):
    async def unwrap(self, request: PrivateV2UnwrapRequest) -> bytes: ...


class ProtectedRSAKeyBackend:
    """Explicit local custody option; no key read until the gate authorizes unwrap.

    The protected custody process/host is trusted. This is not a remote KMS or
    confidential-computing boundary, and must never run on a validator host.
    """

    def __init__(self, private_key_file: Path):
        if not private_key_file.is_absolute() or ".." in private_key_file.parts:
            raise PrivateV2CustodyError("native v2 custody key path invalid")
        self._path = private_key_file

    async def unwrap(self, request: PrivateV2UnwrapRequest) -> bytes:
        try:
            key = serialization.load_pem_private_key(
                read_private(self._path, 32768), password=None
            )
            if (
                not isinstance(key, rsa.RSAPrivateKey)
                or not 3072 <= key.key_size <= 8192
                or key.public_key().public_numbers().e != 65537
            ):
                raise ValueError("key")
            public = key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if hashlib.sha256(public).hexdigest() != request.wrapping_key_sha256:
                raise ValueError("key identity")
            return key.decrypt(
                base64.b64decode(request.wrapped_data_key_b64, validate=True),
                padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=bytes.fromhex(request.aad_sha256),
                ),
            )
        except Exception:
            raise PrivateV2CustodyError("native v2 key custody unavailable") from None

    def __repr__(self) -> str:
        return "ProtectedRSAKeyBackend(private=True)"


class PrivateV2Custody:
    def __init__(
        self,
        *,
        authority: PrivateV2InputAuthority,
        grants: PrivateV2GrantStore,
        backend: PrivateV2KeyBackend,
        clock: Callable[[], int] | None = None,
    ):
        self._authority, self._grants, self._backend = authority, grants, backend
        self._clock = clock or (lambda: int(time.time()))

    async def unwrap(self, request: PrivateV2UnwrapRequest) -> PrivateV2UnwrapResult:
        try:
            if type(request.expires_at_unix) is not int:
                raise ValueError("expiry")
            remaining = request.expires_at_unix - self._clock()
            if not 0 < remaining <= 3600:
                raise ValueError("expiry")
            async with asyncio.timeout(min(20, remaining)):
                grant_id = UUID(request.grant_id)
                grant = await self._grants.active_grant(
                    grant_id=grant_id, audience=request.audience
                )
                if (
                    grant is None
                    or self._authority.unwrap_request(grant, request.role) != request
                ):
                    raise ValueError("authority")
                key = await self._backend.unwrap(request)
                if type(key) is not bytes or len(key) != 32:
                    raise ValueError("data key")
                current = await self._grants.active_grant(
                    grant_id=grant_id, audience=request.audience
                )
                if current != grant or self._clock() >= request.expires_at_unix:
                    raise ValueError("revocation")
                # Reconstruct again, so metadata/phase changes cannot release a key.
                if (
                    current is None
                    or self._authority.unwrap_request(current, request.role) != request
                ):
                    raise ValueError("authority")
                return PrivateV2UnwrapResult(request.digest(), key)
        except Exception:
            raise PrivateV2CustodyError("native v2 key custody unavailable") from None

    async def handle(self, body: bytes) -> bytes:
        """Exact bounded private-process protocol; no outward HTTP route."""
        try:
            raw = _decode_json_document(body, maximum_bytes=16384)
            if not isinstance(raw, dict):
                raise ValueError("request")
            # Unknown fields are non-authoritative and never echoed or signed.
            request = PrivateV2UnwrapRequest(
                **{
                    name: raw[name]
                    for name in PrivateV2UnwrapRequest.__dataclass_fields__
                }
            )
            # Recheck known types/canonical UUID/digest/phase through exact authority.
            result = await self.unwrap(request)
            return canonical(
                {
                    "schema": "dittobench-coding-private-v2-unwrap-result-v1",
                    "request_sha256": result.request_sha256,
                    "data_key_b64": base64.b64encode(result.data_key).decode("ascii"),
                    "weight_eligible": False,
                }
            )
        except Exception:
            raise PrivateV2CustodyError("native v2 key custody unavailable") from None
