"""Dueling-bench seasons: the frozen generator set of bench v16+ agent runs.

From ``DUELING_MIN_BENCH_VERSION`` (16) every agent run is a datagen half plus a
generator slice of ``DUELING_SLICE_COLUMNS`` columns, frozen per season
(``research/dittobench-datagen/docs/dueling-bench.md``). A season is aligned
with generator rounds and spans ``rounds_per_season`` of them. The lifecycle
(:func:`advance_seasons`, run on every generator-round tick, idempotent):

1. **Commit.** While the previous season (or round) runs, Platform draws the
   next season's 32-byte secret and publishes ``commitment =
   season_commitment(s, secret)`` on Platform, where ``s`` is the round index
   the season opens on. The anchor block does not exist yet.
   TODO(dueling): publish ``dds1:{s}:{commitment}`` on chain when
   ``onchain_commitment`` is set. The round machinery elects validators to
   write ``dgr1`` payloads; a ``dds1`` job needs the validator's publisher to
   accept it, which is a validator change outside this layer.
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
4. **Close.** At ``closes_at_round`` (or on an audited rotation), or as soon
   as its bench version is neither active nor rolling out. Disabling dueling
   commits no new season but lets the open one finish.
5. **Reveal.** ``privacy_window_rounds`` subnet epochs after it closes, the
   secret and season seed become readable.

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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import defer

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
from ditto.db.models import (
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
    from ditto.api_server.generator_rounds import ChainPosition

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
    if is_dueling_bench_version(active):
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
    miners = sum(1 for column in columns if not column["public_default"])
    return f"open ({miners} miner column(s))"


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
            reason = (
                f"rotated: {season.rotation_reason}"
                if season.rotation_requested_at is not None
                else "season ended"
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
    if row.attempts >= MAX_SLICE_ATTEMPTS:
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
            slice_row.attempts >= MAX_SLICE_ATTEMPTS
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
    return season_id, report.model_dump(mode="json")
