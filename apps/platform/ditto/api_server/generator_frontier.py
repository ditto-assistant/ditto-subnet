"""Generator frontier cases: oracle attestations and sampled operator audits.

A case no two independent panel owners solve is ``unsolved``: it earns
nothing and the column pays the squared unsolved penalty. Solving proves a
case valid, but failing it does not prove it broken, so a generator whose
valid cases beat every agent used to score 0. The frontier lane gives such a
case a second witness (``research/dittobench-datagen/docs/
generator-competition.md``, "Frontier cases"):

1. **Score.** A round scores exactly as before. If frontier scoring is on
   (``frontier_weight > 0`` and ``frontier_oracle_agent_id`` set) and a miner
   column has unsolved cases, :func:`begin_frontier` records them as the
   round's frontier candidates (``generator_rounds.frontier``).
2. **Oracle.** A validator whose scorer runs ``generator-frontier-oracle-v1``
   leases one oracle run of the configured reference agent on the candidates
   (``endpoints/validator_generator_round.py``). Each case is answered from
   only its bounded ``challenge.OracleContext`` and graded like a panel case.
   A case the oracle passes (score >= ``solve_threshold``, not faulted) is
   attested.
3. **Audit.** For each column with attested cases, Platform draws
   :func:`frontier_audit_sample` (``challenge.FrontierAuditSample``) from the
   secret round seed, so a generator cannot tell which cases are read and
   Platform cannot grind the choice. Operators rule each audit in Backroom; an
   invalid case voids every attestation on the column.
4. **Settle.** Once every audit is ruled, or ``frontier_audit_grace_hours``
   pass, or the oracle never reports within ``frontier_oracle_timeout_hours``,
   :func:`settle_frontier` applies :func:`apply_frontier` to the stored
   verdicts: attested unsolved cases become ``frontier``, and the column
   score, verdict rows, and the generator's ``round_score`` are rewritten.
   The pre-frontier column scores stay on the round.

**Escalation** (``frontier_escalation_agent_id``, off by default). A case the
panel and the oracle both fail stays unsolved, so a hard but valid case
scores like a broken one. With escalation on, the oracle's report does not
open the audits yet: :func:`select_escalation` takes a capped sample of the
oracle-failed candidates (at most ``frontier_escalation_max_cases_per_column``
per column and ``frontier_escalation_max_cases`` per round, lowest
``sha256(domain ‖ round_seed ‖ column ‖ case)`` first, so the secret round
seed decides and a generator cannot), skipping every column whose oracle
failures exceed ``frontier_escalation_max_failed_fraction`` of its cases (a
column that mostly beats the oracle looks broken, not hard). The lane stays
``oracle_pending`` in its ``escalation`` phase while one lease runs the
stronger escalation agent on exactly those cases, on the same bounded
contexts and dataset mode. A case it passes at
``frontier_escalation_solve_threshold`` (default 1.0, a full answer) joins the
round's attestations, and the audits open over every attested case, so an
escalated case is audited and credited exactly like an oracle pass. An
escalation that never reports (timeout or attempt cap) only adds nothing:
the oracle's own attestations still go to audit.

Until a round settles it pays its unattested score, so frontier credit never
reaches the ledger before its audit. :func:`apply_frontier` and
:func:`frontier_audit_sample` port the Go contract and replay its golden
vectors (``challenge/testdata/frontier_vectors.json`` and
``round_vectors.json``), so a settled score is exactly what
``challenge.ScoreCases`` gives with the attestations.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_models.generator_round_validator import GeneratorRoundCaseResult
from ditto.api_models.generator_rounds import (
    GeneratorFrontierAuditView,
    GeneratorFrontierCaseView,
    GeneratorFrontierEscalationView,
    GeneratorFrontierMemoryPair,
    GeneratorFrontierOracleView,
    GeneratorFrontierView,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator_capabilities import ValidatorCapabilities
from ditto.db.models import (
    GeneratorRound,
    GeneratorRoundAttempt,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
    InferenceGrant,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.canonical_outage_voids import GrantOutage, grant_outage

logger = logging.getLogger(__name__)

FRONTIER_AUDIT_DOMAIN = b"dittobench-frontier-audit/v1"
FRONTIER_ESCALATION_DOMAIN = b"dittobench-frontier-escalation/v1"
FRONTIER_DOCUMENT_VERSION = 1
# Oracle leases a round issues before it settles with no attestations, so a
# broken oracle cannot pin a round. A lease still running is always allowed to
# finish (its ticket TTL bounds it), so the last attempt can still deliver.
MAX_ORACLE_ATTEMPTS = 4
# Share of an oracle (or escalation) run's chat calls that may fail upstream
# before its answers are refused as an infrastructure failure. A harness whose
# model calls are refused (context overflow, provider 400, timeout) still
# answers /run, so every case it could not reason about grades as an honest
# 0.0 and the round would settle with no attestations, as if the oracle had
# tried and failed. SN-496: in rounds 26139-26161 every all-zero oracle run
# had 73-97% of its chat calls fail upstream; every run that attested had at
# most 27%.
ORACLE_MAX_FAILED_CHAT_FRACTION = 0.5

STATE_ORACLE_PENDING = "oracle_pending"
STATE_AUDIT_PENDING = "audit_pending"
STATE_SETTLED = "settled"

# Phases of an ``oracle_pending`` lane: the oracle's run, then (with
# escalation on and oracle-failed cases selected) the escalation agent's run.
PHASE_ORACLE = "oracle"
PHASE_ESCALATION = "escalation"

AUDIT_PENDING = "pending"
AUDIT_PASSED = "passed"
AUDIT_FAILED = "failed"

CASE_UNSOLVED = "unsolved"
CASE_FRONTIER = "frontier"
CASE_DISCRIMINATING = "discriminating"


# Neither scan reads a round's dataset, case refs, or generation log.
_SCAN_DEFERRED = (
    defer(GeneratorRound.dataset_bytes, raiseload=True),
    defer(GeneratorRound.column_cases, raiseload=True),
    defer(GeneratorRound.generation, raiseload=True),
)


def _open_frontier(state: str | None = None) -> Any:
    column = GeneratorRound.frontier["state"].as_string()
    return column == state if state is not None else column != STATE_SETTLED


class FrontierRejected(ValueError):
    """A frontier write that does not match the round's frontier state."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# --- contract ports ---------------------------------------------------------


