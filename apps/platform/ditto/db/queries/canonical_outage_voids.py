"""Void canonical quorum scores that measured a provider outage (SN-489).

A canonical :class:`~ditto.db.models.Score` is one upserted row per
``(agent, version, validator)``, and its ``run_id`` is part of the signed
payload, so it cannot be set aside in place the way an append-only
continual-retest sample is. Voiding one therefore means queueing a
same-validator replacement re-test with basis
:data:`~ditto.db.queries.score_retests.OUTAGE_VOID_RETEST_BASIS`:

* the validator re-runs its canonical seed for the agent on the same ticket
  row (re-leased even when a continual retest left it ``expired``), ahead of
  ordinary quorum work on any free slot, without spending an attempt from the
  miner's retry budget;
* when the replacement lands, the score path appends ``score_invalidated``
  (carrying the contaminated score and this basis) and upserts the new run, so
  every median read (leaderboard, ledger pin, crown) recomputes without the
  outage run; an agent still accumulating its quorum cannot finalize on the
  contaminated score, because its ticket is live again until the re-run lands;
* until then the accepted score stays canonical, the same contract as
  ``replace_validator_score``.

The evidence gate is the continual-retest void gate
(:func:`~ditto.db.queries.confirmation_score_voids.outage_evidence_qualifies`)
read off the score's own serving grant. Guards: the exact run id and composite
(and seed when given), a ticket row to reuse, a scoreable agent, and no other
open re-test on the key. The request id is derived from the voided run,
so a repeat is ``already_queued`` and never a second request.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from sqlalchemy import func, select

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import (
    Agent,
    InferenceGrant,
    InferenceRequest,
    Score,
    ScoreAuditEntry,
    ValidatorTicket,
)
from ditto.db.queries.audit import (
    EVENT_SCORE_INVALIDATED,
    EVENT_SCORE_RETEST_QUEUED,
    append_audit_entry,
    get_latest_score_retest_event,
    lock_audit_chain,
)
from ditto.db.queries.confirmation_score_voids import (
    MIN_OUTAGE_FAILED_CALLS,
    OUTAGE_FAILED_CALLS,
    OUTAGE_FAILED_SHARE,
    OUTAGE_FAILURE_CODES,
    VoidSource,
    evidence_digest,
    outage_evidence_qualifies,
)
from ditto.db.queries.score_retests import OUTAGE_VOID_RETEST_BASIS, retest_is_open

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

OUTAGE_VOID_NAMESPACE = UUID("6b0f3c1e-2f4d-4e57-9a8c-5d1b0e489489")
"""uuid5 namespace for the deterministic request id of one canonical void."""
MAX_AUTOMATIC_OUTAGE_RETESTS = 2
"""Sweep-queued replacements per ``(agent, version, validator)``. A third
outage-tainted run on one key is left for an operator (``auto_cap_reached``)."""
RETESTABLE_STATUSES = frozenset(
    {
        AgentStatus.SCORED,
        AgentStatus.LIVE,
        AgentStatus.EVALUATING,
        AgentStatus.ATH_PENDING_REVIEW,
    }
)
"""An agent still accumulating its quorum (or held for ATH review) is voidable
too: re-issuing its ticket keeps it from finalizing on the outage run."""

CanonicalVoidDisposition = Literal[
    "ready",
    "applied",
    "already_queued",
    "already_replaced",
    "not_found",
    "stale_guard",
    "insufficient_evidence",
    "retest_busy",
    "retest_closed",
    "not_retestable",
    "auto_cap_reached",
]


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


async def grant_outage(session: AsyncSession, grant_id: UUID) -> GrantOutage:
    """Chat-call outcome counts on one grant (PK-prefix read)."""
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


async def canonical_serving_grant(session: AsyncSession, score: Score) -> UUID | None:
    """The grant that served a canonical score's run.

    The newest grant for the same agent, version and validator created at or
    before the score was accepted (the pairing rule of
    :func:`~ditto.db.queries.retest_sample_inference.retest_sample_inference`).
    One validator holds at most one live lease per agent, so no other grant of
    that key can fall between the run's lease and its acceptance.
    """
    return await session.scalar(
        select(InferenceGrant.grant_id)
        .where(
            InferenceGrant.agent_id == score.agent_id,
            InferenceGrant.bench_version == score.bench_version,
            InferenceGrant.validator_hotkey == score.validator_hotkey,
            InferenceGrant.created_at <= score.updated_at,
        )
        .order_by(InferenceGrant.created_at.desc())
        .limit(1)
    )


def outage_void_request_id(
    *, agent_id: UUID, bench_version: int, validator_hotkey: str, run_id: str
) -> UUID:
    """The one request id a void of this exact canonical run may use."""
    return uuid5(
        OUTAGE_VOID_NAMESPACE,
        f"provider-outage-void:{agent_id}:{bench_version}:{validator_hotkey}:{run_id}",
    )


@dataclass(frozen=True)
class CanonicalVoidRequest:
    agent_id: UUID
    validator_hotkey: str
    expected_run_id: str
    expected_composite: float
    expected_seed: int | None = None


@dataclass(frozen=True)
class CanonicalVoidOutcome:
    request: CanonicalVoidRequest
    disposition: CanonicalVoidDisposition
    detail: str | None = None
    request_id: UUID | None = None
    seed: int | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    evidence_digest: str | None = None


async def is_canonical_run(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    validator_hotkey: str,
    run_id: str,
) -> bool:
    """Whether ``run_id`` is (or was) this key's canonical score run.

    True for the accepted score's current run, and for a run an outage void
    already named (so a repeat after the replacement landed still resolves to
    the canonical path and reads ``already_replaced``).
    """
    current = await session.scalar(
        select(Score.run_id).where(
            Score.agent_id == agent_id,
            Score.bench_version == bench_version,
            Score.validator_hotkey == validator_hotkey,
        )
    )
    if current == run_id:
        return True
    request_id = outage_void_request_id(
        agent_id=agent_id,
        bench_version=bench_version,
        validator_hotkey=validator_hotkey,
        run_id=run_id,
    )
    return (
        await session.scalar(
            select(ScoreAuditEntry.seq)
            .where(
                ScoreAuditEntry.agent_id == agent_id,
                ScoreAuditEntry.validator_hotkey == validator_hotkey,
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_QUEUED,
                ScoreAuditEntry.payload["request_id"].as_string() == str(request_id),
            )
            .limit(1)
        )
        is not None
    )


async def _replaced(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    validator_hotkey: str,
    run_id: str,
) -> bool:
    return (
        await session.scalar(
            select(ScoreAuditEntry.seq)
            .where(
                ScoreAuditEntry.agent_id == agent_id,
                ScoreAuditEntry.validator_hotkey == validator_hotkey,
                ScoreAuditEntry.event == EVENT_SCORE_INVALIDATED,
                ScoreAuditEntry.payload["run_id"].as_string() == run_id,
                ScoreAuditEntry.payload["bench_version"].as_integer() == bench_version,
            )
            .limit(1)
        )
        is not None
    )


async def _automatic_outage_retests(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    validator_hotkey: str,
) -> int:
    return int(
        await session.scalar(
            select(func.count(ScoreAuditEntry.seq)).where(
                ScoreAuditEntry.agent_id == agent_id,
                ScoreAuditEntry.validator_hotkey == validator_hotkey,
                ScoreAuditEntry.event == EVENT_SCORE_RETEST_QUEUED,
                ScoreAuditEntry.payload["basis"].as_string()
                == OUTAGE_VOID_RETEST_BASIS,
                ScoreAuditEntry.payload["source"].as_string() == "outage_sweep",
                ScoreAuditEntry.payload["bench_version"].as_integer() == bench_version,
            )
        )
        or 0
    )


def _preserved_score(score: Score) -> dict[str, object]:
    return {
        "run_id": score.run_id,
        "seed": str(score.seed),
        "composite": score.composite,
        "tool_mean": score.tool_mean,
        "memory_mean": score.memory_mean,
        "median_ms": score.median_ms,
        "n": score.n,
        "bench_version": score.bench_version,
        "signature": score.signature,
        "generated_at": score.generated_at.isoformat(),
        "accepted_at": score.updated_at.isoformat(),
    }


async def void_canonical_scores(
    session: AsyncSession,
    *,
    requests: list[CanonicalVoidRequest],
    bench_version: int,
    reason: str,
    actor: str,
    source: VoidSource,
    now: datetime,
    apply: bool,
) -> list[CanonicalVoidOutcome]:
    """Guard, gate and (when ``apply``) queue each canonical replacement.

    ``apply=False`` returns the same dispositions without writing (``ready``
    instead of ``applied``). The caller owns the transaction. With ``apply``
    the audit-chain lock is held from the gate through the append, so two
    concurrent voids of one run queue it once. The score row is read
    unlocked: the score path takes the audit-chain lock before the score row,
    so locking the row here first could deadlock against it. A score that
    changes after this read closes the queued request stale at promotion
    (``score.run_id`` must still equal the request's ``run_id``).
    """
    if apply:
        # Serialize the gate with the append: a concurrent void of the same run
        # (the sweep and an operator) waits here, then reads the first one's
        # queued event and answers ``already_queued``. The score path takes this
        # lock before the score row too, so the lock order is unchanged.
        await lock_audit_chain(session)
    outcomes: list[CanonicalVoidOutcome] = []
    for request in requests:
        request_id = outage_void_request_id(
            agent_id=request.agent_id,
            bench_version=bench_version,
            validator_hotkey=request.validator_hotkey,
            run_id=request.expected_run_id,
        )
        outcomes.append(
            await _void_one(
                session,
                request=request,
                request_id=request_id,
                bench_version=bench_version,
                reason=reason,
                actor=actor,
                source=source,
                now=now,
                apply=apply,
            )
        )
    return outcomes


async def _void_one(
    session: AsyncSession,
    *,
    request: CanonicalVoidRequest,
    request_id: UUID,
    bench_version: int,
    reason: str,
    actor: str,
    source: VoidSource,
    now: datetime,
    apply: bool,
) -> CanonicalVoidOutcome:
    def ungated(
        disposition: CanonicalVoidDisposition, detail: str | None = None
    ) -> CanonicalVoidOutcome:
        return CanonicalVoidOutcome(
            request=request,
            disposition=disposition,
            detail=detail,
            request_id=request_id,
        )

    score = await session.scalar(
        select(Score).where(
            Score.agent_id == request.agent_id,
            Score.bench_version == bench_version,
            Score.validator_hotkey == request.validator_hotkey,
        )
    )
    if score is None or score.run_id != request.expected_run_id:
        if await _replaced(
            session,
            agent_id=request.agent_id,
            bench_version=bench_version,
            validator_hotkey=request.validator_hotkey,
            run_id=request.expected_run_id,
        ):
            return ungated(
                "already_replaced",
                "this run was already replaced; its score no longer counts",
            )
        if score is None:
            return ungated("not_found")
        return ungated(
            "stale_guard",
            f"current score is run {score.run_id} at {score.composite}; "
            "re-read before voiding",
        )
    if score.composite != request.expected_composite or (
        request.expected_seed is not None and score.seed != request.expected_seed
    ):
        return ungated(
            "stale_guard",
            f"current score is run {score.run_id} at {score.composite} on seed "
            f"{score.seed}; re-read before voiding",
        )

    grant_id = await canonical_serving_grant(session, score)
    grant = await grant_outage(session, grant_id) if grant_id is not None else None
    evidence: dict[str, object] = {
        "sample_kind": "canonical",
        "agent_id": str(score.agent_id),
        "bench_version": bench_version,
        "validator_hotkey": score.validator_hotkey,
        "seed": str(score.seed),
        "run_id": score.run_id,
        "composite": score.composite,
        "accepted_at": score.updated_at.isoformat(),
        "grant_id": str(grant_id) if grant_id is not None else None,
        "chat_requests": grant.chat_requests if grant else 0,
        "failed_requests": grant.failed_requests if grant else 0,
        "outage_failed_requests": grant.outage_failed if grant else 0,
        "failure_codes": grant.failure_codes if grant else {},
    }
    digest = evidence_digest(evidence)

    def gated(
        disposition: CanonicalVoidDisposition, detail: str | None = None
    ) -> CanonicalVoidOutcome:
        return CanonicalVoidOutcome(
            request=request,
            disposition=disposition,
            detail=detail,
            request_id=request_id,
            seed=score.seed,
            evidence=evidence,
            evidence_digest=digest,
        )

    if grant is None:
        return gated("insufficient_evidence", "no inference grant served this run")
    if not outage_evidence_qualifies(
        chat_requests=grant.chat_requests, outage_failed=grant.outage_failed
    ):
        return gated(
            "insufficient_evidence",
            f"{grant.outage_failed} outage-class failures of {grant.chat_requests} "
            f"chat calls; needs >= {MIN_OUTAGE_FAILED_CALLS} and "
            f">= {OUTAGE_FAILED_CALLS} or >= {OUTAGE_FAILED_SHARE:.0%}",
        )

    latest = await get_latest_score_retest_event(
        session, agent_id=request.agent_id, validator_hotkey=request.validator_hotkey
    )
    if latest is not None and latest.payload.get("request_id") == str(request_id):
        if retest_is_open(latest):
            return gated("already_queued")
        return gated(
            "retest_closed",
            "the replacement for this run closed without a new score "
            f"({latest.payload.get('reason')}); use replace_validator_score "
            "to retry",
        )
    if retest_is_open(latest):
        assert latest is not None
        return gated(
            "retest_busy",
            "another re-test is open on this score "
            f"(basis {latest.payload.get('basis', 'operator')}); it replaces "
            "the score when it lands",
        )
    agent = await session.get(Agent, request.agent_id)
    if agent is None or agent.status not in RETESTABLE_STATUSES:
        state = f" ({agent.status.value})" if agent is not None else ""
        return gated("not_retestable", f"submission is not in a scoreable state{state}")
    ticket = await session.get(
        ValidatorTicket, (request.agent_id, bench_version, request.validator_hotkey)
    )
    # Continual-retest leases reuse this row, so after a failed, expired or
    # outage-parked retest it reads ``expired`` while the accepted score is
    # still this run (the 2026-11-17 case). The score and its exact run are the
    # authority, as for v9 contract repair; a live lease is waited out.
    if ticket is None or ticket.status not in {
        TicketStatus.SCORED,
        TicketStatus.EXPIRED,
        TicketStatus.ISSUED,
    }:
        return gated(
            "not_retestable",
            "accepted score has no validator ticket row to reuse",
        )
    if (
        source == "outage_sweep"
        and await _automatic_outage_retests(
            session,
            agent_id=request.agent_id,
            bench_version=bench_version,
            validator_hotkey=request.validator_hotkey,
        )
        >= MAX_AUTOMATIC_OUTAGE_RETESTS
    ):
        return gated(
            "auto_cap_reached",
            f"{MAX_AUTOMATIC_OUTAGE_RETESTS} automatic outage re-tests already "
            "ran on this key; operator review required",
        )
    if not apply:
        return gated("ready")
    window = await grant_outage_window(session, grant.grant_id)
    await append_audit_entry(
        session,
        agent_id=request.agent_id,
        validator_hotkey=request.validator_hotkey,
        event=EVENT_SCORE_RETEST_QUEUED,
        payload={
            "request_id": str(request_id),
            "actor": actor,
            "reason": reason.strip(),
            "basis": OUTAGE_VOID_RETEST_BASIS,
            "source": source,
            "automatic": source == "outage_sweep",
            "bench_version": bench_version,
            "run_id": score.run_id,
            "preserved_score": _preserved_score(score),
            "evidence": evidence,
            "evidence_digest": digest,
            "outage_started_at": (
                window.started_at.isoformat() if window is not None else None
            ),
            "outage_ended_at": (
                window.ended_at.isoformat() if window is not None else None
            ),
        },
        recorded_at=now,
    )
    return gated("applied")


__all__ = [
    "MAX_AUTOMATIC_OUTAGE_RETESTS",
    "OUTAGE_VOID_NAMESPACE",
    "RETESTABLE_STATUSES",
    "CanonicalVoidDisposition",
    "CanonicalVoidOutcome",
    "CanonicalVoidRequest",
    "GrantOutage",
    "OutageWindow",
    "canonical_serving_grant",
    "grant_outage",
    "grant_outage_window",
    "is_canonical_run",
    "outage_void_request_id",
    "void_canonical_scores",
]
