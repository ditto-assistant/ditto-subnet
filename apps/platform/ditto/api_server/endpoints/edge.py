"""The ``edge/v1`` node protocol: Ditto Edge shadow lane (SN-481).

Opt-in Mac nodes enroll with an Ed25519 device key, poll for assignments,
revocations and challenges, download what they were assigned, acknowledge,
and answer challenges. Every request is signed over its exact method, path,
timestamp, nonce and body hash, and each nonce is used once.

While the lane is ``off`` (the default) every route except status and leave
answers 503, so nothing enrolls, is assigned, or is challenged. In ``shadow``,
nodes are measured into a ledger that pays nothing.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server import edge_lane
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.edge_lane import (
    ERROR_CODE_EDGE_BAD_REQUEST,
    ERROR_CODE_EDGE_STORAGE_UNAVAILABLE,
    EdgeLaneError,
    ObjectBytesCache,
    edge_now,
)
from ditto.api_server.edge_lane_settings import (
    EdgeLaneSettingsResolver,
    ResolvedEdgeLaneSettings,
)
from ditto.api_server.storage import ObjectDownloadFailedError, S3StorageClient
from ditto.db.models import EdgeObject
from ditto_edge_protocol import paths
from ditto_edge_protocol.signing import (
    HEADER_NODE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
)
from ditto_edge_protocol.wire import (
    AckRequest,
    AckResponse,
    AnswerResponse,
    ChallengeAnswer,
    EnrollRequest,
    EnrollResponse,
    LeaveRequest,
    NodeStatusResponse,
    PollRequest,
    PollResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/edge", tags=["edge"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]

CONTROL_MAX_BYTES = 64 * 1024
"""Body cap for enroll, poll, ack and leave: small JSON documents."""
ANSWER_MAX_BYTES = 400 * 1024
"""Body cap for a challenge answer: a 256 KiB range, base64, plus JSON."""

ModelT = TypeVar("ModelT", bound=BaseModel)


def _inline(schema: dict[str, Any]) -> dict[str, Any]:
    """A JSON schema with its ``$defs`` references inlined, for openapi_extra."""
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return resolve(defs[ref.removeprefix("#/$defs/")])
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(value) for value in node]
        return node

    resolved: dict[str, Any] = resolve(schema)
    return resolved


def _signed(body: type[BaseModel] | None = None) -> dict[str, Any]:
    """OpenAPI for a signed edge/v1 route: its four headers and JSON body.

    The handler reads both itself, because it authenticates before it parses
    and the signature covers the body's exact bytes. This only documents them.
    """
    extra: dict[str, Any] = {
        "parameters": [
            {
                "name": name,
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
            }
            for name in (HEADER_NODE, HEADER_TIMESTAMP, HEADER_NONCE, HEADER_SIGNATURE)
        ]
    }
    if body is not None:
        extra["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {"schema": _inline(body.model_json_schema())}
            },
        }
    return extra


def _parse(model: type[ModelT], body: bytes) -> ModelT:
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise EdgeLaneError(
            422, ERROR_CODE_EDGE_BAD_REQUEST, "request does not match edge/v1"
        ) from exc


async def _settings(request: Request) -> ResolvedEdgeLaneSettings:
    resolver: EdgeLaneSettingsResolver = request.app.state.edge_lane_settings
    return await resolver.resolve(request.app.state.session_maker)


def object_cache(request: Request) -> ObjectBytesCache:
    cache = getattr(request.app.state, "edge_object_cache", None)
    if cache is None:
        cache = ObjectBytesCache()
        request.app.state.edge_object_cache = cache
    return cache


async def load_object_bytes(
    request: Request, storage: S3StorageClient, digest: str, size: int
) -> bytes:
    cache = object_cache(request)
    data = cache.get(digest)
    if data is None:
        try:
            data = await storage.get_object(
                key=edge_lane.object_key(digest), max_bytes=size
            )
        except ObjectDownloadFailedError as exc:
            # Missing or unreachable: either way the node is not at fault, and
            # an answer it sends stays pending rather than being judged.
            logger.warning("edge object %s could not be read from storage", digest)
            raise EdgeLaneError(
                503,
                ERROR_CODE_EDGE_STORAGE_UNAVAILABLE,
                "object storage is unavailable; retry later",
            ) from exc
        cache.put(digest, data)
    return data


@router.post(
    paths.ENROLL.removeprefix("/api/v1/edge"),
    response_model=EnrollResponse,
    openapi_extra=_signed(EnrollRequest),
)
async def enroll(request: Request, session: SessionDep) -> EnrollResponse:
    body = await request.body()
    now = edge_now(request)
    resolved = await _settings(request)
    edge_lane.require_shadow(resolved.settings)
    async with session.begin():
        auth = await edge_lane.authenticate(
            session,
            method="POST",
            path=request.url.path,
            headers=request.headers,
            body=body,
            now=now,
        )
        return await edge_lane.enroll(
            session,
            settings=resolved.settings,
            request=_parse(EnrollRequest, body),
            existing=auth.node,
            now=now,
        )


@router.get(
    paths.STATUS.removeprefix("/api/v1/edge"),
    response_model=NodeStatusResponse,
    openapi_extra=_signed(),
)
async def node_status(request: Request, session: SessionDep) -> NodeStatusResponse:
    now = edge_now(request)
    resolved = await _settings(request)
    async with session.begin():
        auth = await edge_lane.authenticate(
            session,
            method="GET",
            path=request.url.path,
            headers=request.headers,
            body=b"",
            now=now,
        )
        return await edge_lane.status(
            session, settings=resolved.settings, node=auth.node, now=now
        )


@router.post(
    paths.LEAVE.removeprefix("/api/v1/edge"),
    status_code=204,
    openapi_extra=_signed(LeaveRequest),
)
async def leave(request: Request, session: SessionDep) -> Response:
    body = await request.body()
    now = edge_now(request)
    async with session.begin():
        auth = await edge_lane.authenticate(
            session,
            method="POST",
            path=request.url.path,
            headers=request.headers,
            body=body,
            now=now,
        )
        _parse(LeaveRequest, body)
        await edge_lane.leave(session, node=auth.node, now=now)
    return Response(status_code=204)


@router.post(
    paths.POLL.removeprefix("/api/v1/edge"),
    response_model=PollResponse,
    openapi_extra=_signed(PollRequest),
)
async def poll(request: Request, session: SessionDep) -> PollResponse:
    body = await request.body()
    now = edge_now(request)
    resolved = await _settings(request)
    edge_lane.require_shadow(resolved.settings)
    async with session.begin():
        auth = await edge_lane.authenticate(
            session,
            method="POST",
            path=request.url.path,
            headers=request.headers,
            body=body,
            now=now,
        )
        response = await edge_lane.poll(
            session,
            settings=resolved.settings,
            settings_revision=resolved.revision,
            node_id=auth.node_id,
            request=_parse(PollRequest, body),
            now=now,
        )
        return response


@router.get(
    "/objects/{digest}",
    response_class=Response,
    responses={
        200: {
            "description": "The object's bytes.",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
    openapi_extra=_signed(),
)
async def fill(
    digest: str, request: Request, session: SessionDep, storage: StorageDep
) -> Response:
    now = edge_now(request)
    resolved = await _settings(request)
    edge_lane.require_shadow(resolved.settings)
    async with session.begin():
        auth = await edge_lane.authenticate(
            session,
            method="GET",
            path=request.url.path,
            headers=request.headers,
            body=b"",
            now=now,
        )
        obj = await edge_lane.fill_target(session, node=auth.node, digest=digest)
        size = obj.size_bytes
    data = await load_object_bytes(request, storage, digest, size)
    # Only a download that succeeded counts as a fill.
    async with session.begin():
        await edge_lane.record_fill(
            session, node_id=auth.node_id, digest=digest, now=now
        )
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Ditto-Edge-Digest": digest},
    )


@router.post(
    paths.ACK.removeprefix("/api/v1/edge"),
    response_model=AckResponse,
    openapi_extra=_signed(AckRequest),
)
async def acknowledge(request: Request, session: SessionDep) -> AckResponse:
    body = await request.body()
    now = edge_now(request)
    edge_lane.require_shadow((await _settings(request)).settings)
    async with session.begin():
        auth = await edge_lane.authenticate(
            session,
            method="POST",
            path=request.url.path,
            headers=request.headers,
            body=body,
            now=now,
        )
        return await edge_lane.acknowledge(
            session, node=auth.node, request=_parse(AckRequest, body), now=now
        )


@router.post(
    "/challenges/{challenge_id}",
    response_model=AnswerResponse,
    openapi_extra=_signed(ChallengeAnswer),
)
async def answer(
    challenge_id: str, request: Request, session: SessionDep, storage: StorageDep
) -> AnswerResponse:
    body = await request.body()
    now = edge_now(request)
    edge_lane.require_shadow((await _settings(request)).settings)
    async with session.begin():
        auth = await edge_lane.authenticate(
            session,
            method="POST",
            path=request.url.path,
            headers=request.headers,
            body=body,
            now=now,
        )
        parsed = _parse(ChallengeAnswer, body)
        pending = await edge_lane.pending_answer(
            session, node=auth.node, challenge_id=challenge_id
        )
        size = 0
        if pending is not None:
            obj = await session.get(EdgeObject, pending.challenge.digest)
            size = obj.size_bytes if obj is not None else 0
    if pending is not None:
        # Download with no transaction open, so a slow storage read holds no
        # connection and no slot lock. The answer is judged at its arrival
        # time, and only a still-pending slot records it.
        data = await load_object_bytes(request, storage, pending.challenge.digest, size)
        async with session.begin():
            await edge_lane.record_answer(
                session, pending=pending, data=data, answer=parsed, now=now
            )
    # A receipt only: whether an answer passed is never reported back.
    return AnswerResponse(received=True)
