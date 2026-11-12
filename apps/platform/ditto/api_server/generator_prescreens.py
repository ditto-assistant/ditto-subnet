"""Generator pre-screens: a cheap first measurement for never-scored generators.

A challenger column measures a never-scored generator on a full round:
``cases_per_seed`` cases, the whole panel, one column per round. A pre-screen
measures several at once, cheaply, in the time no round needs the panel: a
batch of up to ``prescreen_columns`` generators from the head of the
challenger queue (one per owner, each never pre-screened), ``prescreen_cases``
cases each on a fresh secret seed, run by the top ``prescreen_panel_size``
runnable ledger owners. Its column scores only order the challenger queue;
a pre-screen is never paid and never on the ledger.

A batch lives in ``generator_prescreens``, apart from chain-epoch rounds, so
no round code path ever reads one. Its index starts at
:data:`PRESCREEN_INDEX_BASE`, far above any round index, so the scorer and
validator wire protocols, which carry a round index, route a pre-screen
without a release of either. Lifecycle: ``generating`` (columns and panel
frozen) -> ``running`` -> ``scoring`` -> ``scored``, or ``void``. A batch
opens only while no round is in flight and no other batch is, and one not
scored within :data:`PRESCREEN_TTL` is void; its generators return to the
queue unscreened.

The lane ships off (``prescreen_enabled``): it spends inference.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, update

from ditto.api_server.generator_competition import load_settings
from ditto.api_server.generator_rounds import (
    IN_FLIGHT_ROUND_STATUSES,
    choose_round_bench_version,
    select_round_columns,
    select_round_panel,
)
from ditto.db.models import GeneratorPrescreen, GeneratorRound, GeneratorSubmission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_server.generator_competition import ResolvedSettings

logger = logging.getLogger(__name__)

# Pre-screen indexes start here (2**40), far above any chain-epoch round
# index; the database pins it (``generator_prescreens_index_check``).
PRESCREEN_INDEX_BASE = 1 << 40
# A batch not scored this long after it opened is void: its generation or
# panel never finished, and its generators return to the queue.
PRESCREEN_TTL = timedelta(hours=3)
IN_FLIGHT_PRESCREEN_STATUSES = ("generating", "running", "scoring")
# Serializes opening, so two ticks never open two batches at once.
_OPEN_LOCK_KEY = 0x4750_5253_4352_4E01  # "GPRSCRN"


def is_prescreen_index(index: int) -> bool:
    """Whether a round index on the wire names a pre-screen batch."""
    return index >= PRESCREEN_INDEX_BASE


def _column(index: int, row: GeneratorSubmission) -> dict[str, Any]:
    return {
        "column_id": f"c{index}",
        "submission_id": str(row.submission_id),
        "miner_hotkey": row.miner_hotkey,
        "owner_key": row.owner_key,
        "source_sha256": row.source_sha256,
        "counted": False,
        "public_default": False,
    }


async def void_prescreen(
    session: AsyncSession, row: GeneratorPrescreen, reason: str, *, now: datetime
) -> None:
    """Void a batch and return its generators to the queue unscreened."""
    row.status = "void"
    row.void_reason = reason[:1000]
    row.closed_at = now
    row.lease_id = None
    row.lease_expires_at = None
    await session.execute(
        update(GeneratorSubmission)
        .where(
            GeneratorSubmission.prescreen_index == row.prescreen_index,
            GeneratorSubmission.prescreen_score.is_(None),
        )
        .values(prescreen_index=None)
    )


async def void_expired_prescreens(session: AsyncSession, *, now: datetime) -> list[str]:
    notes: list[str] = []
    for row in await session.scalars(
        select(GeneratorPrescreen)
        .where(
            GeneratorPrescreen.status.in_(IN_FLIGHT_PRESCREEN_STATUSES),
            GeneratorPrescreen.expires_at <= now,
        )
        .order_by(GeneratorPrescreen.prescreen_index)
        .with_for_update()
    ):
        await void_prescreen(
            session, row, f"expired: not scored within {PRESCREEN_TTL}", now=now
        )
        notes.append(f"prescreen {row.prescreen_index}: void (expired)")
    return notes


async def open_prescreen(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> GeneratorPrescreen | None:
    """Open one batch when the lane is enabled and idle, else None (caller
    commits)."""
    settings = resolved.settings
    if (
        settings.mode == "off"
        or not settings.prescreen_enabled
        or not settings.validator_rounds_enabled
        or not resolved.valid
    ):
        return None
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _OPEN_LOCK_KEY}
    )
    busy = await session.scalar(
        select(func.count())
        .select_from(GeneratorPrescreen)
        .where(GeneratorPrescreen.status.in_(IN_FLIGHT_PRESCREEN_STATUSES))
    )
    rounds = await session.scalar(
        select(func.count())
        .select_from(GeneratorRound)
        .where(GeneratorRound.status.in_(IN_FLIGHT_ROUND_STATUSES))
    )
    if busy or rounds:
        return None
    queue = (await select_round_columns(session, settings)).queue
    owners: set[str] = set()
    picked: list[GeneratorSubmission] = []
    for entry in queue:
        row = entry.row
        if entry.in_flight_round is not None or row.prescreen_index is not None:
            continue
        if row.owner_key in owners:
            continue
        owners.add(row.owner_key)
        picked.append(row)
        if len(picked) >= settings.prescreen_columns:
            break
    if not picked:
        return None
    panel = (
        await select_round_panel(
            session,
            settings.model_copy(update={"panel_size": settings.prescreen_panel_size}),
        )
    ).panel
    if len(panel) < settings.prescreen_panel_size:
        return None
    contract = await choose_round_bench_version(
        session,
        cases_per_column=settings.prescreen_cases,
        now=now,
        min_round_validators=settings.min_round_validators,
    )
    if contract.bench_version is None:
        return None
    latest = await session.scalar(select(func.max(GeneratorPrescreen.prescreen_index)))
    prescreen = GeneratorPrescreen(
        prescreen_index=max(int(latest or 0) + 1, PRESCREEN_INDEX_BASE),
        status="generating",
        seed=secrets.token_hex(32),
        settings_revision=resolved.revision,
        cases_per_column=settings.prescreen_cases,
        bench_version=contract.bench_version,
        columns=[_column(index, row) for index, row in enumerate(picked)],
        panel=panel,
        opened_at=now,
        expires_at=now + PRESCREEN_TTL,
    )
    session.add(prescreen)
    await session.flush()
    for row in picked:
        row.prescreen_index = prescreen.prescreen_index
        row.prescreen_score = None
    return prescreen


async def advance_prescreens(session: AsyncSession, *, now: datetime) -> list[str]:
    """Void expired batches, then open one if the lane is idle (caller
    commits)."""
    notes = await void_expired_prescreens(session, now=now)
    resolved = await load_settings(session)
    opened = await open_prescreen(session, resolved=resolved, now=now)
    if opened is not None:
        notes.append(
            f"prescreen {opened.prescreen_index}: opened with "
            f"{len(opened.columns)} column(s) of {opened.cases_per_column} cases, "
            f"panel {len(opened.panel)}"
        )
    for note in notes:
        logger.info("generator prescreens: %s", note)
    return notes
