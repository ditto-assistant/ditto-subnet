"""Generator-competition rounds: the chain-epoch agent x generator matrix.

A round is keyed on the chain's own epoch counter,
``round_index = SubnetEpochIndex // epochs_per_round``, so every round opens
and closes on a Yuma epoch boundary and lands in the same epoch-pinned ledger
validators fold. The lifecycle (:func:`advance_rounds`, driven by
:class:`GeneratorRoundLoop` and safe to re-run):

1. **Commit.** While round ``r`` runs, Platform draws round ``r + 1``'s 32-byte
   secret and publishes ``commitment = RoundCommitment(r + 1, secret)`` on
   Platform and, when enabled, on-chain (``Commitments.set_commitment``).
2. **Open.** At the first epoch of round ``r``, a round committed before that
   epoch derives ``seed = RoundSeed(r, secret, open_block_hash)`` (the block
   the epoch fired at, unknowable when the secret was committed) and freezes
   its generator columns (best qualified generators, the public default
   generator filling empty counted columns, plus challengers) and its agent
   panel (the top ledger owners plus reference agents).
3. **Generate.** The scorer builds each column generator and runs it on its
   column seed; Platform stores the canonical round dataset.
4. **Run.** Validators lease each panel agent on a ``generator_round`` ticket
   and post per-case results (``endpoints/validator_generator_round.py``).
5. **Close and score.** When every panel agent reported, or the round's last
   epoch ends, the scorer applies ``challenge.ScoreRound`` to the matrix:
   column scores for generators and pairwise-neutral standings for agents.
6. **Reveal.** ``privacy_window_rounds`` after a round opens, its secret,
   seed, and dataset become public so anyone can re-derive every case.

Derivations mirror ``research/dittobench-datagen/challenge/round.go`` and are
pinned by ``challenge/testdata/round_vectors.json``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import secrets
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update

from ditto.api_models.generator import PanelCaseResult
from ditto.api_models.generator_ledger import (
    GeneratorLedgerEntry,
    GeneratorLedgerResponse,
)
from ditto.api_models.generator_round_validator import GeneratorRoundCaseResult
from ditto.api_models.generator_rounds import (
    GeneratorRoundAttemptView,
    GeneratorRoundColumnView,
    GeneratorRoundGenerateResult,
    GeneratorRoundLoopStatus,
    GeneratorRoundResultView,
    GeneratorRoundSchedule,
    GeneratorRoundScoreResult,
    GeneratorRoundStandingView,
    GeneratorRoundSummary,
    GeneratorRoundWork,
    RoundCaseRef,
    RoundMatrix,
    RoundMatrixAgent,
    RoundMatrixColumn,
    RoundSourceColumn,
    onchain_commitment_payload,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.db.models import (
    Agent,
    GeneratorRound,
    GeneratorRoundAttempt,
    GeneratorRoundCaseVerdict,
    GeneratorRoundResult,
    GeneratorSubmission,
    ValidatorTicket,
)
from ditto.db.queries.generator import claimed_fingerprints

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_models.generator import GeneratorCompetitionSettings
    from ditto.api_server.generator_competition import ResolvedSettings

logger = logging.getLogger(__name__)

ROUND_COMMIT_DOMAIN = b"dittobench-generator-round-commit/v1"
ROUND_SEED_DOMAIN = b"dittobench-generator-round-seed/v1"
COLUMN_SEED_DOMAIN = b"dittobench-generator-round-column-seed/v1"
PUBLIC_DEFAULT_OWNER = "public-default"
MAX_COLUMNS = 8
# A generation or scoring lease the scorer never answers is re-leased after
# this long; a round whose generation keeps failing is voided.
ROUND_LEASE_TTL = timedelta(minutes=30)
MAX_GENERATION_ATTEMPTS = 4


# --- derivations ------------------------------------------------------------


def round_index_for(subnet_epoch_index: int, epochs_per_round: int) -> int:
    return subnet_epoch_index // epochs_per_round


def round_first_epoch(round_index: int, epochs_per_round: int) -> int:
    return round_index * epochs_per_round


def round_commitment(round_index: int, secret: bytes) -> str:
    return hashlib.sha256(
        ROUND_COMMIT_DOMAIN + b"\x00" + struct.pack(">Q", round_index) + secret
    ).hexdigest()


def round_seed(round_index: int, secret: bytes, open_block_hash: str) -> str:
    block = bytes.fromhex(open_block_hash.lower().removeprefix("0x"))
    if len(block) != 32:
        raise ValueError("round open block hash must be 32 bytes of hex")
    return hashlib.sha256(
        ROUND_SEED_DOMAIN + b"\x00" + struct.pack(">Q", round_index) + secret + block
    ).hexdigest()


def round_column_seed(seed: str, column_id: str) -> str:
    return hashlib.sha256(
        COLUMN_SEED_DOMAIN + b"\x00" + seed.encode() + b"\x00" + column_id.encode()
    ).hexdigest()


def round_case_id(column_id: str, case_id: str) -> str:
    return f"{column_id}/{case_id}"


# --- chain position ---------------------------------------------------------


@dataclass(frozen=True)
class ChainPosition:
    """The subnet's epoch position at one finalized head."""

    subnet_epoch_index: int
    last_epoch_block: int
    last_epoch_block_hash: str
    tempo: int
    block: int


