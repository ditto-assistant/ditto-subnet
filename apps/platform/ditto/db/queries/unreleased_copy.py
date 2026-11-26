"""Storage and context for the unreleased-copy signal.

The pure signal is :mod:`ditto.api_server.unreleased_copy`; this module owns
the index rows, the SQL-side overlap scan, owner and release resolution as of a
candidate's upload, and the recorded shadow signals.
"""

from __future__ import annotations

from array import array
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.distinctive_content import (
    ITEMS_VERSION,
    ArtifactItems,
    public_item_keys,
)
from ditto.api_server.unreleased_copy import (
    DEFAULT_THRESHOLD,
    SIGNAL_VERSION,
    CopySignal,
    IndexedAgent,
    canonical_owners,
)
from ditto.db.models import (
    Agent,
    AgentDistinctiveItems,
    AgentKingship,
    AgentStatus,
    AthReview,
    Score,
    UnreleasedCopySettingsRevision,
    UnreleasedCopySignal,
)
from ditto.db.queries.artifact_release import list_public_source_releases
from ditto.db.queries.artifact_release_settings import artifact_release_policy_as_of
from ditto.db.queries.attestation import list_linked_hotkeys
from ditto.db.queries.benchmark_rollout import preserve_desired_authority
from ditto.db.queries.payments import get_miner_coldkeys_for_agents
from ditto.db.queries.scores import SCORING_QUORUM


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def index_keys_from(items: Iterable[int]) -> list[int]:
    """Sorted item keys with the public corpus removed."""
    public = public_item_keys()
    return sorted(k for k in items if k not in public)


def index_keys(extracted: ArtifactItems) -> list[int]:
    """Sorted item keys of an artifact with the public corpus removed."""
    return index_keys_from(extracted.items)


async def store_items(
    session: AsyncSession,
    *,
    agent_id: UUID,
    extracted: ArtifactItems | None = None,
    keys: list[int] | None = None,
    error: str | None = None,
) -> None:
    """Insert or replace one agent's index row (``error`` when unreadable).

    Pass ``extracted`` (upload path) or precomputed ``keys`` (the sentinel's
    worker process returns keys only).
    """
    if keys is None and extracted is not None:
        keys = index_keys(extracted)
    readable = keys is not None
    keys = keys or []
    values = {
        "agent_id": agent_id,
        "items_version": ITEMS_VERSION,
        "item_keys": keys,
        "item_count": len(keys),
        "error": None if readable else (error or "unreadable"),
        "indexed_at": datetime.now(UTC),
    }
    statement = insert(AgentDistinctiveItems).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[AgentDistinctiveItems.agent_id],
            set_={k: statement.excluded[k] for k in values if k != "agent_id"},
        )
    )


async def agents_missing_items(session: AsyncSession, *, limit: int) -> list[Agent]:
    """Agents with no current-version index row, oldest first.

    Oldest first, because a candidate is only evaluated once every agent
    uploaded before it is indexed (:func:`pending_candidates`).
    """
    missing = (
        select(Agent)
        .outerjoin(
            AgentDistinctiveItems,
            and_(
                AgentDistinctiveItems.agent_id == Agent.agent_id,
                AgentDistinctiveItems.items_version == ITEMS_VERSION,
            ),
        )
        .where(AgentDistinctiveItems.agent_id.is_(None))
        .order_by(Agent.created_at.asc(), Agent.agent_id.asc())
        .limit(limit)
    )
    return list((await session.scalars(missing)).all())


# Candidates are agents uploaded since the v14 era (the backfill's scope) and
# every new upload; all indexed agents stay sources. Older candidates belong
# to ecosystems that share most of their content, which makes each evaluation
# disproportionately expensive for no current enforcement value.
EVALUATE_SINCE = datetime(2026, 10, 15, tzinfo=UTC)


