"""Build-bounty publication and the public read model.

Platform is the only authority for bounty terms. An operator publishes each
revision of a bounty's public terms; the first revision opens round 1 with
its paid slots. Revisions, rounds and the public trail are append-only, and a
later revision never changes slots that already exist.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.bounty import (
    CLOSED_SLOT_STATUSES,
    BountyBoardPage,
    BountyDetail,
    BountyEventView,
    BountyPublishPreview,
    BountyPublishPreviewRequest,
    BountyPublishRequest,
    BountyRevisionView,
    BountyRoundView,
    BountySlotCounts,
    BountySlotView,
    BountySummary,
    BountyTerms,
    bounty_publish_confirmation,
    bounty_spec_digest,
)
from ditto.db.models import Bounty, BountyEvent, BountyRevision, BountyRound, BountySlot
from ditto.db.queries.contributor_identity import lock_identity_writes

# Issue #2942: three attempts per round. The field stays in the signed terms
# so a later policy can change it under a new revision.
BOUNTY_ATTEMPTS_PER_ROUND = 3
_PUBLICATION_LOCK = "bounty-publication"
_TEXT_TERMS = ("title", "scope", "public_interfaces", "acceptance_evidence")


def publication_refusals(terms: BountyTerms) -> list[str]:
    """Every reason these terms cannot be published; empty when they can.

    Preview and publish share this one check, so an operator never sees a
    preview pass that publication then refuses.
    """
    refusals: list[str] = []
    if terms.entry_fee_alpha_rao <= 0:
        refusals.append("the entry fee must be a positive alpha amount")
    if terms.reward_pool_alpha_rao <= 0:
        refusals.append("the reward pool must be a positive alpha amount")
    if terms.attempts_per_round != BOUNTY_ATTEMPTS_PER_ROUND:
        refusals.append(
            f"bounties have exactly {BOUNTY_ATTEMPTS_PER_ROUND} paid slots per round"
        )
    if terms.funding_mode != "rehearsal":
        refusals.append(
            "live funding needs separate operator approval; publish in rehearsal mode"
        )
    if terms.fee_escrow_coldkey == terms.fee_escrow_hotkey:
        refusals.append("the fee escrow coldkey and hotkey must be different keys")
    refusals.extend(
        f"{name} must not be blank"
        for name in _TEXT_TERMS
        if not getattr(terms, name).strip()
    )
    return refusals


async def _bounty_and_revision(
    session: AsyncSession, *, repo: str, issue: int
) -> tuple[Bounty | None, int]:
    bounty = await session.scalar(
        select(Bounty).where(Bounty.repo == repo, Bounty.issue_number == issue)
    )
    if bounty is None:
        return None, 0
    current = await session.scalar(
        select(func.max(BountyRevision.revision)).where(
            BountyRevision.bounty_id == bounty.bounty_id
        )
    )
    return bounty, int(current or 0)


async def preview_revision(
    session: AsyncSession, body: BountyPublishPreviewRequest
) -> BountyPublishPreview:
    """The exact digest and confirmation phrase for publishing ``body``."""
    _, current = await _bounty_and_revision(session, repo=body.repo, issue=body.issue)
    refusals = publication_refusals(body.terms)
    if current != body.expected_revision:
        refusals.append(
            f"the current revision is {current}; preview against it before publishing"
        )
    revision = body.expected_revision + 1
    digest = bounty_spec_digest(
        repo=body.repo, issue=body.issue, revision=revision, terms=body.terms
    )
    return BountyPublishPreview(
        revision=revision,
        spec_digest=digest,
        confirmation=bounty_publish_confirmation(
            repo=body.repo, issue=body.issue, revision=revision, spec_digest=digest
        ),
        refusals=refusals,
    )


async def publish_revision(
    session: AsyncSession, body: BountyPublishRequest, *, actor: str
) -> BountyRevisionView:
    """Append revision ``expected_revision + 1``; the first one opens round 1."""
    reviewer_actor = body.reviewer_actor.strip()
    if not reviewer_actor or not reviewer_actor.isprintable():
        raise HTTPException(400, "reviewer_actor must be printable and not blank")
    if not body.reason.strip():
        raise HTTPException(400, "reason must not be blank")
    refusals = publication_refusals(body.terms)
    if refusals:
        raise HTTPException(409, "; ".join(refusals))
    revision = body.expected_revision + 1
    digest = bounty_spec_digest(
        repo=body.repo, issue=body.issue, revision=revision, terms=body.terms
    )
    expected = bounty_publish_confirmation(
        repo=body.repo, issue=body.issue, revision=revision, spec_digest=digest
    )
    if body.confirmation != expected:
        raise HTTPException(
            409,
            "explicit bounty revision confirmation is required; preview the "
            "revision for its exact phrase",
        )
    async with session.begin():
        await lock_identity_writes(session, namespace=_PUBLICATION_LOCK)
        bounty, current = await _bounty_and_revision(
            session, repo=body.repo, issue=body.issue
        )
        if current != body.expected_revision:
            raise HTTPException(
                409, "the bounty revision changed; preview again before publishing"
            )
        now = datetime.now(UTC)
        if bounty is None:
            bounty = Bounty(
                bounty_id=uuid4(),
                repo=body.repo,
                issue_number=body.issue,
                created_at=now,
            )
            session.add(bounty)
            await session.flush()
        terms = body.terms
        row = BountyRevision(
            bounty_id=bounty.bounty_id,
            revision=revision,
            title=terms.title,
            scope=terms.scope,
            public_interfaces=terms.public_interfaces,
            acceptance_evidence=terms.acceptance_evidence,
            dependencies=list(terms.dependencies),
            reviewer_login=terms.reviewer_login,
            entry_fee_alpha_rao=terms.entry_fee_alpha_rao,
            reward_pool_alpha_rao=terms.reward_pool_alpha_rao,
            attempts_per_round=terms.attempts_per_round,
            delivery_days=terms.delivery_days,
            review_days=terms.review_days,
            hold_minutes=terms.hold_minutes,
            funding_mode=terms.funding_mode,
            fee_escrow_coldkey=terms.fee_escrow_coldkey,
            fee_escrow_hotkey=terms.fee_escrow_hotkey,
            spec_digest=digest,
            reviewer_actor=reviewer_actor,
            actor=actor,
            reason=body.reason,
            published_at=now,
        )
        session.add(row)
        await session.flush()
        session.add(
            BountyEvent(
                bounty_id=bounty.bounty_id,
                kind="revision_published",
                revision=revision,
                recorded_at=now,
            )
        )
        if revision == 1:
            await _open_round(
                session,
                bounty_id=bounty.bounty_id,
                round_number=1,
                revision=row,
                actor=actor,
                reason=body.reason,
                now=now,
            )
        await session.flush()
        return revision_view(row)


async def _open_round(
    session: AsyncSession,
    *,
    bounty_id: UUID,
    round_number: int,
    revision: BountyRevision,
    actor: str,
    reason: str,
    now: datetime,
) -> None:
    session.add(
        BountyRound(
            bounty_id=bounty_id,
            round=round_number,
            opened_revision=revision.revision,
            opened_at=now,
            actor=actor,
            reason=reason,
        )
    )
    await session.flush()
    for position in range(1, revision.attempts_per_round + 1):
        session.add(
            BountySlot(
                slot_id=uuid4(),
                bounty_id=bounty_id,
                round=round_number,
                position=position,
                status="open",
                status_changed_at=now,
                created_at=now,
            )
        )
    session.add(
        BountyEvent(
            bounty_id=bounty_id,
            kind="round_opened",
            revision=revision.revision,
            round=round_number,
            recorded_at=now,
        )
    )


def terms_of(row: BountyRevision) -> BountyTerms:
    return BountyTerms(
        title=row.title,
        scope=row.scope,
        public_interfaces=row.public_interfaces,
        acceptance_evidence=row.acceptance_evidence,
        dependencies=list(row.dependencies),
        reviewer_login=row.reviewer_login,
        entry_fee_alpha_rao=row.entry_fee_alpha_rao,
        reward_pool_alpha_rao=row.reward_pool_alpha_rao,
        attempts_per_round=row.attempts_per_round,
        delivery_days=row.delivery_days,
        review_days=row.review_days,
        hold_minutes=row.hold_minutes,
        funding_mode=row.funding_mode,  # type: ignore[arg-type]
        fee_escrow_coldkey=row.fee_escrow_coldkey,
        fee_escrow_hotkey=row.fee_escrow_hotkey,
    )


def revision_view(row: BountyRevision) -> BountyRevisionView:
    return BountyRevisionView(
        revision=row.revision,
        spec_digest=row.spec_digest,
        terms=terms_of(row),
        published_at=row.published_at,
    )


def _slot_counts(statuses: Counter[str]) -> BountySlotCounts:
    return BountySlotCounts(
        open=statuses["open"],
        held=statuses["held"],
        claimed=statuses["claimed"],
        submitted=statuses["submitted"],
        rework=statuses["rework"],
        closed=sum(statuses[status] for status in CLOSED_SLOT_STATUSES),
    )


def _summary(
    bounty: Bounty,
    *,
    first: BountyRevision,
    current: BountyRevision,
    current_round: int,
    statuses: Counter[str],
) -> BountySummary:
    return BountySummary(
        bounty_id=bounty.bounty_id,
        repo=bounty.repo,
        issue=bounty.issue_number,
        title=current.title,
        current_revision=current.revision,
        spec_digest=current.spec_digest,
        entry_fee_alpha_rao=current.entry_fee_alpha_rao,
        reward_pool_alpha_rao=current.reward_pool_alpha_rao,
        delivery_days=current.delivery_days,
        review_days=current.review_days,
        reviewer_login=current.reviewer_login,
        funding_mode=current.funding_mode,  # type: ignore[arg-type]
        current_round=current_round,
        slots=_slot_counts(statuses),
        published_at=first.published_at,
        updated_at=current.published_at,
    )


async def board(session: AsyncSession, *, limit: int, offset: int) -> BountyBoardPage:
    """Newest bounties first, each with its current terms and slot counts."""
    total = int(await session.scalar(select(func.count()).select_from(Bounty)) or 0)
    bounties = list(
        await session.scalars(
            select(Bounty)
            .order_by(Bounty.created_at.desc(), Bounty.bounty_id)
            .offset(offset)
            .limit(limit)
        )
    )
    ids = [bounty.bounty_id for bounty in bounties]
    revisions: dict[UUID, list[BountyRevision]] = {}
    for row in await session.scalars(
        select(BountyRevision)
        .where(BountyRevision.bounty_id.in_(ids))
        .order_by(BountyRevision.bounty_id, BountyRevision.revision)
    ):
        revisions.setdefault(row.bounty_id, []).append(row)
    rounds: dict[UUID, int] = {
        bounty_id: int(latest)
        for bounty_id, latest in (
            await session.execute(
                select(BountyRound.bounty_id, func.max(BountyRound.round))
                .where(BountyRound.bounty_id.in_(ids))
                .group_by(BountyRound.bounty_id)
            )
        ).all()
    }
    statuses: dict[UUID, Counter[str]] = {}
    for bounty_id, status, count in (
        await session.execute(
            select(BountySlot.bounty_id, BountySlot.status, func.count())
            .where(BountySlot.bounty_id.in_(ids))
            .group_by(BountySlot.bounty_id, BountySlot.status)
        )
    ).all():
        statuses.setdefault(bounty_id, Counter())[status] = int(count)
    items = [
        _summary(
            bounty,
            first=revisions[bounty.bounty_id][0],
            current=revisions[bounty.bounty_id][-1],
            current_round=int(rounds.get(bounty.bounty_id, 0)),
            statuses=statuses.get(bounty.bounty_id, Counter()),
        )
        for bounty in bounties
    ]
    return BountyBoardPage(items=items, total=total, limit=limit, offset=offset)


async def detail(session: AsyncSession, bounty_id: UUID) -> BountyDetail:
    bounty = await session.get(Bounty, bounty_id)
    if bounty is None:
        raise HTTPException(404, "bounty not found")
    revisions = list(
        await session.scalars(
            select(BountyRevision)
            .where(BountyRevision.bounty_id == bounty_id)
            .order_by(BountyRevision.revision)
        )
    )
    rounds = list(
        await session.scalars(
            select(BountyRound)
            .where(BountyRound.bounty_id == bounty_id)
            .order_by(BountyRound.round)
        )
    )
    slots = list(
        await session.scalars(
            select(BountySlot)
            .where(BountySlot.bounty_id == bounty_id)
            .order_by(BountySlot.round, BountySlot.position)
        )
    )
    events = list(
        await session.scalars(
            select(BountyEvent)
            .where(BountyEvent.bounty_id == bounty_id)
            .order_by(BountyEvent.event_id)
        )
    )
    return BountyDetail(
        summary=_summary(
            bounty,
            first=revisions[0],
            current=revisions[-1],
            current_round=rounds[-1].round if rounds else 0,
            statuses=Counter(slot.status for slot in slots),
        ),
        revisions=[revision_view(row) for row in revisions],
        rounds=[
            BountyRoundView(
                round=row.round,
                opened_revision=row.opened_revision,
                opened_at=row.opened_at,
            )
            for row in rounds
        ],
        slots=[
            BountySlotView(
                slot_id=slot.slot_id,
                round=slot.round,
                position=slot.position,
                status=slot.status,  # type: ignore[arg-type]
                status_changed_at=slot.status_changed_at,
            )
            for slot in slots
        ],
        events=[
            BountyEventView(
                event_id=event.event_id,
                kind=event.kind,  # type: ignore[arg-type]
                revision=event.revision,
                round=event.round,
                slot_id=event.slot_id,
                recorded_at=event.recorded_at,
            )
            for event in events
        ],
    )
