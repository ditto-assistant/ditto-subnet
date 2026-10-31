"""Generator-competition rounds: the chain-epoch agent x generator matrix.

A round is keyed on the chain's own epoch counter: it opens at a recorded
``first_epoch_index`` and spans ``epochs_per_round`` subnet epochs, so every
round opens and closes on a Yuma epoch boundary and lands in the same
epoch-pinned ledger validators fold. Round indexes continue from the latest
round (:func:`round_clock`), so a cadence change never moves the index space
below rounds that already ran. The lifecycle (:func:`advance_rounds`, driven by
:class:`GeneratorRoundLoop` and safe to re-run):

1. **Commit.** While round ``r`` runs, Platform draws round ``r + 1``'s 32-byte
   secret and publishes ``commitment = RoundCommitment(r + 1, secret)`` on
   Platform. When ``onchain_commitment`` is enabled, Platform elects up to
   :data:`ROUND_COMMITMENT_PUBLISHERS` fresh validators (a stable per-round
   order) to publish ``dgr1:{r + 1}:{commitment}`` with
   ``Commitments.set_commitment`` from their own Pylon identity
   (``endpoints/validator_generator_round.py``). This is best effort: a round
   opens whether or not any validator confirmed the on-chain copy.
2. **Open.** At the first epoch of round ``r``, a round committed before that
   epoch derives ``seed = RoundSeed(r, secret, open_block_hash)`` (the block
   the epoch fired at, unknowable when the secret was committed) and freezes
   its generator columns (best qualified generators, the public default
   generator filling empty counted columns, plus challengers), its agent
   panel (the top ledger owners plus reference agents), and its bench
   contract (:func:`choose_round_bench_version`). Every later step of the
   round runs at that frozen version, whatever activates meanwhile. When the
   only thing missing is validator capacity (heartbeats absent, stale, or
   mid-update), the round stays committed and later ticks of its opening
   epoch retry for :data:`ROUND_OPEN_CAPACITY_GRACE`; only then is it void,
   with an ``infra:`` reason.
3. **Generate.** The scorer builds each column generator and runs it on its
   column seed; Platform stores the canonical round dataset.
4. **Run.** Validators lease each panel agent on a ``generator_round`` ticket
   and post per-case results (``endpoints/validator_generator_round.py``).
   A panel agent that becomes ineligible mid-round is dropped (never leased
   or scored again; its seat is not refilled), and the round is void once
   fewer than ``min_panel`` agents remain seated.
5. **Close and score.** When every seated panel agent reported, or the
   round's last epoch ends, the scorer applies ``challenge.ScoreRound`` to the
   matrix: column scores for generators and pairwise-neutral standings for
   agents. A round with fewer than ``min_panel`` results that answered at
   least one case is void instead (see :class:`PanelTally`).
6. **Reveal.** ``privacy_window_rounds`` subnet epochs after a round opens
   (and once it is scored or void), its secret, seed, and dataset become
   public so anyone can re-derive every case. The window is counted in
   epochs, not in rounds of the current cadence, so it keeps the same
   wall-clock length when ``epochs_per_round`` changes.

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, func, select, true, update
from sqlalchemy.orm import defer

from ditto.api_models.generator import GeneratorScorerFault, PanelCaseResult
from ditto.api_models.generator_ledger import (
    GeneratorLedgerEntry,
    GeneratorLedgerResponse,
)
from ditto.api_models.generator_round_validator import (
    GENERATOR_ROUND_BENCH_VERSIONS,
    LEGACY_ROUND_BENCH_VERSION,
    LEGACY_ROUND_COLUMN_CASES,
    LEGACY_SCORER_ROUND_BENCH_VERSIONS,
    GeneratorRoundCaseResult,
    GeneratorRoundCommitmentJobResponse,
    GeneratorRoundCommitmentReportRequest,
    GeneratorRoundCommitmentReportResponse,
    accepted_grading_versions,
    round_commitment_payload,
)
from ditto.api_models.generator_rounds import (
    GeneratorRoundAttemptView,
    GeneratorRoundColumnView,
    GeneratorRoundCommitmentPublicationView,
    GeneratorRoundGenerateResult,
    GeneratorRoundLoopStatus,
    GeneratorRoundPoint,
    GeneratorRoundResultView,
    GeneratorRoundSchedule,
    GeneratorRoundScoreResult,
    GeneratorRoundStandingView,
    GeneratorRoundSummary,
    GeneratorRoundWork,
    GeneratorSubmissionRoundView,
    RoundCaseRef,
    RoundMatrix,
    RoundMatrixAgent,
    RoundMatrixColumn,
    RoundSourceColumn,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator_capabilities import ValidatorCapabilities
from ditto.api_server.generator_competition import scorer_request_error
from ditto.api_server.generator_scorer_capability import (
    ScorerCapability,
    grading_versions,
    live_scorer,
    version_refusal,
)
from ditto.api_server.generator_scorer_capability import (
    refusal as capability_refusal,
)
from ditto.db.models import (
    Agent,
    BannedHotkey,
    GeneratorRound,
    GeneratorRoundAttempt,
    GeneratorRoundCaseVerdict,
    GeneratorRoundCommitmentPublication,
    GeneratorRoundResult,
    GeneratorSubmission,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import (
    active_bench_version,
    heartbeat_supports_version,
)
from ditto.db.queries.generator import claimed_fingerprints

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ditto.api_models.generator import GeneratorCompetitionSettings
    from ditto.api_server.generator_competition import ResolvedSettings
    from ditto.chain.models import EpochSchedule

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
# Round commitment publication by validators (best effort, never gates a
# round): how many confirmed publications a round wants, how long one elected
# validator holds its claim before the next in order may take its place, how
# often an unfinished election widens by one more validator, and how fresh a
# validator's heartbeat must be to be elected.
ROUND_COMMITMENT_PUBLISHERS = 3
COMMITMENT_CLAIM_TTL = timedelta(minutes=10)
COMMITMENT_ESCALATION_INTERVAL = timedelta(minutes=15)
COMMITMENT_HEARTBEAT_FRESHNESS = timedelta(minutes=5)
PUBLISHER_ORDER_DOMAIN = b"dittobench-generator-round-publisher/v1"
# A round whose opening finds validator capacity as its only blocker (no
# capable validator, or too few for the quorum, usually because heartbeats are
# missing, stale or mid-update at that instant) stays committed and is retried
# on each later tick of its opening epoch for this long before it is voided.
# Two heartbeat freshness windows (5 minutes each): an idle validator
# heartbeats once per scoring sweep, and a fleet that restarts onto a new
# stack is back well within that. The seed binds the opening epoch's boundary
# block, which does not move inside the epoch, so a deferred round opens on
# exactly the seed its commitment promised.
ROUND_OPEN_CAPACITY_GRACE = timedelta(minutes=10)
OPEN_DEFERRED_KEY = "open_deferred"


# --- derivations ------------------------------------------------------------


@dataclass(frozen=True)
class RoundClock:
    """The round a subnet epoch falls in: its index, the epoch it opens at,
    and how many epochs it spans."""

    current: int
    first_epoch: int
    epochs_per_round: int

    @property
    def next_round_epoch(self) -> int:
        return self.first_epoch + self.epochs_per_round


def derive_round_clock(
    subnet_epoch_index: int,
    epochs_per_round: int,
    anchor: tuple[int, int, int] | None,
) -> RoundClock:
    """Place ``subnet_epoch_index`` on the round schedule.

    ``anchor`` is ``(round_index, first_epoch_index, epochs_per_round)`` of
    the latest round that opens at or before the epoch. Inside that round's
    span the epoch belongs to it; past it, rounds of the current
    ``epochs_per_round`` follow back to back from where it ended, one index
    each. Without any round yet, the index is ``epoch // epochs_per_round``
    (the original keying, which every round before ``first_epoch_index``
    followed). Changing the cadence therefore only changes how long the next
    rounds are: indexes keep increasing, so rounds that already ran stay
    "earlier" for closing, revealing and newest-first reads.
    """
    if anchor is None:
        current = subnet_epoch_index // epochs_per_round
        return RoundClock(current, current * epochs_per_round, epochs_per_round)
    index, first, span = anchor
    end = first + span
    if subnet_epoch_index < end:
        return RoundClock(index, first, span)
    skipped = (subnet_epoch_index - end) // epochs_per_round
    return RoundClock(
        index + 1 + skipped, end + skipped * epochs_per_round, epochs_per_round
    )


async def round_clock(
    session: AsyncSession, subnet_epoch_index: int, epochs_per_round: int
) -> RoundClock:
    """:func:`derive_round_clock` anchored on the stored rounds."""
    anchor = (
        await session.execute(
            select(
                GeneratorRound.round_index,
                GeneratorRound.first_epoch_index,
                GeneratorRound.epochs_per_round,
            )
            .where(GeneratorRound.first_epoch_index <= subnet_epoch_index)
            .order_by(GeneratorRound.round_index.desc())
            .limit(1)
        )
    ).first()
    return derive_round_clock(
        subnet_epoch_index,
        epochs_per_round,
        None if anchor is None else (int(anchor[0]), int(anchor[1]), int(anchor[2])),
    )


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


# A panel agent the round lane can lease: a finalized agent with a screened
# image whose miner hotkey is not banned. Anything else (banned, rejected,
# quarantined, an ``ath_pending_review`` hold, a banned hotkey) would sit
# pending until the round ends, so it is skipped when the panel freezes and
# dropped from a round that is already running.
ROUND_RUNNABLE_STATUSES = frozenset({"scored", "live"})


async def panel_ineligibility(
    session: AsyncSession, agent_ids: list[str]
) -> dict[str, str]:
    """Why each of ``agent_ids`` cannot run in a round; runnable ids are absent."""
    if not agent_ids:
        return {}
    rows = {
        str(row.agent_id): row
        for row in await session.execute(
            select(
                Agent.agent_id,
                Agent.status,
                Agent.miner_hotkey,
                Agent.screened_image_sha256,
            ).where(Agent.agent_id.in_([UUID(agent_id) for agent_id in agent_ids]))
        )
    }
    hotkeys = {row.miner_hotkey for row in rows.values()}
    banned = (
        set(
            await session.scalars(
                select(BannedHotkey.hotkey).where(BannedHotkey.hotkey.in_(hotkeys))
            )
        )
        if hotkeys
        else set()
    )
    out: dict[str, str] = {}
    for agent_id in agent_ids:
        row = rows.get(agent_id)
        if row is None:
            out[agent_id] = "agent not found"
        elif str(row.status) not in ROUND_RUNNABLE_STATUSES:
            out[agent_id] = f"agent status is {row.status}"
        elif row.miner_hotkey in banned:
            out[agent_id] = "miner hotkey is banned"
        elif row.screened_image_sha256 is None:
            out[agent_id] = "agent has no screened image"
    return out


@dataclass(frozen=True)
class RoundPanel:
    panel: list[dict[str, Any]]
    skipped_references: list[str]
    """Configured reference agents left off the panel, with why."""
    skipped_agents: list[dict[str, Any]]
    """Ranked owners passed over because their agent cannot run, best first."""


async def select_round_panel(
    session: AsyncSession, settings: GeneratorCompetitionSettings
) -> RoundPanel:
    """The round's agent panel: the best ``panel_size`` runnable ledger owners
    plus the runnable reference agents.

    A ranked agent the lane could not lease (:func:`panel_ineligibility`) is
    skipped and the next ranked owner takes its seat, still one agent per
    owner, so a ban landing just before a round opens does not shrink the
    panel. The champion stays the ledger's first owner: when that agent
    cannot run, no panel agent is the champion.
    """
    from ditto.api_server.generator_competition import select_panel

    # A reference agent that also ranks is seated as a reference only, and the
    # next ranked agent takes its ranked seat, so the panel reaches
    # panel_size + references whenever enough agents exist.
    references = {str(agent_id) for agent_id in settings.reference_agent_ids}
    ranked = await select_panel(session, size=None)
    champion = next((slot["agent_id"] for slot in ranked if slot["champion"]), None)
    candidates = [slot for slot in ranked if slot["agent_id"] not in references]
    blocked = await panel_ineligibility(
        session,
        [slot["agent_id"] for slot in candidates] + sorted(references),
    )
    panel: list[dict[str, Any]] = []
    skipped_agents: list[dict[str, Any]] = []
    for slot in candidates:
        if len(panel) >= settings.panel_size:
            break
        reason = blocked.get(slot["agent_id"])
        if reason is not None:
            skipped_agents.append({**slot, "reason": reason})
            continue
        panel.append(slot)
    seen = {slot["agent_id"] for slot in panel}
    skipped: list[str] = []
    for agent_id in settings.reference_agent_ids:
        if str(agent_id) in seen:
            continue
        reason = blocked.get(str(agent_id))
        if reason is not None:
            skipped.append(f"reference agent {agent_id}: {reason}")
            continue
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        panel.append(
            {
                "agent_id": str(agent_id),
                "owner_key": f"reference:{agent_id}",
                "miner_hotkey": agent.miner_hotkey,
                # A champion that is also a reference stays the champion.
                "champion": str(agent_id) == champion,
                "reference": True,
            }
        )
        seen.add(str(agent_id))
    return RoundPanel(
        panel=panel, skipped_references=skipped, skipped_agents=skipped_agents
    )


# --- lifecycle --------------------------------------------------------------


# A round row carries its whole generated dataset (``dataset_bytes``, often
# megabytes) and every column's case refs. Read views never need either, so
# they defer them: loading them on every public poll pulled the dataset through
# the event loop for each request. ``raiseload`` makes a view that does touch
# them fail loudly in tests instead of lazy-loading under the async session.
_READ_VIEW_DEFERRED = (
    defer(GeneratorRound.dataset_bytes, raiseload=True),
    defer(GeneratorRound.column_cases, raiseload=True),
)


async def get_round(
    session: AsyncSession,
    round_index: int,
    *,
    for_update: bool = False,
    read_view: bool = False,
) -> GeneratorRound | None:
    """One round. ``read_view`` skips the dataset and case refs (see above)."""
    stmt = select(GeneratorRound).where(GeneratorRound.round_index == round_index)
    if read_view:
        stmt = stmt.options(*_READ_VIEW_DEFERRED)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


def _commit_row(
    round_index: int,
    *,
    settings: GeneratorCompetitionSettings,
    first_epoch: int,
    epochs_per_round: int,
    epoch_index: int,
    now: datetime,
) -> GeneratorRound:
    secret = secrets.token_bytes(32)
    return GeneratorRound(
        round_index=round_index,
        status="committed",
        epochs_per_round=epochs_per_round,
        first_epoch_index=first_epoch,
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


def open_deferral(row: GeneratorRound) -> dict[str, Any] | None:
    """The capacity deferral a committed round is waiting out, if any (see
    :data:`ROUND_OPEN_CAPACITY_GRACE`)."""
    value = (row.generation or {}).get(OPEN_DEFERRED_KEY)
    return value if isinstance(value, dict) else None


def _void_unopened(row: GeneratorRound, reason: str, *, now: datetime) -> None:
    """Void a round that never opened. One that was waiting out a capacity
    deferral failed for validator capacity, not for anything a generator
    did, so its reason is an ``infra:`` one naming the deferral."""
    deferral = open_deferral(row)
    if deferral is None:
        void_round(row, reason, now=now)
        return
    void_round(
        row,
        f"infra: {reason} while waiting for validator capacity since "
        f"{deferral.get('since')}: {deferral.get('reason')}",
        now=now,
    )


def active_panel(row: GeneratorRound) -> list[dict[str, Any]]:
    """The round's frozen panel minus agents dropped while it ran."""
    return [slot for slot in row.panel or () if not slot.get("dropped_reason")]


