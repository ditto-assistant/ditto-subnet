"""Bounded, content-addressed transport for private coding-catalog records."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import ValidationError

from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import (
    CodingCatalogMembershipProof,
    CodingCatalogTaskVersion,
    CodingPrivateCatalogRecord,
)
from ditto.api_server.errors import ApiServerConfigError
from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectDownloadTooLargeError,
)
from ditto.api_server.storage.models import StorageConfig
from ditto.coding_selection import (
    CodingSelectionCatalogIntegrityError,
    CodingSelectionCatalogUnavailableError,
    verify_coding_catalog_membership,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_OBJECT_PREFIX = "coding-catalog/v1"
_DEFAULT_MAX_RECORD_BYTES = 64 << 10
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_JSON_DEPTH = 32
_MAX_CATALOG_INDEX = 999_999
_CONFIG_PREFIX = "DITTO_CODING_CATALOG_STORAGE_"


class CodingPrivateCatalogConfigurationError(ApiServerConfigError):
    """The optional private catalog store is partially or unsafely configured."""


class _BoundedObjectReader(Protocol):
    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        """Return exactly one object without listing its surrounding namespace."""


@dataclass(frozen=True, repr=False)
class CodingPrivateCatalogConfig:
    """Separate least-privilege credentials for the private coding catalog."""

    endpoint_url: str = field(repr=False)
    bucket: str = field(repr=False)
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str = "us-east-1"
    use_tls: bool = True
    max_record_bytes: int = _DEFAULT_MAX_RECORD_BYTES
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        try:
            parsed = urlparse(self.endpoint_url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog endpoint must be an origin URL"
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
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog endpoint must be an origin URL"
            )
        if parsed.scheme == "http" and not _is_loopback_host(hostname):
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog permits plaintext HTTP only on loopback"
            )
        if self.use_tls != (parsed.scheme == "https"):
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog TLS flag must match the endpoint scheme"
            )
        if _BUCKET.fullmatch(self.bucket) is None:
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog bucket is outside S3-compatible bounds"
            )
        credentials = (self.access_key, self.secret_key)
        if any(
            not _safe_scalar(credential, maximum_bytes=4096)
            for credential in credentials
        ):
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog credentials are outside safe bounds"
            )
        if not _safe_scalar(self.region, maximum_bytes=128):
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog region is outside safe bounds"
            )
        if not 4 << 10 <= self.max_record_bytes <= 1 << 20:
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog record bound must be between 4 KiB and 1 MiB"
            )
        if not 0.1 <= self.timeout_seconds <= 60.0:
            raise CodingPrivateCatalogConfigurationError(
                "private coding catalog timeout must be between 0.1 and 60 seconds"
            )

    def __repr__(self) -> str:
        return (
            "CodingPrivateCatalogConfig(configured=True, "
            f"region={self.region!r}, use_tls={self.use_tls!r}, "
            f"max_record_bytes={self.max_record_bytes!r}, "
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


def parse_coding_private_catalog_config_from_env(
    environ: Mapping[str, str] | None = None,
) -> CodingPrivateCatalogConfig | None:
    """Resolve the optional private catalog store; absence disables it."""

    import os

    values = os.environ if environ is None else environ
    required_names = (
        f"{_CONFIG_PREFIX}ENDPOINT_URL",
        f"{_CONFIG_PREFIX}BUCKET",
        f"{_CONFIG_PREFIX}ACCESS_KEY",
        f"{_CONFIG_PREFIX}SECRET_KEY",
    )
    optional_names = (
        f"{_CONFIG_PREFIX}REGION",
        f"{_CONFIG_PREFIX}USE_TLS",
        "DITTO_CODING_CATALOG_MAX_RECORD_BYTES",
        "DITTO_CODING_CATALOG_TIMEOUT_SECONDS",
    )
    configured = [
        name for name in (*required_names, *optional_names) if values.get(name)
    ]
    if not configured:
        return None
    missing = [name for name in required_names if not values.get(name)]
    if missing:
        raise CodingPrivateCatalogConfigurationError(
            "private coding catalog configuration is incomplete: " + ", ".join(missing)
        )
    try:
        max_record_bytes = int(
            values.get(
                "DITTO_CODING_CATALOG_MAX_RECORD_BYTES",
                str(_DEFAULT_MAX_RECORD_BYTES),
            )
        )
        timeout_seconds = float(
            values.get(
                "DITTO_CODING_CATALOG_TIMEOUT_SECONDS",
                str(_DEFAULT_TIMEOUT_SECONDS),
            )
        )
        use_tls = _parse_bool(
            f"{_CONFIG_PREFIX}USE_TLS",
            values.get(f"{_CONFIG_PREFIX}USE_TLS", "true"),
        )
    except ValueError as error:
        raise CodingPrivateCatalogConfigurationError(
            "private coding catalog limits or TLS flag are malformed"
        ) from error
    return CodingPrivateCatalogConfig(
        endpoint_url=values[required_names[0]],
        bucket=values[required_names[1]],
        access_key=values[required_names[2]],
        secret_key=values[required_names[3]],
        region=values.get(f"{_CONFIG_PREFIX}REGION", "us-east-1"),
        use_tls=use_tls,
        max_record_bytes=max_record_bytes,
        timeout_seconds=timeout_seconds,
    )


def coding_private_catalog_record_key(
    *, catalog_commitment_sha256: str, catalog_index: int
) -> str:
    """Return the only object key shape accepted by the catalog reader."""

    if _SHA256.fullmatch(catalog_commitment_sha256) is None:
        raise ValueError("catalog commitment must be lowercase SHA-256")
    if (
        isinstance(catalog_index, bool)
        or not isinstance(catalog_index, int)
        or not 0 <= catalog_index <= _MAX_CATALOG_INDEX
    ):
        raise ValueError("catalog index is outside coding contract bounds")
    return (
        f"{_OBJECT_PREFIX}/{catalog_commitment_sha256}/records/{catalog_index:06d}.json"
    )


class S3CodingPrivateCatalogSource:
    """Read one selected record without exposing or enumerating the catalog."""

    def __init__(
        self,
        config: CodingPrivateCatalogConfig,
        *,
        object_reader: _BoundedObjectReader | None = None,
    ) -> None:
        self._config = config
        self._object_reader = object_reader or S3StorageClient(config.storage_config())

    async def get_task_version(
        self,
        *,
        commitment: CodingCatalogCommitment,
        catalog_index: int,
    ) -> tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]:
        try:
            commitment = CodingCatalogCommitment.model_validate_json(
                commitment.model_dump_json(by_alias=True)
            )
            key = coding_private_catalog_record_key(
                catalog_commitment_sha256=commitment.commitment_sha256,
                catalog_index=catalog_index,
            )
        except (ValueError, ValidationError) as error:
            raise CodingSelectionCatalogIntegrityError(
                "private catalog lookup authority is malformed"
            ) from error
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                body = await self._object_reader.get_object(
                    key=key,
                    max_bytes=self._config.max_record_bytes,
                )
        except ObjectDownloadTooLargeError as error:
            raise CodingSelectionCatalogIntegrityError(
                "private catalog record exceeds its byte bound"
            ) from error
        except TimeoutError as error:
            raise CodingSelectionCatalogUnavailableError(
                "private catalog record lookup timed out"
            ) from error
        except ObjectDownloadFailedError as error:
            raise CodingSelectionCatalogUnavailableError(
                "private catalog record is unavailable"
            ) from error
        except Exception as error:
            raise CodingSelectionCatalogUnavailableError(
                "private catalog record lookup failed"
            ) from error
        if not isinstance(body, bytes) or len(body) > self._config.max_record_bytes:
            raise CodingSelectionCatalogIntegrityError(
                "private catalog reader violated its byte bound"
            )
        try:
            decoded = json.loads(
                body,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            _check_json_depth(decoded)
            record = CodingPrivateCatalogRecord.model_validate(decoded)
        except (
            RecursionError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise CodingSelectionCatalogIntegrityError(
                "private catalog record is malformed"
            ) from error
        task = record.task_version
        proof = record.membership_proof
        if (
            record.catalog_commitment_sha256 != commitment.commitment_sha256
            or task.payload.coding_contract_version
            != commitment.coding_contract_version
            or task.payload.corpus_release_id != commitment.corpus_release_id
            or task.payload.catalog_index != catalog_index
            or task.payload.weight_eligible
            or proof.coding_contract_version != commitment.coding_contract_version
            or proof.corpus_release_id != commitment.corpus_release_id
            or proof.catalog_merkle_root != commitment.catalog_merkle_root
            or proof.task_version_count != commitment.task_version_count
            or proof.catalog_index != catalog_index
            or proof.task_commitment_sha256 != task.task_commitment_sha256
            or not verify_coding_catalog_membership(proof)
        ):
            raise CodingSelectionCatalogIntegrityError(
                "private catalog record does not match its registered commitment"
            )
        return task, proof


def create_coding_private_catalog_source(
    config: CodingPrivateCatalogConfig,
) -> S3CodingPrivateCatalogSource:
    return S3CodingPrivateCatalogSource(config)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON object contains a duplicate field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not permitted: {value}")


def _check_json_depth(value: Any, *, depth: int = 1) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("private catalog JSON exceeds its nesting bound")
    if isinstance(value, dict):
        for child in value.values():
            _check_json_depth(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json_depth(child, depth=depth + 1)
