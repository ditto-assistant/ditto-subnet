"""Default-off one-time mediator for encrypted Hippius Coding evidence."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, Protocol
from urllib.parse import urlparse
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_evidence import (
    CODING_SEALED_EVIDENCE_MAX_BYTES,
    CodingSealedEvidenceIdentity,
    CodingSealedEvidenceKind,
    coding_sealed_evidence_identity_digest,
    validate_coding_evidence_safe_scalar,
)
from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_REGION,
    HippiusProbeCredential,
    hippius_sealed_evidence_authority_sha256,
    load_hippius_probe_receipt,
)
from ditto.db.queries.coding_evidence import (
    CodingSealedEvidenceConflictError as LedgerConflictError,
)
from ditto.db.queries.coding_evidence import (
    CodingSealedEvidenceNotAvailableError as LedgerNotAvailableError,
)
from ditto.db.queries.coding_evidence import (
    finalize_coding_sealed_evidence,
    reserve_coding_sealed_evidence,
)

_CONFIG_PREFIX = "DITTO_CODING_HIPPIUS_"
_REMOTE_PREFIX = "coding-sealed-evidence/v1"
_AAD_SCHEMA = "dittobench-coding-hippius-sealed-evidence-aad-v1"
_ENVELOPE_SCHEMA = "dittobench-coding-hippius-sealed-evidence-envelope-v1"
_RECEIPT_SCHEMA = "dittobench-coding-hippius-sealed-evidence-finalization-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_SS58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{47,48}$")
_MAX_ENVELOPE_BYTES = 32 << 10
_MAX_WRAPPED_KEY_BYTES = 16 << 10
_MAX_ACCESS_ID_BYTES = 1 << 9
_MAX_SECRET_BYTES = 1 << 12
_PROBE_MAX_AGE = timedelta(hours=24)
_CLOCK_SKEW = timedelta(minutes=5)


class HippiusSealedEvidenceError(RuntimeError):
    """Base mediator error with no provider or evidence identifiers."""


class HippiusSealedEvidenceUnavailable(HippiusSealedEvidenceError):
    """The exact object, provider, or durable authority is unavailable."""


class HippiusSealedEvidenceConflict(HippiusSealedEvidenceError):
    """Existing storage or ledger authority names different immutable bytes."""


class HippiusSealedEvidenceNotFound(HippiusSealedEvidenceUnavailable):
    """The exact derived object is absent from Hippius."""


class HippiusSealedEvidenceStatus(StrEnum):
    UPLOADED = "uploaded"
    REUSED = "reused"


@dataclass(frozen=True, repr=False)
class HippiusSealedEvidenceConfig:
    endpoint_url: str = field(repr=False)
    bucket: str = field(repr=False)
    mediator: HippiusProbeCredential = field(repr=False)
    region: str = HIPPIUS_REGION
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        try:
            endpoint = urlparse(self.endpoint_url)
            port = endpoint.port
        except ValueError as error:
            raise HippiusSealedEvidenceConflict(
                "Hippius evidence endpoint is invalid"
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
            or not _safe_credential(self.mediator)
            or not 1.0 <= self.timeout_seconds <= 60.0
        ):
            raise HippiusSealedEvidenceConflict(
                "Hippius evidence configuration is unsafe"
            )

    @property
    def authority_sha256(self) -> str:
        return hippius_sealed_evidence_authority_sha256(
            endpoint_url=self.endpoint_url,
            region=self.region,
            bucket=self.bucket,
            mediator_access_key=self.mediator.access_key,
        )

    def __repr__(self) -> str:
        return (
            "HippiusSealedEvidenceConfig(configured=True, "
            f"region={self.region!r}, timeout_seconds={self.timeout_seconds!r})"
        )


@dataclass(frozen=True, repr=False)
class HippiusSealedEvidenceSourceAuthority:
    ticket_id: UUID
    claim_generation: int
    validator_hotkey: str
    instance_id: str
    ticket_deadline: datetime
    evidence_kind: CodingSealedEvidenceKind
    weight_eligible: bool = False

    def __repr__(self) -> str:
        return "HippiusSealedEvidenceSourceAuthority(ticket_bound=True)"


@dataclass(frozen=True, repr=False)
class HippiusSealedEvidencePreparedObject:
    identity: CodingSealedEvidenceIdentity
    remote_key: str = field(repr=False)
    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    wrapped_data_key: bytes = field(repr=False)

    def __repr__(self) -> str:
        return "HippiusSealedEvidencePreparedObject(sealed=True)"


@dataclass(frozen=True)
class HippiusSealedEvidenceReceipt:
    schema: str
    reservation_id: UUID
    identity_sha256: str
    object_key_sha256: str
    ciphertext_sha256: str
    ciphertext_size_bytes: int
    envelope_sha256: str
    probe_receipt_payload_sha256: str
    status: HippiusSealedEvidenceStatus
    finalized_at: str
    ready: bool
    weight_eligible: bool


class HippiusSealedEvidenceKeyWrapper(Protocol):
    @property
    def wrapping_key_sha256(self) -> str: ...

    async def wrap_data_key(self, *, data_key: bytes, aad_sha256: str) -> bytes: ...


class HippiusSealedEvidenceTransport(Protocol):
    async def get_object(self, *, key: str, max_bytes: int) -> bytes: ...

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None: ...


class HippiusSealedEvidenceLedger(Protocol):
    async def reserve(self, identity: CodingSealedEvidenceIdentity) -> None: ...

    async def finalize(
        self,
        identity: CodingSealedEvidenceIdentity,
        status: HippiusSealedEvidenceStatus,
    ) -> HippiusSealedEvidenceStatus: ...


class PostgresHippiusSealedEvidenceLedger:
    """Short transactions around storage I/O; no transaction crosses the network."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self._session_maker = session_maker

    async def reserve(self, identity: CodingSealedEvidenceIdentity) -> None:
        try:
            async with self._session_maker() as session, session.begin():
                await reserve_coding_sealed_evidence(session, identity=identity)
        except LedgerConflictError as error:
            raise HippiusSealedEvidenceConflict(
                "coding evidence reservation conflicts"
            ) from error
        except LedgerNotAvailableError as error:
            raise HippiusSealedEvidenceUnavailable(
                "coding evidence reservation is unavailable"
            ) from error

    async def finalize(
        self,
        identity: CodingSealedEvidenceIdentity,
        status: HippiusSealedEvidenceStatus,
    ) -> HippiusSealedEvidenceStatus:
        try:
            async with self._session_maker() as session, session.begin():
                result = await finalize_coding_sealed_evidence(
                    session,
                    identity=identity,
                    storage_status=status.value,
                )
                return HippiusSealedEvidenceStatus(result.finalization.storage_status)
        except LedgerConflictError as error:
            raise HippiusSealedEvidenceConflict(
                "coding evidence finalization conflicts"
            ) from error
        except LedgerNotAvailableError as error:
            raise HippiusSealedEvidenceUnavailable(
                "coding evidence finalization is unavailable"
            ) from error


