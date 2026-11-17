"""Self-serve paid readmission into the active benchmark era (SN-485).

``POST /readmission/check`` is the pre-payment dry run and price reservation.
``POST /readmission/commit`` verifies the finalized payment and admits the
agent through the same guarded write as the operator's stranded admission
(:func:`~ditto.api_server.benchmark_rollout.admit_stranded_previous_era_agent`),
with ``origin = paid_readmission`` and the payment recorded in the same
transaction.

Order of refusal:

* everything the check can know is refused **before payment** (the gate, the
  owner, every eligibility guard, the cooldown, ban, CLA);
* a payment proof the verifier rejects (3201-3206), or one another lane spent
  (3207), is refused and spends nothing, exactly as for uploads;
* anything discovered only **after** a verified payment (the era moved, a
  rollout opened, the agent was withdrawn, the cooldown, a lost race) banks the
  payment as a 30-day credit and answers ``outcome = credited``. Ditto never
  refunds on chain.

Nothing here runs unless :func:`~ditto.db.queries.readmission_gate.\
readmission_activation` is active.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

import bittensor
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.readmission import (
    ReadmissionCheckRequest,
    ReadmissionCheckResponse,
    ReadmissionCommitRequest,
    ReadmissionCommitResponse,
    ReadmissionEligibilityView,
    ReadmissionOperation,
    readmission_signature_payload,
)
from ditto.api_server.benchmark_rollout import admit_stranded_previous_era_agent
from ditto.api_server.cla import (
    CLA_REQUIRED_ERROR_CODE,
    ClaConsentRequired,
    evaluate_cla_gate,
)
from ditto.api_server.datapipeline import DataPipelineError, DatasetGenerator
from ditto.api_server.dependencies import (
    get_chain_client,
    get_dataset_generator,
    get_payment_verifier,
    get_session,
)
from ditto.api_server.payment_verifier import (
    PaymentProof,
    PaymentRecoveryExpired,
    PaymentReplayedError,
    PaymentVerifier,
    VerifiedPayment,
)
from ditto.api_server.readmission import activation_state
from ditto.api_server.upload_feedback import (
    hotkey_not_registered_message,
    submission_cooldown_message,
)
from ditto.chain import ChainError
from ditto.db.models import ReadmissionPayment
from ditto.db.queries.agents import (
    SubmissionCooldownError,
    submission_cooldown_decision,
)
from ditto.db.queries.bans import is_hotkey_banned
from ditto.db.queries.benchmark_rollout import RolloutConflictError
from ditto.db.queries.contributor_identity import lock_identity_writes
from ditto.db.queries.payments import (
    READMISSION_CREDIT_TTL,
    ReadmissionAlreadyAdmittedError,
    ReadmissionTargetError,
    credit_expired,
    credit_expires_at,
    get_evaluation_payment_for_proof,
    get_generator_payment_for_proof,
    get_readmission_payment_for_proof,
    insert_readmission_payment,
    spend_credit_on_readmission,
)
from ditto.db.queries.readmission import (
    ERROR_CODE_READMISSION_INACTIVE,
    consume_readmission_admission,
    get_readmission_reservation,
    readmission_eligibility,
    reservation_live,
    reserve_readmission,
)
from ditto.db.queries.readmission_gate import (
    ReadmissionActivation,
    current_readmission_activation,
    readmission_fee_rao,
)
from ditto.db.queries.stranded_admission import (
    ORIGIN_PAID_READMISSION,
    StrandedAdmissionTarget,
    StrandedAgentNotFoundError,
)
from ditto.db.queries.submission_settings import (
    effective_submission_settings,
)

if TYPE_CHECKING:
    from uuid import UUID

    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/readmission", tags=["readmission"])

ChainDep = Annotated["ChainClient", Depends(get_chain_client)]
PaymentVerifierDep = Annotated[PaymentVerifier, Depends(get_payment_verifier)]
GeneratorDep = Annotated[DatasetGenerator, Depends(get_dataset_generator)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

READMISSION_SIGNATURE_MAX_SKEW_SECONDS = 300
_ERROR_CODE_BAD_SIGNATURE = 1100
_ERROR_CODE_HOTKEY_NOT_REGISTERED = 1101
_ERROR_CODE_HOTKEY_BANNED = 1103
_ERROR_CODE_SUBMISSION_COOLDOWN = 1105
_PAYMENT_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class _CreditSnapshot:
    """What the commit needs from the credit that pays, read before release."""

    miner_coldkey: str
    expires_at: datetime


def _verify_signature(
    body: ReadmissionCheckRequest | ReadmissionCommitRequest,
    *,
    operation: ReadmissionOperation,
) -> bool:
    if (
        body.signature_nonce.version != 4
        or abs(time.time() - body.signature_timestamp)
        > READMISSION_SIGNATURE_MAX_SKEW_SECONDS
    ):
        return False
    payload = readmission_signature_payload(
        operation=operation,
        hotkey=body.hotkey,
        agent_id=body.agent_id,
        bench_version=body.bench_version,
        signature_timestamp=body.signature_timestamp,
        signature_nonce=body.signature_nonce,
    )
    try:
        keypair = bittensor.Keypair(ss58_address=body.hotkey)
        return bool(keypair.verify(payload, bytes.fromhex(body.signature)))
    except (ValueError, TypeError):
        return False


async def _release(session: AsyncSession) -> None:
    """End an autobegun read transaction before slow chain work."""
    if session.in_transaction():
        result = session.rollback()
        if inspect.isawaitable(result):
            await result


def _inactive_message(activation: ReadmissionActivation) -> str:
    reasons = {
        "announcement_unset": "it has not been announced",
        "bench_version_below_minimum": (
            f"it applies from benchmark v{activation.min_bench_version} "
            f"(active: v{activation.active_bench_version})"
        ),
        "notice_period": (
            "the notice period runs until "
            + (
                activation.effective_at.isoformat()
                if activation.effective_at is not None
                else "the announcement"
            )
        ),
    }
    return "paid readmission is not available yet: " + "; ".join(
        reasons[reason] for reason in activation.inactive_reasons
    )


@router.post("/check", response_model=ReadmissionCheckResponse)
async def check(
    request: Request,
    body: ReadmissionCheckRequest,
    chain: ChainDep,
    session: SessionDep,
) -> ReadmissionCheckResponse:
    """Pre-payment dry run: every reason this readmission would be refused.

    With ``reserve_slot`` and no refusal, reserves the price (exactly the
    upload fee) and the payment address for 24 hours. A miner should pay only
    after ``ok`` with an ``admission_token``.
    """
    chain_config = request.app.state.config.chain
    codes: list[int] = []
    messages: list[str] = []

    def refuse(code: int, message: str) -> None:
        codes.append(code)
        messages.append(message)

    signature_valid = _verify_signature(
        body, operation="reserve" if body.reserve_slot else "check"
    )
    if not signature_valid:
        refuse(_ERROR_CODE_BAD_SIGNATURE, "signature did not verify against the hotkey")

    settings = await effective_submission_settings(
        session, default_payment_address=request.app.state.config.upload_payment_address
    )
    activation = await current_readmission_activation(session, settings=settings)
    if not activation.active:
        refuse(ERROR_CODE_READMISSION_INACTIVE, _inactive_message(activation))

    await _release(session)
    try:
        registration = await chain.get_hotkey_registration(
            body.hotkey, netuid=chain_config.netuid, confirm_absence=signature_valid
        )
    except ChainError as exc:
        logger.warning("chain unreachable during /readmission/check: %s", exc)
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from exc
    owner_coldkey = registration.coldkey
    if not registration.registered:
        refuse(
            _ERROR_CODE_HOTKEY_NOT_REGISTERED,
            hotkey_not_registered_message(registration),
        )

    banned = await is_hotkey_banned(session, hotkey=body.hotkey)
    if banned:
        refuse(_ERROR_CODE_HOTKEY_BANNED, "hotkey is banned from submitting")
    if signature_valid and not banned:
        cla = await evaluate_cla_gate(
            session, netuid=chain_config.netuid, hotkey=body.hotkey
        )
        if not cla.allowed:
            refuse(CLA_REQUIRED_ERROR_CODE, cla.message)

    eligibility = None
    if signature_valid:
        eligibility = await readmission_eligibility(
            session,
            agent_id=body.agent_id,
            hotkey=body.hotkey,
            owner_coldkey=owner_coldkey,
            bench_version=body.bench_version,
            with_owner_rank=True,
        )
        for refusal in eligibility.refusals:
            refuse(refusal.code, refusal.message)
    # Snapshot ORM values now: a later transaction expires the rows.
    rollout_id = (
        eligibility.rollout.rollout_id
        if eligibility is not None and eligibility.rollout is not None
        else None
    )
    previous_bench_version = (
        eligibility.rollout.from_version
        if eligibility is not None
        and eligibility.rollout is not None
        and eligibility.stranded is not None
        else None
    )

    retry_at = None
    if owner_coldkey is not None and signature_valid:
        decision = await submission_cooldown_decision(
            session,
            miner_coldkey=owner_coldkey,
            miner_hotkey=body.hotkey,
            cooldown=timedelta(seconds=settings.cooldown_seconds),
        )
        if decision is not None:
            retry_at = decision.retry_at
            refuse(
                _ERROR_CODE_SUBMISSION_COOLDOWN,
                submission_cooldown_message(
                    retry_at, owner_override=decision.owner_override
                ),
            )

    reservation = None
    if not codes and body.reserve_slot:
        assert owner_coldkey is not None and rollout_id is not None
        await _release(session)
        try:
            async with session.begin():
                settings = await effective_submission_settings(
                    session,
                    default_payment_address=(
                        request.app.state.config.upload_payment_address
                    ),
                )
                row = await reserve_readmission(
                    session,
                    agent_id=body.agent_id,
                    bench_version=body.bench_version,
                    rollout_id=rollout_id,
                    miner_coldkey=owner_coldkey,
                    miner_hotkey=body.hotkey,
                    settings=settings,
                )
                reservation = (
                    row.token,
                    row.expires_at,
                    row.fee_amount_rao,
                    row.payment_send_address,
                )
        except SubmissionCooldownError as exc:
            retry_at = exc.retry_at
            refuse(
                _ERROR_CODE_SUBMISSION_COOLDOWN,
                submission_cooldown_message(
                    retry_at, owner_override=exc.owner_override
                ),
            )
            reservation = None

    stranded = eligibility.stranded if eligibility is not None else None
    return ReadmissionCheckResponse(
        ok=not codes,
        error_codes=codes,
        messages=messages,
        activation=activation_state(activation),
        agent_id=body.agent_id,
        bench_version=activation.active_bench_version,
        rollout_id=rollout_id,
        eligibility=(
            ReadmissionEligibilityView(
                stranded=stranded is not None,
                already_admitted=eligibility.already_admitted,
                previous_bench_version=previous_bench_version,
                previous_score_count=(
                    stranded.previous_score_count if stranded else None
                ),
                previous_median_composite=(
                    stranded.previous_median_composite if stranded else None
                ),
                owner_best_stranded=eligibility.owner_best_stranded,
                cohort_sibling_agent_id=(
                    stranded.cohort_sibling_agent_id if stranded else None
                ),
            )
            if eligibility is not None
            else None
        ),
        payment_required=not codes,
        payment_amount_rao=reservation[2] if reservation else None,
        payment_send_address=reservation[3] if reservation else None,
        admission_token=reservation[0] if reservation else None,
        admission_expires_at=reservation[1] if reservation else None,
        retry_at=retry_at,
        cooldown_seconds=settings.cooldown_seconds,
        netuid=chain_config.netuid,
        subtensor_network=chain_config.subtensor_network,
    )


def _recovered(row: ReadmissionPayment) -> ReadmissionCommitResponse:
    return ReadmissionCommitResponse(
        agent_id=row.agent_id,
        bench_version=row.bench_version,
        outcome="admitted" if row.outcome == "admitted" else "credited",
        payment_disposition=(
            "credited"
            if row.outcome == "credited"
            else ("credit_consumed" if row.funding == "credit" else "consumed")
        ),
        rollout_id=row.rollout_id,
        refusal_reason=row.refusal_reason,
        credit_expires_at=(
            _utc(row.timestamp) + READMISSION_CREDIT_TTL
            if row.outcome == "credited"
            else None
        ),
        recovered=True,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def _exact_retry(
    session: AsyncSession, body: ReadmissionCommitRequest
) -> ReadmissionCommitResponse | None:
    """The committed readmission this request repeats, if any; else refuse a
    proof the readmission lane already holds for something else."""
    prior = await get_readmission_payment_for_proof(
        session,
        block_hash=body.payment_block_hash,
        extrinsic_index=body.payment_extrinsic_index,
    )
    if prior is None:
        return None
    if (prior.agent_id, prior.bench_version, prior.miner_hotkey) == (
        body.agent_id,
        body.bench_version,
        body.hotkey,
    ):
        return _recovered(prior)
    raise PaymentReplayedError("payment proof already paid a different readmission")


@router.post("/commit", response_model=ReadmissionCommitResponse)
async def commit(
    request: Request,
    body: ReadmissionCommitRequest,
    chain: ChainDep,
    verifier: PaymentVerifierDep,
    generator: GeneratorDep,
    session: SessionDep,
) -> ReadmissionCommitResponse:
    """Verify the finalized payment and readmit the agent, or bank a credit.

    An exact retry (same proof, agent, era, hotkey) returns the original
    outcome. A proof another lane holds is a 3207 replay. A verified payment
    whose admission is refused is recorded ``credited`` and banked as a credit
    for this hotkey, spendable on an upload or a readmission for 30 days.
    """
    if not _verify_signature(body, operation="commit"):
        raise HTTPException(
            status_code=400, detail="signature did not verify against the hotkey"
        )
    if await is_hotkey_banned(session, hotkey=body.hotkey):
        raise HTTPException(status_code=403, detail="hotkey is banned from submitting")

    recovered = await _exact_retry(session, body)
    if recovered is not None:
        return recovered
    credit = await get_evaluation_payment_for_proof(
        session,
        block_hash=body.payment_block_hash,
        extrinsic_index=body.payment_extrinsic_index,
    )
    if credit is not None and credit.agent_id is not None:
        raise PaymentReplayedError("payment proof already funded an upload")
    if credit is not None and credit.miner_hotkey != body.hotkey:
        raise PaymentReplayedError("payment credit belongs to a different hotkey")
    if credit is not None and credit_expired(credit):
        raise PaymentRecoveryExpired(
            f"payment credit at {_utc(credit.timestamp).isoformat()} has expired"
        )
    if credit is None and await get_generator_payment_for_proof(
        session,
        block_hash=body.payment_block_hash,
        extrinsic_index=body.payment_extrinsic_index,
    ):
        raise PaymentReplayedError("payment proof already funded a generator upload")
    # Snapshot the credit now: releasing the session expires the row.
    paid_by_credit = (
        _CreditSnapshot(
            miner_coldkey=credit.miner_coldkey, expires_at=credit_expires_at(credit)
        )
        if credit is not None
        else None
    )

    now = datetime.now(UTC)
    reservation_token: UUID | None = None
    settings = await effective_submission_settings(
        session, default_payment_address=request.app.state.config.upload_payment_address
    )
    expected_amount_rao = readmission_fee_rao(settings)
    expected_send_address = settings.payment_address
    if body.admission_token is not None:
        reservation = await get_readmission_reservation(
            session, token=body.admission_token
        )
        if reservation is not None and reservation_live(
            reservation,
            agent_id=body.agent_id,
            bench_version=body.bench_version,
            hotkey=body.hotkey,
            now=now,
        ):
            reservation_token = reservation.token
            expected_amount_rao = reservation.fee_amount_rao
            expected_send_address = reservation.payment_send_address

    await _release(session)
    try:
        registration = await chain.get_hotkey_registration(
            body.hotkey, netuid=request.app.state.config.chain.netuid
        )
    except ChainError as exc:
        logger.warning("chain unreachable during /readmission/commit: %s", exc)
        raise HTTPException(
            status_code=503, detail="chain unavailable; retry shortly"
        ) from exc

    verified: VerifiedPayment | None = None
    if paid_by_credit is None:
        try:
            verified = await verifier.verify_payment(
                PaymentProof(
                    block_hash=body.payment_block_hash,
                    block_number=body.payment_block_number,
                    extrinsic_index=body.payment_extrinsic_index,
                ),
                expected_hotkey=body.hotkey,
                expected_amount_rao=expected_amount_rao,
                legacy_amount_cutoff_at=None,
                expected_send_address=expected_send_address,
            )
        except ChainError as exc:
            logger.warning("chain unreachable during /readmission/commit: %s", exc)
            raise HTTPException(
                status_code=503, detail="chain unavailable; retry shortly"
            ) from exc
        if _utc(verified.block_timestamp) + _PAYMENT_TTL <= datetime.now(UTC):
            raise PaymentRecoveryExpired(
                f"payment at {_utc(verified.block_timestamp).isoformat()} is "
                "outside the 24-hour window"
            )
        payer_coldkey = verified.miner_coldkey
    else:
        payer_coldkey = paid_by_credit.miner_coldkey

    # The payment is good. From here every refusal banks it (or leaves the
    # credit that paid unspent) instead of losing it.
    refusal = await _refusal_after_payment(
        session,
        request=request,
        body=body,
        registration=registration,
        payer_coldkey=payer_coldkey,
        settings=settings,
    )
    if refusal is None:
        try:
            return await _admit(
                session,
                body=body,
                generator=generator,
                verified=verified,
                paid_by_credit=paid_by_credit is not None,
                reservation_token=reservation_token,
                payer_coldkey=payer_coldkey,
                settings=settings,
                netuid=request.app.state.config.chain.netuid,
            )
        except PaymentReplayedError:
            # A concurrent identical commit may have won the proof.
            await _release(session)
            recovered = await _exact_retry(session, body)
            if recovered is not None:
                return recovered
            raise
        except (
            RolloutConflictError,
            StrandedAgentNotFoundError,
            ReadmissionAlreadyAdmittedError,
            ReadmissionTargetError,
            SubmissionCooldownError,
            DataPipelineError,
            ClaConsentRequired,
        ) as exc:
            refusal = (
                f"the readmission could not be admitted after payment: {exc}"
                if not isinstance(exc, SubmissionCooldownError)
                else "the owner's submission cooldown applies: "
                + submission_cooldown_message(
                    exc.retry_at, owner_override=exc.owner_override
                )
            )
    return await _bank_refusal(
        session,
        body=body,
        verified=verified,
        paid_by_credit=paid_by_credit,
        refusal=refusal,
    )


async def _refusal_after_payment(
    session: AsyncSession,
    *,
    request: Request,
    body: ReadmissionCommitRequest,
    registration: Any,
    payer_coldkey: str,
    settings: Any,
) -> str | None:
    activation = await current_readmission_activation(session, settings=settings)
    if not activation.active:
        return _inactive_message(activation)
    if not registration.registered:
        return hotkey_not_registered_message(registration)
    if registration.coldkey != payer_coldkey:
        return "the coldkey that paid no longer owns this hotkey"
    cla = await evaluate_cla_gate(
        session, netuid=request.app.state.config.chain.netuid, hotkey=body.hotkey
    )
    if not cla.allowed:
        return cla.message
    eligibility = await readmission_eligibility(
        session,
        agent_id=body.agent_id,
        hotkey=body.hotkey,
        owner_coldkey=registration.coldkey,
        bench_version=body.bench_version,
    )
    if eligibility.refusals:
        return "; ".join(refusal.message for refusal in eligibility.refusals)
    return None


async def _admit(
    session: AsyncSession,
    *,
    body: ReadmissionCommitRequest,
    generator: DatasetGenerator,
    verified: VerifiedPayment | None,
    paid_by_credit: bool,
    reservation_token: UUID | None,
    payer_coldkey: str,
    settings: Any,
    netuid: int,
) -> ReadmissionCommitResponse:
    eligibility = await readmission_eligibility(
        session,
        agent_id=body.agent_id,
        hotkey=body.hotkey,
        owner_coldkey=payer_coldkey,
        bench_version=body.bench_version,
    )
    agent = eligibility.agent
    stranded = eligibility.stranded
    if eligibility.refusals or agent is None or stranded is None:
        raise RolloutConflictError(
            "; ".join(r.message for r in eligibility.refusals) or "not eligible"
        )
    sha256 = agent.sha256.lower()
    previous_score_count = stranded.previous_score_count
    await _release(session)

    async def record_payment(
        session: AsyncSession, target: StrandedAdmissionTarget
    ) -> dict[str, Any]:
        # Decide consent inside the write, under the shared policy lock (the
        # upload's order: policy, then the coldkey lock), so an agreement that
        # took effect after the pre-write check is never missed. A refusal
        # rolls the admission back and the payment is banked as a credit.
        await lock_identity_writes(session, namespace="cla-policy", shared=True)
        cla = await evaluate_cla_gate(session, netuid=netuid, hotkey=body.hotkey)
        if not cla.allowed:
            raise ClaConsentRequired(cla.message)
        await consume_readmission_admission(
            session,
            agent_id=body.agent_id,
            bench_version=target.rollout.desired_version,
            reservation_token=reservation_token,
            miner_coldkey=payer_coldkey,
            miner_hotkey=body.hotkey,
            settings=settings,
        )
        if paid_by_credit:
            locked = await get_evaluation_payment_for_proof(
                session,
                block_hash=body.payment_block_hash,
                extrinsic_index=body.payment_extrinsic_index,
                for_update=True,
            )
            if locked is None:
                raise PaymentReplayedError("payment credit disappeared")
            row = await spend_credit_on_readmission(
                session,
                credit=locked,
                agent_id=body.agent_id,
                bench_version=target.rollout.desired_version,
                rollout_id=target.rollout.rollout_id,
                miner_hotkey=body.hotkey,
            )
        else:
            assert verified is not None
            row = await insert_readmission_payment(
                session,
                verified=verified,
                agent_id=body.agent_id,
                bench_version=target.rollout.desired_version,
                rollout_id=target.rollout.rollout_id,
            )
        return {
            "block_hash": row.block_hash,
            "extrinsic_index": row.extrinsic_index,
            "amount_rao": row.amount_rao,
            "funding": row.funding,
        }

    result = await admit_stranded_previous_era_agent(
        session,
        generator=generator,
        now=datetime.now(UTC),
        agent_id=body.agent_id,
        expected_active_version=body.bench_version,
        expected_sha256=sha256,
        expected_previous_score_count=previous_score_count,
        actor=f"miner:{body.hotkey}",
        reason="paid readmission into the active benchmark era (SN-485)",
        origin=ORIGIN_PAID_READMISSION,
        category="paid",
        within_write=record_payment,
    )
    logger.info(
        "readmission admitted hotkey=%s agent_id=%s bench_version=%s "
        "payment=%s block_hash=%s",
        body.hotkey,
        body.agent_id,
        result.bench_version,
        "credit" if paid_by_credit else "fresh",
        body.payment_block_hash,
    )
    return ReadmissionCommitResponse(
        agent_id=body.agent_id,
        bench_version=result.bench_version,
        outcome="admitted",
        payment_disposition="credit_consumed" if paid_by_credit else "consumed",
        rollout_id=result.rollout_id,
        position=result.position,
        dataset_sha256=result.dataset_sha256,
    )


async def _bank_refusal(
    session: AsyncSession,
    *,
    body: ReadmissionCommitRequest,
    verified: VerifiedPayment | None,
    paid_by_credit: _CreditSnapshot | None,
    refusal: str,
) -> ReadmissionCommitResponse:
    """Record a refusal found after payment and keep the payment for the miner."""
    await _release(session)
    if paid_by_credit is not None:
        # The credit that paid stays unspent on its own clock.
        logger.info(
            "readmission refused after credit hotkey=%s agent_id=%s: %s",
            body.hotkey,
            body.agent_id,
            refusal,
        )
        return ReadmissionCommitResponse(
            agent_id=body.agent_id,
            bench_version=body.bench_version,
            outcome="credited",
            payment_disposition="credited",
            refusal_reason=refusal,
            credit_expires_at=paid_by_credit.expires_at,
        )
    assert verified is not None
    try:
        async with session.begin():
            await insert_readmission_payment(
                session,
                verified=verified,
                agent_id=body.agent_id,
                bench_version=body.bench_version,
                rollout_id=None,
                outcome="credited",
                refusal_reason=refusal[:2000],
            )
    except PaymentReplayedError:
        await _release(session)
        recovered = await _exact_retry(session, body)
        if recovered is not None:
            return recovered
        raise
    banked = await get_evaluation_payment_for_proof(
        session,
        block_hash=body.payment_block_hash,
        extrinsic_index=body.payment_extrinsic_index,
    )
    logger.info(
        "readmission refused after payment, credited hotkey=%s agent_id=%s: %s",
        body.hotkey,
        body.agent_id,
        refusal,
    )
    return ReadmissionCommitResponse(
        agent_id=body.agent_id,
        bench_version=body.bench_version,
        outcome="credited",
        payment_disposition="credited",
        refusal_reason=refusal,
        credit_expires_at=credit_expires_at(banked) if banked is not None else None,
    )


__all__ = ["router"]
