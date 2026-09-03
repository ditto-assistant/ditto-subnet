"""Disposable, confirmation-gated Hippius Object Lock observation.

This is an API-level capability canary only.  It intentionally does not make
Object Lock a Coding security dependency and never operates on Coding buckets.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_REGION,
    HippiusProbeAccessDenied,
    HippiusProbeConfigurationError,
    HippiusProbeCredential,
    HippiusProbeReceiptError,
    HippiusProbeTransportError,
    _canonical_json,
    _safe_secret,
)

HIPPIUS_OBJECT_LOCK_CONFIRMATION = "OBSERVE HIPPIUS CODING OBJECT LOCK"
HIPPIUS_OBJECT_LOCK_RECEIPT_SCHEMA = "dittobench-coding-hippius-object-lock-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_RECEIPT_BYTES = 1 << 20
_SYNTHETIC_BYTES = 4096
_OBJECT_KEY = "canary.bin"


@dataclass(frozen=True, repr=False)
class HippiusObjectLockCanaryConfig:
    endpoint_url: str = field(repr=False)
    master_credential: HippiusProbeCredential = field(repr=False)
    bucket_prefix: str = "ditto-coding-object-lock-canary"
    region: str = HIPPIUS_REGION
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint_url)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not (hostname == "hippius.com" or hostname.endswith(".hippius.com"))
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or self.region != HIPPIUS_REGION
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", self.bucket_prefix)
            or not self.master_credential.access_key.startswith("hip_")
            or not _safe_secret(self.master_credential.access_key, maximum_bytes=512)
            or not _safe_secret(self.master_credential.secret_key, maximum_bytes=4096)
            or not 1.0 <= self.timeout_seconds <= 60.0
        ):
            raise HippiusProbeConfigurationError(
                "Hippius Object Lock canary configuration is unsafe"
            )


@dataclass(frozen=True)
class HippiusObjectLockReceipt:
    schema: str
    source_sha: str
    provider_profile_payload_sha256: str
    checked_at: str
    canary_authority_sha256: str
    versioning_enabled: bool
    compliance_retention_configured: bool
    same_key_created_new_version: bool
    locked_version_delete_denied: bool
    locked_version_readback_verified: bool
    delete_marker_created: bool
    retained_disposable_bucket: bool
    retained_locked_versions: int
    synthetic_only: bool
    weight_eligible: bool

    def __post_init__(self) -> None:
        if (
            self.schema != HIPPIUS_OBJECT_LOCK_RECEIPT_SCHEMA
            or _SOURCE_SHA.fullmatch(self.source_sha) is None
            or _SHA256.fullmatch(self.provider_profile_payload_sha256) is None
            or _SHA256.fullmatch(self.canary_authority_sha256) is None
            or self.retained_disposable_bucket is not True
            or self.retained_locked_versions != 2
            or self.synthetic_only is not True
            or self.weight_eligible is not False
            or not all(
                (
                    self.versioning_enabled,
                    self.compliance_retention_configured,
                    self.same_key_created_new_version,
                    self.locked_version_delete_denied,
                    self.locked_version_readback_verified,
                    self.delete_marker_created,
                )
            )
        ):
            raise HippiusProbeReceiptError("Hippius Object Lock receipt is invalid")
        try:
            checked_at = datetime.fromisoformat(self.checked_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise HippiusProbeReceiptError(
                "Hippius Object Lock receipt timestamp is invalid"
            ) from error
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            raise HippiusProbeReceiptError(
                "Hippius Object Lock receipt timestamp is invalid"
            )


class HippiusObjectLockTransport(Protocol):
    async def create_disposable_bucket(self, *, bucket: str) -> None: ...

    async def enable_versioning(self, *, bucket: str) -> None: ...

    async def versioning_enabled(self, *, bucket: str) -> bool: ...

    async def configure_compliance_retention(self, *, bucket: str) -> None: ...

    async def compliance_retention_days(self, *, bucket: str) -> int | None: ...

    async def put_object(self, *, bucket: str, key: str, body: bytes) -> str: ...

    async def delete_version(
        self, *, bucket: str, key: str, version_id: str
    ) -> None: ...

    async def get_version(self, *, bucket: str, key: str, version_id: str) -> bytes: ...

    async def delete_current(self, *, bucket: str, key: str) -> bool: ...


class AiobotoHippiusObjectLockTransport:
    def __init__(self, config: HippiusObjectLockCanaryConfig) -> None:
        import aioboto3
        from botocore.config import Config

        self._config = config
        self._session = aioboto3.Session(
            aws_access_key_id=config.master_credential.access_key,
            aws_secret_access_key=config.master_credential.secret_key,
            region_name=config.region,
        )
        self._client_config = Config(
            signature_version="s3v4",
            connect_timeout=config.timeout_seconds,
            read_timeout=config.timeout_seconds,
            retries={"max_attempts": 1, "mode": "standard"},
            request_checksum_calculation="when_required",
            s3={"addressing_style": "path"},
        )

    def _client(self):
        return self._session.client(
            "s3",
            endpoint_url=self._config.endpoint_url,
            use_ssl=True,
            config=self._client_config,
        )

    async def create_disposable_bucket(self, *, bucket: str) -> None:
        try:
            async with self._client() as s3:
                await s3.create_bucket(Bucket=bucket)
        except Exception as error:
            _raise_safe(error)

    async def enable_versioning(self, *, bucket: str) -> None:
        try:
            async with self._client() as s3:
                await s3.put_bucket_versioning(
                    Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
                )
        except Exception as error:
            _raise_safe(error)

    async def versioning_enabled(self, *, bucket: str) -> bool:
        try:
            async with self._client() as s3:
                response = await s3.get_bucket_versioning(Bucket=bucket)
        except Exception as error:
            _raise_safe(error)
        return response.get("Status") == "Enabled"

    async def configure_compliance_retention(self, *, bucket: str) -> None:
        try:
            async with self._client() as s3:
                await s3.put_object_lock_configuration(
                    Bucket=bucket,
                    ObjectLockConfiguration={
                        "ObjectLockEnabled": "Enabled",
                        "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 1}},
                    },
                )
        except Exception as error:
            _raise_safe(error)

    async def compliance_retention_days(self, *, bucket: str) -> int | None:
        try:
            async with self._client() as s3:
                response = await s3.get_object_lock_configuration(Bucket=bucket)
        except Exception as error:
            _raise_safe(error)
        try:
            retention = response["ObjectLockConfiguration"]["Rule"]["DefaultRetention"]
            if retention.get("Mode") != "COMPLIANCE":
                return None
            days = retention.get("Days")
            return days if isinstance(days, int) else None
        except (KeyError, TypeError):
            return None

    async def put_object(self, *, bucket: str, key: str, body: bytes) -> str:
        try:
            async with self._client() as s3:
                response = await s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentMD5=base64.b64encode(
                        hashlib.md5(body, usedforsecurity=False).digest()
                    ).decode("ascii"),
                )
            version_id = response.get("VersionId")
        except Exception as error:
            _raise_safe(error)
        if not isinstance(version_id, str) or not version_id:
            raise HippiusProbeTransportError(
                "Hippius Object Lock did not return a version ID"
            )
        return version_id

    async def delete_version(self, *, bucket: str, key: str, version_id: str) -> None:
        try:
            async with self._client() as s3:
                await s3.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
        except Exception as error:
            _raise_safe(error)

    async def get_version(self, *, bucket: str, key: str, version_id: str) -> bytes:
        try:
            async with self._client() as s3:
                response = await s3.get_object(
                    Bucket=bucket, Key=key, VersionId=version_id
                )
                body = await response["Body"].read(_SYNTHETIC_BYTES + 1)
        except Exception as error:
            _raise_safe(error)
        if len(body) > _SYNTHETIC_BYTES:
            raise HippiusProbeTransportError(
                "Hippius Object Lock readback exceeded bound"
            )
        return body

    async def delete_current(self, *, bucket: str, key: str) -> bool:
        try:
            async with self._client() as s3:
                response = await s3.delete_object(Bucket=bucket, Key=key)
        except Exception as error:
            _raise_safe(error)
        return response.get("DeleteMarker") is True


async def run_hippius_object_lock_canary(
    *,
    config: HippiusObjectLockCanaryConfig,
    transport: HippiusObjectLockTransport,
    source_sha: str,
    provider_profile_payload_sha256: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    synthetic_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> HippiusObjectLockReceipt:
    if (
        _SOURCE_SHA.fullmatch(source_sha) is None
        or _SHA256.fullmatch(provider_profile_payload_sha256) is None
    ):
        raise HippiusProbeConfigurationError("Hippius Object Lock canary is unsafe")
    checked_at = now()
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise HippiusProbeConfigurationError("Hippius Object Lock clock is invalid")
    bucket = f"{config.bucket_prefix}-{secrets.token_hex(10)}"
    first_body = synthetic_bytes(_SYNTHETIC_BYTES)
    second_body = synthetic_bytes(_SYNTHETIC_BYTES)
    if len(first_body) != _SYNTHETIC_BYTES or len(second_body) != _SYNTHETIC_BYTES:
        raise HippiusProbeConfigurationError(
            "Hippius Object Lock synthetic source returned wrong byte count"
        )
    await transport.create_disposable_bucket(bucket=bucket)
    await transport.enable_versioning(bucket=bucket)
    if not await transport.versioning_enabled(bucket=bucket):
        raise HippiusProbeTransportError(
            "Hippius Object Lock versioning readback was inconsistent"
        )
    await transport.configure_compliance_retention(bucket=bucket)
    if await transport.compliance_retention_days(bucket=bucket) != 1:
        raise HippiusProbeTransportError(
            "Hippius Object Lock retention readback was inconsistent"
        )
    first_version = await transport.put_object(
        bucket=bucket, key=_OBJECT_KEY, body=first_body
    )
    second_version = await transport.put_object(
        bucket=bucket, key=_OBJECT_KEY, body=second_body
    )
    if first_version == second_version:
        raise HippiusProbeTransportError(
            "Hippius Object Lock overwrite did not create a new version"
        )
    try:
        await transport.delete_version(
            bucket=bucket, key=_OBJECT_KEY, version_id=first_version
        )
    except HippiusProbeAccessDenied:
        locked_version_delete_denied = True
    else:
        raise HippiusProbeTransportError(
            "Hippius Object Lock allowed permanent deletion of a locked version"
        )
    delete_marker_created = await transport.delete_current(
        bucket=bucket, key=_OBJECT_KEY
    )
    if not delete_marker_created:
        raise HippiusProbeTransportError(
            "Hippius Object Lock delete did not create a delete marker"
        )
    readback = await transport.get_version(
        bucket=bucket, key=_OBJECT_KEY, version_id=first_version
    )
    if readback != first_body:
        raise HippiusProbeTransportError(
            "Hippius Object Lock locked version was unavailable after delete marker"
        )
    return HippiusObjectLockReceipt(
        schema=HIPPIUS_OBJECT_LOCK_RECEIPT_SCHEMA,
        source_sha=source_sha,
        provider_profile_payload_sha256=provider_profile_payload_sha256,
        checked_at=checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        canary_authority_sha256=hashlib.sha256(
            _canonical_json(
                {
                    "bucket": bucket,
                    "endpoint_url": config.endpoint_url.rstrip("/"),
                    "master_access_key": config.master_credential.access_key,
                    "region": config.region,
                    "schema": "dittobench-coding-hippius-object-lock-authority-v1",
                }
            )
        ).hexdigest(),
        versioning_enabled=True,
        compliance_retention_configured=True,
        same_key_created_new_version=True,
        locked_version_delete_denied=locked_version_delete_denied,
        locked_version_readback_verified=True,
        delete_marker_created=True,
        retained_disposable_bucket=True,
        retained_locked_versions=2,
        synthetic_only=True,
        weight_eligible=False,
    )


def write_hippius_object_lock_receipt(
    *, receipt: HippiusObjectLockReceipt, output: Path
) -> str:
    if not output.is_absolute():
        raise HippiusProbeReceiptError("Object Lock receipt output must be absolute")
    payload = asdict(receipt)
    payload_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
    encoded = (
        _canonical_json({**payload, "receipt_payload_sha256": payload_sha256}) + b"\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
    except OSError as error:
        raise HippiusProbeReceiptError(
            "Object Lock receipt output must be new and safely creatable"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise HippiusProbeReceiptError(
                "Object Lock receipt output is not an exclusive regular file"
            )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Object Lock receipt short write")
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


def _raise_safe(error: Exception) -> None:
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as import_error:  # pragma: no cover
        raise HippiusProbeTransportError(
            "Hippius Object Lock storage dependency is unavailable"
        ) from import_error
    if isinstance(error, ClientError):
        status = int(
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        )
        code = str(error.response.get("Error", {}).get("Code", "")).lower()
        if status in {401, 403} or code in {"accessdenied", "unauthorized"}:
            raise HippiusProbeAccessDenied("Hippius Object Lock request was denied")
    if isinstance(error, (BotoCoreError, OSError, TimeoutError, KeyError, TypeError)):
        raise HippiusProbeTransportError(
            "Hippius Object Lock request failed"
        ) from error
    raise HippiusProbeTransportError("Hippius Object Lock request failed") from error


__all__ = [
    "AiobotoHippiusObjectLockTransport",
    "HIPPIUS_OBJECT_LOCK_CONFIRMATION",
    "HIPPIUS_OBJECT_LOCK_RECEIPT_SCHEMA",
    "HippiusObjectLockCanaryConfig",
    "HippiusObjectLockReceipt",
    "HippiusObjectLockTransport",
    "run_hippius_object_lock_canary",
    "write_hippius_object_lock_receipt",
]
