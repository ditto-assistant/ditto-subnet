"""Audited operator access to the inference trace archive (Hippius S3).

The trace capture (services/model-relay ``internal/traces``) ships every
brokered inference call -- bodies included -- to the PRIVATE bucket
``ditto-subnet-traces``. These endpoints are how Backroom reads it: list the
partitioned objects, issue a bounded presigned download URL, or peek at a few
records server-side without downloading a whole file. The bucket credentials
never leave this process; operators get time-bounded URLs and bounded excerpts.

Trace bodies are miner-authored prompts and benchmark case text, so the
download and peek routes demand an ``X-Admin-Actor`` and log it, mirroring the
screening-artifact route. Listing discloses only object keys and sizes.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from ditto.api_models.traces import (
    TraceDownloadURL,
    TraceDownloadURLRequest,
    TraceObject,
    TraceObjectList,
    TracePeekRequest,
    TracePeekResponse,
    TraceRecordSummary,
)
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.hippius import HippiusClient, normalize_object_key
from ditto.api_server.storage.errors import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]
ActorDep = Annotated[str | None, Header(alias="X-Admin-Actor", max_length=320)]

_KEY_PREFIXES = ("traces/v1/", "ledger/v1/")
_PEEK_MAX_COMPRESSED_BYTES = 64 << 20
_PEEK_MAX_SCAN_BYTES = 96 << 20
_PEEK_MAX_LINE_BYTES = 32 << 20
_PEEK_MAX_RECORD_BYTES = 512 << 10
_PEEK_CHUNK_BYTES = 1 << 20


def _traces_client(request: Request) -> HippiusClient:
    client = getattr(request.app.state, "traces_hippius", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="inference trace storage is not configured",
        )
    return client


def _require_actor(x_admin_actor: str | None) -> str:
    if x_admin_actor is None or not 1 <= len(x_admin_actor.strip()) <= 320:
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    return x_admin_actor.strip()


def _checked_key(key: str) -> str:
    normalized = normalize_object_key(key)
    if not normalized.startswith(_KEY_PREFIXES):
        raise HTTPException(
            status_code=422,
            detail="key must be under traces/v1/ or ledger/v1/",
        )
    return normalized


def _partition_prefix(
    scope: str,
    lane: str | None,
    kind: str | None,
    dt: str | None,
    hour: str | None,
) -> str:
    """Build the deepest fully-specified hive prefix.

    Partitions nest lane -> kind -> dt -> hour; a level can only be pinned if
    every level above it is, so a partially-specified combination fails loudly
    instead of silently listing the wrong subtree.
    """
    prefix = f"{scope}/v1/"
    levels = [("lane", lane), ("kind", kind), ("dt", dt), ("hour", hour)]
    pinned = True
    for position, (name, value) in enumerate(levels):
        if value is None:
            pinned = False
            deeper = [n for n, v in levels[position + 1 :] if v is not None]
            if deeper:
                raise HTTPException(
                    status_code=422,
                    detail=f"{deeper[0]} requires {name} to be set too",
                )
            break
        prefix += f"{name}={value}/"
    del pinned
    return prefix


@router.get("/admin/traces", response_model=TraceObjectList)
async def list_trace_objects(
    request: Request,
    _admin: AdminDep,
    scope: Annotated[Literal["traces", "ledger"], Query()] = "traces",
    lane: Annotated[
        Literal["inference", "confirmation", "screening"] | None, Query()
    ] = None,
    kind: Annotated[
        Literal["chat", "embedding", "kaniko", "smoke", "review"] | None, Query()
    ] = None,
    dt: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    hour: Annotated[str | None, Query(pattern=r"^\d{2}$")] = None,
    prefix: Annotated[str | None, Query(max_length=512)] = None,
    max_keys: Annotated[int, Query(ge=1, le=1000)] = 200,
    continuation_token: Annotated[str | None, Query(max_length=2048)] = None,
) -> TraceObjectList:
    """List archive objects, newest partitions by explicit prefix.

    Either give the partition levels (scope/lane/kind/dt/hour) or a raw
    ``prefix`` under ``traces/v1/`` / ``ledger/v1/``. Keys and sizes only --
    no miner content -- so this is a plain read.
    """
    client = _traces_client(request)
    if prefix is not None:
        resolved = normalize_object_key(prefix)
        if not (resolved + "/").startswith(_KEY_PREFIXES) and not resolved.startswith(
            _KEY_PREFIXES
        ):
            raise HTTPException(
                status_code=422,
                detail="prefix must be under traces/v1/ or ledger/v1/",
            )
    else:
        resolved = _partition_prefix(scope, lane, kind, dt, hour)
    try:
        objects, next_token = await client.list_objects(
            prefix=resolved,
            max_keys=max_keys,
            continuation_token=continuation_token,
        )
    except ObjectDownloadFailedError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return TraceObjectList(
        bucket=client.bucket,
        prefix=resolved,
        objects=[
            TraceObject(
                key=o.key, size=o.size, last_modified=o.last_modified, etag=o.etag
            )
            for o in objects
        ],
        continuation_token=next_token,
    )


@router.post("/admin/traces/download-url", response_model=TraceDownloadURL)
async def create_trace_download_url(
    payload: TraceDownloadURLRequest,
    request: Request,
    _admin: AdminDep,
    x_admin_actor: ActorDep = None,
) -> TraceDownloadURL:
    """Issue an audited, time-bounded download URL for one trace object."""
    actor = _require_actor(x_admin_actor)
    client = _traces_client(request)
    key = _checked_key(payload.key)
    url = await client.presigned_get_url(key=key, expires_in=payload.expires_in)
    # X-Admin-Actor is claimed identity behind the shared admin bearer; logged
    # as the best attribution the route has (same stance as artifact URLs).
    logger.info(
        "admin_actor=%s issued trace download url key=%s expires_in=%s",
        actor,
        key,
        payload.expires_in,
    )
    return TraceDownloadURL(
        bucket=client.bucket, key=key, url=url, expires_in=payload.expires_in
    )


@router.post("/admin/traces/peek", response_model=TracePeekResponse)
async def peek_trace_object(
    payload: TracePeekRequest,
    request: Request,
    _admin: AdminDep,
    x_admin_actor: ActorDep = None,
) -> TracePeekResponse:
    """Read a bounded excerpt of one trace object server-side.

    Streams the zstd decode and stops as soon as the requested records are in
    hand, under hard caps (compressed fetch, scanned bytes, line size), so a
    misnamed or pathological object cannot exhaust the API process. Summaries
    are always returned; the full records ride along only when
    ``include_bodies`` is set, and any single record over 512 KiB is elided
    with ``record_omitted="too_large"``.
    """
    import zstandard

    actor = _require_actor(x_admin_actor)
    client = _traces_client(request)
    key = _checked_key(payload.key)
    try:
        compressed = await client.get_object(key=key)
    except ObjectNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ObjectDownloadFailedError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if len(compressed) > _PEEK_MAX_COMPRESSED_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "object is too large to peek server-side; use "
                "/admin/traces/download-url and inspect it locally"
            ),
        )
    logger.info("admin_actor=%s peeked trace key=%s", actor, key)

    records: list[TraceRecordSummary] = []
    scanned = 0
    scan_complete = False
    stream = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(compressed))
    buffer = b""
    index = 0
    try:
        while True:
            chunk = stream.read(_PEEK_CHUNK_BYTES)
            if not chunk:
                # Whatever remains in the buffer is a torn final line; the
                # spooler only rotates complete lines, so ignore it.
                scan_complete = True
                break
            scanned += len(chunk)
            buffer += chunk
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                line, buffer = buffer[:newline], buffer[newline + 1 :]
                if line.strip():
                    if index >= payload.offset_records:
                        records.append(_summarize(index, line, payload.include_bodies))
                    index += 1
                if len(records) >= payload.max_records:
                    break
            if len(records) >= payload.max_records:
                scan_complete = True
                break
            if len(buffer) > _PEEK_MAX_LINE_BYTES or scanned > _PEEK_MAX_SCAN_BYTES:
                break
    finally:
        stream.close()
    return TracePeekResponse(
        bucket=client.bucket,
        key=key,
        records=records,
        records_scanned=index,
        scan_complete=scan_complete,
    )


def _summarize(index: int, line: bytes, include_bodies: bool) -> TraceRecordSummary:
    try:
        record = json.loads(line)
    except ValueError:
        return TraceRecordSummary(index=index, event="unparseable")
    if not isinstance(record, dict):
        return TraceRecordSummary(index=index, event="unparseable")
    req = record.get("request") or {}
    grant = record.get("grant") or {}
    usage = record.get("usage") or {}
    upstream = record.get("upstream") or {}
    outcome = record.get("outcome") or {}
    full: dict | None = None
    omitted: Literal["too_large"] | None = None
    if include_bodies:
        if len(line) <= _PEEK_MAX_RECORD_BYTES:
            full = record
        else:
            omitted = "too_large"
    return TraceRecordSummary(
        index=index,
        recorded_at=_opt_str(record.get("recorded_at")),
        event=_opt_str(record.get("event")),
        lane=_opt_str(req.get("lane")),
        kind=_opt_str(req.get("kind")),
        run_id=_opt_str(req.get("run_id")),
        case_id=_opt_str(req.get("case_id")),
        grant_id=_opt_str(req.get("grant_id")),
        nonce=_opt_str(req.get("nonce")),
        agent_id=_opt_str(grant.get("agent_id")),
        validator_hotkey=_opt_str(grant.get("validator_hotkey")),
        bench_version=_opt_int(grant.get("bench_version")),
        status=_opt_str(outcome.get("status")),
        prompt_tokens=_opt_int(usage.get("prompt_tokens")),
        completion_tokens=_opt_int(usage.get("completion_tokens")),
        provider=_opt_str(upstream.get("provider")),
        latency_ms=_opt_int(upstream.get("latency_ms")),
        body_bytes=_opt_int(req.get("body_bytes")),
        record=full,
        record_omitted=omitted,
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None