def frontier_audit_sample(
    round_seed: str, column_id: str, case_ids: Iterable[str], n: int
) -> list[str]:
    """The ``n`` distinct case ids with the lowest
    ``sha256(domain ‖ 0 ‖ round_seed ‖ 0 ‖ column_id ‖ 0 ‖ case_id)``, lowest
    first (``challenge.FrontierAuditSample``)."""
    if n <= 0:
        return []
    keyed: dict[str, str] = {}
    for case_id in case_ids:
        if case_id in keyed:
            continue
        keyed[case_id] = hashlib.sha256(
            FRONTIER_AUDIT_DOMAIN
            + b"\x00"
            + round_seed.encode()
            + b"\x00"
            + column_id.encode()
            + b"\x00"
            + case_id.encode()
        ).hexdigest()
    return [case_id for case_id, _ in sorted(keyed.items(), key=lambda kv: kv[1])][:n]


def _escalation_key(round_seed: str, case_id: str) -> str:
    column_id, _, local = case_id.partition("/")
    return hashlib.sha256(
        FRONTIER_ESCALATION_DOMAIN
        + b"\x00"
        + round_seed.encode()
        + b"\x00"
        + column_id.encode()
        + b"\x00"
        + local.encode()
    ).hexdigest()


def select_escalation(
    *,
    round_seed: str,
    failed: Sequence[str],
    column_sizes: dict[str, int],
    max_cases: int,
    max_cases_per_column: int,
    max_failed_fraction: float,
) -> tuple[list[str], dict[str, str]]:
    """The oracle-failed round case ids the escalation agent re-checks, and
    the columns skipped with their reason.

    ``failed`` are round case ids (``column/case``) the oracle answered and
    failed (never faulted ones); ``column_sizes`` is each column's scored,
    non-duplicate case count. A column whose failures exceed
    ``max_failed_fraction`` of its cases is skipped whole, so a pack flooded
    with unsolvable cases buys no escalation. From each remaining column the
    ``max_cases_per_column`` cases with the lowest
    ``sha256(domain ‖ 0 ‖ round_seed ‖ 0 ‖ column ‖ 0 ‖ case)`` are taken, then
    the ``max_cases`` lowest of those across the round. The round seed is
    secret until reveal, so a generator cannot steer which cases are picked,
    and the result is returned in key order (deterministic)."""
    by_column: dict[str, list[str]] = {}
    for case_id in dict.fromkeys(failed):
        by_column.setdefault(case_id.split("/", 1)[0], []).append(case_id)
    skipped: dict[str, str] = {}
    pool: list[tuple[str, str]] = []
    for column_id in sorted(by_column):
        case_ids = by_column[column_id]
        size = column_sizes.get(column_id, 0)
        if size <= 0:
            skipped[column_id] = "column has no scored cases"
            continue
        share = len(case_ids) / size
        if share > max_failed_fraction:
            skipped[column_id] = (
                f"oracle failed {len(case_ids)}/{size} cases "
                f"({share:.0%} > {max_failed_fraction:.0%})"
            )
            continue
        keyed = sorted((_escalation_key(round_seed, c), c) for c in case_ids)
        pool.extend(keyed[: max(0, max_cases_per_column)])
    pool.sort()
    return [case_id for _, case_id in pool[: max(0, max_cases)]], skipped


def apply_frontier(
    pack: dict[str, Any],
    attested: Iterable[tuple[str, str]],
    params: dict[str, Any],
) -> dict[str, Any]:
    """The pack score ``challenge.ScoreCases`` gives with ``attested`` as its
    ``FrontierCases``, from the score it gave without them.

    ``attested`` names each case as ``(case_id, fingerprint)``, as the Go
    contract does: an attestation applies only to the exact case content it
    was earned on, never to a different case that reuses its id.

    Frontier status only rewrites ``unsolved`` verdicts, and every input it
    reads (mean score, champion failure) is on the stored verdict, so the
    attested score is a pure post-pass. Floats are summed in case order with
    the same operations as the Go contract, so the result is bit-identical
    (``challenge/testdata/frontier_vectors.json``). A pack with an
    insufficient panel, or ``frontier_weight`` 0, is returned unchanged.
    """
    weight = float(params.get("frontier_weight") or 0.0)
    out = copy.deepcopy(pack)
    if out.get("status") != "scored" or weight <= 0:
        return out
    wanted = set(attested)
    bonus = float(params["champion_bonus"])
    cases: list[dict[str, Any]] = out["cases"]
    for case in cases:
        if (
            case["status"] != CASE_UNSOLVED
            or (
                case["case_id"],
                case["fingerprint"],
            )
            not in wanted
        ):
            continue
        credit = 1 - float(case["mean_score"])
        if case.get("champion_failed"):
            credit *= 1 + bonus
        credit *= weight
        case["status"] = CASE_FRONTIER
        case["credit"] = credit
    raw_credit = 0.0
    unsolved = frontier = 0
    for case in cases:
        if case["status"] in (CASE_DISCRIMINATING, CASE_FRONTIER):
            raw_credit += float(case["credit"])
        if case["status"] == CASE_UNSOLVED:
            unsolved += 1
        elif case["status"] == CASE_FRONTIER:
            frontier += 1
    out["raw_credit"] = raw_credit
    out["unsolved"] = unsolved
    if frontier:
        out["frontier"] = frontier
    else:
        out.pop("frontier", None)
    considered = len(cases) - int(out.get("duplicate") or 0)
    if considered > 0:
        out["unsolved_rate"] = unsolved / considered
    denom = max(len(cases), int(params["target_cases"]))
    solvable = 1 - float(out["unsolved_rate"])
    score = raw_credit / ((1 + bonus) * denom) * solvable * solvable
    out["score"] = min(score, 1.0)
    return out


# --- lifecycle --------------------------------------------------------------


def frontier_candidates(row: GeneratorRound) -> list[str]:
    """The round case ids the panel left unsolved on miner columns, in column
    then case order. The public default generator is owned by nobody and
    never paid, so its cases are never sent to the oracle."""
    miner_columns = {
        column["column_id"]
        for column in row.columns or ()
        if not column.get("public_default")
    }
    out: list[str] = []
    for column in (row.score or {}).get("columns", ()):
        pack = column["pack"]
        if column["column_id"] not in miner_columns or pack.get("status") != "scored":
            continue
        out.extend(
            case["case_id"]
            for case in pack.get("cases", ())
            if case["status"] == CASE_UNSOLVED
        )
    return out


