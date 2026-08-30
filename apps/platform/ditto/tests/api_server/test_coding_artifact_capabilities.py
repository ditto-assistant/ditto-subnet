"""Tests for ticket-bounded shadow coding artifact capabilities."""

from __future__ import annotations

import json
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ditto.api_models.coding_artifacts import CodingArtifactDeliveryPhase
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogIssue,
    CodingCatalogRuntimePolicy,
    CodingSelectionRunManifest,
    CodingTaskSetManifest,
)
from ditto.api_server.coding_artifact_capabilities import (
    CodingArtifactCapabilityIntegrityError,
    CodingArtifactCapabilityMinter,
    CodingArtifactCapabilityPolicy,
    CodingArtifactCapabilityUnavailableError,
    CodingArtifactKind,
    coding_artifact_object_key,
    project_coding_artifact_capability,
)
from ditto.api_server.coding_private_catalog import CodingPrivateCatalogConfig
from ditto.api_server.storage.errors import ObjectUploadFailedError
from ditto.api_server.storage.models import ObjectMetadata
from ditto.db.queries.coding_task_leases import CodingShadowTaskLeaseCore

_VECTOR_PATH = (
    Path(__file__).parents[5]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_selection_v1.json"
)
_NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _lease(*, remaining_seconds: int = 600) -> CodingShadowTaskLeaseCore:
    vector = json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))
    return CodingShadowTaskLeaseCore(
        ticket_id=uuid4(),
        validator_hotkey="5" + "V" * 47,
        issued_at=_NOW - timedelta(minutes=1),
        deadline=_NOW + timedelta(seconds=remaining_seconds),
        run_row_id=uuid4(),
        run_manifest=CodingSelectionRunManifest.model_validate(vector["run_manifest"]),
        task_set_manifest=CodingTaskSetManifest.model_validate(
            vector["task_set_manifest"]
        ),
        repository_epoch=vector["task_version"]["payload"]["repository_epoch"],
        issue=CodingCatalogIssue.model_validate(vector["issue"]),
        runtime_policy=CodingCatalogRuntimePolicy.model_validate(
            vector["runtime_policy"]
        ),
        budgets=CodingCatalogBudgets.model_validate(vector["budgets"]),
    )


def _config() -> CodingPrivateCatalogConfig:
    return CodingPrivateCatalogConfig(
        endpoint_url="https://storage.example.com",
        bucket="private-coding",
        access_key="catalog-access",
        secret_key="catalog-secret",
    )


class _Store:
    def __init__(
        self,
        *,
        wrong_sha: bool = False,
        wrong_kind: bool = False,
        size_bytes: int = 1024,
        size_by_kind: dict[str, int] | None = None,
        url: str | None = None,
        legacy_signature: bool = False,
        head_error: Exception | None = None,
        url_error: Exception | None = None,
    ) -> None:
        self.wrong_sha = wrong_sha
        self.wrong_kind = wrong_kind
        self.size_bytes = size_bytes
        self.size_by_kind = size_by_kind or {}
        self.url = url
        self.legacy_signature = legacy_signature
        self.head_error = head_error
        self.url_error = url_error
        self.head_calls: list[str] = []
        self.url_calls: list[tuple[str, int]] = []

    async def head_object(self, *, key: str) -> ObjectMetadata:
        self.head_calls.append(key)
        if self.head_error is not None:
            raise self.head_error
        parts = key.split("/")
        kind = parts[2]
        digest = parts[-1]
        return ObjectMetadata(
            size_bytes=self.size_by_kind.get(kind, self.size_bytes),
            metadata={
                "sha256": "ff" * 32 if self.wrong_sha else digest,
                "artifact-kind": "wrong" if self.wrong_kind else kind,
            },
        )

    async def presigned_get_url(
        self,
        *,
        key: str,
        expires_in: int = 300,
        attachment_filename: str | None = None,
    ) -> str:
        del attachment_filename
        self.url_calls.append((key, expires_in))
        if self.url_error is not None:
            raise self.url_error
        if self.legacy_signature:
            expires_at = int((_NOW + timedelta(seconds=expires_in)).timestamp())
            return (
                f"https://storage.example.com/private-coding/{key}"
                f"?AWSAccessKeyId=placeholder&Expires={expires_at}&Signature=secret"
            )
        return self.url or (
            f"https://storage.example.com/private-coding/{key}"
            f"?X-Amz-Date={_NOW:%Y%m%dT%H%M%SZ}"
            f"&X-Amz-Expires={expires_in}&X-Amz-Signature=secret"
        )


