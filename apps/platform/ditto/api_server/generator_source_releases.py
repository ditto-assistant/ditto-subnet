"""Publish every pinned dueling generator's exact source after its embargo.

``dueling-bench.md`` section 2 ("Delayed reproducibility"): a v16 slice is
reproducible once the season is revealed and every generator that held one of
its columns has its exact pinned archive published. Publishing is part of the
contract (season open refuses ``disclosure = never``). For one pinned
submission:

``release_at = max(revealed_at of the last season that pinned it,
closed_at of the first season that pinned it + embargo_hours)``

so a generator that holds a column for a long time still becomes auditable
once the embargo runs out after its first season. :func:`sync_generator_source_releases`
recomputes every row on each season tick and marks a row released the first
tick at or after ``release_at`` while the source release policy publishes.
The public default generator is already public and never gets a row; it is
always downloadable for reproducing substituted columns.

Also here: the reproduce pins a revealed slice needs (the
``challenge-scorer reproduce-slice -pins`` JSON shape).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import defer

from ditto.db.models import (
    DuelingSeason,
    DuelingSeasonBattery,
    GeneratorSourceRelease,
    GeneratorSubmission,
)
from ditto.db.queries.artifact_release_settings import artifact_release_policy
from ditto_screening_protocol.dueling import slice_seed

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.db.models import DuelingSlicePreparation

logger = logging.getLogger(__name__)

PINNING_STATUSES = ("open", "closed", "revealed")
"""Season statuses whose frozen columns were served to agent runs."""


def release_time(
    *,
    first_closed_at: datetime | None,
    last_revealed_at: datetime | None,
    embargo_hours: int,
) -> datetime | None:
    """``max(last reveal, first close + embargo)``, or None while either is
    unknown."""
    if first_closed_at is None or last_revealed_at is None:
        return None
    return max(last_revealed_at, first_closed_at + timedelta(hours=embargo_hours))


async def sync_generator_source_releases(
    session: AsyncSession, *, now: datetime
) -> list[str]:
    """Upsert one row per pinned miner generator and release the due ones.

    Idempotent; the caller commits. A released row keeps ``released_at``
    whatever later changes (policy, new pins): publication is a fact.
    """
    policy = await artifact_release_policy(session)
    seasons = list(
        await session.scalars(
            select(DuelingSeason)
            .options(defer(DuelingSeason.secret))
            .where(
                DuelingSeason.status.in_(PINNING_STATUSES),
                DuelingSeason.columns.is_not(None),
            )
            .order_by(DuelingSeason.season_index, DuelingSeason.created_at)
        )
    )
    pinned: dict[UUID, list[DuelingSeason]] = {}
    for season in seasons:
        ids = {
            UUID(str(column["submission_id"]))
            for column in season.columns or ()
            if not column.get("public_default")
        }
        for submission_id in ids:
            pinned.setdefault(submission_id, []).append(season)
    if not pinned:
        return []
    existing = {
        row.submission_id: row
        for row in await session.scalars(
            select(GeneratorSourceRelease).where(
                GeneratorSourceRelease.submission_id.in_(pinned)
            )
        )
    }
    subs = {
        row.submission_id: row
        for row in await session.scalars(
            select(GeneratorSubmission)
            .options(defer(GeneratorSubmission.source_bytes))
            .where(GeneratorSubmission.submission_id.in_(pinned))
        )
    }
    notes: list[str] = []
    for submission_id, history in sorted(pinned.items(), key=lambda kv: str(kv[0])):
        sub = subs.get(submission_id)
        if sub is None:
            continue
        first, last = history[0], history[-1]
        release_at = release_time(
            first_closed_at=first.closed_at,
            last_revealed_at=last.revealed_at,
            embargo_hours=policy.embargo_hours,
        )
        row = existing.get(submission_id)
        if row is None:
            row = GeneratorSourceRelease(submission_id=submission_id)
            session.add(row)
        row.miner_hotkey = sub.miner_hotkey
        row.owner_key = sub.owner_key
        row.tree_sha256 = sub.tree_sha256
        row.source_sha256 = sub.source_sha256
        row.bench_version = first.bench_version
        row.first_season_id = first.id
        row.last_season_id = last.id
        row.seasons = len(history)
        row.first_closed_at = first.closed_at
        row.last_revealed_at = last.revealed_at
        row.embargo_hours = policy.embargo_hours
        row.disclosure = policy.disclosure.value
        row.updated_at = now
        if row.scorer_version is None:
            row.scorer_version = await session.scalar(
                select(DuelingSeasonBattery.scorer_version).where(
                    DuelingSeasonBattery.season_id == first.id,
                    DuelingSeasonBattery.submission_id == submission_id,
                )
            )
        if row.released_at is None:
            row.release_at = release_at
            if (
                release_at is not None
                and release_at <= now
                and policy.releases_publicly
            ):
                row.released_at = now
                notes.append(f"generator {submission_id}: source released")
    await session.flush()
    for note in notes:
        logger.info("generator source releases: %s", note)
    return notes


def season_pins(season: DuelingSeason) -> list[dict[str, Any]]:
    """The season's frozen columns in the reproduce-slice pin shape (no
    substitution)."""
    return [
        {
            "column_id": column["column_id"],
            "submission_id": str(column["submission_id"]),
            "owner_key": str(column.get("owner_key") or ""),
            "miner_hotkey": column["miner_hotkey"],
            "tree_sha256": column["tree_sha256"],
            "grading_bench_version": int(column["grading_bench_version"]),
            "public_default": bool(column.get("public_default")),
            "substituted": False,
        }
        for column in sorted(season.columns or (), key=lambda c: c["column_id"])
    ]


def slice_pins(
    season: DuelingSeason, preparation: DuelingSlicePreparation
) -> dict[str, Any]:
    """``challenge-scorer reproduce-slice -pins`` JSON for one revealed slice.

    A column the pinned generator failed on carries the public default
    generator (the season fallback) on the same slice seed: ``substituted``,
    ``public_default`` and an empty ``owner_key``.
    """
    assert season.season_seed is not None
    fallback = (season.detail or {}).get("fallback") or {}
    substituted = {
        str(column.get("column_id"))
        for column in preparation.columns or ()
        if isinstance(column, dict) and column.get("substituted")
    }
    columns: list[dict[str, Any]] = []
    for pin in season_pins(season):
        pin["slice_seed"] = slice_seed(
            season.season_seed, int(preparation.run_seed), pin["column_id"]
        )
        if pin["column_id"] in substituted:
            pin.update(
                submission_id=str(fallback.get("submission_id", "")),
                owner_key="",
                miner_hotkey=str(fallback.get("miner_hotkey", "")),
                tree_sha256=str(fallback.get("tree_sha256", "")),
                grading_bench_version=int(
                    fallback.get("grading_bench_version")
                    or pin["grading_bench_version"]
                ),
                public_default=True,
                substituted=True,
            )
        columns.append(pin)
    return {"season_id": str(season.id), "columns": columns}
