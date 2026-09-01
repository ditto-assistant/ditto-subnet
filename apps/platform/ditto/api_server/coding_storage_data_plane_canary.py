"""Confirmation-gated private coding storage data-plane canary.

The curator seed and Platform verification are deliberately separate commands.
The curator credential is accepted only from an owner-only mode-0600 file and
is never available to the Platform verification path. Platform verification is
host-bound to the attached ``ditto-platform-api`` GCE identity and fetches only
reader/finalizer secret payloads into process memory.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

import httpx

from ditto.api_models.coding_evidence_upload import CodingSealedEvidenceKind
from ditto.api_server.coding_sealed_evidence_storage import (
    CodingSealedEvidenceCapabilityMinter,
    CodingSealedEvidenceStorageConfig,
    CodingSealedEvidenceStorageIntegrityError,
    CodingSealedEvidenceStorageUnavailableError,
    coding_sealed_evidence_object_key,
)
from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
    ObjectUploadFailedError,
)
from ditto.api_server.storage.models import StorageConfig
from ditto.db.models import CodingSealedEvidenceUpload

_PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ACCESS_ID = re.compile(r"^[^\s\x00-\x1f\x7f]{1,4096}$")
_ENVIRONMENTS = {"dev", "prod"}
_ENDPOINT = "https://storage.googleapis.com"
_REGION = "auto"
_SEED_CONFIRMATION = "SEED CODING STORAGE PRIVATE INPUT CANARY"
_VERIFY_CONFIRMATION = "RUN CODING STORAGE DATA PLANE CANARY"
_METADATA_ROOT = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default"
)


class CodingStorageCanaryError(Exception):
    """The canary input or observed data plane violates the contract."""


@dataclass(frozen=True, repr=False)
class CuratorSeedConfig:
    project: str
    environment: str
    source_sha: str
    curator_access_key: str = field(repr=False)
    curator_secret_file: Path = field(repr=False)
    confirmation: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_common(self.project, self.environment, self.source_sha)
        _validate_access_id(self.curator_access_key)
        if self.confirmation != _SEED_CONFIRMATION:
            raise CodingStorageCanaryError("curator seed confirmation is invalid")


@dataclass(frozen=True, repr=False)
class PlatformVerifyConfig:
    project: str
    environment: str
    source_sha: str
    private_input_access_key: str = field(repr=False)
    evidence_access_key: str = field(repr=False)
    confirmation: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_common(self.project, self.environment, self.source_sha)
        _validate_access_id(self.private_input_access_key)
        _validate_access_id(self.evidence_access_key)
        if self.private_input_access_key == self.evidence_access_key:
            raise CodingStorageCanaryError(
                "reader and finalizer access IDs must differ"
            )
        if self.confirmation != _VERIFY_CONFIRMATION:
            raise CodingStorageCanaryError("Platform canary confirmation is invalid")


class CodingStorageCanaryBackend(Protocol):
    async def seed_private_input(
        self, config: CuratorSeedConfig, *, key: str, payload: bytes
    ) -> dict[str, object]: ...

    async def verify_platform(
        self,
        config: PlatformVerifyConfig,
        *,
        private_key: str,
        private_payload: bytes,
        evidence_key: str,
        evidence_payload: bytes,
    ) -> dict[str, object]: ...


def _validate_common(project: str, environment: str, source_sha: str) -> None:
    if _PROJECT.fullmatch(project) is None:
        raise CodingStorageCanaryError("project id is outside GCP bounds")
    if environment not in _ENVIRONMENTS:
        raise CodingStorageCanaryError("environment must be dev or prod")
    if _COMMIT_SHA.fullmatch(source_sha) is None:
        raise CodingStorageCanaryError("source SHA must be 40 lowercase hex")


def _validate_access_id(value: str) -> None:
    if _SAFE_ACCESS_ID.fullmatch(value) is None:
        raise CodingStorageCanaryError("HMAC access ID is outside safe bounds")


def coding_storage_private_canary_payload(environment: str) -> bytes:
    return (
        "dittobench-coding-storage-private-input-canary-v1\n"
        f"environment={environment}\n"
    ).encode()


def coding_storage_evidence_canary_payload(environment: str) -> bytes:
    return (
        "dittobench-coding-storage-sealed-evidence-canary-v1\n"
        f"environment={environment}\n"
    ).encode()


def coding_storage_private_canary_key(payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    return f"coding-verification/v1/private-input/sha256/{digest}"


def coding_storage_evidence_canary_key(payload: bytes) -> str:
    return coding_sealed_evidence_object_key(
        evidence_kind=CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _receipt(payload: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema": "dittobench-coding-storage-data-plane-canary-v1",
        "verified_at": datetime.now(UTC).isoformat(),
        **payload,
        "bearer_urls_persisted": False,
        "secret_values_persisted": False,
    }
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    value["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()
    return value


async def seed_private_input(
    config: CuratorSeedConfig,
    *,
    backend: CodingStorageCanaryBackend | None = None,
) -> dict[str, Any]:
    payload = coding_storage_private_canary_payload(config.environment)
    key = coding_storage_private_canary_key(payload)
    observed = await (backend or LiveCodingStorageCanaryBackend()).seed_private_input(
        config,
        key=key,
        payload=payload,
    )
    return _receipt(
        {
            "mode": "curator-seed",
            "project": config.project,
            "environment": config.environment,
            "source_sha": config.source_sha,
            "private_input": {
                "key": key,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            },
            "checks": observed,
        }
    )


async def verify_platform(
    config: PlatformVerifyConfig,
    *,
    backend: CodingStorageCanaryBackend | None = None,
) -> dict[str, Any]:
    private_payload = coding_storage_private_canary_payload(config.environment)
    evidence_payload = coding_storage_evidence_canary_payload(config.environment)
    private_key = coding_storage_private_canary_key(private_payload)
    evidence_key = coding_storage_evidence_canary_key(evidence_payload)
    observed = await (backend or LiveCodingStorageCanaryBackend()).verify_platform(
        config,
        private_key=private_key,
        private_payload=private_payload,
        evidence_key=evidence_key,
        evidence_payload=evidence_payload,
    )
    return _receipt(
        {
            "mode": "platform-verify",
            "project": config.project,
            "environment": config.environment,
            "source_sha": config.source_sha,
            "private_input": {
                "key": private_key,
                "sha256": hashlib.sha256(private_payload).hexdigest(),
                "size_bytes": len(private_payload),
            },
            "sealed_evidence": {
                "key": evidence_key,
                "sha256": hashlib.sha256(evidence_payload).hexdigest(),
                "size_bytes": len(evidence_payload),
            },
            "checks": observed,
        }
    )


class LiveCodingStorageCanaryBackend:
    """Live S3-compatible implementation; instantiated only by explicit CLI use."""

    async def seed_private_input(
        self, config: CuratorSeedConfig, *, key: str, payload: bytes
    ) -> dict[str, object]:
        secret = _read_owner_only_secret_file(config.curator_secret_file)
        storage = S3StorageClient(
            _storage_config(
                project=config.project,
                environment=config.environment,
                authority="private-inputs",
                access_key=config.curator_access_key,
                secret_key=secret,
            )
        )
        try:
            stored = await storage.put_object(key=key, body=payload)
        except ObjectUploadFailedError as error:
            raise CodingStorageCanaryError(
                "curator seed was rejected or unavailable"
            ) from error
        if stored.sha256 != hashlib.sha256(payload).hexdigest():
            raise CodingStorageCanaryError("curator seed digest drifted")
        return {
            "curator_create_succeeded": True,
            "private_input_overwrite_attempted": False,
            "object_delete_attempted": False,
        }

    async def verify_platform(
        self,
        config: PlatformVerifyConfig,
        *,
        private_key: str,
        private_payload: bytes,
        evidence_key: str,
        evidence_payload: bytes,
    ) -> dict[str, object]:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            identity, token = await _metadata_identity_and_token(client, config.project)
            await _assert_curator_secret_denied(client, token, config)
            reader_secret = await _read_platform_secret(
                client,
                token,
                project=config.project,
                secret=f"coding-input-reader-{config.environment}-hmac-secret",
            )
            finalizer_secret = await _read_platform_secret(
                client,
                token,
                project=config.project,
                secret=f"coding-evidence-finalizer-{config.environment}-hmac-secret",
            )
            reader_config = _storage_config(
                project=config.project,
                environment=config.environment,
                authority="private-inputs",
                access_key=config.private_input_access_key,
                secret_key=reader_secret,
            )
            finalizer_config = _storage_config(
                project=config.project,
                environment=config.environment,
                authority="sealed-evidence",
                access_key=config.evidence_access_key,
                secret_key=finalizer_secret,
            )
            await _verify_private_input(
                reader_config,
                key=private_key,
                payload=private_payload,
            )
            await _verify_reader_denials(
                reader_config,
                evidence_bucket=finalizer_config.bucket,
                evidence_key=evidence_key,
            )
            evidence_created = await _ensure_evidence(
                client,
                finalizer_config,
                environment=config.environment,
                key=evidence_key,
                payload=evidence_payload,
            )
            await _verify_finalizer_denials(
                finalizer_config,
                private_bucket=reader_config.bucket,
                private_key=private_key,
            )
        return {
            "platform_identity": identity,
            "curator_secret_denied": True,
            "private_input_exact_get": True,
            "reader_list_denied": True,
            "reader_put_denied": True,
            "reader_delete_denied": True,
            "reader_cross_authority_denied": True,
            "evidence_created": evidence_created,
            "evidence_head_verified": True,
            "evidence_full_sha256_verified": True,
            "finalizer_list_denied": True,
            "finalizer_delete_denied": True,
            "finalizer_cross_authority_denied": True,
        }


def _storage_config(
    *,
    project: str,
    environment: str,
    authority: str,
    access_key: str,
    secret_key: str,
) -> StorageConfig:
    if authority == "private-inputs":
        bucket = f"{project}-coding-private-inputs-{environment}"
    elif authority == "sealed-evidence":
        bucket = f"{project}-coding-sealed-evidence-{environment}"
    else:  # pragma: no cover - callers are fixed above
        raise CodingStorageCanaryError("unknown storage authority")
    return StorageConfig(
        endpoint_url=_ENDPOINT,
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=_REGION,
        use_tls=True,
    )


def _read_owner_only_secret_file(path: Path) -> str:
    try:
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not 1 <= info.st_size <= 4096
        ):
            raise CodingStorageCanaryError(
                "curator secret file must be owner-only mode 0600"
            )
        value = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CodingStorageCanaryError("curator secret file is unavailable") from error
    if _SAFE_ACCESS_ID.fullmatch(value) is None:
        raise CodingStorageCanaryError("curator secret is outside safe bounds")
    return value


async def _metadata_identity_and_token(
    client: httpx.AsyncClient, project: str
) -> tuple[str, str]:
    headers = {"Metadata-Flavor": "Google"}
    try:
        email_response = await client.get(f"{_METADATA_ROOT}/email", headers=headers)
        token_response = await client.get(f"{_METADATA_ROOT}/token", headers=headers)
    except httpx.HTTPError as error:
        raise CodingStorageCanaryError(
            "GCE metadata identity is unavailable"
        ) from error
    expected = f"ditto-platform-api@{project}.iam.gserviceaccount.com"
    if email_response.status_code != 200 or email_response.text.strip() != expected:
        raise CodingStorageCanaryError(
            "attached GCE identity is not ditto-platform-api"
        )
    if token_response.status_code != 200:
        raise CodingStorageCanaryError("GCE metadata token is unavailable")
    try:
        token = str(token_response.json()["access_token"])
    except (KeyError, TypeError, ValueError) as error:
        raise CodingStorageCanaryError("GCE metadata token is malformed") from error
    if _SAFE_ACCESS_ID.fullmatch(token) is None:
        raise CodingStorageCanaryError("GCE metadata token is outside safe bounds")
    return expected, token


async def _secret_response(
    client: httpx.AsyncClient,
    token: str,
    *,
    project: str,
    secret: str,
) -> httpx.Response:
    url = (
        f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/"
        f"{secret}/versions/latest:access"
    )
    try:
        return await client.get(url, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as error:
        raise CodingStorageCanaryError("Secret Manager request failed") from error


async def _assert_curator_secret_denied(
    client: httpx.AsyncClient,
    token: str,
    config: PlatformVerifyConfig,
) -> None:
    response = await _secret_response(
        client,
        token,
        project=config.project,
        secret=f"coding-input-curator-{config.environment}-hmac-secret",
    )
    if response.status_code != 403:
        raise CodingStorageCanaryError("Platform curator-secret denial is absent")


async def _read_platform_secret(
    client: httpx.AsyncClient,
    token: str,
    *,
    project: str,
    secret: str,
) -> str:
    response = await _secret_response(
        client,
        token,
        project=project,
        secret=secret,
    )
    if response.status_code != 200:
        raise CodingStorageCanaryError(
            "required Platform storage secret is unavailable"
        )
    try:
        encoded = response.json()["payload"]["data"]
        value = base64.b64decode(encoded, validate=True).decode()
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
        raise CodingStorageCanaryError(
            "Platform storage secret is malformed"
        ) from error
    if _SAFE_ACCESS_ID.fullmatch(value) is None:
        raise CodingStorageCanaryError("Platform storage secret is outside safe bounds")
    return value


async def _verify_private_input(
    config: StorageConfig, *, key: str, payload: bytes
) -> None:
    try:
        observed = await S3StorageClient(config).get_object(
            key=key, max_bytes=len(payload)
        )
    except ObjectDownloadFailedError as error:
        raise CodingStorageCanaryError("private-input canary is unavailable") from error
    if (
        observed != payload
        or hashlib.sha256(observed).digest() != hashlib.sha256(payload).digest()
    ):
        raise CodingStorageCanaryError("private-input canary bytes disagree")


def _s3_client(config: StorageConfig) -> Any:
    import aioboto3
    from botocore.config import Config

    session = aioboto3.Session(
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
    )
    return session.client(
        "s3",
        endpoint_url=config.endpoint_url,
        use_ssl=config.use_tls,
        config=Config(request_checksum_calculation="when_required"),
    )


async def _expect_denied(config: StorageConfig, operation: str, **kwargs: Any) -> None:
    from botocore.exceptions import BotoCoreError, ClientError

    context = _s3_client(config)
    try:
        async with context as client:
            method = getattr(client, operation)
            try:
                await method(**kwargs)
            except ClientError as error:
                code = str(error.response.get("Error", {}).get("Code", ""))
                status_code = int(
                    error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                )
                if status_code in {401, 403} or code in {
                    "AccessDenied",
                    "Forbidden",
                }:
                    return
                raise CodingStorageCanaryError(
                    f"{operation} failed without a permission denial"
                ) from error
            except BotoCoreError as error:
                raise CodingStorageCanaryError(
                    f"{operation} could not reach storage"
                ) from error
    except CodingStorageCanaryError:
        raise
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        status_code = int(
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        )
        if status_code in {401, 403} or code in {"AccessDenied", "Forbidden"}:
            return
        raise CodingStorageCanaryError(
            f"{operation} failed without a permission denial"
        ) from error
    except BotoCoreError as error:
        raise CodingStorageCanaryError(
            f"{operation} could not reach storage"
        ) from error
    raise CodingStorageCanaryError(f"{operation} unexpectedly succeeded")


async def _verify_reader_denials(
    config: StorageConfig, *, evidence_bucket: str, evidence_key: str
) -> None:
    denial_payload = b"reader-write-denial-probe-v1\n"
    denial_key = (
        "coding-verification/v1/reader-write-denial/sha256/"
        + hashlib.sha256(denial_payload).hexdigest()
    )
    await _expect_denied(
        config,
        "list_objects_v2",
        Bucket=config.bucket,
        MaxKeys=1,
    )
    await _expect_denied(
        config,
        "put_object",
        Bucket=config.bucket,
        Key=denial_key,
        Body=denial_payload,
    )
    await _expect_denied(
        config,
        "delete_object",
        Bucket=config.bucket,
        Key=denial_key,
    )
    await _expect_denied(
        config,
        "head_object",
        Bucket=evidence_bucket,
        Key=evidence_key,
    )


async def _ensure_evidence(
    http_client: httpx.AsyncClient,
    config: StorageConfig,
    *,
    environment: str,
    key: str,
    payload: bytes,
) -> bool:
    store = S3StorageClient(config)
    kind = CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT
    digest = hashlib.sha256(payload).hexdigest()
    if key != coding_sealed_evidence_object_key(
        evidence_kind=kind,
        sha256=digest,
    ):
        raise CodingStorageCanaryError("sealed-evidence key is not canonical")
    upload = CodingSealedEvidenceUpload(
        upload_id=uuid5(NAMESPACE_URL, f"ditto-coding-storage-upload:{environment}"),
        ticket_id=uuid5(NAMESPACE_URL, f"ditto-coding-storage-ticket:{environment}"),
        claim_generation=1,
        evidence_kind=kind.value,
        sha256=digest,
        size_bytes=len(payload),
        content_type="application/octet-stream",
        weight_eligible=False,
    )
    minter = CodingSealedEvidenceCapabilityMinter(
        CodingSealedEvidenceStorageConfig(
            endpoint_url=config.endpoint_url,
            bucket=config.bucket,
            access_key=config.access_key,
            secret_key=config.secret_key,
            region=config.region,
            use_tls=config.use_tls,
        ),
        object_store=store,
    )
    created = False
    try:
        await store.head_object(key=key)
    except ObjectNotFoundError:
        now = datetime.now(UTC)
        try:
            capability = await minter.mint(
                upload,
                ticket_deadline=now + timedelta(minutes=10),
                claim_expires_at=now + timedelta(minutes=10),
            )
            response = await http_client.put(
                capability.url,
                content=payload,
                headers={
                    "Content-Length": str(len(payload)),
                    "Content-Type": "application/octet-stream",
                    "x-amz-checksum-sha256": capability.checksum_sha256_b64,
                    "x-amz-meta-sha256": digest,
                    "x-amz-meta-evidence-kind": kind.value,
                },
            )
        except (
            CodingSealedEvidenceStorageIntegrityError,
            CodingSealedEvidenceStorageUnavailableError,
            httpx.HTTPError,
        ) as error:
            raise CodingStorageCanaryError(
                "sealed-evidence capability or PUT is unavailable"
            ) from error
        if not 200 <= response.status_code < 300:
            raise CodingStorageCanaryError("sealed-evidence PUT was rejected") from None
        created = True
    except ObjectUploadFailedError as error:
        raise CodingStorageCanaryError("sealed-evidence HEAD is unavailable") from error
    try:
        verified = await minter.verify(upload)
    except (
        CodingSealedEvidenceStorageIntegrityError,
        CodingSealedEvidenceStorageUnavailableError,
    ) as error:
        raise CodingStorageCanaryError("sealed evidence is unavailable") from error
    if verified.sha256 != digest or verified.size_bytes != len(payload):
        raise CodingStorageCanaryError("sealed-evidence verification drifted")
    return created


async def _verify_finalizer_denials(
    config: StorageConfig, *, private_bucket: str, private_key: str
) -> None:
    missing = "coding-verification/v1/finalizer-delete-denial/missing"
    await _expect_denied(
        config,
        "list_objects_v2",
        Bucket=config.bucket,
        MaxKeys=1,
    )
    await _expect_denied(
        config,
        "delete_object",
        Bucket=config.bucket,
        Key=missing,
    )
    await _expect_denied(
        config,
        "head_object",
        Bucket=private_bucket,
        Key=private_key,
    )


def _write_receipt(receipt: dict[str, Any], output: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(output, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(receipt, handle, sort_keys=True, indent=2)
            handle.write("\n")
    except OSError as error:
        raise CodingStorageCanaryError(
            "receipt output must be a new writable file"
        ) from error
    print(f"receipt_sha256={receipt['receipt_sha256']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    seed = subparsers.add_parser("seed-private-input")
    verify = subparsers.add_parser("verify-platform")
    for subparser in (seed, verify):
        subparser.add_argument("--project", required=True)
        subparser.add_argument(
            "--environment", required=True, choices=sorted(_ENVIRONMENTS)
        )
        subparser.add_argument("--source-sha", required=True)
        subparser.add_argument("--output", required=True, type=Path)
        subparser.add_argument("--confirm", required=True)
    seed.add_argument("--curator-access-key", required=True)
    seed.add_argument("--curator-secret-file", required=True, type=Path)
    verify.add_argument("--private-input-access-key", required=True)
    verify.add_argument("--evidence-access-key", required=True)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode == "seed-private-input":
        return await seed_private_input(
            CuratorSeedConfig(
                project=args.project,
                environment=args.environment,
                source_sha=args.source_sha,
                curator_access_key=args.curator_access_key,
                curator_secret_file=args.curator_secret_file,
                confirmation=args.confirm,
            )
        )
    return await verify_platform(
        PlatformVerifyConfig(
            project=args.project,
            environment=args.environment,
            source_sha=args.source_sha,
            private_input_access_key=args.private_input_access_key,
            evidence_access_key=args.evidence_access_key,
            confirmation=args.confirm,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = asyncio.run(_run(args))
        _write_receipt(receipt, args.output)
    except CodingStorageCanaryError as error:
        print(f"coding storage data-plane canary failed: {error}", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "CodingStorageCanaryBackend",
    "CodingStorageCanaryError",
    "CuratorSeedConfig",
    "LiveCodingStorageCanaryBackend",
    "PlatformVerifyConfig",
    "main",
    "seed_private_input",
    "verify_platform",
]
