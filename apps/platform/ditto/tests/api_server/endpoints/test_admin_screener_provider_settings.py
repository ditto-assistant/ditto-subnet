"""Audited Backroom control for screener and builder provider routing."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import ScreenerCapacityEvent, TrustedImageBuild

pytestmark = pytest.mark.asyncio

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_PATH = "/api/v1/admin/screener-provider-settings"


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
    }
    phrase = (
        f"APPLY SCREENER PROVIDERS BUILDS={'>'.join(builds)} "
        f"RUNTIME={'>'.join(screening)} SOURCE_REVIEW={'>'.join(screening)}"
    )
    return {
        "environment": "prod",
        "expected_revision": expected_revision,
        "settings": settings,
        "reason": "Route around scheduled Targon provider maintenance",
        "actor": "operator@example.com",
        "confirmation": confirmation if confirmation is not None else phrase,
    }


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
                "SOURCE_REVIEW=gcp"
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


async def test_all_lanes_gcp_only_still_use_decomposed_controller_lanes(
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
    assert queued_build.json()["status"] == "queued"
    assert queued_build.json()["error_code"] is None
    assert queued_build.json()["runtime_status"] == "pending"

    queued_review = await client.post(
        f"/api/v1/screener/agent/{agent_id}/submission-source-reviews",
        headers=_AUTH_HEADER,
        json={"attempt_id": attempt_id},
    )
    assert queued_review.status_code == 200, queued_review.text
    assert queued_review.json()["status"] == "queued"
    assert queued_review.json()["error_code"] is None

    build_claim = await client.post(
        "/api/v1/screener/controller/submission-image-builds/claim",
        headers=controller_headers,
        json={"environment": "prod", "controller_epoch": "builder:cutover"},
    )
    assert build_claim.status_code == 200, build_claim.text
    assert build_claim.json()["build"]["build_id"] == queued_build.json()["build_id"]

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
    assert (
        review_claim.json()["review"]["review_id"] == queued_review.json()["review_id"]
    )

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


async def test_gcp_then_targon_queues_decomposed_work_with_targon_fallback(
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
    assert queued.json()["status"] == "queued"
    assert queued.json()["error_code"] is None