@dataclass(frozen=True)
class PanelTally:
    """What the round's still-seated panel reported.

    ``answered`` counts results with at least one unfaulted case. A run that
    faulted every case (an infrastructure failure, not an agent's answer)
    reports nothing a column can be scored against: with only such results
    every case is unsolved, so the round would score every column 0 and wipe
    every generator's ledger. Such a result counts as not reported."""

    seated: int
    reported: int
    answered: int


async def panel_tally(session: AsyncSession, row: GeneratorRound) -> PanelTally:
    rows = await session.execute(
        select(GeneratorRoundResult.agent_id, GeneratorRoundResult.answered).where(
            GeneratorRoundResult.round_index == row.round_index
        )
    )
    return _tally(row, [(str(agent_id), int(answered)) for agent_id, answered in rows])


def _tally(row: GeneratorRound, results: Iterable[tuple[str, int]]) -> PanelTally:
    seated = {slot["agent_id"] for slot in active_panel(row)}
    reported = [answered for agent_id, answered in results if agent_id in seated]
    return PanelTally(
        seated=len(seated),
        reported=len(reported),
        answered=sum(1 for answered in reported if answered > 0),
    )


async def _drop_ineligible(
    session: AsyncSession, row: GeneratorRound, *, now: datetime
) -> dict[str, str]:
    """Drop seated panel agents that can no longer run (caller commits).

    The panel stays frozen: a dropped agent keeps its slot, marked with why
    and when, its live round lease is expired, and it is never leased or
    scored in this round again. Nobody takes its seat mid-round."""
    if row.status != "running":
        return {}
    blocked = await panel_ineligibility(
        session, [slot["agent_id"] for slot in active_panel(row)]
    )
    if not blocked:
        return {}
    row.panel = [
        {
            **slot,
            "dropped_reason": blocked[slot["agent_id"]],
            "dropped_at": now.isoformat(),
        }
        if slot["agent_id"] in blocked and not slot.get("dropped_reason")
        else slot
        for slot in row.panel or ()
    ]
    await expire_round_tickets(
        session,
        row,
        now=now,
        detail="panel agent dropped: no longer eligible",
        agent_ids={UUID(agent_id) for agent_id in blocked},
    )
    for agent_id, reason in blocked.items():
        logger.info(
            "generator round %s: dropped panel agent %s (%s)",
            row.round_index,
            agent_id,
            reason,
        )
    return blocked


