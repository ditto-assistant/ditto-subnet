"""Build-bounty publication (operators) and the public board (everyone)."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.bounty import (
    REPO_PATTERN,
    BountyAppealDecisionRequest,
    BountyAppealRequest,
    BountyApprovalRequest,
    BountyAwardRequest,
    BountyBoardPage,
    BountyCancelRequest,
    BountyClaimRequest,
    BountyClaimsPauseRequest,
    BountyClaimView,
    BountyConfirmRequest,
    BountyDetail,
    BountyDispatchRequest,
    BountyFactRequest,
    BountyObligationState,
    BountyObligationView,
    BountyOperatorView,
    BountyPayoutPolicyRequest,
    BountyPayoutPolicyView,
    BountyPublishPreview,
    BountyPublishPreviewRequest,
    BountyPublishRequest,
    BountyReassignRequest,
    BountyReconcileRequest,
    BountyReopenRequest,
    BountyReviewQueue,
    BountyReviewRequest,
    BountyRevisionView,
    BountySettlementQueue,
    BountySubmitRequest,
    BountySummary,
    BountyWithdrawRequest,
)
from ditto.api_models.http_errors import HttpErrorEnvelope
from ditto.api_server import (
    bounty,
    bounty_admin,
    bounty_claims,
    bounty_reviews,
    bounty_settlement,
)
from ditto.api_server.contributor_identity import admin_actor
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.no_store import no_store

SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
ActorDep = Annotated[str, Depends(admin_actor)]
# Board state changes on operator and contributor actions; a short shared
# cache absorbs polling without hiding a change for long.
BOARD_CACHE_CONTROL = "public, max-age=15, stale-while-revalidate=60"

public_router = APIRouter(prefix="/public/bounties", tags=["bounties"])
admin_router = APIRouter(
    prefix="/admin/bounties", tags=["bounties"], dependencies=[Depends(no_store)]
)
# Signed contributor actions. Claim state changes by the minute, so never cache.
claims_router = APIRouter(
    prefix="/bounties/claims", tags=["bounties"], dependencies=[Depends(no_store)]
)


def _errors(*statuses: int) -> dict[int | str, dict[str, object]]:
    return {status: {"model": HttpErrorEnvelope} for status in statuses}


@public_router.get("", response_model=BountyBoardPage)
async def board(
    response: Response,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    repo: Annotated[str | None, Query(pattern=REPO_PATTERN)] = None,
    issue: Annotated[int | None, Query(ge=1, le=2147483647)] = None,
) -> BountyBoardPage:
    page = await bounty.board(
        session, limit=limit, offset=offset, repo=repo, issue=issue
    )
    response.headers["Cache-Control"] = BOARD_CACHE_CONTROL
    return page


@public_router.get(
    "/{bounty_id}",
    response_model=BountyDetail,
    responses={404: {"model": HttpErrorEnvelope, "description": "Unknown bounty."}},
)
async def bounty_detail(
    bounty_id: UUID, response: Response, session: SessionDep
) -> BountyDetail:
    view = await bounty.detail(session, bounty_id)
    response.headers["Cache-Control"] = BOARD_CACHE_CONTROL
    return view


@admin_router.post(
    "/preview",
    response_model=BountyPublishPreview,
    responses={status: {"model": HttpErrorEnvelope} for status in (401, 503)},
)
async def preview(
    body: BountyPublishPreviewRequest, _admin: AdminDep, session: SessionDep
) -> BountyPublishPreview:
    """The digest, confirmation phrase and refusals for a revision; writes nothing."""
    return await bounty.preview_revision(session, body)


@admin_router.post(
    "/revisions",
    response_model=BountyRevisionView,
    responses={status: {"model": HttpErrorEnvelope} for status in (400, 401, 409, 503)},
)
async def publish(
    body: BountyPublishRequest, _admin: AdminDep, actor: ActorDep, session: SessionDep
) -> BountyRevisionView:
    return await bounty.publish_revision(session, body, actor=actor)


@admin_router.post(
    "/{bounty_id}/claims-paused",
    response_model=BountySummary,
    responses=_errors(400, 401, 404, 409, 503),
)
async def pause_claims(
    bounty_id: UUID,
    body: BountyClaimsPauseRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
    session: SessionDep,
) -> BountySummary:
    """Pause or resume new holds; existing holds and payments are unaffected."""
    await bounty_claims.set_claims_paused(
        request.app.state.session_maker, bounty_id=bounty_id, body=body, actor=actor
    )
    return (await bounty.detail(session, bounty_id)).summary


@claims_router.post(
    "", response_model=BountyClaimView, responses=_errors(400, 403, 404, 409, 429, 503)
)
async def hold(body: BountyClaimRequest, request: Request) -> BountyClaimView:
    """Verify a signed claim and hold one open slot until the fee is confirmed.

    An exact retry of the same signed claim returns the original hold.
    """
    state = request.app.state
    return await bounty_claims.hold_claim(
        state.session_maker,
        chain=state.chain,
        github=state.github_link,
        body=body,
        netuid=state.config.chain.netuid,
    )


@claims_router.post(
    "/{claim_id}/confirm",
    response_model=BountyClaimView,
    responses=_errors(402, 404, 409, 429, 503),
)
async def confirm(
    claim_id: UUID, body: BountyConfirmRequest, request: Request
) -> BountyClaimView:
    """Record a verified fee: claim the held slot, or owe a full refund.

    A receipt is charged once; resending it returns the recorded outcome.
    """
    state = request.app.state
    return await bounty_claims.confirm_claim(
        state.session_maker,
        chain=state.chain,
        verifier=state.bounty_fee_verifier,
        claim_id=claim_id,
        body=body,
    )


@claims_router.get(
    "/{claim_id}", response_model=BountyClaimView, responses=_errors(404, 429)
)
async def claim(claim_id: UUID, session: SessionDep) -> BountyClaimView:
    return await bounty_claims.read_claim(session, claim_id)


def _same_claim(path_claim: UUID, body_claim: UUID) -> None:
    if path_claim != body_claim:
        raise HTTPException(400, "claim_id does not match the route")


@claims_router.post(
    "/{claim_id}/submissions",
    response_model=BountyClaimView,
    responses=_errors(400, 403, 404, 409, 429),
)
async def submit(
    claim_id: UUID, body: BountySubmitRequest, request: Request
) -> BountyClaimView:
    """Submit the signed exact commit of a pull request for review."""
    _same_claim(claim_id, body.claim_id)
    state = request.app.state
    return await bounty_reviews.submit_work(
        state.session_maker, body=body, netuid=state.config.chain.netuid
    )


@claims_router.post(
    "/{claim_id}/withdraw",
    response_model=BountyClaimView,
    responses=_errors(400, 403, 404, 409, 429),
)
async def withdraw(
    claim_id: UUID, body: BountyWithdrawRequest, request: Request
) -> BountyClaimView:
    """Give up a claimed or rework slot; the entry fee is retained."""
    _same_claim(claim_id, body.claim_id)
    state = request.app.state
    return await bounty_reviews.withdraw_claim(
        state.session_maker, body=body, netuid=state.config.chain.netuid
    )


@claims_router.post(
    "/{claim_id}/appeals",
    response_model=BountyClaimView,
    responses=_errors(400, 403, 404, 409, 429),
)
async def appeal(
    claim_id: UUID, body: BountyAppealRequest, request: Request
) -> BountyClaimView:
    """Appeal a rejection or a missed deadline of this claim, once each."""
    _same_claim(claim_id, body.claim_id)
    state = request.app.state
    return await bounty_reviews.file_appeal(
        state.session_maker, body=body, netuid=state.config.chain.netuid
    )


@admin_router.post(
    "/claims/{claim_id}/review",
    response_model=BountyClaimView,
    responses=_errors(400, 401, 403, 404, 409, 503),
)
async def review(
    claim_id: UUID,
    body: BountyReviewRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
) -> BountyClaimView:
    """The assigned reviewer's decision on one exact submission."""
    return await bounty_reviews.review_submission(
        request.app.state.session_maker, claim_id=claim_id, body=body, actor=actor
    )


