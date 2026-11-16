"""Self-serve paid readmission: eligibility and price reservations (SN-485).

Eligibility is exactly what the operator's stranded admission accepts
(:func:`~ditto.db.queries.stranded_admission.resolve_stranded_admission`),
narrowed to what a miner may buy for themselves:

* the agent is the signer's, and the hotkey's current on-chain owner coldkey is
  the coldkey that paid for the agent (a re-registered UID held by someone
  else cannot readmit it);
* it is a *finalized* stranded row of the active era's predecessor (partial
  2-of-3 rows are not eligible: they stay resubmit-only or follow Ditto's free
  carryover);
* it is not already admitted to the active era, has no admitted paid
  readmission for it, and is not retired from it;
* every stranded-admission guard holds: no rollout open, an activated era with
  a previous one, not withdrawn from the active queue, at or above the
  screening-policy floor, a complete screened image.

Any stranded row of the owner is eligible, not only its best; the check reports
``owner_best_stranded`` and the cohort sibling so the miner knows payouts are
owner-best only. Owner concurrency and the similarity budget can delay a
readmitted agent's leases but never exclude it, so they do not refuse here.

A reservation snapshots the price (always the upload fee) and the payment
address for one ``(agent_id, bench_version)`` for 24 hours, under the same
per-coldkey lock as upload reservations, without taking the coldkey's upload
slot.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import func, select

from ditto.db.models import (
    Agent,
    BenchmarkRollout,
    EvaluationPayment,
    ReadmissionPayment,
    ReadmissionReservation,
    Score,
    SubmissionRetirement,
    UploadReadmission,
)
from ditto.db.queries.agents import (
    SubmissionCooldownError,
    submission_cooldown_decision,
    submission_since_reservation_error,
)
from ditto.db.queries.benchmark_admission import agent_is_admitted
from ditto.db.queries.benchmark_era_stranded import (
    StrandedEraAgent,
    stranded_previous_era_agents,
)
from ditto.db.queries.benchmark_rollout import (
    RolloutConflictError,
    active_bench_version,
    open_rollout,
)
from ditto.db.queries.owner_submission_cooldown import lock_owner_cooldown_hotkey
from ditto.db.queries.payments import get_miner_coldkey_for_agent
from ditto.db.queries.readmission_gate import readmission_fee_rao
from ditto.db.queries.scores import SCORING_QUORUM
from ditto.db.queries.stranded_admission import (
    StrandedAgentNotFoundError,
    resolve_stranded_admission,
)
from ditto.db.queries.submission_settings import (
    UPLOAD_ADMISSION_TTL,
    EffectiveSubmissionSettings,
    live_readmission_block_until,
    live_upload_block_until,
    lock_upload_admission_coldkey,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

READMISSION_RESERVATION_TTL = UPLOAD_ADMISSION_TTL

ERROR_CODE_READMISSION_INACTIVE = 1110
ERROR_CODE_READMISSION_NOT_ELIGIBLE = 1111
ERROR_CODE_READMISSION_ALREADY_ADMITTED = 1112
ERROR_CODE_READMISSION_OWNER_MISMATCH = 1113


@dataclass(frozen=True)
class ReadmissionRefusal:
    code: int
    message: str


@dataclass(frozen=True)
class ReadmissionEligibility:
    """One agent's readmission eligibility against the live active era."""

    active_bench_version: int
    agent: Agent | None
    payment_coldkey: str | None
    rollout: BenchmarkRollout | None = None
    stranded: StrandedEraAgent | None = None
    already_admitted: bool = False
    owner_best_stranded: bool | None = None
    refusals: list[ReadmissionRefusal] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return not self.refusals


async def admitted_readmission(
    session: AsyncSession, *, agent_id: UUID, bench_version: int
) -> ReadmissionPayment | None:
    return await session.scalar(
        select(ReadmissionPayment).where(
            ReadmissionPayment.agent_id == agent_id,
            ReadmissionPayment.bench_version == bench_version,
            ReadmissionPayment.outcome == "admitted",
        )
    )


