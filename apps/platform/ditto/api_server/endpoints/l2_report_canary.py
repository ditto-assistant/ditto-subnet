"""Isolated L2 audit jobs; no path in this module writes screening authority."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.l2_report_canary import (
    CanonicalFixtureRegisterRequest,
    CanonicalFixtureReviewRequest,
    CanonicalFixtureScheduleRequest,
    L2CanaryClaimRequest,
    L2CanaryClaimResponse,
    L2CanaryCompleteRequest,
    L2CanaryCompleteResponse,
    L2CanaryPreflightView,
    L2CanaryScheduleRequest,
    L2CanaryView,
)
from ditto.api_models.system_health import fleet_release_from_heartbeat_envelope
from ditto.api_server.canonical_starter_control import (
    ARCHIVE_BYTES,
    ARCHIVE_SHA256,
    DOCKERFILE_SHA256,
    OBJECT_KEY,
    RELEASE,
    RELEASE_COMMIT,
    SOURCE_TREE,
    archive_bytes,
)
from ditto.api_server.dependencies import get_session, get_storage_client
from ditto.api_server.endpoints.admin_quarantine import require_admin
from ditto.api_server.endpoints.screener import (
    _artifact_key,
    _resolve_effective_review_settings,
    require_screener,
)
from ditto.api_server.operator_proof import require_operator_proof
from ditto.api_server.scored_runtime_evidence import scored_runtime_evidence_for_lease
from ditto.api_server.source_inspect import MAX_TARBALL_BYTES
from ditto.api_server.storage import S3StorageClient, StorageError
from ditto.db.models import (
    Agent,
    AthReview,
    AthReviewAction,
    Score,
    ScreenerHeartbeat,
    ScreenerL2ReportCanary,
    ScreenerNode,
    ScreeningAttempt,
    ScreeningReviewEvent,
)
from ditto.db.queries.benchmark_rollout import arrival_bench_version

admin_router = APIRouter(prefix="/admin/screener-l2-report-canaries", tags=["admin"])
screener_router = APIRouter(prefix="/screener/l2-report-canaries", tags=["screener"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
AdminDep = Annotated[None, Depends(require_admin)]
ScreenerDep = Annotated[str, Depends(require_screener)]
_MIN_LEASE = timedelta(minutes=45)
# L1 and L2 each have their own aggregate deadline in the report-only lane.
# Source-only replays also need time to download, validate, and submit the report.
_SOURCE_ONLY_OVERHEAD = timedelta(minutes=10)
_FIXTURE_BUILD_OVERHEAD = timedelta(minutes=60)
# Full-runtime replays additionally build and probe an untrusted image and run
# bounded private challenges before the source-review result is complete.
_FULL_RUNTIME_OVERHEAD = timedelta(minutes=60)
_FULL_RUNTIME_MIN_RELEASE = (0, 317, 2)
_MAX_PARALLEL_SOURCE_ONLY = 4
_WORKER_HEARTBEAT_MAX_AGE = timedelta(minutes=5)


def _canary_lease(
    *,
    source_review_timeout_seconds: int,
    l2_timeout_seconds: int,
    run_mode: str,
    source_kind: str = "submission",
) -> timedelta:
    overhead = (
        _FULL_RUNTIME_OVERHEAD if run_mode == "full_runtime" else _SOURCE_ONLY_OVERHEAD
    )
    if source_kind == "canonical_starter_fixture":
        overhead = _FIXTURE_BUILD_OVERHEAD
    return max(
        _MIN_LEASE,
        timedelta(seconds=source_review_timeout_seconds + l2_timeout_seconds)
        + overhead,
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _view(row: ScreenerL2ReportCanary) -> L2CanaryView:
    return L2CanaryView(
        canary_id=row.canary_id,
        request_id=row.request_id,
        agent_id=row.agent_id,
        source_attempt_id=row.source_attempt_id,
        source_kind=cast(
            Literal["submission", "canonical_starter_fixture"], row.source_kind
        ),
        fixture_key=row.fixture_key,
        artifact_sha256=row.artifact_sha256,
        target_node_id=row.target_node_id,
        expected_agent_status=row.expected_agent_status,
        expected_score_count=row.expected_score_count,
        review_label=row.review_label,
        run_mode=cast(Literal["source_only", "full_runtime"], row.run_mode),
        source_attestation=row.source_attestation,
        status=row.status,
        claimed_instance_id=row.claimed_instance_id,
        lease_expires_at=row.lease_expires_at,
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
    if row.run_mode == "full_runtime":
        codes = report.get("decision_evidence_codes", [])
        if not isinstance(codes, list) or not all(
            isinstance(code, str) for code in codes
        ):
            return False
        challenge_codes = [
            code
            for code in codes
            if code.startswith(("challenge-", "behavioral-oracle-"))
        ]
        if report.get("challenge_evidence_codes") != challenge_codes:
            return False
        if not challenge_codes:
            expected_challenge_status = "not_run"
        elif any(
            code
            not in {
                "challenge-observed",
                "challenge-model-call-missing",
                "challenge-gateway-token-missing",
                "challenge-shape-anomaly",
                "behavioral-oracle-passed",
                "behavioral-oracle-wrong-answer",
                "behavioral-oracle-implausibly-fast",
            }
            for code in challenge_codes
        ):
            expected_challenge_status = "inconclusive"
        else:
            expected_challenge_status = "completed"
        if report.get("challenge_status") != expected_challenge_status:
            return False
    # Rolling workers may finish an older shadow lease, while current workers
    # preview the enforced source decision in either isolated run mode.
    allowed_review_modes = {"shadow", "enforce_preview"}
    report_agent_id = row.agent_id or row.canary_id
    report_attempt_id = row.source_attempt_id or row.canary_id
    return (
        report.get("kind") == "l2_report_canary_v1"
        and report.get("authority") == "none"
        and report.get("review_mode") in allowed_review_modes
        and report.get("canary_id") == str(row.canary_id)
        and report.get("agent_id") == str(report_agent_id)
        and report.get("source_attempt_id") == str(report_attempt_id)
        and report.get("source_kind", "submission") == row.source_kind
        and (
            row.source_kind == "submission"
            or (
                _fixture_attestation_valid(row)
                and report.get("source_attestation") == row.source_attestation
                and report.get("control_result") == _fixture_control_result(report)
            )
        )
        and report.get("artifact_sha256") == row.artifact_sha256
        and report.get("policy_version") == row.policy_version
        and report.get("settings_revision") == row.settings_revision
        and report.get("settings_checksum") == row.settings_checksum
        and report.get("run_mode", "source_only") == row.run_mode
        and secrets.compare_digest(digest, row.runtime_evidence_sha256)
    )


def _fixture_control_result(report: dict) -> str:
    l1 = report.get("l1")
    l2 = report.get("l2")
    if isinstance(l2, dict) and report.get("decision_outcome") in {
        "quarantine",
        "deterministic_reject",
    }:
        return "hold"
    if (
        report.get("decision_outcome") == "pass"
        and isinstance(l1, dict)
        and l1.get("clearance_certified") is True
        and isinstance(l2, dict)
        and l2.get("clearance_certified") is True
        and isinstance(report.get("built_image_digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", report["built_image_digest"])
    ):
        return "certificate"
    return "inconclusive"


async def _full_runtime_worker_ready(
    session: AsyncSession,
    *,
    node: ScreenerNode,
    now: datetime,
    instance_id: str | None = None,
    minimum_release: tuple[int, int, int] = _FULL_RUNTIME_MIN_RELEASE,
) -> bool:
    """Do not give a new-mode lease to a rolling old worker."""
    rows = await session.scalars(
        select(ScreenerHeartbeat).where(
            ScreenerHeartbeat.screener_hotkey == node.screener_hotkey,
            ScreenerHeartbeat.seen_at >= now - timedelta(minutes=5),
        )
    )
    for row in rows:
        if instance_id is None:
            if not row.instance_id.startswith(f"{node.node_id}-worker-"):
                continue
        elif row.instance_id != instance_id:
            continue
        release = fleet_release_from_heartbeat_envelope(row.system_metrics)
        match = (
            re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", release.version)
            if release is not None and release.version is not None
            else None
        )
        if (
            match is not None
            and release is not None
            and release.revision is not None
            and tuple(map(int, match.groups())) >= minimum_release
        ):
            return True
    return False


async def _fixture_worker_ready(
    session: AsyncSession,
    *,
    node: ScreenerNode,
    now: datetime,
    instance_id: str | None = None,
) -> bool:
    """Only a fresh signed v8 capability may claim the new fixture shape."""
    rows = await session.scalars(
        select(ScreenerHeartbeat).where(
            ScreenerHeartbeat.screener_hotkey == node.screener_hotkey,
            ScreenerHeartbeat.protocol_version >= 8,
            ScreenerHeartbeat.seen_at >= now - _WORKER_HEARTBEAT_MAX_AGE,
            ScreenerHeartbeat.state.in_(("polling", "screening")),
        )
    )
    for row in rows:
        if instance_id is None:
            if not row.instance_id.startswith(f"{node.node_id}-worker-"):
                continue
        elif row.instance_id != instance_id:
            continue
        release = fleet_release_from_heartbeat_envelope(row.system_metrics)
        if release is not None and release.source_fixture_v1:
            return True
    return False


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
    if row.agent_id is None or row.source_attempt_id is None:
        raise HTTPException(status_code=409, detail="not a submission canary")
    agent = await session.get(Agent, row.agent_id)
    attempt = await session.get(ScreeningAttempt, row.source_attempt_id)
    if (
        agent is None
        or attempt is None
        or attempt.agent_id != row.agent_id
        or agent.sha256.lower() != row.artifact_sha256
        or attempt.policy_version != row.policy_version
        or agent.status.value != row.expected_agent_status
        or await _score_count(session, row.agent_id) != row.expected_score_count
    ):
        raise HTTPException(status_code=409, detail="canary exact-source guard changed")
    if row.source_attestation is None:
        if (attempt.artifact_sha256 or "").lower() != row.artifact_sha256:
            raise HTTPException(
                status_code=409, detail="canary exact-source guard changed"
            )
    elif (
        not isinstance(row.source_attestation, dict)
        or not row.source_attestation.get("verified_at")
        or attempt.artifact_sha256 is not None
        or row.run_mode != "source_only"
        or row.source_attestation.get("artifact_sha256") != row.artifact_sha256
        or not await _historical_ruling_matches(session, row)
    ):
        raise HTTPException(status_code=409, detail="canary source attestation changed")
    return agent, attempt


def _fixture_attestation_valid(row: ScreenerL2ReportCanary) -> bool:
    att = row.source_attestation
    return bool(
        row.source_kind == "canonical_starter_fixture"
        and row.fixture_key == OBJECT_KEY
        and row.artifact_sha256 == ARCHIVE_SHA256
        and row.run_mode == "source_only"
        and row.policy_version == 13
        and row.bench_version == 13
        and row.review_label == "candidate_clear"
        and isinstance(att, dict)
        and att.get("release") == RELEASE
        and att.get("release_commit") == RELEASE_COMMIT
        and att.get("source_tree") == SOURCE_TREE
        and att.get("archive_sha256") == ARCHIVE_SHA256
        and att.get("archive_size_bytes") == ARCHIVE_BYTES
        and att.get("reviewed_archive_sha256") == ARCHIVE_SHA256
        and att.get("reviewed_dockerfile_sha256") == DOCKERFILE_SHA256
        and isinstance(att.get("registered_by"), str)
        and isinstance(att.get("reviewed_by"), str)
        and att["registered_by"] != att["reviewed_by"]
        and isinstance(att.get("reviewer_built_image_digest"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", att["reviewer_built_image_digest"])
        and isinstance(att.get("reviewer_evidence_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", att["reviewer_evidence_sha256"])
    )


async def _fixture_object_matches(storage: S3StorageClient) -> bool:
    try:
        verified = await storage.verify_object_sha256(
            key=OBJECT_KEY, expected_size_bytes=ARCHIVE_BYTES
        )
    except StorageError:
        return False
    return verified.sha256 == ARCHIVE_SHA256 and verified.size_bytes == ARCHIVE_BYTES


async def _historical_ruling_matches(
    session: AsyncSession, row: ScreenerL2ReportCanary
) -> bool:
    """A historical ruling labels this current-object replay, not past execution."""
    attestation = row.source_attestation
    if not isinstance(attestation, dict):
        return False
    try:
        ruling_id = UUID(attestation["ruling_id"])
    except (KeyError, TypeError, ValueError):
        return False
    if attestation.get("kind") == "ath_clear" and row.review_label == "candidate_clear":
        ath_review = await session.get(AthReview, ruling_id)
        action = await _latest_ath_action(session, ruling_id)
        return bool(
            ath_review is not None
            and action is not None
            and ath_review.agent_id == row.agent_id
            and ath_review.original_policy_version == row.policy_version
            and ath_review.status == "resolved"
            and ath_review.resolution == "clear"
            and ath_review.original_evidence.get("sha256") == row.artifact_sha256
            and action.action == "clear"
            and str(action.action_id) == attestation.get("action_id")
            and ath_review.resolved_at == action.created_at
            and ath_review.resolved_by == action.actor
            and ath_review.resolution_reason == action.reason
        )
    if (
        attestation.get("kind") == "screening_reject"
        and row.review_label == "known_reject"
    ):
        event = await session.get(ScreeningReviewEvent, ruling_id)
        return bool(
            event is not None
            and event.agent_id == row.agent_id
            and event.attempt_id == row.source_attempt_id
            and event.policy_version == row.policy_version
            and event.event_kind == "manual"
            and event.outcome == "reject"
            and event.effective_decision == "reject"
            and event.artifact_sha256 == row.artifact_sha256
        )
    return False


async def _latest_ath_action(
    session: AsyncSession, review_id: UUID
) -> AthReviewAction | None:
    return await session.scalar(
        select(AthReviewAction)
        .where(AthReviewAction.review_id == review_id)
        .order_by(AthReviewAction.created_at.desc(), AthReviewAction.action_id.desc())
        .limit(1)
    )


async def _current_object_matches(
    storage: S3StorageClient, agent: Agent, artifact_sha256: str
) -> tuple[bool, int]:
    expected_size = agent.size_bytes
    if expected_size is not None and not (0 < expected_size <= MAX_TARBALL_BYTES):
        return False, 0
    verified = await storage.verify_object_sha256(
        key=_artifact_key(agent.agent_id),
        expected_size_bytes=expected_size or MAX_TARBALL_BYTES,
    )
    return (
        verified.sha256 == artifact_sha256
        and 0 < verified.size_bytes <= MAX_TARBALL_BYTES
        and (expected_size is None or verified.size_bytes == expected_size),
        verified.size_bytes,
    )


@admin_router.get(
    "/preflight/{agent_id}/{source_attempt_id}", response_model=L2CanaryPreflightView
)
async def get_l2_report_canary_preflight(
    agent_id: UUID,
    source_attempt_id: UUID,
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
) -> L2CanaryPreflightView:
    """Expose exact guard inputs; scheduling still rechecks them under a lock."""
    response.headers["Cache-Control"] = "no-store"
    agent = await session.get(Agent, agent_id)
    attempt = await session.get(ScreeningAttempt, source_attempt_id)
    if agent is None or attempt is None or attempt.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="canary source not found")
    return L2CanaryPreflightView(
        agent_id=agent_id,
        source_attempt_id=source_attempt_id,
        agent_artifact_sha256=agent.sha256.lower(),
        source_attempt_artifact_sha256=(
            attempt.artifact_sha256.lower() if attempt.artifact_sha256 else None
        ),
        agent_status=agent.status.value,
        attempt_policy_version=attempt.policy_version,
        arrival_bench_version=await arrival_bench_version(session, agent=agent),
        score_row_count=await _score_count(session, agent_id),
    )


@admin_router.post("", response_model=L2CanaryView)
async def schedule_l2_report_canary(
    payload: L2CanaryScheduleRequest,
    _admin: AdminDep,
    session: SessionDep,
    storage: Annotated[S3StorageClient, Depends(get_storage_client)],
    x_admin_actor: Annotated[str | None, Header()] = None,
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
                or existing.run_mode != payload.run_mode
                or existing.policy_version != payload.policy_version
                or existing.expected_agent_status != payload.expected_agent_status
                or existing.expected_score_count != payload.expected_score_count
                or (existing.source_attestation or {}).get("kind")
                != payload.historical_ruling_kind
                or (existing.source_attestation or {}).get("ruling_id")
                != (
                    str(payload.historical_ruling_id)
                    if payload.historical_ruling_id is not None
                    else None
                )
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
        if payload.run_mode == "full_runtime" and not await _full_runtime_worker_ready(
            session, node=node, now=datetime.now(UTC)
        ):
            raise HTTPException(409, "full-runtime canary worker not adopted")
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
            run_mode=payload.run_mode,
            status="queued",
        )
        if payload.historical_ruling_id is not None:
            if not x_admin_actor or not x_admin_actor.strip():
                raise HTTPException(status_code=400, detail="operator actor required")
            row.source_attestation = {
                "kind": payload.historical_ruling_kind,
                "ruling_id": str(payload.historical_ruling_id),
                "artifact_sha256": payload.artifact_sha256,
                "scope": "current-object-only; historical execution unverified",
                "actor": x_admin_actor.strip(),
            }
        agent = await session.get(Agent, row.agent_id, with_for_update=True)
        if agent is None:
            raise HTTPException(status_code=409, detail="canary source not found")
        if row.source_attestation is not None:
            if payload.historical_ruling_kind == "ath_clear":
                assert payload.historical_ruling_id is not None
                action = await _latest_ath_action(session, payload.historical_ruling_id)
                if action is None or action.action != "clear":
                    raise HTTPException(
                        status_code=409, detail="ATH clear action missing"
                    )
                row.source_attestation["action_id"] = str(action.action_id)
            if not await _historical_ruling_matches(session, row):
                raise HTTPException(status_code=409, detail="historical ruling changed")
            try:
                matches, size_bytes = await _current_object_matches(
                    storage, agent, row.artifact_sha256
                )
            except StorageError:
                raise HTTPException(
                    503, "source object verification unavailable"
                ) from None
            if not matches:
                raise HTTPException(409, "current source object differs from ruling")
            row.source_attestation["verified_size_bytes"] = size_bytes
            row.source_attestation["verified_at"] = datetime.now(UTC).isoformat()
        agent, _ = await _exact_source(session, row)
        if await arrival_bench_version(session, agent=agent) != 13:
            raise HTTPException(status_code=409, detail="source is not benchmark v13")
        session.add(row)
        await session.flush()
        return _view(row)


@admin_router.get("/fixture/preflight")
async def get_canonical_fixture_preflight(
    response: Response,
    _admin: AdminDep,
    session: SessionDep,
    storage: Annotated[S3StorageClient, Depends(get_storage_client)],
) -> dict:
    """Expose the pinned public source and current object before any queue write."""
    response.headers["Cache-Control"] = "no-store"
    row = await session.scalar(
        select(ScreenerL2ReportCanary).where(
            ScreenerL2ReportCanary.fixture_key == OBJECT_KEY
        )
    )
    node = (
        await session.get(ScreenerNode, row.target_node_id) if row is not None else None
    )
    worker_ready = bool(
        node is not None
        and await _fixture_worker_ready(session, node=node, now=datetime.now(UTC))
    )
    object_matches = await _fixture_object_matches(storage)
    return {
        "release": RELEASE,
        "release_commit": RELEASE_COMMIT,
        "source_tree": SOURCE_TREE,
        "archive_sha256": ARCHIVE_SHA256,
        "archive_size_bytes": ARCHIVE_BYTES,
        "fixture": _view(row).model_dump(mode="json") if row is not None else None,
        "stored_object_matches": object_matches,
        "fixture_capable_worker_ready": worker_ready,
        "can_schedule": bool(
            row is not None
            and row.status == "ready"
            and _fixture_attestation_valid(row)
            and object_matches
            and worker_ready
        ),
    }


@admin_router.post("/fixture/register", response_model=L2CanaryView)
async def register_canonical_fixture(
    payload: CanonicalFixtureRegisterRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: Annotated[S3StorageClient, Depends(get_storage_client)],
) -> L2CanaryView:
    """Stage exact public source without creating a miner or screening attempt."""
    actor = await require_operator_proof(request)
    async with session.begin():
        existing = await session.scalar(
            select(ScreenerL2ReportCanary).where(
                ScreenerL2ReportCanary.fixture_key == OBJECT_KEY
            )
        )
        if existing is not None:
            if (
                existing.request_id == payload.request_id
                and existing.target_node_id == payload.target_node_id
            ):
                return _view(existing)
            raise HTTPException(409, "canonical starter fixture already registered")
        node = await session.get(ScreenerNode, payload.target_node_id)
        if node is None or node.status != "active" or node.provider != "hetzner":
            raise HTTPException(409, "target is not an active Hetzner screener node")
        data = archive_bytes()
        try:
            await storage.put_object(
                key=OBJECT_KEY, body=data, content_type="application/gzip"
            )
        except StorageError:
            raise HTTPException(503, "fixture object upload unavailable") from None
        if not await _fixture_object_matches(storage):
            raise HTTPException(503, "fixture object verification failed")
        row = ScreenerL2ReportCanary(
            canary_id=uuid4(),
            request_id=payload.request_id,
            source_kind="canonical_starter_fixture",
            fixture_key=OBJECT_KEY,
            agent_id=None,
            source_attempt_id=None,
            artifact_sha256=ARCHIVE_SHA256,
            policy_version=13,
            bench_version=13,
            target_node_id=payload.target_node_id,
            expected_agent_status=None,
            expected_score_count=None,
            review_label="unreviewed",
            run_mode="source_only",
            status="awaiting_review",
            source_attestation={
                "release": RELEASE,
                "release_commit": RELEASE_COMMIT,
                "source_tree": SOURCE_TREE,
                "archive_sha256": ARCHIVE_SHA256,
                "archive_size_bytes": ARCHIVE_BYTES,
                "registered_by": actor,
            },
        )
        session.add(row)
        await session.flush()
        return _view(row)


@admin_router.post("/fixture/{canary_id}/review", response_model=L2CanaryView)
async def review_canonical_fixture(
    canary_id: UUID,
    payload: CanonicalFixtureReviewRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: Annotated[S3StorageClient, Depends(get_storage_client)],
) -> L2CanaryView:
    """Require a second authenticated operator's served-path evidence."""
    actor = await require_operator_proof(request)
    async with session.begin():
        row = await session.scalar(
            select(ScreenerL2ReportCanary)
            .where(ScreenerL2ReportCanary.canary_id == canary_id)
            .with_for_update()
        )
        if row is None or row.source_kind != "canonical_starter_fixture":
            raise HTTPException(404, "fixture not found")
        if row.status != "awaiting_review" or not isinstance(
            row.source_attestation, dict
        ):
            raise HTTPException(409, "fixture review state changed")
        if actor == row.source_attestation.get("registered_by"):
            raise HTTPException(409, "independent reviewer required")
        if not await _fixture_object_matches(storage):
            raise HTTPException(409, "fixture object changed")
        row.source_attestation = {
            **row.source_attestation,
            "reviewed_by": actor,
            "reviewer_evidence_sha256": payload.reviewer_evidence_sha256,
            "reviewer_evidence_url": payload.reviewer_evidence_url,
            "reviewed_archive_sha256": payload.reviewed_archive_sha256,
            "reviewed_dockerfile_sha256": payload.reviewed_dockerfile_sha256,
            "reviewer_built_image_digest": payload.built_image_digest,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "scope": "candidate public source and served path; no verdict authority",
        }
        row.review_label = "candidate_clear"
        row.status = "ready"
        if not _fixture_attestation_valid(row):
            raise HTTPException(409, "fixture evidence invalid")
        return _view(row)


