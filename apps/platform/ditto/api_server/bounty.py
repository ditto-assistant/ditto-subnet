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
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

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
    BountySlotClaimant,
    BountySlotCounts,
    BountySlotView,
    BountySummary,
    BountyTerms,
    bounty_publish_confirmation,
    bounty_spec_digest,
)
from ditto.api_server import bounty_claims
from ditto.api_server.bounty_deadlines import (
    DEADLINE_STATUSES,
    effective_slot_status,
    review_overdue,
)
from ditto.db.models import (
    Bounty,
    BountyClaim,
    BountyClaimControl,
    BountyEvent,
    BountyObligation,
    BountyRevision,
    BountyRound,
    BountySlot,
)
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
        # Lifting this also needs every operator write to carry a
        # proof-backed operator email (require_operator_proof): X-Admin-Actor
        # alone is asserted, so distinct approvers are not yet proven
        # (docs/bounty-claims.md).
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


async def _reviewer_claim_refusal(
    session: AsyncSession, bounty_id: UUID, reviewer_login: str, *, now: datetime
) -> str | None:
    """Why ``reviewer_login`` cannot review the bounty, if it holds a live claim.

    A claim whose login is the reviewer's is refused when it is placed
    (``bounty_claims._check_not_reviewer``); this keeps a later revision from
    naming the holder of a live claim. GitHub logins are case-insensitive.
    """
    claim_id = await session.scalar(
        select(BountyClaim.claim_id)
        .join(BountySlot, BountySlot.claim_id == BountyClaim.claim_id)
        .where(
            BountySlot.bounty_id == bounty_id,
            BountySlot.status.in_(bounty_claims.LIVE_SLOT_STATUSES),
            # An unpaid hold past its expiry is already an open slot.
            or_(BountySlot.status != "held", BountyClaim.hold_expires_at > now),
            func.lower(BountyClaim.github_login) == reviewer_login.lower(),
        )
        .limit(1)
    )
    if claim_id is None:
        return None
    return (
        f"reviewer_login {reviewer_login} holds a live claim on this bounty "
        f"({claim_id}); its reviewer cannot be a claimant"
    )


async def preview_revision(
    session: AsyncSession, body: BountyPublishPreviewRequest
) -> BountyPublishPreview:
    """The exact digest and confirmation phrase for publishing ``body``."""
    bounty, current = await _bounty_and_revision(
        session, repo=body.repo, issue=body.issue
    )
    refusals = publication_refusals(body.terms)
    if bounty is not None and (
        refusal := await _reviewer_claim_refusal(
            session,
            bounty.bounty_id,
            body.terms.reviewer_login,
            now=datetime.now(UTC),
        )
    ):
        refusals.append(refusal)
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
        if bounty is not None:
            # Claims check the reviewer under this lock, so neither can race.
            await bounty_claims.locked_bounty(session, bounty.bounty_id)
            refusal = await _reviewer_claim_refusal(
                session, bounty.bounty_id, body.terms.reviewer_login, now=now
            )
            if refusal is not None:
                raise HTTPException(409, refusal)
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


# The only revision columns a summary reads; the board loads nothing else.
_SUMMARY_COLUMNS = (
    BountyRevision.bounty_id,
    BountyRevision.revision,
    BountyRevision.title,
    BountyRevision.spec_digest,
    BountyRevision.entry_fee_alpha_rao,
    BountyRevision.reward_pool_alpha_rao,
    BountyRevision.delivery_days,
    BountyRevision.review_days,
    BountyRevision.reviewer_login,
    BountyRevision.funding_mode,
    BountyRevision.published_at,
)


def _summary(
    bounty: Bounty,
    *,
    published_at: datetime,
    current: BountyRevision,
    current_round: int,
    paused: bool,
    statuses: Counter[str],
    overdue: int,
    awarded: int,
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
        claims_paused=paused,
        slots=_slot_counts(statuses),
        reviews_overdue=overdue,
        awarded_alpha_rao=awarded,
        published_at=published_at,
        updated_at=current.published_at,
    )


