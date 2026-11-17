"""Wire models for Backroom airdrop runs (SN-490).

An airdrop run pays one asset to a list of recipients. A preview (the
default, ``dry_run=true``) never writes. An execute re-derives the preview
server-side, must echo its digest and exact confirmation phrase, and is
idempotent on ``idempotency_key``: a retry resumes the same run, and a
recipient that has started settling is never sent again.

Which asset an airdrop actually moves is still an open decision (SN-490).
Each asset settles through an executor registered for ``(asset, mode)``.
Only simulated rehearsal executors exist, so a ``live`` run is refused for
every asset and no run can move funds or credits.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

AirdropAsset = Literal["alpha", "ditto_credits"]
AirdropMode = Literal["rehearsal", "live"]
AirdropRecipientKind = Literal["wallet", "account"]
AirdropRecipientStatus = Literal[
    "pending", "dispatching", "settled", "failed", "uncertain"
]
AirdropRunStatus = Literal["in_progress", "settled", "needs_attention"]
AirdropBalanceStatus = Literal["sufficient", "insufficient", "unavailable"]

AIRDROP_ASSETS: tuple[AirdropAsset, ...] = ("alpha", "ditto_credits")

# The only recipient kind each asset accepts. A wallet is an SS58 coldkey that
# receives SN118 alpha directly. An account is an SN118 miner hotkey; the
# Ditto account it reaches is the one bound by that hotkey's verified Sign in
# with Ditto link, so this public repository never handles a product email.
AIRDROP_RECIPIENT_KIND: dict[AirdropAsset, AirdropRecipientKind] = {
    "alpha": "wallet",
    "ditto_credits": "account",
}

# Base units the integer ``amount`` fields are denominated in.
AIRDROP_UNIT: dict[AirdropAsset, str] = {"alpha": "rao", "ditto_credits": "credit"}

# Per-run caps. They are pinned in reviewed code on purpose, not a runtime
# setting: raising how much one run may pay should take a reviewed change.
AIRDROP_MAX_RECIPIENTS = 200
AIRDROP_MAX_TOTAL: dict[AirdropAsset, int] = {
    # 10 alpha.
    "alpha": 10 * 10**9,
    # Five of the private Backroom's customary 5b-credit airdrops.
    "ditto_credits": 25 * 10**9,
}

# Hard request bounds. They only keep one request small; the per-run cap above
# is the policy limit, and the preview reports a list over it as a blocker.
AIRDROP_REQUEST_MAX_RECIPIENTS = 1000
# One row may not exceed the largest per-run cap, so every total, even a
# refused over-cap one, stays far below 2**53 for Backroom's JS numbers.
_MAX_AMOUNT = max(AIRDROP_MAX_TOTAL.values())

IdempotencyKey = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
        description=(
            "8-128 chars of [A-Za-z0-9._:-]. Names one run forever: a retry "
            "with the same key resumes it and never pays a recipient twice."
        ),
    ),
]


class AirdropRecipientInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AirdropRecipientKind
    address: Annotated[
        str,
        Field(
            min_length=1,
            max_length=64,
            description="SS58 coldkey for a wallet, SS58 miner hotkey for an account.",
        ),
    ]
    amount: Annotated[
        int,
        Field(
            ge=1,
            le=_MAX_AMOUNT,
            description="Integer amount in the asset's base unit (rao or credit).",
        ),
    ]


class AirdropPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: AirdropAsset
    mode: AirdropMode = "rehearsal"
    idempotency_key: IdempotencyKey
    # Audit evidence: no upper bound, like every operator reason. Stripped so
    # the length check matches the ledger's trimmed check constraint.
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=8)]
    recipients: Annotated[
        list[AirdropRecipientInput],
        Field(min_length=1, max_length=AIRDROP_REQUEST_MAX_RECIPIENTS),
    ]


class AirdropExecuteRequest(AirdropPreviewRequest):
    """Execute, guarded by the preview's digest and exact phrase.

    ``dry_run`` defaults to true: without an explicit ``false`` this returns
    the preview and writes nothing.
    """

    dry_run: bool = True
    confirmation_digest: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None
    confirmation: Annotated[str | None, Field(max_length=200)] = None


class AirdropPreviewRecipient(BaseModel):
    position: int
    kind: AirdropRecipientKind
    address: str
    amount: int
    payee: str | None = Field(
        description="Who is paid: the coldkey, or the linked Ditto account id."
    )
    recipient_key: str | None = Field(
        description="Per-recipient idempotency key; null on a refused row."
    )
    refused: str | None = Field(
        description="Why this row cannot be paid, or null when it can."
    )
    prior_amount: int = Field(
        description=(
            "What this payee already received or is being paid (every status "
            "but failed) for this asset and mode under other run keys, to "
            "catch a retry made with a new key."
        )
    )


class AirdropCapCheck(BaseModel):
    max_recipients: int
    max_total_amount: int
    recipient_count: int
    total_amount: int
    within_cap: bool


class AirdropBalanceCheck(BaseModel):
    status: AirdropBalanceStatus
    source: str | None
    available: int | None
    required: int
    detail: str


class AirdropPreview(BaseModel):
    dry_run: Literal[True] = True
    asset: AirdropAsset
    unit: str
    mode: AirdropMode
    idempotency_key: str
    reason: str
    executor: str | None
    recipients: list[AirdropPreviewRecipient]
    recipient_count: int
    refused_count: int
    total_amount: int
    cap: AirdropCapCheck
    balance: AirdropBalanceCheck
    blockers: list[str]
    ready: bool
    existing_run_id: UUID | None
    existing_run_matches: bool | None
    warnings: list[str] = Field(
        description="Advisories that do not block, such as prior payments."
    )
    confirmation_digest: str
    confirmation: str
    spending_authority: Literal["none"] = "none"


class AirdropRecipientResult(BaseModel):
    position: int
    kind: AirdropRecipientKind
    address: str
    payee: str
    amount: int
    recipient_key: str
    status: AirdropRecipientStatus
    receipt: str | None
    error: str | None
    attempted_at: datetime | None
    settled_at: datetime | None


class AirdropRunSummary(BaseModel):
    run_id: UUID
    idempotency_key: str
    asset: AirdropAsset
    unit: str
    mode: AirdropMode
    executor: str
    reason: str
    actor: str
    digest: str
    created_at: datetime
    completed_at: datetime | None
    status: AirdropRunStatus
    stale_dispatching: int = Field(
        description=(
            "Rows claimed over ten minutes ago with no recorded outcome. They "
            "are never resent and need reconciliation (SN-490)."
        )
    )
    recipient_count: int
    total_amount: int
    settled_amount: int
    counts: dict[str, int]


class AirdropRunReport(AirdropRunSummary):
    recipients: list[AirdropRecipientResult]
    resumed: bool = Field(
        default=False,
        description="True when this call found the run already recorded under its key.",
    )


class AirdropExecuteResponse(BaseModel):
    dry_run: bool
    preview: AirdropPreview
    run: AirdropRunReport | None


class AirdropAssetPolicy(BaseModel):
    asset: AirdropAsset
    unit: str
    recipient_kind: AirdropRecipientKind
    max_total_amount: int
    rehearsal_executor: str | None
    live_executor: str | None
    funding_source: str | None


class AirdropPolicy(BaseModel):
    max_recipients: int
    assets: list[AirdropAssetPolicy]


class AirdropRunList(BaseModel):
    policy: AirdropPolicy
    runs: list[AirdropRunSummary]
