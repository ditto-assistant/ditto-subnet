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
opens only while no round and no other batch is in flight, and only when the
next round opens later than :data:`PRESCREEN_TTL` from now; one not scored
within that TTL is void and its generators return to the queue unscreened.
So a batch always closes before the next round opens: it never holds the
panel or the scorer a round needs, and it never races a round's opening.

The lane ships off (``prescreen_enabled``): it spends inference.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update

from ditto.api_models.generator import PanelCaseResult
from ditto.api_models.generator_round_validator import accepted_grading_versions
from ditto.api_models.generator_rounds import (
    GeneratorRoundWork,
    RoundCaseRef,
    RoundMatrix,
    RoundMatrixAgent,
    RoundMatrixColumn,
    RoundSourceColumn,
)
from ditto.api_server.generator_competition import (
    load_settings,
    scorer_request_error,
    settings_at,
)
from ditto.api_server.generator_rounds import (
    CHAIN_BLOCK_SECONDS,
    IN_FLIGHT_ROUND_STATUSES,
    MAX_GENERATION_ATTEMPTS,
    ROUND_LEASE_TTL,
    RoundResultRejected,
    choose_round_bench_version,
    round_case_id,
    round_column_seed,
    select_round_columns,
    select_round_panel,
)
from ditto.api_server.generator_scorer_capability import (
    ScorerCapability,
    version_refusal,
)
from ditto.api_server.generator_scorer_capability import (
    refusal as capability_refusal,
)
from ditto.db.models import (
    GeneratorPrescreen,
    GeneratorPrescreenResult,
    GeneratorRound,
    GeneratorSubmission,
)
from ditto.db.queries.generator import claimed_fingerprints

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ditto.api_models.generator import (
        GeneratorCompetitionSettings,
        GeneratorScorerFault,
    )
    from ditto.api_models.generator_rounds import (
        GeneratorRoundGenerateResult,
        GeneratorRoundScoreResult,
    )
    from ditto.api_server.generator_competition import ResolvedSettings
    from ditto.api_server.generator_rounds import ChainPosition

logger = logging.getLogger(__name__)

# Pre-screen indexes start here (2**40), far above any chain-epoch round
# index; the database pins it (``generator_prescreens_index_check``).
PRESCREEN_INDEX_BASE = 1 << 40
# A batch not scored this long after it opened is void: its generation or
# panel never finished, and its generators return to the queue. A batch opens
# only when the next round opens later than this, so it always closes first.
# A batch takes minutes (a few 16-case columns on three agents).
PRESCREEN_TTL = timedelta(minutes=60)
IN_FLIGHT_PRESCREEN_STATUSES = ("generating", "running", "scoring")
# Serializes opening, so two ticks never open two batches at once.
_OPEN_LOCK_KEY = 0x4750_5253_4352_4E01  # "GPRSCRN"


def is_prescreen_index(index: int) -> bool:
    """Whether a round index on the wire names a pre-screen batch."""
    return index >= PRESCREEN_INDEX_BASE


