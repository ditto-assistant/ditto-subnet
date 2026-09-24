"""Sanitized telemetry for inference requests refused before reservation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import InferenceAdmissionRejection

RETENTION = timedelta(days=14)
_PRUNE_BATCH = 50


async def record_inference_admission_rejection(
    session: AsyncSession,
    *,
    lane: str,
    http_status: int,
    admission_code: str,
    grant_id: UUID | None,
    validator_hotkey: str | None,
    request_bytes: int,
    byte_limit: int | None,
    platform_revision: str,
    now: datetime | None = None,
) -> InferenceAdmissionRejection:
    """Insert one rejection and drop a small batch of rows past retention."""
    recorded_at = now or datetime.now(UTC)
    row = InferenceAdmissionRejection(
        rejection_id=uuid4(),
        created_at=recorded_at,
        lane=lane,
        http_status=http_status,
        admission_code=admission_code,
        grant_id=grant_id,
        validator_hotkey=validator_hotkey,
        correlation_id=uuid4(),
        request_bytes=request_bytes,
        byte_limit=byte_limit,
        platform_revision=platform_revision[:64],
    )
    session.add(row)
    stale_ids = list(
        await session.scalars(
            select(InferenceAdmissionRejection.rejection_id)
            .where(InferenceAdmissionRejection.created_at < recorded_at - RETENTION)
            .limit(_PRUNE_BATCH)
        )
    )
    if stale_ids:
        await session.execute(
            delete(InferenceAdmissionRejection).where(
                InferenceAdmissionRejection.rejection_id.in_(stale_ids)
            )
        )
    return row


async def admission_rejection_summary(
    session: AsyncSession, *, grant_id: UUID, limit: int = 50
) -> tuple[dict[str, int], list[InferenceAdmissionRejection]]:
    counts = dict(
        (
            await session.execute(
                select(
                    InferenceAdmissionRejection.admission_code,
                    func.count(),
                )
                .where(InferenceAdmissionRejection.grant_id == grant_id)
                .group_by(InferenceAdmissionRejection.admission_code)
            )
        ).all()
    )
    rows = list(
        await session.scalars(
            select(InferenceAdmissionRejection)
            .where(InferenceAdmissionRejection.grant_id == grant_id)
            .order_by(InferenceAdmissionRejection.created_at.desc())
            .limit(limit)
        )
    )
    return {str(code): int(count) for code, count in counts.items()}, rows
