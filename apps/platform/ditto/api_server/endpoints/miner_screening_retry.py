"""Ownership-gated self-serve retry of an interrupted screening (SN-456).

The miner route authorizes exactly what the operator's
``retry_failed_screening_now`` authorizes -- one append-only
``screening_retry_overrides`` grant for the exact latest terminal attempt --
under tighter rules:

- only the owning hotkey, through a miner session holding the ``upload`` scope;
- only ``screening_failed`` agents whose latest attempt ``failed`` or
  ``expired`` (never a rejection, quarantine, or ATH hold);
- never for an agent-fault or policy reason code; an unregistered code is
  treated as an operator retry, exactly as the registry documents;
- only while the operator setting is enabled, and at most
  ``max_retries_per_agent`` miner grants per agent.

Idempotency is the override ledger's own: a second request for the same attempt
returns the existing grant (``idempotent=True``) and spends nothing. The guards
(``expected_attempt_id``, ``expected_sha256``) make a stale client unable to
authorize newer work it was never shown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_screening_retry import (
    MinerScreeningRetryBlocker,
    MinerScreeningRetryRequest,
    MinerScreeningRetryResponse,
    MinerScreeningRetryStatus,
)
from ditto.api_server.endpoints.admin_quarantine import _authorize_screening_retry
from ditto.db.models import (
    Agent,
    AgentStatus,
    MinerScreeningRetrySettingsRevision,
    Score,
    ScreeningAttempt,
    ScreeningRetryOverride,
)
from ditto_screening_protocol.reason_codes import ReasonCodeClass, reason_code_class

logger = logging.getLogger(__name__)

MINER_SELF_SERVE_ACTOR_PREFIX = "miner-self-serve:"
"""``screening_retry_overrides.actor`` prefix for a miner-created grant. The
per-agent budget counts these rows; operator grants never carry it."""

MINER_SELF_SERVE_RETRY_REASON = "Owning miner requested a self-serve screening retry"

_RETRYABLE_ATTEMPT_STATUSES = frozenset({"failed", "expired"})
_OWNED_REASON_CLASSES = frozenset({ReasonCodeClass.AGENT_FAULT, ReasonCodeClass.POLICY})

_BLOCKER_MESSAGES: dict[MinerScreeningRetryBlocker, str] = {
    "not_screening_failed": "submission is not parked after a failed screening",
    "no_attempt": "submission has no screening attempt to retry",
    "attempt_active": "a screening attempt is still running",
    "attempt_not_retryable": (
        "latest screening ended in a review ruling; use the appeal path"
    ),
    "reason_not_retryable": (
        "latest screening failure is owned by the submission or an operator hold"
    ),
    "disabled": "self-serve screening retries are paused by operators",
    "budget_exhausted": (
        "this agent has used its self-serve screening retries; ask an operator"
    ),
}


@dataclass(frozen=True)
class MinerRetryPolicy:
    enabled: bool
    max_retries_per_agent: int


def miner_actor(hotkey: str) -> str:
    return f"{MINER_SELF_SERVE_ACTOR_PREFIX}{hotkey}"


async def current_miner_retry_policy(session: AsyncSession) -> MinerRetryPolicy:
    """Latest revision; a missing table row fails closed (disabled)."""
    row = await session.scalar(
        select(MinerScreeningRetrySettingsRevision)
        .order_by(MinerScreeningRetrySettingsRevision.revision.desc())
        .limit(1)
    )
    if row is None:
        return MinerRetryPolicy(enabled=False, max_retries_per_agent=0)
    return MinerRetryPolicy(
        enabled=bool(row.enabled), max_retries_per_agent=int(row.max_retries_per_agent)
    )


async def _miner_retries_used(session: AsyncSession, agent_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ScreeningRetryOverride)
            .where(
                ScreeningRetryOverride.agent_id == agent_id,
                ScreeningRetryOverride.actor.startswith(MINER_SELF_SERVE_ACTOR_PREFIX),
            )
        )
        or 0
    )


async def _latest_attempt(
    session: AsyncSession, agent_id: UUID, *, for_update: bool = False
) -> ScreeningAttempt | None:
    query = (
        select(ScreeningAttempt)
        .where(ScreeningAttempt.agent_id == agent_id)
        .order_by(
            ScreeningAttempt.started_at.desc(),
            ScreeningAttempt.attempt_id.desc(),
        )
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def _running_attempt_exists(session: AsyncSession, agent_id: UUID) -> bool:
    return (
        await session.scalar(
            select(ScreeningAttempt.attempt_id)
            .where(
                ScreeningAttempt.agent_id == agent_id,
                ScreeningAttempt.status == "running",
            )
            .limit(1)
        )
    ) is not None


@dataclass(frozen=True)
class _Evaluation:
    attempt: ScreeningAttempt | None
    existing: ScreeningRetryOverride | None
    blocker: MinerScreeningRetryBlocker | None
    used: int
    policy: MinerRetryPolicy


async def _evaluate(
    session: AsyncSession,
    agent: Agent,
    *,
    lock_attempt: bool = False,
) -> _Evaluation:
    """The single eligibility decision shared by the read and the write."""
    policy = await current_miner_retry_policy(session)
    used = await _miner_retries_used(session, agent.agent_id)
    attempt = await _latest_attempt(session, agent.agent_id, for_update=lock_attempt)
    existing = (
        await session.scalar(
            select(ScreeningRetryOverride).where(
                ScreeningRetryOverride.attempt_id == attempt.attempt_id
            )
        )
        if attempt is not None
        else None
    )

    def result(blocker: MinerScreeningRetryBlocker | None) -> _Evaluation:
        return _Evaluation(
            attempt=attempt,
            existing=existing,
            blocker=blocker,
            used=used,
            policy=policy,
        )

    if existing is not None:
        # A grant for this exact attempt is already queued. Replaying it is
        # always safe and spends nothing, whatever the policy says now.
        return result(None)
    if agent.status != AgentStatus.SCREENING_FAILED:
        return result("not_screening_failed")
    if attempt is None:
        return result("no_attempt")
    if await _running_attempt_exists(session, agent.agent_id):
        return result("attempt_active")
    if attempt.status not in _RETRYABLE_ATTEMPT_STATUSES:
        return result("attempt_not_retryable")
    if reason_code_class(attempt.reason_code) in _OWNED_REASON_CLASSES:
        return result("reason_not_retryable")
    if not policy.enabled:
        return result("disabled")
    if used >= policy.max_retries_per_agent:
        return result("budget_exhausted")
    return result(None)


def _status(agent: Agent, evaluation: _Evaluation) -> MinerScreeningRetryStatus:
    attempt = evaluation.attempt
    already = evaluation.existing is not None
    return MinerScreeningRetryStatus(
        agent_id=agent.agent_id,
        agent_status=str(agent.status),
        artifact_sha256=agent.sha256,
        latest_attempt_id=attempt.attempt_id if attempt is not None else None,
        latest_attempt_status=attempt.status if attempt is not None else None,
        reason_code=attempt.reason_code if attempt is not None else None,
        public_reason=attempt.public_reason if attempt is not None else None,
        eligible=not already and evaluation.blocker is None,
        blocker=evaluation.blocker,
        already_authorized=already,
        retries_used=evaluation.used,
        retries_allowed=evaluation.policy.max_retries_per_agent,
    )


async def load_owned_retry_status(
    session: AsyncSession, *, hotkey: str, agent_id: UUID
) -> MinerScreeningRetryStatus | None:
    """``None`` for an unknown agent or another hotkey's; callers 404 both."""
    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id, Agent.miner_hotkey == hotkey)
    )
    if agent is None:
        return None
    return _status(agent, await _evaluate(session, agent))


