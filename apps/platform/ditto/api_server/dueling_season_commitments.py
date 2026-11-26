"""Publish dueling season commitments on chain through elected validators.

A bench v16+ dueling season commits its seed before its anchor block exists
(``season_commitment(s, secret)``, ``research/dittobench-datagen/docs/
dueling-bench.md`` section 2, "No grinding"). When the generator competition's
``onchain_commitment`` is on, the commitment is also put on chain as
``dds1:{season_index}:{commitment}`` so anyone can check, block-stamped, that
the season seed was fixed before the anchor. Platform never publishes it
itself: like generator-round ``dgr1`` commitments
(:mod:`ditto.api_server.generator_rounds`), it elects up to
``ROUND_COMMITMENT_PUBLISHERS`` validators advertising the
``season_commitments`` capability, in a stable per-season order, and each
publishes with ``Commitments.set_commitment`` from its own Pylon identity,
reads it back and reports the block.

Only ``committed`` seasons with publication ``pending`` are offered: a value
published after the season opened (its anchor exists) proves nothing. Shadow
seasons publish too; the row says shadow. Publication is best effort and never
gates a season opening.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.generator_round_validator import (
    DuelingSeasonCommitmentJobResponse,
    DuelingSeasonCommitmentReportRequest,
    DuelingSeasonCommitmentReportResponse,
    season_commitment_payload,
)
from ditto.api_models.validator_capabilities import ValidatorCapabilities
from ditto.api_server.generator_rounds import (
    COMMITMENT_CLAIM_TTL,
    COMMITMENT_ESCALATION_INTERVAL,
    COMMITMENT_HEARTBEAT_FRESHNESS,
    ROUND_COMMITMENT_PUBLISHERS,
    CommitmentElection,
)
from ditto.db.models import (
    DuelingSeason,
    DuelingSeasonCommitmentPublication,
    ValidatorHeartbeat,
)

# Its own domain, so the validators publishing a season's commitment are not
# by construction the ones publishing the round it opens on (both are
# committed while the previous round runs, and one hotkey holds one
# commitment at a time).
SEASON_PUBLISHER_ORDER_DOMAIN = b"dittobench-dueling-season-publisher/v1"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def season_publisher_rank_key(season_index: int, validator_hotkey: str) -> str:
    """A validator's place in a season's publisher order (lower goes first)."""
    return hashlib.sha256(
        SEASON_PUBLISHER_ORDER_DOMAIN
        + b"\x00"
        + struct.pack(">Q", season_index)
        + validator_hotkey.encode()
    ).hexdigest()


def season_publisher_order(season_index: int, hotkeys: Iterable[str]) -> list[str]:
    return sorted(
        set(hotkeys), key=lambda h: (season_publisher_rank_key(season_index, h), h)
    )


def season_commitment_capable(heartbeat: ValidatorHeartbeat, *, now: datetime) -> bool:
    """A fresh heartbeat advertising the ``season_commitments`` capability."""
    if now - _aware(heartbeat.seen_at) > COMMITMENT_HEARTBEAT_FRESHNESS:
        return False
    try:
        capabilities = ValidatorCapabilities.model_validate(heartbeat.capabilities)
    except ValueError:
        return False
    return capabilities.season_commitments


async def season_commitment_capable_hotkeys(
    session: AsyncSession, *, now: datetime
) -> list[str]:
    heartbeats = await session.scalars(
        select(ValidatorHeartbeat).where(
            ValidatorHeartbeat.seen_at >= now - COMMITMENT_HEARTBEAT_FRESHNESS
        )
    )
    return [
        heartbeat.validator_hotkey
        for heartbeat in heartbeats
        if season_commitment_capable(heartbeat, now=now)
    ]


def elect_season_publishers(
    season: DuelingSeason,
    publications: Iterable[DuelingSeasonCommitmentPublication],
    capable: Iterable[str],
    *,
    now: datetime,
    target: int = ROUND_COMMITMENT_PUBLISHERS,
) -> CommitmentElection:
    """Elect the validators that should publish ``season``'s commitment now.

    The round election's rules (``generator_rounds.elect_commitment_publishers``)
    in the season's own publisher order: confirmed publications count toward
    ``target``, failed or expired claims are skipped, a live claim keeps its
    place, and each ``COMMITMENT_ESCALATION_INTERVAL`` since the first claim
    without reaching ``target`` widens the election by one validator.
    """
    publications = list(publications)
    confirmed = tuple(
        sorted(p.validator_hotkey for p in publications if p.status == "confirmed")
    )
    needed = max(0, target - len(confirmed))
    if needed == 0:
        return CommitmentElection(target=target, confirmed=confirmed, elected=())
    excluded = set(confirmed) | {
        p.validator_hotkey
        for p in publications
        if p.status == "failed"
        or (p.status == "claimed" and _aware(p.claim_expires_at) <= now)
    }
    claims = [_aware(p.claimed_at) for p in publications]
    widen = int((now - min(claims)) / COMMITMENT_ESCALATION_INTERVAL) if claims else 0
    candidates = [
        hotkey
        for hotkey in season_publisher_order(season.season_index, capable)
        if hotkey not in excluded
    ]
    return CommitmentElection(
        target=target,
        confirmed=confirmed,
        elected=tuple(candidates[: needed + widen]),
    )


async def season_publications(
    session: AsyncSession, season_id: object, *, for_update: bool = False
) -> list[DuelingSeasonCommitmentPublication]:
    stmt = (
        select(DuelingSeasonCommitmentPublication)
        .where(DuelingSeasonCommitmentPublication.season_id == season_id)
        .order_by(DuelingSeasonCommitmentPublication.claimed_at)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(await session.scalars(stmt))


async def season_commitment_confirmations(
    session: AsyncSession, season_id: object
) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                DuelingSeasonCommitmentPublication.season_id == season_id,
                DuelingSeasonCommitmentPublication.status == "confirmed",
            )
        )
        or 0
    )


async def claim_season_commitment_publication(
    session: AsyncSession, *, validator_hotkey: str, now: datetime
) -> DuelingSeasonCommitmentJobResponse | None:
    """Hand ``validator_hotkey`` a season commitment to publish if elected.

    Only ``committed`` seasons with publication ``pending`` are offered. The
    seasons are locked so concurrent polls see each other's claims (caller
    commits).
    """
    seasons = list(
        await session.scalars(
            select(DuelingSeason)
            .where(
                DuelingSeason.status == "committed",
                DuelingSeason.commitment_publication == "pending",
            )
            .order_by(DuelingSeason.season_index, DuelingSeason.bench_version)
            .with_for_update()
        )
    )
    if not seasons:
        return None
    capable = await season_commitment_capable_hotkeys(session, now=now)
    if validator_hotkey not in capable:
        return None
    for season in seasons:
        publications = await season_publications(session, season.id, for_update=True)
        if any(p.validator_hotkey == validator_hotkey for p in publications):
            # Already answered, or a live claim this validator is working on.
            continue
        election = elect_season_publishers(season, publications, capable, now=now)
        if validator_hotkey not in election.elected:
            continue
        claim = DuelingSeasonCommitmentPublication(
            season_id=season.id,
            validator_hotkey=validator_hotkey,
            commitment=season.commitment,
            status="claimed",
            claimed_at=now,
            claim_expires_at=now + COMMITMENT_CLAIM_TTL,
        )
        session.add(claim)
        await session.flush()
        return DuelingSeasonCommitmentJobResponse(
            season_id=season.id,
            season_index=season.season_index,
            bench_version=season.bench_version,
            shadow=season.shadow,
            commitment=season.commitment,
            payload=season_commitment_payload(season.season_index, season.commitment),
            claim_expires_at=claim.claim_expires_at,
        )
    return None


class SeasonCommitmentReportRejected(ValueError):
    """A season commitment report names no claim or another commitment."""


def _record_first_confirmation(
    season: DuelingSeason, publication: DuelingSeasonCommitmentPublication
) -> None:
    """Keep the earliest confirmed block (and its extrinsic) on the season."""
    block = publication.block_number
    if block is None:
        return
    if season.commitment_block is None or block < season.commitment_block:
        season.commitment_block = block
        season.commitment_extrinsic_hash = publication.extrinsic_hash


async def record_season_commitment_report(
    session: AsyncSession,
    report: DuelingSeasonCommitmentReportRequest,
    *,
    now: datetime,
) -> DuelingSeasonCommitmentReportResponse:
    """Record one elected validator's publication outcome (caller commits).

    Idempotent: a repeated report, or a ``failed`` report after a
    ``confirmed`` one, changes nothing. A late ``confirmed`` report is still
    recorded (the value is on chain at its block); it moves the season to
    ``published`` only while the season still collects publications.
    """
    season = await session.get(DuelingSeason, report.season_id, with_for_update=True)
    if season is None:
        raise SeasonCommitmentReportRejected("unknown season")
    if (
        report.commitment != season.commitment
        or report.season_index != season.season_index
    ):
        raise SeasonCommitmentReportRejected("report names another commitment")
    publication = await session.get(
        DuelingSeasonCommitmentPublication,
        (report.season_id, report.validator_hotkey),
        with_for_update=True,
    )
    if publication is None:
        raise SeasonCommitmentReportRejected(
            "no publication was claimed by this validator"
        )
    duplicate = publication.status == "confirmed" or (
        publication.status == "failed" and report.outcome == "failed"
    )
    if not duplicate:
        publication.status = report.outcome
        publication.reported_at = now
        publication.block_number = report.block_number
        publication.extrinsic_hash = report.extrinsic_hash
        publication.detail = report.detail
        await session.flush()
    confirmations = await season_commitment_confirmations(session, season.id)
    if not duplicate and report.outcome == "confirmed":
        season.commitment_published_at = season.commitment_published_at or now
        _record_first_confirmation(season, publication)
        if (
            season.commitment_publication == "pending"
            and confirmations >= ROUND_COMMITMENT_PUBLISHERS
        ):
            season.commitment_publication = "published"
            season.commitment_error = None
    return DuelingSeasonCommitmentReportResponse(
        season_id=season.id,
        accepted=not duplicate,
        duplicate=duplicate,
        confirmations=confirmations,
        target=ROUND_COMMITMENT_PUBLISHERS,
    )


async def settle_season_commitment_publications(session: AsyncSession) -> list[str]:
    """Close publication for seasons that left ``committed`` while pending.

    One confirmed validator publication is enough for ``published``. With
    none, a season that opened is ``failed`` (the on-chain copy never landed
    before its anchor) and a void season ``disabled`` (nobody needs it).
    """
    seasons = list(
        await session.scalars(
            select(DuelingSeason)
            .where(
                DuelingSeason.commitment_publication == "pending",
                DuelingSeason.status != "committed",
            )
            .order_by(DuelingSeason.season_index, DuelingSeason.bench_version)
            .with_for_update()
        )
    )
    notes: list[str] = []
    for season in seasons:
        confirmations = await season_commitment_confirmations(session, season.id)
        if confirmations > 0:
            season.commitment_publication = "published"
            season.commitment_error = None
        elif season.status == "void":
            season.commitment_publication = "disabled"
        else:
            season.commitment_publication = "failed"
            season.commitment_error = (
                "no validator confirmed the on-chain commitment before the "
                "season opened"
            )
        notes.append(
            f"season {season.season_index} v{season.bench_version}: commitment "
            f"publication {season.commitment_publication} "
            f"({confirmations} confirmed)"
        )
    return notes


async def elected_season_publishers(
    session: AsyncSession,
    season: DuelingSeason,
    publications: Iterable[DuelingSeasonCommitmentPublication],
    *,
    now: datetime,
) -> list[str]:
    """Who is elected to publish ``season``'s commitment right now."""
    if season.status != "committed" or season.commitment_publication != "pending":
        return []
    capable = await season_commitment_capable_hotkeys(session, now=now)
    return list(elect_season_publishers(season, publications, capable, now=now).elected)
