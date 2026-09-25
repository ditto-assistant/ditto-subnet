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
from ditto.tests.api_server.endpoints.test_screener import _seed_agent
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


@pytest.mark.asyncio
async def test_l2_canary_lease_duplicate_late_and_authority_isolation(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
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
                status="queued",
            )
        )
    packet = _packet(attempt_id, sha)
    evidence_lookup = AsyncMock(return_value=packet)
    monkeypatch.setattr(endpoints, "scored_runtime_evidence_for_lease", evidence_lookup)
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
    assert evidence_lookup.await_args is not None
    assert evidence_lookup.await_args.kwargs["report_only_current_packet"] is True
    assert claim.source_attempt_id == attempt_id
    assert claim.scored_runtime_evidence == packet
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
        "review_mode": "shadow",
        "canary_id": str(canary_id),
        "agent_id": str(agent_id),
        "source_attempt_id": str(attempt_id),
        "artifact_sha256": sha,
        "policy_version": 13,
        "settings_revision": 124,
        "settings_checksum": "d" * 64,
        "scored_runtime_evidence": packet.model_dump(mode="json"),
        "l2": {"ok": True, "risk_level": "low"},
    }
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
