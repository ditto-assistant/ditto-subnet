"""Clean-board policy revisions and the per-agent facts its fraction reads (#2911).

Board attestation
-----------------
There is no separate attestation table. An agent is *board-attested* when its
ATH review row (one per agent, ``ath_reviews_agent_id_key``) is ``resolved`` as
``clear`` by an operator. That is what the court's ``attest`` step
(``attest_review``) and an operator ``resolve clear`` both write. An
``agent_id`` names one immutable artifact, so the row is bound to the exact
artifact.

Revocation needs no extra bookkeeping:

* ``open_review`` reopens the row to ``pending``;
* a reject resolves it as ``reject``.

Either one ends the attestation from the next ledger read.

Automated clears are not board review and do not attest:

* an owner-link auto-clear (``owner-link:<attestation_id>``) only says two
  hotkeys share an operator;
* the retired deferred source review (``platform:*``) cleared on a screener
  pass.

Operator actors come from ``X-Admin-Actor``, and no operator path writes these
prefixes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import not_, or_, select

from ditto.db.models import AthReview, CleanBoardSettingsRevision
from ditto.db.queries.artifact_release import list_first_score_quorums

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

GLOBAL_SCOPE = "*"
AUTOMATED_CLEAR_ACTOR_PREFIXES = ("platform:", "owner-link:")
"""``resolved_by`` prefixes of clears no operator made."""


@dataclass(frozen=True)
class CleanBoardFacts:
    """What the clean-board fraction reads for one agent."""

    attested: bool
    finalized_at: datetime | None
    """When the agent first reached its score quorum (stable: the quorum-th
    score's first-insert time), or ``None`` if it never has."""


async def latest_clean_board_settings_revision(
    session: AsyncSession, *, scope: str = GLOBAL_SCOPE
) -> CleanBoardSettingsRevision | None:
    return await session.scalar(
        select(CleanBoardSettingsRevision)
        .where(CleanBoardSettingsRevision.scope == scope)
        .order_by(CleanBoardSettingsRevision.revision.desc())
        .limit(1)
    )


async def list_clean_board_settings_revisions(
    session: AsyncSession, *, limit: int = 200
) -> Sequence[CleanBoardSettingsRevision]:
    return list(
        await session.scalars(
            select(CleanBoardSettingsRevision)
            .order_by(CleanBoardSettingsRevision.revision.desc())
            .limit(limit)
        )
    )


async def insert_clean_board_settings_revision(
    session: AsyncSession,
    *,
    parent_revision: int,
    scope: str,
    settings: dict,
    checksum: str,
    reason: str,
    actor: str,
) -> CleanBoardSettingsRevision:
    row = CleanBoardSettingsRevision(
        parent_revision=parent_revision,
        scope=scope,
        settings=settings,
        checksum=checksum,
        reason=reason,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row


async def list_board_attested_agent_ids(
    session: AsyncSession, agent_ids: Collection[UUID]
) -> set[UUID]:
    """The agents among ``agent_ids`` with an active operator board clear."""
    if not agent_ids:
        return set()
    automated = or_(
        *(
            AthReview.resolved_by.startswith(prefix)
            for prefix in AUTOMATED_CLEAR_ACTOR_PREFIXES
        )
    )
    rows = await session.scalars(
        select(AthReview.agent_id).where(
            AthReview.agent_id.in_(list(agent_ids)),
            AthReview.status == "resolved",
            AthReview.resolution == "clear",
            not_(automated),
        )
    )
    return set(rows)


async def load_clean_board_facts(
    session: AsyncSession, agent_ids: Collection[UUID], *, quorum: int
) -> dict[UUID, CleanBoardFacts]:
    """Attestation and first-quorum time for every agent in ``agent_ids``."""
    ids = list(dict.fromkeys(agent_ids))
    if not ids:
        return {}
    attested = await list_board_attested_agent_ids(session, ids)
    quorums = await list_first_score_quorums(session, agent_ids=ids, quorum=quorum)
    return {
        agent_id: CleanBoardFacts(
            attested=agent_id in attested,
            finalized_at=(
                quorums[agent_id].finalized_at if agent_id in quorums else None
            ),
        )
        for agent_id in ids
    }