@dataclass
class RoundActions:
    actions: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        logger.info("generator rounds: %s", message)
        self.actions.append(message)


# --- column and panel selection ---------------------------------------------


async def _qualified_generators(session: AsyncSession) -> list[GeneratorSubmission]:
    return list(
        await session.scalars(
            select(GeneratorSubmission)
            .where(
                GeneratorSubmission.status == "active",
                GeneratorSubmission.qualified_at.is_not(None),
                GeneratorSubmission.public_default.is_(False),
            )
            .order_by(
                GeneratorSubmission.qualified_at, GeneratorSubmission.submitted_at
            )
        )
    )


async def public_default_generator(
    session: AsyncSession,
) -> GeneratorSubmission | None:
    return await session.scalar(
        select(GeneratorSubmission).where(
            GeneratorSubmission.status == "active",
            GeneratorSubmission.public_default.is_(True),
        )
    )


def _column(index: int, row: GeneratorSubmission, *, counted: bool) -> dict[str, Any]:
    return {
        "column_id": f"c{index}",
        "submission_id": str(row.submission_id),
        "miner_hotkey": row.miner_hotkey,
        "owner_key": "" if row.public_default else row.owner_key,
        "source_sha256": row.source_sha256,
        "counted": counted,
        "public_default": row.public_default,
    }


async def select_columns(
    session: AsyncSession, settings: GeneratorCompetitionSettings
) -> list[dict[str, Any]]:
    """Freeze a round's generator columns, counted first.

    Counted columns go to the best qualified generators by latest round
    score, then to qualified generators that have never held a column, then
    to the public default generator (each default column runs on its own
    column seed, so the default can fill several without repeating cases).
    Challenger columns go to qualified generators that have never held a
    column, oldest qualification first, then to the next-best scored ones.
    One column per owner, so a miner with several hotkeys holds one column.
    """
    qualified = await _qualified_generators(session)
    scored = sorted(
        (row for row in qualified if row.round_score is not None),
        key=lambda row: (
            -(row.round_score or 0.0),
            -(row.round_scored_index or 0),
            row.qualified_at or row.submitted_at,
            str(row.submission_id),
        ),
    )
    fresh = [row for row in qualified if row.round_score is None]
    used_owners: set[str] = set()
    used_ids: set[UUID] = set()
    columns: list[dict[str, Any]] = []

    def take(pool: list[GeneratorSubmission], limit: int, *, counted: bool) -> None:
        for row in pool:
            if limit <= 0 or len(columns) >= MAX_COLUMNS:
                return
            if row.owner_key in used_owners or row.submission_id in used_ids:
                continue
            columns.append(_column(len(columns), row, counted=counted))
            used_owners.add(row.owner_key)
            used_ids.add(row.submission_id)
            limit -= 1

    take(scored, settings.counted_columns, counted=True)
    take(fresh, settings.counted_columns - len(columns), counted=True)
    default = await public_default_generator(session)
    if default is not None and default.qualified_at is not None:
        while len(columns) < settings.counted_columns:
            columns.append(_column(len(columns), default, counted=True))
    challengers = settings.challenger_columns
    before = len(columns)
    take(fresh, challengers, counted=False)
    take(scored, challengers - (len(columns) - before), counted=False)
    return columns


async def select_round_panel(
    session: AsyncSession, settings: GeneratorCompetitionSettings
) -> tuple[list[dict[str, Any]], list[str]]:
    """The round's agent panel and any reference agents that were skipped."""
    from ditto.api_server.generator_competition import select_panel

    panel = await select_panel(session, size=settings.panel_size)
    seen = {slot["agent_id"] for slot in panel}
    skipped: list[str] = []
    for agent_id in settings.reference_agent_ids:
        if str(agent_id) in seen:
            continue
        agent = await session.get(Agent, agent_id)
        if agent is None or str(agent.status) not in {"scored", "live"}:
            skipped.append(f"reference agent {agent_id} is not scored or live")
            continue
        if agent.screened_image_sha256 is None:
            skipped.append(f"reference agent {agent_id} has no screened image")
            continue
        panel.append(
            {
                "agent_id": str(agent_id),
                "owner_key": f"reference:{agent_id}",
                "miner_hotkey": agent.miner_hotkey,
                "champion": False,
                "reference": True,
            }
        )
        seen.add(str(agent_id))
    return panel, skipped


# --- lifecycle --------------------------------------------------------------


async def get_round(
    session: AsyncSession, round_index: int, *, for_update: bool = False
) -> GeneratorRound | None:
    stmt = select(GeneratorRound).where(GeneratorRound.round_index == round_index)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