def _minter(store: _Store, **policy: int) -> CodingArtifactCapabilityMinter:
    return CodingArtifactCapabilityMinter(
        _config(),
        object_store=store,
        clock=lambda: _NOW,
        policy=CodingArtifactCapabilityPolicy(**policy),
    )


async def test_mints_exact_four_digest_derived_capabilities() -> None:
    lease = _lease()
    store = _Store()
    result = await _minter(store).mint(lease)

    task = lease.run_manifest.tasks[0]
    expected = [
        coding_artifact_object_key(
            kind=kind,
            sha256=digest,
        )
        for kind, digest in (
            (CodingArtifactKind.VISIBLE_BUNDLE, task.visible_bundle_sha256),
            (CodingArtifactKind.MEMORY_BUNDLE, task.memory_bundle_sha256),
            (CodingArtifactKind.RESOURCE_PROFILE, task.resource_profile_sha256),
            (CodingArtifactKind.GRADER_BUNDLE, task.grader_bundle_sha256),
        )
    ]
    assert store.head_calls == expected
    assert store.url_calls == [(key, 300) for key in expected]
    assert result.ticket_deadline == lease.deadline
    assert result.expires_at == _NOW + timedelta(seconds=300)
    assert "coding-artifacts/" not in repr(result)
    assert "signature=secret" not in repr(result)
    assert result.weight_eligible is False


async def test_projects_only_phase_appropriate_single_capabilities() -> None:
    result = await _minter(_Store()).mint(_lease())
    visible = project_coding_artifact_capability(
        result,
        kind=CodingArtifactKind.VISIBLE_BUNDLE,
        phase=CodingArtifactDeliveryPhase.AUTHORING,
    )
    grader = project_coding_artifact_capability(
        result,
        kind=CodingArtifactKind.GRADER_BUNDLE,
        phase=CodingArtifactDeliveryPhase.GRADING,
    )
    assert visible.ticket_id == result.ticket_id
    assert visible.url.startswith("https://storage.example.com/")
    assert grader.delivery_phase is CodingArtifactDeliveryPhase.GRADING
    assert "signature=secret" not in repr(visible)

    with pytest.raises(
        CodingArtifactCapabilityIntegrityError, match="projection"
    ) as captured:
        project_coding_artifact_capability(
            result,
            kind=CodingArtifactKind.GRADER_BUNDLE,
            phase=CodingArtifactDeliveryPhase.AUTHORING,
        )
    assert "signature=secret" not in "".join(traceback.format_exception(captured.value))
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="projection"):
        project_coding_artifact_capability(
            result,
            kind=CodingArtifactKind.MEMORY_BUNDLE,
            phase=CodingArtifactDeliveryPhase.GRADING,
        )


async def test_capability_ttl_never_outlives_ticket() -> None:
    store = _Store()
    result = await _minter(store, maximum_ttl_seconds=600).mint(
        _lease(remaining_seconds=90)
    )
    assert result.expires_at == _NOW + timedelta(seconds=90)
    assert {ttl for _key, ttl in store.url_calls} == {90}


async def test_accepts_s3_legacy_absolute_expiry() -> None:
    result = await _minter(_Store(legacy_signature=True)).mint(_lease())
    assert result.expires_at == _NOW + timedelta(seconds=300)