async def drop_ineligible_panel_agents(
    session: AsyncSession, row: GeneratorRound, *, now: datetime
) -> list[str]:
    """Drop a running round's panel agents that became ineligible, then void
    the round if fewer than ``min_panel`` remain seated, or close it if every
    remaining agent already reported. Returns one note per change."""
    dropped = await _drop_ineligible(session, row, now=now)
    if not dropped:
        return []
    notes = [
        f"round {row.round_index}: dropped panel agent {agent_id} ({reason})"
        for agent_id, reason in dropped.items()
    ]
    settings = await round_settings(session, row)
    tally = await panel_tally(session, row)
    if tally.seated < settings.min_panel:
        outcome = await close_round(
            session, row, now=now, reason="too few eligible panel agents remain"
        )
        notes.append(f"round {row.round_index}: {outcome} after panel drops")
    elif tally.reported >= tally.seated:
        outcome = await close_round(
            session, row, now=now, reason="every eligible panel agent reported"
        )
        notes.append(f"round {row.round_index}: {outcome} after panel drops")
    return notes


async def expire_round_tickets(
    session: AsyncSession,
    row: GeneratorRound,
    *,
    now: datetime,
    detail: str,
    agent_ids: set[UUID] | None = None,
) -> int:
    """Expire live round tickets of a closed round and close their attempts.

    ``agent_ids`` limits this to those panel agents (a mid-round drop)."""
    if row.dataset_sha256 is None:
        return 0
    only = (
        GeneratorRoundAttempt.agent_id.in_(agent_ids)
        if agent_ids is not None
        else true()
    )
    from ditto.db.queries.inference import revoke_ticket_inference

    # Only tickets this round's own leased attempts name (agent, validator,
    # deadline) are expired; a dataset digest alone does not identify a round.
    tickets = list(
        await session.scalars(
            select(ValidatorTicket)
            .join(
                GeneratorRoundAttempt,
                and_(
                    GeneratorRoundAttempt.round_index == row.round_index,
                    GeneratorRoundAttempt.status == "leased",
                    GeneratorRoundAttempt.agent_id == ValidatorTicket.agent_id,
                    GeneratorRoundAttempt.validator_hotkey
                    == ValidatorTicket.validator_hotkey,
                    GeneratorRoundAttempt.ticket_deadline == ValidatorTicket.deadline,
                ),
            )
            .where(
                ValidatorTicket.purpose == TicketPurpose.GENERATOR_ROUND,
                ValidatorTicket.status == TicketStatus.ISSUED,
                ValidatorTicket.dataset_sha256 == row.dataset_sha256,
                only,
            )
            .with_for_update(of=ValidatorTicket)
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
            only,
        )
        .values(status="failed", detail=detail, finished_at=now)
    )
    return len(tickets)


async def round_settings(
    session: AsyncSession, row: GeneratorRound
) -> GeneratorCompetitionSettings:
    """The policy the round opened under: closing and scoring a frozen round
    never depend on a settings write made after it opened."""
    from ditto.api_server.generator_competition import settings_at

    return await settings_at(session, row.settings_revision)


