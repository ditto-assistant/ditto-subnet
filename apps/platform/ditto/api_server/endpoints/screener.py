"""Screener-facing endpoints — the cheap pre-evaluation gate.

The worker in the private ``ditto-screener`` repository drains freshly uploaded agents,
does a lint + compile + build check on each tarball, and reports a verdict.
A pass promotes the agent ``uploaded -> evaluating`` so the validator queue
picks it up. A deterministic submission failure becomes ``rejected``; a
    infrastructure failure becomes parked ``screening_failed`` work.

The platform stays thin: it owns the state machine + the queue only. The build
check lives in the worker. These endpoints mirror ``/validator/*`` so the two
workers look identical to an operator.

Lifecycle + scope decisions (documented so they're easy to revisit):

- **Queue = new uploads and explicitly authorized manual retries.**
  Two-score provisional contenders drain by score so likely winners can reach
  quorum; other submissions drain by fewest accepted scores, then arrival order.
- **Verdict is a direct promotion.** A pass sets ``evaluating`` (not
  ``screening_passed``). A deterministic fail sets ``rejected``; an
  infrastructure fail parks as ``screening_failed``. Re-reporting the same
  verdict is idempotent; a conflicting or late verdict is a 409.
- **Dedicated auth.** Every request carries a bearer token and the configured
  screener hotkey. Result POSTs additionally verify the hotkey's sr25519
  signature over the verdict and its policy version.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models import (
    ArtifactResponse,
    EffectiveScreenerNodeChannelSettings,
    EffectiveScreenerProviderSettings,
    ScreenedImageAbortRequest,
    ScreenedImageAbortResponse,
    ScreenedImageCompleteRequest,
    ScreenedImageCompleteResponse,
    ScreenedImagePartRequest,
    ScreenedImagePartResponse,
    ScreenedImageUploadRequest,
    ScreenedImageUploadResponse,
    ScreenerBootstrapGrantRequest,
    ScreenerBootstrapGrantResponse,
    ScreenerCapacitySnapshotRequest,
    ScreenerCapacitySnapshotResponse,
    ScreenerControllerFenceRequest,
    ScreenerControllerNodesResponse,
    ScreenerControllerNodeState,
    ScreenerHeartbeatRequest,
    ScreenerHeartbeatResponse,
    ScreenerNodeCredentialResponse,
    ScreenerNodeJobClaimRequest,
    ScreenerNodeJobUpdateRequest,
    ScreenerNodeRefreshRequest,
    ScreenerNodeRegistrationRequest,
    ScreenerNodeRuntimeResultRequest,
    ScreenerNodeStatusRequest,
    ScreenerQueueItem,
    ScreenerQueueResponse,
    ScreenResultRequest,
    ScreenResultResponse,
    SubmissionImageBuildRequest,
    SubmissionImageBuildResponse,
    SubmissionSourceReviewRequest,
    SubmissionSourceReviewResponse,
)
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import (
    ShadowReviewObservationRequest,
    ShadowReviewObservationResponse,
)
from ditto.api_models.screener_nodes import (
    SubmissionBuildCompleteRequest,
    SubmissionBuildCompleteResponse,
    SubmissionBuildSourceResponse,
    SubmissionBuildUploadRequest,
    SubmissionBuildUploadResponse,
    SubmissionImageBuildClaimResponse,
    SubmissionImageBuildClaimView,
    SubmissionImageBuildCleanupRequest,
    SubmissionImageBuildControllerStatusResponse,
    SubmissionImageBuildControllerUpdateRequest,
    SubmissionRuntimeArtifactClaimResponse,
    SubmissionRuntimeArtifactResponse,
    SubmissionRuntimeResultRequest,
    SubmissionSourceReviewClaimResponse,
    SubmissionSourceReviewClaimView,
    SubmissionSourceReviewCleanupRequest,
    SubmissionSourceReviewCompleteRequest,
    SubmissionSourceReviewCompleteResponse,
    SubmissionSourceReviewControllerStatusResponse,
    SubmissionSourceReviewControllerUpdateRequest,
    SubmissionSourceReviewSourceResponse,
    TrustedImageBuildClaimRequest,
    TrustedImageBuildClaimResponse,
    TrustedImageBuildCreateRequest,
    TrustedImageBuildStatus,
    TrustedImageBuildUpdateRequest,
    TrustedImageBuildView,
)
from ditto.api_models.screener_provider_settings import ScreenerProviderSettings
from ditto.api_models.screener_review_settings import (
    EffectiveScreenerReviewSettings,
    ScreenerReviewSettings,
)
from ditto.api_models.system_health import (
    host_specs_signing_token,
    system_metrics_signing_token,
)
from ditto.api_server.artifact_audit import client_ip, request_detail
from ditto.api_server.attestation import expected_netuid
from ditto.api_server.benchmark_rollout import refresh_rolling_qualification
from ditto.api_server.datapipeline import DatasetGenerator
from ditto.api_server.deferred_source_review import (
    DEFERRED_MECHANICAL_REASON,
    DEFERRED_REVIEW_KIND,
    INCONCLUSIVE_REASON_CODE,
)
from ditto.api_server.dependencies import (
    get_dataset_generator,
    get_session,
    get_storage_client,
)
from ditto.api_server.endpoints.retrieval import AgentNotFoundError
from ditto.api_server.endpoints.validator import (
    ChainDep,
    _verify_signature,
)
from ditto.api_server.onchain_seed import derive_seed
from ditto.api_server.queue_policy_settings import resolve_queue_policy_settings
from ditto.api_server.screener_policy_activation import (
    EffectiveScreenerPolicy,
    resolve_screener_policy_activation,
)
from ditto.api_server.storage import (
    ObjectDownloadFailedError,
    ObjectNotFoundError,
    ObjectUploadFailedError,
    S3StorageClient,
)
from ditto.api_server.targon_screening import (
    admit_targon_screening_work,
    finalize_targon_screen_and_pin_dataset,
    remote_lane_selected,
)
from ditto.chain import ChainError
from ditto.db.models import (
    Agent,
    AthReview,
    AthReviewAction,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    ProviderOutageCircuit,
    ScreenedImageUpload,
    ScreenerCapacityEvent,
    ScreenerCapacitySnapshot,
    ScreenerHeartbeat,
    ScreenerNode,
    ScreenerNodeBootstrapGrant,
    ScreenerReviewSettingsRevision,
    ScreenerShadowReview,
    ScreeningAttempt,
    ScreeningQuarantine,
    SubmissionImageBuild,
    SubmissionSourceReview,
    TrustedImageBuild,
)
from ditto.db.queries.agents import get_agent_by_id
from ditto.db.queries.artifact_fetch_audit import (
    ENDPOINT_SCREENER_ARTIFACT,
    record_artifact_fetch,
)
from ditto.db.queries.benchmark_rollout import arrival_bench_version
from ditto.db.queries.heartbeats import (
    prune_stale_screener_heartbeats,
    upsert_screener_heartbeat,
)
from ditto.db.queries.provider_outages import (
    lock_provider_work_gate,
    register_provider_probe,
)
from ditto.db.queries.screener_node_settings import (
    resolve_screener_node_channel_settings,
)
from ditto.db.queries.screener_provider_settings import (
    resolve_screener_provider_settings,
)
from ditto.db.queries.screening import (
    POLICY_ONLY_RESCREEN_REASON,
    claim_screening_attempts,
    get_screening_attempt,
    prerequisite_screening_predicates,
    screening_priority_order,
)
from ditto_screening_protocol import (
    ScreenResultOutcome,
    SourceReviewObservationPayload,
    verdict_signing_message,
)

if TYPE_CHECKING:
    from ditto.chain import ChainClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/screener", tags=["screener"])


async def _required_policy(
    session: AsyncSession,
) -> EffectiveScreenerPolicy:
    """The screening-policy version the queue requires right now.

    The deployed build implements every version up to its built-in constant,
    but the required version rises only when a scheduled activation is due
    (``/admin/screener-policy-activation``): miners get equal notice that the
    rules changed, and workers screen under — and stamp outcomes with — the
    required version, not merely the newest one they ship.
    """
    caller_transaction = session.in_transaction()
    policy = await resolve_screener_policy_activation(session)
    if not caller_transaction:
        # The read auto-began a transaction; end it so a caller that follows up
        # with its own explicit ``session.begin()`` is valid. Never roll back a
        # caller-owned transaction.
        await session.rollback()
    return policy


# How long a pre-signed artifact URL stays valid (mirrors the validator's).
_ARTIFACT_URL_TTL = timedelta(minutes=5)
_SCREENED_IMAGE_UPLOAD_TTL = timedelta(minutes=15)
_SCREENED_IMAGE_PART_SIZE = 64 * 1024**2
# One screening attempt: download + Docker build + serve/health + bounded source
# review + image export + multipart upload. Renewable workers carry only a short
# liveness window; accepted signed progress keeps that window ahead of active
# work instead of pre-granting the full worst-case pipeline duration.
# Legacy workers derive a fixed local deadline and cannot consume renewals.
# Keep their old lease until the fleet rolls, while new workers explicitly opt
# into a short lease extended by accepted signed progress heartbeats.
_LEGACY_SCREENING_LEASE_TTL = timedelta(minutes=70)
_RENEWABLE_SCREENING_LEASE_TTL = timedelta(minutes=10)
_SCREENER_PROGRESS_RANK = {
    stage: rank
    for rank, stage in enumerate(
        (
            "preparing",
            "downloading",
            "validating",
            "building",
            "starting",
            "health_check",
            "source_review_0",
            "source_review_10",
            "source_review_20",
            "source_review_30",
            "source_review_40",
            "source_review_50",
            "source_review_60",
            "source_review_70",
            "source_review_80",
            "source_review_90",
            "source_review_100",
            "submitting",
        )
    )
}
_HEARTBEAT_MAX_SKEW_SECONDS = 300
_HEARTBEAT_MAX_BYTES = 4096
_INSTANCE_ID_PATTERN = r"^[a-zA-Z0-9._-]{1,63}$"


def _targon_trusted_builder_enabled(providers: tuple[str, ...]) -> bool:
    # Trusted release-image builds have a dedicated Targon builder and are not
    # miner submission work. GCP-first applies to the decomposed miner lanes;
    # keeping Targon second must not strand new screener release images.
    return "targon" in providers


def _remote_provider(providers: tuple[str, ...]) -> Literal["targon", "hetzner"] | None:
    if providers and providers[0] in {"targon", "hetzner"}:
        return cast(Literal["targon", "hetzner"], providers[0])
    return None


def _platform_owns_miner_rentals(request: Request) -> bool:
    """True when this process runs TargonRentalLoop for miner Kaniko/smoke/L1."""
    return getattr(request.app.state, "targon_rental_loop", None) is not None


async def _release_targon_rental(request: Request, uid: str | None) -> bool:
    loop = getattr(request.app.state, "targon_rental_loop", None)
    if loop is None or not uid:
        return False
    return bool(await loop.release_rental(uid))


def _effective_provider_settings(
    *, environment: str, revision: int, settings: ScreenerProviderSettings
) -> EffectiveScreenerProviderSettings:
    return EffectiveScreenerProviderSettings(
        environment=environment,
        revision=revision,
        settings=settings,
    )


# instance_id stored for pre-v3 (no per-instance identity) heartbeats. Distinct
# from any real GCE instance name, so upgraded workers never collide with it.
_LEGACY_INSTANCE_ID = "legacy"
# Drop heartbeat rows unseen this long so scaled-in fleet instances (each has a
# unique name) don't accumulate dead rows. Far beyond the online/stale windows,
# so a briefly-offline worker is never pruned out from under the dashboard.
_HEARTBEAT_RETENTION = timedelta(days=1)
_CLAIM_FALLBACK_LOCK = asyncio.Lock()

# Policy v1 used the legacy three-field signature. Every policy from v2 onward
# binds its version, including an older worker reporting a failure during a
# future rolling policy upgrade.
_FIRST_VERSIONED_POLICY = 2


def _artifact_key(agent_id: UUID) -> str:
    return f"{agent_id}/agent.tar.gz"


def _screened_image_key(agent_id: UUID, image_upload_id: UUID) -> str:
    """Return an immutable object key unique to one platform-minted upload."""
    return f"{agent_id}/screened-images/{image_upload_id}.tar"


# Agents a verdict may act on. ``screening`` is included for forward-compat with
# a future claim step; the terminal targets are handled separately (idempotency).
_SCREENABLE_STATUSES = (AgentStatus.UPLOADED, AgentStatus.SCREENING)


def _fresh_dataset_seed() -> int:
    """Fallback local-CSPRNG seed, used only when chain derivation is unavailable.

    Cryptographically random so a miner cannot anticipate their dataset; bounded
    to the signed 64-bit range the ``scores.seed`` / ``agents.dataset_seed``
    columns store (``[0, 2**63)``). Mirrors dittobench-api's ``FreshSeed``. The
    preferred path is :func:`_derive_dataset_seed` (verifiable on-chain); a seed
    from here is flagged by null ``dataset_seed_block`` columns so an observer can
    see it was not chain-derived.
    """
    return secrets.randbits(63)


async def _derive_dataset_seed(
    chain: ChainClient, agent_id: UUID
) -> tuple[int, int | None, str | None]:
    """Derive the dataset seed from the latest on-chain block (verifiable).

    Returns ``(seed, block_number, block_hash)``. On a chain-read failure it falls
    back to a local CSPRNG seed with ``(None, None)`` block reference, so a chain
    blip never halts submissions but the (non-verifiable) provenance is explicit.
    The block is read at job-ready, which is causally after the miner committed
    their submission, so they could not have anticipated the seed.
    """
    try:
        block = await chain.get_latest_block()
    except ChainError as e:
        logger.warning(
            "on-chain seed derivation unavailable (%s); falling back to a local "
            "CSPRNG seed for agent %s (block provenance will be null)",
            e,
            agent_id,
        )
        return _fresh_dataset_seed(), None, None
    return derive_seed(block.hash, agent_id), block.number, block.hash


class ScreenerAuthError(Exception):
    """Raised when a screener request fails authentication/authorization.

    Covers a missing/invalid bearer token, a hotkey other than the configured
    dedicated screener, and a verdict whose signature does not verify. The
    envelope handler maps these to HTTP 401 + code 5000.
    """


class AgentNotScreenableError(Exception):
    """Raised when a verdict targets an agent past the screening stage.

    A verdict only applies to a pre-evaluation agent (``uploaded`` /
    ``screening``), or is an idempotent no-op when the agent already holds the
    verdict's target status. Reporting against an already-``evaluating`` /
    ``scored`` / ``live`` / ``banned`` agent (or flipping a decided verdict) is
    a conflict the worker should not retry: HTTP 409 (code 5001).
    """


SessionDep = Annotated[AsyncSession, Depends(get_session)]
StorageDep = Annotated[S3StorageClient, Depends(get_storage_client)]
GeneratorDep = Annotated[DatasetGenerator, Depends(get_dataset_generator)]


async def require_screener(
    request: Request,
    session: SessionDep,
    x_screener_hotkey: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Authenticate a legacy fleet principal or a rotating per-node principal."""
    auth = request.app.state.config.screener_auth
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise ScreenerAuthError("missing screener bearer token")
    presented_token = authorization[len(prefix) :]
    if (
        auth.hotkey is not None
        and auth.api_token is not None
        and x_screener_hotkey == auth.hotkey
        and secrets.compare_digest(presented_token, auth.api_token)
    ):
        request.state.screener_node_id = None
        request.state.screener_provider = "gcp"
        request.state.screener_node_status = "active"
        return auth.hotkey
    if x_screener_hotkey is None:
        raise ScreenerAuthError("X-Screener-Hotkey is not authorized")
    node = await session.scalar(
        select(ScreenerNode).where(ScreenerNode.screener_hotkey == x_screener_hotkey)
    )
    if node is None or node.status == "revoked":
        raise ScreenerAuthError("X-Screener-Hotkey is not authorized")
    expected_hash = hashlib.sha256(presented_token.encode()).hexdigest()
    if not secrets.compare_digest(expected_hash, node.token_hash):
        raise ScreenerAuthError("invalid screener bearer token")
    expires_at = node.token_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if datetime.now(UTC) >= expires_at:
        raise ScreenerAuthError("screener bearer token has expired")
    node_id = node.node_id
    provider = node.provider
    status = node.status
    hotkey = node.screener_hotkey
    # The dependency shares the request-scoped session with the endpoint. End
    # its read-only autobegin so handlers can open their explicit transaction.
    await session.rollback()
    request.state.screener_node_id = node_id
    request.state.screener_provider = provider
    request.state.screener_node_status = status
    return hotkey


ScreenerDep = Annotated[str, Depends(require_screener)]


