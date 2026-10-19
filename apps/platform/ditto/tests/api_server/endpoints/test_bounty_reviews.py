"""Bounty submissions, deadlines, review, appeals and slot control (#2942, P3).

Real PostgreSQL, real signatures and the real link and CLA flows, through
the claim world of the P2 tests. Time moves through the one clock the claim
engine, the reviews and the public reads share.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ditto.api_models.bounty import (
    bounty_appeal_message,
    bounty_reason_digest,
    bounty_submit_message,
    bounty_withdraw_message,
)
from ditto.api_server import bounty_claims
from ditto.api_server.bounty_deadlines import BountyDeadlineSweeper
from ditto.db.models import (
    Bounty,
    BountyActionNonce,
    BountyAppeal,
    BountyClaim,
    BountyEvent,
    BountyObligation,
    BountySlot,
    BountySlotAction,
    BountySubmission,
    BountyWithdrawal,
)
from ditto.tests.api_server.endpoints.test_bounties import publish as publish_bounty
from ditto.tests.api_server.endpoints.test_bounty_claims import (
    _ADMIN_TOKEN,
    _HEADERS,
    ALICE,
    BOB,
    CAROL,
    CLAIMS,
    ISSUE,
    REPO,
    Person,
    World,
)

pytestmark = pytest.mark.asyncio
REVIEWER = "reviewer-private@example.com"
AS_REVIEWER = {**_ADMIN_TOKEN, "X-Admin-Actor": REVIEWER}
SHA = "ab" * 20
ADMIN = "/api/v1/admin/bounties"


@pytest.fixture
async def world(app, client, session_maker):
    return World(app, client, session_maker)


def clock() -> datetime:
    return bounty_claims.utcnow()


def travel(monkeypatch, delta: timedelta) -> None:
    base = datetime.now(UTC) + delta
    monkeypatch.setattr(bounty_claims, "utcnow", lambda: base)


def proof(who: Person, payload: bytes, kind: str, signer=None) -> dict:
    key = signer or (who.coldkey if kind == "coldkey" else who.hotkey)
    return {
        "key_kind": kind,
        "signer": key.ss58_address,
        "signature": key.sign(payload).hex(),
    }


def submit_body(
    who: Person,
    claim: dict,
    *,
    pr: int = 7,
    sha: str = SHA,
    repo: str = REPO,
    kind: str = "hotkey",
    signer: bittensor.Keypair | None = None,
    nonce: UUID | None = None,
    netuid: int = 118,
) -> dict:
    nonce, issued = nonce or uuid4(), clock()
    key = signer or (who.coldkey if kind == "coldkey" else who.hotkey)
    payload = bounty_submit_message(
        netuid=netuid,
        claim_id=UUID(claim["claim_id"]),
        hotkey=who.hotkey.ss58_address,
        repo=repo,
        pr_number=pr,
        head_sha=sha,
        nonce=nonce,
        issued_at=issued,
        key_kind=kind,
        signer=key.ss58_address,
    )
    return {
        "netuid": netuid,
        "claim_id": claim["claim_id"],
        "hotkey": who.hotkey.ss58_address,
        "repo": repo,
        "pr_number": pr,
        "head_sha": sha,
        "nonce": str(nonce),
        "issued_at": issued.isoformat(),
        "proof": proof(who, payload, kind, signer),
    }


async def submit(world, who: Person, claim: dict, **kwargs):
    return await world.client.post(
        f"{CLAIMS}/{claim['claim_id']}/submissions",
        json=submit_body(who, claim, **kwargs),
    )


async def withdraw(world, who: Person, claim: dict):
    nonce, issued = uuid4(), clock()
    payload = bounty_withdraw_message(
        netuid=118,
        claim_id=UUID(claim["claim_id"]),
        hotkey=who.hotkey.ss58_address,
        nonce=nonce,
        issued_at=issued,
        key_kind="hotkey",
        signer=who.hotkey.ss58_address,
    )
    return await world.client.post(
        f"{CLAIMS}/{claim['claim_id']}/withdraw",
        json={
            "netuid": 118,
            "claim_id": claim["claim_id"],
            "hotkey": who.hotkey.ss58_address,
            "nonce": str(nonce),
            "issued_at": issued.isoformat(),
            "proof": proof(who, payload, "hotkey"),
        },
    )


async def appeal(world, who: Person, claim: dict, event_id: int, reason: str):
    nonce, issued = uuid4(), clock()
    payload = bounty_appeal_message(
        netuid=118,
        claim_id=UUID(claim["claim_id"]),
        hotkey=who.hotkey.ss58_address,
        subject_event_id=event_id,
        reason_digest=bounty_reason_digest(reason),
        nonce=nonce,
        issued_at=issued,
        key_kind="hotkey",
        signer=who.hotkey.ss58_address,
    )
    return await world.client.post(
        f"{CLAIMS}/{claim['claim_id']}/appeals",
        json={
            "netuid": 118,
            "claim_id": claim["claim_id"],
            "hotkey": who.hotkey.ss58_address,
            "subject_event_id": event_id,
            "reason": reason,
            "nonce": str(nonce),
            "issued_at": issued.isoformat(),
            "proof": proof(who, payload, "hotkey"),
        },
    )


async def claimed(world, who: Person) -> dict:
    held = await world.hold(who, issued=clock())
    assert held.status_code == 200, held.text
    paid = await world.confirm(held.json())
    assert paid.json()["state"] == "claimed", paid.text
    return paid.json()


VERBS = {
    "accept": "ACCEPT",
    "changes_requested": "REQUEST CHANGES ON",
    "reject": "REJECT",
}


async def review(
    world, claim: dict, decision: str, *, headers=AS_REVIEWER, submission=None, **extra
):
    submission = submission or claim["submissions"][-1]["submission_id"]
    body = {
        "claim_id": claim["claim_id"],
        "submission_id": submission,
        "decision": decision,
        "reason": f"fixture {decision} decision",
        "confirmation": (
            f"{VERBS[decision]} BOUNTY CLAIM {REPO}#{ISSUE} {claim['claim_id']}"
        ),
        **({"rework_days": 3} if decision == "changes_requested" else {}),
        **extra,
    }
    return await world.client.post(
        f"{ADMIN}/claims/{claim['claim_id']}/review", headers=headers, json=body
    )


async def events(world) -> list[dict]:
    return (await world.refresh())["events"]


def slot_of(detail: dict, claim: dict) -> dict:
    return next(s for s in detail["slots"] if s["slot_id"] == claim["slot_id"])


async def test_a_submission_records_the_exact_commit_and_starts_review(world):
    await world.start(ALICE)
    claim = await claimed(world, ALICE)
    body = submit_body(ALICE, claim)
    sent = await world.client.post(
        f"{CLAIMS}/{claim['claim_id']}/submissions", json=body
    )
    assert sent.status_code == 200, sent.text
    view = sent.json()
    assert view["slot_status"] == "submitted"
    [entry] = view["submissions"]
    assert (entry["pr_number"], entry["head_sha"], entry["decision"]) == (7, SHA, None)
    submitted = datetime.fromisoformat(entry["submitted_at"])
    due = datetime.fromisoformat(entry["review_due_at"])
    assert due - submitted == timedelta(days=7)
    again = await world.client.post(
        f"{CLAIMS}/{claim['claim_id']}/submissions", json=body
    )
    assert again.status_code == 200 and again.json() == view
    reused = await submit(world, ALICE, claim, sha="cd" * 20, nonce=UUID(body["nonce"]))
    assert reused.status_code == 409 and "nonce" in reused.text
    # The same nonce and proof with any other signed field is not a retry.
    for field, value in (
        ("repo", "other-org/other-repo"),
        ("hotkey", BOB.hotkey.ss58_address),
        ("netuid", 119),
    ):
        altered = await world.client.post(
            f"{CLAIMS}/{claim['claim_id']}/submissions", json={**body, field: value}
        )
        assert altered.status_code == 409 and "nonce" in altered.text, field
    detail = await world.refresh()
    slot = slot_of(detail, claim)
    assert (
        slot["status"] == "submitted"
        and slot["review_due_at"] == entry["review_due_at"]
    )
    assert slot["review_overdue"] is False
    assert detail["events"][-1]["kind"] == "submitted"


@pytest.mark.parametrize(
    "case,status,message",
    [
        ("stranger", 403, "only the claimant"),
        ("coldkey", 403, "payee of record"),
        ("repo", 400, "bounty repository"),
        ("netuid", 400, "netuid"),
        ("signature", 400, "did not verify"),
        ("route", 400, "does not match the route"),
    ],
)
async def test_refused_submissions_write_nothing(world, case, status, message):
    await world.start(ALICE, BOB)
    claim = await claimed(world, ALICE)
    kwargs: dict = {}
    who = ALICE
    if case == "stranger":
        who = BOB
    elif case == "coldkey":
        kwargs = {"kind": "coldkey", "signer": BOB.coldkey}
    elif case == "repo":
        kwargs = {"repo": "ditto-assistant/other"}
    elif case == "netuid":
        kwargs = {"netuid": 119}
    body = submit_body(who, claim, **kwargs)
    route = claim["claim_id"]
    if case == "signature":
        body["proof"]["signature"] = "00" * 64
    if case == "route":
        route = str(uuid4())
    refused = await world.client.post(f"{CLAIMS}/{route}/submissions", json=body)
    assert refused.status_code == status, refused.text
    assert message in refused.json()["message"]
    assert slot_of(await world.refresh(), claim)["status"] == "claimed"


async def test_an_unpaid_hold_cannot_submit(world):
    await world.start(ALICE)
    held = (await world.hold(ALICE)).json()
    refused = await submit(world, ALICE, held)
    assert refused.status_code == 409 and "held" in refused.text


async def test_a_missed_delivery_deadline_expires_at_the_deadline(world, monkeypatch):
    await world.start(ALICE)
    claim = await claimed(world, ALICE)
    deadline = claim["deadline_at"]
    travel(monkeypatch, timedelta(days=5, minutes=1))
    # Reads show it at once, stamped at the deadline, without writing.
    detail = await world.refresh()
    slot = slot_of(detail, claim)
    assert (slot["status"], slot["status_changed_at"]) == ("expired", deadline)
    async with world.maker() as session:
        stored = await session.get(BountySlot, UUID(claim["slot_id"]))
        assert stored.status == "claimed"
    late = await submit(world, ALICE, claim)
    assert late.status_code == 409 and "expired" in late.text
    missed = [e for e in await events(world) if e["kind"] == "deadline_missed"]
    assert [e["recorded_at"] for e in missed] == [deadline]


async def test_the_sweeper_records_deadlines_once(world, monkeypatch):
    await world.start(ALICE, BOB)
    late = await claimed(world, ALICE)
    waiting = await claimed(world, BOB)
    assert (await submit(world, BOB, waiting)).status_code == 200
    sweeper = BountyDeadlineSweeper(session_maker=world.maker)
    later = datetime.now(UTC) + timedelta(days=8)
    assert await sweeper.sweep(now=later) == 1
    # A second sweep, or a restart, records nothing new.
    assert await sweeper.sweep(now=later) == 0
    kinds = [e["kind"] for e in await events(world)]
    assert kinds.count("deadline_missed") == 1 and kinds.count("review_overdue") == 1
    detail = await world.refresh()
    assert slot_of(detail, late)["status"] == "expired"
    overdue = slot_of(detail, waiting)
    # An overdue review never forfeits: the slot stays submitted.
    assert overdue["status"] == "submitted"
    travel(monkeypatch, timedelta(days=8))
    detail = await world.refresh()
    assert slot_of(detail, waiting)["review_overdue"] is True
    assert detail["summary"]["reviews_overdue"] == 1


async def test_a_closed_sweeper_can_start_again(world):
    sweeper = BountyDeadlineSweeper(session_maker=world.maker, interval_seconds=60)
    await sweeper.start()
    await sweeper.aclose()
    await sweeper.start()
    # A fresh stop signal: the restarted loop is running, not already stopped.
    await asyncio.sleep(0)
    assert sweeper._task is not None and not sweeper._task.done()
    await sweeper.aclose()
    assert sweeper._task is None


async def test_closing_never_waits_on_a_blocked_sweep(world, monkeypatch):
    from ditto.api_server import bounty_deadlines

    blocked = asyncio.Event()

    async def due(_session, **_kwargs):
        return [uuid4(), uuid4()]

    async def stuck(_maker, _bounty_id, **_kwargs):
        blocked.set()
        await asyncio.Event().wait()  # a row lock that is never released

    monkeypatch.setattr(bounty_deadlines, "due_bounties", due)
    monkeypatch.setattr(bounty_deadlines, "settle_deadlines", stuck)
    sweeper = BountyDeadlineSweeper(session_maker=world.maker, interval_seconds=60)
    await sweeper.start()
    await asyncio.wait_for(blocked.wait(), timeout=5)
    # The blocked sweep is cut short after the grace period.
    await asyncio.wait_for(sweeper.aclose(grace_seconds=0.1), timeout=5)
    assert sweeper._task is None


async def test_a_cancelled_close_still_ends_the_sweep(world, monkeypatch):
    from ditto.api_server import bounty_deadlines

    blocked = asyncio.Event()

    async def due(_session, **_kwargs):
        return [uuid4()]

    async def stuck(_maker, _bounty_id, **_kwargs):
        blocked.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bounty_deadlines, "due_bounties", due)
    monkeypatch.setattr(bounty_deadlines, "settle_deadlines", stuck)
    sweeper = BountyDeadlineSweeper(session_maker=world.maker, interval_seconds=60)
    await sweeper.start()
    running = sweeper._task
    assert running is not None
    await asyncio.wait_for(blocked.wait(), timeout=5)
    closing = asyncio.create_task(sweeper.aclose(grace_seconds=60))
    await asyncio.sleep(0.05)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    # The caller's cancellation ends the sweep too: a restart runs one loop.
    assert running.done()


async def test_one_failing_bounty_never_blocks_the_sweep(world, monkeypatch):
    from ditto.api_server import bounty_deadlines

    await world.start(ALICE)
    late = await claimed(world, ALICE)
    broken = uuid4()
    real_due, real_settle = (
        bounty_deadlines.due_bounties,
        bounty_deadlines.settle_deadlines,
    )

    async def a_broken_bounty_first(session, *, now):
        return [broken, *await real_due(session, now=now)]

    async def settle(maker, bounty_id, *, now):
        if bounty_id == broken:
            raise RuntimeError("fixture: this bounty cannot settle")
        await real_settle(maker, bounty_id, now=now)

    monkeypatch.setattr(bounty_deadlines, "due_bounties", a_broken_bounty_first)
    monkeypatch.setattr(bounty_deadlines, "settle_deadlines", settle)
    sweeper = BountyDeadlineSweeper(session_maker=world.maker)
    later = datetime.now(UTC) + timedelta(days=6)
    assert await sweeper.sweep(now=later) == 1
    assert slot_of(await world.refresh(), late)["status"] == "expired"
    assert [e["kind"] for e in await events(world)].count("deadline_missed") == 1


async def test_review_decisions_by_the_assigned_reviewer(world):
    await world.start(ALICE, BOB, CAROL)
    accepted = await claimed(world, ALICE)
    accepted = (await submit(world, ALICE, accepted)).json()
    wrong_actor = await review(world, accepted, "accept", headers=_HEADERS)
    assert wrong_actor.status_code == 403 and "assigned reviewer" in wrong_actor.text
    stale = await review(world, accepted, "accept", submission=str(uuid4()))
    assert stale.status_code == 409 and "no longer under review" in stale.text
    unconfirmed = await review(world, accepted, "accept", confirmation="ACCEPT")
    assert unconfirmed.status_code == 409 and "ACCEPT BOUNTY CLAIM" in unconfirmed.text
    done = await review(world, accepted, "accept", attribution="Fixture credit to Bob")
    assert done.status_code == 200, done.text
    view = done.json()
    assert view["slot_status"] == "accepted"
    assert view["submissions"][-1]["decision"] == "accept"
    assert view["submissions"][-1]["attribution"] == "Fixture credit to Bob"
    assert view["submissions"][-1]["decision_reason"] == "fixture accept decision"
    again = await review(world, accepted, "accept", attribution="Fixture credit to Bob")
    assert again.status_code == 200 and again.json() == view
    # A decided claim has no delivery deadline left to meet.
    assert view["deadline_at"] is None
    assert slot_of(await world.refresh(), accepted)["deadline_at"] is None
    changed = await review(world, accepted, "reject")
    assert changed.status_code == 409
    # A changed credit is a different decision, not a retry.
    recredited = await review(
        world, accepted, "accept", attribution="Fixture credit to Carol"
    )
    assert recredited.status_code == 409 and "already decided" in recredited.text
    # The deciding operator stays private.
    assert REVIEWER not in str(await world.refresh())

    rejected = (await submit(world, BOB, await claimed(world, BOB))).json()
    decided = (await review(world, rejected, "reject")).json()
    assert (decided["slot_status"], decided["deadline_at"]) == ("rejected", None)
    assert slot_of(await world.refresh(), rejected)["deadline_at"] is None
    # Malformed decisions are refused before anything is written.
    third = (await submit(world, CAROL, await claimed(world, CAROL))).json()
    for decision, extra in (
        ("accept", {"rework_days": 2}),
        ("reject", {"attribution": "credit"}),
    ):
        refused = await review(world, third, decision, **extra)
        assert refused.status_code == 400, refused.text
    assert slot_of(await world.refresh(), third)["status"] == "submitted"


async def test_rework_has_a_recorded_deadline_and_a_new_commit(world, monkeypatch):
    await world.start(ALICE)
    claim = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    rework = (await review(world, claim, "changes_requested")).json()
    assert rework["slot_status"] == "rework"
    deadline = rework["submissions"][-1]["rework_deadline_at"]
    decided = datetime.fromisoformat(rework["submissions"][-1]["decided_at"])
    assert datetime.fromisoformat(deadline) - decided == timedelta(days=3)
    # A later exact retry still matches; a different rework window does not.
    travel(monkeypatch, timedelta(hours=1))
    retried = await review(world, claim, "changes_requested")
    assert retried.status_code == 200, retried.text
    assert retried.json()["submissions"] == rework["submissions"]
    longer = await review(world, claim, "changes_requested", rework_days=5)
    assert longer.status_code == 409 and "already decided" in longer.text
    second = await submit(world, ALICE, rework, sha="cd" * 20)
    assert second.status_code == 200, second.text
    assert [s["head_sha"] for s in second.json()["submissions"]] == [SHA, "cd" * 20]
    # Another round of changes, then the rework deadline passes unanswered.
    again = (await review(world, second.json(), "changes_requested")).json()
    # Past the second rework deadline (decided an hour in, above).
    travel(monkeypatch, timedelta(days=3, hours=1, minutes=1))
    late = await submit(world, ALICE, again, sha="ef" * 20)
    assert late.status_code == 409 and "expired" in late.text
    missed = [e for e in await events(world) if e["kind"] == "deadline_missed"]
    assert [e["recorded_at"] for e in missed] == [
        again["submissions"][-1]["rework_deadline_at"]
    ]


async def test_a_resubmission_at_the_same_instant_is_the_one_under_review(
    world, monkeypatch
):
    await world.start(ALICE)
    frozen = datetime.now(UTC)
    monkeypatch.setattr(bounty_claims, "utcnow", lambda: frozen)
    claim = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    assert (await review(world, claim, "changes_requested")).status_code == 200
    again = (await submit(world, ALICE, claim, sha="cd" * 20)).json()
    first, second = again["submissions"]
    assert first["submitted_at"] == second["submitted_at"]
    # Even on a tied time the view lists the decided submission first and the
    # one under review last, which is the one a reviewer decides.
    assert (first["decision"], second["decision"]) == ("changes_requested", None)
    assert second["head_sha"] == "cd" * 20
    decided = await review(world, again, "accept")
    assert decided.status_code == 200, decided.text
    # With every time frozen, look the decided submission up by its id.
    by_id = {s["submission_id"]: s for s in decided.json()["submissions"]}
    assert by_id[second["submission_id"]]["decision"] == "accept"
    assert by_id[first["submission_id"]]["decision"] == "changes_requested"


async def test_withdrawal_retains_the_fee_and_is_final(world):
    await world.start(ALICE)
    claim = await claimed(world, ALICE)
    gone = await withdraw(world, ALICE, claim)
    assert gone.status_code == 200 and gone.json()["slot_status"] == "withdrawn"
    body = json.loads(gone.request.content)
    for field, value in (("hotkey", BOB.hotkey.ss58_address), ("netuid", 119)):
        altered = await world.client.post(
            f"{CLAIMS}/{claim['claim_id']}/withdraw", json={**body, field: value}
        )
        assert altered.status_code == 409 and "nonce" in altered.text, field
    exact = await world.client.post(f"{CLAIMS}/{claim['claim_id']}/withdraw", json=body)
    assert exact.status_code == 200 and exact.json() == gone.json()
    assert gone.json()["refunds"] == []
    again = await withdraw(world, ALICE, claim)
    assert again.status_code == 409 and "withdrawn" in again.text


async def test_a_submitted_slot_cannot_be_withdrawn(world):
    await world.start(ALICE)
    claim = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    refused = await withdraw(world, ALICE, claim)
    assert refused.status_code == 409 and "submitted" in refused.text


async def test_appeals_are_decided_by_someone_else(world):
    await world.start(ALICE)
    claim = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    await review(world, claim, "reject")
    rejection = next(e for e in await events(world) if e["kind"] == "rejected")
    wrong = next(e for e in await events(world) if e["kind"] == "submitted")
    refused = await appeal(world, ALICE, claim, wrong["event_id"], "Fixture appeal.")
    assert refused.status_code == 409 and "can be appealed" in refused.text
    filed = await appeal(world, ALICE, claim, rejection["event_id"], "Fixture appeal.")
    assert filed.status_code == 200, filed.text
    [entry] = filed.json()["appeals"]
    assert entry["reason_digest"] == bounty_reason_digest("Fixture appeal.")
    assert entry["upheld"] is None
    twice = await appeal(world, ALICE, claim, rejection["event_id"], "Fixture again.")
    assert twice.status_code == 409 and "already appealed" in twice.text
    # The appeal text stays private; only its digest is public.
    assert "Fixture appeal." not in str(await world.refresh())
    route = f"{ADMIN}/appeals/{entry['appeal_id']}/decision"
    body = {
        "upheld": True,
        "reason": "fixture appeal review",
        "restore_days": 2,
        "confirmation": f"UPHOLD BOUNTY APPEAL {entry['appeal_id']}",
    }
    conflicted = await world.client.post(route, headers=AS_REVIEWER, json=body)
    assert (
        conflicted.status_code == 403 and "other than its reviewer" in conflicted.text
    )
    # Reopening is refused while the appeal is pending.
    reopen = await world.client.post(
        f"{ADMIN}/slots/{claim['slot_id']}/reopen",
        headers=_HEADERS,
        json={
            "reason": "fixture reopen attempt",
            "confirmation": f"REOPEN BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 SLOT 1",
        },
    )
    assert reopen.status_code == 409 and "pending appeal" in reopen.text
    upheld = await world.client.post(route, headers=_HEADERS, json=body)
    assert upheld.status_code == 200, upheld.text
    restored = upheld.json()
    again = await world.client.post(route, headers=_HEADERS, json=body)
    assert again.status_code == 200 and again.json() == restored
    # The excluded reviewer, or a different restored deadline, is not a retry.
    for headers, changed in (
        (AS_REVIEWER, body),
        (_HEADERS, {**body, "restore_days": 3}),
    ):
        refused = await world.client.post(route, headers=headers, json=changed)
        assert refused.status_code == 409 and "already decided" in refused.text
    assert restored["slot_status"] == "claimed" and restored["appeals"][0]["upheld"]
    detail = await world.refresh()
    restored_slot = slot_of(detail, claim)
    decided = next(e for e in detail["events"] if e["kind"] == "appeal_upheld")
    assert datetime.fromisoformat(
        restored_slot["deadline_at"]
    ) - datetime.fromisoformat(decided["recorded_at"]) == timedelta(days=2)
    # Operators see the deadline the decision restored, so a retry can be told
    # apart from a different decision.
    bounty_id = detail["summary"]["bounty_id"]
    view = (await world.client.get(f"{ADMIN}/{bounty_id}", headers=_HEADERS)).json()
    [admin_appeal] = view["appeals"]
    assert admin_appeal["restored_deadline_at"] == restored_slot["deadline_at"]
    # The restored claim delivers again, then its new commit is reviewed.
    assert (await submit(world, ALICE, restored, sha="cd" * 20)).status_code == 200


async def test_an_appeal_without_a_recorded_review_is_refused(world):
    await world.start(ALICE)
    claim = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    await review(world, claim, "reject")
    rejection = next(e for e in await events(world) if e["kind"] == "rejected")
    filed = await appeal(world, ALICE, claim, rejection["event_id"], "Fixture appeal.")
    [entry] = filed.json()["appeals"]
    # A review row that is somehow missing leaves the reviewer unknown.
    async with world.maker() as session, session.begin():
        await session.execute(
            text("ALTER TABLE bounty_reviews DISABLE TRIGGER bounty_reviews_immutable")
        )
        await session.execute(text("DELETE FROM bounty_reviews"))
        await session.execute(
            text("ALTER TABLE bounty_reviews ENABLE TRIGGER bounty_reviews_immutable")
        )
    refused = await world.client.post(
        f"{ADMIN}/appeals/{entry['appeal_id']}/decision",
        headers=_HEADERS,
        json={
            "upheld": False,
            "reason": "fixture appeal review",
            "confirmation": f"DENY BOUNTY APPEAL {entry['appeal_id']}",
        },
    )
    assert refused.status_code == 409 and "no recorded review" in refused.text


async def test_a_missed_deadline_can_be_appealed_and_denied(world, monkeypatch):
    await world.start(ALICE)
    claim = await claimed(world, ALICE)
    travel(monkeypatch, timedelta(days=5, minutes=1))
    assert (await submit(world, ALICE, claim)).status_code == 409
    missed = next(e for e in await events(world) if e["kind"] == "deadline_missed")
    # A signed but blank statement is no appeal.
    blank = await appeal(world, ALICE, claim, missed["event_id"], " " * 12)
    assert blank.status_code == 400 and "must not be blank" in blank.text
    filed = (await appeal(world, ALICE, claim, missed["event_id"], "Outage.")).json()
    appeal_id = filed["appeals"][0]["appeal_id"]
    route = f"{ADMIN}/appeals/{appeal_id}/decision"
    missing_days = await world.client.post(
        route,
        headers=_HEADERS,
        json={
            "upheld": True,
            "reason": "fixture appeal review",
            "confirmation": f"UPHOLD BOUNTY APPEAL {appeal_id}",
        },
    )
    assert missing_days.status_code == 400
    denied = await world.client.post(
        route,
        headers=_HEADERS,
        json={
            "upheld": False,
            "reason": "fixture appeal review",
            "confirmation": f"DENY BOUNTY APPEAL {appeal_id}",
        },
    )
    assert denied.status_code == 200 and denied.json()["slot_status"] == "expired"
    assert denied.json()["appeals"][0]["upheld"] is False


async def test_a_missed_rework_deadline_is_appealed_to_someone_else(world, monkeypatch):
    await world.start(ALICE)
    claim = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    assert (await review(world, claim, "changes_requested")).status_code == 200
    travel(monkeypatch, timedelta(days=3, minutes=1))
    # A late resubmission records the missed rework deadline.
    late = await submit(world, ALICE, claim, sha="cd" * 20)
    assert late.status_code == 409
    missed = next(e for e in await events(world) if e["kind"] == "deadline_missed")
    filed = (await appeal(world, ALICE, claim, missed["event_id"], "Outage.")).json()
    appeal_id = filed["appeals"][0]["appeal_id"]
    body = {
        "upheld": False,
        "reason": "fixture appeal review",
        "confirmation": f"DENY BOUNTY APPEAL {appeal_id}",
    }
    route = f"{ADMIN}/appeals/{appeal_id}/decision"
    # The reviewer who set the rework deadline does not judge missing it.
    conflicted = await world.client.post(route, headers=AS_REVIEWER, json=body)
    assert (
        conflicted.status_code == 403 and "other than its reviewer" in conflicted.text
    )
    denied = await world.client.post(route, headers=_HEADERS, json=body)
    assert denied.status_code == 200, denied.text


async def test_a_missed_restored_deadline_is_appealed_to_someone_else(
    world, monkeypatch
):
    await world.start(ALICE)
    claim = await claimed(world, ALICE)
    travel(monkeypatch, timedelta(days=5, minutes=1))
    assert (await submit(world, ALICE, claim)).status_code == 409
    first = next(e for e in await events(world) if e["kind"] == "deadline_missed")
    filed = (await appeal(world, ALICE, claim, first["event_id"], "Outage.")).json()
    upheld = await world.client.post(
        f"{ADMIN}/appeals/{filed['appeals'][0]['appeal_id']}/decision",
        headers=_HEADERS,
        json={
            "upheld": True,
            "restore_days": 2,
            "reason": "fixture appeal review",
            "confirmation": f"UPHOLD BOUNTY APPEAL {filed['appeals'][0]['appeal_id']}",
        },
    )
    assert upheld.status_code == 200 and upheld.json()["slot_status"] == "claimed"
    travel(monkeypatch, timedelta(days=7, minutes=2))
    assert (await submit(world, ALICE, claim)).status_code == 409
    second = next(
        e
        for e in reversed(await events(world))
        if e["kind"] == "deadline_missed" and e["event_id"] != first["event_id"]
    )
    filed = (await appeal(world, ALICE, claim, second["event_id"], "Again.")).json()
    appeal_id = next(a["appeal_id"] for a in filed["appeals"] if a["upheld"] is None)
    body = {
        "upheld": False,
        "reason": "fixture appeal review",
        "confirmation": f"DENY BOUNTY APPEAL {appeal_id}",
    }
    route = f"{ADMIN}/appeals/{appeal_id}/decision"
    # The operator who restored the deadline does not judge missing it.
    conflicted = await world.client.post(route, headers=_HEADERS, json=body)
    assert (
        conflicted.status_code == 403 and "other than its reviewer" in conflicted.text
    )
    denied = await world.client.post(route, headers=AS_REVIEWER, json=body)
    assert denied.status_code == 200, denied.text


async def test_cancellation_refunds_a_paid_fee(world):
    await world.start(ALICE, BOB)
    claim = await claimed(world, ALICE)
    held = (await world.hold(BOB)).json()

    async def cancel(slot: dict, kind: str, position: int):
        return await world.client.post(
            f"{ADMIN}/slots/{slot['slot_id']}/cancel",
            headers=_HEADERS,
            json={
                "kind": kind,
                "reason": "fixture cancellation",
                "confirmation": f"CANCEL BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 SLOT "
                f"{position}",
            },
        )

    pending = await cancel(held, "team", held["position"])
    assert pending.status_code == 409 and "held" in pending.text
    done = await cancel(claim, "system_fault", claim["position"])
    assert done.status_code == 200, done.text
    view = (await world.client.get(f"{CLAIMS}/{claim['claim_id']}")).json()
    assert view["slot_status"] == "cancelled"
    assert [r["reason"] for r in view["refunds"]] == ["system_fault"]
    open_slot = next(
        s for s in (await world.refresh())["slots"] if s["status"] == "open"
    )
    assert (await cancel(open_slot, "team", open_slot["position"])).status_code == 200
    async with world.maker() as session:
        actions = (
            await session.scalars(
                select(BountySlotAction).order_by(BountySlotAction.action_id)
            )
        ).all()
    assert [a.prior_status for a in actions] == ["claimed", "open"]
    async with world.maker() as session:
        refunds = int(
            await session.scalar(select(func.count()).select_from(BountyObligation))
        )
    assert refunds == 1
    # Reopening the cancelled paid slot keeps its refund as the fee's only
    # disposition: a cancellation is never a retention.
    reopened = await world.client.post(
        f"{ADMIN}/slots/{claim['slot_id']}/reopen",
        headers=_HEADERS,
        json={
            "reason": "fixture reopen after cancellation",
            "confirmation": f"REOPEN BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 SLOT "
            f"{claim['position']}",
        },
    )
    assert reopened.status_code == 200, reopened.text
    after = (await world.client.get(f"{CLAIMS}/{claim['claim_id']}")).json()
    assert [o["kind"] for o in after["obligations"]] == ["fee_refund"]


async def test_reopening_is_explicit_and_admits_a_new_claim(world):
    await world.start(ALICE, BOB)
    claim = await claimed(world, ALICE)
    route = f"{ADMIN}/slots/{claim['slot_id']}/reopen"
    body = {
        "reason": "fixture reopen",
        "confirmation": f"REOPEN BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 SLOT 1",
    }
    live = await world.client.post(route, headers=_HEADERS, json=body)
    assert live.status_code == 409 and "claimed" in live.text
    assert (await withdraw(world, ALICE, claim)).status_code == 200
    reopened = await world.client.post(route, headers=_HEADERS, json=body)
    assert reopened.status_code == 200, reopened.text
    slot = slot_of(await world.refresh(), claim)
    assert (slot["status"], slot["claimant"]) == ("open", None)
    taken = (await world.hold(BOB)).json()
    assert taken["slot_id"] == claim["slot_id"]
    # Bob's hold is his own; Alice's claim keeps the outcome it was reopened
    # from.
    mine = (await world.client.get(f"{CLAIMS}/{claim['claim_id']}")).json()
    assert (mine["state"], mine["slot_status"]) == ("claimed", "withdrawn")
    async with world.maker() as session:
        actions = (await session.scalars(select(BountySlotAction))).all()
    assert [(a.action, a.prior_status) for a in actions] == [("reopen", "withdrawn")]


async def test_reassignment_moves_review_authority_and_extends_overdue(
    world, monkeypatch
):
    await world.start(ALICE)
    claim = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    bounty_id = world.detail["summary"]["bounty_id"]
    route = f"{ADMIN}/{bounty_id}/reviewer"
    body = {
        "reviewer_actor": "second-reviewer@example.com",
        "reason": "fixture reviewer is away",
        "confirmation": f"REASSIGN BOUNTY REVIEWER {REPO}#{ISSUE}",
    }
    travel(monkeypatch, timedelta(days=8))
    moved = await world.client.post(route, headers=_HEADERS, json=body)
    assert moved.status_code == 200, moved.text
    assert moved.json()["reviews_overdue"] == 0
    same = await world.client.post(route, headers=_HEADERS, json=body)
    assert same.status_code == 409 and "already the reviewer" in same.text
    old = await review(world, claim, "accept")
    assert old.status_code == 403
    new = await review(
        world,
        claim,
        "accept",
        headers={**_ADMIN_TOKEN, "X-Admin-Actor": "second-reviewer@example.com"},
    )
    assert new.status_code == 200, new.text
    kinds = [e["kind"] for e in await events(world)]
    assert kinds.count("review_overdue") == 1 and "reviewer_reassigned" in kinds
    # Moving the review back never lets another reviewer claim the decision.
    back = await world.client.post(
        route, headers=_HEADERS, json={**body, "reviewer_actor": REVIEWER}
    )
    assert back.status_code == 200, back.text
    repeated = await review(world, claim, "accept")
    assert repeated.status_code == 409 and "already decided" in repeated.text


async def test_each_nonce_is_the_record_of_exactly_one_signed_action(world):
    # The nonce table is no replay cache to prune: each row is the nonce of
    # the one action it authorized, which keeps it by foreign key, and a
    # refused action writes none. It grows only with recorded actions.
    await world.start(ALICE, BOB, CAROL)
    alice = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    await review(world, alice, "reject")
    rejection = next(e for e in await events(world) if e["kind"] == "rejected")
    filed = await appeal(world, ALICE, alice, rejection["event_id"], "Fixture.")
    assert filed.status_code == 200, filed.text
    bob = await claimed(world, BOB)
    assert (await withdraw(world, BOB, bob)).status_code == 200
    assert (await world.hold(CAROL)).status_code == 200
    late = await submit(world, BOB, bob)
    assert late.status_code == 409, late.text
    tables = {
        "claim": BountyClaim,
        "submit": BountySubmission,
        "withdraw": BountyWithdrawal,
        "appeal": BountyAppeal,
    }
    async with world.maker() as session:
        nonces = (await session.execute(select(BountyActionNonce))).scalars().all()
        used = {
            action: set(await session.scalars(select(model.nonce)))
            for action, model in tables.items()
        }
    assert sorted(n.action for n in nonces) == sorted(
        ["claim", "claim", "claim", "submit", "withdraw", "appeal"]
    )
    for nonce in nonces:
        owners = [action for action, found in used.items() if nonce.nonce in found]
        assert owners == [nonce.action]
    assert len(nonces) == sum(len(found) for found in used.values())


async def test_review_history_is_append_only(world):
    await world.start(ALICE)
    claim = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    await review(world, claim, "reject")
    await appeal(
        world,
        ALICE,
        claim,
        next(e for e in await events(world) if e["kind"] == "rejected")["event_id"],
        "Fixture appeal.",
    )
    history = "bounty history is append-only"
    for statement in (
        "UPDATE bounty_submissions SET head_sha = repeat('0', 40)",
        "DELETE FROM bounty_reviews",
        "UPDATE bounty_appeals SET reason = 'rewritten'",
        "TRUNCATE bounty_withdrawals",
        "TRUNCATE bounty_slot_actions",
        "TRUNCATE bounty_reviewer_assignments",
        "TRUNCATE bounty_appeal_decisions",
    ):
        async with world.maker() as session:
            with pytest.raises(DBAPIError, match=history):
                await session.execute(text(statement))
            await session.rollback()
    async with world.maker() as session:
        with pytest.raises(DBAPIError, match="ck_bounty_slots_claim"):
            await session.execute(
                text(
                    "UPDATE bounty_slots SET status = 'submitted', "
                    "review_due_at = NULL WHERE position = 1"
                )
            )
        await session.rollback()
    async with world.maker() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BountyEvent)
                .where(BountyEvent.kind == "appeal_filed")
            )
            == 1
        )


async def test_history_references_stay_within_their_bounty_and_claim(world):
    """The database refuses an event naming another bounty's revision, round,
    slot or claim, a slot held by another bounty's claim, and an appeal of
    another claim's decision."""
    await world.start(ALICE, BOB)
    alice = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    bob = (await submit(world, BOB, await claimed(world, BOB))).json()
    await review(world, bob, "reject")
    published = await publish_bounty(world.client, issue=ISSUE + 1, terms={})
    assert published.status_code == 200, published.text
    async with world.maker() as session:
        other = await session.scalar(
            select(Bounty.bounty_id).where(Bounty.issue_number == ISSUE + 1)
        )
        open_slot = await session.scalar(
            select(BountySlot.slot_id).where(BountySlot.bounty_id == other).limit(1)
        )
        rejection = await session.scalar(
            select(BountyEvent.event_id).where(
                BountyEvent.claim_id == UUID(bob["claim_id"]),
                BountyEvent.kind == "rejected",
            )
        )
    # A slot cancellation may name no claim, so only the key under test refuses.
    event = text(
        "INSERT INTO bounty_events (bounty_id, kind, revision, round, slot_id, "
        "claim_id, recorded_at) VALUES (:bounty, 'slot_cancelled', :revision, "
        ":round, :slot, :claim, now())"
    )
    nothing = {"bounty": other, "revision": None, "round": None, "slot": None}
    cases = [
        (
            event,
            {**nothing, "revision": 2, "claim": None},
            "fk_bounty_events_bounty_id_bounty_revisions",
        ),
        (
            event,
            {**nothing, "round": 2, "claim": None},
            "fk_bounty_events_bounty_id_bounty_rounds",
        ),
        (
            event,
            {**nothing, "slot": UUID(alice["slot_id"]), "claim": None},
            "fk_bounty_events_slot_id_bounty_slots",
        ),
        (
            event,
            {**nothing, "claim": UUID(alice["claim_id"])},
            "fk_bounty_events_claim_id_bounty_claims",
        ),
        (
            text(
                "UPDATE bounty_slots SET status = 'held', claim_id = :claim "
                "WHERE slot_id = :slot"
            ),
            {"slot": open_slot, "claim": UUID(alice["claim_id"])},
            "fk_bounty_slots_claim_id_bounty_claims",
        ),
        (
            text(
                "WITH nonce AS (INSERT INTO bounty_action_nonces (nonce, action, "
                "recorded_at) VALUES (gen_random_uuid(), 'appeal', now()) "
                "RETURNING nonce) INSERT INTO bounty_appeals (appeal_id, claim_id, "
                "subject_event_id, reason, reason_digest, nonce, issued_at, "
                "key_kind, signer, signature, filed_at) SELECT gen_random_uuid(), "
                ":claim, :event, 'fixture', repeat('a', 64), nonce, now(), "
                "'hotkey', 'signer', 'signature', now() FROM nonce"
            ),
            {"claim": UUID(alice["claim_id"]), "event": rejection},
            "fk_bounty_appeals_subject_event_id_bounty_events",
        ),
    ]
    for statement, values, constraint in cases:
        async with world.maker() as session:
            with pytest.raises(DBAPIError, match=constraint):
                await session.execute(statement, values)
            await session.rollback()

    # An event's submission, appeal and obligation belong to its own claim.
    accepted = (await review(world, alice, "accept")).json()
    filed = await appeal(world, BOB, bob, rejection, "Fixture appeal.")
    assert filed.status_code == 200, filed.text
    refund = next(o for o in accepted["obligations"] if o["kind"] == "fee_refund")
    # A kind that may name no claim, so only the reference under test refuses.
    linked = text(
        "INSERT INTO bounty_events (bounty_id, kind, claim_id, submission_id, "
        "appeal_id, obligation_id, recorded_at) VALUES (:bounty, 'slot_cancelled', "
        ":claim, :submission, :appeal, :obligation, now())"
    )
    none = {"submission": None, "appeal": None, "obligation": None}
    for values, constraint in (
        (
            {
                "claim": bob["claim_id"],
                **none,
                "submission": alice["submissions"][-1]["submission_id"],
            },
            "fk_bounty_events_claim_id_bounty_submissions",
        ),
        (
            {
                "claim": alice["claim_id"],
                **none,
                "appeal": filed.json()["appeals"][0]["appeal_id"],
            },
            "fk_bounty_events_claim_id_bounty_appeals",
        ),
        (
            {"claim": bob["claim_id"], **none, "obligation": refund["obligation_id"]},
            "fk_bounty_events_claim_id_bounty_obligations",
        ),
        # Without a claim the composite keys would not apply at all.
        (
            {
                "claim": None,
                **none,
                "submission": alice["submissions"][-1]["submission_id"],
            },
            "ck_bounty_events_review_refs",
        ),
        (
            {"claim": None, **none, "appeal": filed.json()["appeals"][0]["appeal_id"]},
            "ck_bounty_events_review_refs",
        ),
        (
            {"claim": None, **none, "obligation": refund["obligation_id"]},
            "ck_bounty_events_obligation_ref",
        ),
    ):
        async with world.maker() as session:
            with pytest.raises(DBAPIError, match=constraint):
                await session.execute(linked, {"bounty": alice["bounty_id"], **values})
            await session.rollback()


