"""Ticket-scoped platform inference wire contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ditto.api_models.upload import _SIGNATURE_HEX_PATTERN, _SS58_PATTERN


class InferenceGrantOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: UUID
    exchange_url: str
    proxy_url: str
    allowed_models: Annotated[list[str], Field(min_length=1, max_length=4)]
    request_budget: Annotated[int, Field(ge=1)]
    token_budget: Annotated[int, Field(ge=1)]
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expires_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value


class InferenceExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    grant_id: UUID
    broker_public_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}=?$")]
    nonce: UUID
    requested_at: datetime
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]

    @field_validator("requested_at")
    @classmethod
    def requested_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("requested_at must include a timezone")
        return value


class InferenceExchangeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: UUID
    bearer: Annotated[str, Field(min_length=32, max_length=128)]
    proxy_url: str
    expires_at: datetime
    generation: Annotated[int, Field(ge=1)]

    @field_validator("expires_at")
    @classmethod
    def expires_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return value
