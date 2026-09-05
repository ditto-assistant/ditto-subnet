from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import uuid4

import bittensor
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.coding_hosted import HostedCodingRequest, hosted_signing_bytes
from ditto.db.models import Agent, CodingHostedAssignment
from ditto.db.queries.coding_hosted_admission import (
    HostedAdmissionError,
    HostedAdmissionView,
    HostedAssignmentAuthority,
    admit_hosted_request,
    create_hosted_assignment,
    start_hosted_attempt,
)
from ditto.db.queries.coding_private_v2_releases import (
    append_private_v2_release_event,
    insert_private_v2_release,
)
from ditto.db.queries.validator_auth import ValidatorRequestReplayError
from ditto.tests.api_server.endpoints.test_admin_coding_private_v2_releases import (
    _publication_receipt,
    _registration,
)

VALIDATOR = bittensor.Keypair.create_from_uri("//Alice")


async def _seed(
    maker: async_sessionmaker[AsyncSession], *, approve: bool = True
) -> HostedAssignmentAuthority:
    receipt = _publication_receipt(Ed25519PrivateKey.generate())
    registration = _registration(receipt)
    agent_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        release = await insert_private_v2_release(
            session,
            registration=registration,
            receipt=receipt,
            reason="synthetic registration",
            actor="test-operator",
        )
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="synthetic-miner",
                name="synthetic-agent",
                sha256="a" * 64,
                status=AgentStatus.EVALUATING,
                screened_image_sha256="b" * 64,
                screened_image_size_bytes=1,
                screened_image_id="sha256:" + "c" * 64,
                screened_image_ref="synthetic-image",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
            )
        )
        await session.flush()
        authority = HostedAssignmentAuthority(
            evaluation_id=uuid4(),
            attempt_id=uuid4(),
            release_row_id=release.row.release_row_id,
            registration_sha256=registration.registration_sha256,
            agent_id=agent_id,
            validator_hotkey=VALIDATOR.ss58_address,
            artifact_sha256="a" * 64,
            screened_image_sha256="b" * 64,
            selection_sha256="1" * 64,
            policy_sha256="2" * 64,
            execution_profile_sha256="3" * 64,
            grading_profile_sha256="4" * 64,
            deadline_unix=int(now.timestamp()) + 600,
        )
        if approve:
            await create_hosted_assignment(
                session,
                authority=authority,
                confirmed_assignment_sha256=authority.digest(),
                reason="synthetic shadow approval",
                actor="test-operator",
            )
    return authority


def _request(authority: HostedAssignmentAuthority, **changes) -> HostedCodingRequest:
    now = int(datetime.now(UTC).timestamp())
    request = HostedCodingRequest.model_validate(
        {
            "schema": "dittobench-coding-hosted-request-v2",
            "coding_contract_version": 2,
            "shadow_only": True,
            "weight_eligible": False,
            "evaluation_id": authority.evaluation_id,
            "validator_hotkey": authority.validator_hotkey,
            "artifact_sha256": authority.artifact_sha256,
            "assignment_sha256": authority.digest(),
            "policy_sha256": authority.policy_sha256,
            "operation": "evaluate",
            "result_sha256": None,
            "nonce": uuid4(),
            "issued_at_unix": now,
            "expires_at_unix": now + 120,
            "signature": "0" * 128,
            **changes,
        }
    )
    return request.model_copy(
        update={"signature": VALIDATOR.sign(hosted_signing_bytes(request)).hex()}
    )


async def _admit(maker: async_sessionmaker[AsyncSession], request: HostedCodingRequest):
    async with maker() as session, session.begin():
        return await admit_hosted_request(
            session,
            request=request,
            authenticated_validator=VALIDATOR.ss58_address,
            verifier=VALIDATOR,
        )


async def test_registration_alone_does_not_allow_validator_admission(session_maker):
    authority = await _seed(session_maker, approve=False)
    with pytest.raises(HostedAdmissionError, match="unavailable"):
        await _admit(session_maker, _request(authority))
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(CodingHostedAssignment)
            )
            == 0
        )


