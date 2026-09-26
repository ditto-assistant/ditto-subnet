"""The terminal-review emission gate: classifier, window rule, and safe default.

These are the properties that decide whether #2041 is safe to merge, so each one
is asserted on its own rather than through a route:

1. With no revision stored -- what merely merging this actually deploys -- the
   gate evaluates nothing, withholds nothing, and reports itself as running on
   the default.
2. Each withheld class named in the issue is recognized from the state the
   repository already stores, and is reported under its own name.
3. A terminal clear takes effect at the NEXT window and never inside the one it
   landed in, so nothing is ever paid backwards.
4. A malformed revision degrades to the default (keep paying), not to
   withholding.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from ditto.api_models.emission_eligibility import (
    DEFAULT_SETTINGS,
    STATE_REASONS,
    EmissionEligibilitySettings,
    eligibility_checksum,
    next_window_start,
    window_start,
)
from ditto.api_server.emission_eligibility import (
    DEFAULT_POLICY,
    ResolvedEligibilityPolicy,
    classify,
    evaluate_ledger,
    policy_from_row,
    settings_from_row,
    shadow_rows,
)
from ditto.db.queries.emission_eligibility import AgentReviewPosture

_WINDOW = 3_600
# Deliberately not on a boundary: every next-window assertion below would pass
# trivially at :00:00.
_NOW = datetime(2026, 9, 23, 14, 37, 11, tzinfo=UTC)
_SHA = "ab" * 32


def _policy(
    enforcement: str = "enforce", **overrides: object
) -> ResolvedEligibilityPolicy:
    settings = EmissionEligibilitySettings(
        enforcement=enforcement,  # type: ignore[arg-type]
        activation_window_seconds=_WINDOW,
        **overrides,  # type: ignore[arg-type]
    )
    return ResolvedEligibilityPolicy(
        settings=settings,
        revision=7,
        checksum=eligibility_checksum(settings),
        source="revision",
    )


def _row(agent_id: UUID, *, composite: float = 0.9) -> Any:
    """A ``LedgerRow``-shaped stand-in: the gate reads three fields off it."""
    return SimpleNamespace(
        agent_id=agent_id, sha256=_SHA, bench_version=13, composite=composite
    )


def _classify(
    posture: AgentReviewPosture,
    *,
    policy: ResolvedEligibilityPolicy | None = None,
    now: datetime = _NOW,
):
    return classify(
        agent_id=posture.agent_id,
        artifact_sha256=_SHA,
        bench_version=13,
        posture=posture,
        policy=policy or _policy(),
        now=now,
    )


class TestDefaultPosture:
    def test_shipping_default_is_enforcement_off(self) -> None:
        assert DEFAULT_SETTINGS.enforcement == "off"
        assert DEFAULT_POLICY.evaluating is False
        assert DEFAULT_POLICY.enforcing is False

    def test_off_evaluates_nothing_even_for_an_open_hold(self) -> None:
        held = AgentReviewPosture(agent_id=uuid4(), review_status="pending")
        record = _classify(held, policy=ResolvedEligibilityPolicy())
        # Not "withheld but unenforced" -- with the gate off the platform does
        # not form an opinion at all, so nothing downstream can act on one.
        assert record.state == "eligible"
        assert record.reward_eligible is True
        assert record.posture_satisfied is True

    def test_missing_revision_reports_the_default_as_the_source(self) -> None:
        policy = policy_from_row(None)
        assert policy.settings == DEFAULT_SETTINGS
        assert policy.revision == 0
        assert policy.source == "default"

    def test_malformed_revision_falls_back_to_paying_not_withholding(self) -> None:
        row = SimpleNamespace(
            revision=4, checksum="0" * 64, settings={"enforcement": "obliterate"}
        )
        assert settings_from_row(row) is DEFAULT_SETTINGS  # type: ignore[arg-type]
        policy = policy_from_row(row)  # type: ignore[arg-type]
        assert policy.enforcing is False
        # The unusable row stays findable, but the posture must NOT claim to be
        # running on it: an operator who reads "revision 4" would otherwise
        # believe a gate is live that is not.
        assert policy.revision == 4
        assert policy.source == "default"


class TestWithheldClasses:
    def test_unresolved_review_is_withheld_and_explained(self) -> None:
        record = _classify(
            AgentReviewPosture(
                agent_id=uuid4(), review_status="pending", review_kind="copy"
            )
        )
        assert record.state == "unresolved_review"
        assert record.posture_satisfied is False
        assert record.reward_eligible is False
        assert record.reason == STATE_REASONS["unresolved_review"]
        # Bound to the exact artifact, benchmark version and posture revision.
        assert record.artifact_sha256 == _SHA
        assert record.bench_version == 13
        assert record.policy_revision == 7

    def test_inconclusive_bounded_review_is_withheld_without_accusing(self) -> None:
        record = _classify(
            AgentReviewPosture(
                agent_id=uuid4(),
                review_status="pending",
                active_quarantine_reason_code="source-review-inconclusive",
            )
        )
        assert record.state == "review_inconclusive"
        # #2077's contract: a budget outcome is not a finding, and the text a
        # miner reads has to say so.
        assert "not an accusation" in record.reason

    def test_repeated_inconclusive_exhaustion_is_the_same_class(self) -> None:
        record = _classify(
            AgentReviewPosture(
                agent_id=uuid4(),
                review_status="pending",
                latest_attempt_reason_code="repeatedly-inconclusive",
            )
        )
        assert record.state == "review_inconclusive"

    def test_infrastructure_failure_is_withheld_and_named_as_dittos(self) -> None:
        record = _classify(
            AgentReviewPosture(
                agent_id=uuid4(),
                latest_attempt_status="failed",
                latest_attempt_reason_code="docker-build-infrastructure",
            )
        )
        assert record.state == "review_infrastructure_failed"
        assert record.posture_satisfied is False
        # Never a miner violation (#2051): the published reason names Ditto.
        assert "Ditto's own" in record.reason
        assert "not a finding against the submission" in record.reason

    def test_provider_outage_is_an_infrastructure_failure_too(self) -> None:
        record = _classify(
            AgentReviewPosture(
                agent_id=uuid4(),
                latest_attempt_status="failed",
                latest_attempt_reason_code="source-review-retryable-infra",
            )
        )
        assert record.state == "review_infrastructure_failed"

    def test_court_escalation_is_reported_as_escalated_not_merely_open(self) -> None:
        record = _classify(
            AgentReviewPosture(
                agent_id=uuid4(),
                review_status="pending",
                review_kind="copy",
                court_verdict="escalate",
            )
        )
        # Both classes are withheld, but an operator and a miner need the more
        # specific one: "escalated" names who has to act.
        assert record.state == "review_escalated"

    def test_anomalous_score_hold_is_escalated(self) -> None:
        record = _classify(
            AgentReviewPosture(
                agent_id=uuid4(),
                review_status="pending",
                review_kind="anomalous_score",
            )
        )
        assert record.state == "review_escalated"

    def test_stranded_reject_never_earns(self) -> None:
        record = _classify(
            AgentReviewPosture(
                agent_id=uuid4(),
                review_status="resolved",
                review_resolution="reject",
                review_resolved_at=_NOW - timedelta(days=30),
            )
        )
        assert record.state == "review_rejected"
        assert record.posture_satisfied is False
        assert record.activates_at is None
        # The score and the review trail stay published; only the payment stops.
        assert "stay published" in record.reason

    def test_never_reviewed_artifact_stays_eligible_by_default(self) -> None:
        record = _classify(AgentReviewPosture(agent_id=uuid4()))
        assert record.state == "eligible"
        assert record.posture_satisfied is True

    def test_operator_can_require_a_completed_review(self) -> None:
        record = _classify(
            AgentReviewPosture(agent_id=uuid4()),
            policy=_policy(require_completed_review=True),
        )
        assert record.state == "review_missing"

    def test_each_class_can_be_switched_off_independently(self) -> None:
        held = AgentReviewPosture(
            agent_id=uuid4(),
            latest_attempt_status="failed",
            latest_attempt_reason_code="docker-build-infrastructure",
        )
        permissive = _policy(exclude_infrastructure_failed=False)
        assert _classify(held, policy=permissive).state == "eligible"


class TestNextWindowRule:
    def test_window_start_is_a_tumbling_utc_boundary(self) -> None:
        assert window_start(_NOW, window_seconds=_WINDOW) == datetime(
            2026, 9, 23, 14, 0, 0, tzinfo=UTC
        )
        assert next_window_start(_NOW, window_seconds=_WINDOW) == datetime(
            2026, 9, 23, 15, 0, 0, tzinfo=UTC
        )

    def test_a_clear_inside_the_current_window_waits_for_the_next_one(self) -> None:
        cleared = AgentReviewPosture(
            agent_id=uuid4(),
            review_status="resolved",
            review_resolution="clear",
            review_resolved_at=datetime(2026, 9, 23, 14, 5, 0, tzinfo=UTC),
        )
        record = _classify(cleared)
        assert record.state == "awaiting_next_window"
        assert record.posture_satisfied is False
        assert record.activates_at == datetime(2026, 9, 23, 15, 0, 0, tzinfo=UTC)
        # The promise the miner is owed, and the one #2041 forbids breaking.
        assert "never applied backwards" in record.reason

    def test_the_same_clear_is_eligible_in_the_following_window(self) -> None:
        cleared = AgentReviewPosture(
            agent_id=uuid4(),
            review_status="resolved",
            review_resolution="clear",
            review_resolved_at=datetime(2026, 9, 23, 14, 5, 0, tzinfo=UTC),
        )
        later = _classify(cleared, now=datetime(2026, 9, 23, 15, 0, 1, tzinfo=UTC))
        assert later.state == "eligible"
        assert later.posture_satisfied is True
        # Eligibility begins at the boundary. Nothing anywhere in this record
        # describes the withheld period as owed, because there is no back-pay
        # field to put it in.
        assert later.activates_at is None

    def test_a_clear_before_this_window_opened_is_already_eligible(self) -> None:
        cleared = AgentReviewPosture(
            agent_id=uuid4(),
            review_status="resolved",
            review_resolution="clear",
            review_resolved_at=datetime(2026, 9, 23, 13, 59, 59, tzinfo=UTC),
        )
        assert _classify(cleared).state == "eligible"


class TestLedgerEvaluation:
    def test_enforcement_drops_the_top_scorer_and_keeps_the_rest(self) -> None:
        held, clean = uuid4(), uuid4()
        rows = [_row(held, composite=0.95), _row(clean, composite=0.80)]
        evaluation = evaluate_ledger(
            rows,
            {held: AgentReviewPosture(agent_id=held, review_status="pending")},
            policy=_policy("enforce"),
            now=_NOW,
        )
        assert [row.agent_id for row in evaluation.filter_rows(rows)] == [clean]
        assert [record.agent_id for record in evaluation.withheld] == [held]

    def test_shadow_keeps_paying_and_still_records_the_finding(self) -> None:
        held, clean = uuid4(), uuid4()
        rows = [_row(held, composite=0.95), _row(clean)]
        evaluation = evaluate_ledger(
            rows,
            {held: AgentReviewPosture(agent_id=held, review_status="pending")},
            policy=_policy("shadow"),
            now=_NOW,
        )
        # The whole point of shadow: the pool is untouched...
        assert [row.agent_id for row in evaluation.filter_rows(rows)] == [held, clean]
        assert evaluation.records[held].reward_eligible is True
        # ...but the verdict enforcement WOULD have reached is durable.
        assert evaluation.records[held].posture_satisfied is False
        recorded = shadow_rows(evaluation)
        assert [row["agent_id"] for row in recorded] == [held]
        assert recorded[0]["state"] == "unresolved_review"
        assert recorded[0]["enforcement"] == "shadow"
        assert recorded[0]["window_start"] == window_start(_NOW, window_seconds=_WINDOW)
        assert recorded[0]["policy_revision"] == 7

    def test_off_records_nothing_at_all(self) -> None:
        held = uuid4()
        rows = [_row(held)]
        evaluation = evaluate_ledger(
            rows,
            {held: AgentReviewPosture(agent_id=held, review_status="pending")},
            policy=ResolvedEligibilityPolicy(),
            now=_NOW,
        )
        assert evaluation.filter_rows(rows) == rows
        assert evaluation.withheld == []
        assert shadow_rows(evaluation) == []

    def test_an_unevaluated_row_is_never_dropped(self) -> None:
        """Absence of evidence is not a reason to stop paying a miner."""
        rows = [_row(uuid4())]
        evaluation = evaluate_ledger(rows, {}, policy=_policy("enforce"), now=_NOW)
        assert evaluation.filter_rows(rows) == rows

    def test_window_is_shared_by_every_row_in_one_evaluation(self) -> None:
        rows = [_row(uuid4()) for _ in range(3)]
        evaluation = evaluate_ledger(rows, {}, policy=_policy("enforce"), now=_NOW)
        assert {record.window_start for record in evaluation.records.values()} == {
            evaluation.window_start
        }


@pytest.mark.parametrize("state", sorted(STATE_REASONS))
def test_every_state_has_source_free_miner_facing_text(state: str) -> None:
    reason = STATE_REASONS[state]
    assert reason.endswith(".")
    assert len(reason) > 40
    # These strings render on the public board. Nothing that could leak a
    # finding, a threshold or reviewer output belongs in them.
    lowered = reason.lower()
    for forbidden in ("sha256", "prompt", "threshold", "z-score", "cohort"):
        assert forbidden not in lowered
