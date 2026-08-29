"""Append-only future-height assignments for shadow coding selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import (
    CodingSelectionAssignment,
    bind_coding_selection_assignment,
)
from ditto.chain.models import BlockInfo
from ditto.coding_selection import normalize_coding_block_hash
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingSelectionAssignmentRow,
)
from ditto.db.queries.coding_catalog import (
    active_coding_catalog_release,
    catalog_release_matches_commitment,
)
from ditto.db.queries.core_qualification import (
    latest_core_qualification_observation,
    latest_core_qualification_policy,
)


class CodingAssignmentConflictError(Exception):
    """An immutable coding-run identity already names another assignment."""


class CodingAssignmentNotQualifiedError(Exception):
    """The current artifact lacks catalog, core, or certification authority."""


class CodingAssignmentFinalizedSource(Protocol):
    async def get_finalized_block(self) -> BlockInfo:
        """Return the current finalized anchor."""

    async def get_finalized_block_hash(self, block_number: int) -> str:
        """Return one hash only if the requested height is finalized."""


@dataclass(frozen=True)
class CodingAssignmentPolicy:
    selection_delay_blocks: int
    task_count: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.selection_delay_blocks <= 10_000:
            raise ValueError("selection delay must be in 1..=10000 blocks")
        if self.task_count != 1:
            raise ValueError("coding contract v1 assignments select exactly one task")


@dataclass(frozen=True)
class CodingAssignmentInsertResult:
    row: CodingSelectionAssignmentRow
    assignment: CodingSelectionAssignment
    idempotent: bool


def _aware(value: datetime) -> datetime:
    return (
        value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    )


def assignment_from_row(row: CodingSelectionAssignmentRow) -> CodingSelectionAssignment:
    assignment = CodingSelectionAssignment.model_validate(row.assignment)
    if not _row_matches_assignment(row, assignment):
        raise CodingAssignmentConflictError(
            "stored coding selection assignment disagrees with its columns"
        )
    return assignment


def _row_matches_assignment(
    row: CodingSelectionAssignmentRow,
    assignment: CodingSelectionAssignment,
) -> bool:
    return (
        row.assignment_sha256 == assignment.assignment_sha256
        and row.agent_id == assignment.agent_id
        and row.artifact_sha256 == assignment.agent_artifact_sha256
        and row.screened_image_sha256 == assignment.screened_image_sha256
        and row.bench_version == assignment.bench_version
        and row.coding_contract_version == assignment.coding_contract_version
        and row.coding_run_id == assignment.coding_run_id
        and row.corpus_release_id == assignment.corpus_release_id
        and row.catalog_commitment_sha256 == assignment.catalog_commitment_sha256
        and row.anchor_block_number == assignment.anchor_block_number
        and row.anchor_block_hash == assignment.anchor_block_hash
        and row.selection_delay_blocks == assignment.selection_delay_blocks
        and row.selection_block_number == assignment.selection_block_number
        and _aware(row.assigned_at) == assignment.assigned_at
        and row.task_count == assignment.task_count
        and _aware(row.created_at) == assignment.assigned_at
        and row.weight_eligible is False
    )


async def get_coding_selection_assignment(
    session: AsyncSession,
    *,
    agent_id: UUID,
    coding_contract_version: int,
    coding_run_id: str,
    for_update: bool = False,
) -> CodingSelectionAssignmentRow | None:
    statement = select(CodingSelectionAssignmentRow).where(
        CodingSelectionAssignmentRow.agent_id == agent_id,
        CodingSelectionAssignmentRow.coding_contract_version == coding_contract_version,
        CodingSelectionAssignmentRow.coding_run_id == coding_run_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def _artifact_assignment(
    session: AsyncSession,
    *,
    agent: Agent,
    for_update: bool = False,
) -> CodingSelectionAssignmentRow | None:
    statement = select(CodingSelectionAssignmentRow).where(
        CodingSelectionAssignmentRow.agent_id == agent.agent_id,
        CodingSelectionAssignmentRow.artifact_sha256 == agent.sha256,
        CodingSelectionAssignmentRow.screened_image_sha256
        == agent.screened_image_sha256,
        CodingSelectionAssignmentRow.coding_contract_version == 1,
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


def _request_matches_existing(
    row: CodingSelectionAssignmentRow,
    *,
    agent: Agent,
    bench_version: int,
    corpus_release_id: str,
    policy: CodingAssignmentPolicy,
) -> bool:
    return (
        row.agent_id == agent.agent_id
        and row.artifact_sha256 == agent.sha256
        and row.screened_image_sha256 == agent.screened_image_sha256
        and row.bench_version == bench_version
        and row.coding_contract_version == 1
        and row.corpus_release_id == corpus_release_id
        and row.selection_delay_blocks == policy.selection_delay_blocks
        and row.task_count == policy.task_count
        and row.weight_eligible is False
    )


async def _active_certification(
    session: AsyncSession,
    *,
    agent: Agent,
    bench_version: int,
    now: datetime,
) -> CodingCapabilityCertification | None:
    return await session.scalar(
        select(CodingCapabilityCertification)
        .where(
            CodingCapabilityCertification.agent_id == agent.agent_id,
            CodingCapabilityCertification.artifact_sha256 == agent.sha256,
            CodingCapabilityCertification.screened_image_sha256
            == agent.screened_image_sha256,
            CodingCapabilityCertification.bench_version == bench_version,
            CodingCapabilityCertification.coding_contract_version == 1,
            CodingCapabilityCertification.status == "certified",
            CodingCapabilityCertification.expires_at > now,
        )
        .order_by(
            CodingCapabilityCertification.expires_at.desc(),
            CodingCapabilityCertification.created_at.desc(),
        )
        .limit(1)
    )


async def create_coding_selection_assignment(
    session: AsyncSession,
    *,
    finalized_source: CodingAssignmentFinalizedSource,
    agent_id: UUID,
    bench_version: int,
    coding_run_id: str,
    corpus_release_id: str,
    policy: CodingAssignmentPolicy,
) -> CodingAssignmentInsertResult:
    """Commit one exact artifact/catalog pair to a future finalized height."""

    existing = await get_coding_selection_assignment(
        session,
        agent_id=agent_id,
        coding_contract_version=1,
        coding_run_id=coding_run_id,
    )
    agent = await session.get(Agent, agent_id)
    if agent is None or agent.screened_image_sha256 is None:
        raise CodingAssignmentNotQualifiedError(
            "coding assignment requires a screened agent artifact"
        )
    if existing is not None:
        if not _request_matches_existing(
            existing,
            agent=agent,
            bench_version=bench_version,
            corpus_release_id=corpus_release_id,
            policy=policy,
        ):
            raise CodingAssignmentConflictError(
                "coding run identity already names another assignment"
            )
        return CodingAssignmentInsertResult(
            row=existing,
            assignment=assignment_from_row(existing),
            idempotent=True,
        )
    if (
        await _artifact_assignment(
            session,
            agent=agent,
        )
        is not None
    ):
        raise CodingAssignmentConflictError(
            "screened artifact already has a coding assignment"
        )

    genesis_hash = normalize_coding_block_hash(
        await finalized_source.get_finalized_block_hash(0)
    )
    anchor = await finalized_source.get_finalized_block()
    if anchor.number < 1:
        raise CodingAssignmentNotQualifiedError(
            "finalized coding assignment anchor is invalid"
        )
    # Height-scoped hash is the authority; get_finalized_block() is two RPCs.
    anchor_hash = normalize_coding_block_hash(
        await finalized_source.get_finalized_block_hash(anchor.number)
    )

    agent = await session.get(Agent, agent_id, with_for_update=True)
    if agent is None or agent.screened_image_sha256 is None:
        raise CodingAssignmentNotQualifiedError(
            "coding assignment requires a screened agent artifact"
        )
    existing = await get_coding_selection_assignment(
        session,
        agent_id=agent_id,
        coding_contract_version=1,
        coding_run_id=coding_run_id,
        for_update=True,
    )
    if existing is not None:
        if not _request_matches_existing(
            existing,
            agent=agent,
            bench_version=bench_version,
            corpus_release_id=corpus_release_id,
            policy=policy,
        ):
            raise CodingAssignmentConflictError(
                "coding run identity already names another assignment"
            )
        return CodingAssignmentInsertResult(
            row=existing,
            assignment=assignment_from_row(existing),
            idempotent=True,
        )
    if (
        await _artifact_assignment(
            session,
            agent=agent,
            for_update=True,
        )
        is not None
    ):
        raise CodingAssignmentConflictError(
            "screened artifact already has a coding assignment"
        )

    release = await active_coding_catalog_release(
        session,
        corpus_release_id=corpus_release_id,
        for_update=True,
    )
    if release is None:
        raise CodingAssignmentNotQualifiedError(
            "coding assignment requires an active catalog release"
        )
    commitment = CodingCatalogCommitment.model_validate(release.commitment)
    if (
        not catalog_release_matches_commitment(release, commitment=commitment)
        or genesis_hash != commitment.selection_chain_genesis_hash
        or commitment.selection_derivation_id != "coding-selection-v1"
        or _aware(release.committed_at) > _aware(agent.created_at)
        or _aware(release.created_at) > _aware(agent.created_at)
    ):
        raise CodingAssignmentNotQualifiedError(
            "coding assignment catalog authority is invalid"
        )
    qualification_policy = await latest_core_qualification_policy(
        session,
        bench_version=bench_version,
        for_update=True,
    )
    observation = (
        await latest_core_qualification_observation(
            session,
            agent_id=agent.agent_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=agent.screened_image_sha256,
            bench_version=bench_version,
            policy_revision=qualification_policy.revision,
        )
        if qualification_policy is not None
        else None
    )
    if (
        observation is None
        or not observation.qualified
        or not observation.complete_wave
    ):
        raise CodingAssignmentNotQualifiedError(
            "coding assignment requires current complete core qualification"
        )
    database_now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(database_now, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    assigned_at = _aware(database_now)
    certification = await _active_certification(
        session,
        agent=agent,
        bench_version=bench_version,
        now=assigned_at,
    )
    if certification is None:
        raise CodingAssignmentNotQualifiedError(
            "coding assignment requires active artifact certification"
        )

    assignment = bind_coding_selection_assignment(
        {
            "schema": "dittobench-coding-selection-assignment-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "bench_version": bench_version,
            "coding_run_id": coding_run_id,
            "agent_id": str(agent.agent_id),
            "agent_artifact_sha256": agent.sha256,
            "screened_image_sha256": agent.screened_image_sha256,
            "corpus_release_id": release.corpus_release_id,
            "catalog_commitment_sha256": release.commitment_sha256,
            "anchor_block_number": anchor.number,
            "anchor_block_hash": anchor_hash,
            "selection_delay_blocks": policy.selection_delay_blocks,
            "selection_block_number": anchor.number + policy.selection_delay_blocks,
            "assigned_at": assigned_at,
            "task_count": policy.task_count,
        }
    )
    values = {
        "assignment_row_id": uuid4(),
        "assignment_sha256": assignment.assignment_sha256,
        "release_row_id": release.release_row_id,
        "agent_id": assignment.agent_id,
        "artifact_sha256": assignment.agent_artifact_sha256,
        "screened_image_sha256": assignment.screened_image_sha256,
        "bench_version": assignment.bench_version,
        "coding_contract_version": assignment.coding_contract_version,
        "coding_run_id": assignment.coding_run_id,
        "corpus_release_id": assignment.corpus_release_id,
        "catalog_commitment_sha256": assignment.catalog_commitment_sha256,
        "anchor_block_number": assignment.anchor_block_number,
        "anchor_block_hash": assignment.anchor_block_hash,
        "selection_delay_blocks": assignment.selection_delay_blocks,
        "selection_block_number": assignment.selection_block_number,
        "assigned_at": assignment.assigned_at,
        "task_count": assignment.task_count,
        "core_qualification_observation_id": observation.observation_id,
        "certification_row_id": certification.certification_row_id,
        "weight_eligible": False,
        "assignment": assignment.model_dump(mode="json", by_alias=True),
        "created_at": assignment.assigned_at,
    }
    inserted_id = await session.scalar(
        pg_insert(CodingSelectionAssignmentRow)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(CodingSelectionAssignmentRow.assignment_row_id)
    )
    if inserted_id is not None:
        row = await session.get(CodingSelectionAssignmentRow, inserted_id)
        if row is None:  # pragma: no cover - same-transaction invariant
            raise RuntimeError("inserted coding selection assignment disappeared")
        return CodingAssignmentInsertResult(
            row=row,
            assignment=assignment,
            idempotent=False,
        )
    row = await get_coding_selection_assignment(
        session,
        agent_id=agent_id,
        coding_contract_version=1,
        coding_run_id=coding_run_id,
    )
    if row is None or not _request_matches_existing(
        row,
        agent=agent,
        bench_version=bench_version,
        corpus_release_id=corpus_release_id,
        policy=policy,
    ):
        raise CodingAssignmentConflictError(
            "coding run identity already names another assignment"
        )
    return CodingAssignmentInsertResult(
        row=row,
        assignment=assignment_from_row(row),
        idempotent=True,
    )


async def list_agent_coding_selection_assignments(
    session: AsyncSession,
    *,
    agent_id: UUID,
    limit: int,
) -> tuple[list[CodingSelectionAssignmentRow], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CodingSelectionAssignmentRow)
            .where(CodingSelectionAssignmentRow.agent_id == agent_id)
        )
        or 0
    )
    rows = list(
        await session.scalars(
            select(CodingSelectionAssignmentRow)
            .where(CodingSelectionAssignmentRow.agent_id == agent_id)
            .order_by(
                CodingSelectionAssignmentRow.created_at.desc(),
                CodingSelectionAssignmentRow.assignment_row_id.desc(),
            )
            .limit(limit)
        )
    )
    return rows, total