def begin_frontier(
    row: GeneratorRound, settings: GeneratorCompetitionSettings, *, now: datetime
) -> bool:
    """Open the frontier lane on a just-scored round, if it has candidates.

    The policy is frozen from the round's own settings revision, so a later
    settings write never changes how an open round settles."""
    if not settings.frontier_enabled or row.frontier is not None:
        return False
    candidates = frontier_candidates(row)
    if not candidates:
        return False
    row.frontier = {
        "version": FRONTIER_DOCUMENT_VERSION,
        "state": STATE_ORACLE_PENDING,
        "oracle_agent_id": str(settings.frontier_oracle_agent_id),
        "frontier_weight": settings.frontier_weight,
        "audit_cases": settings.frontier_audit_cases,
        "audit_grace_hours": settings.frontier_audit_grace_hours,
        "oracle_timeout_hours": settings.frontier_oracle_timeout_hours,
        "solve_threshold": settings.solve_threshold,
        "candidates": candidates,
        "created_at": _iso(now),
        "oracle_attempts": 0,
        "oracle": None,
        "attested": {},
        "audits": {},
        "phase": PHASE_ORACLE,
        # Frozen like the rest of the policy; None runs no escalation.
        "escalation_policy": (
            {
                "agent_id": str(settings.frontier_escalation_agent_id),
                "solve_threshold": settings.frontier_escalation_solve_threshold,
                "max_cases": settings.frontier_escalation_max_cases,
                "max_cases_per_column": (
                    settings.frontier_escalation_max_cases_per_column
                ),
                "max_failed_fraction": settings.frontier_escalation_max_failed_fraction,
            }
            if settings.frontier_escalation_enabled
            else None
        ),
        "escalation": None,
    }
    return True


def in_escalation(frontier: dict[str, Any] | None) -> bool:
    """The lane is waiting on its escalation run, not the oracle's."""
    frontier = frontier or {}
    return (
        frontier.get("state") == STATE_ORACLE_PENDING
        and frontier.get("phase") == PHASE_ESCALATION
        and bool(frontier.get("escalation"))
    )


def oracle_run_agent(frontier: dict[str, Any]) -> str:
    """The agent the lane's open run leases: the oracle, or in the
    escalation phase the escalation agent."""
    if in_escalation(frontier):
        return str(frontier["escalation"]["agent_id"])
    return str(frontier["oracle_agent_id"])


def oracle_run_cases(frontier: dict[str, Any]) -> list[str]:
    """The round case ids the lane's open run answers: every candidate, or in
    the escalation phase only the selected oracle-failed cases."""
    if in_escalation(frontier):
        return list(frontier["escalation"]["selected"])
    return list(frontier.get("candidates") or ())


def oracle_capable(heartbeat: ValidatorHeartbeat | None) -> bool:
    """The validator's scorer runs the frontier oracle mode. The round column
    capacity check (version, freshness, inference) is applied separately."""
    if heartbeat is None:
        return False
    try:
        capabilities = ValidatorCapabilities.model_validate(heartbeat.capabilities)
    except ValidationError:
        return False
    scorer = capabilities.scorer_benchmarks
    return bool(
        scorer is not None
        and scorer.status == "fresh_verified"
        and scorer.generator_rounds
        and scorer.generator_frontier_oracle
    )


async def live_oracle_lease(
    session: AsyncSession, row: GeneratorRound, *, now: datetime
) -> GeneratorRoundAttempt | None:
    """The round's oracle lease that is still open, if any."""
    return await session.scalar(
        select(GeneratorRoundAttempt)
        .join(
            ValidatorTicket,
            (ValidatorTicket.agent_id == GeneratorRoundAttempt.agent_id)
            & (
                ValidatorTicket.validator_hotkey
                == GeneratorRoundAttempt.validator_hotkey
            )
            & (ValidatorTicket.deadline == GeneratorRoundAttempt.ticket_deadline),
        )
        .where(
            GeneratorRoundAttempt.round_index == row.round_index,
            GeneratorRoundAttempt.kind == "oracle",
            GeneratorRoundAttempt.status == "leased",
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.purpose == TicketPurpose.GENERATOR_ROUND,
            ValidatorTicket.deadline > now,
        )
        .limit(1)
    )


async def lock_oracle_tickets(
    session: AsyncSession, round_index: int
) -> list[ValidatorTicket]:
    """Lock the tickets of a round's open oracle attempts, before its round.

    A result submission locks its ticket and then its round. A writer that
    may expire an oracle lease (an operator settle) must take the same locks
    in the same order, ticket first, or the two can deadlock."""
    return list(
        await session.scalars(
            select(ValidatorTicket)
            .join(
                GeneratorRoundAttempt,
                (GeneratorRoundAttempt.agent_id == ValidatorTicket.agent_id)
                & (
                    GeneratorRoundAttempt.validator_hotkey
                    == ValidatorTicket.validator_hotkey
                )
                & (GeneratorRoundAttempt.ticket_deadline == ValidatorTicket.deadline),
            )
            .where(
                GeneratorRoundAttempt.round_index == round_index,
                GeneratorRoundAttempt.kind == "oracle",
                GeneratorRoundAttempt.status == "leased",
                ValidatorTicket.purpose == TicketPurpose.GENERATOR_ROUND,
                ValidatorTicket.status == TicketStatus.ISSUED,
            )
            .order_by(ValidatorTicket.validator_hotkey, ValidatorTicket.deadline)
            .with_for_update(of=ValidatorTicket)
        )
    )


async def close_stale_oracle_attempts(
    session: AsyncSession, row: GeneratorRound, *, now: datetime
) -> None:
    """Close oracle attempts still marked leased whose ticket is no longer
    live (it expired without results). Call only with no live oracle lease:
    a scored round never runs the round-close sweep that closes them."""
    await session.execute(
        update(GeneratorRoundAttempt)
        .where(
            GeneratorRoundAttempt.round_index == row.round_index,
            GeneratorRoundAttempt.kind == "oracle",
            GeneratorRoundAttempt.status == "leased",
        )
        .values(status="failed", detail="oracle lease expired", finished_at=now)
    )


