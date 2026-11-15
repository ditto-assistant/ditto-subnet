"""Wire models for self-serve paid readmission (SN-485).

``POST /readmission/check`` is the pre-payment dry run: it reports every reason
a readmission of one existing agent into the active benchmark era would be
refused, and with ``reserve_slot`` reserves the price. ``POST
/readmission/commit`` takes the finalized payment proof and admits the agent,
or banks the payment as a credit when admission is refused after payment.

Both are signed by the agent's hotkey over a domain separate from uploads:
``ditto-readmit-v1:{hotkey}:{agent_id}:{bench_version}:{timestamp}:{nonce}``.
An upload signature therefore can never stand in for a readmission, nor the
reverse.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import UUID4, BaseModel, ConfigDict, Field

from ditto.api_models.submission_settings import ReadmissionActivationState
from ditto.api_models.upload import (
    _BLOCK_HASH_PATTERN,
    _SIGNATURE_HEX_PATTERN,
    _SS58_PATTERN,
)

READMISSION_SIGNATURE_DOMAIN = "ditto-readmit-v1"
"""The signed payload is
``ditto-readmit-v1:{hotkey}:{agent_id}:{bench_version}:{timestamp}:{nonce}``."""


def readmission_signature_payload(
    *,
    hotkey: str,
    agent_id: UUID,
    bench_version: int,
    signature_timestamp: int,
    signature_nonce: UUID,
) -> bytes:
    return (
        f"{READMISSION_SIGNATURE_DOMAIN}:{hotkey}:{agent_id}:{bench_version}:"
        f"{signature_timestamp}:{signature_nonce}"
    ).encode("ascii")


class _SignedReadmission(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    agent_id: UUID
    bench_version: Annotated[int, Field(ge=1)]
    """The benchmark era to readmit into; must be the active one."""
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]
    signature_timestamp: Annotated[int, Field(ge=1, le=2**63 - 1)]
    signature_nonce: UUID4


class ReadmissionCheckRequest(_SignedReadmission):
    reserve_slot: bool = False
    """Reserve the price for 24 hours. Only an eligible check reserves."""


class ReadmissionEligibilityView(BaseModel):
    """What the stranded-era read says about this agent."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    stranded: bool
    """The agent is a scored previous-era row the active era does not admit."""
    already_admitted: bool
    previous_bench_version: int | None = None
    previous_score_count: int | None = None
    previous_median_composite: float | None = None
    owner_best_stranded: bool | None = None
    """Whether this is the owner's best stranded row. Payouts are owner-best
    only, so readmitting a weaker sibling may not change what the owner earns."""
    cohort_sibling_agent_id: UUID | None = None
    """A frozen-cohort agent of the same owner already on the active era."""


class ReadmissionCheckResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ok: bool
    error_codes: list[int] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    activation: ReadmissionActivationState
    agent_id: UUID
    bench_version: int
    """The active benchmark era."""
    rollout_id: UUID | None = None
    eligibility: ReadmissionEligibilityView | None = None
    payment_required: bool = False
    payment_amount_rao: int | None = None
    """Exactly the upload fee; set only with a reservation."""
    payment_send_address: str | None = None
    admission_token: UUID | None = None
    admission_expires_at: datetime | None = None
    retry_at: datetime | None = None
    cooldown_seconds: int
    netuid: int
    subtensor_network: str


class ReadmissionCommitRequest(_SignedReadmission):
    admission_token: UUID | None = None
    """The reservation ``/readmission/check`` issued. Without a live one the
    payment is verified against the current fee and, if admission is then
    refused, banked as a credit."""
    payment_block_hash: Annotated[str, Field(pattern=_BLOCK_HASH_PATTERN)]
    payment_block_number: Annotated[int, Field(ge=1)]
    payment_extrinsic_index: Annotated[int, Field(ge=0)]


ReadmissionOutcome = Literal["admitted", "credited"]


class ReadmissionCommitResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agent_id: UUID
    bench_version: int
    outcome: ReadmissionOutcome
    """``admitted``: the agent joined the active era and will be leased.
    ``credited``: admission was refused after payment; the payment is an upload
    credit for this hotkey (``credit_expires_at``), and no TAO is refunded on
    chain."""
    payment_disposition: Literal["consumed", "credit_consumed", "credited"]
    rollout_id: UUID | None = None
    position: int | None = None
    dataset_sha256: str | None = None
    refusal_reason: str | None = None
    credit_expires_at: datetime | None = None
    recovered: bool = False
    """True when this request repeated an already committed readmission."""


__all__ = [
    "READMISSION_SIGNATURE_DOMAIN",
    "ReadmissionCheckRequest",
    "ReadmissionCheckResponse",
    "ReadmissionCommitRequest",
    "ReadmissionCommitResponse",
    "ReadmissionEligibilityView",
    "ReadmissionOutcome",
    "readmission_signature_payload",
]