async def test_nearly_expired_ticket_mints_nothing() -> None:
    store = _Store()
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="lifetime"):
        await _minter(store).mint(_lease(remaining_seconds=59))
    assert store.head_calls == []


@pytest.mark.parametrize(
    "store",
    [
        _Store(wrong_sha=True),
        _Store(wrong_kind=True),
        _Store(size_bytes=0),
        _Store(size_bytes=True),
        _Store(size_by_kind={"visible-bundle": (2 << 30) + 1}),
        _Store(size_by_kind={"memory-bundle": (64 << 20) + 1}),
        _Store(size_by_kind={"resource-profile": (4 << 20) + 1}),
        _Store(size_by_kind={"grader-bundle": (512 << 20) + 1}),
    ],
)
async def test_rejects_object_metadata_drift(store: _Store) -> None:
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="metadata"):
        await _minter(store).mint(_lease())
    assert store.url_calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://objects.example/object?signature=x",
        "https://user:pass@objects.example/object?signature=x",
        "https://objects.example/object#fragment",
        "https://evil.example/object?signature=x",
        "https://storage.example.com:8443/object?signature=x",
        "https://storage.example.com/private-coding/wrong?X-Amz-Signature=x",
        "https://storage.example.com/private-coding/wrong?not-a-signature=x",
        "https://storage.example.com/\n?Signature=x",
        "https://storage.example.com/" + "x" * (16 << 10) + "?Signature=x",
        "not-a-url",
    ],
)
async def test_rejects_unsafe_signed_urls(url: str) -> None:
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="URL|HTTPS"):
        await _minter(_Store(url=url)).mint(_lease())


@pytest.mark.parametrize(
    "url_template",
    [
        "https://storage.example.com/{key}",
        "https://private-coding.storage.example.com/private-coding/{key}",
    ],
)
async def test_rejects_mixed_s3_addressing_styles(url_template: str) -> None:
    lease = _lease()
    task = lease.run_manifest.tasks[0]
    key = coding_artifact_object_key(
        kind=CodingArtifactKind.VISIBLE_BUNDLE,
        sha256=task.visible_bundle_sha256,
    )
    url = (
        url_template.format(key=key)
        + f"?X-Amz-Date={_NOW:%Y%m%dT%H%M%SZ}"
        + "&X-Amz-Expires=300&X-Amz-Signature=secret"
    )
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="URL"):
        await _minter(_Store(url=url)).mint(lease)


async def test_rejects_stale_overlong_v4_signing_window() -> None:
    lease = _lease()
    task = lease.run_manifest.tasks[0]
    key = coding_artifact_object_key(
        kind=CodingArtifactKind.VISIBLE_BUNDLE,
        sha256=task.visible_bundle_sha256,
    )
    signed_at = _NOW - timedelta(seconds=300)
    url = (
        f"https://storage.example.com/private-coding/{key}"
        f"?X-Amz-Date={signed_at:%Y%m%dT%H%M%SZ}"
        "&X-Amz-Expires=600&X-Amz-Signature=secret"
    )
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="expiry"):
        await _minter(_Store(url=url)).mint(lease)


async def test_storage_failure_is_retryable_and_redacted() -> None:
    store = _Store(head_error=ObjectUploadFailedError("private/key?signature=secret"))
    with pytest.raises(CodingArtifactCapabilityUnavailableError) as captured:
        await _minter(store).mint(_lease())
    rendered = "".join(traceback.format_exception(captured.value))
    assert "private/key" not in rendered
    assert "signature=secret" not in rendered


async def test_rejects_signer_expiry_beyond_ticket() -> None:
    lease = _lease(remaining_seconds=600)
    task = lease.run_manifest.tasks[0]
    key = coding_artifact_object_key(
        kind=CodingArtifactKind.VISIBLE_BUNDLE,
        sha256=task.visible_bundle_sha256,
    )
    url = (
        f"https://storage.example.com/private-coding/{key}"
        f"?X-Amz-Date={_NOW:%Y%m%dT%H%M%SZ}"
        "&X-Amz-Expires=601&X-Amz-Signature=secret"
    )
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="expiry"):
        await _minter(_Store(url=url), maximum_ttl_seconds=900).mint(lease)


