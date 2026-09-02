"""Exact, ticket-bound retrieval of encrypted Hippius private Coding inputs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError

from ditto.api_models.coding_artifacts import CodingArtifactDeliveryPhase
from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import CodingPrivateCatalogRecord
from ditto.api_server.coding_hippius_encryption import (
    HippiusEncryptedPrivateInputObject,
    HippiusPrivateInputEncryptionError,
    HippiusPrivateInputTransportManifest,
    hippius_private_input_manifest_aad_bytes,
    load_hippius_private_input_transport_manifest,
)
from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_REGION,
    HippiusProbeCredential,
    hippius_private_input_authority_sha256,
)
from ditto.api_server.coding_hippius_publication import (
    HippiusPrivateInputPublicationError,
    HippiusPrivateInputPublicationReceipt,
    hippius_private_input_remote_key,
    hippius_private_input_signing_message,
    load_curator_signing_public_key,
    load_hippius_private_input_publication_receipt,
)
from ditto.api_server.coding_private_catalog import (
    coding_private_catalog_record_key,
    validate_coding_private_catalog_record,
)
from ditto.coding_selection import CodingSelectionCatalogIntegrityError

_CONFIG_PREFIX = "DITTO_CODING_HIPPIUS_"
_UNWRAP_SCHEMA = "dittobench-coding-hippius-private-input-unwrap-v1"
_MAX_CIPHERTEXT_BYTES = (2 << 20) + 16
_MAX_RECORD_BYTES = 2 << 20
_MAX_JSON_DEPTH = 32
_MAX_UNWRAP_MESSAGE_BYTES = 32 << 10
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SS58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")


class HippiusPrivateInputRetrievalError(RuntimeError):
    """Base error that never contains provider or private-data identity."""


class HippiusPrivateInputRetrievalUnavailable(HippiusPrivateInputRetrievalError):
    """The exact provider object or unwrap service is temporarily unavailable."""


class HippiusPrivateInputRetrievalIntegrity(HippiusPrivateInputRetrievalError):
    """Registered, stored, ticket, unwrap, or plaintext authority disagrees."""


@dataclass(frozen=True, repr=False)
class HippiusPrivateInputRetrievalConfig:
    """Reader-only runtime configuration plus the curator's non-secret key ID."""

    endpoint_url: str = field(repr=False)
    bucket: str = field(repr=False)
    curator_access_key_id: str = field(repr=False)
    reader: HippiusProbeCredential = field(repr=False)
    region: str = HIPPIUS_REGION
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        try:
            endpoint = urlparse(self.endpoint_url)
            port = endpoint.port
        except ValueError as error:
            raise HippiusPrivateInputRetrievalIntegrity(
                "Hippius retrieval endpoint is invalid"
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
            or not _safe_access_key(self.curator_access_key_id)
            or not _safe_credential(self.reader)
            or self.curator_access_key_id == self.reader.access_key
            or not 1.0 <= self.timeout_seconds <= 60.0
        ):
            raise HippiusPrivateInputRetrievalIntegrity(
                "Hippius retrieval configuration is unsafe"
            )

    @property
    def authority_sha256(self) -> str:
        return hippius_private_input_authority_sha256(
            endpoint_url=self.endpoint_url,
            region=self.region,
            bucket=self.bucket,
            curator_access_key=self.curator_access_key_id,
            reader_access_key=self.reader.access_key,
        )

    def __repr__(self) -> str:
        return (
            "HippiusPrivateInputRetrievalConfig(configured=True, "
            f"region={self.region!r}, timeout_seconds={self.timeout_seconds!r})"
        )


@dataclass(frozen=True, repr=False)
class HippiusPrivateInputTicketAuthority:
    """Exact post-assignment authority required before any unwrap request."""

    ticket_id: UUID
    run_row_id: UUID
    validator_hotkey: str
    coding_run_id: str
    assignment_sha256: str
    run_manifest_sha256: str
    ticket_deadline: datetime
    delivery_phase: CodingArtifactDeliveryPhase
    commitment: CodingCatalogCommitment
    catalog_index: int
    transport_manifest_sha256: str
    publication_receipt_payload_sha256: str
    weight_eligible: Literal[False] = False

    def __repr__(self) -> str:
        return (
            "HippiusPrivateInputTicketAuthority(ticket_bound=True, "
            f"delivery_phase={self.delivery_phase.value!r})"
        )


