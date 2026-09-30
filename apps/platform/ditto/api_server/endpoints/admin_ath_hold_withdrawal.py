"""Preview and execute withdrawal of an unsupported precautionary ATH hold.

``clear`` would record a terminal certification and ``reject`` would record a
violation. This route records ``withdraw`` instead: the opening rationale is
retracted, the pre-hold score and rank presentation returns, and reward
eligibility uses the canonical exact-artifact policy and fleet compatibility gate.
A withdrawal does not certify a terminal clear.

Only a manual hold whose review never carried a ``clear`` or ``reject`` can be
withdrawn. A reopened ruling is settled by another ruling: withdrawing a
reopened reject would restore a banned artifact, and withdrawing a re-hold of a
cleared artifact would strip its certification.

The preview token binds the operator, the review and its lifecycle version (the
action ledger and ``reopened_at``), the artifact guards, the public correction
reason, and the board and emission effect. Execute re-reads all of them under
the review's row lock and refuses a stale or replayed request, including a
token issued before the review was withdrawn and reopened.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.admin_ath_hold_withdrawal import (
    AdminAthHoldWithdrawalExecuteRequest,
    AdminAthHoldWithdrawalExecuteResponse,
    AdminAthHoldWithdrawalPreviewRequest,
    AdminAthHoldWithdrawalPreviewResponse,
)
from ditto.api_models.emission_eligibility import AgentEmissionEligibility
from ditto.api_server.ath_hold_withdrawal import (
    WITHDRAW_CONFIRMATION,
    lifecycle_version,
    withdrawal_emission_reason,
    withdrawal_refusal,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.emission_eligibility import classify, effective_policy
from ditto.api_server.endpoints.admin_ath_rulings import (
    _TOKEN_VERSION,
    _crown_effect,
    _require_actor,
    _sign_preview,
    _verify_preview,
    read_board_snapshot,
)
from ditto.api_server.endpoints.admin_copy_review import (
    _get_review,
    _item,
    _review_actions,
    _review_coldkeys,
)
from ditto.api_server.endpoints.admin_quarantine import (
    BATCH_PREVIEW_TTL,
    require_admin,
)
from ditto.api_server.endpoints.scoring import resolve_ledger_context
from ditto.db.models import AgentStatus, AthReview, AthReviewAction, Score
from ditto.db.queries.emission_eligibility import load_review_postures

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

# Signed and verified by the rulings court's helpers (same secret, TTL and
# version); ``kind`` keeps a rulings token from being replayed here.
_TOKEN_KIND = "ath_hold_withdrawal"


def _verify_withdrawal_preview(token: str, secret: str, actor: str) -> dict[str, Any]:
    """The rulings court's signed-token check, narrowed to withdrawal tokens."""
    payload = _verify_preview(token, secret, actor)
    if payload.get("kind") != _TOKEN_KIND:
        raise HTTPException(status_code=422, detail="unsupported preview token")
    return payload


async def _score_count(session: AsyncSession, agent_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Score).where(Score.agent_id == agent_id)
        )
        or 0
    )


def _previous_status(review: AthReview, actions: list[AthReviewAction]) -> str | None:
    """The status the newest (re)open held the agent from.

    ``actions`` is ordered oldest first, so the last ``reopen`` is the newest.
    """
    latest_reopen = next(
        (action for action in reversed(actions) if action.action == "reopen"), None
    )
    evidence = (
        latest_reopen.evidence
        if latest_reopen is not None
        else review.original_evidence
    )
    previous = evidence.get("previous_status") if isinstance(evidence, dict) else None
    return previous if isinstance(previous, str) else None


def _restored_status(previous_status: str | None) -> Literal["scored", "live"]:
    if previous_status == AgentStatus.LIVE.value:
        return "live"
    return "scored"


@dataclass(frozen=True)
class _GuardedHold:
    review: AthReview
    agent: Any
    score_count: int
    previous_status: str | None
    lifecycle: dict[str, Any]
    decision: AgentEmissionEligibility


