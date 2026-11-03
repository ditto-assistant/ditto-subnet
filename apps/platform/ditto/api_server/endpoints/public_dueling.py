"""Public reads of revealed dueling seasons and released generator sources.

Delayed reproducibility (``dueling-bench.md`` section 2): once a season is
revealed, anyone can rebuild any of its slices byte for byte with
``challenge-scorer reproduce-slice``:

- ``GET /public/dueling/seasons``: revealed seasons, newest first, each with
  its secret, anchor block hash, season seed, frozen pins and where each pinned
  generator's source stands.
- ``GET /public/dueling/seasons/{season_id}``: one revealed season.
- ``GET /public/dueling/seasons/{season_id}/slices``: its slices, each with
  ``run_seed``, ``slice_sha256`` and the exact ``-pins`` JSON (substitution
  and slice seeds included).
- ``GET /public/dueling/generator-sources/{submission_id}``: release status.
- ``GET /public/dueling/generator-sources/{submission_id}/archive``: the exact
  pinned archive once released (the public default generator always).

Nothing here serves an unrevealed season: 404 until reveal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from ditto.api_models.dueling import (
    PublicDuelingPins,
    PublicDuelingSeason,
    PublicDuelingSeasonList,
    PublicDuelingSlice,
    PublicDuelingSlicePage,
    PublicGeneratorSource,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.generator_source_releases import season_pins, slice_pins
from ditto.db.models import (
    DuelingSeason,
    DuelingSlicePreparation,
    GeneratorSourceRelease,
    GeneratorSubmission,
)
from ditto.db.queries.artifact_release_settings import artifact_release_policy

router = APIRouter(prefix="/public/dueling", tags=["public"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_CACHE = "public, max-age=60"
_MAX_SEASONS = 50
_MAX_SLICES = 500
_MAX_RUN_SEED = (1 << 63) - 1


def _archive_path(submission_id: UUID) -> str:
    return f"/api/v1/public/dueling/generator-sources/{submission_id}/archive"


def _source_view(
    sub: GeneratorSubmission,
    release: GeneratorSourceRelease | None,
    *,
    publishes: bool,
) -> PublicGeneratorSource:
    released = release is not None and release.released_at is not None
    return PublicGeneratorSource(
        submission_id=sub.submission_id,
        miner_hotkey=sub.miner_hotkey,
        owner_key="" if sub.public_default else sub.owner_key,
        tree_sha256=sub.tree_sha256,
        source_sha256=sub.source_sha256,
        public_default=bool(sub.public_default),
        release_at=None if release is None else release.release_at,
        released_at=None if release is None else release.released_at,
        available=bool(sub.public_default) or (released and publishes),
        archive_path=_archive_path(sub.submission_id),
        scorer_version=None if release is None else release.scorer_version,
    )


async def _season_view(
    session: AsyncSession, season: DuelingSeason, *, publishes: bool
) -> PublicDuelingSeason:
    assert season.season_seed is not None
    fallback = dict((season.detail or {}).get("fallback") or {})
    ids = {UUID(str(c["submission_id"])) for c in season.columns or ()}
    if fallback.get("submission_id"):
        ids.add(UUID(str(fallback["submission_id"])))
    subs = list(
        await session.scalars(
            select(GeneratorSubmission)
            .options(defer(GeneratorSubmission.source_bytes))
            .where(GeneratorSubmission.submission_id.in_(ids))
            .order_by(GeneratorSubmission.submission_id)
        )
    )
    releases = {
        row.submission_id: row
        for row in await session.scalars(
            select(GeneratorSourceRelease).where(
                GeneratorSourceRelease.submission_id.in_(ids)
            )
        )
    }
    slices = await session.scalar(
        select(func.count())
        .select_from(DuelingSlicePreparation)
        .where(
            DuelingSlicePreparation.season_id == season.id,
            DuelingSlicePreparation.slice_sha256.is_not(None),
        )
    )
    return PublicDuelingSeason(
        id=season.id,
        bench_version=season.bench_version,
        season_index=season.season_index,
        shadow=season.shadow,
        commitment=season.commitment,
        secret_hex=season.secret.hex(),
        anchor_block=season.anchor_block,
        anchor_block_hash=season.anchor_block_hash,
        season_seed=season.season_seed,
        opened_at=season.opened_at,
        closed_at=season.closed_at,
        revealed_at=season.revealed_at,
        pins=PublicDuelingPins.model_validate(
            {"season_id": str(season.id), "columns": season_pins(season)}
        ),
        fallback={
            key: fallback.get(key)
            for key in (
                "submission_id",
                "miner_hotkey",
                "tree_sha256",
                "grading_bench_version",
            )
        }
        if fallback
        else None,
        sources=[
            _source_view(sub, releases.get(sub.submission_id), publishes=publishes)
            for sub in subs
        ],
        slices=int(slices or 0),
    )


async def _revealed(session: AsyncSession, season_id: UUID) -> DuelingSeason:
    season = await session.get(DuelingSeason, season_id)
    if season is None or season.status != "revealed" or season.season_seed is None:
        raise HTTPException(status_code=404, detail="no revealed dueling season")
    return season


@router.get("/seasons", response_model=PublicDuelingSeasonList)
async def public_dueling_seasons(
    response: Response,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_SEASONS)] = 10,
    before_index: Annotated[int | None, Query(ge=0)] = None,
    before_bench_version: Annotated[int | None, Query(ge=16)] = None,
) -> PublicDuelingSeasonList:
    """Revealed seasons, newest first.

    Page backwards with the last row's ``season_index`` as ``before_index``
    and its ``bench_version`` as ``before_bench_version`` (a shadow and an
    authoritative season can share an index); ``before_index`` alone skips
    the whole index."""
    response.headers["Cache-Control"] = _CACHE
    stmt = select(DuelingSeason).where(DuelingSeason.status == "revealed")
    if before_index is not None:
        stmt = stmt.where(
            tuple_(DuelingSeason.season_index, DuelingSeason.bench_version)
            < tuple_(before_index, before_bench_version)
            if before_bench_version is not None
            else DuelingSeason.season_index < before_index
        )
    seasons = list(
        await session.scalars(
            stmt.order_by(
                DuelingSeason.season_index.desc(), DuelingSeason.bench_version.desc()
            ).limit(limit)
        )
    )
    publishes = (await artifact_release_policy(session)).releases_publicly
    return PublicDuelingSeasonList(
        seasons=[
            await _season_view(session, season, publishes=publishes)
            for season in seasons
        ]
    )


@router.get("/seasons/{season_id}", response_model=PublicDuelingSeason)
async def public_dueling_season(
    season_id: UUID, response: Response, session: SessionDep
) -> PublicDuelingSeason:
    response.headers["Cache-Control"] = _CACHE
    season = await _revealed(session, season_id)
    publishes = (await artifact_release_policy(session)).releases_publicly
    return await _season_view(session, season, publishes=publishes)


@router.get("/seasons/{season_id}/slices", response_model=PublicDuelingSlicePage)
async def public_dueling_slices(
    season_id: UUID,
    response: Response,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=_MAX_SLICES)] = 100,
    run_seed: Annotated[
        str | None,
        Query(
            pattern=r"^(0|[1-9][0-9]{0,18})$",
            description="Decimal run seed (an int64; sent as text so it is exact)",
        ),
    ] = None,
) -> PublicDuelingSlicePage:
    """Every slice of a revealed season that has a digest, by run seed."""
    response.headers["Cache-Control"] = _CACHE
    season = await _revealed(session, season_id)
    where = [
        DuelingSlicePreparation.season_id == season.id,
        DuelingSlicePreparation.slice_sha256.is_not(None),
    ]
    if run_seed is not None:
        if int(run_seed) > _MAX_RUN_SEED:
            raise HTTPException(status_code=422, detail="run_seed exceeds int64")
        where.append(DuelingSlicePreparation.run_seed == int(run_seed))
    total = await session.scalar(
        select(func.count()).select_from(DuelingSlicePreparation).where(*where)
    )
    rows = list(
        await session.scalars(
            select(DuelingSlicePreparation)
            .options(defer(DuelingSlicePreparation.slice_bytes))
            .where(*where)
            .order_by(DuelingSlicePreparation.run_seed)
            .offset(offset)
            .limit(limit)
        )
    )
    return PublicDuelingSlicePage(
        season_id=season.id,
        total=int(total or 0),
        offset=offset,
        limit=limit,
        slices=[
            PublicDuelingSlice(
                run_seed=str(int(row.run_seed)),
                slice_sha256=str(row.slice_sha256),
                ready_at=row.ready_at,
                pins=PublicDuelingPins.model_validate(slice_pins(season, row)),
            )
            for row in rows
        ],
    )


async def _source(
    session: AsyncSession, submission_id: UUID, *, with_bytes: bool
) -> tuple[GeneratorSubmission, GeneratorSourceRelease | None]:
    stmt = select(GeneratorSubmission).where(
        GeneratorSubmission.submission_id == submission_id
    )
    if not with_bytes:
        stmt = stmt.options(defer(GeneratorSubmission.source_bytes))
    sub = await session.scalar(stmt)
    release = await session.get(GeneratorSourceRelease, submission_id)
    if sub is None or (
        not sub.public_default and (release is None or release.last_revealed_at is None)
    ):
        # Only a generator pinned by a revealed season (or the public default)
        # has a public source status; an unrevealed pin stays private.
        raise HTTPException(status_code=404, detail="no dueling source release")
    return sub, release


@router.get("/generator-sources/{submission_id}", response_model=PublicGeneratorSource)
async def public_generator_source(
    submission_id: UUID, response: Response, session: SessionDep
) -> PublicGeneratorSource:
    response.headers["Cache-Control"] = _CACHE
    sub, release = await _source(session, submission_id, with_bytes=False)
    publishes = (await artifact_release_policy(session)).releases_publicly
    return _source_view(sub, release, publishes=publishes)


@router.get(
    "/generator-sources/{submission_id}/archive",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/gzip": {"schema": {"type": "string", "format": "binary"}}
            },
            "description": "The exact pinned archive (``source_sha256``).",
        }
    },
)
async def public_generator_source_archive(
    submission_id: UUID, session: SessionDep
) -> Response:
    """The exact pinned archive (``source_sha256``) once it is released.

    403 while the live source release policy withholds source, 425 before
    ``release_at``. The policy is re-read here, never trusted from the
    release row.
    """
    sub, release = await _source(session, submission_id, with_bytes=True)
    if not sub.public_default:
        if not (await artifact_release_policy(session)).releases_publicly:
            raise HTTPException(
                status_code=403,
                detail="the source release policy withholds generator source",
            )
        assert release is not None
        if release.released_at is None:
            detail = "generator source is not released yet"
            if release.release_at is not None:
                detail = f"generator source is embargoed until {release.release_at}"
                if release.release_at <= datetime.now(UTC):
                    detail = "generator source release is due on the next tick"
            raise HTTPException(status_code=425, detail=detail)
    return Response(
        content=sub.source_bytes,
        media_type="application/gzip",
        headers={
            "Cache-Control": "public, max-age=3600, immutable",
            "Content-Disposition": (
                f'attachment; filename="ditto-generator-{sub.submission_id}.tar.gz"'
            ),
            "X-Source-SHA256": sub.source_sha256,
            "X-Tree-SHA256": sub.tree_sha256,
        },
    )
