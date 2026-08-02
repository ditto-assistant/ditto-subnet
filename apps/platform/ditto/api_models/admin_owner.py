"""Admin contracts for payment-derived miner owner linkage.

Operator-only. Coldkeys are moderation metadata and never appear on the public
scoring wire; see :mod:`ditto.db.queries.ownership` for why a shared coldkey is
a signal rather than an ownership determination.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

#: One sentence a consumer can render verbatim so no surface has to invent its
#: own wording for the caveat. Kept on the response, not just in docs, because
#: the linkage is easy to over-read once it is out of this codebase.
OWNER_LINKAGE_CAVEAT = (
    "Coldkeys here are payment-time records of who paid for each evaluation, "
    "not on-chain metagraph ownership. Miners routinely pay from several "
    "coldkeys, so a shared coldkey is one signal of common control and "
    "different coldkeys are not evidence of different operators. Confirm "
    "on-chain before treating this as ownership."
)


class AdminOwnerAgent(BaseModel):
    """One submission belonging to a linked hotkey."""

    agent_id: UUID
    agent_name: str
    agent_version: int | None = None
    agent_status: str
    artifact_sha256: str
    submitted_at: datetime
    # Null for legacy/test agents created before paid-upload provenance was
    # mandatory: unknown, not absent.
    miner_coldkey: str | None = None


class AdminOwnerHotkey(BaseModel):
    """One hotkey in the footprint, with the evidence that linked it."""

    miner_hotkey: str
    #: Every payment-time coldkey ever recorded for this hotkey.
    miner_coldkeys: list[str] = Field(default_factory=list)
    #: Payment-record edges from the key that was asked about. 0 is that key
    #: itself; 1 shares a coldkey with it; 2 shares a coldkey with a hop-1
    #: hotkey. Higher hops are progressively weaker evidence.
    link_hop: int
    submission_count: int
    #: How many of those submissions carry a payment row at all. The gap is the
    #: part of this hotkey's history no coldkey can speak to.
    paid_submission_count: int
    latest_submitted_at: datetime | None = None
    agents: list[AdminOwnerAgent] = Field(default_factory=list)
    agents_truncated: bool = False


class AdminOwnerFootprint(BaseModel):
    """Every hotkey reachable from one key through payment records."""

    identifier: str
    #: Which role the records show for the key, since hotkeys and coldkeys are
    #: indistinguishable by shape. ``unknown`` means no record names it.
    identifier_kind: Literal["miner_hotkey", "miner_coldkey", "both", "unknown"]
    depth: int
    miner_coldkeys: list[str] = Field(default_factory=list)
    hotkeys: list[AdminOwnerHotkey] = Field(default_factory=list)
    hotkey_count: int
    submission_count: int
    #: False when the walk stopped at a depth or identity ceiling with more
    #: linkage still reachable, so a caller never reads a truncated set as the
    #: operator's whole footprint.
    expansion_complete: bool
    ownership_basis: Literal["evaluation_payment_records"] = (
        "evaluation_payment_records"
    )
    linkage_caveat: str = OWNER_LINKAGE_CAVEAT
