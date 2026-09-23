"""Exact-guarded, report-only replay leases over the migrated Postgres schema."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError

from ditto.api_models.verification_replay import (
    VerificationReplayBuildUploadRequest,
    VerificationReplayBuildVerifyRequest,
    VerificationReplayCreate,
    VerificationReplayFinish,
    VerificationReplayReceiptRequest,
)
from ditto.api_server.endpoints.verification_replay import (
    _replay_verified_image_key,
    append_replay_receipt,
    claim_replay,
    create_replay,
    finish_replay,
    get_replay_claimability,
    get_replay_inputs,
    mint_replay_build_upload,
    renew_replay,
    verify_replay_build,
)
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreenerNode,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningVerificationReceipt,
    ScreeningVerificationReplay,
    ScreeningVerificationReplayReceipt,
)

ARTIFACT = "a" * 64
IMAGE = "b" * 64
FIRST_WORKER = "5OriginalScreener"
SECOND_WORKER = "5IndependentScreener"
SECOND_NODE = "replay-independent"


def _request(storage=None, *, node_id=SECOND_NODE):
    return SimpleNamespace(
        state=SimpleNamespace(screener_node_status="active", screener_node_id=node_id),
        app=SimpleNamespace(state=SimpleNamespace(storage=storage)),
    )


async def _enroll(
    session,
    *,
    environment="prod",
    hotkey=SECOND_WORKER,
    node_id=SECOND_NODE,
    replay_capacity=None,
):
    settings = (
        {}
        if replay_capacity is None
        else {"verification_replay_capacity": replay_capacity}
    )
    session.add(
        ScreenerNode(
            environment=environment,
            node_id=node_id,
            provider="test",
            provider_resource_id=f"isolated-replay-{node_id}",
            screener_hotkey=hotkey,
            token_hash="f" * 64,
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="active",
            **settings,
        )
    )
    await session.commit()


async def _seed(session):
    now = datetime.now(UTC)
    agent_id, attempt_id, quarantine_id, image_id = (uuid4() for _ in range(4))
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey="5Miner",
            name=f"replay-{agent_id}",
            version=1,
            sha256=ARTIFACT,
            status="quarantined",
            screening_policy_version=13,
            screened_image_sha256=IMAGE,
            screened_image_size_bytes=123,
            screened_image_id="sha256:" + "c" * 64,
            screened_image_ref=f"ditto-screen/{agent_id}:latest",
            screened_image_upload_id=image_id,
            screened_image_verified_at=now,
            created_at=now,
        )
    )
    await session.flush()
    session.add(
        ScreeningAttempt(
            attempt_id=attempt_id,
            agent_id=agent_id,
            artifact_sha256=ARTIFACT,
            screener_hotkey=FIRST_WORKER,
            policy_version=13,
            status="quarantined",
            started_at=now - timedelta(minutes=10),
            deadline=now,
            finished_at=now,
        )
    )
    await session.flush()
    session.add(
        ScreeningQuarantine(
            quarantine_id=quarantine_id,
            agent_id=agent_id,
            attempt_id=attempt_id,
            screener_hotkey=FIRST_WORKER,
            policy_version=13,
            manifest_digest="d" * 64,
            reason_code="source-review-inconclusive",
            status="active",
            created_at=now,
        )
    )
    session.add(
        ScreenedImageUpload(
            image_upload_id=image_id,
            agent_id=agent_id,
            attempt_id=attempt_id,
            screener_hotkey=FIRST_WORKER,
            storage_upload_id=f"storage-{image_id}",
            sha256=IMAGE,
            size_bytes=123,
            image_id="sha256:" + "c" * 64,
            image_ref=f"ditto-screen/{agent_id}:latest",
            status="verified",
            expires_at=now + timedelta(minutes=15),
            verified_at=now,
        )
    )
    await session.commit()
    return agent_id, attempt_id, quarantine_id, image_id


def _payload(attempt_id, quarantine_id, image_id, **changes):
    values = {
        "request_id": uuid4(),
        "quarantine_id": quarantine_id,
        "source_attempt_id": attempt_id,
        "artifact_sha256": ARTIFACT,
        "policy_version": 13,
        "image_upload_id": image_id,
        "image_sha256": IMAGE,
        "expected_agent_status": "quarantined",
        "actor": "operator:test",
        "reason": "Independent post-hold source replay",
    }
    values.update(changes)
    return VerificationReplayCreate(**values)


@pytest.mark.asyncio
async def test_replay_renewal_is_bounded_and_rechecks_exact_source(session):
    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    created = await create_replay(
        agent_id, _payload(attempt_id, quarantine_id, image_id), None, session
    )
    await _enroll(session, replay_capacity=1)
    claimed = await claim_replay(_request(), SECOND_WORKER, session)
    assert claimed is not None and claimed.replay_id == created.replay_id
    assert claimed.lease_started_at is not None
    assert claimed.lease_renewals == 0
    with pytest.raises(HTTPException) as too_early:
        await renew_replay(created.replay_id, _request(), SECOND_WORKER, session)
    assert too_early.value.status_code == 409

    row = await session.get(ScreeningVerificationReplay, created.replay_id)
    assert row is not None
    row.lease_deadline = datetime.now(UTC) + timedelta(minutes=5)
    await session.commit()
    renewed = await renew_replay(created.replay_id, _request(), SECOND_WORKER, session)
    assert renewed.lease_renewals == 1
    assert renewed.lease_deadline is not None
    assert renewed.lease_started_at is not None
    assert renewed.lease_deadline <= renewed.lease_started_at + timedelta(hours=4)
    with pytest.raises(HTTPException) as wrong_worker:
        await renew_replay(created.replay_id, _request(), FIRST_WORKER, session)
    assert wrong_worker.value.status_code == 403

    row = await session.get(ScreeningVerificationReplay, created.replay_id)
    assert row is not None
    row.lease_renewals = 8
    row.lease_deadline = datetime.now(UTC) + timedelta(minutes=5)
    await session.commit()
    with pytest.raises(HTTPException) as budget:
        await renew_replay(created.replay_id, _request(), SECOND_WORKER, session)
    assert budget.value.status_code == 409

    row.lease_renewals = 1
    agent = await session.get(Agent, agent_id)
    assert agent is not None
    agent.status = "scored"
    await session.commit()
    with pytest.raises(HTTPException) as stale:
        await renew_replay(created.replay_id, _request(), SECOND_WORKER, session)
    assert stale.value.status_code == 409


@pytest.mark.asyncio
async def test_replay_exact_guards_independent_claim_and_report_only(session):
    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    payload = _payload(attempt_id, quarantine_id, image_id)
    first = await create_replay(agent_id, payload, None, session)
    availability = await get_replay_claimability(
        agent_id, first.replay_id, None, session
    )
    assert availability.source_binding_current is True
    assert availability.original_screener_hotkey == FIRST_WORKER
    assert availability.independent_replay_enabled is False
    assert availability.replay_enabled_independent_hotkeys == []
    again = await create_replay(agent_id, payload, None, session)
    assert first.replay_id == again.replay_id
    with pytest.raises(HTTPException) as separate_request:
        await create_replay(
            agent_id,
            payload.model_copy(update={"request_id": uuid4()}),
            None,
            session,
        )
    assert separate_request.value.status_code == 409
    with pytest.raises(HTTPException) as legacy:
        await claim_replay(_request(node_id=None), SECOND_WORKER, session)
    assert legacy.value.status_code == 403
    await _enroll(session, replay_capacity=1)
    availability = await get_replay_claimability(
        agent_id, first.replay_id, None, session
    )
    assert availability.independent_replay_enabled is True
    assert availability.replay_enabled_independent_hotkeys == [SECOND_WORKER]
    await _enroll(
        session, hotkey=FIRST_WORKER, node_id="replay-original", replay_capacity=1
    )
    assert (
        await claim_replay(_request(node_id="replay-original"), FIRST_WORKER, session)
        is None
    )
    claimed = await claim_replay(_request(), SECOND_WORKER, session)
    assert claimed is not None and claimed.replay_id == first.replay_id
    next_agent, next_attempt, next_quarantine, next_image = await _seed(session)
    await create_replay(
        next_agent,
        _payload(next_attempt, next_quarantine, next_image),
        None,
        session,
    )
    assert await claim_replay(_request(), SECOND_WORKER, session) is None

    storage = SimpleNamespace(
        presigned_get_url=AsyncMock(side_effect=["source-url", "image-url"])
    )
    inputs = await get_replay_inputs(
        first.replay_id, _request(storage), SECOND_WORKER, session
    )
    assert (inputs.artifact_url, inputs.image_url) == ("source-url", "image-url")
    assert [
        call.kwargs["key"] for call in storage.presigned_get_url.call_args_list
    ] == [
        f"{agent_id}/agent.tar.gz",
        f"{agent_id}/screened-images/{image_id}.tar",
    ]
    with pytest.raises(HTTPException) as unauthorized:
        await get_replay_inputs(
            first.replay_id, _request(storage), FIRST_WORKER, session
        )
    assert unauthorized.value.status_code == 403
    assert storage.presigned_get_url.call_count == 2

    receipt = VerificationReplayReceiptRequest(
        artifact_sha256=ARTIFACT,
        policy_version=13,
        image_upload_id=image_id,
        image_sha256=IMAGE,
        check_code="health",
        evidence_sha256="e" * 64,
    )
    stored = await append_replay_receipt(
        first.replay_id, receipt, _request(), SECOND_WORKER, session
    )
    duplicate = await append_replay_receipt(
        first.replay_id, receipt, _request(), SECOND_WORKER, session
    )
    assert stored.receipt_id == duplicate.receipt_id
    with pytest.raises(HTTPException) as changed_receipt:
        await append_replay_receipt(
            first.replay_id,
            receipt.model_copy(update={"evidence_sha256": "f" * 64}),
            _request(),
            SECOND_WORKER,
            session,
        )
    assert changed_receipt.value.status_code == 409
    with pytest.raises(HTTPException) as changed_binding:
        await append_replay_receipt(
            first.replay_id,
            receipt.model_copy(update={"artifact_sha256": "f" * 64}),
            _request(),
            SECOND_WORKER,
            session,
        )
    assert changed_binding.value.status_code == 409

    result = VerificationReplayFinish(
        status="reported", artifact_sha256=ARTIFACT, image_sha256=IMAGE
    )
    finished = await finish_replay(
        first.replay_id, result, _request(), SECOND_WORKER, session
    )
    duplicate_finish = await finish_replay(
        first.replay_id, result, _request(), SECOND_WORKER, session
    )
    assert finished.status == duplicate_finish.status == "reported"
    assert finished.policy_verification_complete is False
    after_finish_retry = await create_replay(agent_id, payload, None, session)
    assert after_finish_retry.replay_id == first.replay_id
    assert (await session.get(Agent, agent_id)).status == "quarantined"
    assert (await session.get(ScreeningQuarantine, quarantine_id)).status == "active"
    assert (await session.get(ScreeningAttempt, attempt_id)).status == "quarantined"
    assert (
        await session.scalar(
            select(func.count()).select_from(ScreeningVerificationReceipt)
        )
        == 0
    )
    assert (
        await session.scalar(
            select(func.count()).select_from(ScreeningVerificationReplayReceipt)
        )
        == 1
    )
    with pytest.raises(DBAPIError):
        await session.execute(
            delete(ScreeningVerificationReplayReceipt).where(
                ScreeningVerificationReplayReceipt.receipt_id == stored.receipt_id
            )
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_replay_rejects_stale_image_and_missing_pinned_attempt(session):
    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    image = await session.get(ScreenedImageUpload, image_id)
    image.status = "aborted"
    await session.commit()
    with pytest.raises(HTTPException) as stale:
        await create_replay(
            agent_id, _payload(attempt_id, quarantine_id, image_id), None, session
        )
    assert stale.value.status_code == 409
    image.status = "verified"
    await session.commit()
    attempt = await session.get(ScreeningAttempt, attempt_id)
    # This models a historical lease created before SHA pinning. Do not update
    # an existing production attempt: its immutability trigger prevents it.
    assert attempt.artifact_sha256 == ARTIFACT
    with pytest.raises(HTTPException) as wrong_sha:
        await create_replay(
            agent_id,
            _payload(attempt_id, quarantine_id, image_id, artifact_sha256="0" * 64),
            None,
            session,
        )
    assert wrong_sha.value.status_code == 409


@pytest.mark.asyncio
async def test_replay_refuses_nonproduction_enrollment(session):
    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    created = await create_replay(
        agent_id, _payload(attempt_id, quarantine_id, image_id), None, session
    )
    await _enroll(session, environment="test", replay_capacity=1)
    availability = await get_replay_claimability(
        agent_id, created.replay_id, None, session
    )
    assert availability.independent_replay_enabled is False
    with pytest.raises(HTTPException) as unauthorized:
        await claim_replay(_request(), SECOND_WORKER, session)
    assert unauthorized.value.status_code == 403


@pytest.mark.asyncio
async def test_newly_enrolled_node_has_no_replay_authority_by_default(session):
    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    created = await create_replay(
        agent_id, _payload(attempt_id, quarantine_id, image_id), None, session
    )
    await _enroll(session)
    availability = await get_replay_claimability(
        agent_id, created.replay_id, None, session
    )
    assert availability.independent_replay_enabled is False
    with pytest.raises(HTTPException) as unauthorized:
        await claim_replay(_request(), SECOND_WORKER, session)
    assert unauthorized.value.status_code == 403


@pytest.mark.asyncio
async def test_prebuild_hold_gets_separate_verified_replay_image(session):
    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    agent = await session.get(Agent, agent_id)
    agent.screened_image_sha256 = None
    agent.screened_image_size_bytes = None
    agent.screened_image_id = None
    agent.screened_image_ref = None
    agent.screened_image_upload_id = None
    agent.screened_image_verified_at = None
    await session.commit()
    payload = _payload(
        attempt_id, quarantine_id, image_id, image_upload_id=None, image_sha256=None
    )
    created = await create_replay(agent_id, payload, None, session)
    assert created.image_sha256 is None
    await _enroll(session, replay_capacity=1)
    claimed = await claim_replay(_request(), SECOND_WORKER, session)
    assert claimed is not None and claimed.replay_id == created.replay_id
    with pytest.raises(HTTPException) as original_worker:
        await mint_replay_build_upload(
            created.replay_id,
            VerificationReplayBuildUploadRequest(
                artifact_sha256=ARTIFACT,
                image_sha256=IMAGE,
                size_bytes=123,
                image_id="sha256:" + "c" * 64,
            ),
            _request(),
            FIRST_WORKER,
            session,
        )
    assert original_worker.value.status_code == 403
    storage = SimpleNamespace(
        presigned_get_url=AsyncMock(return_value="source-url"),
        presigned_put_url=AsyncMock(return_value="staging-put-url"),
        head_object=AsyncMock(),
        verify_object_sha256=AsyncMock(),
        copy_object=AsyncMock(),
    )
    before_build = await get_replay_inputs(
        created.replay_id, _request(storage), SECOND_WORKER, session
    )
    assert before_build.image_url is None
    assert storage.presigned_get_url.call_count == 1
    upload = VerificationReplayBuildUploadRequest(
        artifact_sha256=ARTIFACT,
        image_sha256=IMAGE,
        size_bytes=123,
        image_id="sha256:" + "c" * 64,
    )
    minted = await mint_replay_build_upload(
        created.replay_id, upload, _request(storage), SECOND_WORKER, session
    )
    assert minted.upload_url == "staging-put-url"
    assert storage.presigned_put_url.call_args.kwargs["key"].startswith(
        f"verification-replays/{created.replay_id}/staging/"
    )
    metadata = storage.presigned_put_url.call_args.kwargs["metadata"]
    storage.head_object.return_value = SimpleNamespace(
        size_bytes=123, metadata=metadata
    )
    storage.verify_object_sha256.return_value = SimpleNamespace(
        size_bytes=123, sha256=IMAGE
    )
    verify = VerificationReplayBuildVerifyRequest(**upload.model_dump())
    built = await verify_replay_build(
        created.replay_id, verify, _request(storage), SECOND_WORKER, session
    )
    assert built.image_verified_at is not None
    assert built.image_upload_id is None
    pinned_key = built.image_verified_storage_key
    assert pinned_key is not None
    assert pinned_key.startswith(f"verification-replays/{created.replay_id}/verified/")
    assert storage.copy_object.call_args.kwargs["dest_key"] == pinned_key
    again = await verify_replay_build(
        created.replay_id, verify, _request(storage), SECOND_WORKER, session
    )
    assert again.image_verified_storage_key == pinned_key
    assert storage.copy_object.call_count == 1
    await get_replay_inputs(
        created.replay_id, _request(storage), SECOND_WORKER, session
    )
    assert storage.presigned_get_url.call_args.kwargs["key"] == pinned_key
    assert (await session.get(Agent, agent_id)).screened_image_upload_id is None
    assert (await session.get(Agent, agent_id)).status == "quarantined"


def test_verified_replay_candidates_never_share_a_final_object_key():
    replay_id = uuid4()
    assert _replay_verified_image_key(replay_id, uuid4()) != (
        _replay_verified_image_key(replay_id, uuid4())
    )
