"""Payment-derived owner linkage, for operator review surfaces only.

Every coldkey in this module comes from ``evaluation_payments.miner_coldkey``:
the coldkey that signed the payment extrinsic for one evaluation, snapshotted
at payment time. That is a record of **who paid**, which is not the same
question as on-chain metagraph ownership at any later block, and it is emphatically
not proof of common control:

* Miners routinely fund submissions from several coldkeys. Two hotkeys with
  different payment coldkeys are frequently the same operator.
* A hotkey can be transferred or re-registered, so an old payment coldkey may
  no longer own it on chain.
* Legacy and test agents predate mandatory paid-upload provenance and carry no
  payment row at all, so their coldkey is simply unknown.

A reviewer should read a shared coldkey as *one corroborating signal* worth
following, and distinct coldkeys as *no signal either way*. For a live on-chain
ownership answer, read the metagraph (``ditto.chain.client.get_registered_coldkey``
or ``btcli``) instead of this module.

Nothing here writes, and nothing here participates in screening, scoring,
weights, or emissions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Integer, cast, exists, func, or_, select

from ditto.db.models import Agent, EvaluationPayment

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

#: Maximum expansion rounds a caller may request. One round is
#: hotkey -> its payment coldkeys -> every other hotkey those coldkeys paid for.
#: Deeper walks chain through shared coldkeys and over-link fast, which is the
#: opposite of what a careful reviewer wants, so the ceiling stays low.
MAX_DEPTH = 3

#: Hard ceiling on distinct identities returned regardless of depth. A runaway
#: walk truncates and says so rather than dragging half the subnet into one
#: "footprint" and implying they are one operator.
MAX_IDENTITIES = 250

IdentifierKind = Literal["miner_hotkey", "miner_coldkey", "both", "unknown"]


@dataclass(frozen=True)
class OwnerAgent:
    """One submission belonging to a linked hotkey."""

    agent_id: UUID
    agent_name: str
    agent_version: int | None
    agent_status: str
    artifact_sha256: str
    submitted_at: datetime
    miner_coldkey: str | None


@dataclass(frozen=True)
class OwnerHotkey:
    """One hotkey in the footprint, with the evidence that linked it."""

    miner_hotkey: str
    miner_coldkeys: tuple[str, ...]
    link_hop: int
    submission_count: int
    paid_submission_count: int
    latest_submitted_at: datetime | None
    agents: tuple[OwnerAgent, ...]
    agents_truncated: bool


@dataclass(frozen=True)
class OwnerFootprint:
    """Every hotkey reachable from one key through payment records."""

    identifier: str
    identifier_kind: IdentifierKind
    depth: int
    miner_coldkeys: tuple[str, ...]
    hotkeys: tuple[OwnerHotkey, ...]
    hotkey_count: int
    submission_count: int
    expansion_complete: bool


async def _classify(session: AsyncSession, identifier: str) -> IdentifierKind:
    """Say whether the key was ever recorded as a hotkey, a coldkey, or both.

    Hotkeys and coldkeys are both SS58 addresses, so shape cannot tell them
    apart; only the records can. An agent with no payment row still counts as a
    hotkey sighting, otherwise a legacy miner would look like an unknown key.
    """
    seen_as_hotkey = or_(
        exists(select(Agent.agent_id).where(Agent.miner_hotkey == identifier)),
        exists(
            select(EvaluationPayment.block_hash).where(
                EvaluationPayment.miner_hotkey == identifier
            )
        ),
    )
    seen_as_coldkey = exists(
        select(EvaluationPayment.block_hash).where(
            EvaluationPayment.miner_coldkey == identifier
        )
    )
    is_hotkey, is_coldkey = (
        await session.execute(select(seen_as_hotkey, seen_as_coldkey))
    ).one()
    if is_hotkey and is_coldkey:
        return "both"
    if is_hotkey:
        return "miner_hotkey"
    if is_coldkey:
        return "miner_coldkey"
    return "unknown"


async def _coldkeys_for_hotkeys(session: AsyncSession, hotkeys: set[str]) -> set[str]:
    if not hotkeys:
        return set()
    rows = await session.scalars(
        select(EvaluationPayment.miner_coldkey)
        .where(EvaluationPayment.miner_hotkey.in_(hotkeys))
        .distinct()
    )
    return {coldkey for coldkey in rows.all() if coldkey is not None}


async def _hotkeys_for_coldkeys(session: AsyncSession, coldkeys: set[str]) -> set[str]:
    if not coldkeys:
        return set()
    rows = await session.scalars(
        select(EvaluationPayment.miner_hotkey)
        .where(EvaluationPayment.miner_coldkey.in_(coldkeys))
        .distinct()
    )
    return {hotkey for hotkey in rows.all() if hotkey is not None}


async def _walk(
    session: AsyncSession, identifier: str, kind: IdentifierKind, depth: int
) -> tuple[dict[str, int], set[str], bool]:
    """Breadth-first walk over payment records from one key.

    Returns the hotkeys with the hop each was found at, the coldkeys traversed,
    and whether the walk ran to completion instead of hitting a ceiling. A hop
    counts payment-record edges: hop 0 is the key that was asked about, hop 1 a
    hotkey named on the same payment as a hop-0 coldkey, hop 2 a hotkey that
    shares a coldkey with a hop-1 hotkey, and so on.
    """
    hotkey_hops: dict[str, int] = {}
    coldkeys: set[str] = set()
    hotkey_frontier: set[str] = set()
    coldkey_frontier: set[str] = set()

    if kind in ("miner_hotkey", "both"):
        hotkey_hops[identifier] = 0
        hotkey_frontier = {identifier}
    if kind in ("miner_coldkey", "both"):
        coldkeys.add(identifier)
        coldkey_frontier = {identifier}

    complete = True
    for round_index in range(depth):
        # hotkeys -> the coldkeys that paid for them
        found_coldkeys = await _coldkeys_for_hotkeys(session, hotkey_frontier)
        new_coldkeys = found_coldkeys - coldkeys
        coldkeys |= new_coldkeys
        coldkey_frontier |= new_coldkeys
        if not coldkey_frontier:
            # No unvisited coldkey remains, so no deeper walk could find more.
            break
        # coldkeys -> every other hotkey they paid for
        found_hotkeys = await _hotkeys_for_coldkeys(session, coldkey_frontier)
        coldkey_frontier = set()
        hop = round_index * 2 + 1
        next_frontier: set[str] = set()
        for hotkey in sorted(found_hotkeys):
            if hotkey in hotkey_hops:
                continue
            if len(hotkey_hops) + len(coldkeys) >= MAX_IDENTITIES:
                complete = False
                break
            hotkey_hops[hotkey] = hop
            next_frontier.add(hotkey)
        hotkey_frontier = next_frontier
        if not complete or not hotkey_frontier:
            break
    else:
        # The walk ran out of rounds rather than out of identities, so a deeper
        # one could still find more. Say so instead of implying a closed set.
        if hotkey_frontier and await _has_unvisited_coldkey(
            session, hotkey_frontier, coldkeys
        ):
            complete = False

    return hotkey_hops, coldkeys, complete


async def _has_unvisited_coldkey(
    session: AsyncSession, hotkeys: set[str], seen: set[str]
) -> bool:
    """True when the outermost hotkeys still lead to an unvisited coldkey."""
    found = await _coldkeys_for_hotkeys(session, hotkeys)
    return bool(found - seen)


async def _hotkey_stats(
    session: AsyncSession, hotkeys: set[str]
) -> dict[str, tuple[int, int, datetime | None]]:
    """Submission count, paid-submission count, and newest submission per hotkey."""
    if not hotkeys:
        return {}
    rows = await session.execute(
        select(
            Agent.miner_hotkey,
            func.count(),
            func.coalesce(
                func.sum(cast(EvaluationPayment.miner_coldkey.is_not(None), Integer)),
                0,
            ),
            func.max(Agent.created_at),
        )
        .outerjoin(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(Agent.miner_hotkey.in_(hotkeys))
        .group_by(Agent.miner_hotkey)
    )
    return {
        hotkey: (int(total), int(paid), latest)
        for hotkey, total, paid, latest in rows.tuples().all()
    }


async def _coldkeys_by_hotkey(
    session: AsyncSession, hotkeys: set[str]
) -> dict[str, list[str]]:
    if not hotkeys:
        return {}
    rows = await session.execute(
        select(EvaluationPayment.miner_hotkey, EvaluationPayment.miner_coldkey)
        .where(EvaluationPayment.miner_hotkey.in_(hotkeys))
        .distinct()
    )
    by_hotkey: dict[str, list[str]] = {}
    for hotkey, coldkey in rows.tuples().all():
        if coldkey is None:
            continue
        by_hotkey.setdefault(hotkey, []).append(coldkey)
    return {hotkey: sorted(keys) for hotkey, keys in by_hotkey.items()}


async def _recent_agents(
    session: AsyncSession, hotkeys: set[str], per_hotkey: int
) -> dict[str, list[OwnerAgent]]:
    """Newest submissions per hotkey, bounded so one prolific miner cannot
    make this read materialize their whole history."""
    if not hotkeys or per_hotkey <= 0:
        return {}
    ranked = (
        select(
            Agent.agent_id.label("agent_id"),
            Agent.miner_hotkey.label("miner_hotkey"),
            Agent.name.label("agent_name"),
            Agent.version.label("agent_version"),
            Agent.status.label("agent_status"),
            Agent.sha256.label("artifact_sha256"),
            Agent.created_at.label("submitted_at"),
            EvaluationPayment.miner_coldkey.label("miner_coldkey"),
            func.row_number()
            .over(
                partition_by=Agent.miner_hotkey,
                order_by=(Agent.created_at.desc(), Agent.agent_id.desc()),
            )
            .label("rank"),
        )
        .outerjoin(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(Agent.miner_hotkey.in_(hotkeys))
        .subquery()
    )
    rows = await session.execute(
        select(ranked)
        .where(ranked.c.rank <= per_hotkey)
        .order_by(ranked.c.miner_hotkey, ranked.c.rank)
    )
    by_hotkey: dict[str, list[OwnerAgent]] = {}
    for row in rows.mappings().all():
        by_hotkey.setdefault(row["miner_hotkey"], []).append(
            OwnerAgent(
                agent_id=row["agent_id"],
                agent_name=row["agent_name"],
                agent_version=row["agent_version"],
                agent_status=row["agent_status"],
                artifact_sha256=row["artifact_sha256"],
                submitted_at=row["submitted_at"],
                miner_coldkey=row["miner_coldkey"],
            )
        )
    return by_hotkey


async def resolve_owner_footprint(
    session: AsyncSession,
    *,
    identifier: str,
    depth: int = 1,
    agents_per_hotkey: int = 10,
) -> OwnerFootprint:
    """Return every hotkey reachable from ``identifier`` through payment records.

    ``identifier`` may be a miner hotkey or a payment coldkey; the records
    decide which. Read the module docstring before presenting the result: this
    is a payment-derived signal, not an ownership determination.
    """
    depth = max(1, min(depth, MAX_DEPTH))
    kind = await _classify(session, identifier)
    hotkey_hops, coldkeys, complete = await _walk(session, identifier, kind, depth)

    hotkeys = set(hotkey_hops)
    stats = await _hotkey_stats(session, hotkeys)
    coldkeys_by_hotkey = await _coldkeys_by_hotkey(session, hotkeys)
    agents_by_hotkey = await _recent_agents(session, hotkeys, agents_per_hotkey)

    entries: list[OwnerHotkey] = []
    for hotkey in sorted(hotkeys, key=lambda key: (hotkey_hops[key], key)):
        total, paid, latest = stats.get(hotkey, (0, 0, None))
        agents = tuple(agents_by_hotkey.get(hotkey, ()))
        entries.append(
            OwnerHotkey(
                miner_hotkey=hotkey,
                miner_coldkeys=tuple(coldkeys_by_hotkey.get(hotkey, ())),
                link_hop=hotkey_hops[hotkey],
                submission_count=total,
                paid_submission_count=paid,
                latest_submitted_at=latest,
                agents=agents,
                agents_truncated=len(agents) < total,
            )
        )

    return OwnerFootprint(
        identifier=identifier,
        identifier_kind=kind,
        depth=depth,
        miner_coldkeys=tuple(sorted(coldkeys)),
        hotkeys=tuple(entries),
        hotkey_count=len(entries),
        submission_count=sum(entry.submission_count for entry in entries),
        expansion_complete=complete,
    )
