"""Ticket-lifetime-bounded capabilities for selected coding artifacts."""

from __future__ import annotations

import asyncio
import ipaddress
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from pydantic import ValidationError

from ditto.api_models.coding_artifacts import (
    CODING_ARTIFACT_AUDIENCE,
    CODING_ARTIFACT_MAX_BYTES,
    CodingArtifactCapabilityEnvelope,
    CodingArtifactDeliveryPhase,
    CodingArtifactKind,
)
from ditto.api_models.coding_selection import coding_task_set_manifest_digest
from ditto.api_server.coding_private_catalog import CodingPrivateCatalogConfig
from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.storage.errors import ObjectUploadFailedError
from ditto.api_server.storage.models import ObjectMetadata
from ditto.db.queries.coding_task_leases import CodingShadowTaskLeaseCore

_OBJECT_PREFIX = "coding-artifacts/v1"
_MIN_CAPABILITY_SECONDS = 60
_MAX_SIGNED_URL_BYTES = 16 << 10


class CodingArtifactCapabilityUnavailableError(Exception):
    """Private object metadata or URL signing is temporarily unavailable."""


class CodingArtifactCapabilityIntegrityError(Exception):
    """Lease, object metadata, size, key, or signed URL authority is invalid."""


class CodingArtifactObjectStore(Protocol):
    async def head_object(self, *, key: str) -> ObjectMetadata:
        """Return selected object metadata without downloading it."""

    async def presigned_get_url(
        self,
        *,
        key: str,
        expires_in: int = 300,
        attachment_filename: str | None = None,
    ) -> str:
        """Mint a short-lived GET capability for one exact object."""


@dataclass(frozen=True)
class CodingArtifactCapabilityPolicy:
    maximum_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_ttl_seconds, bool)
            or not isinstance(self.maximum_ttl_seconds, int)
            or not _MIN_CAPABILITY_SECONDS <= self.maximum_ttl_seconds <= 900
        ):
            raise ValueError("coding artifact capability TTL must be in [60, 900]")


@dataclass(frozen=True)
class CodingArtifactCapability:
    """One server-internal capability; ``url`` is bearer-secret material."""

    kind: CodingArtifactKind
    sha256: str
    size_bytes: int
    expires_at: datetime
    url: str = field(repr=False)


@dataclass(frozen=True)
class CodingArtifactCapabilitySet:
    """Server-internal set that must be projected by artifact audience."""

    ticket_id: UUID
    run_row_id: UUID
    validator_hotkey: str
    ticket_deadline: datetime
    expires_at: datetime
    capabilities: tuple[
        CodingArtifactCapability,
        CodingArtifactCapability,
        CodingArtifactCapability,
        CodingArtifactCapability,
    ]
    weight_eligible: Literal[False] = False


_DEFAULT_CAPABILITY_POLICY = CodingArtifactCapabilityPolicy()


def coding_artifact_object_key(*, kind: CodingArtifactKind, sha256: str) -> str:
    if len(sha256) != 64:
        raise ValueError("coding artifact digest must be SHA-256")
    try:
        raw = bytes.fromhex(sha256)
    except ValueError as error:
        raise ValueError("coding artifact digest must be lowercase SHA-256") from error
    if len(raw) != 32 or sha256 != sha256.lower():
        raise ValueError("coding artifact digest must be lowercase SHA-256")
    return f"{_OBJECT_PREFIX}/{kind.value}/sha256/{sha256}"


