"""Postgres regressions for the separate, exact-attempt L2 audit queue."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.l2_report_canary import (
    L2CanaryClaimRequest,
    L2CanaryCompleteRequest,
    L2CanaryScheduleRequest,
)
from ditto.api_server.endpoints import l2_report_canary as endpoints
from ditto.api_server.storage import S3StorageClient
from ditto.db.models import ScreenerL2ReportCanary, ScreenerNode, ScreeningAttempt
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


@pytest.mark.parametrize(
    ("timeout", "run_mode", "expected_seconds"),
    [
        (600, "source_only", 45 * 60),
        (3600, "source_only", 70 * 60),
        (3600, "full_runtime", 120 * 60),
    ],
)
def test_report_only_lease_covers_review_and_bounded_preparation(
    timeout: int, run_mode: str, expected_seconds: int
) -> None:
    assert endpoints._canary_lease(
        source_review_timeout_seconds=timeout, run_mode=run_mode
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
                settings=SimpleNamespace(source_review_timeout_seconds=3600),
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
    expected_lease = timedelta(minutes=120 if run_mode == "full_runtime" else 70)
    assert abs((claim.lease_expires_at - now - expected_lease).total_seconds()) < 30
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
        AsyncMock(return_value=SimpleNamespace(revision=124, checksum="d" * 64)),
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
