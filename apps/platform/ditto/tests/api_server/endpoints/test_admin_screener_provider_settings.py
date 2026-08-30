"""Audited Backroom control for screener and builder provider routing."""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    ScreenerCapacityEvent,
    ScreenerCapacitySnapshot,
    ScreenerNode,
    ScreenerNodeBootstrapGrant,
    TrustedImageBuild,
)

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_PATH = "/api/v1/admin/screener-provider-settings"
_NODE_TOKEN = "node-token-" + "x" * 48
_BOOTSTRAP_PATH = "/api/v1/admin/screener-bootstrap-grants"
_CONTROLLER_EPOCH = "prod:controller-test-1"
_IMAGE_REFERENCE = (
    "us-central1-docker.pkg.dev/ditto-app-dev/ditto-public-runtime/"
    "screener@sha256:" + "a" * 64
)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _payload(
    *,
    expected_revision: int,
    screening: list[str],
    builds: list[str],
    confirmation: str | None = None,
) -> dict[str, object]:
    settings = {
        "runtime_provider_priority": screening,
        "source_review_provider_priority": screening,
        "build_provider_priority": builds,
        "gce_overflow_enabled": False,
        "primary_node_id": None,
        "gce_overflow_backlog_multiplier": 3,
        "gce_overflow_min_backlog": 12,
        "gce_overflow_max_instances": 6,
    }
    phrase = (
        f"APPLY SCREENER PROVIDERS BUILDS={'>'.join(builds)} "
        f"RUNTIME={'>'.join(screening)} SOURCE_REVIEW={'>'.join(screening)} "
        "GCE_OVERFLOW=DISABLED"
    )
    return {
        "environment": "prod",
        "expected_revision": expected_revision,
        "settings": settings,
        "reason": "Route around scheduled Targon provider maintenance",
        "actor": "operator@example.com",
        "confirmation": confirmation if confirmation is not None else phrase,
    }


async def test_bootstrap_grant_is_fenced_single_use_and_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerCapacitySnapshot(
                environment="prod",
                controller_epoch=_CONTROLLER_EPOCH,
                controller_source_sha="b" * 40,
                provider_ready=True,
                controller_heartbeat_at=now,
                controller_lease_expires_at=now + timedelta(minutes=3),
                runnable_backlog=0,
                active_leases=0,
                desired_slots=0,
                global_cap=6,
                targon_capability="nogo",
                targon_available=0,
                targon_healthy=0,
                targon_pending=0,
                targon_draining=0,
                gce_target=0,
                gce_healthy=0,
                gce_pending=0,
                gce_draining=0,
            )
        )

    payload = {
        "environment": "prod",
        "node_id": "subnet-screener-1",
        "provider": "hetzner",
        "provider_resource_id": "3062657",
        "image_reference": _IMAGE_REFERENCE,
        "expected_controller_epoch": _CONTROLLER_EPOCH,
        "reason": "Enroll the prepared primary Hetzner screener at zero capacity",
        "actor": "operator@example.com",
        "confirmation": (
            "CREATE SCREENER BOOTSTRAP GRANT NODE=subnet-screener-1 "
            f"PROVIDER=hetzner RESOURCE=3062657 IMAGE={_IMAGE_REFERENCE}"
        ),
    }
    stale = await client.post(
        _BOOTSTRAP_PATH,
        headers=_HEADERS,
        json={**payload, "expected_controller_epoch": "prod:stale"},
    )
    assert stale.status_code == 409

    created = await client.post(_BOOTSTRAP_PATH, headers=_HEADERS, json=payload)
    assert created.status_code == 201, created.text
    token = created.json()["registration_token"]
    assert len(token) >= 43

    async with session_maker() as session:
        grant = await session.scalar(
            select(ScreenerNodeBootstrapGrant).where(
                ScreenerNodeBootstrapGrant.node_id == "subnet-screener-1"
            )
        )
        event = await session.scalar(
            select(ScreenerCapacityEvent).where(
                ScreenerCapacityEvent.event_type == "node_bootstrap_grant_created"
            )
        )
    assert grant is not None
    assert grant.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert grant.image_reference == _IMAGE_REFERENCE
    assert event is not None
    assert "operator@example.com" in event.detail
    assert token not in event.detail

    duplicate = await client.post(_BOOTSTRAP_PATH, headers=_HEADERS, json=payload)
    assert duplicate.status_code == 409


