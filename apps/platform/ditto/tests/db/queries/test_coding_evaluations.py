"""Real-Postgres tests for the separate shadow coding evaluation ledger."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.coding_catalog import (
    CodingCatalogCommitment,
    CodingCatalogTaskExposure,
)
from ditto.api_models.coding_evaluation import (
    CodingRunEvidence,
    CodingShadowRunAuthority,
    coding_run_evidence_digest,
)
from ditto.api_models.core_qualification import CoreQualificationPolicy
from ditto.chain.models import BlockInfo
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingCatalogExposure,
    CodingCatalogRelease,
    CodingSelectionAssignmentRow,
    CodingShadowResult,
    CodingShadowRun,
    CodingShadowTicket,
)
from ditto.db.queries.coding_assignments import (
    CodingAssignmentConflictError,
    CodingAssignmentNotQualifiedError,
    CodingAssignmentPolicy,
    create_coding_selection_assignment,
)
from ditto.db.queries.coding_catalog import (
    CodingCatalogConflictError,
    CodingCatalogInactiveError,
    expose_coding_shadow_run_tasks,
    insert_coding_catalog_release,
    retire_coding_catalog_release,
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


class _FinalizedBlocks:
    def __init__(
        self,
        *,
        anchor_number: int = 1_000,
        barrier: asyncio.Barrier | None = None,
    ) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.anchor_number = anchor_number
        self.barrier = barrier

    async def get_finalized_block_hash(self, block_number: int) -> str:
        self.calls.append(("hash", block_number))
        if block_number == 0:
            return "0x" + "22" * 32
        return "0x" + "33" * 32

    async def get_finalized_block(self) -> BlockInfo:
        self.calls.append(("head", None))
        if self.barrier is not None:
            await self.barrier.wait()
        return BlockInfo(number=self.anchor_number, hash="0x" + "ff" * 32)


def _catalog_commitment(
    *,
    corpus_release_id: str = "private-coding-corpus-v1",
    committed_at_unix: int | None = None,
) -> CodingCatalogCommitment:
    values: dict[str, object] = {
        "schema": "dittobench-coding-catalog-commitment-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "corpus_release_id": corpus_release_id,
        "catalog_merkle_root": "11" * 32,
        "selection_derivation_id": "coding-selection-v1",
        "selection_chain_genesis_hash": "0x" + "22" * 32,
        "grader_contract_sha256": "99" * 32,
        "inference_grant_sha256": "01" * 32,
        "task_version_count": 100,
        "curator_hotkey": "5" + "A" * 47,
        "committed_at_unix": (
            int(_NOW.timestamp()) if committed_at_unix is None else committed_at_unix
        ),
    }
    body = (
        json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()
    values["commitment_sha256"] = hashlib.sha256(body).hexdigest()
    return CodingCatalogCommitment.model_validate(values)


def _task_exposure(
    *,
    task_version_id: str = "private-task-v1",
    manifest_index: int = 0,
) -> CodingCatalogTaskExposure:
    return CodingCatalogTaskExposure(
        manifest_index=manifest_index,
        task_version_id=task_version_id,
        task_commitment_sha256="aa" * 32,
        selection_proof_sha256="ab" * 32,
        catalog_membership_proof_sha256="ac" * 32,
        visible_bundle_sha256="ee" * 32,
        base_tree_sha256="ff" * 32,
        memory_bundle_sha256=(
            "b943e6586a202b2ede36cee985e1ebb76d2dc0ab2734e30c174763f81bf51f53"
        ),
        environment_image_digest="sha256:" + "11" * 32,
        resource_profile_sha256=(
            "5d01f16bedb5af936e58d79f7ebc9ca0356dcadb89c06607ba27131a7d3ba8e6"
        ),
        grader_bundle_sha256="33" * 32,
        grader_image_digest="sha256:" + "44" * 32,
        test_manifest_sha256="55" * 32,
        grader_plan_sha256=(
            "cb517c8d7b85cfbe1277a78cf0124b0440544aae6b692ce78ff647b2b5570c3e"
        ),
    )


async def _seed_catalog(session: AsyncSession) -> datetime:
    async with session.begin():
        inserted = await insert_coding_catalog_release(
            session,
            commitment=_catalog_commitment(),
            signature="88" * 64,
            reason="register private shadow catalog",
            actor="test-admin",
        )
    return inserted.row.created_at


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


async def _seed_qualified_agent(
    session: AsyncSession,
    *,
    created_at: datetime = _NOW,
) -> Agent:
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
        created_at=created_at,
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


async def _seed_certification(session: AsyncSession, agent_id: UUID) -> None:
    async with session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        now = datetime.now(UTC)
        session.add(
            CodingCapabilityCertification(
                certification_row_id=uuid4(),
                agent_id=agent.agent_id,
                artifact_sha256=agent.sha256,
                screened_image_sha256=agent.screened_image_sha256,
                validator_hotkey=_VALIDATOR,
                bench_version=_BENCH,
                ticket_deadline=now + timedelta(hours=1),
                coding_contract_version=1,
                certification_id="cert-coding-shadow-001",
                status="certified",
                failure_stage=None,
                failure_code=None,
                certification_sha256="44" * 32,
                canary_manifest_sha256="55" * 32,
                transcript_object_key="sha256/" + "66" * 32,
                frozen_submission_object_key="sha256/" + "77" * 32,
                issued_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(hours=2),
                weight_eligible=False,
                receipt={},
                signature="88" * 64,
                created_at=now,
            )
        )


async def test_selection_assignment_is_future_bound_idempotent_and_append_only(
    session: AsyncSession,
) -> None:
    registered_at = await _seed_catalog(session)
    agent = await _seed_qualified_agent(
        session,
        created_at=registered_at + timedelta(seconds=1),
    )
    await _seed_certification(session, agent.agent_id)
    source = _FinalizedBlocks()
    policy = CodingAssignmentPolicy(selection_delay_blocks=20)

    async with session.begin():
        transaction_started_at = await session.scalar(
            select(func.transaction_timestamp())
        )
        assert transaction_started_at is not None
        await asyncio.sleep(0.01)
        first = await create_coding_selection_assignment(
            session,
            finalized_source=source,
            agent_id=agent.agent_id,
            bench_version=_BENCH,
            coding_run_id="coding-assignment-001",
            corpus_release_id="private-coding-corpus-v1",
            policy=policy,
        )
    assert first.idempotent is False
    assert first.assignment.anchor_block_number == 1_000
    assert first.assignment.anchor_block_hash == "0x" + "33" * 32
    assert first.assignment.selection_block_number == 1_020
    assert first.assignment.assigned_at == first.row.created_at
    assert first.row.created_at > transaction_started_at
    assert source.calls == [("hash", 0), ("head", None), ("hash", 1_000)]
    assignment_row_id = first.row.assignment_row_id

    source.calls.clear()
    async with session.begin():
        replay = await create_coding_selection_assignment(
            session,
            finalized_source=source,
            agent_id=agent.agent_id,
            bench_version=_BENCH,
            coding_run_id="coding-assignment-001",
            corpus_release_id="private-coding-corpus-v1",
            policy=policy,
        )
    assert replay.idempotent is True
    assert replay.assignment == first.assignment
    assert source.calls == []

    with pytest.raises(CodingAssignmentConflictError):
        async with session.begin():
            await create_coding_selection_assignment(
                session,
                finalized_source=source,
                agent_id=agent.agent_id,
                bench_version=_BENCH,
                coding_run_id="coding-assignment-second-height",
                corpus_release_id="private-coding-corpus-v1",
                policy=policy,
            )

    with pytest.raises(SAIntegrityError):
        async with session.begin():
            row = await session.get(
                CodingSelectionAssignmentRow,
                assignment_row_id,
                with_for_update=True,
            )
            assert row is not None
            row.selection_delay_blocks = 21
            await session.flush()
    with pytest.raises(SAIntegrityError):
        async with session.begin():
            row = await session.get(
                CodingSelectionAssignmentRow,
                assignment_row_id,
                with_for_update=True,
            )
            assert row is not None
            await session.delete(row)
            await session.flush()


async def test_selection_assignment_requires_all_shadow_authority(
    session: AsyncSession,
) -> None:
    registered_at = await _seed_catalog(session)
    agent = await _seed_qualified_agent(
        session,
        created_at=registered_at + timedelta(seconds=1),
    )
    agent_id = agent.agent_id
    source = _FinalizedBlocks()
    policy = CodingAssignmentPolicy(selection_delay_blocks=20)

    with pytest.raises(CodingAssignmentNotQualifiedError, match="certification"):
        async with session.begin():
            await create_coding_selection_assignment(
                session,
                finalized_source=source,
                agent_id=agent_id,
                bench_version=_BENCH,
                coding_run_id="coding-assignment-no-cert",
                corpus_release_id="private-coding-corpus-v1",
                policy=policy,
            )

    await _seed_certification(session, agent_id)
    async with session.begin():
        created = await create_coding_selection_assignment(
            session,
            finalized_source=source,
            agent_id=agent_id,
            bench_version=_BENCH,
            coding_run_id="coding-assignment-conflict",
            corpus_release_id="private-coding-corpus-v1",
            policy=policy,
        )
    async with session.begin():
        current = await session.get(Agent, agent_id, with_for_update=True)
        assert current is not None
        current.sha256 = "ef" * 32
    with pytest.raises(CodingAssignmentConflictError):
        async with session.begin():
            await create_coding_selection_assignment(
                session,
                finalized_source=source,
                agent_id=agent_id,
                bench_version=_BENCH,
                coding_run_id=created.assignment.coding_run_id,
                corpus_release_id="private-coding-corpus-v1",
                policy=policy,
            )


async def test_concurrent_assignment_attempts_cannot_grind_multiple_heights(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    registered_at = await _seed_catalog(session)
    agent = await _seed_qualified_agent(
        session,
        created_at=registered_at + timedelta(seconds=1),
    )
    await _seed_certification(session, agent.agent_id)
    barrier = asyncio.Barrier(2)

    async def attempt(*, coding_run_id: str, anchor_number: int) -> str:
        source = _FinalizedBlocks(anchor_number=anchor_number, barrier=barrier)
        try:
            async with session_maker() as attempt_session, attempt_session.begin():
                result = await create_coding_selection_assignment(
                    attempt_session,
                    finalized_source=source,
                    agent_id=agent.agent_id,
                    bench_version=_BENCH,
                    coding_run_id=coding_run_id,
                    corpus_release_id="private-coding-corpus-v1",
                    policy=CodingAssignmentPolicy(selection_delay_blocks=20),
                )
        except CodingAssignmentConflictError:
            return "conflict"
        return f"created:{result.assignment.selection_block_number}"

    outcomes = await asyncio.gather(
        attempt(coding_run_id="coding-race-a", anchor_number=1_000),
        attempt(coding_run_id="coding-race-b", anchor_number=2_000),
    )
    assert sorted(outcome.split(":", 1)[0] for outcome in outcomes) == [
        "conflict",
        "created",
    ]
    async with session_maker() as probe:
        assert (
            await probe.scalar(
                select(func.count())
                .select_from(CodingSelectionAssignmentRow)
                .where(CodingSelectionAssignmentRow.agent_id == agent.agent_id)
            )
            == 1
        )


async def test_selection_assignment_rejects_stale_core_policy(
    session: AsyncSession,
) -> None:
    registered_at = await _seed_catalog(session)
    agent = await _seed_qualified_agent(
        session,
        created_at=registered_at + timedelta(seconds=1),
    )
    await _seed_certification(session, agent.agent_id)
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
            reason="revise shadow coding qualification floors",
            actor="test-admin",
        )
    with pytest.raises(CodingAssignmentNotQualifiedError, match="core qualification"):
        async with session.begin():
            await create_coding_selection_assignment(
                session,
                finalized_source=_FinalizedBlocks(),
                agent_id=agent.agent_id,
                bench_version=_BENCH,
                coding_run_id="coding-assignment-stale-core",
                corpus_release_id="private-coding-corpus-v1",
                policy=CodingAssignmentPolicy(selection_delay_blocks=20),
            )


async def test_run_requires_core_qualification(session: AsyncSession) -> None:
    registered_at = await _seed_catalog(session)
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
        created_at=registered_at + timedelta(seconds=1),
    )
    async with session.begin():
        session.add(agent)
    with pytest.raises(CodingShadowNotQualifiedError, match="core qualification"):
        async with session.begin():
            await insert_coding_shadow_run(
                session, authority=_authority(agent.agent_id)
            )


async def test_run_requires_registered_active_catalog(session: AsyncSession) -> None:
    agent = await _seed_qualified_agent(session)
    with pytest.raises(CodingCatalogInactiveError, match="active catalog"):
        async with session.begin():
            await insert_coding_shadow_run(
                session,
                authority=_authority(agent.agent_id),
            )


async def test_catalog_commitment_must_predate_candidate_artifact(
    session: AsyncSession,
) -> None:
    agent = await _seed_qualified_agent(session)
    commitment = _catalog_commitment(
        corpus_release_id="late-private-coding-corpus-v1",
        committed_at_unix=int(_NOW.timestamp()) + 1,
    )
    async with session.begin():
        await insert_coding_catalog_release(
            session,
            commitment=commitment,
            signature="88" * 64,
            reason="register deliberately late catalog",
            actor="test-admin",
        )
    authority_values = _authority(agent.agent_id).model_dump(mode="json", by_alias=True)
    authority_values["corpus_release_id"] = commitment.corpus_release_id
    late_authority = CodingShadowRunAuthority.model_validate(authority_values)
    with pytest.raises(CodingCatalogInactiveError, match="predate"):
        async with session.begin():
            await insert_coding_shadow_run(session, authority=late_authority)


async def test_shadow_run_ticket_and_result_are_separate_and_idempotent(
    session: AsyncSession,
) -> None:
    registered_at = await _seed_catalog(session)
    agent = await _seed_qualified_agent(
        session,
        created_at=registered_at + timedelta(seconds=1),
    )
    await _seed_certification(session, agent.agent_id)
    authority = _authority(agent.agent_id)
    async with session.begin():
        created = await insert_coding_shadow_run(session, authority=authority)
        assert isinstance(created.row, CodingShadowRun)
        run_row_id = created.row.run_row_id
    assert created.idempotent is False
    async with session.begin():
        replay = await insert_coding_shadow_run(session, authority=authority)
    assert replay.idempotent is True

    ticket_id = uuid4()
    with pytest.raises(CodingShadowNotQualifiedError, match="catalog exposure"):
        async with session.begin():
            await issue_coding_shadow_ticket(
                session,
                run_row_id=run_row_id,
                ticket_id=ticket_id,
                validator_hotkey=_VALIDATOR,
                issued_at=_NOW,
                deadline=_NOW + timedelta(hours=1),
            )
    async with session.begin():
        exposed = await expose_coding_shadow_run_tasks(
            session,
            run_row_id=run_row_id,
            exposures=[_task_exposure()],
        )
    assert exposed.idempotent is False
    async with session.begin():
        issued = await issue_coding_shadow_ticket(
            session,
            run_row_id=run_row_id,
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
            run_row_id=run_row_id,
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
                run_row_id=run_row_id,
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

    with pytest.raises(SAIntegrityError):
        async with session.begin():
            stored_agent = await session.get(Agent, authority.agent_id)
            assert stored_agent is not None
            await session.delete(stored_agent)
    async with session.begin():
        assert (
            await session.scalar(select(func.count()).select_from(CodingShadowResult))
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(CodingCatalogExposure)
            )
            == 1
        )


async def test_catalog_exposure_is_single_use_and_retirement_is_terminal(
    session: AsyncSession,
) -> None:
    await _seed_catalog(session)
    second_commitment = _catalog_commitment(
        corpus_release_id="private-coding-corpus-v2",
    )
    async with session.begin():
        second_catalog = await insert_coding_catalog_release(
            session,
            commitment=second_commitment,
            signature="77" * 64,
            reason="register successor private catalog",
            actor="test-admin",
        )
    agent = await _seed_qualified_agent(
        session,
        created_at=second_catalog.row.created_at + timedelta(seconds=1),
    )
    authority = _authority(agent.agent_id)
    async with session.begin():
        first_run = await insert_coding_shadow_run(session, authority=authority)
        assert isinstance(first_run.row, CodingShadowRun)
        first_run_row_id = first_run.row.run_row_id
        first_exposure = await expose_coding_shadow_run_tasks(
            session,
            run_row_id=first_run_row_id,
            exposures=[_task_exposure()],
        )
    assert first_exposure.idempotent is False

    # A catalog retirement eventually permits public corpus disclosure.  The
    # task-version namespace is therefore global: changing the catalog release
    # must not make an already exposed identifier eligible again.
    second_catalog_authority = authority.model_copy(
        update={
            "coding_run_id": "coding-run-global-reuse-v2",
            "corpus_release_id": second_commitment.corpus_release_id,
            "catalog_merkle_root": second_commitment.catalog_merkle_root,
            "task_set_id": "task-set-global-reuse-v2",
            "task_set_manifest_sha256": "14" * 32,
            "run_manifest_sha256": "15" * 32,
        }
    )
    async with session.begin():
        second_catalog_run = await insert_coding_shadow_run(
            session,
            authority=second_catalog_authority,
        )
        assert isinstance(second_catalog_run.row, CodingShadowRun)
        second_catalog_run_row_id = second_catalog_run.row.run_row_id
    with pytest.raises(CodingCatalogConflictError, match="already exposed"):
        async with session.begin():
            await expose_coding_shadow_run_tasks(
                session,
                run_row_id=second_catalog_run_row_id,
                exposures=[_task_exposure()],
            )

    second_authority = authority.model_copy(
        update={
            "coding_run_id": "coding-run-002",
            "task_set_id": "task-set-002",
            "task_set_manifest_sha256": "12" * 32,
            "run_manifest_sha256": "13" * 32,
            "task_count": 2,
        }
    )
    async with session.begin():
        second_run = await insert_coding_shadow_run(
            session,
            authority=second_authority,
        )
        assert isinstance(second_run.row, CodingShadowRun)
        second_run_row_id = second_run.row.run_row_id
    with pytest.raises(CodingCatalogConflictError, match="already exposed"):
        async with session.begin():
            await expose_coding_shadow_run_tasks(
                session,
                run_row_id=second_run_row_id,
                exposures=[
                    _task_exposure(task_version_id="private-task-v2"),
                    _task_exposure(manifest_index=1),
                ],
            )
    async with session.begin():
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CodingCatalogExposure)
                .where(CodingCatalogExposure.run_row_id == second_run_row_id)
            )
            == 0
        )

    commitment = _catalog_commitment()
    async with session.begin():
        retired = await retire_coding_catalog_release(
            session,
            corpus_release_id=commitment.corpus_release_id,
            expected_commitment_sha256=commitment.commitment_sha256,
            reason="retire after shadow task exposure",
            actor="test-admin",
        )
    assert retired.idempotent is False
    async with session.begin():
        replay = await expose_coding_shadow_run_tasks(
            session,
            run_row_id=first_run_row_id,
            exposures=[_task_exposure()],
        )
    assert replay.idempotent is True
    third_authority = second_authority.model_copy(
        update={
            "coding_run_id": "coding-run-003",
            "task_set_id": "task-set-003",
            "task_set_manifest_sha256": "14" * 32,
            "run_manifest_sha256": "15" * 32,
        }
    )
    with pytest.raises(CodingCatalogInactiveError, match="active catalog"):
        async with session.begin():
            await insert_coding_shadow_run(session, authority=third_authority)

    with pytest.raises(SAIntegrityError, match="append-only"):
        async with session.begin():
            release = await session.scalar(select(CodingCatalogRelease))
            assert release is not None
            release.reason = "attempt to rewrite immutable registration"
            await session.flush()
    with pytest.raises(SAIntegrityError, match="append-only"):
        async with session.begin():
            exposure = await session.scalar(select(CodingCatalogExposure))
            assert exposure is not None
            await session.delete(exposure)
            await session.flush()
