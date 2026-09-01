from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ditto.api_server.coding_storage_data_plane_canary import (
    coding_storage_evidence_canary_payload,
    coding_storage_private_canary_payload,
)
from ditto.api_server.coding_storage_readiness import CodingStorageReadinessProbe
from ditto.api_server.storage.errors import ObjectNotFoundError
from ditto.api_server.storage.models import ObjectMetadata, VerifiedObject


@dataclass
class _PrivateReader:
    body: bytes | Exception

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        assert key.startswith("coding-verification/v1/private-input/sha256/")
        assert max_bytes == len(coding_storage_private_canary_payload("dev"))
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@dataclass
class _EvidenceReader:
    metadata: ObjectMetadata | Exception
    verified: VerifiedObject

    async def head_object(self, *, key: str) -> ObjectMetadata:
        assert key.startswith("coding-evidence/v1/")
        if isinstance(self.metadata, Exception):
            raise self.metadata
        return self.metadata

    async def verify_object_sha256(
        self, *, key: str, expected_size_bytes: int
    ) -> VerifiedObject:
        assert key.startswith("coding-evidence/v1/")
        assert expected_size_bytes == len(coding_storage_evidence_canary_payload("dev"))
        return self.verified


def _probe(
    *,
    private: bytes | Exception | None = None,
    metadata: ObjectMetadata | Exception | None = None,
    verified: VerifiedObject | None = None,
) -> CodingStorageReadinessProbe:
    private_payload = coding_storage_private_canary_payload("dev")
    evidence_payload = coding_storage_evidence_canary_payload("dev")
    digest = hashlib.sha256(evidence_payload).hexdigest()
    return CodingStorageReadinessProbe(
        environment="dev",
        source_sha="ab" * 20,
        private_reader=_PrivateReader(private or private_payload),
        evidence_reader=_EvidenceReader(
            metadata
            or ObjectMetadata(
                size_bytes=len(evidence_payload),
                metadata={
                    "sha256": digest,
                    "evidence-kind": "terminal-publication-acknowledgement",
                },
                content_type="application/octet-stream",
            ),
            verified or VerifiedObject(size_bytes=len(evidence_payload), sha256=digest),
        ),
        private_timeout_seconds=1,
        evidence_timeout_seconds=1,
    )


async def test_readiness_requires_both_exact_retained_canaries() -> None:
    snapshot = await _probe().snapshot()

    assert snapshot.ready is True
    assert snapshot.private_input.status == "ready"
    assert snapshot.sealed_evidence.status == "ready"
    assert snapshot.authorities_distinct is True
    assert snapshot.read_only is True
    assert snapshot.weight_eligible is False
    wire = snapshot.model_dump(mode="json", by_alias=True)
    assert wire["schema"] == "dittobench-coding-storage-readiness-v1"
    assert "key" not in str(wire)
    assert "bucket" not in str(wire)


async def test_missing_or_drifted_authority_fails_closed() -> None:
    missing = await _probe(
        private=ObjectNotFoundError("secret bucket/key must not escape")
    ).snapshot()
    assert missing.ready is False
    assert missing.private_input.status == "missing"

    evidence_payload = coding_storage_evidence_canary_payload("dev")
    drifted = await _probe(
        verified=VerifiedObject(
            size_bytes=len(evidence_payload),
            sha256="ff" * 32,
        )
    ).snapshot()
    assert drifted.ready is False
    assert drifted.sealed_evidence.status == "drifted"