async def test_invalid_expiry_does_not_leak_signed_query_in_traceback() -> None:
    lease = _lease()
    task = lease.run_manifest.tasks[0]
    key = coding_artifact_object_key(
        kind=CodingArtifactKind.VISIBLE_BUNDLE,
        sha256=task.visible_bundle_sha256,
    )
    url = (
        f"https://storage.example.com/private-coding/{key}"
        f"?X-Amz-Date={_NOW:%Y%m%dT%H%M%SZ}"
        "&X-Amz-Expires=private-expiry&X-Amz-Signature=private-signature"
    )
    with pytest.raises(CodingArtifactCapabilityIntegrityError) as captured:
        await _minter(_Store(url=url)).mint(lease)
    rendered = "".join(traceback.format_exception(captured.value))
    assert "private-expiry" not in rendered
    assert "private-signature" not in rendered


@pytest.mark.parametrize(
    "signature_query",
    [
        (
            f"X-Amz-Date={_NOW:%Y%m%dT%H%M%SZ}&X-Amz-Expires=300"
            "&X-Amz-Signature=one&x-amz-signature=two"
        ),
        (
            f"X-Amz-Date={_NOW:%Y%m%dT%H%M%SZ}&X-Amz-Expires=300"
            "&X-Amz-Signature=v4&Expires=1787313900&Signature=v2"
        ),
    ],
)
async def test_rejects_ambiguous_signature_query(signature_query: str) -> None:
    lease = _lease()
    task = lease.run_manifest.tasks[0]
    key = coding_artifact_object_key(
        kind=CodingArtifactKind.VISIBLE_BUNDLE,
        sha256=task.visible_bundle_sha256,
    )
    url = f"https://storage.example.com/private-coding/{key}?{signature_query}"
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="expiry"):
        await _minter(_Store(url=url)).mint(lease)


async def test_signing_failure_is_retryable_and_redacted() -> None:
    store = _Store(url_error=ObjectUploadFailedError("private/key?signature=secret"))
    with pytest.raises(CodingArtifactCapabilityUnavailableError) as captured:
        await _minter(store).mint(_lease())
    assert len(store.head_calls) == 4
    assert len(store.url_calls) == 1
    rendered = "".join(traceback.format_exception(captured.value))
    assert "private/key" not in rendered
    assert "signature=secret" not in rendered


def test_object_key_rejects_non_sha256_input() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        coding_artifact_object_key(
            kind=CodingArtifactKind.VISIBLE_BUNDLE,
            sha256="../private",
        )


@pytest.mark.parametrize("maximum_ttl_seconds", [True, 59, 901, 60.5])
def test_policy_rejects_unsafe_ttl(maximum_ttl_seconds: object) -> None:
    with pytest.raises(ValueError, match="TTL"):
        CodingArtifactCapabilityPolicy(
            maximum_ttl_seconds=maximum_ttl_seconds  # type: ignore[arg-type]
        )


async def test_rejects_task_set_digest_drift_before_storage_access() -> None:
    lease = _lease()
    lease = replace(
        lease,
        run_manifest=lease.run_manifest.model_copy(
            update={"task_set_manifest_sha256": "ff" * 32}
        ),
    )
    store = _Store()
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="manifest"):
        await _minter(store).mint(lease)
    assert store.head_calls == []


async def test_rejects_not_yet_issued_lease_before_storage_access() -> None:
    lease = _lease()
    lease = replace(lease, issued_at=_NOW + timedelta(seconds=1))
    store = _Store()
    with pytest.raises(CodingArtifactCapabilityIntegrityError, match="time"):
        await _minter(store).mint(lease)
    assert store.head_calls == []
