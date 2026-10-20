"""Bounty downgrades refuse while anything they would drop or forbid exists.

Each scenario records only what a downgrade's earlier guard missed, so the
refusal comes from the guard that should catch it, not from a later one. The
real Alembic chain runs against this worker's database and is always
restored to head.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from ditto.db.models import (
    BountyClaimControl,
    BountyEvaluationDecision,
    BountyEvaluationEntry,
    BountyObligation,
    BountyReviewerAssignment,
)
from ditto.tests.api_server.endpoints.test_bounty_claims import (
    _HEADERS,
    ALICE,
    ISSUE,
    REPO,
    World,
)
from ditto.tests.api_server.endpoints.test_bounty_reviews import ADMIN, claimed
from ditto.tests.api_server.endpoints.test_bounty_settlement import accepted

BEFORE_BOUNTIES = "dc41b7a6e205"
P1, P2, P3 = "bb24cc6f573b", "c5f81d2a9e47", "d7a3e9b2c641"
SETTLEMENT = "e1b8c4f7a209"


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


@pytest.fixture
async def world(app, client, session_maker):
    try:
        yield World(app, client, session_maker)
    finally:
        restored = alembic("upgrade", "head")
        assert restored.returncode == 0, restored.stderr


async def count(world, model) -> int:
    async with world.maker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)))


def refused(target: str) -> None:
    result = alembic("downgrade", target)
    assert result.returncode != 0
    assert "refusing to drop recorded bounty" in result.stderr, result.stderr


async def test_a_pause_without_claims_blocks_the_claims_downgrade(world):
    await world.start(ALICE)
    bounty_id = world.detail["summary"]["bounty_id"]
    paused = await world.client.post(
        f"{ADMIN}/{bounty_id}/claims-paused",
        headers=_HEADERS,
        json={
            "paused": True,
            "reason": "fixture pause before any claim",
            "confirmation": f"PAUSE BOUNTY CLAIMS {REPO}#{ISSUE}",
        },
    )
    assert paused.status_code == 200, paused.text
    refused(P1)
    assert await count(world, BountyClaimControl) == 1


async def test_a_reassignment_without_submissions_blocks_the_review_downgrade(world):
    await world.start(ALICE)
    bounty_id = world.detail["summary"]["bounty_id"]
    moved = await world.client.post(
        f"{ADMIN}/{bounty_id}/reviewer",
        headers=_HEADERS,
        json={
            "reviewer_actor": "second-reviewer@example.com",
            "reason": "fixture reviewer is away",
            "confirmation": f"REASSIGN BOUNTY REVIEWER {REPO}#{ISSUE}",
        },
    )
    assert moved.status_code == 200, moved.text
    refused(P2)
    assert await count(world, BountyReviewerAssignment) == 1


async def test_an_accepted_refund_blocks_the_settlement_downgrade(world):
    await world.start(ALICE)
    # Accepting work owes its fee back with a reason the older check forbids;
    # no policy, approval, transfer or fact exists.
    await accepted(world, ALICE)
    refused(P3)
    assert await count(world, BountyObligation) == 1


async def test_a_linked_refund_event_blocks_the_settlement_downgrade(world):
    await world.start(ALICE)
    claim = await claimed(world, ALICE)
    # A team cancellation owes the fee back for a reason, and with event kinds,
    # the older checks allow; only the refund event's obligation is newer.
    cancelled = await world.client.post(
        f"{ADMIN}/slots/{claim['slot_id']}/cancel",
        headers=_HEADERS,
        json={
            "kind": "team",
            "reason": "fixture cancellation",
            "confirmation": f"CANCEL BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 SLOT "
            f"{claim['position']}",
        },
    )
    assert cancelled.status_code == 200, cancelled.text
    refused(P3)
    assert await count(world, BountyObligation) == 1


@pytest.mark.parametrize("record", ["entries", "decisions"])
async def test_a_recorded_evaluation_blocks_the_evaluation_downgrade(world, record):
    await world.start(ALICE)
    bounty_id = world.detail["summary"]["bounty_id"]
    body = (
        {
            "kind": "actual",
            "metric": "reviewer_hours",
            "value": 2.5,
            "expected_entry_id": 0,
            "reason": "fixture actual for the downgrade guard",
            "confirmation": f"RECORD BOUNTY ACTUAL REVIEWER_HOURS {REPO}#{ISSUE}",
        }
        if record == "entries"
        else {
            "decision": "stop",
            "integration_status": "not_integrated",
            "expected_decision_id": 0,
            "reasons": "fixture decision for the downgrade guard",
            "confirmation": f"DECIDE BOUNTY EVALUATION {REPO}#{ISSUE} STOP",
        }
    )
    recorded = await world.client.post(
        f"{ADMIN}/{bounty_id}/evaluation/{record}", headers=_HEADERS, json=body
    )
    assert recorded.status_code == 200, recorded.text
    refused(SETTLEMENT)
    model = BountyEvaluationEntry if record == "entries" else BountyEvaluationDecision
    assert await count(world, model) == 1


async def test_an_empty_bounty_schema_round_trips(world):
    del world
    down = alembic("downgrade", BEFORE_BOUNTIES)
    assert down.returncode == 0, down.stderr
    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
