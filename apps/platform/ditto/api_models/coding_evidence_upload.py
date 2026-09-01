"""Ticket-scoped sealed-evidence upload contracts for shadow coding."""

from __future__ import annotations

import base64
import ipaddress
import json
import posixpath
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from pydantic import (
    AfterValidator,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ditto.api_models.coding_evaluation import CodingEvaluationModel, Sha256

_MAX_JSON_BYTES = 32 << 10
_MAX_SIGNED_URL_BYTES = 16 << 10
_MAX_CAPABILITY_SECONDS = 300
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_PATTERN = r"^[0-9a-fA-F]{128}$"
_CHECKSUM_PATTERN = r"^[A-Za-z0-9+/]{43}=$"
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)


def _bounded_instance_id(value: str) -> str:
    if len(value.encode()) > 128 or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("coding evidence instance identity is invalid")
    return value


InstanceId = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(_bounded_instance_id),
]


class CodingSealedEvidenceKind(StrEnum):
    AUTHORING_TRANSCRIPT = "authoring-transcript"
    FROZEN_SUBMISSION = "frozen-submission"
    AUTHORING_PUBLICATION_REQUEST = "authoring-publication-request"
    AUTHORING_PUBLICATION_ACKNOWLEDGEMENT = "authoring-publication-acknowledgement"
    TERMINAL_PUBLICATION_REQUEST = "terminal-publication-request"
    TERMINAL_PUBLICATION_ACKNOWLEDGEMENT = "terminal-publication-acknowledgement"


CODING_SEALED_EVIDENCE_MAX_BYTES = {
    CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT: 512 << 20,
    CodingSealedEvidenceKind.FROZEN_SUBMISSION: 128 << 20,
    CodingSealedEvidenceKind.AUTHORING_PUBLICATION_REQUEST: 4 << 20,
    CodingSealedEvidenceKind.AUTHORING_PUBLICATION_ACKNOWLEDGEMENT: 1 << 20,
    CodingSealedEvidenceKind.TERMINAL_PUBLICATION_REQUEST: 4 << 20,
    CodingSealedEvidenceKind.TERMINAL_PUBLICATION_ACKNOWLEDGEMENT: 1 << 20,
}


