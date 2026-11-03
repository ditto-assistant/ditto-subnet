"""The dashboard's bounty fixtures are recordings of the real public API (P6).

The board's UI tests render ``apps/platform/dashboard/fixtures/bounty-*.json``.
This test drives real bounties through every public state the board shows and
requires each fixture to equal the live response once ids, times and simulated
block hashes are numbered in order of appearance. A serializer or lifecycle
change fails here until the fixtures are recorded again:

    DITTO_RECORD_BOUNTY_FIXTURES=1 uv run pytest <this file>
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from ditto.api_models.bounty import BountyBoardPage, BountyClaimView, BountyDetail
from ditto.tests.api_server.endpoints.test_bounties import publish as publish_bounty
from ditto.tests.api_server.endpoints.test_bounty_claims import (
    _HEADERS,
    ALICE,
    BOB,
    CAROL,
    CLAIMS,
    DAVE,
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

FIXTURES = Path(__file__).resolve().parents[4] / "dashboard" / "fixtures"
RECORD = os.environ.get("DITTO_RECORD_BOUNTY_FIXTURES") == "1"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_BLOCK = re.compile(r"^0x[0-9a-f]{64}$")
_EVENT_IDS = frozenset({"event_id", "subject_event_id"})


@pytest.fixture
async def world(app, client, session_maker):
    return World(app, client, session_maker)


async def lifecycle(world) -> dict[str, object]:
    """Two bounties; the first has a claim in every state the board shows."""
    await world.start(
        ALICE, BOB, CAROL, DAVE, title="Fixture: build the public bounty board"
    )
    # Dave withdraws and his slot is reopened: his fee is retained, and his
    # claim stays reachable through the history.
    left = await claimed(world, DAVE)
    assert (await withdraw(world, DAVE, left)).status_code == 200
    reopened = await world.client.post(
        f"{ADMIN}/slots/{left['slot_id']}/reopen",
        headers=_HEADERS,
        json={
            "reason": "fixture reopen after withdrawal",
            "confirmation": f"REOPEN BOUNTY SLOT {REPO}#{ISSUE} ROUND 1 SLOT 1",
        },
    )
    assert reopened.status_code == 200, reopened.text
    # Alice takes the reopened slot; her work is accepted and awarded.
    won = await accepted(world, ALICE)
    # Bob's work is rejected and he appeals.
    lost = (await submit(world, BOB, await claimed(world, BOB))).json()
    assert (await review(world, lost, "reject")).status_code == 200
    rejection = next(e for e in await events(world) if e["kind"] == "rejected")
    filed = await appeal(
        world, BOB, lost, rejection["event_id"], "Fixture private statement."
    )
    assert filed.status_code == 200, filed.text
    # Carol is still delivering.
    working = await claimed(world, CAROL)
    assert (await policy(world)).status_code == 200
    granted = (await award(world, won, 40_000_000_000, "Alice built the board")).json()
    refund = obligation(granted, "fee_refund")
    assert (await approve(world, refund, headers=ONE)).status_code == 200
    assert (await dispatch(world, refund)).status_code == 200
    # One of the award's two approvals.
    assert (await approve(world, obligation(granted, "award"))).status_code == 200
    merged = await world.client.post(
        f"{ADMIN}/claims/{won['claim_id']}/facts",
        headers=_HEADERS,
        json={
            "kind": "merged",
            "reference": "cd" * 20,
            "confirmation": f"RECORD BOUNTY MERGED {won['claim_id']}",
        },
    )
    assert merged.status_code == 200, merged.text
    second = await publish_bounty(
        world.client,
        issue=ISSUE + 1,
        terms={"title": "Fixture: document the contributor flow"},
    )
    assert second.status_code == 200, second.text

    board = (await world.client.get("/api/v1/public/bounties")).json()
    bounty_id = world.detail["summary"]["bounty_id"]
    detail = (await world.client.get(f"/api/v1/public/bounties/{bounty_id}")).json()
    claims = {}
    for claim in (left, won, lost, working):
        claims[claim["claim_id"]] = (
            await world.client.get(f"{CLAIMS}/{claim['claim_id']}")
        ).json()
    return {"bounty-board": board, "bounty-detail": detail, "bounty-claims": claims}


def normalize(documents: dict[str, object]) -> object:
    """Number ids, event ids and block hashes in order; erase times."""
    numbers: dict[str, dict[object, str]] = {"id": {}, "event": {}, "block": {}}

    def number(kind: str, value: object) -> str:
        seen = numbers[kind]
        return seen.setdefault(value, f"<{kind} {len(seen) + 1}>")

    def walk(value: object, key: str | None = None) -> object:
        if isinstance(value, dict):
            return {walk(k): walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if key in _EVENT_IDS and isinstance(value, int):
            return number("event", value)
        if isinstance(value, str):
            if _UUID.match(value):
                return number("id", value)
            if _TIME.match(value):
                return "<time>"
            if _BLOCK.match(value):
                return number("block", value)
        return value

    return {name: walk(documents[name]) for name in sorted(documents)}


def recorded() -> dict[str, object]:
    return {
        name: json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("bounty-board", "bounty-detail", "bounty-claims")
    }


async def test_dashboard_fixtures_record_the_live_board(world):
    live = await lifecycle(world)
    if RECORD:
        for name, document in live.items():
            (FIXTURES / f"{name}.json").write_text(
                json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    assert normalize(recorded()) == normalize(live)


def test_dashboard_fixtures_are_valid_wire_models():
    fixtures = recorded()
    BountyBoardPage.model_validate(fixtures["bounty-board"])
    detail = BountyDetail.model_validate(fixtures["bounty-detail"])
    claims = fixtures["bounty-claims"]
    assert isinstance(claims, dict)
    for claim_id, claim in claims.items():
        assert BountyClaimView.model_validate(claim).claim_id.hex == claim_id.replace(
            "-", ""
        )
    # Every claim the detail names has its recorded claim view.
    named = {e.claim_id for e in detail.events if e.claim_id is not None}
    assert {str(claim_id) for claim_id in named} == set(claims)
    # The fixture shows every state the board renders.
    statuses = {slot.status for slot in detail.slots}
    assert statuses == {"accepted", "rejected", "claimed"}
    assert {
        item.slots.open
        for item in BountyBoardPage.model_validate(fixtures["bounty-board"]).items
    } == {0, 3}
    kinds = {
        (o["kind"], o["state"])
        for claim in claims.values()
        for o in claim["obligations"]
    }
    assert kinds == {
        ("fee_retained", "retained"),
        ("fee_refund", "confirmed"),
        ("award", "awaiting_approval"),
    }
