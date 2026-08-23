"""Validator-facing shadow coding inference grant transport."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_PATTERN = r"^[0-9a-fA-F]{128}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_BROKER_KEY_PATTERN = r"^[A-Za-z0-9_-]{43}=?$"
_BEARER_PATTERN = r"^[A-Za-z0-9_-]{32,128}$"
_MAX_URL_BYTES = 2_048


def _bounded_identifier(value: str, maximum: int) -> str:
    if len(value.encode()) > maximum or any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    ):
        raise ValueError("coding inference grant identifier is invalid")
    return value


OpaqueId = Annotated[
    str,
    Field(min_length=1, max_length=256),
    AfterValidator(lambda value: _bounded_identifier(value, 256)),
]
ShortName = Annotated[
    str,
    Field(min_length=1, max_length=128),
    AfterValidator(lambda value: _bounded_identifier(value, 128)),
]
Sha256 = Annotated[str, Field(pattern=_SHA256_PATTERN)]


class CodingInferenceGrantModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        serialize_by_alias=True,
        validate_by_name=True,
    )


def _validate_https_url(value: str, *, suffix: str) -> str:
    if len(value.encode()) > _MAX_URL_BYTES:
        raise ValueError("coding inference grant URL is too large")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("coding inference grant URL has an invalid port") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(suffix)
    ):
        raise ValueError("coding inference grant URL is not an approved HTTPS route")
    return value


class CodingInferenceGrantRequest(BaseModel):
    """Signed request for the one grant bound to a coding ticket."""

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    ticket_id: UUID
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding inference grant timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingInferenceGrantRequest:
        if self.ticket_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding inference grant UUID is nil")
        return self


class CodingInferenceGrantAuthority(CodingInferenceGrantModel):
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    grant_id: UUID
    ticket_id: UUID
    run_row_id: UUID
    case_id: OpaqueId
    profile_capability_id: OpaqueId
    inference_grant_sha256: Sha256
    model: Literal["openai/gpt-5.6-luna"]
    provider_api: Literal["openrouter"]
    provider_route: ShortName
    receipt_provider: ShortName
    provider_route_profile: ShortName
    provider_account_guardrail: Literal["openrouter_private_account_v1"]
    provider_pipeline_policy: Literal["no_plugins_no_transforms_v1"]
    provider_cache_policy: Literal["disabled_v1"]
    reasoning_effort: Literal["medium"]
    request_budget: Annotated[int, Field(strict=True, ge=1, le=256)]
    prompt_token_budget: Annotated[int, Field(strict=True, ge=1, le=2_000_000)]
    completion_token_budget: Annotated[int, Field(strict=True, ge=1, le=250_000)]
    cost_budget_usd_micros: Annotated[int, Field(strict=True, ge=1, le=100_000_000)]
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expires_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding inference grant expiry must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def authority_is_coherent(self) -> CodingInferenceGrantAuthority:
        if any(
            value.int == 0 for value in (self.grant_id, self.ticket_id, self.run_row_id)
        ):
            raise ValueError("coding inference grant authority UUID is nil")
        return self


class CodingInferenceGrantOffer(CodingInferenceGrantAuthority):
    schema_name: Literal["dittobench-coding-inference-grant-offer-v1"] = Field(
        alias="schema"
    )
    status: Literal["pending", "active"]
    generation: Annotated[int, Field(strict=True, ge=0, le=(1 << 31) - 1)]
    exchange_url: Annotated[str, Field(min_length=1, max_length=_MAX_URL_BYTES)]

    @field_validator("exchange_url")
    @classmethod
    def exchange_url_is_approved(cls, value: str) -> str:
        return _validate_https_url(
            value,
            suffix="/api/v1/validator/coding-shadow/inference-exchange",
        )

    @model_validator(mode="after")
    def offer_state_is_coherent(self) -> CodingInferenceGrantOffer:
        if (self.status == "pending") != (self.generation == 0):
            raise ValueError("coding inference grant offer status disagrees")
        return self


class CodingInferenceExchangeRequest(BaseModel):
    """Rotate one live grant onto a validator-authorized broker key."""

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    grant_id: UUID
    broker_public_key: Annotated[str, Field(pattern=_BROKER_KEY_PATTERN)]
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding inference exchange timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingInferenceExchangeRequest:
        if self.grant_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding inference exchange UUID is nil")
        return self


class CodingInferenceExchangeResponse(CodingInferenceGrantAuthority):
    schema_name: Literal["dittobench-coding-inference-exchange-v1"] = Field(
        alias="schema"
    )
    status: Literal["active"]
    generation: Annotated[int, Field(strict=True, ge=1, le=(1 << 31) - 1)]
    bearer: Annotated[str, Field(pattern=_BEARER_PATTERN, repr=False)]
    proxy_url: Annotated[str, Field(min_length=1, max_length=_MAX_URL_BYTES)]
    revoke_bearer: Annotated[str, Field(pattern=_BEARER_PATTERN, repr=False)]
    revoke_url: Annotated[str, Field(min_length=1, max_length=_MAX_URL_BYTES)]

    @field_validator("proxy_url")
    @classmethod
    def proxy_url_is_approved(cls, value: str) -> str:
        return _validate_https_url(
            value,
            suffix="/api/v1/inference/coding/chat/completions",
        )

    @field_validator("revoke_url")
    @classmethod
    def revoke_url_is_approved(cls, value: str) -> str:
        return _validate_https_url(
            value,
            suffix="/api/v1/validator/coding-shadow/inference-revoke-capability",
        )

    @model_validator(mode="after")
    def bearer_scopes_are_distinct(self) -> CodingInferenceExchangeResponse:
        if self.bearer == self.revoke_bearer:
            raise ValueError("coding inference and revoke bearers must differ")
        return self


class CodingInferenceCapabilityRevokeRequest(CodingInferenceGrantModel):
    """One revocation-only bearer request owned by the trusted Go gateway."""

    grant_id: UUID
    ticket_id: UUID
    generation: Annotated[int, Field(strict=True, ge=1, le=(1 << 31) - 1)]

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingInferenceCapabilityRevokeRequest:
        if self.grant_id.int == 0 or self.ticket_id.int == 0:
            raise ValueError("coding inference capability revoke UUID is nil")
        return self


class CodingInferenceRevokeRequest(BaseModel):
    """Signed terminal revocation of a coding inference grant generation."""

    model_config = ConfigDict(extra="ignore")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    grant_id: UUID
    generation: Annotated[int, Field(strict=True, ge=0, le=(1 << 31) - 1)]
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding inference revocation timestamp must be aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingInferenceRevokeRequest:
        if self.grant_id.int == 0 or self.nonce.int == 0:
            raise ValueError("coding inference revocation UUID is nil")
        return self


class CodingInferenceRevokeResponse(CodingInferenceGrantModel):
    schema_name: Literal["dittobench-coding-inference-revocation-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    grant_id: UUID
    ticket_id: UUID
    status: Literal["revoked"]
    generation: Annotated[int, Field(strict=True, ge=0, le=(1 << 31) - 1)]
    revoked_at: datetime
    idempotent: bool

    @field_validator("revoked_at")
    @classmethod
    def revoked_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coding inference revocation timestamp must be aware")
        return value.astimezone(UTC)

    @field_validator("idempotent", mode="before")
    @classmethod
    def idempotent_is_strict_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("coding inference revocation idempotency must be boolean")
        return value

    @model_validator(mode="after")
    def identifiers_are_nonzero(self) -> CodingInferenceRevokeResponse:
        if self.grant_id.int == 0 or self.ticket_id.int == 0:
            raise ValueError("coding inference revocation authority UUID is nil")
        return self


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("coding inference signing timestamp must be aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def coding_inference_grant_signing_message(
    *, validator_hotkey: str, ticket_id: UUID, nonce: UUID, requested_at: datetime
) -> bytes:
    return ":".join(
        (
            "dittobench-coding-inference-grant:v1",
            validator_hotkey,
            str(ticket_id),
            str(nonce),
            _timestamp(requested_at),
        )
    ).encode()


def coding_inference_exchange_signing_message(
    *,
    validator_hotkey: str,
    grant_id: UUID,
    broker_public_key: str,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return ":".join(
        (
            "dittobench-coding-inference-exchange:v1",
            validator_hotkey,
            str(grant_id),
            broker_public_key.rstrip("="),
            str(nonce),
            _timestamp(requested_at),
        )
    ).encode()


def coding_inference_revoke_signing_message(
    *,
    validator_hotkey: str,
    grant_id: UUID,
    generation: int,
    nonce: UUID,
    requested_at: datetime,
) -> bytes:
    return ":".join(
        (
            "dittobench-coding-inference-revoke:v1",
            validator_hotkey,
            str(grant_id),
            str(generation),
            str(nonce),
            _timestamp(requested_at),
        )
    ).encode()


__all__ = [
    "CodingInferenceCapabilityRevokeRequest",
    "CodingInferenceExchangeRequest",
    "CodingInferenceExchangeResponse",
    "CodingInferenceGrantOffer",
    "CodingInferenceGrantRequest",
    "CodingInferenceRevokeRequest",
    "CodingInferenceRevokeResponse",
    "coding_inference_exchange_signing_message",
    "coding_inference_grant_signing_message",
    "coding_inference_revoke_signing_message",
]
