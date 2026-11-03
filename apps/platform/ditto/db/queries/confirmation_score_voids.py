"""Set aside continual-retest samples that measured an inference outage.

A void is an append-only :class:`~ditto.db.models.ConfirmationScoreVoid` row
naming one exact :class:`~ditto.db.models.ConfirmationScore` (key, generation,
run id and composite). Every fold read then skips that row through
:func:`~ditto.db.queries.confirmation_scores.not_voided`, so the seed reads as
unscored for the agent and the continual-retest lane leases it again; the
re-run appends at the next ``void_generation``.

A void is only written on evidence: the run's own inference grant must show
outage-class failures (the gateway's credit refusal or provider backpressure)
above :data:`MIN_OUTAGE_FAILED_CALLS` and either :data:`OUTAGE_FAILED_CALLS`
or :data:`OUTAGE_FAILED_SHARE`. An agent that failed its own calls, or one
stray refusal on an otherwise clean run, does not qualify. Operators and the
post-outage sweep use the same writer and the same gate.

Consensus: validators fold the ``confirmation_history`` Platform serves
(epoch-pinned), median per seed, with no per-row signature check. Excluding a
row here changes that served input identically for every validator in the
next pinned epoch, so no protocol bump is needed and validators cannot split
on it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from sqlalchemy import select

from ditto.api_models.agent_status import AgentStatus
from ditto.db.models import Agent, ConfirmationScore, ConfirmationScoreVoid
from ditto.db.queries.confirmation_scores import not_voided
from ditto.db.queries.retest_sample_inference import retest_sample_inference

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

OUTAGE_FAILURE_CODES: frozenset[str] = frozenset(
    {"upstream_http_402", "upstream_http_429", "upstream_http_503"}
)
"""Relay terminal codes that are the platform's provider path, never the agent:
the gateway's account credit refusal and provider backpressure."""
MIN_OUTAGE_FAILED_CALLS = 5
"""Below this many outage-class failures a run is never voided."""
OUTAGE_FAILED_CALLS = 20
OUTAGE_FAILED_SHARE = 0.05
"""Either bound qualifies a run once :data:`MIN_OUTAGE_FAILED_CALLS` is met."""

VoidSource = Literal["operator", "outage_sweep"]
VoidDisposition = Literal[
    "ready",
    "applied",
    "already_voided",
    "not_found",
    "stale_guard",
    "insufficient_evidence",
]

ConfirmationKey = tuple[UUID, int, str, int, int]


def confirmation_key(score: ConfirmationScore) -> ConfirmationKey:
    return (
        score.agent_id,
        score.bench_version,
        score.validator_hotkey,
        score.seed,
        score.void_generation,
    )


async def voided_confirmation_keys(
    session: AsyncSession, *, agent_id: UUID
) -> frozenset[ConfirmationKey]:
    rows = await session.execute(
        select(
            ConfirmationScoreVoid.agent_id,
            ConfirmationScoreVoid.bench_version,
            ConfirmationScoreVoid.validator_hotkey,
            ConfirmationScoreVoid.seed,
            ConfirmationScoreVoid.void_generation,
        ).where(ConfirmationScoreVoid.agent_id == agent_id)
    )
    return frozenset(
        (agent, int(version), hotkey, int(seed), int(generation))
        for agent, version, hotkey, seed, generation in rows
    )


def evidence_digest(evidence: dict[str, object]) -> str:
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def outage_evidence_qualifies(*, chat_requests: int, outage_failed: int) -> bool:
    if outage_failed < MIN_OUTAGE_FAILED_CALLS:
        return False
    if outage_failed >= OUTAGE_FAILED_CALLS:
        return True
    return chat_requests > 0 and outage_failed / chat_requests >= OUTAGE_FAILED_SHARE


@dataclass(frozen=True)
class VoidRequest:
    agent_id: UUID
    validator_hotkey: str
    seed: int
    expected_run_id: str
    expected_composite: float


