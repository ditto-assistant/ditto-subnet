"""Wire shapes for hotkey-signed bounty claims and contributor identity."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"
_SHA256_HEX_PATTERN = r"^[0-9a-fA-F]{64}$"
_REPO_PATTERN = r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$"
_COMMIT_SHA_PATTERN = r"^[0-9a-fA-F]{40}$"

KeyKind = Literal["hotkey", "coldkey"]
ConcurrencyMode = Literal["exclusive", "open"]
ReservationState = Literal[
    "active",
    "submitted",
    "accepted",
    "expired",
    "revoked",
    "withdrawn",
    "handed_off",
]


class BountyClaimProof(BaseModel):
    """One sr25519 proof of a bounty action."""

    model_config = ConfigDict(extra="ignore")

    key_kind: KeyKind
    signer: Annotated[str, Field(pattern=_SS58_PATTERN)]
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]


class BountySpec(BaseModel):
    """Terms of a bounty under a specific revision."""

    model_config = ConfigDict(extra="ignore")

    scope: Annotated[str, Field(min_length=1, max_length=10000)]
    acceptance_evidence: Annotated[str, Field(min_length=1, max_length=10000)]
    reward_min_tao: Annotated[int, Field(ge=0)]
    reward_max_tao: Annotated[int, Field(ge=0)]
    reviewer: Annotated[str, Field(min_length=1, max_length=128)]
    dependencies: list[str] = Field(default_factory=list)
    bounty_expires_at: datetime
    concurrency: ConcurrencyMode = "exclusive"
    max_open_claims: Annotated[int, Field(ge=1, le=100)] = 1
    reservation_days: Annotated[int, Field(ge=1, le=30)] = 7
    max_renewals: Annotated[int, Field(ge=0, le=10)] = 2
    policy_revision: Annotated[int, Field(ge=1)] = 1


class BountyClaimRequest(BaseModel):
    """Body of POST /bounties/claims."""

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    repo: Annotated[str, Field(pattern=_REPO_PATTERN)]
    issue: Annotated[int, Field(ge=1)]
    bounty_revision: Annotated[int, Field(ge=1)]
    spec_digest: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    payee_coldkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    github_login: Annotated[str, Field(min_length=1, max_length=100)]
    team_digest: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    expires_at: datetime
    nonce: UUID
    issued_at: datetime
    proof: BountyClaimProof


class BountyRenewRequest(BaseModel):
    """Body of POST /bounties/claims/{claim_id}/renew."""

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    claim_id: UUID
    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    bounty_revision: Annotated[int, Field(ge=1)]
    spec_digest: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    new_expires_at: datetime
    nonce: UUID
    issued_at: datetime
    proof: BountyClaimProof


class BountySubmitRequest(BaseModel):
    """Body of POST /bounties/claims/{claim_id}/submit."""

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    claim_id: UUID
    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    pr_number: Annotated[int, Field(ge=1)]
    head_sha: Annotated[str, Field(pattern=_COMMIT_SHA_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: BountyClaimProof


class BountyHandoffRequest(BaseModel):
    """Body of POST /bounties/claims/{claim_id}/handoff."""

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    claim_id: UUID
    side: Literal["from", "to"]
    from_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    to_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: BountyClaimProof


class BountyWithdrawRequest(BaseModel):
    """Body of POST /bounties/claims/{claim_id}/withdraw."""

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    claim_id: UUID
    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: BountyClaimProof


class BountyRebindPayeeRequest(BaseModel):
    """Body of POST /bounties/claims/{claim_id}/rebind-payee."""

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    claim_id: UUID
    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    old_payee_coldkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    new_payee_coldkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: BountyClaimProof


class BountyAppealRequest(BaseModel):
    """Body of POST /bounties/claims/{claim_id}/appeal."""

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    claim_id: UUID
    revocation_entry_hash: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    reason_digest: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: BountyClaimProof


class BountyTeamMemberRequest(BaseModel):
    """Attestation proof signed by an individual team collaborator."""

    model_config = ConfigDict(extra="ignore")

    netuid: Annotated[int, Field(ge=0)]
    team_digest: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    member_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    nonce: UUID
    issued_at: datetime
    proof: BountyClaimProof


class BountyReservationView(BaseModel):
    """Public read representation of an active or past bounty reservation."""

    model_config = ConfigDict(extra="ignore")

    claim_id: UUID
    repo: str
    issue: int
    bounty_revision: int
    spec_digest: str
    claimant_hotkey: str
    payee_coldkey: str
    github_login: str
    team_digest: str
    state: ReservationState
    expires_at: datetime
    pr_number: int | None = None
    head_sha: str | None = None
    renewals_used: int = 0
    created_at: datetime


class BountyClaimResponse(BaseModel):
    """Response returned after a successful claim or state transition."""

    model_config = ConfigDict(extra="ignore")

    claim_id: UUID
    reservation: BountyReservationView
    ledger_entry_hash: str