async def _guarded_hold(
    session: AsyncSession,
    agent_id: UUID,
    payload: AdminAthHoldWithdrawalPreviewRequest,
    *,
    lock: bool,
    request: Request,
) -> _GuardedHold:
    """Re-read the hold and refuse any guard that no longer matches.

    With ``lock`` the review row is held ``FOR UPDATE``. Every ledger writer
    (open, reopen, resolve, withdraw) takes that lock before appending, so the
    ledger read here cannot move until the transaction ends.
    """
    row = await _get_review(session, agent_id, lock=lock)
    if row is None:
        raise HTTPException(status_code=404, detail="copy review not found")
    review, agent = row
    if review.review_id != payload.review_id:
        raise HTTPException(status_code=409, detail="review id does not match")
    if agent.sha256 != payload.expected_sha256:
        raise HTTPException(status_code=409, detail="artifact sha256 changed")
    score_count = await _score_count(session, agent.agent_id)
    if score_count != payload.expected_score_count:
        raise HTTPException(status_code=409, detail="score count changed")
    actions = await _review_actions(session, review.review_id)
    refusal = withdrawal_refusal(review, agent, actions)
    # A completed withdrawal restores agent status, so a replay must say the
    # review is already withdrawn rather than looking like the artifact moved.
    if review.status == "pending" and (
        agent.status.value != payload.expected_agent_status
    ):
        raise HTTPException(status_code=409, detail="agent status changed")
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    previous = _previous_status(review, actions)
    now = datetime.now(UTC)
    context = await resolve_ledger_context(request.app.state, session, now=now)
    policy = effective_policy(
        context.policy.reward_eligibility,
        fleet_ready=context.reward_eligibility_fleet_ready,
    )
    postures = await load_review_postures(session, [agent.agent_id])
    decision = classify(
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
        bench_version=context.active_bench_version,
        posture=replace(
            postures[agent.agent_id],
            review_status="resolved",
            review_resolution="withdraw",
            review_resolved_at=now,
        ),
        policy=policy,
        now=now,
    )
    return _GuardedHold(
        review=review,
        agent=agent,
        score_count=score_count,
        previous_status=previous,
        lifecycle=lifecycle_version(review, actions),
        decision=decision,
    )


async def _board_effect(session: AsyncSession, request: Request, agent: Any):
    board = await read_board_snapshot(session, request)
    would_change_crown, after = await _crown_effect(
        session,
        board=board,
        agent=agent,
        action="withdraw",
    )
    return board, after, would_change_crown