async def test_an_event_about_a_claim_names_it(world):
    await world.start(ALICE)
    insert = text(
        "INSERT INTO bounty_events (bounty_id, kind, recorded_at) "
        "VALUES (:bounty, :kind, now())"
    )
    bounty = world.detail["summary"]["bounty_id"]
    # Bounty-wide events, and cancelling or reopening an open slot, name none.
    async with world.maker() as session:
        for kind in ("claims_paused", "reviewer_reassigned", "slot_cancelled"):
            await session.execute(insert, {"bounty": bounty, "kind": kind})
        await session.rollback()
    for kind in ("slot_held", "submitted", "deadline_missed", "award_recorded"):
        async with world.maker() as session:
            with pytest.raises(DBAPIError, match="ck_bounty_events_claim_scope"):
                await session.execute(insert, {"bounty": bounty, "kind": kind})
            await session.rollback()


async def test_a_slot_action_names_only_the_claim_of_its_slot(world):
    await world.start(ALICE, BOB)
    alice = await claimed(world, ALICE)
    bob = await claimed(world, BOB)
    assert alice["slot_id"] != bob["slot_id"]
    # A cancellation of the slot in `slot`, recorded against Alice's claim.
    cancel = text(
        "INSERT INTO bounty_slot_actions (slot_id, claim_id, action, cancel_kind,"
        " prior_status, actor, reason, recorded_at) VALUES (:slot, :claim,"
        " 'cancel', 'team', 'claimed', 'fixture', 'fixture', now())"
    )
    async with world.maker() as session:
        with pytest.raises(DBAPIError, match="violates foreign key"):
            await session.execute(
                cancel, {"slot": bob["slot_id"], "claim": alice["claim_id"]}
            )
        await session.rollback()
    async with world.maker() as session, session.begin():
        await session.execute(
            cancel, {"slot": alice["slot_id"], "claim": alice["claim_id"]}
        )


async def test_review_routes_declare_their_errors(app):
    paths = app.openapi()["paths"]
    for route in ("submissions", "withdraw", "appeals"):
        assert {"400", "403", "404", "409", "429"} <= set(
            paths[f"{CLAIMS}/{{claim_id}}/{route}"]["post"]["responses"]
        )
    assert {"401", "403", "404", "409"} <= set(
        paths[f"{ADMIN}/claims/{{claim_id}}/review"]["post"]["responses"]
    )
    for route in ("cancel", "reopen"):
        assert {"401", "404", "409"} <= set(
            paths[f"{ADMIN}/slots/{{slot_id}}/{route}"]["post"]["responses"]
        )
    assert (
        "403" in paths[f"{ADMIN}/appeals/{{appeal_id}}/decision"]["post"]["responses"]
    )
