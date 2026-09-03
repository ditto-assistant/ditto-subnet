"""Signed, verified offline publication of encrypted private inputs to Hippius."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, NoReturn, Protocol
from urllib.parse import quote, urlparse

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_hippius_encryption import (
    HippiusEncryptedPrivateInputObject,
    HippiusPrivateInputTransportManifest,
    load_hippius_private_input_transport,
    read_hippius_encrypted_private_input,
)
from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_REGION,
    HippiusProbeCredential,
    hippius_private_input_authority_sha256,
    load_hippius_probe_receipt,
)

HIPPIUS_PRIVATE_INPUT_PUBLICATION_CONFIRMATION = "PUBLISH HIPPIUS CODING PRIVATE INPUTS"
_CONFIG_PREFIX = "DITTO_CODING_HIPPIUS_"
_SIGNING_SCHEMA = "dittobench-coding-hippius-private-input-signing-v1"
_RECEIPT_SCHEMA = "dittobench-coding-hippius-private-input-publication-v1"
_REMOTE_PREFIX = "coding-private-inputs/v1"
_MAX_PUBLIC_KEY_BYTES = 64 << 10
_MAX_SIGNATURE_BYTES = 64
_MAX_CIPHERTEXT_BYTES = (2 << 20) + 16
_MAX_RECEIPT_BYTES = (1_000_000 * (1 << 10)) + (1 << 20)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PROBE_MAX_AGE = timedelta(hours=24)
_CLOCK_SKEW = timedelta(minutes=5)


class HippiusPrivateInputPublicationError(RuntimeError):
    """Publication failed without exposing provider or private-data identity."""


class HippiusPrivateInputNotFound(HippiusPrivateInputPublicationError):
    """The exact remote object is absent."""


class HippiusPrivateInputConflict(HippiusPrivateInputPublicationError):
    """The exact remote identity already contains different bytes."""


class HippiusPrivateInputPublicationStatus(StrEnum):
    UPLOADED = "uploaded"
    REUSED = "reused"


@dataclass(frozen=True, repr=False)
class HippiusPrivateInputPublicationConfig:
    endpoint_url: str = field(repr=False)
    bucket: str = field(repr=False)
    curator: HippiusProbeCredential = field(repr=False)
    reader: HippiusProbeCredential = field(repr=False)
    region: str = HIPPIUS_REGION
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        try:
            endpoint = urlparse(self.endpoint_url)
            port = endpoint.port
        except ValueError as error:
            raise HippiusPrivateInputPublicationError(
                "Hippius publication endpoint is invalid"
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
            or self.region != HIPPIUS_REGION
            or _BUCKET.fullmatch(self.bucket) is None
            or self.curator.access_key == self.reader.access_key
            or self.curator.secret_key == self.reader.secret_key
            or not _safe_credential(self.curator)
            or not _safe_credential(self.reader)
            or not 1.0 <= self.timeout_seconds <= 60.0
        ):
            raise HippiusPrivateInputPublicationError(
                "Hippius publication configuration is unsafe"
            )

    @property
    def authority_sha256(self) -> str:
        return hippius_private_input_authority_sha256(
            endpoint_url=self.endpoint_url,
            region=self.region,
            bucket=self.bucket,
            curator_access_key=self.curator.access_key,
            reader_access_key=self.reader.access_key,
        )

    def __repr__(self) -> str:
        return (
            "HippiusPrivateInputPublicationConfig(configured=True, "
            f"region={self.region!r}, timeout_seconds={self.timeout_seconds!r})"
        )


@dataclass(frozen=True)
class HippiusPrivateInputPublicationObject:
    catalog_index: int
    remote_object_key_sha256: str
    ciphertext_sha256: str
    ciphertext_size_bytes: int
    status: HippiusPrivateInputPublicationStatus


@dataclass(frozen=True)
class HippiusPrivateInputPublicationReceipt:
    schema: str
    source_sha: str
    checked_at: str
    provider: str
    probe_receipt_payload_sha256: str
    private_input_authority_sha256: str
    transport_manifest_sha256: str
    catalog_commitment_sha256: str
    curator_signing_key_sha256: str
    curator_signature_b64: str
    objects: tuple[HippiusPrivateInputPublicationObject, ...]
    ready: bool
    weight_eligible: bool


class HippiusPrivateInputPublicationTransport(Protocol):
    async def get_object(self, *, key: str, max_bytes: int) -> bytes: ...

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None: ...


class AiobotoHippiusPrivateInputPublicationTransport:
    """Separate curator-write and reader-verify clients with redacted errors."""

    def __init__(
        self,
        config: HippiusPrivateInputPublicationConfig,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        import aioboto3
        from botocore.config import Config

        self._config = config
        self._client_config = Config(
            signature_version="s3v4",
            connect_timeout=config.timeout_seconds,
            read_timeout=config.timeout_seconds,
            retries={"max_attempts": 1, "mode": "standard"},
            request_checksum_calculation="when_required",
            proxies={},
            s3={"addressing_style": "path"},
        )
        self._curator_session = aioboto3.Session(
            aws_access_key_id=config.curator.access_key,
            aws_secret_access_key=config.curator.secret_key,
            region_name=config.region,
        )
        self._reader_session = aioboto3.Session(
            aws_access_key_id=config.reader.access_key,
            aws_secret_access_key=config.reader.secret_key,
            region_name=config.region,
        )
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=http_transport,
        )

    async def __aenter__(self) -> AiobotoHippiusPrivateInputPublicationTransport:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        await self._http.aclose()

    def _client(self, session: Any) -> Any:
        return session.client(
            "s3",
            endpoint_url=self._config.endpoint_url,
            use_ssl=True,
            config=self._client_config,
        )

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        if (
            not key
            or len(key.encode()) > 2048
            or any(character.isspace() for character in key)
            or not 1 <= max_bytes <= _MAX_CIPHERTEXT_BYTES
        ):
            raise HippiusPrivateInputPublicationError(
                "Hippius publication exact-read authority is invalid"
            )
        try:
            async with self._client(self._reader_session) as s3:
                url = await s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._config.bucket, "Key": key},
                    ExpiresIn=60,
                )
            _validate_presigned_object_url(config=self._config, key=key, url=url)
            async with self._http.stream("GET", url) as response:
                if 300 <= response.status_code < 400:
                    raise HippiusPrivateInputPublicationError(
                        "Hippius publication read returned a redirect"
                    )
                if response.status_code == 404:
                    raise HippiusPrivateInputNotFound(
                        "Hippius private-input object is unavailable"
                    )
                if response.status_code != 200:
                    raise HippiusPrivateInputPublicationError(
                        "Hippius publication exact read failed"
                    )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(64 << 10):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HippiusPrivateInputPublicationError(
                            "Hippius publication download exceeded its bound"
                        )
                    chunks.append(chunk)
                if size < 1:
                    raise HippiusPrivateInputPublicationError(
                        "Hippius publication exact read returned an empty object"
                    )
                return b"".join(chunks)
        except HippiusPrivateInputPublicationError:
            raise
        except httpx.HTTPError as error:
            raise HippiusPrivateInputPublicationError(
                "Hippius publication exact read failed"
            ) from error
        except Exception as error:
            _raise_safe_provider_error(error)

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None:
        if (
            not key
            or len(key.encode()) > 2048
            or any(character.isspace() for character in key)
            or not body
            or len(body) > _MAX_CIPHERTEXT_BYTES
        ):
            raise HippiusPrivateInputPublicationError(
                "Hippius publication exact-write authority is invalid"
            )
        content_md5 = base64.b64encode(
            hashlib.md5(body, usedforsecurity=False).digest()
        ).decode("ascii")
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-MD5": content_md5,
        }
        signed_metadata = dict(metadata)
        for name, value in signed_metadata.items():
            headers[f"x-amz-meta-{name.lower()}"] = value
        try:
            async with self._client(self._curator_session) as s3:
                url = await s3.generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": self._config.bucket,
                        "Key": key,
                        "ContentType": "application/octet-stream",
                        "ContentMD5": content_md5,
                        "Metadata": signed_metadata,
                    },
                    ExpiresIn=60,
                )
            _validate_presigned_object_url(config=self._config, key=key, url=url)
            response = await self._http.put(url, content=body, headers=headers)
            if 300 <= response.status_code < 400:
                raise HippiusPrivateInputPublicationError(
                    "Hippius publication write returned a redirect"
                )
            if response.status_code not in {200, 204}:
                raise HippiusPrivateInputPublicationError(
                    "Hippius publication exact write failed"
                )
        except HippiusPrivateInputPublicationError:
            raise
        except httpx.HTTPError as error:
            raise HippiusPrivateInputPublicationError(
                "Hippius publication exact write failed"
            ) from error
        except Exception as error:
            _raise_safe_provider_error(error)


async def publish_hippius_private_inputs(
    *,
    config: HippiusPrivateInputPublicationConfig,
    transport: HippiusPrivateInputPublicationTransport,
    transport_dir: Path,
    probe_receipt_path: Path,
    curator_public_key_path: Path,
    curator_signature_path: Path,
    source_sha: str,
    now: datetime | None = None,
) -> HippiusPrivateInputPublicationReceipt:
    """Publish one signed encrypted manifest with exact reader verification."""

    if _SOURCE_SHA.fullmatch(source_sha) is None:
        raise HippiusPrivateInputPublicationError(
            "publication source SHA must be 40 lowercase hex"
        )
    checked_at = datetime.now(UTC) if now is None else now
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise HippiusPrivateInputPublicationError(
            "publication clock must be timezone-aware"
        )
    checked_at = checked_at.astimezone(UTC)
    probe_receipt, probe_payload_sha256 = load_hippius_probe_receipt(probe_receipt_path)
    probe_checked_at = datetime.fromisoformat(
        probe_receipt.checked_at.replace("Z", "+00:00")
    ).astimezone(UTC)
    if (
        probe_checked_at > checked_at + _CLOCK_SKEW
        or checked_at - probe_checked_at > _PROBE_MAX_AGE
    ):
        raise HippiusPrivateInputPublicationError(
            "probe receipt is outside the publication freshness window"
        )
    if probe_receipt.private_input_authority_sha256 != config.authority_sha256:
        raise HippiusPrivateInputPublicationError(
            "probe receipt does not bind the current private-input authority"
        )
    manifest = load_hippius_private_input_transport(transport_dir)
    public_key, signing_key_sha256 = load_curator_signing_public_key(
        curator_public_key_path
    )
    signature = _read_bounded_regular_file(
        curator_signature_path,
        maximum_bytes=_MAX_SIGNATURE_BYTES,
        label="curator signature",
    )
    if len(signature) != _MAX_SIGNATURE_BYTES:
        raise HippiusPrivateInputPublicationError("curator signature is invalid")
    message = hippius_private_input_signing_message(
        manifest=manifest,
        probe_receipt_payload_sha256=probe_payload_sha256,
        private_input_authority_sha256=config.authority_sha256,
        curator_signing_key_sha256=signing_key_sha256,
    )
    try:
        public_key.verify(signature, message)
    except InvalidSignature as error:
        raise HippiusPrivateInputPublicationError(
            "curator signature does not verify"
        ) from error

    published: list[HippiusPrivateInputPublicationObject] = []
    for item in manifest.objects:
        ciphertext = read_hippius_encrypted_private_input(
            directory=transport_dir,
            item=item,
        )
        remote_key = hippius_private_input_remote_key(
            transport_manifest_sha256=manifest.transport_manifest_sha256,
            catalog_index=item.catalog_index,
        )
        try:
            existing = await transport.get_object(
                key=remote_key,
                max_bytes=item.ciphertext_size_bytes,
            )
        except HippiusPrivateInputNotFound:
            await transport.put_object(
                key=remote_key,
                body=ciphertext,
                metadata={
                    "catalog-index": str(item.catalog_index),
                    "ciphertext-sha256": item.ciphertext_sha256,
                    "transport-manifest-sha256": manifest.transport_manifest_sha256,
                },
            )
            status = HippiusPrivateInputPublicationStatus.UPLOADED
        else:
            _verify_remote_ciphertext(existing=existing, expected_item=item)
            status = HippiusPrivateInputPublicationStatus.REUSED
        verified = await transport.get_object(
            key=remote_key,
            max_bytes=item.ciphertext_size_bytes,
        )
        _verify_remote_ciphertext(existing=verified, expected_item=item)
        published.append(
            HippiusPrivateInputPublicationObject(
                catalog_index=item.catalog_index,
                remote_object_key_sha256=hashlib.sha256(
                    remote_key.encode("utf-8")
                ).hexdigest(),
                ciphertext_sha256=item.ciphertext_sha256,
                ciphertext_size_bytes=item.ciphertext_size_bytes,
                status=status,
            )
        )
    return HippiusPrivateInputPublicationReceipt(
        schema=_RECEIPT_SCHEMA,
        source_sha=source_sha,
        checked_at=checked_at.isoformat().replace("+00:00", "Z"),
        provider="hippius",
        probe_receipt_payload_sha256=probe_payload_sha256,
        private_input_authority_sha256=config.authority_sha256,
        transport_manifest_sha256=manifest.transport_manifest_sha256,
        catalog_commitment_sha256=manifest.catalog_commitment_sha256,
        curator_signing_key_sha256=signing_key_sha256,
        curator_signature_b64=base64.b64encode(signature).decode("ascii"),
        objects=tuple(published),
        ready=True,
        weight_eligible=False,
    )


def hippius_private_input_signing_message(
    *,
    manifest: HippiusPrivateInputTransportManifest,
    probe_receipt_payload_sha256: str,
    private_input_authority_sha256: str,
    curator_signing_key_sha256: str,
) -> bytes:
    for value in (
        probe_receipt_payload_sha256,
        private_input_authority_sha256,
        curator_signing_key_sha256,
    ):
        if _SHA256.fullmatch(value) is None:
            raise HippiusPrivateInputPublicationError(
                "publication signing authority is invalid"
            )
    try:
        return coding_canonical_json_bytes(
            {
                "catalog_commitment_sha256": manifest.catalog_commitment_sha256,
                "curator_signing_key_sha256": curator_signing_key_sha256,
                "object_count": len(manifest.objects),
                "private_input_authority_sha256": private_input_authority_sha256,
                "probe_receipt_payload_sha256": probe_receipt_payload_sha256,
                "schema": _SIGNING_SCHEMA,
                "transport_manifest_sha256": manifest.transport_manifest_sha256,
                "weight_eligible": False,
                "wrapping_key_sha256": manifest.wrapping_key_sha256,
            },
            maximum_bytes=16 << 10,
            label="Hippius private-input publication signing message",
        )
    except ValueError as error:
        raise HippiusPrivateInputPublicationError(
            "publication signing message exceeds bounds"
        ) from error


def load_curator_signing_public_key(path: Path) -> tuple[Ed25519PublicKey, str]:
    body = _read_bounded_regular_file(
        path,
        maximum_bytes=_MAX_PUBLIC_KEY_BYTES,
        label="curator signing public key",
    )
    try:
        loaded = serialization.load_pem_public_key(body)
    except (TypeError, ValueError) as error:
        raise HippiusPrivateInputPublicationError(
            "curator signing public key is invalid"
        ) from error
    if not isinstance(loaded, Ed25519PublicKey):
        raise HippiusPrivateInputPublicationError(
            "curator signing public key must be Ed25519"
        )
    raw = loaded.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return loaded, hashlib.sha256(raw).hexdigest()


def hippius_private_input_remote_key(
    *, transport_manifest_sha256: str, catalog_index: int
) -> str:
    if (
        _SHA256.fullmatch(transport_manifest_sha256) is None
        or isinstance(catalog_index, bool)
        or not isinstance(catalog_index, int)
        or not 0 <= catalog_index <= 999_999
    ):
        raise HippiusPrivateInputPublicationError(
            "private-input remote identity is invalid"
        )
    return (
        f"{_REMOTE_PREFIX}/{transport_manifest_sha256}/objects/{catalog_index:06d}.bin"
    )


def parse_hippius_private_input_publication_config(
    environ: Mapping[str, str] | None = None,
) -> HippiusPrivateInputPublicationConfig:
    values = os.environ if environ is None else environ

    def required(suffix: str) -> str:
        name = f"{_CONFIG_PREFIX}{suffix}"
        value = values.get(name, "")
        if not value:
            raise HippiusPrivateInputPublicationError(
                f"required Hippius publication setting is missing: {name}"
            )
        return value

    try:
        timeout_seconds = float(values.get(f"{_CONFIG_PREFIX}TIMEOUT_SECONDS", "10"))
    except ValueError as error:
        raise HippiusPrivateInputPublicationError(
            "Hippius publication timeout is malformed"
        ) from error
    return HippiusPrivateInputPublicationConfig(
        endpoint_url=required("ENDPOINT_URL"),
        bucket=required("PRIVATE_INPUT_BUCKET"),
        curator=HippiusProbeCredential(
            access_key=required("PRIVATE_INPUT_CURATOR_ACCESS_KEY"),
            secret_key=required("PRIVATE_INPUT_CURATOR_SECRET_KEY"),
        ),
        reader=HippiusProbeCredential(
            access_key=required("PRIVATE_INPUT_READER_ACCESS_KEY"),
            secret_key=required("PRIVATE_INPUT_READER_SECRET_KEY"),
        ),
        region=values.get(f"{_CONFIG_PREFIX}REGION", HIPPIUS_REGION),
        timeout_seconds=timeout_seconds,
    )


def write_hippius_private_input_signing_message(*, message: bytes, output: Path) -> str:
    _write_exclusive_file(path=output, body=message)
    return hashlib.sha256(message).hexdigest()


def write_hippius_private_input_publication_receipt(
    *, receipt: HippiusPrivateInputPublicationReceipt, output: Path
) -> str:
    receipt = _validated_receipt(receipt)
    payload = asdict(receipt)
    try:
        payload_bytes = coding_canonical_json_bytes(
            payload,
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="Hippius private-input publication receipt",
        )
    except ValueError as error:
        raise HippiusPrivateInputPublicationError(
            "private-input publication receipt exceeds bounds"
        ) from error
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    try:
        document = coding_canonical_json_bytes(
            {**payload, "receipt_payload_sha256": payload_sha256},
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="Hippius private-input publication receipt",
        )
    except ValueError as error:
        raise HippiusPrivateInputPublicationError(
            "private-input publication receipt exceeds bounds"
        ) from error
    _write_exclusive_file(
        path=output,
        body=document,
    )
    return payload_sha256


def _validated_receipt(
    receipt: HippiusPrivateInputPublicationReceipt,
) -> HippiusPrivateInputPublicationReceipt:
    try:
        signature = base64.b64decode(receipt.curator_signature_b64, validate=True)
        checked_at = datetime.fromisoformat(receipt.checked_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise HippiusPrivateInputPublicationError(
            "private-input publication receipt is inconsistent"
        ) from error
    if (
        receipt.schema != _RECEIPT_SCHEMA
        or _SOURCE_SHA.fullmatch(receipt.source_sha) is None
        or checked_at.tzinfo is None
        or checked_at.utcoffset() is None
        or receipt.provider != "hippius"
        or _SHA256.fullmatch(receipt.probe_receipt_payload_sha256) is None
        or _SHA256.fullmatch(receipt.private_input_authority_sha256) is None
        or _SHA256.fullmatch(receipt.transport_manifest_sha256) is None
        or _SHA256.fullmatch(receipt.catalog_commitment_sha256) is None
        or _SHA256.fullmatch(receipt.curator_signing_key_sha256) is None
        or len(signature) != _MAX_SIGNATURE_BYTES
        or not receipt.objects
        or len(receipt.objects) > 1_000_000
        or receipt.ready is not True
        or receipt.weight_eligible is not False
    ):
        raise HippiusPrivateInputPublicationError(
            "private-input publication receipt is inconsistent"
        )
    remote_keys: set[str] = set()
    ciphertexts: set[str] = set()
    for expected_index, item in enumerate(receipt.objects):
        if (
            item.catalog_index != expected_index
            or _SHA256.fullmatch(item.remote_object_key_sha256) is None
            or _SHA256.fullmatch(item.ciphertext_sha256) is None
            or not 17 <= item.ciphertext_size_bytes <= _MAX_CIPHERTEXT_BYTES
            or item.status
            not in {
                HippiusPrivateInputPublicationStatus.REUSED,
                HippiusPrivateInputPublicationStatus.UPLOADED,
            }
            or item.remote_object_key_sha256 in remote_keys
            or item.ciphertext_sha256 in ciphertexts
        ):
            raise HippiusPrivateInputPublicationError(
                "private-input publication receipt object is inconsistent"
            )
        remote_keys.add(item.remote_object_key_sha256)
        ciphertexts.add(item.ciphertext_sha256)
    return receipt


def _verify_remote_ciphertext(
    *, existing: bytes, expected_item: HippiusEncryptedPrivateInputObject
) -> None:
    if (
        len(existing) != expected_item.ciphertext_size_bytes
        or hashlib.sha256(existing).hexdigest() != expected_item.ciphertext_sha256
    ):
        raise HippiusPrivateInputConflict(
            "Hippius private-input identity contains different bytes"
        )


def _safe_credential(credential: HippiusProbeCredential) -> bool:
    return (
        credential.access_key.startswith("hip_")
        and _safe_scalar(credential.access_key, maximum_bytes=512)
        and _safe_scalar(credential.secret_key, maximum_bytes=4096)
    )


def _safe_scalar(value: str, *, maximum_bytes: int) -> bool:
    return (
        bool(value)
        and len(value.encode()) <= maximum_bytes
        and all(
            character.isprintable() and not character.isspace() for character in value
        )
    )


def _read_bounded_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HippiusPrivateInputPublicationError(f"{label} is unreadable") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= maximum_bytes:
            raise HippiusPrivateInputPublicationError(f"{label} is invalid")
        body = bytearray()
        while len(body) < maximum_bytes + 1:
            chunk = os.read(descriptor, maximum_bytes + 1 - len(body))
            if not chunk:
                break
            body.extend(chunk)
    except HippiusPrivateInputPublicationError:
        raise
    except OSError as error:
        raise HippiusPrivateInputPublicationError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    if not body or len(body) > maximum_bytes:
        raise HippiusPrivateInputPublicationError(f"{label} exceeds bounds")
    return bytes(body)


def _write_exclusive_file(*, path: Path, body: bytes) -> None:
    if not path.is_absolute():
        raise HippiusPrivateInputPublicationError(
            "publication output path must be absolute"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise HippiusPrivateInputPublicationError(
            "publication output must be new and safely creatable"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise HippiusPrivateInputPublicationError(
                    "publication output write made no progress"
                )
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)


def _validate_presigned_object_url(
    *, config: HippiusPrivateInputPublicationConfig, key: str, url: str
) -> None:
    try:
        endpoint = urlparse(config.endpoint_url)
        parsed = urlparse(url)
        endpoint_port = endpoint.port or 443
        parsed_port = parsed.port or 443
    except ValueError as error:
        raise HippiusPrivateInputPublicationError(
            "Hippius publication URL is invalid"
        ) from error
    expected_path = f"/{quote(config.bucket, safe='')}/{quote(key, safe='/')}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != endpoint.hostname
        or parsed_port != endpoint_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or not parsed.query
        or parsed.fragment
    ):
        raise HippiusPrivateInputPublicationError(
            "Hippius publication URL escaped its registered origin"
        )


def _raise_safe_provider_error(error: Exception) -> NoReturn:
    try:
        from botocore.exceptions import ClientError
    except ImportError as import_error:  # pragma: no cover
        raise HippiusPrivateInputPublicationError(
            "Hippius publication dependency is unavailable"
        ) from import_error
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", "")).lower()
        status_code = int(
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0
        )
        if status_code == 404 or code in {
            "404",
            "nosuchbucket",
            "nosuchkey",
            "notfound",
        }:
            raise HippiusPrivateInputNotFound(
                "Hippius private-input object is unavailable"
            ) from error
        if status_code in {401, 403} or code in {
            "accessdenied",
            "invalidaccesskeyid",
            "signaturedoesnotmatch",
            "unauthorized",
        }:
            raise HippiusPrivateInputPublicationError(
                "Hippius publication access was denied"
            ) from error
    raise HippiusPrivateInputPublicationError(
        "Hippius publication provider call failed"
    ) from error


__all__ = [
    "AiobotoHippiusPrivateInputPublicationTransport",
    "HIPPIUS_PRIVATE_INPUT_PUBLICATION_CONFIRMATION",
    "HippiusPrivateInputConflict",
    "HippiusPrivateInputNotFound",
    "HippiusPrivateInputPublicationConfig",
    "HippiusPrivateInputPublicationError",
    "HippiusPrivateInputPublicationObject",
    "HippiusPrivateInputPublicationReceipt",
    "HippiusPrivateInputPublicationStatus",
    "HippiusPrivateInputPublicationTransport",
    "hippius_private_input_remote_key",
    "hippius_private_input_signing_message",
    "load_curator_signing_public_key",
    "parse_hippius_private_input_publication_config",
    "publish_hippius_private_inputs",
    "write_hippius_private_input_publication_receipt",
    "write_hippius_private_input_signing_message",
]
