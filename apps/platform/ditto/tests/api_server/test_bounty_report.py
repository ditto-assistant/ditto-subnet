"""The bounty report over recorded public responses, and each check it makes.

The input is the dashboard's recorded lifecycle (test_bounty_board_fixtures.py
pins it to the live API). The database already refuses every broken invariant,
so each check is shown to catch its own violation on an edited copy.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ditto.api_models.bounty import BountyClaimView, BountyDetail
from ditto.api_server.bounty_report import bounty_report

FIXTURES = Path(__file__).resolve().parents[3] / "dashboard" / "fixtures"
DETAIL = json.loads((FIXTURES / "bounty-detail.json").read_text(encoding="utf-8"))
CLAIMS = json.loads((FIXTURES / "bounty-claims.json").read_text(encoding="utf-8"))
FEE = 2_000_000_000


def by_login(claims: dict[str, Any], login: str) -> dict[str, Any]:
    return next(c for c in claims.values() if c["github_login"] == login)


def report(detail: dict[str, Any], claims: dict[str, Any]):
    return bounty_report(
        detail=BountyDetail.model_validate(detail),
        claims=[BountyClaimView.model_validate(c) for c in claims.values()],
        appeals=[],
        audit=[],
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