async def test_approval_and_assignment_identity_are_required(session_maker):
    authority = await _seed(session_maker, approve=False)
    async with session_maker() as session, session.begin():
        with pytest.raises(HostedAdmissionError, match="approval"):
            await create_hosted_assignment(
                session,
                authority=authority,
                confirmed_assignment_sha256="0" * 64,
                actor="test",
                reason="test approval",
            )
        row = await create_hosted_assignment(
            session,
            authority=authority,
            confirmed_assignment_sha256=authority.digest(),
            actor="test",
            reason="test approval",
        )
        replay = await create_hosted_assignment(
            session,
            authority=authority,
            confirmed_assignment_sha256=authority.digest(),
            actor="test",
            reason="test approval",
        )
        assert row.attempt_id == replay.attempt_id == authority.attempt_id
    altered = replace(authority, selection_sha256="5" * 64)
    async with session_maker() as session, session.begin():
        with pytest.raises(HostedAdmissionError, match="conflicts"):
            await create_hosted_assignment(
                session,
                authority=altered,
                confirmed_assignment_sha256=altered.digest(),
                actor="test",
                reason="test approval",
            )


async def test_concurrent_requests_admit_once_and_nonce_replay_is_rejected(
    session_maker,
):
    authority = await _seed(session_maker)
    requests = [_request(authority), _request(authority)]
    results = await asyncio.gather(
        *(_admit(session_maker, request) for request in requests)
    )
    assert sum(result.newly_admitted for result in results) == 1
    assert {result.attempt_id for result in results} == {authority.attempt_id}
    with pytest.raises(ValidatorRequestReplayError):
        await _admit(session_maker, requests[0])
    status = await _admit(session_maker, _request(authority, operation="status"))
    assert status.state == "admitted" and not status.newly_admitted
    assert set(asdict(status)) == {
        "evaluation_id",
        "attempt_id",
        "state",
        "newly_admitted",
        "newly_started",
    }


async def test_start_is_one_way_and_cannot_transfer_to_another_worker(session_maker):
    authority = await _seed(session_maker)
    await _admit(session_maker, _request(authority))
    worker = uuid4()
    async with session_maker() as session, session.begin():
        first = await start_hosted_attempt(
            session,
            evaluation_id=authority.evaluation_id,
            expected_attempt_id=authority.attempt_id,
            worker_id=worker,
        )
        assert first.newly_started
    async with session_maker() as session, session.begin():
        replay = await start_hosted_attempt(
            session,
            evaluation_id=authority.evaluation_id,
            expected_attempt_id=authority.attempt_id,
            worker_id=worker,
        )
        assert replay.state == "started" and not replay.newly_started
        with pytest.raises(HostedAdmissionError, match="another worker"):
            await start_hosted_attempt(
                session,
                evaluation_id=authority.evaluation_id,
                expected_attempt_id=authority.attempt_id,
                worker_id=uuid4(),
            )
    later = await _admit(session_maker, _request(authority))
    assert later.state == "started" and not later.newly_admitted
    for values in (
        {"started_at": None, "worker_id": None},
        {"artifact_sha256": "0" * 64},
        {"admitted_at": None},
    ):
        with pytest.raises(IntegrityError):
            async with session_maker() as session, session.begin():
                await session.execute(
                    update(CodingHostedAssignment)
                    .where(
                        CodingHostedAssignment.evaluation_id == authority.evaluation_id
                    )
                    .values(**values)
                )
    with pytest.raises(IntegrityError):
        async with session_maker() as session, session.begin():
            await session.execute(
                delete(CodingHostedAssignment).where(
                    CodingHostedAssignment.evaluation_id == authority.evaluation_id
                )
            )


