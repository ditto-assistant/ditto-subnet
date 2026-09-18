"""Audited noncompetitive team canary exclusions.

A team canary is an ordinary upload that the team runs through normal
screening, copy detection and scoring, but that must never compete for weights
or emissions. The exclusion is reserved before upload for one exact
``(miner_hotkey, artifact_sha256)`` pair, so it already applies when the first
score arrives, and is then bound once to the resulting ``agent_id`` and its
screened image digest.

Matching is fail-closed and strictly subtractive:

* an agent is excluded when its hotkey matches a reservation AND either its
  artifact digest or its bound ``agent_id`` matches, so a later screened-image
  rebuild of the bound agent cannot make it compete again;
* a different hotkey carrying the same artifact is never excluded here (copy
  detection still judges it), and a hotkey alone never excludes anything;
* nothing reads an exclusion as permission. Status, screening, copy detection
  and every other gate still apply, and agent status is never changed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from ditto.db.models import Agent, NoncompetitiveAgentExclusion, Score

TEAM_CANARY = "team_canary"
TeamCanaryState = Literal["reserved", "bound", "binding_drift"]
_SHA256 = re.compile(r"[0-9a-f]{64}")


class NoncompetitiveExclusionError(ValueError):
    """Safe operator-facing rejection of a reservation or binding."""


class NoncompetitiveExclusionNotFoundError(NoncompetitiveExclusionError):
    """The exclusion or agent does not exist."""


@dataclass(frozen=True)
class TeamCanaryExclusion:
    exclusion_id: UUID
    miner_hotkey: str
    artifact_sha256: str
    reason: str
    created_by: str
    created_at: datetime
    agent_id: UUID | None
    screened_image_sha256: str | None
    bound_by: str | None
    bound_reason: str | None
    bound_at: datetime | None


def competition_excluded(
    *,
    agent_id: ColumnElement[UUID] | InstrumentedAttribute[UUID],
    miner_hotkey: ColumnElement[str] | InstrumentedAttribute[str],
    sha256: ColumnElement[str] | InstrumentedAttribute[str],
) -> ColumnElement[bool]:
    """Correlated SQL predicate: this agent identity is a team canary."""
    exclusion = NoncompetitiveAgentExclusion
    return exists().where(
        exclusion.miner_hotkey == miner_hotkey,
        or_(exclusion.artifact_sha256 == sha256, exclusion.agent_id == agent_id),
    )


def agent_competition_excluded() -> ColumnElement[bool]:
    """:func:`competition_excluded` correlated against :class:`Agent`."""
    return competition_excluded(
        agent_id=Agent.agent_id, miner_hotkey=Agent.miner_hotkey, sha256=Agent.sha256
    )


def _record(row: NoncompetitiveAgentExclusion) -> TeamCanaryExclusion:
    return TeamCanaryExclusion(
        exclusion_id=row.exclusion_id,
        miner_hotkey=row.miner_hotkey,
        artifact_sha256=row.artifact_sha256,
        reason=row.reason,
        created_by=row.created_by,
        created_at=row.created_at,
        agent_id=row.agent_id,
        screened_image_sha256=row.screened_image_sha256,
        bound_by=row.bound_by,
        bound_reason=row.bound_reason,
        bound_at=row.bound_at,
    )


async def list_team_canary_exclusions(
    session: AsyncSession,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[TeamCanaryExclusion, ...]:
    statement = (
        select(NoncompetitiveAgentExclusion)
        .order_by(
            NoncompetitiveAgentExclusion.created_at,
            NoncompetitiveAgentExclusion.exclusion_id,
        )
        .offset(offset)
    )
    if limit is not None:
        statement = statement.limit(limit)
    rows = await session.scalars(statement)
    return tuple(_record(row) for row in rows)


async def count_team_canary_exclusions(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(NoncompetitiveAgentExclusion)
        )
        or 0
    )


def matching_exclusion(
    exclusions: Iterable[TeamCanaryExclusion],
    *,
    agent_id: UUID,
    miner_hotkey: str,
    sha256: str,
) -> TeamCanaryExclusion | None:
    """Python mirror of :func:`competition_excluded` for materialized rows."""
    for exclusion in exclusions:
        if exclusion.miner_hotkey == miner_hotkey and (
            exclusion.artifact_sha256 == sha256 or exclusion.agent_id == agent_id
        ):
            return exclusion
    return None


def team_canary_state(
    exclusion: TeamCanaryExclusion,
    *,
    agent_id: UUID,
    sha256: str,
    screened_image_sha256: str | None,
) -> TeamCanaryState:
    """Visible label for one matched agent; every state is still excluded."""
    if exclusion.agent_id is None:
        return "reserved"
    if (
        exclusion.agent_id == agent_id
        and exclusion.artifact_sha256 == sha256
        and exclusion.screened_image_sha256 == screened_image_sha256
    ):
        return "bound"
    return "binding_drift"


def exclusion_fingerprint(exclusions: Sequence[TeamCanaryExclusion]) -> str:
    """Cache key component that changes whenever an exclusion is added or bound."""
    digest = hashlib.sha256()
    for exclusion in sorted(exclusions, key=lambda item: str(item.exclusion_id)):
        digest.update(
            f"{exclusion.exclusion_id}|{exclusion.miner_hotkey}|"
            f"{exclusion.artifact_sha256}|{exclusion.agent_id}\n".encode()
        )
    return digest.hexdigest()


async def excluded_agent_ids(
    session: AsyncSession, agent_ids: Iterable[UUID]
) -> set[UUID]:
    """Which of these agents are team canaries."""
    ids = set(agent_ids)
    if not ids:
        return set()
    return set(
        await session.scalars(
            select(Agent.agent_id).where(
                Agent.agent_id.in_(ids), agent_competition_excluded()
            )
        )
    )


def _require_identity(miner_hotkey: str, artifact_sha256: str) -> None:
    if (
        not isinstance(miner_hotkey, str)
        or not miner_hotkey
        or miner_hotkey != miner_hotkey.strip()
        or not isinstance(artifact_sha256, str)
        or _SHA256.fullmatch(artifact_sha256) is None
    ):
        raise NoncompetitiveExclusionError("team canary identity is invalid")


async def reserve_team_canary(
    session: AsyncSession,
    *,
    miner_hotkey: str,
    artifact_sha256: str,
    reason: str,
    actor: str,
) -> TeamCanaryExclusion:
    """Reserve an exclusion before the canary can collect its first score.

    A reservation that would retroactively cover an already-scored agent is
    refused: write-once kingship, frozen efficiency epochs and frozen rollout
    cohorts may already include it, so that case needs an explicit review.
    """
    _require_identity(miner_hotkey, artifact_sha256)
    existing = await session.scalar(
        select(NoncompetitiveAgentExclusion.exclusion_id).where(
            NoncompetitiveAgentExclusion.miner_hotkey == miner_hotkey,
            NoncompetitiveAgentExclusion.artifact_sha256 == artifact_sha256,
        )
    )
    if existing is not None:
        raise NoncompetitiveExclusionError("team canary is already reserved")
    scored = await session.scalar(
        select(func.count())
        .select_from(Score)
        .join(Agent, Agent.agent_id == Score.agent_id)
        .where(Agent.miner_hotkey == miner_hotkey, Agent.sha256 == artifact_sha256)
    )
    if scored:
        raise NoncompetitiveExclusionError(
            "team canary artifact already has scores; reserve before upload"
        )
    row = NoncompetitiveAgentExclusion(
        exclusion_id=uuid4(),
        kind=TEAM_CANARY,
        miner_hotkey=miner_hotkey,
        artifact_sha256=artifact_sha256,
        reason=reason,
        created_by=actor,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return _record(row)


async def bind_team_canary(
    session: AsyncSession,
    *,
    exclusion_id: UUID,
    agent_id: UUID,
    miner_hotkey: str,
    artifact_sha256: str,
    screened_image_sha256: str,
    reason: str,
    actor: str,
) -> TeamCanaryExclusion:
    """Bind a reservation once to the exact agent and its screened image."""
    _require_identity(miner_hotkey, artifact_sha256)
    if _SHA256.fullmatch(screened_image_sha256) is None:
        raise NoncompetitiveExclusionError("screened image digest is invalid")
    row = await session.scalar(
        select(NoncompetitiveAgentExclusion)
        .where(NoncompetitiveAgentExclusion.exclusion_id == exclusion_id)
        .with_for_update()
    )
    if row is None:
        raise NoncompetitiveExclusionNotFoundError("team canary was not found")
    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id).with_for_update()
    )
    if agent is None:
        raise NoncompetitiveExclusionNotFoundError("agent was not found")
    if row.agent_id is not None:
        raise NoncompetitiveExclusionError("team canary is already bound")
    if not (
        row.miner_hotkey == miner_hotkey == agent.miner_hotkey
        and row.artifact_sha256 == artifact_sha256 == agent.sha256
        and agent.screened_image_sha256 is not None
        and agent.screened_image_sha256 == screened_image_sha256
    ):
        raise NoncompetitiveExclusionError(
            "team canary binding does not match the exact agent identity"
        )
    other = await session.scalar(
        select(NoncompetitiveAgentExclusion.exclusion_id).where(
            and_(
                NoncompetitiveAgentExclusion.agent_id == agent_id,
                NoncompetitiveAgentExclusion.exclusion_id != exclusion_id,
            )
        )
    )
    if other is not None:
        raise NoncompetitiveExclusionError("agent is already bound to a team canary")
    row.agent_id = agent_id
    row.screened_image_sha256 = screened_image_sha256
    row.bound_by = actor
    row.bound_reason = reason
    row.bound_at = func.now()
    await session.flush()
    await session.refresh(row)
    return _record(row)


@dataclass(frozen=True)
class TeamCanaryAgent:
    agent_id: UUID
    status: str
    sha256: str
    screened_image_sha256: str | None
    state: TeamCanaryState


async def list_team_canary_agents(
    session: AsyncSession, exclusions: Sequence[TeamCanaryExclusion]
) -> dict[UUID, list[TeamCanaryAgent]]:
    """Every agent each exclusion currently removes from competition."""
    result: dict[UUID, list[TeamCanaryAgent]] = {
        exclusion.exclusion_id: [] for exclusion in exclusions
    }
    hotkeys = {exclusion.miner_hotkey for exclusion in exclusions}
    if not hotkeys:
        return result
    agents = await session.execute(
        select(
            Agent.agent_id,
            Agent.miner_hotkey,
            Agent.status,
            Agent.sha256,
            Agent.screened_image_sha256,
        )
        .where(Agent.miner_hotkey.in_(hotkeys), agent_competition_excluded())
        .order_by(Agent.created_at, Agent.agent_id)
    )
    for agent in agents:
        exclusion = matching_exclusion(
            exclusions,
            agent_id=agent.agent_id,
            miner_hotkey=agent.miner_hotkey,
            sha256=agent.sha256,
        )
        if exclusion is None:  # pragma: no cover - SQL and Python agree
            continue
        result[exclusion.exclusion_id].append(
            TeamCanaryAgent(
                agent_id=agent.agent_id,
                status=str(agent.status.value),
                sha256=agent.sha256,
                screened_image_sha256=agent.screened_image_sha256,
                state=team_canary_state(
                    exclusion,
                    agent_id=agent.agent_id,
                    sha256=agent.sha256,
                    screened_image_sha256=agent.screened_image_sha256,
                ),
            )
        )
    return result
