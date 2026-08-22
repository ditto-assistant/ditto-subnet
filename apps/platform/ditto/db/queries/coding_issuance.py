"""Finality-gated, atomic shadow coding run issuance."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.coding_selection import (
    CodingPrivateCatalogSource,
    CodingSelectionChainIntegrityError,
    CodingSelectionError,
    CodingSelectionResult,
    normalize_coding_block_hash,
    select_shadow_coding_run,
)
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingCatalogExposure,
    CodingSelectionAssignmentRow,
    CodingShadowRun,
    CodingShadowRunIssuance,
)
from ditto.db.queries.coding_assignments import assignment_from_row
from ditto.db.queries.coding_catalog import (
    CodingCatalogConflictError,
    CodingCatalogInactiveError,
    active_coding_catalog_release,
    catalog_release_matches_commitment,
    expose_coding_shadow_run_tasks,
)
from ditto.db.queries.coding_certifications import coding_certification_stale_reason
from ditto.db.queries.coding_evaluations import (
    CodingShadowConflictError,
    CodingShadowNotQualifiedError,
    insert_coding_shadow_run,
)
from ditto.db.queries.core_qualification import (
    latest_core_qualification_observation,
    latest_core_qualification_policy,
)


class CodingIssuanceUnavailableError(Exception):
    """Finalized-chain or private-catalog transport is temporarily unavailable."""


class CodingIssuanceIntegrityError(Exception):
    """Assignment, block time, selection, or stored issuance integrity failed."""


class CodingIssuanceNotQualifiedError(Exception):
    """The assigned artifact no longer has current shadow authority."""


class CodingIssuanceConflictError(Exception):
    """The assignment or selected run already names different bytes."""


class CodingIssuanceFinalizedSource(Protocol):
    async def get_finalized_block_hash(self, block_number: int) -> str:
        """Return the canonical hash only when one exact height is finalized."""

    async def get_block_timestamp(self, block_hash: str) -> int:
        """Return the canonical block timestamp in Unix seconds."""


@dataclass(frozen=True)
class CodingIssuancePolicy:
    external_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 1.0 <= self.external_timeout_seconds <= 300.0:
            raise ValueError("coding issuance external timeout must be in [1, 300]")


_DEFAULT_ISSUANCE_POLICY = CodingIssuancePolicy()


@dataclass(frozen=True)
class CodingIssuanceResult:
    issuance: CodingShadowRunIssuance
    run: CodingShadowRun
    exposures: list[CodingCatalogExposure]
    selection: CodingSelectionResult | None
    idempotent: bool


@dataclass(frozen=True)
class _PinnedFinalizedBlocks:
    genesis_hash: str
    selection_block_number: int
    selection_block_hash: str

    async def get_finalized_block_hash(self, block_number: int) -> str:
        if block_number == 0:
            return self.genesis_hash
        if block_number == self.selection_block_number:
            return self.selection_block_hash
        raise CodingSelectionChainIntegrityError(
            "selector requested a block outside the pinned issuance authority"
        )


def _aware(value: datetime) -> datetime:
    return (
        value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    )


def _issuance_matches(
    row: CodingShadowRunIssuance,
    *,
    assignment: CodingSelectionAssignmentRow,
    run: CodingShadowRun,
) -> bool:
    return (
        row.assignment_row_id == assignment.assignment_row_id
        and row.run_row_id == run.run_row_id
        and row.assignment_sha256 == assignment.assignment_sha256
        and row.agent_id == assignment.agent_id == run.agent_id
        and row.artifact_sha256 == assignment.artifact_sha256 == run.artifact_sha256
        and row.screened_image_sha256
        == assignment.screened_image_sha256
        == run.screened_image_sha256
        and row.bench_version == assignment.bench_version == run.bench_version
        and row.coding_contract_version
        == assignment.coding_contract_version
        == run.coding_contract_version
        and row.coding_run_id == assignment.coding_run_id == run.coding_run_id
        and row.corpus_release_id
        == assignment.corpus_release_id
        == run.corpus_release_id
        and row.selection_block_number
        == assignment.selection_block_number
        == run.selection_block_number
        and row.selection_block_hash == run.selection_block_hash
        and 0 <= row.selection_candidate_probe <= 999_999
        and 0 <= row.selection_catalog_index <= 999_999
        and row.task_count == assignment.task_count == run.task_count == 1
        and _aware(assignment.created_at) < _aware(row.selection_block_timestamp)
        and row.weight_eligible is False
        and run.weight_eligible is False
    )


async def _existing_issuance(
    session: AsyncSession,
    *,
    assignment: CodingSelectionAssignmentRow,
    issuance: CodingShadowRunIssuance,
) -> CodingIssuanceResult:
    run = await session.get(CodingShadowRun, issuance.run_row_id)
    exposures = list(
        await session.scalars(
            select(CodingCatalogExposure)
            .where(CodingCatalogExposure.run_row_id == issuance.run_row_id)
            .order_by(CodingCatalogExposure.manifest_index)
        )
    )
    if (
        run is None
        or not _issuance_matches(issuance, assignment=assignment, run=run)
        or len(exposures) != run.task_count
        or [item.manifest_index for item in exposures] != list(range(run.task_count))
        or exposures[0].selection_proof_sha256 != issuance.selection_proof_sha256
    ):
        raise CodingIssuanceIntegrityError(
            "stored coding issuance is incomplete or disagrees with its authority"
        )
    return CodingIssuanceResult(
        issuance=issuance,
        run=run,
        exposures=exposures,
        selection=None,
        idempotent=True,
    )


async def issue_finalized_shadow_coding_run(
    session: AsyncSession,
    *,
    assignment_row_id: UUID,
    finalized_source: CodingIssuanceFinalizedSource,
    catalog_source: CodingPrivateCatalogSource,
    policy: CodingIssuancePolicy = _DEFAULT_ISSUANCE_POLICY,
) -> CodingIssuanceResult:
    """Select, persist, and consume one assignment in the caller's transaction."""

    assignment_row = await session.get(
        CodingSelectionAssignmentRow,
        assignment_row_id,
        with_for_update=True,
    )
    if assignment_row is None:
        raise CodingIssuanceNotQualifiedError("coding assignment does not exist")
    try:
        assignment = assignment_from_row(assignment_row)
    except Exception as error:
        raise CodingIssuanceIntegrityError(
            "stored coding assignment failed canonical validation"
        ) from error
    existing = await session.get(CodingShadowRunIssuance, assignment_row_id)
    if existing is not None:
        return await _existing_issuance(
            session,
            assignment=assignment_row,
            issuance=existing,
        )

    agent = await session.get(Agent, assignment.agent_id, with_for_update=True)
    if (
        agent is None
        or agent.sha256 != assignment.agent_artifact_sha256
        or agent.screened_image_sha256 != assignment.screened_image_sha256
    ):
        raise CodingIssuanceNotQualifiedError(
            "coding assignment no longer matches the screened artifact"
        )
    release = await active_coding_catalog_release(
        session,
        corpus_release_id=assignment.corpus_release_id,
        for_update=True,
    )
    if release is None or release.release_row_id != assignment_row.release_row_id:
        raise CodingIssuanceNotQualifiedError(
            "coding assignment catalog is absent or retired"
        )
    try:
        commitment = CodingCatalogCommitment.model_validate(release.commitment)
    except ValueError as error:
        raise CodingIssuanceIntegrityError(
            "stored coding catalog commitment is malformed"
        ) from error
    if (
        not catalog_release_matches_commitment(release, commitment=commitment)
        or release.commitment_sha256 != assignment.catalog_commitment_sha256
    ):
        raise CodingIssuanceIntegrityError(
            "coding assignment no longer matches its catalog commitment"
        )
    qualification_policy = await latest_core_qualification_policy(
        session,
        bench_version=assignment.bench_version,
        for_update=True,
    )
    observation = (
        await latest_core_qualification_observation(
            session,
            agent_id=assignment.agent_id,
            artifact_sha256=assignment.agent_artifact_sha256,
            screened_image_sha256=assignment.screened_image_sha256,
            bench_version=assignment.bench_version,
            policy_revision=qualification_policy.revision,
        )
        if qualification_policy is not None
        else None
    )
    if (
        observation is None
        or not observation.complete_wave
        or not observation.qualified
    ):
        raise CodingIssuanceNotQualifiedError(
            "coding issuance requires current complete core qualification"
        )
    admission_now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(admission_now, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    certification = await session.get(
        CodingCapabilityCertification,
        assignment_row.certification_row_id,
    )
    if (
        certification is None
        or certification.agent_id != assignment.agent_id
        or certification.artifact_sha256 != assignment.agent_artifact_sha256
        or certification.screened_image_sha256 != assignment.screened_image_sha256
        or certification.bench_version != assignment.bench_version
        or certification.coding_contract_version != assignment.coding_contract_version
        or coding_certification_stale_reason(
            certification,
            agent,
            now=admission_now,
        )
        != "active"
    ):
        raise CodingIssuanceNotQualifiedError(
            "coding issuance requires its exact active capability certification"
        )

    try:
        async with asyncio.timeout(policy.external_timeout_seconds):
            genesis_hash = normalize_coding_block_hash(
                await finalized_source.get_finalized_block_hash(0)
            )
            selection_block_hash = normalize_coding_block_hash(
                await finalized_source.get_finalized_block_hash(
                    assignment.selection_block_number
                )
            )
            block_timestamp = await finalized_source.get_block_timestamp(
                selection_block_hash
            )
    except TimeoutError as error:
        raise CodingIssuanceUnavailableError(
            "finalized selection block lookup timed out"
        ) from error
    except CodingSelectionError:
        raise
    except Exception as error:
        raise CodingIssuanceUnavailableError(
            "finalized selection block identity is unavailable"
        ) from error
    try:
        selection_block_timestamp = datetime.fromtimestamp(block_timestamp, UTC)
    except (OverflowError, OSError, TypeError, ValueError) as error:
        raise CodingIssuanceIntegrityError(
            "selection block timestamp is invalid"
        ) from error
    if (
        block_timestamp <= 0
        or _aware(assignment_row.created_at) >= selection_block_timestamp
    ):
        raise CodingIssuanceIntegrityError(
            "coding assignment does not provably predate finalized block revelation"
        )

    consumed_task_ids = frozenset(
        await session.scalars(
            select(CodingCatalogExposure.task_version_id).where(
                CodingCatalogExposure.release_row_id == release.release_row_id
            )
        )
    )
    try:
        async with asyncio.timeout(policy.external_timeout_seconds):
            selection = await select_shadow_coding_run(
                assignment=assignment,
                commitment=commitment,
                finalized_block_source=_PinnedFinalizedBlocks(
                    genesis_hash=genesis_hash,
                    selection_block_number=assignment.selection_block_number,
                    selection_block_hash=selection_block_hash,
                ),
                catalog_source=catalog_source,
                consumed_task_version_ids=consumed_task_ids,
            )
    except TimeoutError as error:
        raise CodingIssuanceUnavailableError(
            "private catalog selection timed out"
        ) from error
    except CodingSelectionError:
        raise
    except Exception as error:  # pragma: no cover - selector owns typed failures
        raise CodingIssuanceIntegrityError("coding selection failed") from error

    persistence_now = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(persistence_now, datetime):  # pragma: no cover - DB invariant
        raise RuntimeError("database clock did not return a timestamp")
    if selection_block_timestamp > _aware(persistence_now) + timedelta(seconds=5):
        raise CodingIssuanceIntegrityError(
            "finalized selection block timestamp is ahead of the database clock"
        )
    if (
        coding_certification_stale_reason(
            certification,
            agent,
            now=persistence_now,
        )
        != "active"
    ):
        raise CodingIssuanceNotQualifiedError(
            "coding capability certification expired during selection"
        )

    try:
        async with session.begin_nested():
            run_result = await insert_coding_shadow_run(
                session,
                authority=selection.authority,
            )
            if not isinstance(run_result.row, CodingShadowRun):  # pragma: no cover
                raise RuntimeError("coding run insertion returned another row type")
            exposure_result = await expose_coding_shadow_run_tasks(
                session,
                run_row_id=run_result.row.run_row_id,
                exposures=[selection.exposure],
            )
            issuance = CodingShadowRunIssuance(
                assignment_row_id=assignment_row.assignment_row_id,
                run_row_id=run_result.row.run_row_id,
                assignment_sha256=assignment.assignment_sha256,
                agent_id=assignment.agent_id,
                artifact_sha256=assignment.agent_artifact_sha256,
                screened_image_sha256=assignment.screened_image_sha256,
                bench_version=assignment.bench_version,
                coding_contract_version=assignment.coding_contract_version,
                coding_run_id=assignment.coding_run_id,
                corpus_release_id=assignment.corpus_release_id,
                selection_block_number=assignment.selection_block_number,
                selection_block_hash=selection_block_hash,
                selection_candidate_probe=selection.selection_proof.candidate_probe,
                selection_catalog_index=selection.selection_proof.catalog_index,
                selection_proof_sha256=(
                    selection.selection_proof.selection_proof_sha256
                ),
                selection_block_timestamp=selection_block_timestamp,
                task_count=assignment.task_count,
                weight_eligible=False,
            )
            session.add(issuance)
            await session.flush()
    except (CodingCatalogConflictError, CodingShadowConflictError) as error:
        raise CodingIssuanceConflictError(
            "selected coding run or exposure conflicts with stored authority"
        ) from error
    except (CodingCatalogInactiveError, CodingShadowNotQualifiedError) as error:
        raise CodingIssuanceNotQualifiedError(
            "selected coding run could not be persisted under current authority"
        ) from error
    return CodingIssuanceResult(
        issuance=issuance,
        run=run_result.row,
        exposures=exposure_result.rows,
        selection=selection,
        idempotent=False,
    )
