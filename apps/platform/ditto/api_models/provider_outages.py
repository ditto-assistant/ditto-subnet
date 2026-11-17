"""Wire shapes for the provider-outage operator read.

One response carries the relay-owned circuit rows, the evidence gate the
post-outage sweep applies, and its append-only runs (voids written, outage
windows observed, canonical scores flagged and voided by a same-validator
replacement re-test).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ProviderOutageCircuitView(BaseModel):
    provider: str
    state: Literal["open", "closed"]
    epoch: UUID
    opened_at: datetime
    retry_at: datetime
    last_failure_at: datetime
    closed_at: datetime | None = None
    failure_count: int
    last_status: int | None = None
    last_error_code: str
    probe_kind: str | None = None
    probe_expires_at: datetime | None = None
    updated_at: datetime
    outage_active: bool
    """Open, or closed with a failure inside the recovery quiet window."""


class OutageSweepPolicy(BaseModel):
    interval_seconds: float
    batch_limit: int
    first_lookback_seconds: float
    settle_lag_seconds: float
    outage_failure_codes: list[str]
    min_outage_failed_calls: int
    outage_failed_calls: int
    outage_failed_share: float


class OutageSweepVoid(BaseModel):
    void_id: UUID
    agent_id: UUID
    validator_hotkey: str
    seed: str
    void_generation: int
    run_id: str
    composite: float
    accepted_at: datetime
    grant_id: UUID
    chat_requests: int
    outage_failed_requests: int
    outage_started_at: datetime | None = None
    outage_ended_at: datetime | None = None


class OutageSweepWindow(BaseModel):
    grant_id: UUID
    agent_id: UUID
    validator_hotkey: str
    sample: Literal["confirmation", "canonical"]
    started_at: datetime
    ended_at: datetime
    outage_failed_requests: int


class OutageSweepCanonicalFlag(BaseModel):
    agent_id: UUID
    validator_hotkey: str
    run_id: str
    composite: float
    accepted_at: datetime
    grant_id: UUID
    chat_requests: int
    failed_requests: int
    outage_failed_requests: int
    failure_codes: dict[str, int] = {}
    outage_started_at: datetime | None = None
    outage_ended_at: datetime | None = None
    void_disposition: str | None = None
    """What the sweep's canonical void did (SN-489): ``applied`` queued the
    replacement re-test; ``already_queued``, ``retest_busy``,
    ``not_retestable``, ``auto_cap_reached`` and the guard dispositions left the
    score as is; ``error`` means the void raised. Null on runs before SN-489."""
    void_request_id: UUID | None = None
    """Deterministic replacement request id (``get_validator_score_replacement``
    shows its state)."""
    void_detail: str | None = None


class OutageSweepError(BaseModel):
    agent_id: UUID | None = None
    validator_hotkey: str | None = None
    seed: str | None = None
    void_generation: int | None = None
    canonical_scores: bool = False
    error: str


class OutageSweepRunView(BaseModel):
    sweep_id: UUID
    bench_version: int
    started_at: datetime
    finished_at: datetime
    watermark_from: datetime
    watermark_to: datetime
    examined_count: int
    voided_count: int
    error_count: int
    voids: list[OutageSweepVoid] = []
    outage_windows: list[OutageSweepWindow] = []
    canonical_flagged: list[OutageSweepCanonicalFlag] = []
    """Canonical scores whose grant met the gate, each with the disposition of
    the automatic same-validator replacement re-test it queued."""
    errors: list[OutageSweepError] = []


class ProviderOutagesResponse(BaseModel):
    circuits: list[ProviderOutageCircuitView]
    sweep_policy: OutageSweepPolicy
    runs_total: int
    limit: int
    offset: int
    runs: list[OutageSweepRunView]