def _commit_row(
    round_index: int,
    *,
    settings: GeneratorCompetitionSettings,
    epoch_index: int,
    now: datetime,
) -> GeneratorRound:
    secret = secrets.token_bytes(32)
    return GeneratorRound(
        round_index=round_index,
        status="committed",
        epochs_per_round=settings.epochs_per_round,
        secret=secret,
        commitment=round_commitment(round_index, secret),
        committed_at=now,
        committed_epoch_index=epoch_index,
        commitment_publication="pending" if settings.onchain_commitment else "disabled",
        lease_attempts=0,
    )


def void_round(row: GeneratorRound, reason: str, *, now: datetime) -> None:
    row.status = "void"
    row.void_reason = reason[:1000]
    row.lease_id = None
    row.lease_expires_at = None
    row.closed_at = row.closed_at or now


async def _result_count(session: AsyncSession, round_index: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(GeneratorRoundResult.round_index == round_index)
        )
        or 0
    )


async def expire_round_tickets(
    session: AsyncSession, row: GeneratorRound, *, now: datetime, detail: str
) -> int:
    """Expire live round tickets of a closed round and close their attempts."""
    if row.dataset_sha256 is None:
        return 0
    from ditto.db.queries.inference import revoke_ticket_inference

    tickets = list(
        await session.scalars(
            select(ValidatorTicket)
            .where(
                ValidatorTicket.purpose == TicketPurpose.GENERATOR_ROUND,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.dataset_sha256 == row.dataset_sha256,
            )
            .with_for_update()
        )
    )
    for ticket in tickets:
        await revoke_ticket_inference(session, ticket=ticket, now=now)
        ticket.status = TicketStatus.EXPIRED
        ticket.retry_after = None
    await session.execute(
        update(GeneratorRoundAttempt)
        .where(
            GeneratorRoundAttempt.round_index == row.round_index,
            GeneratorRoundAttempt.status == "leased",
        )
        .values(status="failed", detail=detail, finished_at=now)
    )
    return len(tickets)


async def close_round(
    session: AsyncSession,
    row: GeneratorRound,
    *,
    settings: GeneratorCompetitionSettings,
    now: datetime,
    reason: str,
) -> str:
    """Move a running round to scoring, or void it with too few results."""
    results = await _result_count(session, row.round_index)
    await expire_round_tickets(session, row, now=now, detail=f"round closed: {reason}")
    row.closed_at = now
    if results < settings.min_panel:
        void_round(
            row,
            f"insufficient panel: {results} agent result(s), "
            f"min_panel {settings.min_panel} ({reason})",
            now=now,
        )
        return "void"
    row.status = "scoring"
    row.lease_id = None
    row.lease_expires_at = None
    return "scoring"


async def advance_rounds(
    session: AsyncSession,
    *,
    resolved: ResolvedSettings,
    position: ChainPosition,
    now: datetime,
) -> RoundActions:
    """Apply every due lifecycle transition for this chain position (caller
    commits). Idempotent: re-running at the same position changes nothing."""
    out = RoundActions()
    settings = resolved.settings
    if settings.mode == "off":
        return out
    per = settings.epochs_per_round
    current = round_index_for(position.subnet_epoch_index, per)

    # 1. Close every earlier round still in flight.
    stale = list(
        await session.scalars(
            select(GeneratorRound)
            .where(
                GeneratorRound.round_index < current,
                GeneratorRound.status.in_(("committed", "generating", "running")),
            )
            .with_for_update()
        )
    )
    for row in stale:
        if row.status == "committed":
            void_round(row, "round passed without opening", now=now)
            out.note(f"round {row.round_index}: void (never opened)")
        elif row.status == "generating":
            void_round(row, "round dataset was not generated before it closed", now=now)
            out.note(f"round {row.round_index}: void (not generated)")
        else:
            outcome = await close_round(
                session, row, settings=settings, now=now, reason="round ended"
            )
            out.note(f"round {row.round_index}: {outcome} at round end")

    # 2. Commit the next round ahead of time.
    if await get_round(session, current + 1) is None:
        session.add(
            _commit_row(
                current + 1,
                settings=settings,
                epoch_index=position.subnet_epoch_index,
                now=now,
            )
        )
        out.note(f"round {current + 1}: committed")

    # 3. Open the current round.
    opening = await get_round(session, current, for_update=True)
    first_epoch = round_first_epoch(current, per)
    if opening is None:
        late = _commit_row(
            current, settings=settings, epoch_index=position.subnet_epoch_index, now=now
        )
        late.commitment_publication = "disabled"
        void_round(late, "not committed before the round opened", now=now)
        session.add(late)
        out.note(f"round {current}: void (not committed in time)")
    elif opening.status == "committed":
        if opening.committed_epoch_index >= first_epoch:
            opening.commitment_publication = (
                opening.commitment_publication
                if opening.commitment_publication == "published"
                else "disabled"
            )
            void_round(opening, "not committed before the round opened", now=now)
            out.note(f"round {current}: void (committed too late)")
        else:
            await open_round(
                session, opening, resolved=resolved, position=position, now=now
            )
            out.note(f"round {current}: {opening.status}")

    # 4. Reveal rounds past the privacy window.
    reveal = await session.scalars(
        select(GeneratorRound)
        .where(
            GeneratorRound.round_index <= current - settings.privacy_window_rounds,
            GeneratorRound.status.in_(("scored", "void")),
            GeneratorRound.revealed_at.is_(None),
        )
        .with_for_update()
    )
    for revealed in reveal:
        revealed.revealed_at = now
        out.note(f"round {revealed.round_index}: revealed")
    await session.flush()
    return out