@admin_router.post(
    "/{bounty_id}/reviewer",
    response_model=BountySummary,
    responses=_errors(400, 401, 404, 409, 503),
)
async def reassign(
    bounty_id: UUID,
    body: BountyReassignRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
    session: SessionDep,
) -> BountySummary:
    """Name a new reviewer; overdue reviews get one more review window."""
    await bounty_reviews.reassign_reviewer(
        request.app.state.session_maker, bounty_id=bounty_id, body=body, actor=actor
    )
    return (await bounty.detail(session, bounty_id)).summary


@admin_router.post(
    "/slots/{slot_id}/cancel",
    response_model=BountySummary,
    responses=_errors(400, 401, 404, 409, 503),
)
async def cancel(
    slot_id: UUID,
    body: BountyCancelRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
    session: SessionDep,
) -> BountySummary:
    """Cancel a slot; a paid entry fee is refunded in full."""
    bounty_id = await bounty_reviews.cancel_slot(
        request.app.state.session_maker, slot_id=slot_id, body=body, actor=actor
    )
    return (await bounty.detail(session, bounty_id)).summary


@admin_router.post(
    "/slots/{slot_id}/reopen",
    response_model=BountySummary,
    responses=_errors(400, 401, 404, 409, 503),
)
async def reopen(
    slot_id: UUID,
    body: BountyReopenRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
    session: SessionDep,
) -> BountySummary:
    """Reopen a closed slot for a new claim."""
    bounty_id = await bounty_reviews.reopen_slot(
        request.app.state.session_maker, slot_id=slot_id, body=body, actor=actor
    )
    return (await bounty.detail(session, bounty_id)).summary


