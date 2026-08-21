"""Append-only shadow coding run, lease, and signed-result persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ditto.api_models.coding_evaluation import (
    CodingRunEvidence,
    CodingShadowRunAuthority,
    coding_run_evidence_digest,
)
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingShadowResult,
    CodingShadowRun,
    CodingShadowTicket,
    CoreQualificationObservation,
)
from ditto.db.queries.coding_catalog import (
    coding_shadow_run_ready_for_ticket,
    require_active_catalog_for_run_authority,
)
from ditto.db.queries.coding_certifications import (
    active_validator_coding_certification,
)
from ditto.db.queries.core_qualification import (
    latest_core_qualification_observation,
    latest_core_qualification_policy,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class CodingShadowConflictError(Exception):
    """An immutable coding run, ticket, or result identity changed bytes."""


class CodingShadowNotQualifiedError(Exception):
    """The exact artifact lacks current core or coding qualification."""


@dataclass(frozen=True)
class CodingShadowInsertResult:
    row: CodingShadowRun | CodingShadowTicket | CodingShadowResult
    idempotent: bool


@dataclass(frozen=True)
class CodingShadowRunBundle:
    run: CodingShadowRun
    tickets: list[CodingShadowTicket]
    results: dict[UUID, CodingShadowResult]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _run_matches(row: CodingShadowRun, authority: CodingShadowRunAuthority) -> bool:
    return (
        row.agent_id == authority.agent_id
        and row.artifact_sha256 == authority.agent_artifact_sha256
        and row.screened_image_sha256 == authority.screened_image_sha256
        and row.bench_version == authority.bench_version
        and row.coding_contract_version == authority.coding_contract_version
        and row.coding_run_id == authority.coding_run_id
        and row.corpus_release_id == authority.corpus_release_id
        and row.catalog_merkle_root == authority.catalog_merkle_root
        and row.selection_derivation_id == authority.selection_derivation_id
        and row.selection_chain_genesis_hash == authority.selection_chain_genesis_hash
        and row.selection_block_number == authority.selection_block_number
        and row.selection_block_hash == authority.selection_block_hash
        and row.inference_grant_sha256 == authority.inference_grant_sha256
        and row.grader_contract_sha256 == authority.grader_contract_sha256
        and row.task_set_id == authority.task_set_id
        and row.task_set_manifest_sha256 == authority.task_set_manifest_sha256
        and row.run_manifest_sha256 == authority.run_manifest_sha256
        and row.task_count == authority.task_count
        and row.weight_eligible is False
    )


async def insert_coding_shadow_run(
    session: AsyncSession,
    *,
    authority: CodingShadowRunAuthority,
) -> CodingShadowInsertResult:
    """Persist one shared run only from a current qualified artifact."""

    existing = await session.scalar(
        select(CodingShadowRun).where(
            CodingShadowRun.agent_id == authority.agent_id,
            CodingShadowRun.coding_contract_version
            == authority.coding_contract_version,
            CodingShadowRun.coding_run_id == authority.coding_run_id,
        )
    )
    if existing is not None:
        if not _run_matches(existing, authority):
            raise CodingShadowConflictError(
                "coding run identity already names different authority"
            )
        return CodingShadowInsertResult(row=existing, idempotent=True)
    agent = await session.get(Agent, authority.agent_id, with_for_update=True)
    if (
        agent is None
        or agent.sha256 != authority.agent_artifact_sha256
        or agent.screened_image_sha256 != authority.screened_image_sha256
    ):
        raise CodingShadowNotQualifiedError(
            "coding run authority does not match the current screened artifact"
        )
    await require_active_catalog_for_run_authority(
        session,
        authority=authority,
        artifact_committed_at=agent.created_at,
    )
    policy = await latest_core_qualification_policy(
        session,
        bench_version=authority.bench_version,
    )
    observation = (
        await latest_core_qualification_observation(
            session,
            agent_id=agent.agent_id,
            artifact_sha256=agent.sha256,
            screened_image_sha256=authority.screened_image_sha256,
            bench_version=authority.bench_version,
            policy_revision=policy.revision,
        )
        if policy is not None
        else None
    )
    if (
        observation is None
        or not observation.qualified
        or not observation.complete_wave
    ):
        raise CodingShadowNotQualifiedError(
            "coding run requires current complete core qualification"
        )

    values = {
        "run_row_id": uuid4(),
        "agent_id": authority.agent_id,
        "artifact_sha256": authority.agent_artifact_sha256,
        "screened_image_sha256": authority.screened_image_sha256,
        "bench_version": authority.bench_version,
        "coding_contract_version": authority.coding_contract_version,
        "coding_run_id": authority.coding_run_id,
        "corpus_release_id": authority.corpus_release_id,
        "catalog_merkle_root": authority.catalog_merkle_root,
        "selection_derivation_id": authority.selection_derivation_id,
        "selection_chain_genesis_hash": authority.selection_chain_genesis_hash,
        "selection_block_number": authority.selection_block_number,
        "selection_block_hash": authority.selection_block_hash,
        "inference_grant_sha256": authority.inference_grant_sha256,
        "grader_contract_sha256": authority.grader_contract_sha256,
        "task_set_id": authority.task_set_id,
        "task_set_manifest_sha256": authority.task_set_manifest_sha256,
        "run_manifest_sha256": authority.run_manifest_sha256,
        "task_count": authority.task_count,
        "core_qualification_observation_id": observation.observation_id,
        "weight_eligible": False,
    }
    inserted_id = await session.scalar(
        pg_insert(CodingShadowRun)
        .values(**values)
        .on_conflict_do_nothing(constraint="coding_shadow_runs_identity_key")
        .returning(CodingShadowRun.run_row_id)
    )
    if inserted_id is not None:
        row = await session.get(CodingShadowRun, inserted_id)
        if row is None:  # pragma: no cover
            raise RuntimeError("inserted coding shadow run was not readable")
        return CodingShadowInsertResult(row=row, idempotent=False)
    row = await session.scalar(
        select(CodingShadowRun).where(
            CodingShadowRun.agent_id == authority.agent_id,
            CodingShadowRun.coding_contract_version
            == authority.coding_contract_version,
            CodingShadowRun.coding_run_id == authority.coding_run_id,
        )
    )
    if row is None or not _run_matches(row, authority):
        raise CodingShadowConflictError(
            "coding run identity already names different authority"
        )
    return CodingShadowInsertResult(row=row, idempotent=True)


async def issue_coding_shadow_ticket(
    session: AsyncSession,
    *,
    run_row_id: UUID,
    ticket_id: UUID,
    validator_hotkey: str,
    issued_at: datetime,
    deadline: datetime,
) -> CodingShadowInsertResult:
    """Bind one validator lease to its own current capability receipt."""

    issued_at = _aware(issued_at)
    deadline = _aware(deadline)
    if deadline <= issued_at or deadline > issued_at + timedelta(hours=2):
        raise ValueError("coding shadow ticket lifetime must be in (0, 2h]")
    run = await session.get(CodingShadowRun, run_row_id, with_for_update=True)
    if run is None:
        raise CodingShadowNotQualifiedError("coding shadow run does not exist")
    agent = await session.get(Agent, run.agent_id, with_for_update=True)
    if (
        agent is None
        or agent.sha256 != run.artifact_sha256
        or agent.screened_image_sha256 != run.screened_image_sha256
    ):
        raise CodingShadowNotQualifiedError(
            "coding shadow run no longer matches the screened artifact"
        )
    existing_ticket = await session.get(CodingShadowTicket, ticket_id)
    if existing_ticket is not None:
        existing_certification = await session.get(
            CodingCapabilityCertification,
            existing_ticket.certification_row_id,
        )
        if (
            existing_ticket.run_row_id != run_row_id
            or existing_ticket.task_count != run.task_count
            or existing_ticket.validator_hotkey != validator_hotkey
            or _aware(existing_ticket.issued_at) != issued_at
            or _aware(existing_ticket.deadline) != deadline
            or existing_certification is None
            or existing_certification.status != "certified"
            or existing_certification.agent_id != run.agent_id
            or existing_certification.validator_hotkey != validator_hotkey
            or existing_certification.artifact_sha256 != run.artifact_sha256
            or existing_certification.screened_image_sha256 != run.screened_image_sha256
            or existing_certification.coding_contract_version
            != run.coding_contract_version
            or _aware(existing_certification.expires_at) <= deadline
        ):
            raise CodingShadowConflictError(
                "coding ticket identity already names different authority"
            )
        return CodingShadowInsertResult(row=existing_ticket, idempotent=True)
    if not await coding_shadow_run_ready_for_ticket(session, run=run):
        raise CodingShadowNotQualifiedError(
            "coding shadow run lacks complete active catalog exposure"
        )
    policy = await latest_core_qualification_policy(
        session,
        bench_version=run.bench_version,
    )
    observation = await session.get(
        CoreQualificationObservation,
        run.core_qualification_observation_id,
    )
    if (
        policy is None
        or observation is None
        or observation.policy_revision != policy.revision
        or observation.agent_id != run.agent_id
        or observation.artifact_sha256 != run.artifact_sha256
        or observation.screened_image_sha256 != run.screened_image_sha256
        or observation.bench_version != run.bench_version
        or not observation.qualified
        or not observation.complete_wave
    ):
        raise CodingShadowNotQualifiedError(
            "coding shadow run no longer has current core qualification"
        )
    certification = await active_validator_coding_certification(
        session,
        agent=agent,
        validator_hotkey=validator_hotkey,
        coding_contract_version=run.coding_contract_version,
        active_through=deadline,
    )
    if certification is None:
        raise CodingShadowNotQualifiedError(
            "validator lacks certification valid through the coding lease"
        )
    values = {
        "ticket_id": ticket_id,
        "run_row_id": run_row_id,
        "task_count": run.task_count,
        "validator_hotkey": validator_hotkey,
        "certification_row_id": certification.certification_row_id,
        "issued_at": issued_at,
        "deadline": deadline,
    }
    inserted = await session.scalar(
        pg_insert(CodingShadowTicket)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(CodingShadowTicket.ticket_id)
    )
    if inserted is not None:
        row = await session.get(CodingShadowTicket, inserted)
        if row is None:  # pragma: no cover
            raise RuntimeError("inserted coding shadow ticket was not readable")
        return CodingShadowInsertResult(row=row, idempotent=False)
    row = await session.get(CodingShadowTicket, ticket_id)
    if row is None or any(
        (
            row.run_row_id != run_row_id,
            row.validator_hotkey != validator_hotkey,
            row.certification_row_id != certification.certification_row_id,
            _aware(row.issued_at) != issued_at,
            _aware(row.deadline) != deadline,
        )
    ):
        raise CodingShadowConflictError(
            "coding ticket identity already names different authority"
        )
    return CodingShadowInsertResult(row=row, idempotent=True)


def coding_shadow_result_matches(
    row: CodingShadowResult,
    *,
    evidence: CodingRunEvidence,
    run_evidence_sha256: str,
) -> bool:
    return (
        row.run_evidence_sha256 == run_evidence_sha256
        and row.evidence == evidence.model_dump(mode="json", by_alias=True)
    )


async def insert_coding_shadow_result(
    session: AsyncSession,
    *,
    ticket: CodingShadowTicket,
    evidence: CodingRunEvidence,
    run_evidence_sha256: str,
    signature: str,
) -> CodingShadowInsertResult:
    run = await session.get(CodingShadowRun, ticket.run_row_id)
    if (
        run is None
        or evidence.coding_run_id != run.coding_run_id
        or evidence.validator_ticket_id != str(ticket.ticket_id)
        or evidence.run_manifest_sha256 != run.run_manifest_sha256
        or evidence.task_set_manifest_sha256 != run.task_set_manifest_sha256
        or evidence.coding_contract_version != run.coding_contract_version
        or len(evidence.tasks) != run.task_count
        or coding_run_evidence_digest(evidence) != run_evidence_sha256
    ):
        raise CodingShadowConflictError(
            "coding result evidence does not match immutable run authority"
        )
    values = {
        "result_id": uuid4(),
        "ticket_id": ticket.ticket_id,
        "run_row_id": ticket.run_row_id,
        "run_evidence_sha256": run_evidence_sha256,
        "task_count": len(evidence.tasks),
        "resolved_count": evidence.resolved_count,
        "repair_failure_count": evidence.repair_failure_count,
        "infrastructure_count": evidence.infrastructure_count,
        "invalid_count": evidence.invalid_count,
        "candidate_integrity_count": evidence.candidate_integrity_count,
        "control_plane_integrity_count": evidence.control_plane_integrity_count,
        "scoreable_task_count": evidence.scoreable_task_count,
        "repair_mean_micros": evidence.repair_mean_micros,
        "weight_eligible": False,
        "evidence": evidence.model_dump(mode="json", by_alias=True),
        "signature": signature.lower(),
    }
    inserted_id = await session.scalar(
        pg_insert(CodingShadowResult)
        .values(**values)
        .on_conflict_do_nothing(constraint="coding_shadow_results_ticket_key")
        .returning(CodingShadowResult.result_id)
    )
    if inserted_id is not None:
        row = await session.get(CodingShadowResult, inserted_id)
        if row is None:  # pragma: no cover
            raise RuntimeError("inserted coding shadow result was not readable")
        return CodingShadowInsertResult(row=row, idempotent=False)
    row = await session.scalar(
        select(CodingShadowResult).where(
            CodingShadowResult.ticket_id == ticket.ticket_id
        )
    )
    if row is None or not coding_shadow_result_matches(
        row,
        evidence=evidence,
        run_evidence_sha256=run_evidence_sha256,
    ):
        raise CodingShadowConflictError(
            "coding ticket already names different result evidence"
        )
    return CodingShadowInsertResult(row=row, idempotent=True)


async def list_agent_coding_shadow_runs(
    session: AsyncSession,
    *,
    agent_id: UUID,
    limit: int,
) -> tuple[list[CodingShadowRunBundle], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CodingShadowRun)
            .where(CodingShadowRun.agent_id == agent_id)
        )
        or 0
    )
    runs: Sequence[CodingShadowRun] = list(
        await session.scalars(
            select(CodingShadowRun)
            .where(CodingShadowRun.agent_id == agent_id)
            .order_by(CodingShadowRun.created_at.desc(), CodingShadowRun.run_row_id)
            .limit(limit)
        )
    )
    if not runs:
        return [], total
    run_ids = [row.run_row_id for row in runs]
    tickets = list(
        await session.scalars(
            select(CodingShadowTicket)
            .where(CodingShadowTicket.run_row_id.in_(run_ids))
            .order_by(CodingShadowTicket.validator_hotkey)
        )
    )
    ticket_ids = [ticket.ticket_id for ticket in tickets]
    results = (
        list(
            await session.scalars(
                select(CodingShadowResult).where(
                    CodingShadowResult.ticket_id.in_(ticket_ids)
                )
            )
        )
        if ticket_ids
        else []
    )
    tickets_by_run: dict[UUID, list[CodingShadowTicket]] = {
        run_id: [] for run_id in run_ids
    }
    for ticket in tickets:
        tickets_by_run[ticket.run_row_id].append(ticket)
    results_by_ticket = {result.ticket_id: result for result in results}
    return [
        CodingShadowRunBundle(
            run=run,
            tickets=tickets_by_run[run.run_row_id],
            results={
                ticket.ticket_id: results_by_ticket[ticket.ticket_id]
                for ticket in tickets_by_run[run.run_row_id]
                if ticket.ticket_id in results_by_ticket
            },
        )
        for run in runs
    ], total
