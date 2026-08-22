"""Real-Postgres tests for finalized shadow coding run issuance."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogIssue,
    CodingCatalogManifestTask,
    CodingCatalogMembershipProof,
    CodingCatalogRuntimePolicy,
    CodingCatalogTaskPayload,
    CodingCatalogTaskVersion,
    coding_catalog_budgets_digest,
    coding_catalog_issue_digest,
    coding_catalog_runtime_policy_digest,
    coding_catalog_task_commitment_digest,
)
from ditto.api_models.core_qualification import CoreQualificationPolicy
from ditto.chain.models import BlockInfo
from ditto.coding_selection import coding_catalog_leaf_hash
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingCatalogExposure,
    CodingShadowRun,
    CodingShadowRunIssuance,
)
from ditto.db.queries.coding_assignments import (
    CodingAssignmentPolicy,
    create_coding_selection_assignment,
)
from ditto.db.queries.coding_catalog import (
    CodingCatalogConflictError,
    coding_shadow_run_ready_for_ticket,
    insert_coding_catalog_release,
)
from ditto.db.queries.coding_issuance import (
    CodingIssuanceConflictError,
    CodingIssuanceIntegrityError,
    CodingIssuanceNotQualifiedError,
    CodingIssuancePolicy,
    CodingIssuanceUnavailableError,
    issue_finalized_shadow_coding_run,
)
from ditto.db.queries.core_qualification import (
    insert_core_qualification_policy,
    observe_core_qualification,
)
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, upsert_score

_BENCH = 12
_CORPUS = "private-coding-issuer-v1"
_CURATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


class _AssignmentBlocks:
    async def get_finalized_block_hash(self, block_number: int) -> str:
        assert block_number == 0
        return "0x" + "22" * 32

    async def get_finalized_block(self) -> BlockInfo:
        return BlockInfo(number=1_000, hash="0x" + "33" * 32)


class _IssuerBlocks:
    def __init__(self, *, timestamp: int, fail: bool = False) -> None:
        self.timestamp = timestamp
        self.fail = fail
        self.hash_calls: list[int] = []
        self.timestamp_calls: list[str] = []

    async def get_finalized_block_hash(self, block_number: int) -> str:
        self.hash_calls.append(block_number)
        if self.fail:
            raise TimeoutError("finalized block unavailable")
        return "0x" + ("22" if block_number == 0 else "77") * 32

    async def get_block_timestamp(self, block_hash: str) -> int:
        self.timestamp_calls.append(block_hash)
        if self.fail:
            raise TimeoutError("block timestamp unavailable")
        return self.timestamp


class _CatalogSource:
    def __init__(
        self,
        *,
        task: CodingCatalogTaskVersion,
        proof: CodingCatalogMembershipProof,
    ) -> None:
        self.task = task
        self.proof = proof
        self.calls: list[tuple[str, int]] = []

    async def get_task_version(
        self,
        *,
        commitment: CodingCatalogCommitment,
        catalog_index: int,
    ) -> tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]:
        self.calls.append((commitment.corpus_release_id, catalog_index))
        assert commitment.corpus_release_id == _CORPUS
        assert catalog_index == 0
        return self.task, self.proof


class _NeverCatalog:
    async def get_task_version(
        self,
        *,
        commitment: CodingCatalogCommitment,
        catalog_index: int,
    ) -> tuple[CodingCatalogTaskVersion, CodingCatalogMembershipProof]:
        del commitment, catalog_index
        raise AssertionError("idempotent replay contacted the private catalog")


@dataclass(frozen=True)
class _Fixture:
    agent_id: UUID
    assignment_row_id: UUID
    assignment_created_at: datetime
    task: CodingCatalogTaskVersion
    proof: CodingCatalogMembershipProof


def _digest(values: dict[str, object], *, label: str) -> str:
    return coding_canonical_sha256(
        values,
        maximum_bytes=4 << 20,
        label=label,
    )


def _single_task_catalog() -> tuple[
    CodingCatalogCommitment,
    CodingCatalogTaskVersion,
    CodingCatalogMembershipProof,
]:
    issue = CodingCatalogIssue(
        title="Repair the synthetic issuer task",
        description="Return the current repository behavior after one bounded edit.",
        constraints=["Do not add a dependency."],
    )
    runtime_policy = CodingCatalogRuntimePolicy(
        editable_paths=["src/app.py"],
        test_command_ids=["visible-tests"],
        build_command_ids=["build-check"],
    )
    budgets = CodingCatalogBudgets(
        model_input_tokens=200_000,
        model_output_tokens=30_000,
        workspace_tool_calls=150,
        wall_time_seconds=1_800,
    )
    manifest_task = CodingCatalogManifestTask(
        case_id="private-case-issuer-001",
        variant_id="v1",
        profile_capability_id="private-profile-issuer-001",
        visible_bundle_sha256="11" * 32,
        base_tree_sha256="12" * 32,
        memory_bundle_sha256="13" * 32,
        environment_image_digest="sha256:" + "14" * 32,
        environment_platform="linux/amd64",
        resource_profile_sha256="15" * 32,
        grader_bundle_sha256="16" * 32,
        grader_image_digest="sha256:" + "17" * 32,
        grader_platform="linux/amd64",
        test_manifest_sha256="18" * 32,
        grader_plan_sha256="19" * 32,
    )
    payload = CodingCatalogTaskPayload(
        schema="dittobench-coding-catalog-task-v1",
        coding_contract_version=1,
        weight_eligible=False,
        corpus_release_id=_CORPUS,
        catalog_index=0,
        task_version_id="private-task-issuer-001",
        repository_epoch="repository-epoch-issuer-001",
        issue_sha256=coding_catalog_issue_digest(issue),
        runtime_policy_sha256=coding_catalog_runtime_policy_digest(runtime_policy),
        budgets_sha256=coding_catalog_budgets_digest(budgets),
        task=manifest_task,
    )
    task = CodingCatalogTaskVersion(
        payload=payload,
        task_commitment_sha256=coding_catalog_task_commitment_digest(payload),
    )
    root = coding_catalog_leaf_hash(
        catalog_index=0,
        task_commitment_sha256=task.task_commitment_sha256,
    )
    proof_values: dict[str, object] = {
        "schema": "dittobench-coding-catalog-membership-proof-v1",
        "coding_contract_version": 1,
        "corpus_release_id": _CORPUS,
        "catalog_merkle_root": root,
        "task_version_count": 1,
        "catalog_index": 0,
        "task_commitment_sha256": task.task_commitment_sha256,
        "sibling_sha256": [],
    }
    proof_values["catalog_membership_proof_sha256"] = _digest(
        proof_values, label="coding catalog membership proof"
    )
    proof = CodingCatalogMembershipProof.model_validate(proof_values)
    commitment_values: dict[str, object] = {
        "schema": "dittobench-coding-catalog-commitment-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "corpus_release_id": _CORPUS,
        "catalog_merkle_root": root,
        "selection_derivation_id": "coding-selection-v1",
        "selection_chain_genesis_hash": "0x" + "22" * 32,
        "grader_contract_sha256": "33" * 32,
        "inference_grant_sha256": "44" * 32,
        "task_version_count": 1,
        "curator_hotkey": _CURATOR,
        "committed_at_unix": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
    }
    commitment_values["commitment_sha256"] = _digest(
        commitment_values,
        label="catalog commitment",
    )
    return CodingCatalogCommitment.model_validate(commitment_values), task, proof


async def _seed_fixture(session: AsyncSession) -> _Fixture:
    commitment, task, proof = _single_task_catalog()
    async with session.begin():
        await insert_coding_catalog_release(
            session,
            commitment=commitment,
            signature="88" * 64,
            reason="register issuer test catalog",
            actor="test-admin",
        )
    now = datetime.now(UTC)
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="5IssuerMiner1111111111111111111111111111111111111",
        name="coding-issuer-agent",
        sha256="ab" * 32,
        status=AgentStatus.SCORED,
        screening_policy_version=9,
        screened_image_sha256="cd" * 32,
        screened_image_size_bytes=1234,
        screened_image_id="sha256:" + "ef" * 32,
        screened_image_ref="ditto-screen/coding-issuer:latest",
        screened_image_upload_id=uuid4(),
        screened_image_verified_at=now,
        created_at=now + timedelta(minutes=5),
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
            reason="calibrate issuer test admission",
            actor="test-admin",
        )
        for index in range(3):
            await upsert_score(
                session,
                agent_id=agent.agent_id,
                validator_hotkey=f"issuer-core-validator-{index}",
                bench_version=_BENCH,
                run_id=f"issuer-core-run-{index}",
                seed=index,
                composite=0.9,
                tool_mean=0.9,
                memory_mean=0.9,
                median_ms=100,
                n=MIN_ELIGIBLE_CASES,
                generated_at=now,
                signature=(f"{index + 1:02x}" * 64),
            )
        observed = await observe_core_qualification(
            session,
            agent_id=agent.agent_id,
            bench_version=_BENCH,
            now=now,
        )
        assert observed is not None and observed.row.qualified
        session.add(
            CodingCapabilityCertification(
                certification_row_id=uuid4(),
                agent_id=agent.agent_id,
                artifact_sha256=agent.sha256,
                screened_image_sha256=agent.screened_image_sha256,
                validator_hotkey="5" + "V" * 47,
                bench_version=_BENCH,
                ticket_deadline=now + timedelta(hours=1),
                coding_contract_version=1,
                certification_id="issuer-cert-001",
                status="certified",
                failure_stage=None,
                failure_code=None,
                certification_sha256="55" * 32,
                canary_manifest_sha256="56" * 32,
                transcript_object_key="sha256/" + "57" * 32,
                frozen_submission_object_key="sha256/" + "58" * 32,
                issued_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(hours=2),
                weight_eligible=False,
                receipt={},
                signature="59" * 64,
                created_at=now,
            )
        )
    async with session.begin():
        assignment = await create_coding_selection_assignment(
            session,
            finalized_source=_AssignmentBlocks(),
            agent_id=agent.agent_id,
            bench_version=_BENCH,
            coding_run_id="coding-run-issuer-001",
            corpus_release_id=_CORPUS,
            policy=CodingAssignmentPolicy(selection_delay_blocks=20),
        )
    return _Fixture(
        agent_id=agent.agent_id,
        assignment_row_id=assignment.row.assignment_row_id,
        assignment_created_at=assignment.row.created_at,
        task=task,
        proof=proof,
    )


def _block_timestamp(fixture: _Fixture) -> int:
    return math.floor(fixture.assignment_created_at.timestamp()) + 1


def test_issuer_policy_bounds_external_lock_time() -> None:
    assert CodingIssuancePolicy().external_timeout_seconds == 30.0
    with pytest.raises(ValueError, match="timeout"):
        CodingIssuancePolicy(external_timeout_seconds=0.5)
    with pytest.raises(ValueError, match="timeout"):
        CodingIssuancePolicy(external_timeout_seconds=301.0)


async def test_issuer_atomically_persists_selection_run_exposure_and_replay(
    session: AsyncSession,
) -> None:
    fixture = await _seed_fixture(session)
    blocks = _IssuerBlocks(timestamp=_block_timestamp(fixture))
    catalog = _CatalogSource(task=fixture.task, proof=fixture.proof)
    async with session.begin():
        created = await issue_finalized_shadow_coding_run(
            session,
            assignment_row_id=fixture.assignment_row_id,
            finalized_source=blocks,
            catalog_source=catalog,
        )
    assert created.idempotent is False
    assert created.selection is not None
    assert created.issuance.run_row_id == created.run.run_row_id
    assert created.issuance.assignment_row_id == fixture.assignment_row_id
    assert created.issuance.selection_candidate_probe == 0
    assert created.issuance.selection_catalog_index == 0
    assert created.issuance.selection_proof_sha256 == (
        created.exposures[0].selection_proof_sha256
    )
    assert created.exposures[0].task_version_id == fixture.task.payload.task_version_id
    assert blocks.hash_calls == [0, 1_020]
    assert blocks.timestamp_calls == ["0x" + "77" * 32]
    assert catalog.calls == [(_CORPUS, 0)]
    async with session.begin():
        assert await coding_shadow_run_ready_for_ticket(session, run=created.run)
        replay = await issue_finalized_shadow_coding_run(
            session,
            assignment_row_id=fixture.assignment_row_id,
            finalized_source=_IssuerBlocks(timestamp=0, fail=True),
            catalog_source=_NeverCatalog(),
        )
    assert replay.idempotent is True
    assert replay.selection is None
    assert replay.run.run_row_id == created.run.run_row_id

    with pytest.raises(SAIntegrityError, match="append-only"):
        async with session.begin():
            issuance = await session.get(
                CodingShadowRunIssuance,
                fixture.assignment_row_id,
                with_for_update=True,
            )
            assert issuance is not None
            issuance.selection_block_hash = "0x" + "99" * 32
            await session.flush()


async def test_issuer_rejects_unavailable_or_non_precommitted_block(
    session: AsyncSession,
) -> None:
    fixture = await _seed_fixture(session)
    with pytest.raises(CodingIssuanceUnavailableError):
        async with session.begin():
            await issue_finalized_shadow_coding_run(
                session,
                assignment_row_id=fixture.assignment_row_id,
                finalized_source=_IssuerBlocks(timestamp=0, fail=True),
                catalog_source=_CatalogSource(task=fixture.task, proof=fixture.proof),
            )
    with pytest.raises(CodingIssuanceIntegrityError, match="predate"):
        async with session.begin():
            await issue_finalized_shadow_coding_run(
                session,
                assignment_row_id=fixture.assignment_row_id,
                finalized_source=_IssuerBlocks(
                    timestamp=math.floor(fixture.assignment_created_at.timestamp())
                ),
                catalog_source=_CatalogSource(task=fixture.task, proof=fixture.proof),
            )
    async with session.begin():
        assert (
            await session.scalar(select(func.count()).select_from(CodingShadowRun)) == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(CodingCatalogExposure)
            )
            == 0
        )


async def test_issuer_revalidates_the_current_screened_artifact(
    session: AsyncSession,
) -> None:
    fixture = await _seed_fixture(session)
    async with session.begin():
        agent = await session.get(Agent, fixture.agent_id, with_for_update=True)
        assert agent is not None
        agent.sha256 = "ff" * 32
    blocks = _IssuerBlocks(timestamp=_block_timestamp(fixture))
    with pytest.raises(CodingIssuanceNotQualifiedError, match="screened artifact"):
        async with session.begin():
            await issue_finalized_shadow_coding_run(
                session,
                assignment_row_id=fixture.assignment_row_id,
                finalized_source=blocks,
                catalog_source=_CatalogSource(task=fixture.task, proof=fixture.proof),
            )
    assert blocks.hash_calls == []


async def test_issuer_rolls_back_run_when_exposure_fails(
    session: AsyncSession,
    monkeypatch,
) -> None:
    fixture = await _seed_fixture(session)

    async def fail_exposure(*args, **kwargs):
        del args, kwargs
        raise CodingCatalogConflictError("injected exposure conflict")

    monkeypatch.setattr(
        "ditto.db.queries.coding_issuance.expose_coding_shadow_run_tasks",
        fail_exposure,
    )
    async with session.begin():
        with pytest.raises(CodingIssuanceConflictError, match="conflicts"):
            await issue_finalized_shadow_coding_run(
                session,
                assignment_row_id=fixture.assignment_row_id,
                finalized_source=_IssuerBlocks(timestamp=_block_timestamp(fixture)),
                catalog_source=_CatalogSource(task=fixture.task, proof=fixture.proof),
            )
    async with session.begin():
        assert (
            await session.scalar(select(func.count()).select_from(CodingShadowRun)) == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(CodingShadowRunIssuance)
            )
            == 0
        )


async def test_concurrent_issuers_converge_on_one_run(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    fixture = await _seed_fixture(session)
    blocks = _IssuerBlocks(timestamp=_block_timestamp(fixture))
    catalog = _CatalogSource(task=fixture.task, proof=fixture.proof)

    async def issue():
        async with session_maker() as attempt_session, attempt_session.begin():
            return await issue_finalized_shadow_coding_run(
                attempt_session,
                assignment_row_id=fixture.assignment_row_id,
                finalized_source=blocks,
                catalog_source=catalog,
            )

    results = await asyncio.gather(issue(), issue())
    assert sorted(result.idempotent for result in results) == [False, True]
    assert results[0].run.run_row_id == results[1].run.run_row_id
    assert blocks.hash_calls == [0, 1_020]
    assert catalog.calls == [(_CORPUS, 0)]
    async with session_maker() as probe:
        assert (
            await probe.scalar(
                select(func.count()).select_from(CodingShadowRunIssuance)
            )
            == 1
        )
