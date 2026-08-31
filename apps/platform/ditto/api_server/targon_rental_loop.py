"""Create Targon screening rentals from the Platform API process.

This replaces the separate capacity-controller / image-builder host. One GCE
VM (Platform) admits work, launches rentals, and attests verdicts.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_server.builder_image import is_digest_pinned_image
from ditto.api_server.config import TargonRentalConfig
from ditto.api_server.screening_provider import (
    BuildSpec,
    ProvisionObservation,
    ReviewSpec,
    ScreeningComputeProvider,
    ScreeningProviderError,
    SmokeSpec,
    inflight_failure_code,
)
from ditto.api_server.screening_traces import (
    SCREENING_TRACE_SCHEMA,
    PutTrace,
    encode_screening_trace,
    screening_trace_key,
)
from ditto.api_server.targon_provider import TargonComputeProvider, TargonRentals
from ditto.api_server.targon_screening import admit_targon_screening_work
from ditto.db.models import (
    ProviderOutageCircuit,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
    SubmissionImageBuild,
    SubmissionSourceReview,
    TrustedImageBuild,
)
from ditto.db.queries.provider_outages import (
    OPENROUTER_PROVIDER,
    lock_provider_work_gate,
    register_provider_probe,
)
from ditto.db.queries.screener_provider_settings import (
    resolve_screener_provider_settings,
)
from ditto_screening_protocol.private_failure import (
    private_failure_text as _private_failure_text,
)

logger = logging.getLogger(__name__)

_BUILD_LEASE = timedelta(minutes=50)
_JOB_TTL = timedelta(minutes=80)
_SOURCE_LEASE = timedelta(minutes=30)
_REAP_LIMIT = 16
_TERMINAL_JOB = ("succeeded", "consumed", "canceled", "fallback_required")
_TERMINAL_RUNTIME = ("succeeded", "fallback_required", "skipped")
_INFLIGHT_JOB = ("leased", "running")
_PROVIDER_TERMINAL = frozenset({"error", "deleted", "suspended"})
_TARGON_BUILD_FALLBACK_CODES = frozenset(
    {"TARGON_PROVISION_ERROR", "TARGON_PROVISION_TIMEOUT"}
)
_CANDIDATE_REGISTRY = (
    "us-central1-docker.pkg.dev/ditto-app-dev/ditto-screening-candidates/miner"
)


async def _default_health_probe(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}/health")
    except httpx.HTTPError:
        return False
    return 200 <= response.status_code < 300


PromoteArchive = Callable[[str, str, str], Awaitable[str]]
MintToken = Callable[[str], Awaitable[str]]


def _source_review_layer_env(
    settings: ScreenerReviewSettings,
) -> tuple[tuple[str, str], ...]:
    """Pin L1/L2/L3 knobs on the one-shot rental so GCE is not required."""
    return (
        (
            "SCREENER_L2_REVIEW_MODE",
            settings.mode if settings.mode != "inherit" else "off",
        ),
        ("SCREENER_L2_REVIEW_MODEL", settings.l2_model),
        ("SCREENER_L2_FALLBACK_MODELS", ",".join(settings.l2_fallback_models)),
        ("SCREENER_L3_REVIEW_ENABLED", "true" if settings.l3_enabled else "false"),
        ("SCREENER_L3_REVIEW_MODEL", settings.l3_model),
        ("SCREENER_L2_TIMEOUT_SECONDS", str(int(settings.timeout_seconds))),
        ("SCREENER_L2_MAX_STEPS", str(int(settings.max_steps))),
        (
            "SCREENER_SOURCE_REVIEW_MAX_STEPS",
            str(int(settings.source_review_max_steps)),
        ),
        (
            "SCREENER_SOURCE_REVIEW_MAX_READ_BYTES",
            str(int(settings.source_review_max_read_bytes)),
        ),
        (
            "SCREENER_SOURCE_REVIEW_MAX_COMPLETION_TOKENS",
            str(int(settings.source_review_max_completion_tokens)),
        ),
        (
            "SCREENER_SOURCE_REVIEW_REASONING_EFFORT",
            settings.source_review_reasoning_effort,
        ),
        ("SCREENER_SOURCE_REVIEW_MODEL", settings.source_review_model),
        ("SCREENER_L2_MAX_INPUT_TOKENS", str(int(settings.max_input_tokens))),
        ("SCREENER_L2_MAX_OUTPUT_TOKENS", str(int(settings.max_output_tokens))),
        (
            "SCREENER_L2_MAX_COMPLETION_TOKENS",
            str(int(settings.max_completion_tokens)),
        ),
        ("SCREENER_L2_MAX_COST_USD", str(settings.max_cost_usd)),
        ("SCREENER_L2_CRITIC_REASONING_EFFORT", settings.critic_reasoning_effort),
        ("SCREENER_L2_CACHE_TTL_SECONDS", str(int(settings.cache_ttl_seconds))),
        ("SCREENER_L2_AUDIT_RETENTION_DAYS", str(int(settings.audit_retention_days))),
        (
            "SCREENER_REVIEW_CONCERN_HOLD_COUNT",
            str(int(settings.concern_hold_count)),
        ),
        ("SCREENER_REVIEW_CLEAR_MIN_NOTES", str(int(settings.clear_min_notes))),
        ("SCREENER_ADJUDICATOR_MODE", settings.adjudicator_mode),
        ("SCREENER_ADJUDICATOR_MODEL", settings.adjudicator_model),
        (
            "SCREENER_ADJUDICATOR_MAX_STEPS",
            str(int(settings.adjudicator_max_steps)),
        ),
        (
            "SCREENER_ADJUDICATOR_TIMEOUT_SECONDS",
            str(int(settings.adjudicator_timeout_seconds)),
        ),
    )


class TargonRentalLoop:
    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        config: TargonRentalConfig,
        targon: TargonRentals | None = None,
        screener_hotkey: str,
        promote_archive: PromoteArchive | None = None,
        mint_token: MintToken | None = None,
        health_probe: Callable[[str], Awaitable[bool]] | None = None,
        interval_seconds: float | None = None,
        providers: Sequence[ScreeningComputeProvider] | None = None,
        complete_screen: Callable[[UUID], Awaitable[None]] | None = None,
        resolve_builder_image: Callable[[str], str] | None = None,
        storage: object | None = None,
        traces_put: PutTrace | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._config = config
        self._screener_hotkey = screener_hotkey
        self._promote_archive = promote_archive
        self._mint_token = mint_token
        probe = health_probe or _default_health_probe
        self._health_probe = probe
        if providers is None:
            if targon is None:
                raise ValueError("targon or providers is required")
            providers = [TargonComputeProvider(targon, config, health_probe=probe)]
        self._providers = list(providers)
        self._interval_seconds = interval_seconds or config.interval_seconds
        self._complete_screen = complete_screen
        self._resolve_builder_image = resolve_builder_image or (lambda image: image)
        self._storage = storage
        self._traces_put = traces_put
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._epoch = "platform-targon-loop"

    def _builder_image(self) -> str:
        # Resolve on every launch. The requested ``:sha-{commit}`` tag is
        # published after Platform boots, and caching the latest fallback
        # keeps the previous Kaniko helper for the whole process lifetime.
        return self._resolve_builder_image(self._config.submission_builder_image)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="targon-rental-loop")

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("targon rental loop tick failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue

    async def tick(self) -> bool:
        """Admit work and launch at most one job per lane. Returns if any ran."""
        handled = await self._reap_finished_rentals()
        handled = await self._park_source_reviews_for_outage() or handled
        async with self._session_maker() as session, session.begin():
            _, provider_settings = await resolve_screener_provider_settings(
                session, environment=self._config.environment
            )
            archive_exists = getattr(self._storage, "object_exists", None)
            await admit_targon_screening_work(
                session,
                screener_hotkey=self._screener_hotkey,
                environment=self._config.environment,
                now=datetime.now(UTC),
                archive_exists=archive_exists,
            )
        if provider_settings.build_provider_priority[0] == "targon":
            handled = await self._launch_kaniko() or handled
        if provider_settings.runtime_provider_priority[0] == "targon":
            handled = await self._launch_smoke() or handled
        if provider_settings.source_review_provider_priority[0] == "targon":
            handled = await self._launch_source_review() or handled
        handled = await self._finalize_ready_attempts() or handled
        handled = await self._repair_kaniko_image_ids() or handled
        return handled

    async def _park_source_reviews_for_outage(self) -> bool:
        """Delete and requeue every non-probe court rental while the relay is open."""
        now = datetime.now(UTC)
        pending: list[tuple[UUID, str, str | None]] = []
        async with self._session_maker() as session, session.begin():
            circuit = await session.scalar(
                select(ProviderOutageCircuit)
                .where(ProviderOutageCircuit.provider == OPENROUTER_PROVIDER)
                .with_for_update()
            )
            if circuit is None or circuit.state != "open":
                return False
            probe_key = (
                circuit.probe_key
                if circuit.probe_kind == "screening"
                and circuit.probe_expires_at is not None
                and (
                    circuit.probe_expires_at
                    if circuit.probe_expires_at.tzinfo is not None
                    else circuit.probe_expires_at.replace(tzinfo=UTC)
                )
                > now
                else None
            )
            rows = list(
                await session.scalars(
                    select(SubmissionSourceReview)
                    .where(
                        or_(
                            SubmissionSourceReview.status.in_(_INFLIGHT_JOB),
                            and_(
                                SubmissionSourceReview.status == "queued",
                                SubmissionSourceReview.provider_resource_id.is_not(
                                    None
                                ),
                            ),
                        )
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                if probe_key == str(row.review_id):
                    continue
                if row.provider_resource_id is not None:
                    pending.append(
                        (row.review_id, row.provider_resource_id, row.provider)
                    )
                row.status = "queued"
                # A logical source review gets one no-fault outage resume in
                # its lifetime, not one per circuit epoch.  The latter turns a
                # flapping provider into an unbounded model-spend reset.
                row.provider_outage_epoch = (
                    circuit.epoch
                    if row.provider_outage_attempted_epoch is None
                    else None
                )
                row.controller_epoch = None
                row.lease_expires_at = None
                row.job_token_hash = None
                row.job_token_expires_at = None
                row.updated_at = now
        handled = bool(pending or rows)
        for review_id, uid, stored_provider in pending:
            if await self._delete_resource(stored_provider, uid):
                await self._clear_resource_id("review", review_id)
        return handled

    async def _repair_kaniko_image_ids(self) -> bool:
        from ditto.api_server.targon_screening import (
            repair_kaniko_screened_image_identities,
        )

        repaired = await repair_kaniko_screened_image_identities(self._session_maker)
        return repaired > 0

    async def _finalize_ready_attempts(self) -> bool:
        """Finalize Platform-owned Targon/Cloud Run lanes after smoke.

        A persistent Hetzner node only supplies local build/runtime/review
        evidence to the signed screener worker.  It must never be selected by
        this legacy controller finalizer: the worker's signed verdict is the
        sole terminal result for that attempt.
        """
        if self._complete_screen is None:
            return False
        now = datetime.now(UTC)
        async with self._session_maker() as session:
            attempt_ids = list(
                await session.scalars(
                    select(SubmissionImageBuild.attempt_id)
                    .join(
                        ScreeningAttempt,
                        ScreeningAttempt.attempt_id == SubmissionImageBuild.attempt_id,
                    )
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.provider.in_(("targon", "gcp")),
                        ScreeningAttempt.status == "running",
                        ScreeningAttempt.deadline > now,
                        or_(
                            SubmissionImageBuild.status == "fallback_required",
                            and_(
                                SubmissionImageBuild.status.in_(
                                    ("succeeded", "consumed")
                                ),
                                SubmissionImageBuild.runtime_status.in_(
                                    ("succeeded", "fallback_required")
                                ),
                            ),
                        ),
                    )
                )
            )
        finalized = False
        for attempt_id in attempt_ids:
            try:
                await self._complete_screen(attempt_id)
            except Exception:
                logger.exception(
                    "targon screen finalize failed attempt_id=%s", attempt_id
                )
                continue
            finalized = True
        return finalized

    def _provider_named(self, stored: str | None) -> ScreeningComputeProvider | None:
        wanted = stored or "targon"
        for provider in self._providers:
            if provider.stored_provider == wanted or provider.name == wanted:
                return provider
        # Persistent fleet providers (for example Hetzner) share the job rows
        # with disposable cloud rentals, but are owned by the fleet agent.  An
        # unknown provider must never silently fall back to Targon: doing so
        # makes the cloud reaper probe a local Docker container name as a
        # Targon rental and fail healthy work after the provision timeout.
        return None

    async def _lane_providers(
        self, lane: str, pinned_provider: str | None
    ) -> list[ScreeningComputeProvider]:
        """Resolve the live ordered provider list for one decomposed lane."""
        by_name = {provider.stored_provider: provider for provider in self._providers}
        if pinned_provider is not None:
            provider = by_name.get(pinned_provider)
            return [provider] if provider is not None else []
        async with self._session_maker() as session:
            _, settings = await resolve_screener_provider_settings(
                session, environment=self._config.environment
            )
        priorities = {
            "build": settings.build_provider_priority,
            "runtime": settings.runtime_provider_priority,
            "review": settings.source_review_provider_priority,
        }[lane]
        return [by_name[name] for name in priorities if name in by_name]

    def _smoke_wait_seconds(self, provider: ScreeningComputeProvider) -> float:
        if provider.name == "targon":
            return self._config.smoke_provision_timeout_seconds
        return self._config.provision_timeout_seconds

    async def _observe_provision(
        self, provider: ScreeningComputeProvider, uid: str
    ) -> ProvisionObservation:
        return await provider.observe_provision(uid)

    async def _dead_replica_code(
        self,
        provider: ScreeningComputeProvider,
        uid: str,
        provisioned: str,
    ) -> str:
        if provisioned == "timeout":
            return inflight_failure_code(provider.stored_provider, "timeout")
        observation = await self._observe_provision(provider, uid)
        status = observation.status or provisioned
        return inflight_failure_code(
            provider.stored_provider, status, observation.message
        )

    def _live_build_inflight(self, now: datetime) -> ColumnElement[bool]:
        """Kaniko rows that still occupy a Targon create/deploy slot."""
        cutoff = self._provision_cutoff(now)
        lease_dead = and_(
            SubmissionImageBuild.lease_expires_at.is_not(None),
            SubmissionImageBuild.lease_expires_at < now,
        )
        abandoned = and_(
            SubmissionImageBuild.provider_resource_id.is_(None),
            SubmissionImageBuild.updated_at < cutoff,
        )
        return and_(
            SubmissionImageBuild.status.in_(_INFLIGHT_JOB),
            ~lease_dead,
            ~abandoned,
            SubmissionImageBuild.updated_at >= now - _BUILD_LEASE,
        )

    def _live_runtime_inflight(self, now: datetime) -> ColumnElement[bool]:
        """Smoke rows that still occupy a Targon create/deploy slot."""
        abandoned = and_(
            SubmissionImageBuild.runtime_provider_resource_id.is_(None),
            SubmissionImageBuild.updated_at < self._provision_cutoff(now),
        )
        return and_(
            SubmissionImageBuild.runtime_status == "running",
            ~abandoned,
        )

    def _live_review_inflight(self, now: datetime) -> ColumnElement[bool]:
        """L1 rows that still occupy a Targon create/deploy slot."""
        cutoff = self._provision_cutoff(now)
        lease_dead = and_(
            SubmissionSourceReview.lease_expires_at.is_not(None),
            SubmissionSourceReview.lease_expires_at < now,
        )
        abandoned = and_(
            SubmissionSourceReview.provider_resource_id.is_(None),
            SubmissionSourceReview.updated_at < cutoff,
        )
        return and_(
            SubmissionSourceReview.status.in_(_INFLIGHT_JOB),
            ~lease_dead,
            ~abandoned,
        )

    async def _targon_inflight(self) -> int:
        """Count live Targon rentals we still own.

        Targon cannot provision an unbounded burst. Kaniko, smoke, and L1
        share one cap so create/deploy stays inside that window. Expired
        leases and resource-less rows past the provision window are not
        live: they never created a rental, so they must not block the queue.
        """
        now = datetime.now(UTC)
        async with self._session_maker() as session:
            builds = await session.scalar(
                select(func.count())
                .select_from(SubmissionImageBuild)
                .where(
                    SubmissionImageBuild.environment == self._config.environment,
                    SubmissionImageBuild.provider == "targon",
                    or_(
                        self._live_build_inflight(now),
                        self._live_runtime_inflight(now),
                    ),
                )
            )
            reviews = await session.scalar(
                select(func.count())
                .select_from(SubmissionSourceReview)
                .where(
                    SubmissionSourceReview.environment == self._config.environment,
                    SubmissionSourceReview.provider == "targon",
                    self._live_review_inflight(now),
                )
            )
        return int(builds or 0) + int(reviews or 0)

    async def _provider_has_capacity(self, provider: ScreeningComputeProvider) -> bool:
        if not await provider.capacity_ok():
            return False
        if provider.stored_provider != "targon":
            return True
        return await self._targon_inflight() < self._config.max_inflight

    async def _any_capacity(self) -> bool:
        for provider in self._providers:
            if await self._provider_has_capacity(provider):
                return True
        return False

    def _provision_cutoff(self, now: datetime) -> datetime:
        return now - timedelta(seconds=self._config.provision_timeout_seconds)

    async def _launch_kaniko(self) -> bool:
        image = self._builder_image()
        if not is_digest_pinned_image(image):
            logger.error(
                "submission builder image is unpublished; not launching image=%s",
                image,
            )
            return False
        if not await self._any_capacity():
            return False
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            row = await session.scalar(
                select(SubmissionImageBuild)
                .join(
                    ScreeningAttempt,
                    ScreeningAttempt.attempt_id == SubmissionImageBuild.attempt_id,
                )
                .where(
                    SubmissionImageBuild.environment == self._config.environment,
                    ScreeningAttempt.status == "running",
                    ScreeningAttempt.deadline > now,
                    SubmissionImageBuild.status == "queued",
                )
                .order_by(SubmissionImageBuild.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            token = secrets.token_urlsafe(48)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            row.status = "leased"
            row.controller_epoch = self._epoch
            row.lease_expires_at = now + _BUILD_LEASE
            row.attempt_count += 1
            row.job_token_hash = token_hash
            row.job_token_expires_at = now + _JOB_TTL
            row.updated_at = now
            build_id = row.build_id
            agent_id = row.agent_id
            artifact_sha256 = row.artifact_sha256
            pinned_provider = row.provider
            skip_targon = (row.error_code or "") in _TARGON_BUILD_FALLBACK_CODES
        if not skip_targon:
            skip_targon = await self._targon_provision_exhausted(
                agent_id, artifact_sha256
            )
        spec = BuildSpec(
            name=f"ditto-miner-build-{str(build_id).replace('-', '')[:12]}"[:32],
            image=image,
            env=(
                ("DITTO_PLATFORM_URL", self._config.public_platform_url),
                ("DITTO_BUILD_ID", str(build_id)),
                ("DITTO_BUILD_JOB_TOKEN", token),
            ),
        )
        error_code = "TARGON_SUBMISSION_PROVIDER_ERROR"
        for provider in await self._lane_providers("build", pinned_provider):
            if skip_targon and provider.stored_provider == "targon":
                continue
            if not await self._provider_has_capacity(provider):
                continue
            uid: str | None = None
            try:
                uid = await provider.create_build(spec)
                launch_active = False
                async with self._session_maker() as session, session.begin():
                    stored = await session.get(
                        SubmissionImageBuild, build_id, with_for_update=True
                    )
                    if (
                        stored is not None
                        and stored.status == "leased"
                        and stored.controller_epoch == self._epoch
                        and stored.job_token_hash == token_hash
                        and stored.provider_resource_id is None
                    ):
                        stored.status = "running"
                        stored.provider = provider.stored_provider
                        stored.provider_resource_id = uid
                        stored.error_code = None
                        stored.updated_at = datetime.now(UTC)
                        launch_active = True
                if not launch_active:
                    # The GCE worker can cancel while provider creation is in
                    # flight. Never resurrect that row or start a job whose
                    # capability was revoked by the cancellation.
                    await provider.delete(uid)
                    return True
                await provider.start(uid)
                # Provisioning is observed by the next rental-loop tick. Waiting
                # here serializes every build, runtime, review, and reaper lane
                # behind one slow Targon rental for up to ten minutes.
                return True
            except ScreeningProviderError:
                logger.exception(
                    "%s kaniko launch failed build_id=%s", provider.name, build_id
                )
                error_code = (
                    "TARGON_SUBMISSION_PROVIDER_ERROR"
                    if provider.stored_provider == "targon"
                    else "CLOUDRUN_PROVIDER_ERROR"
                )
            if uid is not None:
                await self._capture_replica_trace(
                    kind="kaniko",
                    build_id=build_id,
                    uid=uid,
                    provider=provider,
                    error_code=error_code,
                )
                await provider.delete(uid)
                await self._clear_resource_id("build", build_id)
        await self._fail_build_provision(build_id, error_code)
        return True

    async def _launch_smoke(self) -> bool:
        mint_token = self._mint_token
        promote_archive = self._promote_archive
        if promote_archive is None or mint_token is None:
            return False
        if not self._config.candidate_writer_sa or not self._config.candidate_reader_sa:
            return False
        if not await self._any_capacity():
            return False
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            row = await session.scalar(
                select(SubmissionImageBuild)
                .where(
                    SubmissionImageBuild.environment == self._config.environment,
                    SubmissionImageBuild.status.in_(("succeeded", "consumed")),
                    or_(
                        SubmissionImageBuild.runtime_status == "pending",
                        and_(
                            SubmissionImageBuild.runtime_status == "running",
                            SubmissionImageBuild.runtime_provider_resource_id.is_(None),
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
                return False
            row.runtime_status = "running"
            row.controller_epoch = self._epoch
            row.updated_at = now
            build_id = row.build_id
            output_key = row.output_key
            destination = f"{_CANDIDATE_REGISTRY}:build-{build_id.hex}"
        writer = await mint_token(self._config.candidate_writer_sa)
        try:
            image_reference = await promote_archive(output_key, destination, writer)
        except Exception:
            logger.exception("runtime archive promote failed build_id=%s", build_id)
            await self._fail_runtime_provision(
                build_id, "TARGON_RUNTIME_PROVIDER_ERROR"
            )
            return True
        pull = await mint_token(self._config.candidate_reader_sa)
        spec = SmokeSpec(
            name=f"ditto-runtime-{str(build_id).replace('-', '')[:14]}"[:32],
            image=image_reference,
            env=(
                ("OPENROUTER_API_KEY", "sk-screener-smoke"),
                ("DITTOBENCH_DB", "/tmp/dittobench.db"),
            ),
            registry_auth={
                "server": destination.split("/", 1)[0],
                "username": "oauth2accesstoken",
                "password": pull,
            },
        )
        error_code = "TARGON_RUNTIME_PROVIDER_ERROR"
        last_uid: str | None = None
        last_provider: ScreeningComputeProvider | None = None
        healthy = False
        for provider in await self._lane_providers("runtime", None):
            if not await self._provider_has_capacity(provider):
                continue
            uid: str | None = None
            try:
                uid = await provider.create_smoke(spec)
                last_uid = uid
                last_provider = provider
                async with self._session_maker() as session, session.begin():
                    stored = await session.get(SubmissionImageBuild, build_id)
                    if stored is not None:
                        stored.runtime_provider_resource_id = uid
                        stored.runtime_image_reference = image_reference
                        stored.updated_at = datetime.now(UTC)
                await provider.start(uid)
                provisioned = await provider.wait_until_running(
                    uid, self._smoke_wait_seconds(provider)
                )
                if provisioned == "running":
                    healthy = await provider.probe_smoke(
                        uid,
                        timeout_seconds=self._config.runtime_timeout_seconds,
                    )
                    if healthy:
                        error_code = ""
                        break
                    error_code = "TARGON_RUNTIME_HEALTH_FAILED"
                else:
                    error_code = await self._dead_replica_code(
                        provider, uid, provisioned
                    )
            except Exception:
                logger.exception(
                    "%s runtime launch failed build_id=%s", provider.name, build_id
                )
                error_code = (
                    "TARGON_RUNTIME_PROVIDER_ERROR"
                    if provider.stored_provider == "targon"
                    else "CLOUDRUN_PROVIDER_ERROR"
                )
            if uid is not None:
                await provider.delete(uid)
                await self._clear_resource_id("runtime", build_id)
                last_uid = None
                last_provider = None
        async with self._session_maker() as session, session.begin():
            stored = await session.get(SubmissionImageBuild, build_id)
            if stored is None:
                return True
            stored.runtime_image_reference = image_reference
            stored.updated_at = datetime.now(UTC)
            if healthy:
                stored.runtime_status = "succeeded"
                stored.runtime_completed_at = datetime.now(UTC)
            else:
                stored.runtime_status = "fallback_required"
                stored.runtime_error_code = error_code
                stored.runtime_completed_at = datetime.now(UTC)
        if (
            last_uid is not None
            and last_provider is not None
            and await last_provider.delete(last_uid)
        ):
            await self._clear_resource_id("runtime", build_id)
        return True

    async def _launch_source_review(self) -> bool:
        if (
            not self._config.bootstrap_sa
            or not self._config.source_review_secret_resource
        ):
            return False
        mint_token = self._mint_token
        if mint_token is None:
            return False
        if not await self._any_capacity():
            return False
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            image_build = await session.scalar(
                select(TrustedImageBuild)
                .where(
                    TrustedImageBuild.environment == self._config.environment,
                    TrustedImageBuild.component == "screener",
                    TrustedImageBuild.status == "succeeded",
                    TrustedImageBuild.image_digest.is_not(None),
                )
                .order_by(TrustedImageBuild.completed_at.desc())
                .limit(1)
            )
            if image_build is None or image_build.image_digest is None:
                return False
            repository = image_build.destination.rsplit(":", 1)[0]
            image_reference = f"{repository}@{image_build.image_digest}"
            await session.execute(
                update(SubmissionSourceReview)
                .where(
                    SubmissionSourceReview.environment == self._config.environment,
                    SubmissionSourceReview.status == "queued",
                    SubmissionSourceReview.attempt_count >= 3,
                    SubmissionSourceReview.provider_outage_epoch.is_(None),
                )
                .values(
                    status="fallback_required",
                    error_code="TARGON_SOURCE_REVIEW_LEASE_EXHAUSTED",
                    completed_at=now,
                    updated_at=now,
                )
            )
            candidate_id = await session.scalar(
                select(SubmissionSourceReview.review_id)
                .join(
                    ScreeningAttempt,
                    ScreeningAttempt.attempt_id == SubmissionSourceReview.attempt_id,
                )
                .where(
                    SubmissionSourceReview.environment == self._config.environment,
                    ScreeningAttempt.status == "running",
                    ScreeningAttempt.build_only.is_(False),
                    ScreeningAttempt.deadline > now,
                    SubmissionSourceReview.status == "queued",
                    SubmissionSourceReview.provider_resource_id.is_(None),
                )
                .order_by(SubmissionSourceReview.created_at)
                .limit(1)
            )
            if candidate_id is None:
                return False
            provider_gate = await lock_provider_work_gate(
                session,
                now=now,
                kind="screening",
                key=str(candidate_id),
            )
            if not provider_gate.admitted:
                return False
            row = await session.scalar(
                select(SubmissionSourceReview)
                .join(
                    ScreeningAttempt,
                    ScreeningAttempt.attempt_id == SubmissionSourceReview.attempt_id,
                )
                .where(
                    SubmissionSourceReview.review_id == candidate_id,
                    SubmissionSourceReview.environment == self._config.environment,
                    ScreeningAttempt.status == "running",
                    ScreeningAttempt.build_only.is_(False),
                    ScreeningAttempt.deadline > now,
                    SubmissionSourceReview.status == "queued",
                    SubmissionSourceReview.provider_resource_id.is_(None),
                )
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return False
            token = secrets.token_urlsafe(48)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            row.status = "leased"
            row.controller_epoch = self._epoch
            row.lease_expires_at = now + _SOURCE_LEASE
            parked_epoch = row.provider_outage_epoch
            if parked_epoch is None:
                row.attempt_count += 1
            else:
                row.provider_outage_attempted_epoch = parked_epoch
            row.provider_outage_epoch = None
            row.job_token_hash = token_hash
            row.job_token_expires_at = now + _JOB_TTL
            row.updated_at = now
            review_id = row.review_id
            register_provider_probe(
                provider_gate,
                now=now,
                kind="screening",
                key=str(review_id),
            )
            attempt_id = row.attempt_id
            artifact_sha256 = row.artifact_sha256
            pinned_provider = row.provider
            settings_row = await session.scalar(
                select(ScreenerReviewSettingsRevision)
                .where(ScreenerReviewSettingsRevision.scope == "*")
                .order_by(ScreenerReviewSettingsRevision.revision.desc())
                .limit(1)
            )
            review_settings = (
                ScreenerReviewSettings.model_validate(settings_row.settings)
                if settings_row is not None
                else ScreenerReviewSettings()
            )
        bootstrap = await mint_token(self._config.bootstrap_sa)
        spec = ReviewSpec(
            name=f"ditto-source-{str(review_id).replace('-', '')[:16]}"[:32],
            image=image_reference,
            env=(
                ("DITTO_PLATFORM_URL", self._config.public_platform_url),
                ("DITTO_SOURCE_REVIEW_ID", str(review_id)),
                ("DITTO_SOURCE_REVIEW_ATTEMPT_ID", str(attempt_id)),
                ("DITTO_SOURCE_REVIEW_ARTIFACT_SHA256", artifact_sha256),
                ("DITTO_SOURCE_REVIEW_JOB_TOKEN", token),
                ("DITTO_SOURCE_REVIEW_JOB", "1"),
                (
                    "SCREENER_NODE_CREDENTIAL_FILE",
                    "/tmp/ditto-source-review/node.json",
                ),
                ("SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN", bootstrap),
                (
                    "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE",
                    self._config.source_review_secret_resource,
                ),
                (
                    "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS",
                    str(int(self._config.source_review_timeout_seconds)),
                ),
                *_source_review_layer_env(review_settings),
            ),
            commands=("/app/workers/screener/.venv/bin/python", "-m"),
            args=("ditto_screener.source_review_job",),
        )
        error_code = "TARGON_SOURCE_REVIEW_PROVIDER_ERROR"
        for provider in await self._lane_providers("review", pinned_provider):
            if not await self._provider_has_capacity(provider):
                continue
            uid: str | None = None
            try:
                uid = await provider.create_source_review(spec)
                launch_active = False
                async with self._session_maker() as session, session.begin():
                    stored = await session.get(
                        SubmissionSourceReview, review_id, with_for_update=True
                    )
                    if (
                        stored is not None
                        and stored.status == "leased"
                        and stored.controller_epoch == self._epoch
                        and stored.job_token_hash == token_hash
                        and stored.provider_resource_id is None
                    ):
                        stored.status = "running"
                        stored.provider = provider.stored_provider
                        stored.provider_resource_id = uid
                        stored.error_code = None
                        stored.updated_at = datetime.now(UTC)
                        launch_active = True
                if not launch_active:
                    await provider.delete(uid)
                    return True
                await provider.start(uid)
                # The reaper observes progress and performs any provider
                # fallback without blocking the other screening lanes.
                return True
            except ScreeningProviderError:
                logger.exception(
                    "%s source-review launch failed review_id=%s",
                    provider.name,
                    review_id,
                )
                error_code = (
                    "TARGON_SOURCE_REVIEW_PROVIDER_ERROR"
                    if provider.stored_provider == "targon"
                    else "CLOUDRUN_PROVIDER_ERROR"
                )
            if uid is not None:
                await provider.delete(uid)
                await self._clear_resource_id("review", review_id)
        await self._fail_review_provision(review_id, error_code)
        return True

    async def _targon_provision_exhausted(
        self, agent_id: UUID, artifact_sha256: str
    ) -> bool:
        async with self._session_maker() as session:
            code = await session.scalar(
                select(SubmissionImageBuild.error_code)
                .where(
                    SubmissionImageBuild.agent_id == agent_id,
                    SubmissionImageBuild.artifact_sha256 == artifact_sha256,
                    SubmissionImageBuild.error_code.in_(
                        tuple(_TARGON_BUILD_FALLBACK_CODES)
                    ),
                )
                .limit(1)
            )
        return code is not None

    async def _cloudrun_fallback_available(self) -> bool:
        for provider in self._providers:
            if provider.stored_provider != "gcp":
                continue
            if await provider.capacity_ok():
                return True
        return False

    async def _requeue_build_for_cloudrun(
        self, build_id: UUID, error_code: str
    ) -> bool:
        if error_code not in _TARGON_BUILD_FALLBACK_CODES:
            return False
        if not any(provider.stored_provider == "gcp" for provider in self._providers):
            return False
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            stored = await session.get(SubmissionImageBuild, build_id)
            attempt = (
                await session.get(ScreeningAttempt, stored.attempt_id)
                if stored is not None
                else None
            )
            if (
                stored is None
                or attempt is None
                or stored.status not in _INFLIGHT_JOB
                or stored.provider != "targon"
                or attempt.status != "running"
                or attempt.deadline <= now
            ):
                return False
            stored.status = "queued"
            stored.provider = "gcp"
            stored.provider_resource_id = None
            stored.error_code = error_code
            stored.controller_epoch = None
            stored.lease_expires_at = None
            stored.job_token_hash = None
            stored.job_token_expires_at = None
            stored.completed_at = None
            stored.updated_at = now
        return True

    async def _requeue_review_for_cloudrun(
        self, review_id: UUID, error_code: str
    ) -> bool:
        if error_code not in _TARGON_BUILD_FALLBACK_CODES:
            return False
        if not any(provider.stored_provider == "gcp" for provider in self._providers):
            return False
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            stored = await session.get(SubmissionSourceReview, review_id)
            attempt = (
                await session.get(ScreeningAttempt, stored.attempt_id)
                if stored is not None
                else None
            )
            if (
                stored is None
                or attempt is None
                or stored.status not in _INFLIGHT_JOB
                or stored.provider != "targon"
                or attempt.status != "running"
                or attempt.deadline <= now
            ):
                return False
            stored.status = "queued"
            stored.provider = "gcp"
            stored.provider_resource_id = None
            stored.error_code = error_code
            stored.controller_epoch = None
            stored.lease_expires_at = None
            stored.job_token_hash = None
            stored.job_token_expires_at = None
            stored.completed_at = None
            stored.updated_at = now
        return True

    async def _capture_replica_trace(
        self,
        *,
        kind: str,
        build_id: UUID,
        uid: str,
        provider: ScreeningComputeProvider,
        error_code: str,
        observation: ProvisionObservation | None = None,
    ) -> None:
        log_tail = ""
        try:
            log_tail = await provider.replica_logs(uid, tail=400)
        except Exception:
            logger.exception(
                "replica log fetch failed provider=%s uid=%s", provider.name, uid
            )
        agent_id = None
        attempt_id = None
        now = datetime.now(UTC)
        detail = _private_failure_text(
            observation.message if observation is not None else ""
        )
        log_tail = _private_failure_text(log_tail)
        async with self._session_maker() as session, session.begin():
            if kind == "review":
                stored_review = await session.get(SubmissionSourceReview, build_id)
                if stored_review is not None:
                    agent_id = stored_review.agent_id
                    attempt_id = stored_review.attempt_id
            else:
                stored_build = await session.get(SubmissionImageBuild, build_id)
                if stored_build is not None:
                    agent_id = stored_build.agent_id
                    attempt_id = stored_build.attempt_id
            attempt = (
                await session.get(ScreeningAttempt, attempt_id)
                if attempt_id is not None
                else None
            )
            if attempt is not None:
                attempt.failure_provider = provider.stored_provider
                attempt.failure_lane = kind
                attempt.private_failure_detail = detail or None
                attempt.private_failure_log_tail = log_tail or None
                attempt.failure_captured_at = now
        record = {
            "schema": SCREENING_TRACE_SCHEMA,
            "captured_at": now.isoformat(),
            "lane": "screening",
            "kind": kind,
            "provider": provider.stored_provider,
            "agent_id": str(agent_id) if agent_id else None,
            "attempt_id": str(attempt_id) if attempt_id else None,
            "build_id": str(build_id),
            "resource_id": uid,
            "error_code": error_code,
            "observation": {
                "status": observation.status if observation is not None else "",
                "message": detail,
            },
            "log_tail": log_tail,
        }
        if self._traces_put is None:
            return
        key = screening_trace_key(
            kind=kind,
            provider=provider.stored_provider,
            build_id=build_id,
            uid=uid,
            now=now,
        )
        try:
            await self._traces_put(
                key, encode_screening_trace(record), "application/zstd"
            )
        except Exception:
            logger.exception("screening trace upload failed key=%s", key)

    async def _fail_build_provision(self, build_id: UUID, error_code: str) -> None:
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            stored = await session.get(SubmissionImageBuild, build_id)
            if stored is None or stored.status not in _INFLIGHT_JOB:
                return
            stored.status = "fallback_required"
            stored.error_code = error_code
            stored.completed_at = now
            stored.updated_at = now
            stored.lease_expires_at = None

    async def _fail_review_provision(self, review_id: UUID, error_code: str) -> None:
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            stored = await session.get(SubmissionSourceReview, review_id)
            if stored is None or stored.status not in _INFLIGHT_JOB:
                return
            stored.status = "fallback_required"
            stored.error_code = error_code
            stored.completed_at = now
            stored.updated_at = now
            stored.lease_expires_at = None

    async def _fail_runtime_provision(self, build_id: UUID, error_code: str) -> None:
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            stored = await session.get(SubmissionImageBuild, build_id)
            if stored is None or stored.runtime_status != "running":
                return
            stored.runtime_status = "fallback_required"
            stored.runtime_error_code = error_code
            stored.runtime_completed_at = now
            stored.updated_at = now

    async def release_rental(self, uid: str | None) -> bool:
        """DELETE a rental as soon as its Platform job has completed."""
        if not uid:
            return False
        return await self._delete_resource(None, uid)

    async def _reap_abandoned_without_resource(self, now: datetime) -> bool:
        """Fail inflight rows that never received a provider resource id.

        A crash after ``leased`` / ``runtime_status=running`` and before
        ``create_*`` returns leaves ``provider_resource_id`` null. Reap
        queries that require a uid never see those rows, so they occupied
        the Targon inflight cap until the 50-minute lease — or forever when
        the lease expired without a uid.
        """
        cutoff = self._provision_cutoff(now)
        handled = False
        async with self._session_maker() as session, session.begin():
            builds = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.status.in_(_INFLIGHT_JOB),
                        SubmissionImageBuild.provider_resource_id.is_(None),
                        or_(
                            and_(
                                SubmissionImageBuild.lease_expires_at.is_not(None),
                                SubmissionImageBuild.lease_expires_at < now,
                            ),
                            SubmissionImageBuild.updated_at < cutoff,
                            SubmissionImageBuild.updated_at < now - _BUILD_LEASE,
                        ),
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in builds:
                row.status = "fallback_required"
                row.error_code = (
                    "TARGON_SUBMISSION_LEASE_EXPIRED"
                    if row.lease_expires_at is not None and row.lease_expires_at < now
                    else "TARGON_PROVISION_TIMEOUT"
                )
                row.completed_at = now
                row.updated_at = now
                row.lease_expires_at = None
                handled = True
            runtimes = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.runtime_status == "running",
                        SubmissionImageBuild.runtime_provider_resource_id.is_(None),
                        SubmissionImageBuild.updated_at < cutoff,
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in runtimes:
                row.runtime_status = "fallback_required"
                row.runtime_error_code = "TARGON_PROVISION_TIMEOUT"
                row.runtime_completed_at = now
                row.updated_at = now
                handled = True
            reviews = (
                await session.scalars(
                    select(SubmissionSourceReview)
                    .where(
                        SubmissionSourceReview.environment == self._config.environment,
                        SubmissionSourceReview.status.in_(_INFLIGHT_JOB),
                        SubmissionSourceReview.provider_resource_id.is_(None),
                        or_(
                            and_(
                                SubmissionSourceReview.lease_expires_at.is_not(None),
                                SubmissionSourceReview.lease_expires_at < now,
                            ),
                            SubmissionSourceReview.updated_at < cutoff,
                        ),
                    )
                    .order_by(SubmissionSourceReview.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in reviews:
                row.status = "fallback_required"
                row.error_code = "TARGON_PROVISION_TIMEOUT"
                row.completed_at = now
                row.updated_at = now
                row.lease_expires_at = None
                handled = True
        return handled

    async def _cancel_orphaned_source_reviews(self) -> bool:
        """Cancel L1 work whose parent screening attempt is already terminal.

        Source review starts in parallel with build and runtime smoke. Either
        sibling lane can therefore fail the attempt while L1 is still queued
        or running. Leaving that row active keeps a disposable rental billing,
        consumes the shared Targon inflight cap, and lets a late completion
        race a decision that can no longer use it.
        """
        pending: list[tuple[UUID, str, str | None]] = []
        now = datetime.now(UTC)
        handled = False
        async with self._session_maker() as session, session.begin():
            rows = (
                await session.scalars(
                    select(SubmissionSourceReview)
                    .join(
                        ScreeningAttempt,
                        ScreeningAttempt.attempt_id
                        == SubmissionSourceReview.attempt_id,
                    )
                    .where(
                        SubmissionSourceReview.environment == self._config.environment,
                        SubmissionSourceReview.status.in_(("queued", *_INFLIGHT_JOB)),
                        ScreeningAttempt.status != "running",
                    )
                    .order_by(SubmissionSourceReview.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in rows:
                uid = row.provider_resource_id
                row.status = "canceled"
                row.error_code = row.error_code or "SCREENING_ATTEMPT_TERMINAL"
                row.completed_at = now
                row.updated_at = now
                row.lease_expires_at = None
                row.job_token_hash = None
                row.job_token_expires_at = None
                if uid:
                    pending.append((row.review_id, uid, row.provider))
                handled = True
        for review_id, uid, stored_provider in pending:
            if await self._delete_resource(stored_provider, uid):
                await self._clear_resource_id("review", review_id)
        return handled

    async def _reap_unprovisioned_rentals(self) -> bool:
        """Fail inflight rentals that died or never became running.

        Targon ``error`` / ``deleted`` / ``suspended`` is failed on the next
        tick so a Kaniko crash (exit 72) cannot sit in ``running`` until
        ``provision_timeout_seconds``. Still-provisioning replicas wait for
        that cutoff, then timeout. Jobs that remain ``running`` are left
        until they POST complete or the 50-minute lease expires.
        """
        now = datetime.now(UTC)
        cutoff = self._provision_cutoff(now)
        handled = await self._reap_abandoned_without_resource(now)
        candidates: list[tuple[str, UUID, str, str | None, datetime]] = []
        async with self._session_maker() as session, session.begin():
            builds = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.status.in_(_INFLIGHT_JOB),
                        SubmissionImageBuild.provider_resource_id.is_not(None),
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in builds:
                uid = row.provider_resource_id
                if uid:
                    candidates.append(
                        ("build", row.build_id, uid, row.provider, row.updated_at)
                    )
            runtimes = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.runtime_status == "running",
                        SubmissionImageBuild.runtime_provider_resource_id.is_not(None),
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in runtimes:
                uid = row.runtime_provider_resource_id
                if uid:
                    candidates.append(
                        ("runtime", row.build_id, uid, row.provider, row.updated_at)
                    )
            reviews = (
                await session.scalars(
                    select(SubmissionSourceReview)
                    .where(
                        SubmissionSourceReview.environment == self._config.environment,
                        SubmissionSourceReview.status.in_(_INFLIGHT_JOB),
                        SubmissionSourceReview.provider_resource_id.is_not(None),
                    )
                    .order_by(SubmissionSourceReview.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in reviews:
                uid = row.provider_resource_id
                if uid:
                    candidates.append(
                        ("review", row.review_id, uid, row.provider, row.updated_at)
                    )
        for kind, row_id, uid, stored_provider, updated_at in candidates:
            provider = self._provider_named(stored_provider)
            if provider is None:
                continue
            observation = await self._observe_provision(provider, uid)
            status = observation.status
            if status == "running" and observation.ready is not False:
                continue
            seen = updated_at
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=UTC)
            if status in _PROVIDER_TERMINAL:
                error_code = inflight_failure_code(
                    provider.stored_provider, status, observation.message
                )
            elif seen < cutoff:
                error_code = inflight_failure_code(provider.stored_provider, "timeout")
            else:
                continue
            await self._capture_replica_trace(
                kind=kind if kind != "build" else "kaniko",
                build_id=row_id,
                uid=uid,
                provider=provider,
                error_code=error_code,
                observation=observation,
            )
            if (
                kind == "build"
                and provider.stored_provider == "targon"
                and await self._cloudrun_fallback_available()
            ):
                await provider.delete(uid)
                if await self._requeue_build_for_cloudrun(row_id, error_code):
                    handled = True
                    continue
            if (
                kind == "review"
                and provider.stored_provider == "targon"
                and await self._cloudrun_fallback_available()
            ):
                await provider.delete(uid)
                if await self._requeue_review_for_cloudrun(row_id, error_code):
                    handled = True
                    continue
            if kind == "build":
                await self._fail_build_provision(row_id, error_code)
            elif kind == "runtime":
                await self._fail_runtime_provision(row_id, error_code)
            else:
                await self._fail_review_provision(row_id, error_code)
            if await provider.delete(uid):
                await self._clear_resource_id(kind, row_id)
            handled = True
        return handled

    async def _reap_finished_rentals(self) -> bool:
        """DELETE one-shots whose Platform job is already terminal.

        Kaniko and L1 stay up until the job posts completion. Runtime smoke
        is deleted in `_launch_smoke`; this also drains leftovers from before
        that change. Expired in-flight Kaniko rows are treated as finished so
        a crash-loop that never POSTs complete cannot keep billing. Rentals
        that never leave Targon provisioning after ``provision_timeout_seconds``
        are failed with ``TARGON_PROVISION_TIMEOUT`` without waiting for the
        50-minute build lease.
        """
        handled = await self._cancel_orphaned_source_reviews()
        handled = await self._reap_unprovisioned_rentals() or handled
        pending: list[tuple[str, UUID, str, str | None]] = []
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            stale = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.status.in_(_INFLIGHT_JOB),
                        or_(
                            and_(
                                SubmissionImageBuild.lease_expires_at.is_not(None),
                                SubmissionImageBuild.lease_expires_at < now,
                            ),
                            SubmissionImageBuild.updated_at < now - _BUILD_LEASE,
                        ),
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in stale:
                uid = row.provider_resource_id
                row.status = "fallback_required"
                row.error_code = "TARGON_SUBMISSION_LEASE_EXPIRED"
                row.completed_at = now
                row.updated_at = now
                row.lease_expires_at = None
                if uid:
                    pending.append(("build", row.build_id, uid, row.provider))
            builds = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.status.in_(_TERMINAL_JOB),
                        SubmissionImageBuild.provider_resource_id.is_not(None),
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in builds:
                uid = row.provider_resource_id
                if uid:
                    pending.append(("build", row.build_id, uid, row.provider))
            runtimes = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.runtime_status.in_(_TERMINAL_RUNTIME),
                        SubmissionImageBuild.runtime_provider_resource_id.is_not(None),
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in runtimes:
                uid = row.runtime_provider_resource_id
                if uid:
                    pending.append(("runtime", row.build_id, uid, row.provider))
            reviews = (
                await session.scalars(
                    select(SubmissionSourceReview)
                    .where(
                        SubmissionSourceReview.environment == self._config.environment,
                        SubmissionSourceReview.status.in_(_TERMINAL_JOB),
                        SubmissionSourceReview.provider_resource_id.is_not(None),
                    )
                    .order_by(SubmissionSourceReview.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in reviews:
                uid = row.provider_resource_id
                if uid:
                    pending.append(("review", row.review_id, uid, row.provider))
        for kind, row_id, uid, stored_provider in pending:
            if await self._delete_resource(stored_provider, uid):
                await self._clear_resource_id(kind, row_id)
                handled = True
        return handled

    async def _clear_resource_id(self, kind: str, row_id: UUID) -> None:
        async with self._session_maker() as session, session.begin():
            if kind in {"build", "runtime"}:
                stored = await session.get(SubmissionImageBuild, row_id)
                if stored is None:
                    return
                if kind == "build":
                    stored.provider_resource_id = None
                else:
                    stored.runtime_provider_resource_id = None
                stored.updated_at = datetime.now(UTC)
                return
            stored_review = await session.get(SubmissionSourceReview, row_id)
            if stored_review is not None:
                stored_review.provider_resource_id = None
                stored_review.updated_at = datetime.now(UTC)

    async def _delete_resource(self, stored_provider: str | None, uid: str) -> bool:
        if stored_provider is None:
            stored_provider = (
                "gcp" if uid.startswith(("job:", "service:")) else "targon"
            )
        provider = self._provider_named(stored_provider)
        if provider is None:
            return False
        return await provider.delete(uid)
