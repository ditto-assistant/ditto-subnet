"""Unwired trusted uploader for immutable shadow-coding evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

import httpx

from ditto.api_models.coding_claims import CodingClaimResponse
from ditto.api_models.coding_evidence_upload import (
    CodingSealedEvidenceFinalization,
    CodingSealedEvidenceKind,
    CodingSealedEvidenceUploadCapability,
)
from ditto.validator.coding_publication import (
    CodingPublicationClient,
    SealedEvidenceKind,
)
from ditto.validator.errors import PlatformInfrastructureError


class CodingEvidencePlatform(Protocol):
    async def request_coding_evidence_upload_capability(
        self,
        claim: CodingClaimResponse,
        *,
        evidence_kind: CodingSealedEvidenceKind,
        sha256: str,
        size_bytes: int,
    ) -> CodingSealedEvidenceUploadCapability: ...

    async def finalize_coding_evidence_upload(
        self,
        claim: CodingClaimResponse,
        capability: CodingSealedEvidenceUploadCapability,
    ) -> CodingSealedEvidenceFinalization: ...


@dataclass(frozen=True, repr=False)
class CodingSealedEvidenceUploader:
    """Stream one local object through an exact capability and finalize it."""

    platform: CodingEvidencePlatform
    outbox: CodingPublicationClient
    storage_client: httpx.AsyncClient
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if (
            self.platform is None
            or self.outbox is None
            or self.storage_client is None
            or getattr(self.storage_client, "trust_env", True)
            or not callable(self.clock)
        ):
            raise ValueError("coding evidence uploader configuration is invalid")

    async def upload(
        self,
        claim: CodingClaimResponse,
        *,
        record_id: str,
        evidence_kind: CodingSealedEvidenceKind,
        sha256: str,
        size_bytes: int,
    ) -> CodingSealedEvidenceFinalization:
        capability = await self.reserve(
            claim,
            evidence_kind=evidence_kind,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        return await self.upload_reserved(
            claim,
            record_id=record_id,
            capability=capability,
        )

    async def reserve(
        self,
        claim: CodingClaimResponse,
        *,
        evidence_kind: CodingSealedEvidenceKind,
        sha256: str,
        size_bytes: int,
    ) -> CodingSealedEvidenceUploadCapability:
        """Reserve and validate one exact capability without opening bytes."""

        capability = await self.platform.request_coding_evidence_upload_capability(
            claim,
            evidence_kind=evidence_kind,
            sha256=sha256,
            size_bytes=size_bytes,
        )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PlatformInfrastructureError(
                "coding evidence uploader clock is invalid"
            )
        now = now.astimezone(UTC)
        if (
            capability.ticket_id != claim.ticket_id
            or capability.claim_generation != claim.claim_generation
            or capability.ticket_deadline != claim.ticket_deadline
            or capability.evidence_kind != evidence_kind
            or capability.sha256 != sha256
            or capability.size_bytes != size_bytes
            or capability.expires_at <= now
            or capability.expires_at > claim.claim_expires_at
        ):
            raise PlatformInfrastructureError(
                "coding evidence upload capability authority is invalid"
            )
        return capability

    async def upload_reserved(
        self,
        claim: CodingClaimResponse,
        *,
        record_id: str,
        capability: CodingSealedEvidenceUploadCapability,
    ) -> CodingSealedEvidenceFinalization:
        """Upload and finalize one previously validated exact capability."""

        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise PlatformInfrastructureError(
                "coding evidence uploader clock is invalid"
            )
        now = now.astimezone(UTC)
        if (
            capability.ticket_id != claim.ticket_id
            or capability.claim_generation != claim.claim_generation
            or capability.ticket_deadline != claim.ticket_deadline
            or capability.expires_at <= now
            or capability.expires_at > claim.claim_expires_at
        ):
            raise PlatformInfrastructureError(
                "coding evidence upload capability authority is invalid"
            )
        evidence_kind = capability.evidence_kind
        sha256 = capability.sha256
        size_bytes = capability.size_bytes
        headers = {
            "Content-Length": str(size_bytes),
            "Content-Type": "application/octet-stream",
            "X-Amz-Checksum-SHA256": capability.checksum_sha256_b64,
            "X-Amz-Meta-Evidence-Kind": evidence_kind.value,
            "X-Amz-Meta-Sha256": sha256,
        }
        try:
            async with (
                self.outbox.stream_evidence(
                    ticket_id=str(claim.ticket_id),
                    record_id=record_id,
                    evidence_kind=cast(SealedEvidenceKind, evidence_kind.value),
                    sha256=sha256,
                    size_bytes=size_bytes,
                ) as chunks,
                self.storage_client.stream(
                    "PUT",
                    capability.url,
                    headers=headers,
                    content=chunks,
                    follow_redirects=False,
                ) as response,
            ):
                response_body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16 << 10):
                    if len(response_body) + len(chunk) > 64 << 10:
                        raise PlatformInfrastructureError(
                            "coding evidence storage response is too large"
                        )
                    response_body.extend(chunk)
                if response.status_code != 200:
                    raise PlatformInfrastructureError(
                        "coding evidence storage upload was rejected"
                    )
        except httpx.HTTPError:
            raise PlatformInfrastructureError(
                "coding evidence storage upload failed"
            ) from None
        return await self.platform.finalize_coding_evidence_upload(
            claim,
            capability,
        )

    def __str__(self) -> str:
        return "CodingSealedEvidenceUploader{private}"

    def __repr__(self) -> str:
        return self.__str__()


__all__ = ["CodingSealedEvidenceUploader"]
