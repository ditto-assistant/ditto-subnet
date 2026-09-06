from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_hosted_start import HostedStartRequest
from ditto.api_server.coding_hosted_start import HostedStartStore
from ditto.db.models import Agent, CodingHostedAssignment, CodingPrivateV2Release
from ditto.db.queries.coding_hosted_admission import HostedAdmissionError
from ditto.db.queries.coding_hosted_private import close_hosted_private_task
from ditto.db.queries.coding_private_v2_releases import append_private_v2_release_event
from ditto.tests.db.queries.test_coding_hosted_admission import _admit, _request
from ditto.tests.db.queries.test_coding_hosted_private import _prepared
from ditto_screening_protocol import SCREENING_POLICY_VERSION


async def prepared(maker, *, bind=True):
    authority, _, worker, _ = await _prepared(maker, start=False, bind=bind)
    await _admit(maker, _request(authority))
    image_ref = f"ditto-screen/{authority.agent_id}:latest"
    async with maker() as session, session.begin():
        await session.execute(
            update(Agent)
            .where(Agent.agent_id == authority.agent_id)
            .values(
                screened_image_ref=image_ref,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
    request = HostedStartRequest.model_validate(
        {
            "schema": "dittobench-coding-hosted-start-v2",
            "evaluation_id": str(authority.evaluation_id),
            "attempt_id": str(authority.attempt_id),
            "worker_id": str(worker),
            "agent_id": str(authority.agent_id),
            "assignment_sha256": authority.digest(),
            "artifact_sha256": authority.artifact_sha256,
            "screened_image_sha256": authority.screened_image_sha256,
            "screened_image_id": "sha256:" + "c" * 64,
            "screened_image_ref": image_ref,
            "screened_image_size_bytes": 1,
            "screening_policy_version": SCREENING_POLICY_VERSION,
            "profile_capability_id": f"hosted-{authority.attempt_id}",
            "harness_instance_id": "synthetic-instance",
            "deadline_unix": authority.deadline_unix,
        }
    )
    return authority, worker, request


async def test_start_commits_before_response_and_concurrent_replays_cannot_launch(
    session_maker,
):
    authority, worker, request = await prepared(session_maker)
    store = HostedStartStore(session_maker, worker)
    results = await asyncio.gather(*(store.commit_start(request) for _ in range(8)))
    assert results.count(True) == 1
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row.started_at is not None and row.worker_id == worker
    assert await store.commit_start(request) is False
    other = request.model_copy(update={"harness_instance_id": "replacement-instance"})
    assert await store.commit_start(other) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("assignment_sha256", "f" * 64),
        ("artifact_sha256", "f" * 64),
        ("screened_image_sha256", "f" * 64),
        ("screened_image_size_bytes", 2),
        ("screened_image_id", "sha256:" + "f" * 64),
        ("screening_policy_version", 999),
        ("deadline_unix", 1),
    ],
)
async def test_drift_never_consumes_start(session_maker, field, value):
    authority, worker, request = await prepared(session_maker)
    bad = request.model_copy(update={field: value})
    with pytest.raises(HostedAdmissionError):
        await HostedStartStore(session_maker, worker).commit_start(bad)
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row.started_at is None
    assert await HostedStartStore(session_maker, worker).commit_start(request)


async def test_wrong_worker_and_retirement_fail_closed(session_maker):
    authority, worker, request = await prepared(session_maker)
    with pytest.raises(HostedAdmissionError):
        await HostedStartStore(session_maker, uuid4()).commit_start(request)
    async with session_maker() as session, session.begin():
        release = await session.get(CodingPrivateV2Release, authority.release_row_id)
        await append_private_v2_release_event(
            session,
            corpus_release_id=release.corpus_release_id,
            expected_registration_sha256=authority.registration_sha256,
            action="retired",
            actor="test",
            reason="synthetic retirement",
        )
    with pytest.raises(HostedAdmissionError):
        await HostedStartStore(session_maker, worker).commit_start(request)


async def test_cancellation_while_waiting_for_lock_does_not_start(session_maker):
    authority, worker, request = await prepared(session_maker)
    async with session_maker() as owner, owner.begin():
        await owner.get(Agent, authority.agent_id, with_for_update=True)
        pending = asyncio.create_task(
            HostedStartStore(session_maker, worker).commit_start(request)
        )
        await asyncio.sleep(0.05)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row.started_at is None
    assert await HostedStartStore(session_maker, worker).commit_start(request)


async def test_failed_commit_returns_no_permission_and_rolls_back(
    engine, session_maker
):
    authority, worker, request = await prepared(session_maker)

    class FailingSession(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            event.listen(self.sync_session, "before_commit", self.fail_commit)

        @staticmethod
        def fail_commit(_session):
            raise RuntimeError("synthetic commit failure")

    maker = async_sessionmaker(engine, class_=FailingSession, expire_on_commit=False)
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await HostedStartStore(maker, worker).commit_start(request)
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row.started_at is None and row.worker_id is None
    assert await HostedStartStore(session_maker, worker).commit_start(request)


async def test_missing_private_binding_never_launches(session_maker):
    authority, worker, request = await prepared(session_maker, bind=False)
    with pytest.raises(HostedAdmissionError):
        await HostedStartStore(session_maker, worker).commit_start(request)
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row.started_at is None


async def test_closed_task_and_worker_transfer_never_launch(session_maker):
    authority, worker, request = await prepared(session_maker)
    assert await HostedStartStore(session_maker, worker).commit_start(request)
    other = uuid4()
    with pytest.raises(HostedAdmissionError):
        await HostedStartStore(session_maker, other).commit_start(
            request.model_copy(update={"worker_id": other})
        )
    async with session_maker() as session, session.begin():
        await close_hosted_private_task(
            session,
            evaluation_id=authority.evaluation_id,
            attempt_id=authority.attempt_id,
            worker_id=worker,
            reason="aborted",
        )
    with pytest.raises(HostedAdmissionError):
        await HostedStartStore(session_maker, worker).commit_start(request)


async def test_go_lifecycle_uses_real_python_postgres_start_handoff(session_maker):
    authority, worker, request = await prepared(session_maker)
    binding = {
        "EvaluationID": str(request.evaluation_id),
        "AttemptID": str(request.attempt_id),
        "WorkerID": str(worker),
        "AssignmentSHA256": request.assignment_sha256,
        "AgentID": str(request.agent_id),
        "AgentArtifactSHA256": request.artifact_sha256,
        "ProfileCapabilityID": request.profile_capability_id,
        "Deadline": datetime.fromtimestamp(request.deadline_unix, UTC).isoformat(),
        "ScreenedImageSHA256": request.screened_image_sha256,
        "ScreenedImageID": request.screened_image_id,
        "ScreenedImageRef": request.screened_image_ref,
        "ScreenedImageSize": request.screened_image_size_bytes,
        "ScreeningPolicyVersion": request.screening_policy_version,
    }
    repo = Path(__file__).resolve().parents[5]
    environment = dict(os.environ)
    environment.update(
        DITTO_HOSTED_START_TEST_PYTHON=sys.executable,
        DITTO_HOSTED_START_TEST_BINDING=json.dumps(binding),
    )
    process = await asyncio.create_subprocess_exec(
        "go",
        "test",
        "./internal/codingharness",
        "-run",
        "^TestHostedStartCommandPlatformIntegration$",
        "-count=1",
        cwd=repo / "services/dittobench-api",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row.started_at is not None and row.worker_id == worker
