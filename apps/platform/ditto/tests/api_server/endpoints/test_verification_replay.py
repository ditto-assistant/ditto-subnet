"""Exact-guarded, report-only replay leases over the migrated Postgres schema."""

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
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
from ditto.api_server.endpoints import verification_replay
from ditto.api_server.endpoints.verification_replay import (
    _replay_verified_image_key,
    append_replay_receipt,
    append_signed_replay_observation,
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
    ScreenerHeartbeat,
    ScreenerNode,
    ScreenerReplayProcessKey,
    ScreeningAttempt,
    ScreeningQuarantine,
    ScreeningVerificationReceipt,
    ScreeningVerificationReplay,
    ScreeningVerificationReplayReceipt,
    ScreeningVerificationReplaySignedObservation,
)
from ditto_screening_protocol.models import SourceReviewNote, source_review_notes_digest
from ditto_screening_protocol.v13_replay_observation import (
    V13ReplayBinding,
    V13ReplayObservation,
)

from ditto_screening_protocol.v13_replay_process_identity import V13ReplayProcessProof

ARTIFACT = "a" * 64
IMAGE = "b" * 64
FIRST_WORKER = "5OriginalScreener"
SECOND_WORKER = "5IndependentScreener"
SECOND_NODE = "replay-independent"


@pytest.fixture(autouse=True)
def _test_replay_runner_release(monkeypatch):
    from ditto.api_server.endpoints import admin_screener_capacity

    # Production remains unset. Existing lease tests exercise the future
    # report-only runner contract with a synthetic minimum release.
    monkeypatch.setattr(
        admin_screener_capacity,
        "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE",
        (0, 999, 0),
    )


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
    now = datetime.now(UTC)
    session.add(
        ScreenerNode(
            environment=environment,
            node_id=node_id,
            provider="test",
            provider_resource_id=f"isolated-replay-{node_id}",
            screener_hotkey=hotkey,
            token_hash="f" * 64,
            token_expires_at=now + timedelta(hours=1),
            status="active",
            **settings,
        )
    )
    if replay_capacity:
        session.add(
            ScreenerHeartbeat(
                screener_hotkey=hotkey,
                instance_id=f"{node_id}-worker-1",
                software_version="v0.999.0",
                protocol_version=7,
                policy_version=13,
                state="polling",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
                system_metrics={
                    "release": {
                        "builtin_policy_version": 13,
                        "revision": "a" * 40,
                        "version": "v0.999.0",
                        "activated_at": int(now.timestamp()),
                    }
                },
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


async def _seed_source_review_failure(session, reason_code="l2-model-total-budget"):
    agent_id, attempt_id, quarantine_id, _image_id = await _seed(session)
    agent = await session.get(Agent, agent_id)
    attempt = await session.get(ScreeningAttempt, attempt_id)
    quarantine = await session.get(ScreeningQuarantine, quarantine_id)
    assert agent is not None and attempt is not None and quarantine is not None
    agent.status = "screening_failed"
    agent.screened_image_sha256 = None
    agent.screened_image_size_bytes = None
    agent.screened_image_id = None
    agent.screened_image_ref = None
    agent.screened_image_upload_id = None
    agent.screened_image_verified_at = None
    attempt.status = "expired"
    attempt.reason_code = reason_code
    quarantine.status = "resolved"
    quarantine.resolution = "rescreen"
    quarantine.resolved_at = datetime.now(UTC)
    quarantine.reason_code = reason_code
    notes = [
        SourceReviewNote(
            kind="observation", path="src/main.py", line=1, summary="Budget hold"
        )
    ]
    quarantine.review_notes = [note.model_dump(mode="json") for note in notes]
    quarantine.review_notes_digest = source_review_notes_digest(notes)
    await session.commit()
    return agent_id, attempt_id, quarantine_id


@pytest.mark.parametrize(
    "reason_code", ["l2-model-total-budget", "l2-model-inconclusive"]
)
@pytest.mark.asyncio
async def test_failed_source_review_can_enter_independent_replay_without_release(
    session,
    reason_code,
):
    agent_id, attempt_id, quarantine_id = await _seed_source_review_failure(
        session, reason_code=reason_code
    )
    payload = _payload(
        attempt_id,
        quarantine_id,
        None,
        image_sha256=None,
        expected_agent_status="screening_failed",
    )
    replay = await create_replay(agent_id, payload, None, session)
    await _enroll(session, replay_capacity=1)
    claimed = await claim_replay(_request(), SECOND_WORKER, session)
    assert claimed is not None and claimed.replay_id == replay.replay_id
    assert claimed.image_sha256 is None
    storage = SimpleNamespace(
        presigned_get_url=AsyncMock(return_value="source-url"),
        presigned_put_url=AsyncMock(return_value="staging-put-url"),
        head_object=AsyncMock(),
        verify_object_sha256=AsyncMock(),
        copy_object=AsyncMock(),
    )
    inputs = await get_replay_inputs(
        replay.replay_id, _request(storage), SECOND_WORKER, session
    )
    assert inputs.image_url is None
    upload = VerificationReplayBuildUploadRequest(
        artifact_sha256=ARTIFACT,
        image_sha256=IMAGE,
        size_bytes=123,
        image_id="sha256:" + "c" * 64,
    )
    minted = await mint_replay_build_upload(
        replay.replay_id, upload, _request(storage), SECOND_WORKER, session
    )
    assert minted.upload_url == "staging-put-url"
    metadata = storage.presigned_put_url.call_args.kwargs["metadata"]
    storage.head_object.return_value = SimpleNamespace(
        size_bytes=123, metadata=metadata
    )
    storage.verify_object_sha256.return_value = SimpleNamespace(
        size_bytes=123, sha256=IMAGE
    )
    built = await verify_replay_build(
        replay.replay_id,
        VerificationReplayBuildVerifyRequest(**upload.model_dump()),
        _request(storage),
        SECOND_WORKER,
        session,
    )
    assert built.image_verified_at is not None
    assert built.image_upload_id is None
    agent = await session.get(Agent, agent_id)
    attempt = await session.get(ScreeningAttempt, attempt_id)
    assert agent is not None and agent.status == "screening_failed"
    assert agent.screened_image_upload_id is None
    assert attempt is not None and attempt.status == "expired"


@pytest.mark.asyncio
async def test_source_review_replay_rejects_stale_or_unverified_evidence(session):
    agent_id, attempt_id, quarantine_id = await _seed_source_review_failure(session)
    payload = _payload(
        attempt_id,
        quarantine_id,
        None,
        image_sha256=None,
        expected_agent_status="screening_failed",
    )
    quarantine = await session.get(ScreeningQuarantine, quarantine_id)
    assert quarantine is not None
    quarantine.review_notes_digest = "f" * 64
    await session.commit()
    with pytest.raises(HTTPException) as tampered:
        await create_replay(agent_id, payload, None, session)
    assert tampered.value.status_code == 409

    notes = [SourceReviewNote.model_validate(note) for note in quarantine.review_notes]
    quarantine.review_notes_digest = source_review_notes_digest(notes)
    quarantine.review_audit_digest = "f" * 64
    # The DB also rejects an orphan digest; disable autoflush here to prove
    # the admission guard itself rejects it before any write.
    with session.no_autoflush, pytest.raises(HTTPException) as orphan_audit:
        await create_replay(agent_id, payload, None, session)
    assert orphan_audit.value.status_code == 409
    quarantine = await session.get(ScreeningQuarantine, quarantine_id)
    assert quarantine is not None
    quarantine.review_notes_digest = source_review_notes_digest(notes)
    quarantine.review_audit_digest = None
    await session.commit()
    replay = await create_replay(agent_id, payload, None, session)
    assert replay.replay_id is not None
    session.add(
        ScreeningAttempt(
            attempt_id=uuid4(),
            agent_id=agent_id,
            artifact_sha256=ARTIFACT,
            screener_hotkey=FIRST_WORKER,
            policy_version=13,
            status="running",
            started_at=datetime.now(UTC),
            deadline=datetime.now(UTC) + timedelta(minutes=30),
        )
    )
    await session.commit()
    availability = await get_replay_claimability(
        agent_id, replay.replay_id, None, session
    )
    assert availability.source_binding_current is False


@pytest.mark.asyncio
async def test_non_source_review_failure_cannot_enter_replay(session):
    agent_id, attempt_id, quarantine_id = await _seed_source_review_failure(
        session, reason_code="challenge-http-500"
    )
    payload = _payload(
        attempt_id,
        quarantine_id,
        None,
        image_sha256=None,
        expected_agent_status="screening_failed",
    )
    with pytest.raises(HTTPException) as rejected:
        await create_replay(agent_id, payload, None, session)
    assert rejected.value.status_code == 409


@pytest.mark.asyncio
async def test_signed_observation_remains_report_only_and_rejects_rebinding(
    session, monkeypatch
):
    from ditto.api_server.endpoints import verification_replay

    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    created = await create_replay(
        agent_id, _payload(attempt_id, quarantine_id, image_id), None, session
    )
    await _enroll(session, replay_capacity=1)
    claimed = await claim_replay(_request(), SECOND_WORKER, session)
    assert claimed is not None and claimed.replay_id == created.replay_id
    signed_messages = []
    monkeypatch.setattr(
        verification_replay,
        "_verify_signature",
        lambda hotkey, message, signature: (
            signed_messages.append((hotkey, message, signature)) or True
        ),
    )
    observation = V13ReplayObservation(
        binding=V13ReplayBinding(
            replay_id=created.replay_id,
            agent_id=agent_id,
            attempt_id=attempt_id,
            artifact_sha256=ARTIFACT,
            image_sha256=IMAGE,
            image_id="sha256:" + "c" * 64,
        ),
        check_code="health",
        status="passed",
        evidence_sha256="e" * 64,
        runner_hotkey=SECOND_WORKER,
        observed_at=datetime.now(UTC),
        signature="f" * 128,
    )
    accepted = await append_signed_replay_observation(
        created.replay_id, observation, _request(), SECOND_WORKER, session
    )
    assert accepted.status == "passed"
    assert accepted.policy_verification_complete is False
    assert signed_messages == [
        (SECOND_WORKER, observation.signing_message(), observation.signature)
    ]
    assert (
        await session.scalar(
            select(
                func.count(ScreeningVerificationReplaySignedObservation.observation_id)
            )
        )
    ) == 1
    assert (
        await append_signed_replay_observation(
            created.replay_id, observation, _request(), SECOND_WORKER, session
        )
    ).observation_id == accepted.observation_id
    for changed in (
        observation.model_copy(
            update={
                "binding": observation.binding.model_copy(
                    update={"attempt_id": uuid4()}
                )
            }
        ),
        observation.model_copy(
            update={
                "binding": observation.binding.model_copy(
                    update={"image_sha256": "d" * 64}
                )
            }
        ),
        observation.model_copy(update={"runner_hotkey": FIRST_WORKER}),
    ):
        with pytest.raises(HTTPException):
            await append_signed_replay_observation(
                created.replay_id, changed, _request(), SECOND_WORKER, session
            )
    monkeypatch.setattr(verification_replay, "_verify_signature", lambda *_: False)
    with pytest.raises(HTTPException) as bad_signature:
        await append_signed_replay_observation(
            created.replay_id, observation, _request(), SECOND_WORKER, session
        )
    assert bad_signature.value.status_code == 403


@pytest.mark.asyncio
async def test_replay_claim_rechecks_worker_readiness_after_capacity_enabled(
    session, monkeypatch
):
    from ditto.api_server.endpoints import admin_screener_capacity

    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    created = await create_replay(
        agent_id, _payload(attempt_id, quarantine_id, image_id), None, session
    )
    await _enroll(session, replay_capacity=1)
    heartbeat = await session.get(
        ScreenerHeartbeat, (SECOND_WORKER, f"{SECOND_NODE}-worker-1")
    )
    assert heartbeat is not None

    # An old capacity grant cannot issue claims before the runner minimum is
    # configured, nor after all node-2 worker heartbeats become stale.
    monkeypatch.setattr(
        admin_screener_capacity, "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE", None
    )
    with pytest.raises(HTTPException) as no_runner:
        await claim_replay(_request(), SECOND_WORKER, session)
    assert no_runner.value.status_code == 409
    monkeypatch.setattr(
        admin_screener_capacity,
        "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE",
        (0, 999, 0),
    )
    heartbeat.seen_at = datetime.now(UTC) - timedelta(minutes=6)
    await session.commit()
    with pytest.raises(HTTPException) as stale:
        await claim_replay(_request(), SECOND_WORKER, session)
    assert stale.value.status_code == 409

    heartbeat.seen_at = datetime.now(UTC)
    heartbeat.policy_version = 12
    await session.commit()
    with pytest.raises(HTTPException) as wrong_policy:
        await claim_replay(_request(), SECOND_WORKER, session)
    assert wrong_policy.value.status_code == 409

    heartbeat.policy_version = 13
    await session.commit()
    claimed = await claim_replay(_request(), SECOND_WORKER, session)
    assert claimed is not None and claimed.replay_id == created.replay_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "purpose", ["renew", "inputs", "build-upload", "build-verify", "receipts", "finish"]
)
async def test_rotated_sibling_process_key_cannot_use_existing_lease(
    session, monkeypatch, purpose
):
    """A process with the shared node hotkey cannot reuse another key's lease."""
    node_id = "subnet-screener-2"
    instance_id = f"{node_id}-worker-1"
    agent_id, attempt_id, quarantine_id, image_id = await _seed(session)
    created = await create_replay(
        agent_id, _payload(attempt_id, quarantine_id, image_id), None, session
    )
    await _enroll(session, node_id=node_id, replay_capacity=1)
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    now = datetime.now(UTC)

    def process_key(private, status):
        public_hex = (
            private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        )
        return ScreenerReplayProcessKey(
            node_id=node_id,
            instance_id=instance_id,
            public_key_hex=public_hex,
            key_sha256=hashlib.sha256(bytes.fromhex(public_hex)).hexdigest(),
            revision=1 if status == "revoked" else 2,
            status=status,
            registered_at=now - timedelta(minutes=1),
            revoked_at=now if status == "revoked" else None,
        )

    original = process_key(old_key, "revoked")
    replacement = process_key(new_key, "active")
    session.add_all([original, replacement])
    await session.flush()
    row = await session.get(ScreeningVerificationReplay, created.replay_id)
    assert row is not None
    row.status = "running"
    row.worker_hotkey = SECOND_WORKER
    row.process_key_sha256 = original.key_sha256
    row.lease_started_at = now
    row.lease_deadline = now + timedelta(minutes=5)
    await session.commit()
    monkeypatch.setattr(
        verification_replay, "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE", (0, 301, 0)
    )
    path = f"/screener/verification-replays/{created.replay_id}/{purpose}"
    method = "GET" if purpose == "inputs" else "POST"
    proof = V13ReplayProcessProof(
        purpose=purpose,
        node_id=node_id,
        instance_id=instance_id,
        method=method,
        path=path,
        body_sha256=hashlib.sha256(b"").hexdigest(),
        issued_at=int(now.timestamp()),
        nonce="b" * 32,
    )

    class SignedRequest:
        state = SimpleNamespace(screener_node_status="active", screener_node_id=node_id)
        headers = {
            "x-replay-process-instance": instance_id,
            "x-replay-process-proof": proof.model_dump_json(),
            "x-replay-process-signature": new_key.sign(proof.signing_bytes()).hex(),
        }

        async def body(self):
            return b""

    request = SignedRequest()
    request.method = method
    build = VerificationReplayBuildUploadRequest(
        artifact_sha256=ARTIFACT,
        image_sha256=IMAGE,
        size_bytes=123,
        image_id="sha256:" + "c" * 64,
    )
    if purpose == "renew":
        call = renew_replay(created.replay_id, request, SECOND_WORKER, session)
    elif purpose == "inputs":
        call = get_replay_inputs(created.replay_id, request, SECOND_WORKER, session)
    elif purpose == "build-upload":
        call = mint_replay_build_upload(
            created.replay_id, build, request, SECOND_WORKER, session
        )
    elif purpose == "build-verify":
        call = verify_replay_build(
            created.replay_id,
            VerificationReplayBuildVerifyRequest(**build.model_dump()),
            request,
            SECOND_WORKER,
            session,
        )
    elif purpose == "receipts":
        call = append_replay_receipt(
            created.replay_id,
            VerificationReplayReceiptRequest(
                artifact_sha256=ARTIFACT,
                policy_version=13,
                check_code="archive_sha",
                evidence_sha256="d" * 64,
            ),
            request,
            SECOND_WORKER,
            session,
        )
    else:
        call = finish_replay(
            created.replay_id,
            VerificationReplayFinish(
                status="failed",
                failure_code="canary-failed",
                artifact_sha256=ARTIFACT,
                image_sha256=IMAGE,
            ),
            request,
            SECOND_WORKER,
            session,
        )
    with pytest.raises(HTTPException, match="another process key") as blocked:
        await call
    assert blocked.value.status_code == 403


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
