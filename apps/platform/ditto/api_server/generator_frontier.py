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
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_models.generator_round_validator import GeneratorRoundCaseResult
from ditto.api_models.generator_rounds import (
    GeneratorFrontierAuditView,
    GeneratorFrontierCaseView,
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
    ValidatorHeartbeat,
    ValidatorTicket,
)

FRONTIER_AUDIT_DOMAIN = b"dittobench-frontier-audit/v1"
FRONTIER_DOCUMENT_VERSION = 1
# Oracle leases that end without results (failed, or expired) before the
# round settles with no attestations, so a broken oracle cannot pin a round.
MAX_ORACLE_ATTEMPTS = 4

STATE_ORACLE_PENDING = "oracle_pending"
STATE_AUDIT_PENDING = "audit_pending"
STATE_SETTLED = "settled"

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


def apply_frontier(
    pack: dict[str, Any], attested: Iterable[str], params: dict[str, Any]
) -> dict[str, Any]:
    """The pack score ``challenge.ScoreCases`` gives with ``attested`` as its
    ``FrontierCaseIDs``, from the score it gave without them.

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
        if case["status"] != CASE_UNSOLVED or case["case_id"] not in wanted:
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
    }
    return True


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


async def oracle_round(
    session: AsyncSession, *, now: datetime
) -> GeneratorRound | None:
    """The oldest scored round waiting on an oracle run with no open lease,
    locked for the caller."""
    rows = await session.scalars(
        select(GeneratorRound)
        .options(*_SCAN_DEFERRED)
        .where(
            GeneratorRound.status == "scored",
            _open_frontier(STATE_ORACLE_PENDING),
            GeneratorRound.dataset_sha256.is_not(None),
        )
        .order_by(GeneratorRound.round_index)
        .with_for_update(skip_locked=True)
    )
    for row in rows:
        if await live_oracle_lease(session, row, now=now) is not None:
            continue
        return row
    return None


def is_oracle_run(row: GeneratorRound, agent_id: UUID) -> bool:
    """A result or dataset read on ``row`` from ``agent_id`` is the frontier
    oracle's: the round is scored and waiting on its oracle."""
    frontier = row.frontier or {}
    return (
        row.status == "scored"
        and frontier.get("state") == STATE_ORACLE_PENDING
        and frontier.get("oracle_agent_id") == str(agent_id)
    )


def note_oracle_lease(row: GeneratorRound) -> None:
    frontier = dict(row.frontier or {})
    frontier["oracle_attempts"] = int(frontier.get("oracle_attempts") or 0) + 1
    row.frontier = frontier


def validate_oracle_results(
    row: GeneratorRound, results: Sequence[GeneratorRoundCaseResult]
) -> None:
    expected = list((row.frontier or {}).get("candidates", ()))
    got = [r.case_id for r in results]
    if len(got) != len(set(got)) or set(got) != set(expected):
        raise FrontierRejected(
            "oracle results must cover exactly the round's frontier candidates"
        )


def _columns_of(case_ids: Iterable[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for case_id in case_ids:
        column_id = case_id.split("/", 1)[0]
        out.setdefault(column_id, []).append(case_id)
    return out


def record_oracle_results(
    row: GeneratorRound,
    results: Sequence[GeneratorRoundCaseResult],
    *,
    validator_hotkey: str,
    run_id: str,
    signature: str,
    now: datetime,
) -> None:
    """Store the oracle's results, attest the cases it passed, and open the
    sampled audits (or settle-ready state when there is nothing to audit)."""
    validate_oracle_results(row, results)
    frontier = dict(row.frontier or {})
    threshold = float(frontier.get("solve_threshold") or 0.5)
    passed = [r.case_id for r in results if not r.fault and float(r.score) >= threshold]
    order = {case_id: i for i, case_id in enumerate(frontier["candidates"])}
    attested = _columns_of(sorted(passed, key=order.__getitem__))
    audits: dict[str, Any] = {}
    n = int(frontier.get("audit_cases") or 0)
    for column_id, case_ids in attested.items():
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
            "oracle": {
                "validator_hotkey": validator_hotkey,
                "run_id": run_id,
                "signature": signature,
                "recorded_at": _iso(now),
                "results": [r.model_dump(mode="json") for r in results],
            },
            "attested": attested,
            "audits": audits,
            "audit_opened_at": _iso(now),
        }
    )
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


def settle_reason(row: GeneratorRound, *, now: datetime) -> str | None:
    """Why the round's frontier lane should settle now, or None to wait."""
    frontier = row.frontier or {}
    state = frontier.get("state")
    if state == STATE_ORACLE_PENDING:
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
        after = apply_frontier(before, case_ids, post_params)
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
    rows = await session.scalars(
        select(GeneratorRound)
        .options(*_SCAN_DEFERRED)
        .where(GeneratorRound.status == "scored", _open_frontier())
        .order_by(GeneratorRound.round_index)
        .with_for_update(skip_locked=True)
    )
    for row in rows:
        reason = settle_reason(row, now=now)
        if reason is None:
            continue
        applied = await settle_frontier(
            session, row, params=await params_for(row), reason=reason, now=now
        )
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
    pending = frontier.get("state") == STATE_ORACLE_PENDING
    base = frontier.get("base_columns")
    return GeneratorFrontierView(
        round_index=row.round_index,
        state=frontier["state"],
        oracle_agent_id=UUID(frontier["oracle_agent_id"]),
        frontier_weight=float(frontier["frontier_weight"]),
        audit_cases=int(frontier.get("audit_cases") or 0),
        candidates=list(frontier.get("candidates") or ()),
        oracle_attempts=int(frontier.get("oracle_attempts") or 0),
        oracle_deadline=(
            created + timedelta(hours=int(frontier.get("oracle_timeout_hours") or 12))
            if pending
            else None
        ),
        oracle=oracle_view,
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
) -> list[GeneratorFrontierAuditView]:
    """Frontier audits awaiting an operator ruling, oldest round first."""
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
        if len(out) >= offset + limit:
            break
    return out[offset : offset + limit]


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
