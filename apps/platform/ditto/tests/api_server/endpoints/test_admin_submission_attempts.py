"""Real paid-ledger/owner-history operator reads; no admission writes."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.storage import ObjectDownloadFailedError
from ditto.db.models import (
    Agent,
    EvaluationPayment,
    OwnerAttestation,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ValidatorTicket,
)
from ditto.tests.submission_attempt_fixtures import archive, paid_submission, source

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
TOKEN = "test-attempt-read-token-at-least-32-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/api/v1/admin/submission-attempts"


@pytest.fixture
async def observations(app, session_maker):
    async def sessions():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    app.state.config = replace(
        app.state.config, admin_api_token=TOKEN, commit_hash="exact-build"
    )
    objects = {}
    storage = MagicMock()

    async def download(*, key, max_bytes):
        assert max_bytes == 2 * 1024 * 1024
        return objects[key]

    storage.get_object = AsyncMock(side_effect=download)
    app.dependency_overrides[get_storage_client] = lambda: storage
    async with session_maker() as session, session.begin():
        data = archive({"main.py": source("memory")})
        prior = paid_submission(
            session, data, coldkey="owner", created_at=NOW - timedelta(hours=1)
        )
        current = paid_submission(session, data, coldkey="owner", created_at=NOW)
        await session.flush()
        objects[f"{prior.agent_id}/agent.tar.gz"] = data
        objects[f"{current.agent_id}/agent.tar.gz"] = data
        ids = prior.agent_id, current.agent_id
    return {"prior": ids[0], "current": ids[1], "objects": objects, "storage": storage}


async def test_auth_and_no_write_routes(client, observations):
    assert (await client.get(BASE)).status_code == 401
    policy = await client.get(BASE, headers=HEADERS)
    assert policy.status_code == 200
    assert policy.headers["cache-control"] == "no-store"
    assert policy.json()["source_build"] == "exact-build"
    assert policy.json()["admission_effect"] == "none"
    assert (await client.post(BASE, headers=HEADERS, json={})).status_code == 405
    assert (
        await client.post("/api/v1/upload/check-artifact", json={})
    ).status_code == 404
    observations["storage"].get_object.assert_not_awaited()


async def test_paid_pair_reads_only_two_objects_and_writes_nothing(
    client, observations, session_maker
):
    for _ in range(2):
        response = await client.get(
            f"{BASE}/{observations['current']}", headers=HEADERS
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["classification"] == "small_source_delta"
        assert body["reference_agent_id"] == str(observations["prior"])
        assert not body["policy"]["source_clearance"]
        assert not body["policy"]["integrity_clearance"]
        assert "profile" not in body and "fingerprint" not in body
    assert observations["storage"].get_object.await_count == 4
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 2
        assert (
            await session.scalar(select(func.count()).select_from(EvaluationPayment))
            == 2
        )
        assert (
            await session.scalar(select(func.count()).select_from(ScreeningAttempt))
            == 0
        )
        assert {
            a.status.value for a in (await session.scalars(select(Agent))).all()
        } == {"uploaded"}


@pytest.mark.parametrize(
    "mutation", ["tampered", "missing", "oversized", "unknown_size"]
)
async def test_unavailable_artifact_is_inconclusive(
    client, observations, session_maker, mutation
):
    if mutation == "tampered":
        observations["objects"][f"{observations['current']}/agent.tar.gz"] = b"tampered"
    elif mutation == "missing":
        observations["storage"].get_object.side_effect = ObjectDownloadFailedError(
            "not available"
        )
    else:
        async with session_maker() as session, session.begin():
            agent = await session.get(Agent, observations["current"])
            agent.size_bytes = 2 * 1024 * 1024 + 1 if mutation == "oversized" else None
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["classification"] == "inconclusive"
    if mutation in {"oversized", "unknown_size"}:
        observations["storage"].get_object.assert_not_awaited()


async def test_unpaid_candidate_and_reference_do_not_infer_payment(
    client, observations, session_maker
):
    async with session_maker() as session, session.begin():
        unpaid = paid_submission(
            session,
            b"unused",
            coldkey="owner",
            created_at=NOW - timedelta(minutes=10),
            paid=False,
        )
        await session.flush()
        unpaid_id = unpaid.agent_id
    assert (await client.get(f"{BASE}/{unpaid_id}", headers=HEADERS)).status_code == 404
    response = await client.get(
        f"{BASE}/{observations['current']}?reference_agent_id={unpaid_id}",
        headers=HEADERS,
    )
    assert response.json()["classification"] == "inconclusive"
    observations["storage"].get_object.assert_not_awaited()


def attestation(
    *,
    lo="owner",
    hi="other",
    kind="coldkey",
    created_at=NOW - timedelta(minutes=30),
    revoked_at=None,
):
    return OwnerAttestation(
        netuid=expected_netuid(),
        hotkey_lo=uuid4().hex + "a",
        hotkey_hi="z" + uuid4().hex,
        nonce=uuid4(),
        issued_at=created_at,
        created_at=created_at,
        lo_key_kind=kind,
        hi_key_kind=kind,
        lo_signer=lo,
        hi_signer=hi,
        lo_signature="a" * 128,
        hi_signature="b" * 128,
        revoked_at=revoked_at,
        revoked_by="operator" if revoked_at else None,
    )


@pytest.mark.parametrize(
    "link,matched",
    [
        ("none", False),
        ("direct", True),
        ("hotkey", False),
        ("mixed", False),
        ("transitive", False),
        ("future", False),
        ("revoked_before", False),
        ("revoked_after", True),
        ("wrong_netuid", False),
    ],
)
async def test_only_direct_coldkey_links_at_candidate_timestamp(
    client, observations, session_maker, link, matched
):
    async with session_maker() as session, session.begin():
        current = await session.get(Agent, observations["current"])
        prior = await session.get(Agent, observations["prior"])
        # Hotkey reuse does not establish payer identity.
        other = paid_submission(
            session,
            observations["objects"][f"{prior.agent_id}/agent.tar.gz"],
            coldkey="other",
            created_at=NOW - timedelta(minutes=20),
            hotkey=current.miner_hotkey,
        )
        await session.flush()
        other_id = other.agent_id
        if link == "transitive":
            session.add(attestation(hi="middle"))
            session.add(attestation(lo="middle", hi="other"))
        elif link != "none":
            row = attestation(
                kind="hotkey" if link == "hotkey" else "coldkey",
                created_at=NOW + timedelta(seconds=1)
                if link == "future"
                else NOW - timedelta(minutes=30),
                revoked_at=NOW - timedelta(seconds=1)
                if link == "revoked_before"
                else NOW + timedelta(seconds=1)
                if link == "revoked_after"
                else None,
            )
            if link == "mixed":
                row.hi_key_kind = "hotkey"
            if link == "wrong_netuid":
                row.netuid += 1
            session.add(row)
    observations["objects"][f"{other_id}/agent.tar.gz"] = observations["objects"][
        f"{observations['prior']}/agent.tar.gz"
    ]
    response = await client.get(
        f"{BASE}/{observations['current']}?reference_agent_id={other_id}",
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["classification"] == (
        "small_source_delta" if matched else "inconclusive"
    )
    assert observations["storage"].get_object.await_count == (2 if matched else 0)


@pytest.mark.parametrize(
    "status,reason,future,expected",
    [
        ("failed", "docker-build-infrastructure", False, "infrastructure_retry"),
        ("failed", "docker-build-infrastructure", True, "small_source_delta"),
        ("expired", "worker-lost", False, "small_source_delta"),
        ("failed", "model-reject", False, "small_source_delta"),
    ],
)
async def test_feedback_cutoff_and_expiry_never_infers_infrastructure(
    client, observations, session_maker, status, reason, future, expected
):
    finished = NOW + timedelta(minutes=1) if future else NOW - timedelta(minutes=1)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=uuid4(),
                agent_id=observations["prior"],
                screener_hotkey="worker",
                policy_version=13,
                status=status,
                started_at=NOW - timedelta(minutes=30),
                deadline=NOW + timedelta(minutes=30),
                finished_at=finished,
                reason_code=reason,
            )
        )
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["classification"] == expected


async def test_packaging_change_after_infrastructure_failure_is_not_a_retry(
    client, observations, session_maker
):
    archives = {
        "prior": archive({"main.py": source("memory"), "Dockerfile": b"FROM a"}),
        "current": archive({"main.py": source("memory"), "Dockerfile": b"FROM b"}),
    }
    async with session_maker() as session, session.begin():
        for key, data in archives.items():
            agent = await session.get(Agent, observations[key])
            agent.sha256 = hashlib.sha256(data).hexdigest()
            agent.size_bytes = len(data)
            observations["objects"][f"{agent.agent_id}/agent.tar.gz"] = data
        session.add(
            ScreeningAttempt(
                attempt_id=uuid4(),
                agent_id=observations["prior"],
                screener_hotkey="worker",
                policy_version=13,
                status="failed",
                started_at=NOW - timedelta(minutes=30),
                deadline=NOW + timedelta(minutes=30),
                finished_at=NOW - timedelta(minutes=1),
                reason_code="docker-build-infrastructure",
            )
        )
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["classification"] == "packaging_only_repair"
    assert body["feedback_status"] == "infrastructure"
    assert body["feedback_reason"] == "docker-build-infrastructure"


@pytest.mark.parametrize(
    "status,hold,rulings,expected",
    [
        ("passed", None, [], ("completed", None, -40)),
        ("quarantined", None, [], ("pending", "source-review-flagged", -40)),
        ("quarantined", "active", [], ("pending", "source-review-flagged", -40)),
        (
            "quarantined",
            "resolved",
            [("release", -10)],
            ("completed", "operator-released-quarantine", -10),
        ),
        (
            "quarantined",
            "resolved",
            [("reject", -10)],
            ("completed", "operator-rejected-quarantine", -10),
        ),
        (
            "quarantined",
            "resolved",
            [("rescreen", -10)],
            ("pending", "operator-rescreened-quarantine", -10),
        ),
        # Rulings after the candidate's timestamp were not feedback yet.
        (
            "quarantined",
            "resolved",
            [("release", 10)],
            ("pending", "source-review-flagged", -40),
        ),
        (
            "quarantined",
            "resolved",
            [("reject", -10), ("release", 10)],
            ("completed", "operator-rejected-quarantine", -10),
        ),
    ],
)
async def test_quarantine_feedback_follows_rulings_at_candidate_timestamp(
    client, observations, session_maker, status, hold, rulings, expected
):
    attempt_id = uuid4()
    reason = None if status == "passed" else "source-review-flagged"
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=observations["prior"],
                screener_hotkey="worker",
                policy_version=13,
                status=status,
                started_at=NOW - timedelta(minutes=50),
                deadline=NOW + timedelta(minutes=30),
                finished_at=NOW - timedelta(minutes=40),
                reason_code=reason,
            )
        )
        if hold is not None:
            await session.flush()
            quarantine_id = uuid4()
            last = rulings[-1] if rulings else None
            session.add(
                ScreeningQuarantine(
                    quarantine_id=quarantine_id,
                    agent_id=observations["prior"],
                    attempt_id=attempt_id,
                    screener_hotkey="worker",
                    policy_version=13,
                    manifest_digest="a" * 64,
                    reason_code="source-review-flagged",
                    status=hold,
                    created_at=NOW - timedelta(minutes=40),
                    resolved_at=NOW + timedelta(minutes=last[1]) if last else None,
                    resolved_by="operator" if last else None,
                    resolution=last[0] if last else None,
                )
            )
            await session.flush()
            for resolution, minutes in rulings:
                session.add(
                    ScreeningQuarantineResolution(
                        resolution_id=uuid4(),
                        quarantine_id=quarantine_id,
                        resolution=resolution,
                        reason="operator ruling",
                        actor="operator",
                        created_at=NOW + timedelta(minutes=minutes),
                    )
                )
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    feedback_status, feedback_reason, minutes = expected
    assert body["feedback_status"] == feedback_status
    assert body["feedback_reason"] == feedback_reason
    assert datetime.fromisoformat(body["feedback_at"]) == NOW + timedelta(
        minutes=minutes
    )
    # Feedback is context only: an identical runtime is still the same delta.
    assert body["classification"] == "small_source_delta"


async def test_no_prior_submission_and_timestamp_ties(
    client, observations, session_maker
):
    first = await client.get(f"{BASE}/{observations['prior']}", headers=HEADERS)
    assert first.json()["classification"] == "first_submission"
    async with session_maker() as session, session.begin():
        tie = paid_submission(session, b"unused", coldkey="owner", created_at=NOW)
        await session.flush()
        tie_id = tie.agent_id
    # The default reference is strictly earlier, so a tie is skipped rather
    # than selected and then refused.
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["classification"] == "small_source_delta"
    assert response.json()["reference_agent_id"] == str(observations["prior"])
    # An explicitly supplied tie is still refused by the endpoint guard.
    explicit = await client.get(
        f"{BASE}/{observations['current']}?reference_agent_id={tie_id}",
        headers=HEADERS,
    )
    assert explicit.json()["classification"] == "inconclusive"
    assert "earlier submission" in explicit.json()["reason"]
    # Equally old earlier predecessors resolve by agent id, not by scan order.
    data = observations["objects"][f"{observations['prior']}/agent.tar.gz"]
    async with session_maker() as session, session.begin():
        earlier = [
            paid_submission(
                session, data, coldkey="owner", created_at=NOW - timedelta(minutes=30)
            )
            for _ in range(3)
        ]
        await session.flush()
        latest_id = max(agent.agent_id for agent in earlier)
    for agent in earlier:
        observations["objects"][f"{agent.agent_id}/agent.tar.gz"] = data
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["reference_agent_id"] == str(latest_id)


@pytest.mark.parametrize(
    "reason,purpose,expected",
    [
        ("infrastructure", TicketPurpose.CANONICAL_QUORUM, "infrastructure_retry"),
        ("scoring_error", TicketPurpose.CANONICAL_QUORUM, "small_source_delta"),
        ("sandbox_oom", TicketPurpose.CANONICAL_QUORUM, "small_source_delta"),
        ("infrastructure", TicketPurpose.BENCHMARK_CANARY, "small_source_delta"),
    ],
)
async def test_only_canonical_infrastructure_failures_qualify(
    client, observations, session_maker, reason, purpose, expected
):
    async with session_maker() as session, session.begin():
        session.add(
            ValidatorTicket(
                agent_id=observations["prior"],
                validator_hotkey="validator",
                bench_version=13,
                purpose=purpose,
                status=TicketStatus.EXPIRED,
                issued_at=NOW - timedelta(minutes=30),
                deadline=NOW + timedelta(minutes=30),
                failed_at=NOW - timedelta(minutes=1),
                failure_reason=reason,
            )
        )
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["classification"] == expected


async def test_excess_owner_links_are_inconclusive_before_download(
    client, observations, session_maker, monkeypatch
):
    from ditto.db.queries import submission_attempts

    monkeypatch.setattr(submission_attempts, "MAX_OWNER_LINKS", 1)
    async with session_maker() as session, session.begin():
        session.add(attestation(hi="one"))
        session.add(attestation(hi="two"))
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.json()["classification"] == "inconclusive"
    assert "owner-link budget" in response.json()["reason"]
    observations["storage"].get_object.assert_not_awaited()


@pytest.mark.parametrize("extra", ["second_hotkey_pair", "same_coldkey_link"])
async def test_owner_link_budget_counts_distinct_peers(
    client, observations, session_maker, monkeypatch, extra
):
    from ditto.db.queries import submission_attempts

    monkeypatch.setattr(submission_attempts, "MAX_OWNER_LINKS", 1)
    async with session_maker() as session, session.begin():
        # One active link per hotkey pair, so one coldkey pair can hold several
        # rows; a link between two hotkeys of one coldkey names no new peer.
        session.add(attestation(hi="other"))
        session.add(
            attestation(hi="other")
            if extra == "second_hotkey_pair"
            else attestation(lo="owner", hi="owner")
        )
    response = await client.get(f"{BASE}/{observations['current']}", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["classification"] == "small_source_delta"
    assert response.json()["reference_agent_id"] == str(observations["prior"])