async def pending_candidates(
    session: AsyncSession, *, signal_version: str, limit: int
) -> list[Agent]:
    """Indexed agents without a signal whose whole upload history is indexed.

    Newest first, so a fresh upload is evaluated within one tick even while
    an old backlog is still being worked.
    """
    frontier = await session.scalar(
        select(func.min(Agent.created_at))
        .outerjoin(
            AgentDistinctiveItems,
            and_(
                AgentDistinctiveItems.agent_id == Agent.agent_id,
                AgentDistinctiveItems.items_version == ITEMS_VERSION,
            ),
        )
        .where(AgentDistinctiveItems.agent_id.is_(None))
    )
    statement = (
        select(Agent)
        .join(AgentDistinctiveItems, AgentDistinctiveItems.agent_id == Agent.agent_id)
        .outerjoin(
            UnreleasedCopySignal,
            and_(
                UnreleasedCopySignal.agent_id == Agent.agent_id,
                UnreleasedCopySignal.signal_version == signal_version,
            ),
        )
        .where(
            AgentDistinctiveItems.items_version == ITEMS_VERSION,
            AgentDistinctiveItems.error.is_(None),
            UnreleasedCopySignal.agent_id.is_(None),
            Agent.created_at >= EVALUATE_SINCE,
        )
        .order_by(Agent.created_at.desc(), Agent.agent_id.desc())
        .limit(limit)
    )
    if frontier is not None:
        statement = statement.where(Agent.created_at <= frontier)
    return list((await session.scalars(statement)).all())


@dataclass(frozen=True)
class SignalContext:
    candidate: IndexedAgent
    others: list[IndexedAgent]
    """Identity, owner and release time of each overlapping agent; their keys
    travel separately in :attr:`overlaps`."""
    overlaps: dict[UUID, array]
    """Each other agent's overlap with the candidate, as ``array('q')``."""
    released_before: list[UUID]
    """Every agent publicly released before the candidate's upload (for the
    fuzzy released-content check), whether or not it shares an exact item."""
    owner_names: list[str]
    """Agent names of the candidate's owner (for the same-owner namespace hint)."""


OverlapSource = Callable[[list[int], list[UUID]], Awaitable[dict[UUID, array]]]


async def eligible_sources(session: AsyncSession, agent: Agent) -> list[UUID]:
    """Indexed agents uploaded at or before ``agent`` (candidate excluded)."""
    return list(
        (
            await session.scalars(
                select(AgentDistinctiveItems.agent_id)
                .join(Agent, Agent.agent_id == AgentDistinctiveItems.agent_id)
                .where(
                    AgentDistinctiveItems.agent_id != agent.agent_id,
                    AgentDistinctiveItems.items_version == ITEMS_VERSION,
                    AgentDistinctiveItems.error.is_(None),
                    Agent.created_at <= agent.created_at,
                )
            )
        ).all()
    )