class CodingSealedEvidenceUploadCapability(CodingEvaluationModel):
    schema_name: Literal["dittobench-coding-sealed-evidence-upload-capability-v1"] = (
        Field(alias="schema")
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    ticket_id: UUID
    claim_generation: Annotated[int, Field(strict=True, ge=1, le=(1 << 31) - 1)]
    ticket_deadline: datetime
    upload_id: UUID
    evidence_kind: CodingSealedEvidenceKind
    sha256: Sha256
    size_bytes: Annotated[int, Field(strict=True, ge=1)]
    content_type: Literal["application/octet-stream"]
    checksum_sha256_b64: Annotated[str, Field(pattern=_CHECKSUM_PATTERN)]
    url: Annotated[
        str, Field(min_length=1, max_length=_MAX_SIGNED_URL_BYTES, repr=False)
    ]
    expires_at: datetime

    @field_validator("ticket_deadline", "expires_at", mode="before")
    @classmethod
    def timestamp_input_is_strict(cls, value: Any) -> Any:
        if isinstance(value, str) and _RFC3339.fullmatch(value) is None:
            raise ValueError("coding evidence timestamps must use RFC3339")
        if not isinstance(value, str | datetime):
            raise ValueError("coding evidence timestamps must be strings or datetimes")
        return value

    @field_validator("ticket_deadline", "expires_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding evidence timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def capability_is_coherent(self) -> CodingSealedEvidenceUploadCapability:
        if self.ticket_id.int == 0 or self.upload_id.int == 0:
            raise ValueError("coding evidence UUID authority is invalid")
        if self.expires_at.microsecond != 0 or self.expires_at > self.ticket_deadline:
            raise ValueError("coding evidence expiry is outside ticket authority")
        if self.size_bytes > CODING_SEALED_EVIDENCE_MAX_BYTES[self.evidence_kind]:
            raise ValueError("coding evidence size exceeds its kind bound")
        try:
            checksum = base64.b64decode(self.checksum_sha256_b64, validate=True)
        except ValueError as error:  # pragma: no cover - regex rejects this first
            raise ValueError("coding evidence checksum is invalid") from error
        if checksum != bytes.fromhex(self.sha256):
            raise ValueError("coding evidence checksum disagrees with SHA-256")
        _validate_signed_url(self)
        return self


class _SignedEvidenceRequest(CodingEvaluationModel):
    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    instance_id: InstanceId
    ticket_id: UUID
    claim_generation: Annotated[int, Field(strict=True, ge=1, le=(1 << 31) - 1)]
    evidence_kind: CodingSealedEvidenceKind
    sha256: Sha256
    size_bytes: Annotated[int, Field(strict=True, ge=1)]
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding evidence request timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def request_is_coherent(self) -> _SignedEvidenceRequest:
        if self.ticket_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding evidence request UUID authority is invalid")
        if self.size_bytes > CODING_SEALED_EVIDENCE_MAX_BYTES[self.evidence_kind]:
            raise ValueError("coding evidence request size exceeds its kind bound")
        return self


class CodingSealedEvidenceUploadCapabilityRequest(_SignedEvidenceRequest):
    """Signed validator request for one evidence PUT capability."""


class CodingSealedEvidenceFinalizeRequest(_SignedEvidenceRequest):
    """Signed validator request to finalize one already uploaded object."""

    upload_id: UUID

    @model_validator(mode="after")
    def finalization_is_coherent(self) -> CodingSealedEvidenceFinalizeRequest:
        if self.upload_id.int == 0:
            raise ValueError("coding evidence upload ID is invalid")
        return self


class CodingSealedEvidenceFinalization(CodingEvaluationModel):
    schema_name: Literal["dittobench-coding-sealed-evidence-finalized-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    ticket_id: UUID
    claim_generation: Annotated[int, Field(strict=True, ge=1, le=(1 << 31) - 1)]
    upload_id: UUID
    evidence_kind: CodingSealedEvidenceKind
    sha256: Sha256
    size_bytes: Annotated[int, Field(strict=True, ge=1)]
    finalized_at: datetime
    accepted: Literal[True]
    idempotent: bool

    @field_validator("finalized_at")
    @classmethod
    def finalized_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "coding evidence finalization timestamp must be timezone-aware"
            )
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def finalization_is_coherent(self) -> CodingSealedEvidenceFinalization:
        if self.ticket_id.int == 0 or self.upload_id.int == 0:
            raise ValueError("coding evidence finalization UUID authority is invalid")
        if self.size_bytes > CODING_SEALED_EVIDENCE_MAX_BYTES[self.evidence_kind]:
            raise ValueError("coding evidence finalization size exceeds its kind bound")
        return self


def coding_sealed_evidence_upload_signing_message(
    *,
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    evidence_kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return _signing_message(
        domain="dittobench-coding-sealed-evidence-upload-capability:v1",
        upload_id=None,
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        ticket_id=ticket_id,
        claim_generation=claim_generation,
        evidence_kind=evidence_kind,
        sha256=sha256,
        size_bytes=size_bytes,
        nonce=nonce,
        requested_at=requested_at,
    )


def coding_sealed_evidence_finalize_signing_message(
    *,
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    upload_id: UUID,
    evidence_kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return _signing_message(
        domain="dittobench-coding-sealed-evidence-finalize:v1",
        upload_id=upload_id,
        validator_hotkey=validator_hotkey,
        instance_id=instance_id,
        ticket_id=ticket_id,
        claim_generation=claim_generation,
        evidence_kind=evidence_kind,
        sha256=sha256,
        size_bytes=size_bytes,
        nonce=nonce,
        requested_at=requested_at,
    )


def parse_coding_sealed_evidence_upload_capability_json(
    raw: str | bytes,
) -> CodingSealedEvidenceUploadCapability:
    body = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not 1 <= len(body) <= _MAX_JSON_BYTES:
        raise ValueError("coding evidence capability JSON size is outside bounds")
    try:
        decoded = json.loads(
            body, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except RecursionError as error:
        raise ValueError(
            "coding evidence capability JSON nesting exceeds bounds"
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("coding evidence capability JSON is invalid") from error
    _validate_json_tree(decoded, depth=0)
    if not isinstance(decoded, dict):
        raise ValueError("coding evidence capability JSON must be an object")
    try:
        return CodingSealedEvidenceUploadCapability.model_validate(decoded)
    except ValidationError:
        raise ValueError(
            "coding evidence capability known fields are invalid"
        ) from None


def _signing_message(
    *,
    domain: str,
    validator_hotkey: str,
    instance_id: str,
    ticket_id: UUID,
    claim_generation: int,
    evidence_kind: CodingSealedEvidenceKind,
    sha256: str,
    size_bytes: int,
    upload_id: UUID | None,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    if (
        ticket_id.int == 0
        or nonce.int == 0
        or (upload_id is not None and upload_id.int == 0)
        or not 1 <= claim_generation <= (1 << 31) - 1
        or not 1 <= size_bytes <= CODING_SEALED_EVIDENCE_MAX_BYTES[evidence_kind]
        or len(instance_id.encode()) > 128
        or any(
            character.isspace() or unicodedata.category(character) == "Cc"
            for character in instance_id
        )
        or requested_at.tzinfo is None
        or requested_at.utcoffset() is None
    ):
        raise ValueError("coding evidence signing authority is invalid")
    values = [
        domain,
        validator_hotkey,
        instance_id,
        str(ticket_id),
        str(claim_generation),
        evidence_kind.value,
        sha256,
        str(size_bytes),
    ]
    if upload_id is not None:
        values.append(str(upload_id))
    values.extend(
        (str(nonce), requested_at.astimezone(UTC).isoformat(timespec="microseconds"))
    )
    return "\x00".join(values).encode()


def _validate_signed_url(capability: CodingSealedEvidenceUploadCapability) -> None:
    value = capability.url
    if (
        len(value.encode()) > _MAX_SIGNED_URL_BYTES
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("coding evidence URL is outside bounds")
    try:
        parsed, port = urlparse(value), urlparse(value).port
    except ValueError as error:
        raise ValueError("coding evidence URL is invalid") from error
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.query
        or ";" in parsed.query
        or not _valid_percent_encoding(parsed.query)
        or (port is not None and not 1 <= port <= 65_535)
        or "%" in parsed.path
        or "//" in parsed.path
        or posixpath.normpath(parsed.path) != parsed.path
    ):
        raise ValueError("coding evidence URL is invalid")
    if parsed.scheme == "http" and not _loopback(hostname):
        raise ValueError("coding evidence URL requires HTTPS outside loopback")
    expected = (
        f"/coding-evidence/v1/{capability.evidence_kind.value}"
        f"/sha256/{capability.sha256}"
    )
    if not parsed.path.endswith(expected):
        raise ValueError("coding evidence URL path disagrees with known fields")
    query: dict[str, list[str]] = {}
    for name, values in parse_qs(parsed.query).items():
        query.setdefault(name.lower(), []).extend(values)
    if (
        sum(len(values) for values in query.values()) > 64
        or _signed_expiry(query) != capability.expires_at
    ):
        raise ValueError("coding evidence signed expiry disagrees with known fields")


def _signed_expiry(query: dict[str, list[str]]) -> datetime:
    v4, v2 = query.get("x-amz-signature", []), query.get("signature", [])
    if bool(v4) == bool(v2):
        raise ValueError("coding evidence signature fields are ambiguous")
    if v4:
        dates, durations = query.get("x-amz-date", []), query.get("x-amz-expires", [])
        if len(v4) != 1 or len(dates) != 1 or len(durations) != 1:
            raise ValueError("coding evidence v4 signature fields are invalid")
        try:
            signed_at = datetime.strptime(dates[0], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=UTC
            )
            duration = _parse_decimal(durations[0])
        except ValueError as error:
            raise ValueError("coding evidence v4 expiry is invalid") from error
        if not 60 <= duration <= _MAX_CAPABILITY_SECONDS:
            raise ValueError("coding evidence v4 expiry is outside bounds")
        return signed_at + timedelta(seconds=duration)
    expires = query.get("expires", [])
    if len(v2) != 1 or len(expires) != 1:
        raise ValueError("coding evidence v2 signature fields are invalid")
    return datetime.fromtimestamp(_parse_decimal(expires[0]), tz=UTC)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"coding evidence capability JSON repeats field {key!r}")
        output[key] = value
    return output


def _reject_constant(value: str) -> Any:
    raise ValueError(f"coding evidence capability JSON has non-finite {value}")


def _validate_json_tree(value: Any, *, depth: int) -> None:
    if depth > 32:
        raise ValueError("coding evidence capability JSON nesting exceeds 32 levels")
    if isinstance(value, str) and any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise ValueError(
            "coding evidence capability JSON contains an unpaired surrogate"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_tree(key, depth=depth + 1)
            _validate_json_tree(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_json_tree(item, depth=depth + 1)


def _loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _valid_percent_encoding(value: str) -> bool:
    hex_digits = frozenset("0123456789abcdefABCDEF")
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
        elif (
            index + 2 >= len(value)
            or value[index + 1] not in hex_digits
            or value[index + 2] not in hex_digits
        ):
            return False
        else:
            index += 3
    return True


def _parse_decimal(value: str) -> int:
    if not value or any(character not in "0123456789" for character in value):
        raise ValueError("coding evidence expiry must be ASCII decimal")
    return int(value)


__all__ = [
    "CODING_SEALED_EVIDENCE_MAX_BYTES",
    "CodingSealedEvidenceFinalizeRequest",
    "CodingSealedEvidenceFinalization",
    "CodingSealedEvidenceKind",
    "CodingSealedEvidenceUploadCapability",
    "CodingSealedEvidenceUploadCapabilityRequest",
    "coding_sealed_evidence_finalize_signing_message",
    "coding_sealed_evidence_upload_signing_message",
    "parse_coding_sealed_evidence_upload_capability_json",
]
