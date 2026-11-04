"""Season-seeded rotation re-scores: one run is quorum and duel evidence.

``docs/title-duels.md`` (phase B2). Under ``retest_mode = season_duel`` with
``season_duel_rotation_seeds`` on, a dueling season rotation re-score runs on
one of the season's shared duel seeds instead of the validator's per-agent
canonical seed:

* **Seed choice.** Quorum validator ``i`` of an agent (rank of its hotkey among
  the agent's scorers at the version) takes duel seed ``i mod K``, so the
  usual three rotation re-scores cover the season's three duel seeds.
* **Lease.** The ticket keeps the seed and the season anchor's block binding
  (``seed_block`` / ``seed_block_hash``); the lease serves it as
  ``seed_scope: "season"`` and the validator re-derives it from that binding
  (protocol 35). Only protocol-35 validators are given one.
* **Mirror.** The accepted canonical score is also appended as the seed's
  confirmation record, so the title duel pairs it and the continual lane owes
  nothing more for that seed.
* **Fallbacks.** No pinned season anchor (after a short grace), an older
  validator, the setting off: the re-score keeps (or returns to) its canonical
  seed. A ticket is never left season-scoped when it should not be.

The fold is unchanged: these are ordinary pinned CRN seeds and ordinary
confirmation records. Levels are unchanged too: a seed's wave value equals the
quorum score it mirrors, so the continual mean over quorum plus waves is the
quorum mean.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from ditto.api_models.continual_retest_settings import CROWN_DUEL_PROTOCOL
from ditto.api_server.confirmation_seed_anchor import (
    SeedBinding,
    anchor_season_key,
    bind_confirmation_seed,
    list_reign_seed_anchors,
)
from ditto.api_server.onchain_seed import derive_validator_seed
from ditto.api_server.season_duel import season_duel_seed_count, season_duel_seeds
from ditto.db.models import ConfirmationSeedAnchor, Score
from ditto.db.queries.audit import (
    EVENT_SCORE_RETEST_QUEUED,
    EVENT_SCORE_RETEST_REQUESTED,
    get_latest_score_retest_event,
)
from ditto.db.queries.confirmation_scores import (
    ConfirmationSeedScore,
    append_confirmation_scores,
)
from ditto.db.queries.score_retests import DUELING_ROTATION_RETEST_BASIS
from ditto_screening_protocol.dueling import is_dueling_bench_version

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_models.continual_retest_settings import ContinualRetestSettings
    from ditto.db.models import DuelingSeason, ValidatorTicket

ROTATION_ANCHOR_GRACE = timedelta(minutes=20)
"""How long a season-seeded rotation re-score waits for the season's anchor.

The anchor pins ``CRN_ANCHOR_BLOCK_DELTA`` (10) blocks after the season's first
continual-lane claim, so this is minutes in practice. Past it, re-scores take
their canonical seed rather than stall the season's quorum."""


def rotation_seeds_active(
    settings: ContinualRetestSettings, *, bench_version: int
) -> bool:
    """Whether rotation re-scores at ``bench_version`` run on duel seeds."""
    return (
        settings.season_duel_rotation_seeds
        and season_duel_seed_count(settings, bench_version=bench_version) is not None
    )


def rotation_seed_index(
    validator_hotkey: str, scorers: Iterable[str], count: int
) -> int:
    """Duel seed index for one quorum validator: its rank among the scorers."""
    ordered = sorted(set(scorers) | {validator_hotkey})
    return ordered.index(validator_hotkey) % max(1, int(count))


async def _season_anchor(
    session: AsyncSession, *, bench_version: int
) -> ConfirmationSeedAnchor | None:
    """The current season's first (family-defining) anchor row, if any."""
    season_key = await anchor_season_key(session, bench_version)
    return await session.scalar(
        select(ConfirmationSeedAnchor)
        .where(
            ConfirmationSeedAnchor.bench_version == bench_version,
            ConfirmationSeedAnchor.season_key == season_key,
        )
        .order_by(
            ConfirmationSeedAnchor.anchor_block,
            ConfirmationSeedAnchor.champion_agent_id,
        )
        .limit(1)
    )


async def defer_for_season_anchor(
    session: AsyncSession,
    *,
    settings: ContinualRetestSettings,
    bench_version: int,
    protocol_version: int | None,
    season: DuelingSeason | None,
    now: datetime,
) -> bool:
    """Whether to hold this validator's rotation re-scores for the anchor.

    Only while season-seeded rotation applies to this validator, the season's
    anchor is not pinned yet, and the season opened less than
    :data:`ROTATION_ANCHOR_GRACE` ago. Never holds anything otherwise.
    """
    if not rotation_seeds_active(settings, bench_version=bench_version):
        return False
    if protocol_version is None or protocol_version < CROWN_DUEL_PROTOCOL:
        return False
    if season is None or season.opened_at is None:
        return False
    opened = season.opened_at
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=now.tzinfo)
    if now - opened >= ROTATION_ANCHOR_GRACE:
        return False
    anchor = await _season_anchor(session, bench_version=bench_version)
    return anchor is None or anchor.anchor_block_hash is None