async def board(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    repo: str | None = None,
    issue: int | None = None,
) -> BountyBoardPage:
    """Newest bounties first, each with its current terms and slot counts.

    ``repo`` and ``issue`` narrow the board, so a client can find one issue's
    bounty without paging.
    """
    filters = []
    if repo is not None:
        filters.append(Bounty.repo == repo)
    if issue is not None:
        filters.append(Bounty.issue_number == issue)
    total = int(
        await session.scalar(select(func.count()).select_from(Bounty).where(*filters))
        or 0
    )
    bounties = list(
        await session.scalars(
            select(Bounty)
            .where(*filters)
            .order_by(Bounty.created_at.desc(), Bounty.bounty_id)
            .offset(offset)
            .limit(limit)
        )
    )
    ids = [bounty.bounty_id for bounty in bounties]
    # Each bounty's current revision, and when its first was published.
    current: dict[UUID, BountyRevision] = {
        row.bounty_id: row
        for row in await session.scalars(
            select(BountyRevision)
            .options(load_only(*_SUMMARY_COLUMNS))
            .where(BountyRevision.bounty_id.in_(ids))
            .order_by(BountyRevision.bounty_id, BountyRevision.revision.desc())
            .distinct(BountyRevision.bounty_id)
        )
    }
    first_published: dict[UUID, datetime] = dict(
        (
            await session.execute(
                select(BountyRevision.bounty_id, func.min(BountyRevision.published_at))
                .where(BountyRevision.bounty_id.in_(ids))
                .group_by(BountyRevision.bounty_id)
            )
        )
        .tuples()
        .all()
    )
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
    now = bounty_claims.utcnow()
    statuses: dict[UUID, Counter[str]] = {}
    overdue: Counter[UUID] = Counter()
    for slot, holder in (
        await session.execute(
            select(BountySlot, BountyClaim)
            .outerjoin(BountyClaim, BountyClaim.claim_id == BountySlot.claim_id)
            # Only what a slot's effective status and review deadline read.
            .options(
                load_only(
                    BountySlot.bounty_id,
                    BountySlot.status,
                    BountySlot.deadline_at,
                    BountySlot.review_due_at,
                ),
                load_only(BountyClaim.hold_expires_at),
            )
            .where(BountySlot.bounty_id.in_(ids))
        )
    ).all():
        status = effective_slot_status(slot, holder, now=now)
        statuses.setdefault(slot.bounty_id, Counter())[status] += 1
        overdue[slot.bounty_id] += review_overdue(slot, now=now)
    paused = await _paused(session, ids)
    awarded = await _awarded(session, ids)
    items = [
        _summary(
            bounty,
            published_at=first_published[bounty.bounty_id],
            current=current[bounty.bounty_id],
            current_round=int(rounds.get(bounty.bounty_id, 0)),
            paused=paused.get(bounty.bounty_id, False),
            statuses=statuses.get(bounty.bounty_id, Counter()),
            overdue=overdue[bounty.bounty_id],
            awarded=awarded.get(bounty.bounty_id, 0),
        )
        for bounty in bounties
    ]
    return BountyBoardPage(items=items, total=total, limit=limit, offset=offset)


async def _awarded(session: AsyncSession, ids: list[UUID]) -> dict[UUID, int]:
    """Each bounty's recorded awards, which never exceed its reward pool."""
    rows = await session.execute(
        select(BountyObligation.bounty_id, func.sum(BountyObligation.amount_alpha_rao))
        .where(BountyObligation.bounty_id.in_(ids), BountyObligation.kind == "award")
        .group_by(BountyObligation.bounty_id)
    )
    return {bounty_id: int(total) for bounty_id, total in rows.all()}


async def _paused(session: AsyncSession, ids: list[UUID]) -> dict[UUID, bool]:
    """Each bounty's latest pause control; no control means claims are open."""
    latest = (
        select(
            BountyClaimControl.bounty_id,
            BountyClaimControl.paused,
            func.row_number()
            .over(
                partition_by=BountyClaimControl.bounty_id,
                order_by=BountyClaimControl.control_id.desc(),
            )
            .label("rank"),
        )
        .where(BountyClaimControl.bounty_id.in_(ids))
        .subquery()
    )
    rows = await session.execute(
        select(latest.c.bounty_id, latest.c.paused).where(latest.c.rank == 1)
    )
    return {bounty_id: bool(value) for bounty_id, value in rows.all()}


def _slot_view(
    slot: BountySlot, holder: BountyClaim | None, *, now: datetime
) -> BountySlotView:
    status = effective_slot_status(slot, holder, now=now)
    # A lapsed hold has no claimant; every other claimed slot shows its claim.
    claimant = holder if status != "open" else None
    return BountySlotView(
        slot_id=slot.slot_id,
        round=slot.round,
        position=slot.position,
        status=status,  # type: ignore[arg-type]
        # A deadline a read projects is stamped where a write would stamp it.
        status_changed_at=(
            holder.hold_expires_at
            if holder is not None and slot.status == "held" and status == "open"
            else slot.deadline_at
            if status != slot.status and slot.deadline_at is not None
            else slot.status_changed_at
        ),
        hold_expires_at=claimant.hold_expires_at
        if status == "held" and claimant
        else None,
        deadline_at=slot.deadline_at if status in DEADLINE_STATUSES else None,
        review_due_at=slot.review_due_at if status == "submitted" else None,
        review_overdue=review_overdue(slot, now=now),
        claimant=(
            BountySlotClaimant(
                claim_id=claimant.claim_id,
                hotkey=claimant.hotkey,
                payee_coldkey=claimant.payee_coldkey,
                github_login=claimant.github_login,
                revision=claimant.revision,
            )
            if claimant
            else None
        ),
    )


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
    now = bounty_claims.utcnow()
    slots = (
        await session.execute(
            select(BountySlot, BountyClaim)
            .outerjoin(BountyClaim, BountyClaim.claim_id == BountySlot.claim_id)
            .where(BountySlot.bounty_id == bounty_id)
            .order_by(BountySlot.round, BountySlot.position)
        )
    ).all()
    views = [_slot_view(slot, holder, now=now) for slot, holder in slots]
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
            published_at=revisions[0].published_at,
            current=revisions[-1],
            current_round=rounds[-1].round if rounds else 0,
            paused=(await _paused(session, [bounty_id])).get(bounty_id, False),
            statuses=Counter(view.status for view in views),
            overdue=sum(view.review_overdue for view in views),
            awarded=(await _awarded(session, [bounty_id])).get(bounty_id, 0),
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
        slots=views,
        events=[
            BountyEventView(
                event_id=event.event_id,
                kind=event.kind,  # type: ignore[arg-type]
                revision=event.revision,
                round=event.round,
                slot_id=event.slot_id,
                claim_id=event.claim_id,
                recorded_at=event.recorded_at,
            )
            for event in events
        ],
    )
