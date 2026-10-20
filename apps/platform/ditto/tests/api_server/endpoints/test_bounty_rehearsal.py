"""The no-funds rehearsal of whole bounty rounds, ending in their reports (#2944).

Two bounty rounds run through every financial outcome the pilot must
rehearse. The first: publication, three claims, a timely submission with a
change request, a missed deadline, a rejection, two appeals, a reopened slot,
a system-fault cancellation, a shared award, the winners' fee refunds, and
transfers that fail, go unknown and are reconciled. The second: a team
cancellation of a paid slot, a withdrawal whose fee is retained on reopening,
a late fee, a duplicate payment, an award above the payout policy's limit,
an award held while its hotkey has another owner, and transfers that expire
or are reconciled as failed, each followed by a fresh approval round.

Everything goes through the real API with real signatures; money is
simulated. Each operator view's report must then account for every fee and
award, and every settlement check must pass.
"""

from __future__ import annotations

import json
from datetime import timedelta

from ditto.api_server.bounty_claims import (
    SimulatedBountyFeeVerifier,
    VerifiedBountyFee,
)
from ditto.tests.api_server.endpoints.test_bounty_claims import (
    _HEADERS,
    ALICE,
    BOB,
    CAROL,
    DAVE,
    ISSUE,
    REPO,
    World,
)
from ditto.tests.api_server.endpoints.test_bounty_reviews import (
    ADMIN,
    AS_REVIEWER,
    appeal,
    claimed,
    events,
    reopen,
    review,
    submit,
    travel,
    withdraw,
)
from ditto.tests.api_server.endpoints.test_bounty_settlement import (
    FEE,
    ONE,
    POOL,
    SINGLE,
    TWO,
    approve,
    award,
    claim_of,
    dispatch,
    obligation,
    policy,
    reconcile,
)


async def slot_action(world, claim: dict, action: str, **extra):
    return await world.client.post(
        f"{ADMIN}/slots/{claim['slot_id']}/{action}",
        headers=_HEADERS,
        json={
            "reason": f"rehearsal {action}",
            "confirmation": f"{action.upper()} BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 "
            f"SLOT {claim['position']}",
            **extra,
        },
    )


async def decide(world, claim: dict, *, upheld: bool, **extra):
    [filed] = (await claim_of(world, claim))["appeals"]
    verb = "UPHOLD" if upheld else "DENY"
    return await world.client.post(
        f"{ADMIN}/appeals/{filed['appeal_id']}/decision",
        headers=_HEADERS,
        json={
            "upheld": upheld,
            "reason": "rehearsal appeal decision",
            "confirmation": f"{verb} BOUNTY APPEAL {filed['appeal_id']}",
            **extra,
        },
    )


def event_of(trail: list[dict], kind: str, claim: dict) -> dict:
    return next(
        e for e in trail if e["kind"] == kind and e["claim_id"] == claim["claim_id"]
    )


async def pay(world, owed: dict, *approvers: dict, outcome: str = "confirmed"):
    for headers in approvers:
        approved = await approve(world, owed, headers=headers)
        assert approved.status_code == 200, approved.text
    sent = await dispatch(world, owed, outcome=outcome)
    assert sent.status_code == 200, sent.text
    return sent.json()