async def open_round(
    session: AsyncSession,
    row: GeneratorRound,
    *,
    resolved: ResolvedSettings,
    position: ChainPosition,
    now: datetime,
) -> None:
    settings = resolved.settings
    row.open_epoch_index = position.subnet_epoch_index
    row.open_block = position.last_epoch_block
    row.open_block_hash = position.last_epoch_block_hash.lower()
    row.opened_at = now
    # Status and seed move together: the table pins committed <=> no seed.
    row.status = "generating"
    row.seed = round_seed(row.round_index, row.secret, position.last_epoch_block_hash)
    row.settings_revision = resolved.revision
    row.cases_per_column = settings.cases_per_seed
    columns = await select_columns(session, settings)
    panel, skipped = await select_round_panel(session, settings)
    row.columns = columns
    row.panel = panel
    if not any(column["counted"] for column in columns):
        void_round(
            row,
            "no counted generator columns (no qualified generator and no "
            "qualified public default generator)",
            now=now,
        )
        return
    if len(panel) < settings.min_panel:
        void_round(
            row,
            f"panel has {len(panel)} agent(s), min_panel {settings.min_panel}"
            + (f"; {'; '.join(skipped)}" if skipped else ""),
            now=now,
        )
        return
    row.generation = {"skipped_reference_agents": skipped} if skipped else None


# --- scorer work ------------------------------------------------------------


@dataclass(frozen=True)
class RoundLease:
    row: GeneratorRound
    work: GeneratorRoundWork


class RoundResultRejected(ValueError):
    """A scorer round result does not match the round's lease."""


