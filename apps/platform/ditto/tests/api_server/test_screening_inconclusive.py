"""Which INCONCLUSIVE screening outcomes an operator reviews vs. a retry."""

from __future__ import annotations

import pytest

from ditto.api_server.screening_inconclusive import (
    inconclusive_is_infrastructure_fault,
    inconclusive_requires_operator_review,
)
from ditto_screening_protocol.reason_codes import (
    INFRA_AUTO_RETRY_REASON_CODES,
    PROVIDER_BACKOFF_REASON_CODES,
    ReasonCodeClass,
    reason_codes,
)


@pytest.mark.parametrize(
    "reason_code",
    [
        # Reviewer non-verdicts the worker emits as INCONCLUSIVE.
        "l2-model-inconclusive",
        "source-review-inconclusive",
        "l2-unsupported-artifact-shape",
        "l2-only-clearance-unproven",
        "l2-unresolved-violation",
        "l2-causal-finding-unavailable",
        "l3-critic-inconclusive",
        "l3-adjudicator-inconclusive",
        "l3-adjudicator-incomplete",
        "l3-violation-adjudicator-inconclusive",
        "l3-violation-adjudicator-disagreement",
        "l3-cause-disagreement",
        "l3-cause-disagreement-unresolved",
        "challenge-inconclusive",
        "behavioral-oracle-inconclusive",
        # Bounded budgets that settled on the notes ledger are a review result.
        "source-review-read-budget-exhausted",
        "source-review-step-budget-exhausted",
        "source-review-lease-budget-exhausted",
        "l2-model-total-budget",
        "source-review-inconsistent-verdict",
        # No code at all: hold rather than silently retry.
        None,
    ],
)
def test_reviewer_non_verdicts_are_held_for_operator(reason_code: str | None) -> None:
    assert inconclusive_requires_operator_review(reason_code)
    assert not inconclusive_is_infrastructure_fault(reason_code)


@pytest.mark.parametrize(
    "reason_code",
    [
        # Provider and transport faults carried as INCONCLUSIVE after an
        # earlier finding (``l2_review._carry_l1_notes``).
        "l3-adjudicator-model-provider-fault",
        "l3-critic-model-provider-fault",
        "l3-violation-adjudicator-model-provider-fault",
        "l3-cause-disagreement-model-provider-fault",
        "l2-model-provider-fault",
        "l3-adjudicator-model-response-contract",
        "l3-critic-http-503",
        "l3-adjudicator-http-400-body-empty",
        "source-review-http-429",
        "l3-adjudicator-connecterror",
        "l3-violation-adjudicator-readtimeout",
        "l3-critic-remoteprotocolerror",
        "l3-adjudicator-oserror",
        "l2-analyzer-contract",
        "l2-analyzer-exited-137",
        "challenge-transport-failure",
        "source-review-provider-credits-exhausted",
        "source-review-model-timeout",
        # The signed-runtime preflight (fleet infrastructure).
        "l2-runtime-evidence-unavailable",
    ],
)
def test_infrastructure_faults_keep_the_retry_path(reason_code: str) -> None:
    assert inconclusive_is_infrastructure_fault(reason_code)
    assert not inconclusive_requires_operator_review(reason_code)


def test_every_registered_infrastructure_code_keeps_the_retry_path() -> None:
    infrastructure = (
        *INFRA_AUTO_RETRY_REASON_CODES,
        *PROVIDER_BACKOFF_REASON_CODES,
        *reason_codes(ReasonCodeClass.OPERATOR_RETRY),
    )
    assert infrastructure
    for code in infrastructure:
        assert inconclusive_is_infrastructure_fault(code), code


def test_registered_policy_codes_are_held_for_operator() -> None:
    for code in reason_codes(ReasonCodeClass.POLICY):
        assert inconclusive_requires_operator_review(code), code