async def require_screener_controller(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = request.app.state.config.screener_auth.controller_api_token
    if expected is None:
        raise HTTPException(status_code=503, detail="screener controller is disabled")
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing controller bearer token")
    if not secrets.compare_digest(authorization[len(prefix) :], expected):
        raise HTTPException(status_code=401, detail="invalid controller bearer token")


ControllerDep = Annotated[None, Depends(require_screener_controller)]
_CONTROLLER_HEARTBEAT_READY_SECONDS = 180
_NODE_TOKEN_ROTATION_GRACE_SECONDS = 120
_TRUSTED_BUILD_LEASE_TTL = timedelta(minutes=45)
_SUBMISSION_BUILD_LEASE_TTL = timedelta(minutes=50)
_SUBMISSION_BUILD_JOB_TTL = timedelta(minutes=45)
_SUBMISSION_BUILD_URL_TTL = timedelta(minutes=15)
_SUBMISSION_BUILD_MAX_BYTES = 4 * 1024**3
_SOURCE_REVIEW_LEASE_TTL = timedelta(minutes=35)
_SOURCE_REVIEW_JOB_TTL = timedelta(minutes=30)
_SOURCE_REVIEW_URL_TTL = timedelta(minutes=10)
_TRUSTED_SOURCE_REPOSITORY = "https://github.com/ditto-assistant/ditto-subnet.git"
_TRUSTED_RUNTIME_REGISTRY = (
    "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-runtime"
)
_CANDIDATE_RUNTIME_REGISTRY = (
    "us-central1-docker.pkg.dev/ditto-app-dev/ditto-screening-candidates/miner"
)


def _trusted_build_view(row: TrustedImageBuild) -> TrustedImageBuildView:
    def aware(value: datetime | None) -> datetime | None:
        if value is None or value.tzinfo is not None:
            return value
        return value.replace(tzinfo=UTC)

    return TrustedImageBuildView(
        build_id=row.build_id,
        environment=row.environment,
        component=cast(Literal["screener"], row.component),
        source_repository=row.source_repository,
        source_sha=row.source_sha,
        context_path=row.context_path,
        dockerfile_path=row.dockerfile_path,
        destination=row.destination,
        status=cast(TrustedImageBuildStatus, row.status),
        provider=cast(Literal["targon", "gcp"] | None, row.provider),
        provider_resource_id=row.provider_resource_id,
        image_digest=row.image_digest,
        error_code=row.error_code,
        attempt_count=row.attempt_count,
        controller_epoch=row.controller_epoch,
        lease_expires_at=aware(row.lease_expires_at),
        created_by=row.created_by,
        reason=row.reason,
        created_at=cast(datetime, aware(row.created_at)),
        started_at=aware(row.started_at),
        completed_at=aware(row.completed_at),
        updated_at=cast(datetime, aware(row.updated_at)),
    )


async def _submission_build_view(
    row: SubmissionImageBuild,
    *,
    storage: S3StorageClient | None = None,
) -> SubmissionImageBuildResponse:
    download_url: str | None = None
    externally_ready = row.status == "succeeded" and row.runtime_status not in {
        "pending",
        "running",
    }
    if externally_ready and storage is not None:
        download_url = await storage.presigned_get_url(
            key=row.output_key,
            expires_in=int(_SUBMISSION_BUILD_URL_TTL.total_seconds()),
        )
    external_status = (
        "running" if row.status == "succeeded" and not externally_ready else row.status
    )
    return SubmissionImageBuildResponse(
        build_id=row.build_id,
        attempt_id=row.attempt_id,
        status=cast(Any, external_status),
        provider=cast(Literal["targon", "gcp"] | None, row.provider),
        artifact_sha256=row.artifact_sha256,
        image_ref=row.image_ref,
        output_sha256=row.output_sha256 if externally_ready else None,
        output_size_bytes=(row.output_size_bytes if externally_ready else None),
        download_url=download_url,
        error_code=row.error_code,
        runtime_status=cast(Any, row.runtime_status),
        runtime_provider="targon" if row.runtime_image_reference is not None else None,
        runtime_image_reference=row.runtime_image_reference,
        runtime_error_code=row.runtime_error_code,
    )


def _submission_build_token(authorization: str | None) -> str:
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing build job token")
    return authorization[len(prefix) :]


async def _locked_submission_build_for_job(
    session: AsyncSession,
    *,
    build_id: UUID,
    authorization: str | None,
) -> SubmissionImageBuild:
    token = _submission_build_token(authorization)
    row = await session.scalar(
        select(SubmissionImageBuild)
        .where(SubmissionImageBuild.build_id == build_id)
        .with_for_update()
    )
    if row is None or row.job_token_hash is None:
        raise HTTPException(status_code=401, detail="invalid build job token")
    presented_hash = hashlib.sha256(token.encode()).hexdigest()
    if not secrets.compare_digest(presented_hash, row.job_token_hash):
        raise HTTPException(status_code=401, detail="invalid build job token")
    expiry = row.job_token_expires_at
    if expiry is None:
        raise HTTPException(status_code=401, detail="build job token expired")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if datetime.now(UTC) >= expiry:
        raise HTTPException(status_code=401, detail="build job token expired")
    if row.status not in {"leased", "running"}:
        raise HTTPException(status_code=409, detail="build job is not active")
    return row


def _source_review_view(row: SubmissionSourceReview) -> SubmissionSourceReviewResponse:
    observation = (
        SourceReviewObservationPayload.model_validate(row.observation)
        if row.status == "succeeded" and row.observation is not None
        else None
    )
    return SubmissionSourceReviewResponse(
        review_id=row.review_id,
        attempt_id=row.attempt_id,
        status=cast(Any, row.status),
        provider=cast(Literal["targon", "gcp"] | None, row.provider),
        artifact_sha256=row.artifact_sha256,
        observation=observation,
        error_code=row.error_code,
    )


async def _locked_source_review_for_job(
    session: AsyncSession,
    *,
    review_id: UUID,
    authorization: str | None,
) -> SubmissionSourceReview:
    token = _submission_build_token(authorization)
    row = await session.scalar(
        select(SubmissionSourceReview)
        .where(SubmissionSourceReview.review_id == review_id)
        .with_for_update()
    )
    if row is None or row.job_token_hash is None:
        raise HTTPException(status_code=401, detail="invalid source-review job token")
    presented_hash = hashlib.sha256(token.encode()).hexdigest()
    if not secrets.compare_digest(presented_hash, row.job_token_hash):
        raise HTTPException(status_code=401, detail="invalid source-review job token")
    expiry = row.job_token_expires_at
    if expiry is None:
        raise HTTPException(status_code=401, detail="source-review job token expired")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    if datetime.now(UTC) >= expiry:
        raise HTTPException(status_code=401, detail="source-review job token expired")
    if row.status not in {"leased", "running"}:
        raise HTTPException(status_code=409, detail="source-review job is not active")
    return row


def _node_registration_message(payload: ScreenerNodeRegistrationRequest) -> bytes:
    return (
        "ditto-screener-node-register:v1:"
        f"{payload.environment}:{payload.node_id}:{payload.provider}:"
        f"{payload.provider_resource_id}:"
        f"{payload.screener_hotkey}:{payload.timestamp}:{payload.registration_id}"
    ).encode()


def _node_refresh_message(payload: ScreenerNodeRefreshRequest) -> bytes:
    return (
        "ditto-screener-node-refresh:v1:"
        f"{payload.node_id}:{payload.screener_hotkey}:{payload.timestamp}:"
        f"{payload.refresh_id}"
    ).encode()


def _fresh_node_token() -> str:
    return secrets.token_urlsafe(48)


def _idempotent_node_token(*, secret: str, node_id: str, request_id: UUID) -> str:
    """Derive a retry-stable bearer without persisting recoverable plaintext."""
    digest = hmac.new(
        secret.encode(),
        f"ditto-screener-node-token:v1:{node_id}:{request_id}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def _require_controller_epoch(
    session: AsyncSession, *, environment: str, epoch: str, now: datetime
) -> ScreenerCapacitySnapshot:
    """Lock and validate the lease held by the current normal writer."""
    snapshot = await session.scalar(
        select(ScreenerCapacitySnapshot)
        .where(ScreenerCapacitySnapshot.environment == environment)
        .with_for_update()
    )
    if snapshot is None or snapshot.controller_epoch != epoch:
        raise HTTPException(status_code=409, detail="controller epoch is not current")
    lease_expiry = snapshot.controller_lease_expires_at
    if lease_expiry.tzinfo is None:
        lease_expiry = lease_expiry.replace(tzinfo=UTC)
    if now >= lease_expiry:
        raise HTTPException(status_code=409, detail="controller lease has expired")
    return snapshot


@router.post(
    "/controller/bootstrap-grants",
    response_model=ScreenerBootstrapGrantResponse,
)
async def create_bootstrap_grant(
    payload: ScreenerBootstrapGrantRequest,
    _controller: ControllerDep,
    session: SessionDep,
    request: Request,
) -> ScreenerBootstrapGrantResponse:
    """Mint one node-bound, single-use registration capability."""
    now = datetime.now(UTC)
    expires_at = now + timedelta(
        seconds=request.app.state.config.screener_auth.bootstrap_ttl_seconds
    )
    token = _fresh_node_token()
    grant = ScreenerNodeBootstrapGrant(
        grant_id=uuid4(),
        environment=payload.environment,
        node_id=payload.node_id,
        provider=payload.provider,
        provider_resource_id=payload.provider_resource_id,
        controller_epoch=payload.controller_epoch,
        image_reference=payload.image_reference,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=expires_at,
    )
    async with session.begin():
        await _require_controller_epoch(
            session,
            environment=payload.environment,
            epoch=payload.controller_epoch,
            now=now,
        )
        session.add(grant)
    return ScreenerBootstrapGrantResponse(
        grant_id=grant.grant_id,
        registration_token=token,
        expires_at=expires_at,
    )


@router.post("/nodes/register", response_model=ScreenerNodeCredentialResponse)
async def register_screener_node(
    payload: ScreenerNodeRegistrationRequest,
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> ScreenerNodeCredentialResponse:
    """Exchange a one-time capability and hotkey proof for short-lived authority."""
    prefix = "Bootstrap "
    if authorization is None or not authorization.startswith(prefix):
        raise ScreenerAuthError("missing screener bootstrap token")
    now = datetime.now(UTC)
    if abs(int(now.timestamp()) - payload.timestamp) > _HEARTBEAT_MAX_SKEW_SECONDS:
        raise ScreenerAuthError("node registration timestamp is stale")
    if not _verify_signature(
        payload.screener_hotkey,
        _node_registration_message(payload),
        payload.signature,
    ):
        raise ScreenerAuthError("node registration signature verification failed")
    token_hash = hashlib.sha256(authorization[len(prefix) :].encode()).hexdigest()
    controller_secret = request.app.state.config.screener_auth.controller_api_token
    if controller_secret is None:
        raise HTTPException(status_code=503, detail="screener controller is disabled")
    api_token = _idempotent_node_token(
        secret=controller_secret,
        node_id=payload.node_id,
        request_id=payload.registration_id,
    )
    expires_at = now + timedelta(
        seconds=request.app.state.config.screener_auth.node_token_ttl_seconds
    )
    async with session.begin():
        grant = await session.scalar(
            select(ScreenerNodeBootstrapGrant)
            .where(ScreenerNodeBootstrapGrant.token_hash == token_hash)
            .with_for_update()
        )
        if grant is None:
            raise ScreenerAuthError("invalid screener bootstrap token")
        if grant.consumed_at is not None:
            existing = await session.get(ScreenerNode, payload.node_id)
            if (
                grant.registration_id != payload.registration_id
                or existing is None
                or existing.environment != payload.environment
                or existing.provider != payload.provider
                or existing.provider_resource_id != payload.provider_resource_id
                or existing.screener_hotkey != payload.screener_hotkey
                or not secrets.compare_digest(
                    existing.token_hash,
                    hashlib.sha256(api_token.encode()).hexdigest(),
                )
            ):
                raise ScreenerAuthError("invalid or consumed screener bootstrap token")
            expires_at = existing.token_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if now >= expires_at:
                raise ScreenerAuthError("registered screener bearer token has expired")
        else:
            grant_expiry = grant.expires_at
            if grant_expiry.tzinfo is None:
                grant_expiry = grant_expiry.replace(tzinfo=UTC)
            if now >= grant_expiry:
                raise ScreenerAuthError("screener bootstrap token has expired")
            if (
                grant.environment != payload.environment
                or grant.node_id != payload.node_id
                or grant.provider != payload.provider
                or grant.provider_resource_id != payload.provider_resource_id
            ):
                raise ScreenerAuthError("screener bootstrap identity mismatch")
            await _require_controller_epoch(
                session,
                environment=grant.environment,
                epoch=grant.controller_epoch,
                now=now,
            )
            existing = await session.get(ScreenerNode, payload.node_id)
            hotkey_owner = await session.scalar(
                select(ScreenerNode).where(
                    ScreenerNode.screener_hotkey == payload.screener_hotkey
                )
            )
            if existing is not None or hotkey_owner is not None:
                raise ScreenerAuthError("screener node identity is already registered")
            grant.consumed_at = now
            grant.registration_id = payload.registration_id
            session.add(
                ScreenerNode(
                    environment=grant.environment,
                    node_id=payload.node_id,
                    provider=payload.provider,
                    provider_resource_id=payload.provider_resource_id,
                    screener_hotkey=payload.screener_hotkey,
                    token_hash=hashlib.sha256(api_token.encode()).hexdigest(),
                    token_expires_at=expires_at,
                    status="active",
                    capacity=1,
                    image_reference=grant.image_reference,
                    registered_at=now,
                    rotated_at=now,
                )
            )
    return ScreenerNodeCredentialResponse(
        environment=payload.environment,
        node_id=payload.node_id,
        screener_hotkey=payload.screener_hotkey,
        api_token=api_token,
        expires_at=expires_at,
    )


@router.post("/nodes/refresh", response_model=ScreenerNodeCredentialResponse)
async def refresh_screener_node(
    payload: ScreenerNodeRefreshRequest,
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> ScreenerNodeCredentialResponse:
    """Rotate a node bearer while it still owns valid signed authority."""
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise ScreenerAuthError("missing screener bearer token")
    now = datetime.now(UTC)
    if abs(int(now.timestamp()) - payload.timestamp) > _HEARTBEAT_MAX_SKEW_SECONDS:
        raise ScreenerAuthError("node refresh timestamp is stale")
    if not _verify_signature(
        payload.screener_hotkey,
        _node_refresh_message(payload),
        payload.signature,
    ):
        raise ScreenerAuthError("node refresh signature verification failed")
    presented_hash = hashlib.sha256(authorization[len(prefix) :].encode()).hexdigest()
    controller_secret = request.app.state.config.screener_auth.controller_api_token
    if controller_secret is None:
        raise HTTPException(status_code=503, detail="screener controller is disabled")
    api_token = _idempotent_node_token(
        secret=controller_secret,
        node_id=payload.node_id,
        request_id=payload.refresh_id,
    )
    expires_at = now + timedelta(
        seconds=request.app.state.config.screener_auth.node_token_ttl_seconds
    )
    async with session.begin():
        node = await session.scalar(
            select(ScreenerNode)
            .where(ScreenerNode.node_id == payload.node_id)
            .with_for_update()
        )
        if node is None or node.screener_hotkey != payload.screener_hotkey:
            raise ScreenerAuthError("invalid screener node authority")
        current_matches = secrets.compare_digest(node.token_hash, presented_hash)
        previous_expiry = node.previous_token_expires_at
        if previous_expiry is not None and previous_expiry.tzinfo is None:
            previous_expiry = previous_expiry.replace(tzinfo=UTC)
        previous_matches = bool(
            node.previous_token_hash is not None
            and previous_expiry is not None
            and now < previous_expiry
            and secrets.compare_digest(node.previous_token_hash, presented_hash)
        )
        same_request = node.last_refresh_id == payload.refresh_id
        if node.status == "revoked" or not (
            current_matches or (same_request and previous_matches)
        ):
            raise ScreenerAuthError("invalid screener node authority")
        prior_expiry = node.token_expires_at
        if prior_expiry.tzinfo is None:
            prior_expiry = prior_expiry.replace(tzinfo=UTC)
        if now >= prior_expiry:
            raise ScreenerAuthError("screener bearer token has expired")
        derived_hash = hashlib.sha256(api_token.encode()).hexdigest()
        if same_request:
            if not secrets.compare_digest(node.token_hash, derived_hash):
                raise ScreenerAuthError("refresh request identity is inconsistent")
            expires_at = node.token_expires_at
        else:
            # Keep the prior bearer usable only for an identical request. This
            # closes the response-loss window without granting general overlap.
            node.previous_token_hash = node.token_hash
            node.previous_token_expires_at = now + timedelta(
                seconds=_NODE_TOKEN_ROTATION_GRACE_SECONDS
            )
            node.token_hash = derived_hash
            node.token_expires_at = expires_at
            node.last_refresh_id = payload.refresh_id
            node.rotated_at = now
    return ScreenerNodeCredentialResponse(
        environment=node.environment,
        node_id=payload.node_id,
        screener_hotkey=payload.screener_hotkey,
        api_token=api_token,
        expires_at=expires_at,
    )


@router.put(
    "/controller/capacity",
    response_model=ScreenerCapacitySnapshotResponse,
)
async def update_screener_capacity(
    payload: ScreenerCapacitySnapshotRequest,
    _controller: ControllerDep,
    session: SessionDep,
    request: Request,
) -> ScreenerCapacitySnapshotResponse:
    """Persist the controller heartbeat and a bounded append-only event batch."""
    now = datetime.now(UTC)
    values = payload.model_dump(mode="python", exclude={"events"})
    values["controller_heartbeat_at"] = now
    lease_expires_at = now + timedelta(
        seconds=request.app.state.config.screener_auth.controller_lease_seconds
    )
    values["controller_lease_expires_at"] = lease_expires_at
    values["updated_at"] = now
    async with session.begin():
        row = await session.scalar(
            select(ScreenerCapacitySnapshot)
            .where(ScreenerCapacitySnapshot.environment == payload.environment)
            .with_for_update()
        )
        if row is None:
            row = ScreenerCapacitySnapshot(**values)
            session.add(row)
        else:
            current_expiry = row.controller_lease_expires_at
            if current_expiry.tzinfo is None:
                current_expiry = current_expiry.replace(tzinfo=UTC)
            if (
                row.controller_epoch != payload.controller_epoch
                and now < current_expiry
            ):
                raise HTTPException(
                    status_code=409, detail="another controller holds the lease"
                )
            for key, value in values.items():
                setattr(row, key, value)
        for event in payload.events[:50]:
            session.add(
                ScreenerCapacityEvent(
                    event_id=uuid4(),
                    environment=payload.environment,
                    event_type=event.event_type,
                    provider=event.provider,
                    node_id=event.node_id,
                    detail=event.detail,
                    controller_epoch=payload.controller_epoch,
                    created_at=now,
                )
            )
    return ScreenerCapacitySnapshotResponse(
        **payload.model_dump(mode="python"),
        controller_heartbeat_at=now,
        controller_lease_expires_at=lease_expires_at,
        updated_at=now,
    )


@router.post("/controller/fence", response_model=None, status_code=204)
async def fence_screener_controller(
    payload: ScreenerControllerFenceRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    """Recheck writer ownership without extending its watchdog lease."""
    async with session.begin():
        await _require_controller_epoch(
            session,
            environment=payload.environment,
            epoch=payload.controller_epoch,
            now=datetime.now(UTC),
        )


@router.post("/controller/release", response_model=None, status_code=204)
async def release_screener_controller(
    payload: ScreenerControllerFenceRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    """Relinquish the exact current writer lease during a graceful deploy."""
    now = datetime.now(UTC)
    async with session.begin():
        snapshot = await _require_controller_epoch(
            session,
            environment=payload.environment,
            epoch=payload.controller_epoch,
            now=now,
        )
        snapshot.controller_lease_expires_at = now


@router.get(
    "/controller/provider-settings",
    response_model=EffectiveScreenerProviderSettings,
)
async def get_controller_provider_settings(
    _controller: ControllerDep,
    session: SessionDep,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod",
) -> EffectiveScreenerProviderSettings:
    """Return the routing revision every provider mutator must obey."""
    revision, settings = await resolve_screener_provider_settings(
        session, environment=environment
    )
    return _effective_provider_settings(
        environment=environment, revision=revision, settings=settings
    )


@router.post(
    "/controller/trusted-image-builds",
    response_model=TrustedImageBuildView,
)
async def queue_release_image_build(
    payload: TrustedImageBuildCreateRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> TrustedImageBuildView:
    """Idempotently queue the fixed release image contract for an exact SHA."""
    async with session.begin():
        values = {
            "build_id": uuid4(),
            "environment": "prod",
            "component": "screener",
            "source_repository": _TRUSTED_SOURCE_REPOSITORY,
            "source_sha": payload.source_sha,
            "context_path": ".",
            "dockerfile_path": "workers/screener/Dockerfile",
            "destination": (
                f"{_TRUSTED_RUNTIME_REGISTRY}/screener:sha-{payload.source_sha}"
            ),
            "status": "queued",
            "provider": None,
            "controller_epoch": None,
            "error_code": None,
            "completed_at": None,
            "created_by": f"github-release:{payload.source_sha}",
            "reason": payload.reason,
        }
        await session.execute(
            pg_insert(TrustedImageBuild)
            .values(**values)
            .on_conflict_do_nothing(constraint="trusted_image_builds_source_key")
        )
        row = await session.scalar(
            select(TrustedImageBuild).where(
                TrustedImageBuild.environment == "prod",
                TrustedImageBuild.component == payload.component,
                TrustedImageBuild.source_sha == payload.source_sha,
            )
        )
        if row is None:  # pragma: no cover - INSERT/SELECT share one transaction
            raise HTTPException(
                status_code=503, detail="trusted build queue unavailable"
            )
    return _trusted_build_view(row)


@router.get(
    "/controller/trusted-image-builds/latest",
    response_model=TrustedImageBuildView,
)
async def get_latest_release_image_build(
    _controller: ControllerDep,
    session: SessionDep,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod",
) -> TrustedImageBuildView:
    """Return the newest successfully published immutable screener image."""
    row = await session.scalar(
        select(TrustedImageBuild)
        .where(
            TrustedImageBuild.environment == environment,
            TrustedImageBuild.component == "screener",
            TrustedImageBuild.status == "succeeded",
            TrustedImageBuild.image_digest.is_not(None),
        )
        .order_by(
            TrustedImageBuild.completed_at.desc(), TrustedImageBuild.created_at.desc()
        )
        .limit(1)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no successful screener image")
    return _trusted_build_view(row)


@router.get(
    "/controller/trusted-image-builds/{build_id}",
    response_model=TrustedImageBuildView,
)
async def get_release_image_build(
    build_id: UUID,
    _controller: ControllerDep,
    session: SessionDep,
) -> TrustedImageBuildView:
    row = await session.get(TrustedImageBuild, build_id)
    if row is None:
        raise HTTPException(status_code=404, detail="trusted image build not found")
    return _trusted_build_view(row)


@router.post(
    "/controller/trusted-image-builds/claim",
    response_model=TrustedImageBuildClaimResponse,
)
async def claim_trusted_image_build(
    payload: TrustedImageBuildClaimRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> TrustedImageBuildClaimResponse:
    """Lease one allowlisted trusted build under the current controller epoch."""
    now = datetime.now(UTC)
    async with session.begin():
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment=payload.environment
        )
        if not _targon_trusted_builder_enabled(
            provider_settings.build_provider_priority
        ):
            await session.execute(
                update(TrustedImageBuild)
                .where(
                    TrustedImageBuild.environment == payload.environment,
                    TrustedImageBuild.status == "queued",
                )
                .values(
                    status="fallback_required",
                    provider="targon",
                    controller_epoch=payload.controller_epoch,
                    error_code="TARGON_BUILD_DISABLED_BY_POLICY",
                    completed_at=now,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            return TrustedImageBuildClaimResponse(build=None)
        # A single abandoned lease is terminal. A new claim requires an
        # explicit operator action that creates new work.
        await session.execute(
            update(TrustedImageBuild)
            .where(
                TrustedImageBuild.environment == payload.environment,
                TrustedImageBuild.status.in_(("leased", "running")),
                TrustedImageBuild.lease_expires_at < now,
                TrustedImageBuild.attempt_count >= 1,
            )
            .values(
                status="fallback_required",
                provider="targon",
                error_code="TARGON_BUILD_LEASE_EXHAUSTED",
                completed_at=now,
                lease_expires_at=None,
                updated_at=now,
            )
        )
        row = await session.scalar(
            select(TrustedImageBuild)
            .where(
                TrustedImageBuild.environment == payload.environment,
                TrustedImageBuild.status == "queued",
            )
            .order_by(TrustedImageBuild.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return TrustedImageBuildClaimResponse(build=None)
        row.status = "leased"
        row.controller_epoch = payload.controller_epoch
        row.lease_expires_at = now + _TRUSTED_BUILD_LEASE_TTL
        row.attempt_count += 1
        row.updated_at = now
    return TrustedImageBuildClaimResponse(build=_trusted_build_view(row))


@router.put(
    "/controller/trusted-image-builds/{build_id}",
    response_model=TrustedImageBuildView,
)
async def update_trusted_image_build(
    build_id: UUID,
    payload: TrustedImageBuildUpdateRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> TrustedImageBuildView:
    """Record redacted provider progress and the immutable output digest."""
    now = datetime.now(UTC)
    async with session.begin():
        row = await session.scalar(
            select(TrustedImageBuild)
            .where(TrustedImageBuild.build_id == build_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="trusted image build not found")
        if row.controller_epoch != payload.controller_epoch:
            raise HTTPException(status_code=409, detail="build lease epoch is stale")
        if row.status in {"succeeded", "failed", "canceled"}:
            raise HTTPException(status_code=409, detail="trusted build is terminal")
        if row.status == "fallback_required":
            if payload.status != "succeeded" or payload.provider != "gcp":
                raise HTTPException(
                    status_code=409,
                    detail="fallback build accepts only a successful GCP result",
                )
        elif payload.provider != "targon":
            raise HTTPException(
                status_code=422,
                detail="GCP may report only an explicitly requested fallback",
            )
        if payload.status == "succeeded" and payload.image_digest is None:
            raise HTTPException(
                status_code=422, detail="successful build requires digest"
            )
        if payload.status != "succeeded" and payload.image_digest is not None:
            raise HTTPException(
                status_code=422, detail="only a successful build carries digest"
            )
        if (
            payload.status in {"failed", "fallback_required"}
            and payload.error_code is None
        ):
            raise HTTPException(
                status_code=422, detail="failed build requires error code"
            )
        row.status = payload.status
        row.provider = payload.provider
        row.provider_resource_id = payload.provider_resource_id
        row.image_digest = payload.image_digest
        row.error_code = payload.error_code
        row.started_at = row.started_at or now
        row.updated_at = now
        if payload.status in {"succeeded", "failed", "fallback_required"}:
            row.completed_at = now
            row.lease_expires_at = None
    return _trusted_build_view(row)


@router.post(
    "/controller/trusted-image-builds/{build_id}/cleanup-required",
    response_model=None,
    status_code=204,
)
async def record_trusted_image_build_cleanup(
    build_id: UUID,
    payload: SubmissionImageBuildCleanupRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    """Keep trusted Kaniko deletion failures visible after zero-replica suspension."""
    async with session.begin():
        row = await session.get(TrustedImageBuild, build_id)
        if row is None:
            raise HTTPException(status_code=404, detail="trusted image build not found")
        if (
            row.environment != payload.environment
            or row.controller_epoch != payload.controller_epoch
            or row.provider_resource_id != payload.provider_resource_id
        ):
            raise HTTPException(
                status_code=409, detail="trusted build cleanup lease is stale"
            )
        session.add(
            ScreenerCapacityEvent(
                event_id=uuid4(),
                environment=payload.environment,
                event_type="provider_cleanup_required",
                provider="targon",
                node_id=None,
                detail=(
                    "A suspended zero-replica trusted Kaniko rental requires "
                    "provider deletion retry."
                ),
                controller_epoch=payload.controller_epoch,
                created_at=datetime.now(UTC),
            )
        )


@router.post(
    "/agent/{agent_id}/submission-image-builds",
    response_model=SubmissionImageBuildResponse,
)
async def queue_submission_image_build(
    agent_id: UUID,
    payload: SubmissionImageBuildRequest,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
) -> SubmissionImageBuildResponse:
    """Queue a provider build only after the owning screener validated source."""
    now = datetime.now(UTC)
    async with session.begin():
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment="prod"
        )
        build_provider = _remote_provider(provider_settings.build_provider_priority)
        runtime_provider = _remote_provider(provider_settings.runtime_provider_priority)
        agent = await get_agent_by_id(session, agent_id=agent_id, for_update=True)
        if agent is None:
            raise AgentNotFoundError(f"no agent with id={agent_id}")
        attempt = await get_screening_attempt(
            session, attempt_id=payload.attempt_id, for_update=True
        )
        deadline = attempt.deadline if attempt is not None else now
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if (
            attempt is None
            or attempt.agent_id != agent_id
            or attempt.screener_hotkey != screener_hotkey
            or attempt.status != "running"
            or now >= deadline
        ):
            raise AgentNotScreenableError(
                "remote build does not match an active screening attempt"
            )
        build_id = uuid4()
        values = {
            "build_id": build_id,
            "agent_id": agent_id,
            "attempt_id": payload.attempt_id,
            "environment": "prod",
            "artifact_sha256": agent.sha256.lower(),
            "image_ref": f"ditto-screen/{agent_id}-{payload.attempt_id}:latest",
            "output_key": f"remote-builds/{build_id}/image.tar",
            "status": "queued" if build_provider is not None else "fallback_required",
            "provider": None if build_provider is not None else "gcp",
            "error_code": (
                None
                if build_provider is not None
                else "TARGON_SUBMISSION_BUILD_DISABLED_BY_POLICY"
            ),
            "completed_at": None if build_provider is not None else now,
            "runtime_status": (
                "pending"
                if build_provider is not None and runtime_provider is not None
                else "skipped"
            ),
            "runtime_error_code": (
                None
                if build_provider is not None and runtime_provider is not None
                else (
                    "TARGON_RUNTIME_DISABLED_BY_POLICY"
                    if runtime_provider is None
                    else "TARGON_RUNTIME_SKIPPED_BUILD_UNAVAILABLE"
                )
            ),
        }
        await session.execute(
            pg_insert(SubmissionImageBuild)
            .values(**values)
            .on_conflict_do_nothing(constraint="submission_image_builds_attempt_key")
        )
        row = await session.scalar(
            select(SubmissionImageBuild).where(
                SubmissionImageBuild.attempt_id == payload.attempt_id
            )
        )
        if row is None:  # pragma: no cover - INSERT/SELECT share one transaction
            raise HTTPException(
                status_code=503, detail="remote build queue unavailable"
            )
    return await _submission_build_view(row)


@router.get(
    "/agent/{agent_id}/submission-image-builds/{build_id}",
    response_model=SubmissionImageBuildResponse,
)
async def get_submission_image_build(
    agent_id: UUID,
    build_id: UUID,
    attempt_id: UUID,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
) -> SubmissionImageBuildResponse:
    row = await session.get(SubmissionImageBuild, build_id)
    attempt = await session.get(ScreeningAttempt, attempt_id)
    if row is None or row.agent_id != agent_id or row.attempt_id != attempt_id:
        raise HTTPException(status_code=404, detail="submission image build not found")
    if attempt is None or attempt.screener_hotkey != screener_hotkey:
        raise AgentNotScreenableError("remote build does not match screener lease")
    return await _submission_build_view(row, storage=storage)


@router.delete(
    "/agent/{agent_id}/submission-image-builds/{build_id}",
    response_model=None,
    status_code=204,
)
async def consume_submission_image_build(
    agent_id: UUID,
    build_id: UUID,
    attempt_id: UUID,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
) -> None:
    """Delete the temporary remote archive after the GCE daemon imported it."""
    async with session.begin():
        row = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.build_id == build_id)
            .with_for_update()
        )
        attempt = await session.get(ScreeningAttempt, attempt_id)
        if row is None or row.agent_id != agent_id or row.attempt_id != attempt_id:
            raise HTTPException(
                status_code=404, detail="submission image build not found"
            )
        if attempt is None or attempt.screener_hotkey != screener_hotkey:
            raise AgentNotScreenableError("remote build does not match screener lease")
        active = row.status in {"queued", "leased", "running"}
        if row.status not in {
            "queued",
            "leased",
            "running",
            "succeeded",
            "consumed",
        }:
            raise AgentNotScreenableError("remote build is not discardable")
        output_key = row.output_key
        now = datetime.now(UTC)
        # Keep a pending runtime archive too. The builder only smokes after
        # the Kaniko rental returns, so GCE often consumes first and used to
        # delete the archive before dest-auth could claim it.
        runtime_in_flight = row.runtime_status in {"pending", "running"}
        if row.runtime_status == "pending" and active:
            row.runtime_status = "skipped"
            row.runtime_error_code = "TARGON_RUNTIME_SKIPPED_BUILD_CANCELED"
            row.runtime_completed_at = now
            row.updated_at = now
            runtime_in_flight = False
        if active:
            row.status = "canceled"
            row.completed_at = now
            row.lease_expires_at = None
            row.job_token_hash = None
            row.job_token_expires_at = None
            row.updated_at = now
    # A claimed runtime smoke still needs the verified archive. Deleting it
    # here makes the in-flight download 403 and reports a fake provider error.
    if not runtime_in_flight and await storage.object_exists(key=output_key):
        await storage.delete_object(key=output_key)
    async with session.begin():
        stored = await session.get(SubmissionImageBuild, build_id, with_for_update=True)
        if stored is not None and stored.status in {"succeeded", "consumed"}:
            stored.status = "consumed"
            stored.consumed_at = datetime.now(UTC)
            stored.updated_at = datetime.now(UTC)


@router.post(
    "/controller/submission-image-builds/claim",
    response_model=SubmissionImageBuildClaimResponse,
)
async def claim_submission_image_build(
    payload: TrustedImageBuildClaimRequest,
    request: Request,
    _controller: ControllerDep,
    session: SessionDep,
    storage: StorageDep,
) -> SubmissionImageBuildClaimResponse:
    """Lease one miner build and mint only its short-lived job capability."""
    if _platform_owns_miner_rentals(request):
        return SubmissionImageBuildClaimResponse(build=None)
    now = datetime.now(UTC)
    async with session.begin():
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment=payload.environment
        )
        attester = request.app.state.config.screener_auth.hotkey
        if attester is not None and remote_lane_selected(
            provider_settings.build_provider_priority
        ):
            await admit_targon_screening_work(
                session,
                screener_hotkey=attester,
                environment=payload.environment,
                now=now,
                archive_exists=storage.object_exists,
            )
        if not remote_lane_selected(provider_settings.build_provider_priority):
            if _remote_provider(provider_settings.build_provider_priority) == "hetzner":
                return SubmissionImageBuildClaimResponse(build=None)
            await session.execute(
                update(SubmissionImageBuild)
                .where(
                    SubmissionImageBuild.environment == payload.environment,
                    SubmissionImageBuild.status == "queued",
                )
                .values(
                    status="fallback_required",
                    provider="targon",
                    error_code="TARGON_SUBMISSION_BUILD_DISABLED_BY_POLICY",
                    runtime_status="skipped",
                    runtime_error_code="TARGON_RUNTIME_SKIPPED_BUILD_UNAVAILABLE",
                    runtime_completed_at=now,
                    completed_at=now,
                    lease_expires_at=None,
                    job_token_hash=None,
                    job_token_expires_at=None,
                    updated_at=now,
                )
            )
            return SubmissionImageBuildClaimResponse(build=None)
        await session.execute(
            update(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.environment == payload.environment,
                SubmissionImageBuild.status.in_(("leased", "running")),
                SubmissionImageBuild.lease_expires_at < now,
                SubmissionImageBuild.attempt_count >= 1,
            )
            .values(
                status="fallback_required",
                error_code="TARGON_SUBMISSION_BUILD_LEASE_EXHAUSTED",
                runtime_status="skipped",
                runtime_error_code="TARGON_RUNTIME_SKIPPED_BUILD_UNAVAILABLE",
                runtime_completed_at=now,
                completed_at=now,
                lease_expires_at=None,
                job_token_hash=None,
                job_token_expires_at=None,
                updated_at=now,
            )
        )
        await session.execute(
            update(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.environment == payload.environment,
                SubmissionImageBuild.status == "queued",
                ~exists().where(
                    (ScreeningAttempt.attempt_id == SubmissionImageBuild.attempt_id)
                    & (ScreeningAttempt.status == "running")
                    & (ScreeningAttempt.deadline > now)
                ),
            )
            .values(
                status="canceled",
                runtime_status="skipped",
                runtime_error_code="TARGON_RUNTIME_SKIPPED_BUILD_CANCELED",
                runtime_completed_at=now,
                completed_at=now,
                updated_at=now,
            )
        )
        row = await session.scalar(
            select(SubmissionImageBuild)
            .join(
                ScreeningAttempt,
                ScreeningAttempt.attempt_id == SubmissionImageBuild.attempt_id,
            )
            .where(
                SubmissionImageBuild.environment == payload.environment,
                ScreeningAttempt.status == "running",
                ScreeningAttempt.deadline > now,
                SubmissionImageBuild.status == "queued",
            )
            .order_by(SubmissionImageBuild.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return SubmissionImageBuildClaimResponse(build=None)
        token = _fresh_node_token()
        token_expires_at = now + _SUBMISSION_BUILD_JOB_TTL
        row.status = "leased"
        row.provider = "targon"
        row.controller_epoch = payload.controller_epoch
        row.lease_expires_at = now + _SUBMISSION_BUILD_LEASE_TTL
        row.attempt_count += 1
        row.job_token_hash = hashlib.sha256(token.encode()).hexdigest()
        row.job_token_expires_at = token_expires_at
        row.updated_at = now
    return SubmissionImageBuildClaimResponse(
        build=SubmissionImageBuildClaimView(
            build_id=row.build_id,
            agent_id=row.agent_id,
            attempt_id=row.attempt_id,
            artifact_sha256=row.artifact_sha256,
            image_ref=row.image_ref,
            job_token=token,
            job_token_expires_at=token_expires_at,
        )
    )


@router.put(
    "/controller/submission-image-builds/{build_id}",
    response_model=None,
    status_code=204,
)
async def update_submission_image_build(
    build_id: UUID,
    payload: SubmissionImageBuildControllerUpdateRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    now = datetime.now(UTC)
    async with session.begin():
        row = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.build_id == build_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail="submission image build not found"
            )
        if row.controller_epoch != payload.controller_epoch:
            raise HTTPException(status_code=409, detail="build lease epoch is stale")
        if row.status not in {"leased", "running"}:
            raise HTTPException(status_code=409, detail="submission build is terminal")
        row.status = payload.status
        row.provider = "targon"
        row.provider_resource_id = payload.provider_resource_id
        row.error_code = payload.error_code
        row.started_at = row.started_at or now
        row.updated_at = now
        if payload.status == "fallback_required":
            if row.runtime_status in {"pending", "running"}:
                row.runtime_status = "skipped"
                row.runtime_error_code = "TARGON_RUNTIME_SKIPPED_BUILD_UNAVAILABLE"
                row.runtime_completed_at = now
            row.completed_at = now
            row.lease_expires_at = None
            row.job_token_hash = None
            row.job_token_expires_at = None


@router.get(
    "/controller/submission-image-builds/{build_id}",
    response_model=SubmissionImageBuildControllerStatusResponse,
)
async def get_controller_submission_image_build(
    build_id: UUID,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")],
    controller_epoch: Annotated[str, Query(pattern=r"^[A-Za-z0-9._:@/-]{8,200}$")],
    _controller: ControllerDep,
    session: SessionDep,
) -> SubmissionImageBuildControllerStatusResponse:
    row = await session.get(SubmissionImageBuild, build_id)
    if row is None:
        raise HTTPException(status_code=404, detail="submission image build not found")
    if row.environment != environment or row.controller_epoch != controller_epoch:
        raise HTTPException(status_code=409, detail="build lease epoch is stale")
    return SubmissionImageBuildControllerStatusResponse(
        build_id=row.build_id,
        status=cast(Any, row.status),
    )


@router.post(
    "/controller/submission-runtime-smokes/claim",
    response_model=SubmissionRuntimeArtifactClaimResponse,
)
async def claim_submission_runtime_smoke(
    payload: TrustedImageBuildClaimRequest,
    request: Request,
    _controller: ControllerDep,
    session: SessionDep,
    storage: StorageDep,
) -> SubmissionRuntimeArtifactClaimResponse:
    if _platform_owns_miner_rentals(request):
        return SubmissionRuntimeArtifactClaimResponse(artifact=None)
    now = datetime.now(UTC)
    async with session.begin():
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment=payload.environment
        )
        if not remote_lane_selected(provider_settings.runtime_provider_priority):
            if (
                _remote_provider(provider_settings.runtime_provider_priority)
                == "hetzner"
            ):
                return SubmissionRuntimeArtifactClaimResponse(artifact=None)
            await session.execute(
                update(SubmissionImageBuild)
                .where(
                    SubmissionImageBuild.environment == payload.environment,
                    SubmissionImageBuild.runtime_status.in_(("pending", "running")),
                )
                .values(
                    runtime_status="skipped",
                    runtime_error_code="TARGON_RUNTIME_DISABLED_BY_POLICY",
                    runtime_completed_at=now,
                    updated_at=now,
                )
            )
            return SubmissionRuntimeArtifactClaimResponse(artifact=None)
        row = await session.scalar(
            select(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.environment == payload.environment,
                SubmissionImageBuild.status.in_(("succeeded", "consumed")),
                or_(
                    SubmissionImageBuild.runtime_status == "pending",
                    (
                        (SubmissionImageBuild.runtime_status == "running")
                        & (
                            SubmissionImageBuild.updated_at
                            < now - timedelta(minutes=20)
                        )
                    ),
                ),
                SubmissionImageBuild.output_sha256.is_not(None),
                SubmissionImageBuild.output_size_bytes.is_not(None),
            )
            .order_by(SubmissionImageBuild.completed_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return SubmissionRuntimeArtifactClaimResponse(artifact=None)
        row.runtime_status = "running"
        row.controller_epoch = payload.controller_epoch
        row.updated_at = now
        output_sha256 = cast(str, row.output_sha256)
        output_size_bytes = cast(int, row.output_size_bytes)
        output_key = row.output_key
        build_id = row.build_id
    url = await storage.presigned_get_url(
        key=output_key,
        expires_in=int(_SUBMISSION_BUILD_URL_TTL.total_seconds()),
    )
    return SubmissionRuntimeArtifactClaimResponse(
        artifact=SubmissionRuntimeArtifactResponse(
            build_id=build_id,
            archive_url_b64=base64.b64encode(url.encode()).decode(),
            output_sha256=output_sha256,
            output_size_bytes=output_size_bytes,
            destination=f"{_CANDIDATE_RUNTIME_REGISTRY}:build-{build_id.hex}",
        )
    )


@router.post(
    "/controller/submission-image-builds/{build_id}/runtime-result",
    response_model=None,
    status_code=204,
)
async def complete_submission_runtime_smoke(
    build_id: UUID,
    payload: SubmissionRuntimeResultRequest,
    request: Request,
    _controller: ControllerDep,
    session: SessionDep,
    storage: StorageDep,
    generator: GeneratorDep,
    chain: ChainDep,
) -> None:
    now = datetime.now(UTC)
    output_key: str | None = None
    async with session.begin():
        row = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.build_id == build_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail="submission image build not found"
            )
        if (
            row.environment != payload.environment
            or row.controller_epoch != payload.controller_epoch
        ):
            raise HTTPException(status_code=409, detail="runtime smoke fence is stale")
        if row.status not in {"succeeded", "consumed"} or row.runtime_status not in {
            "pending",
            "running",
        }:
            raise HTTPException(status_code=409, detail="runtime smoke is terminal")
        row.runtime_status = payload.status
        row.runtime_provider_resource_id = payload.provider_resource_id
        row.runtime_image_reference = payload.image_reference
        row.runtime_error_code = payload.error_code
        row.updated_at = now
        if payload.status in {"succeeded", "fallback_required"}:
            row.runtime_completed_at = now
            if row.consumed_at is not None:
                output_key = row.output_key
        if payload.status == "succeeded":
            _, provider_settings = await resolve_screener_provider_settings(
                session, environment=row.environment
            )
            attempt = await session.get(
                ScreeningAttempt, row.attempt_id, with_for_update=True
            )
            deadline = attempt.deadline if attempt is not None else now
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            if (
                _remote_provider(provider_settings.source_review_provider_priority)
                is not None
                and attempt is not None
                and not attempt.build_only
                and attempt.status == "running"
                and now < deadline
            ):
                await session.execute(
                    pg_insert(SubmissionSourceReview)
                    .values(
                        review_id=uuid4(),
                        agent_id=row.agent_id,
                        attempt_id=row.attempt_id,
                        environment=row.environment,
                        artifact_sha256=row.artifact_sha256,
                        status="queued",
                    )
                    .on_conflict_do_nothing(
                        constraint="submission_source_reviews_attempt_key"
                    )
                )
        finalize_attempt_id = row.attempt_id
    if (
        output_key is not None
        and payload.status != "succeeded"
        and await storage.object_exists(key=output_key)
    ):
        await storage.delete_object(key=output_key)
    attester = request.app.state.config.screener_auth.hotkey
    if attester is not None:
        await finalize_targon_screen_and_pin_dataset(
            session,
            storage=storage,
            screener_hotkey=attester,
            attempt_id=finalize_attempt_id,
            now=datetime.now(UTC),
            generator=generator,
            chain=chain,
        )


@router.post(
    "/controller/submission-image-builds/{build_id}/runtime-cleanup-required",
    response_model=None,
    status_code=204,
)
async def mark_submission_runtime_cleanup_required(
    build_id: UUID,
    payload: SubmissionImageBuildCleanupRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    now = datetime.now(UTC)
    async with session.begin():
        row = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.build_id == build_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail="submission image build not found"
            )
        if (
            row.environment != payload.environment
            or row.controller_epoch != payload.controller_epoch
            or row.runtime_provider_resource_id != payload.provider_resource_id
        ):
            raise HTTPException(
                status_code=409, detail="runtime cleanup fence is stale"
            )
        row.runtime_error_code = "TARGON_RUNTIME_CLEANUP_REQUIRED"
        row.updated_at = now
        session.add(
            ScreenerCapacityEvent(
                event_id=uuid4(),
                environment=payload.environment,
                event_type="provider_cleanup_required",
                provider="targon",
                node_id=None,
                detail=(
                    "A suspended zero-replica runtime-smoke rental requires "
                    "provider deletion retry."
                ),
                controller_epoch=payload.controller_epoch,
                created_at=now,
            )
        )


@router.post(
    "/controller/submission-image-builds/{build_id}/cleanup-required",
    response_model=None,
    status_code=204,
)
async def record_submission_image_build_cleanup(
    build_id: UUID,
    payload: SubmissionImageBuildCleanupRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    """Keep provider deletion failures visible after zero-replica suspension."""
    async with session.begin():
        row = await session.get(SubmissionImageBuild, build_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail="submission image build not found"
            )
        if (
            row.environment != payload.environment
            or row.controller_epoch != payload.controller_epoch
            or row.provider_resource_id != payload.provider_resource_id
        ):
            raise HTTPException(status_code=409, detail="build cleanup lease is stale")
        session.add(
            ScreenerCapacityEvent(
                event_id=uuid4(),
                environment=payload.environment,
                event_type="provider_cleanup_required",
                provider="targon",
                node_id=None,
                detail=(
                    "A suspended zero-replica submission build rental requires "
                    "provider deletion retry."
                ),
                controller_epoch=payload.controller_epoch,
                created_at=datetime.now(UTC),
            )
        )


@router.get(
    "/submission-image-builds/{build_id}/source",
    response_model=SubmissionBuildSourceResponse,
)
async def get_submission_build_source(
    build_id: UUID,
    session: SessionDep,
    storage: StorageDep,
    authorization: Annotated[str | None, Header()] = None,
) -> SubmissionBuildSourceResponse:
    async with session.begin():
        row = await _locked_submission_build_for_job(
            session, build_id=build_id, authorization=authorization
        )
        agent_id = row.agent_id
        artifact_sha256 = row.artifact_sha256
        image_ref = row.image_ref
    url = await storage.presigned_get_url(
        key=_artifact_key(agent_id),
        expires_in=int(_SUBMISSION_BUILD_URL_TTL.total_seconds()),
    )
    return SubmissionBuildSourceResponse(
        source_url_b64=base64.b64encode(url.encode()).decode(),
        artifact_sha256=artifact_sha256,
        image_ref=image_ref,
    )


@router.post(
    "/submission-image-builds/{build_id}/upload",
    response_model=SubmissionBuildUploadResponse,
)
async def mint_submission_build_upload(
    build_id: UUID,
    payload: SubmissionBuildUploadRequest,
    session: SessionDep,
    storage: StorageDep,
    authorization: Annotated[str | None, Header()] = None,
) -> SubmissionBuildUploadResponse:
    now = datetime.now(UTC)
    async with session.begin():
        row = await _locked_submission_build_for_job(
            session, build_id=build_id, authorization=authorization
        )
        if payload.output_size_bytes > _SUBMISSION_BUILD_MAX_BYTES:
            raise HTTPException(
                status_code=413, detail="remote image archive too large"
            )
        if row.upload_minted_at is not None and (
            row.output_sha256 != payload.output_sha256
            or row.output_size_bytes != payload.output_size_bytes
            or (
                row.output_image_id is not None
                and row.output_image_id != payload.image_id
            )
        ):
            raise HTTPException(status_code=409, detail="remote build output changed")
        row.output_sha256 = payload.output_sha256
        row.output_size_bytes = payload.output_size_bytes
        row.output_image_id = payload.image_id
        row.upload_minted_at = row.upload_minted_at or now
        row.updated_at = now
        key = row.output_key
        metadata = {
            "sha256": payload.output_sha256,
            "build-id": str(row.build_id),
            "attempt-id": str(row.attempt_id),
            "artifact-sha256": row.artifact_sha256,
        }
    expires_in = int(_SUBMISSION_BUILD_URL_TTL.total_seconds())
    url = await storage.presigned_put_url(
        key=key,
        size_bytes=payload.output_size_bytes,
        metadata=metadata,
        expires_in=expires_in,
    )
    return SubmissionBuildUploadResponse(
        upload_url_b64=base64.b64encode(url.encode()).decode(),
        required_headers={
            "Content-Length": str(payload.output_size_bytes),
            "Content-Type": "application/x-tar",
            **{f"x-amz-meta-{key}": value for key, value in metadata.items()},
        },
        expires_at=now + timedelta(seconds=expires_in),
    )


@router.post(
    "/submission-image-builds/{build_id}/complete",
    response_model=SubmissionBuildCompleteResponse,
)
async def complete_submission_build_upload(
    build_id: UUID,
    payload: SubmissionBuildCompleteRequest,
    request: Request,
    session: SessionDep,
    storage: StorageDep,
    authorization: Annotated[str | None, Header()] = None,
) -> SubmissionBuildCompleteResponse:
    async with session.begin():
        row = await _locked_submission_build_for_job(
            session, build_id=build_id, authorization=authorization
        )
        if (
            row.output_sha256 != payload.output_sha256
            or row.output_size_bytes != payload.output_size_bytes
            or row.upload_minted_at is None
            or (
                row.output_image_id is not None
                and row.output_image_id != payload.image_id
            )
        ):
            raise HTTPException(status_code=409, detail="remote build output changed")
        row.output_image_id = payload.image_id
        key = row.output_key
        metadata = {
            "sha256": payload.output_sha256,
            "build-id": str(row.build_id),
            "attempt-id": str(row.attempt_id),
            "artifact-sha256": row.artifact_sha256,
        }
    try:
        head = await storage.head_object(key=key)
        verified = await storage.verify_object_sha256(
            key=key, expected_size_bytes=payload.output_size_bytes
        )
    except (ObjectNotFoundError, ObjectUploadFailedError, ObjectDownloadFailedError):
        raise HTTPException(
            status_code=503, detail="remote build storage verification unavailable"
        ) from None
    if (
        head.size_bytes != payload.output_size_bytes
        or head.metadata != metadata
        or verified.size_bytes != payload.output_size_bytes
        or verified.sha256 != payload.output_sha256
    ):
        await storage.delete_object(key=key)
        raise HTTPException(status_code=409, detail="remote build archive mismatch")
    now = datetime.now(UTC)
    async with session.begin():
        stored = await session.get(SubmissionImageBuild, build_id, with_for_update=True)
        if stored is None or stored.status not in {"leased", "running"}:
            raise HTTPException(
                status_code=409, detail="remote build is no longer active"
            )
        stored.status = "succeeded"
        stored.completed_at = now
        stored.updated_at = now
        stored.lease_expires_at = None
        stored.job_token_hash = None
        stored.job_token_expires_at = None
        rental_uid = stored.provider_resource_id
        provider = stored.provider
    if provider == "targon" and await _release_targon_rental(request, rental_uid):
        async with session.begin():
            stored = await session.get(
                SubmissionImageBuild, build_id, with_for_update=True
            )
            if stored is not None and stored.provider_resource_id == rental_uid:
                stored.provider_resource_id = None
                stored.updated_at = datetime.now(UTC)
    return SubmissionBuildCompleteResponse(verified=True)


@router.post(
    "/agent/{agent_id}/submission-source-reviews",
    response_model=SubmissionSourceReviewResponse,
)
async def queue_submission_source_review(
    agent_id: UUID,
    payload: SubmissionSourceReviewRequest,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
) -> SubmissionSourceReviewResponse:
    """Queue a bounded read-only review alongside the mechanical lane."""
    now = datetime.now(UTC)
    async with session.begin():
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment="prod"
        )
        review_provider = _remote_provider(
            provider_settings.source_review_provider_priority
        )
        agent = await get_agent_by_id(session, agent_id=agent_id, for_update=True)
        attempt = await get_screening_attempt(
            session, attempt_id=payload.attempt_id, for_update=True
        )
        deadline = attempt.deadline if attempt is not None else now
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if (
            agent is None
            or attempt is None
            or attempt.agent_id != agent_id
            or attempt.screener_hotkey != screener_hotkey
            or attempt.status != "running"
            or now >= deadline
        ):
            raise AgentNotScreenableError(
                "remote source review does not match an active screening attempt"
            )
        values = {
            "review_id": uuid4(),
            "agent_id": agent_id,
            "attempt_id": payload.attempt_id,
            "environment": "prod",
            "artifact_sha256": agent.sha256.lower(),
            "status": "queued" if review_provider is not None else "fallback_required",
            "provider": None if review_provider is not None else "gcp",
            "error_code": (
                None
                if review_provider is not None
                else "TARGON_SOURCE_REVIEW_DISABLED_BY_POLICY"
            ),
            "completed_at": None if review_provider is not None else now,
        }
        await session.execute(
            pg_insert(SubmissionSourceReview)
            .values(**values)
            .on_conflict_do_nothing(constraint="submission_source_reviews_attempt_key")
        )
        row = await session.scalar(
            select(SubmissionSourceReview).where(
                SubmissionSourceReview.attempt_id == payload.attempt_id
            )
        )
        if row is None:  # pragma: no cover
            raise HTTPException(
                status_code=503, detail="source-review queue unavailable"
            )
    return _source_review_view(row)


@router.get(
    "/agent/{agent_id}/submission-source-reviews/{review_id}",
    response_model=SubmissionSourceReviewResponse,
)
async def get_submission_source_review(
    agent_id: UUID,
    review_id: UUID,
    attempt_id: UUID,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
) -> SubmissionSourceReviewResponse:
    row = await session.get(SubmissionSourceReview, review_id)
    attempt = await session.get(ScreeningAttempt, attempt_id)
    if row is None or row.agent_id != agent_id or row.attempt_id != attempt_id:
        raise HTTPException(
            status_code=404, detail="submission source review not found"
        )
    if attempt is None or attempt.screener_hotkey != screener_hotkey:
        raise AgentNotScreenableError(
            "remote source review does not match screener lease"
        )
    return _source_review_view(row)


@router.delete(
    "/agent/{agent_id}/submission-source-reviews/{review_id}",
    response_model=None,
    status_code=204,
)
async def consume_submission_source_review(
    agent_id: UUID,
    review_id: UUID,
    attempt_id: UUID,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
) -> None:
    async with session.begin():
        row = await session.scalar(
            select(SubmissionSourceReview)
            .where(SubmissionSourceReview.review_id == review_id)
            .with_for_update()
        )
        attempt = await session.get(ScreeningAttempt, attempt_id)
        if row is None or row.agent_id != agent_id or row.attempt_id != attempt_id:
            raise HTTPException(
                status_code=404, detail="submission source review not found"
            )
        if attempt is None or attempt.screener_hotkey != screener_hotkey:
            raise AgentNotScreenableError(
                "remote source review does not match screener lease"
            )
        if row.status in {"queued", "leased", "running"}:
            row.status = "canceled"
            row.completed_at = datetime.now(UTC)
        elif row.status == "succeeded":
            row.status = "consumed"
            row.consumed_at = datetime.now(UTC)
        elif row.status not in {"fallback_required", "canceled", "consumed"}:
            raise AgentNotScreenableError("remote source review is not discardable")
        row.lease_expires_at = None
        row.job_token_hash = None
        row.job_token_expires_at = None
        row.updated_at = datetime.now(UTC)


@router.post(
    "/controller/submission-source-reviews/claim",
    response_model=SubmissionSourceReviewClaimResponse,
)
async def claim_submission_source_review(
    payload: TrustedImageBuildClaimRequest,
    request: Request,
    _controller: ControllerDep,
    session: SessionDep,
) -> SubmissionSourceReviewClaimResponse:
    if _platform_owns_miner_rentals(request):
        return SubmissionSourceReviewClaimResponse(review=None)
    now = datetime.now(UTC)
    async with session.begin():
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment=payload.environment
        )
        if not remote_lane_selected(provider_settings.source_review_provider_priority):
            if (
                _remote_provider(provider_settings.source_review_provider_priority)
                == "hetzner"
            ):
                return SubmissionSourceReviewClaimResponse(review=None)
            await session.execute(
                update(SubmissionSourceReview)
                .where(
                    SubmissionSourceReview.environment == payload.environment,
                    SubmissionSourceReview.status == "queued",
                )
                .values(
                    status="fallback_required",
                    provider="targon",
                    error_code="TARGON_SOURCE_REVIEW_DISABLED_BY_POLICY",
                    completed_at=now,
                    updated_at=now,
                )
            )
            return SubmissionSourceReviewClaimResponse(review=None)
        await session.execute(
            update(SubmissionSourceReview)
            .where(
                SubmissionSourceReview.environment == payload.environment,
                SubmissionSourceReview.status.in_(("leased", "running")),
                SubmissionSourceReview.lease_expires_at < now,
                SubmissionSourceReview.attempt_count >= 1,
            )
            .values(
                status="fallback_required",
                error_code="TARGON_SOURCE_REVIEW_LEASE_EXHAUSTED",
                completed_at=now,
                lease_expires_at=None,
                job_token_hash=None,
                job_token_expires_at=None,
                updated_at=now,
            )
        )
        await session.execute(
            update(SubmissionSourceReview)
            .where(
                SubmissionSourceReview.environment == payload.environment,
                SubmissionSourceReview.status == "queued",
                SubmissionSourceReview.attempt_count >= 3,
                SubmissionSourceReview.provider_outage_epoch.is_(None),
            )
            .values(
                status="fallback_required",
                provider="targon",
                error_code="TARGON_SOURCE_REVIEW_LEASE_EXHAUSTED",
                completed_at=now,
                updated_at=now,
            )
        )
        await session.execute(
            update(SubmissionSourceReview)
            .where(
                SubmissionSourceReview.environment == payload.environment,
                SubmissionSourceReview.status == "queued",
                ~exists().where(
                    (ScreeningAttempt.attempt_id == SubmissionSourceReview.attempt_id)
                    & (ScreeningAttempt.status == "running")
                    & (ScreeningAttempt.deadline > now)
                ),
            )
            .values(status="canceled", completed_at=now, updated_at=now)
        )
        candidate_id = await session.scalar(
            select(SubmissionSourceReview.review_id)
            .join(
                ScreeningAttempt,
                ScreeningAttempt.attempt_id == SubmissionSourceReview.attempt_id,
            )
            .where(
                SubmissionSourceReview.environment == payload.environment,
                ScreeningAttempt.status == "running",
                ScreeningAttempt.deadline > now,
                SubmissionSourceReview.status == "queued",
                exists().where(
                    (
                        SubmissionImageBuild.attempt_id
                        == SubmissionSourceReview.attempt_id
                    )
                    & (SubmissionImageBuild.status.in_(("succeeded", "consumed")))
                    & (SubmissionImageBuild.runtime_status == "succeeded")
                ),
            )
            .order_by(SubmissionSourceReview.created_at)
            .limit(1)
        )
        if candidate_id is None:
            return SubmissionSourceReviewClaimResponse(review=None)
        provider_gate = await lock_provider_work_gate(
            session,
            now=now,
            kind="screening",
            key=str(candidate_id),
        )
        if not provider_gate.admitted:
            return SubmissionSourceReviewClaimResponse(review=None)
        row = await session.scalar(
            select(SubmissionSourceReview)
            .join(
                ScreeningAttempt,
                ScreeningAttempt.attempt_id == SubmissionSourceReview.attempt_id,
            )
            .where(
                SubmissionSourceReview.review_id == candidate_id,
                SubmissionSourceReview.environment == payload.environment,
                ScreeningAttempt.status == "running",
                ScreeningAttempt.deadline > now,
                or_(
                    SubmissionSourceReview.status == "queued",
                    (
                        SubmissionSourceReview.status.in_(("leased", "running"))
                        & (SubmissionSourceReview.lease_expires_at < now)
                        & (SubmissionSourceReview.attempt_count < 3)
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return SubmissionSourceReviewClaimResponse(review=None)
        image_build = await session.scalar(
            select(TrustedImageBuild)
            .where(
                TrustedImageBuild.environment == payload.environment,
                TrustedImageBuild.component == "screener",
                TrustedImageBuild.status == "succeeded",
                TrustedImageBuild.image_digest.is_not(None),
            )
            .order_by(TrustedImageBuild.completed_at.desc())
            .limit(1)
        )
        if image_build is None or image_build.image_digest is None:
            row.status = "fallback_required"
            row.provider = "targon"
            row.error_code = "TARGON_SOURCE_REVIEW_IMAGE_UNPUBLISHED"
            row.completed_at = now
            row.updated_at = now
            return SubmissionSourceReviewClaimResponse(review=None)
        image_repository = image_build.destination.rsplit(":", 1)[0]
        image_reference = f"{image_repository}@{image_build.image_digest}"
        token = _fresh_node_token()
        token_expires_at = now + _SOURCE_REVIEW_JOB_TTL
        row.status = "leased"
        row.provider = "targon"
        row.controller_epoch = payload.controller_epoch
        row.lease_expires_at = now + _SOURCE_REVIEW_LEASE_TTL
        parked_epoch = row.provider_outage_epoch
        if parked_epoch is None:
            row.attempt_count += 1
        else:
            row.provider_outage_attempted_epoch = parked_epoch
        row.provider_outage_epoch = None
        row.job_token_hash = hashlib.sha256(token.encode()).hexdigest()
        row.job_token_expires_at = token_expires_at
        row.updated_at = now
        register_provider_probe(
            provider_gate,
            now=now,
            kind="screening",
            key=str(row.review_id),
        )
    return SubmissionSourceReviewClaimResponse(
        review=SubmissionSourceReviewClaimView(
            review_id=row.review_id,
            agent_id=row.agent_id,
            attempt_id=row.attempt_id,
            artifact_sha256=row.artifact_sha256,
            image_reference=image_reference,
            job_token=token,
            job_token_expires_at=token_expires_at,
        )
    )


@router.put(
    "/controller/submission-source-reviews/{review_id}",
    response_model=None,
    status_code=204,
)
async def update_submission_source_review(
    review_id: UUID,
    payload: SubmissionSourceReviewControllerUpdateRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    now = datetime.now(UTC)
    async with session.begin():
        row = await session.scalar(
            select(SubmissionSourceReview)
            .where(SubmissionSourceReview.review_id == review_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail="submission source review not found"
            )
        if row.controller_epoch != payload.controller_epoch:
            raise HTTPException(
                status_code=409, detail="source-review lease epoch is stale"
            )
        if row.status not in {"leased", "running"}:
            raise HTTPException(
                status_code=409, detail="submission source review is terminal"
            )
        row.status = payload.status
        row.provider = "targon"
        row.provider_resource_id = payload.provider_resource_id
        row.error_code = payload.error_code
        row.started_at = row.started_at or now
        row.updated_at = now
        if payload.status == "fallback_required":
            row.completed_at = now
            row.lease_expires_at = None
            row.job_token_hash = None
            row.job_token_expires_at = None


@router.get(
    "/controller/submission-source-reviews/{review_id}",
    response_model=SubmissionSourceReviewControllerStatusResponse,
)
async def get_controller_submission_source_review(
    review_id: UUID,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")],
    controller_epoch: Annotated[str, Query(pattern=r"^[A-Za-z0-9._:@/-]{8,200}$")],
    _controller: ControllerDep,
    session: SessionDep,
) -> SubmissionSourceReviewControllerStatusResponse:
    row = await session.get(SubmissionSourceReview, review_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail="submission source review not found"
        )
    if row.environment != environment or row.controller_epoch != controller_epoch:
        raise HTTPException(
            status_code=409, detail="source-review lease epoch is stale"
        )
    return SubmissionSourceReviewControllerStatusResponse(
        review_id=row.review_id, status=cast(Any, row.status)
    )


async def _locked_active_node(
    request: Request,
    session: AsyncSession,
    *,
    environment: str,
    require_active: bool = True,
) -> ScreenerNode:
    node_id = getattr(request.state, "screener_node_id", None)
    if node_id is None:
        raise ScreenerAuthError("node-scoped job claims require enrolled-node auth")
    node = await session.scalar(
        select(ScreenerNode).where(ScreenerNode.node_id == node_id).with_for_update()
    )
    if node is None or node.environment != environment:
        raise ScreenerAuthError("screener node is not authorized for this environment")
    if require_active and node.status != "active":
        raise HTTPException(status_code=409, detail="screener node is not active")
    if not require_active and node.status not in {"active", "draining"}:
        raise HTTPException(status_code=409, detail="screener node cannot update jobs")
    return node


async def _node_sandbox_usage(session: AsyncSession, *, node_id: str) -> int:
    builds = await session.scalar(
        select(func.count())
        .select_from(SubmissionImageBuild)
        .where(
            SubmissionImageBuild.node_id == node_id,
            SubmissionImageBuild.status.in_(("leased", "running")),
        )
    )
    runtime = await session.scalar(
        select(func.count())
        .select_from(SubmissionImageBuild)
        .where(
            SubmissionImageBuild.runtime_node_id == node_id,
            SubmissionImageBuild.runtime_status == "running",
        )
    )
    return int(builds or 0) + int(runtime or 0)


@router.get(
    "/nodes/channel-settings",
    response_model=EffectiveScreenerNodeChannelSettings,
)
async def get_effective_node_channel_settings(
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
) -> EffectiveScreenerNodeChannelSettings:
    node_id = getattr(request.state, "screener_node_id", None)
    if node_id is None:
        raise ScreenerAuthError("node settings require enrolled-node auth")
    node = await session.get(ScreenerNode, node_id)
    if node is None:
        raise ScreenerAuthError("screener node is not authorized")
    revision, settings = await resolve_screener_node_channel_settings(
        session, node_id=node.node_id
    )
    return EffectiveScreenerNodeChannelSettings(
        environment=node.environment,
        node_id=node.node_id,
        revision=revision,
        settings=settings,
    )


@router.post(
    "/nodes/jobs/submission-image-builds/claim",
    response_model=SubmissionImageBuildClaimResponse,
)
async def claim_node_submission_image_build(
    payload: ScreenerNodeJobClaimRequest,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
) -> SubmissionImageBuildClaimResponse:
    """Atomically enforce node and shared-VM limits before minting a job token."""
    now = datetime.now(UTC)
    async with session.begin():
        node = await _locked_active_node(
            request, session, environment=payload.environment
        )
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment=payload.environment
        )
        if not provider_settings.build_provider_priority or (
            provider_settings.build_provider_priority[0] != node.provider
        ):
            return SubmissionImageBuildClaimResponse(build=None)
        _, limits = await resolve_screener_node_channel_settings(
            session, node_id=node.node_id
        )
        active_builds = await session.scalar(
            select(func.count())
            .select_from(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.node_id == node.node_id,
                SubmissionImageBuild.status.in_(("leased", "running")),
            )
        )
        if int(active_builds or 0) >= limits.build_concurrency or (
            await _node_sandbox_usage(session, node_id=node.node_id)
            >= limits.sandbox_slots
        ):
            return SubmissionImageBuildClaimResponse(build=None)
        await session.execute(
            update(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.node_id == node.node_id,
                SubmissionImageBuild.status.in_(("leased", "running")),
                SubmissionImageBuild.lease_expires_at < now,
            )
            .values(
                status="fallback_required",
                error_code="FLEET_SUBMISSION_BUILD_LEASE_EXHAUSTED",
                runtime_status="skipped",
                runtime_error_code="FLEET_RUNTIME_SKIPPED_BUILD_UNAVAILABLE",
                runtime_completed_at=now,
                completed_at=now,
                lease_expires_at=None,
                job_token_hash=None,
                job_token_expires_at=None,
                updated_at=now,
            )
        )
        row = await session.scalar(
            select(SubmissionImageBuild)
            .join(
                ScreeningAttempt,
                ScreeningAttempt.attempt_id == SubmissionImageBuild.attempt_id,
            )
            .where(
                SubmissionImageBuild.environment == payload.environment,
                ScreeningAttempt.status == "running",
                ScreeningAttempt.deadline > now,
                SubmissionImageBuild.status == "queued",
            )
            .order_by(SubmissionImageBuild.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return SubmissionImageBuildClaimResponse(build=None)
        token = _fresh_node_token()
        token_expires_at = now + _SUBMISSION_BUILD_JOB_TTL
        row.status = "leased"
        row.provider = node.provider
        row.node_id = node.node_id
        row.controller_epoch = f"node:{node.node_id}:{uuid4().hex[:16]}"
        row.lease_expires_at = now + _SUBMISSION_BUILD_LEASE_TTL
        row.attempt_count += 1
        row.job_token_hash = hashlib.sha256(token.encode()).hexdigest()
        row.job_token_expires_at = token_expires_at
        row.updated_at = now
    return SubmissionImageBuildClaimResponse(
        build=SubmissionImageBuildClaimView(
            build_id=row.build_id,
            agent_id=row.agent_id,
            attempt_id=row.attempt_id,
            artifact_sha256=row.artifact_sha256,
            image_ref=row.image_ref,
            job_token=token,
            job_token_expires_at=token_expires_at,
        )
    )


@router.put(
    "/nodes/jobs/submission-image-builds/{build_id}",
    response_model=None,
    status_code=204,
)
async def update_node_submission_image_build(
    build_id: UUID,
    payload: ScreenerNodeJobUpdateRequest,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
) -> None:
    now = datetime.now(UTC)
    async with session.begin():
        node = await _locked_active_node(
            request, session, environment="prod", require_active=False
        )
        row = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.build_id == build_id)
            .with_for_update()
        )
        if row is None or row.node_id != node.node_id:
            raise HTTPException(
                status_code=404, detail="submission image build not found"
            )
        if row.status not in {"leased", "running"}:
            raise HTTPException(status_code=409, detail="submission build is terminal")
        row.status = payload.status
        row.provider = node.provider
        row.provider_resource_id = payload.provider_resource_id
        row.error_code = payload.error_code
        row.started_at = row.started_at or now
        row.updated_at = now
        if payload.status == "fallback_required":
            row.completed_at = now
            row.lease_expires_at = None
            row.job_token_hash = None
            row.job_token_expires_at = None


@router.get(
    "/nodes/jobs/submission-image-builds/{build_id}",
    response_model=SubmissionImageBuildControllerStatusResponse,
)
async def get_node_submission_image_build(
    build_id: UUID,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
) -> SubmissionImageBuildControllerStatusResponse:
    node_id = getattr(request.state, "screener_node_id", None)
    row = await session.get(SubmissionImageBuild, build_id)
    if row is None or row.node_id != node_id:
        raise HTTPException(status_code=404, detail="submission image build not found")
    return SubmissionImageBuildControllerStatusResponse(
        build_id=row.build_id, status=cast(Any, row.status)
    )


@router.post(
    "/nodes/jobs/submission-runtime-smokes/claim",
    response_model=SubmissionRuntimeArtifactClaimResponse,
)
async def claim_node_submission_runtime_smoke(
    payload: ScreenerNodeJobClaimRequest,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
) -> SubmissionRuntimeArtifactClaimResponse:
    now = datetime.now(UTC)
    async with session.begin():
        node = await _locked_active_node(
            request, session, environment=payload.environment
        )
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment=payload.environment
        )
        if not provider_settings.runtime_provider_priority or (
            provider_settings.runtime_provider_priority[0] != node.provider
        ):
            return SubmissionRuntimeArtifactClaimResponse(artifact=None)
        _, limits = await resolve_screener_node_channel_settings(
            session, node_id=node.node_id
        )
        active_runtime = await session.scalar(
            select(func.count())
            .select_from(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.runtime_node_id == node.node_id,
                SubmissionImageBuild.runtime_status == "running",
            )
        )
        if int(active_runtime or 0) >= limits.runtime_concurrency or (
            await _node_sandbox_usage(session, node_id=node.node_id)
            >= limits.sandbox_slots
        ):
            return SubmissionRuntimeArtifactClaimResponse(artifact=None)
        row = await session.scalar(
            select(SubmissionImageBuild)
            .where(
                SubmissionImageBuild.environment == payload.environment,
                SubmissionImageBuild.status.in_(("succeeded", "consumed")),
                SubmissionImageBuild.runtime_status == "pending",
                SubmissionImageBuild.output_sha256.is_not(None),
                SubmissionImageBuild.output_size_bytes.is_not(None),
            )
            .order_by(SubmissionImageBuild.completed_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return SubmissionRuntimeArtifactClaimResponse(artifact=None)
        row.runtime_status = "running"
        row.runtime_node_id = node.node_id
        row.controller_epoch = f"node:{node.node_id}:{uuid4().hex[:16]}"
        row.updated_at = now
        output_sha256 = cast(str, row.output_sha256)
        output_size_bytes = cast(int, row.output_size_bytes)
        output_key = row.output_key
        build_id = row.build_id
    url = await storage.presigned_get_url(
        key=output_key,
        expires_in=int(_SUBMISSION_BUILD_URL_TTL.total_seconds()),
    )
    return SubmissionRuntimeArtifactClaimResponse(
        artifact=SubmissionRuntimeArtifactResponse(
            build_id=build_id,
            archive_url_b64=base64.b64encode(url.encode()).decode(),
            output_sha256=output_sha256,
            output_size_bytes=output_size_bytes,
            destination=f"{_CANDIDATE_RUNTIME_REGISTRY}:build-{build_id.hex}",
        )
    )


@router.post(
    "/nodes/jobs/submission-runtime-smokes/{build_id}/result",
    response_model=None,
    status_code=204,
)
async def complete_node_submission_runtime_smoke(
    build_id: UUID,
    payload: ScreenerNodeRuntimeResultRequest,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
    generator: GeneratorDep,
    chain: ChainDep,
) -> None:
    async with session.begin():
        node = await _locked_active_node(
            request, session, environment="prod", require_active=False
        )
        row = await session.scalar(
            select(SubmissionImageBuild)
            .where(SubmissionImageBuild.build_id == build_id)
            .with_for_update()
        )
        if row is None or row.runtime_node_id != node.node_id:
            raise HTTPException(status_code=404, detail="runtime smoke not found")
        epoch = row.controller_epoch
        environment = row.environment
        image_reference = payload.image_reference
        if (
            payload.status == "succeeded"
            and image_reference is None
            and row.output_image_id is not None
        ):
            image_reference = (
                f"fleet.local/{node.node_id}/candidate@{row.output_image_id}"
            )
    if epoch is None:
        raise HTTPException(status_code=409, detail="runtime smoke fence is stale")
    await complete_submission_runtime_smoke(
        build_id=build_id,
        payload=SubmissionRuntimeResultRequest(
            environment=environment,
            controller_epoch=epoch,
            status=payload.status,
            provider_resource_id=payload.provider_resource_id,
            image_reference=image_reference,
            error_code=payload.error_code,
        ),
        request=request,
        _controller=None,
        session=session,
        storage=storage,
        generator=generator,
        chain=chain,
    )


@router.post(
    "/nodes/jobs/submission-source-reviews/claim",
    response_model=SubmissionSourceReviewClaimResponse,
)
async def claim_node_submission_source_review(
    payload: ScreenerNodeJobClaimRequest,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
) -> SubmissionSourceReviewClaimResponse:
    now = datetime.now(UTC)
    async with session.begin():
        node = await _locked_active_node(
            request, session, environment=payload.environment
        )
        _, provider_settings = await resolve_screener_provider_settings(
            session, environment=payload.environment
        )
        if not provider_settings.source_review_provider_priority or (
            provider_settings.source_review_provider_priority[0] != node.provider
        ):
            return SubmissionSourceReviewClaimResponse(review=None)
        _, limits = await resolve_screener_node_channel_settings(
            session, node_id=node.node_id
        )
        active_reviews = await session.scalar(
            select(func.count())
            .select_from(SubmissionSourceReview)
            .where(
                SubmissionSourceReview.node_id == node.node_id,
                SubmissionSourceReview.status.in_(("leased", "running")),
            )
        )
        if int(active_reviews or 0) >= limits.source_review_concurrency:
            return SubmissionSourceReviewClaimResponse(review=None)
        row = await session.scalar(
            select(SubmissionSourceReview)
            .join(
                ScreeningAttempt,
                ScreeningAttempt.attempt_id == SubmissionSourceReview.attempt_id,
            )
            .where(
                SubmissionSourceReview.environment == payload.environment,
                ScreeningAttempt.status == "running",
                ScreeningAttempt.deadline > now,
                SubmissionSourceReview.status == "queued",
                exists().where(
                    (
                        SubmissionImageBuild.attempt_id
                        == SubmissionSourceReview.attempt_id
                    )
                    & (SubmissionImageBuild.status.in_(("succeeded", "consumed")))
                    & (SubmissionImageBuild.runtime_status == "succeeded")
                ),
            )
            .order_by(SubmissionSourceReview.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return SubmissionSourceReviewClaimResponse(review=None)
        image_build = await session.scalar(
            select(TrustedImageBuild)
            .where(
                TrustedImageBuild.environment == payload.environment,
                TrustedImageBuild.component == "screener",
                TrustedImageBuild.status == "succeeded",
                TrustedImageBuild.image_digest.is_not(None),
            )
            .order_by(TrustedImageBuild.completed_at.desc())
            .limit(1)
        )
        if image_build is None or image_build.image_digest is None:
            return SubmissionSourceReviewClaimResponse(review=None)
        token = _fresh_node_token()
        token_expires_at = now + _SOURCE_REVIEW_JOB_TTL
        row.status = "leased"
        row.provider = node.provider
        row.node_id = node.node_id
        row.controller_epoch = f"node:{node.node_id}:{uuid4().hex[:16]}"
        row.lease_expires_at = now + _SOURCE_REVIEW_LEASE_TTL
        row.attempt_count += 1
        row.job_token_hash = hashlib.sha256(token.encode()).hexdigest()
        row.job_token_expires_at = token_expires_at
        row.updated_at = now
        image_repository = image_build.destination.rsplit(":", 1)[0]
        image_reference = f"{image_repository}@{image_build.image_digest}"
    return SubmissionSourceReviewClaimResponse(
        review=SubmissionSourceReviewClaimView(
            review_id=row.review_id,
            agent_id=row.agent_id,
            attempt_id=row.attempt_id,
            artifact_sha256=row.artifact_sha256,
            image_reference=image_reference,
            job_token=token,
            job_token_expires_at=token_expires_at,
        )
    )


@router.put(
    "/nodes/jobs/submission-source-reviews/{review_id}",
    response_model=None,
    status_code=204,
)
async def update_node_submission_source_review(
    review_id: UUID,
    payload: ScreenerNodeJobUpdateRequest,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
) -> None:
    now = datetime.now(UTC)
    async with session.begin():
        node = await _locked_active_node(
            request, session, environment="prod", require_active=False
        )
        row = await session.scalar(
            select(SubmissionSourceReview)
            .where(SubmissionSourceReview.review_id == review_id)
            .with_for_update()
        )
        if row is None or row.node_id != node.node_id:
            raise HTTPException(status_code=404, detail="source review not found")
        if row.status not in {"leased", "running"}:
            raise HTTPException(status_code=409, detail="source review is terminal")
        row.status = payload.status
        row.provider = node.provider
        row.provider_resource_id = payload.provider_resource_id
        row.error_code = payload.error_code
        row.started_at = row.started_at or now
        row.updated_at = now
        if payload.status == "fallback_required":
            row.completed_at = now
            row.lease_expires_at = None
            row.job_token_hash = None
            row.job_token_expires_at = None


@router.get(
    "/nodes/jobs/submission-source-reviews/{review_id}",
    response_model=SubmissionSourceReviewControllerStatusResponse,
)
async def get_node_submission_source_review(
    review_id: UUID,
    request: Request,
    _screener: ScreenerDep,
    session: SessionDep,
) -> SubmissionSourceReviewControllerStatusResponse:
    node_id = getattr(request.state, "screener_node_id", None)
    row = await session.get(SubmissionSourceReview, review_id)
    if row is None or row.node_id != node_id:
        raise HTTPException(status_code=404, detail="source review not found")
    return SubmissionSourceReviewControllerStatusResponse(
        review_id=row.review_id, status=cast(Any, row.status)
    )


@router.post(
    "/controller/submission-source-reviews/{review_id}/cleanup-required",
    response_model=None,
    status_code=204,
)
async def mark_submission_source_review_cleanup_required(
    review_id: UUID,
    payload: SubmissionSourceReviewCleanupRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    now = datetime.now(UTC)
    async with session.begin():
        row = await session.scalar(
            select(SubmissionSourceReview)
            .where(SubmissionSourceReview.review_id == review_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(
                status_code=404, detail="submission source review not found"
            )
        if (
            row.environment != payload.environment
            or row.controller_epoch != payload.controller_epoch
            or row.provider_resource_id != payload.provider_resource_id
        ):
            raise HTTPException(
                status_code=409, detail="source-review cleanup fence is stale"
            )
        if row.status != "succeeded" and row.error_code is None:
            row.error_code = "TARGON_SOURCE_REVIEW_CLEANUP_REQUIRED"
        row.updated_at = now
        session.add(
            ScreenerCapacityEvent(
                event_id=uuid4(),
                environment=payload.environment,
                event_type="provider_cleanup_required",
                provider="targon",
                node_id=None,
                detail=(
                    "A suspended zero-replica source-review rental requires "
                    "provider deletion retry."
                ),
                controller_epoch=payload.controller_epoch,
                created_at=now,
            )
        )


@router.get(
    "/submission-source-reviews/{review_id}/source",
    response_model=SubmissionSourceReviewSourceResponse,
)
async def get_submission_source_review_source(
    review_id: UUID,
    session: SessionDep,
    storage: StorageDep,
    authorization: Annotated[str | None, Header()] = None,
) -> SubmissionSourceReviewSourceResponse:
    async with session.begin():
        row = await _locked_source_review_for_job(
            session, review_id=review_id, authorization=authorization
        )
        agent_id = row.agent_id
        artifact_sha256 = row.artifact_sha256
    url = await storage.presigned_get_url(
        key=_artifact_key(agent_id),
        expires_in=int(_SOURCE_REVIEW_URL_TTL.total_seconds()),
    )
    return SubmissionSourceReviewSourceResponse(
        source_url_b64=base64.b64encode(url.encode()).decode(),
        artifact_sha256=artifact_sha256,
    )


@router.post(
    "/submission-source-reviews/{review_id}/complete",
    response_model=SubmissionSourceReviewCompleteResponse,
)
async def complete_submission_source_review(
    review_id: UUID,
    payload: SubmissionSourceReviewCompleteRequest,
    request: Request,
    session: SessionDep,
    storage: StorageDep,
    generator: GeneratorDep,
    chain: ChainDep,
    authorization: Annotated[str | None, Header()] = None,
) -> SubmissionSourceReviewCompleteResponse:
    now = datetime.now(UTC)
    parked = False
    async with session.begin():
        circuit = await session.scalar(
            select(ProviderOutageCircuit)
            .where(ProviderOutageCircuit.provider == "openrouter")
            .with_for_update()
        )
        row = await _locked_source_review_for_job(
            session, review_id=review_id, authorization=authorization
        )
        finding = payload.observation.finding
        if finding is not None and finding.artifact_sha256 != row.artifact_sha256:
            raise HTTPException(
                status_code=409, detail="source-review artifact mismatch"
            )
        row.observation = payload.observation.model_dump(mode="json")
        parked = bool(
            circuit is not None
            and circuit.state == "open"
            and payload.observation.error_code == "source-review-http-429"
        )
        row.status = "queued" if parked else "succeeded"
        if parked:
            assert circuit is not None
            row.provider_outage_epoch = (
                circuit.epoch
                if row.provider_outage_attempted_epoch != circuit.epoch
                else None
            )
        else:
            row.provider_outage_epoch = None
        row.completed_at = None if parked else now
        row.updated_at = now
        row.lease_expires_at = None
        row.job_token_hash = None
        row.job_token_expires_at = None
        if parked:
            row.controller_epoch = None
        attempt_id = row.attempt_id
        rental_uid = row.provider_resource_id
        provider = row.provider
    if provider == "targon" and await _release_targon_rental(request, rental_uid):
        async with session.begin():
            stored_review = await session.get(
                SubmissionSourceReview, review_id, with_for_update=True
            )
            if (
                stored_review is not None
                and stored_review.provider_resource_id == rental_uid
            ):
                stored_review.provider_resource_id = None
                stored_review.updated_at = datetime.now(UTC)
    attester = request.app.state.config.screener_auth.hotkey
    if attester is not None and not parked:
        await finalize_targon_screen_and_pin_dataset(
            session,
            storage=storage,
            screener_hotkey=attester,
            attempt_id=attempt_id,
            now=datetime.now(UTC),
            generator=generator,
            chain=chain,
        )
    return SubmissionSourceReviewCompleteResponse(verified=True)


@router.put("/controller/nodes/{node_id}", response_model=None, status_code=204)
async def update_screener_node_status(
    node_id: str,
    payload: ScreenerNodeStatusRequest,
    _controller: ControllerDep,
    session: SessionDep,
) -> None:
    """Drain, quarantine, reactivate, or revoke one enrolled node."""
    now = datetime.now(UTC)
    async with session.begin():
        await _require_controller_epoch(
            session,
            environment=payload.environment,
            epoch=payload.controller_epoch,
            now=now,
        )
        node = await session.scalar(
            select(ScreenerNode)
            .where(ScreenerNode.node_id == node_id)
            .with_for_update()
        )
        if node is None:
            raise HTTPException(status_code=404, detail="screener node not found")
        if node.status == "revoked" and payload.status != "revoked":
            raise HTTPException(status_code=409, detail="revoked node is terminal")
        node.status = payload.status
        node.status_reason = payload.reason
        node.revoked_at = now if payload.status == "revoked" else None


@router.get("/controller/nodes", response_model=ScreenerControllerNodesResponse)
async def list_controller_nodes(
    _controller: ControllerDep,
    session: SessionDep,
    environment: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "prod",
) -> ScreenerControllerNodesResponse:
    """Return redacted enrollment readiness for provider reconciliation."""
    now = datetime.now(UTC)
    required_policy = (await _required_policy(session)).required_policy_version
    nodes = list(
        await session.scalars(
            select(ScreenerNode)
            .where(ScreenerNode.environment == environment)
            .order_by(ScreenerNode.node_id)
        )
    )
    heartbeats: dict[tuple[str, str], ScreenerHeartbeat] = {}
    for row in await session.scalars(
        select(ScreenerHeartbeat).order_by(ScreenerHeartbeat.seen_at.desc())
    ):
        heartbeats.setdefault((row.screener_hotkey, row.instance_id), row)
    active_hotkeys = set(
        await session.scalars(
            select(ScreeningAttempt.screener_hotkey).where(
                ScreeningAttempt.status == "running",
                ScreeningAttempt.deadline > now,
            )
        )
    )
    response: list[ScreenerControllerNodeState] = []
    enrolled_instance_ids = {node.node_id for node in nodes}
    for node in nodes:
        _, channel_settings = await resolve_screener_node_channel_settings(
            session, node_id=node.node_id
        )
        heartbeat = heartbeats.get((node.screener_hotkey, node.node_id))
        seen_at = heartbeat.seen_at if heartbeat is not None else None
        if seen_at is not None and seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        ready = bool(
            node.status == "active"
            and heartbeat is not None
            and seen_at is not None
            and now - seen_at <= timedelta(seconds=_CONTROLLER_HEARTBEAT_READY_SECONDS)
            and heartbeat.policy_version == required_policy
        )
        response.append(
            ScreenerControllerNodeState.model_validate(
                {
                    "node_id": node.node_id,
                    "provider_resource_id": node.provider_resource_id,
                    "provider": node.provider,
                    "status": node.status,
                    "ready": ready,
                    "active_lease": node.screener_hotkey in active_hotkeys,
                    "screening_concurrency": (channel_settings.screening_concurrency),
                    "image_reference": node.image_reference,
                    "heartbeat_seen_at": seen_at,
                }
            )
        )
    for heartbeat in heartbeats.values():
        if (
            not heartbeat.instance_id.startswith("ditto-screener-fleet-")
            or heartbeat.instance_id in enrolled_instance_ids
        ):
            continue
        seen_at = heartbeat.seen_at
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=UTC)
        ready = bool(
            now - seen_at <= timedelta(seconds=_CONTROLLER_HEARTBEAT_READY_SECONDS)
            and heartbeat.policy_version == required_policy
        )
        response.append(
            ScreenerControllerNodeState(
                node_id=heartbeat.instance_id,
                provider_resource_id=heartbeat.instance_id,
                provider="gcp",
                status="active",
                ready=ready,
                active_lease=heartbeat.screener_hotkey in active_hotkeys,
                screening_concurrency=1,
                heartbeat_seen_at=seen_at,
            )
        )
    return ScreenerControllerNodesResponse(nodes=tuple(response))


def _review_settings_checksum(settings: ScreenerReviewSettings) -> str:
    payload = json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


async def _resolve_effective_review_settings(
    session: AsyncSession, *, instance_id: str
) -> EffectiveScreenerReviewSettings:
    """Resolve one immutable settings snapshot for fetch and claim binding."""
    rows = list(
        await session.scalars(
            select(ScreenerReviewSettingsRevision)
            .where(ScreenerReviewSettingsRevision.scope.in_((instance_id, "*")))
            .order_by(ScreenerReviewSettingsRevision.revision.desc())
        )
    )
    latest_by_scope: dict[str, ScreenerReviewSettingsRevision] = {}
    for candidate in rows:
        latest_by_scope.setdefault(candidate.scope, candidate)
    exact = latest_by_scope.get(instance_id)
    row = (
        exact
        if exact is not None and exact.settings.get("mode") != "inherit"
        else latest_by_scope.get("*")
    )
    if row is None:
        settings = ScreenerReviewSettings()
        return EffectiveScreenerReviewSettings(
            revision=0,
            scope="builtin-default",
            settings=settings,
            checksum=_review_settings_checksum(settings),
        )
    return EffectiveScreenerReviewSettings(
        revision=row.revision,
        scope=row.scope,
        settings=ScreenerReviewSettings.model_validate_json(json.dumps(row.settings)),
        checksum=row.checksum,
    )


@router.get("/review-settings", response_model=EffectiveScreenerReviewSettings)
async def effective_review_settings(
    response: Response,
    _screener_hotkey: ScreenerDep,
    session: SessionDep,
    instance_id: Annotated[str, Query(pattern=_INSTANCE_ID_PATTERN)],
) -> EffectiveScreenerReviewSettings:
    """Return the exact-instance override or global settings revision."""
    result = await _resolve_effective_review_settings(session, instance_id=instance_id)
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["ETag"] = f'"{result.revision}-{result.checksum}"'
    return result


@router.post(
    "/agent/{agent_id}/shadow-review",
    response_model=ShadowReviewObservationResponse,
)
async def submit_shadow_review(
    agent_id: UUID,
    payload: ShadowReviewObservationRequest,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
) -> ShadowReviewObservationResponse:
    """Persist attempt-owned telemetry without mutating submission state."""
    async with session.begin():
        attempt = await get_screening_attempt(
            session, attempt_id=payload.attempt_id, for_update=True
        )
        if (
            attempt is None
            or attempt.agent_id != agent_id
            or attempt.screener_hotkey != screener_hotkey
            or attempt.status != "running"
            or attempt.build_only
        ):
            raise AgentNotScreenableError(
                "shadow review does not match an active screening attempt"
            )
        deadline = attempt.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if datetime.now(UTC) > deadline:
            raise AgentNotScreenableError("shadow review arrived after lease expiry")
        agent = await get_agent_by_id(session, agent_id=agent_id, for_update=True)
        if agent is None:
            raise AgentNotFoundError(f"no agent with id={agent_id}")
        if agent.sha256.lower() != payload.artifact_sha256:
            raise AgentNotScreenableError(
                "shadow review artifact does not match the claimed submission"
            )
        settings = await session.get(
            ScreenerReviewSettingsRevision, payload.settings_revision
        )
        if (
            settings is None
            or settings.scope != payload.settings_scope
            or settings.checksum != payload.settings_checksum
            or settings.settings.get("mode") != "shadow"
        ):
            raise AgentNotScreenableError(
                "shadow review does not match an applied shadow revision"
            )
        values = {
            "agent_id": agent_id,
            "screener_hotkey": screener_hotkey,
            "artifact_sha256": payload.artifact_sha256,
            "settings_revision": payload.settings_revision,
            "settings_scope": payload.settings_scope,
            "settings_checksum": payload.settings_checksum,
            "disposition": payload.disposition,
            "risk_level": payload.risk_level,
            "categories": list(payload.categories),
            "finding_digest": payload.finding_digest,
            "resolution_basis": payload.resolution_basis,
            "clearance_path": payload.clearance_path,
            "critic_disposition": payload.critic_disposition,
            "adjudicator_disposition": payload.adjudicator_disposition,
            "response_models": list(payload.response_models),
            "response_providers": list(payload.response_providers),
            "usage": payload.usage.model_dump(mode="json"),
        }
        existing = await session.get(ScreenerShadowReview, payload.attempt_id)
        if existing is not None:
            if any(getattr(existing, key) != value for key, value in values.items()):
                raise AgentNotScreenableError(
                    "shadow review conflicts with the stored attempt observation"
                )
            return ShadowReviewObservationResponse(accepted=True)
        session.add(ScreenerShadowReview(attempt_id=payload.attempt_id, **values))
    return ShadowReviewObservationResponse(accepted=True)


def _heartbeat_signing_message(payload: ScreenerHeartbeatRequest) -> bytes:
    """Canonical versioned heartbeat bytes mirrored by ``ditto-screener``."""
    if payload.protocol_version == 1:
        return (
            "ditto-screener-heartbeat:v1:"
            f"{payload.screener_hotkey}:{payload.software_version}:"
            f"{payload.protocol_version}:{payload.policy_version}:{payload.state}:"
            f"{payload.active_agent_id or ''}:"
            f"{system_metrics_signing_token(payload.system_metrics)}:{payload.timestamp}"
        ).encode()
    progress = (
        f"{payload.progress.stage},{payload.progress.started_at}"
        if payload.progress is not None
        else "-"
    )
    if payload.protocol_version >= 4:
        assert payload.review_settings is not None
        review = payload.review_settings
        review_fields = [
            str(review.revision),
            review.scope,
            review.mode,
            review.checksum,
            review.source,
        ]
        if payload.protocol_version >= 5:
            review_fields.extend(
                (
                    review.policy_manifest_profile,
                    review.policy_manifest_rotation_id,
                    review.policy_manifest_digest,
                )
            )
        review_token = ",".join(review_fields)
        # v5 and v6 extend the v4 field list rather than opening a new prefix:
        # protocol_version is itself signed, so appending a field can never make
        # an older message re-read as a newer one.
        fields = [
            payload.screener_hotkey,
            payload.software_version,
            str(payload.protocol_version),
            str(payload.policy_version),
            payload.state,
            str(payload.active_agent_id or ""),
            str(payload.instance_id),
            progress,
            system_metrics_signing_token(payload.system_metrics),
            review_token,
        ]
        if payload.protocol_version >= 6:
            fields.append(host_specs_signing_token(payload.host_specs))
        fields.append(str(payload.timestamp))
        return ("ditto-screener-heartbeat:v4:" + ":".join(fields)).encode()
    if payload.protocol_version >= 3:
        # v3 signs the per-instance identity (the fleet shares one hotkey).
        # instance_id is required for v3 (validated on the request model).
        return (
            "ditto-screener-heartbeat:v3:"
            f"{payload.screener_hotkey}:{payload.software_version}:"
            f"{payload.protocol_version}:{payload.policy_version}:{payload.state}:"
            f"{payload.active_agent_id or ''}:{payload.instance_id}:"
            f"{progress}:"
            f"{system_metrics_signing_token(payload.system_metrics)}:{payload.timestamp}"
        ).encode()
    return (
        "ditto-screener-heartbeat:v2:"
        f"{payload.screener_hotkey}:{payload.software_version}:"
        f"{payload.protocol_version}:{payload.policy_version}:{payload.state}:"
        f"{payload.active_agent_id or ''}:"
        f"{progress}:"
        f"{system_metrics_signing_token(payload.system_metrics)}:{payload.timestamp}"
    ).encode()


@router.post(
    "/heartbeat",
    response_model=ScreenerHeartbeatResponse,
    responses={
        401: {"description": "Invalid screener credentials, signature, or timestamp."},
        413: {"description": "Heartbeat payload exceeds the bounded contract."},
    },
)
async def heartbeat(
    request: Request,
    request_body: ScreenerHeartbeatRequest,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
) -> ScreenerHeartbeatResponse:
    """Record a fresh report signed by the dedicated screener identity."""
    content_length = request.headers.get("content-length")
    try:
        claimed_bytes = int(content_length) if content_length is not None else 0
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid Content-Length") from error
    if (
        claimed_bytes > _HEARTBEAT_MAX_BYTES
        or len(await request.body()) > _HEARTBEAT_MAX_BYTES
    ):
        raise HTTPException(status_code=413, detail="heartbeat payload too large")
    if request_body.screener_hotkey != screener_hotkey:
        raise ScreenerAuthError("heartbeat body hotkey does not match header")
    enrolled_node_id = getattr(request.state, "screener_node_id", None)
    if enrolled_node_id is not None and request_body.instance_id != enrolled_node_id:
        raise ScreenerAuthError("heartbeat instance does not match enrolled node")
    now = datetime.now(UTC)
    if abs(int(now.timestamp()) - request_body.timestamp) > _HEARTBEAT_MAX_SKEW_SECONDS:
        raise ScreenerAuthError("heartbeat timestamp is stale or too far in the future")
    if (
        request_body.system_metrics is not None
        and abs(request_body.timestamp - request_body.system_metrics.collected_at)
        > _HEARTBEAT_MAX_SKEW_SECONDS
    ):
        raise ScreenerAuthError(
            "system metrics timestamp is outside the heartbeat window"
        )
    if request_body.active_agent_id is not None and request_body.state != "screening":
        raise ScreenerAuthError("active agent requires screening state")
    if not _verify_signature(
        screener_hotkey,
        _heartbeat_signing_message(request_body),
        request_body.signature,
    ):
        raise ScreenerAuthError("heartbeat signature verification failed")

    reported_at = datetime.fromtimestamp(request_body.timestamp, tz=UTC)
    instance_id = request_body.instance_id or _LEGACY_INSTANCE_ID
    renewed_lease_deadline: datetime | None = None
    async with session.begin():
        previous_heartbeat = await session.get(
            ScreenerHeartbeat,
            (screener_hotkey, instance_id),
            with_for_update=True,
        )
        previous_active_agent_id = (
            previous_heartbeat.active_agent_id
            if previous_heartbeat is not None
            else None
        )
        previous_progress = None
        if previous_heartbeat is not None and isinstance(
            previous_heartbeat.system_metrics, dict
        ):
            previous_progress = previous_heartbeat.system_metrics.get(
                "screening_progress"
            )
        current_progress = (
            request_body.progress.model_dump(mode="json")
            if request_body.progress is not None
            else None
        )
        previous_started_at = (
            previous_progress.get("started_at")
            if isinstance(previous_progress, dict)
            else None
        )
        current_started_at = (
            current_progress.get("started_at")
            if isinstance(current_progress, dict)
            else None
        )
        previous_stage = (
            previous_progress.get("stage")
            if isinstance(previous_progress, dict)
            else None
        )
        current_stage = (
            current_progress.get("stage")
            if isinstance(current_progress, dict)
            else None
        )
        previous_stage_rank = (
            _SCREENER_PROGRESS_RANK.get(previous_stage)
            if isinstance(previous_stage, str)
            else None
        )
        current_stage_rank = (
            _SCREENER_PROGRESS_RANK.get(current_stage)
            if isinstance(current_stage, str)
            else None
        )
        progress_advanced = (
            previous_heartbeat is None
            or previous_active_agent_id != request_body.active_agent_id
            or previous_started_at != current_started_at
            or (
                previous_stage_rank is not None
                and current_stage_rank is not None
                and current_stage_rank > previous_stage_rank
            )
        )
        row, accepted = await upsert_screener_heartbeat(
            session,
            screener_hotkey=screener_hotkey,
            instance_id=instance_id,
            software_version=request_body.software_version,
            protocol_version=request_body.protocol_version,
            policy_version=request_body.policy_version,
            state=request_body.state,
            active_agent_id=request_body.active_agent_id,
            screening_progress=current_progress,
            system_metrics=(
                request_body.system_metrics.model_dump(mode="json")
                if request_body.system_metrics is not None
                else None
            ),
            review_settings=(
                request_body.review_settings.model_dump(
                    mode="json",
                    exclude=(
                        {
                            "policy_manifest_profile",
                            "policy_manifest_rotation_id",
                            "policy_manifest_digest",
                        }
                        if request_body.protocol_version < 5
                        else None
                    ),
                )
                if request_body.review_settings is not None
                else None
            ),
            host_specs=(
                request_body.host_specs.model_dump(mode="json")
                if request_body.host_specs is not None
                else None
            ),
            reported_at=reported_at,
            seen_at=now,
            signature=request_body.signature,
        )
        if (
            accepted
            and progress_advanced
            and request_body.state == "screening"
            and request_body.active_agent_id is not None
            and request_body.progress is not None
        ):
            attempt = await session.scalar(
                select(ScreeningAttempt)
                .where(
                    ScreeningAttempt.agent_id == request_body.active_agent_id,
                    ScreeningAttempt.screener_hotkey == screener_hotkey,
                    ScreeningAttempt.status == "running",
                    ScreeningAttempt.deadline > now,
                )
                .order_by(ScreeningAttempt.started_at.desc())
                .with_for_update()
                .limit(1)
            )
            if attempt is not None:
                renewed_lease_deadline = now + _RENEWABLE_SCREENING_LEASE_TTL
                attempt.deadline = renewed_lease_deadline
        # Reap heartbeats from long-gone instances (scaled-in fleet workers)
        # so the per-instance list stays bounded. Cheap indexed delete.
        await prune_stale_screener_heartbeats(
            session, before=now - _HEARTBEAT_RETENTION
        )
    seen_at = row.seen_at
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=UTC)
    return ScreenerHeartbeatResponse(
        accepted=accepted,
        seen_at=seen_at,
        lease_deadline=renewed_lease_deadline,
    )


@router.get(
    "/queue",
    response_model=ScreenerQueueResponse,
    responses={
        401: {"description": "Missing/invalid screener auth."},
        409: {"description": "Worker screening policy does not match platform."},
        422: {"description": "Malformed query parameter."},
    },
)
async def queue(
    response: Response,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ScreenerQueueResponse:
    """List completion-lane contenders, then least-scored pending agents."""
    response.headers["Cache-Control"] = "no-store"
    screener_policy = await _required_policy(session)
    required_policy = screener_policy.required_policy_version
    rolling_qualified = exists(
        select(BenchmarkRolloutMember.agent_id)
        .join(BenchmarkRollout)
        .where(
            BenchmarkRolloutMember.agent_id == Agent.agent_id,
            BenchmarkRollout.status.in_(("collecting", "blocked_ineligible")),
        )
    )
    missing_v3_screen = (
        (Agent.screening_policy_version < required_policy)
        | Agent.screened_image_sha256.is_(None)
        | Agent.screened_image_size_bytes.is_(None)
        | Agent.screened_image_id.is_(None)
        | Agent.screened_image_ref.is_(None)
        | Agent.screened_image_upload_id.is_(None)
        | Agent.screened_image_verified_at.is_(None)
    )
    missing_dataset, prerequisite_admitted = await prerequisite_screening_predicates(
        session
    )
    # A due activation re-queues everything screened under a stale policy on
    # the same criteria: evaluating/rejected rows always rescreen (the
    # ``< required_policy`` arm above), and scored/live rows join them only
    # when the governing activation opted them in, so a routine version bump
    # cannot silently pull champions off the ledger without an operator
    # decision recorded on the schedule row.
    stale_scored_rescreen = (
        screener_policy.rescreen_stale_agents and screener_policy.rescreen_scored
    )
    agents = (
        await session.scalars(
            select(Agent)
            .where(
                or_(
                    Agent.status == AgentStatus.UPLOADED,
                    Agent.status == AgentStatus.SCREENING_FAILED,
                    (
                        Agent.status.in_(
                            (
                                AgentStatus.EVALUATING,
                                AgentStatus.REJECTED,
                            )
                        )
                        & (Agent.screening_policy_version < required_policy)
                        & prerequisite_admitted
                    ),
                    (
                        Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE))
                        & stale_scored_rescreen
                        & (Agent.screening_policy_version < required_policy)
                    ),
                    (
                        Agent.status.in_((AgentStatus.SCORED, AgentStatus.LIVE))
                        & rolling_qualified
                        & missing_v3_screen
                    ),
                    (
                        (Agent.status == AgentStatus.EVALUATING)
                        & prerequisite_admitted
                        & missing_dataset
                    ),
                )
            )
            .order_by(*screening_priority_order())
            .limit(limit)
        )
    ).all()
    items = [
        ScreenerQueueItem(
            agent_id=a.agent_id,
            miner_hotkey=a.miner_hotkey,
            name=a.name,
            sha256=a.sha256,
            status=a.status,
            created_at=a.created_at,
        )
        for a in agents
    ]
    logger.info("screener=%s polled queue: %d item(s)", screener_hotkey, len(items))
    return ScreenerQueueResponse(
        items=items,
        count=len(items),
        required_policy_version=required_policy,
    )


@router.post(
    "/claim",
    response_model=ScreenerQueueResponse,
    responses={
        401: {"description": "Missing/invalid screener auth."},
        422: {"description": "Malformed query parameter."},
    },
)
async def claim(
    request: Request,
    response: Response,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    policy_version: Annotated[int, Query(ge=1)],
    limit: Annotated[int, Query(ge=1, le=20)] = 1,
    renewable_lease: Annotated[bool, Query()] = False,
    review_settings_revision: Annotated[int | None, Query(ge=0)] = None,
    review_settings_instance_id: Annotated[
        str | None, Query(pattern=_INSTANCE_ID_PATTERN)
    ] = None,
    review_settings_scope: Annotated[
        str | None, Query(min_length=1, max_length=63)
    ] = None,
    review_settings_checksum: Annotated[
        str | None, Query(pattern=r"^[0-9a-f]{64}$")
    ] = None,
) -> ScreenerQueueResponse:
    """Lease pending work and make its active screening state public."""
    response.headers["Cache-Control"] = "no-store"
    node_status = getattr(request.state, "screener_node_status", "active")
    if node_status != "active":
        raise AgentNotScreenableError(
            f"screener node is {node_status}; no new work may be claimed"
        )
    required_policy = (await _required_policy(session)).required_policy_version
    if policy_version != required_policy:
        raise AgentNotScreenableError(
            "screening policy mismatch before claim: platform requires "
            f"{required_policy}, worker declared {policy_version}"
        )
    now = datetime.now(UTC)
    lease_ttl = (
        _RENEWABLE_SCREENING_LEASE_TTL
        if renewable_lease
        else _LEGACY_SCREENING_LEASE_TTL
    )
    supplied_binding = (
        review_settings_revision,
        review_settings_instance_id,
        review_settings_scope,
        review_settings_checksum,
    )
    if any(value is not None for value in supplied_binding) and any(
        value is None for value in supplied_binding
    ):
        raise AgentNotScreenableError("review settings claim binding must be complete")

    async def resolve_claim_binding() -> tuple[int, str, str, str] | None:
        if not all(value is not None for value in supplied_binding):
            return None
        assert review_settings_instance_id is not None
        effective = await _resolve_effective_review_settings(
            session, instance_id=review_settings_instance_id
        )
        expected = (
            effective.revision,
            review_settings_instance_id,
            effective.scope,
            effective.checksum,
        )
        if supplied_binding != expected:
            raise AgentNotScreenableError(
                "review settings changed before claim; refresh and retry"
            )
        # Revision zero is computed from built-in defaults and has no immutable row.
        return expected if effective.revision >= 1 else None

    if session.get_bind().dialect.name == "postgresql":
        async with session.begin():
            node_id = getattr(request.state, "screener_node_id", None)
            if node_id is not None:
                node = await session.scalar(
                    select(ScreenerNode)
                    .where(ScreenerNode.node_id == node_id)
                    .with_for_update()
                )
                if node is None:
                    raise ScreenerAuthError("screener node is not authorized")
                _, limits = await resolve_screener_node_channel_settings(
                    session, node_id=node_id
                )
                active = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ScreeningAttempt)
                        .where(
                            ScreeningAttempt.screener_hotkey == screener_hotkey,
                            ScreeningAttempt.status == "running",
                            ScreeningAttempt.deadline > now,
                        )
                    )
                    or 0
                )
                limit = min(limit, max(0, limits.screening_concurrency - active))
                if limit == 0:
                    return ScreenerQueueResponse(
                        items=[],
                        count=0,
                        required_policy_version=required_policy,
                    )
            queue_settings = await resolve_queue_policy_settings(session)
            binding = await resolve_claim_binding()
            claimed = await claim_screening_attempts(
                session,
                screener_hotkey=screener_hotkey,
                now=now,
                ttl=lease_ttl,
                limit=limit,
                netuid=expected_netuid(),
                deferred_review_mode=queue_settings.deferred_source_review.mode,
                review_settings_binding=binding,
            )
    else:
        # SQLite is used by local/test deployments and has no advisory locks.
        # Hold a process-local lock through commit so its behavior matches the
        # Postgres transaction-scoped lock used in production.
        async with _CLAIM_FALLBACK_LOCK, session.begin():
            node_id = getattr(request.state, "screener_node_id", None)
            if node_id is not None:
                node = await session.get(ScreenerNode, node_id)
                if node is None:
                    raise ScreenerAuthError("screener node is not authorized")
                _, limits = await resolve_screener_node_channel_settings(
                    session, node_id=node_id
                )
                active = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ScreeningAttempt)
                        .where(
                            ScreeningAttempt.screener_hotkey == screener_hotkey,
                            ScreeningAttempt.status == "running",
                            ScreeningAttempt.deadline > now,
                        )
                    )
                    or 0
                )
                limit = min(limit, max(0, limits.screening_concurrency - active))
                if limit == 0:
                    return ScreenerQueueResponse(
                        items=[],
                        count=0,
                        required_policy_version=required_policy,
                    )
            queue_settings = await resolve_queue_policy_settings(session)
            binding = await resolve_claim_binding()
            claimed = await claim_screening_attempts(
                session,
                screener_hotkey=screener_hotkey,
                now=now,
                ttl=lease_ttl,
                limit=limit,
                netuid=expected_netuid(),
                deferred_review_mode=queue_settings.deferred_source_review.mode,
                review_settings_binding=binding,
            )
    items = [
        ScreenerQueueItem(
            agent_id=agent.agent_id,
            miner_hotkey=agent.miner_hotkey,
            name=agent.name,
            sha256=agent.sha256,
            status=agent.status,
            created_at=agent.created_at,
            attempt_id=attempt.attempt_id,
            lease_deadline=attempt.deadline,
            # ``precheck_reason_code`` is the exact-duplicate channel and the
            # signed queue contract requires it to be paired with
            # ``duplicate_of``. Mechanical deferred admission has its own
            # explicit flags below; leaking its internal attempt reason into
            # the duplicate channel makes response validation fail after the
            # lease transaction commits.
            precheck_reason_code=(
                attempt.reason_code if duplicate_of is not None else None
            ),
            duplicate_of=duplicate_of,
            build_only=attempt.build_only,
            policy_only=attempt.reason_code == POLICY_ONLY_RESCREEN_REASON,
            deferred_source_review=(
                attempt.build_only and attempt.reason_code == DEFERRED_MECHANICAL_REASON
            ),
        )
        for agent, attempt, duplicate_of in claimed
    ]
    logger.info("screener=%s claimed %d item(s)", screener_hotkey, len(items))
    return ScreenerQueueResponse(
        items=items,
        count=len(items),
        required_policy_version=required_policy,
    )


@router.get(
    "/agent/{agent_id}/artifact",
    response_model=ArtifactResponse,
    responses={
        401: {"description": "Missing/invalid screener auth."},
        404: {"description": "No agent with the given id."},
        409: {"description": "No active screening attempt for this screener/agent."},
        422: {"description": "Malformed UUID path or query parameter."},
    },
)
async def agent_artifact(
    agent_id: UUID,
    request: Request,
    response: Response,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
    attempt_id: Annotated[UUID | None, Query()] = None,
    instance_id: Annotated[str | None, Query(pattern=_INSTANCE_ID_PATTERN)] = None,
) -> ArtifactResponse:
    """Return a short-lived pre-signed download URL for the agent's tarball.

    Download is bound to an active screening lease for this screener. Callers
    should pass the claim ``attempt_id``; without it the platform still requires
    a unique running attempt for ``(screener_hotkey, agent_id)``.

    ``instance_id`` is optional and records *which* worker in the shared-hotkey
    fleet took the source, for the artifact audit trail. It is accepted here so
    the platform is ready for it; a screener that does not send it is still
    served normally and simply audits without per-instance attribution.
    """
    response.headers["Cache-Control"] = "no-store"
    now = datetime.now(UTC)
    async with session.begin():
        agent = await get_agent_by_id(session, agent_id=agent_id)
        if agent is None:
            raise AgentNotFoundError(f"no agent with id={agent_id}")
        if attempt_id is not None:
            attempt = await get_screening_attempt(
                session, attempt_id=attempt_id, for_update=True
            )
        else:
            attempt = await session.scalar(
                select(ScreeningAttempt)
                .where(
                    ScreeningAttempt.agent_id == agent_id,
                    ScreeningAttempt.screener_hotkey == screener_hotkey,
                    ScreeningAttempt.status == "running",
                )
                .with_for_update()
            )
        if (
            attempt is None
            or attempt.agent_id != agent_id
            or attempt.screener_hotkey != screener_hotkey
            or attempt.status != "running"
        ):
            raise AgentNotScreenableError(
                "artifact download does not match an active screening attempt"
            )
        deadline = attempt.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if now > deadline:
            raise AgentNotScreenableError("screening attempt lease has expired")
    url = await storage.presigned_get_url(
        key=_artifact_key(agent_id),
        expires_in=int(_ARTIFACT_URL_TTL.total_seconds()),
    )
    logger.info(
        "screener=%s fetched artifact url for agent_id=%s attempt_id=%s",
        screener_hotkey,
        agent_id,
        attempt.attempt_id,
    )
    # SCREENER_HOTKEY is one shared string across an autoscaled fleet, so the
    # attempt lease is the sharpest attribution available until callers start
    # sending instance_id.
    await record_artifact_fetch(
        session,
        agent_id=agent_id,
        endpoint=ENDPOINT_SCREENER_ARTIFACT,
        requester_kind="screener",
        requester_id=screener_hotkey,
        requester_instance_id=instance_id,
        lease_id=attempt.attempt_id,
        artifact_sha256=agent.sha256,
        source_ip=client_ip(request),
        detail=request_detail(request),
    )
    return ArtifactResponse(
        agent_id=agent_id,
        sha256=agent.sha256,
        download_url=url,
        expires_at=datetime.now(UTC) + _ARTIFACT_URL_TTL,
    )


@router.post(
    "/agent/{agent_id}/screened-image-upload",
    response_model=ScreenedImageUploadResponse,
    responses={
        401: {"description": "Missing/invalid screener auth."},
        409: {"description": "Screening attempt is not active."},
        422: {"description": "Malformed image metadata."},
    },
)
async def screened_image_upload(
    agent_id: UUID,
    payload: ScreenedImageUploadRequest,
    response: Response,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
) -> ScreenedImageUploadResponse:
    """Initiate an immutable multipart upload bound to the active lease."""
    response.headers["Cache-Control"] = "no-store"
    expected_ref = f"ditto-screen/{agent_id}:latest"
    if payload.image_ref != expected_ref:
        raise AgentNotScreenableError("screened image ref does not match agent")
    now = datetime.now(UTC)
    required_policy = (await _required_policy(session)).required_policy_version
    async with session.begin():
        attempt = await get_screening_attempt(
            session, attempt_id=payload.attempt_id, for_update=True
        )
        if (
            attempt is None
            or attempt.agent_id != agent_id
            or attempt.screener_hotkey != screener_hotkey
            or attempt.policy_version != required_policy
            or attempt.status != "running"
        ):
            raise AgentNotScreenableError(
                "screened image upload does not match an active screening attempt"
            )
        deadline = attempt.deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if now > deadline:
            raise AgentNotScreenableError("screened image upload lease has expired")

    image_upload_id = uuid4()
    expires_at = min(now + _SCREENED_IMAGE_UPLOAD_TTL, deadline)
    metadata = {
        "sha256": payload.sha256,
        "image-id": payload.image_id,
        "image-ref": payload.image_ref,
        "attempt-id": str(payload.attempt_id),
        "image-upload-id": str(image_upload_id),
    }
    key = _screened_image_key(agent_id, image_upload_id)
    storage_upload_id = await storage.create_multipart_upload(
        key=key,
        metadata=metadata,
    )
    try:
        async with session.begin():
            attempt = await get_screening_attempt(
                session, attempt_id=payload.attempt_id, for_update=True
            )
            if (
                attempt is None
                or attempt.agent_id != agent_id
                or attempt.screener_hotkey != screener_hotkey
                or attempt.policy_version != required_policy
                or attempt.status != "running"
            ):
                raise AgentNotScreenableError(
                    "screened image upload lease changed during initiation"
                )
            session.add(
                ScreenedImageUpload(
                    image_upload_id=image_upload_id,
                    agent_id=agent_id,
                    attempt_id=payload.attempt_id,
                    screener_hotkey=screener_hotkey,
                    storage_upload_id=storage_upload_id,
                    sha256=payload.sha256,
                    size_bytes=payload.size_bytes,
                    image_id=payload.image_id,
                    image_ref=payload.image_ref,
                    status="initiated",
                    expires_at=expires_at,
                )
            )
    except Exception:
        await storage.abort_multipart_upload(key=key, upload_id=storage_upload_id)
        raise
    return ScreenedImageUploadResponse(
        image_upload_id=image_upload_id,
        storage_upload_id=storage_upload_id,
        part_size_bytes=_SCREENED_IMAGE_PART_SIZE,
        expires_at=expires_at,
    )


async def _load_active_image_upload(
    session: AsyncSession,
    *,
    agent_id: UUID,
    image_upload_id: UUID,
    attempt_id: UUID,
    storage_upload_id: str,
    screener_hotkey: str,
    for_update: bool = False,
) -> ScreenedImageUpload:
    """Load and authenticate one unexpired, attempt-bound multipart session."""
    upload = await session.get(
        ScreenedImageUpload, image_upload_id, with_for_update=for_update
    )
    if (
        upload is None
        or upload.agent_id != agent_id
        or upload.attempt_id != attempt_id
        or upload.screener_hotkey != screener_hotkey
        or upload.storage_upload_id != storage_upload_id
        or upload.status != "initiated"
    ):
        raise AgentNotScreenableError(
            "screened image multipart session is not active or does not match owner"
        )
    expires_at = upload.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if datetime.now(UTC) > expires_at:
        raise AgentNotScreenableError("screened image multipart session has expired")
    attempt = await get_screening_attempt(
        session, attempt_id=attempt_id, for_update=for_update
    )
    if (
        attempt is None
        or attempt.agent_id != agent_id
        or attempt.screener_hotkey != screener_hotkey
        or attempt.status != "running"
        or attempt.policy_version
        != (await resolve_screener_policy_activation(session)).required_policy_version
    ):
        raise AgentNotScreenableError("screening attempt is no longer active")
    return upload


@router.post(
    "/agent/{agent_id}/screened-image-upload/{image_upload_id}/part",
    response_model=ScreenedImagePartResponse,
)
async def screened_image_upload_part(
    agent_id: UUID,
    image_upload_id: UUID,
    payload: ScreenedImagePartRequest,
    response: Response,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
) -> ScreenedImagePartResponse:
    """Mint one short-lived, attempt-bound multipart part URL."""
    response.headers["Cache-Control"] = "no-store"
    async with session.begin():
        upload = await _load_active_image_upload(
            session,
            agent_id=agent_id,
            image_upload_id=image_upload_id,
            attempt_id=payload.attempt_id,
            storage_upload_id=payload.storage_upload_id,
            screener_hotkey=screener_hotkey,
        )
        max_parts = (
            upload.size_bytes + _SCREENED_IMAGE_PART_SIZE - 1
        ) // _SCREENED_IMAGE_PART_SIZE
        if payload.part_number > max_parts:
            raise AgentNotScreenableError("multipart part exceeds declared image size")
        expected_size = min(
            _SCREENED_IMAGE_PART_SIZE,
            upload.size_bytes - (payload.part_number - 1) * _SCREENED_IMAGE_PART_SIZE,
        )
        if payload.size_bytes != expected_size:
            raise AgentNotScreenableError(
                "multipart part size does not match declaration"
            )
        expires_at = upload.expires_at
    ttl = max(
        1,
        min(
            int(_SCREENED_IMAGE_UPLOAD_TTL.total_seconds()),
            int(
                (
                    expires_at.replace(tzinfo=UTC)
                    if expires_at.tzinfo is None
                    else expires_at
                ).timestamp()
                - datetime.now(UTC).timestamp()
            ),
        ),
    )
    url = await storage.presigned_upload_part_url(
        key=_screened_image_key(agent_id, image_upload_id),
        upload_id=payload.storage_upload_id,
        part_number=payload.part_number,
        expires_in=ttl,
    )
    return ScreenedImagePartResponse(
        upload_url=url,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        required_headers={"Content-Length": str(payload.size_bytes)},
    )


@router.post(
    "/agent/{agent_id}/screened-image-upload/{image_upload_id}/complete",
    response_model=ScreenedImageCompleteResponse,
)
async def screened_image_upload_complete(
    agent_id: UUID,
    image_upload_id: UUID,
    payload: ScreenedImageCompleteRequest,
    response: Response,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
) -> ScreenedImageCompleteResponse:
    """Complete multipart upload and verify the exact final archive bytes."""
    response.headers["Cache-Control"] = "no-store"
    async with session.begin():
        upload = await _load_active_image_upload(
            session,
            agent_id=agent_id,
            image_upload_id=image_upload_id,
            attempt_id=payload.attempt_id,
            storage_upload_id=payload.storage_upload_id,
            screener_hotkey=screener_hotkey,
        )
        if (
            upload.sha256 != payload.sha256
            or upload.size_bytes != payload.size_bytes
            or upload.image_id != payload.image_id
            or upload.image_ref != payload.image_ref
        ):
            raise AgentNotScreenableError(
                "multipart completion metadata does not match initiation"
            )
    key = _screened_image_key(agent_id, image_upload_id)
    try:
        await storage.complete_multipart_upload(
            key=key,
            upload_id=payload.storage_upload_id,
            parts=[
                {"PartNumber": part.part_number, "ETag": part.etag}
                for part in payload.parts
            ],
        )
        stored = await storage.head_object(key=key)
        expected_metadata = {
            "sha256": payload.sha256,
            "image-id": payload.image_id,
            "image-ref": payload.image_ref,
            "attempt-id": str(payload.attempt_id),
            "image-upload-id": str(image_upload_id),
        }
        verified = await storage.verify_object_sha256(
            key=key, expected_size_bytes=payload.size_bytes
        )
    except ObjectNotFoundError as error:
        raise AgentNotScreenableError(
            "screened image upload is missing or incomplete"
        ) from error
    except (ObjectUploadFailedError, ObjectDownloadFailedError) as error:
        raise HTTPException(
            status_code=503, detail="screened image storage verification unavailable"
        ) from error
    if (
        stored.size_bytes != payload.size_bytes
        or stored.metadata != expected_metadata
        or verified.size_bytes != payload.size_bytes
        or verified.sha256 != payload.sha256
    ):
        await storage.delete_object(key=key)
        async with session.begin():
            stored_upload = await session.get(
                ScreenedImageUpload, image_upload_id, with_for_update=True
            )
            if stored_upload is not None:
                stored_upload.status = "aborted"
        raise AgentNotScreenableError(
            "completed screened image bytes do not match the declared digest"
        )
    async with session.begin():
        stored_upload = await session.get(
            ScreenedImageUpload, image_upload_id, with_for_update=True
        )
        if stored_upload is None or stored_upload.status != "initiated":
            raise AgentNotScreenableError("multipart session is no longer active")
        stored_upload.status = "verified"
        stored_upload.verified_at = datetime.now(UTC)
    return ScreenedImageCompleteResponse(verified=True)


@router.post(
    "/agent/{agent_id}/screened-image-upload/{image_upload_id}/abort",
    response_model=ScreenedImageAbortResponse,
)
async def screened_image_upload_abort(
    agent_id: UUID,
    image_upload_id: UUID,
    payload: ScreenedImageAbortRequest,
    response: Response,
    screener_hotkey: ScreenerDep,
    session: SessionDep,
    storage: StorageDep,
) -> ScreenedImageAbortResponse:
    """Abort a multipart upload and mark the session terminal."""
    response.headers["Cache-Control"] = "no-store"
    async with session.begin():
        upload = await _load_active_image_upload(
            session,
            agent_id=agent_id,
            image_upload_id=image_upload_id,
            attempt_id=payload.attempt_id,
            storage_upload_id=payload.storage_upload_id,
            screener_hotkey=screener_hotkey,
            for_update=True,
        )
        upload.status = "aborted"
    await storage.abort_multipart_upload(
        key=_screened_image_key(agent_id, image_upload_id),
        upload_id=payload.storage_upload_id,
    )
    return ScreenedImageAbortResponse(aborted=True)


_RUST_CONTRACT_DIAGNOSTIC_RE = re.compile(r"^error\[(SCR-RUST-\d{3})\]:", re.IGNORECASE)
_RUST_CONTRACT_PUBLIC_REASONS = {
    "SCR-RUST-001": (
        "archive contains an unsafe path. Remove absolute paths, parent traversals, "
        "backslashes, and drive-prefixed entries."
    ),
    "SCR-RUST-002": (
        "archive contains a duplicate path. Package each path exactly once."
    ),
    "SCR-RUST-003": (
        "archive contains a link or special file. Package only regular files and "
        "directories."
    ),
    "SCR-RUST-004": (
        "archive expands beyond the safety limit. Remove generated assets and build "
        "output before packaging."
    ),
    "SCR-RUST-005": (
        "Dockerfile is missing from the archive root. Package the crate contents so "
        "Dockerfile is at the top level."
    ),
    "SCR-RUST-006": (
        "Cargo.toml is missing from the archive root. Package the crate contents, not "
        "the directory containing the crate."
    ),
    "SCR-RUST-007": (
        "no Rust source file was found under src/. Include at least one .rs source "
        "file below src/."
    ),
    "SCR-RUST-008": (
        "Cargo.toml could not be read. Recreate the archive from a readable UTF-8 "
        "crate manifest."
    ),
    "SCR-RUST-009": (
        "Cargo.toml is not valid UTF-8 TOML. Run cargo metadata locally and fix the "
        "first manifest error."
    ),
    "SCR-RUST-010": (
        "Cargo.toml has no [package] table. Submit a runnable Rust package rather "
        "than a virtual workspace."
    ),
    "SCR-RUST-011": (
        "archive is not a readable gzip-compressed tar. Recreate it as a .tar.gz "
        "archive and retry."
    ),
    "SCR-RUST-012": (
        "Dockerfile is not valid UTF-8 text. Commit a readable UTF-8 Dockerfile that "
        "builds the crate."
    ),
}

_CONTAINER_CONTRACT_DIAGNOSTIC_RE = re.compile(
    r"^error\[(SCR-(?:ARCHIVE|CONTRACT)-\d{3})\]:", re.IGNORECASE
)
_CONTAINER_CONTRACT_PUBLIC_REASONS = {
    "SCR-ARCHIVE-001": (
        "archive contains an unsafe or non-canonical path. Remove absolute paths, "
        "parent traversals, backslashes, drive prefixes, and redundant components."
    ),
    "SCR-ARCHIVE-002": (
        "archive contains a duplicate path. Package each path exactly once."
    ),
    "SCR-ARCHIVE-003": (
        "archive contains a link or special file. Package only regular files and "
        "directories."
    ),
    "SCR-ARCHIVE-004": (
        "archive expands beyond the safety limit. Remove generated assets and build "
        "output before packaging."
    ),
    "SCR-ARCHIVE-005": (
        "archive contains too many members. Remove generated directories and package "
        "only the harness."
    ),
    "SCR-ARCHIVE-006": (
        "archive is not a readable gzip-compressed tar. Recreate it as a .tar.gz "
        "archive and retry."
    ),
    "SCR-CONTRACT-001": (
        "Dockerfile is missing from the archive root. Package the harness contents "
        "so Dockerfile is at the top level."
    ),
    "SCR-CONTRACT-002": (
        "Dockerfile could not be read. Recreate the archive from readable regular "
        "files."
    ),
    "SCR-CONTRACT-003": (
        "Dockerfile is not valid UTF-8 text. Commit a readable UTF-8 Dockerfile."
    ),
    "SCR-CONTRACT-004": (
        "Dockerfile requests an insecure build entitlement. Remove host networking "
        "and insecure build security modes."
    ),
}


def _public_screening_reason(detail: str, reason_code: str | None = None) -> str:
    """Map untrusted screener detail to a stable, public-safe failure category.

    ``detail`` can include a Docker build-log tail produced by miner-controlled
    code. Never persist or return it verbatim: a malicious Dockerfile could print
    the BuildKit secret mounted for private dependency access.
    """
    if reason_code == "exact-cross-miner-duplicate":
        return "Artifact is an exact duplicate of another miner submission"
    if reason_code == "container-harness-contract":
        match = _CONTAINER_CONTRACT_DIAGNOSTIC_RE.match(detail.strip())
        if match is not None:
            diagnostic_code = match.group(1).upper()
            public_detail = _CONTAINER_CONTRACT_PUBLIC_REASONS.get(diagnostic_code)
            if public_detail is not None:
                return (
                    "Container harness contract failed "
                    f"({diagnostic_code}): {public_detail}"
                )
        return (
            "Submission does not satisfy the container harness contract. Rebuild "
            "the archive as a readable .tar.gz containing only regular files and "
            "directories, with Dockerfile at the archive root. The implementation "
            "language is unrestricted."
        )
    if reason_code == "rust-harness-contract":
        match = _RUST_CONTRACT_DIAGNOSTIC_RE.match(detail.strip())
        if match is not None:
            diagnostic_code = match.group(1).upper()
            public_detail = _RUST_CONTRACT_PUBLIC_REASONS.get(diagnostic_code)
            if public_detail is not None:
                return (
                    f"Rust harness contract failed ({diagnostic_code}): {public_detail}"
                )
        return (
            "Submission does not satisfy the Rust harness contract. Rebuild the "
            "archive as a readable .tar.gz containing only regular files and "
            "directories, with Dockerfile and Cargo.toml at the archive root and "
            "Rust source under src/."
        )
    normalized = detail.strip().casefold()
    if reason_code == "docker-build-infrastructure":
        return (
            "Docker build infrastructure failed before screening completed. This "
            "is operator-owned and requires a manual retry."
        )
    if reason_code == "docker-build" or normalized.startswith("build failed"):
        if (
            "couldn't read" in normalized or "could not read" in normalized
        ) and "no such file or directory" in normalized:
            return (
                "Docker image build failed: source code referenced by the build is "
                "missing from the submitted archive."
            )
        if "failed to calculate checksum" in normalized and "not found" in normalized:
            return (
                "Docker image build failed: Dockerfile COPY references a path that "
                "is missing from the submitted archive."
            )
        if any(
            marker in normalized
            for marker in (
                "dockerfile parse error",
                "failed to parse dockerfile",
                "unknown instruction",
            )
        ):
            return "Docker image build failed: Dockerfile syntax is invalid."
        if any(
            marker in normalized
            for marker in (
                "pull access denied",
                "manifest unknown",
                "no match for platform in manifest",
            )
        ):
            return (
                "Docker image build failed: a base image is unavailable, private, "
                "or incompatible with the screener platform."
            )
        if any(
            marker in normalized
            for marker in (
                "could not compile",
                "compilation terminated",
                "error: aborting due to",
            )
        ):
            return "Docker image build failed while compiling the submitted source."
        if "failed to solve: process" in normalized:
            return "Docker image build failed because a Dockerfile command exited."
        return "Docker image build failed"
    if "no dockerfile at tarball root" in normalized:
        return "Dockerfile missing from archive root"
    if normalized.startswith("serve check failed"):
        return (
            "Container did not return a 2xx response from GET /health on port "
            "8080 during startup"
        )
    if "tarball exceeds" in normalized:
        return "Submission archive exceeded the size limit"
    if "sha256 mismatch" in normalized:
        return "Submission artifact failed integrity verification"
    if normalized.startswith("artifact download"):
        return "Submission artifact could not be downloaded"
    if normalized.startswith("screener error"):
        return "Screening infrastructure error"
    if normalized.startswith("policy failed"):
        return "Submission failed anti-cheat screening"
    if normalized.startswith("contract failed"):
        return "Submission does not satisfy the Rust harness contract"
    if normalized.startswith("model canary"):
        return "Harness did not use the validator model gateway"
    return "Screening failed"


def _failed_screening_target(detail: str) -> AgentStatus:
    """Separate submission rejection from parked screening infrastructure."""
    normalized = detail.strip().casefold()
    infrastructure_markers = (
        "artifact download",
        "screener error",
        "could not resolve published port",
        "cannot connect to the docker daemon",
        "docker daemon",
    )
    if any(marker in normalized for marker in infrastructure_markers):
        return AgentStatus.SCREENING_FAILED
    return AgentStatus.REJECTED


def _quarantine_payload_json(
    payload: ScreenResultRequest,
) -> tuple[list[dict] | None, dict | None]:
    """JSON-encode the bounded review payloads carried on a quarantine verdict."""
    evidence_json = (
        [item.model_dump(mode="json") for item in payload.evidence]
        if payload.evidence
        else None
    )
    if payload.adjudication is not None:
        adjudication = payload.adjudication
        adjudication_evidence = {
            "module_id": "adjudication",
            "code": (
                "adjudicated-source-review-"
                + (adjudication.decision if adjudication.decision else "unknown")
            ),
            "summary": adjudication.reason[:240],
            "digest": payload.adjudication_digest,
        }
        evidence_json = [*(evidence_json or [])[:15], adjudication_evidence]
    finding_json = (
        payload.finding.model_dump(mode="json") if payload.finding is not None else None
    )
    return evidence_json, finding_json


async def _backfill_quarantine_payloads(
    session: AsyncSession,
    *,
    attempt_id: UUID,
    payload: ScreenResultRequest,
) -> None:
    """Backfill review payloads onto an existing quarantine, never rewriting.

    A re-reported verdict may carry payloads an older worker, an earlier
    retry, or an older platform build did not persist. Only null fields are
    filled, and a finding is only accepted for the digest the original signed
    verdict bound.
    """
    evidence_json, finding_json = _quarantine_payload_json(payload)
    audit_json = (
        payload.review_audit.model_dump(mode="json")
        if payload.review_audit is not None
        else None
    )
    if evidence_json is None and finding_json is None and audit_json is None:
        return
    quarantine = await session.scalar(
        select(ScreeningQuarantine).where(ScreeningQuarantine.attempt_id == attempt_id)
    )
    if quarantine is None:
        return
    if audit_json is not None and payload.review_audit_digest is not None:
        if quarantine.review_audit is None and quarantine.review_audit_digest is None:
            quarantine.review_audit = audit_json
            quarantine.review_audit_digest = payload.review_audit_digest
        elif (
            quarantine.review_audit != audit_json
            or quarantine.review_audit_digest != payload.review_audit_digest
        ):
            raise AgentNotScreenableError(
                "re-reported review audit conflicts with retained evidence"
            )
    if quarantine.evidence is None and evidence_json:
        quarantine.evidence = evidence_json
    if (
        quarantine.finding is None
        and finding_json
        and quarantine.finding_digest == payload.finding_digest
    ):
        quarantine.finding = finding_json


@router.post(
    "/agent/{agent_id}/result",
    response_model=ScreenResultResponse,
    responses={
        401: {"description": "Invalid screener credentials or signature."},
        404: {"description": "No agent with the given id."},
        409: {"description": "Agent is past the screening stage."},
        422: {"description": "Malformed request body or UUID path parameter."},
    },
)
async def submit_result(
    agent_id: UUID,
    payload: ScreenResultRequest,
    screener_hotkey: ScreenerDep,
    response: Response,
    chain: ChainDep,
    session: SessionDep,
    generator: GeneratorDep,
    storage: StorageDep,
) -> ScreenResultResponse:
    """Record the screener's verdict and advance the agent's lifecycle.

    Ordering is cheap-before-expensive; no DB write happens until every check
    passes: (1) dedicated screener bearer authentication, (2) signature over
    the versioned verdict, (3) generate the per-submission
    dataset (pass + generation enabled), (4) one transaction that promotes
    ``uploaded -> evaluating`` (pass, pinning the dataset) or ``uploaded ->
    screening_failed``.

    The dataset generation (3) is a network call to the private generate service;
    it runs BEFORE the row-lock transaction (never hold a lock across I/O) and
    only when the agent isn't already pinned, so a re-reported verdict doesn't
    regenerate. If generation fails it raises and the agent is NOT promoted — the
    verdict can be retried — so an evaluating agent always has a scoreable dataset.
    """
    response.headers["Cache-Control"] = "no-store"
    screener_policy = await _required_policy(session)
    required_policy = screener_policy.required_policy_version

    if payload.screener_hotkey != screener_hotkey:
        raise ScreenerAuthError("payload hotkey does not match authenticated screener")
    if payload.policy_version >= 9 and payload.outcome is None:
        raise AgentNotScreenableError("policy-9 verdicts require a typed outcome")
    image_upload_id = payload.image_upload_id

    # Signature proves the screener owns the hotkey and binds THIS verdict:
    #    ``passed`` is signed, so a captured result can't be replayed with the
    #    boolean flipped to grief (or unfairly promote) a miner.
    if payload.outcome is not None:
        signed = verdict_signing_message(
            screener_hotkey=payload.screener_hotkey,
            agent_id=agent_id,
            attempt_id=payload.attempt_id,
            passed=payload.passed,
            policy_version=payload.policy_version,
            outcome=payload.outcome,
            manifest_digest=payload.manifest_digest,
            finding_digest=payload.finding_digest,
            review_audit_digest=payload.review_audit_digest,
            adjudication_digest=payload.adjudication_digest,
            deferred_source_review=payload.deferred_source_review,
            policy_only=payload.policy_only,
            review_settings_revision=payload.review_settings_revision,
            review_settings_instance_id=payload.review_settings_instance_id,
            review_settings_scope=payload.review_settings_scope,
            review_settings_checksum=payload.review_settings_checksum,
            reason_code=payload.reason_code,
            image_sha256=payload.image_sha256,
            image_size_bytes=payload.image_size_bytes,
            image_id=payload.image_id,
            image_ref=payload.image_ref,
            image_upload_id=image_upload_id,
        )
    elif payload.attempt_id is not None:
        signed = verdict_signing_message(
            screener_hotkey=payload.screener_hotkey,
            agent_id=agent_id,
            attempt_id=payload.attempt_id,
            passed=payload.passed,
            policy_version=payload.policy_version,
        )
    elif payload.policy_version >= _FIRST_VERSIONED_POLICY:
        signed = verdict_signing_message(
            screener_hotkey=payload.screener_hotkey,
            agent_id=agent_id,
            passed=payload.passed,
            policy_version=payload.policy_version,
        )
    else:
        signed = f"{payload.screener_hotkey}:{agent_id}:{payload.passed}".encode()
    if not _verify_signature(payload.screener_hotkey, signed, payload.signature):
        raise ScreenerAuthError(
            f"verdict signature did not verify for hotkey {payload.screener_hotkey}"
        )

    # A legacy worker may still report a failure during a rolling deploy, but it
    # can never promote a submission without attesting the required policy —
    # which is the scheduled-activation version, not merely the newest one the
    # build ships.
    if payload.passed and payload.policy_version != required_policy:
        raise AgentNotScreenableError(
            f"passing verdict requires screening policy {required_policy}"
        )
    if (
        payload.reason_code == "exact-cross-miner-duplicate"
        and payload.outcome != ScreenResultOutcome.DETERMINISTIC_REJECT
    ):
        raise AgentNotScreenableError(
            "exact duplicate precheck requires a deterministic rejection"
        )

    verified_upload: ScreenedImageUpload | None = None
    if payload.image_sha256 is not None:
        if image_upload_id is None:
            raise AgentNotScreenableError(
                "passing screened image is missing its upload identity"
            )
        async with session.begin():
            verified_upload = await session.get(ScreenedImageUpload, image_upload_id)
        if (
            verified_upload is None
            or verified_upload.status != "verified"
            or verified_upload.agent_id != agent_id
            or verified_upload.attempt_id != payload.attempt_id
            or verified_upload.screener_hotkey != screener_hotkey
            or verified_upload.sha256 != payload.image_sha256
            or verified_upload.size_bytes != payload.image_size_bytes
            or verified_upload.image_id != payload.image_id
            or verified_upload.image_ref != payload.image_ref
            or verified_upload.verified_at is None
        ):
            raise AgentNotScreenableError(
                "screened image was not verified for this screening attempt"
            )
        key = _screened_image_key(agent_id, image_upload_id)
        try:
            stored_image = await storage.head_object(key=key)
        except ObjectNotFoundError as error:
            raise AgentNotScreenableError(
                "verified screened image is missing from storage"
            ) from error
        if stored_image.size_bytes != payload.image_size_bytes:
            raise AgentNotScreenableError(
                "stored screened image size changed after verification"
            )

    outcome_value = payload.outcome.value if payload.outcome is not None else None
    stored_reason_code = (
        f"adjudicated-source-review-{payload.adjudication.decision}"
        if payload.adjudication is not None
        else payload.reason_code
    )
    records_review_evidence = (
        outcome_value
        in {
            "pass_inconclusive",
            "inconclusive",
            "quarantine",
        }
        or payload.adjudication is not None
    )
    deferred_review: AthReview | None = None
    reported_attempt: ScreeningAttempt | None = None
    current_agent: Agent | None = None
    deferred_attempt_lifecycle = False
    deferred_deep_attempt = False
    restore_status = AgentStatus.SCORED
    if payload.attempt_id is not None:
        async with session.begin():
            reported_attempt = await get_screening_attempt(
                session, attempt_id=payload.attempt_id
            )
            current_agent = await get_agent_by_id(session, agent_id=agent_id)
            if current_agent is not None:
                deferred_review = await session.scalar(
                    select(AthReview).where(
                        AthReview.agent_id == agent_id,
                        AthReview.algorithm_provenance["review_kind"].as_string()
                        == DEFERRED_REVIEW_KIND,
                    )
                )
            review_started_at = (
                deferred_review.reopened_at or deferred_review.opened_at
                if deferred_review is not None
                else None
            )
            deferred_attempt_lifecycle = bool(
                reported_attempt is not None
                and not reported_attempt.build_only
                and review_started_at is not None
                and reported_attempt.started_at >= review_started_at
            )
            deferred_deep_attempt = bool(
                deferred_attempt_lifecycle
                and current_agent is not None
                and current_agent.status == AgentStatus.ATH_PENDING_REVIEW
                and deferred_review is not None
                and deferred_review.status == "pending"
            )
            if deferred_review is not None:
                previous = deferred_review.original_evidence.get("previous_status")
                if previous == AgentStatus.LIVE.value:
                    restore_status = AgentStatus.LIVE

    deferred_mechanical_admission = bool(
        reported_attempt is not None
        and reported_attempt.build_only
        and reported_attempt.reason_code == DEFERRED_MECHANICAL_REASON
    )
    policy_only_rescreen = bool(
        reported_attempt is not None
        and reported_attempt.reason_code == POLICY_ONLY_RESCREEN_REASON
    )
    if (
        reported_attempt is not None
        and payload.deferred_source_review != deferred_mechanical_admission
    ):
        raise AgentNotScreenableError(
            "verdict execution mode does not match the claimed screening attempt"
        )
    # A worker from before the policy-only contract ignores the unknown queue
    # field and performs a normal full screen. Permit that conservative path
    # during a rolling deploy; its passing payload still carries a newly
    # verified image. Never permit the inverse replay (policy_only=true for an
    # attempt Platform did not mark for reuse).
    if payload.policy_only and not policy_only_rescreen:
        raise AgentNotScreenableError(
            "verdict policy-only mode does not match the claimed screening attempt"
        )
    if (
        policy_only_rescreen
        and payload.passed
        and (
            current_agent is None
            or any(
                value is None
                for value in (
                    current_agent.screened_image_sha256,
                    current_agent.screened_image_size_bytes,
                    current_agent.screened_image_id,
                    current_agent.screened_image_ref,
                    current_agent.screened_image_upload_id,
                    current_agent.screened_image_verified_at,
                )
            )
        )
    ):
        raise AgentNotScreenableError(
            "policy-only rescreen requires a retained verified screened image"
        )
    public_reason: str | None
    if deferred_deep_attempt and outcome_value == "pass":
        target = restore_status
        public_reason = None
    elif deferred_deep_attempt and outcome_value in {
        "pass_inconclusive",
        "inconclusive",
        "quarantine",
    }:
        # A completed deep review that cannot clear the artifact remains a
        # reward hold. It is terminal for this attempt: no inconclusive loop.
        target = AgentStatus.ATH_PENDING_REVIEW
        public_reason = "Deferred source review requires operator adjudication"
    elif (
        deferred_deep_attempt and payload.outcome == ScreenResultOutcome.RETRYABLE_INFRA
    ):
        target = AgentStatus.ATH_PENDING_REVIEW
        public_reason = "Deferred source review was interrupted; manual retry required"
    elif (
        deferred_deep_attempt
        and payload.outcome == ScreenResultOutcome.DETERMINISTIC_REJECT
        and payload.reason_code == "health-contract"
    ):
        # The immutable artifact already passed the score-first mechanical
        # admission and completed enough validator runs to qualify for this
        # source-only review. A later container-health miss on a different
        # screener host is useful operator evidence, but cannot honestly undo
        # those retained proofs or turn a pending source review into a miner
        # rejection. Keep the reward hold and park until an operator authorizes
        # another exact attempt.
        target = AgentStatus.ATH_PENDING_REVIEW
        public_reason = (
            "Deferred source review runtime verification was interrupted; "
            "manual retry required"
        )
    elif outcome_value == "pass_inconclusive":
        target = AgentStatus.EVALUATING
        public_reason = (
            "Mechanical admission passed; deep source review deferred until score "
            "qualification"
            if deferred_mechanical_admission
            else "Bounded source review exhausted; admitted for scoring"
        )
    elif payload.outcome == ScreenResultOutcome.INCONCLUSIVE:
        # Inconclusive remains a NON-verdict: it neither passes nor rejects the
        # submission. Persist the completed attempt and park it for an exact
        # operator-issued retry.
        target = AgentStatus.SCREENING_FAILED
        public_reason = "Screening was inconclusive; manual retry required"
    elif payload.outcome == ScreenResultOutcome.QUARANTINE:
        # A build-only attempt rebuilds an already-adjudicated submission's
        # prerequisites and runs no source review, so it must not be able to
        # re-quarantine — that would let a screener silently override the
        # operator release / prior pass that made it EVALUATING.
        if (
            reported_attempt is not None
            and reported_attempt.build_only
            and not (deferred_mechanical_admission and payload.deferred_source_review)
        ):
            raise AgentNotScreenableError(
                "a build-only screening attempt cannot quarantine"
            )
        target = AgentStatus.QUARANTINED
        public_reason = "Submission held for anti-cheat review"
    elif payload.outcome == ScreenResultOutcome.RETRYABLE_INFRA:
        # The wire value is retained for worker compatibility. Policy is now
        # fail-closed: the terminal attempt parks until an operator authorizes
        # one exact retry through Backroom.
        target = AgentStatus.SCREENING_FAILED
        public_reason = "Screening was interrupted; manual retry required"
    elif payload.outcome == ScreenResultOutcome.DETERMINISTIC_REJECT:
        target = AgentStatus.REJECTED
        public_reason = _public_screening_reason(payload.detail, payload.reason_code)
    else:
        # Legacy workers did not send typed pass/fail outcomes. Preserve their
        # detail-based behavior during rolling upgrades.
        target = (
            AgentStatus.EVALUATING
            if payload.passed
            else _failed_screening_target(payload.detail)
        )
        public_reason = (
            None
            if payload.passed
            else _public_screening_reason(payload.detail, payload.reason_code)
        )

    # 3. Generate the scoring-version dataset (outside the row lock). A new
    #    submission received after an open rollout starts enters that desired
    #    benchmark era immediately; older submissions remain on the active
    #    version unless they separately qualify for the rollout cohort.
    #    The legacy
    #    agent-level columns hold the original/v2 pin, so their presence does not
    #    prove a v3 BenchmarkDataset exists. A policy rescreen after activation
    #    must backfill that missing row from the same immutable seed or the agent
    #    is left evaluating forever with no ticket candidate.
    new_dataset: tuple[int, int, str, str, int | None, str | None] | None = None
    if payload.passed and generator.run_size is not None:
        # Own transaction so the read commits/closes before the write txn below
        # (a bare SELECT autobegins a transaction that would collide with it).
        async with session.begin():
            existing = await get_agent_by_id(session, agent_id=agent_id)
            if existing is None:
                raise AgentNotFoundError(f"no agent with id={agent_id}")
            bench_version = await arrival_bench_version(session, agent=existing)
            versioned_dataset = await session.get(
                BenchmarkDataset, (agent_id, bench_version)
            )
            needs_dataset = versioned_dataset is None
            existing_seed = existing.dataset_seed
            existing_seed_block = existing.dataset_seed_block
            existing_seed_block_hash = existing.dataset_seed_block_hash
        if needs_dataset:
            if existing_seed is None:
                seed, block_number, block_hash = await _derive_dataset_seed(
                    chain, agent_id
                )
            else:
                seed = existing_seed
                block_number = existing_seed_block
                block_hash = existing_seed_block_hash
            dataset_sha256 = await generator.generate(seed, bench_version=bench_version)
            new_dataset = (
                bench_version,
                seed,
                dataset_sha256,
                generator.run_size,
                block_number,
                block_hash,
            )

    # 4. Atomic: apply the verdict + pin the dataset. The row lock serializes
    #    concurrent verdicts so the status guard + transition can't be lost-updated.
    async with session.begin():
        agent = await get_agent_by_id(session, agent_id=agent_id, for_update=True)
        if agent is None:
            raise AgentNotFoundError(f"no agent with id={agent_id}")
        attempt: ScreeningAttempt | None = None
        attempt_status = (
            "passed"
            if deferred_attempt_lifecycle
            and outcome_value in {"pass", "pass_inconclusive", "inconclusive"}
            else "expired"
            if payload.outcome == ScreenResultOutcome.INCONCLUSIVE
            else "quarantined"
            if target == AgentStatus.QUARANTINED or outcome_value == "quarantine"
            else "passed"
            if payload.passed
            else ("rejected" if target == AgentStatus.REJECTED else "failed")
        )
        if payload.attempt_id is not None:
            attempt = await get_screening_attempt(
                session, attempt_id=payload.attempt_id, for_update=True
            )
            if (
                attempt is None
                or attempt.agent_id != agent_id
                or attempt.screener_hotkey != screener_hotkey
                or attempt.policy_version != payload.policy_version
            ):
                raise AgentNotScreenableError(
                    "verdict does not match the claimed screening attempt"
                )
            binding = (
                payload.review_settings_revision,
                payload.review_settings_instance_id,
                payload.review_settings_scope,
                payload.review_settings_checksum,
            )
            if all(value is not None for value in binding):
                revision = await session.get(
                    ScreenerReviewSettingsRevision,
                    payload.review_settings_revision,
                )
                if revision is None:
                    raise AgentNotScreenableError(
                        "verdict references an unknown reviewer settings revision"
                    )
                settings = ScreenerReviewSettings.model_validate(revision.settings)
                if (
                    revision.scope != payload.review_settings_scope
                    or revision.checksum != payload.review_settings_checksum
                    or revision.scope not in {"*", payload.review_settings_instance_id}
                ):
                    raise AgentNotScreenableError(
                        "verdict reviewer settings binding does not match "
                        "platform state"
                    )
            else:
                settings = ScreenerReviewSettings()
            claimed_binding = (
                attempt.review_settings_revision,
                attempt.review_settings_instance_id,
                attempt.review_settings_scope,
                attempt.review_settings_checksum,
            )
            if all(value is not None for value in claimed_binding):
                if binding != claimed_binding:
                    raise AgentNotScreenableError(
                        "verdict reviewer settings binding does not match the claim"
                    )
                effective_settings = settings
            else:
                # Compatibility for attempts claimed by a worker that predates
                # claim-time settings binding. Once a claim is bound, later
                # global revisions cannot rewrite its execution contract.
                latest_rows = (
                    await session.scalars(
                        select(ScreenerReviewSettingsRevision)
                        .where(
                            ScreenerReviewSettingsRevision.scope.in_(
                                ("*", payload.review_settings_instance_id or "")
                            )
                        )
                        .order_by(ScreenerReviewSettingsRevision.revision.desc())
                    )
                ).all()
                effective_settings = ScreenerReviewSettings()
                effective = next(
                    (
                        row
                        for row in latest_rows
                        if row.scope == payload.review_settings_instance_id
                    ),
                    next((row for row in latest_rows if row.scope == "*"), None),
                )
                if effective is not None:
                    effective_settings = ScreenerReviewSettings.model_validate(
                        effective.settings
                    )
                    if effective_settings.mode == "inherit":
                        effective = next(
                            (row for row in latest_rows if row.scope == "*"), None
                        )
                        effective_settings = (
                            ScreenerReviewSettings.model_validate(effective.settings)
                            if effective is not None
                            else ScreenerReviewSettings()
                        )
                    if (
                        effective is not None
                        and effective_settings.mode == "enforce"
                        and (
                            payload.review_settings_revision != effective.revision
                            or payload.review_settings_checksum != effective.checksum
                            or settings.mode != "enforce"
                        )
                    ):
                        raise AgentNotScreenableError(
                            "enforced reviewer verdict is missing the effective "
                            "settings binding"
                        )
            if (
                payload.adjudication is not None
                and payload.adjudication.decision == "reject"
                and effective_settings.adjudicator_mode == "enforce"
            ):
                # The worker transports a reject as a quarantine because a
                # policy module cannot ban a miner. Platform owns the final
                # authority boundary: execute only after verifying the exact
                # operator posture bound to the claim and signed verdict above.
                target = AgentStatus.REJECTED
                public_reason = payload.adjudication.reason
                attempt_status = "rejected"
            if attempt.reason_code == "exact-cross-miner-duplicate" and (
                payload.reason_code != attempt.reason_code
            ):
                raise AgentNotScreenableError(
                    "verdict does not match the platform precheck disposition"
                )
            deadline = attempt.deadline
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            if attempt.status == attempt_status and (
                agent.status == target
                or (
                    attempt_status == "passed"
                    and agent.status in (AgentStatus.SCORED, AgentStatus.LIVE)
                )
            ):
                # Idempotent re-report: nothing transitions, but a retry may
                # carry review payloads that an earlier report (or an older
                # platform build) did not persist. Backfill them before
                # returning or they would be unrecoverable for this attempt.
                if records_review_evidence:
                    await _backfill_quarantine_payloads(
                        session, attempt_id=attempt.attempt_id, payload=payload
                    )
                result_status = agent.status
                return ScreenResultResponse(
                    agent_id=agent_id, status=result_status, accepted=True
                )
            if attempt.status != "running" or datetime.now(UTC) > deadline:
                raise AgentNotScreenableError(
                    "screening attempt is expired or already completed"
                )
        deferred_review_active_now = False
        if deferred_attempt_lifecycle:
            deferred_review = await session.scalar(
                select(AthReview)
                .where(
                    AthReview.agent_id == agent_id,
                    AthReview.algorithm_provenance["review_kind"].as_string()
                    == DEFERRED_REVIEW_KIND,
                )
                .with_for_update()
            )
            deferred_review_active_now = bool(
                deferred_review is not None
                and deferred_review.status == "pending"
                and agent.status == AgentStatus.ATH_PENDING_REVIEW
            )
        late_deferred_result = (
            deferred_attempt_lifecycle and not deferred_review_active_now
        )
        rescreening = (
            agent.status
            in (
                AgentStatus.EVALUATING,
                AgentStatus.SCREENING_FAILED,
                AgentStatus.REJECTED,
            )
            and agent.screening_policy_version < required_policy
        )
        # A due activation that opted scored/live rows in: the agent is being
        # rescreened in place while it stays on the ledger (mirrors the
        # rolling-rollout rescreen below).
        stale_scored_rescreen = (
            required_policy > screener_policy.floor_policy_version
            and screener_policy.rescreen_scored
            and agent.status in (AgentStatus.SCORED, AgentStatus.LIVE)
            and agent.screening_policy_version < required_policy
        )
        rolling_rescreen = bool(
            await session.scalar(
                select(
                    exists().where(
                        BenchmarkRolloutMember.agent_id == agent.agent_id,
                        BenchmarkRolloutMember.rollout_id
                        == BenchmarkRollout.rollout_id,
                        BenchmarkRollout.status.in_(
                            ("collecting", "blocked_ineligible")
                        ),
                    )
                )
            )
        ) and agent.status in (AgentStatus.SCORED, AgentStatus.LIVE)
        idempotent = agent.status == target
        if late_deferred_result:
            # An operator resolution is authoritative. A screener response that
            # was already in flight may finish after clear/reject; retain its
            # signed attempt evidence below, but never resurrect the hold or
            # overwrite the operator-selected status/reason.
            pass
        elif deferred_deep_attempt:
            agent.status = target
        elif (rolling_rescreen or stale_scored_rescreen) and payload.passed:
            pass
        elif (
            rolling_rescreen
            or stale_scored_rescreen
            or agent.status in (_SCREENABLE_STATUSES)
            or rescreening
        ):
            agent.status = target
        elif agent.status == target:
            pass  # idempotent re-report of the same verdict
        else:
            raise AgentNotScreenableError(
                f"agent {agent_id} is {agent.status}, cannot apply verdict "
                f"passed={payload.passed} (target {target})"
            )
        if not late_deferred_result:
            agent.screening_reason = public_reason
            agent.screening_reason_code = (
                stored_reason_code
                if outcome_value in {"pass_inconclusive", "inconclusive", "quarantine"}
                else None
                if payload.passed
                else stored_reason_code
            )
        if late_deferred_result:
            pass
        elif payload.reason_code == "exact-cross-miner-duplicate":
            if attempt is None or attempt.duplicate_of is None:
                raise AgentNotScreenableError(
                    "exact duplicate verdict requires a platform precheck"
                )
            agent.duplicate_of = attempt.duplicate_of
        elif payload.passed:
            agent.duplicate_of = None
        # Persist the policy that produced either terminal verdict. Any later
        # screening run requires an explicit operator authorization or a due
        # scheduled activation's cohort-wide rescreen; infrastructure failures
        # remain retryable under the same policy.
        if not late_deferred_result and payload.policy_version == required_policy:
            agent.screening_policy_version = payload.policy_version
        if payload.passed and not late_deferred_result and not payload.policy_only:
            agent.screened_image_sha256 = payload.image_sha256
            agent.screened_image_size_bytes = payload.image_size_bytes
            agent.screened_image_id = payload.image_id
            agent.screened_image_ref = payload.image_ref
            agent.screened_image_upload_id = image_upload_id
            agent.screened_image_verified_at = (
                verified_upload.verified_at if verified_upload is not None else None
            )
        if (
            deferred_deep_attempt
            and deferred_review_active_now
            and outcome_value == "pass"
        ):
            assert deferred_review is not None
            cleared_at = datetime.now(UTC)
            deferred_review.status = "resolved"
            deferred_review.resolved_at = cleared_at
            deferred_review.resolved_by = "platform:deferred-source-review"
            deferred_review.resolution = "clear"
            deferred_review.resolution_reason = (
                "Deferred source review completed without a decisive finding"
            )
            agent.review_reason = None
            agent.duplicate_of = None
            session.add(
                AthReviewAction(
                    action_id=uuid4(),
                    review_id=deferred_review.review_id,
                    action="clear",
                    reason=deferred_review.resolution_reason,
                    actor=deferred_review.resolved_by,
                    evidence={
                        "sha256": agent.sha256,
                        "score_count": int(
                            deferred_review.original_evidence.get("score_count", 0)
                        ),
                        "previous_status": deferred_review.original_evidence.get(
                            "previous_status", AgentStatus.SCORED.value
                        ),
                    },
                    created_at=cleared_at,
                )
            )
        if attempt is None and not idempotent:
            now = datetime.now(UTC)
            attempt = ScreeningAttempt(
                attempt_id=payload.attempt_id or UUID(int=secrets.randbits(128)),
                agent_id=agent_id,
                screener_hotkey=screener_hotkey,
                policy_version=payload.policy_version,
                status=attempt_status,
                started_at=now,
                deadline=now,
                finished_at=now,
                public_reason=public_reason,
            )
            session.add(attempt)
        elif attempt is not None:
            attempt.status = attempt_status
            attempt.finished_at = datetime.now(UTC)
            attempt.public_reason = public_reason
            if attempt.reason_code not in {
                DEFERRED_MECHANICAL_REASON,
                POLICY_ONLY_RESCREEN_REASON,
            }:
                attempt.reason_code = stored_reason_code
            attempt.review_settings_revision = payload.review_settings_revision
            attempt.review_settings_instance_id = payload.review_settings_instance_id
            attempt.review_settings_scope = payload.review_settings_scope
            attempt.review_settings_checksum = payload.review_settings_checksum
        if target == AgentStatus.QUARANTINED or records_review_evidence:
            if attempt is None:
                raise AgentNotScreenableError(
                    "source-review evidence requires a claimed screening attempt"
                )
            if target == AgentStatus.QUARANTINED and (
                payload.manifest_digest is None or payload.reason_code is None
            ):
                raise AgentNotScreenableError(
                    "quarantine result is missing bounded evidence"
                )
            # The review payloads were digest/bounds-validated at parse time
            # (the finding must hash to the signed finding_digest).
            evidence_json, finding_json = _quarantine_payload_json(payload)
            existing_quarantine = await session.scalar(
                select(ScreeningQuarantine).where(
                    ScreeningQuarantine.attempt_id == attempt.attempt_id
                )
            )
            if existing_quarantine is None:
                evidence_deferred = target != AgentStatus.QUARANTINED or (
                    deferred_attempt_lifecycle
                )
                resolved_at = datetime.now(UTC) if evidence_deferred else None
                session.add(
                    ScreeningQuarantine(
                        quarantine_id=uuid4(),
                        agent_id=agent_id,
                        attempt_id=attempt.attempt_id,
                        screener_hotkey=screener_hotkey,
                        policy_version=payload.policy_version,
                        manifest_digest=payload.manifest_digest
                        or hashlib.sha256(
                            b"ditto:deferred-source-review:inconclusive:v1"
                        ).hexdigest(),
                        finding_digest=payload.finding_digest,
                        review_audit_digest=payload.review_audit_digest,
                        review_audit=(
                            payload.review_audit.model_dump(mode="json")
                            if payload.review_audit is not None
                            else None
                        ),
                        reason_code=stored_reason_code or INCONCLUSIVE_REASON_CODE,
                        evidence=evidence_json,
                        finding=finding_json,
                        status="resolved" if evidence_deferred else "active",
                        resolved_at=resolved_at,
                        resolved_by=(
                            "platform:deferred-source-review"
                            if evidence_deferred
                            else None
                        ),
                        resolution="rescreen" if evidence_deferred else None,
                        resolution_reason=(
                            payload.adjudication.reason
                            if payload.adjudication is not None
                            else (
                                "Late deep-review evidence retained after operator "
                                "action"
                            )
                            if late_deferred_result
                            else "Evidence retained on the score-qualified ATH review"
                            if deferred_attempt_lifecycle
                            else "Deep source review deferred until score qualification"
                            if deferred_mechanical_admission
                            else "Bounded source review exhausted; admitted for scoring"
                        )
                        if evidence_deferred
                        else None,
                    )
                )
            else:
                await _backfill_quarantine_payloads(
                    session, attempt_id=attempt.attempt_id, payload=payload
                )
            if (
                deferred_review is not None
                and deferred_deep_attempt
                and deferred_review_active_now
            ):
                deferred_snapshot = deferred_review.original_evidence.get(
                    "deferred_review", {}
                )
                deferred_review.original_evidence = {
                    **deferred_review.original_evidence,
                    "deferred_review": {
                        **(
                            deferred_snapshot
                            if isinstance(deferred_snapshot, dict)
                            else {}
                        ),
                        "screening_reason_code": stored_reason_code,
                        "review_audit_digest": payload.review_audit_digest,
                        "review_audit": (
                            payload.review_audit.model_dump(mode="json")
                            if payload.review_audit is not None
                            else None
                        ),
                    },
                    "deep_review_result": {
                        "attempt_id": str(attempt.attempt_id),
                        "outcome": outcome_value,
                        "reason_code": stored_reason_code,
                        "manifest_digest": payload.manifest_digest,
                        "finding_digest": payload.finding_digest,
                        "review_audit_digest": payload.review_audit_digest,
                        "review_audit": (
                            payload.review_audit.model_dump(mode="json")
                            if payload.review_audit is not None
                            else None
                        ),
                    },
                }
        # Pin the generated version row once. Locking the agent serializes two
        # verdicts; the second lookup prevents a duplicate insert if another
        # path backfilled the row while generation was in flight.
        if (
            new_dataset is not None
            and not late_deferred_result
            and agent.status
            in (
                AgentStatus.EVALUATING,
                AgentStatus.SCORED,
                AgentStatus.LIVE,
            )
        ):
            (
                bench_version,
                seed,
                dataset_sha256,
                dataset_run_size,
                seed_block,
                seed_block_hash,
            ) = new_dataset
            existing_versioned = await session.get(
                BenchmarkDataset, (agent.agent_id, bench_version)
            )
            if existing_versioned is None:
                session.add(
                    BenchmarkDataset(
                        agent_id=agent.agent_id,
                        bench_version=bench_version,
                        seed=seed,
                        sha256=dataset_sha256,
                        run_size=dataset_run_size,
                        seed_block=seed_block,
                        seed_block_hash=seed_block_hash,
                    )
                )
            # First-time submissions still mirror their initial pin into the
            # compatibility columns. Never overwrite an older/v2 pin during a
            # v3 backfill.
            if agent.dataset_seed is None:
                agent.dataset_seed = seed
                agent.dataset_sha256 = dataset_sha256
                agent.dataset_run_size = dataset_run_size
                agent.dataset_seed_block = seed_block
                agent.dataset_seed_block_hash = seed_block_hash
        result_status = agent.status

    try:
        await refresh_rolling_qualification(
            session, generator=generator, now=datetime.now(UTC)
        )
    except Exception:
        # The signed verdict and image binding are already committed. A dataset
        # renderer outage must not make the screener retry an accepted verdict.
        logger.exception("rolling benchmark qualification refresh failed")

    logger.info(
        "screen verdict agent_id=%s screener=%s passed=%s status=%s dataset=%s "
        "reason=%r",
        agent_id,
        payload.screener_hotkey,
        payload.passed,
        result_status,
        "pinned" if new_dataset is not None else "none",
        agent.screening_reason,
    )
    return ScreenResultResponse(agent_id=agent_id, status=result_status, accepted=True)
