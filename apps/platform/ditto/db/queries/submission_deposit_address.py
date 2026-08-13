"""Effective and audited miner submission deposit address."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import SubmissionDepositAddressRevision


async def latest_submission_deposit_address(
    session: AsyncSession,
) -> SubmissionDepositAddressRevision | None:
    return await session.scalar(
        select(SubmissionDepositAddressRevision)
        .order_by(SubmissionDepositAddressRevision.revision.desc())
        .limit(1)
    )


async def effective_submission_deposit_address(
    session: AsyncSession, *, default_address: str
) -> str:
    latest = await latest_submission_deposit_address(session)
    return latest.payment_address if latest is not None else default_address
