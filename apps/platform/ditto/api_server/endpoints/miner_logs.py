"""Self-serve harness diagnostics for the miner who owns the agent.

Deliberately its own module rather than another route in ``retrieval.py``: that
one is documented as public, unauthed reads, and this is the opposite. Keeping
the signed route separate means ``retrieval.py``'s invariant stays true instead
of quietly acquiring an exception.

The failure this exists for: a submission fails scoring and its owner learns
only the coarse class. Agent ``5fdadd33`` burned four validator leases in 82-108
seconds each, every one reporting a bare ``scoring_error``. The harness's own
output -- already bounded and redacted by the scorer -- named the cause the
entire time and reached nobody. An operator can now read it through Backroom,
but operator triage does not scale to every miner debugging their own build.
This is the path that does.

Authentication is proof-of-possession, not a bearer secret: the caller signs a
payload naming their hotkey and the agent, and the platform checks the signature
against that hotkey and then checks the hotkey owns the agent. Nothing is
issued, stored, or revocable, and a miner can only ever read their own rows.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

import bittensor
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.miner_logs import (
    HARNESS_LOGS_MAX_SKEW_SECONDS,
    MinerHarnessLogAttempt,
    MinerHarnessLogsRequest,
    MinerHarnessLogsResponse,
)
from ditto.api_server.dependencies import get_session
from ditto.db.models import Agent, ValidatorTicket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/miner", tags=["miner"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class HarnessLogsDeniedError(Exception):
    """The caller did not prove ownership of the requested agent.

    One exception for every denial -- bad signature, stale timestamp, unknown
    agent, or an agent owned by a different hotkey -- because they must be
    indistinguishable to the caller. A separate "you signed correctly but this
    agent is not yours" would confirm that an agent id exists and is owned by
    somebody, which is a membership oracle over other miners' submissions.

    Mapped to 404, not 403, for the same reason.
    """


def build_harness_logs_payload(
    *, hotkey_ss58: str, agent_id: str, requested_at: str
) -> bytes:
    """Return the exact UTF-8 bytes the CLI signs and this server verifies.

    The canonical implementation lives in ``ditto.miner_cli.signing`` in the
    monorepo root, which the CLI imports; this is the verifying copy, kept
    byte-identical by ``test_harness_logs_payload_matches_miner_cli``. Two copies
    rather than a shared import because the miner CLI ships independently of the
    platform and must not take a dependency on it -- the same arrangement
    ``build_upload_payload`` already has with ``/upload/check``.

    ``requested_at`` is pre-formatted by the caller rather than taken as a
    ``datetime`` so both sides serialize it exactly once, in one place. A
    signature over a timestamp is only verifiable if both ends agree on its
    spelling to the microsecond.
    """
    return f"ditto-harness-logs:v1:{hotkey_ss58}:{agent_id}:{requested_at}".encode()


def _verify_signature(hotkey: str, payload: bytes, signature_hex: str) -> bool:
    """Return True iff ``signature_hex`` is a valid sr25519 sig over ``payload``.

    Narrow catch, matching ``upload._verify_signature``: ``ValueError`` covers
    malformed hex and malformed SS58, ``TypeError`` covers wrong-shape inputs
    from the wallet library. Anything else is a bug that should reach the error
    envelope as a 500 rather than be reported as "signature did not verify".
    """
    try:
        keypair = bittensor.Keypair(ss58_address=hotkey)
        return bool(keypair.verify(payload, bytes.fromhex(signature_hex)))
    except (ValueError, TypeError):
        return False


@router.post(
    "/harness-logs",
    response_model=MinerHarnessLogsResponse,
    responses={
        404: {"description": "Signature invalid, stale, or agent not owned."},
        422: {"description": "Malformed request body."},
    },
)
async def harness_logs(
    body: MinerHarnessLogsRequest,
    session: SessionDep,
    response: Response,
) -> MinerHarnessLogsResponse:
    """Return this miner's own harness diagnostics for one of their agents.

    POST rather than GET because the signature and timestamp belong in a body:
    a query string lands in access logs, proxy caches, and browser history, and
    these are credentials in every sense that matters.
    """
    response.headers["Cache-Control"] = "no-store"

    # Freshness before signature: a stale request is rejected without spending a
    # verify, and the ordering is not observable to the caller since every
    # denial returns the same thing.
    age = abs((datetime.now(UTC) - body.requested_at.astimezone(UTC)).total_seconds())
    if age > HARNESS_LOGS_MAX_SKEW_SECONDS:
        raise HarnessLogsDeniedError("request timestamp outside the freshness window")

    payload = build_harness_logs_payload(
        hotkey_ss58=body.miner_hotkey,
        agent_id=str(body.agent_id),
        requested_at=body.requested_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        ),
    )
    if not _verify_signature(body.miner_hotkey, payload, body.signature):
        raise HarnessLogsDeniedError("signature did not verify against the hotkey")

    # Ownership in one indexed lookup: (agent_id, miner_hotkey) is unique, so a
    # hit proves the signer owns this agent and a miss covers both "no such
    # agent" and "someone else's agent" without distinguishing them.
    agent = (
        await session.execute(
            select(Agent).where(
                Agent.agent_id == body.agent_id,
                Agent.miner_hotkey == body.miner_hotkey,
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        logger.info(
            "harness-logs denied for hotkey=%s agent=%s",
            body.miner_hotkey,
            body.agent_id,
        )
        raise HarnessLogsDeniedError("no such agent for this hotkey")

    tickets = (
        (
            await session.execute(
                select(ValidatorTicket)
                .where(ValidatorTicket.agent_id == body.agent_id)
                .order_by(ValidatorTicket.issued_at.desc())
            )
        )
        .scalars()
        .all()
    )

    return MinerHarnessLogsResponse(
        agent_id=agent.agent_id,
        miner_hotkey=agent.miner_hotkey,
        agent_status=agent.status,
        attempts=[
            MinerHarnessLogAttempt(
                validator_hotkey=ticket.validator_hotkey,
                bench_version=ticket.bench_version,
                status=ticket.status,
                issued_at=ticket.issued_at,
                deadline=ticket.deadline,
                failed_at=ticket.failed_at,
                failure_reason=ticket.failure_reason,
                failure_detail=ticket.failure_detail,
                container_log_tail=ticket.container_log_tail,
            )
            for ticket in tickets
        ],
    )