async def oracle_round(
    session: AsyncSession,
    *,
    now: datetime,
    runnable: Callable[[GeneratorRound], bool] = lambda _row: True,
) -> GeneratorRound | None:
    """The oldest scored round waiting on an oracle run that this validator
    can run (``runnable``) and that has no open lease, locked for the caller.

    ``runnable`` is the caller's compatibility test (the round's frozen bench
    version and largest column against this validator's scorer). It is
    applied before anything is locked, so an older round this validator
    cannot run neither hides younger rounds it can, nor is held from the
    validators that can run it.

    Candidates are read unlocked and only the chosen round is locked: a
    result submission locks its ticket and then its round, so locking every
    pending round here (before the lease locks tickets) could deadlock with
    it. A round whose oracle timed out or exhausted its attempts is left for
    the loop to settle rather than leased again."""
    candidates = list(
        await session.scalars(
            select(GeneratorRound)
            .options(*_SCAN_DEFERRED)
            .where(
                GeneratorRound.status == "scored",
                _open_frontier(STATE_ORACLE_PENDING),
                GeneratorRound.dataset_sha256.is_not(None),
            )
            .order_by(GeneratorRound.round_index)
        )
    )
    indexes = [row.round_index for row in candidates if runnable(row)]
    for index in indexes:
        row = await session.scalar(
            select(GeneratorRound)
            .options(*_SCAN_DEFERRED)
            .where(
                GeneratorRound.round_index == index,
                GeneratorRound.status == "scored",
                _open_frontier(STATE_ORACLE_PENDING),
            )
            .with_for_update(skip_locked=True)
        )
        if row is None or await live_oracle_lease(session, row, now=now) is not None:
            continue
        if (
            settle_reason(row, now=now) is not None
            or escalation_expired(row, now=now) is not None
        ):
            continue
        await close_stale_oracle_attempts(session, row, now=now)
        return row
    return None


def oracle_column_cases(row: GeneratorRound) -> int:
    """The largest column an oracle run decodes: what the validator's scorer
    must be able to run, exactly as for a panel run. Read from the scored
    packs (the oracle scans defer ``column_cases``), falling back to the
    frozen ``cases_per_column``. The oracle's total case count may exceed it:
    each column runs on its own, so only the per-column bound applies."""
    sizes = [
        len(column["pack"].get("cases") or ())
        for column in (row.score or {}).get("columns", ())
    ]
    return max(sizes, default=row.cases_per_column or 0)


def is_oracle_run(row: GeneratorRound, agent_id: UUID) -> bool:
    """A result or dataset read on ``row`` from ``agent_id`` is the frontier
    oracle's (or, in the escalation phase, the escalation agent's): the round
    is scored and waiting on that run."""
    frontier = row.frontier or {}
    return (
        row.status == "scored"
        and frontier.get("state") == STATE_ORACLE_PENDING
        and oracle_run_agent(frontier) == str(agent_id)
    )


def note_oracle_lease(row: GeneratorRound) -> None:
    frontier = dict(row.frontier or {})
    if in_escalation(frontier):
        escalation = dict(frontier["escalation"])
        escalation["attempts"] = int(escalation.get("attempts") or 0) + 1
        frontier["escalation"] = escalation
    else:
        frontier["oracle_attempts"] = int(frontier.get("oracle_attempts") or 0) + 1
    row.frontier = frontier


def validate_oracle_results(
    row: GeneratorRound, results: Sequence[GeneratorRoundCaseResult]
) -> None:
    expected = oracle_run_cases(row.frontier or {})
    got = [r.case_id for r in results]
    if len(got) != len(set(got)) or set(got) != set(expected):
        raise FrontierRejected(
            "oracle results must cover exactly the round's frontier candidates"
            if not in_escalation(row.frontier)
            else "escalation results must cover exactly the selected cases"
        )
    if all(r.fault for r in results):
        # Like a panel run that faulted every case, this is an infrastructure
        # failure, not an answer: refused, so the lease fails and the oracle
        # runs again instead of forfeiting the round's frontier credit.
        raise FrontierRejected(
            "every oracle case faulted; hand the lease back as an infrastructure "
            "failure"
        )


def oracle_inference_refusal(outage: GrantOutage | None) -> str | None:
    """Why an oracle run's answers cannot stand as a witness, or None.

    An unknown grant (``None``) or a run with no chat calls is never refused
    here: absent evidence is not an outage. Only failed calls count, so a
    call still in flight when the run reports never tips the share."""
    if outage is None or outage.chat_requests == 0:
        return None
    share = outage.failed_requests / outage.chat_requests
    if share < ORACLE_MAX_FAILED_CHAT_FRACTION:
        return None
    codes = ", ".join(
        f"{code}={count}"
        for code, count in sorted(
            outage.failure_codes.items(), key=lambda item: (-item[1], item[0])
        )
    )
    return (
        f"oracle inference failed upstream on {outage.failed_requests} of "
        f"{outage.chat_requests} chat calls ({codes}); hand the lease back as "
        "an infrastructure failure"
    )


async def oracle_lease_inference_refusal(
    session: AsyncSession, ticket: ValidatorTicket
) -> str | None:
    """:func:`oracle_inference_refusal` for the grant of ``ticket``'s lease.

    The grant is resolved on the ``inference_grants_ticket_lease`` key, so it
    is exactly this run's, never a neighbouring lease's. Read-only."""
    deadline = ticket.deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    grant_id = await session.scalar(
        select(InferenceGrant.grant_id).where(
            InferenceGrant.agent_id == ticket.agent_id,
            InferenceGrant.bench_version == ticket.bench_version,
            InferenceGrant.validator_hotkey == ticket.validator_hotkey,
            InferenceGrant.ticket_deadline == deadline,
        )
    )
    if grant_id is None:
        return None
    return oracle_inference_refusal(await grant_outage(session, grant_id))