async def test_provider_settings_are_atomic_audited_and_cas_guarded(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    initial = await client.get(_PATH, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    assert initial.json()["current"]["revision"] == 0
    assert initial.json()["current"]["settings"] == {
        "runtime_provider_priority": ["gcp", "targon"],
        "source_review_provider_priority": ["gcp", "targon"],
        "build_provider_priority": ["gcp", "targon"],
        "gce_overflow_enabled": False,
        "primary_node_id": None,
        "gce_overflow_backlog_multiplier": 3,
        "gce_overflow_min_backlog": 12,
        "gce_overflow_max_instances": 6,
    }

    applied = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["gcp", "targon"],
            builds=["gcp"],
        ),
    )
    assert applied.status_code == 200, applied.text
    revision = applied.json()["revision"]

    capacity = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)
    assert capacity.status_code == 200, capacity.text
    control = capacity.json()["provider_control"]
    assert control["current"]["revision"] == revision
    assert control["current"]["settings"]["build_provider_priority"] == ["gcp"]

    stale = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["targon", "gcp"],
            builds=["targon", "gcp"],
        ),
    )
    assert stale.status_code == 409


async def test_provider_settings_require_gcp_and_exact_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    single_provider = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["targon"],
            builds=["targon"],
        ),
    )
    assert single_provider.status_code == 422

    wrong_confirmation = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["gcp"],
            builds=["gcp"],
            confirmation="APPLY",
        ),
    )
    assert wrong_confirmation.status_code == 409


