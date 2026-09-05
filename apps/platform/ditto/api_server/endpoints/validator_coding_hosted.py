"""Default-off signed control admission; private execution stays on Platform."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol

import bittensor
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message

from ditto.api_models.coding_hosted import (
    HostedCodingRequest,
    HostedCodingStatus,
    hosted_message_digest,
    hosted_signing_bytes,
)
from ditto.api_server.coding_hosted_verification import verify_hosted_request
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints.validator import (
    ValidatorAuthError,
    _assert_validator_permitted,
)
from ditto.db.models import CodingHostedAssignment
from ditto.db.queries.coding_hosted_admission import (
    HostedAdmissionError,
    admit_hosted_request,
)
from ditto.db.queries.validator_auth import ValidatorRequestReplayError

_NO_STORE = {"Cache-Control": "no-store"}
_MAX_REQUEST_BYTES = 8192


class HostedSigner(Protocol):
    @property
    def ss58_address(self) -> str: ...
    def sign(self, data: bytes) -> bytes: ...


@dataclass(frozen=True, repr=False)
class HostedCodingControl:
    """Provision only in the trusted Platform runtime; absent means disabled."""

    signer: HostedSigner


class _BoundedPrivateControlRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            if not isinstance(
                getattr(request.app.state, "coding_hosted_control", None),
                HostedCodingControl,
            ):
                return JSONResponse(
                    {"detail": "hosted Coding control is disabled"},
                    status_code=503,
                    headers=_NO_STORE,
                )
            try:
                body = bytearray()
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > _MAX_REQUEST_BYTES:
                            return JSONResponse(
                                {"detail": "hosted Coding request is too large"},
                                status_code=413,
                                headers=_NO_STORE,
                            )
                        body.extend(chunk)
                sent = False

                async def receive() -> Message:
                    nonlocal sent
                    if sent:
                        return {"type": "http.disconnect"}
                    sent = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }

                async with asyncio.timeout(30):
                    return await handler(Request(request.scope, receive))
            except RequestValidationError:
                return JSONResponse(
                    {"detail": "hosted Coding request is invalid"},
                    status_code=422,
                    headers=_NO_STORE,
                )
            except HTTPException as error:
                return JSONResponse(
                    {"detail": "hosted Coding request refused"},
                    status_code=error.status_code,
                    headers=_NO_STORE,
                )
            except Exception:
                return JSONResponse(
                    {"detail": "hosted Coding control unavailable"},
                    status_code=503,
                    headers=_NO_STORE,
                )

        return bounded


router = APIRouter(
    prefix="/validator", tags=["validator"], route_class=_BoundedPrivateControlRoute
)
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/coding-hosted/control",
    response_model=HostedCodingStatus,
    status_code=202,
    responses={
        401: {"description": "Validator identity is not permitted."},
        409: {"description": "Request replay or assignment conflict."},
        413: {"description": "Streamed request exceeds the byte limit."},
        422: {"description": "Malformed request; input values are not returned."},
        503: {"description": "Hosted control is disabled or unavailable."},
    },
)
async def control(
    payload: HostedCodingRequest, request: Request, session: SessionDep
) -> Response:
    config = getattr(request.app.state, "coding_hosted_control", None)
    if not isinstance(config, HostedCodingControl):
        raise HTTPException(503, "hosted control disabled", headers=_NO_STORE)
    try:
        verifier = bittensor.Keypair(ss58_address=payload.validator_hotkey)
        verify_hosted_request(
            request=payload,
            expected_validator=payload.validator_hotkey,
            verifier=verifier,
            now_unix=int(time.time()),
        )
        chain = await get_chain_client(request)
        await _assert_validator_permitted(
            chain,
            request.app.state.config.chain.netuid,
            payload.validator_hotkey,
            network=request.app.state.config.chain.subtensor_network,
        )
    except (ValidatorAuthError, ValueError):
        raise HTTPException(
            401, "hosted validator invalid", headers=_NO_STORE
        ) from None
    try:
        async with session.begin():
            view = await admit_hosted_request(
                session,
                request=payload,
                authenticated_validator=payload.validator_hotkey,
                verifier=verifier,
            )
            row = await session.get(CodingHostedAssignment, view.evaluation_id)
            if row is None:
                raise HostedAdmissionError("hosted assignment unavailable")
            database_now = await session.scalar(select(func.clock_timestamp()))
            if not isinstance(database_now, datetime):
                raise HostedAdmissionError("hosted clock unavailable")
            issued = int(database_now.timestamp())
            status = HostedCodingStatus.model_validate(
                {
                    "schema": "dittobench-coding-hosted-status-v2",
                    "coding_contract_version": 2,
                    "shadow_only": True,
                    "weight_eligible": False,
                    "evaluation_id": view.evaluation_id,
                    "attempt_id": view.attempt_id,
                    "validator_hotkey": row.validator_hotkey,
                    "platform_hotkey": config.signer.ss58_address,
                    "request_sha256": hosted_message_digest(payload),
                    "artifact_sha256": row.artifact_sha256,
                    "assignment_sha256": row.assignment_sha256,
                    "policy_sha256": row.authority["policy_sha256"],
                    "execution_profile_sha256": row.authority[
                        "execution_profile_sha256"
                    ],
                    "grading_profile_sha256": row.authority["grading_profile_sha256"],
                    "state": view.state,
                    "issued_at_unix": issued,
                    "expires_at_unix": issued + 120,
                    "signature": "0" * 128,
                }
            )
            signed = hosted_signing_bytes(status)
            signature = config.signer.sign(signed)
            if not bittensor.Keypair(ss58_address=config.signer.ss58_address).verify(
                signed, signature
            ):
                raise RuntimeError("hosted signer unavailable")
            status = HostedCodingStatus.model_validate(
                {
                    **status.model_dump(mode="json", by_alias=True),
                    "signature": signature.hex(),
                }
            )
            body = (
                json.dumps(
                    status.model_dump(mode="json", by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                + "\n"
            ).encode()
            if len(body) > 8192:
                raise RuntimeError("hosted response bounds")
        return Response(
            content=body,
            status_code=202,
            media_type="application/json",
            headers=_NO_STORE,
        )
    except (HostedAdmissionError, ValidatorRequestReplayError):
        raise HTTPException(
            409, "hosted assignment conflict", headers=_NO_STORE
        ) from None
