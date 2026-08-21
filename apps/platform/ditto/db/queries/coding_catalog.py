"""Signed coding-catalog commitments, retirement, and exposure consumption."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ditto.api_models.coding_catalog import (
    CodingCatalogCommitment,
    CodingCatalogTaskExposure,
)
from ditto.api_models.coding_evaluation import CodingShadowRunAuthority
from ditto.db.models import (
    CodingCatalogExposure,
    CodingCatalogRelease,
    CodingCatalogRetirement,
    CodingShadowRun,
    CodingShadowRunIssuance,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class CodingCatalogConflictError(Exception):
    """An immutable catalog, retirement, or exposure identity changed."""


class CodingCatalogInactiveError(Exception):
    """A release is absent, retired, or not ready for a new lease."""


@dataclass(frozen=True)
class CodingCatalogInsertResult:
    row: CodingCatalogRelease | CodingCatalogRetirement
    idempotent: bool


@dataclass(frozen=True)
class CodingCatalogExposureResult:
    rows: list[CodingCatalogExposure]
    idempotent: bool


@dataclass(frozen=True)
class CodingCatalogReleaseBundle:
    release: CodingCatalogRelease
    retirement: CodingCatalogRetirement | None
    exposure_count: int
    exposed_run_count: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def catalog_release_matches_commitment(
    row: CodingCatalogRelease,
    *,
    commitment: CodingCatalogCommitment,
) -> bool:
    return (
        row.corpus_release_id == commitment.corpus_release_id
        and row.coding_contract_version == commitment.coding_contract_version
        and row.weight_eligible is False
        and row.catalog_merkle_root == commitment.catalog_merkle_root
        and row.selection_derivation_id == commitment.selection_derivation_id
        and row.selection_chain_genesis_hash == commitment.selection_chain_genesis_hash
        and row.grader_contract_sha256 == commitment.grader_contract_sha256
        and row.inference_grant_sha256 == commitment.inference_grant_sha256
        and row.task_version_count == commitment.task_version_count
        and row.curator_hotkey == commitment.curator_hotkey
        and int(_aware(row.committed_at).timestamp()) == commitment.committed_at_unix
        and row.commitment_sha256 == commitment.commitment_sha256
        and row.commitment == commitment.model_dump(mode="json", by_alias=True)
    )


async def get_coding_catalog_release(
    session: AsyncSession,
    *,
    corpus_release_id: str,
    for_update: bool = False,
) -> CodingCatalogRelease | None:
    statement = select(CodingCatalogRelease).where(
        CodingCatalogRelease.corpus_release_id == corpus_release_id
    )
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def active_coding_catalog_release(
    session: AsyncSession,
    *,
    corpus_release_id: str,
    for_update: bool = False,
) -> CodingCatalogRelease | None:
    statement = (
        select(CodingCatalogRelease)
        .outerjoin(
            CodingCatalogRetirement,
            CodingCatalogRetirement.release_row_id
            == CodingCatalogRelease.release_row_id,
        )
        .where(
            CodingCatalogRelease.corpus_release_id == corpus_release_id,
            CodingCatalogRetirement.release_row_id.is_(None),
        )
    )
    if for_update:
        statement = statement.with_for_update(of=CodingCatalogRelease)
    return await session.scalar(statement)


async def insert_coding_catalog_release(
    session: AsyncSession,
    *,
    commitment: CodingCatalogCommitment,
    signature: str,
    reason: str,
    actor: str,
) -> CodingCatalogInsertResult:
    values = {
        "release_row_id": uuid4(),
        "corpus_release_id": commitment.corpus_release_id,
        "coding_contract_version": commitment.coding_contract_version,
        "weight_eligible": False,
        "catalog_merkle_root": commitment.catalog_merkle_root,
        "selection_derivation_id": commitment.selection_derivation_id,
        "selection_chain_genesis_hash": commitment.selection_chain_genesis_hash,
        "grader_contract_sha256": commitment.grader_contract_sha256,
        "inference_grant_sha256": commitment.inference_grant_sha256,
        "task_version_count": commitment.task_version_count,
        "curator_hotkey": commitment.curator_hotkey,
        "committed_at": datetime.fromtimestamp(commitment.committed_at_unix, UTC),
        "commitment_sha256": commitment.commitment_sha256,
        "commitment": commitment.model_dump(mode="json", by_alias=True),
        "signature": signature.lower(),
        "reason": reason.strip(),
        "actor": actor.strip(),
    }
    inserted_id = await session.scalar(
        pg_insert(CodingCatalogRelease)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(CodingCatalogRelease.release_row_id)
    )
    if inserted_id is not None:
        row = await session.get(CodingCatalogRelease, inserted_id)
        if row is None:  # pragma: no cover
            raise RuntimeError("inserted coding catalog release was not readable")
        return CodingCatalogInsertResult(row=row, idempotent=False)
    row = await get_coding_catalog_release(
        session,
        corpus_release_id=commitment.corpus_release_id,
    )
    if row is None or not catalog_release_matches_commitment(
        row,
        commitment=commitment,
    ):
        raise CodingCatalogConflictError(
            "coding catalog identity or commitment already names different bytes"
        )
    return CodingCatalogInsertResult(row=row, idempotent=True)


async def retire_coding_catalog_release(
    session: AsyncSession,
    *,
    corpus_release_id: str,
    expected_commitment_sha256: str,
    reason: str,
    actor: str,
) -> CodingCatalogInsertResult:
    release = await get_coding_catalog_release(
        session,
        corpus_release_id=corpus_release_id,
        for_update=True,
    )
    if release is None:
        raise CodingCatalogInactiveError("coding catalog release does not exist")
    if release.commitment_sha256 != expected_commitment_sha256:
        raise CodingCatalogConflictError(
            "coding catalog commitment changed; re-read before retirement"
        )
    existing = await session.get(CodingCatalogRetirement, release.release_row_id)
    if existing is not None:
        return CodingCatalogInsertResult(row=existing, idempotent=True)
    row = CodingCatalogRetirement(
        release_row_id=release.release_row_id,
        expected_commitment_sha256=expected_commitment_sha256,
        reason=reason.strip(),
        actor=actor.strip(),
    )
    session.add(row)
    await session.flush()
    return CodingCatalogInsertResult(row=row, idempotent=False)


def catalog_release_matches_run_authority(
    release: CodingCatalogRelease,
    authority: CodingShadowRunAuthority,
) -> bool:
    return (
        release.corpus_release_id == authority.corpus_release_id
        and release.coding_contract_version == authority.coding_contract_version
        and release.catalog_merkle_root == authority.catalog_merkle_root
        and release.selection_derivation_id == authority.selection_derivation_id
        and release.selection_chain_genesis_hash
        == authority.selection_chain_genesis_hash
        and release.grader_contract_sha256 == authority.grader_contract_sha256
        and release.inference_grant_sha256 == authority.inference_grant_sha256
        and release.task_version_count >= authority.task_count
        and release.weight_eligible is False
    )


async def require_active_catalog_for_run_authority(
    session: AsyncSession,
    *,
    authority: CodingShadowRunAuthority,
    artifact_committed_at: datetime,
) -> CodingCatalogRelease:
    release = await active_coding_catalog_release(
        session,
        corpus_release_id=authority.corpus_release_id,
        for_update=True,
    )
    if release is None or not catalog_release_matches_run_authority(release, authority):
        raise CodingCatalogInactiveError(
            "coding run lacks a matching active catalog commitment"
        )
    artifact_committed_at = _aware(artifact_committed_at)
    if (
        _aware(release.committed_at) > artifact_committed_at
        or _aware(release.created_at) > artifact_committed_at
    ):
        raise CodingCatalogInactiveError(
            "coding catalog commitment does not predate the candidate artifact"
        )
    return release


def _exposure_matches(
    row: CodingCatalogExposure,
    exposure: CodingCatalogTaskExposure,
) -> bool:
    return (
        row.manifest_index == exposure.manifest_index
        and row.task_version_id == exposure.task_version_id
        and row.task_commitment_sha256 == exposure.task_commitment_sha256
        and row.selection_proof_sha256 == exposure.selection_proof_sha256
        and row.catalog_membership_proof_sha256
        == exposure.catalog_membership_proof_sha256
        and row.visible_bundle_sha256 == exposure.visible_bundle_sha256
        and row.base_tree_sha256 == exposure.base_tree_sha256
        and row.memory_bundle_sha256 == exposure.memory_bundle_sha256
        and row.environment_image_digest == exposure.environment_image_digest
        and row.resource_profile_sha256 == exposure.resource_profile_sha256
        and row.grader_bundle_sha256 == exposure.grader_bundle_sha256
        and row.grader_image_digest == exposure.grader_image_digest
        and row.test_manifest_sha256 == exposure.test_manifest_sha256
        and row.grader_plan_sha256 == exposure.grader_plan_sha256
        and row.weight_eligible is False
    )


async def expose_coding_shadow_run_tasks(
    session: AsyncSession,
    *,
    run_row_id: UUID,
    exposures: Sequence[CodingCatalogTaskExposure],
) -> CodingCatalogExposureResult:
    """Atomically consume every selected task version before ticket issuance."""

    run = await session.get(CodingShadowRun, run_row_id, with_for_update=True)
    if run is None:
        raise CodingCatalogInactiveError("coding shadow run does not exist")
    identities = [item.task_version_id for item in exposures]
    indexes = [item.manifest_index for item in exposures]
    if (
        len(exposures) != run.task_count
        or indexes != list(range(run.task_count))
        or len(set(identities)) != len(identities)
    ):
        raise CodingCatalogConflictError(
            "coding exposure set must exactly cover the shared run manifest"
        )
    existing = list(
        await session.scalars(
            select(CodingCatalogExposure)
            .where(CodingCatalogExposure.run_row_id == run_row_id)
            .order_by(CodingCatalogExposure.manifest_index)
        )
    )
    if existing:
        if len(existing) != len(exposures) or any(
            not _exposure_matches(row, exposure)
            for row, exposure in zip(existing, exposures, strict=True)
        ):
            raise CodingCatalogConflictError(
                "coding run already carries different or partial exposure evidence"
            )
        return CodingCatalogExposureResult(rows=existing, idempotent=True)
    release = await active_coding_catalog_release(
        session,
        corpus_release_id=run.corpus_release_id,
        for_update=True,
    )
    if (
        release is None
        or release.coding_contract_version != run.coding_contract_version
        or release.catalog_merkle_root != run.catalog_merkle_root
        or release.selection_derivation_id != run.selection_derivation_id
        or release.selection_chain_genesis_hash != run.selection_chain_genesis_hash
        or release.grader_contract_sha256 != run.grader_contract_sha256
        or release.inference_grant_sha256 != run.inference_grant_sha256
        or release.task_version_count < run.task_count
    ):
        raise CodingCatalogInactiveError(
            "coding shadow run no longer matches an active catalog commitment"
        )
    values = [
        {
            "exposure_id": uuid4(),
            "release_row_id": release.release_row_id,
            "corpus_release_id": release.corpus_release_id,
            "run_row_id": run.run_row_id,
            "run_task_count": run.task_count,
            "manifest_index": exposure.manifest_index,
            "task_version_id": exposure.task_version_id,
            "task_commitment_sha256": exposure.task_commitment_sha256,
            "selection_proof_sha256": exposure.selection_proof_sha256,
            "catalog_membership_proof_sha256": (
                exposure.catalog_membership_proof_sha256
            ),
            "visible_bundle_sha256": exposure.visible_bundle_sha256,
            "base_tree_sha256": exposure.base_tree_sha256,
            "memory_bundle_sha256": exposure.memory_bundle_sha256,
            "environment_image_digest": exposure.environment_image_digest,
            "resource_profile_sha256": exposure.resource_profile_sha256,
            "grader_bundle_sha256": exposure.grader_bundle_sha256,
            "grader_image_digest": exposure.grader_image_digest,
            "test_manifest_sha256": exposure.test_manifest_sha256,
            "grader_plan_sha256": exposure.grader_plan_sha256,
            "weight_eligible": False,
        }
        for exposure in exposures
    ]
    async with session.begin_nested():
        inserted_ids = list(
            (
                await session.scalars(
                    pg_insert(CodingCatalogExposure)
                    .values(values)
                    .on_conflict_do_nothing()
                    .returning(CodingCatalogExposure.exposure_id)
                )
            ).all()
        )
        if len(inserted_ids) != len(values):
            raise CodingCatalogConflictError(
                "selected coding task version was already exposed"
            )
    rows = list(
        await session.scalars(
            select(CodingCatalogExposure)
            .where(CodingCatalogExposure.exposure_id.in_(inserted_ids))
            .order_by(CodingCatalogExposure.manifest_index)
        )
    )
    return CodingCatalogExposureResult(rows=rows, idempotent=False)


async def coding_shadow_run_ready_for_ticket(
    session: AsyncSession,
    *,
    run: CodingShadowRun,
) -> bool:
    issuance = await session.scalar(
        select(CodingShadowRunIssuance).where(
            CodingShadowRunIssuance.run_row_id == run.run_row_id
        )
    )
    if issuance is None:
        return False
    release = await active_coding_catalog_release(
        session,
        corpus_release_id=run.corpus_release_id,
        for_update=True,
    )
    if release is None:
        return False
    exposure_count = int(
        await session.scalar(
            select(func.count())
            .select_from(CodingCatalogExposure)
            .where(
                CodingCatalogExposure.run_row_id == run.run_row_id,
                CodingCatalogExposure.release_row_id == release.release_row_id,
            )
        )
        or 0
    )
    return exposure_count == run.task_count


async def list_coding_catalog_releases(
    session: AsyncSession,
    *,
    limit: int,
) -> tuple[list[CodingCatalogReleaseBundle], int]:
    total = int(
        await session.scalar(select(func.count()).select_from(CodingCatalogRelease))
        or 0
    )
    releases = list(
        await session.scalars(
            select(CodingCatalogRelease)
            .order_by(CodingCatalogRelease.created_at.desc())
            .limit(limit)
        )
    )
    bundles: list[CodingCatalogReleaseBundle] = []
    for release in releases:
        retirement = await session.get(CodingCatalogRetirement, release.release_row_id)
        exposure_count, run_count = (
            await session.execute(
                select(
                    func.count(CodingCatalogExposure.exposure_id),
                    func.count(func.distinct(CodingCatalogExposure.run_row_id)),
                ).where(CodingCatalogExposure.release_row_id == release.release_row_id)
            )
        ).one()
        bundles.append(
            CodingCatalogReleaseBundle(
                release=release,
                retirement=retirement,
                exposure_count=int(exposure_count or 0),
                exposed_run_count=int(run_count or 0),
            )
        )
    return bundles, total