@dataclass(frozen=True)
class VoidOutcome:
    request: VoidRequest
    disposition: VoidDisposition
    detail: str | None = None
    void_id: UUID | None = None
    void_generation: int | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    evidence_digest: str | None = None


async def void_confirmation_samples(
    session: AsyncSession,
    *,
    requests: list[VoidRequest],
    bench_version: int,
    reason: str,
    actor: str,
    source: VoidSource,
    now: datetime,
    apply: bool,
    outage_started_at: datetime | None = None,
    outage_ended_at: datetime | None = None,
) -> list[VoidOutcome]:
    """Guard, gate and (when ``apply``) void each requested sample.

    The target is the newest un-voided row on the key, locked ``FOR UPDATE``.
    Its run id and composite must equal the request's expected values, so a
    re-run that already landed, or a second operator's void, cannot be voided by
    a stale request. ``apply=False`` returns the same dispositions without
    writing (``ready`` instead of ``applied``). The caller owns the transaction.
    """
    outcomes: list[VoidOutcome] = []
    for request in requests:
        target = await session.scalar(
            select(ConfirmationScore)
            .where(
                ConfirmationScore.agent_id == request.agent_id,
                ConfirmationScore.bench_version == bench_version,
                ConfirmationScore.validator_hotkey == request.validator_hotkey,
                ConfirmationScore.seed == request.seed,
                not_voided(),
            )
            .order_by(ConfirmationScore.void_generation.desc())
            .limit(1)
            .with_for_update()
        )
        if target is None:
            already = await session.scalar(
                select(ConfirmationScoreVoid.void_id).where(
                    ConfirmationScoreVoid.agent_id == request.agent_id,
                    ConfirmationScoreVoid.bench_version == bench_version,
                    ConfirmationScoreVoid.validator_hotkey == request.validator_hotkey,
                    ConfirmationScoreVoid.seed == request.seed,
                    ConfirmationScoreVoid.run_id == request.expected_run_id,
                )
            )
            outcomes.append(
                VoidOutcome(
                    request=request,
                    disposition="already_voided" if already else "not_found",
                    void_id=already,
                )
            )
            continue
        if (
            target.run_id != request.expected_run_id
            or target.composite != request.expected_composite
        ):
            outcomes.append(
                VoidOutcome(
                    request=request,
                    disposition="stale_guard",
                    detail=(
                        f"current sample is run {target.run_id} at "
                        f"{target.composite}; re-read before voiding"
                    ),
                )
            )
            continue
        samples = await retest_sample_inference(
            session,
            agent_id=request.agent_id,
            bench_version=bench_version,
            run_ids=[target.run_id],
        )
        sample = next(
            (
                item
                for item in samples
                if item.validator_hotkey == target.validator_hotkey
                and item.seed == target.seed
                and item.void_generation == target.void_generation
            ),
            None,
        )
        codes = dict(sample.failure_codes) if sample is not None else {}
        outage_failed = sum(
            count for code, count in codes.items() if code in OUTAGE_FAILURE_CODES
        )
        chat_requests = sample.chat_requests if sample is not None else 0
        evidence: dict[str, object] = {
            "agent_id": str(request.agent_id),
            "bench_version": bench_version,
            "validator_hotkey": target.validator_hotkey,
            "seed": str(target.seed),
            "void_generation": target.void_generation,
            "run_id": target.run_id,
            "composite": target.composite,
            "accepted_at": target.created_at.isoformat(),
            "grant_id": str(sample.grant_id) if sample and sample.grant_id else None,
            "chat_requests": chat_requests,
            "failed_requests": sample.failed_requests if sample is not None else 0,
            "outage_failed_requests": outage_failed,
            "failure_codes": dict(sorted(codes.items())),
        }
        digest = evidence_digest(evidence)
        if not outage_evidence_qualifies(
            chat_requests=chat_requests, outage_failed=outage_failed
        ):
            outcomes.append(
                VoidOutcome(
                    request=request,
                    disposition="insufficient_evidence",
                    detail=(
                        f"{outage_failed} outage-class failures of {chat_requests} "
                        f"chat calls; needs >= {MIN_OUTAGE_FAILED_CALLS} and "
                        f">= {OUTAGE_FAILED_CALLS} or "
                        f">= {OUTAGE_FAILED_SHARE:.0%}"
                    ),
                    evidence=evidence,
                    evidence_digest=digest,
                )
            )
            continue
        if not apply:
            outcomes.append(
                VoidOutcome(
                    request=request,
                    disposition="ready",
                    void_generation=target.void_generation,
                    evidence=evidence,
                    evidence_digest=digest,
                )
            )
            continue
        void = ConfirmationScoreVoid(
            void_id=uuid4(),
            agent_id=target.agent_id,
            bench_version=bench_version,
            validator_hotkey=target.validator_hotkey,
            seed=target.seed,
            void_generation=target.void_generation,
            sample_kind="continual_retest",
            run_id=target.run_id,
            composite=target.composite,
            reason=reason.strip(),
            evidence=evidence,
            evidence_digest=digest,
            actor=actor,
            source=source,
            outage_started_at=outage_started_at,
            outage_ended_at=outage_ended_at,
            created_at=now,
        )
        session.add(void)
        await session.flush()
        outcomes.append(
            VoidOutcome(
                request=request,
                disposition="applied",
                void_id=void.void_id,
                void_generation=target.void_generation,
                evidence=evidence,
                evidence_digest=digest,
            )
        )
    return outcomes