class CodingArtifactCapabilityMinter:
    def __init__(
        self,
        config: CodingPrivateCatalogConfig,
        *,
        object_store: CodingArtifactObjectStore | None = None,
        clock: Callable[[], datetime] | None = None,
        policy: CodingArtifactCapabilityPolicy = _DEFAULT_CAPABILITY_POLICY,
    ) -> None:
        self._config = config
        self._store = object_store or S3StorageClient(config.storage_config())
        self._clock = clock or (lambda: datetime.now(UTC))
        self._policy = policy

    async def mint(
        self,
        lease: CodingShadowTaskLeaseCore,
    ) -> CodingArtifactCapabilitySet:
        now = _aware(self._clock())
        deadline = _aware(lease.deadline)
        remaining = math.floor((deadline - now).total_seconds())
        if lease.weight_eligible is not False:
            raise CodingArtifactCapabilityIntegrityError(
                "coding artifact capabilities require shadow-only lease authority"
            )
        issued_at = _aware(lease.issued_at)
        if issued_at > now or deadline <= issued_at:
            raise CodingArtifactCapabilityIntegrityError(
                "coding artifact capability lease time is invalid"
            )
        if remaining < _MIN_CAPABILITY_SECONDS:
            raise CodingArtifactCapabilityIntegrityError(
                "coding ticket has insufficient lifetime for artifact capabilities"
            )
        if (
            len(lease.run_manifest.tasks) != 1
            or len(lease.task_set_manifest.tasks) != 1
        ):
            raise CodingArtifactCapabilityIntegrityError(
                "coding contract v1 capabilities require exactly one task"
            )
        manifest_task = lease.run_manifest.tasks[0]
        selected_task = lease.task_set_manifest.tasks[0]
        task_set_digest = coding_task_set_manifest_digest(lease.task_set_manifest)
        if (
            selected_task.task != manifest_task
            or selected_task.repository_epoch != lease.repository_epoch
            or lease.task_set_manifest.coding_run_id != lease.run_manifest.coding_run_id
            or task_set_digest != lease.run_manifest.task_set_manifest_sha256
            or lease.run_manifest.task_set_id != f"coding-task-set-v1-{task_set_digest}"
        ):
            raise CodingArtifactCapabilityIntegrityError(
                "coding lease task material disagrees with shared manifest"
            )
        identities = (
            (CodingArtifactKind.VISIBLE_BUNDLE, manifest_task.visible_bundle_sha256),
            (CodingArtifactKind.MEMORY_BUNDLE, manifest_task.memory_bundle_sha256),
            (
                CodingArtifactKind.RESOURCE_PROFILE,
                manifest_task.resource_profile_sha256,
            ),
            (CodingArtifactKind.GRADER_BUNDLE, manifest_task.grader_bundle_sha256),
        )
        verified: list[tuple[CodingArtifactKind, str, str, ObjectMetadata]] = []
        for kind, digest in identities:
            key = coding_artifact_object_key(kind=kind, sha256=digest)
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    metadata = await self._store.head_object(key=key)
            except TimeoutError:
                raise CodingArtifactCapabilityUnavailableError(
                    "coding artifact metadata verification timed out"
                ) from None
            except ObjectUploadFailedError:
                raise CodingArtifactCapabilityUnavailableError(
                    "coding artifact capability source is unavailable"
                ) from None
            if (
                isinstance(metadata.size_bytes, bool)
                or not isinstance(metadata.size_bytes, int)
                or metadata.size_bytes < 1
                or metadata.size_bytes > CODING_ARTIFACT_MAX_BYTES[kind]
                or metadata.metadata.get("sha256") != digest
                or metadata.metadata.get("artifact-kind") != kind.value
            ):
                raise CodingArtifactCapabilityIntegrityError(
                    "coding artifact metadata disagrees with lease authority"
                )
            verified.append((kind, digest, key, metadata))

        capabilities: list[CodingArtifactCapability] = []
        for kind, digest, key, metadata in verified:
            signing_started_at = _aware(self._clock())
            remaining = math.floor((deadline - signing_started_at).total_seconds())
            ttl = min(self._policy.maximum_ttl_seconds, remaining)
            if ttl < _MIN_CAPABILITY_SECONDS:
                raise CodingArtifactCapabilityIntegrityError(
                    "coding ticket has insufficient lifetime for artifact capabilities"
                )
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    url = await self._store.presigned_get_url(
                        key=key,
                        expires_in=ttl,
                    )
            except TimeoutError:
                raise CodingArtifactCapabilityUnavailableError(
                    "coding artifact capability mint timed out"
                ) from None
            except ObjectUploadFailedError:
                raise CodingArtifactCapabilityUnavailableError(
                    "coding artifact capability signer is unavailable"
                ) from None
            validated_at = _aware(self._clock())
            expires_at = _validate_signed_url(
                url,
                endpoint_url=self._config.endpoint_url,
                bucket=self._config.bucket,
                key=key,
                requested_ttl=ttl,
                validated_at=validated_at,
                ticket_deadline=deadline,
            )
            capabilities.append(
                CodingArtifactCapability(
                    kind=kind,
                    sha256=digest,
                    size_bytes=metadata.size_bytes,
                    expires_at=expires_at,
                    url=url,
                )
            )
        if len(capabilities) != 4:  # pragma: no cover - fixed tuple invariant
            raise RuntimeError("coding artifact capability set is incomplete")
        capability_set_expires_at = min(
            capability.expires_at for capability in capabilities
        )
        completed_at = _aware(self._clock())
        if (
            math.floor((capability_set_expires_at - completed_at).total_seconds())
            < _MIN_CAPABILITY_SECONDS
        ):
            raise CodingArtifactCapabilityIntegrityError(
                "coding ticket has insufficient lifetime for artifact capabilities"
            )
        return CodingArtifactCapabilitySet(
            ticket_id=lease.ticket_id,
            run_row_id=lease.run_row_id,
            validator_hotkey=lease.validator_hotkey,
            ticket_deadline=deadline,
            expires_at=capability_set_expires_at,
            capabilities=(
                capabilities[0],
                capabilities[1],
                capabilities[2],
                capabilities[3],
            ),
        )


