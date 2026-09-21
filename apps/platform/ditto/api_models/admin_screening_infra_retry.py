"""Operator view of automatic infrastructure-failure retries (issue #475).

Read-only. Nothing here is stored: every number is derived from
``screening_attempts`` at read time by the same planner the screening claim
uses, so it can lag a claim that lands a moment later.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

InfraRetryState = Literal["backoff", "breaker_held", "probe_due", "due", "capped"]
InfraRetryClaimOutlook = Literal[
    "ready", "waiting_backoff", "waiting_breaker", "needs_operator", "not_admitted"
]
"""``ready`` means admitted with the backoff and breaker hold elapsed; the claim
may still skip it (one probe per signature per pass, ownership rules).
``waiting_breaker`` is a hold for workers on the signature's provider (every
worker when the signature has no provider); a worker on another provider can
still claim the agent by backoff alone."""
InfraRetryBreakerPhase = Literal["closed", "open", "half_open"]


class InfraRetryPolicy(BaseModel):
    """The constants the planner runs with; durations are seconds."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    auto_retry_reason_codes: list[str]
    base_backoff_seconds: int
    max_backoff_seconds: int
    jitter_fraction: float
    auto_retry_max_age_seconds: int
    auto_retry_max_streak: int
    plan_max_claimable: int
    breaker_distinct_agents: int
    breaker_window_seconds: int
    breaker_open_seconds: int
    breaker_probe_interval_seconds: int
    breaker_history_lookback_seconds: int


class InfraRetrySummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    parked_agents: int
    """All agents the planner is holding, before the ``agents`` list is bounded."""
    by_state: dict[InfraRetryState, int]
    not_admitted: int
    """Parked agents the claim's admission guard would skip regardless of state."""
    aged_out_agents: int
    """Parked on an infrastructure failure older than ``auto_retry_max_age_seconds``
    with no operator retry: never retried automatically, not listed individually."""
    open_breakers: int
    """Breakers whose open window has not elapsed (phase ``open``)."""
    half_open_breakers: int
    """Open window elapsed, no recovery seen yet: probes are allowed."""
    breakers_total: int


class InfraRetryAgentView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    agent_id: UUID
    attempt_id: UUID
    """Latest screening attempt, the infrastructure failure being retried."""
    reason_code: str
    provider: str | None = None
    lane: str | None = None
    consecutive_failures: int
    """Consecutive infrastructure failures since the last operator clear or retry."""
    failed_at: datetime
    backoff_until: datetime
    """End of this agent's own backoff, ignoring the breaker."""
    next_retry_at: datetime
    """Earliest a claim may take it, seen with no particular claimant: backoff,
    then the breaker's next probe. A worker on another provider than the
    signature's is held by backoff alone."""
    state: InfraRetryState
    breaker_phase: InfraRetryBreakerPhase | None = None
    """``None`` when no breaker state exists for the agent's signature."""
    admitted: bool
    claim_outlook: InfraRetryClaimOutlook


class InfraRetryBreakerView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    reason_code: str
    provider: str | None = None
    lane: str | None = None
    phase: InfraRetryBreakerPhase
    """``open`` while now < open_until; ``half_open`` after that until a probe
    recovers (or the failures age out of the history window); else ``closed``.
    Computed at read time from the derived state."""
    opened_at: datetime | None = None
    open_until: datetime | None = None
    last_probe_at: datetime | None = None
    next_probe_at: datetime | None = None
    parked_agents: int
    """Agents parked on this signature now (not the historical failure count)."""


class ScreeningInfraRetryView(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    generated_at: datetime
    basis: str
    policy: InfraRetryPolicy
    summary: InfraRetrySummary
    agents: list[InfraRetryAgentView] = Field(default_factory=list)
    agents_limit: int
    agents_truncated: bool
    breakers: list[InfraRetryBreakerView] = Field(default_factory=list)
    breakers_limit: int
    breakers_truncated: bool
