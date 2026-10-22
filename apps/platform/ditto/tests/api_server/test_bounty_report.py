"""The bounty report over recorded public responses, and each check it makes.

The input is the dashboard's recorded lifecycle (test_bounty_board_fixtures.py
pins it to the live API). The database already refuses every broken invariant,
so each check is shown to catch its own violation on an edited copy.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ditto.api_models.bounty import (
    BountyClaimView,
    BountyDetail,
    BountyEvaluationDecisionView,
    BountyEvaluationEntryView,
    BountyEvaluationView,
)
from ditto.api_server.bounty_report import bounty_report

FIXTURES = Path(__file__).resolve().parents[3] / "dashboard" / "fixtures"
DETAIL = json.loads((FIXTURES / "bounty-detail.json").read_text(encoding="utf-8"))
CLAIMS = json.loads((FIXTURES / "bounty-claims.json").read_text(encoding="utf-8"))
FEE = 2_000_000_000


def by_login(claims: dict[str, Any], login: str) -> dict[str, Any]:
    return next(c for c in claims.values() if c["github_login"] == login)


NONE = BountyEvaluationView(entries=[], decisions=[])


def report(
    detail: dict[str, Any],
    claims: dict[str, Any],
    evaluation: BountyEvaluationView = NONE,
):
    return bounty_report(
        detail=BountyDetail.model_validate(detail),
        claims=[BountyClaimView.model_validate(c) for c in claims.values()],
        appeals=[],
        audit=[],
        evaluation=evaluation,
    )


def test_the_recorded_round_reports_every_fee_and_award():
    made = report(DETAIL, CLAIMS)
    assert [check.name for check in made.checks if not check.ok] == []
    outcomes = {
        c.github_login: (c.outcome, c.fee, c.award_alpha_rao) for c in made.claims
    }
    assert outcomes == {
        "dave-dev": ("withdrawn", "retained", 0),
        "alice-dev": ("accepted", "refunded", 40_000_000_000),
        "bob-dev": ("rejected", "undecided", 0),
        "carol-dev": ("claimed", "undecided", 0),
    }
    assert (made.fees_paid.count, made.fees_paid.alpha_rao) == (4, 4 * FEE)
    assert (made.fees_refunded.count, made.fees_retained.count) == (1, 1)
    assert made.fees_undecided.count == 2
    assert (made.awarded.alpha_rao, made.awards_paid.count) == (40_000_000_000, 0)
    assert made.transfers == {"confirmed": 1}
    assert made.decisions == {"accept": 1, "reject": 1}
    assert made.facts == {"merged": 1}


def test_the_report_names_the_bounty_and_what_accepted_work_delivered():
    made = report(DETAIL, CLAIMS)
    summary = DETAIL["summary"]
    assert (
        made.repo,
        made.issue,
        made.title,
        made.revision,
        made.spec_digest,
        made.published_at,
    ) == (
        summary["repo"],
        summary["issue"],
        summary["title"],
        summary["current_revision"],
        summary["spec_digest"],
        datetime.fromisoformat(summary["published_at"]),
    )
    alice = by_login(CLAIMS, "alice-dev")
    [submission] = alice["submissions"]
    accepted_at = datetime.fromisoformat(submission["decided_at"])
    claimed_at = datetime.fromisoformat(alice["claimed_at"])
    published_at = datetime.fromisoformat(summary["published_at"])
    assert made.publish_to_first_accept_seconds == round(
        (accepted_at - published_at).total_seconds()
    )
    reported = {c.github_login: c.accepted for c in made.claims}
    # Only accepted work has a delivery to report.
    assert [login for login, accepted in reported.items() if accepted] == ["alice-dev"]
    delivered = reported["alice-dev"]
    assert delivered is not None
    assert (delivered.pr_number, delivered.head_sha, delivered.accepted_at) == (
        submission["pr_number"],
        submission["head_sha"],
        accepted_at,
    )
    assert delivered.claim_to_accept_seconds == round(
        (accepted_at - claimed_at).total_seconds()
    )
    assert delivered.integration_status == "merged"
    assert [(f.kind, f.reference) for f in delivered.facts] == [("merged", "cd" * 20)]
    refund = next(o for o in alice["obligations"] if o["kind"] == "fee_refund")
    [paid] = refund["transfers"]
    assert [
        (r.obligation_id, r.kind, r.amount_alpha_rao, r.round, r.receipt.block_hash)
        for r in delivered.receipts
    ] == [
        (
            UUID(refund["obligation_id"]),
            "fee_refund",
            FEE,
            1,
            paid["receipt"]["block_hash"],
        )
    ]


@pytest.mark.parametrize(
    ("kinds", "status"),
    [
        ((), "pending"),
        (("merged",), "merged"),
        (("merged", "deployed"), "deployed"),
        # Each fact is recorded on its own; the furthest one counts.
        (("adopted",), "adopted"),
        (("deployed", "merged"), "deployed"),
    ],
)
def test_integration_is_the_furthest_delivery_fact(kinds, status):
    claims = copy.deepcopy(CLAIMS)
    alice = by_login(claims, "alice-dev")
    alice["facts"] = [
        {"kind": kind, "reference": "fixture", "recorded_at": alice["claimed_at"]}
        for kind in kinds
    ]
    delivered = next(c for c in report(DETAIL, claims).claims if c.accepted)
    assert delivered.accepted is not None
    assert delivered.accepted.integration_status == status


def test_only_confirmed_transfers_are_receipts():
    claims = copy.deepcopy(CLAIMS)
    refund = alice_refund({"claims": claims})
    confirmed = refund["transfers"][0]
    refund["transfers"] = [
        {**confirmed, "round": 1, "state": "failed", "receipt": None},
        {**confirmed, "round": 2},
    ]
    refund["round"] = 2
    delivered = next(c for c in report(DETAIL, claims).claims if c.accepted)
    assert delivered.accepted is not None
    assert [r.round for r in delivered.accepted.receipts] == [2]


def test_a_bounty_without_accepted_work_has_no_first_acceptance():
    claims = copy.deepcopy(CLAIMS)
    by_login(claims, "alice-dev")["slot_status"] = "rejected"
    made = report(DETAIL, claims)
    assert made.publish_to_first_accept_seconds is None
    assert all(c.accepted is None for c in made.claims)


def test_a_later_smaller_pool_never_fails_an_award_its_claim_signed_for():
    detail = copy.deepcopy(DETAIL)
    first = detail["revisions"][0]
    smaller = {**copy.deepcopy(first), "revision": 2}
    smaller["terms"]["reward_pool_alpha_rao"] = 1
    detail["revisions"].append(smaller)
    detail["summary"].update(current_revision=2, reward_pool_alpha_rao=1)
    made = report(detail, CLAIMS)
    assert [check.name for check in made.checks if not check.ok] == []
    assert made.reward_pool_alpha_rao == 1
    # The check's evidence is the bound it checks, not the current pool.
    check = next(c for c in made.checks if c.name == "awards_within_pool")
    assert check.detail == (
        "40000000000 alpha rao awarded, each bounded by the pool its claim "
        "signed: 100000000000 in revision 1"
    )


@pytest.mark.parametrize(
    ("amount", "ok"), [(60_000_000_000, True), (60_000_000_001, False)]
)
def test_awards_recorded_at_the_same_instant_count_against_each_other(amount, ok):
    claims = copy.deepcopy(CLAIMS)
    alice = by_login(claims, "alice-dev")
    award = next(o for o in alice["obligations"] if o["kind"] == "award")
    tied = {
        **copy.deepcopy(award),
        "obligation_id": "00000000-0000-4000-8000-0000000000aa",
        "amount_alpha_rao": amount,
    }
    by_login(claims, "carol-dev")["obligations"].append(tied)
    made = report(DETAIL, claims)
    # Alice's 40 alpha and this award share an instant against a 100 alpha pool.
    check = next(c for c in made.checks if c.name == "awards_within_pool")
    assert check.ok is ok


def test_the_median_review_time_rounds_to_the_nearest_second():
    claims = copy.deepcopy(CLAIMS)
    decided = [s for c in claims.values() for s in c["submissions"] if s["decided_at"]]
    assert len(decided) == 2
    # Reviews of 1.4 and 2.4 seconds: a median of 1.9 reads 2, not 1.
    for submission, seconds in zip(decided, (1.4, 2.4), strict=True):
        submitted = datetime.fromisoformat(submission["submitted_at"])
        submission["decided_at"] = (submitted + timedelta(seconds=seconds)).isoformat()
    assert report(DETAIL, claims).median_review_seconds == 2


State = dict[str, dict[str, Any]]


@pytest.mark.parametrize("retained_first", [True, False])
def test_conflicting_dispositions_report_the_same_fee_in_any_order(retained_first):
    claims = copy.deepcopy(CLAIMS)
    retained = next(
        o
        for o in by_login(claims, "dave-dev")["obligations"]
        if o["kind"] == "fee_retained"
    )
    alice = by_login(claims, "alice-dev")
    extra = {**copy.deepcopy(retained), "claim_id": alice["claim_id"]}
    alice["obligations"] = (
        [extra, *alice["obligations"]]
        if retained_first
        else [*alice["obligations"], extra]
    )
    made = report(DETAIL, claims)
    fees = {c.github_login: c.fee for c in made.claims}
    assert fees["alice-dev"] == "refunded"
    assert "one_disposition_per_paid_fee" in {c.name for c in made.checks if not c.ok}


def alice_refund(state: State) -> dict[str, Any]:
    return next(
        o
        for o in by_login(state["claims"], "alice-dev")["obligations"]
        if o["kind"] == "fee_refund"
    )


def _award_over_pool(state: State) -> None:
    # The pool alice's claim signed, not only the one the summary shows.
    state["detail"]["revisions"][0]["terms"]["reward_pool_alpha_rao"] = 1


def _award_on_rejected_work(state: State) -> None:
    by_login(state["claims"], "alice-dev")["slot_status"] = "rejected"


def _two_dispositions(state: State) -> None:
    dave = by_login(state["claims"], "dave-dev")
    dave["obligations"].append(copy.deepcopy(dave["obligations"][0]))


def _accepted_without_refund(state: State) -> None:
    alice = by_login(state["claims"], "alice-dev")
    alice["obligations"] = [
        o for o in alice["obligations"] if o["kind"] != "fee_refund"
    ]


def _two_live_transfers(state: State) -> None:
    refund = alice_refund(state)
    refund["transfers"].append(copy.deepcopy(refund["transfers"][0]))


def _confirmed_without_receipt(state: State) -> None:
    alice_refund(state)["transfers"][0]["receipt"] = None


def _live_money_in_rehearsal(state: State) -> None:
    by_login(state["claims"], "dave-dev")["obligations"][0]["simulated"] = False


@pytest.mark.parametrize(
    ("edit", "check"),
    [
        (_award_over_pool, "awards_within_pool"),
        (_award_on_rejected_work, "awards_on_accepted_work"),
        (_two_dispositions, "one_disposition_per_paid_fee"),
        (_accepted_without_refund, "accepted_fees_refunded"),
        (_two_live_transfers, "one_live_transfer_per_obligation"),
        (_confirmed_without_receipt, "confirmed_transfers_have_receipts"),
        (_live_money_in_rehearsal, "funding_mode_matches"),
    ],
)
def test_each_check_catches_its_own_violation(
    edit: Callable[[State], None], check: str
):
    state = {"detail": copy.deepcopy(DETAIL), "claims": copy.deepcopy(CLAIMS)}
    edit(state)
    made = report(state["detail"], state["claims"])
    assert {c.name for c in made.checks if not c.ok} == {check}


AT = datetime.fromisoformat("2026-10-20T00:00:00+00:00")


def entry(entry_id: int, kind: str, metric: str, value: float):
    return BountyEvaluationEntryView(
        entry_id=entry_id,
        kind=kind,
        metric=metric,
        value=value,
        note=f"private note {entry_id}",
        reason=f"private reason {entry_id}",
        actor=f"operator-{entry_id}@example.com",
        recorded_at=AT + timedelta(hours=entry_id),
    )


def test_an_unrecorded_evaluation_reads_null():
    made = report(DETAIL, CLAIMS).evaluation
    nothing = dict.fromkeys(
        (
            "build_hours",
            "reviewer_hours",
            "integration_hours",
            "operator_hours",
            "turnaround_days",
        )
    )
    assert made.estimates.model_dump() == made.actuals.model_dump() == nothing
    assert (made.decision, made.integration_status, made.reasons) == (None,) * 3
    assert made.decided_at is None


def test_the_evaluation_keeps_estimates_and_actuals_apart_and_names_no_one():
    evaluation = BountyEvaluationView(
        entries=[
            entry(1, "estimate", "build_hours", 40.0),
            entry(2, "actual", "build_hours", 52.5),
            entry(3, "estimate", "reviewer_hours", 6.0),
            # A later estimate supersedes the first; the actual stays apart.
            entry(4, "estimate", "build_hours", 45.0),
            entry(5, "actual", "turnaround_days", 9.0),
        ],
        decisions=[
            BountyEvaluationDecisionView(
                decision_id=1,
                decision="revise",
                integration_status="integrating",
                reasons="Smaller scope next time.",
                actor="decider-one@example.com",
                recorded_at=AT,
            ),
            BountyEvaluationDecisionView(
                decision_id=2,
                decision="expand",
                integration_status="integrated",
                reasons="Cheaper than building it internally.",
                actor="decider-two@example.com",
                recorded_at=AT + timedelta(days=1),
            ),
        ],
    )
    made = report(DETAIL, CLAIMS, evaluation).evaluation
    assert made.estimates.model_dump() == {
        "build_hours": 45.0,
        "reviewer_hours": 6.0,
        "integration_hours": None,
        "operator_hours": None,
        "turnaround_days": None,
    }
    assert made.actuals.model_dump() == {
        "build_hours": 52.5,
        "reviewer_hours": None,
        "integration_hours": None,
        "operator_hours": None,
        "turnaround_days": 9.0,
    }
    assert (made.decision, made.integration_status, made.reasons) == (
        "expand",
        "integrated",
        "Cheaper than building it internally.",
    )
    assert made.decided_at == AT + timedelta(days=1)
    # Source-safe: no operator, and no entry's private note or reason.
    published = report(DETAIL, CLAIMS, evaluation).model_dump_json()
    for private in ("@example.com", "private note", "private reason"):
        assert private not in published