async def assign_rotation_seed(
    session: AsyncSession,
    ticket: ValidatorTicket,
    *,
    settings: ContinualRetestSettings,
    protocol_version: int | None,
) -> int | None:
    """Put a just-promoted rotation ticket on its duel seed, or back on canonical.

    Returns the duel seed assigned, or ``None`` when the ticket runs its
    canonical seed. A ticket left season-scoped by an earlier season (or by a
    setting since turned off) is reset so the lease re-derives its canonical
    seed from the agent's pinned dataset block.
    """
    version = int(ticket.bench_version)
    count = season_duel_seed_count(settings, bench_version=version)
    duel_seed: int | None = None
    anchor = None
    if (
        rotation_seeds_active(settings, bench_version=version)
        and protocol_version is not None
        and protocol_version >= CROWN_DUEL_PROTOCOL
        and count is not None
    ):
        anchor = await _season_anchor(session, bench_version=version)
        if anchor is not None and anchor.anchor_block_hash is not None:
            seeds = season_duel_seeds(
                anchor.champion_agent_id,
                version=version,
                count=count,
                block_hash=anchor.anchor_block_hash,
                allow_fresh_seeds=True,
            )
            scorers = await session.scalars(
                select(Score.validator_hotkey).where(
                    Score.agent_id == ticket.agent_id, Score.bench_version == version
                )
            )
            duel_seed = seeds[
                rotation_seed_index(ticket.validator_hotkey, scorers.all(), count)
            ]
    if duel_seed is not None and anchor is not None:
        if ticket.seed != duel_seed:
            ticket.dataset_sha256 = None
        ticket.seed = duel_seed
        ticket.seed_block = anchor.anchor_block
        ticket.seed_block_hash = anchor.anchor_block_hash
        return duel_seed
    if await season_seed_binding(session, ticket) is not None:
        ticket.seed = None
        ticket.dataset_sha256 = None
        ticket.seed_block = None
        ticket.seed_block_hash = None
    return None


async def season_seed_binding(
    session: AsyncSession, ticket: ValidatorTicket
) -> SeedBinding | None:
    """The season anchor binding of a season-scoped canonical ticket, or None.

    A ticket is season-scoped exactly when its pinned seed is NOT its canonical
    ``derive_validator_seed`` over its own pinned block, and a pinned season
    anchor of the version derives it over that same block. A canonical ticket
    never satisfies the first condition, so the classification needs no
    stored marker and cannot misread an ordinary quorum ticket.
    """
    if (
        ticket.seed is None
        or not ticket.seed_block_hash
        or not is_dueling_bench_version(int(ticket.bench_version))
    ):
        return None
    if ticket.seed == derive_validator_seed(
        ticket.seed_block_hash, ticket.agent_id, ticket.validator_hotkey
    ):
        return None
    binding = bind_confirmation_seed(
        await list_reign_seed_anchors(session, bench_version=int(ticket.bench_version)),
        seed=int(ticket.seed),
        bench_version=int(ticket.bench_version),
    )
    if binding is None or binding.seed_block_hash != ticket.seed_block_hash:
        return None
    return binding


async def mirror_season_seeded_score(
    session: AsyncSession,
    ticket: ValidatorTicket,
    *,
    validator_hotkey: str,
    seed: int,
    composite: float,
    run_id: str,
    signature: str | None,
    dueling_season_id: UUID | None,
    dueling: dict | None,
    created_at: datetime,
) -> bool:
    """Append an accepted season-seeded quorum score as its confirmation record.

    No-op (``False``) for any ticket that is not season-scoped. Idempotent: the
    append is ``ON CONFLICT DO NOTHING`` on ``(agent, validator, version, seed,
    generation)``.
    """
    if await season_seed_binding(session, ticket) is None:
        return False
    await append_confirmation_scores(
        session,
        rows=[
            ConfirmationSeedScore(
                agent_id=ticket.agent_id,
                validator_hotkey=validator_hotkey,
                seed=int(seed),
                composite=float(composite),
                run_id=run_id,
                signature=signature,
                dueling_season_id=dueling_season_id,
                dueling=dueling,
            )
        ],
        bench_version=int(ticket.bench_version),
        created_at=created_at,
    )
    return True


async def agents_with_pending_rotation(
    session: AsyncSession,
    *,
    agent_ids: Iterable[UUID],
    season_id: UUID,
    bench_version: int,
) -> frozenset[UUID]:
    """Agents whose season rotation re-score is still queued or running.

    While a member's season-seeded rotation is pending, the continual lane owes
    it no duel seed (the rotation will cover them); once every rotation entry
    for the season closed, any seed it did not cover is ordinary duel work.
    """
    pending: set[UUID] = set()
    wanted = str(season_id)
    for agent_id in dict.fromkeys(agent_ids):
        scorers = await session.scalars(
            select(Score.validator_hotkey).where(
                Score.agent_id == agent_id, Score.bench_version == bench_version
            )
        )
        for validator_hotkey in scorers.all():
            latest = await get_latest_score_retest_event(
                session, agent_id=agent_id, validator_hotkey=validator_hotkey
            )
            if (
                latest is not None
                and latest.event
                in (EVENT_SCORE_RETEST_QUEUED, EVENT_SCORE_RETEST_REQUESTED)
                and latest.payload.get("basis") == DUELING_ROTATION_RETEST_BASIS
                and latest.payload.get("dueling_season_id") == wanted
            ):
                pending.add(agent_id)
                break
    return frozenset(pending)
