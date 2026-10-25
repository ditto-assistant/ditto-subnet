"""Validator lane for generator-competition round panel runs.

A running round (``ditto.api_server.generator_rounds``) needs every panel agent
run on its round dataset. Validators that advertise the scorer's
``generator_rounds`` capability poll ``POST /validator/generator-round-job``
with a free slot. Platform picks the next panel agent with no result and no
live lease, re-leases that agent's own ``validator_tickets`` row with purpose
``generator_round`` (the continual-retest re-lease rails: one lease per slot,
the V13 scorer pin, finalized agents only, the agent's retry budget
untouched), and returns the job with the round dataset's digest and the
ticket's inference capability. The validator downloads the dataset through the
private-dataset route, runs it with dittobench-api's ``generator-round-v1``
mode, and posts signed per-case results to
``/validator/agent/{id}/generator-round-score``. The results enter only the
round matrix; no agent score row is ever written by this lane.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import select

from ditto.api_models.benchmark_contract import benchmark_contract
from ditto.api_models.generator_round_validator import (
    GeneratorRoundFailRequest,
    GeneratorRoundFailResponse,
    GeneratorRoundJobRequest,
    GeneratorRoundJobResponse,
    GeneratorRoundScoreRequest,
    GeneratorRoundScoreResponse,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator import ValidatorCapabilities
from ditto.api_server.endpoints import validator as v
from ditto.api_server.generator_competition import load_settings
from ditto.api_server.generator_rounds import (
    RoundSubmissionRejected,
    pending_panel_agents,
    record_panel_result,
    round_case_ids,
    round_for_dataset,
    running_round,
    validate_round_results,
)
from ditto.api_server.inference_concurrency_settings import resolved_proxy_config
from ditto.db.models import (
    Agent,
    GeneratorRound,
    GeneratorRoundAttempt,
    ValidatorHeartbeat,
    ValidatorTicket,
)
from ditto.db.queries.benchmark_rollout import heartbeat_supports_version
from ditto.db.queries.inference import ensure_inference_grant, revoke_ticket_inference
from ditto.db.queries.tickets import (
    finish_generator_round_ticket,
    issue_generator_round_ticket,
)
from ditto.db.queries.validator_auth import nonce_expires_at

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/validator", tags=["validator"])

# A round panel run is a few generator columns of memory cases, far shorter
# than a full benchmark; this bounds a lease the validator never answers.
ROUND_TICKET_TTL = timedelta(minutes=90)


def round_ticket_seed(row: GeneratorRound) -> int:
    """The positive placeholder seed ``/v2/score`` requires. The round dataset,
    not this number, determines every case."""
    assert row.seed is not None
    return int(row.seed[:15], 16) + 1


def _no_work() -> Response:
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def _round_capable(heartbeat: ValidatorHeartbeat | None, *, now: datetime) -> bool:
    if heartbeat is None or not heartbeat_supports_version(
        heartbeat, now=now, version=13
    ):
        return False
    try:
        capabilities = ValidatorCapabilities.model_validate(heartbeat.capabilities)
    except ValidationError:
        return False
    scorer = capabilities.scorer_benchmarks
    # Round runs carry their own dataset bytes; they do not need the private
    # V13 transport (``private_datasets``), which stays off fleet-wide.
    return bool(
        capabilities.ticket_inference
        and capabilities.sandbox_memory_limit
        and scorer is not None
        and scorer.status == "fresh_verified"
        and scorer.generator_rounds
    )


async def _verify_signed(
    *,
    request: Request,
    chain: v.ChainClient,
    hotkey: str,
    message: bytes,
    signature: str,
    requested_at: datetime | None,
    what: str,
) -> datetime:
    if not v._verify_signature(hotkey, message, signature):
        raise v.ValidatorAuthError(f"{what} signature did not verify")
    now = datetime.now(UTC)
    if requested_at is not None and (
        abs(now - requested_at.astimezone(UTC)) > v._JOB_REQUEST_MAX_AGE
    ):
        raise HTTPException(status_code=409, detail=f"{what} timestamp is stale")
    config = request.app.state.config
    await v._assert_validator_permitted(
        chain,
        config.chain.netuid,
        hotkey,
        network=config.chain.subtensor_network,
    )
    return now


@router.post(
    "/generator-round-job",
    response_model=GeneratorRoundJobResponse,
    responses={204: {"description": "No round panel run is claimable."}},
)
async def request_generator_round_job(
    payload: GeneratorRoundJobRequest,
    request: Request,
    response: Response,
    chain: v.ChainDep,
    session: v.SessionDep,
    x_validator_hotkey: Annotated[str | None, Header()] = None,
) -> GeneratorRoundJobResponse | Response:
    """Lease the next round panel run onto this validator slot, if any."""
    response.headers["Cache-Control"] = "no-store"
    if x_validator_hotkey != payload.validator_hotkey:
        raise v.ValidatorAuthError("round job header does not match signed hotkey")
    now = await _verify_signed(
        request=request,
        chain=chain,
        hotkey=payload.validator_hotkey,
        message=payload.signing_message(),
        signature=payload.signature,
        requested_at=payload.requested_at,
        what="round job",
    )
    config = request.app.state.config
    inference_config = await resolved_proxy_config(
        request.app.state, config.inference_proxy
    )
    inference_settings = await request.app.state.inference_concurrency_settings.resolve(
        getattr(request.app.state, "session_maker", None)
    )
    slot_settings = await v._validator_slot_settings(request)
    async with session.begin():
        await v._assert_validator_compatible(
            session,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            config=config.validator_compatibility,
        )
        try:
            await v.consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=nonce_expires_at(
                    now=now,
                    requested_at=payload.requested_at,
                    max_age=v._JOB_REQUEST_MAX_AGE,
                ),
            )
        except v.ValidatorRequestReplayError as exc:
            raise HTTPException(409, "round job nonce has already been used") from exc
        resolved = await load_settings(session)
        settings = resolved.settings
        if settings.mode == "off" or not settings.validator_rounds_enabled:
            return _no_work()
        heartbeat = await session.get(ValidatorHeartbeat, payload.validator_hotkey)
        if not _round_capable(heartbeat, now=now):
            return _no_work()
        # Locked so concurrent polls pick pending agents one at a time: the
        # second sees the first's committed attempt instead of leasing the
        # same panel agent.
        row = await running_round(session, for_update=True)
        if row is None or row.dataset_sha256 is None:
            return _no_work()
        slot = await v._idle_retest_slot(
            session,
            heartbeat=heartbeat,
            slot_settings=slot_settings,
            validator_hotkey=payload.validator_hotkey,
            now=now,
            # Rounds may take any free slot below the validator's allowance,
            # including the ones the reservation holds back from other lanes.
            lane="round",
            round_reservation_active=True,
            requested_slot_id=payload.slot_id,
        )
        if slot is None:
            return _no_work()
        ticket: ValidatorTicket | None = None
        for agent_id in await pending_panel_agents(session, row, now=now):
            ticket = await issue_generator_round_ticket(
                session,
                agent_id=agent_id,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                ttl=ROUND_TICKET_TTL,
                seed=round_ticket_seed(row),
                dataset_sha256=row.dataset_sha256,
                slot_id=slot,
            )
            if ticket is not None:
                break
        if ticket is None:
            return _no_work()
        agent = await session.get(Agent, ticket.agent_id)
        assert agent is not None
        grant = await ensure_inference_grant(
            session, ticket=ticket, config=inference_config
        )
        if grant is None:
            raise HTTPException(503, "ticket inference capability is unavailable")
        session.add(
            GeneratorRoundAttempt(
                round_index=row.round_index,
                agent_id=ticket.agent_id,
                validator_hotkey=payload.validator_hotkey,
                ticket_deadline=ticket.deadline,
                slot_id=ticket.slot_id,
                status="leased",
                issued_at=now,
            )
        )
        contract = benchmark_contract(13)
        job = GeneratorRoundJobResponse(
            round_index=row.round_index,
            agent_id=agent.agent_id,
            slot_id=ticket.slot_id,
            miner_hotkey=agent.miner_hotkey,
            sha256=agent.sha256,
            deadline=ticket.deadline,
            bench_version=13,
            dataset_sha256=row.dataset_sha256,
            seed=round_ticket_seed(row),
            run_size="full",
            case_count=len(round_case_ids(row)),
            minimum_screening_policy_version=contract.minimum_screening_policy_version,
            requires_screened_image=contract.requires_screened_image,
            benchmark_runtime=inference_settings.benchmark_runtime.model_dump(
                mode="json"
            ),
            inference=v._inference_grant_offer(
                request=request, grant=grant, bench_version=13
            ),
        )
    logger.info(
        "issued generator round job round=%s agent=%s validator=%s slot=%s",
        job.round_index,
        job.agent_id,
        payload.validator_hotkey,
        job.slot_id,
    )
    return job


async def _open_round_ticket(
    session,  # type: ignore[no-untyped-def]
    *,
    agent_id: UUID,
    validator_hotkey: str,
    deadline: datetime,
    now: datetime,
) -> ValidatorTicket:
    ticket = await session.scalar(
        select(ValidatorTicket)
        .where(
            ValidatorTicket.agent_id == agent_id,
            ValidatorTicket.validator_hotkey == validator_hotkey,
            ValidatorTicket.bench_version == 13,
            ValidatorTicket.status == TicketStatus.ISSUED,
            ValidatorTicket.purpose == TicketPurpose.GENERATOR_ROUND,
            ValidatorTicket.deadline == deadline,
            ValidatorTicket.deadline > now,
        )
        .with_for_update()
    )
    if ticket is None:
        raise HTTPException(409, "round lease is not open")
    return ticket


@router.post(
    "/agent/{agent_id}/generator-round-score",
    response_model=GeneratorRoundScoreResponse,
)
async def submit_generator_round_score(
    agent_id: UUID,
    payload: GeneratorRoundScoreRequest,
    request: Request,
    response: Response,
    chain: v.ChainDep,
    session: v.SessionDep,
) -> GeneratorRoundScoreResponse:
    """Accept one panel agent's signed round results for its open lease."""
    response.headers["Cache-Control"] = "no-store"
    now = await _verify_signed(
        request=request,
        chain=chain,
        hotkey=payload.validator_hotkey,
        message=payload.signing_message(agent_id),
        signature=payload.signature,
        requested_at=None,
        what="round score",
    )
    async with session.begin():
        ticket = await _open_round_ticket(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            deadline=payload.ticket_deadline,
            now=now,
        )
        if ticket.dataset_sha256 != payload.dataset_sha256:
            raise HTTPException(409, "round results name another dataset")
        row = await round_for_dataset(session, payload.dataset_sha256)
        if row is None or row.round_index != payload.round_index:
            raise HTTPException(409, "round results name another round")
        row = await session.get(GeneratorRound, row.round_index, with_for_update=True)
        assert row is not None
        if row.status != "running":
            raise HTTPException(409, f"round is {row.status}; results are closed")
        if agent_id not in {UUID(slot["agent_id"]) for slot in row.panel or ()}:
            raise HTTPException(409, "agent is not on this round's panel")
        try:
            validate_round_results(row, payload.results)
        except RoundSubmissionRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        await revoke_ticket_inference(session, ticket=ticket, now=now)
        finish_generator_round_ticket(ticket)
        accepted = await record_panel_result(
            session,
            row,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            ticket_deadline=payload.ticket_deadline,
            run_id=payload.run_id,
            results=payload.results,
            signature=payload.signature,
            now=now,
        )
    logger.info(
        "generator round result round=%s agent=%s validator=%s accepted=%s",
        payload.round_index,
        agent_id,
        payload.validator_hotkey,
        accepted,
    )
    return GeneratorRoundScoreResponse(
        round_index=payload.round_index,
        agent_id=agent_id,
        accepted=accepted,
        duplicate=not accepted,
    )