async def load_context(
    session: AsyncSession, agent: Agent, *, overlap: OverlapSource | None = None
) -> SignalContext | None:
    """Everything :func:`ditto.api_server.unreleased_copy.evaluate` needs.

    Each other agent carries only its overlap with the candidate (computed by
    ``overlap`` in the sentinel's worker, else in process), which is all the
    signal reads. Owners include payment coldkeys
    and the candidate's attested hotkey links; release times are publication
    evidence under the policy in force when the candidate was uploaded.
    """
    row = await session.get(AgentDistinctiveItems, agent.agent_id)
    if row is None or row.error is not None or row.items_version != ITEMS_VERSION:
        return None
    keys = list(row.item_keys)
    uploaded_at = _utc(agent.created_at)
    # Streamed and kept as compact ``array('q')`` (8 bytes a key): a candidate
    # from a heavily shared ecosystem overlaps every earlier member, and Python
    # int sets of that would be gigabytes in the API process. The worker
    # process builds the sets it scores with.
    shared: dict[UUID, array] = {}
    if keys and overlap is not None:
        # The sentinel's worker holds the whole index in memory, so the overlap
        # is a vectorized pass there instead of a full-index scan in Postgres.
        shared = await overlap(keys, await eligible_sources(session, agent))
    elif keys:
        # No worker (tests, one-off reads): the same vectorized membership pass
        # in process, over the eligible rows streamed from Postgres.
        candidate_keys = np.unique(np.asarray(keys, dtype=np.int64))
        stream = await session.stream(
            select(AgentDistinctiveItems.agent_id, AgentDistinctiveItems.item_keys)
            .join(Agent, Agent.agent_id == AgentDistinctiveItems.agent_id)
            .where(
                AgentDistinctiveItems.agent_id != agent.agent_id,
                AgentDistinctiveItems.items_version == ITEMS_VERSION,
                AgentDistinctiveItems.error.is_(None),
                Agent.created_at <= agent.created_at,
            )
        )
        async for aid, item_keys in stream:
            hit = np.intersect1d(np.asarray(item_keys, dtype=np.int64), candidate_keys)
            if len(hit):
                values = array("q")
                values.frombytes(hit.tobytes())
                shared[aid] = values
    rows = (
        (
            await session.scalars(
                select(Agent).where(Agent.agent_id.in_([*shared, agent.agent_id]))
            )
        ).all()
        if shared
        else [agent]
    )
    by_id = {a.agent_id: a for a in rows}
    coldkeys = await get_miner_coldkeys_for_agents(session, agent_ids=set(by_id))
    from ditto.api_server.attestation import expected_netuid

    links = [
        (agent.miner_hotkey, link.hotkey)
        for link in await list_linked_hotkeys(
            session, hotkey=agent.miner_hotkey, netuid=expected_netuid()
        )
    ]
    owners = canonical_owners(
        ((a.agent_id, a.miner_hotkey, coldkeys.get(a.agent_id)) for a in rows), links
    )
    policy = await artifact_release_policy_as_of(session, at=uploaded_at)
    released = await list_public_source_releases(
        session, agent_ids=list(shared), quorum=SCORING_QUORUM, policy=policy
    )

    def indexed(a: Agent, item_keys: frozenset[int] = frozenset()) -> IndexedAgent:
        return IndexedAgent(
            agent_id=a.agent_id,
            owner=owners[a.agent_id],
            uploaded_at=_utc(a.created_at),
            released_at=released.get(a.agent_id),
            keys=item_keys,
            name=a.name,
            version=a.version,
            hotkey=a.miner_hotkey,
            status=a.status.value,
        )

    return SignalContext(
        candidate=indexed(agent, frozenset(keys)),
        others=[indexed(by_id[aid]) for aid in shared if aid in by_id],
        overlaps={aid: ks for aid, ks in shared.items() if aid in by_id},
        released_before=await released_agent_ids(session, at=uploaded_at),
        owner_names=await _owner_names(session, agent, rows, owners, links),
    )


async def released_agent_ids(session: AsyncSession, *, at: datetime) -> list[UUID]:
    """Released kings (the fuzzy-pass corpus) as of ``at``, under that policy."""
    policy = await artifact_release_policy_as_of(session, at=at)
    kings = list((await session.scalars(select(AgentKingship.agent_id))).all())
    released = await list_public_source_releases(
        session, agent_ids=kings, quorum=SCORING_QUORUM, policy=policy
    )
    return sorted((a for a, t in released.items() if t < at), key=str)


async def _owner_names(
    session: AsyncSession,
    agent: Agent,
    rows: Sequence[Agent],
    owners: Mapping[UUID, str],
    links: Sequence[tuple[str, str]],
) -> list[str]:
    """Every agent name the candidate's owner has used, before or after it.

    Names identify the owner for the shared-namespace hint, so an owner whose
    namespace is named after a later agent (one operator renaming a line) is
    still recognized; content timing does not apply to a label.
    """
    owner = owners[agent.agent_id]
    hotkeys = {a.miner_hotkey for a in rows if owners.get(a.agent_id) == owner}
    hotkeys.update(h for pair in links for h in pair)
    names = await session.scalars(
        select(Agent.name).where(Agent.miner_hotkey.in_(sorted(hotkeys))).distinct()
    )
    return sorted({*names.all(), agent.name})


