"""Preview and execute withdrawal of an unsupported precautionary ATH hold.

``clear`` would record a terminal certification and ``reject`` would record a
violation. This route records ``withdraw`` instead: the opening rationale is
retracted, the pre-hold score and rank presentation returns, and reward
eligibility stays on the terminal exact-artifact gate. While that gate is
unavailable the withdrawal fails closed on emissions.

The preview token binds the operator, the review, the artifact guards, the
public correction reason, and the board and emission effect. Execute re-reads
all of them and refuses a stale or replayed request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
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
from ditto.api_server.ath_hold_withdrawal import (
    WITHDRAW_CONFIRMATION,
    WithdrawalRewardDecision,
    is_manual_precautionary_hold,
    withdrawal_reward_decision,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_ath_rulings import (
    _crown_effect,
    read_board_snapshot,
)
from ditto.api_server.endpoints.admin_copy_review import (
    _get_review,
    _item,
    _review_coldkeys,
)
from ditto.api_server.endpoints.admin_quarantine import (
    BATCH_PREVIEW_TTL,
    require_admin,
)
from ditto.db.models import AgentStatus, AthReview, AthReviewAction, Score

router = APIRouter(prefix="/admin", tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]

_TOKEN_VERSION = 1
_TOKEN_KIND = "ath_hold_withdrawal"


def _require_actor(x_admin_actor: str | None) -> str:
    actor = x_admin_actor.strip() if x_admin_actor is not None else ""
    if not 1 <= len(actor) <= 120:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return actor


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign_preview(secret: str, payload: dict[str, Any], issued_at: int) -> str:
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signed = f"{issued_at}.{body}"
    digest = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"{signed}.{digest}"


def _verify_preview(token: str, secret: str, actor: str) -> dict[str, Any]:
    try:
        issued_text, body, digest = token.split(".", 2)
        issued_at = int(issued_text)
        payload = json.loads(_unb64(body))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="invalid preview token") from None
    expected = hmac.new(
        secret.encode(), f"{issued_at}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    if not secrets.compare_digest(digest.encode(), expected.encode()):
        raise HTTPException(status_code=409, detail="preview token signature mismatch")
    now = int(datetime.now(UTC).timestamp())
    if issued_at > now + 30 or now - issued_at > int(BATCH_PREVIEW_TTL.total_seconds()):
        raise HTTPException(status_code=409, detail="preview expired; preview again")
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _TOKEN_VERSION
        or payload.get("kind") != _TOKEN_KIND
    ):
        raise HTTPException(status_code=422, detail="unsupported preview token")
    if payload.get("actor") != actor:
        raise HTTPException(
            status_code=409, detail="preview token was issued to another operator"
        )
    return payload


async def _score_count(session: AsyncSession, agent_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Score).where(Score.agent_id == agent_id)
        )
        or 0
    )


async def _previous_status(session: AsyncSession, review: AthReview) -> str | None:
    latest_reopen = await session.scalar(
        select(AthReviewAction)
        .where(
            AthReviewAction.review_id == review.review_id,
            AthReviewAction.action == "reopen",
        )
        .order_by(AthReviewAction.created_at.desc(), AthReviewAction.action_id.desc())
        .limit(1)
    )
    previous = (
        latest_reopen.evidence.get("previous_status")
        if latest_reopen is not None
        else review.original_evidence.get("previous_status")
    )
    return previous if isinstance(previous, str) else None


def _restored_status(previous_status: str | None) -> Literal["scored", "live"]:
    if previous_status == AgentStatus.LIVE.value:
        return "live"
    return "scored"


async def _guarded_hold(
    session: AsyncSession,
    agent_id: UUID,
    payload: AdminAthHoldWithdrawalPreviewRequest,
    *,
    lock: bool,
) -> tuple[AthReview, Any, int, str | None, WithdrawalRewardDecision]:
    """Re-read the hold and refuse any guard that no longer matches."""
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
    # A completed withdrawal restores agent status, so a replay must say the
    # review is already withdrawn rather than looking like the artifact moved.
    if review.status != "pending":
        if review.resolution == "withdraw":
            raise HTTPException(status_code=409, detail="review already withdrawn")
        raise HTTPException(status_code=409, detail="review already resolved")
    if agent.status.value != payload.expected_agent_status:
        raise HTTPException(status_code=409, detail="agent status changed")
    if agent.status != AgentStatus.ATH_PENDING_REVIEW:
        raise HTTPException(status_code=409, detail="agent is no longer held")
    if not is_manual_precautionary_hold(review.algorithm_provenance):
        raise HTTPException(
            status_code=409,
            detail="only a manual precautionary hold can be withdrawn",
        )
    if agent.duplicate_of != review.original_duplicate_of:
        raise HTTPException(
            status_code=409, detail="agent hold evidence no longer matches review"
        )
    if agent.review_reason != review.original_reason:
        raise HTTPException(
            status_code=409, detail="agent hold reason no longer matches review"
        )
    previous = await _previous_status(session, review)
    decision = withdrawal_reward_decision(
        agent_id=agent.agent_id,
        artifact_sha256=agent.sha256,
    )
    return review, agent, score_count, previous, decision


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
    review, agent, score_count, previous, decision = await _guarded_hold(
        session, agent_id, payload, lock=False
    )
    board, after, would_change_crown = await _board_effect(session, request, agent)
    restored = _restored_status(previous)
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
        "sha256": agent.sha256,
        "score_count": score_count,
        "agent_status": agent.status.value,
        "reason": payload.reason,
        "restored_status": restored,
        "board_before_fingerprint": board.fingerprint,
        "board_after_fingerprint": after.fingerprint,
        "would_change_crown": would_change_crown,
        "would_change_emission_crown": would_change_emission_crown,
        "emission_reward_eligible": decision.reward_eligible,
        "emission_gate": decision.gate,
    }
    return AdminAthHoldWithdrawalPreviewResponse(
        agent_id=agent.agent_id,
        review_id=review.review_id,
        artifact_sha256=agent.sha256,
        score_count=score_count,
        agent_status=agent.status.value,
        restored_status=restored,
        board_before=board.projection_model(),
        board_after=after.projection_model(),
        would_change_crown=would_change_crown,
        emission_reward_eligible=decision.reward_eligible,
        emission_gate=decision.gate,
        would_change_emission_crown=would_change_emission_crown,
        emission_reason=decision.reason,
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
    token = _verify_preview(payload.preview_token, secret, actor)
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
        review, agent, score_count, previous, decision = await _guarded_hold(
            session, agent_id, payload, lock=True
        )
        board, after, would_change_crown = await _board_effect(session, request, agent)
        restored = _restored_status(previous)
        would_change_emission_crown = bool(
            decision.reward_eligible and would_change_crown
        )
        if (
            token.get("board_before_fingerprint") != board.fingerprint
            or token.get("board_after_fingerprint") != after.fingerprint
            or token.get("would_change_crown") != would_change_crown
            or token.get("would_change_emission_crown") != would_change_emission_crown
            or token.get("restored_status") != restored
            or token.get("emission_reward_eligible") != decision.reward_eligible
            or token.get("emission_gate") != decision.gate
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
                    "previous_status": previous,
                    "sha256": agent.sha256,
                    "score_count": score_count,
                    "agent_status": payload.expected_agent_status,
                    "emission_gate": decision.gate,
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
        emission_gate=decision.gate,
        emission_reason=decision.reason,
    )