async def list_confirmation_score_voids(
    session: AsyncSession,
    *,
    agent_id: UUID | None = None,
    source: VoidSource | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ConfirmationScoreVoid]:
    statement = select(ConfirmationScoreVoid).order_by(
        ConfirmationScoreVoid.created_at.desc(), ConfirmationScoreVoid.void_id
    )
    if agent_id is not None:
        statement = statement.where(ConfirmationScoreVoid.agent_id == agent_id)
    if source is not None:
        statement = statement.where(ConfirmationScoreVoid.source == source)
    if since is not None:
        statement = statement.where(ConfirmationScoreVoid.created_at >= since)
    return list(await session.scalars(statement.limit(limit).offset(offset)))


@dataclass(frozen=True)
class VoidRerunDebt:
    """Replacement samples one ``(agent, version, seed)`` is still owed.

    A void sets one sample aside; it is not finished until a replacement for
    that exact seed lands. The debt is per seed, not per key, because the
    replacement may come from either side:

    * the voiding validator re-runs its own key, which appends at
      ``void_generation + 1`` and is the path :func:`append_confirmation_scores`
      already accepts for a single-seed lease; or
    * another validator with no row on the seed at all runs it fresh, which
      appends at generation 0 under that validator's own key.

    ``outstanding`` is the number of voided keys with no ``generation + 1`` row,
    less the fresh generation-0 rows other validators recorded on this seed
    after the oldest such void. It never exceeds the number of voids, so the
    lane cannot spend more runs than were set aside.
    """

    agent_id: UUID
    bench_version: int
    seed: int
    outstanding: int
    rerun_validators: frozenset[str]
    """Validators whose own voided key still lacks its ``generation + 1`` row."""
    scored_validators: frozenset[str]
    """Validators holding any row on this seed; a fresh run there would no-op."""
    oldest_void_at: datetime
    newest_void_at: datetime


