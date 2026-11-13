"""Dueling-bench seasons: the frozen generator set of bench v16+ agent runs.

From ``DUELING_MIN_BENCH_VERSION`` (16) every agent run is a datagen half plus a
generator slice of ``DUELING_SLICE_COLUMNS`` columns, frozen per season
(``research/dittobench-datagen/docs/dueling-bench.md``). A season is aligned
with generator rounds and spans ``rounds_per_season`` of them. The lifecycle
(:func:`advance_seasons`, run on every generator-round tick, idempotent):

1. **Commit.** While the previous season (or round) runs, Platform draws the
   next season's 32-byte secret and publishes ``commitment =
   season_commitment(s, secret)`` on Platform, where ``s`` is the round index
   the season opens on. The anchor block does not exist yet. When
   ``onchain_commitment`` is set, elected validators publish
   ``dds1:{s}:{commitment}`` on chain while the season is committed
   (:mod:`ditto.api_server.dueling_season_commitments`, the season twin of
   the round lane's ``dgr1``); shadow seasons publish too.
2. **Battery.** Each candidate column (eligibility rules 1, 2 and 4 of section
   1: active, qualified, a counted or challenger column in a scored round
   inside ``window_days`` with ``column score > 0`` and an unsolved fraction of
   at most 0.20, one per owner by ledger score) and the public default
   generator run a season qualification battery on the trusted generator
   scorer (``dueling-work`` kind ``battery``).
3. **Open.** At the season's opening round boundary, once a scored round
   exists, ``season_seed = season_seed(s, secret, anchor_block_hash)`` with the
   round's open block as anchor, and up to five columns freeze: battery-passed
   candidates by rank, one per owner, then the public default generator, which
   is never masked and fills every remaining column.
   Opening is a mini-rollout (:func:`queue_rotation_retests`): the champion,
   then the retest cohort by rank, get a rotation re-test of every accepted
   score on the new set, and the champion's confirmation family re-anchors
   per ``(champion, bench_version, season)``. Scores of the season before
   stay provisional for one season, then expire from the authoritative
   ledger (:mod:`ditto.db.queries.dueling_season_window`). Then a paid or
   public top-five agent with no score in the season that just closed gets
   one catch-up re-test on the new set (:func:`queue_catchup_retests`), so a
   mid-season scorer does not expire unscored. A shadow season of an open
   rollout's desired version catches up its ranked quorum agents instead,
   so the rollout's activation count does not collapse at the boundary.
4. **Close.** At ``closes_at_round`` (or on an audited rotation), or as soon
   as its bench version is neither active nor rolling out. A due close waits
   while the committed successor cannot open yet (:func:`successor_blocker`,
   e.g. its default-generator battery is still running): every v16+ lease
   needs an open season, so the old set keeps serving and the successor opens
   mid-round the tick it is ready. If the round passes, the successor voids
   and the next boundary tries a fresh one. Disabling dueling
   commits no new season but lets the open one finish. With
   ``skip_unchanged_rounds``, an *idle* boundary extends the open season by
   ``rounds_per_season`` instead (:func:`idle_season_extension`): the next
   season would pin the same generators for the same cohort, and nobody new
   is eligible, so a rotation would only buy the same evidence again. No new
   season opens, so the two-season score window does not advance (nothing
   expires) and no rotation re-test is queued for anyone. Agents uploaded
   after the first extension owe no season duel seeds in it
   (:func:`ditto.api_server.season_duel.season_duel_entrant_excluded`).
5. **Reveal.** ``privacy_window_rounds`` subnet epochs after it closes, the
   secret and season seed become readable, publicly
   (``/public/dueling/seasons``), with the pins ``reproduce-slice`` needs.
6. **Release.** Each pinned generator's exact source is published at
   ``max(last pinning season's reveal, first pinning season's close +
   embargo_hours)`` (:mod:`ditto.api_server.generator_source_releases`).
   Once every source a revealed season pinned is released and the score
   dispute window has passed, its slice bytes are pruned (digests kept).

Seasons only open while ``dueling_enabled``, a contract ``>= 16`` is active
(an authoritative season) or is the desired version of an open rollout (a
``shadow`` season), and the source release policy discloses. Nothing here
runs below ``DUELING_MIN_BENCH_VERSION``: until a v16 contract ships, every
path is inert.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import defer

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.dueling import (
    DuelingBatteryAck,
    DuelingBatteryCandidate,
    DuelingBatteryResult,
    DuelingSliceColumnWork,
    DuelingSliceFallback,
    DuelingSliceResult,
    DuelingWork,
)
from ditto.api_models.generator_round_validator import accepted_grading_versions
from ditto.api_models.source_disclosure import SourceDisclosure
from ditto.api_server.dueling_season_commitments import (
    settle_season_commitment_publications,
)
from ditto.api_server.generator_source_releases import (
    prune_released_slice_bytes,
    sync_generator_source_releases,
)
from ditto.db.models import (
    Agent,
    DuelingSeason,
    DuelingSeasonBattery,
    DuelingSlicePreparation,
    GeneratorRound,
    GeneratorSubmission,
)
from ditto.db.queries.artifact_release_settings import artifact_release_policy
from ditto.db.queries.benchmark_rollout import active_bench_version, open_rollout
from ditto_screening_protocol.dueling import (
    DUELING_SLICE_CASES_PER_COLUMN,
    DUELING_SLICE_COLUMNS,
    DUELING_SLICE_CONTRACT,
    DuelingColumnPin,
    DuelingReport,
    is_dueling_bench_version,
    season_commitment,
    season_seed,
    slice_seed,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_models.generator import (
        GeneratorCompetitionSettings,
        GeneratorScorerFault,
    )
    from ditto.api_server.generator_competition import ResolvedSettings
    from ditto.api_server.generator_rounds import ChainPosition, RoundClock

logger = logging.getLogger(__name__)

DUELING_BATTERY_SEEDS = 32
"""Fresh secret seeds in one season qualification battery."""
DUELING_BATTERY_MINER_CANDIDATES = 2 * DUELING_SLICE_COLUMNS
"""Ranked miner candidates batteried per season: twice the columns, so a
battery failure still leaves the season five eligible owners."""
MAX_DUELING_UNSOLVED_FRACTION = 0.20
"""Eligibility rule 2: at most this share of a column's non-duplicate round
cases may be unsolved."""
BATTERY_LEASE_TTL = timedelta(minutes=30)
SLICE_LEASE_TTL = timedelta(minutes=10)
MAX_BATTERY_ATTEMPTS = 3
MAX_SLICE_ATTEMPTS = 3
DUELING_WORK_KINDS: tuple[str, ...] = ("battery", "slice")
MAX_SLICE_BYTES = 32 << 20
"""``challenge.MaxRoundDatasetBytes``: the largest slice dataset."""


# --- target ----------------------------------------------------------------


@dataclass(frozen=True)
class DuelingTarget:
    """The bench version seasons run at now, and why none can open.

    ``bench_version`` is set whenever a dueling version is active or rolling
    out, even when ``blocked`` (so a lease can still name the version it
    cannot serve); a season opens only when ``blocked`` is None.
    """

    bench_version: int | None
    shadow: bool
    blocked: str | None
    live_versions: frozenset[int]
    """Dueling versions active or rolling out: an open season of any other
    version closes at once."""


async def dueling_target(
    session: AsyncSession, settings: GeneratorCompetitionSettings
) -> DuelingTarget:
    active = await active_bench_version(session)
    rollout = await open_rollout(session)
    desired = rollout.desired_version if rollout is not None else None
    live = frozenset(
        version
        for version in (active, desired)
        if version is not None and is_dueling_bench_version(version)
    )
    version: int | None = None
    shadow = False
    if desired is not None and is_dueling_bench_version(desired) and desired > active:
        # Rollout to a higher dueling version: shadow the desired version so
        # rollout scoring can begin while the authoritative board is still active.
        version, shadow = desired, True
    elif is_dueling_bench_version(active):
        version = active
    elif desired is not None and is_dueling_bench_version(desired):
        version, shadow = desired, True
    elif settings.dueling_shadow_bench_version is not None and is_dueling_bench_version(
        settings.dueling_shadow_bench_version
    ):
        # A canary target: shadow seasons, never authoritative, so a canary at
        # this version can lease a slice before any rollout opens.
        version, shadow = settings.dueling_shadow_bench_version, True
        live = live | {version}
    blocked: str | None = None
    if settings.mode == "off":
        blocked = "generator competition mode is off"
    elif not settings.dueling_enabled:
        blocked = "dueling_enabled is false"
    elif version is None:
        blocked = (
            f"no bench contract >= 16 is active or rolling out (active v{active}"
            + (f", rollout desired v{desired}" if desired is not None else "")
            + ")"
        )
    else:
        policy = await artifact_release_policy(session)
        if policy.disclosure is SourceDisclosure.NEVER:
            blocked = (
                "the source release policy has disclosure = never; a dueling "
                "season requires every pinned generator's source to be released"
            )
    return DuelingTarget(
        bench_version=version, shadow=shadow, blocked=blocked, live_versions=live
    )


# --- eligibility ------------------------------------------------------------


@dataclass(frozen=True)
class SeasonCandidate:
    submission: GeneratorSubmission
    rank: int
    evidence: dict[str, Any]


def _column_score(rnd: Any, submission_id: UUID) -> dict[str, Any] | None:
    for column in rnd.columns or ():
        if column.get("submission_id") != str(submission_id):
            continue
        for scored in (rnd.score or {}).get("columns", ()):
            if scored.get("column_id") == column.get("column_id"):
                return {"column_id": column["column_id"], **scored.get("pack", {})}
    return None


async def season_candidates(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> tuple[list[SeasonCandidate], list[dict[str, Any]]]:
    """Ranked miner candidates for the next season, and who was passed over.

    Eligibility rules 1, 2 and 4 of ``dueling-bench.md`` section 1: an
    ``active``, qualified generator whose latest scored round column (the
    generator ledger entry, inside ``window_days``) scored above 0 with an
    unsolved fraction of at most :data:`MAX_DUELING_UNSOLVED_FRACTION`, one per
    owner, ordered by ledger score. The battery (rule 3) runs on these.
    """
    from ditto.api_server.generator_rounds import build_round_ledger

    ledger = await build_round_ledger(session, resolved=resolved, now=now)
    out: list[SeasonCandidate] = []
    skipped: list[dict[str, Any]] = []
    owners: set[str] = set()
    # One read, without the source archives eligibility never looks at.
    subs = {
        row.submission_id: row
        for row in await session.scalars(
            select(GeneratorSubmission)
            .options(defer(GeneratorSubmission.source_bytes))
            .where(
                GeneratorSubmission.submission_id.in_(
                    [entry.submission_id for entry in ledger.entries]
                )
            )
        )
    }
    for entry in ledger.entries:
        sub = subs.get(entry.submission_id)
        why: str | None = None
        if sub is None or sub.public_default:
            continue
        if sub.status != "active":
            why = f"submission is {sub.status}"
        elif sub.qualified_at is None:
            why = "submission is not qualified"
        if why is None:
            rnd = await session.get(GeneratorRound, entry.round_index)
            column = None if rnd is None else _column_score(rnd, sub.submission_id)
            if column is None:
                why = f"no scored column in round {entry.round_index}"
            elif float(column.get("score") or 0.0) <= 0.0:
                why = f"round {entry.round_index} column score is 0"
            elif float(column.get("unsolved_rate") or 0.0) > (
                MAX_DUELING_UNSOLVED_FRACTION
            ):
                why = (
                    f"round {entry.round_index} unsolved fraction "
                    f"{float(column['unsolved_rate']):.3f} > "
                    f"{MAX_DUELING_UNSOLVED_FRACTION}"
                )
            elif sub.owner_key in owners:
                why = "owner already holds a better-ranked candidate"
        if why is not None:
            skipped.append({"submission_id": str(entry.submission_id), "reason": why})
            continue
        assert sub is not None and column is not None
        owners.add(sub.owner_key)
        out.append(
            SeasonCandidate(
                submission=sub,
                rank=len(out),
                evidence={
                    "ledger_score": entry.score,
                    "round_index": entry.round_index,
                    "column_id": column["column_id"],
                    "unsolved_rate": column.get("unsolved_rate"),
                },
            )
        )
    return out, skipped


# --- lifecycle --------------------------------------------------------------


async def open_season(
    session: AsyncSession, bench_version: int, *, for_update: bool = False
) -> DuelingSeason | None:
    """The open season of ``bench_version``, if any."""
    stmt = select(DuelingSeason).where(
        DuelingSeason.bench_version == bench_version, DuelingSeason.status == "open"
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


def _void(season: DuelingSeason, reason: str) -> None:
    season.status = "void"
    season.void_reason = reason[:1000]


def _close(
    season: DuelingSeason,
    *,
    reason: str,
    position: ChainPosition,
    settings: GeneratorCompetitionSettings,
    now: datetime,
) -> None:
    season.status = "closed"
    season.closed_at = now
    season.close_epoch_index = position.subnet_epoch_index
    season.reveal_at_epoch = (
        position.subnet_epoch_index + settings.privacy_window_rounds
    )
    season.detail = {**(season.detail or {}), "close_reason": reason}


def _commit_season(
    *,
    bench_version: int,
    shadow: bool,
    season_index: int,
    resolved: ResolvedSettings,
    position: ChainPosition,
    now: datetime,
) -> DuelingSeason:
    secret = secrets.token_bytes(32)
    return DuelingSeason(
        id=uuid4(),
        bench_version=bench_version,
        season_index=season_index,
        shadow=shadow,
        status="committed",
        commitment=season_commitment(season_index, secret),
        secret=secret,
        committed_at=now,
        rounds_per_season=resolved.settings.rounds_per_season,
        settings_revision=resolved.revision,
        detail={"committed_epoch_index": position.subnet_epoch_index},
        commitment_publication=(
            "pending" if resolved.settings.onchain_commitment else "disabled"
        ),
    )


async def _queue_batteries(
    session: AsyncSession,
    season: DuelingSeason,
    *,
    resolved: ResolvedSettings,
    now: datetime,
) -> int:
    """Queue every not-yet-batteried candidate of a committed season."""
    from ditto.api_server.generator_rounds import public_default_generator

    existing = {
        row.submission_id: row
        for row in await session.scalars(
            select(DuelingSeasonBattery).where(
                DuelingSeasonBattery.season_id == season.id
            )
        )
    }
    seeds = (
        next(iter(existing.values())).seeds
        if existing
        else [secrets.token_hex(32) for _ in range(DUELING_BATTERY_SEEDS)]
    )
    candidates, skipped = await season_candidates(session, resolved=resolved, now=now)
    excluded = await _excluded_submissions(session, season)
    queued = 0
    eligible = [
        c for c in candidates if str(c.submission.submission_id) not in excluded
    ]
    wanted: list[tuple[GeneratorSubmission, int, dict[str, Any] | None]] = [
        (c.submission, c.rank, c.evidence)
        for c in eligible[:DUELING_BATTERY_MINER_CANDIDATES]
    ]
    default = await public_default_generator(session)
    if default is not None and default.qualified_at is not None:
        wanted.append((default, -1, None))
    for sub, rank, evidence in wanted:
        if sub.submission_id in existing:
            continue
        session.add(
            DuelingSeasonBattery(
                season_id=season.id,
                submission_id=sub.submission_id,
                miner_hotkey=sub.miner_hotkey,
                owner_key="" if sub.public_default else sub.owner_key,
                source_sha256=sub.source_sha256,
                tree_sha256=sub.tree_sha256,
                rank=rank,
                evidence=evidence,
                seeds=seeds,
                status="pending",
                attempts=0,
            )
        )
        existing[sub.submission_id] = None  # type: ignore[assignment]
        queued += 1
    season.detail = {
        **(season.detail or {}),
        "candidates_checked_at": now.isoformat(),
        "skipped_candidates": skipped[:20],
    }
    return queued


async def _excluded_submissions(
    session: AsyncSession, season: DuelingSeason
) -> set[str]:
    """Generators this season may not pin: those recorded at commit, plus
    every pinned generator the season before it substituted on any seed so
    far (it sits out one season and must pass a fresh battery after)."""
    excluded = set((season.detail or {}).get("excluded_submissions", ()))
    previous = await session.scalar(
        select(DuelingSeason)
        .where(
            DuelingSeason.bench_version == season.bench_version,
            DuelingSeason.season_index < season.season_index,
            DuelingSeason.opened_at.is_not(None),
        )
        .order_by(DuelingSeason.season_index.desc())
        .limit(1)
    )
    if previous is not None:
        excluded |= set((previous.detail or {}).get("substituted_submissions", ()))
    return excluded


async def freeze_columns(
    session: AsyncSession, season: DuelingSeason
) -> tuple[list[dict[str, Any]], dict[str, Any] | str]:
    """The season's frozen columns and fallback, or why it cannot open.

    Battery-passed miner candidates in rank order (one per owner, still
    ``active``), then the public default generator in every remaining
    column. The default must itself have passed the battery: it is the
    fallback for every column.
    """
    batteries = list(
        await session.scalars(
            select(DuelingSeasonBattery)
            .where(DuelingSeasonBattery.season_id == season.id)
            .order_by(DuelingSeasonBattery.rank)
        )
    )
    accepted = accepted_grading_versions(season.bench_version)
    excluded = await _excluded_submissions(session, season)
    default: tuple[GeneratorSubmission, DuelingSeasonBattery] | None = None
    miners: list[tuple[GeneratorSubmission, DuelingSeasonBattery]] = []
    for battery in batteries:
        if battery.status != "passed":
            continue
        if battery.grading_bench_version not in accepted:
            continue
        sub = await session.get(GeneratorSubmission, battery.submission_id)
        if sub is None or sub.status != "active":
            continue
        if sub.source_sha256 != battery.source_sha256:
            continue
        if sub.public_default:
            default = (sub, battery)
        elif str(sub.submission_id) not in excluded:
            miners.append((sub, battery))
    if default is None:
        return [], (
            "the public default generator has not passed this season's battery "
            "(register and qualify it with register_default_generator)"
        )
    columns: list[dict[str, Any]] = []
    owners: set[str] = set()
    for sub, battery in miners:
        if len(columns) >= DUELING_SLICE_COLUMNS:
            break
        if sub.owner_key in owners:
            continue
        owners.add(sub.owner_key)
        columns.append(_pin(len(columns), sub, battery, public_default=False))
    default_sub, default_battery = default
    while len(columns) < DUELING_SLICE_COLUMNS:
        columns.append(
            _pin(len(columns), default_sub, default_battery, public_default=True)
        )
    fallback = {
        "submission_id": str(default_sub.submission_id),
        "miner_hotkey": default_sub.miner_hotkey,
        "source_sha256": default_sub.source_sha256,
        "tree_sha256": default_sub.tree_sha256,
        "grading_bench_version": default_battery.grading_bench_version,
    }
    return columns, fallback


def _pin(
    index: int,
    sub: GeneratorSubmission,
    battery: DuelingSeasonBattery,
    *,
    public_default: bool,
) -> dict[str, Any]:
    assert battery.grading_bench_version is not None
    return DuelingColumnPin(
        column_id=f"c{index}",
        submission_id=str(sub.submission_id),
        miner_hotkey=sub.miner_hotkey,
        owner_key="" if public_default else sub.owner_key,
        tree_sha256=sub.tree_sha256,
        source_sha256=sub.source_sha256,
        grading_bench_version=battery.grading_bench_version,
        public_default=public_default,
    ).model_dump(mode="json")


async def _open(
    session: AsyncSession,
    season: DuelingSeason,
    *,
    resolved: ResolvedSettings,
    position: ChainPosition,
    anchor: tuple[int, str],
    now: datetime,
) -> str:
    scored = await session.scalar(
        select(func.count())
        .select_from(GeneratorRound)
        .where(GeneratorRound.status == "scored")
    )
    if not scored:
        _void(season, "no generator round has been scored yet")
        return "void (no scored round yet)"
    columns, fallback = await freeze_columns(session, season)
    if isinstance(fallback, str):
        _void(season, fallback)
        return f"void ({fallback})"
    # Read before the season opens: opening advances the score window, and
    # the agents a catch-up exists for drop out of the board with it.
    catchup_candidates = await _catchup_candidates(
        session, season, settings=resolved.settings
    )
    block, block_hash = anchor
    season.anchor_block = block
    season.anchor_block_hash = block_hash.lower()
    season.season_seed = season_seed(season.season_index, season.secret, block_hash)
    season.columns = columns
    season.status = "open"
    season.opened_at = now
    season.rounds_per_season = resolved.settings.rounds_per_season
    season.closes_at_round = season.season_index + season.rounds_per_season
    season.settings_revision = resolved.revision
    season.detail = {
        **(season.detail or {}),
        "fallback": fallback,
        "opened_epoch_index": position.subnet_epoch_index,
    }
    await session.flush()
    rotation = await queue_rotation_retests(session, season, now=now)
    catchup = await queue_catchup_retests(
        session,
        season,
        candidates=catchup_candidates,
        settings=resolved.settings,
        now=now,
    )
    await _log_rotation_spend(session, season, rotation=rotation, catchup=catchup)
    miners = sum(1 for column in columns if not column["public_default"])
    return (
        f"open ({miners} miner column(s)); "
        f"{rotation.get('queued', 0)} rotation re-test(s) queued; "
        f"{catchup.get('queued', 0)} catch-up re-test(s) queued"
    )


async def _rotation_cohort(
    session: AsyncSession, season: DuelingSeason
) -> tuple[list[UUID], str]:
    """Who a season's opening re-scores, champion first, and where from.

    An authoritative season re-scores the continual-retest cohort of its
    (active) version: the champion, then ``retest_cohort_size`` agents by
    rank. A shadow season re-scores the open rollout's frozen cohort in
    position order. A canary-only shadow season has no cohort.
    """
    from ditto.db.models import BenchmarkRolloutMember

    if season.shadow:
        rollout = await open_rollout(session)
        if rollout is None or rollout.desired_version != season.bench_version:
            return [], "none (canary shadow season: no rollout cohort)"
        members = list(
            await session.scalars(
                select(BenchmarkRolloutMember.agent_id)
                .where(BenchmarkRolloutMember.rollout_id == rollout.rollout_id)
                .order_by(BenchmarkRolloutMember.position)
            )
        )
        return members, "rollout_members"
    from ditto.api_server.continual_retest_settings import settings_from_row
    from ditto.api_server.endpoints.validator import _current_retest_cohort
    from ditto.db.queries.continual_retest_settings import (
        latest_continual_retest_settings_revision,
    )

    revision = await latest_continual_retest_settings_revision(session)
    settings = settings_from_row(revision)
    emission, _wave, cohort, _challengers = await _current_retest_cohort(
        session, canonical_version=season.bench_version, settings=settings
    )
    ordered = list(
        dict.fromkeys(
            [*(m.agent_id for m in emission[:1]), *(m.agent_id for m in cohort)]
        )
    )
    return ordered[: max(1, settings.retest_cohort_size)], "continual_cohort"


async def queue_rotation_retests(
    session: AsyncSession, season: DuelingSeason, *, now: datetime
) -> dict[str, Any]:
    """Queue the season-open rotation re-tests and record them on the season.

    Runs in its own savepoint: a cohort read that fails leaves the season
    open with the failure recorded (``detail.rotation.error``), never a
    season that cannot open. Caller commits.
    """
    from ditto.db.queries.score_retests import queue_dueling_rotation_retests

    rotation: dict[str, Any] = {"queued_at": now.isoformat()}
    try:
        async with session.begin_nested():
            cohort, source = await _rotation_cohort(session, season)
            counts = await queue_dueling_rotation_retests(
                session,
                season_id=season.id,
                bench_version=season.bench_version,
                cohort=cohort,
                now=now,
            )
    except Exception as exc:  # noqa: BLE001 - recorded, never blocks the open
        logger.exception("dueling season %s: rotation re-tests failed", season.id)
        rotation["error"] = f"{type(exc).__name__}: {exc}"[:500]
    else:
        rotation.update(
            source=source,
            cohort=[
                {"agent_id": str(agent_id), "rank": rank}
                for rank, agent_id in enumerate(cohort)
            ],
            **counts,
        )
    season.detail = {**(season.detail or {}), "rotation": rotation}
    return rotation


CATCHUP_KEY = "catchup"
"""``DuelingSeason.detail`` key: the season-boundary catch-up it queued."""
CATCHUP_SOURCE_BOARD = "continual_board"
"""Authoritative season: the active version's paid set and public top five."""
CATCHUP_SOURCE_ROLLOUT = "rollout_ranked_quorum"
"""Shadow season of an open rollout: the desired version's ranked quorum."""


