"""Private runtime metrics and bounded Go profile capture for Backroom."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.inference_observability import (
    InferenceLaneCurrent,
    InferenceLaneWindow,
    InferenceRuntimeMetrics,
    RelayRuntimeSnapshot,
    RuntimeProfileArtifact,
    RuntimeProfileCaptureRequest,
    RuntimeProfileTarget,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.inference_concurrency_settings import settings_from_row
from ditto.api_server.runtime_profiles import (
    RuntimeProfileBusyError,
    RuntimeProfileError,
    RuntimeProfileNotFoundError,
    RuntimeProfileStore,
)
from ditto.db.queries.inference_concurrency_settings import (
    latest_inference_concurrency_settings_revision,
)
from ditto.db.queries.inference_observability import load_inference_runtime_rows

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
ActorDep = Annotated[
    str | None,
    Header(alias="X-Admin-Actor", max_length=320),
]
CAPTURE_CONFIRMATION = "CAPTURE RUNTIME PROFILE"
_CAPACITY_RE = re.compile(
    r"^ditto_inference_admission_at_capacity_total\{"
    r'lane="([^"]+)",scope="([^"]+)"\} ([0-9.eE+-]+)$'
)
_PROCESS_START_RE = re.compile(r"^process_start_time_seconds ([0-9.eE+-]+)$")
_RELAY_PORTS: dict[RuntimeProfileTarget, int] = {
    "platform-relay-1": 8010,
    "platform-relay-2": 8011,
}


def _profile_store(request: Request) -> RuntimeProfileStore:
    return request.app.state.runtime_profiles


async def _relay_snapshot(
    target: RuntimeProfileTarget, port: int
) -> RelayRuntimeSnapshot:
    try:
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(5),
        ) as client:
            health_response, metrics_response = await asyncio.gather(
                client.get(f"http://127.0.0.1:{port}/health"),
                client.get(f"http://127.0.0.1:{port}/metrics"),
            )
        health_response.raise_for_status()
        metrics_response.raise_for_status()
        health = health_response.json()
        capacity_declines: dict[str, int] = {}
        process_started_at: datetime | None = None
        for line in metrics_response.text.splitlines():
            if match := _CAPACITY_RE.match(line):
                capacity_declines[f"{match.group(1)}:{match.group(2)}"] = int(
                    float(match.group(3))
                )
            elif match := _PROCESS_START_RE.match(line):
                process_started_at = datetime.fromtimestamp(float(match.group(1)), UTC)
        return RelayRuntimeSnapshot(
            target=target,
            status="ok",
            source_revision=health.get("commit"),
            checked_out_revision=health.get("checked_out_commit"),
            revision_drift=health.get("commit_drift"),
            process_started_at=process_started_at,
            capacity_declines=capacity_declines,
        )
    except (httpx.HTTPError, ValueError, KeyError) as error:
        return RelayRuntimeSnapshot(
            target=target,
            status="unavailable",
            error=type(error).__name__,
        )


@router.get(
    "/admin/inference-runtime-metrics",
    response_model=InferenceRuntimeMetrics,
)
async def get_inference_runtime_metrics(
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
) -> InferenceRuntimeMetrics:
    """Current load, recent throughput/latency, headroom, and relay health."""
    config = request.app.state.config.inference_proxy
    latest = await latest_inference_concurrency_settings_revision(session)
    settings = settings_from_row(latest)
    # The relay probes share nothing with the session, so their up-to-5s
    # timeouts overlap the ledger reads instead of queueing behind them.
    (current_rows, window_rows, peak_rows), relays = await asyncio.gather(
        load_inference_runtime_rows(
            session,
            stale_after_seconds=config.timeout_seconds * 2,
        ),
        asyncio.gather(
            *(_relay_snapshot(target, port) for target, port in _RELAY_PORTS.items())
        ),
    )
    peaks = {
        (str(row["scope"]), str(row["request_kind"])): int(row["peak"])
        for row in peak_rows
    }
    windows = [
        InferenceLaneWindow(
            window_seconds=int(row["window_seconds"]),
            request_kind=cast(Literal["chat", "embedding"], str(row["request_kind"])),
            calls=int(row["calls"]),
            calls_per_second=round(float(row["calls_per_second"]), 4),
            tokens=int(row["tokens"]),
            tokens_per_second=round(float(row["tokens_per_second"]), 2),
            completed=int(row["completed"]),
            failed=int(row["failed"]),
            canceled=int(row["canceled"]),
            timed_out=int(row["timed_out"]),
            latency_p50_ms=(
                round(float(row["latency_p50_ms"]))
                if row["latency_p50_ms"] is not None
                else None
            ),
            latency_p95_ms=(
                round(float(row["latency_p95_ms"]))
                if row["latency_p95_ms"] is not None
                else None
            ),
            latency_max_ms=row["latency_max_ms"],
            peak_global_concurrency=int(row["peak_global_concurrency"]),
        )
        for row in window_rows
    ]
    global_peaks = {
        window.request_kind: window.peak_global_concurrency
        for window in windows
        if window.window_seconds == 3600
    }
    lanes: list[InferenceLaneCurrent] = []
    for row in current_rows:
        kind = cast(Literal["chat", "embedding"], str(row["request_kind"]))
        if kind == "chat":
            per_ticket = settings.chat_per_ticket_concurrency
            per_validator = settings.chat_per_validator_concurrency
            global_limit = settings.chat_global_concurrency
            per_ticket_rpm = settings.chat_per_ticket_requests_per_minute
            per_validator_rpm = settings.chat_per_validator_requests_per_minute
            global_rpm = settings.chat_global_requests_per_minute
        else:
            per_ticket = settings.embedding_per_ticket_concurrency
            per_validator = settings.embedding_per_validator_concurrency
            global_limit = settings.embedding_global_concurrency
            per_ticket_rpm = settings.embedding_per_ticket_requests_per_minute
            per_validator_rpm = settings.embedding_per_validator_requests_per_minute
            global_rpm = settings.embedding_global_requests_per_minute
        lanes.append(
            InferenceLaneCurrent(
                request_kind=kind,
                active_requests=int(row["active_requests"]),
                live_grants=int(row["live_grants"]),
                stale_started_requests=int(row["stale_started_requests"]),
                per_ticket_limit=per_ticket,
                per_validator_limit=per_validator,
                global_limit=global_limit,
                per_ticket_rpm_limit=per_ticket_rpm,
                per_validator_rpm_limit=per_validator_rpm,
                global_rpm_limit=global_rpm,
                peak_per_ticket_concurrency_60m=peaks.get(("ticket", kind), 0),
                peak_per_validator_concurrency_60m=peaks.get(("validator", kind), 0),
                peak_global_concurrency_60m=global_peaks.get(kind, 0),
            )
        )
    return InferenceRuntimeMetrics(
        observed_at=datetime.now(UTC),
        settings_revision=latest.revision if latest is not None else 0,
        settings_checksum=latest.checksum if latest is not None else "",
        lanes=lanes,
        windows=windows,
        relays=list(relays),
    )


@router.post(
    "/admin/runtime-profiles",
    response_model=RuntimeProfileArtifact,
)
async def capture_runtime_profile(
    payload: RuntimeProfileCaptureRequest,
    request: Request,
    _admin: AdminDep,
    x_admin_actor: ActorDep = None,
) -> RuntimeProfileArtifact:
    if not x_admin_actor or not x_admin_actor.strip():
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    if payload.confirmation != CAPTURE_CONFIRMATION:
        raise HTTPException(
            status_code=409,
            detail=f"confirmation must be exactly {CAPTURE_CONFIRMATION}",
        )
    try:
        artifact = await _profile_store(request).capture(
            target=payload.target,
            profile_type=payload.profile_type,
            seconds=payload.seconds,
            actor=x_admin_actor.strip(),
            reason=payload.reason.strip(),
        )
    except RuntimeProfileBusyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RuntimeProfileError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    logger.info(
        "runtime profile captured target=%s type=%s seconds=%s actor=%s "
        "profile_id=%s sha256=%s bytes=%s reason=%r",
        artifact.target,
        artifact.profile_type,
        artifact.seconds,
        artifact.actor,
        artifact.profile_id,
        artifact.sha256,
        artifact.byte_size,
        artifact.reason,
    )
    return artifact


@router.get(
    "/admin/runtime-profiles/{profile_id}",
    response_model=RuntimeProfileArtifact,
)
async def get_runtime_profile(
    profile_id: UUID,
    request: Request,
    _admin: AdminDep,
) -> RuntimeProfileArtifact:
    try:
        return _profile_store(request).get(profile_id)
    except RuntimeProfileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get(
    "/admin/runtime-profiles/{profile_id}/download",
    response_class=FileResponse,
)
async def download_runtime_profile(
    profile_id: UUID,
    request: Request,
    _admin: AdminDep,
    x_admin_actor: ActorDep = None,
) -> FileResponse:
    if not x_admin_actor or not x_admin_actor.strip():
        raise HTTPException(status_code=422, detail="X-Admin-Actor is required")
    try:
        artifact, path = _profile_store(request).download(profile_id)
    except RuntimeProfileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    logger.info(
        "runtime profile downloaded profile_id=%s actor=%s sha256=%s bytes=%s",
        artifact.profile_id,
        x_admin_actor.strip(),
        artifact.sha256,
        artifact.byte_size,
    )
    return FileResponse(
        path,
        media_type=artifact.media_type,
        filename=artifact.filename,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Profile-SHA256": artifact.sha256,
        },
    )
