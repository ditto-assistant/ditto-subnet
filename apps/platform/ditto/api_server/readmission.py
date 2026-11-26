"""Wire projections of the SN-485 readmission gate.

The predicate lives in :mod:`ditto.db.queries.readmission_gate`; this module
only renders it for the admin settings view, the public submission terms and
the readmission endpoints, so every surface reports the same state.
"""

from __future__ import annotations

from ditto.api_models.submission_settings import ReadmissionActivationState
from ditto.api_models.submission_terms import ReadmissionCostBasis, ReadmissionTerms
from ditto.api_server.readmission_cost_basis import CostBasis
from ditto.db.queries.free_retry_cap import FREE_RETRY_CAP_FAILED_LEASES
from ditto.db.queries.readmission_gate import (
    READMISSION_MIN_BENCH_VERSION,
    READMISSION_NOTICE_PERIOD,
    ReadmissionActivation,
)

READMISSION_RULES: tuple[str, ...] = (
    "A readmission is the owner coldkey re-entering an existing agent lineage: "
    "opting an existing agent into a new benchmark era, or re-running an "
    "unchanged agent after one of its evaluations completed. An identical or "
    "near-identical re-upload of the owner's own agent counts as one.",
    "A readmission costs exactly the upload fee, is paid the same way, and "
    "counts toward and starts the same owner-coldkey upload cooldown.",
    f"The fee applies from benchmark v{READMISSION_MIN_BENCH_VERSION} and goes "
    f"live no earlier than {READMISSION_NOTICE_PERIOD.days} days after it is "
    "announced (activation.effective_at). Nothing submitted before then is "
    "charged as a readmission, and nothing is readmitted automatically.",
    "ditto readmit AGENT_ID re-enters one of your finalized agents from the "
    "previous era into the active one. POST /api/v1/readmission/check refuses "
    "before payment: not your agent, a partial (2 of 3) row, withdrawn, retired, "
    "below the screening-policy floor, no screened image, already admitted, a "
    "rollout open, or the cooldown. At most one paid readmission per agent per "
    "era; payouts still count only the owner's best agent.",
    "A refusal found only after payment keeps the payment as an upload credit "
    "for the same hotkey for 30 days. Ditto never refunds TAO on chain.",
    "Retries caused by Ditto, its queue, validators or data (stalls, stranded "
    "evaluations, issuance gaps, validator crashes, provider outages, expired "
    "leases the agent did not cause, and Ditto's own re-tests such as season "
    "rotation and catch-up) are always free. Eligibility comes only from "
    "recorded validator and queue error codes, never from a request. Free "
    f"re-leases are capped at {FREE_RETRY_CAP_FAILED_LEASES} failed leases per "
    "agent per era; a failure the agent caused is remedied by a paid re-run or "
    "a new upload, never a free retry.",
)


def activation_state(activation: ReadmissionActivation) -> ReadmissionActivationState:
    return ReadmissionActivationState(
        active=activation.active,
        inactive_reasons=list(activation.inactive_reasons),
        announced_at=activation.announced_at,
        effective_at=activation.effective_at,
        active_bench_version=activation.active_bench_version,
        min_bench_version=activation.min_bench_version,
        notice_period_seconds=activation.notice_period_seconds,
        fee_amount_rao=activation.fee_amount_rao,
        settings_revision=activation.settings_revision,
    )


def cost_basis_view(basis: CostBasis) -> ReadmissionCostBasis:
    return ReadmissionCostBasis(
        bench_version=basis.bench_version,
        agents_sampled=basis.agents_sampled,
        runs_per_evaluation=basis.runs_per_evaluation,
        median_run_cost_usd=basis.median_run_cost_usd,
        median_agent_cost_usd=basis.median_agent_cost_usd,
        measured_at=basis.measured_at,
    )


def readmission_terms(
    activation: ReadmissionActivation, cost_basis: CostBasis | None = None
) -> ReadmissionTerms:
    return ReadmissionTerms(
        activation=activation_state(activation),
        rules=list(READMISSION_RULES),
        cost_basis=cost_basis_view(cost_basis) if cost_basis is not None else None,
    )


__all__ = [
    "READMISSION_RULES",
    "activation_state",
    "cost_basis_view",
    "readmission_terms",
]