async def test_node_channel_settings_default_disabled_and_cas_guarded(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id="subnet-screener-1",
                provider="hetzner",
                provider_resource_id="robot-2984021",
                screener_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
                token_hash=hashlib.sha256(_NODE_TOKEN.encode()).hexdigest(),
                token_expires_at=now + timedelta(hours=6),
                status="active",
                capacity=3,
            )
        )

    path = "/api/v1/admin/screener-nodes/subnet-screener-1/channel-settings"
    initial = await client.get(path, headers=_HEADERS)
    assert initial.status_code == 200, initial.text
    assert initial.json()["current"]["revision"] == 0
    assert initial.json()["current"]["settings"] == {
        "screening_concurrency": 0,
        "sandbox_slots": 0,
        "build_concurrency": 0,
        "runtime_concurrency": 0,
        "source_review_concurrency": 0,
    }

    settings = {
        "screening_concurrency": 8,
        "sandbox_slots": 3,
        "build_concurrency": 3,
        "runtime_concurrency": 3,
        "source_review_concurrency": 6,
    }
    applied = await client.post(
        path,
        headers=_HEADERS,
        json={
            "environment": "prod",
            "expected_revision": 0,
            "settings": settings,
            "reason": "Activate the first dedicated 64 GB screener host",
            "actor": "operator@example.com",
            "confirmation": (
                "APPLY SCREENER NODE subnet-screener-1 SCREENING=8 "
                "SANDBOX=3 BUILD=3 "
                "RUNTIME=3 SOURCE_REVIEW=6"
            ),
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["settings"] == settings

    stale = await client.post(
        path,
        headers=_HEADERS,
        json={
            "environment": "prod",
            "expected_revision": 0,
            "settings": settings,
            "reason": "Repeat a stale capacity mutation request",
            "actor": "operator@example.com",
            "confirmation": (
                "APPLY SCREENER NODE subnet-screener-1 SCREENING=8 "
                "SANDBOX=3 BUILD=3 "
                "RUNTIME=3 SOURCE_REVIEW=6"
            ),
        },
    )
    assert stale.status_code == 409

    capacity = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)
    assert capacity.status_code == 200, capacity.text
    assert capacity.json()["node_controls"][0]["current"]["settings"] == settings


async def test_hetzner_node_claim_is_identity_bound_and_platform_limited(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.api_models.agent_status import AgentStatus
    from ditto.db.models import ScreeningAttempt, SubmissionImageBuild
    from ditto.tests.api_server.endpoints.test_screener import (
        _AUTH_HEADER,
        _CLAIM_URL,
        _SCREENER_HOTKEY,
        _install_chain,
        _install_db,
        _install_storage,
        _seed_agent,
    )

    _install(app, session_maker)
    _install_db(app, session_maker)
    _install_chain(app)
    _install_storage(app)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id="subnet-screener-1",
                provider="hetzner",
                provider_resource_id="robot-2984021",
                screener_hotkey=_SCREENER_HOTKEY,
                token_hash=hashlib.sha256(_NODE_TOKEN.encode()).hexdigest(),
                token_expires_at=now + timedelta(hours=6),
                status="active",
                capacity=3,
            )
        )
    provider = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["hetzner", "gcp"],
            builds=["hetzner", "gcp"],
        ),
    )
    assert provider.status_code == 200, provider.text

    agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
    attempt_id = claim.json()["items"][0]["attempt_id"]
    queued = await client.post(
        f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
        headers=_AUTH_HEADER,
        json={"attempt_id": attempt_id},
    )
    assert queued.status_code == 200, queued.text
    build_id = queued.json()["build_id"]
    node_headers = {
        "Authorization": f"Bearer {_NODE_TOKEN}",
        "X-Screener-Hotkey": _SCREENER_HOTKEY,
    }

    disabled = await client.post(
        "/api/v1/screener/nodes/jobs/submission-image-builds/claim",
        headers=node_headers,
        json={"environment": "prod"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["build"] is None

    limits_path = "/api/v1/admin/screener-nodes/subnet-screener-1/channel-settings"
    enabled = await client.post(
        limits_path,
        headers=_HEADERS,
        json={
            "environment": "prod",
            "expected_revision": 0,
            "settings": {
                "screening_concurrency": 2,
                "sandbox_slots": 1,
                "build_concurrency": 1,
                "runtime_concurrency": 1,
                "source_review_concurrency": 2,
            },
            "reason": "Enable one isolated VM slot for the node claim test",
            "actor": "operator@example.com",
            "confirmation": (
                "APPLY SCREENER NODE subnet-screener-1 SCREENING=2 "
                "SANDBOX=1 BUILD=1 "
                "RUNTIME=1 SOURCE_REVIEW=2"
            ),
        },
    )
    assert enabled.status_code == 200, enabled.text

    leased = await client.post(
        "/api/v1/screener/nodes/jobs/submission-image-builds/claim",
        headers=node_headers,
        json={"environment": "prod"},
    )
    assert leased.status_code == 200, leased.text
    assert leased.json()["build"]["build_id"] == build_id
    async with session_maker() as session:
        row = await session.get(SubmissionImageBuild, build_id)
        assert row is not None
        assert row.node_id == "subnet-screener-1"
        assert row.provider == "hetzner"

    failed = await client.put(
        f"/api/v1/screener/nodes/jobs/submission-image-builds/{build_id}",
        headers=node_headers,
        json={
            "status": "fallback_required",
            "provider_resource_id": "ditto-build-test",
            "error_code": (
                "FLEET_SUBMISSION_BUILDKIT_LOCAL_CARGO_DEPENDENCY_MISSING_FAILED"
            ),
        },
    )
    assert failed.status_code == 204, failed.text
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        row = await session.get(SubmissionImageBuild, build_id)
        assert attempt is not None
        assert row is not None
        assert row.runtime_status == "skipped"
        assert row.runtime_error_code == "FLEET_RUNTIME_SKIPPED_BUILD_UNAVAILABLE"
        assert attempt.failure_provider == "hetzner"
        assert attempt.failure_lane == "buildkit"
        assert attempt.private_failure_detail == (
            "A local Cargo dependency is declared but absent from the Docker image "
            "build context. Copy the dependency directory into the build stage "
            "before running cargo build (for example, COPY vendor ./vendor)."
        )
        assert attempt.failure_captured_at is not None

    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    second = await client.post(
        _CLAIM_URL,
        headers=node_headers,
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    capped = await client.post(
        _CLAIM_URL,
        headers=node_headers,
    )
    assert capped.status_code == 200, capped.text
    assert capped.json()["items"] == []


async def test_failed_trusted_build_requires_exact_manual_retry(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    build_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            TrustedImageBuild(
                build_id=build_id,
                environment="prod",
                component="screener",
                source_repository="https://github.com/ditto-assistant/ditto-subnet.git",
                source_sha="a" * 40,
                context_path=".",
                dockerfile_path="workers/screener/Dockerfile",
                destination="example.invalid/screener:sha-test",
                status="failed",
                provider="targon",
                provider_resource_id="build-failed-1",
                error_code="TARGON_BUILD_FAILED",
                attempt_count=47,
                controller_epoch="controller-before-repair",
                created_by="release@example.com",
                reason="Build the exact release candidate",
            )
        )

    response = await client.post(
        f"/api/v1/admin/trusted-image-builds/{build_id}/retry",
        headers={**_HEADERS, "X-Admin-Actor": "operator@example.com"},
        json={
            "expected_status": "failed",
            "expected_attempt_count": 47,
            "reason": "Targon builder infrastructure has been repaired",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert response.json()["attempt_count"] == 47
    assert response.json()["provider"] is None

    stale = await client.post(
        f"/api/v1/admin/trusted-image-builds/{build_id}/retry",
        headers={**_HEADERS, "X-Admin-Actor": "operator@example.com"},
        json={
            "expected_status": "failed",
            "expected_attempt_count": 47,
            "reason": "Repeat the same stale operator request",
        },
    )
    assert stale.status_code == 409

    async with session_maker() as session:
        event = await session.scalar(
            select(ScreenerCapacityEvent).where(
                ScreenerCapacityEvent.event_type == "trusted_build_manual_retry"
            )
        )
    assert event is not None
    assert str(build_id) in event.detail
    assert "operator@example.com" in event.detail


async def test_unknown_fields_ignored_and_gcp_first_keeps_targon_fallback() -> None:
    from ditto.api_models.screener_provider_settings import (
        ScreenerProviderSettings,
        ScreenerProviderSettingsWriteRequest,
    )

    settings = ScreenerProviderSettings.model_validate(
        {
            "runtime_provider_priority": ["gcp", "targon"],
            "source_review_provider_priority": ["gcp"],
            "build_provider_priority": ["gcp", "targon"],
            "future_flag": True,
        }
    )
    assert settings.runtime_provider_priority == ("gcp", "targon")
    assert settings.targon_runtime_enabled() is True
    assert settings.targon_source_review_enabled() is False
    assert settings.targon_builders_enabled() is True
    assert settings.all_lanes_gcp_only() is False
    assert settings.all_lanes_targon_first() is False

    payload = ScreenerProviderSettingsWriteRequest.model_validate(
        {
            "expected_revision": 0,
            "settings": settings.model_dump(mode="json"),
            "reason": "Cut over every lane to the old GCE path",
            "confirmation": (
                "APPLY SCREENER PROVIDERS BUILDS=gcp>targon RUNTIME=gcp>targon "
                "SOURCE_REVIEW=gcp GCE_OVERFLOW=DISABLED"
            ),
            "unknown_operator_hint": "ignored",
        }
    )
    assert "unknown_operator_hint" not in payload.model_dump()

    targon_first = ScreenerProviderSettings(
        runtime_provider_priority=("targon", "gcp"),
        source_review_provider_priority=("targon", "gcp"),
        build_provider_priority=("targon", "gcp"),
    )
    assert targon_first.all_lanes_targon_first() is True
    assert targon_first.all_lanes_gcp_only() is False
    assert ScreenerProviderSettings().all_lanes_targon_first() is False


async def test_all_lanes_gcp_only_keep_submission_work_on_gce_fleet(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.api_models.agent_status import AgentStatus
    from ditto.tests.api_server.endpoints.test_screener import (
        _AUTH_HEADER,
        _CLAIM_URL,
        _CONTROLLER_TOKEN,
        _install_chain,
        _install_db,
        _install_storage,
        _seed_agent,
    )

    _install(app, session_maker)
    _install_db(app, session_maker)
    _install_chain(app)
    _install_storage(app)
    app.state.config = replace(
        app.state.config,
        screener_auth=replace(
            app.state.config.screener_auth,
            controller_api_token=_CONTROLLER_TOKEN,
        ),
    )
    agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
    attempt_id = claim.json()["items"][0]["attempt_id"]
    controller_headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
    async with session_maker() as session, session.begin():
        session.add(
            TrustedImageBuild(
                build_id=uuid4(),
                environment="prod",
                component="screener",
                source_repository=(
                    "https://github.com/ditto-assistant/ditto-subnet.git"
                ),
                source_sha="a" * 40,
                context_path=".",
                dockerfile_path="workers/screener/Dockerfile",
                destination=(
                    "us-central1-docker.pkg.dev/ditto-app-dev/"
                    "ditto-public-runtime/screener:sha-test"
                ),
                status="succeeded",
                provider="gcp",
                image_digest="sha256:" + "b" * 64,
                completed_at=datetime.now(UTC),
                created_by="test",
                reason="provide a pinned reviewed source worker image",
            )
        )

    cutover = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["gcp"],
            builds=["gcp"],
        ),
    )
    assert cutover.status_code == 200, cutover.text

    queued_build = await client.post(
        f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
        headers=_AUTH_HEADER,
        json={"attempt_id": attempt_id},
    )
    assert queued_build.status_code == 200, queued_build.text
    assert queued_build.json()["status"] == "fallback_required"
    assert (
        queued_build.json()["error_code"]
        == "TARGON_SUBMISSION_BUILD_DISABLED_BY_POLICY"
    )
    assert queued_build.json()["runtime_status"] == "skipped"

    queued_review = await client.post(
        f"/api/v1/screener/agent/{agent_id}/submission-source-reviews",
        headers=_AUTH_HEADER,
        json={"attempt_id": attempt_id},
    )
    assert queued_review.status_code == 200, queued_review.text
    assert queued_review.json()["status"] == "fallback_required"
    assert (
        queued_review.json()["error_code"] == "TARGON_SOURCE_REVIEW_DISABLED_BY_POLICY"
    )

    build_claim = await client.post(
        "/api/v1/screener/controller/submission-image-builds/claim",
        headers=controller_headers,
        json={"environment": "prod", "controller_epoch": "builder:cutover"},
    )
    assert build_claim.status_code == 200, build_claim.text
    assert build_claim.json()["build"] is None

    runtime_claim = await client.post(
        "/api/v1/screener/controller/submission-runtime-smokes/claim",
        headers=controller_headers,
        json={"environment": "prod", "controller_epoch": "builder:cutover"},
    )
    assert runtime_claim.status_code == 200, runtime_claim.text
    assert runtime_claim.json()["artifact"] is None

    review_claim = await client.post(
        "/api/v1/screener/controller/submission-source-reviews/claim",
        headers=controller_headers,
        json={"environment": "prod", "controller_epoch": "builder:cutover"},
    )
    assert review_claim.status_code == 200, review_claim.text
    assert review_claim.json()["review"] is None

    restored = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=cutover.json()["revision"],
            screening=["targon", "gcp"],
            builds=["targon", "gcp"],
        ),
    )
    assert restored.status_code == 200, restored.text

    restore_agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    restore_claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
    restore_attempt_id = restore_claim.json()["items"][0]["attempt_id"]
    restored_build = await client.post(
        f"/api/v1/screener/agent/{restore_agent_id}/submission-image-builds",
        headers=_AUTH_HEADER,
        json={"attempt_id": restore_attempt_id},
    )
    assert restored_build.status_code == 200, restored_build.text
    assert restored_build.json()["status"] == "queued"
    leased = await client.post(
        "/api/v1/screener/controller/submission-image-builds/claim",
        headers=controller_headers,
        json={"environment": "prod", "controller_epoch": "builder:restore"},
    )
    assert leased.status_code == 200, leased.text
    assert leased.json()["build"]["build_id"] == restored_build.json()["build_id"]


async def test_gcp_then_targon_keeps_submission_work_on_gce_fleet(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.api_models.agent_status import AgentStatus
    from ditto.tests.api_server.endpoints.test_screener import (
        _AUTH_HEADER,
        _CLAIM_URL,
        _CONTROLLER_TOKEN,
        _install_chain,
        _install_db,
        _install_storage,
        _seed_agent,
    )

    _install(app, session_maker)
    _install_db(app, session_maker)
    _install_chain(app)
    _install_storage(app)
    app.state.config = replace(
        app.state.config,
        screener_auth=replace(
            app.state.config.screener_auth,
            controller_api_token=_CONTROLLER_TOKEN,
        ),
    )
    applied = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["gcp", "targon"],
            builds=["gcp", "targon"],
        ),
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["settings"]["runtime_provider_priority"] == ["gcp", "targon"]

    agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
    attempt_id = claim.json()["items"][0]["attempt_id"]
    queued = await client.post(
        f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
        headers=_AUTH_HEADER,
        json={"attempt_id": attempt_id},
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["status"] == "fallback_required"
    assert queued.json()["error_code"] == "TARGON_SUBMISSION_BUILD_DISABLED_BY_POLICY"