@dataclass(frozen=True, repr=False)
class HippiusPrivateInputUnwrapRequest:
    """Opaque wrapped-key request bound to one ticket and delivery phase."""

    ticket_id: UUID
    run_row_id: UUID
    validator_hotkey: str
    coding_run_id: str
    assignment_sha256: str
    run_manifest_sha256: str
    ticket_deadline: datetime
    delivery_phase: CodingArtifactDeliveryPhase
    catalog_commitment_sha256: str
    catalog_index: int
    transport_manifest_sha256: str
    publication_receipt_payload_sha256: str
    wrapping_key_sha256: str
    aad_sha256: str
    ciphertext_sha256: str
    wrapped_data_key: bytes = field(repr=False)
    request_sha256: str

    def __repr__(self) -> str:
        return (
            "HippiusPrivateInputUnwrapRequest(ticket_bound=True, "
            f"delivery_phase={self.delivery_phase.value!r})"
        )


@dataclass(frozen=True, repr=False)
class HippiusPrivateInputUnwrapResult:
    request_sha256: str
    data_key: bytes = field(repr=False)
    expires_at: datetime

    def __repr__(self) -> str:
        return "HippiusPrivateInputUnwrapResult(ticket_bound=True)"


class HippiusPrivateInputReader(Protocol):
    async def get_object(self, *, key: str, max_bytes: int) -> bytes: ...


class HippiusPrivateInputUnwrapper(Protocol):
    async def unwrap_data_key(
        self, request: HippiusPrivateInputUnwrapRequest
    ) -> HippiusPrivateInputUnwrapResult: ...


class AiobotoHippiusPrivateInputReader:
    """Reader-only exact GET adapter with no proxy or redirect inheritance."""

    def __init__(
        self,
        config: HippiusPrivateInputRetrievalConfig,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        import aioboto3
        from botocore.config import Config

        self._config = config
        self._session = aioboto3.Session(
            aws_access_key_id=config.reader.access_key,
            aws_secret_access_key=config.reader.secret_key,
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
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=http_transport,
        )

    async def __aenter__(self) -> AiobotoHippiusPrivateInputReader:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        await self._http.aclose()

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        if (
            not key
            or len(key.encode()) > 2048
            or any(character.isspace() for character in key)
            or not 17 <= max_bytes <= _MAX_CIPHERTEXT_BYTES
        ):
            raise HippiusPrivateInputRetrievalIntegrity(
                "private-input exact-read authority is invalid"
            )
        try:
            async with self._session.client(
                "s3",
                endpoint_url=self._config.endpoint_url,
                use_ssl=True,
                config=self._client_config,
            ) as s3:
                url = await s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._config.bucket, "Key": key},
                    ExpiresIn=60,
                )
            _validate_presigned_get_url(config=self._config, key=key, url=url)
            async with self._http.stream("GET", url) as response:
                if 300 <= response.status_code < 400:
                    raise HippiusPrivateInputRetrievalIntegrity(
                        "Hippius exact read returned a redirect"
                    )
                if response.status_code == 404:
                    raise HippiusPrivateInputRetrievalUnavailable(
                        "Hippius private-input object is unavailable"
                    )
                if response.status_code != 200:
                    raise HippiusPrivateInputRetrievalUnavailable(
                        "Hippius private-input exact read failed"
                    )
                raw_length = response.headers.get("content-length")
                if raw_length is not None:
                    try:
                        content_length = int(raw_length)
                    except ValueError as error:
                        raise HippiusPrivateInputRetrievalIntegrity(
                            "Hippius exact-read length is malformed"
                        ) from error
                    if content_length < 1 or content_length > max_bytes:
                        raise HippiusPrivateInputRetrievalIntegrity(
                            "Hippius exact-read length is outside its bound"
                        )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes(64 << 10):
                    size += len(chunk)
                    if size > max_bytes:
                        raise HippiusPrivateInputRetrievalIntegrity(
                            "Hippius exact read exceeded its byte bound"
                        )
                    chunks.append(chunk)
                if size < 1:
                    raise HippiusPrivateInputRetrievalIntegrity(
                        "Hippius exact read returned an empty object"
                    )
                return b"".join(chunks)
        except HippiusPrivateInputRetrievalError:
            raise
        except httpx.HTTPError as error:
            raise HippiusPrivateInputRetrievalUnavailable(
                "Hippius private-input exact read failed"
            ) from error
        except Exception as error:
            raise HippiusPrivateInputRetrievalUnavailable(
                "Hippius private-input reader failed safely"
            ) from error


