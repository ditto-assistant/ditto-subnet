"""Lineage reference selection and eligibility for screening attempts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Exists

from ditto.db.models import (
    Agent,
    AgentStatus,
    ArtifactFetchAudit,
    EvaluationPayment,
    ScreeningAttempt,
)
from ditto.db.queries.artifact_fetch_audit import ENDPOINT_PUBLIC_ARTIFACT

REFERENCE_SAME_OWNER_VERSION = "same_owner_version"
REFERENCE_SAME_OWNER = "same_owner"
REFERENCE_RELEASED_BASE = "released_base"
# A reference must not itself have been refused: its review is what the
# packet carries forward.
_REFUSED_STATUSES = (AgentStatus.BANNED, AgentStatus.REJECTED)


def _passed_screening(policy_version: int | None = None) -> Exists:
    clauses: list[ColumnElement[bool]] = [
        ScreeningAttempt.agent_id == Agent.agent_id,
        ScreeningAttempt.artifact_sha256 == Agent.sha256,
        ScreeningAttempt.status == "passed",
        ScreeningAttempt.build_only.is_(False),
    ]
    if policy_version is not None:
        clauses.append(ScreeningAttempt.policy_version == policy_version)
    return exists().where(*clauses)


async def _same_hotkey_version(session: AsyncSession, agent: Agent) -> Agent | None:
    if agent.version is None:
        return None
    return await session.scalar(
        select(Agent)
        .where(
            Agent.miner_hotkey == agent.miner_hotkey,
            Agent.name == agent.name,
            Agent.version.is_not(None),
            Agent.version < agent.version,
        )
        .order_by(Agent.version.desc())
        .limit(1)
    )


async def payment_coldkey(session: AsyncSession, agent_id: UUID) -> str | None:
    return await session.scalar(
        select(EvaluationPayment.miner_coldkey).where(
            EvaluationPayment.agent_id == agent_id
        )
    )


async def select_lineage_parent(
    session: AsyncSession, agent: Agent, *, mode: str = REFERENCE_SAME_OWNER_VERSION
) -> Agent | None:
    """The claim-time lineage reference for ``agent``, if any. DB-only.

    Always the same hotkey's latest earlier version of this agent name when
    one exists (``(miner_hotkey, name, version)`` is unique, so one index
    probe). In ``same_owner`` and ``released_base`` modes, with no such
    version, the latest earlier upload paid by the same coldkey that passed
    screening and was not refused. Released bases are chosen later, at packet
    time, because they need the candidate archive.
    """
    parent = await _same_hotkey_version(session, agent)
    if parent is not None or mode == REFERENCE_SAME_OWNER_VERSION:
        return parent
    coldkey = await payment_coldkey(session, agent.agent_id)
    if coldkey is None:
        return None
    return await session.scalar(
        select(Agent)
        .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(
            EvaluationPayment.miner_coldkey == coldkey,
            Agent.agent_id != agent.agent_id,
            Agent.created_at < agent.created_at,
            Agent.status.not_in(_REFUSED_STATUSES),
            _passed_screening(),
        )
        .order_by(Agent.created_at.desc())
        .limit(1)
    )


async def earliest_public_handoff(
    session: AsyncSession, agent: Agent
) -> datetime | None:
    """First exact-SHA public artifact handoff (the publication proof)."""
    return await session.scalar(
        select(func.min(ArtifactFetchAudit.fetched_at)).where(
            ArtifactFetchAudit.agent_id == agent.agent_id,
            ArtifactFetchAudit.artifact_sha256 == agent.sha256,
            ArtifactFetchAudit.endpoint == ENDPOINT_PUBLIC_ARTIFACT,
            ArtifactFetchAudit.requester_kind == "public",
            ArtifactFetchAudit.requester_id.is_(None),
        )
    )


async def classify_lineage_reference(
    session: AsyncSession,
    *,
    agent: Agent,
    reference: Agent,
    policy_version: int,
) -> str | None:
    """Which kind of reference ``reference`` may be for ``agent``, or None.

    * ``same_owner_version`` -- the same hotkey's lower version of this name;
    * ``same_owner`` -- uploaded earlier and paid by the same coldkey;
    * ``released_base`` -- exact SHA publicly handed off at or before this
      upload, not refused, and passed (non-rebuild) screening under
      ``policy_version``.

    Anything else would put an unrelated artifact and history in the packet.
    """
    if reference.agent_id == agent.agent_id:
        return None
    if (
        reference.version is not None
        and agent.version is not None
        and reference.miner_hotkey == agent.miner_hotkey
        and reference.name == agent.name
        and reference.version < agent.version
    ):
        return REFERENCE_SAME_OWNER_VERSION
    if reference.created_at >= agent.created_at:
        return None
    coldkey = await payment_coldkey(session, agent.agent_id)
    if coldkey is not None and coldkey == await payment_coldkey(
        session, reference.agent_id
    ):
        return REFERENCE_SAME_OWNER
    if reference.status in _REFUSED_STATUSES:
        return None
    published = await earliest_public_handoff(session, reference)
    if published is None or published > agent.created_at:
        return None
    passed = await session.scalar(
        select(Agent.agent_id).where(
            Agent.agent_id == reference.agent_id, _passed_screening(policy_version)
        )
    )
    return REFERENCE_RELEASED_BASE if passed is not None else None


async def resolve_declared_references(
    session: AsyncSession,
    *,
    agent: Agent,
    shas: set[str],
    agent_ids: set[UUID],
    name_versions: set[tuple[str, int]],
    limit: int,
) -> list[Agent]:
    """Agents uploaded before ``agent`` that its declarations name."""
    clauses: list[ColumnElement[bool]] = []
    if shas:
        clauses.append(Agent.sha256.in_(sorted(shas)))
    if agent_ids:
        clauses.append(Agent.agent_id.in_(sorted(agent_ids)))
    clauses.extend(
        (Agent.name == name) & (Agent.version == version)
        for name, version in sorted(name_versions)[:16]
    )
    if not clauses:
        return []
    rows = await session.scalars(
        select(Agent)
        .where(
            or_(*clauses),
            Agent.agent_id != agent.agent_id,
            Agent.created_at < agent.created_at,
        )
        .order_by(Agent.created_at.desc())
        .limit(limit * 4)
    )
    return list(rows)


__all__ = [
    "REFERENCE_RELEASED_BASE",
    "REFERENCE_SAME_OWNER",
    "REFERENCE_SAME_OWNER_VERSION",
    "classify_lineage_reference",
    "earliest_public_handoff",
    "payment_coldkey",
    "resolve_declared_references",
    "select_lineage_parent",
]
