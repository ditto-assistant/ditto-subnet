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
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_server.config import TargonRentalConfig
from ditto.api_server.screening_provider import (
    BuildSpec,
    ReviewSpec,
    ScreeningComputeProvider,
    ScreeningProviderError,
    SmokeSpec,
    provision_error_code,
)
from ditto.api_server.targon_provider import TargonComputeProvider, TargonRentals
from ditto.api_server.targon_screening import admit_targon_screening_work
from ditto.db.models import (
    ScreeningAttempt,
    SubmissionImageBuild,
    SubmissionSourceReview,
    TrustedImageBuild,
)

logger = logging.getLogger(__name__)

_BUILD_LEASE = timedelta(minutes=50)
_JOB_TTL = timedelta(minutes=45)
_SOURCE_LEASE = timedelta(minutes=35)
_REAP_LIMIT = 16
_TERMINAL_JOB = ("succeeded", "consumed", "canceled", "fallback_required")
_TERMINAL_RUNTIME = ("succeeded", "fallback_required", "skipped")
_INFLIGHT_JOB = ("leased", "running")
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
        self._resolved_builder_image: str | None = None
        self._storage = storage
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._epoch = "platform-targon-loop"

    def _builder_image(self) -> str:
        if self._resolved_builder_image is None:
            self._resolved_builder_image = self._resolve_builder_image(
                self._config.submission_builder_image
            )
        return self._resolved_builder_image

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
        async with self._session_maker() as session, session.begin():
            await admit_targon_screening_work(
                session,
                screener_hotkey=self._screener_hotkey,
                environment=self._config.environment,
                now=datetime.now(UTC),
            )
        handled = await self._launch_kaniko() or handled
        handled = await self._launch_smoke() or handled
        handled = await self._launch_source_review() or handled
        handled = await self._finalize_ready_attempts() or handled
        # v0.98.8 re-pinned by downloading screened tars on this loop and
        # stalled /health plus the public dashboard. Bind still pins new
        # Kaniko screens to the config digest. Do not re-enable until the
        # digest read is proven not to block request serving.
        return handled

    async def _repair_kaniko_image_ids(self) -> bool:
        if self._storage is None:
            return False
        from ditto.api_server.storage.client import S3StorageClient
        from ditto.api_server.targon_screening import (
            repair_kaniko_screened_image_identities,
        )

        storage = self._storage
        if not isinstance(storage, S3StorageClient):
            return False
        repaired = await repair_kaniko_screened_image_identities(
            self._session_maker, storage
        )
        return repaired > 0

    async def _finalize_ready_attempts(self) -> bool:
        """Attest Targon passes after smoke, or fail-retry after Kaniko fallback."""
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
                        ScreeningAttempt.status == "running",
                        ScreeningAttempt.deadline > now,
                        or_(
                            SubmissionImageBuild.status == "fallback_required",
                            and_(
                                SubmissionImageBuild.status.in_(
                                    ("succeeded", "consumed")
                                ),
                                SubmissionImageBuild.runtime_status == "succeeded",
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

    def _provider_named(self, stored: str | None) -> ScreeningComputeProvider:
        wanted = stored or "targon"
        for provider in self._providers:
            if provider.stored_provider == wanted or provider.name == wanted:
                return provider
        return self._providers[0]

    async def _targon_inflight(self) -> int:
        """Count live Targon rentals we still own.

        Targon cannot provision an unbounded burst. Kaniko, smoke, and L1
        share one cap so create/deploy stays inside that window.
        """
        async with self._session_maker() as session:
            builds = await session.scalar(
                select(func.count())
                .select_from(SubmissionImageBuild)
                .where(
                    SubmissionImageBuild.environment == self._config.environment,
                    SubmissionImageBuild.provider == "targon",
                    or_(
                        SubmissionImageBuild.status.in_(_INFLIGHT_JOB),
                        SubmissionImageBuild.runtime_status == "running",
                    ),
                )
            )
            reviews = await session.scalar(
                select(func.count())
                .select_from(SubmissionSourceReview)
                .where(
                    SubmissionSourceReview.environment == self._config.environment,
                    SubmissionSourceReview.provider == "targon",
                    SubmissionSourceReview.status.in_(_INFLIGHT_JOB),
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
            row.status = "leased"
            row.controller_epoch = self._epoch
            row.lease_expires_at = now + _BUILD_LEASE
            row.attempt_count += 1
            row.job_token_hash = hashlib.sha256(token.encode()).hexdigest()
            row.job_token_expires_at = now + _JOB_TTL
            row.updated_at = now
            build_id = row.build_id
        spec = BuildSpec(
            name=f"ditto-miner-build-{str(build_id).replace('-', '')[:12]}"[:32],
            image=self._builder_image(),
            env=(
                ("DITTO_PLATFORM_URL", self._config.public_platform_url),
                ("DITTO_BUILD_ID", str(build_id)),
                ("DITTO_BUILD_JOB_TOKEN", token),
            ),
        )
        error_code = "TARGON_SUBMISSION_PROVIDER_ERROR"
        for provider in self._providers:
            if not await self._provider_has_capacity(provider):
                continue
            uid: str | None = None
            try:
                uid = await provider.create_build(spec)
                async with self._session_maker() as session, session.begin():
                    stored = await session.get(SubmissionImageBuild, build_id)
                    if stored is not None:
                        stored.status = "running"
                        stored.provider = provider.stored_provider
                        stored.provider_resource_id = uid
                        stored.updated_at = datetime.now(UTC)
                await provider.start(uid)
                provisioned = await provider.wait_until_running(
                    uid, self._config.provision_timeout_seconds
                )
                if provisioned == "running":
                    return True
                error_code = provision_error_code(provider.stored_provider, provisioned)
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
                    SubmissionImageBuild.runtime_status == "pending",
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
        for provider in self._providers:
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
                    uid, self._config.provision_timeout_seconds
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
                    error_code = provision_error_code(
                        provider.stored_provider, provisioned
                    )
            except ScreeningProviderError:
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
                attempt = await session.get(ScreeningAttempt, stored.attempt_id)
                if attempt is not None and not attempt.build_only:
                    await session.execute(
                        pg_insert(SubmissionSourceReview)
                        .values(
                            review_id=uuid4(),
                            agent_id=stored.agent_id,
                            attempt_id=stored.attempt_id,
                            environment=stored.environment,
                            artifact_sha256=stored.artifact_sha256,
                            status="queued",
                        )
                        .on_conflict_do_nothing(
                            constraint="submission_source_reviews_attempt_key"
                        )
                    )
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
            row = await session.scalar(
                select(SubmissionSourceReview)
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
                )
                .order_by(SubmissionSourceReview.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            token = secrets.token_urlsafe(48)
            row.status = "leased"
            row.controller_epoch = self._epoch
            row.lease_expires_at = now + _SOURCE_LEASE
            row.attempt_count += 1
            row.job_token_hash = hashlib.sha256(token.encode()).hexdigest()
            row.job_token_expires_at = now + _JOB_TTL
            row.updated_at = now
            review_id = row.review_id
            attempt_id = row.attempt_id
            artifact_sha256 = row.artifact_sha256
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
            ),
            commands=("/app/workers/screener/.venv/bin/python", "-m"),
            args=("ditto_screener.source_review_job",),
        )
        error_code = "TARGON_SOURCE_REVIEW_PROVIDER_ERROR"
        for provider in self._providers:
            if not await self._provider_has_capacity(provider):
                continue
            uid: str | None = None
            try:
                uid = await provider.create_source_review(spec)
                async with self._session_maker() as session, session.begin():
                    stored = await session.get(SubmissionSourceReview, review_id)
                    if stored is not None:
                        stored.status = "running"
                        stored.provider = provider.stored_provider
                        stored.provider_resource_id = uid
                        stored.updated_at = datetime.now(UTC)
                await provider.start(uid)
                provisioned = await provider.wait_until_running(
                    uid, self._config.provision_timeout_seconds
                )
                if provisioned == "running":
                    return True
                error_code = provision_error_code(provider.stored_provider, provisioned)
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

    async def _reap_unprovisioned_rentals(self) -> bool:
        """Fail in-flight rentals that never reached a running workload."""
        now = datetime.now(UTC)
        cutoff = self._provision_cutoff(now)
        candidates: list[tuple[str, UUID, str, str | None]] = []
        async with self._session_maker() as session, session.begin():
            builds = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.status.in_(_INFLIGHT_JOB),
                        SubmissionImageBuild.provider_resource_id.is_not(None),
                        SubmissionImageBuild.updated_at < cutoff,
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in builds:
                uid = row.provider_resource_id
                if uid:
                    candidates.append(("build", row.build_id, uid, row.provider))
            runtimes = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.runtime_status == "running",
                        SubmissionImageBuild.runtime_provider_resource_id.is_not(None),
                        SubmissionImageBuild.updated_at < cutoff,
                    )
                    .order_by(SubmissionImageBuild.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in runtimes:
                uid = row.runtime_provider_resource_id
                if uid:
                    candidates.append(("runtime", row.build_id, uid, row.provider))
            reviews = (
                await session.scalars(
                    select(SubmissionSourceReview)
                    .where(
                        SubmissionSourceReview.environment == self._config.environment,
                        SubmissionSourceReview.status.in_(_INFLIGHT_JOB),
                        SubmissionSourceReview.provider_resource_id.is_not(None),
                        SubmissionSourceReview.updated_at < cutoff,
                    )
                    .order_by(SubmissionSourceReview.updated_at)
                    .with_for_update(skip_locked=True)
                    .limit(_REAP_LIMIT)
                )
            ).all()
            for row in reviews:
                uid = row.provider_resource_id
                if uid:
                    candidates.append(("review", row.review_id, uid, row.provider))
        handled = False
        for kind, row_id, uid, stored_provider in candidates:
            provider = self._provider_named(stored_provider)
            status = await provider.provision_status(uid)
            if status == "running":
                continue
            result = (
                "error" if status in {"error", "deleted", "suspended"} else "timeout"
            )
            error_code = provision_error_code(provider.stored_provider, result)
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
        handled = await self._reap_unprovisioned_rentals()
        pending: list[tuple[str, UUID, str, str | None]] = []
        now = datetime.now(UTC)
        async with self._session_maker() as session, session.begin():
            stale = (
                await session.scalars(
                    select(SubmissionImageBuild)
                    .where(
                        SubmissionImageBuild.environment == self._config.environment,
                        SubmissionImageBuild.status.in_(_INFLIGHT_JOB),
                        SubmissionImageBuild.provider_resource_id.is_not(None),
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
        return await self._provider_named(stored_provider).delete(uid)
