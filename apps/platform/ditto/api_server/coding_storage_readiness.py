"""Read-only exact-object readiness probe for dedicated Coding storage."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from ditto.api_models.coding_evidence_upload import CodingSealedEvidenceKind
from ditto.api_models.coding_storage_readiness import (
    AdminCodingStorageReadinessResponse,
    CodingStorageAuthorityReadiness,
)
from ditto.api_server.coding_private_catalog import CodingPrivateCatalogConfig
from ditto.api_server.coding_sealed_evidence_storage import (
    CodingSealedEvidenceStorageConfig,
)
from ditto.api_server.coding_storage_data_plane_canary import (
    coding_storage_evidence_canary_key,
    coding_storage_evidence_canary_payload,
    coding_storage_private_canary_key,
    coding_storage_private_canary_payload,
)
from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectDownloadTooLargeError,
    ObjectNotFoundError,
    ObjectUploadFailedError,
)
from ditto.api_server.storage.models import ObjectMetadata, VerifiedObject

_EVIDENCE_CONTENT_TYPE = "application/octet-stream"
_EVIDENCE_KIND = CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT.value


class CodingStorageReadinessReader(Protocol):
    async def get_object(self, *, key: str, max_bytes: int) -> bytes: ...


class CodingStorageReadinessEvidenceReader(Protocol):
    async def head_object(self, *, key: str) -> ObjectMetadata: ...

    async def verify_object_sha256(
        self, *, key: str, expected_size_bytes: int
    ) -> VerifiedObject: ...


@dataclass(frozen=True)
class CodingStorageReadinessProbe:
    """Verify retained canaries through the production read identities only."""

    environment: Literal["dev", "prod"]
    source_sha: str
    private_reader: CodingStorageReadinessReader
    evidence_reader: CodingStorageReadinessEvidenceReader
    private_timeout_seconds: float
    evidence_timeout_seconds: float

    @classmethod
    def from_config(
        cls,
        *,
        environment: Literal["dev", "prod"],
        source_sha: str,
        private_config: CodingPrivateCatalogConfig,
        evidence_config: CodingSealedEvidenceStorageConfig,
    ) -> CodingStorageReadinessProbe:
        return cls(
            environment=environment,
            source_sha=source_sha,
            private_reader=S3StorageClient(private_config.storage_config()),
            evidence_reader=S3StorageClient(evidence_config.storage_config()),
            private_timeout_seconds=private_config.timeout_seconds,
            evidence_timeout_seconds=evidence_config.timeout_seconds,
        )

    async def snapshot(self) -> AdminCodingStorageReadinessResponse:
        private_payload = coding_storage_private_canary_payload(self.environment)
        evidence_payload = coding_storage_evidence_canary_payload(self.environment)
        private, evidence = await asyncio.gather(
            self._private_status(private_payload),
            self._evidence_status(evidence_payload),
        )
        return AdminCodingStorageReadinessResponse(
            schema="dittobench-coding-storage-readiness-v1",
            environment=self.environment,
            source_sha=self.source_sha,
            checked_at=datetime.now(UTC),
            ready=(private.status == "ready" and evidence.status == "ready"),
            private_input=private,
            sealed_evidence=evidence,
            authorities_distinct=True,
            read_only=True,
            weight_eligible=False,
        )

    async def _private_status(self, payload: bytes) -> CodingStorageAuthorityReadiness:
        expected_sha256 = hashlib.sha256(payload).hexdigest()
        try:
            async with asyncio.timeout(self.private_timeout_seconds):
                observed = await self.private_reader.get_object(
                    key=coding_storage_private_canary_key(payload),
                    max_bytes=len(payload),
                )
        except ObjectNotFoundError:
            return _status("missing", expected_sha256, len(payload))
        except (
            ObjectDownloadFailedError,
            ObjectDownloadTooLargeError,
            ObjectUploadFailedError,
            TimeoutError,
        ):
            return _status("unavailable", expected_sha256, len(payload))
        status: Literal["ready", "drifted"] = (
            "ready" if observed == payload else "drifted"
        )
        return _status(status, expected_sha256, len(payload))

    async def _evidence_status(self, payload: bytes) -> CodingStorageAuthorityReadiness:
        expected_sha256 = hashlib.sha256(payload).hexdigest()
        key = coding_storage_evidence_canary_key(payload)
        try:
            async with asyncio.timeout(self.evidence_timeout_seconds):
                metadata = await self.evidence_reader.head_object(key=key)
                verified = await self.evidence_reader.verify_object_sha256(
                    key=key, expected_size_bytes=len(payload)
                )
        except ObjectNotFoundError:
            return _status("missing", expected_sha256, len(payload))
        except (
            ObjectDownloadFailedError,
            ObjectDownloadTooLargeError,
            ObjectUploadFailedError,
            TimeoutError,
        ):
            return _status("unavailable", expected_sha256, len(payload))
        metadata_matches = (
            metadata.size_bytes == len(payload)
            and metadata.content_type == _EVIDENCE_CONTENT_TYPE
            and metadata.metadata.get("sha256") == expected_sha256
            and metadata.metadata.get("evidence-kind") == _EVIDENCE_KIND
        )
        digest_matches = (
            verified.size_bytes == len(payload) and verified.sha256 == expected_sha256
        )
        status: Literal["ready", "drifted"] = (
            "ready" if metadata_matches and digest_matches else "drifted"
        )
        return _status(status, expected_sha256, len(payload))


def _status(
    status: Literal["ready", "missing", "drifted", "unavailable"],
    sha256: str,
    size_bytes: int,
) -> CodingStorageAuthorityReadiness:
    return CodingStorageAuthorityReadiness(
        status=status,
        sha256=sha256,
        size_bytes=size_bytes,
        exact_object_verified=status == "ready",
    )