@router.post(
    "/agent/{agent_id}/generator-round-fail",
    response_model=GeneratorRoundFailResponse,
)
async def fail_generator_round_job(
    agent_id: UUID,
    payload: GeneratorRoundFailRequest,
    request: Request,
    response: Response,
    chain: v.ChainDep,
    session: v.SessionDep,
) -> GeneratorRoundFailResponse:
    """Release a round lease the validator could not finish.

    The agent goes back to the round's pending set for another validator. No
    attempt is charged: a round run is never a scoring attempt.
    """
    response.headers["Cache-Control"] = "no-store"
    now = await _verify_signed(
        request=request,
        chain=chain,
        hotkey=payload.validator_hotkey,
        message=payload.signing_message(agent_id),
        signature=payload.signature,
        requested_at=payload.requested_at,
        what="round fail",
    )
    async with session.begin():
        try:
            await v.consume_validator_nonce(
                session,
                nonce=payload.nonce,
                validator_hotkey=payload.validator_hotkey,
                now=now,
                expires_at=nonce_expires_at(
                    now=now,
                    requested_at=payload.requested_at,
                    max_age=v._JOB_REQUEST_MAX_AGE,
                ),
            )
        except v.ValidatorRequestReplayError as exc:
            raise HTTPException(409, "round fail nonce has already been used") from exc
        ticket = await _open_round_ticket(
            session,
            agent_id=agent_id,
            validator_hotkey=payload.validator_hotkey,
            deadline=payload.ticket_deadline,
            now=now,
        )
        # Validate the round and find its attempt before changing anything, so
        # a payload naming another round cannot close this ticket while the
        # real attempt stays leased.
        row = (
            await round_for_dataset(session, ticket.dataset_sha256)
            if ticket.dataset_sha256 is not None
            else None
        )
        if row is None or row.round_index != payload.round_index:
            raise HTTPException(409, "round failure names another round")
        attempt = await session.get(
            GeneratorRoundAttempt,
            (
                payload.round_index,
                agent_id,
                payload.validator_hotkey,
                payload.ticket_deadline,
            ),
            with_for_update=True,
        )
        if attempt is None or attempt.status != "leased":
            raise HTTPException(409, "round attempt is not open")
        await revoke_ticket_inference(session, ticket=ticket, now=now)
        finish_generator_round_ticket(ticket)
        attempt.status = "failed"
        attempt.detail = f"{payload.reason}: {payload.detail or ''}".strip()[:500]
        attempt.finished_at = now
    logger.info(
        "generator round lease released round=%s agent=%s validator=%s reason=%s",
        payload.round_index,
        agent_id,
        payload.validator_hotkey,
        payload.reason,
    )
    return GeneratorRoundFailResponse(
        round_index=payload.round_index, agent_id=agent_id, released=True
    )
