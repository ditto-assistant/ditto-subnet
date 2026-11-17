"""Wire projections of the SN-485 readmission gate.

The predicate lives in :mod:`ditto.db.queries.readmission_gate`; this module
only renders it for the admin settings view, the public submission terms and
the readmission endpoints, so every surface reports the same state.
"""

from __future__ import annotations

from ditto.api_models.submission_settings import ReadmissionActivationState
from ditto.api_models.submission_terms import ReadmissionCostBasis, ReadmissionTerms
from ditto.api_server.readmission_cost_basis import CostBasis
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