@router.post(
    "/copy-reviews/{agent_id}/withdraw/preview",
    response_model=AdminAthHoldWithdrawalPreviewResponse,
)
async def preview_ath_hold_withdrawal(
    agent_id: UUID,
    payload: AdminAthHoldWithdrawalPreviewRequest,
    _admin: AdminDep,
    session: SessionDep,
    request: Request,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminAthHoldWithdrawalPreviewResponse:
    actor = _require_actor(x_admin_actor)
    hold = await _guarded_hold(session, agent_id, payload, lock=False, request=request)
    review, agent, decision = hold.review, hold.agent, hold.decision
    board, after, would_change_crown = await _board_effect(session, request, agent)
    restored = _restored_status(hold.previous_status)
    would_change_emission_crown = bool(decision.reward_eligible and would_change_crown)
    issued_at = int(datetime.now(UTC).timestamp())
    secret = request.app.state.config.admin_api_token
    assert secret is not None
    token_payload = {
        "v": _TOKEN_VERSION,
        "kind": _TOKEN_KIND,
        "actor": actor,
        "agent_id": str(agent.agent_id),
        "review_id": str(review.review_id),
        "lifecycle": hold.lifecycle,
        "sha256": agent.sha256,
        "score_count": hold.score_count,
        "agent_status": agent.status.value,
        "reason": payload.reason,
        "restored_status": restored,
        "board_before_fingerprint": board.fingerprint,
        "board_after_fingerprint": after.fingerprint,
        "would_change_crown": would_change_crown,
        "would_change_emission_crown": would_change_emission_crown,
        "emission_reward_eligible": decision.reward_eligible,
        "emission_gate": decision.enforcement,
        "eligibility_revision": decision.policy_revision,
        "eligibility_checksum": decision.policy_checksum,
        "eligibility_state": decision.state,
    }
    return AdminAthHoldWithdrawalPreviewResponse(
        agent_id=agent.agent_id,
        review_id=review.review_id,
        artifact_sha256=agent.sha256,
        score_count=hold.score_count,
        agent_status=agent.status.value,
        restored_status=restored,
        board_before=board.projection_model(),
        board_after=after.projection_model(),
        would_change_crown=would_change_crown,
        emission_reward_eligible=decision.reward_eligible,
        emission_gate=decision.enforcement,
        would_change_emission_crown=would_change_emission_crown,
        emission_reason=withdrawal_emission_reason(decision),
        preview_token=_sign_preview(secret, token_payload, issued_at),
        expires_at=datetime.fromtimestamp(issued_at, UTC) + BATCH_PREVIEW_TTL,
    )


@router.post(
    "/copy-reviews/{agent_id}/withdraw",
    response_model=AdminAthHoldWithdrawalExecuteResponse,
)
async def execute_ath_hold_withdrawal(
    agent_id: UUID,
    payload: AdminAthHoldWithdrawalExecuteRequest,
    _admin: AdminDep,
    session: SessionDep,
    request: Request,
    x_admin_actor: Annotated[str | None, Header()] = None,
) -> AdminAthHoldWithdrawalExecuteResponse:
    actor = _require_actor(x_admin_actor)
    if payload.confirmation != WITHDRAW_CONFIRMATION:
        raise HTTPException(
            status_code=422,
            detail=f"confirmation must be {WITHDRAW_CONFIRMATION}",
        )
    secret = request.app.state.config.admin_api_token
    assert secret is not None
    token = _verify_withdrawal_preview(payload.preview_token, secret, actor)
    if token.get("agent_id") != str(agent_id) or token.get("review_id") != str(
        payload.review_id
    ):
        raise HTTPException(
            status_code=409, detail="preview token does not match this request"
        )
    if (
        token.get("sha256") != payload.expected_sha256
        or token.get("score_count") != payload.expected_score_count
        or token.get("agent_status") != payload.expected_agent_status
        or token.get("reason") != payload.reason
    ):
        raise HTTPException(
            status_code=409, detail="preview token does not match this request"
        )
    async with session.begin():
        hold = await _guarded_hold(
            session, agent_id, payload, lock=True, request=request
        )
        review, agent, decision = hold.review, hold.agent, hold.decision
        restored = _restored_status(hold.previous_status)
        # Under the row lock the ledger cannot move, so a token issued before a
        # withdraw -> reopen (or any other ledger write) is refused here even
        # when every artifact guard still matches.
        if (
            token.get("lifecycle") != hold.lifecycle
            or token.get("restored_status") != restored
        ):
            raise HTTPException(
                status_code=409, detail="review lifecycle changed; preview again"
            )
        if (
            token.get("emission_reward_eligible") != decision.reward_eligible
            or token.get("emission_gate") != decision.enforcement
            or token.get("eligibility_revision") != decision.policy_revision
            or token.get("eligibility_checksum") != decision.policy_checksum
            or token.get("eligibility_state") != decision.state
        ):
            raise HTTPException(
                status_code=409,
                detail="emission eligibility policy changed; preview again",
            )
        board, after, would_change_crown = await _board_effect(session, request, agent)
        would_change_emission_crown = bool(
            decision.reward_eligible and would_change_crown
        )
        if (
            token.get("board_before_fingerprint") != board.fingerprint
            or token.get("board_after_fingerprint") != after.fingerprint
            or token.get("would_change_crown") != would_change_crown
            or token.get("would_change_emission_crown") != would_change_emission_crown
        ):
            raise HTTPException(status_code=409, detail="board changed; preview again")
        now = datetime.now(UTC)
        agent.status = AgentStatus(restored)
        review.status = "resolved"
        review.resolved_at = now
        review.resolved_by = actor
        review.resolution = "withdraw"
        review.resolution_reason = payload.reason
        session.add(
            AthReviewAction(
                action_id=uuid4(),
                review_id=review.review_id,
                action="withdraw",
                reason=payload.reason,
                actor=actor,
                evidence={
                    "previous_status": hold.previous_status,
                    "sha256": agent.sha256,
                    "score_count": hold.score_count,
                    "agent_status": payload.expected_agent_status,
                    "emission_gate": decision.enforcement,
                    "eligibility_revision": decision.policy_revision,
                    "eligibility_checksum": decision.policy_checksum,
                    "eligibility_state": decision.state,
                    "emission_reward_eligible": decision.reward_eligible,
                },
                created_at=now,
            )
        )
        await session.flush()
        matched = None
    candidate_coldkey, reference_coldkey = await _review_coldkeys(
        session, agent, matched
    )
    return AdminAthHoldWithdrawalExecuteResponse(
        review=_item(
            review,
            agent,
            matched,
            miner_coldkey=candidate_coldkey,
            duplicate_of_coldkey=reference_coldkey,
        ),
        agent_status=agent.status.value,
        restored_status=restored,
        emission_reward_eligible=decision.reward_eligible,
        emission_gate=decision.enforcement,
        emission_reason=withdrawal_emission_reason(decision),
    )
