"""Bounty settlement: dispositions, awards, approvals, transfers, facts (#2942, P4).

Built on the P2/P3 world: real PostgreSQL, real signatures and the real link
and CLA flows. Money is simulated end to end; nothing here moves funds.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ditto.api_models.bounty import simulated_transfer_block_hash
from ditto.chain import ChainConnectionError
from ditto.db.models import BountyObligation
from ditto.tests.api_server.endpoints.test_bounty_claims import (
    _ADMIN_TOKEN,
    _HEADERS,
    ALICE,
    BOB,
    CLAIMS,
    REPO,
    World,
)
from ditto.tests.api_server.endpoints.test_bounty_reviews import (
    ADMIN,
    AS_REVIEWER,
    claimed,
    events,
    review,
    submit,
    travel,
    withdraw,
)

pytestmark = pytest.mark.asyncio
ONE = {**_ADMIN_TOKEN, "X-Admin-Actor": "approver-one@example.com"}
TWO = {**_ADMIN_TOKEN, "X-Admin-Actor": "approver-two@example.com"}
SINGLE, DUAL = 5_000_000_000, 50_000_000_000
POOL = 100_000_000_000
FEE = 2_000_000_000


@pytest.fixture
async def world(app, client, session_maker):
    return World(app, client, session_maker)


async def accepted(world, who) -> dict:
    claim = (await submit(world, who, await claimed(world, who))).json()
    done = await review(world, claim, "accept", attribution=f"Credit to {who.login}")
    assert done.status_code == 200, done.text
    return done.json()


async def claim_of(world, claim: dict) -> dict:
    return (await world.client.get(f"{CLAIMS}/{claim['claim_id']}")).json()


def obligation(view: dict, kind: str) -> dict:
    return next(o for o in view["obligations"] if o["kind"] == kind)


async def policy(world, expected=0, single=SINGLE, dual=DUAL, headers=_HEADERS):
    return await world.client.post(
        f"{ADMIN}/payout-policy",
        headers=headers,
        json={
            "expected_revision": expected,
            "single_approver_max_alpha_rao": single,
            "dual_approver_max_alpha_rao": dual,
            "reason": "fixture payout thresholds",
            "confirmation": f"SET BOUNTY PAYOUT POLICY {expected + 1}",
        },
    )


async def award(world, claim: dict, amount: int, attribution="Fixture credit"):
    return await world.client.post(
        f"{ADMIN}/claims/{claim['claim_id']}/award",
        headers=_HEADERS,
        json={
            "amount_alpha_rao": amount,
            "attribution": attribution,
            "reason": "fixture award decision",
            "confirmation": (
                f"AWARD BOUNTY CLAIM {claim['claim_id']} {amount} ALPHA RAO"
            ),
        },
    )


async def approve(world, owed: dict, headers=ONE, **over):
    body = {
        "round": owed["round"],
        "payee_coldkey": owed["payee_coldkey"],
        "amount_alpha_rao": owed["amount_alpha_rao"],
        "policy_revision": 1,
        **over,
    }
    body["confirmation"] = over.get(
        "confirmation",
        f"APPROVE BOUNTY OBLIGATION {owed['obligation_id']} ROUND {body['round']} "
        f"{body['amount_alpha_rao']} ALPHA RAO TO {body['payee_coldkey']}",
    )
    return await world.client.post(
        f"{ADMIN}/obligations/{owed['obligation_id']}/approve",
        headers=headers,
        json=body,
    )


async def dispatch(world, owed: dict, outcome="confirmed", round_number=None):
    round_number = round_number or owed["round"]
    return await world.client.post(
        f"{ADMIN}/obligations/{owed['obligation_id']}/dispatch",
        headers=_HEADERS,
        json={
            "round": round_number,
            "rehearsal_outcome": outcome,
            "confirmation": (
                f"DISPATCH BOUNTY OBLIGATION {owed['obligation_id']} "
                f"ROUND {round_number}"
            ),
        },
    )


async def reconcile(world, transfer_id: str, outcome="confirmed"):
    return await world.client.post(
        f"{ADMIN}/transfers/{transfer_id}/reconcile",
        headers=_HEADERS,
        json={
            "rehearsal_outcome": outcome,
            "confirmation": f"RECONCILE BOUNTY TRANSFER {transfer_id}",
        },
    )


async def test_accepted_work_refunds_its_fee_and_takes_an_award(world):
    await world.start(ALICE)
    claim = await accepted(world, ALICE)
    refund = obligation(claim, "fee_refund")
    assert (refund["reason"], refund["amount_alpha_rao"]) == ("accepted", FEE)
    assert refund["payee_coldkey"] == ALICE.coldkey.ss58_address
    assert refund["state"] == "awaiting_approval" and refund["simulated"] is True
    granted = await award(world, claim, 40_000_000_000, "Fixture credit to alice")
    assert granted.status_code == 200, granted.text
    prize = obligation(granted.json(), "award")
    assert (prize["reason"], prize["attribution"]) == (
        "accepted_work",
        "Fixture credit to alice",
    )
    # An exact retry returns the award; a different one is refused.
    again = await award(world, claim, 40_000_000_000, "Fixture credit to alice")
    assert again.status_code == 200 and again.json() == granted.json()
    other = await award(world, claim, 1_000_000_000)
    assert other.status_code == 409 and "already awarded" in other.text
    detail = await world.refresh()
    assert detail["summary"]["awarded_alpha_rao"] == 40_000_000_000
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds.count("fee_refund_owed") == 1 and kinds.count("award_recorded") == 1
    # Who decided the award, and why, is recorded but stays private.
    assert "operator-fixture" not in str(granted.json())
    assert "fixture award decision" not in str(granted.json())
    async with world.maker() as session:
        recorded = await session.get(BountyObligation, UUID(prize["obligation_id"]))
        assert (recorded.actor, recorded.decision_reason) == (
            "operator-fixture@example.com",
            "fixture award decision",
        )


async def test_awards_never_exceed_the_pool_and_need_accepted_work(world):
    await world.start(ALICE, BOB)
    first = await accepted(world, ALICE)
    waiting = (await submit(world, BOB, await claimed(world, BOB))).json()
    early = await award(world, waiting, 1_000_000_000)
    assert early.status_code == 409 and "only accepted work" in early.text
    second = (await review(world, waiting, "accept")).json()
    assert (await award(world, first, 70_000_000_000)).status_code == 200
    # Amounts stay exact for JSON clients, whatever the pool.
    assert (await award(world, second, 2**53)).status_code == 422
    over = await award(world, second, 30_000_000_001)
    assert over.status_code == 409 and "30000000000 alpha rao remain" in over.text
    # A shared award: each accepted claim takes its own part of the pool.
    assert (await award(world, second, 30_000_000_000)).status_code == 200
    assert (await world.refresh())["summary"]["awarded_alpha_rao"] == POOL


@pytest.mark.parametrize("outcome", ["rejected", "withdrawn", "expired"])
async def test_reopening_a_closed_slot_retains_its_fee(world, monkeypatch, outcome):
    await world.start(ALICE)
    claim = await claimed(world, ALICE)
    if outcome == "rejected":
        submitted = (await submit(world, ALICE, claim)).json()
        await review(world, submitted, "reject")
    elif outcome == "withdrawn":
        assert (await withdraw(world, ALICE, claim)).status_code == 200
    else:
        travel(monkeypatch, timedelta(days=6))
    # Until reopening, the outcome may still be appealed: no disposition yet.
    assert (await claim_of(world, claim))["obligations"] == []
    reopened = await world.client.post(
        f"{ADMIN}/slots/{claim['slot_id']}/reopen",
        headers=_HEADERS,
        json={
            "reason": "fixture reopen",
            "confirmation": f"REOPEN BOUNTY SLOT {REPO}#2942 ROUND 1 SLOT 1",
        },
    )
    assert reopened.status_code == 200, reopened.text
    view = await claim_of(world, claim)
    [kept] = view["obligations"]
    assert (kept["kind"], kept["reason"], kept["state"]) == (
        "fee_retained",
        outcome,
        "retained",
    )
    refused = await approve(world, kept, round=1)
    assert refused.status_code == 409 and "moves no money" in refused.text


async def test_an_award_payout_holds_while_the_hotkey_has_another_owner(world):
    await world.start(ALICE)
    won = await accepted(world, ALICE)
    await policy(world)
    granted = (await award(world, won, SINGLE)).json()
    prize, refund = obligation(granted, "award"), obligation(granted, "fee_refund")
    hotkey = ALICE.hotkey.ss58_address
    world.owners[hotkey] = BOB.coldkey.ss58_address
    held = await approve(world, prize)
    assert held.status_code == 409 and "payout held" in held.text
    assert BOB.coldkey.ss58_address in held.text
    # A refund returns the fee to the coldkey that paid it, whoever owns the
    # hotkey now.
    assert (await approve(world, refund)).status_code == 200
    assert (await dispatch(world, refund)).json()["state"] == "confirmed"
    world.owners[hotkey] = ALICE.coldkey.ss58_address
    assert (await approve(world, prize)).json()["state"] == "approved"
    # The owner changes again before payout: the approved award waits.
    world.owners[hotkey] = BOB.coldkey.ss58_address
    waiting = await dispatch(world, prize)
    assert waiting.status_code == 409 and "payout held" in waiting.text
    assert obligation(await claim_of(world, won), "award")["transfers"] == []
    world.owners[hotkey] = ALICE.coldkey.ss58_address
    assert (await dispatch(world, prize)).json()["state"] == "confirmed"


async def test_an_award_payout_needs_the_finalized_owner(world):
    await world.start(ALICE)
    won = await accepted(world, ALICE)
    await policy(world)
    granted = (await award(world, won, SINGLE)).json()
    world.app.state.chain.get_finalized_block = AsyncMock(
        side_effect=ChainConnectionError("fixture outage")
    )
    unknown = await approve(world, obligation(granted, "award"))
    assert unknown.status_code == 503 and "retry" in unknown.text
    assert obligation(await claim_of(world, won), "award")["approvals"] == 0
    # Refunds never read the chain.
    assert (await approve(world, obligation(granted, "fee_refund"))).status_code == 200


async def test_payout_policy_revisions(world):
    await world.start(ALICE)
    missing = await world.client.get(f"{ADMIN}/payout-policy", headers=_HEADERS)
    assert missing.status_code == 404
    inverted = await policy(world, single=DUAL, dual=SINGLE)
    assert inverted.status_code == 400
    unconfirmed = await world.client.post(
        f"{ADMIN}/payout-policy",
        headers=_HEADERS,
        json={
            "expected_revision": 0,
            "single_approver_max_alpha_rao": SINGLE,
            "dual_approver_max_alpha_rao": DUAL,
            "reason": "fixture payout thresholds",
            "confirmation": "SET BOUNTY PAYOUT POLICY",
        },
    )
    assert unconfirmed.status_code == 409
    first = await policy(world)
    assert first.status_code == 200 and first.json()["revision"] == 1
    # An exact retry returns it; a stale writer is refused.
    assert (await policy(world)).json() == first.json()
    stale = await policy(world, single=1)
    assert stale.status_code == 409 and "revision is 1" in stale.text
    read = await world.client.get(f"{ADMIN}/payout-policy", headers=_HEADERS)
    assert read.json() == first.json()


async def test_approvals_bind_payee_amount_policy_and_distinct_operators(world):
    await world.start(ALICE)
    claim = await accepted(world, ALICE)
    refund = obligation(claim, "fee_refund")
    unconfigured = await approve(world, refund)
    assert (
        unconfigured.status_code == 409
        and "no bounty payout policy" in unconfigured.text
    )
    await policy(world)
    for change, message in (
        ({"amount_alpha_rao": FEE - 1}, "exact payee and amount"),
        ({"payee_coldkey": BOB.coldkey.ss58_address}, "exact payee and amount"),
        ({"round": 2}, "approve round 1"),
        ({"policy_revision": 2}, "approve under revision 1"),
        ({"confirmation": "APPROVE"}, "explicit confirmation"),
    ):
        refused = await approve(world, refund, **change)
        assert refused.status_code == 409 and message in refused.text, change
    # The reviewer who accepted the work cannot approve its money.
    conflicted = await approve(world, refund, headers=AS_REVIEWER)
    assert conflicted.status_code == 403
    approved = await approve(world, refund)
    assert approved.status_code == 200, approved.text
    assert (approved.json()["state"], approved.json()["approvals"]) == ("approved", 1)
    assert (await approve(world, refund)).json() == approved.json()

    granted = (await award(world, claim, 40_000_000_000)).json()
    prize = obligation(granted, "award")
    decider = await approve(world, prize, headers=_HEADERS)
    assert decider.status_code == 403 and "cannot approve" in decider.text
    one = (await approve(world, prize)).json()
    assert (one["state"], one["approvals"], one["approvals_required"]) == (
        "awaiting_approval",
        1,
        2,
    )
    two = (await approve(world, prize, headers=TWO)).json()
    assert two["state"] == "approved"
    # A new policy revision needs fresh approvals under it.
    await policy(world, expected=1, single=SINGLE, dual=DUAL + 1)
    stale = (await claim_of(world, claim))["obligations"]
    assert {o["state"] for o in stale if o["kind"] == "award"} == {"awaiting_approval"}


async def test_amounts_beyond_the_policy_cannot_be_approved(world):
    await world.start(ALICE)
    claim = await accepted(world, ALICE)
    await policy(world, single=SINGLE, dual=SINGLE)
    prize = obligation((await award(world, claim, SINGLE + 1)).json(), "award")
    refused = await approve(world, prize)
    assert refused.status_code == 409 and "lets anyone approve" in refused.text


async def test_an_approved_obligation_is_dispatched_once(world):
    await world.start(ALICE)
    claim = await accepted(world, ALICE)
    await policy(world)
    refund = obligation(claim, "fee_refund")
    early = await dispatch(world, refund)
    assert early.status_code == 409 and "awaiting_approval" in early.text
    await approve(world, refund)
    sent = await dispatch(world, refund)
    assert sent.status_code == 200, sent.text
    paid = sent.json()
    [transfer] = paid["transfers"]
    assert paid["state"] == "confirmed" and transfer["simulated"] is True
    assert transfer["receipt"]["block_hash"] == simulated_transfer_block_hash(
        UUID(transfer["transfer_id"])
    )
    again = await dispatch(world, refund)
    assert again.status_code == 409 and "confirmed" in again.text
    kinds = [e["kind"] for e in await events(world)]
    assert kinds[-2:] == ["transfer_dispatched", "transfer_confirmed"]
    # Delivery facts are never a condition: this paid without any.
    assert (await claim_of(world, claim))["facts"] == []


async def test_a_failed_transfer_needs_fresh_approval(world):
    await world.start(ALICE)
    claim = await accepted(world, ALICE)
    await policy(world)
    refund = obligation(claim, "fee_refund")
    await approve(world, refund)
    failed = (await dispatch(world, refund, "failed")).json()
    assert (failed["state"], failed["round"]) == ("failed", 2)
    assert [t["state"] for t in failed["transfers"]] == ["failed"]
    retry = await dispatch(world, refund, round_number=2)
    assert retry.status_code == 409 and "failed" in retry.text
    stale = await approve(world, refund, round=1)
    assert stale.status_code == 409 and "approve round 2" in stale.text
    assert (await approve(world, refund, round=2)).json()["state"] == "approved"
    paid = (await dispatch(world, refund, round_number=2)).json()
    assert paid["state"] == "confirmed"
    assert [t["state"] for t in paid["transfers"]] == ["failed", "confirmed"]


async def test_an_uncertain_transfer_is_reconciled_before_anything_else(world):
    await world.start(ALICE)
    claim = await accepted(world, ALICE)
    await policy(world)
    refund = obligation(claim, "fee_refund")
    await approve(world, refund)
    unknown = (await dispatch(world, refund, "unknown")).json()
    assert unknown["state"] == "uncertain"
    transfer_id = unknown["transfers"][0]["transfer_id"]
    assert (await dispatch(world, refund)).status_code == 409
    blocked = await approve(world, refund, round=2)
    assert blocked.status_code == 409 and "already uncertain" in blocked.text
    still = (await reconcile(world, transfer_id, "unknown")).json()
    assert still["state"] == "uncertain"
    settled = (await reconcile(world, transfer_id, "confirmed")).json()
    assert settled["state"] == "confirmed"
    assert settled["transfers"][0]["settled_at"] is not None
    twice = await reconcile(world, transfer_id)
    assert twice.status_code == 409 and "already confirmed" in twice.text


async def test_an_expired_transfer_reconciles_to_a_new_round(world):
    await world.start(ALICE)
    claim = await accepted(world, ALICE)
    await policy(world)
    refund = obligation(claim, "fee_refund")
    await approve(world, refund)
    unknown = (await dispatch(world, refund, "unknown")).json()
    gone = (
        await reconcile(world, unknown["transfers"][0]["transfer_id"], "expired")
    ).json()
    assert (gone["state"], gone["round"]) == ("failed", 2)


async def test_delivery_facts_are_public_records_of_accepted_work(world):
    await world.start(ALICE, BOB)
    claim = await accepted(world, ALICE)
    waiting = await claimed(world, BOB)

    async def fact(target: dict, kind: str, reference: str):
        return await world.client.post(
            f"{ADMIN}/claims/{target['claim_id']}/facts",
            headers=_HEADERS,
            json={
                "kind": kind,
                "reference": reference,
                "confirmation": f"RECORD BOUNTY {kind.upper()} {target['claim_id']}",
            },
        )

    early = await fact(waiting, "merged", "a" * 40)
    assert early.status_code == 409 and "accepted work" in early.text
    merged = await fact(claim, "merged", "a" * 40)
    assert merged.status_code == 200, merged.text
    assert merged.json()["facts"][0]["reference"] == "a" * 40
    assert (await fact(claim, "merged", "a" * 40)).json() == merged.json()
    changed = await fact(claim, "merged", "b" * 40)
    assert changed.status_code == 409 and "already recorded" in changed.text
    assert (await fact(claim, "deployed", "v0.370.0")).status_code == 200
    assert "operator-fixture" not in (await claim_of(world, claim)).__repr__()


async def test_settlement_history_is_append_only(world):
    await world.start(ALICE)
    claim = await accepted(world, ALICE)
    await policy(world)
    refund = obligation(claim, "fee_refund")
    await approve(world, refund)
    await dispatch(world, refund)
    history = "bounty history is append-only"
    for statement in (
        "UPDATE bounty_payout_policies SET single_approver_max_alpha_rao = 1",
        "DELETE FROM bounty_obligation_approvals",
        "TRUNCATE bounty_delivery_facts",
        "TRUNCATE bounty_transfers",
        "UPDATE bounty_obligations SET amount_alpha_rao = 1",
    ):
        async with world.maker() as session:
            with pytest.raises(DBAPIError, match=history):
                await session.execute(text(statement))
            await session.rollback()
    async with world.maker() as session:
        with pytest.raises(DBAPIError, match="settled bounty transfer cannot change"):
            await session.execute(text("UPDATE bounty_transfers SET state = 'failed'"))
        await session.rollback()
    async with world.maker() as session:
        with pytest.raises(DBAPIError, match="bounty_transfers_live_idx"):
            await session.execute(
                text(
                    "INSERT INTO bounty_transfers (transfer_id, obligation_id, round, "
                    "executor, simulated, state, actor, dispatched_at) VALUES "
                    "(gen_random_uuid(), :obligation, 9, 'simulated', true, "
                    "'dispatched', 'fixture', now())"
                ),
                {"obligation": refund["obligation_id"]},
            )
        await session.rollback()


async def test_settlement_routes_declare_their_errors(app):
    paths = app.openapi()["paths"]
    for route, statuses in (
        ("/claims/{claim_id}/award", {"401", "404", "409"}),
        ("/payout-policy", {"401", "409"}),
        ("/obligations/{obligation_id}/approve", {"401", "403", "404", "409"}),
        ("/obligations/{obligation_id}/dispatch", {"401", "404", "409"}),
        ("/transfers/{transfer_id}/reconcile", {"401", "404", "409"}),
        ("/claims/{claim_id}/facts", {"401", "404", "409"}),
    ):
        assert statuses <= set(paths[f"{ADMIN}{route}"]["post"]["responses"]), route
