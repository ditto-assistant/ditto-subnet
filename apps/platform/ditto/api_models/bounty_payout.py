"""Wire shapes for bounty acceptance, payout proofs, disputes, and public accounting."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_HEX_PATTERN = r"^[0-9a-fA-F]{128}$"
_SHA256_HEX_PATTERN = r"^[0-9a-fA-F]{64}$"
_REPO_PATTERN = r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$"
_COMMIT_SHA_PATTERN = r"^[0-9a-fA-F]{40}$"

PayoutState = Literal[
    "claimed",
    "submitted",
    "accepted",
    "merged",
    "deployed_verified",
    "approved",
    "paid",
    "rejected",
    "appealed",
    "cancelled",
    "handed_off",
    "payout_failed",
]

OperatorRole = Literal["reviewer", "approver", "lead", "treasury_admin"]


class OperatorApproval(BaseModel):
    """Cryptographic approval signature from an authorized treasury operator."""

    model_config = ConfigDict(extra="ignore")

    operator_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    signature: Annotated[str, Field(pattern=_SIGNATURE_HEX_PATTERN)]
    role: OperatorRole = "approver"
    approved_at: datetime


class BountySplitShare(BaseModel):
    """Allocated payment share for a team member in a collaborative bounty."""

    model_config = ConfigDict(extra="ignore")

    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    payee_coldkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    amount_rao: Annotated[int, Field(gt=0)]
    share_ratio: Annotated[float, Field(gt=0.0, le=1.0)]


class BountyApprovalRecord(BaseModel):
    """Immutable record binding accepted technical work to financial disbursement."""

    model_config = ConfigDict(extra="ignore")

    repo: Annotated[str, Field(pattern=_REPO_PATTERN)]
    issue: Annotated[int, Field(ge=1)]
    claim_id: UUID
    commit_sha: Annotated[str, Field(pattern=_COMMIT_SHA_PATTERN)]
    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    payee_coldkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    reward_revision: Annotated[int, Field(ge=1)]
    amount_rao: Annotated[int, Field(gt=0)]
    amount_tao: Annotated[float, Field(gt=0.0)]
    treasury_policy_revision: Annotated[int, Field(ge=1)]
    split_shares: list[BountySplitShare] = Field(default_factory=list)
    approvals: list[OperatorApproval] = Field(default_factory=list)
    confirmation_phrase: str = "APPROVE BOUNTY PAYOUT"
    checksum: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    approved_at: datetime


class BountyPayoutProof(BaseModel):
    """Extrinsic location and parameters submitted to prove on-chain disbursement."""

    model_config = ConfigDict(extra="ignore")

    block_number: Annotated[int, Field(ge=0)]
    block_hash: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    extrinsic_index: Annotated[int, Field(ge=0)]
    claim_id: UUID
    payee_coldkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    amount_rao: Annotated[int, Field(gt=0)]


class BountyPayoutReceipt(BaseModel):
    """Verified on-chain transaction receipt recorded in the treasury ledger."""

    model_config = ConfigDict(extra="ignore")

    block_hash: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    extrinsic_index: Annotated[int, Field(ge=0)]
    claim_id: UUID
    payee_coldkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    amount_rao: Annotated[int, Field(gt=0)]
    treasury_coldkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    block_timestamp: datetime
    verified_at: datetime


class BountyDisputeRecord(BaseModel):
    """Formal dispute or appeal record contesting a bounty rejection or revocation."""

    model_config = ConfigDict(extra="ignore")

    dispute_id: UUID
    claim_id: UUID
    claimant_hotkey: Annotated[str, Field(pattern=_SS58_PATTERN)]
    reason: Annotated[str, Field(min_length=1, max_length=5000)]
    evidence_url: str | None = None
    status: Literal["open", "upheld", "dismissed"] = "open"
    resolution_notes: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class BountyLedgerEntry(BaseModel):
    """One tamper-evident, hash-chained entry in the public bounty ledger."""

    model_config = ConfigDict(extra="ignore")

    seq: Annotated[int, Field(ge=1)]
    entry_id: UUID
    claim_id: UUID
    repo: Annotated[str, Field(pattern=_REPO_PATTERN)]
    issue: Annotated[int, Field(ge=1)]
    event: str
    payload: dict[str, Any]
    prev_hash: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    entry_hash: Annotated[str, Field(pattern=_SHA256_HEX_PATTERN)]
    recorded_at: datetime


class PublicBountyAuditRecord(BaseModel):
    """Source-safe public projection of bounty status, accounting, and receipts."""

    model_config = ConfigDict(extra="ignore")

    repo: str
    issue: int
    claim_id: UUID
    status: PayoutState
    commit_sha: str | None = None
    claimant_hotkey: str
    payee_coldkey: str
    amount_rao: int
    amount_tao: float
    block_hash: str | None = None
    extrinsic_index: int | None = None
    state_transitions: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime
