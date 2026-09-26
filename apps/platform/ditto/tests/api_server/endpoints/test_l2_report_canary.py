"""Postgres regressions for the separate, exact-attempt L2 audit queue."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.l2_report_canary import (
    L2CanaryClaimRequest,
    L2CanaryCompleteRequest,
    L2CanaryScheduleRequest,
)
from ditto.api_server.endpoints import l2_report_canary as endpoints
from ditto.api_server.storage import S3StorageClient
from ditto.api_server.storage.models import VerifiedObject
from ditto.db.models import (
    Agent,
    AthReview,
    AthReviewAction,
    ScreenerHeartbeat,
    ScreenerL2ReportCanary,
    ScreenerNode,
    ScreeningAttempt,
    ScreeningReviewEvent,
)
from ditto.tests.api_server.endpoints.test_screener import _seed_agent, _seed_score
from ditto_screening_protocol import ScoredRuntimeEvidenceLease


def test_l2_canary_schedule_accepts_uuid_strings_from_http_json() -> None:
    request_id, agent_id, attempt_id = uuid4(), uuid4(), uuid4()
    payload = L2CanaryScheduleRequest.model_validate(
        {
            "request_id": str(request_id),
            "agent_id": str(agent_id),
            "source_attempt_id": str(attempt_id),
            "artifact_sha256": "a" * 64,
            "policy_version": 13,
            "expected_agent_status": "screening_failed",
            "expected_score_count": 0,
            "target_node_id": "subnet-screener-1",
            "review_label": "candidate_clear",
            "confirm_report_only": True,
        }
    )
    assert (payload.request_id, payload.agent_id, payload.source_attempt_id) == (
        request_id,
        agent_id,
        attempt_id,
    )


def test_historical_canary_request_requires_matching_source_only_ruling() -> None:
    fields = {
        "request_id": str(uuid4()),
        "agent_id": str(uuid4()),
        "source_attempt_id": str(uuid4()),
        "artifact_sha256": "a" * 64,
        "policy_version": 13,
        "expected_agent_status": "live",
        "expected_score_count": 1,
        "target_node_id": "subnet-screener-1",
        "review_label": "candidate_clear",
        "confirm_report_only": True,
    }
    with pytest.raises(ValueError, match="kind and id"):
        L2CanaryScheduleRequest.model_validate(
            {**fields, "historical_ruling_kind": "ath_clear"}
        )
    with pytest.raises(ValueError, match="source-only review label"):
        L2CanaryScheduleRequest.model_validate(
            {
                **fields,
                "run_mode": "full_runtime",
                "historical_ruling_kind": "ath_clear",
                "historical_ruling_id": str(uuid4()),
            }
        )
    with pytest.raises(ValueError, match="source-only review label"):
        L2CanaryScheduleRequest.model_validate(
            {
                **fields,
                "historical_ruling_kind": "screening_reject",
                "historical_ruling_id": str(uuid4()),
            }
        )


@pytest.mark.asyncio
async def test_historical_ruling_matches_exact_source() -> None:
    ruling_id, action_id, agent_id, attempt_id = uuid4(), uuid4(), uuid4(), uuid4()
    sha = "a" * 64
    now = datetime.now(UTC)
    row = cast(
        ScreenerL2ReportCanary,
        SimpleNamespace(
            agent_id=agent_id,
            source_attempt_id=attempt_id,
            artifact_sha256=sha,
            policy_version=13,
            review_label="candidate_clear",
            source_attestation={
                "kind": "ath_clear",
                "ruling_id": str(ruling_id),
                "action_id": str(action_id),
            },
        ),
    )
    ruling = SimpleNamespace(
        agent_id=agent_id,
        original_policy_version=13,
        status="resolved",
        resolution="clear",
        resolved_at=now,
        resolved_by="human-reviewer",
        resolution_reason="Exact artifact independently cleared",
        original_evidence={"sha256": sha},
    )
    action = SimpleNamespace(
        action_id=action_id,
        action="clear",
        created_at=now,
        actor="human-reviewer",
        reason="Exact artifact independently cleared",
    )
    session = cast(
        AsyncSession,
        SimpleNamespace(
            get=AsyncMock(return_value=ruling),
            scalar=AsyncMock(return_value=action),
        ),
    )
    assert await endpoints._historical_ruling_matches(session, row)
    session.get.assert_awaited_with(AthReview, ruling_id)  # type: ignore[attr-defined]
    action.action_id = uuid4()  # Same review was reopened and cleared again.
    assert not await endpoints._historical_ruling_matches(session, row)
    action.action_id = action_id
    ruling.original_evidence["sha256"] = "b" * 64
    assert not await endpoints._historical_ruling_matches(session, row)
    ruling.original_evidence["sha256"] = sha
    ruling.agent_id = uuid4()
    assert not await endpoints._historical_ruling_matches(session, row)

    row.review_label = "known_reject"
    assert row.source_attestation is not None
    row.source_attestation["kind"] = "screening_reject"
    event = SimpleNamespace(
        agent_id=agent_id,
        attempt_id=attempt_id,
        policy_version=13,
        event_kind="manual",
        outcome="reject",
        effective_decision="reject",
        artifact_sha256=sha,
    )
    session.get.return_value = event  # type: ignore[attr-defined]
    assert await endpoints._historical_ruling_matches(session, row)
    session.get.assert_awaited_with(  # type: ignore[attr-defined]
        ScreeningReviewEvent, ruling_id
    )
    event.attempt_id = uuid4()
    assert not await endpoints._historical_ruling_matches(session, row)


@pytest.mark.asyncio
async def test_current_object_attestation_rejects_replacement() -> None:
    agent_id = uuid4()
    agent = cast(Agent, SimpleNamespace(agent_id=agent_id, size_bytes=123))
    storage = cast(
        S3StorageClient,
        SimpleNamespace(
            verify_object_sha256=AsyncMock(
                return_value=VerifiedObject(size_bytes=123, sha256="a" * 64)
            )
        ),
    )
    assert await endpoints._current_object_matches(storage, agent, "a" * 64) == (
        True,
        123,
    )
    storage.verify_object_sha256.assert_awaited_with(  # type: ignore[attr-defined]
        key=f"{agent_id}/agent.tar.gz", expected_size_bytes=123
    )
    storage.verify_object_sha256.return_value = VerifiedObject(  # type: ignore[attr-defined]
        size_bytes=123, sha256="b" * 64
    )
    matches, _ = await endpoints._current_object_matches(storage, agent, "a" * 64)
    assert matches is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "object_drift", "ruling_replaced"])
async def test_null_sha_historical_clear_replay_rehashes_at_claim(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    sha = "a" * 64
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCORED, sha256=sha)
    await _seed_score(session_maker, agent_id=agent_id)
    attempt_id, ruling_id, request_id = uuid4(), uuid4(), uuid4()
    node_id = f"canary-test-{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add_all(
            [
                ScreenerNode(
                    environment="prod",
                    node_id=node_id,
                    provider="hetzner",
                    provider_resource_id=node_id,
                    screener_hotkey=f"hotkey-{node_id}",
                    token_hash="f" * 64,
                    token_expires_at=now + timedelta(hours=1),
                    status="active",
                    capacity=1,
                ),
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    artifact_sha256=None,
                    screener_hotkey=f"hotkey-{node_id}",
                    policy_version=13,
                    status="passed",
                    started_at=now - timedelta(minutes=1),
                    deadline=now,
                    finished_at=now,
                ),
                AthReview(
                    review_id=ruling_id,
                    agent_id=agent_id,
                    status="resolved",
                    resolved_at=now,
                    resolved_by="human-reviewer",
                    resolution="clear",
                    resolution_reason="Exact artifact independently cleared",
                    original_policy_version=13,
                    original_evidence={"sha256": sha},
                    algorithm_provenance={},
                ),
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=ruling_id,
                    action="clear",
                    reason="Exact artifact independently cleared",
                    actor="human-reviewer",
                    evidence={},
                    created_at=now,
                ),
            ]
        )
    monkeypatch.setattr(endpoints, "arrival_bench_version", AsyncMock(return_value=13))
    storage = cast(
        S3StorageClient,
        SimpleNamespace(
            verify_object_sha256=AsyncMock(
                return_value=VerifiedObject(size_bytes=123, sha256=sha)
            ),
            presigned_get_url=AsyncMock(return_value="https://example.test/source"),
        ),
    )
    payload = L2CanaryScheduleRequest(
        request_id=request_id,
        agent_id=agent_id,
        source_attempt_id=attempt_id,
        artifact_sha256=sha,
        policy_version=13,
        expected_agent_status="scored",
        expected_score_count=1,
        target_node_id=node_id,
        review_label="candidate_clear",
        historical_ruling_kind="ath_clear",
        historical_ruling_id=ruling_id,
        confirm_report_only=True,
    )
    async with session_maker() as session:
        scheduled = await endpoints.schedule_l2_report_canary(
            payload, None, session, storage, "operator@example.com"
        )
    assert scheduled.source_attestation is not None
    assert scheduled.source_attestation["scope"].startswith("current-object-only")
    assert scheduled.source_attestation["actor"] == "operator@example.com"
    assert storage.verify_object_sha256.await_count == 1  # type: ignore[attr-defined]
    if change == "object_drift":
        storage.verify_object_sha256.return_value = VerifiedObject(  # type: ignore[attr-defined]
            size_bytes=123, sha256="b" * 64
        )
    elif change == "ruling_replaced":
        later = now + timedelta(seconds=1)
        async with session_maker() as session, session.begin():
            review = await session.get(AthReview, ruling_id, with_for_update=True)
            assert review is not None
            review.resolved_at = later
            review.resolved_by = "second-reviewer"
            review.resolution_reason = "A newer independent clear ruling"
            session.add(
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=ruling_id,
                    action="clear",
                    reason="A newer independent clear ruling",
                    actor="second-reviewer",
                    evidence={},
                    created_at=later,
                )
            )
    monkeypatch.setattr(
        endpoints,
        "_resolve_effective_review_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                revision=136,
                checksum="d" * 64,
                settings=SimpleNamespace(
                    source_review_timeout_seconds=3600, timeout_seconds=1800
                ),
            )
        ),
    )
    monkeypatch.setattr(
        endpoints,
        "scored_runtime_evidence_for_lease",
        AsyncMock(return_value=_packet(attempt_id, sha)),
    )
    request = cast(
        Request, SimpleNamespace(state=SimpleNamespace(screener_node_id=node_id))
    )
    async with session_maker() as session:
        claimed = await endpoints.claim_l2_report_canary(
            L2CanaryClaimRequest(
                instance_id=node_id + "-worker-1",
                settings_revision=136,
                settings_checksum="d" * 64,
            ),
            request,
            Response(),
            "hotkey",
            session,
            storage,
        )
    assert (claimed is None) == (change != "none")
    async with session_maker() as session:
        row = await session.get(ScreenerL2ReportCanary, scheduled.canary_id)
        agent = await session.get(Agent, agent_id)
        attempt = await session.get(ScreeningAttempt, attempt_id)
    assert row is not None
    assert row.status == ("leased" if change == "none" else "incomplete")
    assert (
        row.error_code
        == {
            "none": None,
            "object_drift": "source-object-drift",
            "ruling_replaced": "exact-source-changed",
        }[change]
    )
    assert row.report is None
    assert agent is not None and agent.status == AgentStatus.SCORED
    assert attempt is not None and attempt.artifact_sha256 is None
    if change == "none":
        assert claimed is not None
        assert claimed.artifact_sha256 == sha
        assert storage.verify_object_sha256.await_count == 2  # type: ignore[attr-defined]
        assert storage.presigned_get_url.await_count == 1  # type: ignore[attr-defined]


def _packet(attempt_id, sha: str) -> ScoredRuntimeEvidenceLease:
    revision = "a" * 40
    keys = ("SAFE_KEY",)
    digest = hashlib.sha256(
        ("scored-runtime-env-v1\n13\n" + revision + "\n" + "\n".join(keys)).encode()
    ).hexdigest()
    return ScoredRuntimeEvidenceLease(
        attempt_id=attempt_id,
        artifact_sha256=sha,
        policy_version=13,
        bench_version=13,
        scorer_source_revision=revision,
        release_descriptor_digest="sha256:" + "b" * 64,
        scorer_image_digest="sha256:" + "c" * 64,
        scorer_env_sha256=digest,
        injected_keys=keys,
        validator_count=3,
        observed_at=int(datetime.now(UTC).timestamp()),
    )


@pytest.mark.asyncio
async def test_source_only_claims_use_one_lease_per_fresh_worker_and_expire_cleanly(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 64
    agent_id = await _seed_agent(session_maker, status=AgentStatus.REJECTED, sha256=sha)
    node_id = f"canary-parallel-{uuid4().hex[:12]}"
    hotkey = f"hotkey-{node_id}"
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id=node_id,
                provider="hetzner",
                provider_resource_id=node_id,
                screener_hotkey=hotkey,
                token_hash="f" * 64,
                token_expires_at=now + timedelta(hours=1),
                status="active",
                capacity=1,
            )
        )
        for worker in range(1, 5):
            session.add(
                ScreenerHeartbeat(
                    screener_hotkey=hotkey,
                    instance_id=f"{node_id}-worker-{worker}",
                    software_version="0.319.0",
                    protocol_version=7,
                    policy_version=13,
                    state="polling",
                    reported_at=now,
                    seen_at=now,
                    signature="f" * 128,
                )
            )
        attempt_ids = [uuid4() for _ in range(5)]
        for attempt_id in attempt_ids:
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    artifact_sha256=sha,
                    screener_hotkey=hotkey,
                    policy_version=13,
                    status="rejected",
                    started_at=now - timedelta(minutes=1),
                    deadline=now,
                    finished_at=now,
                )
            )
        await session.flush()
        for attempt_id in attempt_ids:
            session.add(
                ScreenerL2ReportCanary(
                    canary_id=uuid4(),
                    request_id=uuid4(),
                    agent_id=agent_id,
                    source_attempt_id=attempt_id,
                    artifact_sha256=sha,
                    policy_version=13,
                    bench_version=13,
                    target_node_id=node_id,
                    expected_agent_status="rejected",
                    expected_score_count=0,
                    review_label="known_reject",
                    run_mode="source_only",
                    status="queued",
                )
            )

    monkeypatch.setattr(
        endpoints,
        "_resolve_effective_review_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                revision=137,
                checksum="d" * 64,
                settings=SimpleNamespace(
                    source_review_timeout_seconds=3600, timeout_seconds=1800
                ),
            )
        ),
    )

    async def evidence_lookup(_session: AsyncSession, *, attempt_id, **_kwargs):
        return _packet(attempt_id, sha)

    monkeypatch.setattr(endpoints, "scored_runtime_evidence_for_lease", evidence_lookup)
    storage = cast(
        S3StorageClient,
        SimpleNamespace(
            presigned_get_url=AsyncMock(return_value="https://example.test/source")
        ),
    )
    request = cast(
        Request, SimpleNamespace(state=SimpleNamespace(screener_node_id=node_id))
    )

    async def claim(worker: int):
        async with session_maker() as session:
            return await endpoints.claim_l2_report_canary(
                L2CanaryClaimRequest(
                    instance_id=f"{node_id}-worker-{worker}",
                    settings_revision=137,
                    settings_checksum="d" * 64,
                ),
                request,
                Response(),
                hotkey,
                session,
                storage,
            )

    claims = await asyncio.gather(*(claim(worker) for worker in (1, 2, 3, 4, 1)))
    assert len([claim for claim in claims if claim is not None]) == 4
    assert await claim(5) is None  # A fifth worker has no fresh heartbeat.
    async with session_maker() as session, session.begin():
        leased = list(
            await session.scalars(
                select(ScreenerL2ReportCanary).where(
                    ScreenerL2ReportCanary.target_node_id == node_id,
                    ScreenerL2ReportCanary.status == "leased",
                )
            )
        )
        assert {row.claimed_instance_id for row in leased} == {
            f"{node_id}-worker-{worker}" for worker in range(1, 5)
        }
        assert len(leased) == 4
        assert all(row.run_mode == "source_only" for row in leased)
        next(
            row for row in leased if row.claimed_instance_id == f"{node_id}-worker-1"
        ).lease_expires_at = now - timedelta(seconds=1)
        heartbeat = await session.scalar(
            select(ScreenerHeartbeat).where(
                ScreenerHeartbeat.screener_hotkey == hotkey,
                ScreenerHeartbeat.instance_id == f"{node_id}-worker-1",
            )
        )
        assert heartbeat is not None
        heartbeat.seen_at = now - timedelta(minutes=6)
    assert await claim(1) is None  # Expired lease does not admit a stale worker.
    async with session_maker() as session, session.begin():
        heartbeat = await session.scalar(
            select(ScreenerHeartbeat).where(
                ScreenerHeartbeat.screener_hotkey == hotkey,
                ScreenerHeartbeat.instance_id == f"{node_id}-worker-1",
            )
        )
        assert heartbeat is not None
        heartbeat.seen_at = datetime.now(UTC)
    assert await claim(1) is not None
    async with session_maker() as session:
        statuses = list(
            await session.scalars(
                select(ScreenerL2ReportCanary.status).where(
                    ScreenerL2ReportCanary.target_node_id == node_id
                )
            )
        )
    assert statuses.count("leased") == 4
    assert statuses.count("expired") == 1


@pytest.mark.parametrize(
    ("timeout", "l2_timeout", "run_mode", "expected_seconds"),
    [
        (600, 1200, "source_only", 45 * 60),
        (3600, 1800, "source_only", 100 * 60),
        (3600, 1800, "full_runtime", 150 * 60),
    ],
)
def test_report_only_lease_covers_review_and_bounded_preparation(
    timeout: int, l2_timeout: int, run_mode: str, expected_seconds: int
) -> None:
    assert endpoints._canary_lease(
        source_review_timeout_seconds=timeout,
        l2_timeout_seconds=l2_timeout,
        run_mode=run_mode,
    ) == timedelta(seconds=expected_seconds)


@pytest.mark.asyncio
async def test_full_runtime_claim_requires_exact_adopted_worker() -> None:
    node = cast(
        ScreenerNode,
        SimpleNamespace(node_id="subnet-screener-1", screener_hotkey="hotkey"),
    )
    current = datetime.now(UTC)
    release = {
        "builtin_policy_version": 13,
        "revision": "a" * 40,
        "version": "v0.317.2",
        "activated_at": int(current.timestamp()),
    }
    heartbeat = SimpleNamespace(
        instance_id="subnet-screener-1-worker-1",
        system_metrics={"release": release},
    )
    session = cast(
        AsyncSession, SimpleNamespace(scalars=AsyncMock(return_value=[heartbeat]))
    )
    assert await endpoints._full_runtime_worker_ready(
        session, node=node, now=current, instance_id=heartbeat.instance_id
    )
    assert not await endpoints._full_runtime_worker_ready(
        session, node=node, now=current, instance_id="subnet-screener-1-worker-2"
    )
    heartbeat.system_metrics = {"release": {**release, "version": "v0.317.1"}}
    assert not await endpoints._full_runtime_worker_ready(
        session, node=node, now=current, instance_id=heartbeat.instance_id
    )


@pytest.mark.asyncio
async def test_canary_preflight_exposes_scheduler_guard_values_without_mutation(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_sha, attempt_sha = "a" * 64, "b" * 64
    agent_id = await _seed_agent(
        session_maker, status=AgentStatus.EVALUATING, sha256=agent_sha
    )
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256=attempt_sha,
                screener_hotkey="preflight-test-hotkey",
                policy_version=13,
                status="passed",
                started_at=now - timedelta(minutes=1),
                deadline=now,
                finished_at=now,
            )
        )
    await _seed_score(session_maker, agent_id=agent_id)
    response = Response()
    async with session_maker() as session:
        view = await endpoints.get_l2_report_canary_preflight(
            agent_id, attempt_id, response, None, session
        )
    assert response.headers["Cache-Control"] == "no-store"
    assert view.agent_id == agent_id
    assert view.source_attempt_id == attempt_id
    assert view.agent_artifact_sha256 == agent_sha
    assert view.source_attempt_artifact_sha256 == attempt_sha
    assert view.agent_status == "evaluating"
    assert view.attempt_policy_version == 13
    assert view.score_row_count == 1
    assert view.arrival_bench_version >= 1
    async with session_maker() as session:
        with pytest.raises(HTTPException) as error:
            await endpoints.get_l2_report_canary_preflight(
                uuid4(), attempt_id, Response(), None, session
            )
    assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_mode", "review_mode"),
    [
        ("source_only", "shadow"),
        ("full_runtime", "shadow"),  # An already leased older worker can finish.
        ("full_runtime", "enforce_preview"),
    ],
)
async def test_l2_canary_lease_duplicate_late_and_authority_isolation(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    run_mode: str,
    review_mode: str,
) -> None:
    sha = "a" * 64
    agent_id = await _seed_agent(session_maker, status=AgentStatus.REJECTED, sha256=sha)
    attempt_id, canary_id, request_id = uuid4(), uuid4(), uuid4()
    node_id = f"canary-test-{uuid4().hex[:12]}"
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id=node_id,
                provider="hetzner",
                provider_resource_id=node_id,
                screener_hotkey=f"hotkey-{node_id}",
                token_hash="f" * 64,
                token_expires_at=now + timedelta(hours=1),
                status="active",
                capacity=1,
            )
        )
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256=sha,
                screener_hotkey=f"hotkey-{node_id}",
                policy_version=13,
                status="rejected",
                started_at=now - timedelta(minutes=1),
                deadline=now,
                finished_at=now,
            )
        )
        await session.flush()
        session.add(
            ScreenerL2ReportCanary(
                canary_id=canary_id,
                request_id=request_id,
                agent_id=agent_id,
                source_attempt_id=attempt_id,
                artifact_sha256=sha,
                policy_version=13,
                bench_version=13,
                target_node_id=node_id,
                expected_agent_status="rejected",
                expected_score_count=0,
                review_label="known_reject",
                run_mode=run_mode,
                status="queued",
            )
        )
    packet = _packet(attempt_id, sha)
    if run_mode == "full_runtime":
        monkeypatch.setattr(
            endpoints, "_full_runtime_worker_ready", AsyncMock(return_value=True)
        )
    evidence_lookup = AsyncMock(return_value=packet)
    monkeypatch.setattr(endpoints, "scored_runtime_evidence_for_lease", evidence_lookup)
    monkeypatch.setattr(
        endpoints,
        "_resolve_effective_review_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                revision=124,
                checksum="d" * 64,
                settings=SimpleNamespace(
                    source_review_timeout_seconds=3600, timeout_seconds=1800
                ),
            )
        ),
    )
    request = cast(
        Request, SimpleNamespace(state=SimpleNamespace(screener_node_id=node_id))
    )
    storage = cast(
        S3StorageClient,
        SimpleNamespace(
            presigned_get_url=AsyncMock(return_value="https://example.test/source")
        ),
    )
    async with session_maker() as session:
        claim = await endpoints.claim_l2_report_canary(
            L2CanaryClaimRequest(
                instance_id=node_id + "-worker-1",
                settings_revision=124,
                settings_checksum="d" * 64,
            ),
            request,
            Response(),
            "hotkey",
            session,
            storage,
        )
    assert claim is not None
    assert evidence_lookup.await_args is not None
    assert evidence_lookup.await_args.kwargs["report_only_current_packet"] is True
    assert claim.source_attempt_id == attempt_id
    assert claim.run_mode == run_mode
    assert claim.scored_runtime_evidence == packet
    expected_lease = timedelta(minutes=150 if run_mode == "full_runtime" else 100)
    assert abs((claim.lease_expires_at - now - expected_lease).total_seconds()) < 30
    # A fresh second worker must not claim the opposite run mode while one is
    # leased. The source-only case also exercises the queued full-runtime gate.
    other_canary_id = uuid4()
    async with session_maker() as session, session.begin():
        other_attempt_id = uuid4()
        session.add(
            ScreeningAttempt(
                attempt_id=other_attempt_id,
                agent_id=agent_id,
                artifact_sha256=sha,
                screener_hotkey=f"hotkey-{node_id}",
                policy_version=13,
                status="rejected",
                started_at=now - timedelta(minutes=1),
                deadline=now,
                finished_at=now,
            )
        )
        for worker in (1, 2):
            session.add(
                ScreenerHeartbeat(
                    screener_hotkey=f"hotkey-{node_id}",
                    instance_id=f"{node_id}-worker-{worker}",
                    software_version="0.319.0",
                    protocol_version=7,
                    policy_version=13,
                    state="polling",
                    reported_at=now,
                    seen_at=now,
                    signature="f" * 128,
                )
            )
        await session.flush()
        session.add(
            ScreenerL2ReportCanary(
                canary_id=other_canary_id,
                request_id=uuid4(),
                agent_id=agent_id,
                source_attempt_id=other_attempt_id,
                artifact_sha256=sha,
                policy_version=13,
                bench_version=13,
                target_node_id=node_id,
                expected_agent_status="rejected",
                expected_score_count=0,
                review_label="known_reject",
                run_mode="full_runtime" if run_mode == "source_only" else "source_only",
                status="queued",
            )
        )
    monkeypatch.setattr(
        endpoints, "_full_runtime_worker_ready", AsyncMock(return_value=True)
    )
    async with session_maker() as session:
        view = await endpoints.get_l2_report_canary(claim.canary_id, None, session)
    assert view.lease_expires_at == claim.lease_expires_at
    async with session_maker() as session:
        second = await endpoints.claim_l2_report_canary(
            L2CanaryClaimRequest(
                instance_id=node_id + "-worker-2",
                settings_revision=124,
                settings_checksum="d" * 64,
            ),
            request,
            Response(),
            "hotkey",
            session,
            storage,
        )
    assert second is None
    async with session_maker() as session, session.begin():
        other = await session.get(ScreenerL2ReportCanary, other_canary_id)
        assert other is not None
        other.status = "incomplete"
        other.error_code = "test-opposite-mode"
        other.completed_at = datetime.now(UTC)
    report = {
        "kind": "l2_report_canary_v1",
        "authority": "none",
        "review_mode": review_mode,
        "canary_id": str(canary_id),
        "agent_id": str(agent_id),
        "source_attempt_id": str(attempt_id),
        "artifact_sha256": sha,
        "policy_version": 13,
        "run_mode": run_mode,
        "settings_revision": 124,
        "settings_checksum": "d" * 64,
        "scored_runtime_evidence": packet.model_dump(mode="json"),
        "l2": {"ok": True, "risk_level": "low"},
    }
    if run_mode == "full_runtime":
        report["challenge_status"] = "completed"
        report["challenge_evidence_codes"] = ["behavioral-oracle-passed"]
        report["decision_evidence_codes"] = ["behavioral-oracle-passed"]
    else:
        # A rolling old worker may still complete an already leased source-only run.
        del report["run_mode"]
    body = L2CanaryCompleteRequest(
        lease_token=claim.lease_token, status="succeeded", report=report
    )
    async with session_maker() as session:
        with pytest.raises(HTTPException) as bad_identity:
            await endpoints.complete_l2_report_canary(
                canary_id,
                body.model_copy(
                    update={"report": {**report, "authority": "screening"}}
                ),
                request,
                "hotkey",
                session,
            )
    assert bad_identity.value.status_code == 409
    if run_mode == "source_only":
        async with session_maker() as session:
            with pytest.raises(HTTPException) as wrong_review_mode:
                await endpoints.complete_l2_report_canary(
                    canary_id,
                    body.model_copy(
                        update={"report": {**report, "review_mode": "enforce_preview"}}
                    ),
                    request,
                    "hotkey",
                    session,
                )
        assert wrong_review_mode.value.status_code == 409
    if run_mode == "full_runtime":
        async with session_maker() as session:
            with pytest.raises(HTTPException) as wrong_mode:
                await endpoints.complete_l2_report_canary(
                    canary_id,
                    body.model_copy(
                        update={"report": {**report, "run_mode": "source_only"}}
                    ),
                    request,
                    "hotkey",
                    session,
                )
        assert wrong_mode.value.status_code == 409
        async with session_maker() as session:
            with pytest.raises(HTTPException) as false_challenge:
                await endpoints.complete_l2_report_canary(
                    canary_id,
                    body.model_copy(
                        update={"report": {**report, "challenge_status": "not_run"}}
                    ),
                    request,
                    "hotkey",
                    session,
                )
        assert false_challenge.value.status_code == 409
    async with session_maker() as session:
        await endpoints.complete_l2_report_canary(
            canary_id, body, request, "hotkey", session
        )
    async with session_maker() as session:
        await endpoints.complete_l2_report_canary(
            canary_id, body, request, "hotkey", session
        )
    async with session_maker() as session:
        with pytest.raises(HTTPException) as conflict:
            await endpoints.complete_l2_report_canary(
                canary_id,
                body.model_copy(
                    update={"report": {**report, "authority": "screening"}}
                ),
                request,
                "hotkey",
                session,
            )
    assert conflict.value.status_code == 409
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        row = await session.get(ScreenerL2ReportCanary, canary_id)
    assert attempt is not None and attempt.status == "rejected"
    assert row is not None and row.status == "succeeded"
    assert row.report == report

    # An independent append-only run can expire without altering this report.
    later_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerL2ReportCanary(
                canary_id=later_id,
                request_id=uuid4(),
                agent_id=agent_id,
                source_attempt_id=attempt_id,
                artifact_sha256=sha,
                policy_version=13,
                bench_version=13,
                target_node_id=node_id,
                expected_agent_status="rejected",
                expected_score_count=0,
                review_label="known_reject",
                status="leased",
                lease_token_hash=hashlib.sha256(b"late-token").hexdigest(),
                lease_expires_at=now - timedelta(seconds=1),
                settings_revision=124,
                settings_checksum="d" * 64,
                runtime_evidence_sha256=hashlib.sha256(
                    json.dumps(
                        packet.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            )
        )
    async with session_maker() as session:
        assert (
            await endpoints.claim_l2_report_canary(
                L2CanaryClaimRequest(
                    instance_id=node_id + "-worker-1",
                    settings_revision=124,
                    settings_checksum="d" * 64,
                ),
                request,
                Response(),
                "hotkey",
                session,
                storage,
            )
            is None
        )
    async with session_maker() as session:
        with pytest.raises(HTTPException) as late:
            await endpoints.complete_l2_report_canary(
                later_id,
                body.model_copy(update={"lease_token": "late-token"}),
                request,
                "hotkey",
                session,
            )
    assert late.value.status_code == 409
    async with session_maker() as session:
        original = await session.get(ScreenerL2ReportCanary, canary_id)
        expired = await session.get(ScreenerL2ReportCanary, later_id)
    assert original is not None and original.status == "succeeded"
    assert expired is not None and expired.status == "expired"


@pytest.mark.asyncio
async def test_unready_worker_skips_an_older_full_runtime_row(
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scheduling accepts a full-runtime row when any worker on the node has
    # adopted; a worker that has not must still claim the source-only rows
    # queued behind it instead of returning nothing forever.
    sha = "b" * 64
    agent_id = await _seed_agent(session_maker, status=AgentStatus.REJECTED, sha256=sha)
    node_id = f"canary-test-{uuid4().hex[:12]}"
    full_attempt, source_attempt = uuid4(), uuid4()
    full_canary, source_canary = uuid4(), uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id=node_id,
                provider="hetzner",
                provider_resource_id=node_id,
                screener_hotkey=f"hotkey-{node_id}",
                token_hash="f" * 64,
                token_expires_at=now + timedelta(hours=1),
                status="active",
                capacity=1,
            )
        )
        for attempt_id in (full_attempt, source_attempt):
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    artifact_sha256=sha,
                    screener_hotkey=f"hotkey-{node_id}",
                    policy_version=13,
                    status="rejected",
                    started_at=now - timedelta(minutes=1),
                    deadline=now,
                    finished_at=now,
                )
            )
        await session.flush()
        for canary_id, attempt_id, run_mode, created_at in (
            (full_canary, full_attempt, "full_runtime", now - timedelta(minutes=5)),
            (source_canary, source_attempt, "source_only", now),
        ):
            session.add(
                ScreenerL2ReportCanary(
                    canary_id=canary_id,
                    request_id=uuid4(),
                    agent_id=agent_id,
                    source_attempt_id=attempt_id,
                    artifact_sha256=sha,
                    policy_version=13,
                    bench_version=13,
                    target_node_id=node_id,
                    expected_agent_status="rejected",
                    expected_score_count=0,
                    review_label="known_reject",
                    run_mode=run_mode,
                    status="queued",
                    created_at=created_at,
                )
            )
    monkeypatch.setattr(
        endpoints, "_full_runtime_worker_ready", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        endpoints,
        "scored_runtime_evidence_for_lease",
        AsyncMock(return_value=_packet(source_attempt, sha)),
    )
    monkeypatch.setattr(
        endpoints,
        "_resolve_effective_review_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                revision=124,
                checksum="d" * 64,
                settings=SimpleNamespace(
                    source_review_timeout_seconds=3600, timeout_seconds=1800
                ),
            )
        ),
    )
    request = cast(
        Request, SimpleNamespace(state=SimpleNamespace(screener_node_id=node_id))
    )
    storage = cast(
        S3StorageClient,
        SimpleNamespace(
            presigned_get_url=AsyncMock(return_value="https://example.test/source")
        ),
    )

    async with session_maker() as session:
        claim = await endpoints.claim_l2_report_canary(
            L2CanaryClaimRequest(
                instance_id=node_id + "-worker-1",
                settings_revision=124,
                settings_checksum="d" * 64,
            ),
            request,
            Response(),
            "hotkey",
            session,
            storage,
        )

    assert claim is not None
    assert claim.canary_id == source_canary
    assert claim.run_mode == "source_only"
    async with session_maker() as session:
        waiting = await endpoints.get_l2_report_canary(full_canary, None, session)
    assert waiting.status == "queued"
