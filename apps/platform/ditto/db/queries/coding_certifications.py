"""Append-only persistence for shadow coding capability certifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_certification import (
    CodingCapabilityCertificationReceipt,
)
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingCertificationLease,
)


class CodingCertificationConflictError(Exception):
    """The same certification identity was replayed with different bytes."""


@dataclass(frozen=True)
class CodingCertificationInsertResult:
    row: CodingCapabilityCertification
    idempotent: bool


@dataclass(frozen=True)
class AgentCodingCertificationSummary:
    coding_supported: bool
    active_certification_count: int

    @property
    def coding_certified(self) -> bool:
        return self.active_certification_count > 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def get_coding_certification_identity(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    coding_contract_version: int,
    certification_id: str,
) -> CodingCapabilityCertification | None:
    return await session.scalar(
        select(CodingCapabilityCertification).where(
            CodingCapabilityCertification.agent_id == agent_id,
            CodingCapabilityCertification.validator_hotkey == validator_hotkey,
            CodingCapabilityCertification.coding_contract_version
            == coding_contract_version,
            CodingCapabilityCertification.certification_id == certification_id,
        )
    )


def coding_certification_matches(
    row: CodingCapabilityCertification,
    *,
    artifact_sha256: str,
    screened_image_sha256: str,
    bench_version: int,
    lease_id: UUID,
    ticket_deadline: datetime,
    receipt: CodingCapabilityCertificationReceipt,
) -> bool:
    return (
        row.artifact_sha256 == artifact_sha256
        and row.screened_image_sha256 == screened_image_sha256
        and row.bench_version == bench_version
        and row.lease_id == lease_id
        and _aware(row.ticket_deadline) == _aware(ticket_deadline)
        and row.certification_sha256 == receipt.certification_sha256
        and row.receipt == receipt.model_dump(mode="json", by_alias=True)
    )


async def get_coding_certification_by_lease(
    session: AsyncSession,
    *,
    lease_id: UUID,
) -> CodingCapabilityCertification | None:
    return await session.scalar(
        select(CodingCapabilityCertification).where(
            CodingCapabilityCertification.lease_id == lease_id
        )
    )


def coding_certification_lease_accepts_receipt(
    lease: CodingCertificationLease,
    *,
    validator_hotkey: str,
    agent_id: UUID,
    artifact_sha256: str,
    screened_image_sha256: str,
    bench_version: int,
    receipt: CodingCapabilityCertificationReceipt,
) -> bool:
    return (
        lease.status == "claimed"
        and lease.validator_hotkey == validator_hotkey
        and lease.agent_id == agent_id
        and lease.artifact_sha256 == artifact_sha256
        and lease.screened_image_sha256 == screened_image_sha256
        and lease.bench_version == bench_version
        and lease.coding_contract_version == receipt.coding_contract_version
        and lease.canary_manifest_sha256 == receipt.canary_manifest_sha256
        and lease.grader_plan_sha256 == receipt.grader_plan_sha256
        and not lease.weight_eligible
        and not receipt.weight_eligible
    )


async def insert_coding_certification(
    session: AsyncSession,
    *,
    agent_id: UUID,
    artifact_sha256: str,
    screened_image_sha256: str,
    validator_hotkey: str,
    bench_version: int,
    lease_id: UUID,
    ticket_deadline: datetime,
    receipt: CodingCapabilityCertificationReceipt,
    signature: str,
) -> CodingCertificationInsertResult:
    """Insert one immutable receipt or accept its exact transport replay."""

    receipt_json = receipt.model_dump(mode="json", by_alias=True)
    values = {
        "certification_row_id": uuid4(),
        "agent_id": agent_id,
        "artifact_sha256": artifact_sha256,
        "screened_image_sha256": screened_image_sha256,
        "validator_hotkey": validator_hotkey,
        "bench_version": bench_version,
        "lease_id": lease_id,
        "ticket_deadline": ticket_deadline,
        "coding_contract_version": receipt.coding_contract_version,
        "certification_id": receipt.certification_id,
        "status": receipt.status.value,
        "failure_stage": (
            receipt.failure_stage.value if receipt.failure_stage is not None else None
        ),
        "failure_code": receipt.failure_code,
        "certification_sha256": receipt.certification_sha256,
        "canary_manifest_sha256": receipt.canary_manifest_sha256,
        "transcript_object_key": receipt.authoring_transcript_object_key,
        "frozen_submission_object_key": receipt.frozen_submission_object_key,
        "issued_at": datetime.fromtimestamp(receipt.issued_at_unix, UTC),
        "expires_at": datetime.fromtimestamp(receipt.expires_at_unix, UTC),
        "weight_eligible": receipt.weight_eligible,
        "receipt": receipt_json,
        "signature": signature.lower(),
    }
    inserted_id = await session.scalar(
        pg_insert(CodingCapabilityCertification)
        .values(**values)
        .on_conflict_do_nothing(constraint="coding_certifications_identity_key")
        .returning(CodingCapabilityCertification.certification_row_id)
    )
    if inserted_id is not None:
        row = await session.get(CodingCapabilityCertification, inserted_id)
        if row is None:  # pragma: no cover - same-transaction invariant
            raise RuntimeError("inserted coding certification was not readable")
        return CodingCertificationInsertResult(row=row, idempotent=False)

    row = await get_coding_certification_identity(
        session,
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        coding_contract_version=receipt.coding_contract_version,
        certification_id=receipt.certification_id,
    )
    if row is None:  # pragma: no cover - unique conflict must name one row
        raise RuntimeError("coding certification conflict row disappeared")
    if not coding_certification_matches(
        row,
        artifact_sha256=artifact_sha256,
        screened_image_sha256=screened_image_sha256,
        bench_version=bench_version,
        lease_id=lease_id,
        ticket_deadline=ticket_deadline,
        receipt=receipt,
    ):
        raise CodingCertificationConflictError(
            "coding certification identity already names different evidence"
        )
    return CodingCertificationInsertResult(row=row, idempotent=True)


async def list_agent_coding_certifications(
    session: AsyncSession,
    *,
    agent_id: UUID,
    limit: int,
) -> tuple[list[CodingCapabilityCertification], int]:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CodingCapabilityCertification)
            .where(CodingCapabilityCertification.agent_id == agent_id)
        )
        or 0
    )
    rows = list(
        (
            await session.scalars(
                select(CodingCapabilityCertification)
                .where(CodingCapabilityCertification.agent_id == agent_id)
                .order_by(
                    CodingCapabilityCertification.created_at.desc(),
                    CodingCapabilityCertification.certification_row_id.desc(),
                )
                .limit(limit)
            )
        ).all()
    )
    return rows, total


async def summarize_agent_coding_certifications(
    session: AsyncSession,
    *,
    agent: Agent,
    now: datetime,
) -> AgentCodingCertificationSummary:
    """Derive current status from all rows, independent of history pagination."""

    if agent.screened_image_sha256 is None:
        return AgentCodingCertificationSummary(
            coding_supported=False,
            active_certification_count=0,
        )
    supported_count, certified_count = (
        await session.execute(
            select(
                func.count().filter(
                    CodingCapabilityCertification.status != "unsupported"
                ),
                func.count().filter(
                    CodingCapabilityCertification.status == "certified"
                ),
            ).where(
                CodingCapabilityCertification.agent_id == agent.agent_id,
                CodingCapabilityCertification.artifact_sha256 == agent.sha256,
                CodingCapabilityCertification.screened_image_sha256
                == agent.screened_image_sha256,
                CodingCapabilityCertification.expires_at > _aware(now),
            )
        )
    ).one()
    return AgentCodingCertificationSummary(
        coding_supported=int(supported_count or 0) > 0,
        active_certification_count=int(certified_count or 0),
    )


async def active_validator_coding_certification(
    session: AsyncSession,
    *,
    agent: Agent,
    validator_hotkey: str,
    bench_version: int,
    coding_contract_version: int,
    active_through: datetime,
) -> CodingCapabilityCertification | None:
    """Return exact-artifact certification valid through a proposed lease."""

    if agent.screened_image_sha256 is None:
        return None
    return await session.scalar(
        select(CodingCapabilityCertification)
        .where(
            CodingCapabilityCertification.agent_id == agent.agent_id,
            CodingCapabilityCertification.artifact_sha256 == agent.sha256,
            CodingCapabilityCertification.screened_image_sha256
            == agent.screened_image_sha256,
            CodingCapabilityCertification.validator_hotkey == validator_hotkey,
            CodingCapabilityCertification.bench_version == bench_version,
            CodingCapabilityCertification.coding_contract_version
            == coding_contract_version,
            CodingCapabilityCertification.status == "certified",
            CodingCapabilityCertification.expires_at > _aware(active_through),
        )
        .order_by(CodingCapabilityCertification.created_at.desc())
        .limit(1)
    )


def coding_certification_stale_reason(
    row: CodingCapabilityCertification,
    agent: Agent,
    *,
    now: datetime,
) -> Literal[
    "active",
    "expired",
    "not_certified",
    "artifact_changed",
    "screened_image_changed",
]:
    if row.artifact_sha256 != agent.sha256:
        return "artifact_changed"
    if row.screened_image_sha256 != agent.screened_image_sha256:
        return "screened_image_changed"
    if row.status != "certified":
        return "not_certified"
    if _aware(row.expires_at) <= _aware(now):
        return "expired"
    return "active"
