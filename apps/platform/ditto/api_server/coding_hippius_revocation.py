"""Confirmation-gated measurement of Hippius S3 sub-token revocation delay.

This module deliberately measures a disposable, caller-supplied sub-token.  It
does not manage any production credential and never writes a provider endpoint,
bucket, object key, or credential into its receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from ditto.api_server.coding_hippius_probe import (
    HippiusProbeAccessDenied,
    HippiusProbeConfig,
    HippiusProbeConfigurationError,
    HippiusProbeCredential,
    HippiusProbeNotFound,
    HippiusProbeReceiptError,
    HippiusProbeTransportError,
    _canonical_json,
    _safe_secret,
)

HIPPIUS_REVOCATION_CONFIRMATION = "OBSERVE HIPPIUS CODING REVOCATION"
HIPPIUS_REVOCATION_RECEIPT_SCHEMA = "dittobench-coding-hippius-revocation-v1"
HIPPIUS_MANAGEMENT_API_ORIGIN = "https://api.hippius.com"
HIPPIUS_REVOCATION_MAX_WAIT = timedelta(seconds=60)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MAX_RECEIPT_BYTES = 1 << 20
_SYNTHETIC_BYTES = 4096
_PROBE_PREFIX = "coding-revocation-probe/v1"


@dataclass(frozen=True, repr=False)
class HippiusRevocationManagementConfig:
    access_token: str = field(repr=False)
    endpoint_url: str = field(default=HIPPIUS_MANAGEMENT_API_ORIGIN, repr=False)
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.hippius.com"
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or not _safe_secret(self.access_token, maximum_bytes=4096)
            or not 1.0 <= self.timeout_seconds <= 60.0
        ):
            raise HippiusProbeConfigurationError(
                "Hippius revocation management configuration is unsafe"
            )


@dataclass(frozen=True, repr=False)
class HippiusRevocationTarget:
    token_id: str
    credential: HippiusProbeCredential = field(repr=False)

    def __post_init__(self) -> None:
        if (
            _UUID.fullmatch(self.token_id) is None
            or not self.credential.access_key.startswith("hip_")
            or not _safe_secret(self.credential.access_key, maximum_bytes=512)
            or not _safe_secret(self.credential.secret_key, maximum_bytes=4096)
        ):
            raise HippiusProbeConfigurationError(
                "Hippius revocation target configuration is unsafe"
            )


@dataclass(frozen=True)
class HippiusRevocationReceipt:
    schema: str
    source_sha: str
    provider_profile_payload_sha256: str
    target_token_sha256: str
    observed_at: str
    revoked_at: str
    rejected_at: str
    rejection_delay_milliseconds: int
    post_revoke_attempts: int
    synthetic_only: bool
    weight_eligible: bool

    def __post_init__(self) -> None:
        if (
            self.schema != HIPPIUS_REVOCATION_RECEIPT_SCHEMA
            or re.fullmatch(r"[0-9a-f]{40}", self.source_sha) is None
            or any(
                _SHA256.fullmatch(value) is None
                for value in (
                    self.provider_profile_payload_sha256,
                    self.target_token_sha256,
                )
            )
            or not 0
            <= self.rejection_delay_milliseconds
            <= int(HIPPIUS_REVOCATION_MAX_WAIT.total_seconds() * 1000)
            or not 1 <= self.post_revoke_attempts <= 601
            or self.synthetic_only is not True
            or self.weight_eligible is not False
        ):
            raise HippiusProbeReceiptError("Hippius revocation receipt is invalid")
        try:
            observed_at = _parse_time(self.observed_at)
            revoked_at = _parse_time(self.revoked_at)
            rejected_at = _parse_time(self.rejected_at)
        except ValueError as error:
            raise HippiusProbeReceiptError(
                "Hippius revocation receipt timestamp is invalid"
            ) from error
        if not observed_at <= revoked_at <= rejected_at:
            raise HippiusProbeReceiptError(
                "Hippius revocation receipt timestamps are inconsistent"
            )


class HippiusRevocationStorage(Protocol):
    async def put_synthetic(self, *, bucket: str, key: str, body: bytes) -> None: ...

    async def get_as_target(
        self, *, target: HippiusRevocationTarget, bucket: str, key: str
    ) -> bytes: ...

    async def delete_synthetic(self, *, bucket: str, key: str) -> None: ...


class HippiusRevocationManagement(Protocol):
    async def revoke_sub_token(self, *, token_id: str) -> None: ...


class HttpxHippiusRevocationManagement:
    """Minimal management-plane adapter; only revocation is implemented."""

    def __init__(self, config: HippiusRevocationManagementConfig) -> None:
        self._config = config

    async def revoke_sub_token(self, *, token_id: str) -> None:
        if _UUID.fullmatch(token_id) is None:
            raise HippiusProbeConfigurationError("Hippius sub-token ID is invalid")
        url = f"{self._config.endpoint_url}/objectstore/sub-tokens/{token_id}/revoke/"
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(self._config.timeout_seconds),
            ) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Token {self._config.access_token}"},
                )
        except httpx.HTTPError as error:
            raise HippiusProbeTransportError(
                "Hippius revocation management request failed"
            ) from error
        if response.status_code in {401, 403, 404}:
            raise HippiusProbeAccessDenied("Hippius revocation was denied")
        if response.status_code != 200:
            raise HippiusProbeTransportError(
                "Hippius revocation returned an unexpected status"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise HippiusProbeTransportError(
                "Hippius revocation returned malformed JSON"
            ) from error
        if not isinstance(payload, dict) or payload.get("status") != "revoked":
            raise HippiusProbeTransportError(
                "Hippius revocation response was inconsistent"
            )


async def run_hippius_revocation_observation(
    *,
    config: HippiusProbeConfig,
    target: HippiusRevocationTarget,
    storage: HippiusRevocationStorage,
    management: HippiusRevocationManagement,
    source_sha: str,
    provider_profile_payload_sha256: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    synthetic_bytes: Callable[[int], bytes] = secrets.token_bytes,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    poll_interval_seconds: float = 0.25,
) -> HippiusRevocationReceipt:
    """Revoke a disposable target and measure first observed S3 rejection.

    The caller must scope the target to this disposable prefix and private-input
    bucket.  The target must not be one of the three production plane identities.
    """

    if (
        re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or _SHA256.fullmatch(provider_profile_payload_sha256) is None
        or not 0.1 <= poll_interval_seconds <= 1.0
        or target.credential.access_key
        in {
            config.private_input_curator.access_key,
            config.private_input_reader.access_key,
            config.evidence_mediator.access_key,
        }
    ):
        raise HippiusProbeConfigurationError("Hippius revocation observation is unsafe")
    observed_at = _require_time(now())
    body = synthetic_bytes(_SYNTHETIC_BYTES)
    if len(body) != _SYNTHETIC_BYTES:
        raise HippiusProbeConfigurationError(
            "Hippius revocation synthetic source returned wrong byte count"
        )
    key = f"{_PROBE_PREFIX}/{secrets.token_hex(16)}.bin"
    seeded = False
    try:
        await storage.put_synthetic(
            bucket=config.private_input_bucket, key=key, body=body
        )
        seeded = True
        before_revoke = await _get_outcome(
            storage=storage, target=target, bucket=config.private_input_bucket, key=key
        )
        if before_revoke != "allowed":
            raise HippiusProbeTransportError(
                "Hippius revocation target was not usable before revocation"
            )
        await management.revoke_sub_token(token_id=target.token_id)
        revoked_at = _require_time(now())
        revoked_tick = monotonic()
        attempts = 0
        while True:
            attempts += 1
            outcome = await _get_outcome(
                storage=storage,
                target=target,
                bucket=config.private_input_bucket,
                key=key,
            )
            rejected_at = _require_time(now())
            elapsed_seconds = monotonic() - revoked_tick
            if elapsed_seconds < 0:
                raise HippiusProbeConfigurationError(
                    "Hippius revocation monotonic clock moved backwards"
                )
            if outcome == "denied":
                return HippiusRevocationReceipt(
                    schema=HIPPIUS_REVOCATION_RECEIPT_SCHEMA,
                    source_sha=source_sha,
                    provider_profile_payload_sha256=provider_profile_payload_sha256,
                    target_token_sha256=hashlib.sha256(
                        target.token_id.encode("ascii")
                    ).hexdigest(),
                    observed_at=_format_time(observed_at),
                    revoked_at=_format_time(revoked_at),
                    rejected_at=_format_time(rejected_at),
                    rejection_delay_milliseconds=round(elapsed_seconds * 1000),
                    post_revoke_attempts=attempts,
                    synthetic_only=True,
                    weight_eligible=False,
                )
            if (
                outcome != "allowed"
                or elapsed_seconds >= HIPPIUS_REVOCATION_MAX_WAIT.total_seconds()
            ):
                raise HippiusProbeTransportError(
                    "Hippius revocation did not produce an authentication denial"
                )
            await sleep(poll_interval_seconds)
    finally:
        if seeded:
            await storage.delete_synthetic(bucket=config.private_input_bucket, key=key)


async def _get_outcome(
    *,
    storage: HippiusRevocationStorage,
    target: HippiusRevocationTarget,
    bucket: str,
    key: str,
) -> str:
    try:
        body = await storage.get_as_target(target=target, bucket=bucket, key=key)
    except HippiusProbeAccessDenied:
        return "denied"
    except (HippiusProbeNotFound, HippiusProbeTransportError):
        return "unavailable"
    if len(body) != _SYNTHETIC_BYTES:
        return "unavailable"
    return "allowed"


def write_hippius_revocation_receipt(
    *, receipt: HippiusRevocationReceipt, output: Path
) -> str:
    if not output.is_absolute():
        raise HippiusProbeReceiptError("revocation receipt output must be absolute")
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
            "revocation receipt output must be new and safely creatable"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise HippiusProbeReceiptError(
                "revocation receipt output is not an exclusive regular file"
            )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("revocation receipt short write")
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


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is not timezone aware")
    return parsed.astimezone(UTC)


def _require_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HippiusProbeConfigurationError("Hippius revocation clock is invalid")
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "HIPPIUS_MANAGEMENT_API_ORIGIN",
    "HIPPIUS_REVOCATION_CONFIRMATION",
    "HIPPIUS_REVOCATION_MAX_WAIT",
    "HIPPIUS_REVOCATION_RECEIPT_SCHEMA",
    "HippiusRevocationManagement",
    "HippiusRevocationManagementConfig",
    "HippiusRevocationReceipt",
    "HippiusRevocationStorage",
    "HippiusRevocationTarget",
    "HttpxHippiusRevocationManagement",
    "run_hippius_revocation_observation",
    "write_hippius_revocation_receipt",
]