class HippiusPrivateInputRetriever:
    """Verify, fetch, unwrap, decrypt, and revalidate one ticket-selected record."""

    def __init__(
        self,
        *,
        config: HippiusPrivateInputRetrievalConfig,
        manifest_path: Path,
        publication_receipt_path: Path,
        curator_public_key_path: Path,
        reader: HippiusPrivateInputReader,
        unwrapper: HippiusPrivateInputUnwrapper,
    ) -> None:
        self._config = config
        self._reader = reader
        self._unwrapper = unwrapper
        try:
            manifest = load_hippius_private_input_transport_manifest(manifest_path)
            receipt, receipt_payload_sha256 = (
                load_hippius_private_input_publication_receipt(publication_receipt_path)
            )
            public_key, signing_key_sha256 = load_curator_signing_public_key(
                curator_public_key_path
            )
            signature = base64.b64decode(
                receipt.curator_signature_b64,
                validate=True,
            )
            message = hippius_private_input_signing_message(
                manifest=manifest,
                probe_receipt_payload_sha256=receipt.probe_receipt_payload_sha256,
                private_input_authority_sha256=config.authority_sha256,
                curator_signing_key_sha256=signing_key_sha256,
            )
            public_key.verify(signature, message)
            _validate_registered_transport(
                config=config,
                manifest=manifest,
                receipt=receipt,
                signing_key_sha256=signing_key_sha256,
            )
        except (
            InvalidSignature,
            ValueError,
            HippiusPrivateInputEncryptionError,
            HippiusPrivateInputPublicationError,
        ) as error:
            raise HippiusPrivateInputRetrievalIntegrity(
                "registered Hippius private-input authority is invalid"
            ) from error
        self._manifest = manifest
        self._receipt = receipt
        self._receipt_payload_sha256 = receipt_payload_sha256

    @property
    def timeout_seconds(self) -> float:
        return self._config.timeout_seconds

    async def get_task_material(
        self,
        *,
        authority: HippiusPrivateInputTicketAuthority,
        now: datetime | None = None,
    ) -> CodingPrivateCatalogRecord:
        """Return one verified plaintext record without writing it to disk."""

        checked_at = _utc_now(now)
        authority = _validated_ticket_authority(authority)
        if checked_at >= authority.ticket_deadline:
            raise HippiusPrivateInputRetrievalUnavailable(
                "private-input ticket is no longer active"
            )
        if (
            authority.commitment.commitment_sha256
            != self._manifest.catalog_commitment_sha256
            or authority.commitment.coding_contract_version
            != self._manifest.coding_contract_version
            or authority.commitment.corpus_release_id
            != self._manifest.corpus_release_id
            or authority.commitment.task_version_count != len(self._manifest.objects)
            or authority.transport_manifest_sha256
            != self._manifest.transport_manifest_sha256
            or authority.publication_receipt_payload_sha256
            != self._receipt_payload_sha256
        ):
            raise HippiusPrivateInputRetrievalIntegrity(
                "ticket does not bind the registered private-input release"
            )

        item = self._manifest.objects[authority.catalog_index]
        receipt_item = self._receipt.objects[authority.catalog_index]
        expected_logical_key = coding_private_catalog_record_key(
            catalog_commitment_sha256=authority.commitment.commitment_sha256,
            catalog_index=authority.catalog_index,
        )
        remote_key = hippius_private_input_remote_key(
            transport_manifest_sha256=self._manifest.transport_manifest_sha256,
            catalog_index=authority.catalog_index,
        )
        if (
            item.logical_object_key != expected_logical_key
            or receipt_item.remote_object_key_sha256
            != hashlib.sha256(remote_key.encode()).hexdigest()
        ):
            raise HippiusPrivateInputRetrievalIntegrity(
                "selected private-input object identity is inconsistent"
            )

        timeout_seconds = min(
            self._config.timeout_seconds,
            (authority.ticket_deadline - checked_at).total_seconds(),
        )
        if timeout_seconds <= 0:
            raise HippiusPrivateInputRetrievalUnavailable(
                "private-input ticket has insufficient lifetime"
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                ciphertext = await self._reader.get_object(
                    key=remote_key,
                    max_bytes=item.ciphertext_size_bytes,
                )
        except TimeoutError as error:
            raise HippiusPrivateInputRetrievalUnavailable(
                "Hippius private-input exact read timed out"
            ) from error
        except HippiusPrivateInputRetrievalError:
            raise
        except Exception as error:
            raise HippiusPrivateInputRetrievalUnavailable(
                "Hippius private-input exact read failed"
            ) from error
        if (
            len(ciphertext) != item.ciphertext_size_bytes
            or hashlib.sha256(ciphertext).hexdigest() != item.ciphertext_sha256
            or receipt_item.ciphertext_sha256 != item.ciphertext_sha256
            or receipt_item.ciphertext_size_bytes != item.ciphertext_size_bytes
        ):
            raise HippiusPrivateInputRetrievalIntegrity(
                "Hippius private-input ciphertext identity is inconsistent"
            )

        aad = hippius_private_input_manifest_aad_bytes(
            manifest=self._manifest,
            item=item,
        )
        if hashlib.sha256(aad).hexdigest() != item.aad_sha256:
            raise HippiusPrivateInputRetrievalIntegrity(
                "private-input authenticated-data identity is inconsistent"
            )
        request = _build_unwrap_request(
            authority=authority,
            manifest=self._manifest,
            item=item,
            publication_receipt_payload_sha256=self._receipt_payload_sha256,
        )
        checked_at = _utc_now(now)
        timeout_seconds = min(
            self._config.timeout_seconds,
            (authority.ticket_deadline - checked_at).total_seconds(),
        )
        if timeout_seconds <= 0:
            raise HippiusPrivateInputRetrievalUnavailable(
                "private-input ticket expired before unwrap"
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await self._unwrapper.unwrap_data_key(request)
        except TimeoutError as error:
            raise HippiusPrivateInputRetrievalUnavailable(
                "private-input unwrap timed out"
            ) from error
        except HippiusPrivateInputRetrievalError:
            raise
        except Exception as error:
            raise HippiusPrivateInputRetrievalUnavailable(
                "private-input unwrap failed"
            ) from error
        data_key = _validated_unwrap_result(
            request=request,
            result=result,
            now=checked_at,
            ticket_deadline=authority.ticket_deadline,
        )
        mutable_key = bytearray(data_key)
        try:
            plaintext = AESGCM(bytes(mutable_key)).decrypt(
                base64.b64decode(item.nonce_b64, validate=True),
                ciphertext,
                aad,
            )
        except (InvalidTag, ValueError) as error:
            raise HippiusPrivateInputRetrievalIntegrity(
                "private-input ciphertext authentication failed"
            ) from error
        finally:
            mutable_key[:] = b"\x00" * len(mutable_key)
        if (
            len(plaintext) != item.plaintext_size_bytes
            or hashlib.sha256(plaintext).hexdigest() != item.plaintext_sha256
        ):
            raise HippiusPrivateInputRetrievalIntegrity(
                "private-input plaintext identity is inconsistent"
            )
        if _utc_now(now) >= authority.ticket_deadline:
            raise HippiusPrivateInputRetrievalUnavailable(
                "private-input ticket expired before material delivery"
            )
        return _decode_private_catalog_record(
            body=plaintext,
            commitment=authority.commitment,
            catalog_index=authority.catalog_index,
            item=item,
        )


def parse_hippius_private_input_retrieval_config(
    environ: Mapping[str, str] | None = None,
) -> HippiusPrivateInputRetrievalConfig:
    values = os.environ if environ is None else environ

    def required(suffix: str) -> str:
        name = f"{_CONFIG_PREFIX}{suffix}"
        value = values.get(name, "")
        if not value:
            raise HippiusPrivateInputRetrievalIntegrity(
                f"required Hippius retrieval setting is missing: {name}"
            )
        return value

    try:
        timeout_seconds = float(values.get(f"{_CONFIG_PREFIX}TIMEOUT_SECONDS", "10"))
    except ValueError as error:
        raise HippiusPrivateInputRetrievalIntegrity(
            "Hippius retrieval timeout is malformed"
        ) from error
    return HippiusPrivateInputRetrievalConfig(
        endpoint_url=required("ENDPOINT_URL"),
        bucket=required("PRIVATE_INPUT_BUCKET"),
        curator_access_key_id=required("PRIVATE_INPUT_CURATOR_ACCESS_KEY"),
        reader=HippiusProbeCredential(
            access_key=required("PRIVATE_INPUT_READER_ACCESS_KEY"),
            secret_key=required("PRIVATE_INPUT_READER_SECRET_KEY"),
        ),
        region=values.get(f"{_CONFIG_PREFIX}REGION", HIPPIUS_REGION),
        timeout_seconds=timeout_seconds,
    )


def _validate_registered_transport(
    *,
    config: HippiusPrivateInputRetrievalConfig,
    manifest: HippiusPrivateInputTransportManifest,
    receipt: HippiusPrivateInputPublicationReceipt,
    signing_key_sha256: str,
) -> None:
    if (
        receipt.private_input_authority_sha256 != config.authority_sha256
        or receipt.transport_manifest_sha256 != manifest.transport_manifest_sha256
        or receipt.catalog_commitment_sha256 != manifest.catalog_commitment_sha256
        or receipt.curator_signing_key_sha256 != signing_key_sha256
        or len(receipt.objects) != len(manifest.objects)
    ):
        raise HippiusPrivateInputRetrievalIntegrity(
            "publication receipt does not bind the registered transport"
        )
    for expected_index, (manifest_item, receipt_item) in enumerate(
        zip(manifest.objects, receipt.objects, strict=True)
    ):
        remote_key = hippius_private_input_remote_key(
            transport_manifest_sha256=manifest.transport_manifest_sha256,
            catalog_index=expected_index,
        )
        if (
            manifest_item.catalog_index != expected_index
            or receipt_item.catalog_index != expected_index
            or receipt_item.remote_object_key_sha256
            != hashlib.sha256(remote_key.encode()).hexdigest()
            or receipt_item.ciphertext_sha256 != manifest_item.ciphertext_sha256
            or receipt_item.ciphertext_size_bytes != manifest_item.ciphertext_size_bytes
        ):
            raise HippiusPrivateInputRetrievalIntegrity(
                "publication receipt object does not bind its transport object"
            )


def _validated_ticket_authority(
    authority: HippiusPrivateInputTicketAuthority,
) -> HippiusPrivateInputTicketAuthority:
    try:
        commitment = CodingCatalogCommitment.model_validate_json(
            authority.commitment.model_dump_json(by_alias=True)
        )
    except (AttributeError, ValidationError, ValueError) as error:
        raise HippiusPrivateInputRetrievalIntegrity(
            "private-input ticket commitment is invalid"
        ) from error
    if (
        not isinstance(authority.ticket_id, UUID)
        or authority.ticket_id.int == 0
        or not isinstance(authority.run_row_id, UUID)
        or authority.run_row_id.int == 0
        or not isinstance(authority.validator_hotkey, str)
        or _SS58.fullmatch(authority.validator_hotkey) is None
        or not isinstance(authority.coding_run_id, str)
        or not _safe_scalar(authority.coding_run_id, maximum_bytes=256)
        or not isinstance(authority.assignment_sha256, str)
        or _SHA256.fullmatch(authority.assignment_sha256) is None
        or not isinstance(authority.run_manifest_sha256, str)
        or _SHA256.fullmatch(authority.run_manifest_sha256) is None
        or not isinstance(authority.ticket_deadline, datetime)
        or authority.ticket_deadline.tzinfo is None
        or authority.ticket_deadline.utcoffset() is None
        or not isinstance(authority.delivery_phase, CodingArtifactDeliveryPhase)
        or authority.delivery_phase
        not in {
            CodingArtifactDeliveryPhase.AUTHORING,
            CodingArtifactDeliveryPhase.GRADING,
        }
        or isinstance(authority.catalog_index, bool)
        or not isinstance(authority.catalog_index, int)
        or not 0 <= authority.catalog_index < commitment.task_version_count
        or not isinstance(authority.transport_manifest_sha256, str)
        or _SHA256.fullmatch(authority.transport_manifest_sha256) is None
        or not isinstance(authority.publication_receipt_payload_sha256, str)
        or _SHA256.fullmatch(authority.publication_receipt_payload_sha256) is None
        or authority.weight_eligible is not False
    ):
        raise HippiusPrivateInputRetrievalIntegrity(
            "private-input ticket authority is invalid"
        )
    return replace(
        authority,
        ticket_deadline=authority.ticket_deadline.astimezone(UTC),
        commitment=commitment,
    )


def _build_unwrap_request(
    *,
    authority: HippiusPrivateInputTicketAuthority,
    manifest: HippiusPrivateInputTransportManifest,
    item: HippiusEncryptedPrivateInputObject,
    publication_receipt_payload_sha256: str,
) -> HippiusPrivateInputUnwrapRequest:
    try:
        wrapped_data_key = base64.b64decode(item.wrapped_data_key_b64, validate=True)
    except ValueError as error:
        raise HippiusPrivateInputRetrievalIntegrity(
            "private-input wrapped key is invalid"
        ) from error
    projection = {
        "aad_sha256": item.aad_sha256,
        "assignment_sha256": authority.assignment_sha256,
        "catalog_commitment_sha256": authority.commitment.commitment_sha256,
        "catalog_index": authority.catalog_index,
        "ciphertext_sha256": item.ciphertext_sha256,
        "coding_run_id": authority.coding_run_id,
        "delivery_phase": authority.delivery_phase.value,
        "publication_receipt_payload_sha256": publication_receipt_payload_sha256,
        "run_manifest_sha256": authority.run_manifest_sha256,
        "run_row_id": str(authority.run_row_id),
        "schema": _UNWRAP_SCHEMA,
        "ticket_deadline": authority.ticket_deadline.isoformat().replace("+00:00", "Z"),
        "ticket_id": str(authority.ticket_id),
        "transport_manifest_sha256": authority.transport_manifest_sha256,
        "validator_hotkey": authority.validator_hotkey,
        "weight_eligible": False,
        "wrapped_data_key_sha256": hashlib.sha256(wrapped_data_key).hexdigest(),
        "wrapping_key_sha256": manifest.wrapping_key_sha256,
    }
    try:
        request_sha256 = hashlib.sha256(
            coding_canonical_json_bytes(
                projection,
                maximum_bytes=_MAX_UNWRAP_MESSAGE_BYTES,
                label="Hippius private-input unwrap request",
            )
        ).hexdigest()
    except ValueError as error:
        raise HippiusPrivateInputRetrievalIntegrity(
            "private-input unwrap request exceeds bounds"
        ) from error
    return HippiusPrivateInputUnwrapRequest(
        ticket_id=authority.ticket_id,
        run_row_id=authority.run_row_id,
        validator_hotkey=authority.validator_hotkey,
        coding_run_id=authority.coding_run_id,
        assignment_sha256=authority.assignment_sha256,
        run_manifest_sha256=authority.run_manifest_sha256,
        ticket_deadline=authority.ticket_deadline,
        delivery_phase=authority.delivery_phase,
        catalog_commitment_sha256=authority.commitment.commitment_sha256,
        catalog_index=authority.catalog_index,
        transport_manifest_sha256=authority.transport_manifest_sha256,
        publication_receipt_payload_sha256=publication_receipt_payload_sha256,
        wrapping_key_sha256=manifest.wrapping_key_sha256,
        aad_sha256=item.aad_sha256,
        ciphertext_sha256=item.ciphertext_sha256,
        wrapped_data_key=wrapped_data_key,
        request_sha256=request_sha256,
    )


def _validated_unwrap_result(
    *,
    request: HippiusPrivateInputUnwrapRequest,
    result: HippiusPrivateInputUnwrapResult,
    now: datetime,
    ticket_deadline: datetime,
) -> bytes:
    if (
        not isinstance(result, HippiusPrivateInputUnwrapResult)
        or result.request_sha256 != request.request_sha256
        or not isinstance(result.data_key, bytes)
        or len(result.data_key) != 32
        or not isinstance(result.expires_at, datetime)
        or result.expires_at.tzinfo is None
        or result.expires_at.utcoffset() is None
        or result.expires_at.astimezone(UTC) <= now
        or result.expires_at.astimezone(UTC) > ticket_deadline
    ):
        raise HippiusPrivateInputRetrievalIntegrity(
            "private-input unwrap response is inconsistent"
        )
    return result.data_key


def _decode_private_catalog_record(
    *,
    body: bytes,
    commitment: CodingCatalogCommitment,
    catalog_index: int,
    item: HippiusEncryptedPrivateInputObject,
) -> CodingPrivateCatalogRecord:
    try:
        raw = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        _check_json_depth(raw)
        record = CodingPrivateCatalogRecord.model_validate(raw)
        canonical = coding_canonical_json_bytes(
            record.model_dump(mode="json", by_alias=True),
            maximum_bytes=_MAX_RECORD_BYTES,
            label="private catalog record",
        )
        if canonical != body:
            raise ValueError("record bytes are not canonical")
        record = validate_coding_private_catalog_record(
            commitment=commitment,
            catalog_index=catalog_index,
            record=record,
        )
    except (
        RecursionError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        CodingSelectionCatalogIntegrityError,
    ) as error:
        raise HippiusPrivateInputRetrievalIntegrity(
            "decrypted private catalog record is invalid"
        ) from error
    if (
        record.task_version.task_commitment_sha256 != item.task_commitment_sha256
        or record.task_version.payload.task_version_id != item.task_version_id
    ):
        raise HippiusPrivateInputRetrievalIntegrity(
            "decrypted private catalog task identity is inconsistent"
        )
    return record


def _validate_presigned_get_url(
    *, config: HippiusPrivateInputRetrievalConfig, key: str, url: str
) -> None:
    try:
        endpoint = urlparse(config.endpoint_url)
        parsed = urlparse(url)
        endpoint_port = endpoint.port or 443
        parsed_port = parsed.port or 443
    except ValueError as error:
        raise HippiusPrivateInputRetrievalIntegrity(
            "Hippius exact-read URL is invalid"
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
        raise HippiusPrivateInputRetrievalIntegrity(
            "Hippius exact-read URL escaped its registered origin"
        )


def _utc_now(value: datetime | None) -> datetime:
    resolved = datetime.now(UTC) if value is None else value
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise HippiusPrivateInputRetrievalIntegrity(
            "private-input retrieval clock must be timezone-aware"
        )
    return resolved.astimezone(UTC)


def _safe_access_key(value: str) -> bool:
    return value.startswith("hip_") and _safe_scalar(value, maximum_bytes=512)


def _safe_credential(value: HippiusProbeCredential) -> bool:
    return _safe_access_key(value.access_key) and _safe_scalar(
        value.secret_key,
        maximum_bytes=4096,
    )


def _safe_scalar(value: str, *, maximum_bytes: int) -> bool:
    return (
        bool(value)
        and len(value.encode()) <= maximum_bytes
        and not any(
            character.isspace() or unicodedata.category(character) == "Cc"
            for character in value
        )
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not permitted: {value}")


def _check_json_depth(value: object, *, depth: int = 1) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("private catalog JSON exceeds its nesting bound")
    if isinstance(value, dict):
        for child in value.values():
            _check_json_depth(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json_depth(child, depth=depth + 1)


__all__ = [
    "AiobotoHippiusPrivateInputReader",
    "HippiusPrivateInputReader",
    "HippiusPrivateInputRetrievalConfig",
    "HippiusPrivateInputRetrievalError",
    "HippiusPrivateInputRetrievalIntegrity",
    "HippiusPrivateInputRetrievalUnavailable",
    "HippiusPrivateInputRetriever",
    "HippiusPrivateInputTicketAuthority",
    "HippiusPrivateInputUnwrapRequest",
    "HippiusPrivateInputUnwrapResult",
    "HippiusPrivateInputUnwrapper",
    "parse_hippius_private_input_retrieval_config",
]
