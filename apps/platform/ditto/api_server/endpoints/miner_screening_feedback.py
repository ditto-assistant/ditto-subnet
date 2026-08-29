"""Ownership-gated builder for miner-private screening diagnostics."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_screening_feedback import (
    MinerScreeningFailure,
    MinerScreeningFeedbackResponse,
)
from ditto.db.models import Agent, ScreeningAttempt


async def load_owned_screening_feedback(
    session: AsyncSession,
    *,
    hotkey: str,
    agent_id: UUID,
) -> MinerScreeningFeedbackResponse | None:
    agent = await session.scalar(
        select(Agent).where(
            Agent.agent_id == agent_id,
            Agent.miner_hotkey == hotkey,
        )
    )
    if agent is None:
        return None
    attempts = (
        await session.scalars(
            select(ScreeningAttempt)
            .where(ScreeningAttempt.agent_id == agent_id)
            .order_by(ScreeningAttempt.started_at.desc())
        )
    ).all()
    return MinerScreeningFeedbackResponse(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        agent_status=str(agent.status),
        attempts=[
            MinerScreeningFailure(
                attempt_id=item.attempt_id,
                status=item.status,
                policy_version=item.policy_version,
                started_at=item.started_at,
                finished_at=item.finished_at,
                reason_code=item.reason_code,
                public_reason=item.public_reason,
                provider=item.failure_provider,
                lane=item.failure_lane,
                detail=item.private_failure_detail,
                log_tail=item.private_failure_log_tail,
                captured_at=item.failure_captured_at,
            )
            for item in attempts
        ],
    )