async def outstanding_void_rerun_debts(
    session: AsyncSession,
    *,
    bench_version: int,
    agent_ids: Iterable[UUID] | None = None,
) -> list[VoidRerunDebt]:
    """Every voided seed whose replacement has not landed yet, oldest first.

    Only agents still ``scored`` or ``live`` owe anything: a rejected, banned or
    superseded generation's voids stay on the audit trail and issue nothing.
    The caller still applies ledger eligibility, which needs the fold.
    """
    statement = (
        select(
            ConfirmationScoreVoid.agent_id,
            ConfirmationScoreVoid.validator_hotkey,
            ConfirmationScoreVoid.seed,
            ConfirmationScoreVoid.void_generation,
            ConfirmationScoreVoid.created_at,
        )
        .join(Agent, Agent.agent_id == ConfirmationScoreVoid.agent_id)
        .where(
            ConfirmationScoreVoid.bench_version == bench_version,
            Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE)),
        )
    )
    if agent_ids is not None:
        ids = list(dict.fromkeys(agent_ids))
        if not ids:
            return []
        statement = statement.where(ConfirmationScoreVoid.agent_id.in_(ids))
    voids: dict[tuple[UUID, int], list[tuple[str, int, datetime]]] = {}
    for (
        agent_id,
        validator_hotkey,
        seed,
        generation,
        created_at,
    ) in await session.execute(statement):
        voids.setdefault((agent_id, int(seed)), []).append(
            (validator_hotkey, int(generation), created_at)
        )
    if not voids:
        return []
    rows: dict[tuple[UUID, int], list[tuple[str, int, datetime]]] = {}
    for (
        agent_id,
        validator_hotkey,
        seed,
        generation,
        created_at,
    ) in await session.execute(
        select(
            ConfirmationScore.agent_id,
            ConfirmationScore.validator_hotkey,
            ConfirmationScore.seed,
            ConfirmationScore.void_generation,
            ConfirmationScore.created_at,
        ).where(
            ConfirmationScore.bench_version == bench_version,
            ConfirmationScore.agent_id.in_(
                sorted({agent_id for agent_id, _seed in voids})
            ),
            ConfirmationScore.seed.in_(sorted({seed for _agent, seed in voids})),
        )
    ):
        key = (agent_id, int(seed))
        if key in voids:
            rows.setdefault(key, []).append(
                (validator_hotkey, int(generation), created_at)
            )
    debts: list[VoidRerunDebt] = []
    for (agent_id, seed), group in voids.items():
        seed_rows = rows.get((agent_id, seed), [])
        row_keys = {(validator, generation) for validator, generation, _ in seed_rows}
        unpaid = [
            (validator, created_at)
            for validator, generation, created_at in group
            if (validator, generation + 1) not in row_keys
        ]
        if not unpaid:
            continue
        oldest = min(created_at for _validator, created_at in unpaid)
        voiding_validators = {validator for validator, _generation, _ in group}
        fresh_replacements = sum(
            1
            for validator, generation, created_at in seed_rows
            if validator not in voiding_validators
            and generation == 0
            and created_at >= oldest
        )
        outstanding = len(unpaid) - fresh_replacements
        if outstanding <= 0:
            continue
        debts.append(
            VoidRerunDebt(
                agent_id=agent_id,
                bench_version=bench_version,
                seed=seed,
                outstanding=outstanding,
                rerun_validators=frozenset(validator for validator, _ in unpaid),
                scored_validators=frozenset(
                    validator for validator, _generation, _ in seed_rows
                ),
                oldest_void_at=oldest,
                newest_void_at=max(created_at for _validator, created_at in unpaid),
            )
        )
    debts.sort(key=lambda debt: (debt.oldest_void_at, str(debt.agent_id), debt.seed))
    return debts


__all__ = [
    "MIN_OUTAGE_FAILED_CALLS",
    "OUTAGE_FAILED_CALLS",
    "OUTAGE_FAILED_SHARE",
    "OUTAGE_FAILURE_CODES",
    "ConfirmationKey",
    "VoidOutcome",
    "VoidRequest",
    "VoidRerunDebt",
    "confirmation_key",
    "evidence_digest",
    "list_confirmation_score_voids",
    "outage_evidence_qualifies",
    "outstanding_void_rerun_debts",
    "void_confirmation_samples",
    "voided_confirmation_keys",
]
