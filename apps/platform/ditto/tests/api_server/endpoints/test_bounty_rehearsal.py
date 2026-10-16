"""The no-funds rehearsal of a whole bounty round, ending in its report (#2944).

One bounty runs through every financial outcome the pilot must rehearse:
publication, three claims, a timely submission with a change request, a missed
deadline, a rejection, two appeals, a reopened slot, a system-fault
cancellation, a shared award, the winners' fee refunds, and transfers that
fail, go unknown and are reconciled. Everything goes through the real API with
real signatures; money is simulated. The operator view's report must then
account for every fee and award, and every settlement check must pass.
"""

from __future__ import annotations

import json
from datetime import timedelta

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
    review,
    submit,
    travel,
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

    # The report accounts for every fee, award and transfer.
    bounty_id = world.detail["summary"]["bounty_id"]
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
    # Source-safe: no operator, private reason or appeal statement appears.
    published = json.dumps(report)
    for private in (
        "operator-fixture@example.com",
        "reviewer-private@example.com",
        "approver-one@example.com",
        "rehearsal appeal decision",
        "My CI was down.",
    ):
        assert private not in published