def _columns_of(case_ids: Iterable[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for case_id in case_ids:
        column_id = case_id.split("/", 1)[0]
        out.setdefault(column_id, []).append(case_id)
    return out


def _case_fingerprints(row: GeneratorRound) -> dict[str, str]:
    # Each attestation is bound to the content it was earned on (the case's
    # fingerprint in the scored round), as the Go contract's FrontierCases.
    return {
        case["case_id"]: case["fingerprint"]
        for column in (row.score or {}).get("columns", ())
        for case in column["pack"].get("cases", ())
    }


def _column_sizes(row: GeneratorRound) -> dict[str, int]:
    """Each scored column's non-duplicate case count."""
    out: dict[str, int] = {}
    for column in (row.score or {}).get("columns", ()):
        pack = column["pack"]
        if pack.get("status") != "scored":
            continue
        out[column["column_id"]] = len(pack.get("cases") or ()) - int(
            pack.get("duplicate") or 0
        )
    return out


def _open_audits(row: GeneratorRound, frontier: dict[str, Any], now: datetime) -> None:
    """Draw each attested column's audit sample and move to ``audit_pending``
    (settle-ready when there is nothing to audit)."""
    audits: dict[str, Any] = {}
    n = int(frontier.get("audit_cases") or 0)
    for column_id, case_ids in (frontier.get("attested") or {}).items():
        if n <= 0:
            continue
        audits[column_id] = {
            "sample": frontier_audit_sample(row.seed or "", column_id, case_ids, n),
            "status": AUDIT_PENDING,
            "invalid_case_ids": [],
            "reason": None,
            "actor": None,
            "ruled_at": None,
        }
    frontier.update(
        {
            "state": STATE_AUDIT_PENDING,
            "audits": audits,
            "audit_opened_at": _iso(now),
        }
    )


def _attest(frontier: dict[str, Any], passed: Iterable[str]) -> dict[str, list[str]]:
    """Every attested case so far plus ``passed``, per column, in candidate
    order."""
    order = {case_id: i for i, case_id in enumerate(frontier["candidates"])}
    already = [c for ids in (frontier.get("attested") or {}).values() for c in ids]
    merged = set(already) | set(passed)
    return _columns_of(sorted(merged, key=order.__getitem__))


def record_oracle_results(
    row: GeneratorRound,
    results: Sequence[GeneratorRoundCaseResult],
    *,
    validator_hotkey: str,
    run_id: str,
    signature: str,
    now: datetime,
) -> None:
    """Store the open run's results and attest the cases it passed.

    The oracle's run attests at the round's ``solve_threshold``. With
    escalation on and oracle-failed cases selected (:func:`select_escalation`)
    the lane then waits on the escalation run; otherwise, and after the
    escalation run, the sampled audits open over every attested case (or the
    lane is settle-ready when there is nothing to audit)."""
    validate_oracle_results(row, results)
    run = {
        "validator_hotkey": validator_hotkey,
        "run_id": run_id,
        "signature": signature,
        "recorded_at": _iso(now),
        "results": [r.model_dump(mode="json") for r in results],
    }
    frontier = dict(row.frontier or {})
    fingerprints = _case_fingerprints(row)
    if in_escalation(frontier):
        escalation = dict(frontier["escalation"])
        threshold = float(escalation["solve_threshold"])
        passed = [
            r.case_id for r in results if not r.fault and float(r.score) >= threshold
        ]
        escalation.update({**run, "passed": passed, "outcome": "reported"})
        frontier["escalation"] = escalation
        frontier["attested"] = _attest(frontier, passed)
        frontier["attested_fingerprints"] = {
            **(frontier.get("attested_fingerprints") or {}),
            **{case_id: fingerprints[case_id] for case_id in passed},
        }
        _open_audits(row, frontier, now)
        row.frontier = frontier
        return

    threshold = float(frontier.get("solve_threshold") or 0.5)
    passed = [r.case_id for r in results if not r.fault and float(r.score) >= threshold]
    frontier.update(
        {
            "oracle": run,
            "attested": _attest({**frontier, "attested": {}}, passed),
            "attested_fingerprints": {
                case_id: fingerprints[case_id] for case_id in passed
            },
        }
    )
    policy = frontier.get("escalation_policy")
    if policy:
        # A faulted case is the scorer's failure, not the oracle's answer:
        # only answered-and-failed cases are escalated.
        failed = [
            r.case_id for r in results if not r.fault and float(r.score) < threshold
        ]
        seated = {str(slot.get("agent_id")) for slot in row.panel or ()}
        selected: list[str]
        skipped: dict[str, str]
        if policy["agent_id"] in seated:
            # It already answered these cases on the panel; a second run
            # would only re-roll its own failed attempt.
            outcome = "escalation agent sat on the round panel"
            selected = []
            skipped = dict.fromkeys(_columns_of(failed), outcome)
        else:
            selected, skipped = select_escalation(
                round_seed=row.seed or "",
                failed=failed,
                column_sizes=_column_sizes(row),
                max_cases=int(policy["max_cases"]),
                max_cases_per_column=int(policy["max_cases_per_column"]),
                max_failed_fraction=float(policy["max_failed_fraction"]),
            )
            outcome = "pending" if selected else "nothing selected"
        frontier["escalation"] = {
            "agent_id": policy["agent_id"],
            "solve_threshold": float(policy["solve_threshold"]),
            "oracle_failed": len(failed),
            "selected": selected,
            "skipped_columns": skipped,
            "attempts": 0,
            "created_at": _iso(now),
            "passed": [],
            "outcome": outcome,
        }
        if selected:
            frontier["phase"] = PHASE_ESCALATION
            row.frontier = frontier
            return
    _open_audits(row, frontier, now)
    row.frontier = frontier


def escalation_expired(row: GeneratorRound, *, now: datetime) -> str | None:
    """Why the lane's escalation run should be given up, or None. It gets the
    oracle's own budget: ``frontier_oracle_timeout_hours`` from its start and
    ``MAX_ORACLE_ATTEMPTS`` leases."""
    frontier = row.frontier or {}
    if not in_escalation(frontier):
        return None
    escalation = frontier["escalation"]
    created = _parse(escalation.get("created_at")) or now
    timeout = timedelta(hours=int(frontier.get("oracle_timeout_hours") or 12))
    if now - created >= timeout:
        return "escalation timed out"
    if int(escalation.get("attempts") or 0) >= MAX_ORACLE_ATTEMPTS:
        return "escalation attempts exhausted"
    return None


def close_escalation(row: GeneratorRound, *, reason: str, now: datetime) -> None:
    """Give up an escalation run that never reported. It adds nothing, and the
    oracle's own attestations go to audit as if escalation were off."""
    frontier = dict(row.frontier or {})
    if not in_escalation(frontier):
        return
    escalation = dict(frontier["escalation"])
    escalation["outcome"] = reason
    frontier["escalation"] = escalation
    _open_audits(row, frontier, now)
    row.frontier = frontier


def rule_audit(
    row: GeneratorRound,
    *,
    column_id: str,
    valid: bool,
    invalid_case_ids: Sequence[str],
    reason: str,
    actor: str,
    now: datetime,
) -> dict[str, Any]:
    """Record an operator's ruling on one column's audit sample."""
    frontier = dict(row.frontier or {})
    if frontier.get("state") != STATE_AUDIT_PENDING:
        raise FrontierRejected(
            f"round {row.round_index} has no open frontier audit "
            f"(state {frontier.get('state')!r})"
        )
    audits = {k: dict(v) for k, v in (frontier.get("audits") or {}).items()}
    audit = audits.get(column_id)
    if audit is None:
        raise FrontierRejected(f"column {column_id} has no frontier audit")
    if audit["status"] != AUDIT_PENDING:
        raise FrontierRejected(f"column {column_id} audit is already {audit['status']}")
    invalid = list(dict.fromkeys(invalid_case_ids))
    if valid and invalid:
        raise FrontierRejected("a valid ruling names no invalid cases")
    if not valid and (not invalid or not set(invalid) <= set(audit["sample"])):
        raise FrontierRejected(
            "an invalid ruling names one or more cases from the audit sample"
        )
    audit.update(
        {
            "status": AUDIT_PASSED if valid else AUDIT_FAILED,
            "invalid_case_ids": invalid,
            "reason": reason,
            "actor": actor,
            "ruled_at": _iso(now),
        }
    )
    audits[column_id] = audit
    frontier["audits"] = audits
    row.frontier = frontier
    return audit


def settle_reason(
    row: GeneratorRound, *, now: datetime, oracle_running: bool = False
) -> str | None:
    """Why the round's frontier lane should settle now, or None to wait.

    While an oracle lease is running the lane waits for it, past the timeout
    and the attempt cap alike: its ticket TTL bounds the wait, and settling
    under it would throw away a run that may yet deliver."""
    frontier = row.frontier or {}
    state = frontier.get("state")
    if in_escalation(frontier):
        # The oracle reported: an escalation that never reports opens the
        # audits (close_escalation, escalation_expired), never a settle that
        # would skip them.
        return None
    if state == STATE_ORACLE_PENDING:
        if oracle_running:
            return None
        created = _parse(frontier.get("created_at")) or now
        timeout = timedelta(hours=int(frontier.get("oracle_timeout_hours") or 12))
        if now - created >= timeout:
            return "oracle timed out"
        if int(frontier.get("oracle_attempts") or 0) >= MAX_ORACLE_ATTEMPTS:
            return "oracle attempts exhausted"
        return None
    if state != STATE_AUDIT_PENDING:
        return None
    audits = (frontier.get("audits") or {}).values()
    if all(audit["status"] != AUDIT_PENDING for audit in audits):
        return "audits ruled" if frontier.get("audits") else "no audit required"
    opened = _parse(frontier.get("audit_opened_at")) or now
    grace = timedelta(hours=int(frontier.get("audit_grace_hours") or 24))
    if now - opened >= grace:
        return "audit grace elapsed"
    return None


async def settle_frontier(
    session: AsyncSession,
    row: GeneratorRound,
    *,
    params: dict[str, Any],
    reason: str,
    now: datetime,
) -> dict[str, list[str]]:
    """Apply the round's surviving attestations and close its frontier lane.

    A column whose audit failed applies none. ``params`` is the round's own
    ``score_params()``. Returns the case ids applied per column."""
    frontier = dict(row.frontier or {})
    if frontier.get("state") == STATE_SETTLED:
        return dict(frontier.get("applied") or {})
    if frontier.get("state") == STATE_ORACLE_PENDING:
        # An operator settle can land under a running oracle lease: expire it
        # (revoking its inference) and close its attempt, so the validator
        # stops spending on a run whose result would be refused.
        from ditto.api_server.generator_rounds import expire_round_tickets

        await expire_round_tickets(
            session, row, now=now, detail=f"frontier settled: {reason}"[:500]
        )
    if in_escalation(frontier):
        # An operator settle during the escalation run gives it up as a
        # timeout would: the oracle's own attestations draw their audit
        # samples first, so none is credited without one, and then settle as
        # a settle with audits still pending does.
        close_escalation(row, reason=f"frontier settled: {reason}"[:500], now=now)
        frontier = dict(row.frontier or {})
    attested: dict[str, list[str]] = (
        frontier.get("attested") or {}
        if frontier.get("state") == STATE_AUDIT_PENDING
        else {}
    )
    audits = frontier.get("audits") or {}
    applied = {
        column_id: case_ids
        for column_id, case_ids in attested.items()
        if (audits.get(column_id) or {}).get("status") != AUDIT_FAILED
    }
    score = copy.deepcopy(row.score or {})
    base_columns = copy.deepcopy(score.get("columns", []))
    post_params = {**params, "frontier_weight": frontier.get("frontier_weight", 0.0)}
    frozen = {column["column_id"]: column for column in row.columns or ()}
    for column in score.get("columns", []):
        case_ids = applied.get(column["column_id"])
        if not case_ids:
            continue
        before = column["pack"]
        bound = frontier.get("attested_fingerprints") or {}
        after = apply_frontier(
            before,
            [(case_id, bound[case_id]) for case_id in case_ids if case_id in bound],
            post_params,
        )
        column["pack"] = after
        changed = {
            case["case_id"]: case
            for case in after["cases"]
            if case["status"] == CASE_FRONTIER
        }
        if changed:
            verdicts = await session.scalars(
                select(GeneratorRoundCaseVerdict)
                .where(
                    GeneratorRoundCaseVerdict.round_index == row.round_index,
                    GeneratorRoundCaseVerdict.column_id == column["column_id"],
                    GeneratorRoundCaseVerdict.case_id.in_(list(changed)),
                )
                .with_for_update()
            )
            for verdict in verdicts:
                verdict.status = CASE_FRONTIER
                verdict.credit = float(changed[verdict.case_id]["credit"])
        meta = frozen.get(column["column_id"]) or {}
        if not meta.get("public_default") and meta.get("submission_id"):
            sub = await session.get(
                GeneratorSubmission, UUID(meta["submission_id"]), with_for_update=True
            )
            if sub is not None and sub.round_scored_index == row.round_index:
                sub.round_score = float(after["score"])
                sub.updated_at = now
    row.score = score
    frontier.update(
        {
            "state": STATE_SETTLED,
            "applied": applied,
            "base_columns": base_columns,
            "settled_at": _iso(now),
            "settle_reason": reason,
        }
    )
    row.frontier = frontier
    await session.flush()
    return applied


async def advance_frontier_rounds(
    session: AsyncSession,
    *,
    params_for: Any,
    now: datetime,
) -> list[str]:
    """Settle every scored round whose frontier lane is ready. ``params_for``
    resolves a round's own ``score_params()`` (its settings revision)."""
    notes: list[str] = []
    indexes = list(
        await session.scalars(
            select(GeneratorRound.round_index)
            .where(GeneratorRound.status == "scored", _open_frontier())
            .order_by(GeneratorRound.round_index)
        )
    )
    for index in indexes:
        # Ticket locks first, then the round, as a result submission takes
        # them. A submission can lock its ticket just before the deadline and
        # then wait on the round; once the deadline passes the lease no longer
        # counts as live, so a settle here may expire that very ticket. Taking
        # the tickets first makes the sweep wait for the submission instead of
        # holding the round it waits on.
        await lock_oracle_tickets(session, index)
        row = await session.scalar(
            select(GeneratorRound)
            .options(*_SCAN_DEFERRED)
            .where(
                GeneratorRound.round_index == index,
                GeneratorRound.status == "scored",
                _open_frontier(),
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            continue
        running = (row.frontier or {}).get("state") == STATE_ORACLE_PENDING and (
            await live_oracle_lease(session, row, now=now) is not None
        )
        if not running and (expired := escalation_expired(row, now=now)):
            await close_stale_oracle_attempts(session, row, now=now)
            close_escalation(row, reason=expired, now=now)
            notes.append(f"round {row.round_index} frontier {expired}")
        reason = settle_reason(row, now=now, oracle_running=running)
        if reason is None:
            continue
        # One round's failure (a malformed document, an unreadable settings
        # revision) must not roll back the others or the caller's work.
        try:
            async with session.begin_nested():
                applied = await settle_frontier(
                    session, row, params=await params_for(row), reason=reason, now=now
                )
        except Exception as exc:  # noqa: BLE001 - isolate one round
            logger.exception("frontier settle failed for round %s", row.round_index)
            notes.append(
                f"round {row.round_index} frontier settle failed: "
                f"{type(exc).__name__}: {exc}"[:300]
            )
            continue
        cases = sum(len(ids) for ids in applied.values())
        notes.append(
            f"round {row.round_index} frontier settled ({reason}): "
            f"{cases} frontier case(s) on {len(applied)} column(s)"
        )
    return notes


# --- views ------------------------------------------------------------------

# Visible-memory pairs an audit detail lists per case without a search.
MEMORY_LISTING_CAP = 200


def _audit_deadline(frontier: dict[str, Any]) -> datetime | None:
    opened = _parse(frontier.get("audit_opened_at"))
    if opened is None:
        return None
    return opened + timedelta(hours=int(frontier.get("audit_grace_hours") or 24))


def audit_views(row: GeneratorRound) -> list[GeneratorFrontierAuditView]:
    frontier = row.frontier or {}
    frozen = {column["column_id"]: column for column in row.columns or ()}
    attested = frontier.get("attested") or {}
    deadline = _audit_deadline(frontier)
    out: list[GeneratorFrontierAuditView] = []
    for column_id, audit in sorted((frontier.get("audits") or {}).items()):
        meta = frozen.get(column_id) or {}
        out.append(
            GeneratorFrontierAuditView(
                round_index=row.round_index,
                column_id=column_id,
                submission_id=(
                    UUID(meta["submission_id"]) if meta.get("submission_id") else None
                ),
                miner_hotkey=None
                if meta.get("public_default")
                else meta.get("miner_hotkey"),
                status=audit["status"],
                sample=list(audit["sample"]),
                attested_cases=len(attested.get(column_id, ())),
                invalid_case_ids=list(audit.get("invalid_case_ids") or ()),
                reason=audit.get("reason"),
                actor=audit.get("actor"),
                ruled_at=_parse(audit.get("ruled_at")),
                audit_deadline=deadline if audit["status"] == AUDIT_PENDING else None,
            )
        )
    return out


def _column_scores(columns: Iterable[dict[str, Any]]) -> dict[str, float]:
    return {column["column_id"]: float(column["pack"]["score"]) for column in columns}


def _escalation_view(
    frontier: dict[str, Any], timeout: timedelta
) -> GeneratorFrontierEscalationView | None:
    escalation = frontier.get("escalation")
    if not escalation:
        return None
    created = _parse(escalation.get("created_at"))
    return GeneratorFrontierEscalationView(
        agent_id=UUID(escalation["agent_id"]),
        solve_threshold=float(escalation["solve_threshold"]),
        oracle_failed=int(escalation.get("oracle_failed") or 0),
        selected=list(escalation.get("selected") or ()),
        skipped_columns=dict(escalation.get("skipped_columns") or {}),
        attempts=int(escalation.get("attempts") or 0),
        deadline=(
            created + timeout
            if created is not None and in_escalation(frontier)
            else None
        ),
        outcome=str(escalation.get("outcome") or "pending"),
        validator_hotkey=escalation.get("validator_hotkey"),
        run_id=escalation.get("run_id"),
        recorded_at=_parse(escalation.get("recorded_at")),
        passed=list(escalation.get("passed") or ()),
        results=list(escalation.get("results") or ()),
    )


def frontier_view(row: GeneratorRound) -> GeneratorFrontierView | None:
    frontier = row.frontier
    if not frontier:
        return None
    oracle = frontier.get("oracle")
    oracle_view = None
    if oracle:
        threshold = float(frontier.get("solve_threshold") or 0.5)
        results = list(oracle["results"])
        oracle_view = GeneratorFrontierOracleView(
            validator_hotkey=oracle["validator_hotkey"],
            run_id=oracle["run_id"],
            recorded_at=datetime.fromisoformat(oracle["recorded_at"]),
            passed=sum(
                1 for r in results if not r["fault"] and r["score"] >= threshold
            ),
            failed=sum(1 for r in results if not r["fault"] and r["score"] < threshold),
            faulted=sum(1 for r in results if r["fault"]),
            results=results,
        )
    created = datetime.fromisoformat(frontier["created_at"])
    timeout = timedelta(hours=int(frontier.get("oracle_timeout_hours") or 12))
    pending = frontier.get("state") == STATE_ORACLE_PENDING and not in_escalation(
        frontier
    )
    base = frontier.get("base_columns")
    return GeneratorFrontierView(
        round_index=row.round_index,
        state=frontier["state"],
        oracle_agent_id=UUID(frontier["oracle_agent_id"]),
        frontier_weight=float(frontier["frontier_weight"]),
        audit_cases=int(frontier.get("audit_cases") or 0),
        candidates=list(frontier.get("candidates") or ()),
        oracle_attempts=int(frontier.get("oracle_attempts") or 0),
        oracle_deadline=created + timeout if pending else None,
        oracle=oracle_view,
        escalation=_escalation_view(frontier, timeout),
        attested=dict(frontier.get("attested") or {}),
        audits=audit_views(row),
        applied=frontier.get("applied"),
        column_scores=_column_scores((row.score or {}).get("columns", ())),
        base_column_scores=_column_scores(base) if base is not None else None,
        created_at=created,
        settled_at=_parse(frontier.get("settled_at")),
        settle_reason=frontier.get("settle_reason"),
    )


async def pending_audits(
    session: AsyncSession,
    *,
    include_ruled: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[GeneratorFrontierAuditView], int]:
    """One page of frontier audits awaiting an operator ruling, oldest round
    first, and the total. Only rounds with an open frontier lane are read, so
    the whole queue is small (a few columns per unsettled round)."""
    stmt = (
        select(GeneratorRound)
        .options(*_SCAN_DEFERRED)
        .where(GeneratorRound.status == "scored")
        .order_by(GeneratorRound.round_index)
    )
    stmt = stmt.where(
        _open_frontier() if include_ruled else _open_frontier(STATE_AUDIT_PENDING)
    )
    out: list[GeneratorFrontierAuditView] = []
    for row in await session.scalars(stmt):
        out.extend(
            audit
            for audit in audit_views(row)
            if include_ruled or audit.status == AUDIT_PENDING
        )
    return out[offset : offset + limit], len(out)


def _pairs_by_user(pack: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for wave_index, wave in enumerate(pack.get("memory_waves") or ()):
        for pair in wave.get("pairs") or ():
            out.setdefault(wave.get("user_id") or "", []).append(
                {**pair, "wave": wave_index}
            )
    return out


def _pair_view(pair: dict[str, Any]) -> GeneratorFrontierMemoryPair:
    return GeneratorFrontierMemoryPair(
        pair_id=pair["pair_id"],
        timestamp=pair.get("timestamp", ""),
        wave=int(pair["wave"]),
        prompt=pair.get("prompt", ""),
        response=pair.get("response", ""),
    )


def audit_case_views(
    row: GeneratorRound,
    dataset: bytes,
    *,
    column_id: str,
    case_ids: Sequence[str],
    query: str | None = None,
) -> list[GeneratorFrontierCaseView]:
    """The audited cases as the generator wrote them, decoded from the
    round's stored dataset (``challenge.EncodeRoundDataset``)."""
    document = json.loads(dataset)
    pack = next(
        (
            column["pack"]
            for column in document.get("columns", ())
            if column["column_id"] == column_id
        ),
        None,
    )
    if pack is None:
        raise FrontierRejected(f"column {column_id} is not in the round dataset")
    by_user = _pairs_by_user(pack)
    verdicts = {
        case["case_id"]: case
        for column in (row.score or {}).get("columns", ())
        if column["column_id"] == column_id
        for case in column["pack"].get("cases", ())
    }
    oracle = {
        r["case_id"]: r
        for r in ((row.frontier or {}).get("oracle") or {}).get("results", ())
    }
    cases = {case["id"]: case for case in pack.get("cases", ())}
    needle = (query or "").strip().lower()
    out: list[GeneratorFrontierCaseView] = []
    for round_case_id in case_ids:
        case = cases.get(round_case_id.split("/", 1)[-1])
        if case is None:
            raise FrontierRejected(f"case {round_case_id} is not in its pack")
        run_after = int(case.get("run_after_wave") or 0)
        visible = [
            pair
            for pair in by_user.get(case.get("user_id") or "", ())
            if pair["wave"] <= run_after
        ]
        evidence_ids = set(case.get("evidence_pair_ids") or ())
        matches = [
            pair
            for pair in visible
            if not needle
            or needle in pair.get("prompt", "").lower()
            or needle in pair.get("response", "").lower()
        ]
        verdict = verdicts.get(round_case_id) or {}
        oracle_result = oracle.get(round_case_id)
        out.append(
            GeneratorFrontierCaseView(
                case_id=round_case_id,
                question=case.get("question", ""),
                expected_answer=case.get("expected_answer", ""),
                answer_kind=case.get("answer_kind"),
                answer_items=list(case.get("answer_items") or ()),
                accept_any=list(case.get("accept_any") or ()),
                distractor_answers=list(case.get("distractor_answers") or ()),
                forbidden_answer=case.get("forbidden_answer"),
                dump_guard=list(case.get("dump_guard") or ()),
                user_id=case.get("user_id") or "",
                run_after_wave=run_after,
                evidence=[
                    _pair_view(pair)
                    for pair in visible
                    if pair["pair_id"] in evidence_ids
                ],
                visible_pairs=len(visible),
                memory_matches=[
                    _pair_view(pair) for pair in matches[:MEMORY_LISTING_CAP]
                ],
                memory_truncated=len(matches) > MEMORY_LISTING_CAP,
                panel_mean_score=verdict.get("mean_score"),
                panel_solvers=verdict.get("solvers"),
                panel_size=verdict.get("panel_size"),
                oracle_score=oracle_result["score"] if oracle_result else None,
                oracle_fault=oracle_result["fault"] if oracle_result else None,
            )
        )
    return out
