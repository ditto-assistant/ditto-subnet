"""Confirmation-gated capability probe for the private Coding Hippius plane."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, Protocol, TypeVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

HIPPIUS_PROBE_CONFIRMATION = "PROBE HIPPIUS CODING STORAGE"
HIPPIUS_REVIEWED_REVISION = "1fa2066a366a0b839e83be60f8ab643153a772f6"
HIPPIUS_REGION = "decentralized"
_CONFIG_PREFIX = "DITTO_CODING_HIPPIUS_"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_SYNTHETIC_BYTES = 4096
_MAX_DOWNLOAD_BYTES = _SYNTHETIC_BYTES + 1
_MAX_RECEIPT_BYTES = 1 << 20
_PROBE_PREFIX = "coding-capability-probe/v1"


class HippiusProbeError(RuntimeError):
    """Base error with messages safe for operator output."""


class HippiusProbeConfigurationError(HippiusProbeError):
    """Probe configuration is incomplete or unsafe."""


class HippiusProbeAccessDenied(HippiusProbeError):
    """The provider denied an operation."""


class HippiusProbeNotFound(HippiusProbeError):
    """The provider reported a missing object."""


class HippiusProbeTransportError(HippiusProbeError):
    """The provider response was unavailable or unsafe."""


class HippiusProbeReceiptError(HippiusProbeError):
    """The redacted receipt could not be written safely."""


class HippiusCredentialRole(StrEnum):
    PRIVATE_INPUT_CURATOR = "private_input_curator"
    PRIVATE_INPUT_READER = "private_input_reader"
    EVIDENCE_MEDIATOR = "evidence_mediator"


class HippiusProbeOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    NOT_FOUND = "not_found"
    TRANSPORT_ERROR = "transport_error"


class HippiusProbeCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    OBSERVED = "observed"


@dataclass(frozen=True, repr=False)
class HippiusProbeCredential:
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class HippiusProbeConfig:
    endpoint_url: str = field(repr=False)
    private_input_bucket: str = field(repr=False)
    sealed_evidence_bucket: str = field(repr=False)
    private_input_curator: HippiusProbeCredential = field(repr=False)
    private_input_reader: HippiusProbeCredential = field(repr=False)
    evidence_mediator: HippiusProbeCredential = field(repr=False)
    region: str = HIPPIUS_REGION
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        try:
            endpoint = urlparse(self.endpoint_url)
            port = endpoint.port
        except ValueError as error:
            raise HippiusProbeConfigurationError(
                "Hippius endpoint must be an HTTPS origin"
            ) from error
        hostname = (endpoint.hostname or "").lower()
        if (
            endpoint.scheme != "https"
            or not hostname
            or not (hostname == "hippius.com" or hostname.endswith(".hippius.com"))
            or port not in {None, 443}
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.params
            or endpoint.query
            or endpoint.fragment
        ):
            raise HippiusProbeConfigurationError(
                "Hippius endpoint must be an HTTPS hippius.com origin"
            )
        if self.region != HIPPIUS_REGION:
            raise HippiusProbeConfigurationError(
                "Hippius Coding region must be decentralized"
            )
        buckets = (self.private_input_bucket, self.sealed_evidence_bucket)
        if any(_BUCKET.fullmatch(bucket) is None for bucket in buckets):
            raise HippiusProbeConfigurationError(
                "Hippius Coding buckets are outside safe S3 bounds"
            )
        if len(set(buckets)) != len(buckets):
            raise HippiusProbeConfigurationError(
                "private-input and sealed-evidence buckets must be distinct"
            )
        credentials = (
            self.private_input_curator,
            self.private_input_reader,
            self.evidence_mediator,
        )
        if any(
            not credential.access_key.startswith("hip_")
            or not _safe_secret(credential.access_key, maximum_bytes=512)
            or not _safe_secret(credential.secret_key, maximum_bytes=4096)
            for credential in credentials
        ):
            raise HippiusProbeConfigurationError(
                "Hippius Coding credentials are outside safe bounds"
            )
        if len({credential.access_key for credential in credentials}) != 3:
            raise HippiusProbeConfigurationError(
                "Hippius Coding credentials must use distinct access keys"
            )
        if len({credential.secret_key for credential in credentials}) != 3:
            raise HippiusProbeConfigurationError(
                "Hippius Coding credentials must use distinct secrets"
            )
        if not 1.0 <= self.timeout_seconds <= 60.0:
            raise HippiusProbeConfigurationError(
                "Hippius probe timeout must be between 1 and 60 seconds"
            )

    def credential(self, role: HippiusCredentialRole) -> HippiusProbeCredential:
        if role is HippiusCredentialRole.PRIVATE_INPUT_CURATOR:
            return self.private_input_curator
        if role is HippiusCredentialRole.PRIVATE_INPUT_READER:
            return self.private_input_reader
        return self.evidence_mediator

    def __repr__(self) -> str:
        return (
            "HippiusProbeConfig(configured=True, region='decentralized', "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


@dataclass(frozen=True)
class HippiusProbeObjectMetadata:
    size_bytes: int
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class HippiusProbeHttpResponse:
    status_code: int
    body: bytes


@dataclass(frozen=True)
class HippiusProbeCheck:
    name: str
    status: HippiusProbeCheckStatus
    detail: str


@dataclass(frozen=True)
class HippiusProbeReceipt:
    schema: str
    source_sha: str
    reviewed_hippius_revision: str
    checked_at: str
    provider: str
    private_input_authority_sha256: str
    sealed_evidence_authority_sha256: str
    synthetic_only: bool
    retained_synthetic_objects: int
    ready: bool
    weight_eligible: bool
    checks: tuple[HippiusProbeCheck, ...]

    def __post_init__(self) -> None:
        if (
            self.schema != "dittobench-coding-hippius-capability-probe-v1"
            or _SOURCE_SHA.fullmatch(self.source_sha) is None
            or self.reviewed_hippius_revision != HIPPIUS_REVIEWED_REVISION
            or self.provider != "hippius"
            or _SHA256.fullmatch(self.private_input_authority_sha256) is None
            or _SHA256.fullmatch(self.sealed_evidence_authority_sha256) is None
            or self.synthetic_only is not True
            or self.weight_eligible is not False
            or not 0 <= self.retained_synthetic_objects <= 5
            or not self.checks
            or len({check.name for check in self.checks}) != len(self.checks)
            or self.ready
            != all(
                check.status is not HippiusProbeCheckStatus.FAIL
                for check in self.checks
            )
        ):
            raise HippiusProbeReceiptError("Hippius probe receipt is inconsistent")
        try:
            checked_at = datetime.fromisoformat(self.checked_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise HippiusProbeReceiptError(
                "Hippius probe receipt timestamp is invalid"
            ) from error
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise HippiusProbeReceiptError(
                "Hippius probe receipt timestamp must be timezone-aware"
            )


class HippiusProbeTransport(Protocol):
    async def put_object(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None: ...

    async def get_object(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> bytes: ...

    async def head_object(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        key: str,
    ) -> HippiusProbeObjectMetadata: ...

    async def list_prefix(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        prefix: str,
    ) -> None: ...

    async def delete_object(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
    ) -> None: ...

    async def presigned_get_url(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
        expires_in: int,
    ) -> str: ...

    async def request_presigned(
        self, *, method: str, url: str
    ) -> HippiusProbeHttpResponse: ...


class AiobotoHippiusProbeTransport:
    """Live adapter whose errors and return values never expose authority."""

    def __init__(self, config: HippiusProbeConfig) -> None:
        import aioboto3
        from botocore import UNSIGNED
        from botocore.config import Config

        self._config = config
        self._aioboto3 = aioboto3
        common = {
            "connect_timeout": config.timeout_seconds,
            "read_timeout": config.timeout_seconds,
            "retries": {"max_attempts": 1, "mode": "standard"},
            "request_checksum_calculation": "when_required",
            "s3": {"addressing_style": "path"},
        }
        self._signed_config = Config(signature_version="s3v4", **common)
        self._anonymous_config = Config(signature_version=UNSIGNED, **common)
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(config.timeout_seconds),
        )

    async def __aenter__(self) -> AiobotoHippiusProbeTransport:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        await self._http.aclose()

    def _session(self, role: HippiusCredentialRole | None):
        if role is None:
            return self._aioboto3.Session(region_name=self._config.region)
        credential = self._config.credential(role)
        return self._aioboto3.Session(
            aws_access_key_id=credential.access_key,
            aws_secret_access_key=credential.secret_key,
            region_name=self._config.region,
        )

    def _client(self, role: HippiusCredentialRole | None):
        return self._session(role).client(
            "s3",
            endpoint_url=self._config.endpoint_url,
            use_ssl=True,
            config=(self._anonymous_config if role is None else self._signed_config),
        )

    async def put_object(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None:
        try:
            async with self._client(role) as s3:
                await s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/octet-stream",
                    ContentMD5=base64.b64encode(
                        hashlib.md5(body, usedforsecurity=False).digest()
                    ).decode("ascii"),
                    Metadata=dict(metadata),
                )
        except Exception as error:
            _raise_safe_provider_error(error)

    async def get_object(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> bytes:
        try:
            async with self._client(role) as s3:
                response = await s3.get_object(Bucket=bucket, Key=key)
                stream = response["Body"]
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = await stream.read(min(64 * 1024, max_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise HippiusProbeTransportError(
                            "Hippius probe response exceeded its byte bound"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except HippiusProbeError:
            raise
        except Exception as error:
            _raise_safe_provider_error(error)

    async def head_object(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        key: str,
    ) -> HippiusProbeObjectMetadata:
        try:
            async with self._client(role) as s3:
                response = await s3.head_object(Bucket=bucket, Key=key)
        except Exception as error:
            _raise_safe_provider_error(error)
        try:
            return HippiusProbeObjectMetadata(
                size_bytes=int(response["ContentLength"]),
                metadata={str(k): str(v) for k, v in response["Metadata"].items()},
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise HippiusProbeTransportError(
                "Hippius probe metadata response was malformed"
            ) from error

    async def list_prefix(
        self,
        *,
        role: HippiusCredentialRole | None,
        bucket: str,
        prefix: str,
    ) -> None:
        try:
            async with self._client(role) as s3:
                await s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
        except Exception as error:
            _raise_safe_provider_error(error)

    async def delete_object(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
    ) -> None:
        try:
            async with self._client(role) as s3:
                await s3.delete_object(Bucket=bucket, Key=key)
        except Exception as error:
            _raise_safe_provider_error(error)

    async def presigned_get_url(
        self,
        *,
        role: HippiusCredentialRole,
        bucket: str,
        key: str,
        expires_in: int,
    ) -> str:
        try:
            async with self._client(role) as s3:
                url = await s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=expires_in,
                )
        except Exception as error:
            _raise_safe_provider_error(error)
        if not isinstance(url, str) or not _is_safe_presigned_url(
            url, endpoint_url=self._config.endpoint_url
        ):
            raise HippiusProbeTransportError(
                "Hippius probe presigned URL was outside the approved origin"
            )
        return url

    async def request_presigned(
        self, *, method: str, url: str
    ) -> HippiusProbeHttpResponse:
        if not _is_safe_presigned_url(url, endpoint_url=self._config.endpoint_url):
            raise HippiusProbeAccessDenied(
                "Hippius probe presigned URL was outside the approved origin"
            )
        try:
            async with self._http.stream(method, url) as response:
                if response.status_code in {400, 401, 403, 405}:
                    raise HippiusProbeAccessDenied("Hippius probe access was denied")
                if response.status_code == 404:
                    raise HippiusProbeNotFound("Hippius probe object was unavailable")
                if response.status_code != 200:
                    raise HippiusProbeTransportError(
                        "Hippius probe returned an unexpected HTTP status"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_DOWNLOAD_BYTES:
                        raise HippiusProbeTransportError(
                            "Hippius probe response exceeded its byte bound"
                        )
                return HippiusProbeHttpResponse(
                    status_code=response.status_code, body=bytes(body)
                )
        except HippiusProbeError:
            raise
        except httpx.HTTPError as error:
            raise HippiusProbeTransportError(
                "Hippius probe HTTP request failed"
            ) from error


@dataclass(frozen=True)
class _CallResult:
    outcome: HippiusProbeOutcome
    value: object | None = None


_T = TypeVar("_T")


async def _capture(call: Awaitable[_T]) -> _CallResult:
    try:
        return _CallResult(HippiusProbeOutcome.ALLOWED, await call)
    except HippiusProbeAccessDenied:
        return _CallResult(HippiusProbeOutcome.DENIED)
    except HippiusProbeNotFound:
        return _CallResult(HippiusProbeOutcome.NOT_FOUND)
    except HippiusProbeError:
        return _CallResult(HippiusProbeOutcome.TRANSPORT_ERROR)


def _required_outcome(
    checks: list[HippiusProbeCheck],
    *,
    name: str,
    result: _CallResult,
    expected: HippiusProbeOutcome,
) -> None:
    checks.append(
        HippiusProbeCheck(
            name=name,
            status=(
                HippiusProbeCheckStatus.PASS
                if result.outcome is expected
                else HippiusProbeCheckStatus.FAIL
            ),
            detail=result.outcome.value,
        )
    )


def _required_value(
    checks: list[HippiusProbeCheck],
    *,
    name: str,
    result: _CallResult,
    expected: object,
) -> None:
    passed = result.outcome is HippiusProbeOutcome.ALLOWED and result.value == expected
    checks.append(
        HippiusProbeCheck(
            name=name,
            status=(
                HippiusProbeCheckStatus.PASS if passed else HippiusProbeCheckStatus.FAIL
            ),
            detail=("verified" if passed else result.outcome.value),
        )
    )


def _observed_outcome(
    checks: list[HippiusProbeCheck], *, name: str, result: _CallResult
) -> None:
    checks.append(
        HippiusProbeCheck(
            name=name,
            status=(
                HippiusProbeCheckStatus.FAIL
                if result.outcome
                in {
                    HippiusProbeOutcome.NOT_FOUND,
                    HippiusProbeOutcome.TRANSPORT_ERROR,
                }
                else HippiusProbeCheckStatus.OBSERVED
            ),
            detail=result.outcome.value,
        )
    )


async def run_hippius_capability_probe(
    *,
    config: HippiusProbeConfig,
    transport: HippiusProbeTransport,
    source_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    synthetic_bytes: Callable[[int], bytes] = secrets.token_bytes,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> HippiusProbeReceipt:
    """Run one synthetic capability probe without returning provider identity."""

    if _SOURCE_SHA.fullmatch(source_sha) is None:
        raise HippiusProbeConfigurationError(
            "probe source SHA must be 40 lowercase hex"
        )
    checked_at = now()
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise HippiusProbeConfigurationError("probe clock must be timezone-aware")

    run_id = synthetic_bytes(16).hex()
    input_body = synthetic_bytes(_SYNTHETIC_BYTES)
    evidence_body = synthetic_bytes(_SYNTHETIC_BYTES)
    if len(input_body) != _SYNTHETIC_BYTES or len(evidence_body) != _SYNTHETIC_BYTES:
        raise HippiusProbeConfigurationError(
            "probe entropy source returned the wrong byte count"
        )
    prefix = f"{_PROBE_PREFIX}/{run_id}"
    input_key = f"{prefix}/private-input.bin"
    evidence_key = f"{prefix}/sealed-evidence.bin"
    reader_write_key = f"{prefix}/reader-write-probe.bin"
    reader_delete_key = f"{prefix}/reader-delete-probe.bin"
    curator_cross_write_key = f"{prefix}/curator-cross-write-probe.bin"
    mediator_cross_write_key = f"{prefix}/mediator-cross-write-probe.bin"
    input_sha = hashlib.sha256(input_body).hexdigest()
    evidence_sha = hashlib.sha256(evidence_body).hexdigest()
    checks: list[HippiusProbeCheck] = []
    retained = 0

    input_put = await _capture(
        transport.put_object(
            role=HippiusCredentialRole.PRIVATE_INPUT_CURATOR,
            bucket=config.private_input_bucket,
            key=input_key,
            body=input_body,
            metadata={"probe-kind": "private-input", "probe-sha256": input_sha},
        )
    )
    _required_outcome(
        checks,
        name="private_input_curator_put",
        result=input_put,
        expected=HippiusProbeOutcome.ALLOWED,
    )
    if input_put.outcome is HippiusProbeOutcome.ALLOWED:
        retained += 1

    evidence_put = await _capture(
        transport.put_object(
            role=HippiusCredentialRole.EVIDENCE_MEDIATOR,
            bucket=config.sealed_evidence_bucket,
            key=evidence_key,
            body=evidence_body,
            metadata={"probe-kind": "sealed-evidence", "probe-sha256": evidence_sha},
        )
    )
    _required_outcome(
        checks,
        name="evidence_mediator_put",
        result=evidence_put,
        expected=HippiusProbeOutcome.ALLOWED,
    )
    if evidence_put.outcome is HippiusProbeOutcome.ALLOWED:
        retained += 1

    anonymous_input = await _capture(
        transport.get_object(
            role=None,
            bucket=config.private_input_bucket,
            key=input_key,
            max_bytes=_MAX_DOWNLOAD_BYTES,
        )
    )
    _required_outcome(
        checks,
        name="private_input_anonymous_get_denied",
        result=anonymous_input,
        expected=HippiusProbeOutcome.DENIED,
    )
    anonymous_evidence = await _capture(
        transport.get_object(
            role=None,
            bucket=config.sealed_evidence_bucket,
            key=evidence_key,
            max_bytes=_MAX_DOWNLOAD_BYTES,
        )
    )
    _required_outcome(
        checks,
        name="sealed_evidence_anonymous_get_denied",
        result=anonymous_evidence,
        expected=HippiusProbeOutcome.DENIED,
    )
    for name, bucket, key in (
        (
            "private_input_anonymous_head_denied",
            config.private_input_bucket,
            input_key,
        ),
        (
            "sealed_evidence_anonymous_head_denied",
            config.sealed_evidence_bucket,
            evidence_key,
        ),
    ):
        _required_outcome(
            checks,
            name=name,
            result=await _capture(
                transport.head_object(role=None, bucket=bucket, key=key)
            ),
            expected=HippiusProbeOutcome.DENIED,
        )
    for name, bucket in (
        ("private_input_anonymous_list_denied", config.private_input_bucket),
        ("sealed_evidence_anonymous_list_denied", config.sealed_evidence_bucket),
    ):
        result = await _capture(
            transport.list_prefix(role=None, bucket=bucket, prefix=prefix)
        )
        _required_outcome(
            checks,
            name=name,
            result=result,
            expected=HippiusProbeOutcome.DENIED,
        )

    reader_get = await _capture(
        transport.get_object(
            role=HippiusCredentialRole.PRIVATE_INPUT_READER,
            bucket=config.private_input_bucket,
            key=input_key,
            max_bytes=_MAX_DOWNLOAD_BYTES,
        )
    )
    _required_value(
        checks,
        name="private_input_reader_full_sha256",
        result=reader_get,
        expected=input_body,
    )
    reader_head = await _capture(
        transport.head_object(
            role=HippiusCredentialRole.PRIVATE_INPUT_READER,
            bucket=config.private_input_bucket,
            key=input_key,
        )
    )
    head_ok = (
        reader_head.outcome is HippiusProbeOutcome.ALLOWED
        and isinstance(reader_head.value, HippiusProbeObjectMetadata)
        and reader_head.value.size_bytes == len(input_body)
        and reader_head.value.metadata.get("probe-kind") == "private-input"
        and reader_head.value.metadata.get("probe-sha256") == input_sha
    )
    checks.append(
        HippiusProbeCheck(
            name="private_input_reader_head_metadata",
            status=(
                HippiusProbeCheckStatus.PASS
                if head_ok
                else HippiusProbeCheckStatus.FAIL
            ),
            detail="verified" if head_ok else reader_head.outcome.value,
        )
    )
    _observed_outcome(
        checks,
        name="private_input_reader_list_scope",
        result=await _capture(
            transport.list_prefix(
                role=HippiusCredentialRole.PRIVATE_INPUT_READER,
                bucket=config.private_input_bucket,
                prefix=prefix,
            )
        ),
    )
    reader_write = await _capture(
        transport.put_object(
            role=HippiusCredentialRole.PRIVATE_INPUT_READER,
            bucket=config.private_input_bucket,
            key=reader_write_key,
            body=synthetic_bytes(32),
            metadata={"probe-kind": "reader-write-denial"},
        )
    )
    _required_outcome(
        checks,
        name="private_input_reader_write_denied",
        result=reader_write,
        expected=HippiusProbeOutcome.DENIED,
    )
    if reader_write.outcome is HippiusProbeOutcome.ALLOWED:
        retained += 1
    _required_outcome(
        checks,
        name="private_input_reader_delete_denied",
        result=await _capture(
            transport.delete_object(
                role=HippiusCredentialRole.PRIVATE_INPUT_READER,
                bucket=config.private_input_bucket,
                key=reader_delete_key,
            )
        ),
        expected=HippiusProbeOutcome.DENIED,
    )
    _required_outcome(
        checks,
        name="private_input_reader_cross_bucket_denied",
        result=await _capture(
            transport.get_object(
                role=HippiusCredentialRole.PRIVATE_INPUT_READER,
                bucket=config.sealed_evidence_bucket,
                key=evidence_key,
                max_bytes=_MAX_DOWNLOAD_BYTES,
            )
        ),
        expected=HippiusProbeOutcome.DENIED,
    )
    _required_outcome(
        checks,
        name="private_input_curator_cross_bucket_denied",
        result=await _capture(
            transport.get_object(
                role=HippiusCredentialRole.PRIVATE_INPUT_CURATOR,
                bucket=config.sealed_evidence_bucket,
                key=evidence_key,
                max_bytes=_MAX_DOWNLOAD_BYTES,
            )
        ),
        expected=HippiusProbeOutcome.DENIED,
    )
    _required_outcome(
        checks,
        name="evidence_mediator_cross_bucket_denied",
        result=await _capture(
            transport.get_object(
                role=HippiusCredentialRole.EVIDENCE_MEDIATOR,
                bucket=config.private_input_bucket,
                key=input_key,
                max_bytes=_MAX_DOWNLOAD_BYTES,
            )
        ),
        expected=HippiusProbeOutcome.DENIED,
    )
    curator_cross_write = await _capture(
        transport.put_object(
            role=HippiusCredentialRole.PRIVATE_INPUT_CURATOR,
            bucket=config.sealed_evidence_bucket,
            key=curator_cross_write_key,
            body=synthetic_bytes(32),
            metadata={"probe-kind": "curator-cross-write-denial"},
        )
    )
    _required_outcome(
        checks,
        name="private_input_curator_cross_bucket_write_denied",
        result=curator_cross_write,
        expected=HippiusProbeOutcome.DENIED,
    )
    if curator_cross_write.outcome is HippiusProbeOutcome.ALLOWED:
        retained += 1
    mediator_cross_write = await _capture(
        transport.put_object(
            role=HippiusCredentialRole.EVIDENCE_MEDIATOR,
            bucket=config.private_input_bucket,
            key=mediator_cross_write_key,
            body=synthetic_bytes(32),
            metadata={"probe-kind": "mediator-cross-write-denial"},
        )
    )
    _required_outcome(
        checks,
        name="evidence_mediator_cross_bucket_write_denied",
        result=mediator_cross_write,
        expected=HippiusProbeOutcome.DENIED,
    )
    if mediator_cross_write.outcome is HippiusProbeOutcome.ALLOWED:
        retained += 1

    evidence_get = await _capture(
        transport.get_object(
            role=HippiusCredentialRole.EVIDENCE_MEDIATOR,
            bucket=config.sealed_evidence_bucket,
            key=evidence_key,
            max_bytes=_MAX_DOWNLOAD_BYTES,
        )
    )
    _required_value(
        checks,
        name="evidence_mediator_full_sha256",
        result=evidence_get,
        expected=evidence_body,
    )
    _observed_outcome(
        checks,
        name="evidence_mediator_list_scope",
        result=await _capture(
            transport.list_prefix(
                role=HippiusCredentialRole.EVIDENCE_MEDIATOR,
                bucket=config.sealed_evidence_bucket,
                prefix=prefix,
            )
        ),
    )

    presign = await _capture(
        transport.presigned_get_url(
            role=HippiusCredentialRole.PRIVATE_INPUT_READER,
            bucket=config.private_input_bucket,
            key=input_key,
            expires_in=30,
        )
    )
    if presign.outcome is HippiusProbeOutcome.ALLOWED and isinstance(
        presign.value, str
    ):
        valid_url = presign.value
        _required_value(
            checks,
            name="presigned_exact_get",
            result=await _capture(
                transport.request_presigned(method="GET", url=valid_url)
            ),
            expected=HippiusProbeHttpResponse(status_code=200, body=input_body),
        )
        _required_outcome(
            checks,
            name="presigned_wrong_method_denied",
            result=await _capture(
                transport.request_presigned(method="HEAD", url=valid_url)
            ),
            expected=HippiusProbeOutcome.DENIED,
        )
        _required_outcome(
            checks,
            name="presigned_wrong_key_denied",
            result=await _capture(
                transport.request_presigned(
                    method="GET", url=_tamper_presigned_path(valid_url)
                )
            ),
            expected=HippiusProbeOutcome.DENIED,
        )
        _required_outcome(
            checks,
            name="presigned_wrong_signature_denied",
            result=await _capture(
                transport.request_presigned(
                    method="GET", url=_tamper_presigned_signature(valid_url)
                )
            ),
            expected=HippiusProbeOutcome.DENIED,
        )
        _required_outcome(
            checks,
            name="presigned_wrong_origin_denied",
            result=await _capture(
                transport.request_presigned(
                    method="GET", url=_tamper_presigned_origin(valid_url)
                )
            ),
            expected=HippiusProbeOutcome.DENIED,
        )
    else:
        for name in (
            "presigned_exact_get",
            "presigned_wrong_method_denied",
            "presigned_wrong_key_denied",
            "presigned_wrong_signature_denied",
            "presigned_wrong_origin_denied",
        ):
            checks.append(
                HippiusProbeCheck(
                    name=name,
                    status=HippiusProbeCheckStatus.FAIL,
                    detail=presign.outcome.value,
                )
            )

    expiring = await _capture(
        transport.presigned_get_url(
            role=HippiusCredentialRole.PRIVATE_INPUT_READER,
            bucket=config.private_input_bucket,
            key=input_key,
            expires_in=1,
        )
    )
    if expiring.outcome is HippiusProbeOutcome.ALLOWED and isinstance(
        expiring.value, str
    ):
        await sleep(2.0)
        expired_result = await _capture(
            transport.request_presigned(method="GET", url=expiring.value)
        )
    else:
        expired_result = expiring
    _required_outcome(
        checks,
        name="presigned_expiry_denied",
        result=expired_result,
        expected=HippiusProbeOutcome.DENIED,
    )

    ready = all(check.status is not HippiusProbeCheckStatus.FAIL for check in checks)
    return HippiusProbeReceipt(
        schema="dittobench-coding-hippius-capability-probe-v1",
        source_sha=source_sha,
        reviewed_hippius_revision=HIPPIUS_REVIEWED_REVISION,
        checked_at=checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        provider="hippius",
        private_input_authority_sha256=hippius_private_input_authority_sha256(
            endpoint_url=config.endpoint_url,
            region=config.region,
            bucket=config.private_input_bucket,
            curator_access_key=config.private_input_curator.access_key,
            reader_access_key=config.private_input_reader.access_key,
        ),
        sealed_evidence_authority_sha256=_hippius_sealed_evidence_authority_sha256(
            endpoint_url=config.endpoint_url,
            region=config.region,
            bucket=config.sealed_evidence_bucket,
            mediator_access_key=config.evidence_mediator.access_key,
        ),
        synthetic_only=True,
        retained_synthetic_objects=retained,
        ready=ready,
        weight_eligible=False,
        checks=tuple(checks),
    )


def parse_hippius_probe_config(
    environ: Mapping[str, str] | None = None,
) -> HippiusProbeConfig:
    values = os.environ if environ is None else environ

    def required(suffix: str) -> str:
        name = f"{_CONFIG_PREFIX}{suffix}"
        value = values.get(name, "")
        if not value:
            raise HippiusProbeConfigurationError(
                f"required Hippius probe setting is missing: {name}"
            )
        return value

    try:
        timeout_seconds = float(values.get(f"{_CONFIG_PREFIX}TIMEOUT_SECONDS", "10"))
    except ValueError as error:
        raise HippiusProbeConfigurationError(
            "Hippius probe timeout is malformed"
        ) from error
    return HippiusProbeConfig(
        endpoint_url=required("ENDPOINT_URL"),
        region=values.get(f"{_CONFIG_PREFIX}REGION", HIPPIUS_REGION),
        private_input_bucket=required("PRIVATE_INPUT_BUCKET"),
        sealed_evidence_bucket=required("SEALED_EVIDENCE_BUCKET"),
        private_input_curator=HippiusProbeCredential(
            access_key=required("PRIVATE_INPUT_CURATOR_ACCESS_KEY"),
            secret_key=required("PRIVATE_INPUT_CURATOR_SECRET_KEY"),
        ),
        private_input_reader=HippiusProbeCredential(
            access_key=required("PRIVATE_INPUT_READER_ACCESS_KEY"),
            secret_key=required("PRIVATE_INPUT_READER_SECRET_KEY"),
        ),
        evidence_mediator=HippiusProbeCredential(
            access_key=required("EVIDENCE_MEDIATOR_ACCESS_KEY"),
            secret_key=required("EVIDENCE_MEDIATOR_SECRET_KEY"),
        ),
        timeout_seconds=timeout_seconds,
    )


def hippius_private_input_authority_sha256(
    *,
    endpoint_url: str,
    region: str,
    bucket: str,
    curator_access_key: str,
    reader_access_key: str,
) -> str:
    """Bind the non-secret private-input authority without disclosing it."""

    return hashlib.sha256(
        _canonical_json(
            {
                "bucket": bucket,
                "curator_access_key": curator_access_key,
                "endpoint_url": endpoint_url.rstrip("/"),
                "reader_access_key": reader_access_key,
                "region": region,
                "schema": "dittobench-coding-hippius-private-input-authority-v1",
            }
        )
    ).hexdigest()


def _hippius_sealed_evidence_authority_sha256(
    *,
    endpoint_url: str,
    region: str,
    bucket: str,
    mediator_access_key: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "bucket": bucket,
                "endpoint_url": endpoint_url.rstrip("/"),
                "mediator_access_key": mediator_access_key,
                "region": region,
                "schema": "dittobench-coding-hippius-sealed-evidence-authority-v1",
            }
        )
    ).hexdigest()


def resolve_repository_source_sha(repository_root: Path) -> str:
    try:
        value = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise HippiusProbeConfigurationError(
            "probe source revision is unavailable"
        ) from error
    if _SOURCE_SHA.fullmatch(value) is None:
        raise HippiusProbeConfigurationError(
            "probe source revision is not a full Git SHA"
        )
    return value


def write_hippius_probe_receipt(*, receipt: HippiusProbeReceipt, output: Path) -> str:
    if not output.is_absolute():
        raise HippiusProbeReceiptError("probe receipt path must be absolute")
    payload = asdict(receipt)
    payload_bytes = _canonical_json(payload)
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    document = {**payload, "receipt_payload_sha256": payload_sha256}
    encoded = _canonical_json(document) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
    except OSError as error:
        raise HippiusProbeReceiptError(
            "probe receipt output must be new and safely creatable"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise HippiusProbeReceiptError(
                "probe receipt output is not an exclusive regular file"
            )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        try:
            output.unlink(missing_ok=True)
        finally:
            os.close(descriptor)
        raise
    os.close(descriptor)
    return payload_sha256


def load_hippius_probe_receipt(path: Path) -> tuple[HippiusProbeReceipt, str]:
    """Load one canonical successful receipt without exposing its source path."""

    body = _read_bounded_regular_file(
        path,
        maximum_bytes=_MAX_RECEIPT_BYTES,
        label="Hippius probe receipt",
    )
    try:
        raw = json.loads(body, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict):
            raise ValueError("receipt root is not an object")
        payload_sha256 = str(raw.pop("receipt_payload_sha256"))
        raw_checks = raw.pop("checks")
        if not isinstance(raw_checks, list):
            raise ValueError("receipt checks are not a list")
        checks = tuple(
            HippiusProbeCheck(
                name=str(item["name"]),
                status=HippiusProbeCheckStatus(str(item["status"])),
                detail=str(item["detail"]),
            )
            for item in raw_checks
            if isinstance(item, dict) and set(item) == {"name", "status", "detail"}
        )
        if len(checks) != len(raw_checks):
            raise ValueError("receipt check shape is invalid")
        receipt = HippiusProbeReceipt(checks=checks, **raw)
        payload = asdict(receipt)
        if (
            _SHA256.fullmatch(payload_sha256) is None
            or hashlib.sha256(_canonical_json(payload)).hexdigest() != payload_sha256
            or _canonical_json({**payload, "receipt_payload_sha256": payload_sha256})
            + b"\n"
            != body
            or receipt.ready is not True
        ):
            raise ValueError("receipt digest or readiness is invalid")
    except (KeyError, TypeError, ValueError, HippiusProbeReceiptError) as error:
        raise HippiusProbeReceiptError(
            "Hippius probe receipt is invalid or not ready"
        ) from error
    return receipt, payload_sha256


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _read_bounded_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HippiusProbeReceiptError(f"{label} is unreadable") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= maximum_bytes:
            raise HippiusProbeReceiptError(f"{label} is outside safe bounds")
        chunks = bytearray()
        while len(chunks) < maximum_bytes + 1:
            chunk = os.read(descriptor, maximum_bytes + 1 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
    except HippiusProbeReceiptError:
        raise
    except OSError as error:
        raise HippiusProbeReceiptError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    if not chunks or len(chunks) > maximum_bytes:
        raise HippiusProbeReceiptError(f"{label} is outside safe bounds")
    return bytes(chunks)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _safe_secret(value: str, *, maximum_bytes: int) -> bool:
    if not value or len(value.encode("utf-8")) > maximum_bytes:
        return False
    return all(
        character.isprintable() and not character.isspace() for character in value
    )


def _is_safe_presigned_url(url: str, *, endpoint_url: str) -> bool:
    try:
        parsed = urlparse(url)
        endpoint = urlparse(endpoint_url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
        and parsed.hostname == endpoint.hostname
        and parsed.port in {None, 443}
        and bool(parsed.query)
    )


def _tamper_presigned_path(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"{parsed.path}-wrong"))


def _tamper_presigned_signature(url: str) -> str:
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    changed = False
    tampered: list[tuple[str, str]] = []
    for name, value in pairs:
        if name.lower() == "x-amz-signature" and value:
            replacement = ("0" if value[0] != "0" else "1") + value[1:]
            tampered.append((name, replacement))
            changed = True
        else:
            tampered.append((name, value))
    if not changed:
        raise HippiusProbeTransportError(
            "Hippius probe presigned URL omitted its signature"
        )
    return urlunparse(parsed._replace(query=urlencode(tampered)))


def _tamper_presigned_origin(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(netloc="not-hippius.invalid"))


def _raise_safe_provider_error(error: Exception) -> NoReturn:
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as import_error:  # pragma: no cover - installed at runtime
        raise HippiusProbeTransportError(
            "Hippius probe storage dependency is unavailable"
        ) from import_error
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", "")).lower()
        status_code = int(
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0
        )
        if status_code in {401, 403} or code in {
            "accessdenied",
            "allaccessdisabled",
            "invalidaccesskeyid",
            "signaturedoesnotmatch",
            "unauthorized",
        }:
            raise HippiusProbeAccessDenied("Hippius probe access was denied") from error
        if status_code == 404 or code in {
            "404",
            "nosuchbucket",
            "nosuchkey",
            "notfound",
        }:
            raise HippiusProbeNotFound(
                "Hippius probe object was unavailable"
            ) from error
    if isinstance(error, (BotoCoreError, OSError, TimeoutError, KeyError, TypeError)):
        raise HippiusProbeTransportError(
            "Hippius probe provider call failed"
        ) from error
    raise HippiusProbeTransportError("Hippius probe provider call failed") from error


__all__ = [
    "AiobotoHippiusProbeTransport",
    "HIPPIUS_PROBE_CONFIRMATION",
    "HIPPIUS_REVIEWED_REVISION",
    "HippiusCredentialRole",
    "HippiusProbeAccessDenied",
    "HippiusProbeCheck",
    "HippiusProbeCheckStatus",
    "HippiusProbeConfig",
    "HippiusProbeConfigurationError",
    "HippiusProbeCredential",
    "HippiusProbeError",
    "HippiusProbeHttpResponse",
    "HippiusProbeNotFound",
    "HippiusProbeObjectMetadata",
    "HippiusProbeReceipt",
    "HippiusProbeReceiptError",
    "HippiusProbeTransport",
    "HippiusProbeTransportError",
    "parse_hippius_probe_config",
    "load_hippius_probe_receipt",
    "hippius_private_input_authority_sha256",
    "resolve_repository_source_sha",
    "run_hippius_capability_probe",
    "write_hippius_probe_receipt",
]