async def test_retirement_and_artifact_drift_block_start(session_maker):
    authority = await _seed(session_maker)
    await _admit(session_maker, _request(authority))
    async with session_maker() as session, session.begin():
        await session.execute(
            update(Agent)
            .where(Agent.agent_id == authority.agent_id)
            .values(sha256="c" * 64)
        )
    with pytest.raises(HostedAdmissionError, match="screened artifact"):
        async with session_maker() as session, session.begin():
            await start_hosted_attempt(
                session,
                evaluation_id=authority.evaluation_id,
                expected_attempt_id=authority.attempt_id,
                worker_id=uuid4(),
            )
    async with session_maker() as session, session.begin():
        await append_private_v2_release_event(
            session,
            corpus_release_id="private-coding-v2-release-001",
            expected_registration_sha256=authority.registration_sha256,
            action="retired",
            reason="synthetic retirement",
            actor="test-operator",
        )
    with pytest.raises(HostedAdmissionError, match="release"):
        await _admit(session_maker, _request(authority))


async def test_invalid_signature_or_assignment_cannot_admit(session_maker):
    authority = await _seed(session_maker)
    for request in (
        _request(authority).model_copy(update={"signature": "0" * 128}),
        _request(authority, artifact_sha256="f" * 64),
        _request(authority, issued_at_unix=1, expires_at_unix=100),
    ):
        with pytest.raises(ValueError):
            await _admit(session_maker, request)
    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, authority.evaluation_id)
        assert row is not None and row.admitted_at is None


async def test_rollback_preserves_request_and_attempt_atomicity(session_maker):
    authority = await _seed(session_maker)
    request = _request(authority)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        async with session_maker() as session, session.begin():
            await admit_hosted_request(
                session,
                request=request,
                authenticated_validator=VALIDATOR.ss58_address,
                verifier=VALIDATOR,
            )
            raise RuntimeError("synthetic rollback")
    accepted = await _admit(session_maker, request)
    assert accepted.newly_admitted and accepted.attempt_id == authority.attempt_id


async def test_concurrent_workers_cannot_both_cross_start_boundary(session_maker):
    authority = await _seed(session_maker)
    await _admit(session_maker, _request(authority))

    async def claim():
        async with session_maker() as session, session.begin():
            return await start_hosted_attempt(
                session,
                evaluation_id=authority.evaluation_id,
                expected_attempt_id=authority.attempt_id,
                worker_id=uuid4(),
            )

    results = await asyncio.gather(claim(), claim(), return_exceptions=True)
    assert (
        sum(
            isinstance(result, HostedAdmissionView) and result.newly_started
            for result in results
        )
        == 1
    )
    assert sum(isinstance(result, HostedAdmissionError) for result in results) == 1


async def test_null_lifecycle_pairs_are_rejected_by_database(session_maker):
    authority = await _seed(session_maker)
    for values in (
        {"admission_request_sha256": "f" * 64},
        {"worker_id": uuid4()},
        {"started_at": datetime.now(UTC)},
    ):
        with pytest.raises(IntegrityError):
            async with session_maker() as session, session.begin():
                await session.execute(
                    update(CodingHostedAssignment)
                    .where(
                        CodingHostedAssignment.evaluation_id == authority.evaluation_id
                    )
                    .values(**values)
                )


async def test_status_does_not_admit_and_start_requires_admission(session_maker):
    authority = await _seed(session_maker)
    status = await _admit(session_maker, _request(authority, operation="status"))
    assert status.state == "assigned" and not status.newly_admitted
    with pytest.raises(HostedAdmissionError, match="not admitted"):
        async with session_maker() as session, session.begin():
            await start_hosted_attempt(
                session,
                evaluation_id=authority.evaluation_id,
                expected_attempt_id=authority.attempt_id,
                worker_id=uuid4(),
            )
    with pytest.raises(HostedAdmissionError, match="acknowledgement"):
        await _admit(
            session_maker,
            _request(authority, operation="acknowledge", result_sha256="0" * 64),
        )
