"""Postgres tests for qualified coding-certification lease issuance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import bittensor
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.coding_certification_leases import CodingCertificationLeaseStatus
from ditto.api_models.core_qualification import CoreQualificationPolicy
from ditto.api_server.coding_certification_canary import public_certification_canary
from ditto.db.models import Agent, CoreQualificationObservation
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseConflictError,
    CodingCertificationLeaseNotAvailableError,
    abort_coding_certification_lease,
    claim_coding_certification_lease,
    get_coding_certification_lease,
    issue_coding_certification_lease,
)
from ditto.db.queries.core_qualification import (
    insert_core_qualification_policy,
    latest_core_qualification_policy,
)

_BENCH_VERSION = 812
_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
_VALIDATOR = bittensor.Keypair.create_from_uri("//Alice").ss58_address
_OTHER_VALIDATOR = bittensor.Keypair.create_from_uri("//Bob").ss58_address


def _policy() -> CoreQualificationPolicy:
    return CoreQualificationPolicy(
        schema="ditto-core-qualification-policy-v1",
        weight_eligible=False,
        bench_version=_BENCH_VERSION,
        enter_composite=0.8,
        enter_tool_mean=0.8,
        enter_memory_mean=0.8,
        exit_composite=0.7,
        exit_tool_mean=0.7,
        exit_memory_mean=0.7,
        enter_observations=2,
        exit_observations=2,
    )


async def _seed_agent(session: AsyncSession, *, screened: bool = True) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey=bittensor.Keypair.create_from_uri("//Charlie").ss58_address,
        name="coding-certification-lease-agent",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
        screened_image_sha256="cd" * 32 if screened else None,
        screened_image_size_bytes=1234 if screened else None,
        screened_image_id="sha256:" + "ef" * 32 if screened else None,
        screened_image_ref="ditto-screen/coding-cert-lease:latest"
        if screened
        else None,
        screened_image_upload_id=uuid4() if screened else None,
        screened_image_verified_at=_NOW if screened else None,
        created_at=_NOW,
    )
    async with session.begin():
        session.add(agent)
    return agent


async def _seed_observation(
    session: AsyncSession,
    agent: Agent,
    *,
    qualified: bool = True,
    complete_wave: bool = True,
    evidence_sha256: str = "11" * 32,
) -> CoreQualificationObservation:
    async with session.begin():
        policy = await latest_core_qualification_policy(
            session, bench_version=_BENCH_VERSION
        )
        if policy is None:
            policy = await insert_core_qualification_policy(
                session,
                parent_revision=0,
                policy=_policy(),
                reason="start shadow qualification",
                actor="test-admin",
            )
        decision = (
            "entered"
            if qualified and complete_wave
            else ("partial_wave" if not complete_wave else "below_entry")
        )
        observation = CoreQualificationObservation(
            observation_id=uuid4(),
            agent_id=agent.agent_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=agent.screened_image_sha256 or "cd" * 32,
            bench_version=_BENCH_VERSION,
            policy_revision=policy.revision,
            policy_checksum=policy.checksum,
            score_evidence_sha256=evidence_sha256,
            score_count=3,
            full_size=True,
            complete_wave=complete_wave,
            score_evidence={"scores": []},
            median_composite=0.9 if qualified else 0.1,
            median_tool_mean=0.9 if qualified else 0.1,
            median_memory_mean=0.9 if qualified else 0.1,
            entry_passed=qualified,
            retention_passed=qualified,
            qualified=qualified,
            enter_streak=2 if qualified else 0,
            exit_streak=0,
            decision=decision,
            source="score_commit",
            actor=None,
            reason=None,
            weight_eligible=False,
            observed_at=_NOW,
        )
        session.add(observation)
    return observation


async def test_issue_requires_current_complete_qualification(
    session: AsyncSession,
) -> None:
    agent = await _seed_agent(session)
    async with session.begin():
        with pytest.raises(CodingCertificationLeaseNotAvailableError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
            )
    await _seed_observation(session, agent, qualified=False)
    async with session.begin():
        with pytest.raises(CodingCertificationLeaseNotAvailableError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
            )


async def test_issue_claim_abort_and_stale_artifact_are_fail_closed(
    session: AsyncSession,
) -> None:
    agent = await _seed_agent(session)
    await _seed_observation(session, agent)
    canary = public_certification_canary()
    async with session.begin():
        issued = await issue_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            agent_id=agent.agent_id,
            bench_version=_BENCH_VERSION,
        )
    assert issued.idempotent is False
    assert issued.row.status == CodingCertificationLeaseStatus.ISSUED.value
    assert issued.row.weight_eligible is False
    assert issued.authority.canary_manifest_sha256 == canary.canary_manifest_sha256
    assert issued.authority.inference_policy_sha256 == canary.inference_policy_sha256
    assert issued.row.deadline - issued.row.issued_at == timedelta(minutes=20)

    async with session.begin():
        replay = await issue_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            agent_id=agent.agent_id,
            bench_version=_BENCH_VERSION,
        )
        with pytest.raises(CodingCertificationLeaseConflictError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_OTHER_VALIDATOR,
                agent_id=agent.agent_id,
                bench_version=_BENCH_VERSION,
            )
    assert replay.idempotent is True
    assert replay.row.lease_id == issued.row.lease_id

    async with session.begin():
        claimed = await claim_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            lease_id=issued.row.lease_id,
        )
        claimed_again = await claim_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            lease_id=issued.row.lease_id,
        )
        with pytest.raises(CodingCertificationLeaseConflictError):
            await abort_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                lease_id=issued.row.lease_id,
            )
    assert claimed.idempotent is False
    assert claimed.row.status == CodingCertificationLeaseStatus.CLAIMED.value
    assert claimed_again.idempotent is True

    fresh = await _seed_agent(session)
    await _seed_observation(session, fresh)
    async with session.begin():
        abortable = await issue_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            agent_id=fresh.agent_id,
            bench_version=_BENCH_VERSION,
        )
        aborted = await abort_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            lease_id=abortable.row.lease_id,
        )
        reissued = await issue_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            agent_id=fresh.agent_id,
            bench_version=_BENCH_VERSION,
        )
    assert aborted.row.status == CodingCertificationLeaseStatus.ABORTED.value
    assert reissued.row.lease_id != abortable.row.lease_id

    stale = await _seed_agent(session)
    await _seed_observation(session, stale)
    async with session.begin():
        agent_row = await session.get(Agent, stale.agent_id)
        assert agent_row is not None
        agent_row.sha256 = "99" * 32
        with pytest.raises(CodingCertificationLeaseNotAvailableError):
            await issue_coding_certification_lease(
                session,
                validator_hotkey=_VALIDATOR,
                agent_id=stale.agent_id,
                bench_version=_BENCH_VERSION,
            )


async def test_issue_uses_latest_complete_observation(
    session: AsyncSession,
) -> None:
    agent = await _seed_agent(session)
    complete = await _seed_observation(session, agent)
    await _seed_observation(
        session,
        agent,
        qualified=False,
        complete_wave=False,
        evidence_sha256="22" * 32,
    )
    async with session.begin():
        issued = await issue_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            agent_id=agent.agent_id,
            bench_version=_BENCH_VERSION,
        )
    assert issued.row.status == CodingCertificationLeaseStatus.ISSUED.value
    assert issued.row.core_qualification_observation_id == complete.observation_id


async def test_expired_claim_and_abort_persist_expired(
    session: AsyncSession,
) -> None:
    claim_agent = await _seed_agent(session)
    await _seed_observation(session, claim_agent)
    async with session.begin():
        claimable = await issue_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            agent_id=claim_agent.agent_id,
            bench_version=_BENCH_VERSION,
        )
        past_issued = datetime.now(UTC) - timedelta(minutes=25)
        claimable.row.issued_at = past_issued
        claimable.row.deadline = past_issued + timedelta(minutes=20)

    async with session.begin():
        expired_claim = await claim_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            lease_id=claimable.row.lease_id,
        )
    assert expired_claim.row.status == CodingCertificationLeaseStatus.EXPIRED.value
    async with session.begin():
        stored = await get_coding_certification_lease(
            session, lease_id=claimable.row.lease_id
        )
    assert stored is not None
    assert stored.status == CodingCertificationLeaseStatus.EXPIRED.value

    abort_agent = await _seed_agent(session)
    await _seed_observation(session, abort_agent, evidence_sha256="33" * 32)
    async with session.begin():
        abortable = await issue_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            agent_id=abort_agent.agent_id,
            bench_version=_BENCH_VERSION,
        )
        past_issued = datetime.now(UTC) - timedelta(minutes=25)
        abortable.row.issued_at = past_issued
        abortable.row.deadline = past_issued + timedelta(minutes=20)

    async with session.begin():
        expired_abort = await abort_coding_certification_lease(
            session,
            validator_hotkey=_VALIDATOR,
            lease_id=abortable.row.lease_id,
        )
    assert expired_abort.row.status == CodingCertificationLeaseStatus.EXPIRED.value
    async with session.begin():
        stored_abort = await get_coding_certification_lease(
            session, lease_id=abortable.row.lease_id
        )
    assert stored_abort is not None
    assert stored_abort.status == CodingCertificationLeaseStatus.EXPIRED.value
