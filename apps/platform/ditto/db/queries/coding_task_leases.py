"""Reconstruct one private, ticket-bound shadow coding task lease core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogIssue,
    CodingCatalogRuntimePolicy,
    CodingPrivateCatalogRecord,
    CodingSelectionAssignment,
    CodingSelectionRunManifest,
    CodingTaskSetManifest,
)
from ditto.coding_selection import rebuild_coding_selection_result
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingCatalogExposure,
    CodingSelectionAssignmentRow,
    CodingShadowRun,
    CodingShadowRunIssuance,
    CodingShadowTicket,
)
from ditto.db.queries.coding_assignments import assignment_from_row
from ditto.db.queries.coding_catalog import (
    catalog_release_matches_commitment,
    get_coding_catalog_release,
)
from ditto.db.queries.coding_certifications import coding_certification_stale_reason


class CodingTaskLeaseIntegrityError(Exception):
    """Persisted lease, catalog, manifest, or exposure authority disagrees."""


class CodingTaskLeaseNotAvailableError(Exception):
    """The ticket is absent, expired, or no longer valid for delivery."""


class CodingTaskMaterialSource(Protocol):
    async def get_task_material(
        self,
        *,
        commitment: CodingCatalogCommitment,
        catalog_index: int,
    ) -> CodingPrivateCatalogRecord:
        """Return one hydrated private task record."""


@dataclass(frozen=True)
class CodingShadowTaskLeaseCore:
    ticket_id: UUID
    validator_hotkey: str
    issued_at: datetime
    deadline: datetime
    run_row_id: UUID
    run_manifest: CodingSelectionRunManifest
    task_set_manifest: CodingTaskSetManifest
    repository_epoch: str
    issue: CodingCatalogIssue
    runtime_policy: CodingCatalogRuntimePolicy
    budgets: CodingCatalogBudgets
    weight_eligible: Literal[False] = False


def _aware(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _run_matches(run: CodingShadowRun, authority: object) -> bool:
    return all(
        getattr(run, column) == getattr(authority, field)
        for column, field in (
            ("agent_id", "agent_id"),
            ("artifact_sha256", "agent_artifact_sha256"),
            ("screened_image_sha256", "screened_image_sha256"),
            ("bench_version", "bench_version"),
            ("coding_contract_version", "coding_contract_version"),
            ("coding_run_id", "coding_run_id"),
            ("corpus_release_id", "corpus_release_id"),
            ("catalog_merkle_root", "catalog_merkle_root"),
            ("selection_derivation_id", "selection_derivation_id"),
            ("selection_chain_genesis_hash", "selection_chain_genesis_hash"),
            ("selection_block_number", "selection_block_number"),
            ("selection_block_hash", "selection_block_hash"),
            ("inference_grant_sha256", "inference_grant_sha256"),
            ("grader_contract_sha256", "grader_contract_sha256"),
            ("task_set_id", "task_set_id"),
            ("task_set_manifest_sha256", "task_set_manifest_sha256"),
            ("run_manifest_sha256", "run_manifest_sha256"),
            ("task_count", "task_count"),
            ("weight_eligible", "weight_eligible"),
        )
    )


def _exposure_matches(
    exposure: CodingCatalogExposure,
    expected: object,
) -> bool:
    return (
        all(
            getattr(exposure, field) == getattr(expected, field)
            for field in (
                "manifest_index",
                "task_version_id",
                "task_commitment_sha256",
                "selection_proof_sha256",
                "catalog_membership_proof_sha256",
                "visible_bundle_sha256",
                "base_tree_sha256",
                "memory_bundle_sha256",
                "environment_image_digest",
                "resource_profile_sha256",
                "grader_bundle_sha256",
                "grader_image_digest",
                "test_manifest_sha256",
                "grader_plan_sha256",
            )
        )
        and exposure.weight_eligible is False
    )


def _issuance_matches(
    issuance: CodingShadowRunIssuance,
    *,
    assignment: CodingSelectionAssignment,
    run: CodingShadowRun,
) -> bool:
    return all(
        (
            issuance.run_row_id == run.run_row_id,
            issuance.assignment_sha256 == assignment.assignment_sha256,
            issuance.agent_id == run.agent_id,
            issuance.artifact_sha256 == run.artifact_sha256,
            issuance.screened_image_sha256 == run.screened_image_sha256,
            issuance.bench_version == run.bench_version,
            issuance.coding_contract_version == run.coding_contract_version,
            issuance.coding_run_id == run.coding_run_id,
            issuance.corpus_release_id == run.corpus_release_id,
            issuance.selection_block_number == run.selection_block_number,
            issuance.selection_block_hash == run.selection_block_hash,
            issuance.task_count == run.task_count,
            issuance.weight_eligible is False,
        )
    )


async def build_coding_shadow_task_lease(
    session: AsyncSession,
    *,
    ticket_id: UUID,
    material_source: CodingTaskMaterialSource,
) -> CodingShadowTaskLeaseCore:
    """Rebuild and verify one selected task without minting transport URLs."""

    ticket = await session.get(CodingShadowTicket, ticket_id)
    if ticket is None:
        raise CodingTaskLeaseNotAvailableError("coding shadow ticket does not exist")
    run = await session.get(CodingShadowRun, ticket.run_row_id)
    issuance = await session.scalar(
        select(CodingShadowRunIssuance).where(
            CodingShadowRunIssuance.run_row_id == ticket.run_row_id
        )
    )
    if run is None or issuance is None:
        raise CodingTaskLeaseIntegrityError(
            "coding ticket lacks finalized run authority"
        )
    assignment_row = await session.get(
        CodingSelectionAssignmentRow,
        issuance.assignment_row_id,
    )
    certification = await session.get(
        CodingCapabilityCertification,
        ticket.certification_row_id,
    )
    agent = await session.get(Agent, run.agent_id)
    database_now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(database_now, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    if (
        assignment_row is None
        or certification is None
        or agent is None
        or _aware(ticket.deadline) <= _aware(database_now)
        or certification.validator_hotkey != ticket.validator_hotkey
        or certification.agent_id != run.agent_id
        or certification.bench_version != run.bench_version
        or certification.coding_contract_version != run.coding_contract_version
        or ticket.task_count != run.task_count
        or _aware(certification.expires_at) <= _aware(ticket.deadline)
        or coding_certification_stale_reason(
            certification,
            agent,
            now=database_now,
        )
        != "active"
    ):
        raise CodingTaskLeaseNotAvailableError(
            "coding ticket or artifact certification is no longer active"
        )
    try:
        assignment = assignment_from_row(assignment_row)
    except Exception as error:
        raise CodingTaskLeaseIntegrityError(
            "stored coding assignment is malformed"
        ) from error
    if not _issuance_matches(issuance, assignment=assignment, run=run):
        raise CodingTaskLeaseIntegrityError(
            "stored coding issuance disagrees with assignment or run authority"
        )
    release = await get_coding_catalog_release(
        session,
        corpus_release_id=run.corpus_release_id,
    )
    if release is None:
        raise CodingTaskLeaseIntegrityError("coding catalog release is missing")
    try:
        commitment = CodingCatalogCommitment.model_validate(release.commitment)
    except ValueError as error:
        raise CodingTaskLeaseIntegrityError(
            "stored coding catalog commitment is malformed"
        ) from error
    if not catalog_release_matches_commitment(release, commitment=commitment):
        raise CodingTaskLeaseIntegrityError("coding catalog commitment drifted")
    material = await material_source.get_task_material(
        commitment=commitment,
        catalog_index=issuance.selection_catalog_index,
    )
    rebuilt = rebuild_coding_selection_result(
        assignment=assignment,
        commitment=commitment,
        selection_block_hash=issuance.selection_block_hash,
        candidate_probe=issuance.selection_candidate_probe,
        task_version=material.task_version,
        membership=material.membership_proof,
    )
    exposures = list(
        await session.scalars(
            select(CodingCatalogExposure)
            .where(CodingCatalogExposure.run_row_id == run.run_row_id)
            .order_by(CodingCatalogExposure.manifest_index)
        )
    )
    if (
        len(exposures) != 1
        or exposures[0].run_row_id != run.run_row_id
        or exposures[0].corpus_release_id != run.corpus_release_id
        or exposures[0].run_task_count != run.task_count
        or issuance.selection_proof_sha256
        != rebuilt.selection_proof.selection_proof_sha256
        or not _run_matches(run, rebuilt.authority)
        or not _exposure_matches(exposures[0], rebuilt.exposure)
    ):
        raise CodingTaskLeaseIntegrityError(
            "reconstructed coding task disagrees with persisted run authority"
        )
    return CodingShadowTaskLeaseCore(
        ticket_id=ticket.ticket_id,
        validator_hotkey=ticket.validator_hotkey,
        issued_at=_aware(ticket.issued_at),
        deadline=_aware(ticket.deadline),
        run_row_id=run.run_row_id,
        run_manifest=rebuilt.run_manifest,
        task_set_manifest=rebuilt.task_set_manifest,
        repository_epoch=material.task_version.payload.repository_epoch,
        issue=material.issue,
        runtime_policy=material.runtime_policy,
        budgets=material.budgets,
    )