@admin_router.post("/fixture/{canary_id}/schedule", response_model=L2CanaryView)
async def schedule_canonical_fixture(
    canary_id: UUID,
    _payload: CanonicalFixtureScheduleRequest,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    storage: Annotated[S3StorageClient, Depends(get_storage_client)],
) -> L2CanaryView:
    """Queue one source-only report; no submission or admission state changes."""
    actor = await require_operator_proof(request)
    async with session.begin():
        row = await session.scalar(
            select(ScreenerL2ReportCanary)
            .where(ScreenerL2ReportCanary.canary_id == canary_id)
            .with_for_update()
        )
        if row is None or row.source_kind != "canonical_starter_fixture":
            raise HTTPException(404, "fixture not found")
        if row.status != "ready" or not _fixture_attestation_valid(row):
            raise HTTPException(409, "fixture not independently ready")
        assert isinstance(row.source_attestation, dict)
        if actor == row.source_attestation["reviewed_by"]:
            raise HTTPException(409, "reviewer cannot schedule their own control")
        if not await _fixture_object_matches(storage):
            raise HTTPException(409, "fixture object changed")
        node = await session.get(ScreenerNode, row.target_node_id)
        if node is None or node.status != "active" or node.provider != "hetzner":
            raise HTTPException(409, "target node unavailable")
        if not await _fixture_worker_ready(session, node=node, now=datetime.now(UTC)):
            raise HTTPException(409, "fixture-capable worker not adopted")
        row.status = "queued"
        row.source_attestation = {**row.source_attestation, "scheduled_by": actor}
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
        active = list(
            await session.scalars(
                select(ScreenerL2ReportCanary)
                .where(
                    ScreenerL2ReportCanary.target_node_id == node_id,
                    ScreenerL2ReportCanary.status == "leased",
                )
                .with_for_update()
            )
        )
        if any(row.claimed_instance_id == payload.instance_id for row in active):
            return None
        # Keep private-challenge runs isolated. Preserve the legacy first lease
        # without requiring a heartbeat; additional source-only leases require
        # fresh worker heartbeats and the node lock serializes their count.
        if any(row.run_mode == "full_runtime" for row in active):
            return None
        if active:
            healthy_workers = set(
                await session.scalars(
                    select(ScreenerHeartbeat.instance_id).where(
                        ScreenerHeartbeat.screener_hotkey == node.screener_hotkey,
                        ScreenerHeartbeat.instance_id.like(f"{node_id}-worker-%"),
                        ScreenerHeartbeat.seen_at >= now - _WORKER_HEARTBEAT_MAX_AGE,
                        ScreenerHeartbeat.state.in_(("polling", "screening")),
                    )
                )
            )
            if payload.instance_id not in healthy_workers or len(active) >= min(
                _MAX_PARALLEL_SOURCE_ONLY, len(healthy_workers)
            ):
                return None
        queued = select(ScreenerL2ReportCanary).where(
            ScreenerL2ReportCanary.target_node_id == node_id,
            ScreenerL2ReportCanary.status == "queued",
        )
        if active:
            queued = queued.where(ScreenerL2ReportCanary.run_mode == "source_only")
        if not await _full_runtime_worker_ready(
            session, node=node, now=now, instance_id=payload.instance_id
        ):
            # Leave full-runtime rows for an adopted worker rather than
            # returning nothing: scheduling accepts any adopted worker on the
            # node, so the oldest row may be one this caller can never take,
            # and it must not block the source-only rows queued behind it.
            queued = queued.where(ScreenerL2ReportCanary.run_mode != "full_runtime")
        if not await _fixture_worker_ready(
            session,
            node=node,
            now=now,
            instance_id=payload.instance_id,
        ):
            queued = queued.where(
                ScreenerL2ReportCanary.source_kind != "canonical_starter_fixture"
            )
        row = await session.scalar(
            queued.order_by(ScreenerL2ReportCanary.created_at).with_for_update(
                skip_locked=True
            )
        )
        if row is None:
            return None
        agent = None
        if row.source_kind == "canonical_starter_fixture":
            if not _fixture_attestation_valid(row) or not await _fixture_object_matches(
                storage
            ):
                row.status = "incomplete"
                row.error_code = "fixture-source-drift"
                row.completed_at = now
                return None
        else:
            try:
                agent, _ = await _exact_source(session, row)
            except HTTPException:
                row.status = "incomplete"
                row.error_code = "exact-source-changed"
                row.completed_at = now
                return None
        if agent is not None and row.source_attestation is not None:
            try:
                matches, _ = await _current_object_matches(
                    storage, agent, row.artifact_sha256
                )
            except StorageError:
                return None
            if not matches:
                row.status = "incomplete"
                row.error_code = "source-object-drift"
                row.completed_at = now
                return None
        evidence = await scored_runtime_evidence_for_lease(
            session,
            attempt_id=row.source_attempt_id or row.canary_id,
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
        row.lease_expires_at = now + _canary_lease(
            source_review_timeout_seconds=(
                effective.settings.source_review_timeout_seconds
            ),
            l2_timeout_seconds=effective.settings.timeout_seconds,
            run_mode=row.run_mode,
            source_kind=row.source_kind,
        )
        # URL issuance is scoped to this canary, not to a running screening attempt.
        if agent is None:
            key = row.fixture_key
            if key is None:
                raise HTTPException(409, "fixture object key missing")
        else:
            key = _artifact_key(agent.agent_id)
        url = await storage.presigned_get_url(
            key=key,
            expires_in=900,
        )
        return L2CanaryClaimResponse(
            canary_id=row.canary_id,
            agent_id=row.agent_id or row.canary_id,
            source_attempt_id=row.source_attempt_id or row.canary_id,
            artifact_sha256=row.artifact_sha256,
            bench_version=row.bench_version,
            policy_version=row.policy_version,
            run_mode=cast(Literal["source_only", "full_runtime"], row.run_mode),
            source_kind=cast(
                Literal["submission", "canonical_starter_fixture"], row.source_kind
            ),
            source_attestation=row.source_attestation if agent is None else None,
            miner_hotkey=agent.miner_hotkey
            if agent is not None
            else "operator-source-fixture",
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
    storage: Annotated[S3StorageClient | None, Depends(get_storage_client)] = None,
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
        if row.source_kind == "canonical_starter_fixture":
            if (
                storage is None
                or not _fixture_attestation_valid(row)
                or not await _fixture_object_matches(storage)
            ):
                raise HTTPException(409, "fixture source changed")
        else:
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