async def test_a_bounty_round_rehearses_every_outcome(
    app, client, session_maker, monkeypatch
):
    world = World(app, client, session_maker)
    # Publication: revision 1 opens three paid slots.
    await world.start(ALICE, BOB, CAROL, DAVE)
    # Three claims. Alice and Carol submit on time; Bob never does.
    alice = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    carol = (await submit(world, CAROL, await claimed(world, CAROL))).json()
    bob = await claimed(world, BOB)
    assert [c["position"] for c in (alice, carol, bob)] == [1, 2, 3]

    # Six days on, Bob's five-day delivery deadline has passed.
    travel(monkeypatch, timedelta(days=6))
    changes = await review(world, alice, "changes_requested", headers=AS_REVIEWER)
    assert changes.json()["slot_status"] == "rework", changes.text
    alice = (await submit(world, ALICE, changes.json(), sha="cd" * 20)).json()
    accepted = await review(
        world, alice, "accept", headers=AS_REVIEWER, attribution="Alice: the board"
    )
    assert accepted.json()["slot_status"] == "accepted", accepted.text
    rejected = await review(world, carol, "reject", headers=AS_REVIEWER)
    assert rejected.json()["slot_status"] == "rejected", rejected.text

    # Both decisions are appealed. Bob's missed deadline is upheld, Carol's
    # rejection denied, each by an operator other than the reviewer.
    trail = await events(world)
    missed = event_of(trail, "deadline_missed", bob)
    lost = event_of(trail, "rejected", carol)
    assert (
        await appeal(world, BOB, bob, missed["event_id"], "My CI was down.")
    ).status_code == 200
    assert (
        await appeal(world, CAROL, carol, lost["event_id"], "Please re-run it.")
    ).status_code == 200
    restored = await decide(world, bob, upheld=True, restore_days=2)
    assert restored.json()["slot_status"] == "claimed", restored.text
    denied = await decide(world, carol, upheld=False)
    assert denied.json()["slot_status"] == "rejected", denied.text
    bob = (await submit(world, BOB, restored.json(), sha="ef" * 20)).json()
    shared = await review(
        world, bob, "accept", headers=AS_REVIEWER, attribution="Bob: the guide"
    )
    assert shared.status_code == 200, shared.text

    # Carol's slot is reopened, retaining her fee; Dave claims it and a
    # system fault cancels his slot, refunding his fee in full.
    assert (await slot_action(world, carol, "reopen")).status_code == 200
    dave = await claimed(world, DAVE)
    assert dave["slot_id"] == carol["slot_id"]
    assert (
        await slot_action(world, dave, "cancel", kind="system_fault")
    ).status_code == 200

    # A shared award of the whole pool; both need two approvers.
    assert (await policy(world, single=SINGLE, dual=POOL)).status_code == 200
    first = (await award(world, alice, 60_000_000_000, "Alice: the board")).json()
    second = (await award(world, bob, 40_000_000_000, "Bob: the guide")).json()
    # Fee refunds: one confirms, one goes unknown and is reconciled.
    await pay(world, obligation(first, "fee_refund"), ONE)
    unknown = await pay(world, obligation(second, "fee_refund"), ONE, outcome="unknown")
    assert unknown["state"] == "uncertain"
    settled = await reconcile(world, unknown["transfers"][0]["transfer_id"])
    assert settled.json()["state"] == "confirmed", settled.text
    await pay(world, obligation(await claim_of(world, dave), "fee_refund"), ONE)
    # Awards: one transfer fails and is approved again for its next round.
    failed = await pay(world, obligation(first, "award"), ONE, TWO, outcome="failed")
    assert (failed["state"], failed["round"]) == ("failed", 2)
    retried = await pay(world, failed, ONE, TWO)
    assert retried["state"] == "confirmed"
    assert (await pay(world, obligation(second, "award"), ONE, TWO))[
        "state"
    ] == "confirmed"
    merged = await world.client.post(
        f"{ADMIN}/claims/{alice['claim_id']}/facts",
        headers=_HEADERS,
        json={
            "kind": "merged",
            "reference": "cd" * 20,
            "confirmation": f"RECORD BOUNTY MERGED {alice['claim_id']}",
        },
    )
    assert merged.status_code == 200, merged.text

    # The pilot's evaluation: the build's estimate and actual, and a decision.
    bounty_id = world.detail["summary"]["bounty_id"]
    for kind, value in (("estimate", 30.0), ("actual", 36.5)):
        recorded = await world.client.post(
            f"{ADMIN}/{bounty_id}/evaluation/entries",
            headers=ONE,
            json={
                "kind": kind,
                "metric": "build_hours",
                "value": value,
                "expected_entry_id": 0,
                "note": "rehearsal evaluation note",
                "reason": "rehearsal evaluation reason",
                "confirmation": f"RECORD BOUNTY {kind.upper()} BUILD_HOURS "
                f"{REPO}#{ISSUE}",
            },
        )
        assert recorded.status_code == 200, recorded.text
    decided = await world.client.post(
        f"{ADMIN}/{bounty_id}/evaluation/decisions",
        headers=TWO,
        json={
            "decision": "expand",
            "integration_status": "integrated",
            "expected_decision_id": 0,
            "reasons": "Two of three slots delivered.",
            "confirmation": f"DECIDE BOUNTY EVALUATION {REPO}#{ISSUE} EXPAND",
        },
    )
    assert decided.status_code == 200, decided.text

    # The report accounts for every fee, award and transfer.
    view = (await world.client.get(f"{ADMIN}/{bounty_id}", headers=_HEADERS)).json()
    report = view["report"]
    assert [(c["name"], c["ok"]) for c in report["checks"] if not c["ok"]] == []
    assert report["funding_mode"] == "rehearsal"
    assert report["slots"]["closed"] == 3
    outcomes = {
        c["github_login"]: (
            c["outcome"],
            c["fee"],
            c["award_alpha_rao"],
            c["award_state"],
        )
        for c in report["claims"]
    }
    assert outcomes == {
        "alice-dev": ("accepted", "refunded", 60_000_000_000, "confirmed"),
        "bob-dev": ("accepted", "refunded", 40_000_000_000, "confirmed"),
        "carol-dev": ("rejected", "retained", 0, None),
        "dave-dev": ("cancelled", "refunded", 0, None),
    }
    # What each accepted claim delivered, how far it got, and what it was paid.
    delivered = {c["github_login"]: c["accepted"] for c in report["claims"]}
    assert delivered["carol-dev"] is None and delivered["dave-dev"] is None
    alice_work, bob_work = delivered["alice-dev"], delivered["bob-dev"]
    assert (
        alice_work["pr_number"],
        alice_work["head_sha"],
        alice_work["integration_status"],
        [(f["kind"], f["reference"]) for f in alice_work["facts"]],
    ) == (7, "cd" * 20, "merged", [("merged", "cd" * 20)])
    assert (bob_work["head_sha"], bob_work["integration_status"]) == (
        "ef" * 20,
        "pending",
    )
    # Each confirmed transfer once: Alice's award was paid in its second round.
    assert sorted((r["kind"], r["round"]) for r in alice_work["receipts"]) == [
        ("award", 2),
        ("fee_refund", 1),
    ]
    assert sorted((r["kind"], r["round"]) for r in bob_work["receipts"]) == [
        ("award", 1),
        ("fee_refund", 1),
    ]
    assert all(r["receipt"] for r in alice_work["receipts"] + bob_work["receipts"])
    # The bounty is named, and its first acceptance came six days in.
    assert (report["repo"], report["issue"], report["revision"]) == (REPO, ISSUE, 1)
    assert report["spec_digest"] == world.detail["summary"]["spec_digest"]
    assert report["publish_to_first_accept_seconds"] >= 6 * 86_400
    assert alice_work["claim_to_accept_seconds"] >= 6 * 86_400
    assert report["fees_paid"] == {"count": 4, "alpha_rao": 4 * FEE}
    assert report["fees_refunded"] == {"count": 3, "alpha_rao": 3 * FEE}
    assert report["fees_retained"] == {"count": 1, "alpha_rao": FEE}
    for empty in ("fees_refund_owed", "fees_undecided", "payments_returned"):
        assert report[empty] == {"count": 0, "alpha_rao": 0}, empty
    assert report["awarded"] == {"count": 2, "alpha_rao": POOL}
    assert report["awards_paid"] == {"count": 2, "alpha_rao": POOL}
    assert report["reward_pool_alpha_rao"] == POOL
    assert report["transfers"] == {"confirmed": 5, "failed": 1}
    assert report["decisions"] == {"changes_requested": 1, "accept": 2, "reject": 1}
    assert (
        report["appeals_filed"],
        report["appeals_upheld"],
        report["appeals_denied"],
    ) == (2, 1, 1)
    assert report["facts"] == {"merged": 1}
    assert report["operator_actions"] == len(view["audit"]) > 0
    evaluation = report["evaluation"]
    assert (
        evaluation["estimates"]["build_hours"],
        evaluation["actuals"]["build_hours"],
        evaluation["decision"],
        evaluation["integration_status"],
        evaluation["reasons"],
    ) == (30.0, 36.5, "expand", "integrated", "Two of three slots delivered.")
    # Source-safe: no operator, private reason or appeal statement appears.
    published = json.dumps(report)
    for private in (
        "operator-fixture@example.com",
        "reviewer-private@example.com",
        "approver-one@example.com",
        "approver-two@example.com",
        "rehearsal appeal decision",
        "My CI was down.",
        "rehearsal evaluation note",
        "rehearsal evaluation reason",
    ):
        assert private not in published