async def close_round(
    session: AsyncSession,
    row: GeneratorRound,
    *,
    now: datetime,
    reason: str,
) -> str:
    """Move a running round to scoring, or void it.

    Panel agents that became ineligible are dropped first, so their results
    never reach the matrix. The round is void (and so never credits a column
    score) with fewer than ``min_panel`` results from its seated panel
    (``insufficient_panel``), or fewer than ``min_panel`` of those results
    with at least one answered case (``insufficient_answered_results``; see
    :class:`PanelTally`). ``min_panel`` comes from the round's own settings
    revision."""
    settings = await round_settings(session, row)
    await _drop_ineligible(session, row, now=now)
    tally = await panel_tally(session, row)
    dropped = len(row.panel or ()) - tally.seated
    await expire_round_tickets(session, row, now=now, detail=f"round closed: {reason}")
    row.closed_at = now
    note = f"{reason}" + (f"; {dropped} panel agent(s) dropped" if dropped else "")
    if tally.reported < settings.min_panel:
        void_round(
            row,
            f"insufficient_panel: {tally.reported} agent result(s) from "
            f"{tally.seated} eligible panel agent(s), min_panel "
            f"{settings.min_panel} ({note})",
            now=now,
        )
        return "void"
    if tally.answered < settings.min_panel:
        void_round(
            row,
            f"insufficient_answered_results: {tally.answered} of "
            f"{tally.reported} agent result(s) answered at least one case, "
            f"min_panel {settings.min_panel}; every other result faulted every "
            f"case ({note})",
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
    clock = await round_clock(session, position.subnet_epoch_index, per)
    current = clock.current

    # 1. Close every earlier round still in flight. Every multi-round lock in
    # this module is taken in ascending round_index order (see
    # claim_commitment_publication), so a tick and a validator poll that lock
    # overlapping rounds cannot deadlock.
    stale = list(
        await session.scalars(
            select(GeneratorRound)
            .where(
                GeneratorRound.round_index < current,
                GeneratorRound.status.in_(("committed", "generating", "running")),
            )
            .order_by(GeneratorRound.round_index)
            .with_for_update()
        )
    )
    for row in stale:
        if row.status == "committed":
            _void_unopened(row, "round passed without opening", now=now)
            out.note(f"round {row.round_index}: void (never opened)")
        elif row.status == "generating":
            void_round(row, "round dataset was not generated before it closed", now=now)
            out.note(f"round {row.round_index}: void (not generated)")
        else:
            outcome = await close_round(session, row, now=now, reason="round ended")
            out.note(f"round {row.round_index}: {outcome} at round end")

    # 1b. Drop panel agents that became ineligible from the running round.
    running = list(
        await session.scalars(
            select(GeneratorRound)
            .where(
                GeneratorRound.round_index >= current,
                GeneratorRound.status == "running",
            )
            .with_for_update()
        )
    )
    for row in running:
        for note in await drop_ineligible_panel_agents(session, row, now=now):
            out.note(note)

    # 2. Commit the next round ahead of time. It opens where the current
    # round ends and spans the current setting's epochs.
    if await get_round(session, current + 1) is None:
        session.add(
            _commit_row(
                current + 1,
                settings=settings,
                first_epoch=clock.next_round_epoch,
                epochs_per_round=per,
                epoch_index=position.subnet_epoch_index,
                now=now,
            )
        )
        out.note(f"round {current + 1}: committed")

    # 3. Open the current round.
    opening = await get_round(session, current, for_update=True)
    first_epoch = clock.first_epoch if opening is None else opening.first_epoch_index
    if opening is None:
        late = _commit_row(
            current,
            settings=settings,
            first_epoch=clock.first_epoch,
            epochs_per_round=clock.epochs_per_round,
            epoch_index=position.subnet_epoch_index,
            now=now,
        )
        late.commitment_publication = "disabled"
        void_round(late, "not committed before the round opened", now=now)
        session.add(late)
        out.note(f"round {current}: void (not committed in time)")
    elif opening.status == "committed":
        if position.subnet_epoch_index != first_epoch:
            # The seed binds the boundary block of the round's first epoch, and
            # only the latest boundary is observable: a round the loop reaches
            # late cannot be opened on the block its commitment promised.
            _void_unopened(opening, "round opening epoch was missed", now=now)
            out.note(f"round {current}: void (opening epoch missed)")
        elif opening.committed_epoch_index >= first_epoch:
            void_round(opening, "not committed before the round opened", now=now)
            out.note(f"round {current}: void (committed too late)")
        else:
            await open_round(
                session, opening, resolved=resolved, position=position, now=now
            )
            deferral = open_deferral(opening)
            if opening.status == "committed" and deferral is not None:
                out.note(
                    f"round {current}: opening deferred (attempt "
                    f"{deferral.get('attempts')}, until {deferral.get('until')}): "
                    f"{deferral.get('reason')}"
                )
            else:
                out.note(f"round {current}: {opening.status}")

    # 4. Settle the publication of every round that left ``committed``: no
    # validator is asked to publish a commitment once its round opened.
    for settled in await settle_commitment_publications(session):
        out.note(settled)

    # 5. Reveal rounds past the privacy window: ``privacy_window_rounds``
    # subnet epochs after each round's own first epoch. Counting epochs, not
    # round indexes of the current cadence, keeps every round's reveal on its
    # original wall-clock schedule across an ``epochs_per_round`` change.
    reveal = await session.scalars(
        select(GeneratorRound)
        .where(
            GeneratorRound.first_epoch_index
            <= position.subnet_epoch_index - settings.privacy_window_rounds,
            GeneratorRound.status.in_(("scored", "void")),
            GeneratorRound.revealed_at.is_(None),
        )
        .order_by(GeneratorRound.round_index)
        .with_for_update()
    )
    for revealed in reveal:
        revealed.revealed_at = now
        out.note(f"round {revealed.round_index}: revealed")
    await session.flush()
    return out


def round_bench_version(row: GeneratorRound) -> int:
    """The bench contract ``row`` runs at: frozen when it opened, or the legacy
    v13 pin for a round opened before rounds recorded one."""
    if row.bench_version is None:
        return LEGACY_ROUND_BENCH_VERSION
    return int(row.bench_version)


def round_column_capacity(
    heartbeat: ValidatorHeartbeat | None, *, now: datetime, bench_version: int
) -> int | None:
    """The largest round column this validator can run at ``bench_version``,
    or None for none.

    The version gate is the same fresh, identity-matched scorer report agent
    tickets use (:func:`heartbeat_supports_version`). A scorer that does not
    advertise ``generator_round_max_cases`` predates columns above the legacy
    size: its validator refuses a larger job (the old 8 x 32 case bound) and
    its scorer a larger pack, so it gets only rounds of at most
    ``LEGACY_ROUND_COLUMN_CASES`` per column.
    """
    if heartbeat is None or not heartbeat_supports_version(
        heartbeat, now=now, version=bench_version
    ):
        return None
    try:
        capabilities = ValidatorCapabilities.model_validate(heartbeat.capabilities)
    except ValidationError:
        return None
    scorer = capabilities.scorer_benchmarks
    # Round runs carry their own dataset bytes; they do not need the private
    # V13 transport (``private_datasets``), which stays off fleet-wide.
    if not (
        capabilities.ticket_inference
        and capabilities.sandbox_memory_limit
        and scorer is not None
        and scorer.status == "fresh_verified"
        and scorer.generator_rounds
    ):
        return None
    # The scorer's round mode must run this version: a scorer that advertises
    # no ``generator-round-bench-<n>`` runs only the original v13 rounds.
    round_versions = (
        scorer.generator_round_bench_versions
        if scorer.generator_round_bench_versions is not None
        else LEGACY_SCORER_ROUND_BENCH_VERSIONS
    )
    if bench_version not in round_versions:
        return None
    return scorer.generator_round_max_cases or LEGACY_ROUND_COLUMN_CASES


async def round_capable_validators(
    session: AsyncSession, *, bench_version: int, cases_per_column: int, now: datetime
) -> int:
    """Validators a round at ``bench_version`` could be leased to right now.

    The same gates :func:`issue_generator_round_ticket` and the lane apply: a
    fresh scorer for the version that runs rounds of this column size, and,
    for bench 13, a member signing the V13 scorer-cohort authority packet.
    """
    from ditto.api_server.v13_scorer_cohort import pinned_validator_allowed

    count = 0
    for heartbeat in await session.scalars(select(ValidatorHeartbeat)):
        capacity = round_column_capacity(
            heartbeat, now=now, bench_version=bench_version
        )
        if capacity is None or capacity < cases_per_column:
            continue
        if bench_version == 13 and not await pinned_validator_allowed(
            session, hotkey=heartbeat.validator_hotkey, now=now
        ):
            continue
        count += 1
    return count


async def round_contract_capacity(
    session: AsyncSession, *, cases_per_column: int, now: datetime
) -> dict[str, int]:
    """Validators that could run a round of this column size now, per round
    contract (Backroom's round preview)."""
    return {
        str(version): await round_capable_validators(
            session,
            bench_version=version,
            cases_per_column=cases_per_column,
            now=now,
        )
        for version in GENERATOR_ROUND_BENCH_VERSIONS
    }


def round_contract_for(active: int) -> int:
    """The round contract a round would open at under active bench ``active``,
    before validator capacity: the active version when it is a round contract,
    else the newest one below it, else the oldest (the contract rounds have
    always run at). Qualification runs request it, so a generator qualifies on
    the pack grading policies rounds will accept from it."""
    below = [version for version in GENERATOR_ROUND_BENCH_VERSIONS if version <= active]
    return max(below) if below else min(GENERATOR_ROUND_BENCH_VERSIONS)


async def active_round_contract(session: AsyncSession) -> int:
    """:func:`round_contract_for` the active benchmark version."""
    return round_contract_for(await active_bench_version(session))


async def qualification_bench_version(
    session: AsyncSession, scorer: ScorerCapability | None = None
) -> int:
    """The round contract a qualification run requests now: the one rounds
    would open at under the active benchmark version (:func:`round_contract_for`),
    capped to the newest one the polling scorer advertises it grades. A
    scorer that advertises none is v13-only, so it qualifies at v13."""
    contract = await active_round_contract(session)
    supported = [v for v in grading_versions(scorer) if v <= contract]
    return max(supported) if supported else LEGACY_ROUND_BENCH_VERSION


@dataclass(frozen=True)
class RoundContract:
    """The bench contract a round opens at. ``bench_version`` is None when no
    round-capable contract has a validator to run it (the round is void)."""

    active_version: int
    bench_version: int | None
    reason: str | None = None
    previous_version: int = LEGACY_ROUND_BENCH_VERSION
    """The version the newest opened round froze: rounds switch to a newer
    one only at ``min_round_validators``."""
    min_round_validators: int = 1
    scorer_grading_versions: tuple[int, ...] = LEGACY_SCORER_ROUND_BENCH_VERSIONS
    """What the live generator scorer advertises it grades (v13 when none)."""
    capacity_blocked: bool = False
    """With no ``bench_version``: at least one candidate the scorer grades
    was refused only for want of capable validators. That is a fleet gap
    (heartbeats missing, stale or mid-update) that heals on its own, so the
    round defers its opening rather than voiding (see
    :data:`ROUND_OPEN_CAPACITY_GRACE`)."""


async def previous_round_bench_version(session: AsyncSession) -> int:
    """The bench version the newest opened round froze, or the legacy v13
    for a lane whose rounds never recorded one."""
    version = await session.scalar(
        select(GeneratorRound.bench_version)
        .where(GeneratorRound.bench_version.is_not(None))
        .order_by(GeneratorRound.round_index.desc())
        .limit(1)
    )
    return int(version) if version is not None else LEGACY_ROUND_BENCH_VERSION


async def choose_round_bench_version(
    session: AsyncSession,
    *,
    cases_per_column: int,
    now: datetime,
    min_round_validators: int = 1,
) -> RoundContract:
    """Pick the bench contract a round opening now freezes.

    It follows the active benchmark version. When that version has no round
    contract (``GENERATOR_ROUND_BENCH_VERSIONS``: the pack grading policy and
    the scorer's round mode), the live generator scorer does not advertise it
    (``grading_bench_versions``; none advertised is v13-only), or too few
    validators can run a round at it, the round falls back to the newest
    round contract below it that can run, and records why. An active version
    older than every round contract keeps the oldest one, the contract rounds
    have always run at. With no candidate able to run, the round is voided.

    Quorum: a version newer than the one the previous round froze needs
    ``min_round_validators`` capable validators before rounds switch to it,
    mirroring the agent rollout, where a version takes authority only once
    each priority-cohort position holds ``SCORING_QUORUM`` (3) desired-version
    scores, one per distinct validator. Below quorum, rounds stay on the
    previous version. A version at or below the previous one needs one.
    """
    active = await active_bench_version(session)
    previous = await previous_round_bench_version(session)
    scorer = await live_scorer(session, now=now)
    scorer_versions = grading_versions(scorer)
    candidates = sorted(
        (version for version in GENERATOR_ROUND_BENCH_VERSIONS if version <= active),
        reverse=True,
    )
    notes: list[str] = []
    capacity_blocked = False
    if not candidates:
        # An active version older than every round contract (a fresh or
        # restored deployment) keeps the rounds' historical contract.
        candidates = [min(GENERATOR_ROUND_BENCH_VERSIONS)]
        notes.append(
            f"active bench v{active} predates the generator-round contracts; "
            f"rounds run at v{candidates[0]}"
        )
    elif active not in GENERATOR_ROUND_BENCH_VERSIONS:
        supported = ", ".join(f"v{v}" for v in GENERATOR_ROUND_BENCH_VERSIONS)
        notes.append(
            f"active bench v{active} has no generator-round contract (round "
            f"packs grade at {supported}, and the scorer's generator-round-v1 "
            "mode runs only those)"
        )
    for version in candidates:
        refused = version_refusal(scorer, version)
        if refused is not None:
            notes.append(refused)
            continue
        capable = await round_capable_validators(
            session, bench_version=version, cases_per_column=cases_per_column, now=now
        )
        required = min_round_validators if version > previous else 1
        if capable >= required:
            return RoundContract(
                active_version=active,
                bench_version=version,
                reason="; ".join(notes) or None,
                previous_version=previous,
                min_round_validators=min_round_validators,
                scorer_grading_versions=scorer_versions,
            )
        capacity_blocked = True
        if capable == 0:
            notes.append(
                f"no validator can run a {cases_per_column}-case round column "
                f"at bench v{version}"
            )
        else:
            notes.append(
                f"{capable} validator(s) can run a {cases_per_column}-case round "
                f"column at bench v{version}, below min_round_validators "
                f"{required}: rounds stay on v{previous} until a quorum can "
                f"run v{version}"
            )
    return RoundContract(
        active_version=active,
        bench_version=None,
        reason="; ".join(notes),
        previous_version=previous,
        min_round_validators=min_round_validators,
        scorer_grading_versions=scorer_versions,
        capacity_blocked=capacity_blocked,
    )


async def open_round(
    session: AsyncSession,
    row: GeneratorRound,
    *,
    resolved: ResolvedSettings,
    position: ChainPosition,
    now: datetime,
) -> None:
    """Open a committed round at its opening epoch, defer it, or void it.

    The caller has checked that ``position`` is the round's first epoch. When
    validator capacity is the only blocker (:attr:`RoundContract.capacity_blocked`),
    the round stays ``committed`` with an ``open_deferred`` record and the
    next tick retries, for up to :data:`ROUND_OPEN_CAPACITY_GRACE` from the
    first deferred attempt; then it is void with an ``infra:`` reason. Every
    other blocker voids at once, as before.
    """
    settings = resolved.settings
    # Frozen for the round's life: an activation mid-round must not move its
    # panel runs, tickets, or capability routing to another contract. Chosen
    # before the row changes, so a deferred round is left exactly as committed.
    contract = await choose_round_bench_version(
        session,
        cases_per_column=settings.cases_per_seed,
        now=now,
        min_round_validators=settings.min_round_validators,
    )
    deferral = open_deferral(row)
    if contract.bench_version is None and contract.capacity_blocked:
        since = (
            datetime.fromisoformat(deferral["since"])
            if deferral is not None and isinstance(deferral.get("since"), str)
            else now
        )
        until = since + ROUND_OPEN_CAPACITY_GRACE
        if now < until:
            row.generation = {
                **(row.generation or {}),
                OPEN_DEFERRED_KEY: {
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "attempts": int((deferral or {}).get("attempts") or 0) + 1,
                    "last_attempt_at": now.isoformat(),
                    "active_version": contract.active_version,
                    "reason": contract.reason,
                },
            }
            return
    row.open_epoch_index = position.subnet_epoch_index
    row.open_block = position.last_epoch_block
    row.open_block_hash = position.last_epoch_block_hash.lower()
    row.opened_at = now
    # Status and seed move together: the table pins committed <=> no seed.
    row.status = "generating"
    row.seed = round_seed(row.round_index, row.secret, position.last_epoch_block_hash)
    row.settings_revision = resolved.revision
    row.cases_per_column = settings.cases_per_seed
    row.bench_version = contract.bench_version
    columns = await select_columns(session, settings)
    frozen = await select_round_panel(session, settings)
    panel, skipped = frozen.panel, frozen.skipped_references
    row.columns = columns
    row.panel = panel
    # Recorded before any void below, so a round voided for an undersized
    # panel still shows which agents were skipped and why.
    generation: dict[str, Any] = {}
    if deferral is not None:
        # Kept on the opened (or voided) round: it opened late, and why.
        generation[OPEN_DEFERRED_KEY] = deferral
    if skipped:
        generation["skipped_reference_agents"] = skipped
    if frozen.skipped_agents:
        generation["skipped_panel_agents"] = frozen.skipped_agents
    if contract.reason is not None:
        generation["bench_contract"] = {
            "active_version": contract.active_version,
            "bench_version": contract.bench_version,
            "reason": contract.reason,
        }
    row.generation = generation or None
    if contract.bench_version is None:
        reason = (
            f"no generator-round contract can run at active bench "
            f"v{contract.active_version}: {contract.reason}"
        )
        if contract.capacity_blocked:
            # The grace window ran out: validator capacity never recovered.
            reason = (
                f"infra: {reason} (validator capacity did not recover within "
                f"{int(ROUND_OPEN_CAPACITY_GRACE.total_seconds() // 60)} minutes "
                "of the round's opening)"
            )
        void_round(row, reason, now=now)
        return
    refused = capability_refusal(
        await live_scorer(session, now=now),
        settings.cases_per_seed,
        what="the round's cases_per_column",
    )
    if refused is not None:
        void_round(row, f"infra: {refused}", now=now)
        return
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
            + (f"; {'; '.join(skipped)}" if skipped else "")
            + (
                f"; {len(frozen.skipped_agents)} ranked agent(s) skipped as ineligible"
                if frozen.skipped_agents
                else ""
            ),
            now=now,
        )
        return


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
        for slot in active_panel(row)
        if slot["agent_id"] in results
    ]
    fingerprints = [ref.fingerprint for column in columns for ref in column.cases]
    claimed = await claimed_fingerprints(
        session, fingerprints=fingerprints, exclude_round_index=row.round_index
    )
    return RoundMatrix(columns=columns, agents=agents, claimed_fingerprints=claimed)


