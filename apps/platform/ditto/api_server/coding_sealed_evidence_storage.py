"""Default-off S3 capability minting for sealed shadow-coding evidence.

The database reservation remains the authority.  This module derives one
content-addressed key and short-lived PUT URL from that reservation, but has no
HTTP route and does not persist a bucket URL or object key.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse
from uuid import UUID

from pydantic import ValidationError

from ditto.api_models.coding_evidence_upload import (
    CODING_SEALED_EVIDENCE_MAX_BYTES,
    CodingSealedEvidenceKind,
    CodingSealedEvidenceUploadCapability,
)
from ditto.api_server.coding_artifact_capabilities import (
    CodingArtifactCapabilityIntegrityError,
    _validate_signed_url,
)
from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
    ObjectUploadFailedError,
)
from ditto.api_server.storage.models import (
    ObjectMetadata,
    StorageConfig,
    VerifiedObject,
)
from ditto.db.models import CodingSealedEvidenceUpload

_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_PREFIX = "DITTO_CODING_EVIDENCE_STORAGE_"
_OBJECT_PREFIX = "coding-evidence/v1"
_MAX_CAPABILITY_SECONDS = 300
_MIN_CAPABILITY_SECONDS = 60
_DEFAULT_TIMEOUT_SECONDS = 10.0


class CodingSealedEvidenceStorageConfigurationError(ApiServerConfigError):
    """The optional sealed-evidence S3 authority is unsafe or incomplete."""


class CodingSealedEvidenceStorageUnavailableError(Exception):
    """The dedicated object-store signer is temporarily unavailable."""


class CodingSealedEvidenceStorageIntegrityError(Exception):
    """The reservation, key, clock, or returned PUT URL is incoherent."""


class CodingSealedEvidenceObjectStore(Protocol):
    async def presigned_put_url(
        self,
        *,
        key: str,
        size_bytes: int,
        metadata: dict[str, str],
        content_type: str,
        checksum_sha256_b64: str,
        expires_in: int,
    ) -> str:
        """Mint one short-lived PUT capability for an exact immutable object."""

    async def head_object(self, *, key: str) -> ObjectMetadata:
        """Read bounded metadata for one exact evidence key."""

    async def verify_object_sha256(
        self, *, key: str, expected_size_bytes: int
    ) -> VerifiedObject:
        """Stream and hash the entire expected object."""


@dataclass(frozen=True)
class CodingSealedEvidenceVerifiedObject:
    upload_id: UUID
    ticket_id: UUID
    claim_generation: int
    evidence_kind: CodingSealedEvidenceKind
    sha256: str
    size_bytes: int


@dataclass(frozen=True, repr=False)
class CodingSealedEvidenceStorageConfig:
    """Separate least-privilege credentials for sealed evidence bytes only."""

    endpoint_url: str = field(repr=False)
    bucket: str = field(repr=False)
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = "us-east-1"
    use_tls: bool = True
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        try:
            parsed = urlparse(self.endpoint_url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise CodingSealedEvidenceStorageConfigurationError(
                "sealed coding evidence endpoint must be an origin URL"
            ) from error
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or (port is not None and not 1 <= port <= 65_535)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise CodingSealedEvidenceStorageConfigurationError(
                "sealed coding evidence endpoint must be an origin URL"
            )
        if parsed.scheme == "http" and not _loopback(hostname):
            raise CodingSealedEvidenceStorageConfigurationError(
                "sealed coding evidence permits HTTP only on loopback"
            )
        if self.use_tls != (parsed.scheme == "https"):
            raise CodingSealedEvidenceStorageConfigurationError(
                "sealed coding evidence TLS flag must match the endpoint scheme"
            )
        if _BUCKET.fullmatch(self.bucket) is None:
            raise CodingSealedEvidenceStorageConfigurationError(
                "sealed coding evidence bucket is outside S3-compatible bounds"
            )
        if any(
            not _safe_scalar(value, maximum_bytes=4096)
            for value in (self.access_key, self.secret_key)
        ) or not _safe_scalar(self.region, maximum_bytes=128):
            raise CodingSealedEvidenceStorageConfigurationError(
                "sealed coding evidence credentials are outside safe bounds"
            )
        if not 0.1 <= self.timeout_seconds <= 60.0:
            raise CodingSealedEvidenceStorageConfigurationError(
                "sealed coding evidence timeout must be between 0.1 and 60 seconds"
            )

    def __repr__(self) -> str:
        return (
            "CodingSealedEvidenceStorageConfig(configured=True, "
            f"region={self.region!r}, use_tls={self.use_tls!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )

    def storage_config(self) -> StorageConfig:
        return StorageConfig(
            endpoint_url=self.endpoint_url,
            bucket=self.bucket,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            use_tls=self.use_tls,
        )


def parse_coding_sealed_evidence_storage_config_from_env(
    environ: Mapping[str, str] | None = None,
) -> CodingSealedEvidenceStorageConfig | None:
    """Resolve the optional dedicated evidence store; absence is disabled."""

    import os

    values = os.environ if environ is None else environ
    required_names = tuple(
        f"{_CONFIG_PREFIX}{suffix}"
        for suffix in ("ENDPOINT_URL", "BUCKET", "ACCESS_KEY", "SECRET_KEY")
    )
    optional_names = (
        f"{_CONFIG_PREFIX}REGION",
        f"{_CONFIG_PREFIX}USE_TLS",
        "DITTO_CODING_EVIDENCE_TIMEOUT_SECONDS",
    )
    configured = [
        name for name in (*required_names, *optional_names) if values.get(name)
    ]
    if not configured:
        return None
    missing = [name for name in required_names if not values.get(name)]
    if missing:
        raise CodingSealedEvidenceStorageConfigurationError(
            "sealed coding evidence configuration is incomplete: " + ", ".join(missing)
        )
    try:
        use_tls = _parse_bool(
            f"{_CONFIG_PREFIX}USE_TLS",
            values.get(f"{_CONFIG_PREFIX}USE_TLS", "true"),
        )
        timeout_seconds = float(
            values.get(
                "DITTO_CODING_EVIDENCE_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS
            )
        )
    except ValueError as error:
        raise CodingSealedEvidenceStorageConfigurationError(
            "sealed coding evidence timeout or TLS flag is malformed"
        ) from error
    return CodingSealedEvidenceStorageConfig(
        endpoint_url=values[required_names[0]],
        bucket=values[required_names[1]],
        access_key=values[required_names[2]],
        secret_key=values[required_names[3]],
        region=values.get(f"{_CONFIG_PREFIX}REGION", "us-east-1"),
        use_tls=use_tls,
        timeout_seconds=timeout_seconds,
    )


def coding_sealed_evidence_object_key(
    *, evidence_kind: CodingSealedEvidenceKind, sha256: str
) -> str:
    """Return the only physical key shape for a sealed evidence identity."""

    if (
        not isinstance(evidence_kind, CodingSealedEvidenceKind)
        or _SHA256.fullmatch(sha256) is None
    ):
        raise ValueError("sealed coding evidence identity is invalid")
    return f"{_OBJECT_PREFIX}/{evidence_kind.value}/sha256/{sha256}"


class CodingSealedEvidenceCapabilityMinter:
    """Mint a redacted short-lived PUT capability from an existing reservation."""

    def __init__(
        self,
        config: CodingSealedEvidenceStorageConfig,
        *,
        object_store: CodingSealedEvidenceObjectStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._store = object_store or S3StorageClient(config.storage_config())
        self._clock = clock or (lambda: datetime.now(UTC))

    async def mint(
        self,
        upload: CodingSealedEvidenceUpload,
        *,
        ticket_deadline: datetime,
        claim_expires_at: datetime,
    ) -> CodingSealedEvidenceUploadCapability:
        """Return one exact PUT capability without storing its bearer URL."""

        now = _aware(self._clock())
        ticket_deadline = _aware(ticket_deadline)
        claim_expires_at = _aware(claim_expires_at)
        kind = _validate_reservation(upload)
        if ticket_deadline <= now or claim_expires_at <= now:
            raise CodingSealedEvidenceStorageIntegrityError(
                "sealed coding evidence reservation authority is invalid"
            )
        authority_deadline = min(ticket_deadline, claim_expires_at)
        remaining = int((authority_deadline - now).total_seconds())
        ttl = min(_MAX_CAPABILITY_SECONDS, remaining)
        if ttl < _MIN_CAPABILITY_SECONDS:
            raise CodingSealedEvidenceStorageIntegrityError(
                "sealed coding evidence claim has insufficient lifetime"
            )
        key = coding_sealed_evidence_object_key(
            evidence_kind=kind,
            sha256=upload.sha256,
        )
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                url = await self._store.presigned_put_url(
                    key=key,
                    size_bytes=upload.size_bytes,
                    metadata={"sha256": upload.sha256, "evidence-kind": kind.value},
                    content_type=upload.content_type,
                    checksum_sha256_b64=base64.b64encode(
                        bytes.fromhex(upload.sha256)
                    ).decode("ascii"),
                    expires_in=ttl,
                )
        except TimeoutError:
            raise CodingSealedEvidenceStorageUnavailableError(
                "sealed coding evidence capability mint timed out"
            ) from None
        except ObjectUploadFailedError:
            raise CodingSealedEvidenceStorageUnavailableError(
                "sealed coding evidence capability signer is unavailable"
            ) from None
        validated_at = _aware(self._clock())
        try:
            expires_at = _validate_signed_url(
                url,
                endpoint_url=self._config.endpoint_url,
                bucket=self._config.bucket,
                key=key,
                requested_ttl=ttl,
                signing_started_at=now,
                validated_at=validated_at,
                ticket_deadline=authority_deadline,
            )
            return CodingSealedEvidenceUploadCapability(
                schema="dittobench-coding-sealed-evidence-upload-capability-v1",
                coding_contract_version=1,
                weight_eligible=False,
                ticket_id=upload.ticket_id,
                claim_generation=upload.claim_generation,
                ticket_deadline=ticket_deadline,
                upload_id=upload.upload_id,
                evidence_kind=kind,
                sha256=upload.sha256,
                size_bytes=upload.size_bytes,
                content_type="application/octet-stream",
                checksum_sha256_b64=base64.b64encode(
                    bytes.fromhex(upload.sha256)
                ).decode("ascii"),
                url=url,
                expires_at=expires_at,
            )
        except (
            CodingArtifactCapabilityIntegrityError,
            ValidationError,
            ValueError,
        ) as error:
            raise CodingSealedEvidenceStorageIntegrityError(
                "sealed coding evidence signer returned invalid authority"
            ) from error

    async def verify(
        self,
        upload: CodingSealedEvidenceUpload,
    ) -> CodingSealedEvidenceVerifiedObject:
        """Verify metadata, exact size, and full object SHA-256 before finalizing."""

        kind = _validate_reservation(upload)
        key = coding_sealed_evidence_object_key(
            evidence_kind=kind,
            sha256=upload.sha256,
        )
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                metadata = await self._store.head_object(key=key)
        except TimeoutError:
            raise CodingSealedEvidenceStorageUnavailableError(
                "sealed coding evidence metadata verification timed out"
            ) from None
        except (ObjectNotFoundError, ObjectUploadFailedError):
            raise CodingSealedEvidenceStorageUnavailableError(
                "sealed coding evidence object is unavailable"
            ) from None
        expected_metadata = {
            "sha256": upload.sha256,
            "evidence-kind": kind.value,
        }
        if (
            metadata.size_bytes != upload.size_bytes
            or metadata.content_type != "application/octet-stream"
            or metadata.metadata != expected_metadata
        ):
            raise CodingSealedEvidenceStorageIntegrityError(
                "sealed coding evidence metadata disagrees with reservation"
            )
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                verified = await self._store.verify_object_sha256(
                    key=key,
                    expected_size_bytes=upload.size_bytes,
                )
        except TimeoutError:
            raise CodingSealedEvidenceStorageUnavailableError(
                "sealed coding evidence full verification timed out"
            ) from None
        except (ObjectDownloadFailedError, ObjectUploadFailedError):
            raise CodingSealedEvidenceStorageUnavailableError(
                "sealed coding evidence object verification failed"
            ) from None
        if verified.size_bytes != upload.size_bytes or verified.sha256 != upload.sha256:
            raise CodingSealedEvidenceStorageIntegrityError(
                "sealed coding evidence bytes disagree with reservation"
            )
        return CodingSealedEvidenceVerifiedObject(
            upload_id=upload.upload_id,
            ticket_id=upload.ticket_id,
            claim_generation=upload.claim_generation,
            evidence_kind=kind,
            sha256=upload.sha256,
            size_bytes=upload.size_bytes,
        )


def _reservation_kind(upload: CodingSealedEvidenceUpload) -> CodingSealedEvidenceKind:
    try:
        return CodingSealedEvidenceKind(upload.evidence_kind)
    except (TypeError, ValueError) as error:
        raise CodingSealedEvidenceStorageIntegrityError(
            "sealed coding evidence kind is invalid"
        ) from error


def _validate_reservation(
    upload: CodingSealedEvidenceUpload,
) -> CodingSealedEvidenceKind:
    kind = _reservation_kind(upload)
    if (
        upload.upload_id.int == 0
        or upload.ticket_id.int == 0
        or upload.claim_generation < 1
        or upload.claim_generation > (1 << 31) - 1
        or upload.weight_eligible
        or upload.content_type != "application/octet-stream"
        or _SHA256.fullmatch(upload.sha256) is None
        or isinstance(upload.size_bytes, bool)
        or not 1 <= upload.size_bytes <= CODING_SEALED_EVIDENCE_MAX_BYTES[kind]
    ):
        raise CodingSealedEvidenceStorageIntegrityError(
            "sealed coding evidence reservation authority is invalid"
        )
    return kind


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CodingSealedEvidenceStorageIntegrityError(
            "sealed coding evidence time must be timezone-aware"
        )
    return value.astimezone(UTC)


def _loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _safe_scalar(value: str, *, maximum_bytes: int) -> bool:
    return (
        bool(value)
        and len(value.encode()) <= maximum_bytes
        and not any(
            character.isspace() or unicodedata.category(character) == "Cc"
            for character in value
        )
    )


def _parse_bool(name: str, raw: str) -> bool:
    if raw.lower() in {"true", "1", "yes", "on"}:
        return True
    if raw.lower() in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


__all__ = [
    "CodingSealedEvidenceCapabilityMinter",
    "CodingSealedEvidenceStorageConfig",
    "CodingSealedEvidenceStorageConfigurationError",
    "CodingSealedEvidenceStorageIntegrityError",
    "CodingSealedEvidenceStorageUnavailableError",
    "CodingSealedEvidenceVerifiedObject",
    "coding_sealed_evidence_object_key",
    "parse_coding_sealed_evidence_storage_config_from_env",
]