def signal_payload(signal: CopySignal, *, evidence_status: str) -> dict[str, Any]:
    """JSON evidence for one signal (operator-only)."""
    return {
        "signal_version": signal.version,
        "items_version": ITEMS_VERSION,
        "threshold": signal.threshold,
        "flagged": signal.flagged,
        "top_score": signal.top_score,
        "candidate_item_count": signal.candidate_item_count,
        "compared_agent_count": signal.compared_agent_count,
        "excluded": signal.excluded,
        "evidence_status": evidence_status,
        "matches": [
            {
                "agent_id": str(m.matched_agent_id),
                "agent_name": m.matched_name,
                "agent_version": m.matched_version,
                "miner_hotkey": m.matched_hotkey,
                "agent_status": m.matched_status,
                "uploaded_at": m.matched_uploaded_at.isoformat(),
                "released_at": (
                    m.matched_released_at.isoformat() if m.matched_released_at else None
                ),
                "release_state": (
                    "released_after_upload"
                    if m.matched_released_at is not None
                    else "unreleased"
                ),
                "score": m.score,
                "probable_same_owner": m.probable_same_owner,
                "shared_count": m.shared_count,
                "shared_by_kind": m.shared_by_kind,
                "items": [
                    {
                        "kind": it.kind,
                        "text": it.preview,
                        "candidate": f"{it.candidate_path}:{it.candidate_line}",
                        "matched": f"{it.matched_path}:{it.matched_line}",
                    }
                    for it in m.items
                ],
            }
            for m in signal.matches
        ],
    }


async def record_signal(
    session: AsyncSession, *, signal: CopySignal, mode: str, payload: dict[str, Any]
) -> None:
    values = {
        "agent_id": signal.candidate_agent_id,
        "signal_version": signal.version,
        "mode": mode,
        "threshold": signal.threshold,
        "flagged": signal.flagged,
        "top_score": signal.top_score,
        "top_matched_agent_id": (
            signal.matches[0].matched_agent_id if signal.matches else None
        ),
        "compared_agent_count": signal.compared_agent_count,
        "candidate_item_count": signal.candidate_item_count,
        "evidence": payload,
        "computed_at": datetime.now(UTC),
    }
    statement = insert(UnreleasedCopySignal).values(**values)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[
                UnreleasedCopySignal.agent_id,
                UnreleasedCopySignal.signal_version,
            ],
            set_={
                k: statement.excluded[k]
                for k in values
                if k not in {"agent_id", "signal_version"}
            },
        )
    )


