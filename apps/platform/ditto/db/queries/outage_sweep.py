"""Post-outage automatic void sweep over continual-retest samples.

The relay's provider-outage circuit parks scoring while OpenRouter refuses
calls (credit exhaustion included), but it closes on any successful call. In a
low-balance flap a retest can still finish with many refused calls and be
accepted as if it measured the agent. This sweep finds those samples after the
fact and voids them through :func:`void_confirmation_samples` with source
``outage_sweep``, the same writer and the same evidence gate an operator uses.

One run:

1. resumes from the previous run's keyset watermark for the active version
   (the first run reads the active version's whole confirmation history, so
   every sample since that version activated is examined once; with no
   samples it falls back to :data:`SWEEP_FIRST_LOOKBACK`);
2. reads at most :data:`SWEEP_BATCH_LIMIT` un-voided samples accepted at or
   before ``now - SWEEP_SETTLE_LAG`` (rows still committing are left for the
   next run);
3. for each sample whose serving grant meets the gate, voids it with the
   grant's outage window attached (first and last outage-class failure);
4. flags canonical ``scores`` rows accepted in the same time range whose
   grant meets the gate; canonical scores are never replaced here
   (operators use ``replace_validator_score``);
5. appends one :class:`~ditto.db.models.OutageSweepRun`.

Each sample is examined inside its own savepoint, so one row's error is
recorded and skipped without aborting the sweep. Grant failures are always
read by ``grant_id`` (the ``inference_requests`` primary-key prefix), never by
time alone.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, literal, select, text, tuple_

from ditto.db.models import (
    ConfirmationScore,
    InferenceGrant,
    InferenceRequest,
    OutageSweepRun,
    Score,
)
from ditto.db.queries.confirmation_score_voids import (
    OUTAGE_FAILURE_CODES,
    VoidRequest,
    outage_evidence_qualifies,
    void_confirmation_samples,
)
from ditto.db.queries.confirmation_scores import not_voided
from ditto.db.queries.retest_sample_inference import (
    RetestSampleInference,
    retest_sample_inference,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 300.0
"""How often the platform-role loop runs a sweep."""
SWEEP_BATCH_LIMIT = 200
"""Samples examined per run; a full batch resumes on the next run."""
SWEEP_FIRST_LOOKBACK = timedelta(hours=6)
"""How far back the first run (no prior watermark for the version) reads."""
SWEEP_SETTLE_LAG = timedelta(minutes=2)
"""Rows accepted this recently are left for the next run, so a transaction
still committing with an earlier ``created_at`` is never stepped over."""
SWEEP_ACTOR = "platform:outage_sweep"
SWEEP_REASON = "automatic: provider outage evidence on the serving grant"
_SWEEP_LOCK = (118, 3305)
"""``pg_try_advisory_xact_lock`` key: one sweep at a time per database."""


@dataclass(frozen=True)
class GrantOutage:
    grant_id: UUID
    chat_requests: int
    failed_requests: int
    outage_failed: int
    failure_codes: dict[str, int]


@dataclass(frozen=True)
class OutageWindow:
    started_at: datetime
    ended_at: datetime


async def grant_outage_window(
    session: AsyncSession, grant_id: UUID
) -> OutageWindow | None:
    """First and last outage-class failure on one grant (PK-prefix read)."""
    row = (
        await session.execute(
            select(
                func.min(InferenceRequest.started_at),
                func.max(InferenceRequest.started_at),
            ).where(
                InferenceRequest.grant_id == grant_id,
                InferenceRequest.request_kind == "chat",
                InferenceRequest.status == "failed",
                InferenceRequest.terminal_error_code.in_(sorted(OUTAGE_FAILURE_CODES)),
            )
        )
    ).one()
    if row[0] is None:
        return None
    return OutageWindow(started_at=row[0], ended_at=row[1])


async def _grant_outage(session: AsyncSession, grant_id: UUID) -> GrantOutage:
    chat = failed = 0
    codes: Counter[str] = Counter()
    for status, code, count in await session.execute(
        select(
            InferenceRequest.status,
            InferenceRequest.terminal_error_code,
            func.count(),
        )
        .where(
            InferenceRequest.grant_id == grant_id,
            InferenceRequest.request_kind == "chat",
        )
        .group_by(InferenceRequest.status, InferenceRequest.terminal_error_code)
    ):
        chat += count
        if status == "failed":
            failed += count
            codes[code or "unknown"] += count
    return GrantOutage(
        grant_id=grant_id,
        chat_requests=chat,
        failed_requests=failed,
        outage_failed=sum(
            count for code, count in codes.items() if code in OUTAGE_FAILURE_CODES
        ),
        failure_codes=dict(sorted(codes.items())),
    )


def _iso(value: datetime) -> str:
    return value.isoformat()


def _sample_key(row: ConfirmationScore) -> dict[str, object]:
    return {
        "agent_id": str(row.agent_id),
        "validator_hotkey": row.validator_hotkey,
        "seed": str(row.seed),
        "void_generation": row.void_generation,
    }


async def latest_sweep_run(
    session: AsyncSession, *, bench_version: int
) -> OutageSweepRun | None:
    return await session.scalar(
        select(OutageSweepRun)
        .where(OutageSweepRun.bench_version == bench_version)
        .order_by(OutageSweepRun.started_at.desc(), OutageSweepRun.watermark_to.desc())
        .limit(1)
    )


async def _canonical_flags(
    session: AsyncSession,
    *,
    bench_version: int,
    after: datetime,
    through: datetime,
) -> list[tuple[Score, GrantOutage, OutageWindow | None]]:
    """Canonical scores accepted in ``(after, through]`` whose grant qualifies."""
    if through <= after:
        return []
    scores = list(
        await session.scalars(
            select(Score)
            .where(
                Score.bench_version == bench_version,
                Score.updated_at > after,
                Score.updated_at <= through,
            )
            .order_by(Score.updated_at, Score.agent_id, Score.validator_hotkey)
        )
    )
    flagged: list[tuple[Score, GrantOutage, OutageWindow | None]] = []
    for score in scores:
        # The serving grant: the newest one for the same agent, version and
        # validator created at or before the score was accepted (the same
        # pairing rule as retest_sample_inference).
        grant_id = await session.scalar(
            select(InferenceGrant.grant_id)
            .where(
                InferenceGrant.agent_id == score.agent_id,
                InferenceGrant.bench_version == bench_version,
                InferenceGrant.validator_hotkey == score.validator_hotkey,
                InferenceGrant.created_at <= score.updated_at,
            )
            .order_by(InferenceGrant.created_at.desc())
            .limit(1)
        )
        if grant_id is None:
            continue
        outage = await _grant_outage(session, grant_id)
        if not outage_evidence_qualifies(
            chat_requests=outage.chat_requests, outage_failed=outage.outage_failed
        ):
            continue
        flagged.append((score, outage, await grant_outage_window(session, grant_id)))
    return flagged


async def sweep_outage_voids(
    session: AsyncSession,
    *,
    now: datetime,
    bench_version: int,
    batch_limit: int = SWEEP_BATCH_LIMIT,
    first_lookback: timedelta = SWEEP_FIRST_LOOKBACK,
    settle_lag: timedelta = SWEEP_SETTLE_LAG,
    first_run_full_history: bool = True,
) -> OutageSweepRun | None:
    """Run one sweep inside the caller's transaction.

    Returns the appended run, or ``None`` when another sweep holds the lock.
    """
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:a, :b)"),
        {"a": _SWEEP_LOCK[0], "b": _SWEEP_LOCK[1]},
    )
    if not locked:
        return None
    previous = await latest_sweep_run(session, bench_version=bench_version)
    horizon = now - settle_lag
    if previous is None:
        watermark_from = now - first_lookback
        if first_run_full_history:
            # Backfill: the 2026-10-31 credit outage predates this sweep, so
            # the first run walks every sample the active version has.
            earliest = await session.scalar(
                select(func.min(ConfirmationScore.created_at)).where(
                    ConfirmationScore.bench_version == bench_version
                )
            )
            if earliest is not None:
                watermark_from = min(
                    watermark_from, earliest - timedelta(microseconds=1)
                )
        from_key: dict | None = None
    else:
        watermark_from = previous.watermark_to
        from_key = previous.watermark_to_key

    order = (
        ConfirmationScore.created_at,
        ConfirmationScore.agent_id,
        ConfirmationScore.validator_hotkey,
        ConfirmationScore.seed,
        ConfirmationScore.void_generation,
    )
    if from_key is None:
        resume = ConfirmationScore.created_at > watermark_from
    else:
        cursor = (
            watermark_from,
            UUID(str(from_key["agent_id"])),
            str(from_key["validator_hotkey"]),
            int(str(from_key["seed"])),
            int(str(from_key["void_generation"])),
        )
        resume = tuple_(*order) > tuple_(
            *(
                literal(value, type_=column.type)
                for value, column in zip(cursor, order, strict=True)
            )
        )
    rows = list(
        await session.scalars(
            select(ConfirmationScore)
            .where(
                ConfirmationScore.bench_version == bench_version,
                ConfirmationScore.created_at <= horizon,
                resume,
                not_voided(),
            )
            .order_by(*order)
            .limit(batch_limit)
        )
    )
    if len(rows) >= batch_limit:
        watermark_to = rows[-1].created_at
        to_key: dict | None = _sample_key(rows[-1])
    else:
        # Drained: everything accepted at or before the horizon is examined.
        watermark_to = max(horizon, watermark_from)
        to_key = None if horizon >= watermark_from else from_key

    evidence_by_agent: dict[UUID, dict[tuple[str, int, int], RetestSampleInference]]
    evidence_by_agent = {}
    windows: dict[UUID, dict[str, object]] = {}
    voids: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    async def evidence_for(row: ConfirmationScore) -> RetestSampleInference | None:
        if row.agent_id not in evidence_by_agent:
            run_ids = sorted({r.run_id for r in rows if r.agent_id == row.agent_id})
            samples = await retest_sample_inference(
                session,
                agent_id=row.agent_id,
                bench_version=bench_version,
                run_ids=run_ids,
                limit=max(4 * len(run_ids), 30),
            )
            evidence_by_agent[row.agent_id] = {
                (s.validator_hotkey, s.seed, s.void_generation): s for s in samples
            }
        return evidence_by_agent[row.agent_id].get(
            (row.validator_hotkey, row.seed, row.void_generation)
        )

    def observe(
        grant_id: UUID,
        window: OutageWindow,
        *,
        agent_id: UUID,
        validator_hotkey: str,
        outage_failed: int,
        sample: str,
    ) -> None:
        windows.setdefault(
            grant_id,
            {
                "grant_id": str(grant_id),
                "agent_id": str(agent_id),
                "validator_hotkey": validator_hotkey,
                "sample": sample,
                "started_at": _iso(window.started_at),
                "ended_at": _iso(window.ended_at),
                "outage_failed_requests": outage_failed,
            },
        )

    for row in rows:
        try:
            async with session.begin_nested():
                sample = await evidence_for(row)
                if sample is None or sample.grant_id is None:
                    continue
                outage_failed = sum(
                    count
                    for code, count in sample.failure_codes
                    if code in OUTAGE_FAILURE_CODES
                )
                if not outage_evidence_qualifies(
                    chat_requests=sample.chat_requests, outage_failed=outage_failed
                ):
                    continue
                window = await grant_outage_window(session, sample.grant_id)
                (outcome,) = await void_confirmation_samples(
                    session,
                    requests=[
                        VoidRequest(
                            agent_id=row.agent_id,
                            validator_hotkey=row.validator_hotkey,
                            seed=row.seed,
                            expected_run_id=row.run_id,
                            expected_composite=row.composite,
                        )
                    ],
                    bench_version=bench_version,
                    reason=SWEEP_REASON,
                    actor=SWEEP_ACTOR,
                    source="outage_sweep",
                    now=now,
                    apply=True,
                    outage_started_at=window.started_at if window else None,
                    outage_ended_at=window.ended_at if window else None,
                )
                if window is not None:
                    observe(
                        sample.grant_id,
                        window,
                        agent_id=row.agent_id,
                        validator_hotkey=row.validator_hotkey,
                        outage_failed=outage_failed,
                        sample="confirmation",
                    )
                if outcome.disposition == "applied" and outcome.void_id is not None:
                    voids.append(
                        {
                            "void_id": str(outcome.void_id),
                            **_sample_key(row),
                            "run_id": row.run_id,
                            "composite": row.composite,
                            "accepted_at": _iso(row.created_at),
                            "grant_id": str(sample.grant_id),
                            "chat_requests": sample.chat_requests,
                            "outage_failed_requests": outage_failed,
                            "outage_started_at": (
                                _iso(window.started_at) if window else None
                            ),
                            "outage_ended_at": _iso(window.ended_at)
                            if window
                            else None,
                        }
                    )
        except Exception as exc:
            logger.exception(
                "outage sweep could not examine sample %s/%s/%s",
                row.agent_id,
                row.validator_hotkey,
                row.seed,
            )
            errors.append({**_sample_key(row), "error": type(exc).__name__})

    canonical: list[dict[str, object]] = []
    try:
        async with session.begin_nested():
            for score, outage, window in await _canonical_flags(
                session,
                bench_version=bench_version,
                after=watermark_from,
                through=watermark_to,
            ):
                if window is not None:
                    observe(
                        outage.grant_id,
                        window,
                        agent_id=score.agent_id,
                        validator_hotkey=score.validator_hotkey,
                        outage_failed=outage.outage_failed,
                        sample="canonical",
                    )
                canonical.append(
                    {
                        "agent_id": str(score.agent_id),
                        "validator_hotkey": score.validator_hotkey,
                        "run_id": score.run_id,
                        "composite": score.composite,
                        "accepted_at": _iso(score.updated_at),
                        "grant_id": str(outage.grant_id),
                        "chat_requests": outage.chat_requests,
                        "failed_requests": outage.failed_requests,
                        "outage_failed_requests": outage.outage_failed,
                        "failure_codes": outage.failure_codes,
                        "outage_started_at": (
                            _iso(window.started_at) if window else None
                        ),
                        "outage_ended_at": _iso(window.ended_at) if window else None,
                    }
                )
    except Exception as exc:
        logger.exception("outage sweep could not read canonical scores")
        canonical = []
        errors.append({"canonical_scores": True, "error": type(exc).__name__})

    run = OutageSweepRun(
        sweep_id=uuid4(),
        bench_version=bench_version,
        started_at=now,
        finished_at=max(now, datetime.now(now.tzinfo)),
        watermark_from=watermark_from,
        watermark_to=watermark_to,
        watermark_to_key=to_key,
        examined_count=len(rows),
        voided_count=len(voids),
        error_count=sum(1 for error in errors if "canonical_scores" not in error),
        voids=voids,
        outage_windows=sorted(
            windows.values(),
            key=lambda item: (str(item["started_at"]), item["grant_id"]),
        ),
        canonical_flagged=canonical,
        errors=errors,
    )
    session.add(run)
    await session.flush()
    return run


async def list_outage_sweep_runs(
    session: AsyncSession,
    *,
    with_findings: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> tuple[int, list[OutageSweepRun]]:
    """Newest-first sweep runs and the total matching count."""
    statement = select(OutageSweepRun)
    count = select(func.count()).select_from(OutageSweepRun)
    if with_findings:
        finding = (
            (OutageSweepRun.voided_count > 0)
            | (OutageSweepRun.error_count > 0)
            | (func.jsonb_array_length(OutageSweepRun.canonical_flagged) > 0)
            | (func.jsonb_array_length(OutageSweepRun.errors) > 0)
        )
        statement = statement.where(finding)
        count = count.where(finding)
    total = int(await session.scalar(count) or 0)
    runs = list(
        await session.scalars(
            statement.order_by(
                OutageSweepRun.started_at.desc(), OutageSweepRun.sweep_id
            )
            .limit(limit)
            .offset(offset)
        )
    )
    return total, runs


__all__ = [
    "SWEEP_ACTOR",
    "SWEEP_BATCH_LIMIT",
    "SWEEP_FIRST_LOOKBACK",
    "SWEEP_INTERVAL_SECONDS",
    "SWEEP_REASON",
    "SWEEP_SETTLE_LAG",
    "GrantOutage",
    "OutageWindow",
    "grant_outage_window",
    "latest_sweep_run",
    "list_outage_sweep_runs",
    "sweep_outage_voids",
]