async def authorize_owned_retry(
    session: AsyncSession,
    *,
    hotkey: str,
    agent_id: UUID,
    payload: MinerScreeningRetryRequest,
) -> MinerScreeningRetryResponse:
    """Append (or replay) one exact-attempt grant. Caller owns the transaction."""
    agent = await session.scalar(
        select(Agent)
        .where(Agent.agent_id == agent_id, Agent.miner_hotkey == hotkey)
        .with_for_update()
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="no such agent for this hotkey")
    if agent.sha256 != payload.expected_sha256:
        raise HTTPException(status_code=409, detail="artifact identity changed")
    evaluation = await _evaluate(session, agent, lock_attempt=True)
    attempt = evaluation.attempt
    if attempt is None or attempt.attempt_id != payload.expected_attempt_id:
        raise HTTPException(
            status_code=409,
            detail="screening attempt changed; refresh the retry status and try again",
        )
    if evaluation.existing is not None:
        override = evaluation.existing
        return MinerScreeningRetryResponse(
            override_id=override.override_id,
            agent_id=agent.agent_id,
            attempt_id=attempt.attempt_id,
            agent_status=str(agent.status),
            created_at=override.created_at,
            idempotent=True,
            authorized_by_operator=not override.actor.startswith(
                MINER_SELF_SERVE_ACTOR_PREFIX
            ),
            retries_used=evaluation.used,
            retries_allowed=evaluation.policy.max_retries_per_agent,
        )
    if evaluation.blocker is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"screening retry unavailable ({evaluation.blocker}): "
                f"{_BLOCKER_MESSAGES[evaluation.blocker]}"
            ),
        )
    score_count = int(
        await session.scalar(
            select(func.count()).select_from(Score).where(Score.agent_id == agent_id)
        )
        or 0
    )
    override = await _authorize_screening_retry(
        session,
        agent=agent,
        attempt_id=attempt.attempt_id,
        expected_score_count=score_count,
        reason=MINER_SELF_SERVE_RETRY_REASON,
        actor=miner_actor(hotkey),
        now=datetime.now(UTC),
    )
    logger.info(
        "miner self-serve screening retry agent_id=%s attempt_id=%s "
        "override_id=%s reason_code=%s used=%d allowed=%d",
        agent.agent_id,
        attempt.attempt_id,
        override.override_id,
        attempt.reason_code,
        evaluation.used + 1,
        evaluation.policy.max_retries_per_agent,
    )
    return MinerScreeningRetryResponse(
        override_id=override.override_id,
        agent_id=agent.agent_id,
        attempt_id=attempt.attempt_id,
        agent_status=str(agent.status),
        created_at=override.created_at,
        idempotent=False,
        authorized_by_operator=False,
        retries_used=evaluation.used + 1,
        retries_allowed=evaluation.policy.max_retries_per_agent,
    )
