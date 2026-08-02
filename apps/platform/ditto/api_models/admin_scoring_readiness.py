"""Operator observability: why an agent can or cannot be leased for scoring.

The DB lives on a GCE VM operators cannot easily reach, so this exposes the
ticket-issuance prerequisites (`issue_ticket`, `issue_rollout_ticket`) as a read
model — dataset, screened image, screening policy — to explain a submission
stuck below quorum without a live validator ever picking it up. Answers are
scoped to the benchmark era the submission is queued in, which is not always the
active version; see `scoring_bench_version`.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ScreenedImageReadiness(BaseModel):
    complete: bool
    """All identity fields plus the verification timestamp are present."""
    verified: bool
    policy_ok: bool
    """Built under a screening policy at or above the active contract's minimum."""
    missing_fields: list[str]


class AgentScoringReadiness(BaseModel):
    agent_id: UUID
    agent_name: str
    miner_hotkey: str
    status: str
    active_bench_version: int
    scoring_bench_version: int
    """Benchmark era this submission is actually queued in — every check below
    is answered against this version, not ``active_bench_version``. They differ
    during an open rollout: a submission that arrived after the rollout opened,
    and a frozen cohort member being rescored, are both leased at the desired
    version while the active version still holds ledger authority."""
    scoring_lane: Literal["ordinary", "fresh_submission", "rollout_cohort"]
    """Which issuance lane would lease this submission. ``rollout_cohort`` is
    served by ``issue_rollout_ticket`` out of ``scored``/``live``; the other two
    are served by ``issue_ticket`` out of ``evaluating``."""
    screening_policy_version: int
    required_screening_policy_version: int
    requires_screened_image: bool
    has_versioned_dataset: bool
    screened_image: ScreenedImageReadiness
    leaseable: bool
    """True iff a validator's next sweep could lease this agent at
    ``scoring_bench_version`` — i.e. ``blocking_reasons`` is empty. Evidence,
    not authority: the lease path does not consult this endpoint, and this does
    not model attempt budgets or per-slot validator occupancy."""
    blocking_reasons: list[str]
