"""Operator reads: the private bounty audit, review and settlement queues (P5).

Public views hide operators; these admin reads must show every control, actor
and reason so Backroom can run and audit the workflow without the UI.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from ditto.tests.api_server.endpoints.test_bounty_claims import (
    _ADMIN_TOKEN,
    _HEADERS,
    ALICE,
    BOB,
    CAROL,
    ISSUE,
    REPO,
    World,
)
from ditto.tests.api_server.endpoints.test_bounty_reviews import (
    ADMIN,
    appeal,
    claimed,
    events,
    review,
    submit,
    travel,
    withdraw,
)
from ditto.tests.api_server.endpoints.test_bounty_settlement import (
    ONE,
    accepted,
    approve,
    award,
    dispatch,
    obligation,
    policy,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def world(app, client, session_maker):
    return World(app, client, session_maker)


async def operator(world) -> dict:
    bounty_id = world.detail["summary"]["bounty_id"]
    read = await world.client.get(f"{ADMIN}/{bounty_id}", headers=_HEADERS)
    assert read.status_code == 200, read.text
    return read.json()


async def test_the_operator_view_audits_every_decision(world):
    await world.start(ALICE, BOB)
    bounty_id = world.detail["summary"]["bounty_id"]
    paused = await world.client.post(
        f"{ADMIN}/{bounty_id}/claims-paused",
        headers=_HEADERS,
        json={
            "paused": True,
            "reason": "fixture pause for audit",
            "confirmation": f"PAUSE BOUNTY CLAIMS {REPO}#{ISSUE}",
        },
    )
    assert paused.status_code == 200
    await world.client.post(
        f"{ADMIN}/{bounty_id}/claims-paused",
        headers=_HEADERS,
        json={
            "paused": False,
            "reason": "fixture resume for audit",
            "confirmation": f"RESUME BOUNTY CLAIMS {REPO}#{ISSUE}",
        },
    )
    won = await accepted(world, ALICE)
    lost = (await submit(world, BOB, await claimed(world, BOB))).json()
    await review(world, lost, "reject")
    rejection = next(e for e in await events(world) if e["kind"] == "rejected")
    await appeal(world, BOB, lost, rejection["event_id"], "Fixture private statement.")
    await policy(world)
    granted = (await award(world, won, 40_000_000_000)).json()
    refund = obligation(granted, "fee_refund")
    await approve(world, refund)
    await dispatch(world, refund)
    view = await operator(world)
    assert view["reviewer_actor"] == "reviewer-private@example.com"
    [revision] = view["revisions"]
    assert (revision["actor"], revision["reviewer_actor"]) == (
        "operator-fixture@example.com",
        "reviewer-private@example.com",
    )
    assert {c["claim_id"] for c in view["claims"]} == {
        won["claim_id"],
        lost["claim_id"],
    }
    [statement] = view["appeals"]
    assert statement["statement"] == "Fixture private statement."
    assert statement["upheld"] is None
    audit = [(e["kind"], e["actor"], e["reason"]) for e in view["audit"]]
    expected = [
        ("revision_published", "operator-fixture@example.com", None),
        ("claims_paused", "operator-fixture@example.com", "fixture pause for audit"),
        ("claims_resumed", "operator-fixture@example.com", "fixture resume for audit"),
        ("review_decided", "reviewer-private@example.com", "fixture accept decision"),
        ("review_decided", "reviewer-private@example.com", "fixture reject decision"),
        ("award_recorded", "operator-fixture@example.com", "fixture award decision"),
        ("obligation_approved", "approver-one@example.com", None),
        ("transfer_dispatched", "operator-fixture@example.com", None),
    ]
    for kind, actor, reason in expected:
        assert any(
            entry[0] == kind
            and entry[1] == actor
            and (reason is None or entry[2] == reason)
            for entry in audit
        ), (kind, audit)
    times = [entry["recorded_at"] for entry in view["audit"]]
    assert times == sorted(times)
    # None of it reaches the public views.
    public = (await world.client.get(f"/api/v1/public/bounties/{bounty_id}")).text
    for private in (
        "operator-fixture@example.com",
        "approver-one@example.com",
        "Fixture private statement.",
        "fixture award decision",
        "fixture pause for audit",
    ):
        assert private not in public


async def test_slot_actions_audit_what_they_changed(world):
    await world.start(ALICE, BOB)
    left = await claimed(world, ALICE)
    assert (await withdraw(world, ALICE, left)).status_code == 200
    reopened = await world.client.post(
        f"{ADMIN}/slots/{left['slot_id']}/reopen",
        headers=_HEADERS,
        json={
            "reason": "fixture reopen for audit",
            "confirmation": f"REOPEN BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 SLOT 1",
        },
    )
    assert reopened.status_code == 200, reopened.text
    working = await claimed(world, BOB)
    cancelled = await world.client.post(
        f"{ADMIN}/slots/{working['slot_id']}/cancel",
        headers=_HEADERS,
        json={
            "kind": "system_fault",
            "reason": "fixture cancel for audit",
            "confirmation": f"CANCEL BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 SLOT "
            f"{working['position']}",
        },
    )
    assert cancelled.status_code == 200, cancelled.text
    audit = {
        e["kind"]: (e["detail"], e["claim_id"])
        for e in (await operator(world))["audit"]
        if e["kind"] in ("slot_reopened", "slot_cancelled")
    }
    assert audit == {
        "slot_reopened": ("round 1 slot 1, was withdrawn", left["claim_id"]),
        "slot_cancelled": (
            f"round 1 slot {working['position']}, was claimed (system_fault)",
            working["claim_id"],
        ),
    }


async def test_the_review_queue_lists_overdue_first(world, monkeypatch):
    await world.start(ALICE, BOB, CAROL)
    first = (await submit(world, ALICE, await claimed(world, ALICE))).json()
    travel(monkeypatch, timedelta(days=1))
    second = (await submit(world, BOB, await claimed(world, BOB))).json()
    travel(monkeypatch, timedelta(days=7, hours=12))
    queue = (await world.client.get(f"{ADMIN}/queue/reviews", headers=_HEADERS)).json()
    assert [item["claim_id"] for item in queue["items"]] == [
        first["claim_id"],
        second["claim_id"],
    ]
    assert [item["overdue"] for item in queue["items"]] == [True, False]
    assert queue["items"][0]["reviewer_actor"] == "reviewer-private@example.com"
    assert queue["items"][0]["submission"]["head_sha"] == "ab" * 20
    overdue = (
        await world.client.get(
            f"{ADMIN}/queue/reviews", headers=_HEADERS, params={"overdue_only": True}
        )
    ).json()
    assert [item["claim_id"] for item in overdue["items"]] == [first["claim_id"]]


async def test_the_settlement_queue_shows_what_needs_an_operator(world):
    await world.start(ALICE)
    won = await accepted(world, ALICE)
    await policy(world)
    granted = (await award(world, won, 40_000_000_000)).json()
    prize = obligation(granted, "award")
    refund = obligation(granted, "fee_refund")
    await approve(world, prize, headers=ONE)
    queue = (
        await world.client.get(f"{ADMIN}/queue/settlement", headers=_HEADERS)
    ).json()
    by_kind = {item["obligation"]["kind"]: item for item in queue["items"]}
    assert set(by_kind) == {"award", "fee_refund"}
    assert by_kind["award"]["approvers"] == ["approver-one@example.com"]
    assert by_kind["award"]["obligation"]["approvals_required"] == 2
    await approve(world, refund, headers=ONE)
    await dispatch(world, refund)
    remaining = (
        await world.client.get(f"{ADMIN}/queue/settlement", headers=_HEADERS)
    ).json()
    # Confirmed money leaves the queue.
    assert [item["obligation"]["kind"] for item in remaining["items"]] == ["award"]
    approved = (
        await world.client.get(
            f"{ADMIN}/queue/settlement",
            headers=_HEADERS,
            params={"state": "confirmed"},
        )
    ).json()
    assert [item["obligation"]["kind"] for item in approved["items"]] == ["fee_refund"]
    invalid = await world.client.get(
        f"{ADMIN}/queue/settlement", headers=_HEADERS, params={"state": "paid"}
    )
    assert invalid.status_code == 422


# A fixed id: parametrized ids must match across test workers.
@pytest.mark.parametrize(
    "path",
    [
        "/queue/reviews",
        "/queue/settlement",
        "/00000000-0000-4000-8000-000000000000",
    ],
)
async def test_operator_reads_need_the_admin_token(world, path):
    await world.start(ALICE)
    assert (await world.client.get(f"{ADMIN}{path}")).status_code == 401


async def test_unknown_bounties_are_404(world):
    await world.start(ALICE)
    missing = await world.client.get(f"{ADMIN}/{uuid4()}", headers=_ADMIN_TOKEN)
    assert missing.status_code == 404


async def test_operator_routes_declare_their_errors(app):
    paths = app.openapi()["paths"]
    assert "404" in paths[f"{ADMIN}/{{bounty_id}}"]["get"]["responses"]
    for route in ("/queue/reviews", "/queue/settlement"):
        assert "401" in paths[f"{ADMIN}{route}"]["get"]["responses"]
