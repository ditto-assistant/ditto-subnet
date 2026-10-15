"""Versioned build-bounty wire contracts and the public spec digest.

A bounty is a GitHub issue whose terms Platform fixes in signed, append-only
revisions. Contributors sign over a revision's ``spec_digest``, so the digest
is computed here, once, from an explicit versioned field list: adding a field
to ``BountyTerms`` later must not change the digest of a published revision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from ditto_screening_protocol.contributor_identity import (
    Address,
    Digest,
    GithubLogin,
    WireModel,
)

# owner/name in its one canonical spelling: GitHub matches repository names
# case-insensitively, so only lowercase is accepted and one issue can never be
# signed or published under two spellings. ':' and '#' never appear, so signed
# and displayed references stay unambiguous.
_REPO = r"[a-z0-9][a-z0-9-]{0,38}/[a-z0-9._-]{1,100}"
REPO_PATTERN = rf"^{_REPO}$"
Repo = Annotated[str, Field(pattern=REPO_PATTERN)]
IssueNumber = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
IssueRef = Annotated[str, Field(pattern=rf"^{_REPO}#[1-9][0-9]{{0,9}}$")]
# 1 alpha = 10^9 rao. Amounts are exact integers, never floats, and at most
# 2^53 - 1 (over nine million alpha) so every JSON client, the dashboard and
# Backroom included, reads them exactly.
MAX_ALPHA_RAO = 2**53 - 1
AlphaRao = Annotated[int, Field(strict=True, ge=0, le=MAX_ALPHA_RAO)]
FundingMode = Literal["rehearsal", "live"]
BountySlotStatus = Literal[
    "open",
    "held",
    "claimed",
    "submitted",
    "rework",
    "accepted",
    "rejected",
    "expired",
    "withdrawn",
    "cancelled",
]
CLOSED_SLOT_STATUSES: tuple[BountySlotStatus, ...] = (
    "accepted",
    "rejected",
    "expired",
    "withdrawn",
    "cancelled",
)
BountyEventKind = Literal["revision_published", "round_opened"]

SPEC_DOMAIN = "ditto-bounty-spec:v1"
# The digest covers exactly these terms, in this domain version. A new field
# needs a new domain version, never a silent change to this tuple.
SPEC_V1_FIELDS = (
    "title",
    "scope",
    "public_interfaces",
    "acceptance_evidence",
    "dependencies",
    "reviewer_login",
    "entry_fee_alpha_rao",
    "reward_pool_alpha_rao",
    "attempts_per_round",
    "delivery_days",
    "review_days",
    "hold_minutes",
    "funding_mode",
    "fee_escrow_coldkey",
    "fee_escrow_hotkey",
)
LongText = Annotated[str, Field(min_length=1, max_length=20000)]


class BountyTerms(WireModel):
    """The public, signed terms of one bounty revision."""

    title: Annotated[str, Field(min_length=1, max_length=200)]
    scope: LongText
    public_interfaces: LongText
    acceptance_evidence: LongText
    dependencies: Annotated[list[IssueRef], Field(max_length=20)] = []
    reviewer_login: GithubLogin
    entry_fee_alpha_rao: AlphaRao
    reward_pool_alpha_rao: AlphaRao
    attempts_per_round: Annotated[int, Field(strict=True, ge=1, le=10)] = 3
    delivery_days: Annotated[int, Field(strict=True, ge=1, le=60)] = 5
    review_days: Annotated[int, Field(strict=True, ge=1, le=60)] = 7
    hold_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)] = 30
    funding_mode: FundingMode
    fee_escrow_coldkey: Address
    fee_escrow_hotkey: Address


def bounty_spec_digest(
    *, repo: str, issue: int, revision: int, terms: BountyTerms
) -> str:
    """SHA-256 of the canonical JSON of one revision's public terms."""
    values = terms.model_dump(mode="json")
    document = {
        "domain": SPEC_DOMAIN,
        "repo": repo,
        "issue": issue,
        "revision": revision,
        "terms": {name: values[name] for name in SPEC_V1_FIELDS},
    }
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def bounty_publish_confirmation(
    *, repo: str, issue: int, revision: int, spec_digest: str
) -> str:
    return f"PUBLISH BOUNTY {repo}#{issue} REVISION {revision} {spec_digest}"


class BountyPublishPreviewRequest(WireModel):
    repo: Repo
    issue: IssueNumber
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=2147483646)]
    terms: BountyTerms


class BountyPublishRequest(BountyPublishPreviewRequest):
    # The operator who records decisions; private, never part of the terms.
    reviewer_actor: Annotated[str, Field(min_length=1, max_length=120)]
    # Audit evidence: unbounded, like every operator reason.
    reason: Annotated[str, Field(min_length=8)]
    confirmation: Annotated[str, Field(min_length=1, max_length=400)]


class BountyPublishPreview(WireModel):
    revision: int
    spec_digest: Digest
    confirmation: str
    # Empty when the revision can be published as given.
    refusals: list[str]


class BountyRevisionView(WireModel):
    revision: int
    spec_digest: Digest
    terms: BountyTerms
    published_at: datetime


class BountyRoundView(WireModel):
    round: int
    opened_revision: int
    opened_at: datetime


class BountySlotView(WireModel):
    slot_id: UUID
    round: int
    position: int
    status: BountySlotStatus
    status_changed_at: datetime


class BountyEventView(WireModel):
    event_id: int
    kind: BountyEventKind
    revision: int | None
    round: int | None
    slot_id: UUID | None
    recorded_at: datetime


class BountySlotCounts(WireModel):
    open: int
    held: int
    claimed: int
    submitted: int
    rework: int
    closed: int


class BountySummary(WireModel):
    bounty_id: UUID
    repo: str
    issue: int
    title: str
    current_revision: int
    spec_digest: Digest
    entry_fee_alpha_rao: int
    reward_pool_alpha_rao: int
    delivery_days: int
    review_days: int
    reviewer_login: str
    funding_mode: FundingMode
    current_round: int
    slots: BountySlotCounts
    published_at: datetime
    updated_at: datetime


class BountyBoardPage(WireModel):
    items: list[BountySummary]
    total: int
    limit: int
    offset: int


class BountyDetail(WireModel):
    summary: BountySummary
    revisions: list[BountyRevisionView]
    rounds: list[BountyRoundView]
    slots: list[BountySlotView]
    events: list[BountyEventView]
