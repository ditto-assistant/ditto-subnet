"""Wire models for a miner's self-serve retry of an interrupted screening (SN-456).

A screening attempt that fails for an infrastructure-shaped reason parks the
submission in ``screening_failed`` with "manual retry required". Before this
route existed the only way out was an operator running
``retry_failed_screening_now`` from Backroom, so the miner had to DM someone.

These models back the session-authenticated route that lets the owning miner
authorize that same exact-attempt retry themselves, within a small per-agent
budget an operator controls. It writes the same append-only
``screening_retry_overrides`` row the operator path writes, so the screener's
claim path, Backroom's audit view, and the no-automatic-retry policy all see one
ledger. A miner can never retry a rejection, a quarantine, an operator hold, or
an agent-fault failure: those stay with the review rulings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

MinerScreeningRetryBlocker = Literal[
    "not_screening_failed",
    "no_attempt",
    "attempt_active",
    "attempt_not_retryable",
    "reason_not_retryable",
    "disabled",
    "budget_exhausted",
]
"""Why a self-serve retry is unavailable right now.

- ``not_screening_failed``: the submission is not parked after a failed screen.
- ``no_attempt``: no screening attempt exists yet.
- ``attempt_active``: a screening attempt is still running.
- ``attempt_not_retryable``: the latest attempt ended in a ruling (rejected or
  quarantined) rather than an interruption; use the appeal path instead.
- ``reason_not_retryable``: the latest failure is one the submission owns
  (agent fault) or an operator policy hold.
- ``disabled``: operators have paused self-serve retries.
- ``budget_exhausted``: this agent already used its self-serve retries; ask an
  operator.
"""

MINER_SCREENING_RETRY_COST_NOTICE = (
    "A retry starts a new full screening of this exact artifact: image build, "
    "runtime smoke, and source review. It costs no TAO, but it uses one of this "
    "agent's limited self-serve retries and paid screening capacity. If the "
    "retry fails again for the same reason, wait for an operator instead of "
    "re-uploading."
)
"""Fixed copy every client shows before it sends the retry."""


class MinerScreeningRetryStatus(BaseModel):
    """Whether the signed-in owner may retry this agent's screening now.

    The ``latest_attempt_id`` and ``artifact_sha256`` are the guards a client
    echoes back in :class:`MinerScreeningRetryRequest`, so a retry can only
    authorize the attempt the miner was shown.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    agent_status: str
    artifact_sha256: str
    latest_attempt_id: UUID | None
    latest_attempt_status: str | None
    reason_code: str | None
    public_reason: str | None
    eligible: bool
    """True when a POST with these guards would authorize a new retry."""
    blocker: MinerScreeningRetryBlocker | None
    """Set exactly when ``eligible`` is false and no retry is already queued."""
    already_authorized: bool
    """A retry of ``latest_attempt_id`` is already queued (by this miner or an
    operator). Re-sending the POST is a no-op that returns the same grant."""
    retries_used: int
    retries_allowed: int
    cost_notice: str = MINER_SCREENING_RETRY_COST_NOTICE


class MinerScreeningRetryRequest(BaseModel):
    """Authorize one retry of the exact attempt the miner was shown."""

    model_config = ConfigDict(extra="ignore")

    expected_attempt_id: UUID
    expected_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    acknowledge_cost: StrictBool
    """The client showed :data:`MINER_SCREENING_RETRY_COST_NOTICE` and the miner
    accepted it. Anything other than JSON ``true`` (including ``1``) is a 422."""

    @field_validator("acknowledge_cost")
    @classmethod
    def _must_acknowledge(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("acknowledge_cost must be true")
        return value


class MinerScreeningRetryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    override_id: UUID
    agent_id: UUID
    attempt_id: UUID
    agent_status: str
    created_at: datetime
    idempotent: bool
    """True when this attempt already had a retry grant; nothing new was
    written and no budget was spent."""
    authorized_by_operator: bool
    """The existing grant came from an operator, not this miner."""
    retries_used: int
    retries_allowed: int


class MinerScreeningRetrySettingsRevision(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    revision: int
    parent_revision: int
    enabled: bool
    max_retries_per_agent: int
    reason: str
    actor: str
    created_at: datetime | None


class AdminMinerScreeningRetrySettingsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current: MinerScreeningRetrySettingsRevision
    history: list[MinerScreeningRetrySettingsRevision]
    recent_self_serve_retries_24h: int
    """Miner self-serve retry grants created in the last 24 hours, fleet-wide."""


class AdminMinerScreeningRetrySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, str_strip_whitespace=True)

    expected_revision: Annotated[int, Field(ge=0)]
    enabled: bool
    max_retries_per_agent: Annotated[int, Field(ge=0, le=3)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str


def miner_screening_retry_settings_confirmation(
    *, enabled: bool, max_retries_per_agent: int
) -> str:
    """The exact phrase an operator types to apply a revision."""
    if not enabled:
        return "DISABLE MINER SCREENING RETRY"
    return f"ENABLE MINER SCREENING RETRY MAX {max_retries_per_agent}"