async def _source_columns(
    session: AsyncSession, row: GeneratorRound
) -> list[RoundSourceColumn]:
    out: list[RoundSourceColumn] = []
    for column in row.columns or ():
        sub = await session.get(GeneratorSubmission, UUID(column["submission_id"]))
        if sub is None or sub.source_sha256 != column["source_sha256"]:
            raise RoundResultRejected(
                f"column {column['column_id']} source is no longer available"
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


async def build_matrix(session: AsyncSession, row: GeneratorRound) -> RoundMatrix:
    """The round's agent x generator matrix, in ScoreRound's input shape."""
    cases_by_column: dict[str, list[dict[str, str]]] = row.column_cases or {}
    columns = [
        RoundMatrixColumn(
            column_id=column["column_id"],
            owner_key=column["owner_key"],
            counted=bool(column["counted"]),
            cases=[RoundCaseRef(**ref) for ref in cases_by_column[column["column_id"]]],
        )
        for column in row.columns or ()
        if column["column_id"] in cases_by_column
    ]
    results = {
        str(result.agent_id): result
        for result in await session.scalars(
            select(GeneratorRoundResult).where(
                GeneratorRoundResult.round_index == row.round_index
            )
        )
    }
    agents = [
        RoundMatrixAgent(
            agent_id=slot["agent_id"],
            owner_key=slot["owner_key"],
            reference=bool(slot.get("reference", False)),
            champion=bool(slot.get("champion", False)),
            results=[PanelCaseResult(**r) for r in results[slot["agent_id"]].results],
        )
        for slot in row.panel or ()
        if slot["agent_id"] in results
    ]
    fingerprints = [ref.fingerprint for column in columns for ref in column.cases]
    claimed = await claimed_fingerprints(
        session, fingerprints=fingerprints, exclude_round_index=row.round_index
    )
    return RoundMatrix(columns=columns, agents=agents, claimed_fingerprints=claimed)


async def lease_round_work(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> RoundLease | None:
    """Lease the oldest round needing scorer work (caller commits)."""
    if resolved.settings.mode == "off":
        return None
    row = await session.scalar(
        select(GeneratorRound)
        .where(
            GeneratorRound.status.in_(("generating", "scoring")),
            (GeneratorRound.lease_id.is_(None))
            | (GeneratorRound.lease_expires_at <= now),
        )
        .order_by(GeneratorRound.round_index)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        return None
    if row.status == "generating" and row.lease_attempts >= MAX_GENERATION_ATTEMPTS:
        void_round(row, "round generation lease expired repeatedly", now=now)
        await session.flush()
        return None
    row.lease_id = uuid4()
    row.lease_expires_at = now + ROUND_LEASE_TTL
    row.lease_attempts += 1
    await session.flush()
    if row.status == "generating":
        assert row.seed is not None and row.cases_per_column is not None
        work = GeneratorRoundWork(
            kind="generate",
            lease_id=row.lease_id,
            round_index=row.round_index,
            lease_expires_at=row.lease_expires_at,
            seed=row.seed,
            cases_per_column=row.cases_per_column,
            columns=await _source_columns(session, row),
        )
    else:
        work = GeneratorRoundWork(
            kind="score",
            lease_id=row.lease_id,
            round_index=row.round_index,
            lease_expires_at=row.lease_expires_at,
            params=resolved.settings.score_params(),
            matrix=await build_matrix(session, row),
        )
    return RoundLease(row=row, work=work)


def _check_lease(
    row: GeneratorRound | None, lease_id: UUID, status: str
) -> GeneratorRound:
    if row is None:
        raise RoundResultRejected("round not found")
    if row.status != status or row.lease_id != lease_id:
        raise RoundResultRejected(f"round is {row.status}; lease is not current")
    return row


async def record_generation(
    session: AsyncSession,
    payload: GeneratorRoundGenerateResult,
    *,
    now: datetime,
) -> GeneratorRound:
    """Store a round's generated dataset (caller commits).

    A miner generator that qualified but fails on its round column seed is
    rejected like a failed qualification: generators must work on every seed.
    The public default generator failing only drops its column.
    """
    row = _check_lease(
        await get_round(session, payload.round_index, for_update=True),
        payload.lease_id,
        "generating",
    )
    frozen = {column["column_id"]: column for column in row.columns or ()}
    if {c.column_id for c in payload.columns} != set(frozen):
        raise RoundResultRejected("result columns do not match the round's columns")
    column_cases: dict[str, list[dict[str, str]]] = {}
    summary: list[dict[str, Any]] = []
    for result in payload.columns:
        column = frozen[result.column_id]
        if str(result.submission_id) != column["submission_id"]:
            raise RoundResultRejected(
                f"column {result.column_id} names another submission"
            )
        expected_seed = round_column_seed(row.seed or "", result.column_id)
        ok = bool(result.ok and result.seed is not None and result.seed.valid)
        if ok:
            assert result.seed is not None
            if result.seed.seed != expected_seed:
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
        summary.append(
            {
                "column_id": result.column_id,
                "ok": ok,
                "error": (result.error or result.build.error or "")[:2000] or None,
                "build_ms": result.build.duration_ms,
                "generate_ms": result.seed.duration_ms if result.seed else None,
            }
        )
        if not ok and not column["public_default"]:
            sub = await session.get(
                GeneratorSubmission, UUID(column["submission_id"]), with_for_update=True
            )
            if sub is not None and sub.status == "active":
                sub.status = "rejected"
                sub.status_reason = (
                    f"round_generation_failed: round {row.round_index} column "
                    f"{result.column_id}: {result.error or 'invalid pack'}"
                )[:1000]
                sub.updated_at = now
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
    counted_ok = [cid for cid in column_cases if frozen[cid]["counted"]]
    if (
        not counted_ok
        or payload.dataset_base64 is None
        or payload.dataset_sha256 is None
    ):
        void_round(
            row, "no counted column produced a valid pack on its round seed", now=now
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


async def record_round_score(
    session: AsyncSession, payload: GeneratorRoundScoreResult, *, now: datetime
) -> GeneratorRound:
    """Persist a scored matrix: verdicts, column scores, standings (caller commits)."""
    row = _check_lease(
        await get_round(session, payload.round_index, for_update=True),
        payload.lease_id,
        "scoring",
    )
    frozen = {column["column_id"]: column for column in row.columns or ()}
    expected = set(row.column_cases or {})
    if {c.column_id for c in payload.score.columns} != expected:
        raise RoundResultRejected("score columns do not match the generated columns")
    for column_score in payload.score.columns:
        column = frozen[column_score.column_id]
        for position, verdict in enumerate(column_score.pack.cases):
            session.add(
                GeneratorRoundCaseVerdict(
                    round_index=row.round_index,
                    column_id=column_score.column_id,
                    case_id=verdict.case_id,
                    submission_id=UUID(column["submission_id"]),
                    position=position,
                    fingerprint=verdict.fingerprint,
                    status=verdict.status,
                    panel_size=verdict.panel_size,
                    solvers=verdict.solvers,
                    solver_owners=verdict.solver_owners,
                    mean_score=verdict.mean_score,
                    champion_failed=verdict.champion_failed,
                    credit=verdict.credit,
                )
            )
        if not column["public_default"]:
            sub = await session.get(
                GeneratorSubmission, UUID(column["submission_id"]), with_for_update=True
            )
            if sub is not None and (
                sub.round_scored_index is None
                or sub.round_scored_index <= row.round_index
            ):
                sub.round_score = column_score.pack.score
                sub.round_scored_index = row.round_index
                sub.updated_at = now
    row.score = payload.score.model_dump(mode="json")
    row.scorer_version = payload.scorer_version
    row.scored_at = now
    row.status = "scored"
    row.lease_id = None
    row.lease_expires_at = None
    await session.flush()
    return row


# --- validator panel runs ---------------------------------------------------


async def running_round(session: AsyncSession) -> GeneratorRound | None:
    return await session.scalar(
        select(GeneratorRound)
        .where(GeneratorRound.status == "running")
        .order_by(GeneratorRound.round_index.desc())
        .limit(1)
    )


async def round_for_dataset(
    session: AsyncSession, dataset_sha256: str
) -> GeneratorRound | None:
    return await session.scalar(
        select(GeneratorRound).where(GeneratorRound.dataset_sha256 == dataset_sha256)
    )


async def pending_panel_agents(
    session: AsyncSession, row: GeneratorRound, *, now: datetime
) -> list[UUID]:
    """Panel agents with no accepted result and no live lease, panel order."""
    done = set(
        await session.scalars(
            select(GeneratorRoundResult.agent_id).where(
                GeneratorRoundResult.round_index == row.round_index
            )
        )
    )
    live = set(
        await session.scalars(
            select(GeneratorRoundAttempt.agent_id)
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
                GeneratorRoundAttempt.status == "leased",
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.purpose == TicketPurpose.GENERATOR_ROUND,
                ValidatorTicket.deadline > now,
            )
        )
    )
    return [
        UUID(slot["agent_id"])
        for slot in row.panel or ()
        if UUID(slot["agent_id"]) not in done and UUID(slot["agent_id"]) not in live
    ]


def round_case_ids(row: GeneratorRound) -> list[str]:
    return [
        ref["case_id"] for refs in (row.column_cases or {}).values() for ref in refs
    ]


class RoundSubmissionRejected(ValueError):
    """A validator's round results do not match the round's dataset."""


def validate_round_results(
    row: GeneratorRound, results: list[GeneratorRoundCaseResult]
) -> None:
    expected = round_case_ids(row)
    got = [r.case_id for r in results]
    if len(got) != len(set(got)):
        raise RoundSubmissionRejected("round results repeat a case")
    if set(got) != set(expected):
        raise RoundSubmissionRejected(
            "round results must cover exactly the round dataset's cases"
        )


async def record_panel_result(
    session: AsyncSession,
    row: GeneratorRound,
    *,
    agent_id: UUID,
    validator_hotkey: str,
    ticket_deadline: datetime,
    run_id: str,
    results: list[GeneratorRoundCaseResult],
    signature: str,
    settings: GeneratorCompetitionSettings,
    now: datetime,
) -> bool:
    """Store one agent's results; ``False`` when the agent already reported.
    Closes the round early once every panel agent reported."""
    existing = await session.get(GeneratorRoundResult, (row.round_index, agent_id))
    attempt = await session.get(
        GeneratorRoundAttempt,
        (row.round_index, agent_id, validator_hotkey, ticket_deadline),
    )
    if attempt is not None:
        attempt.status = "completed"
        attempt.finished_at = now
    if existing is not None:
        return False
    session.add(
        GeneratorRoundResult(
            round_index=row.round_index,
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
            ticket_deadline=ticket_deadline,
            run_id=run_id,
            results=[r.model_dump(mode="json") for r in results],
            answered=sum(1 for r in results if not r.fault),
            faulted=sum(1 for r in results if r.fault),
            signature=signature,
            recorded_at=now,
        )
    )
    await session.flush()
    if await _result_count(session, row.round_index) >= len(row.panel or ()):
        await close_round(
            session,
            row,
            settings=settings,
            now=now,
            reason="every panel agent reported",
        )
    return True


# --- views ------------------------------------------------------------------


def _revealed(row: GeneratorRound) -> bool:
    return row.revealed_at is not None


async def round_summary(
    session: AsyncSession, row: GeneratorRound, *, operator: bool = False
) -> GeneratorRoundSummary:
    revealed = _revealed(row)
    return GeneratorRoundSummary(
        round_index=row.round_index,
        status=row.status,  # type: ignore[arg-type]
        epochs_per_round=row.epochs_per_round,
        commitment=row.commitment,
        commitment_publication=row.commitment_publication,  # type: ignore[arg-type]
        committed_at=row.committed_at,
        open_epoch_index=row.open_epoch_index,
        open_block=row.open_block,
        open_block_hash=row.open_block_hash,
        opened_at=row.opened_at,
        column_count=len(row.columns or ()),
        panel_size=len(row.panel or ()),
        results=await _result_count(session, row.round_index),
        closed_at=row.closed_at,
        scored_at=row.scored_at,
        void_reason=row.void_reason,
        revealed_at=row.revealed_at,
        secret_hex=row.secret.hex() if revealed else None,
        seed=row.seed if revealed else None,
        dataset_sha256=row.dataset_sha256 if (revealed or operator) else None,
    )


def column_views(row: GeneratorRound) -> list[GeneratorRoundColumnView]:
    scores = {
        column["column_id"]: column["pack"]
        for column in (row.score or {}).get("columns", ())
    }
    generated = {
        column["column_id"]: bool(column.get("ok"))
        for column in (row.generation or {}).get("columns", ())
    }
    out: list[GeneratorRoundColumnView] = []
    for column in row.columns or ():
        pack = scores.get(column["column_id"])
        out.append(
            GeneratorRoundColumnView(
                column_id=column["column_id"],
                submission_id=UUID(column["submission_id"]),
                miner_hotkey=None
                if column["public_default"]
                else column["miner_hotkey"],
                counted=bool(column["counted"]),
                public_default=bool(column["public_default"]),
                generated=generated.get(column["column_id"]),
                score=pack["score"] if pack else None,
                discriminating=pack["discriminating"] if pack else None,
                trivial=pack["trivial"] if pack else None,
                unsolved=pack["unsolved"] if pack else None,
                duplicate=pack["duplicate"] if pack else None,
            )
        )
    return out


def standing_views(row: GeneratorRound) -> list[GeneratorRoundStandingView]:
    reference = {slot["agent_id"] for slot in row.panel or () if slot.get("reference")}
    return [
        GeneratorRoundStandingView(
            agent_id=UUID(agent["agent_id"]),
            rank=agent["rank"],
            pairwise_score=agent["pairwise_score"],
            wins=agent["wins"],
            ties=agent["ties"],
            losses=agent["losses"],
            opponents=agent["opponents"],
            common_mean=agent["common_mean"],
            common_cases=agent["common_cases"],
            reference=agent["agent_id"] in reference,
        )
        for agent in (row.score or {}).get("agents", ())
    ]


async def attempt_views(
    session: AsyncSession, round_index: int
) -> list[GeneratorRoundAttemptView]:
    rows = await session.scalars(
        select(GeneratorRoundAttempt)
        .where(GeneratorRoundAttempt.round_index == round_index)
        .order_by(GeneratorRoundAttempt.issued_at)
    )
    return [
        GeneratorRoundAttemptView(
            agent_id=a.agent_id,
            validator_hotkey=a.validator_hotkey,
            ticket_deadline=a.ticket_deadline,
            slot_id=a.slot_id,
            status=a.status,  # type: ignore[arg-type]
            detail=a.detail,
            issued_at=a.issued_at,
            finished_at=a.finished_at,
        )
        for a in rows
    ]


async def result_views(
    session: AsyncSession, round_index: int
) -> list[GeneratorRoundResultView]:
    rows = await session.scalars(
        select(GeneratorRoundResult)
        .where(GeneratorRoundResult.round_index == round_index)
        .order_by(GeneratorRoundResult.recorded_at)
    )
    out = []
    for r in rows:
        answered = [c["score"] for c in r.results if not c.get("fault")]
        out.append(
            GeneratorRoundResultView(
                agent_id=r.agent_id,
                validator_hotkey=r.validator_hotkey,
                run_id=r.run_id,
                answered=r.answered,
                faulted=r.faulted,
                mean_score=(sum(answered) / len(answered)) if answered else None,
                recorded_at=r.recorded_at,
            )
        )
    return out


async def list_rounds(
    session: AsyncSession, *, limit: int = 20, offset: int = 0
) -> list[GeneratorRound]:
    return list(
        await session.scalars(
            select(GeneratorRound)
            .order_by(GeneratorRound.round_index.desc())
            .limit(limit)
            .offset(offset)
        )
    )


async def round_counts_by_status(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(GeneratorRound.status, func.count()).group_by(GeneratorRound.status)
    )
    return {status: int(count) for status, count in rows}


# --- ledger -----------------------------------------------------------------


async def build_round_ledger(
    session: AsyncSession, *, resolved: ResolvedSettings, now: datetime
) -> GeneratorLedgerResponse:
    """Each hotkey's latest round column score within the freshness window.

    Shadow only: every entry is ``weight_eligible=False``. The public default
    generator and rejected generators never appear; ``mode=off`` serves an
    empty ledger, which folds to zero generator emission.
    """
    settings = resolved.settings
    if settings.mode == "off":
        return GeneratorLedgerResponse(mode="off", window_days=settings.window_days)
    since = now - timedelta(days=settings.window_days)
    latest = (
        select(
            GeneratorRoundCaseVerdict.submission_id,
            func.max(GeneratorRoundCaseVerdict.round_index).label("round_index"),
        )
        .group_by(GeneratorRoundCaseVerdict.submission_id)
        .subquery()
    )
    rows = await session.execute(
        select(GeneratorSubmission, GeneratorRound)
        .join(latest, latest.c.submission_id == GeneratorSubmission.submission_id)
        .join(GeneratorRound, GeneratorRound.round_index == latest.c.round_index)
        .where(
            GeneratorSubmission.public_default.is_(False),
            GeneratorSubmission.status != "rejected",
            GeneratorRound.status == "scored",
            GeneratorRound.scored_at >= since,
        )
    )
    best: dict[str, tuple[GeneratorSubmission, GeneratorRound]] = {}
    for sub, rnd in rows.tuples():
        prior = best.get(sub.miner_hotkey)
        if prior is None or rnd.round_index > prior[1].round_index:
            best[sub.miner_hotkey] = (sub, rnd)
    entries: list[GeneratorLedgerEntry] = []
    for sub, rnd in best.values():
        column = next(
            (
                c
                for c in (rnd.score or {}).get("columns", ())
                if any(
                    col["column_id"] == c["column_id"]
                    and col["submission_id"] == str(sub.submission_id)
                    for col in rnd.columns or ()
                )
            ),
            None,
        )
        if column is None:
            continue
        entries.append(
            GeneratorLedgerEntry(
                miner_hotkey=sub.miner_hotkey,
                submission_id=sub.submission_id,
                round_index=rnd.round_index,
                weight_eligible=False,
                score=float(column["pack"]["score"]),
                discriminating_cases=int(column["pack"]["discriminating"]),
                first_seen=sub.submitted_at,
                scored_at=rnd.scored_at or now,
            )
        )
    entries.sort(key=lambda e: (-e.score, e.first_seen, e.miner_hotkey))
    return GeneratorLedgerResponse(
        entries=entries,
        count=len(entries),
        window_days=settings.window_days,
        mode=settings.mode,
        generated_at=now,
    )


# --- driver -----------------------------------------------------------------


CommitmentPublisher = Callable[[bytes], Awaitable[None]]


class GeneratorRoundLoop:
    """Drive round transitions from the chain's epoch counter.

    One loop per platform role. Every tick reads the subnet's epoch schedule
    at a finalized head, applies :func:`advance_rounds` in one transaction,
    then publishes pending commitments outside it. A chain read failure
    changes nothing: rounds never advance on a guess.
    """

    def __init__(
        self,
        *,
        app_state: Any,
        session_maker: async_sessionmaker,
        interval_seconds: float = 60.0,
    ) -> None:
        self._app_state = app_state
        self._session_maker = session_maker
        self._interval = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.schedule: GeneratorRoundSchedule | None = None
        self.status = GeneratorRoundLoopStatus(
            last_tick_at=None,
            last_success_at=None,
            last_error=None,
            last_actions=[],
            ticks=0,
        )

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="generator-round-loop")

    async def aclose(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive any tick
                logger.exception("generator round loop tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)

    async def read_position(self) -> ChainPosition:
        chain = getattr(self._app_state, "chain", None)
        if chain is None:
            raise RuntimeError("chain client is unavailable")
        netuid = self._app_state.config.chain.netuid
        schedule = await chain.read_epoch_schedule(netuid)
        block_hash = await chain.get_finalized_block_hash(schedule.last_epoch_block)
        return ChainPosition(
            subnet_epoch_index=schedule.subnet_epoch_index,
            last_epoch_block=schedule.last_epoch_block,
            last_epoch_block_hash=block_hash,
            tempo=schedule.tempo,
            block=schedule.block,
        )

    async def tick(self, *, now: datetime | None = None) -> RoundActions:
        from ditto.api_server.generator_competition import load_settings

        now = now or datetime.now(UTC)
        actions = RoundActions()
        error: str | None = None
        try:
            async with self._session_maker() as session:
                resolved = await load_settings(session)
            per = resolved.settings.epochs_per_round
            if resolved.settings.mode == "off":
                self.schedule = GeneratorRoundSchedule(
                    mode="off", epochs_per_round=per, observed_at=now
                )
                return actions
            position = await self.read_position()
            current = round_index_for(position.subnet_epoch_index, per)
            self.schedule = GeneratorRoundSchedule(
                mode=resolved.settings.mode,
                epochs_per_round=per,
                subnet_epoch_index=position.subnet_epoch_index,
                current_round=current,
                next_round_epoch=round_first_epoch(current + 1, per),
                tempo=position.tempo,
                block=position.block,
                observed_at=now,
            )
            async with self._session_maker() as session, session.begin():
                actions = await advance_rounds(
                    session, resolved=resolved, position=position, now=now
                )
            await self.publish_commitments(now=now)
            return actions
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            if self.schedule is not None:
                self.schedule = self.schedule.model_copy(update={"last_error": error})
            raise
        finally:
            self.status = GeneratorRoundLoopStatus(
                last_tick_at=now,
                last_success_at=now if error is None else self.status.last_success_at,
                last_error=error,
                last_actions=actions.actions[-20:],
                ticks=self.status.ticks + 1,
            )

    async def publish_commitments(self, *, now: datetime) -> None:
        """Publish every pending commitment on-chain, best effort.

        The Platform record is authoritative; the on-chain copy is a public
        timestamp. A failure is recorded and can be retried from Backroom.
        """
        chain = getattr(self._app_state, "chain", None)
        async with self._session_maker() as session:
            pending = list(
                await session.scalars(
                    select(GeneratorRound)
                    .where(GeneratorRound.commitment_publication == "pending")
                    .order_by(GeneratorRound.round_index)
                )
            )
        for row in pending:
            outcome, error = "published", None
            try:
                if chain is None or not hasattr(chain, "set_commitment"):
                    raise RuntimeError("chain client cannot publish commitments")
                await chain.set_commitment(
                    onchain_commitment_payload(row.round_index, row.commitment)
                )
            except Exception as exc:  # noqa: BLE001 - recorded for operators
                outcome, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                logger.warning(
                    "generator round %s commitment publication failed: %s",
                    row.round_index,
                    error,
                )
            async with self._session_maker() as session, session.begin():
                locked = await get_round(session, row.round_index, for_update=True)
                if locked is not None and locked.commitment_publication == "pending":
                    locked.commitment_publication = outcome
                    locked.commitment_error = error
                    locked.commitment_published_at = now if error is None else None
