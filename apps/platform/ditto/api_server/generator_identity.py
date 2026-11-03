"""Public identity for generator submissions and round panel agents.

The generator board, round detail, and submission detail show the same
identity the agent leaderboard does: the miner's avatar, the miner-chosen
name (struck when another family holds an upheld handle claim on it), and a
per-hotkey version number. Everything here is public: no source, no seeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select

from ditto.api_server.miner_avatar import public_avatar_path
from ditto.db.models import Agent, GeneratorSubmission
from ditto.db.queries.miner_avatars import list_miner_avatars
from ditto.db.queries.scores import attested_emission_owner_roots

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_models.name_claim import PublicNameHandle


@dataclass(frozen=True)
class GeneratorIdentity:
    submission_id: UUID
    miner_hotkey: str
    name: str | None
    name_handle: PublicNameHandle | None
    avatar_url: str | None
    version: int
    status: str
    submitted_at: datetime
    qualified_at: datetime | None
    public_default: bool


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: UUID
    miner_hotkey: str
    name: str
    version: int | None
    avatar_url: str | None


async def _claims(session: AsyncSession) -> dict:
    from ditto.api_server.name_claim import expected_netuid
    from ditto.db.queries.name_claims import active_handle_claims

    return await active_handle_claims(session, netuid=expected_netuid())


async def generator_identities(
    session: AsyncSession, submission_ids: Iterable[UUID]
) -> dict[UUID, GeneratorIdentity]:
    """Identity for each submission, in a fixed number of queries.

    ``version`` numbers a hotkey's generators 1, 2, ... by upload time, over
    every generator the hotkey uploaded, so it is stable across reads.
    """
    from ditto.api_server.endpoints.public import _public_named

    ids = list(set(submission_ids))
    if not ids:
        return {}
    hotkeys = select(GeneratorSubmission.miner_hotkey).where(
        GeneratorSubmission.submission_id.in_(ids)
    )
    numbered = (
        select(
            GeneratorSubmission.submission_id,
            GeneratorSubmission.miner_hotkey,
            GeneratorSubmission.owner_key,
            GeneratorSubmission.name,
            GeneratorSubmission.status,
            GeneratorSubmission.submitted_at,
            GeneratorSubmission.qualified_at,
            GeneratorSubmission.public_default,
            func.row_number()
            .over(
                partition_by=GeneratorSubmission.miner_hotkey,
                order_by=(
                    GeneratorSubmission.submitted_at,
                    GeneratorSubmission.submission_id,
                ),
            )
            .label("version"),
        )
        .where(GeneratorSubmission.miner_hotkey.in_(hotkeys))
        .subquery()
    )
    rows = (
        await session.execute(select(numbered).where(numbered.c.submission_id.in_(ids)))
    ).all()
    avatars = await list_miner_avatars(
        session, hotkeys={row.miner_hotkey for row in rows}
    )
    named = [row for row in rows if row.name]
    claims = await _claims(session) if named else {}
    roots: dict[UUID, str] = {}
    if claims:
        resolved = await attested_emission_owner_roots(
            session, [(row.miner_hotkey, row.owner_key) for row in named]
        )
        roots = {
            row.submission_id: root for row, root in zip(named, resolved, strict=True)
        }
    out: dict[UUID, GeneratorIdentity] = {}
    for row in rows:
        name, handle = (
            _public_named(row.name, roots.get(row.submission_id), claims)
            if row.name
            else (None, None)
        )
        out[row.submission_id] = GeneratorIdentity(
            submission_id=row.submission_id,
            miner_hotkey=row.miner_hotkey,
            name=name,
            name_handle=handle,
            avatar_url=(
                public_avatar_path(row.miner_hotkey)
                if row.miner_hotkey in avatars and not row.public_default
                else None
            ),
            version=int(row.version),
            status=row.status,
            submitted_at=row.submitted_at,
            qualified_at=row.qualified_at,
            public_default=bool(row.public_default),
        )
    return out


async def agent_identities(
    session: AsyncSession, agent_ids: Iterable[UUID]
) -> dict[UUID, AgentIdentity]:
    """Public name, version, owner hotkey and avatar per panel agent."""
    from ditto.api_server.endpoints.public import (
        _attested_owner_roots_for_agents,
        _public_named,
    )

    ids = list(set(agent_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Agent.agent_id, Agent.name, Agent.version, Agent.miner_hotkey).where(
                Agent.agent_id.in_(ids)
            )
        )
    ).all()
    avatars = await list_miner_avatars(
        session, hotkeys={row.miner_hotkey for row in rows}
    )
    claims = await _claims(session)
    roots = (
        await _attested_owner_roots_for_agents(session, (row.agent_id for row in rows))
        if claims
        else {}
    )
    return {
        row.agent_id: AgentIdentity(
            agent_id=row.agent_id,
            miner_hotkey=row.miner_hotkey,
            name=_public_named(row.name, roots.get(row.agent_id), claims)[0],
            version=row.version,
            avatar_url=(
                public_avatar_path(row.miner_hotkey)
                if row.miner_hotkey in avatars
                else None
            ),
        )
        for row in rows
    }
