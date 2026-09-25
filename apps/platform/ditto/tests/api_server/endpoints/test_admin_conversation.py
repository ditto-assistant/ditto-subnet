"""Real-Postgres claim fencing, replay protection and non-authoritative scores."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import Request
from sqlalchemy import func, select

from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.screener import require_screener
from ditto.db.models import Agent, AgentStatus, ConversationAssessment, Score
from ditto_screening_protocol.conversation import DIMENSIONS, ConversationReport
from ditto_screening_protocol.conversation_story import story, story_digest

TOKEN = uuid4().hex
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/api/v1/admin/conversation-assessments"
pytestmark = pytest.mark.asyncio


async def install(app, session_maker, monkeypatch, *, enabled=True, count=6):
    app.state.config = replace(
        app.state.config, conversation_shadow_enabled=enabled, admin_api_token=TOKEN
    )

    async def session() -> AsyncIterator:
        async with session_maker() as value:
            yield value

    app.dependency_overrides[get_session] = session
    entries = []
    async with session_maker() as db, db.begin():
        for i in range(count):
            agent_id = uuid4()
            db.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"hotkey-{i}",
                    name=f"agent-{i}",
                    sha256=agent_id.hex * 2,
                    status=AgentStatus.SCORED,
                    created_at=datetime.now(UTC),
                    screened_image_sha256="b" * 64,
                    screened_image_size_bytes=1,
                    screened_image_id="sha256:" + "b" * 64,
                    screened_image_ref=f"screened:{agent_id}",
                    screened_image_upload_id=uuid4(),
                    screened_image_verified_at=datetime.now(UTC),
                )
            )
            entries.append(
                SimpleNamespace(
                    agent_id=agent_id,
                    rank=i + 1,
                    finalized=True,
                    bench_version=13,
                    official_composite=0.9 - i / 100,
                )
            )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.public.build_public_leaderboard",
        AsyncMock(return_value=SimpleNamespace(entries=entries)),
    )
    return entries


def report_for(claim):
    turns = story(claim["seed"])
    return ConversationReport(
        assessment_id=claim["assessment_id"],
        agent_id=claim["agent_id"],
        artifact_sha256=claim["artifact_sha256"],
        screened_image_sha256=claim["screened_image_sha256"],
        bench_version=claim["bench_version"],
        story_sha256=story_digest(claim["seed"]),
        provider_model="gpt-6-astra",
        status="completed",
        exchanges=[
            {
                "turn_id": t.turn_id,
                "session": (t.turn_id - 1) // 3 + 1,
                "user": t.user,
                "assistant": f"Answer {t.turn_id}",
            }
            for t in turns
        ],
        grades={
            "probes": [
                {
                    "turn_id": i,
                    "dimension": DIMENSIONS[(i - 11) % 5],
                    "score": 2,
                    "quote": f"Answer {i}",
                    "rationale": "This is partial evidence for the stated criterion.",
                }
                for i in range(11, 31)
            ]
        },
        judge_requests=31,
        input_tokens=100,
        output_tokens=100,
        reserved_microusd=25_000_000,
        spent_microusd=6000,
    ).model_dump(mode="json")


async def test_off_and_unauthorized_do_not_reserve(
    app, client, session_maker, monkeypatch
):
    await install(app, session_maker, monkeypatch, enabled=False)
    assert (await client.post(BASE + "/claim")).status_code == 401
    assert (await client.post(BASE + "/claim", headers=HEADERS)).json() is None
    listing = (await client.get(BASE, headers=HEADERS)).json()
    assert listing["mode"] == "off" and listing["items"] == []


async def test_audited_switch_stops_claims_and_rejects_stale_revision(
    app, client, session_maker, monkeypatch
):
    await install(app, session_maker, monkeypatch, enabled=False)
    payload = {
        "mode": "shadow",
        "expected_revision": 0,
        "actor": "operator@example.com",
        "reason": "Start the bounded shadow canary",
        "confirmation": "APPLY CONVERSATION SHADOW SETTINGS",
    }
    changed = await client.post(BASE + "/settings", headers=HEADERS, json=payload)
    assert changed.status_code == 200, changed.text
    assert changed.json()["settings_revision"] == 1
    assert changed.json()["settings_actor"] == payload["actor"]
    assert (
        await client.post(BASE + "/settings", headers=HEADERS, json=payload)
    ).status_code == 409
    payload.update(mode="off", expected_revision=1)
    assert (
        await client.post(BASE + "/settings", headers=HEADERS, json=payload)
    ).status_code == 200
    assert (await client.post(BASE + "/claim", headers=HEADERS)).json() is None


async def test_worker_claim_is_image_bound_and_result_requires_its_owner(
    app, client, session_maker, monkeypatch
):
    await install(app, session_maker, monkeypatch)

    async def worker(request: Request):
        request.state.screener_node_status = "active"
        return request.headers.get("x-test-worker", "worker-one")

    app.dependency_overrides[require_screener] = worker
    storage = SimpleNamespace(
        presigned_get_url=AsyncMock(return_value="https://artifacts.example/image.tar")
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.screener_conversation.get_storage_client",
        AsyncMock(return_value=storage),
    )
    worker_base = "/api/v1/screener/conversation-assessments"
    response = await client.post(worker_base + "/claim")
    assert response.status_code == 200, response.text
    claim = response.json()
    assert claim["screened_image_url"] == "https://artifacts.example/image.tar"
    report = report_for(claim)
    payload = {"lease_token": claim["lease_token"], "report": report}
    path = worker_base + f"/{claim['assessment_id']}/result"
    assert (
        await client.post(path, headers={"x-test-worker": "worker-two"}, json=payload)
    ).status_code == 403
    assert (await client.post(path, json=payload)).status_code == 422
    report["harness_usage"] = {
        "profile": "conversation-openrouter-oss20b-pplx768-v1",
        "requests": 60,
        "tokens": 10000,
        "spent_microusd": 30000,
        "unmetered": False,
        "failed": False,
    }
    report["judge_cost_is_upper_bound"] = False
    accepted = await client.post(path, json=payload)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["spent_microusd"] == 36000
    listing = (await client.get(BASE, headers=HEADERS)).json()
    assert "seed" not in str(listing) and "lease_token" not in str(listing)
    assert "screened_image_url" not in str(listing)


async def test_concurrent_claims_only_admit_top_five_once(
    app, client, session_maker, monkeypatch
):
    entries = await install(app, session_maker, monkeypatch)
    responses = await asyncio.gather(
        *[client.post(BASE + "/claim", headers=HEADERS) for _ in range(8)]
    )
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    claims = [r.json() for r in responses if r.json()]
    assert len(claims) == 1  # Global paid concurrency is one, even across workers.
    for _ in range(4):
        current = claims[-1]
        response = await client.post(
            BASE + f"/{current['assessment_id']}/result",
            headers=HEADERS,
            json={"lease_token": current["lease_token"], "report": report_for(current)},
        )
        assert response.status_code == 200, response.text
        claims.append((await client.post(BASE + "/claim", headers=HEADERS)).json())
    assert {c["agent_id"] for c in claims} == {str(e.agent_id) for e in entries[:5]}
    assert len({c["seed"] for c in claims}) == 5
    listing = (await client.get(BASE, headers=HEADERS)).json()
    assert listing["reserved_last_day_microusd"] == 150_000_000
    assert sum(i["proposed_quality_micros"] is None for i in listing["items"]) == 1


async def test_bound_immutable_report_projects_quality_without_writing_scores(
    app, client, session_maker, monkeypatch
):
    await install(app, session_maker, monkeypatch)
    claim = (await client.post(BASE + "/claim", headers=HEADERS)).json()
    report = report_for(claim)
    payload = {"lease_token": claim["lease_token"], "report": report}
    path = BASE + f"/{claim['assessment_id']}/result"
    forged = {**payload, "lease_token": str(uuid4())}
    assert (await client.post(path, headers=HEADERS, json=forged)).status_code == 409
    accepted = await client.post(path, headers=HEADERS, json=payload)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["conversation_micros"] == 500000
    assert accepted.json()["proposed_quality_micros"] == 766667
    assert (await client.post(path, headers=HEADERS, json=payload)).status_code == 200
    report["grades"]["probes"][0]["score"] = 4
    assert (await client.post(path, headers=HEADERS, json=payload)).status_code == 409
    stored = (
        await client.get(BASE + f"/{claim['assessment_id']}/report", headers=HEADERS)
    ).json()
    assert stored["grades"]["probes"][0]["score"] == 2
    async with session_maker() as db:
        assert await db.scalar(select(func.count()).select_from(Score)) == 0


async def test_changed_story_and_expired_lease_cannot_score(
    app, client, session_maker, monkeypatch
):
    await install(app, session_maker, monkeypatch)
    claim = (await client.post(BASE + "/claim", headers=HEADERS)).json()
    report = report_for(claim)
    payload = {"lease_token": claim["lease_token"], "report": report}
    path = BASE + f"/{claim['assessment_id']}/result"
    report["exchanges"][0]["user"] = "A different, easier assessment"
    assert (await client.post(path, headers=HEADERS, json=payload)).status_code == 409
    payload["report"] = report_for(claim)
    async with session_maker() as db, db.begin():
        row = await db.scalar(select(ConversationAssessment))
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert (await client.post(path, headers=HEADERS, json=payload)).status_code == 409
    listing = (await client.get(BASE, headers=HEADERS)).json()
    assert listing["items"][0]["status"] == "expired"
    assert listing["reserved_last_day_microusd"] == 30_000_000


async def test_fee_proposal_uses_existing_audited_revision_and_preserves_cooldown(
    app, client, session_maker, monkeypatch
):
    await install(app, session_maker, monkeypatch, enabled=False)
    observation = (await client.get(BASE, headers=HEADERS)).json()
    assert observation["current_submission_fee_rao"] == 40_000_000
    proposal = observation["fee_change_request"]
    assert proposal["fee_amount_rao"] == 200_000_000
    updated = await client.post(
        "/api/v1/admin/submission-settings", headers=HEADERS, json=proposal
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["fee_amount_rao"] == 200_000_000
    assert updated.json()["cooldown_seconds"] == proposal["cooldown_seconds"]
    # The same reviewable proposal cannot overwrite a newer operator revision.
    assert (
        await client.post(
            "/api/v1/admin/submission-settings", headers=HEADERS, json=proposal
        )
    ).status_code == 409


async def failed_attempt(client):
    claim = (await client.post(BASE + "/claim", headers=HEADERS)).json()
    report = report_for(claim)
    report.update(
        status="incomplete",
        error_code="harness_inference_incomplete",
        grades=None,
        exchanges=[],
    )
    response = await client.post(
        BASE + f"/{claim['assessment_id']}/result",
        headers=HEADERS,
        json={"lease_token": claim["lease_token"], "report": report},
    )
    assert response.status_code == 200, response.text
    return (
        claim,
        report,
        {
            "expected_revision": 0,
            "assessment_id": claim["assessment_id"],
            "expected_artifact_sha256": claim["artifact_sha256"],
            "expected_report_sha256": response.json()["report_sha256"],
            "actor": "operator@example.com",
            "reason": "Explicit approval for one retry after the relay fix",
            "confirmation": "AUTHORIZE ONE CONVERSATION RETRY",
        },
    )


async def test_manual_retry_preserves_history_and_never_retries_twice(
    app, client, session_maker, monkeypatch
):
    await install(app, session_maker, monkeypatch, count=1)
    original, report, approval = await failed_attempt(client)
    assert (await client.post(BASE + "/claim", headers=HEADERS)).json() is None
    assert (
        await client.post(BASE + "/authorize-retry", json=approval)
    ).status_code == 401
    for field, value in [
        ("expected_revision", 7),
        ("expected_artifact_sha256", "f" * 64),
        ("expected_report_sha256", "f" * 64),
    ]:
        assert (
            await client.post(
                BASE + "/authorize-retry",
                headers=HEADERS,
                json={**approval, field: value},
            )
        ).status_code == 409
    first = await client.post(BASE + "/authorize-retry", headers=HEADERS, json=approval)
    assert first.status_code == 200, first.text
    authorization = first.json()["items"][0]["retry_authorization"]
    replay = await client.post(
        BASE + "/authorize-retry", headers=HEADERS, json=approval
    )
    assert replay.json()["items"][0]["retry_authorization"] == authorization
    assert (
        await client.post(
            BASE + "/authorize-retry",
            headers=HEADERS,
            json={
                **approval,
                "reason": "Another authorization must not replace the audit",
            },
        )
    ).status_code == 409
    claims = await asyncio.gather(
        *[client.post(BASE + "/claim", headers=HEADERS) for _ in range(5)]
    )
    assert all(c.status_code == 200 for c in claims)
    retries = [c.json() for c in claims if c.json()]
    assert len(retries) == 1
    retry = retries[0]
    assert retry["assessment_id"] != original["assessment_id"]
    for field in (
        "seed",
        "agent_id",
        "artifact_sha256",
        "bench_version",
        "screened_image_sha256",
    ):
        assert retry[field] == original[field]
    retried_report = {**report, "assessment_id": retry["assessment_id"]}
    result = await client.post(
        BASE + f"/{retry['assessment_id']}/result",
        headers=HEADERS,
        json={"lease_token": retry["lease_token"], "report": retried_report},
    )
    assert result.status_code == 200, result.text
    child_approval = {
        **approval,
        "assessment_id": retry["assessment_id"],
        "expected_report_sha256": result.json()["report_sha256"],
    }
    assert (
        await client.post(
            BASE + "/authorize-retry", headers=HEADERS, json=child_approval
        )
    ).status_code == 409
    assert (
        await client.post(BASE + "/authorize-retry", headers=HEADERS, json=approval)
    ).status_code == 200
    assert (await client.post(BASE + "/claim", headers=HEADERS)).json() is None
    listing = (await client.get(BASE, headers=HEADERS)).json()
    assert listing["reserved_last_day_microusd"] == 60_000_000
    parent = next(
        i for i in listing["items"] if i["assessment_id"] == original["assessment_id"]
    )
    assert parent["retry_assessment_id"] == retry["assessment_id"]
    assert parent["retry_authorization"] == authorization
    assert result.json()["retry_of"] == original["assessment_id"]
    assert (
        await client.get(BASE + f"/{original['assessment_id']}/report", headers=HEADERS)
    ).json() == report
    assert "lease_token" not in str(listing) and "seed" not in str(listing)
    async with session_maker() as db:
        assert await db.scalar(select(func.count()).select_from(Score)) == 0


async def test_cap_admits_one_extra_retry_then_waits_without_erasing_reservations(
    app, client, session_maker, monkeypatch
):
    await install(app, session_maker, monkeypatch, count=5)
    original, _, approval = await failed_attempt(client)
    second, _, second_approval = await failed_attempt(client)
    for _ in range(3):
        await failed_attempt(client)
    response = await client.post(
        BASE + "/authorize-retry", headers=HEADERS, json=approval
    )
    assert response.status_code == 200, response.text
    assert response.json()["daily_budget_microusd"] == 180_000_000
    assert response.json()["reserved_last_day_microusd"] == 150_000_000
    assert response.json()["next_budget_slot_at"] is None
    retry, _, _ = await failed_attempt(client)
    assert retry["seed"] == original["seed"]
    assert retry["agent_id"] == original["agent_id"]
    # Even an independently authorized second retry cannot reserve a seventh slot.
    response = await client.post(
        BASE + "/authorize-retry", headers=HEADERS, json=second_approval
    )
    assert response.status_code == 200, response.text
    assert response.json()["reserved_last_day_microusd"] == 180_000_000
    assert response.json()["next_budget_slot_at"] is not None
    assert (await client.post(BASE + "/claim", headers=HEADERS)).json() is None
    async with session_maker() as db, db.begin():
        parent = await db.get(ConversationAssessment, UUID(original["assessment_id"]))
        parent.created_at = datetime.now(UTC) - timedelta(days=1, seconds=1)
    retried = (await client.post(BASE + "/claim", headers=HEADERS)).json()
    assert retried["seed"] == second["seed"]
    listing = (await client.get(BASE, headers=HEADERS)).json()
    assert listing["reserved_last_day_microusd"] == 180_000_000
    assert listing["current_submission_fee_rao"] == 40_000_000
    assert len(listing["items"]) == 7
    assert all(item["reserved_microusd"] == 30_000_000 for item in listing["items"])


@pytest.mark.parametrize("guard", ["off", "rank", "image", "expired", "changed_report"])
async def test_authorized_retry_rechecks_admission_guards(
    app, client, session_maker, monkeypatch, guard
):
    entries = await install(app, session_maker, monkeypatch, count=1)
    original, _, approval = await failed_attempt(client)
    assert (
        await client.post(BASE + "/authorize-retry", headers=HEADERS, json=approval)
    ).status_code == 200
    if guard == "off":
        await client.post(
            BASE + "/settings",
            headers=HEADERS,
            json={
                "mode": "off",
                "expected_revision": 0,
                "actor": "operator@example.com",
                "reason": "Stop paid shadow claims",
                "confirmation": "APPLY CONVERSATION SHADOW SETTINGS",
            },
        )
    elif guard == "rank":
        entries[0].rank = 6
    else:
        async with session_maker() as db, db.begin():
            parent = await db.get(
                ConversationAssessment, UUID(original["assessment_id"])
            )
            if guard == "image":
                agent = await db.get(Agent, entries[0].agent_id)
                agent.screened_image_sha256 = "c" * 64
            elif guard == "expired":
                parent.retry_authorization = {
                    **parent.retry_authorization,
                    "expires_at": (
                        datetime.now(UTC) - timedelta(seconds=1)
                    ).isoformat(),
                }
            else:
                parent.report = {**parent.report, "output_tokens": 999}
    assert (await client.post(BASE + "/claim", headers=HEADERS)).json() is None
