"""Audited operator control for the miner submission deposit address."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from scalecodec.utils.ss58 import ss58_decode

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{32,64}$"


def _valid_bittensor_ss58(address: str) -> str:
    try:
        ss58_decode(address, valid_ss58_format=42)
    except (TypeError, ValueError) as error:
        raise ValueError("must be a valid Bittensor SS58 address") from error
    return address


SubmissionPaymentAddress = Annotated[
    str,
    Field(pattern=_SS58_PATTERN),
    AfterValidator(_valid_bittensor_ss58),
]


class SubmissionDepositAddressRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int
    parent_revision: int
    payment_address: Annotated[str, Field(pattern=_SS58_PATTERN)]
    reason: str
    actor: str
    created_at: datetime | None


class AdminSubmissionDepositAddressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current: SubmissionDepositAddressRevision
    history: list[SubmissionDepositAddressRevision]


class AdminSubmissionDepositAddressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    expected_revision: Annotated[int, Field(ge=0)]
    payment_address: SubmissionPaymentAddress
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str
