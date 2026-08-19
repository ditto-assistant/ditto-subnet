"""Create Targon screening rentals from the Platform API process.

This replaces the separate capacity-controller / image-builder host. One GCE
VM (Platform) admits work, launches rentals, and attests verdicts.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from ditto.api_server.config import TargonRentalConfig
from ditto.api_server.targon_client import TargonAPIError
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


class TargonRentals(Protocol):
    async def inventory(self) -> list[dict[str, Any]]: ...

    async def create_rental(self, **payload: Any) -> dict[str, Any]: ...

    async def deploy(self, uid: str) -> None: ...

    async def state(self, uid: str) -> dict[str, Any]: ...

    async def delete(self, uid: str) -> None: ...


PromoteArchive = Callable[[str, str, str], Awaitable[str]]
MintToken = Callable[[str], Awaitable[str]]


class TargonRentalLoop:
    def __init__(
        self,
        *,
        session_maker: async_sessionmaker,
        config: TargonRentalConfig,
        targon: TargonRentals,
        screener_hotkey: str,
        promote_archive: PromoteArchive | None = None,
        mint_token: MintToken | None = None,
        health_probe: Callable[[str], Awaitable[bool]] | None = None,
        interval_seconds: float | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._config = config
        self._targon = targon
        self._screener_hotkey = screener_hotkey
        self._promote_archive = promote_archive
        self._mint_token = mint_token
        self._health_probe = health_probe or _default_health_probe
        self._interval_seconds = interval_seconds or config.interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._epoch = "platform-targon-loop"

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
        handled = False
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
        return handled

    async def _capacity_ok(self) -> bool:
        try:
            rows = await self._targon.inventory()
        except TargonAPIError:
            logger.warning("targon inventory unavailable", exc_info=True)
            return False
        available = sum(
            max(0, int(row.get("available", 0)))
            for row in rows
            if str(row.get("name", "")) == self._config.resource
        )
        return available >= 1

    async def _launch_kaniko(self) -> bool:
        if not await self._capacity_ok():
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
            row.provider = "targon"
            row.controller_epoch = self._epoch
            row.lease_expires_at = now + _BUILD_LEASE
            row.attempt_count += 1
            row.job_token_hash = hashlib.sha256(token.encode()).hexdigest()
            row.job_token_expires_at = now + _JOB_TTL
            row.updated_at = now
            build_id = row.build_id
        name = f"ditto-miner-build-{str(build_id).replace('-', '')[:12]}"[:32]
        uid: str | None = None
        try:
            created = await self._targon.create_rental(
                name=name,
                image=self._config.submission_builder_image,
                resource_name=self._config.resource,
                envs=[
                    {
                        "name": "DITTO_PLATFORM_URL",
                        "value": self._config.public_platform_url,
                    },
                    {"name": "DITTO_BUILD_ID", "value": str(build_id)},
                    {"name": "DITTO_BUILD_JOB_TOKEN", "value": token},
                ],
            )
            uid = str(created["uid"])
            async with self._session_maker() as session, session.begin():
                stored = await session.get(SubmissionImageBuild, build_id)
                if stored is not None:
                    stored.status = "running"
                    stored.provider_resource_id = uid
                    stored.updated_at = datetime.now(UTC)
            await self._targon.deploy(uid)
            return True
        except (TargonAPIError, KeyError, ValueError):
            logger.exception("targon kaniko launch failed build_id=%s", build_id)
            async with self._session_maker() as session, session.begin():
                stored = await session.get(SubmissionImageBuild, build_id)
                if stored is not None:
                    stored.status = "fallback_required"
                    stored.error_code = "TARGON_SUBMISSION_PROVIDER_ERROR"
                    stored.updated_at = datetime.now(UTC)
            if uid is not None:
                await self._safe_delete(uid)
            return True

    async def _launch_smoke(self) -> bool:
        if self._promote_archive is None or self._mint_token is None:
            return False
        if not self._config.candidate_writer_sa or not self._config.candidate_reader_sa:
            return False
        if not await self._capacity_ok():
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
        uid: str | None = None
        keep = False
        try:
            writer = await self._mint_token(self._config.candidate_writer_sa)
            image_reference = await self._promote_archive(
                output_key, destination, writer
            )
            pull = await self._mint_token(self._config.candidate_reader_sa)
            created = await self._targon.create_rental(
                name=f"ditto-runtime-{str(build_id).replace('-', '')[:14]}"[:32],
                image=image_reference,
                resource_name=self._config.resource,
                envs=[
                    {"name": "OPENROUTER_API_KEY", "value": "sk-screener-smoke"},
                    {"name": "DITTOBENCH_DB", "value": "/tmp/dittobench.db"},
                ],
                ports=[{"port": 8080, "protocol": "TCP", "routing": "PROXIED"}],
                registry_auth={
                    "server": destination.split("/", 1)[0],
                    "username": "oauth2accesstoken",
                    "password": pull,
                },
            )
            uid = str(created["uid"])
            await self._targon.deploy(uid)
            healthy = await self._wait_for_health(uid)
            async with self._session_maker() as session, session.begin():
                stored = await session.get(SubmissionImageBuild, build_id)
                if stored is None:
                    return True
                stored.runtime_provider_resource_id = uid
                stored.runtime_image_reference = image_reference
                stored.updated_at = datetime.now(UTC)
                if healthy:
                    stored.runtime_status = "succeeded"
                    stored.runtime_completed_at = datetime.now(UTC)
                    keep = True
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
                    stored.runtime_error_code = "TARGON_RUNTIME_HEALTH_FAILED"
                    stored.runtime_completed_at = datetime.now(UTC)
            return True
        except Exception:
            logger.exception("targon runtime launch failed build_id=%s", build_id)
            async with self._session_maker() as session, session.begin():
                stored = await session.get(SubmissionImageBuild, build_id)
                if stored is not None:
                    stored.runtime_status = "fallback_required"
                    stored.runtime_error_code = "TARGON_RUNTIME_PROVIDER_ERROR"
                    stored.updated_at = datetime.now(UTC)
            return True
        finally:
            if uid is not None and not keep:
                await self._safe_delete(uid)

    async def _launch_source_review(self) -> bool:
        if (
            not self._config.bootstrap_sa
            or not self._config.source_review_secret_resource
        ):
            return False
        if self._mint_token is None:
            return False
        if not await self._capacity_ok():
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
            row.provider = "targon"
            row.controller_epoch = self._epoch
            row.lease_expires_at = now + _SOURCE_LEASE
            row.attempt_count += 1
            row.job_token_hash = hashlib.sha256(token.encode()).hexdigest()
            row.job_token_expires_at = now + _JOB_TTL
            row.updated_at = now
            review_id = row.review_id
            attempt_id = row.attempt_id
            artifact_sha256 = row.artifact_sha256
        uid: str | None = None
        try:
            bootstrap = await self._mint_token(self._config.bootstrap_sa)
            created = await self._targon.create_rental(
                name=f"ditto-source-{str(review_id).replace('-', '')[:16]}"[:32],
                image=image_reference,
                resource_name=self._config.resource,
                envs=[
                    {
                        "name": "DITTO_PLATFORM_URL",
                        "value": self._config.public_platform_url,
                    },
                    {"name": "DITTO_SOURCE_REVIEW_ID", "value": str(review_id)},
                    {
                        "name": "DITTO_SOURCE_REVIEW_ATTEMPT_ID",
                        "value": str(attempt_id),
                    },
                    {
                        "name": "DITTO_SOURCE_REVIEW_ARTIFACT_SHA256",
                        "value": artifact_sha256,
                    },
                    {"name": "DITTO_SOURCE_REVIEW_JOB_TOKEN", "value": token},
                    {"name": "DITTO_SOURCE_REVIEW_JOB", "value": "1"},
                    {
                        "name": "SCREENER_NODE_CREDENTIAL_FILE",
                        "value": "/tmp/ditto-source-review/node.json",
                    },
                    {
                        "name": "SCREENER_GCP_BOOTSTRAP_ACCESS_TOKEN",
                        "value": bootstrap,
                    },
                    {
                        "name": "SCREENER_SOURCE_REVIEW_SECRET_RESOURCE",
                        "value": self._config.source_review_secret_resource,
                    },
                    {
                        "name": "SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS",
                        "value": str(int(self._config.source_review_timeout_seconds)),
                    },
                ],
                commands=["/app/workers/screener/.venv/bin/python", "-m"],
                args=["ditto_screener.source_review_job"],
            )
            uid = str(created["uid"])
            async with self._session_maker() as session, session.begin():
                stored = await session.get(SubmissionSourceReview, review_id)
                if stored is not None:
                    stored.status = "running"
                    stored.provider_resource_id = uid
                    stored.updated_at = datetime.now(UTC)
            await self._targon.deploy(uid)
            return True
        except Exception:
            logger.exception(
                "targon source-review launch failed review_id=%s", review_id
            )
            async with self._session_maker() as session, session.begin():
                stored = await session.get(SubmissionSourceReview, review_id)
                if stored is not None:
                    stored.status = "fallback_required"
                    stored.error_code = "TARGON_SOURCE_REVIEW_PROVIDER_ERROR"
                    stored.updated_at = datetime.now(UTC)
            if uid is not None:
                await self._safe_delete(uid)
            return True

    async def _wait_for_health(self, uid: str) -> bool:
        if self._health_probe is None:
            return False
        deadline = (
            asyncio.get_running_loop().time() + self._config.runtime_timeout_seconds
        )
        while asyncio.get_running_loop().time() < deadline:
            try:
                state = await self._targon.state(uid)
            except TargonAPIError:
                await asyncio.sleep(1)
                continue
            urls = state.get("urls")
            if isinstance(urls, list):
                for row in urls:
                    if not isinstance(row, dict) or row.get("port") != 8080:
                        continue
                    url = str(row.get("url", "")).rstrip("/")
                    if url.startswith("https://") and await self._health_probe(url):
                        return True
            if str(state.get("status", "")).casefold() == "error":
                return False
            await asyncio.sleep(1)
        return False

    async def _safe_delete(self, uid: str) -> None:
        try:
            await self._targon.delete(uid)
        except TargonAPIError:
            logger.warning("targon delete failed uid=%s", uid, exc_info=True)
