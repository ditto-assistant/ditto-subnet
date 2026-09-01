"""Tests for the default-off sealed-evidence S3 capability minter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ditto.api_models.coding_evidence_upload import CodingSealedEvidenceKind
from ditto.api_server.coding_sealed_evidence_storage import (
    CodingSealedEvidenceCapabilityMinter,
    CodingSealedEvidenceStorageConfig,
    CodingSealedEvidenceStorageConfigurationError,
    CodingSealedEvidenceStorageIntegrityError,
    coding_sealed_evidence_object_key,
    parse_coding_sealed_evidence_storage_config_from_env,
)
from ditto.db.models import CodingSealedEvidenceUpload

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class _Store:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def presigned_put_url(
        self,
        *,
        key: str,
        size_bytes: int,
        metadata: dict[str, str],
        content_type: str,
        expires_in: int,
    ) -> str:
        self.calls.append(
            {
                "key": key,
                "size_bytes": size_bytes,
                "metadata": metadata,
                "content_type": content_type,
                "expires_in": expires_in,
            }
        )
        return (
            f"https://evidence.example.com/sealed-coding-evidence/{key}"
            "?X-Amz-Date=20260901T120000Z"
            f"&X-Amz-Expires={expires_in}&X-Amz-Signature=synthetic"
        )


def _config() -> CodingSealedEvidenceStorageConfig:
    return CodingSealedEvidenceStorageConfig(
        endpoint_url="https://evidence.example.com",
        bucket="sealed-coding-evidence",
        access_key="evidence-access",
        secret_key="evidence-secret",
    )


def _upload() -> CodingSealedEvidenceUpload:
    return CodingSealedEvidenceUpload(
        upload_id=uuid4(),
        ticket_id=uuid4(),
        claim_generation=7,
        evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT.value,
        sha256="ab" * 32,
        size_bytes=4096,
        content_type="application/octet-stream",
        weight_eligible=False,
    )


def test_parse_storage_config_is_optional_and_all_or_nothing() -> None:
    assert parse_coding_sealed_evidence_storage_config_from_env({}) is None
    with pytest.raises(
        CodingSealedEvidenceStorageConfigurationError, match="incomplete"
    ):
        parse_coding_sealed_evidence_storage_config_from_env(
            {"DITTO_CODING_EVIDENCE_STORAGE_BUCKET": "sealed-coding-evidence"}
        )


def test_evidence_key_is_content_addressed_and_kind_scoped() -> None:
    assert coding_sealed_evidence_object_key(
        evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
        sha256="ab" * 32,
    ) == ("coding-evidence/v1/authoring-transcript/sha256/" + "ab" * 32)
    with pytest.raises(ValueError):
        coding_sealed_evidence_object_key(
            evidence_kind=CodingSealedEvidenceKind.FROZEN_SUBMISSION,
            sha256="AB" * 32,
        )


async def test_minter_binds_exact_reservation_without_retaining_url() -> None:
    store = _Store()
    upload = _upload()
    minter = CodingSealedEvidenceCapabilityMinter(
        _config(), object_store=store, clock=lambda: _NOW
    )

    capability = await minter.mint(
        upload,
        ticket_deadline=_NOW + timedelta(hours=1),
        claim_expires_at=_NOW + timedelta(minutes=2),
    )

    assert capability.ticket_id == upload.ticket_id
    assert capability.upload_id == upload.upload_id
    assert capability.weight_eligible is False
    assert capability.expires_at == _NOW + timedelta(minutes=2)
    assert "synthetic" not in repr(capability)
    assert store.calls == [
        {
            "key": coding_sealed_evidence_object_key(
                evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
                sha256=upload.sha256,
            ),
            "size_bytes": upload.size_bytes,
            "metadata": {
                "sha256": upload.sha256,
                "evidence-kind": "authoring-transcript",
            },
            "content_type": "application/octet-stream",
            "expires_in": 120,
        }
    ]


async def test_minter_rejects_near_expiry_or_identity_drift() -> None:
    store = _Store()
    minter = CodingSealedEvidenceCapabilityMinter(
        _config(), object_store=store, clock=lambda: _NOW
    )
    with pytest.raises(CodingSealedEvidenceStorageIntegrityError, match="lifetime"):
        await minter.mint(
            _upload(),
            ticket_deadline=_NOW + timedelta(hours=1),
            claim_expires_at=_NOW + timedelta(seconds=59),
        )
    drifted = _upload()
    drifted.evidence_kind = "unknown-kind"
    with pytest.raises(CodingSealedEvidenceStorageIntegrityError, match="kind"):
        await minter.mint(
            drifted,
            ticket_deadline=_NOW + timedelta(hours=1),
            claim_expires_at=_NOW + timedelta(minutes=2),
        )
