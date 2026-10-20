"""Bounty evaluation records: estimates, actuals and pilot decisions (#2944).

Operators record them through the admin API with typed confirmations. Each
write names the record it supersedes, the history stays, the operator view
shows every record with its operator, and the report shows only current
values and the current decision, never who recorded them.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ditto.tests.api_server.endpoints.test_bounty_claims import (
    _ADMIN_TOKEN,
    _HEADERS,
    ALICE,
    ISSUE,
    REPO,
    World,
)
from ditto.tests.api_server.endpoints.test_bounty_reviews import ADMIN

pytestmark = pytest.mark.asyncio
OTHER = {**_ADMIN_TOKEN, "X-Admin-Actor": "evaluator-two@example.com"}


@pytest.fixture
async def world(app, client, session_maker):
    world = World(app, client, session_maker)
    await world.start(ALICE)
    return world


def bounty_of(world) -> str:
    return world.detail["summary"]["bounty_id"]


async def entry(world, *, headers=_HEADERS, bounty=None, **over):
    body = {
        "kind": "estimate",
        "metric": "build_hours",
        "value": 40,
        "expected_entry_id": 0,
        "note": "fixture: from the issue's scope",
        "reason": "fixture estimate before the pilot",
        **over,
    }
    body.setdefault(
        "confirmation",
        f"RECORD BOUNTY {body['kind'].upper()} {body['metric'].upper()} {REPO}#{ISSUE}",
    )
    return await world.client.post(
        f"{ADMIN}/{bounty or bounty_of(world)}/evaluation/entries",
        headers=headers,
        json=body,
    )


async def decision(world, *, headers=_HEADERS, **over):
    body = {
        "decision": "expand",
        "integration_status": "integrated",
        "expected_decision_id": 0,
        "reasons": "fixture: cheaper than an internal build",
        **over,
    }
    body.setdefault(
        "confirmation",
        f"DECIDE BOUNTY EVALUATION {REPO}#{ISSUE} {body['decision'].upper()}",
    )
    return await world.client.post(
        f"{ADMIN}/{bounty_of(world)}/evaluation/decisions",
        headers=headers,
        json=body,
    )


async def operator(world) -> dict:
    read = await world.client.get(f"{ADMIN}/{bounty_of(world)}", headers=_HEADERS)
    assert read.status_code == 200, read.text
    return read.json()


async def test_the_latest_entry_wins_and_the_history_stays(world):
    first = await entry(world)
    assert first.status_code == 200, first.text
    recorded = first.json()
    assert (recorded["kind"], recorded["metric"], recorded["value"]) == (
        "estimate",
        "build_hours",
        40.0,
    )
    assert recorded["actor"] == "operator-fixture@example.com"
    actual = await entry(world, kind="actual", value=52.5, headers=OTHER)
    assert actual.status_code == 200, actual.text
    # A second estimate names the one it supersedes.
    second = await entry(
        world, value=45.25, expected_entry_id=recorded["entry_id"], note=None
    )
    assert second.status_code == 200, second.text
    view = await operator(world)
    assert [
        (e["kind"], e["value"], e["actor"]) for e in view["evaluation"]["entries"]
    ] == [
        ("estimate", 40.0, "operator-fixture@example.com"),
        ("actual", 52.5, "evaluator-two@example.com"),
        ("estimate", 45.25, "operator-fixture@example.com"),
    ]
    evaluation = view["report"]["evaluation"]
    assert evaluation["estimates"]["build_hours"] == 45.25
    assert evaluation["actuals"]["build_hours"] == 52.5
    assert evaluation["estimates"]["reviewer_hours"] is None
    assert evaluation["decision"] is None
    recorded_audit = [e for e in view["audit"] if e["kind"] == "evaluation_recorded"]
    assert [(e["actor"], e["detail"]) for e in recorded_audit] == [
        ("operator-fixture@example.com", "estimate build_hours 40.0"),
        ("evaluator-two@example.com", "actual build_hours 52.5"),
        ("operator-fixture@example.com", "estimate build_hours 45.25"),
    ]


async def test_an_entry_supersedes_only_the_entry_it_names(world):
    first = (await entry(world)).json()
    second = await entry(world, value=45, expected_entry_id=first["entry_id"])
    assert second.status_code == 200, second.text
    # An exact retry returns the entry it made, and records nothing more.
    again = await entry(world, value=45, expected_entry_id=first["entry_id"])
    assert again.status_code == 200 and again.json() == second.json()
    # Any other write naming a superseded entry is refused, as is a stale 0.
    for stale in (
        {"value": 50, "expected_entry_id": first["entry_id"]},
        {"value": 45, "expected_entry_id": first["entry_id"], "headers": OTHER},
        {"value": 50, "expected_entry_id": 0},
    ):
        refused = await entry(world, **stale)
        assert refused.status_code == 409, refused.text
        assert f"current entry is {second.json()['entry_id']}" in refused.text
    # Another metric has its own history.
    other = await entry(world, metric="operator_hours", value=3)
    assert other.status_code == 200, other.text
    entries = (await operator(world))["evaluation"]["entries"]
    assert len(entries) == 3


async def test_the_latest_decision_wins_and_the_history_stays(world):
    first = await decision(world, decision="revise", integration_status="integrating")
    assert first.status_code == 200, first.text
    again = await decision(world, decision="revise", integration_status="integrating")
    assert again.status_code == 200 and again.json() == first.json()
    stale = await decision(world)
    assert stale.status_code == 409 and "current decision is" in stale.text
    second = await decision(
        world, headers=OTHER, expected_decision_id=first.json()["decision_id"]
    )
    assert second.status_code == 200, second.text
    view = await operator(world)
    assert [
        (d["decision"], d["integration_status"], d["actor"])
        for d in view["evaluation"]["decisions"]
    ] == [
        ("revise", "integrating", "operator-fixture@example.com"),
        ("expand", "integrated", "evaluator-two@example.com"),
    ]
    evaluation = view["report"]["evaluation"]
    assert (evaluation["decision"], evaluation["integration_status"]) == (
        "expand",
        "integrated",
    )
    assert evaluation["reasons"] == "fixture: cheaper than an internal build"
    assert evaluation["decided_at"] == second.json()["recorded_at"]
    decided = [e for e in view["audit"] if e["kind"] == "evaluation_decided"]
    assert [(e["actor"], e["detail"]) for e in decided] == [
        ("operator-fixture@example.com", "revise, integrating"),
        ("evaluator-two@example.com", "expand, integrated"),
    ]


async def test_the_report_names_no_one_and_no_private_note(world):
    assert (await entry(world, kind="actual", headers=OTHER)).status_code == 200
    assert (await decision(world, headers=OTHER)).status_code == 200
    view = await operator(world)
    published = json.dumps(view["report"])
    assert "evaluator-two@example.com" in json.dumps(view["evaluation"])
    for private in (
        "evaluator-two@example.com",
        "fixture: from the issue's scope",
        "fixture estimate before the pilot",
    ):
        assert private not in published
    # The decision's reasons are part of the evaluation.
    assert "fixture: cheaper than an internal build" in published
    public = (
        await world.client.get(f"/api/v1/public/bounties/{bounty_of(world)}")
    ).text
    assert "evaluator-two@example.com" not in public
    assert "fixture: cheaper than an internal build" not in public


@pytest.mark.parametrize(
    "over,status,message",
    [
        ({"confirmation": "RECORD BOUNTY"}, 409, "RECORD BOUNTY ESTIMATE BUILD_HOURS"),
        ({"reason": " " * 12}, 400, "reason must not be blank"),
        ({"note": "   "}, 400, "note must not be blank"),
        ({"value": -1}, 422, "request validation failed"),
        ({"value": 1_000_001}, 422, "request validation failed"),
        ({"value": "40"}, 422, "request validation failed"),
        ({"metric": "lines_of_code"}, 422, "request validation failed"),
        ({"kind": "guess"}, 422, "request validation failed"),
        ({"expected_entry_id": -1}, 422, "request validation failed"),
        ({"bounty": "00000000-0000-4000-8000-000000000000"}, 404, "bounty not found"),
    ],
)
async def test_refused_entries_record_nothing(world, over, status, message):
    refused = await entry(world, **over)
    assert refused.status_code == status, refused.text
    assert message in refused.json()["message"]
    assert (await operator(world))["evaluation"]["entries"] == []


@pytest.mark.parametrize(
    "over,status,message",
    [
        ({"confirmation": "DECIDE"}, 409, f"DECIDE BOUNTY EVALUATION {REPO}#{ISSUE}"),
        ({"reasons": " " * 12}, 400, "reasons must not be blank"),
        ({"reasons": "short"}, 422, "request validation failed"),
        ({"decision": "pause"}, 422, "request validation failed"),
        ({"integration_status": "merged"}, 422, "request validation failed"),
        ({"expected_decision_id": 7}, 409, "current decision is 0"),
    ],
)
async def test_refused_decisions_record_nothing(world, over, status, message):
    refused = await decision(world, **over)
    assert refused.status_code == status, refused.text
    assert message in refused.json()["message"]
    assert (await operator(world))["evaluation"]["decisions"] == []


async def test_evaluation_records_are_append_only(world):
    assert (await entry(world)).status_code == 200
    assert (await decision(world)).status_code == 200
    history = "bounty history is append-only"
    for statement in (
        "UPDATE bounty_evaluation_entries SET value = 1",
        "DELETE FROM bounty_evaluation_entries",
        "TRUNCATE bounty_evaluation_entries",
        "UPDATE bounty_evaluation_decisions SET decision = 'stop'",
        "DELETE FROM bounty_evaluation_decisions",
        "TRUNCATE bounty_evaluation_decisions",
    ):
        async with world.maker() as session:
            with pytest.raises(DBAPIError, match=history):
                await session.execute(text(statement))
            await session.rollback()
    # The checks refuse what the API refuses, written around it.
    bounty = bounty_of(world)
    for statement, check in (
        (
            "INSERT INTO bounty_evaluation_entries (bounty_id, kind, metric, value,"
            " reason, actor, recorded_at) VALUES (:bounty, 'estimate', 'build_hours',"
            " 'NaN', 'fixture reason', 'fixture', now())",
            "ck_bounty_evaluation_entries_entry",
        ),
        (
            "INSERT INTO bounty_evaluation_decisions (bounty_id, decision,"
            " integration_status, reasons, actor, recorded_at) VALUES (:bounty,"
            " 'expand', 'integrated', '   ', 'fixture', now())",
            "ck_bounty_evaluation_decisions_decision",
        ),
    ):
        async with world.maker() as session:
            with pytest.raises(DBAPIError, match=check):
                await session.execute(text(statement), {"bounty": bounty})
            await session.rollback()


async def test_evaluation_routes_declare_their_errors(app):
    paths = app.openapi()["paths"]
    for route in ("entries", "decisions"):
        assert {"400", "401", "404", "409"} <= set(
            paths[f"{ADMIN}/{{bounty_id}}/evaluation/{route}"]["post"]["responses"]
        ), route


async def test_an_unknown_bounty_has_no_evaluation(world):
    missing = await world.client.post(
        f"{ADMIN}/{uuid4()}/evaluation/decisions",
        headers=_HEADERS,
        json={
            "decision": "stop",
            "integration_status": "not_integrated",
            "expected_decision_id": 0,
            "reasons": "fixture: no such bounty",
            "confirmation": "DECIDE BOUNTY EVALUATION",
        },
    )
    assert missing.status_code == 404, missing.text
