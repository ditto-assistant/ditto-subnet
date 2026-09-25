"""Isolated L2 audit jobs; no path in this module writes screening authority."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.l2_report_canary import (
    L2CanaryClaimRequest,
    L2CanaryClaimResponse,
    L2CanaryCompleteRequest,
    L2CanaryCompleteResponse,
    L2CanaryScheduleRequest,
    L2CanaryView,
)
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.screener import (
    _artifact_key,
    _resolve_effective_review_settings,
    require_screener,
)
from ditto.api_server.scored_runtime_evidence import scored_runtime_evidence_for_lease
from ditto.api_server.storage import S3StorageClient
from ditto.db.models import (
    Agent,
    Score,
    ScreenerL2ReportCanary,
    ScreenerNode,
    ScreeningAttempt,
)
from ditto.db.queries.benchmark_rollout import arrival_bench_version

admin_router = APIRouter(prefix="/admin/screener-l2-report-canaries", tags=["admin"])
screener_router = APIRouter(prefix="/screener/l2-report-canaries", tags=["screener"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
ScreenerDep = Annotated[str, Depends(require_screener)]
_LEASE = timedelta(minutes=45)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _view(row: ScreenerL2ReportCanary) -> L2CanaryView:
    return L2CanaryView(
        canary_id=row.canary_id,
        request_id=row.request_id,
        agent_id=row.agent_id,
        source_attempt_id=row.source_attempt_id,
        artifact_sha256=row.artifact_sha256,
        target_node_id=row.target_node_id,
        expected_agent_status=row.expected_agent_status,
        expected_score_count=row.expected_score_count,
        review_label=row.review_label,
        status=row.status,
        claimed_instance_id=row.claimed_instance_id,
        report=row.report,
        error_code=row.error_code,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


def _valid_report(row: ScreenerL2ReportCanary, report: dict) -> bool:
    packet = report.get("scored_runtime_evidence")
    if not isinstance(packet, dict) or row.runtime_evidence_sha256 is None:
        return False
    digest = hashlib.sha256(
        json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        report.get("kind") == "l2_report_canary_v1"
        and report.get("authority") == "none"
        and report.get("review_mode") == "shadow"
        and report.get("canary_id") == str(row.canary_id)
        and report.get("agent_id") == str(row.agent_id)
        and report.get("source_attempt_id") == str(row.source_attempt_id)
        and report.get("artifact_sha256") == row.artifact_sha256
        and report.get("policy_version") == row.policy_version
        and report.get("settings_revision") == row.settings_revision
        and report.get("settings_checksum") == row.settings_checksum
        and secrets.compare_digest(digest, row.runtime_evidence_sha256)
    )


async def _score_count(session: AsyncSession, agent_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Score).where(Score.agent_id == agent_id)
        )
        or 0
    )


async def _exact_source(
    session: AsyncSession, row: ScreenerL2ReportCanary
) -> tuple[Agent, ScreeningAttempt]:
    agent = await session.get(Agent, row.agent_id)
    attempt = await session.get(ScreeningAttempt, row.source_attempt_id)
    if (
        agent is None
        or attempt is None
        or attempt.agent_id != row.agent_id
        or (attempt.artifact_sha256 or "").lower() != row.artifact_sha256
        or agent.sha256.lower() != row.artifact_sha256
        or attempt.policy_version != row.policy_version
        or agent.status.value != row.expected_agent_status
        or await _score_count(session, row.agent_id) != row.expected_score_count
    ):
        raise HTTPException(status_code=409, detail="canary exact-source guard changed")
    return agent, attempt


@admin_router.post("", response_model=L2CanaryView)
async def schedule_l2_report_canary(
    payload: L2CanaryScheduleRequest,
    _admin: AdminDep,
    session: SessionDep,
) -> L2CanaryView:
    """Queue one exact source once; this never reopens a screening attempt."""
    async with session.begin():
        existing = await session.scalar(
            select(ScreenerL2ReportCanary).where(
                ScreenerL2ReportCanary.request_id == payload.request_id
            )
        )
        if existing is not None:
            if (
                existing.agent_id != payload.agent_id
                or existing.source_attempt_id != payload.source_attempt_id
                or existing.artifact_sha256 != payload.artifact_sha256
                or existing.target_node_id != payload.target_node_id
                or existing.review_label != payload.review_label
                or existing.policy_version != payload.policy_version
                or existing.expected_agent_status != payload.expected_agent_status
                or existing.expected_score_count != payload.expected_score_count
            ):
                raise HTTPException(
                    status_code=409, detail="canary already scheduled differently"
                )
            return _view(existing)
        node = await session.get(ScreenerNode, payload.target_node_id)
        if node is None or node.status != "active" or node.provider != "hetzner":
            raise HTTPException(
                status_code=409, detail="target is not an active Hetzner screener node"
            )
        # Serialize two distinct request ids for the same source attempt before
        # the partial unique index supplies its final database backstop.
        await session.scalar(
            select(ScreeningAttempt)
            .where(ScreeningAttempt.attempt_id == payload.source_attempt_id)
            .with_for_update()
        )
        active = await session.scalar(
            select(ScreenerL2ReportCanary.canary_id).where(
                ScreenerL2ReportCanary.source_attempt_id == payload.source_attempt_id,
                ScreenerL2ReportCanary.status.in_(("queued", "leased")),
            )
        )
        if active is not None:
            raise HTTPException(
                status_code=409, detail="source already has an active canary"
            )
        row = ScreenerL2ReportCanary(
            canary_id=uuid4(),
            request_id=payload.request_id,
            agent_id=payload.agent_id,
            source_attempt_id=payload.source_attempt_id,
            artifact_sha256=payload.artifact_sha256,
            policy_version=13,
            bench_version=13,
            target_node_id=payload.target_node_id,
            expected_agent_status=payload.expected_agent_status,
            expected_score_count=payload.expected_score_count,
            review_label=payload.review_label,
            status="queued",
        )
        agent, _ = await _exact_source(session, row)
        if await arrival_bench_version(session, agent=agent) != 13:
            raise HTTPException(status_code=409, detail="source is not benchmark v13")
        session.add(row)
        await session.flush()
        return _view(row)


@admin_router.get("/{canary_id}", response_model=L2CanaryView)
async def get_l2_report_canary(
    canary_id: UUID, _admin: AdminDep, session: SessionDep
) -> L2CanaryView:
    row = await session.get(ScreenerL2ReportCanary, canary_id)
    if row is None:
        raise HTTPException(status_code=404, detail="canary not found")
    return _view(row)


@screener_router.post("/claim", response_model=L2CanaryClaimResponse | None)
async def claim_l2_report_canary(
    payload: L2CanaryClaimRequest,
    request: Request,
    response: Response,
    _screener: ScreenerDep,
    session: SessionDep,
    storage: Annotated[S3StorageClient, Depends(get_storage_client)],
) -> L2CanaryClaimResponse | None:
    response.headers["Cache-Control"] = "no-store"
    node_id = getattr(request.state, "screener_node_id", None)
    if node_id is None or not (
        payload.instance_id == node_id
        or payload.instance_id.startswith(f"{node_id}-worker-")
    ):
        raise HTTPException(status_code=401, detail="enrolled node instance required")
    now = datetime.now(UTC)
    async with session.begin():
        node = await session.scalar(
            select(ScreenerNode)
            .where(ScreenerNode.node_id == node_id)
            .with_for_update()
        )
        if node is None or node.status != "active" or node.provider != "hetzner":
            raise HTTPException(status_code=409, detail="node unavailable")
        effective = await _resolve_effective_review_settings(
            session, instance_id=payload.instance_id, enrolled_node_id=node_id
        )
        if (
            effective.revision != payload.settings_revision
            or effective.checksum != payload.settings_checksum
        ):
            raise HTTPException(
                status_code=409, detail="canary review settings changed"
            )
        expired = list(
            await session.scalars(
                select(ScreenerL2ReportCanary)
                .where(
                    ScreenerL2ReportCanary.target_node_id == node_id,
                    ScreenerL2ReportCanary.status == "leased",
                    ScreenerL2ReportCanary.lease_expires_at < now,
                )
                .with_for_update()
            )
        )
        for stale in expired:
            stale.status = "expired"
            stale.error_code = "lease-expired"
            stale.completed_at = now
        active = await session.scalar(
            select(func.count())
            .select_from(ScreenerL2ReportCanary)
            .where(
                ScreenerL2ReportCanary.target_node_id == node_id,
                ScreenerL2ReportCanary.status == "leased",
            )
        )
        if active:
            return None
        row = await session.scalar(
            select(ScreenerL2ReportCanary)
            .where(
                ScreenerL2ReportCanary.target_node_id == node_id,
                ScreenerL2ReportCanary.status == "queued",
            )
            .order_by(ScreenerL2ReportCanary.created_at)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        try:
            agent, _ = await _exact_source(session, row)
        except HTTPException:
            row.status = "incomplete"
            row.error_code = "exact-source-changed"
            row.completed_at = now
            return None
        evidence = await scored_runtime_evidence_for_lease(
            session,
            attempt_id=row.source_attempt_id,
            artifact_sha256=row.artifact_sha256,
            policy_version=13,
            bench_version=13,
            now=now,
            report_only_current_packet=True,
        )
        if evidence is None:
            return None
        token = secrets.token_urlsafe(32)
        row.status = "leased"
        row.claimed_instance_id = payload.instance_id
        row.settings_revision = payload.settings_revision
        row.settings_checksum = payload.settings_checksum
        row.runtime_evidence_sha256 = hashlib.sha256(
            json.dumps(
                evidence.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        row.lease_token_hash = hashlib.sha256(token.encode()).hexdigest()
        row.lease_expires_at = now + _LEASE
        # URL issuance is scoped to this canary, not to a running screening attempt.
        url = await storage.presigned_get_url(
            key=_artifact_key(agent.agent_id), expires_in=900
        )
        return L2CanaryClaimResponse(
            canary_id=row.canary_id,
            agent_id=row.agent_id,
            source_attempt_id=row.source_attempt_id,
            artifact_sha256=row.artifact_sha256,
            bench_version=row.bench_version,
            policy_version=row.policy_version,
            miner_hotkey=agent.miner_hotkey,
            lease_token=token,
            lease_expires_at=row.lease_expires_at,
            download_url=url,
            scored_runtime_evidence=evidence,
        )


@screener_router.post("/{canary_id}/complete", response_model=L2CanaryCompleteResponse)
async def complete_l2_report_canary(
    canary_id: UUID,
    payload: L2CanaryCompleteRequest,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
) -> L2CanaryCompleteResponse:
    node_id = getattr(request.state, "screener_node_id", None)
    now = datetime.now(UTC)
    async with session.begin():
        row = await session.scalar(
            select(ScreenerL2ReportCanary)
            .where(ScreenerL2ReportCanary.canary_id == canary_id)
            .with_for_update()
        )
        if row is None or row.target_node_id != node_id:
            raise HTTPException(status_code=404, detail="canary not found")
        presented = hashlib.sha256(payload.lease_token.encode()).hexdigest()
        if row.lease_token_hash is None or not secrets.compare_digest(
            presented, row.lease_token_hash
        ):
            raise HTTPException(status_code=401, detail="invalid canary lease token")
        if row.status in {"succeeded", "incomplete"}:
            if (
                row.status == payload.status
                and row.report == payload.report
                and row.error_code == payload.error_code
            ):
                return L2CanaryCompleteResponse(accepted=True)
            raise HTTPException(status_code=409, detail="conflicting canary completion")
        if (
            row.status != "leased"
            or row.lease_expires_at is None
            or now > _utc(row.lease_expires_at)
        ):
            raise HTTPException(status_code=409, detail="canary lease expired")
        await _exact_source(session, row)
        if not _valid_report(row, payload.report):
            raise HTTPException(
                status_code=409, detail="canary report identity mismatch"
            )
        if payload.status == "succeeded" and not isinstance(
            payload.report.get("l2"), dict
        ):
            raise HTTPException(
                status_code=409, detail="successful canary lacks L2 audit"
            )
        row.status = payload.status
        row.report = payload.report
        row.error_code = payload.error_code
        row.completed_at = now
    return L2CanaryCompleteResponse(accepted=True)