class AiobotoHippiusSealedEvidenceTransport:
    """Evidence-only S3 client exposing no list, delete, or arbitrary bucket API."""

    def __init__(self, config: HippiusSealedEvidenceConfig) -> None:
        import aioboto3
        from botocore.config import Config

        self._config = config
        self._session = aioboto3.Session(
            aws_access_key_id=config.mediator.access_key,
            aws_secret_access_key=config.mediator.secret_key,
            region_name=config.region,
        )
        self._client_config = Config(
            signature_version="s3v4",
            connect_timeout=config.timeout_seconds,
            read_timeout=config.timeout_seconds,
            retries={"max_attempts": 1, "mode": "standard"},
            proxies={},
            request_checksum_calculation="when_required",
            s3={"addressing_style": "path"},
        )

    async def __aenter__(self) -> AiobotoHippiusSealedEvidenceTransport:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        return None

    def _client(self):
        return self._session.client(
            "s3",
            endpoint_url=self._config.endpoint_url,
            use_ssl=True,
            config=self._client_config,
        )

    async def get_object(self, *, key: str, max_bytes: int) -> bytes:
        try:
            async with self._client() as s3:
                response = await s3.get_object(Bucket=self._config.bucket, Key=key)
                stream = response["Body"]
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = await stream.read(min(64 << 10, max_bytes + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise HippiusSealedEvidenceConflict(
                            "Hippius evidence download exceeded its bound"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except HippiusSealedEvidenceError:
            raise
        except Exception as error:
            _raise_safe_provider_error(error)

    async def put_object(
        self,
        *,
        key: str,
        body: bytes,
        metadata: Mapping[str, str],
    ) -> None:
        try:
            async with self._client() as s3:
                await s3.put_object(
                    Bucket=self._config.bucket,
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


async def prepare_hippius_sealed_evidence(
    *,
    authority: HippiusSealedEvidenceSourceAuthority,
    plaintext: bytes,
    key_wrapper: HippiusSealedEvidenceKeyWrapper,
    reservation_id: UUID | None = None,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> HippiusSealedEvidencePreparedObject:
    """Create fresh exact ciphertext which the caller must durably retain."""

    authority = _validated_source_authority(authority)
    if not isinstance(plaintext, bytes):
        raise HippiusSealedEvidenceConflict("coding evidence plaintext is invalid")
    maximum = CODING_SEALED_EVIDENCE_MAX_BYTES[authority.evidence_kind]
    if not 1 <= len(plaintext) <= maximum:
        raise HippiusSealedEvidenceConflict(
            "coding evidence plaintext exceeds its kind bound"
        )
    wrapping_key_sha256 = key_wrapper.wrapping_key_sha256
    if _SHA256.fullmatch(wrapping_key_sha256) is None:
        raise HippiusSealedEvidenceConflict(
            "coding evidence wrapping authority is invalid"
        )
    resolved_reservation_id = uuid4() if reservation_id is None else reservation_id
    if (
        not isinstance(resolved_reservation_id, UUID)
        or resolved_reservation_id.int == 0
    ):
        raise HippiusSealedEvidenceConflict(
            "coding evidence reservation identity is invalid"
        )
    plaintext_sha256 = hashlib.sha256(plaintext).hexdigest()
    aad = _evidence_aad(
        authority=authority,
        reservation_id=resolved_reservation_id,
        plaintext_sha256=plaintext_sha256,
        plaintext_size_bytes=len(plaintext),
        wrapping_key_sha256=wrapping_key_sha256,
    )
    aad_sha256 = hashlib.sha256(aad).hexdigest()
    data_key = random_bytes(32)
    nonce = random_bytes(12)
    if len(data_key) != 32 or len(nonce) != 12:
        raise HippiusSealedEvidenceConflict(
            "coding evidence entropy source returned the wrong byte count"
        )
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, aad)
    try:
        wrapped_data_key = await key_wrapper.wrap_data_key(
            data_key=data_key,
            aad_sha256=aad_sha256,
        )
    except HippiusSealedEvidenceError:
        raise
    except Exception as error:
        raise HippiusSealedEvidenceUnavailable(
            "coding evidence key wrapping failed"
        ) from error
    finally:
        mutable_key = bytearray(data_key)
        mutable_key[:] = b"\x00" * len(mutable_key)
    if (
        not isinstance(wrapped_data_key, bytes)
        or not 1 <= len(wrapped_data_key) <= _MAX_WRAPPED_KEY_BYTES
    ):
        raise HippiusSealedEvidenceConflict(
            "coding evidence wrapped data key is invalid"
        )
    ciphertext_sha256 = hashlib.sha256(ciphertext).hexdigest()
    remote_key = hippius_sealed_evidence_remote_key(
        reservation_id=resolved_reservation_id,
        evidence_kind=authority.evidence_kind,
        ciphertext_sha256=ciphertext_sha256,
    )
    envelope_sha256 = hashlib.sha256(
        coding_canonical_json_bytes(
            {
                "aad_sha256": aad_sha256,
                "nonce_b64": base64.b64encode(nonce).decode("ascii"),
                "schema": _ENVELOPE_SCHEMA,
                "wrapped_data_key_b64": base64.b64encode(wrapped_data_key).decode(
                    "ascii"
                ),
                "wrapping_key_sha256": wrapping_key_sha256,
            },
            maximum_bytes=_MAX_ENVELOPE_BYTES,
            label="Hippius sealed-evidence envelope",
        )
    ).hexdigest()
    identity_values = {
        "schema": "dittobench-coding-sealed-evidence-identity-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "reservation_id": resolved_reservation_id,
        "ticket_id": authority.ticket_id,
        "claim_generation": authority.claim_generation,
        "validator_hotkey": authority.validator_hotkey,
        "instance_id": authority.instance_id,
        "ticket_deadline": authority.ticket_deadline,
        "evidence_kind": authority.evidence_kind,
        "plaintext_sha256": plaintext_sha256,
        "plaintext_size_bytes": len(plaintext),
        "ciphertext_sha256": ciphertext_sha256,
        "ciphertext_size_bytes": len(ciphertext),
        "object_key_sha256": hashlib.sha256(remote_key.encode()).hexdigest(),
        "envelope_sha256": envelope_sha256,
        "wrapping_key_sha256": wrapping_key_sha256,
        "aad_sha256": aad_sha256,
        "identity_sha256": "0" * 64,
    }
    draft = CodingSealedEvidenceIdentity.model_construct(
        _fields_set=set(identity_values),
        **identity_values,
    )
    identity_values["identity_sha256"] = coding_sealed_evidence_identity_digest(draft)
    identity = CodingSealedEvidenceIdentity.model_validate(identity_values)
    return _validated_prepared(
        HippiusSealedEvidencePreparedObject(
            identity=identity,
            remote_key=remote_key,
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_data_key=wrapped_data_key,
        )
    )


class HippiusSealedEvidenceMediator:
    def __init__(
        self,
        *,
        config: HippiusSealedEvidenceConfig,
        probe_receipt_path: Path,
        transport: HippiusSealedEvidenceTransport,
        ledger: HippiusSealedEvidenceLedger,
    ) -> None:
        try:
            probe_receipt, probe_payload_sha256 = load_hippius_probe_receipt(
                probe_receipt_path
            )
        except Exception as error:
            raise HippiusSealedEvidenceConflict(
                "Hippius evidence probe receipt is invalid"
            ) from error
        if probe_receipt.sealed_evidence_authority_sha256 != config.authority_sha256:
            raise HippiusSealedEvidenceConflict(
                "Hippius evidence probe does not bind the mediator authority"
            )
        self._config = config
        self._probe_receipt = probe_receipt
        self._probe_payload_sha256 = probe_payload_sha256
        self._transport = transport
        self._ledger = ledger

    async def publish(
        self,
        *,
        prepared: HippiusSealedEvidencePreparedObject,
        now: datetime | None = None,
    ) -> HippiusSealedEvidenceReceipt:
        checked_at = _utc_now(now)
        prepared = _validated_prepared(prepared)
        probe_checked_at = datetime.fromisoformat(
            self._probe_receipt.checked_at.replace("Z", "+00:00")
        ).astimezone(UTC)
        if (
            probe_checked_at > checked_at + _CLOCK_SKEW
            or checked_at - probe_checked_at > _PROBE_MAX_AGE
        ):
            raise HippiusSealedEvidenceUnavailable(
                "Hippius evidence probe is outside its freshness window"
            )
        if checked_at >= prepared.identity.ticket_deadline:
            raise HippiusSealedEvidenceUnavailable(
                "coding evidence ticket is no longer active"
            )
        try:
            await self._ledger.reserve(prepared.identity)
        except HippiusSealedEvidenceError:
            raise
        except Exception as error:
            raise HippiusSealedEvidenceUnavailable(
                "coding evidence reservation failed"
            ) from error
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                existing = await self._transport.get_object(
                    key=prepared.remote_key,
                    max_bytes=prepared.identity.ciphertext_size_bytes,
                )
        except HippiusSealedEvidenceNotFound:
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    await self._transport.put_object(
                        key=prepared.remote_key,
                        body=prepared.ciphertext,
                        metadata={
                            "ciphertext-sha256": prepared.identity.ciphertext_sha256,
                            "evidence-kind": prepared.identity.evidence_kind.value,
                            "identity-sha256": prepared.identity.identity_sha256,
                        },
                    )
            except HippiusSealedEvidenceError:
                raise
            except Exception as error:
                raise HippiusSealedEvidenceUnavailable(
                    "Hippius evidence upload failed"
                ) from error
            status = HippiusSealedEvidenceStatus.UPLOADED
        except HippiusSealedEvidenceError:
            raise
        except Exception as error:
            raise HippiusSealedEvidenceUnavailable(
                "Hippius evidence preflight failed"
            ) from error
        else:
            _verify_ciphertext(existing, prepared.identity)
            status = HippiusSealedEvidenceStatus.REUSED
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                verified = await self._transport.get_object(
                    key=prepared.remote_key,
                    max_bytes=prepared.identity.ciphertext_size_bytes,
                )
        except HippiusSealedEvidenceError:
            raise
        except Exception as error:
            raise HippiusSealedEvidenceUnavailable(
                "Hippius evidence verification failed"
            ) from error
        _verify_ciphertext(verified, prepared.identity)
        if _utc_now(now) >= prepared.identity.ticket_deadline:
            raise HippiusSealedEvidenceUnavailable(
                "coding evidence ticket expired before finalization"
            )
        try:
            finalized_status = await self._ledger.finalize(prepared.identity, status)
        except HippiusSealedEvidenceError:
            raise
        except Exception as error:
            raise HippiusSealedEvidenceUnavailable(
                "coding evidence finalization failed"
            ) from error
        return HippiusSealedEvidenceReceipt(
            schema=_RECEIPT_SCHEMA,
            reservation_id=prepared.identity.reservation_id,
            identity_sha256=prepared.identity.identity_sha256,
            object_key_sha256=prepared.identity.object_key_sha256,
            ciphertext_sha256=prepared.identity.ciphertext_sha256,
            ciphertext_size_bytes=prepared.identity.ciphertext_size_bytes,
            envelope_sha256=prepared.identity.envelope_sha256,
            probe_receipt_payload_sha256=self._probe_payload_sha256,
            status=finalized_status,
            finalized_at=checked_at.isoformat().replace("+00:00", "Z"),
            ready=True,
            weight_eligible=False,
        )


def hippius_sealed_evidence_remote_key(
    *,
    reservation_id: UUID,
    evidence_kind: CodingSealedEvidenceKind,
    ciphertext_sha256: str,
) -> str:
    if (
        not isinstance(reservation_id, UUID)
        or reservation_id.int == 0
        or not isinstance(evidence_kind, CodingSealedEvidenceKind)
        or _SHA256.fullmatch(ciphertext_sha256) is None
    ):
        raise HippiusSealedEvidenceConflict(
            "coding evidence remote identity is invalid"
        )
    return (
        f"{_REMOTE_PREFIX}/{reservation_id.hex}/{evidence_kind.value}/"
        f"{ciphertext_sha256}.bin"
    )


def parse_hippius_sealed_evidence_config(
    environ: Mapping[str, str] | None = None,
) -> HippiusSealedEvidenceConfig:
    values = os.environ if environ is None else environ

    def required(suffix: str) -> str:
        name = f"{_CONFIG_PREFIX}{suffix}"
        value = values.get(name, "")
        if not value:
            raise HippiusSealedEvidenceConflict(
                f"required Hippius evidence setting is missing: {name}"
            )
        return value

    try:
        timeout_seconds = float(values.get(f"{_CONFIG_PREFIX}TIMEOUT_SECONDS", "20"))
    except ValueError as error:
        raise HippiusSealedEvidenceConflict(
            "Hippius evidence timeout is malformed"
        ) from error
    return HippiusSealedEvidenceConfig(
        endpoint_url=required("ENDPOINT_URL"),
        bucket=required("SEALED_EVIDENCE_BUCKET"),
        mediator=HippiusProbeCredential(
            access_key=required("EVIDENCE_MEDIATOR_ACCESS_KEY"),
            secret_key=required("EVIDENCE_MEDIATOR_SECRET_KEY"),
        ),
        region=values.get(f"{_CONFIG_PREFIX}REGION", HIPPIUS_REGION),
        timeout_seconds=timeout_seconds,
    )


def _evidence_aad(
    *,
    authority: HippiusSealedEvidenceSourceAuthority,
    reservation_id: UUID,
    plaintext_sha256: str,
    plaintext_size_bytes: int,
    wrapping_key_sha256: str,
) -> bytes:
    return coding_canonical_json_bytes(
        {
            "aad_schema": _AAD_SCHEMA,
            "claim_generation": authority.claim_generation,
            "evidence_kind": authority.evidence_kind.value,
            "instance_id": authority.instance_id,
            "plaintext_sha256": plaintext_sha256,
            "plaintext_size_bytes": plaintext_size_bytes,
            "reservation_id": str(reservation_id),
            "ticket_deadline": authority.ticket_deadline.isoformat().replace(
                "+00:00", "Z"
            ),
            "ticket_id": str(authority.ticket_id),
            "validator_hotkey": authority.validator_hotkey,
            "weight_eligible": False,
            "wrapping_key_sha256": wrapping_key_sha256,
        },
        maximum_bytes=_MAX_ENVELOPE_BYTES,
        label="Hippius sealed-evidence AAD",
    )


def _validated_source_authority(
    authority: HippiusSealedEvidenceSourceAuthority,
) -> HippiusSealedEvidenceSourceAuthority:
    if (
        not isinstance(authority.ticket_id, UUID)
        or authority.ticket_id.int == 0
        or isinstance(authority.claim_generation, bool)
        or not isinstance(authority.claim_generation, int)
        or not 1 <= authority.claim_generation <= (1 << 31) - 1
        or not isinstance(authority.validator_hotkey, str)
        or _SS58.fullmatch(authority.validator_hotkey) is None
        or not isinstance(authority.instance_id, str)
        or not validate_coding_evidence_safe_scalar(
            authority.instance_id, maximum_bytes=128
        )
        or not isinstance(authority.ticket_deadline, datetime)
        or authority.ticket_deadline.tzinfo is None
        or authority.ticket_deadline.utcoffset() is None
        or not isinstance(authority.evidence_kind, CodingSealedEvidenceKind)
        or authority.weight_eligible is not False
    ):
        raise HippiusSealedEvidenceConflict(
            "coding evidence source authority is invalid"
        )
    return HippiusSealedEvidenceSourceAuthority(
        ticket_id=authority.ticket_id,
        claim_generation=authority.claim_generation,
        validator_hotkey=authority.validator_hotkey,
        instance_id=authority.instance_id,
        ticket_deadline=authority.ticket_deadline.astimezone(UTC),
        evidence_kind=authority.evidence_kind,
        weight_eligible=False,
    )


def _validated_prepared(
    prepared: HippiusSealedEvidencePreparedObject,
) -> HippiusSealedEvidencePreparedObject:
    try:
        identity = CodingSealedEvidenceIdentity.model_validate_json(
            prepared.identity.model_dump_json(by_alias=True)
        )
    except (AttributeError, ValueError) as error:
        raise HippiusSealedEvidenceConflict(
            "prepared coding evidence identity is invalid"
        ) from error
    expected_key = hippius_sealed_evidence_remote_key(
        reservation_id=identity.reservation_id,
        evidence_kind=identity.evidence_kind,
        ciphertext_sha256=identity.ciphertext_sha256,
    )
    if (
        not isinstance(prepared.ciphertext, bytes)
        or len(prepared.ciphertext) != identity.ciphertext_size_bytes
        or hashlib.sha256(prepared.ciphertext).hexdigest() != identity.ciphertext_sha256
        or prepared.remote_key != expected_key
        or hashlib.sha256(expected_key.encode()).hexdigest()
        != identity.object_key_sha256
        or not isinstance(prepared.nonce, bytes)
        or len(prepared.nonce) != 12
        or not isinstance(prepared.wrapped_data_key, bytes)
        or not 1 <= len(prepared.wrapped_data_key) <= _MAX_WRAPPED_KEY_BYTES
    ):
        raise HippiusSealedEvidenceConflict(
            "prepared coding evidence bytes are inconsistent"
        )
    envelope_sha256 = hashlib.sha256(
        coding_canonical_json_bytes(
            {
                "aad_sha256": identity.aad_sha256,
                "nonce_b64": base64.b64encode(prepared.nonce).decode("ascii"),
                "schema": _ENVELOPE_SCHEMA,
                "wrapped_data_key_b64": base64.b64encode(
                    prepared.wrapped_data_key
                ).decode("ascii"),
                "wrapping_key_sha256": identity.wrapping_key_sha256,
            },
            maximum_bytes=_MAX_ENVELOPE_BYTES,
            label="Hippius sealed-evidence envelope",
        )
    ).hexdigest()
    if envelope_sha256 != identity.envelope_sha256:
        raise HippiusSealedEvidenceConflict(
            "prepared coding evidence envelope is inconsistent"
        )
    return HippiusSealedEvidencePreparedObject(
        identity=identity,
        remote_key=prepared.remote_key,
        ciphertext=prepared.ciphertext,
        nonce=prepared.nonce,
        wrapped_data_key=prepared.wrapped_data_key,
    )


def _verify_ciphertext(
    body: bytes,
    identity: CodingSealedEvidenceIdentity,
) -> None:
    if (
        not isinstance(body, bytes)
        or len(body) != identity.ciphertext_size_bytes
        or hashlib.sha256(body).hexdigest() != identity.ciphertext_sha256
    ):
        raise HippiusSealedEvidenceConflict(
            "Hippius evidence identity contains different bytes"
        )


def _safe_credential(credential: HippiusProbeCredential) -> bool:
    values = (credential.access_key, credential.secret_key)
    return (
        values[0].startswith("hip_")
        and validate_coding_evidence_safe_scalar(
            values[0], maximum_bytes=_MAX_ACCESS_ID_BYTES
        )
        and validate_coding_evidence_safe_scalar(
            values[1], maximum_bytes=_MAX_SECRET_BYTES
        )
    )


def _utc_now(value: datetime | None) -> datetime:
    resolved = datetime.now(UTC) if value is None else value
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise HippiusSealedEvidenceConflict(
            "coding evidence clock must be timezone-aware"
        )
    return resolved.astimezone(UTC)


def _raise_safe_provider_error(error: Exception) -> NoReturn:
    try:
        from botocore.exceptions import ClientError
    except ImportError as import_error:  # pragma: no cover
        raise HippiusSealedEvidenceUnavailable(
            "Hippius evidence dependency is unavailable"
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
            raise HippiusSealedEvidenceNotFound(
                "Hippius evidence object is unavailable"
            ) from error
    raise HippiusSealedEvidenceUnavailable(
        "Hippius evidence provider call failed"
    ) from error


__all__ = [
    "AiobotoHippiusSealedEvidenceTransport",
    "HippiusSealedEvidenceConfig",
    "HippiusSealedEvidenceConflict",
    "HippiusSealedEvidenceError",
    "HippiusSealedEvidenceKeyWrapper",
    "HippiusSealedEvidenceLedger",
    "HippiusSealedEvidenceMediator",
    "HippiusSealedEvidenceNotFound",
    "HippiusSealedEvidencePreparedObject",
    "HippiusSealedEvidenceReceipt",
    "HippiusSealedEvidenceSourceAuthority",
    "HippiusSealedEvidenceStatus",
    "HippiusSealedEvidenceTransport",
    "HippiusSealedEvidenceUnavailable",
    "PostgresHippiusSealedEvidenceLedger",
    "hippius_sealed_evidence_remote_key",
    "parse_hippius_sealed_evidence_config",
    "prepare_hippius_sealed_evidence",
]