def seconds_until_epoch(position: ChainPosition, epoch: int) -> float | None:
    """Estimated seconds from ``position`` until ``SubnetEpochIndex`` reaches
    ``epoch``, by ``generator_rounds.round_seconds_left``'s arithmetic: the
    next epoch fires at ``next_epoch_block`` and each further one adds
    ``tempo`` blocks. None without a predicted next epoch block."""
    if position.next_epoch_block is None:
        return None
    epochs_after_next = epoch - position.subnet_epoch_index - 1
    if epochs_after_next < 0:
        return 0.0
    block = position.next_epoch_block + epochs_after_next * position.tempo
    return float((block - position.block) * CHAIN_BLOCK_SECONDS)


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
    session: AsyncSession,
    *,
    resolved: ResolvedSettings,
    now: datetime,
    next_round_in: float | None,
) -> GeneratorPrescreen | None:
    """Open one batch when the lane is enabled and idle, else None (caller
    commits). ``next_round_in`` is the seconds until the next round opens; a
    batch needs more than :data:`PRESCREEN_TTL`, and none opens without it."""
    settings = resolved.settings
    if (
        settings.mode == "off"
        or not settings.prescreen_enabled
        or not settings.validator_rounds_enabled
        or not resolved.valid
        or next_round_in is None
        or next_round_in <= PRESCREEN_TTL.total_seconds()
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


async def advance_prescreens(
    session: AsyncSession, *, now: datetime, next_round_in: float | None = None
) -> list[str]:
    """Void expired batches, then open one if the lane is idle and the next
    round is more than :data:`PRESCREEN_TTL` away (caller commits)."""
    notes = await void_expired_prescreens(session, now=now)
    resolved = await load_settings(session)
    opened = await open_prescreen(
        session, resolved=resolved, now=now, next_round_in=next_round_in
    )
    if opened is not None:
        notes.append(
            f"prescreen {opened.prescreen_index}: opened with "
            f"{len(opened.columns)} column(s) of {opened.cases_per_column} cases, "
            f"panel {len(opened.panel)}"
        )
    for note in notes:
        logger.info("generator prescreens: %s", note)
    return notes


# --- scorer work ------------------------------------------------------------
#
# The scorer leases a batch through the round work protocol unchanged: the
# batch's index rides in ``round_index``, and the round endpoints route an
# index at or above PRESCREEN_INDEX_BASE here. A batch is leased only when no
# round needs the scorer.


@dataclass(frozen=True)
class PrescreenLease:
    row: GeneratorPrescreen
    work: GeneratorRoundWork


def prescreen_score_params(
    settings: GeneratorCompetitionSettings,
) -> dict[str, float | int]:
    """The ``challenge.ScoreParams`` a batch is scored with: the round policy
    at the batch's case count, with panel minimums no larger than its panel."""
    params = dict(settings.score_params())
    params["target_cases"] = settings.prescreen_cases
    min_panel = min(int(params["min_panel"]), settings.prescreen_panel_size)
    params["min_panel"] = min_panel
    params["min_solver_owners"] = min(int(params["min_solver_owners"]), min_panel)
    return params


async def get_prescreen(
    session: AsyncSession, index: int, *, for_update: bool = False
) -> GeneratorPrescreen | None:
    stmt = select(GeneratorPrescreen).where(GeneratorPrescreen.prescreen_index == index)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def _source_columns(
    session: AsyncSession, row: GeneratorPrescreen
) -> list[RoundSourceColumn]:
    out: list[RoundSourceColumn] = []
    for column in row.columns:
        sub = await session.get(GeneratorSubmission, UUID(column["submission_id"]))
        if sub is None or sub.source_sha256 != column["source_sha256"]:
            raise RoundResultRejected(
                f"pre-screen column {column['column_id']} source is no longer available"
            )
        out.append(
            RoundSourceColumn(
                column_id=column["column_id"],
                submission_id=sub.submission_id,
                miner_hotkey=column["miner_hotkey"],
                source_sha256=sub.source_sha256,
                source_base64=base64.b64encode(sub.source_bytes).decode("ascii"),
            )
        )
    return out


async def build_prescreen_matrix(
    session: AsyncSession, row: GeneratorPrescreen
) -> RoundMatrix:
    """The batch's agent x generator matrix, in ScoreRound's input shape.

    Every column counts in its own matrix: a batch has no counted columns to
    rank agents by, and its agent standings are discarded. Only column scores
    are kept.
    """
    cases_by_column: dict[str, list[dict[str, str]]] = row.column_cases or {}
    columns = [
        RoundMatrixColumn(
            column_id=column["column_id"],
            owner_key=column["owner_key"],
            counted=True,
            cases=[RoundCaseRef(**ref) for ref in cases_by_column[column["column_id"]]],
        )
        for column in row.columns
        if column["column_id"] in cases_by_column
    ]
    # Keyed by UUID: a panel slot stores its agent id as text.
    results = {
        result.agent_id: result
        for result in await session.scalars(
            select(GeneratorPrescreenResult).where(
                GeneratorPrescreenResult.prescreen_index == row.prescreen_index
            )
        )
    }
    agents = [
        RoundMatrixAgent(
            agent_id=slot["agent_id"],
            owner_key=slot["owner_key"],
            reference=bool(slot.get("reference", False)),
            champion=bool(slot.get("champion", False)),
            results=[
                PanelCaseResult(**r) for r in results[UUID(slot["agent_id"])].results
            ],
        )
        for slot in row.panel
        if UUID(slot["agent_id"]) in results
    ]
    claimed = await claimed_fingerprints(
        session,
        fingerprints=[ref.fingerprint for column in columns for ref in column.cases],
    )
    return RoundMatrix(columns=columns, agents=agents, claimed_fingerprints=claimed)


async def lease_prescreen_work(
    session: AsyncSession,
    *,
    resolved: ResolvedSettings,
    now: datetime,
    capability: ScorerCapability | None = None,
) -> PrescreenLease | None:
    """Lease the oldest batch needing generation or scoring (caller commits).

    Mirrors ``generator_rounds.lease_round_work``: a batch the polling scorer
    cannot generate (case bounds or grading versions) is void with an
    ``infra:`` reason, and one whose generation lease keeps expiring is void.
    """
    settings = resolved.settings
    if settings.mode == "off" or not settings.prescreen_enabled:
        return None
    row = await session.scalar(
        select(GeneratorPrescreen)
        .where(
            GeneratorPrescreen.status.in_(("generating", "scoring")),
            (GeneratorPrescreen.lease_id.is_(None))
            | (GeneratorPrescreen.lease_expires_at <= now),
        )
        .order_by(GeneratorPrescreen.prescreen_index)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    if row.status == "generating" and row.lease_attempts >= MAX_GENERATION_ATTEMPTS:
        await void_prescreen(
            session, row, "pre-screen generation lease expired repeatedly", now=now
        )
        await session.flush()
        return None
    refused = None
    if row.status == "generating":
        refused = capability_refusal(
            capability, row.cases_per_column, what="the pre-screen's cases_per_column"
        ) or version_refusal(capability, row.bench_version)
    if refused is not None:
        await void_prescreen(session, row, f"infra: {refused}", now=now)
        await session.flush()
        return None
    row.lease_id = uuid4()
    row.lease_expires_at = now + ROUND_LEASE_TTL
    row.lease_attempts += 1
    await session.flush()
    if row.status == "generating":
        work = GeneratorRoundWork(
            kind="generate",
            lease_id=row.lease_id,
            round_index=row.prescreen_index,
            lease_expires_at=row.lease_expires_at,
            seed=row.seed,
            cases_per_column=row.cases_per_column,
            columns=await _source_columns(session, row),
            bench_version=row.bench_version,
        )
    else:
        work = GeneratorRoundWork(
            kind="score",
            lease_id=row.lease_id,
            round_index=row.prescreen_index,
            lease_expires_at=row.lease_expires_at,
            params=prescreen_score_params(
                await settings_at(session, row.settings_revision)
            ),
            matrix=await build_prescreen_matrix(session, row),
        )
    return PrescreenLease(row=row, work=work)


def _check_lease(
    row: GeneratorPrescreen | None, lease_id: UUID, status: str
) -> GeneratorPrescreen:
    if row is None:
        raise RoundResultRejected("pre-screen not found")
    if row.status != status or row.lease_id != lease_id:
        raise RoundResultRejected(f"pre-screen is {row.status}; lease is not current")
    return row


async def _void_for_scorer_fault(
    session: AsyncSession,
    row: GeneratorPrescreen,
    *,
    kind: str,
    detail: str,
    scorer_version: str,
    now: datetime,
) -> None:
    generation = dict(row.generation or {})
    generation["scorer_fault"] = {
        "error_kind": kind,
        "detail": detail[:2000],
        "scorer_version": scorer_version,
        "at": now.isoformat(),
    }
    row.generation = generation
    await void_prescreen(
        session,
        row,
        f"infra: scorer {scorer_version} could not generate this pre-screen "
        f"({kind}): {detail}",
        now=now,
    )


async def record_prescreen_generation(
    session: AsyncSession, payload: GeneratorRoundGenerateResult, *, now: datetime
) -> GeneratorPrescreen:
    """Store a batch's generated dataset (caller commits).

    A column that fails on its pre-screen seed scores zero, and its generator
    is not rejected: a batch asks for ``prescreen_cases``, which rounds never
    ask for, so rejection stays a round's verdict.
    """
    row = _check_lease(
        await get_prescreen(session, payload.round_index, for_update=True),
        payload.lease_id,
        "generating",
    )
    frozen = {column["column_id"]: column for column in row.columns}
    if {c.column_id for c in payload.columns} != set(frozen):
        raise RoundResultRejected(
            "result columns do not match the pre-screen's columns"
        )
    refused = next(
        (
            c.error or (c.seed.error if c.seed else None)
            for c in payload.columns
            if scorer_request_error(c.error)
            or (c.seed is not None and scorer_request_error(c.seed.error))
        ),
        None,
    )
    if refused is not None:
        await _void_for_scorer_fault(
            session,
            row,
            kind="unsupported_request",
            detail=refused,
            scorer_version=payload.scorer_version,
            now=now,
        )
        await session.flush()
        return row
    accepted = accepted_grading_versions(row.bench_version)
    column_cases: dict[str, list[dict[str, str]]] = {}
    summary: list[dict[str, Any]] = []
    failed: list[str] = []
    for result in payload.columns:
        declared = result.seed.grading_bench_version if result.seed else None
        if declared is not None and declared not in accepted:
            raise RoundResultRejected(
                f"column {result.column_id} pack grades at v{declared}, which a "
                f"v{row.bench_version} pre-screen does not accept"
            )
        column = frozen[result.column_id]
        if str(result.submission_id) != column["submission_id"]:
            raise RoundResultRejected(
                f"column {result.column_id} names another submission"
            )
        ok = bool(result.ok and result.seed is not None and result.seed.valid)
        if ok:
            assert result.seed is not None
            if result.seed.seed != round_column_seed(row.seed, result.column_id):
                raise RoundResultRejected(
                    f"column {result.column_id} ran on another seed"
                )
            refs = result.seed.validation.cases or []
            if len(refs) != row.cases_per_column:
                raise RoundResultRejected(
                    f"column {result.column_id} case count mismatch"
                )
            column_cases[result.column_id] = [
                {
                    "case_id": round_case_id(result.column_id, ref.case_id),
                    "fingerprint": ref.fingerprint,
                }
                for ref in refs
            ]
        else:
            failed.append(column["submission_id"])
        summary.append(
            {
                "column_id": result.column_id,
                "ok": ok,
                "error": (result.error or result.build.error or "")[:2000] or None,
                "build_ms": result.build.duration_ms,
                "generate_ms": result.seed.duration_ms if result.seed else None,
            }
        )
    generation = dict(row.generation or {})
    generation.update(
        {
            "scorer_version": payload.scorer_version,
            "sandbox": payload.sandbox,
            "columns": summary,
            "generated_at": now.isoformat(),
        }
    )
    row.generation = generation
    row.lease_id = None
    row.lease_expires_at = None
    for submission_id in failed:
        sub = await session.get(
            GeneratorSubmission, UUID(submission_id), with_for_update=True
        )
        if sub is not None and sub.prescreen_index == row.prescreen_index:
            sub.prescreen_score = 0.0
            sub.updated_at = now
    if (
        not column_cases
        or payload.dataset_base64 is None
        or payload.dataset_sha256 is None
    ):
        await void_prescreen(
            session,
            row,
            "no column produced a valid pack on its pre-screen seed",
            now=now,
        )
        return row
    try:
        dataset = base64.b64decode(payload.dataset_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RoundResultRejected("dataset_base64 is not valid base64") from exc
    if hashlib.sha256(dataset).hexdigest() != payload.dataset_sha256:
        raise RoundResultRejected("dataset bytes do not match dataset_sha256")
    row.dataset_bytes = dataset
    row.dataset_sha256 = payload.dataset_sha256
    row.column_cases = column_cases
    row.status = "running"
    await session.flush()
    return row


async def record_prescreen_fault(
    session: AsyncSession, payload: GeneratorScorerFault, *, now: datetime
) -> GeneratorPrescreen:
    """Apply a scorer fault to a batch's current lease (caller commits): a
    request the scorer cannot serve voids it; a sandbox fault releases it."""
    if payload.round_index is None:
        raise RoundResultRejected("pre-screen fault names no batch")
    row = await get_prescreen(session, payload.round_index, for_update=True)
    if row is None:
        raise RoundResultRejected("pre-screen not found")
    if row.lease_id != payload.lease_id or row.status not in ("generating", "scoring"):
        raise RoundResultRejected(f"pre-screen is {row.status}; lease is not current")
    if payload.error_kind == "unsupported_request":
        await _void_for_scorer_fault(
            session,
            row,
            kind=payload.error_kind,
            detail=payload.detail,
            scorer_version=payload.scorer_version,
            now=now,
        )
    else:
        generation = dict(row.generation or {})
        faults = list(generation.get("scorer_faults") or [])[-4:]
        faults.append(
            {
                "error_kind": payload.error_kind,
                "detail": payload.detail[:500],
                "scorer_version": payload.scorer_version,
                "at": now.isoformat(),
            }
        )
        generation["scorer_faults"] = faults
        row.generation = generation
        row.lease_id = None
        row.lease_expires_at = None
    await session.flush()
    return row


async def close_prescreen_if_reported(
    session: AsyncSession, row: GeneratorPrescreen
) -> bool:
    """Move a running batch to scoring once every panel agent reported."""
    if row.status != "running":
        return False
    reported = set(
        await session.scalars(
            select(GeneratorPrescreenResult.agent_id).where(
                GeneratorPrescreenResult.prescreen_index == row.prescreen_index
            )
        )
    )
    # A panel slot stores its agent id as text; compare as UUIDs.
    if not all(UUID(slot["agent_id"]) in reported for slot in row.panel):
        return False
    row.status = "scoring"
    row.lease_id = None
    row.lease_expires_at = None
    await session.flush()
    return True


async def record_prescreen_score(
    session: AsyncSession, payload: GeneratorRoundScoreResult, *, now: datetime
) -> GeneratorPrescreen:
    """Keep a scored batch's column scores (caller commits).

    Each column's pack score becomes its generator's ``prescreen_score``. No
    case verdict is stored: a pre-screen never claims a fingerprint or feeds
    promotion candidates, and its agent standings are discarded.
    """
    row = _check_lease(
        await get_prescreen(session, payload.round_index, for_update=True),
        payload.lease_id,
        "scoring",
    )
    frozen = {column["column_id"]: column for column in row.columns}
    if {c.column_id for c in payload.score.columns} != set(row.column_cases or {}):
        raise RoundResultRejected("score columns do not match the generated columns")
    for column_score in payload.score.columns:
        column = frozen[column_score.column_id]
        sub = await session.get(
            GeneratorSubmission, UUID(column["submission_id"]), with_for_update=True
        )
        if sub is not None and sub.prescreen_index == row.prescreen_index:
            sub.prescreen_score = column_score.pack.score
            sub.updated_at = now
    row.score = payload.score.model_dump(mode="json")
    row.scorer_version = payload.scorer_version
    row.status = "scored"
    row.closed_at = now
    row.lease_id = None
    row.lease_expires_at = None
    await session.flush()
    return row