class SecondReceipt:
    """Verifies any receipt as the claim's exact fee from its payee.

    The simulated verifier accepts exactly one receipt per claim, so a
    duplicate payment is rehearsed with a verifier that accepts another.
    """

    async def verify(self, receipt, *, claim):
        return VerifiedBountyFee(
            block_hash=receipt.block_hash,
            block_number=receipt.block_number,
            extrinsic_index=receipt.extrinsic_index,
            payer_coldkey=claim.payee_coldkey,
            amount_alpha_rao=claim.fee_alpha_rao,
            simulated=True,
        )


async def test_a_bounty_round_rehearses_the_remaining_outcomes(
    app, client, session_maker, monkeypatch
):
    world = World(app, client, session_maker)
    await world.start(ALICE, BOB, CAROL, DAVE)
    alice = await claimed(world, ALICE)
    bob = await claimed(world, BOB)
    carol = await claimed(world, CAROL)
    assert [c["position"] for c in (alice, bob, carol)] == [1, 2, 3]

    # The team cancels Carol's paid slot: her fee is owed back in full.
    assert (await slot_action(world, carol, "cancel", kind="team")).status_code == 200
    # Bob withdraws, and reopening his slot retains his fee.
    assert (await withdraw(world, BOB, bob)).status_code == 200
    assert (await reopen(world, bob)).status_code == 200
    # Dave holds the reopened slot but pays after the hold lapsed: refunded.
    held = await world.hold(DAVE)
    assert held.json()["slot_id"] == bob["slot_id"], held.text
    travel(monkeypatch, timedelta(minutes=31))
    late = (await world.confirm(held.json())).json()
    assert [r["reason"] for r in late["refunds"]] == ["hold_expired"]
    # Carol claims the slot again and pays twice: the second fee is refunded.
    again = await claimed(world, CAROL)
    assert again["slot_id"] == bob["slot_id"]
    world.app.state.bounty_fee_verifier = SecondReceipt()
    second = {"block_hash": "0x" + "22" * 32, "block_number": 9, "extrinsic_index": 1}
    twice = (await world.confirm(again, second)).json()
    world.app.state.bounty_fee_verifier = SimulatedBountyFeeVerifier()
    assert (twice["state"], [r["reason"] for r in twice["refunds"]]) == (
        "claimed",
        ["duplicate_payment"],
    )

    # Alice's work is accepted and awarded more than two approvers may approve.
    alice = (await submit(world, ALICE, alice)).json()
    accepted = await review(
        world, alice, "accept", headers=AS_REVIEWER, attribution="Alice: the docs"
    )
    assert accepted.status_code == 200, accepted.text
    alice = accepted.json()
    assert (await policy(world, single=SINGLE, dual=40 * 10**9)).status_code == 200
    granted = await award(world, alice, 60 * 10**9, "Alice: the docs")
    prize = obligation(granted.json(), "award")
    assert (prize["state"], prize["approvals_required"]) == ("over_policy_limit", None)
    refused = await approve(world, prize)
    assert refused.status_code == 409 and "exceeds" in refused.text
    raised = await policy(world, expected=1, single=SINGLE, dual=POOL)
    assert raised.status_code == 200, raised.text
    # While Alice's hotkey has another owner, her award's payout holds.
    world.owners[ALICE.hotkey.ss58_address] = BOB.coldkey.ss58_address
    held_award = await approve(world, prize, policy_revision=2)
    assert held_award.status_code == 409 and "payout held" in held_award.text
    world.owners[ALICE.hotkey.ss58_address] = ALICE.coldkey.ss58_address

    async def settle(owed: dict, *approvers: dict, outcome: str = "confirmed"):
        for headers in approvers:
            approved = await approve(world, owed, headers=headers, policy_revision=2)
            assert approved.status_code == 200, approved.text
        sent = await dispatch(world, owed, outcome=outcome)
        assert sent.status_code == 200, sent.text
        return sent.json()

    # Alice's refund goes unknown and expires; a fresh round is approved.
    unknown = await settle(obligation(alice, "fee_refund"), ONE, outcome="unknown")
    expired = await reconcile(world, unknown["transfers"][0]["transfer_id"], "expired")
    assert (expired.json()["state"], expired.json()["round"]) == ("failed", 2)
    assert (await settle(expired.json(), ONE))["state"] == "confirmed"
    # Her award goes unknown and reconciles as failed; two approve it again.
    unknown = await settle(prize, ONE, TWO, outcome="unknown")
    failed = await reconcile(world, unknown["transfers"][0]["transfer_id"], "failed")
    assert (failed.json()["state"], failed.json()["round"]) == ("failed", 2)
    assert (await settle(failed.json(), ONE, TWO))["state"] == "confirmed"
    # The cancellation refund and the late fee are paid; the duplicate is owed.
    cancelled = obligation(await claim_of(world, carol), "fee_refund")
    assert cancelled["reason"] == "cancelled"
    assert (await settle(cancelled, ONE))["state"] == "confirmed"
    assert (await settle(obligation(late, "fee_refund"), ONE))["state"] == "confirmed"

    bounty_id = world.detail["summary"]["bounty_id"]
    view = (await world.client.get(f"{ADMIN}/{bounty_id}", headers=_HEADERS)).json()
    report = view["report"]
    assert [(c["name"], c["ok"]) for c in report["checks"] if not c["ok"]] == []
    outcomes = {
        c["claim_id"]: (c["outcome"], c["fee"], c["award_alpha_rao"], c["award_state"])
        for c in report["claims"]
    }
    assert outcomes == {
        alice["claim_id"]: ("accepted", "refunded", 60 * 10**9, "confirmed"),
        bob["claim_id"]: ("withdrawn", "retained", 0, None),
        carol["claim_id"]: ("cancelled", "refunded", 0, None),
        late["claim_id"]: ("refund_owed", "unpaid", 0, None),
        again["claim_id"]: ("claimed", "undecided", 0, None),
    }
    assert report["slots"]["closed"] == 2 and report["slots"]["claimed"] == 1
    assert report["fees_paid"] == {"count": 4, "alpha_rao": 4 * FEE}
    assert report["fees_refunded"] == {"count": 2, "alpha_rao": 2 * FEE}
    assert report["fees_refund_owed"] == {"count": 0, "alpha_rao": 0}
    assert report["fees_retained"] == {"count": 1, "alpha_rao": FEE}
    assert report["fees_undecided"] == {"count": 1, "alpha_rao": FEE}
    assert report["payments_returned"] == {"count": 1, "alpha_rao": FEE}
    assert report["payments_return_owed"] == {"count": 1, "alpha_rao": FEE}
    assert report["awarded"] == {"count": 1, "alpha_rao": 60 * 10**9}
    assert report["awards_paid"] == {"count": 1, "alpha_rao": 60 * 10**9}
    assert report["transfers"] == {"confirmed": 4, "expired": 1, "failed": 1}
    assert report["decisions"] == {"accept": 1}
    # Both of Alice's payments landed in their second rounds.
    [work] = [c["accepted"] for c in report["claims"] if c["accepted"]]
    assert (work["head_sha"], work["integration_status"]) == ("ab" * 20, "pending")
    assert sorted((r["kind"], r["round"]) for r in work["receipts"]) == [
        ("award", 2),
        ("fee_refund", 2),
    ]
    # The runbook's escrow balance: every fee received less every refund paid.
    received = sum(
        report[name]["alpha_rao"]
        for name in ("fees_paid", "payments_returned", "payments_return_owed")
    )
    returned = (
        report["fees_refunded"]["alpha_rao"] + report["payments_returned"]["alpha_rao"]
    )
    held_by_escrow = sum(
        report[name]["alpha_rao"]
        for name in (
            "fees_retained",
            "fees_refund_owed",
            "fees_undecided",
            "payments_return_owed",
        )
    )
    assert received - returned == held_by_escrow == 3 * FEE