@admin_router.post(
    "/appeals/{appeal_id}/decision",
    response_model=BountyClaimView,
    responses=_errors(400, 401, 403, 404, 409, 503),
)
async def decide_appeal(
    appeal_id: UUID,
    body: BountyAppealDecisionRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
) -> BountyClaimView:
    """Uphold or deny an appeal; its reviewer may not decide it."""
    return await bounty_reviews.decide_appeal(
        request.app.state.session_maker, appeal_id=appeal_id, body=body, actor=actor
    )


@admin_router.post(
    "/claims/{claim_id}/award",
    response_model=BountyClaimView,
    responses=_errors(400, 401, 404, 409, 503),
)
async def award(
    claim_id: UUID,
    body: BountyAwardRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
) -> BountyClaimView:
    """Award accepted work from the reward pool, with public credit."""
    return await bounty_settlement.award_claim(
        request.app.state.session_maker, claim_id=claim_id, body=body, actor=actor
    )


@admin_router.get(
    "/payout-policy",
    response_model=BountyPayoutPolicyView,
    responses=_errors(401, 404, 503),
)
async def payout_policy(
    _admin: AdminDep, session: SessionDep
) -> BountyPayoutPolicyView:
    return await bounty_settlement.read_payout_policy(session)


@admin_router.post(
    "/payout-policy",
    response_model=BountyPayoutPolicyView,
    responses=_errors(400, 401, 409, 503),
)
async def set_payout_policy(
    body: BountyPayoutPolicyRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
) -> BountyPayoutPolicyView:
    """Append a revision of the approval thresholds; approvals bind it."""
    return await bounty_settlement.set_payout_policy(
        request.app.state.session_maker, body=body, actor=actor
    )


@admin_router.post(
    "/obligations/{obligation_id}/approve",
    response_model=BountyObligationView,
    responses=_errors(401, 403, 404, 409, 503),
)
async def approve(
    obligation_id: UUID,
    body: BountyApprovalRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
) -> BountyObligationView:
    """Approve the exact payee, amount and policy of one obligation round."""
    return await bounty_settlement.approve_obligation(
        request.app.state.session_maker,
        chain=request.app.state.chain,
        obligation_id=obligation_id,
        body=body,
        actor=actor,
    )


@admin_router.post(
    "/obligations/{obligation_id}/dispatch",
    response_model=BountyObligationView,
    responses=_errors(400, 401, 404, 409, 503),
)
async def dispatch(
    obligation_id: UUID,
    body: BountyDispatchRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
) -> BountyObligationView:
    """Dispatch an approved round once through the transfer executor."""
    state = request.app.state
    return await bounty_settlement.dispatch_obligation(
        state.session_maker,
        chain=state.chain,
        executor=state.bounty_transfer_executor,
        obligation_id=obligation_id,
        body=body,
        actor=actor,
    )


@admin_router.post(
    "/transfers/{transfer_id}/reconcile",
    response_model=BountyObligationView,
    responses=_errors(400, 401, 404, 409, 503),
)
async def reconcile(
    transfer_id: UUID,
    body: BountyReconcileRequest,
    request: Request,
    _admin: AdminDep,
    _actor: ActorDep,
) -> BountyObligationView:
    """Settle a dispatched or uncertain transfer before anything else."""
    state = request.app.state
    return await bounty_settlement.reconcile_transfer(
        state.session_maker,
        executor=state.bounty_transfer_executor,
        transfer_id=transfer_id,
        body=body,
    )


@admin_router.post(
    "/claims/{claim_id}/facts",
    response_model=BountyClaimView,
    responses=_errors(400, 401, 404, 409, 503),
)
async def fact(
    claim_id: UUID,
    body: BountyFactRequest,
    request: Request,
    _admin: AdminDep,
    actor: ActorDep,
) -> BountyClaimView:
    """Record a public merge, deployment or adoption fact for accepted work."""
    return await bounty_settlement.record_fact(
        request.app.state.session_maker, claim_id=claim_id, body=body, actor=actor
    )


# Operator reads. Static paths are declared before the bounty-id path.
@admin_router.get(
    "/queue/reviews", response_model=BountyReviewQueue, responses=_errors(401, 503)
)
async def review_queue(
    _admin: AdminDep,
    session: SessionDep,
    overdue_only: Annotated[bool, Query()] = False,
) -> BountyReviewQueue:
    """Submissions awaiting a decision, the most overdue first."""
    return await bounty_admin.review_queue(session, overdue_only=overdue_only)


@admin_router.get(
    "/queue/settlement",
    response_model=BountySettlementQueue,
    responses=_errors(401, 503),
)
async def settlement_queue(
    _admin: AdminDep,
    session: SessionDep,
    state: Annotated[BountyObligationState | None, Query()] = None,
) -> BountySettlementQueue:
    """Obligations that still need an operator, with their approvers."""
    return await bounty_admin.settlement_queue(session, state=state)


@admin_router.get(
    "/{bounty_id}",
    response_model=BountyOperatorView,
    responses=_errors(401, 404, 503),
)
async def operator_view(
    bounty_id: UUID, _admin: AdminDep, session: SessionDep
) -> BountyOperatorView:
    """The public detail plus every private actor, reason and statement."""
    return await bounty_admin.operator_view(session, bounty_id)
