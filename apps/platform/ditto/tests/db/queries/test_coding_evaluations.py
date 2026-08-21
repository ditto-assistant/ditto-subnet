"""Real-Postgres tests for the separate shadow coding evaluation ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.coding_evaluation import (
    CodingRunEvidence,
    CodingShadowRunAuthority,
    coding_run_evidence_digest,
)
from ditto.api_models.core_qualification import CoreQualificationPolicy
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingShadowResult,
    CodingShadowTicket,
)
from ditto.db.queries.coding_evaluations import (
    CodingShadowConflictError,
    CodingShadowNotQualifiedError,
    insert_coding_shadow_result,
    insert_coding_shadow_run,
    issue_coding_shadow_ticket,
)
from ditto.db.queries.core_qualification import (
    insert_core_qualification_policy,
    observe_core_qualification,
)
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, upsert_score

_NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)
_BENCH = 12
_VALIDATOR = "5" + "B" * 47


def _evidence(ticket_id) -> CodingRunEvidence:
    vector = json.loads(
        (
            Path(__file__).parents[6]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_contract_v1.json"
        ).read_text(encoding="utf-8")
    )["run_evidence"]
    vector["validator_ticket_id"] = str(ticket_id)
    return CodingRunEvidence.model_validate_json(json.dumps(vector))


def _authority(agent_id) -> CodingShadowRunAuthority:
    return CodingShadowRunAuthority(
        schema="dittobench-coding-shadow-run-authority-v1",
        bench_family="coding",
        coding_contract_version=1,
        weight_eligible=False,
        bench_version=_BENCH,
        coding_run_id="coding-run-001",
        agent_id=agent_id,
        agent_artifact_sha256="ab" * 32,
        screened_image_sha256="cd" * 32,
        corpus_release_id="private-coding-corpus-v1",
        catalog_merkle_root="11" * 32,
        selection_derivation_id="coding-selection-v1",
        selection_chain_genesis_hash="0x" + "22" * 32,
        selection_block_number=123,
        selection_block_hash="0x" + "33" * 32,
        inference_grant_sha256="01" * 32,
        grader_contract_sha256="99" * 32,
        task_set_id="task-set-001",
        task_set_manifest_sha256="dd" * 32,
        run_manifest_sha256=(
            "e7b431a640aca1f35a5cabe7341ee0aaba25ccee9a174ef0c8f4ab0f3ff80dc4"
        ),
        task_count=1,
    )


async def _seed_qualified_agent(session: AsyncSession) -> Agent:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5CodingShadowMiner11111111111111111111111111111111",
        name="coding-shadow-agent",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
        screened_image_sha256="cd" * 32,
        screened_image_size_bytes=1234,
        screened_image_id="sha256:" + "ef" * 32,
        screened_image_ref="ditto-screen/coding-shadow:latest",
        screened_image_upload_id=uuid4(),
        screened_image_verified_at=_NOW,
        created_at=_NOW,
    )
    async with session.begin():
        session.add(agent)
        await insert_core_qualification_policy(
            session,
            parent_revision=0,
            policy=CoreQualificationPolicy(
                schema="ditto-core-qualification-policy-v1",
                weight_eligible=False,
                bench_version=_BENCH,
                enter_composite=0.8,
                enter_tool_mean=0.8,
                enter_memory_mean=0.8,
                exit_composite=0.7,
                exit_tool_mean=0.7,
                exit_memory_mean=0.7,
                enter_observations=1,
                exit_observations=2,
            ),
            reason="calibrate coding shadow admission",
            actor="test-admin",
        )
        for index in range(3):
            await upsert_score(
                session,
                agent_id=agent.agent_id,
                validator_hotkey=f"core-validator-{index}",
                bench_version=_BENCH,
                run_id=f"core-run-{index}",
                seed=index,
                composite=0.9,
                tool_mean=0.9,
                memory_mean=0.9,
                median_ms=100,
                n=MIN_ELIGIBLE_CASES,
                generated_at=_NOW,
                signature=(f"{index + 1:02x}" * 64),
            )
        observed = await observe_core_qualification(
            session,
            agent_id=agent.agent_id,
            bench_version=_BENCH,
            now=_NOW,
        )
        assert observed is not None and observed.row.qualified
    return agent


async def _seed_certification(session: AsyncSession, agent: Agent) -> None:
    async with session.begin():
        session.add(
            CodingCapabilityCertification(
                certification_row_id=uuid4(),
                agent_id=agent.agent_id,
                artifact_sha256=agent.sha256,
                screened_image_sha256="cd" * 32,
                validator_hotkey=_VALIDATOR,
                bench_version=_BENCH,
                ticket_deadline=_NOW + timedelta(hours=1),
                coding_contract_version=1,
                certification_id="cert-coding-shadow-001",
                status="certified",
                failure_stage=None,
                failure_code=None,
                certification_sha256="44" * 32,
                canary_manifest_sha256="55" * 32,
                transcript_object_key="sha256/" + "66" * 32,
                frozen_submission_object_key="sha256/" + "77" * 32,
                issued_at=_NOW - timedelta(minutes=5),
                expires_at=_NOW + timedelta(hours=2),
                weight_eligible=False,
                receipt={},
                signature="88" * 64,
                created_at=_NOW,
            )
        )


async def test_run_requires_core_qualification(session: AsyncSession) -> None:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5UnqualifiedCodingMiner1111111111111111111111111111",
        name="unqualified-coding-agent",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
        screened_image_sha256="cd" * 32,
        screened_image_size_bytes=1234,
        screened_image_id="sha256:" + "ef" * 32,
        screened_image_ref="ditto-screen/unqualified-coding:latest",
        screened_image_upload_id=uuid4(),
        screened_image_verified_at=_NOW,
        created_at=_NOW,
    )
    async with session.begin():
        session.add(agent)
    with pytest.raises(CodingShadowNotQualifiedError, match="core qualification"):
        async with session.begin():
            await insert_coding_shadow_run(
                session, authority=_authority(agent.agent_id)
            )


async def test_shadow_run_ticket_and_result_are_separate_and_idempotent(
    session: AsyncSession,
) -> None:
    agent = await _seed_qualified_agent(session)
    await _seed_certification(session, agent)
    authority = _authority(agent.agent_id)
    async with session.begin():
        created = await insert_coding_shadow_run(session, authority=authority)
    assert created.idempotent is False
    async with session.begin():
        replay = await insert_coding_shadow_run(session, authority=authority)
    assert replay.idempotent is True

    ticket_id = uuid4()
    async with session.begin():
        issued = await issue_coding_shadow_ticket(
            session,
            run_row_id=created.row.run_row_id,
            ticket_id=ticket_id,
            validator_hotkey=_VALIDATOR,
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=1),
        )
    assert issued.idempotent is False
    assert isinstance(issued.row, CodingShadowTicket)
    async with session.begin():
        ticket_replay = await issue_coding_shadow_ticket(
            session,
            run_row_id=created.row.run_row_id,
            ticket_id=ticket_id,
            validator_hotkey=_VALIDATOR,
            issued_at=_NOW,
            deadline=_NOW + timedelta(hours=1),
        )
    assert ticket_replay.idempotent is True
    async with session.begin():
        await insert_core_qualification_policy(
            session,
            parent_revision=1,
            policy=CoreQualificationPolicy(
                schema="ditto-core-qualification-policy-v1",
                weight_eligible=False,
                bench_version=_BENCH,
                enter_composite=0.85,
                enter_tool_mean=0.85,
                enter_memory_mean=0.85,
                exit_composite=0.75,
                exit_tool_mean=0.75,
                exit_memory_mean=0.75,
                enter_observations=1,
                exit_observations=2,
            ),
            reason="raise shadow admission after calibration",
            actor="test-admin",
        )
    with pytest.raises(CodingShadowNotQualifiedError, match="core qualification"):
        async with session.begin():
            await issue_coding_shadow_ticket(
                session,
                run_row_id=created.row.run_row_id,
                ticket_id=uuid4(),
                validator_hotkey=_VALIDATOR,
                issued_at=_NOW,
                deadline=_NOW + timedelta(hours=1),
            )
    async with session.begin():
        stale_run_replay = await insert_coding_shadow_run(
            session,
            authority=authority,
        )
    assert stale_run_replay.idempotent is True
    evidence = _evidence(ticket_id)
    digest = coding_run_evidence_digest(evidence)
    async with session.begin():
        stored_ticket = await session.get(CodingShadowTicket, ticket_id)
        assert stored_ticket is not None
        result = await insert_coding_shadow_result(
            session,
            ticket=stored_ticket,
            evidence=evidence,
            run_evidence_sha256=digest,
            signature="99" * 64,
        )
    assert result.idempotent is False
    assert isinstance(result.row, CodingShadowResult)
    assert result.row.repair_mean_micros == 1_000_000
    assert result.row.weight_eligible is False
    async with session.begin():
        stored_ticket = await session.get(CodingShadowTicket, ticket_id)
        assert stored_ticket is not None
        replayed = await insert_coding_shadow_result(
            session,
            ticket=stored_ticket,
            evidence=evidence,
            run_evidence_sha256=digest,
            signature="99" * 64,
        )
    assert replayed.idempotent is True

    changed = evidence.model_copy(update={"run_manifest_sha256": "ff" * 32})
    with pytest.raises(CodingShadowConflictError):
        async with session.begin():
            stored_ticket = await session.get(CodingShadowTicket, ticket_id)
            assert stored_ticket is not None
            await insert_coding_shadow_result(
                session,
                ticket=stored_ticket,
                evidence=changed,
                run_evidence_sha256=coding_run_evidence_digest(changed),
                signature="99" * 64,
            )

    async with session.begin():
        stored_agent = await session.get(Agent, authority.agent_id)
        assert stored_agent is not None
        await session.delete(stored_agent)
    async with session.begin():
        assert (
            await session.scalar(select(func.count()).select_from(CodingShadowResult))
            == 0
        )