async def readmission_eligibility(
    session: AsyncSession,
    *,
    agent_id: UUID,
    hotkey: str,
    owner_coldkey: str | None,
    bench_version: int,
    with_owner_rank: bool = False,
) -> ReadmissionEligibility:
    """Every reason ``hotkey`` may not readmit ``agent_id`` into the active era.

    ``owner_coldkey`` is the hotkey's *current* on-chain owner. Read-only.
    ``with_owner_rank`` also reads the whole stranded list to say whether this
    is the owner's best stranded row (the pre-payment check wants it; the
    commit does not).
    """
    active = await active_bench_version(session)
    refusals: list[ReadmissionRefusal] = []

    def refuse(code: int, message: str) -> None:
        refusals.append(ReadmissionRefusal(code, message))

    agent = await session.get(Agent, agent_id)
    if agent is None:
        refuse(ERROR_CODE_READMISSION_NOT_ELIGIBLE, f"agent {agent_id} not found")
        return ReadmissionEligibility(active, None, None, refusals=refusals)
    payment_coldkey = await get_miner_coldkey_for_agent(session, agent_id=agent_id)
    if agent.miner_hotkey != hotkey:
        refuse(
            ERROR_CODE_READMISSION_OWNER_MISMATCH,
            "this agent was uploaded by a different hotkey",
        )
    elif payment_coldkey is None or owner_coldkey != payment_coldkey:
        refuse(
            ERROR_CODE_READMISSION_OWNER_MISMATCH,
            "this hotkey's current owner coldkey is not the coldkey that paid "
            "for the agent; only its owner can readmit it",
        )
    if bench_version != active:
        refuse(
            ERROR_CODE_READMISSION_NOT_ELIGIBLE,
            f"readmission is into the active benchmark v{active} only, "
            f"not v{bench_version}",
        )
    if refusals:
        return ReadmissionEligibility(active, agent, payment_coldkey, refusals=refusals)

    if await admitted_readmission(
        session, agent_id=agent_id, bench_version=active
    ) is not None or await agent_is_admitted(
        session, bench_version=active, agent_id=agent_id
    ):
        refuse(
            ERROR_CODE_READMISSION_ALREADY_ADMITTED,
            f"agent {agent_id} is already admitted to v{active}; there is "
            "nothing to readmit",
        )
        return ReadmissionEligibility(
            active, agent, payment_coldkey, already_admitted=True, refusals=refusals
        )
    if await open_rollout(session) is not None:
        refuse(
            ERROR_CODE_READMISSION_NOT_ELIGIBLE,
            "a benchmark rollout is open; readmission opens after it activates",
        )
        return ReadmissionEligibility(active, agent, payment_coldkey, refusals=refusals)

    report = await stranded_previous_era_agents(session, agent_id=agent_id)
    stranded = report.items[0] if report.items else None
    if stranded is None:
        refuse(
            ERROR_CODE_READMISSION_NOT_ELIGIBLE,
            f"agent {agent_id} is not a scored previous-era agent that "
            f"v{active} leaves out: it is not scored or live, holds a "
            f"v{active} score, or has no previous-era scores. Resubmit instead",
        )
        return ReadmissionEligibility(
            active, agent, payment_coldkey, rollout=report.rollout, refusals=refusals
        )
    if stranded.previous_score_count < SCORING_QUORUM:
        refuse(
            ERROR_CODE_READMISSION_NOT_ELIGIBLE,
            f"agent {agent_id} has {stranded.previous_score_count} of "
            f"{SCORING_QUORUM} previous-era scores; a partial row is not "
            "eligible for readmission. Resubmit instead",
        )
    # Retirement names the era it applies to: refuse an agent retired from the
    # stranded (previous) era as well as from the active one.
    retired_from = await session.scalar(
        select(func.max(SubmissionRetirement.bench_version)).where(
            SubmissionRetirement.agent_id == agent_id,
            SubmissionRetirement.bench_version.in_(
                {active}
                | (
                    {report.rollout.from_version}
                    if report.rollout is not None
                    else set()
                )
            ),
        )
    )
    if retired_from is not None:
        refuse(
            ERROR_CODE_READMISSION_NOT_ELIGIBLE,
            f"agent {agent_id} is retired from the v{retired_from} queue",
        )
    if not refusals:
        try:
            await resolve_stranded_admission(
                session,
                agent_id=agent_id,
                expected_active_version=active,
                expected_sha256=agent.sha256,
                expected_previous_score_count=stranded.previous_score_count,
            )
        except (RolloutConflictError, StrandedAgentNotFoundError) as exc:
            refuse(ERROR_CODE_READMISSION_NOT_ELIGIBLE, str(exc))
    owner_best = None
    if with_owner_rank and not refusals:
        owners = await stranded_previous_era_agents(session)
        owner_best = next(
            (
                item.owner_best_stranded
                for item in owners.items
                if item.agent_id == agent_id
            ),
            None,
        )
    return ReadmissionEligibility(
        active,
        agent,
        payment_coldkey,
        rollout=report.rollout,
        stranded=stranded,
        owner_best_stranded=owner_best,
        refusals=refusals,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def reservation_live(
    row: ReadmissionReservation | None,
    *,
    agent_id: UUID,
    bench_version: int,
    hotkey: str,
    now: datetime,
) -> bool:
    return (
        row is not None
        and row.agent_id == agent_id
        and row.bench_version == bench_version
        and row.miner_hotkey == hotkey
        and _utc(row.expires_at) > _utc(now)
    )


async def get_readmission_reservation(
    session: AsyncSession, *, token: UUID, for_update: bool = False
) -> ReadmissionReservation | None:
    stmt = select(ReadmissionReservation).where(ReadmissionReservation.token == token)
    if for_update:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def reserve_readmission(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    rollout_id: UUID,
    miner_coldkey: str,
    miner_hotkey: str,
    settings: EffectiveSubmissionSettings,
    now: datetime | None = None,
) -> ReadmissionReservation:
    """Reserve one readmission's price for 24 hours, in the caller's transaction.

    A live reservation for the same agent, era and hotkey is returned as is,
    so its price snapshot is honoured. A new one requires the owner's upload
    cooldown to be clear (a readmission counts toward it), and holds the
    coldkey's single submission slot like an upload reservation: at most one
    live readmission reservation per coldkey, and none while an upload or
    another readmission reservation is inside its 15-minute block window. An
    older sibling past that window is replaced, as uploads replace theirs.

    Raises:
        SubmissionCooldownError: the owner is inside the cooldown, or another
            reservation of the coldkey still holds the slot.
    """
    current = _utc(now or datetime.now(UTC))
    await lock_upload_admission_coldkey(session, miner_coldkey)
    await lock_owner_cooldown_hotkey(session, miner_hotkey)
    existing = await session.get(
        ReadmissionReservation, (agent_id, bench_version), with_for_update=True
    )
    if existing is not None:
        if reservation_live(
            existing,
            agent_id=agent_id,
            bench_version=bench_version,
            hotkey=miner_hotkey,
            now=current,
        ) and (existing.miner_coldkey == miner_coldkey):
            return existing
        await session.delete(existing)
        await session.flush()
    upload_block = await live_upload_block_until(
        session, miner_coldkey=miner_coldkey, now=current
    )
    sibling_block = await live_readmission_block_until(
        session,
        miner_coldkey=miner_coldkey,
        now=current,
        exclude=(agent_id, bench_version),
    )
    blocked = max(
        (block for block in (upload_block, sibling_block) if block is not None),
        default=None,
    )
    if blocked is not None:
        raise SubmissionCooldownError(blocked)
    # Past their block window, the coldkey's other readmission reservations
    # give way: one live readmission reservation per coldkey.
    for sibling in await session.scalars(
        select(ReadmissionReservation)
        .where(ReadmissionReservation.miner_coldkey == miner_coldkey)
        .with_for_update()
    ):
        await session.delete(sibling)
    await session.flush()
    decision = await submission_cooldown_decision(
        session,
        miner_coldkey=miner_coldkey,
        miner_hotkey=miner_hotkey,
        cooldown=timedelta(seconds=settings.cooldown_seconds),
        now=current,
    )
    if decision is not None:
        raise decision.error()
    row = ReadmissionReservation(
        agent_id=agent_id,
        bench_version=bench_version,
        token=uuid.uuid4(),
        rollout_id=rollout_id,
        miner_hotkey=miner_hotkey,
        miner_coldkey=miner_coldkey,
        settings_revision=settings.revision,
        cooldown_seconds=settings.cooldown_seconds,
        # Never more than the upload fee: the readmission price is the upload
        # fee itself, with no override of its own.
        fee_amount_rao=min(readmission_fee_rao(settings), settings.fee_amount_rao),
        payment_send_address=settings.payment_address,
        created_at=current,
        expires_at=current + READMISSION_RESERVATION_TTL,
    )
    session.add(row)
    await session.flush()
    return row


async def consume_readmission_admission(
    session: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    reservation_token: UUID | None,
    miner_coldkey: str,
    miner_hotkey: str,
    settings: EffectiveSubmissionSettings,
    now: datetime | None = None,
) -> None:
    """Take the owner's submission slot for a readmission being written.

    Mirrors the upload's consume-or-enforce: a live reservation was granted
    with the cooldown clear and is simply consumed; without one the cooldown
    is enforced now. Holds the coldkey then hotkey locks through commit.

    Raises:
        SubmissionCooldownError: no live reservation and the cooldown applies.
    """
    current = _utc(now or datetime.now(UTC))
    await lock_upload_admission_coldkey(session, miner_coldkey)
    await lock_owner_cooldown_hotkey(session, miner_hotkey)
    if reservation_token is not None:
        locked = await session.get(
            ReadmissionReservation, (agent_id, bench_version), with_for_update=True
        )
        if (
            locked is not None
            and locked.token == reservation_token
            and locked.miner_hotkey == miner_hotkey
            and _utc(locked.expires_at) > current
        ):
            reserved_at = locked.created_at
            reserved_cooldown = locked.cooldown_seconds
            await session.delete(locked)
            await session.flush()
            # Another submission of this coldkey after the reservation was
            # issued started a cooldown the reservation cannot cover. The
            # longer of the reserved and the current cooldown applies, so a
            # change in either direction never lets it end early.
            stale = await submission_since_reservation_error(
                session,
                miner_coldkey=miner_coldkey,
                reserved_at=reserved_at,
                cooldown=timedelta(
                    seconds=max(reserved_cooldown, settings.cooldown_seconds)
                ),
                now=current,
            )
            if stale is not None:
                raise stale
            return
    decision = await submission_cooldown_decision(
        session,
        miner_coldkey=miner_coldkey,
        miner_hotkey=miner_hotkey,
        cooldown=timedelta(seconds=settings.cooldown_seconds),
        now=current,
    )
    if decision is not None:
        raise decision.error()


UploadReadmissionKind = Literal["identical_rescore", "near_duplicate"]


@dataclass(frozen=True)
class UploadReadmissionLabel:
    """Why an accepted upload is a readmission of the owner's own lineage."""

    kind: UploadReadmissionKind
    lineage_agent_id: UUID
    lineage_evaluation_completed: bool


async def get_same_owner_agent_by_normalized_source(
    session: AsyncSession,
    *,
    miner_coldkey: str,
    normalized_source_hash: str,
    exclude_sha256: str,
) -> UUID | None:
    """The owner's earliest paid agent with the same normalized source but other
    bytes: the existing near-duplicate (repack/reformat) signal."""
    return await session.scalar(
        select(Agent.agent_id)
        .join(EvaluationPayment, EvaluationPayment.agent_id == Agent.agent_id)
        .where(
            EvaluationPayment.miner_coldkey == miner_coldkey,
            Agent.normalized_source_hash == normalized_source_hash,
            Agent.sha256 != exclude_sha256,
        )
        .order_by(Agent.created_at.asc(), Agent.agent_id.asc())
        .limit(1)
    )


async def lineage_evaluation_completed(session: AsyncSession, agent_id: UUID) -> bool:
    """Whether ``agent_id`` finished a scoring quorum in any era."""
    quorum = (
        select(Score.bench_version)
        .where(Score.agent_id == agent_id)
        .group_by(Score.bench_version)
        .having(func.count(Score.validator_hotkey) >= SCORING_QUORUM)
        .limit(1)
    )
    return await session.scalar(quorum) is not None


async def classify_upload_readmission(
    session: AsyncSession,
    *,
    miner_coldkey: str,
    sha256: str,
    normalized_source_hash: str | None,
    identical_agent_id: UUID | None,
) -> UploadReadmissionLabel | None:
    """Classify an upload being accepted against the owner's own lineage.

    Reuses the same-owner machinery the upload already runs: an
    ``identical_agent_id`` from :func:`~ditto.db.queries.payments.\
get_same_owner_agent_by_sha` (only reached with ``allow_identical_rescore``)
    is an identical rescore; otherwise a same-owner agent with the same
    normalized source is a near-duplicate. Keys only on the owner's uploads,
    never on score re-tests, so platform-caused re-tests (``dueling_season_\
rotation`` and the rest) are never labelled or charged.
    """
    lineage: UUID | None = identical_agent_id
    kind: UploadReadmissionKind = "identical_rescore"
    if lineage is None and normalized_source_hash:
        lineage = await get_same_owner_agent_by_normalized_source(
            session,
            miner_coldkey=miner_coldkey,
            normalized_source_hash=normalized_source_hash,
            exclude_sha256=sha256,
        )
        kind = "near_duplicate"
    if lineage is None:
        return None
    return UploadReadmissionLabel(
        kind=kind,
        lineage_agent_id=lineage,
        lineage_evaluation_completed=await lineage_evaluation_completed(
            session, lineage
        ),
    )


async def get_upload_readmission(
    session: AsyncSession, *, agent_id: UUID
) -> UploadReadmission | None:
    """The readmission label recorded for an accepted upload, if any."""
    row = await session.scalar(
        select(UploadReadmission).where(UploadReadmission.agent_id == agent_id)
    )
    return row if isinstance(row, UploadReadmission) else None


async def record_upload_readmission(
    session: AsyncSession,
    *,
    agent_id: UUID,
    label: UploadReadmissionLabel,
    bench_version: int,
    miner_coldkey: str,
) -> None:
    """Label an accepted upload, in the upload's own transaction."""
    session.add(
        UploadReadmission(
            agent_id=agent_id,
            kind=label.kind,
            lineage_agent_id=label.lineage_agent_id,
            bench_version=bench_version,
            miner_coldkey=miner_coldkey,
            lineage_evaluation_completed=label.lineage_evaluation_completed,
        )
    )
    await session.flush()


__all__ = [
    "ERROR_CODE_READMISSION_ALREADY_ADMITTED",
    "ERROR_CODE_READMISSION_INACTIVE",
    "ERROR_CODE_READMISSION_NOT_ELIGIBLE",
    "ERROR_CODE_READMISSION_OWNER_MISMATCH",
    "READMISSION_RESERVATION_TTL",
    "ReadmissionEligibility",
    "ReadmissionRefusal",
    "SubmissionCooldownError",
    "UploadReadmissionKind",
    "UploadReadmissionLabel",
    "admitted_readmission",
    "classify_upload_readmission",
    "get_same_owner_agent_by_normalized_source",
    "get_upload_readmission",
    "lineage_evaluation_completed",
    "record_upload_readmission",
    "consume_readmission_admission",
    "get_readmission_reservation",
    "readmission_eligibility",
    "reservation_live",
    "reserve_readmission",
]