async def lease_round_work(
    session: AsyncSession,
    *,
    resolved: ResolvedSettings,
    now: datetime,
    capability: ScorerCapability | None = None,
) -> RoundLease | None:
    """Lease the oldest round needing scorer work (caller commits).

    A generating round the polling scorer cannot run (its advertised case
    bounds exclude ``cases_per_column``, or its advertised grading policies
    exclude the round's version; none advertised is v13-only) is voided with
    an infrastructure reason rather than leased to fail.
    """
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
    refused = (
        capability_refusal(
            capability, row.cases_per_column, what="the round's cases_per_column"
        )
        if row.status == "generating" and row.cases_per_column is not None
        else None
    )
    if refused is None and row.status == "generating":
        # The polling scorer must grade the round's version (none advertised
        # is v13-only); generating it anyway would request policies it lacks.
        refused = version_refusal(capability, round_bench_version(row))
    if refused is not None:
        void_round(row, f"infra: {refused}", now=now)
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
            bench_version=round_bench_version(row),
        )
    else:
        work = GeneratorRoundWork(
            kind="score",
            lease_id=row.lease_id,
            round_index=row.round_index,
            lease_expires_at=row.lease_expires_at,
            params=(await round_settings(session, row)).score_params(),
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
        # A scorer older than this round's request refused it for every
        # column alike: an infrastructure fault, never a column's failure.
        _void_for_scorer_fault(
            row,
            kind="unsupported_request",
            detail=refused,
            scorer_version=payload.scorer_version,
            now=now,
        )
        await session.flush()
        return row
    column_cases: dict[str, list[dict[str, str]]] = {}
    summary: list[dict[str, Any]] = []
    bench = round_bench_version(row)
    for result in payload.columns:
        declared = result.seed.grading_bench_version if result.seed else None
        if declared is not None and declared not in accepted_grading_versions(bench):
            # The scorer requested only the policies in the round's window
            # (N-1 and N), so a pack outside them is a scorer inconsistency.
            raise RoundResultRejected(
                f"column {result.column_id} pack grades at v{declared}, which "
                f"a v{bench} round does not accept"
            )
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
        # Recorded only when the round offered more than the baseline policy,
        # so a v13 round's summary keeps its original shape.
        if declared is not None:
            summary[-1]["grading_bench_version"] = declared
        if result.seed is not None and result.seed.legacy_retry:
            summary[-1]["legacy_retry"] = result.seed.legacy_retry[:500]
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


def _void_for_scorer_fault(
    row: GeneratorRound,
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
    row.lease_id = None
    row.lease_expires_at = None
    void_round(
        row,
        f"infra: scorer {scorer_version} could not generate this round "
        f"({kind}): {detail}"[:1000],
        now=now,
    )


async def record_round_fault(
    session: AsyncSession, payload: GeneratorScorerFault, *, now: datetime
) -> GeneratorRound:
    """Apply a scorer-reported fault to a round's current lease (caller commits).

    No column is blamed. A request the scorer cannot serve voids the round
    with an infrastructure reason (re-leasing it to the same scorer cannot
    help); a sandbox failure releases the lease so the round is re-leased,
    bounded by ``MAX_GENERATION_ATTEMPTS``.
    """
    if payload.round_index is None:
        raise RoundResultRejected("round fault names no round")
    row = await get_round(session, payload.round_index, for_update=True)
    if row is None:
        raise RoundResultRejected("round not found")
    if row.lease_id != payload.lease_id or row.status not in ("generating", "scoring"):
        raise RoundResultRejected(f"round is {row.status}; lease is not current")
    if payload.error_kind == "unsupported_request":
        _void_for_scorer_fault(
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


# A panel run must finish before its round closes: closing expires the round's
# tickets and revokes their inference grants, so a run leased minutes before
# the boundary loses inference mid-seed and faults every case. No lease is
# issued once less than ``round_lease_cutoff`` remains.
#
# The cutoff scales with the round's size. Measured lease-to-result times: an
# 80-case round (5 x 16) took about 3.3 minutes; round 25948 (4 valid columns x
# 32 = 128 cases) took 4.3, 4.5, 4.8, 5.0 and 6.0 minutes for its five panel
# runs. That fits ~80 s of fixed work (image fetch, sandbox start, a memory
# seeding per column, dataset checks) plus ~1.5 s per case, with the slowest
# run at ~2.8 s per case all-in. Budgeting 5 minutes plus 4 s per case keeps
# at least a 1.6x margin over that slowest rate:
#
#   cases  cutoff   expected run (fit .. slowest rate)
#   160    20 min   ~5-7.5 min     (5 x 32: the floor applies)
#   350    28 min   ~10-16 min     (5 x 70)
#   400    32 min   ~11-19 min     (5 x 80)
#   640    48 min   ~17-30 min     (8 x 80, the cap)
#
# A one-epoch round (~72 min) opens with ~2.5 minutes of generation, so at 70
# per column it still leases panel runs for ~40 minutes. At the 640-case cap
# only ~20 minutes remain, so a round that large wants epochs_per_round >= 2.
ROUND_LEASE_CUTOFF = timedelta(minutes=20)
"""The cutoff floor: no round is leased with less than this left."""
ROUND_LEASE_OVERHEAD = timedelta(minutes=5)
ROUND_LEASE_SECONDS_PER_CASE = 4.0
CHAIN_BLOCK_SECONDS = 12


def round_lease_cutoff(case_count: int) -> timedelta:
    """Time a round must have left before a panel run of ``case_count`` cases
    is leased: the floor, or the run's budgeted length when that is longer."""
    budget = ROUND_LEASE_OVERHEAD + timedelta(
        seconds=ROUND_LEASE_SECONDS_PER_CASE * max(0, case_count)
    )
    return max(ROUND_LEASE_CUTOFF, budget)


def round_column_cases(row: GeneratorRound) -> int:
    """The largest column in a generated round: what a validator's scorer must
    be able to run. Falls back to the frozen ``cases_per_column``."""
    sizes = [len(refs) for refs in (row.column_cases or {}).values()]
    return max(sizes, default=row.cases_per_column or 0)


def round_seconds_left(row: GeneratorRound, head: EpochSchedule) -> float | None:
    """Estimated seconds until ``row`` closes, at a chain ``head`` read now.

    The round closes when ``SubnetEpochIndex`` reaches the first epoch of the
    next round. The next epoch fires at the chain client's predicted
    ``next_epoch_block``, which honors an owner-scheduled ``PendingEpochAt``
    and the stateful scheduler; each further epoch the round still spans adds
    ``tempo`` blocks. The caller reads ``head`` at lease time,
    so nothing is extrapolated from a stale snapshot. Zero once the head has
    reached the closing epoch; None when the head predates the round.
    """
    closing_epoch = row.first_epoch_index + row.epochs_per_round
    epochs_after_next = closing_epoch - head.subnet_epoch_index - 1
    if head.subnet_epoch_index < row.first_epoch_index:
        return None
    if epochs_after_next < 0:
        # The chain is already past the round's closing boundary.
        return 0.0
    close_block = head.next_epoch_block + epochs_after_next * head.tempo
    return (close_block - head.block) * CHAIN_BLOCK_SECONDS


async def running_round(
    session: AsyncSession, *, for_update: bool = False
) -> GeneratorRound | None:
    stmt = (
        select(GeneratorRound)
        .where(GeneratorRound.status == "running")
        .order_by(GeneratorRound.round_index.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


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
        for slot in active_panel(row)
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
    now: datetime,
) -> bool:
    """Store one agent's results; ``False`` when the agent already reported.
    Closes the round early once every seated panel agent reported."""
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
    tally = await panel_tally(session, row)
    if tally.reported >= tally.seated:
        await close_round(
            session,
            row,
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
    return (await round_summaries(session, [row], operator=operator))[0]


async def round_summaries(
    session: AsyncSession,
    rows: Sequence[GeneratorRound],
    *,
    operator: bool = False,
) -> list[GeneratorRoundSummary]:
    """Summaries for ``rows`` in two queries, whatever their number.

    Each summary needs the round's reported results and confirmed commitment
    publications; reading those per round made a page of ten rounds thirty
    queries deep.
    """
    indexes = [row.round_index for row in rows]
    if not indexes:
        return []
    reported: dict[int, list[tuple[str, int]]] = {index: [] for index in indexes}
    for index, agent_id, answered in await session.execute(
        select(
            GeneratorRoundResult.round_index,
            GeneratorRoundResult.agent_id,
            GeneratorRoundResult.answered,
        ).where(GeneratorRoundResult.round_index.in_(indexes))
    ):
        reported[int(index)].append((str(agent_id), int(answered)))
    confirmed = {
        int(index): int(count)
        for index, count in await session.execute(
            select(GeneratorRoundCommitmentPublication.round_index, func.count())
            .where(
                GeneratorRoundCommitmentPublication.round_index.in_(indexes),
                GeneratorRoundCommitmentPublication.status == "confirmed",
            )
            .group_by(GeneratorRoundCommitmentPublication.round_index)
        )
    }
    return [
        _summary(
            row,
            tally=_tally(row, reported[row.round_index]),
            results=len(reported[row.round_index]),
            confirmations=confirmed.get(row.round_index, 0),
            operator=operator,
        )
        for row in rows
    ]


def _summary(
    row: GeneratorRound,
    *,
    tally: PanelTally,
    results: int,
    confirmations: int,
    operator: bool,
) -> GeneratorRoundSummary:
    revealed = _revealed(row)
    return GeneratorRoundSummary(
        round_index=row.round_index,
        status=row.status,  # type: ignore[arg-type]
        epochs_per_round=row.epochs_per_round,
        commitment=row.commitment,
        commitment_publication=row.commitment_publication,  # type: ignore[arg-type]
        commitment_confirmations=confirmations,
        committed_at=row.committed_at,
        open_epoch_index=row.open_epoch_index,
        open_block=row.open_block,
        open_block_hash=row.open_block_hash,
        opened_at=row.opened_at,
        column_count=len(row.columns or ()),
        panel_size=len(row.panel or ()),
        panel_dropped=len(row.panel or ()) - tally.seated,
        results=results,
        answered_results=tally.answered,
        closed_at=row.closed_at,
        scored_at=row.scored_at,
        void_reason=row.void_reason,
        revealed_at=row.revealed_at,
        secret_hex=row.secret.hex() if revealed else None,
        seed=row.seed if revealed else None,
        dataset_sha256=row.dataset_sha256 if (revealed or operator) else None,
    )


def declared_grading_versions(generation: dict[str, Any] | None) -> dict[str, int]:
    """Each generated column's declared pack grading policy, by column id. A
    column whose pack declared none (every v13 round, and generators that
    predate v14) graded at the baseline, v13."""
    return {
        column["column_id"]: int(
            column.get("grading_bench_version") or LEGACY_ROUND_BENCH_VERSION
        )
        for column in (generation or {}).get("columns", ())
        if column.get("ok")
    }


def column_views(row: GeneratorRound) -> list[GeneratorRoundColumnView]:
    declared = declared_grading_versions(row.generation)
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
                grading_bench_version=declared.get(column["column_id"]),
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


def _result_exclusion(
    result: GeneratorRoundResult, dropped: dict[str, str]
) -> str | None:
    reason = dropped.get(str(result.agent_id))
    if reason is not None:
        return f"agent dropped from the panel: {reason}"
    if result.answered == 0:
        return "faulted every case; counts as not reported"
    return None


async def result_views(
    session: AsyncSession, row: GeneratorRound
) -> list[GeneratorRoundResultView]:
    dropped = {
        slot["agent_id"]: slot["dropped_reason"]
        for slot in row.panel or ()
        if slot.get("dropped_reason")
    }
    rows = await session.scalars(
        select(GeneratorRoundResult)
        .where(GeneratorRoundResult.round_index == row.round_index)
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
                excluded_reason=_result_exclusion(r, dropped),
            )
        )
    return out


async def list_rounds(
    session: AsyncSession, *, limit: int = 20, offset: int = 0
) -> list[GeneratorRound]:
    """A page of rounds for listing: no dataset, case refs, or generation log."""
    return list(
        await session.scalars(
            select(GeneratorRound)
            .options(
                *_READ_VIEW_DEFERRED,
                defer(GeneratorRound.generation, raiseload=True),
            )
            .order_by(GeneratorRound.round_index.desc())
            .limit(limit)
            .offset(offset)
        )
    )


def _column_ranks(row: Any) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """A scored round's column packs by id and each column's rank (1 = best)."""
    packs = {
        column["column_id"]: column["pack"]
        for column in (row.score or {}).get("columns", ())
    }
    scores = [float(pack["score"]) for pack in packs.values()]
    ranks = {
        column_id: 1 + sum(1 for other in scores if other > float(pack["score"]))
        for column_id, pack in packs.items()
    }
    return packs, ranks


async def hotkey_round_points(
    session: AsyncSession, hotkeys: Iterable[str], *, since: datetime, limit: int
) -> dict[str, tuple[int, list[GeneratorRoundPoint]]]:
    """Per hotkey: how many scored rounds since ``since`` held one of its
    generators, and the latest ``limit`` of those columns, oldest first.

    Reads the rounds' column and score documents only (never datasets), one
    pass over the window.
    """
    wanted = set(hotkeys)
    if not wanted:
        return {}
    rows = await session.execute(
        select(
            GeneratorRound.round_index,
            GeneratorRound.columns,
            GeneratorRound.score,
            GeneratorRound.scored_at,
        )
        .where(GeneratorRound.status == "scored", GeneratorRound.scored_at >= since)
        .order_by(GeneratorRound.round_index)
    )
    points: dict[str, list[GeneratorRoundPoint]] = {}
    for row in rows:
        packs, ranks = _column_ranks(row)
        for column in row.columns or ():
            hotkey = column.get("miner_hotkey")
            pack = packs.get(column["column_id"])
            if column.get("public_default") or hotkey not in wanted or pack is None:
                continue
            points.setdefault(hotkey, []).append(
                GeneratorRoundPoint(
                    round_index=row.round_index,
                    submission_id=UUID(column["submission_id"]),
                    score=float(pack["score"]),
                    rank=ranks[column["column_id"]],
                    columns=len(packs),
                    scored_at=row.scored_at,
                )
            )
    return {hotkey: (len(held), held[-limit:]) for hotkey, held in points.items()}


async def submission_round_views(
    session: AsyncSession, submission_id: UUID, *, limit: int = 50
) -> list[GeneratorSubmissionRoundView]:
    """The scored rounds one generator held a column in, newest first."""
    indexes = (
        select(GeneratorRoundCaseVerdict.round_index)
        .where(GeneratorRoundCaseVerdict.submission_id == submission_id)
        .distinct()
        .order_by(GeneratorRoundCaseVerdict.round_index.desc())
        .limit(limit)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(
            GeneratorRound.round_index,
            GeneratorRound.columns,
            GeneratorRound.score,
            GeneratorRound.scored_at,
            GeneratorRound.panel,
            GeneratorRound.generation,
        )
        .where(
            GeneratorRound.status == "scored",
            GeneratorRound.round_index.in_(indexes),
        )
        .order_by(GeneratorRound.round_index.desc())
    )
    out: list[GeneratorSubmissionRoundView] = []
    for row in rows:
        packs, ranks = _column_ranks(row)
        column = next(
            (
                c
                for c in row.columns or ()
                if c["submission_id"] == str(submission_id) and c["column_id"] in packs
            ),
            None,
        )
        if column is None:
            continue
        pack = packs[column["column_id"]]
        out.append(
            GeneratorSubmissionRoundView(
                round_index=row.round_index,
                scored_at=row.scored_at,
                column_id=column["column_id"],
                counted=bool(column.get("counted")),
                score=float(pack["score"]),
                rank=ranks[column["column_id"]],
                columns=len(packs),
                discriminating=int(pack["discriminating"]),
                trivial=int(pack["trivial"]),
                unsolved=int(pack["unsolved"]),
                duplicate=int(pack["duplicate"]),
                panel_size=sum(
                    1 for slot in row.panel or () if not slot.get("dropped_reason")
                ),
                grading_bench_version=declared_grading_versions(row.generation).get(
                    column["column_id"]
                ),
            )
        )
    return out


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

    ``shadow`` marks every entry ``weight_eligible=False``. ``enforce`` marks an
    entry eligible when its generator qualified (a scored round column implies
    it did, but the check is explicit so an unqualified row can never be paid).
    A superseded generator stays eligible: its last round score keeps counting
    until the hotkey's new generator is scored. The public default generator
    and rejected generators never appear; ``mode=off`` serves an empty ledger,
    which folds to zero generator emission. Eligibility alone pays nothing:
    validators pay the generator track only from a governed share.
    """
    settings = resolved.settings
    if settings.mode == "off":
        return GeneratorLedgerResponse(
            count=0, mode="off", window_days=settings.window_days
        )
    since = now - timedelta(days=settings.window_days)
    # Only the columns the fold reads. Loading whole rows pulled every
    # ledgered generator's source archive and, once per submission, its
    # round's full dataset through the event loop on each read; aggregating
    # every verdict ever recorded made the cost grow with the round history.
    first = await session.scalar(
        select(func.min(GeneratorRound.round_index)).where(
            GeneratorRound.status == "scored", GeneratorRound.scored_at >= since
        )
    )
    if first is None:
        return _ledger_response([], settings=settings, now=now)
    # Every scored round from the window's first on: a generator whose latest
    # scored round is older than ``first`` cannot be inside the window, so its
    # latest round is never needed.
    rounds = {
        int(row.round_index): row
        for row in await session.execute(
            select(
                GeneratorRound.round_index,
                GeneratorRound.columns,
                GeneratorRound.score,
                GeneratorRound.scored_at,
            ).where(
                GeneratorRound.status == "scored",
                GeneratorRound.round_index >= first,
            )
        )
    }
    # The latest *scored* round per generator: a void round never credits or
    # replaces a generator's column score, even if it holds verdicts.
    latest = {
        submission_id: int(round_index)
        for submission_id, round_index in await session.execute(
            select(
                GeneratorRoundCaseVerdict.submission_id,
                func.max(GeneratorRoundCaseVerdict.round_index),
            )
            .where(GeneratorRoundCaseVerdict.round_index.in_(list(rounds)))
            .group_by(GeneratorRoundCaseVerdict.submission_id)
        )
    }
    latest = {
        submission_id: index
        for submission_id, index in latest.items()
        if rounds[index].scored_at is not None and rounds[index].scored_at >= since
    }
    subs: Sequence[Any] = (
        (
            await session.execute(
                select(
                    GeneratorSubmission.submission_id,
                    GeneratorSubmission.miner_hotkey,
                    GeneratorSubmission.submitted_at,
                    GeneratorSubmission.qualified_at,
                ).where(
                    GeneratorSubmission.submission_id.in_(list(latest)),
                    GeneratorSubmission.public_default.is_(False),
                    GeneratorSubmission.status != "rejected",
                )
            )
        ).all()
        if latest
        else []
    )
    best: dict[str, tuple[Any, Any]] = {}
    for sub in subs:
        rnd = rounds[latest[sub.submission_id]]
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
                weight_eligible=(
                    settings.mode == "enforce" and sub.qualified_at is not None
                ),
                score=float(column["pack"]["score"]),
                discriminating_cases=int(column["pack"]["discriminating"]),
                first_seen=sub.submitted_at,
                scored_at=rnd.scored_at or now,
            )
        )
    return _ledger_response(entries, settings=settings, now=now)


def _ledger_response(
    entries: list[GeneratorLedgerEntry],
    *,
    settings: GeneratorCompetitionSettings,
    now: datetime,
) -> GeneratorLedgerResponse:
    entries.sort(key=lambda e: (-e.score, e.first_seen, e.miner_hotkey))
    return GeneratorLedgerResponse(
        entries=entries,
        count=len(entries),
        window_days=settings.window_days,
        mode=settings.mode,
        generated_at=now,
    )


# --- driver -----------------------------------------------------------------


class GeneratorRoundLoop:
    """Drive round transitions from the chain's epoch counter.

    One loop per platform role. Every tick reads the subnet's epoch schedule
    at a finalized head and applies :func:`advance_rounds` in one
    transaction. A chain read failure changes nothing: rounds never advance on
    a guess. Platform never publishes a round commitment itself; elected
    validators do (:func:`claim_commitment_publication`).
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
            async with self._session_maker() as session:
                clock = await round_clock(session, position.subnet_epoch_index, per)
            self.schedule = GeneratorRoundSchedule(
                mode=resolved.settings.mode,
                epochs_per_round=per,
                subnet_epoch_index=position.subnet_epoch_index,
                current_round=clock.current,
                next_round_epoch=clock.next_round_epoch,
                tempo=position.tempo,
                block=position.block,
                observed_at=now,
            )
            async with self._session_maker() as session, session.begin():
                actions = await advance_rounds(
                    session, resolved=resolved, position=position, now=now
                )
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


# --- commitment publication by validators -----------------------------------


def publisher_rank_key(round_index: int, validator_hotkey: str) -> str:
    """A validator's place in a round's publisher order (lower goes first).

    Stable per round and unpredictable across rounds, so the same few
    validators are asked for one round while the duty rotates between rounds.
    """
    return hashlib.sha256(
        PUBLISHER_ORDER_DOMAIN
        + b"\x00"
        + struct.pack(">Q", round_index)
        + validator_hotkey.encode()
    ).hexdigest()


def publisher_order(round_index: int, hotkeys: Iterable[str]) -> list[str]:
    return sorted(set(hotkeys), key=lambda h: (publisher_rank_key(round_index, h), h))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def commitment_capable(heartbeat: ValidatorHeartbeat, *, now: datetime) -> bool:
    """A fresh heartbeat advertising the ``round_commitments`` capability."""
    if now - _aware(heartbeat.seen_at) > COMMITMENT_HEARTBEAT_FRESHNESS:
        return False
    try:
        capabilities = ValidatorCapabilities.model_validate(heartbeat.capabilities)
    except ValidationError:
        return False
    return capabilities.round_commitments


async def commitment_capable_hotkeys(
    session: AsyncSession, *, now: datetime
) -> list[str]:
    heartbeats = await session.scalars(
        select(ValidatorHeartbeat).where(
            ValidatorHeartbeat.seen_at >= now - COMMITMENT_HEARTBEAT_FRESHNESS
        )
    )
    return [
        heartbeat.validator_hotkey
        for heartbeat in heartbeats
        if commitment_capable(heartbeat, now=now)
    ]


@dataclass(frozen=True)
class CommitmentElection:
    """Who should publish one round's commitment now."""

    target: int
    confirmed: tuple[str, ...]
    elected: tuple[str, ...]


def elect_commitment_publishers(
    row: GeneratorRound,
    publications: Iterable[GeneratorRoundCommitmentPublication],
    capable: Iterable[str],
    *,
    now: datetime,
    target: int = ROUND_COMMITMENT_PUBLISHERS,
) -> CommitmentElection:
    """Elect the validators that should publish ``row``'s commitment now.

    Confirmed publications count toward ``target``. The remaining places go to
    capable validators in the round's publisher order, skipping any that
    already failed or let a claim expire. A live claim keeps its place. Each
    :data:`COMMITMENT_ESCALATION_INTERVAL` since the first claim without
    reaching ``target`` widens the election by one validator, so a validator
    that is fresh but never answers cannot stall publication. Once ``target``
    confirmed, nobody else is elected.
    """
    publications = list(publications)
    confirmed = tuple(
        sorted(p.validator_hotkey for p in publications if p.status == "confirmed")
    )
    needed = max(0, target - len(confirmed))
    if needed == 0:
        return CommitmentElection(target=target, confirmed=confirmed, elected=())
    excluded = set(confirmed) | {
        p.validator_hotkey
        for p in publications
        if p.status == "failed"
        or (p.status == "claimed" and _aware(p.claim_expires_at) <= now)
    }
    claims = [_aware(p.claimed_at) for p in publications]
    widen = int((now - min(claims)) / COMMITMENT_ESCALATION_INTERVAL) if claims else 0
    candidates = [
        hotkey
        for hotkey in publisher_order(row.round_index, capable)
        if hotkey not in excluded
    ]
    return CommitmentElection(
        target=target,
        confirmed=confirmed,
        elected=tuple(candidates[: needed + widen]),
    )


async def _publications(
    session: AsyncSession, round_index: int, *, for_update: bool = False
) -> list[GeneratorRoundCommitmentPublication]:
    stmt = (
        select(GeneratorRoundCommitmentPublication)
        .where(GeneratorRoundCommitmentPublication.round_index == round_index)
        .order_by(GeneratorRoundCommitmentPublication.claimed_at)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list(await session.scalars(stmt))


async def commitment_confirmations(session: AsyncSession, round_index: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).where(
                GeneratorRoundCommitmentPublication.round_index == round_index,
                GeneratorRoundCommitmentPublication.status == "confirmed",
            )
        )
        or 0
    )


async def claim_commitment_publication(
    session: AsyncSession, *, validator_hotkey: str, now: datetime
) -> GeneratorRoundCommitmentJobResponse | None:
    """Hand ``validator_hotkey`` a commitment to publish if it is elected.

    Only rounds still ``committed`` with publication ``pending`` are offered:
    a commitment published after its round opened proves nothing. The rounds
    are locked so concurrent polls see each other's claims (caller commits).
    """
    rows = list(
        await session.scalars(
            select(GeneratorRound)
            .where(
                GeneratorRound.status == "committed",
                GeneratorRound.commitment_publication == "pending",
            )
            .order_by(GeneratorRound.round_index)
            .with_for_update()
        )
    )
    if not rows:
        return None
    capable = await commitment_capable_hotkeys(session, now=now)
    if validator_hotkey not in capable:
        return None
    for row in rows:
        publications = await _publications(session, row.round_index, for_update=True)
        mine = next(
            (p for p in publications if p.validator_hotkey == validator_hotkey), None
        )
        if mine is not None:
            # Already answered, or a live claim this validator is working on.
            continue
        election = elect_commitment_publishers(row, publications, capable, now=now)
        if validator_hotkey not in election.elected:
            continue
        claim = GeneratorRoundCommitmentPublication(
            round_index=row.round_index,
            validator_hotkey=validator_hotkey,
            commitment=row.commitment,
            status="claimed",
            claimed_at=now,
            claim_expires_at=now + COMMITMENT_CLAIM_TTL,
        )
        session.add(claim)
        await session.flush()
        return GeneratorRoundCommitmentJobResponse(
            round_index=row.round_index,
            commitment=row.commitment,
            payload=round_commitment_payload(row.round_index, row.commitment),
            claim_expires_at=claim.claim_expires_at,
        )
    return None


class CommitmentReportRejected(ValueError):
    """A commitment report names no claim or another commitment."""


async def record_commitment_report(
    session: AsyncSession,
    report: GeneratorRoundCommitmentReportRequest,
    *,
    now: datetime,
) -> GeneratorRoundCommitmentReportResponse:
    """Record one elected validator's publication outcome (caller commits).

    Idempotent: a repeated report, or a ``failed`` report after a
    ``confirmed`` one, changes nothing. A late ``confirmed`` report is still
    recorded (the value is on chain at its block); it moves the round to
    ``published`` only while the round still collects publications.
    """
    row = await get_round(session, report.round_index, for_update=True)
    if row is None:
        raise CommitmentReportRejected("unknown round")
    if report.commitment != row.commitment:
        raise CommitmentReportRejected("report names another commitment")
    publication = await session.get(
        GeneratorRoundCommitmentPublication,
        (report.round_index, report.validator_hotkey),
        with_for_update=True,
    )
    if publication is None:
        raise CommitmentReportRejected("no publication was claimed by this validator")
    duplicate = publication.status == "confirmed" or (
        publication.status == "failed" and report.outcome == "failed"
    )
    if not duplicate:
        publication.status = report.outcome
        publication.reported_at = now
        publication.block_number = report.block_number
        publication.extrinsic_hash = report.extrinsic_hash
        publication.detail = report.detail
        await session.flush()
    confirmations = await commitment_confirmations(session, row.round_index)
    if not duplicate and report.outcome == "confirmed":
        row.commitment_published_at = row.commitment_published_at or now
        if (
            row.commitment_publication == "pending"
            and confirmations >= ROUND_COMMITMENT_PUBLISHERS
        ):
            row.commitment_publication = "published"
            row.commitment_error = None
    return GeneratorRoundCommitmentReportResponse(
        round_index=row.round_index,
        accepted=not duplicate,
        duplicate=duplicate,
        confirmations=confirmations,
        target=ROUND_COMMITMENT_PUBLISHERS,
    )


async def settle_commitment_publications(session: AsyncSession) -> list[str]:
    """Close publication for rounds that left ``committed`` while pending.

    One confirmed validator publication is enough for ``published``. With
    none, an opened round is ``failed`` (the on-chain copy never landed) and a
    void round ``disabled`` (nobody needs it). Returns one note per round.
    """
    rows = list(
        await session.scalars(
            select(GeneratorRound)
            .where(
                GeneratorRound.commitment_publication == "pending",
                GeneratorRound.status != "committed",
            )
            .order_by(GeneratorRound.round_index)
            .with_for_update()
        )
    )
    notes: list[str] = []
    for row in rows:
        confirmations = await commitment_confirmations(session, row.round_index)
        if confirmations > 0:
            row.commitment_publication = "published"
            row.commitment_error = None
        elif row.status == "void":
            row.commitment_publication = "disabled"
        else:
            row.commitment_publication = "failed"
            row.commitment_error = (
                "no validator confirmed the on-chain commitment before the round opened"
            )
        notes.append(
            f"round {row.round_index}: commitment publication "
            f"{row.commitment_publication} ({confirmations} confirmed)"
        )
    return notes


async def commitment_publication_views(
    session: AsyncSession, row: GeneratorRound, *, now: datetime
) -> tuple[list[GeneratorRoundCommitmentPublicationView], list[str]]:
    """Every validator publication of ``row`` plus who is elected right now."""
    publications = await _publications(session, row.round_index)
    order = {
        hotkey: rank
        for rank, hotkey in enumerate(
            publisher_order(row.round_index, (p.validator_hotkey for p in publications))
        )
    }
    views = [
        GeneratorRoundCommitmentPublicationView(
            validator_hotkey=p.validator_hotkey,
            status=(
                "expired"
                if p.status == "claimed" and _aware(p.claim_expires_at) <= now
                else p.status  # type: ignore[arg-type]
            ),
            claimed_at=p.claimed_at,
            claim_expires_at=p.claim_expires_at,
            reported_at=p.reported_at,
            block_number=p.block_number,
            extrinsic_hash=p.extrinsic_hash,
            detail=p.detail,
        )
        for p in sorted(publications, key=lambda p: order[p.validator_hotkey])
    ]
    elected: list[str] = []
    if row.status == "committed" and row.commitment_publication == "pending":
        capable = await commitment_capable_hotkeys(session, now=now)
        elected = list(
            elect_commitment_publishers(row, publications, capable, now=now).elected
        )
    return views, elected