async def list_signals(
    session: AsyncSession,
    *,
    signal_version: str,
    flagged_only: bool,
    min_score: float,
    limit: int,
    offset: int,
) -> tuple[int, Sequence[tuple[UnreleasedCopySignal, Agent]]]:
    where = [
        UnreleasedCopySignal.signal_version == signal_version,
        UnreleasedCopySignal.top_score >= min_score,
    ]
    if flagged_only:
        where.append(UnreleasedCopySignal.flagged.is_(True))
    total = await session.scalar(
        select(func.count()).select_from(UnreleasedCopySignal).where(*where)
    )
    rows = (
        await session.execute(
            select(UnreleasedCopySignal, Agent)
            .join(Agent, Agent.agent_id == UnreleasedCopySignal.agent_id)
            .where(*where)
            .order_by(UnreleasedCopySignal.top_score.desc(), Agent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).tuples()
    return int(total or 0), list(rows.all())


async def index_coverage(
    session: AsyncSession, *, signal_version: str
) -> dict[str, Any]:
    """Counts that say how far the backfill and evaluation have got."""
    row = (
        await session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM agents) AS agents,
                  (SELECT count(*) FROM agent_distinctive_items
                    WHERE items_version = :iv) AS indexed,
                  (SELECT count(*) FROM agent_distinctive_items
                    WHERE items_version = :iv AND error IS NOT NULL) AS unreadable,
                  (SELECT min(a.created_at) FROM agents a
                    LEFT JOIN agent_distinctive_items d
                      ON d.agent_id = a.agent_id AND d.items_version = :iv
                    WHERE d.agent_id IS NULL) AS oldest_unindexed_at,
                  (SELECT count(*) FROM unreleased_copy_signals
                    WHERE signal_version = :sv) AS evaluated,
                  (SELECT count(*) FROM unreleased_copy_signals
                    WHERE signal_version = :sv AND flagged) AS flagged
                """
            ),
            {"iv": ITEMS_VERSION, "sv": signal_version},
        )
    ).one()
    return dict(row._mapping)


# --- audited mode -------------------------------------------------------------

DEFAULT_MODE = "shadow"
HOLD_REASON_MAX_LOCATIONS = 8


@dataclass(frozen=True)
class EffectiveSettings:
    mode: str
    threshold: float
    revision: int
    enforce_since: datetime | None
    """Start of the current unbroken run of ``enforce`` revisions. Only agents
    uploaded at or after it can be held: enforcement is not retroactive."""


async def settings_history(
    session: AsyncSession, *, limit: int
) -> list[UnreleasedCopySettingsRevision]:
    return list(
        (
            await session.scalars(
                select(UnreleasedCopySettingsRevision)
                .order_by(UnreleasedCopySettingsRevision.revision.desc())
                .limit(limit)
            )
        ).all()
    )


async def effective_settings(session: AsyncSession) -> EffectiveSettings:
    """Current mode and threshold; no revision means shadow at the default."""
    rows = await settings_history(session, limit=500)
    if not rows:
        return EffectiveSettings(DEFAULT_MODE, DEFAULT_THRESHOLD, 0, None)
    current = rows[0]
    since: datetime | None = None
    if current.mode == "enforce":
        for row in rows:
            if row.mode != "enforce":
                break
            since = _utc(row.created_at)
    return EffectiveSettings(current.mode, current.threshold, current.revision, since)


def hold_reason(evidence: dict[str, Any], *, top_score: float, threshold: float) -> str:
    """Miner-visible hold reason citing only the candidate's own locations.

    Deliberately free of the near-duplicate hint, so the copy court classes the
    hold as one it must escalate to an operator rather than resolve itself.
    """
    locations: list[str] = []
    matches = evidence.get("matches") or []
    for item in (matches[0].get("items") or []) if matches else []:
        location = item.get("candidate")
        if location and location not in locations:
            locations.append(location)
        if len(locations) >= HOLD_REASON_MAX_LOCATIONS:
            break
    cited = f" Locations in this artifact: {', '.join(locations)}." if locations else ""
    return (
        "Held for operator copy review by the unreleased-copy signal "
        f"(score {top_score:.0f} weighted shared items, threshold {threshold:.0f}; "
        "a count, not a percentage): this artifact carries "
        "distinctive content that another owner's agent held before this upload "
        f"while it was still unreleased.{cited} This is a hold, not a rejection; "
        "an operator rules on it."
    )


def _eligible(agent: Agent, settings: EffectiveSettings) -> bool:
    return (
        settings.mode == "enforce"
        and settings.enforce_since is not None
        and _utc(agent.created_at) >= settings.enforce_since
    )


async def finalization_hold(
    session: AsyncSession, agent: Agent
) -> tuple[UUID, str] | None:
    """``(duplicate_of, reason)`` when score finalization must hold ``agent``."""
    settings = await effective_settings(session)
    if not _eligible(agent, settings):
        return None
    row = await session.get(UnreleasedCopySignal, (agent.agent_id, SIGNAL_VERSION))
    if (
        row is None
        or row.top_matched_agent_id is None
        or row.top_score < settings.threshold
    ):
        return None
    return row.top_matched_agent_id, hold_reason(
        row.evidence, top_score=row.top_score, threshold=settings.threshold
    )


_FINALIZING = frozenset(
    {
        AgentStatus.UPLOADED,
        AgentStatus.SCREENING,
        AgentStatus.SCREENING_PASSED,
        AgentStatus.QUARANTINED,
        AgentStatus.EVALUATING,
    }
)


async def enforce_signal(
    session: AsyncSession,
    *,
    agent_id: UUID,
    settings: EffectiveSettings,
    now: datetime,
) -> str:
    """Route one flagged signal: hold a scored agent, defer an unscored one.

    Returns the outcome recorded on the signal. Never rejects: a hold is a
    copy-kind ATH review an operator resolves, and the copy court escalates it.
    """
    agent = await session.scalar(
        select(Agent).where(Agent.agent_id == agent_id).with_for_update()
    )
    row = await session.get(UnreleasedCopySignal, (agent_id, SIGNAL_VERSION))
    if agent is None or row is None or row.top_matched_agent_id is None:
        return "not_held:no_signal"
    if row.top_score < settings.threshold:
        return "not_held:below_threshold"
    if not _eligible(agent, settings):
        return "not_held:uploaded_before_enforcement"
    if agent.status in _FINALIZING:
        return "deferred:held_at_score_finalization"
    if agent.status not in (AgentStatus.SCORED, AgentStatus.LIVE):
        return f"not_held:status_{agent.status.value}"
    if await session.scalar(
        select(AthReview.review_id).where(AthReview.agent_id == agent_id)
    ):
        return "not_held:review_exists"
    score_count = int(
        await session.scalar(
            select(func.count()).select_from(Score).where(Score.agent_id == agent_id)
        )
        or 0
    )
    reason = hold_reason(
        row.evidence, top_score=row.top_score, threshold=settings.threshold
    )
    await preserve_desired_authority(session, now=now)
    session.add(
        AthReview(
            review_id=uuid4(),
            agent_id=agent_id,
            status="pending",
            opened_at=now,
            original_duplicate_of=row.top_matched_agent_id,
            original_reason=reason,
            original_policy_version=agent.screening_policy_version,
            original_evidence={
                "sha256": agent.sha256,
                "score_count": score_count,
                "previous_status": agent.status.value,
                "unreleased_copy_score": row.top_score,
            },
            algorithm_provenance={
                "snapshot": "unreleased-copy-sentinel",
                "review_kind": "copy",
                "trigger": "unreleased_copy",
                "algorithm_version": SIGNAL_VERSION,
                "settings_revision": settings.revision,
                "backfilled": False,
                "opened_at_source": "unreleased_copy_sentinel",
            },
        )
    )
    agent.status = AgentStatus.ATH_PENDING_REVIEW
    agent.duplicate_of = row.top_matched_agent_id
    agent.review_reason = reason
    return "held"


async def record_enforcement(
    session: AsyncSession, *, agent_id: UUID, outcome: str, settings: EffectiveSettings
) -> None:
    row = await session.get(UnreleasedCopySignal, (agent_id, SIGNAL_VERSION))
    if row is None:
        return
    row.evidence = {
        **row.evidence,
        "enforcement": {
            "outcome": outcome,
            "settings_revision": settings.revision,
            "at": datetime.now(UTC).isoformat(),
        },
    }


async def stream_index_rows(
    session: AsyncSession, *, since: datetime | None, batch: int
) -> AsyncIterator[list[tuple[UUID, list[int], datetime]]]:
    """Current-version index rows written after ``since``, in batches."""
    statement = select(
        AgentDistinctiveItems.agent_id,
        AgentDistinctiveItems.item_keys,
        AgentDistinctiveItems.indexed_at,
    ).where(
        AgentDistinctiveItems.items_version == ITEMS_VERSION,
        AgentDistinctiveItems.error.is_(None),
    )
    if since is not None:
        statement = statement.where(AgentDistinctiveItems.indexed_at > since)
    result = await session.stream(statement)
    rows: list[tuple[UUID, list[int], datetime]] = []
    async for agent_id, item_keys, indexed_at in result:
        rows.append((agent_id, item_keys, _utc(indexed_at)))
        if len(rows) >= batch:
            yield rows
            rows = []
    if rows:
        yield rows


async def index_rows_for(
    session: AsyncSession, agent_ids: list[UUID]
) -> list[tuple[UUID, list[int], datetime]]:
    rows = await session.execute(
        select(
            AgentDistinctiveItems.agent_id,
            AgentDistinctiveItems.item_keys,
            AgentDistinctiveItems.indexed_at,
        ).where(
            AgentDistinctiveItems.agent_id.in_(agent_ids),
            AgentDistinctiveItems.items_version == ITEMS_VERSION,
            AgentDistinctiveItems.error.is_(None),
        )
    )
    return [(a, k, _utc(t)) for a, k, t in rows.all()]