@dataclass(frozen=True)
class CatchupCandidates:
    """Who the board held when the previous season closed, and who of them
    had no score in it. Read before the window advances."""

    closed_season_id: UUID | None
    board: tuple[UUID, ...]
    """Paid set (folded emission set), then the public top five, deduped; for
    a rollout's shadow season, the desired version's ranked quorum agents,
    one per owner family (:func:`_rollout_catchup_board`)."""
    missing: tuple[UUID, ...]
    """``board`` members with no accepted score in the closed season, in
    board order."""
    error: str | None = None
    source: str = CATCHUP_SOURCE_BOARD
    skipped: str | None = None
    """Why this season has no catch-up at all (a canary shadow season)."""


async def _rollout_catchup_board(
    session: AsyncSession, bench_version: int
) -> tuple[UUID, ...]:
    """A shadow season's catch-up board: who the rollout's flip counts.

    The agents holding a ranked desired-version quorum in the season window
    (:func:`~ditto.db.queries.scores.ranked_quorum_agent_ids`, the set
    ``count_ranked_quorum_agents`` gates activation on), best median
    composite first, one per owner family. Not the KOTH board: an explicit
    version ledger read admits pre-quorum medians, and the rollout gate never
    counts those. Scalar columns only, never ``Score.details``.
    """
    from ditto.db.models import EvaluationPayment, Score
    from ditto.db.queries.dueling_season_window import season_scope_predicate
    from ditto.db.queries.scores import (
        attested_emission_owner_roots,
        emission_owner_key,
        ranked_quorum_agent_ids,
    )

    ranked = await ranked_quorum_agent_ids(session, bench_version=bench_version)
    if not ranked:
        return ()
    rows: dict[UUID, list[tuple[float, str]]] = {}
    for agent_id, composite, validator_hotkey in (
        await session.execute(
            select(Score.agent_id, Score.composite, Score.validator_hotkey).where(
                Score.agent_id.in_(ranked),
                Score.bench_version == bench_version,
                await season_scope_predicate(
                    session,
                    version_column=Score.bench_version,
                    season_column=Score.dueling_season_id,
                    bench_versions=(bench_version,),
                ),
            )
        )
    ).tuples():
        rows.setdefault(agent_id, []).append((composite, validator_hotkey))
    # The ledger's median row: ascending composite, validator tie-break.
    medians = {
        agent_id: sorted(scores)[(len(scores) - 1) // 2][0]
        for agent_id, scores in rows.items()
    }
    owners = {
        agent_id: (miner_hotkey, owner, created_at)
        for agent_id, miner_hotkey, owner, created_at in (
            await session.execute(
                select(
                    Agent.agent_id,
                    Agent.miner_hotkey,
                    emission_owner_key(),
                    Agent.created_at,
                )
                .outerjoin(
                    EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id
                )
                .where(Agent.agent_id.in_(medians))
            )
        ).tuples()
    }
    ordered = sorted(
        owners,
        key=lambda agent_id: (-medians[agent_id], owners[agent_id][2], agent_id),
    )
    roots = await attested_emission_owner_roots(
        session, [owners[agent_id][:2] for agent_id in ordered]
    )
    board: dict[str, UUID] = {}
    for agent_id, root in zip(ordered, roots, strict=True):
        board.setdefault(root, agent_id)
    return tuple(board.values())


async def _catchup_candidates(
    session: AsyncSession,
    season: DuelingSeason,
    *,
    settings: GeneratorCompetitionSettings,
) -> CatchupCandidates:
    """The season-boundary catch-up candidates for ``season`` (not yet open).

    An agent scored after a season's opening cohort was picked holds scores
    only in that season. They count one more season as provisional, then
    expire at the next open, and nothing re-scores it on the way: it was not
    in either opening cohort. Every paid or public top-five agent with no
    score in the season that just closed is in that state, so it is read
    here, while those scores still count. Never raises: a failed read is
    recorded and queues nothing.

    A shadow season of the version an open rollout targets reads its board
    from the desired version's ranked quorum instead
    (:func:`_rollout_catchup_board`): its opening cohort is the rollout's
    frozen members, so a new submission that reached a ranked quorum would
    otherwise expire and drop out of the activation count. A canary-only
    shadow season has no catch-up.
    """
    from ditto.db.queries.dueling_season_window import dueling_season_window

    if not settings.season_catchup_enabled:
        return CatchupCandidates(closed_season_id=None, board=(), missing=())
    source = CATCHUP_SOURCE_ROLLOUT if season.shadow else CATCHUP_SOURCE_BOARD
    try:
        async with session.begin_nested():
            if season.shadow:
                rollout = await open_rollout(session)
                if rollout is None or rollout.desired_version != season.bench_version:
                    return CatchupCandidates(
                        closed_season_id=None,
                        board=(),
                        missing=(),
                        source=source,
                        skipped="canary shadow season: no rollout at this version",
                    )
            window = await dueling_season_window(session, season.bench_version)
            closed = window.current if window is not None else None
            if closed is None:
                return CatchupCandidates(
                    closed_season_id=None, board=(), missing=(), source=source
                )
            from ditto.db.models import Score

            if season.shadow:
                board = await _rollout_catchup_board(session, season.bench_version)
            else:
                board = await _continual_catchup_board(session, season.bench_version)
            current = set(
                await session.scalars(
                    select(Score.agent_id).where(
                        Score.agent_id.in_(board or (UUID(int=0),)),
                        Score.bench_version == season.bench_version,
                        Score.dueling_season_id == closed,
                    )
                )
            )
    except Exception as exc:  # noqa: BLE001 - recorded, never blocks the open
        logger.exception("dueling season %s: catch-up read failed", season.id)
        return CatchupCandidates(
            closed_season_id=None,
            board=(),
            missing=(),
            error=f"{type(exc).__name__}: {exc}"[:500],
            source=source,
        )
    return CatchupCandidates(
        closed_season_id=closed,
        board=board,
        missing=tuple(agent_id for agent_id in board if agent_id not in current),
        source=source,
    )


async def _continual_catchup_board(
    session: AsyncSession, bench_version: int
) -> tuple[UUID, ...]:
    """An authoritative season's catch-up board: the paid set (folded
    emission set), then the public top five, deduped."""
    from ditto.api_server.continual_retest_settings import settings_from_row
    from ditto.api_server.endpoints.validator import _current_retest_cohort
    from ditto.db.queries.continual_retest_settings import (
        latest_continual_retest_settings_revision,
    )

    retest_settings = settings_from_row(
        await latest_continual_retest_settings_revision(session)
    )
    emission, wave, _cohort, _challengers = await _current_retest_cohort(
        session, canonical_version=bench_version, settings=retest_settings
    )
    return tuple(
        dict.fromkeys([*(m.agent_id for m in emission), *(m.agent_id for m in wave)])
    )


async def queue_catchup_retests(
    session: AsyncSession,
    season: DuelingSeason,
    *,
    candidates: CatchupCandidates,
    settings: GeneratorCompetitionSettings,
    now: datetime,
) -> dict[str, Any]:
    """Queue one rotation re-test for each catch-up candidate, once.

    Runs after :func:`queue_rotation_retests`: anyone the opening cohort
    already re-scores is skipped, the rest (at most
    ``season_catchup_max_agents``, board order) queue behind the cohort with
    the same mechanics: the new set, the season's rotation seed, idempotent
    per ``(season, agent, validator)``. Nothing else ever queues a catch-up,
    so an agent gets at most one per season. Recorded on the season
    (``detail.catchup``); caller commits.
    """
    from ditto.db.queries.score_retests import queue_dueling_rotation_retests

    record: dict[str, Any] = {
        "queued_at": now.isoformat(),
        "enabled": settings.season_catchup_enabled,
        "max_agents": settings.season_catchup_max_agents,
    }
    if not settings.season_catchup_enabled:
        record["skipped"] = "season_catchup_enabled is off"
    elif candidates.skipped is not None:
        record["skipped"] = candidates.skipped
    elif candidates.error is not None:
        record["error"] = candidates.error
    elif candidates.closed_season_id is None:
        record["skipped"] = "no earlier season opened"
    if set(record) & {"skipped", "error"}:
        season.detail = {**(season.detail or {}), CATCHUP_KEY: record}
        return record
    rotation = (season.detail or {}).get("rotation") or {}
    in_cohort = {str(member.get("agent_id")) for member in rotation.get("cohort", ())}
    owed = [a for a in candidates.missing if str(a) not in in_cohort]
    cap = settings.season_catchup_max_agents
    agents, capped = owed[:cap], owed[cap:]
    record.update(
        source=candidates.source,
        closed_season_id=str(candidates.closed_season_id),
        board=[str(agent_id) for agent_id in candidates.board],
        in_opening_cohort=[str(a) for a in candidates.missing if str(a) in in_cohort],
        agents=[str(agent_id) for agent_id in agents],
        capped=[str(agent_id) for agent_id in capped],
    )
    if agents:
        try:
            async with session.begin_nested():
                counts = await queue_dueling_rotation_retests(
                    session,
                    season_id=season.id,
                    bench_version=season.bench_version,
                    cohort=agents,
                    now=now,
                    catch_up=True,
                    first_rank=len(rotation.get("cohort", ())),
                )
        except Exception as exc:  # noqa: BLE001 - recorded, never blocks the open
            logger.exception("dueling season %s: catch-up re-tests failed", season.id)
            record["error"] = f"{type(exc).__name__}: {exc}"[:500]
        else:
            record.update(counts)
    season.detail = {**(season.detail or {}), CATCHUP_KEY: record}
    return record


async def _log_rotation_spend(
    session: AsyncSession,
    season: DuelingSeason,
    *,
    rotation: dict[str, Any],
    catchup: dict[str, Any],
) -> None:
    """One log line per season open: what it queued, and what the closed
    season's opening rotation and catch-up re-tests really cost."""
    from ditto.db.queries.dueling_rotation_spend import (
        rotation_spend_by_season,
        rotation_spend_since,
    )
    from ditto.db.queries.dueling_season_window import dueling_season_window

    try:
        async with session.begin_nested():
            window = await dueling_season_window(session, season.bench_version)
            closed = window.previous if window is not None else None
            spent = (
                None
                if closed is None
                else (await rotation_spend_by_season(session, [closed])).get(closed)
            )
            day = await rotation_spend_since(
                session, (season.opened_at or datetime.now(UTC)) - timedelta(days=1)
            )
    except Exception:  # noqa: BLE001 - a log line never blocks the open
        logger.exception("dueling season %s: rotation spend read failed", season.id)
        return
    logger.info(
        "dueling season %s v%s opened: %s rotation and %s catch-up re-test(s) "
        "queued (catch-up agents %s, capped %s); closed season %s spent "
        "opening %s run(s) $%.4f, catch-up %s run(s) $%.4f; last 24h opening "
        "$%.4f/day, catch-up $%.4f/day",
        season.season_index,
        season.bench_version,
        rotation.get("queued", 0),
        catchup.get("queued", 0),
        len(catchup.get("agents", ())),
        len(catchup.get("capped", ())),
        closed,
        spent.opening.runs if spent else 0,
        spent.opening.cost_usd if spent else 0.0,
        spent.catchup.runs if spent else 0,
        spent.catchup.cost_usd if spent else 0.0,
        day.opening.cost_usd,
        day.catchup.cost_usd,
    )


IDLE_EXTENSIONS_KEY = "idle_extensions"
"""``DuelingSeason.detail`` key: how often, and when, the season was extended."""
MAX_RECORDED_IDLE_EXTENSIONS = 20
IDLE_ELIGIBLE_STATUSES = (AgentStatus.SCORED, AgentStatus.LIVE, AgentStatus.EVALUATING)
"""An agent created since the season opened in one of these statuses is a
change: scored, or being scored right now (it reaches the board within the
season). A long operator hold (``ath_pending_review``) is not, or one held
agent would force a paid rotation every boundary; its release is."""


def _pin_identity(columns: Any) -> list[list[Any]]:
    """A season's frozen generator set, order-free: who fills each column with
    which exact source and grading policy."""
    return sorted(
        [
            str(column.get("submission_id")),
            str(column.get("source_sha256")),
            column.get("grading_bench_version"),
            bool(column.get("public_default")),
        ]
        for column in columns or ()
    )


def season_first_extended_at(season: DuelingSeason | None) -> datetime | None:
    """When ``season`` first ran past its scheduled close, if ever: extended
    as idle, or its close deferred for a successor (both keep its duel seeds
    public longer than one season)."""
    detail = (season.detail or {}) if season is not None else {}
    stamps: list[datetime] = []
    for key, field in (
        (IDLE_EXTENSIONS_KEY, "first_extended_at"),
        (CLOSE_DEFERRAL_KEY, "first_deferred_at"),
    ):
        record = detail.get(key)
        first = record.get(field) if isinstance(record, dict) else None
        if not isinstance(first, str):
            continue
        # Always aware: it is compared with TIMESTAMPTZ columns (Agent.created_at).
        parsed = datetime.fromisoformat(first)
        stamps.append(
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        )
    return min(stamps) if stamps else None


async def idle_season_extension(
    session: AsyncSession,
    season: DuelingSeason,
    *,
    resolved: ResolvedSettings,
    target: DuelingTarget,
    current: int,
) -> str | None:
    """Why the open, due ``season`` should not rotate, or None to rotate.

    Idle means a rotation would only re-buy the evidence the season already
    holds, so it is extended instead (all or nothing: the whole cohort keeps
    its rows, nobody is re-tested):

    a. the committed season that would open now freezes the same generator
       pins (submission, source digest, grading policy, default flag);
    b. the rotation cohort has the members this season re-scored when it
       opened;
    c. no agent created since the season opened is scored, live or being
       scored;
    d. no rotation was requested (checked by the caller).
    """
    if not resolved.settings.skip_unchanged_rounds:
        return None
    if target.blocked is not None or target.bench_version != season.bench_version:
        return None
    if target.shadow != season.shadow or season.opened_at is None:
        return None
    committed = await session.scalar(
        select(DuelingSeason).where(
            DuelingSeason.bench_version == season.bench_version,
            DuelingSeason.status == "committed",
            DuelingSeason.season_index == current,
        )
    )
    if committed is None or committed.shadow != season.shadow:
        return None
    columns, fallback = await freeze_columns(session, committed)
    if isinstance(fallback, str) or _pin_identity(columns) != _pin_identity(
        season.columns
    ):
        return None
    rotation = (season.detail or {}).get("rotation") or {}
    if rotation.get("error") or "cohort" not in rotation:
        return None
    # Membership, not rank order: the same agents re-scored on the same pins
    # buy the same evidence whatever order the board lists them in.
    opened_cohort = sorted(str(member.get("agent_id")) for member in rotation["cohort"])
    cohort, _source = await _rotation_cohort(session, season)
    if sorted(str(agent_id) for agent_id in cohort) != opened_cohort:
        return None
    entrant = await session.scalar(
        select(Agent.agent_id)
        .where(
            Agent.created_at > season.opened_at,
            Agent.status.in_(IDLE_ELIGIBLE_STATUSES),
        )
        .limit(1)
    )
    if entrant is not None:
        return None
    return (
        "idle: same generator pins and rotation cohort, no new eligible agent "
        "since the season opened"
    )


def _extend(
    season: DuelingSeason,
    *,
    why: str,
    current: int,
    position: ChainPosition,
    now: datetime,
) -> None:
    """Push ``season``'s close one season length out and record why."""
    season.closes_at_round = current + season.rounds_per_season
    detail = dict(season.detail or {})
    record = dict(detail.get(IDLE_EXTENSIONS_KEY) or {})
    history = list(record.get("recent") or [])
    history.append(
        {
            "at_round": current,
            "closes_at_round": season.closes_at_round,
            "epoch_index": position.subnet_epoch_index,
            "at": now.isoformat(),
            "reason": why,
        }
    )
    record.update(
        count=int(record.get("count") or 0) + 1,
        first_extended_at=record.get("first_extended_at") or now.isoformat(),
        last_extended_at=now.isoformat(),
        recent=history[-MAX_RECORDED_IDLE_EXTENSIONS:],
    )
    detail[IDLE_EXTENSIONS_KEY] = record
    season.detail = detail


CLOSE_DEFERRAL_KEY = "close_deferral"
"""``DuelingSeason.detail`` key: a due close held for an unready successor."""
MAX_DEFERRED_ROUNDS = 6
"""Rounds a due close may wait for a successor (about a day at 4.8h rounds).
Past it the season closes as before: a successor that cannot open for a day
is an operator problem, and reveals and source releases must not wait on it
indefinitely."""
ANCHOR_PASSED_KEY = "anchor_passed_at"
"""``DuelingSeason.detail`` key on a committed successor whose anchor block
exists while the season before it is held open: a commitment published from
then on proves nothing, so publication is settled and republish refused."""


async def successor_blocker(
    session: AsyncSession,
    season: DuelingSeason,
    *,
    target: DuelingTarget,
    clock: RoundClock,
    position: ChainPosition,
) -> tuple[str | None, DuelingSeason | None]:
    """Why the season that should follow the due ``season`` cannot open at
    this position (None when it can, or when ``season`` should close
    regardless), and that successor if one is committed.

    Every v16+ lease needs an open season of its version
    (:func:`resolve_dueling_slice`). Closing a due season before its
    successor can open leaves the version with none, and validators are
    refused until a later boundary: on 2026-11-12 the successor's default
    generator battery finished 46 minutes after the boundary, the successor
    was voided, and canonical scoring stalled for 4.8 hours (SN-476). The
    checks mirror what step 3 of :func:`advance_seasons` and :func:`_open`
    would void the successor for. A dueling target that is blocked or moved
    to another version closes the season as before.
    """
    if target.blocked is not None or target.bench_version != season.bench_version:
        return None, None
    current = clock.current
    successor = await session.scalar(
        select(DuelingSeason).where(
            DuelingSeason.bench_version == season.bench_version,
            DuelingSeason.status == "committed",
            DuelingSeason.season_index == current,
        )
    )
    if successor is None:
        return f"no season is committed for round {current}", None
    if successor.shadow != target.shadow:
        return "the committed successor's contract changed before it opened", successor
    committed_epoch = int((successor.detail or {}).get("committed_epoch_index", -1))
    if committed_epoch >= clock.first_epoch:
        return "the committed successor was committed after its anchor block", successor
    if await _anchor(session, current, clock.first_epoch, position) is None:
        return "the successor's opening epoch was missed", successor
    scored = await session.scalar(
        select(func.count())
        .select_from(GeneratorRound)
        .where(GeneratorRound.status == "scored")
    )
    if not scored:
        return "no generator round has been scored yet", successor
    _columns, fallback = await freeze_columns(session, successor)
    if isinstance(fallback, str):
        return fallback, successor
    return None, successor


def _deferred_rounds(season: DuelingSeason) -> int:
    record = (season.detail or {}).get(CLOSE_DEFERRAL_KEY)
    return int(record.get("rounds") or 0) if isinstance(record, dict) else 0


def _defer_close(
    season: DuelingSeason, *, why: str, current: int, now: datetime
) -> bool:
    """Record that ``season``'s due close waits for its successor; True when
    the record changed. Written only when the reason or round changes, so a
    tick loop does not churn."""
    detail = dict(season.detail or {})
    record = dict(detail.get(CLOSE_DEFERRAL_KEY) or {})
    if record.get("reason") == why and record.get("at_round") == current:
        return False
    record.update(
        first_deferred_at=record.get("first_deferred_at") or now.isoformat(),
        last_deferred_at=now.isoformat(),
        at_round=current,
        reason=why[:500],
        rounds=int(record.get("rounds") or 0)
        + (1 if record.get("at_round") != current else 0),
    )
    detail[CLOSE_DEFERRAL_KEY] = record
    season.detail = detail
    return True


async def _settle_successor_at_anchor(
    session: AsyncSession, successor: DuelingSeason, *, now: datetime
) -> None:
    """The successor's anchor block exists while it waits: settle its
    on-chain commitment publication now, exactly as opening would, so no
    claim is offered and no confirmation counts after the anchor."""
    from ditto.api_server.dueling_season_commitments import (
        season_commitment_confirmations,
    )

    if ANCHOR_PASSED_KEY in (successor.detail or {}):
        return
    successor.detail = {**(successor.detail or {}), ANCHOR_PASSED_KEY: now.isoformat()}
    if successor.commitment_publication != "pending":
        return
    if await season_commitment_confirmations(session, successor.id) > 0:
        successor.commitment_publication = "published"
        successor.commitment_error = None
    else:
        successor.commitment_publication = "failed"
        successor.commitment_error = (
            "no validator confirmed the on-chain commitment before the "
            "season's anchor block"
        )


async def advance_seasons(
    session: AsyncSession,
    *,
    resolved: ResolvedSettings,
    position: ChainPosition,
    now: datetime,
) -> list[str]:
    """Apply every due season transition at this chain position (caller
    commits). Idempotent at the same position. Every lock is taken in
    ``season_index`` order."""
    from ditto.api_server.generator_rounds import round_clock

    settings = resolved.settings
    notes: list[str] = []
    target = await dueling_target(session, settings)
    clock = await round_clock(
        session, position.subnet_epoch_index, settings.epochs_per_round
    )
    current = clock.current

    # 1. Close open seasons that are due, rotated immediately, or whose bench
    # version is no longer active or rolling out.
    for season in await session.scalars(
        select(DuelingSeason)
        .where(DuelingSeason.status == "open")
        .order_by(DuelingSeason.season_index)
        .with_for_update()
    ):
        if (
            season.rotation_requested_at is not None
            and not season.rotation_immediate
            and (season.closes_at_round is None or season.closes_at_round > current + 1)
        ):
            # An audited rotation closes the season at the next round boundary.
            season.closes_at_round = current + 1
            notes.append(
                f"season {season.season_index} v{season.bench_version}: rotation "
                f"closes it at round {current + 1}"
            )
        reason: str | None = None
        if season.bench_version not in target.live_versions:
            reason = f"bench v{season.bench_version} is no longer active or rolling out"
        elif season.rotation_requested_at is not None and season.rotation_immediate:
            reason = f"rotated immediately: {season.rotation_reason}"
        elif season.closes_at_round is not None and season.closes_at_round <= current:
            idle = (
                await idle_season_extension(
                    session,
                    season,
                    resolved=resolved,
                    target=target,
                    current=current,
                )
                if season.rotation_requested_at is None
                else None
            )
            if idle is not None:
                # The privacy window counts from the extended close, so the
                # secret is never revealed before the season really ends.
                _extend(season, why=idle, current=current, position=position, now=now)
                notes.append(
                    f"season {season.season_index} v{season.bench_version}: "
                    f"extended to round {season.closes_at_round} ({idle})"
                )
                continue
            # An audited rotation keeps its promise to close at the boundary
            # (an operator rotating out a bad generator wants it gone).
            blocker, successor = (
                await successor_blocker(
                    session, season, target=target, clock=clock, position=position
                )
                if season.rotation_requested_at is None
                else (None, None)
            )
            deferred = _deferred_rounds(season)
            capped = blocker is not None and deferred >= MAX_DEFERRED_ROUNDS
            if blocker is not None and not capped:
                # Closing now would leave the version with no open season, and
                # every v16+ lease is refused until one opens. Keep serving
                # this season's set until the successor can take over.
                if successor is not None and successor.status == "committed":
                    await _settle_successor_at_anchor(session, successor, now=now)
                if _defer_close(season, why=blocker, current=current, now=now):
                    logger.warning(
                        "dueling season %s v%s: close deferred at round %s: %s",
                        season.season_index,
                        season.bench_version,
                        current,
                        blocker,
                    )
                    notes.append(
                        f"season {season.season_index} v{season.bench_version}: "
                        f"close deferred ({blocker})"
                    )
                continue
            if capped:
                logger.error(
                    "dueling season %s v%s: successor still not ready after %s "
                    "deferred round(s); closing with no successor: %s",
                    season.season_index,
                    season.bench_version,
                    deferred,
                    blocker,
                )
            reason = (
                f"rotated: {season.rotation_reason}"
                if season.rotation_requested_at is not None
                else (
                    f"season ended (successor not ready after {deferred} "
                    f"deferred round(s): {blocker})"[:500]
                    if capped
                    else "season ended"
                )
            )
        if reason is not None:
            _close(season, reason=reason, position=position, settings=settings, now=now)
            notes.append(
                f"season {season.season_index} v{season.bench_version}: closed"
            )

    # 2. Reveal closed seasons past the privacy window (counted in epochs).
    for season in await session.scalars(
        select(DuelingSeason)
        .where(
            DuelingSeason.status == "closed",
            DuelingSeason.reveal_at_epoch <= position.subnet_epoch_index,
        )
        .order_by(DuelingSeason.season_index)
        .with_for_update()
    ):
        season.status = "revealed"
        season.revealed_at = now
        notes.append(f"season {season.season_index} v{season.bench_version}: revealed")

    # 3. Open the committed season of the current round, or void committed
    # seasons that can no longer open as committed.
    for season in await session.scalars(
        select(DuelingSeason)
        .where(DuelingSeason.status == "committed")
        .order_by(DuelingSeason.season_index)
        .with_for_update()
    ):
        label = f"season {season.season_index} v{season.bench_version}"
        if season.season_index < current:
            _void(season, "season boundary passed without opening")
            notes.append(f"{label}: void (boundary passed)")
            continue
        if season.bench_version != target.bench_version or (
            season.shadow != target.shadow
        ):
            _void(season, "bench contract changed before the season opened")
            notes.append(f"{label}: void (contract changed)")
            continue
        if target.blocked is not None:
            _void(season, f"cannot open: {target.blocked}")
            notes.append(f"{label}: void ({target.blocked})")
            continue
        if season.season_index != current:
            continue
        committed_epoch = int((season.detail or {}).get("committed_epoch_index", -1))
        if committed_epoch >= clock.first_epoch:
            _void(season, "not committed before its anchor block")
            notes.append(f"{label}: void (committed too late)")
            continue
        if await open_season(session, season.bench_version) is not None:
            continue  # the previous season has not closed yet
        anchor = await _anchor(session, current, clock.first_epoch, position)
        if anchor is None:
            _void(season, "season opening epoch was missed")
            notes.append(f"{label}: void (opening epoch missed)")
            continue
        await session.flush()
        outcome = await _open(
            session,
            season,
            resolved=resolved,
            position=position,
            anchor=anchor,
            now=now,
        )
        notes.append(f"{label}: {outcome}")

    # 4. Commit the next season ahead of its anchor, and queue its batteries.
    if target.blocked is None and target.bench_version is not None:
        await session.flush()
        live = await open_season(session, target.bench_version)
        next_index = (
            live.closes_at_round
            if live is not None and live.closes_at_round is not None
            else current + 1
        )
        next_index = max(next_index, current + 1)
        committed = await session.scalar(
            select(DuelingSeason)
            .where(
                DuelingSeason.bench_version == target.bench_version,
                DuelingSeason.status == "committed",
            )
            .with_for_update()
        )
        if (
            live is not None
            and live.closes_at_round is not None
            and live.closes_at_round <= current
            and committed is not None
            and committed.season_index == current
        ):
            # The live season's close is deferred until this round's
            # committed successor can open: keep it, it opens mid-round.
            next_index = current
        if committed is not None and committed.season_index != next_index:
            _void(committed, f"season boundary moved to round {next_index}")
            notes.append(f"season {committed.season_index}: void (boundary moved)")
            await session.flush()
            committed = None
        if committed is None:
            already = await session.scalar(
                select(DuelingSeason)
                .where(
                    DuelingSeason.bench_version == target.bench_version,
                    DuelingSeason.season_index == next_index,
                )
                .with_for_update()
            )
            if (
                already is not None
                and already.status == "void"
                and already.opened_at is None
            ):
                # A season voided before it opened (its boundary has not come
                # yet) is recommitted on a fresh secret rather than leaving
                # its round without a season.
                await session.execute(
                    delete(DuelingSeasonBattery).where(
                        DuelingSeasonBattery.season_id == already.id
                    )
                )
                await session.delete(already)
                await session.flush()
                notes.append(
                    f"season {next_index}: void season replaced by a fresh commit"
                )
                already = None
            if already is None:
                committed = _commit_season(
                    bench_version=target.bench_version,
                    shadow=target.shadow,
                    season_index=next_index,
                    resolved=resolved,
                    position=position,
                    now=now,
                )
                if live is not None:
                    committed.detail = {
                        **(committed.detail or {}),
                        "excluded_submissions": list(
                            (live.detail or {}).get("substituted_submissions", ())
                        ),
                    }
                session.add(committed)
                await session.flush()
                notes.append(
                    f"season {next_index} v{target.bench_version}: committed"
                    + (" (shadow)" if target.shadow else "")
                )
        if committed is not None:
            queued = await _queue_batteries(
                session, committed, resolved=resolved, now=now
            )
            if queued:
                notes.append(
                    f"season {committed.season_index}: {queued} battery candidate(s)"
                )
    await session.flush()
    # Close on-chain commitment publication for seasons that left committed.
    notes.extend(await settle_season_commitment_publications(session))
    # 5. Publish pinned generator sources whose embargo and reveal have passed.
    try:
        async with session.begin_nested():
            notes.extend(await sync_generator_source_releases(session, now=now))
    except Exception:  # noqa: BLE001 - a release sync never blocks seasons
        logger.exception("dueling seasons: generator source release sync failed")
    # 6. Prune slice bytes anyone can now regenerate, in its own savepoint.
    try:
        async with session.begin_nested():
            notes.extend(await prune_released_slice_bytes(session, now=now))
    except Exception:  # noqa: BLE001 - pruning never blocks seasons
        logger.exception("dueling seasons: slice byte prune failed")
    for note in notes:
        logger.info("dueling seasons: %s", note)
    return notes


async def _anchor(
    session: AsyncSession, round_index: int, first_epoch: int, position: ChainPosition
) -> tuple[int, str] | None:
    """The season anchor: the opening round's open block, which is the
    boundary block of the round's first epoch."""
    row = await session.get(GeneratorRound, round_index)
    if row is not None and row.open_block is not None and row.open_block_hash:
        return int(row.open_block), row.open_block_hash
    if position.subnet_epoch_index == first_epoch:
        return position.last_epoch_block, position.last_epoch_block_hash
    return None


# --- scorer work ------------------------------------------------------------


class DuelingResultRejected(ValueError):
    """A scorer dueling result does not match its lease."""


async def _submission_source(
    session: AsyncSession, submission_id: str | UUID, source_sha256: str
) -> GeneratorSubmission:
    sub = await session.get(GeneratorSubmission, UUID(str(submission_id)))
    if sub is None or sub.source_sha256 != source_sha256:
        raise DuelingResultRejected(
            f"generator {submission_id} source is no longer available"
        )
    return sub


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


async def lease_slice_work(
    session: AsyncSession, *, now: datetime
) -> DuelingWork | None:
    """Lease the oldest slice preparation waiting for the scorer."""
    row = await session.scalar(
        select(DuelingSlicePreparation)
        .where(
            DuelingSlicePreparation.status.in_(("pending", "leased")),
            (DuelingSlicePreparation.lease_expires_at.is_(None))
            | (DuelingSlicePreparation.lease_expires_at <= now),
        )
        .order_by(DuelingSlicePreparation.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    season = await session.get(DuelingSeason, row.season_id)
    if slice_attempts_exhausted(row):
        _fail_slice(row, "slice lease expired repeatedly", now=now)
        await session.flush()
        return None
    if season is None or season.season_seed is None or not season.columns:
        _fail_slice(row, "season has no frozen columns", now=now)
        await session.flush()
        return None
    fallback = (season.detail or {}).get("fallback") or {}
    try:
        fallback_sub = await _submission_source(
            session,
            fallback.get("submission_id", ""),
            fallback.get("source_sha256", ""),
        )
        columns = []
        for column in season.columns:
            sub = await _submission_source(
                session, column["submission_id"], column["source_sha256"]
            )
            columns.append(
                DuelingSliceColumnWork(
                    column_id=column["column_id"],
                    submission_id=sub.submission_id,
                    miner_hotkey=column["miner_hotkey"],
                    owner_key=column["owner_key"],
                    source_sha256=sub.source_sha256,
                    source_base64=_b64(sub.source_bytes),
                    tree_sha256=sub.tree_sha256,
                    grading_bench_version=int(column["grading_bench_version"]),
                    slice_seed=slice_seed(
                        season.season_seed, int(row.run_seed), column["column_id"]
                    ),
                    public_default=bool(column["public_default"]),
                )
            )
    except (DuelingResultRejected, ValueError) as exc:
        _fail_slice(row, str(exc), now=now)
        await session.flush()
        return None
    row.status = "leased"
    row.lease_id = uuid4()
    row.lease_expires_at = now + SLICE_LEASE_TTL
    row.attempts += 1
    row.first_leased_at = row.first_leased_at or now
    await session.flush()
    return DuelingWork(
        kind="slice",
        lease_id=row.lease_id,
        season_id=season.id,
        bench_version=season.bench_version,
        lease_expires_at=row.lease_expires_at,
        preparation_id=row.id,
        season_index=season.season_index,
        run_seed=int(row.run_seed),
        cases_per_column=DUELING_SLICE_CASES_PER_COLUMN,
        columns=columns,
        fallback=DuelingSliceFallback(
            submission_id=fallback_sub.submission_id,
            miner_hotkey=fallback_sub.miner_hotkey,
            source_sha256=fallback_sub.source_sha256,
            source_base64=_b64(fallback_sub.source_bytes),
            tree_sha256=fallback_sub.tree_sha256,
        ),
    )


UNSUPPORTED_SLICE_ERROR_PREFIX = "infra: unsupported_request:"
"""A preparation failed this way was refused by the scorer itself (a digest
mismatch on the pinned source, an unbuildable request): a retry repeats it,
so it needs a season rotation, not a re-queue."""


def slice_attempts_exhausted(row: DuelingSlicePreparation) -> bool:
    """Each operator retry restarts the lease attempt cap."""
    return row.attempts >= MAX_SLICE_ATTEMPTS * (1 + (row.retries or 0))


def slice_retry_refusal(
    row: DuelingSlicePreparation, season: DuelingSeason | None, *, force: bool
) -> str | None:
    """Why this preparation cannot be re-queued now, or None."""
    if row.status != "failed":
        return f"preparation is {row.status}; only a failed one is retried"
    if season is None or season.status != "open":
        status = "missing" if season is None else season.status
        return (
            f"its season is {status}; only an open season's slices are retried "
            "(a closed season's tickets keep their pinned bytes or are refused)"
        )
    if not force and (row.error or "").startswith(UNSUPPORTED_SLICE_ERROR_PREFIX):
        return (
            "the scorer refused this request itself (a digest mismatch or an "
            "unbuildable pin): rotate the season instead, or pass force with a "
            "reason"
        )
    return None


def retry_slice(
    row: DuelingSlicePreparation, *, actor: str, reason: str, now: datetime
) -> None:
    """Re-queue one failed preparation: attempts kept, error cleared."""
    row.status = "pending"
    row.error = None
    row.failed_at = None
    row.columns = None
    row.scorer_version = None
    row.lease_id = None
    row.lease_expires_at = None
    row.retries = (row.retries or 0) + 1
    row.last_retry_at = now
    row.last_retry_actor = actor
    row.last_retry_reason = reason


def _fail_slice(row: DuelingSlicePreparation, error: str, *, now: datetime) -> None:
    row.status = "failed"
    row.error = error[:2000]
    row.failed_at = now
    row.lease_id = None
    row.lease_expires_at = None


async def lease_battery_work(
    session: AsyncSession, *, now: datetime
) -> DuelingWork | None:
    """Lease every waiting battery candidate of the oldest committed season."""
    first = await session.scalar(
        select(DuelingSeasonBattery)
        .join(DuelingSeason, DuelingSeason.id == DuelingSeasonBattery.season_id)
        .where(
            DuelingSeason.status == "committed",
            DuelingSeasonBattery.status.in_(("pending", "leased")),
            (DuelingSeasonBattery.lease_expires_at.is_(None))
            | (DuelingSeasonBattery.lease_expires_at <= now),
        )
        .order_by(DuelingSeason.season_index, DuelingSeasonBattery.rank)
        .limit(1)
        .with_for_update(of=DuelingSeasonBattery, skip_locked=True)
    )
    if first is None:
        return None
    season = await session.get(DuelingSeason, first.season_id)
    assert season is not None
    rows = list(
        await session.scalars(
            select(DuelingSeasonBattery)
            .where(
                DuelingSeasonBattery.season_id == season.id,
                DuelingSeasonBattery.status.in_(("pending", "leased")),
                (DuelingSeasonBattery.lease_expires_at.is_(None))
                | (DuelingSeasonBattery.lease_expires_at <= now),
            )
            .order_by(DuelingSeasonBattery.rank)
            .with_for_update(skip_locked=True)
        )
    )
    lease_id = uuid4()
    expires = now + BATTERY_LEASE_TTL
    candidates: list[DuelingBatteryCandidate] = []
    for row in rows:
        if row.attempts >= MAX_BATTERY_ATTEMPTS:
            _finish_battery(
                row, ok=False, error="infra: battery lease expired repeatedly", now=now
            )
            continue
        sub = await session.get(GeneratorSubmission, row.submission_id)
        if sub is None or sub.source_sha256 != row.source_sha256:
            _finish_battery(
                row, ok=False, error="source is no longer available", now=now
            )
            continue
        row.status = "leased"
        row.lease_id = lease_id
        row.lease_expires_at = expires
        row.attempts += 1
        candidates.append(
            DuelingBatteryCandidate(
                submission_id=sub.submission_id,
                miner_hotkey=sub.miner_hotkey,
                owner_key=row.owner_key,
                source_sha256=sub.source_sha256,
                source_base64=_b64(sub.source_bytes),
            )
        )
    await session.flush()
    if not candidates:
        return None
    return DuelingWork(
        kind="battery",
        lease_id=lease_id,
        season_id=season.id,
        bench_version=season.bench_version,
        lease_expires_at=expires,
        cases=DUELING_SLICE_CASES_PER_COLUMN,
        seeds=list(rows[0].seeds),
        candidates=candidates,
    )


def _finish_battery(
    row: DuelingSeasonBattery,
    *,
    ok: bool,
    now: datetime,
    error: str | None = None,
    grading: int | None = None,
    scorer_version: str | None = None,
) -> None:
    row.status = "passed" if ok else "failed"
    row.grading_bench_version = grading
    row.error = None if ok else (error or "battery failed")[:2000]
    row.scorer_version = scorer_version
    row.finished_at = now
    row.lease_id = None
    row.lease_expires_at = None


async def lease_dueling_work(
    session: AsyncSession,
    *,
    resolved: ResolvedSettings,
    kinds: frozenset[str],
    now: datetime,
) -> DuelingWork | None:
    """Slices first (a validator lease waits on each), then batteries."""
    if resolved.settings.mode == "off":
        return None
    if "slice" in kinds:
        work = await lease_slice_work(session, now=now)
        if work is not None:
            return work
    if "battery" in kinds:
        return await lease_battery_work(session, now=now)
    return None


async def record_battery(
    session: AsyncSession, payload: DuelingBatteryResult, *, now: datetime
) -> DuelingBatteryAck:
    rows = list(
        await session.scalars(
            select(DuelingSeasonBattery)
            .where(
                DuelingSeasonBattery.season_id == payload.season_id,
                DuelingSeasonBattery.lease_id == payload.lease_id,
            )
            .with_for_update()
        )
    )
    if not rows:
        raise DuelingResultRejected("battery lease is not current")
    season = await session.get(DuelingSeason, payload.season_id)
    assert season is not None
    accepted = accepted_grading_versions(season.bench_version)
    results = {result.submission_id: result for result in payload.candidates}
    passed: list[UUID] = []
    failed: list[UUID] = []
    released: list[UUID] = []
    for row in rows:
        result = results.get(row.submission_id)
        if result is None:
            row.status = "pending"
            row.lease_id = None
            row.lease_expires_at = None
            released.append(row.submission_id)
            continue
        ok = result.ok
        error = result.error
        if ok and result.grading_bench_version not in accepted:
            ok = False
            error = (
                f"declares grading policy v{result.grading_bench_version}; a "
                f"v{season.bench_version} season accepts "
                + ", ".join(f"v{v}" for v in accepted)
            )
        _finish_battery(
            row,
            ok=ok,
            error=error,
            grading=result.grading_bench_version if ok else None,
            scorer_version=payload.scorer_version,
            now=now,
        )
        (passed if ok else failed).append(row.submission_id)
    await session.flush()
    return DuelingBatteryAck(
        season_id=payload.season_id, passed=passed, failed=failed, released=released
    )


def _check_slice_bytes(
    raw: bytes, *, season: DuelingSeason, row: DuelingSlicePreparation
) -> dict[str, dict[str, Any]]:
    """Light structural check of a posted slice against the season pin.

    The scorer and the validator run the full Go contract
    (``challenge.DecodeSliceDataset``); Platform only refuses bytes that do
    not belong to this season, run seed and pin.
    """
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DuelingResultRejected("slice is not JSON") from exc
    if not isinstance(doc, dict) or doc.get("contract") != DUELING_SLICE_CONTRACT:
        raise DuelingResultRejected(f"slice contract is not {DUELING_SLICE_CONTRACT}")
    if (
        doc.get("season_id") != str(season.id)
        or doc.get("season_index") != season.season_index
        or doc.get("run_seed") != row.run_seed
        or doc.get("bench_version") != season.bench_version
    ):
        raise DuelingResultRejected("slice does not name this season and run seed")
    pinned = {column["column_id"]: column for column in season.columns or ()}
    fallback = (season.detail or {}).get("fallback") or {}
    columns = doc.get("columns")
    if not isinstance(columns, list) or not all(isinstance(c, dict) for c in columns):
        raise DuelingResultRejected("slice columns are malformed")
    if [c.get("column_id") for c in columns] != sorted(pinned):
        raise DuelingResultRejected("slice columns do not match the season pin")
    out: dict[str, dict[str, Any]] = {}
    assert season.season_seed is not None
    for column in columns:
        pin = pinned[column["column_id"]]
        substituted = bool(column.get("substituted"))
        expected_submission = (
            fallback.get("submission_id") if substituted else pin["submission_id"]
        )
        if column.get("submission_id") != expected_submission:
            raise DuelingResultRejected(
                f"slice column {pin['column_id']} names another generator"
            )
        if column.get("slice_seed") != slice_seed(
            season.season_seed, int(row.run_seed), pin["column_id"]
        ):
            raise DuelingResultRejected(
                f"slice column {pin['column_id']} has the wrong slice seed"
            )
        if substituted and column.get("owner_key"):
            raise DuelingResultRejected(
                f"substituted slice column {pin['column_id']} names an owner"
            )
        out[pin["column_id"]] = {
            "column_id": pin["column_id"],
            "submission_id": column.get("submission_id"),
            "substituted": substituted,
            "pack_sha256": column.get("pack_sha256"),
        }
    return out


async def record_slice(
    session: AsyncSession, payload: DuelingSliceResult, *, now: datetime
) -> DuelingSlicePreparation:
    """Pin a posted slice for the current lease (caller commits).

    Existing pinned bytes win: a preparation that is already ready is never
    overwritten. A post without bytes (a column ended with no pack, or the
    slice would not encode) fails the preparation for operator review.
    """
    row = await session.scalar(
        select(DuelingSlicePreparation)
        .where(DuelingSlicePreparation.id == payload.preparation_id)
        .with_for_update()
    )
    if row is None:
        raise DuelingResultRejected("slice preparation not found")
    if row.status != "leased" or row.lease_id != payload.lease_id:
        raise DuelingResultRejected(
            f"slice preparation is {row.status}; lease is not current"
        )
    season = await session.get(DuelingSeason, row.season_id)
    assert season is not None
    row.scorer_version = payload.scorer_version
    column_errors: list[dict[str, Any]] = [
        {
            "column_id": c.column_id,
            "ok": c.ok,
            "substituted": c.substituted,
            "error": c.error,
        }
        for c in payload.columns
    ]
    if payload.slice_base64 is None or payload.slice_sha256 is None:
        errors = (
            "; ".join(f"{c.column_id}: {c.error}" for c in payload.columns if c.error)
            or "scorer posted no slice"
        )
        # A column with no pack (its generator and the fallback both
        # failed) or a slice that would not encode: terminal, for operator
        # review, like a failed private dataset preparation.
        row.columns = column_errors
        _fail_slice(row, errors, now=now)
        await session.flush()
        return row
    try:
        raw = base64.b64decode(payload.slice_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DuelingResultRejected("slice_base64 is not valid base64") from exc
    if not 0 < len(raw) <= MAX_SLICE_BYTES:
        raise DuelingResultRejected("slice size is out of bounds")
    if hashlib.sha256(raw).hexdigest() != payload.slice_sha256:
        raise DuelingResultRejected("slice_sha256 does not match the slice bytes")
    checked = _check_slice_bytes(raw, season=season, row=row)
    for column in column_errors:
        entry = checked.get(str(column["column_id"]))
        if entry is not None:  # a report for an unpinned column is ignored
            entry.update({"ok": column["ok"], "error": column["error"]})
    row.status = "ready"
    row.slice_bytes = raw
    row.slice_sha256 = payload.slice_sha256
    row.columns = [checked[key] for key in sorted(checked)]
    row.error = None
    row.ready_at = now
    row.lease_id = None
    row.lease_expires_at = None
    substituted = sorted(
        {
            pin["submission_id"]
            for pin in season.columns or ()
            if checked.get(pin["column_id"], {}).get("substituted")
            and not pin["public_default"]
        }
    )
    if substituted:
        # A pinned generator that failed on a run seed sits out the next
        # season and must pass a fresh battery after that.
        known = set((season.detail or {}).get("substituted_submissions", ()))
        season.detail = {
            **(season.detail or {}),
            "substituted_submissions": sorted(known | set(substituted)),
        }
    await session.flush()
    return row


async def record_dueling_fault(
    session: AsyncSession, payload: GeneratorScorerFault, *, now: datetime
) -> str:
    """A scorer-side fault on a dueling lease: release it (sandbox), or fail
    it without re-leasing on an unsupported request (including a source or
    tree digest mismatch). Never blames a generator."""
    detail = f"infra: {payload.error_kind}: {payload.detail}"[:2000]
    slice_row = await session.scalar(
        select(DuelingSlicePreparation)
        .where(DuelingSlicePreparation.lease_id == payload.lease_id)
        .with_for_update()
    )
    if slice_row is not None:
        if payload.error_kind == "unsupported_request" or (
            slice_attempts_exhausted(slice_row)
        ):
            _fail_slice(slice_row, detail, now=now)
            await session.flush()
            return "voided"
        slice_row.status = "pending"
        slice_row.error = detail
        slice_row.lease_id = None
        slice_row.lease_expires_at = None
        await session.flush()
        return "released"
    rows = list(
        await session.scalars(
            select(DuelingSeasonBattery)
            .where(DuelingSeasonBattery.lease_id == payload.lease_id)
            .with_for_update()
        )
    )
    if not rows:
        raise DuelingResultRejected("dueling lease is not current")
    if payload.error_kind == "unsupported_request":
        # The scorer refused the request itself (a version, column or digest
        # it cannot build): re-leasing would only repeat it.
        for row in rows:
            _finish_battery(row, ok=False, error=detail, now=now)
        await session.flush()
        return "voided"
    for row in rows:
        row.status = "pending"
        row.error = detail
        row.lease_id = None
        row.lease_expires_at = None
    await session.flush()
    return "released"


# --- lease and ingestion ----------------------------------------------------

SLICE_RETRY_AFTER_SECONDS = 15
MAX_PENDING_SLICES = 64
"""Bounded admission: preparations the scorer has not finished, across
seasons. A slice is seconds of generator CPU, so this caps a stuck scorer's
backlog, not spend."""


@dataclass(frozen=True)
class DuelingLeaseSlice:
    """The generator slice a bench v16+ lease serves."""

    season_id: UUID
    slice_bytes: bytes
    slice_sha256: str

    def __repr__(self) -> str:  # never log private slice bytes
        return f"DuelingLeaseSlice(season_id={self.season_id}, sha={self.slice_sha256})"


class DuelingSliceUnavailable(Exception):
    """No ready slice yet; the lease answers 503 with ``Retry-After``."""

    def __init__(self, detail: str, *, retry_after: int = SLICE_RETRY_AFTER_SECONDS):
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after


async def resolve_dueling_slice(
    sessions: async_sessionmaker[AsyncSession],
    *,
    bench_version: int,
    run_seed: int,
    pinned_season_id: UUID | None,
    pinned_slice_sha256: str | None,
    now: datetime,
) -> DuelingLeaseSlice:
    """The ready slice for ``(season, run_seed)``, or reserve one and refuse.

    Runs in its own short transaction (the private-dataset preparation
    pattern): a refusal rolls back the caller's lease, never the reservation.
    A ticket that already holds a slice for this seed keeps it, even after its
    season closed, so quorum re-leases and retries run the same bytes;
    otherwise the open season of ``bench_version`` serves. Concurrent leases
    of one seed share one preparation row; existing pinned bytes win.
    """
    async with sessions() as session, session.begin():
        if pinned_season_id is not None and pinned_slice_sha256 is not None:
            # The ticket already ran this seed in a season: it keeps those
            # bytes or is refused, never served another season's slice. A
            # new seed on a reused ticket row (a later retest) takes the
            # open season below.
            pinned = await session.scalar(
                select(DuelingSlicePreparation).where(
                    DuelingSlicePreparation.season_id == pinned_season_id,
                    DuelingSlicePreparation.run_seed == run_seed,
                )
            )
            # The pin names this seed only when the preparation for it is the
            # one the ticket was served (its digest); any other row is a new
            # seed's preparation and resolves through the open season below.
            if pinned is not None and pinned.slice_sha256 == pinned_slice_sha256:
                if pinned.status == "ready" and pinned.slice_bytes is not None:
                    return DuelingLeaseSlice(
                        pinned_season_id, pinned.slice_bytes, pinned_slice_sha256
                    )
                raise DuelingSliceUnavailable(
                    "the ticket's pinned dueling slice is no longer servable; "
                    "operator review required (list_dueling_slices)",
                    retry_after=300,
                )
        season = await open_season(session, bench_version)
        if season is None or season.season_seed is None:
            raise DuelingSliceUnavailable(
                f"no dueling season is open for bench v{bench_version}; one opens "
                "at the next generator-round boundary",
                retry_after=60,
            )
        row = await session.scalar(
            select(DuelingSlicePreparation).where(
                DuelingSlicePreparation.season_id == season.id,
                DuelingSlicePreparation.run_seed == run_seed,
            )
        )
        if row is not None and row.status == "ready":
            if row.slice_bytes is None:
                raise DuelingSliceUnavailable(
                    "dueling slice bytes were pruned; operator review required "
                    "(list_dueling_slices)",
                    retry_after=300,
                )
            assert row.slice_sha256 is not None
            return DuelingLeaseSlice(season.id, row.slice_bytes, row.slice_sha256)
        if row is None:
            pending = await session.scalar(
                select(func.count())
                .select_from(DuelingSlicePreparation)
                .where(DuelingSlicePreparation.status.in_(("pending", "leased")))
            )
            if (pending or 0) >= MAX_PENDING_SLICES:
                raise DuelingSliceUnavailable(
                    "dueling slice preparation backlog is full", retry_after=60
                )
            await session.execute(
                pg_insert(DuelingSlicePreparation)
                .values(
                    id=uuid4(),
                    season_id=season.id,
                    run_seed=run_seed,
                    status="pending",
                    attempts=0,
                    requested_by="lease",
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    constraint="dueling_slice_preparations_seed_key"
                )
            )
            status = "pending"
        else:
            status = row.status
    if status == "failed":
        raise DuelingSliceUnavailable(
            "dueling slice preparation failed; operator review required "
            "(list_dueling_slices)",
            retry_after=300,
        )
    raise DuelingSliceUnavailable("dueling slice preparation pending")


async def agent_owner_key(session: AsyncSession, agent: Any) -> str:
    """The scored agent's emission owner (coldkey, else hotkey): the key the
    scorer self-masks and generator columns are owned under."""
    from ditto.db.models import EvaluationPayment
    from ditto.db.queries.scores import emission_owner

    coldkey = await session.scalar(
        select(EvaluationPayment.miner_coldkey).where(
            EvaluationPayment.agent_id == agent.agent_id
        )
    )
    return emission_owner(miner_hotkey=agent.miner_hotkey, miner_coldkey=coldkey)


class DuelingEvidenceRejected(ValueError):
    """A bench v16+ report's ``details.dueling`` does not match its lease."""


def dueling_score_evidence(
    details: dict[str, Any] | None,
    *,
    bench_version: int,
    season_id: UUID | None,
    slice_sha256: str | None,
    owner_key: str | None,
) -> tuple[UUID | None, dict[str, Any] | None]:
    """The season and aggregates to store with a score, or a refusal.

    Below the dueling floor nothing is stored. At v16+ the signed report must
    carry ``details.dueling`` naming the lease's season and pinned slice
    digest; anything else is refused rather than stored, so a v16 row without
    a season or aggregates never reaches the ledger.
    """
    if not is_dueling_bench_version(bench_version):
        return None, None
    raw = (details or {}).get("dueling")
    if raw is None:
        raise DuelingEvidenceRejected("bench v16+ report carries no details.dueling")
    try:
        report = DuelingReport.model_validate(raw)
    except ValidationError as exc:
        raise DuelingEvidenceRejected(
            f"details.dueling is malformed: {exc.error_count()} error(s)"
        ) from exc
    if season_id is None or report.season_id != str(season_id):
        raise DuelingEvidenceRejected(
            "details.dueling names another season than the lease"
        )
    if slice_sha256 is None or report.slice_sha256 != slice_sha256:
        raise DuelingEvidenceRejected(
            "details.dueling slice digest does not match the leased slice"
        )
    if owner_key is None or report.owner_key != owner_key:
        raise DuelingEvidenceRejected(
            "details.dueling owner_key is not the scored agent's owner"
        )
    # Bench v19+ weights the columns at 0.15 and carries the datagen
    # qualifier; a block weighted for another version is not this ticket's.
    try:
        report.check_bench_version(bench_version)
    except ValueError as exc:
        raise DuelingEvidenceRejected(str(exc)) from exc
    return season_id, report.model_dump(mode="json")