def project_coding_artifact_capability(
    capability_set: CodingArtifactCapabilitySet,
    *,
    kind: CodingArtifactKind,
    phase: CodingArtifactDeliveryPhase,
) -> CodingArtifactCapabilityEnvelope:
    """Project one bearer URL without serializing the complete capability set."""

    selected = [
        capability
        for capability in capability_set.capabilities
        if capability.kind is kind
    ]
    if len(selected) != 1:
        raise CodingArtifactCapabilityIntegrityError(
            "coding artifact capability set lacks one exact kind"
        )
    capability = selected[0]
    try:
        return CodingArtifactCapabilityEnvelope(
            schema="dittobench-coding-artifact-capability-v1",
            coding_contract_version=1,
            weight_eligible=False,
            ticket_id=capability_set.ticket_id,
            ticket_deadline=capability_set.ticket_deadline,
            delivery_phase=phase,
            artifact_kind=kind,
            audience=CODING_ARTIFACT_AUDIENCE[kind],
            sha256=capability.sha256,
            size_bytes=capability.size_bytes,
            url=capability.url,
            expires_at=capability.expires_at,
        )
    except ValidationError:
        raise CodingArtifactCapabilityIntegrityError(
            "coding artifact delivery projection is invalid"
        ) from None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CodingArtifactCapabilityIntegrityError(
            "coding artifact capability time must be timezone-aware"
        )
    return value.astimezone(UTC)


def _validate_signed_url(
    value: str,
    *,
    endpoint_url: str,
    bucket: str,
    key: str,
    requested_ttl: int,
    validated_at: datetime,
    ticket_deadline: datetime,
) -> datetime:
    try:
        if (
            not isinstance(value, str)
            or not 1 <= len(value.encode("utf-8")) <= _MAX_SIGNED_URL_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError
        parsed = urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
        expected = urlparse(endpoint_url)
        expected_hostname = expected.hostname
        expected_port = expected.port or (443 if expected.scheme == "https" else 80)
        actual_port = port or (443 if parsed.scheme == "https" else 80)
        query: dict[str, list[str]] = {}
        for name, values in parse_qs(
            parsed.query,
            max_num_fields=64,
        ).items():
            query.setdefault(name.lower(), []).extend(values)
    except ValueError:
        raise CodingArtifactCapabilityIntegrityError(
            "coding artifact signer returned an invalid URL"
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or (port is not None and not 1 <= port <= 65_535)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not any(
            query.get(signature_name)
            for signature_name in ("x-amz-signature", "signature")
        )
        or not expected_hostname
        or parsed.scheme != expected.scheme
        or actual_port != expected_port
        or hostname not in {expected_hostname, f"{bucket.lower()}.{expected_hostname}"}
        or parsed.path
        not in {
            f"/{bucket}/{key}",
            f"/{key}",
        }
    ):
        raise CodingArtifactCapabilityIntegrityError(
            "coding artifact signer returned an invalid URL"
        )
    if parsed.scheme == "http" and not _loopback(hostname):
        raise CodingArtifactCapabilityIntegrityError(
            "coding artifact capability must use HTTPS outside loopback"
        )
    expires_at = _signed_url_expiry(query)
    remaining = math.floor((expires_at - validated_at).total_seconds())
    if (
        remaining < _MIN_CAPABILITY_SECONDS
        or expires_at > ticket_deadline
        or expires_at > validated_at + timedelta(seconds=requested_ttl)
    ):
        raise CodingArtifactCapabilityIntegrityError(
            "coding artifact signer returned an invalid expiry"
        )
    return expires_at


def _signed_url_expiry(query: dict[str, list[str]]) -> datetime:
    try:
        has_v4_signature = "x-amz-signature" in query
        has_v2_signature = "signature" in query
        if has_v4_signature == has_v2_signature:
            raise ValueError
        if has_v4_signature:
            signature_values = query["x-amz-signature"]
            signed_at_values = query["x-amz-date"]
            duration_values = query["x-amz-expires"]
            if (
                len(signature_values) != 1
                or len(signed_at_values) != 1
                or len(duration_values) != 1
            ):
                raise ValueError
            signed_at = datetime.strptime(
                signed_at_values[0], "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=UTC)
            duration = int(duration_values[0])
            if duration < 1:
                raise ValueError
            return signed_at + timedelta(seconds=duration)
        signature_values = query["signature"]
        expiry_values = query["expires"]
        if len(signature_values) != 1 or len(expiry_values) != 1:
            raise ValueError
        return datetime.fromtimestamp(int(expiry_values[0]), tz=UTC)
    except (KeyError, OverflowError, ValueError):
        raise CodingArtifactCapabilityIntegrityError(
            "coding artifact signer returned an invalid expiry"
        ) from None


def _loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
