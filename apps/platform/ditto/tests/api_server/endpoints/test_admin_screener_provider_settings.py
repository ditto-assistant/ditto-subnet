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

from ditto.api_models.screener_node_settings import (
    ScreenerNodeChannelSettings,
    node_channel_settings_confirmation,
)
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    ScreenerCapacityEvent,
    ScreenerCapacitySnapshot,
    ScreenerHeartbeat,
    ScreenerNode,
    ScreenerNodeBootstrapGrant,
    ScreenerNodeChannelSettingsRevision,
    ScreenerProviderSettingsRevision,
    ScreenerReplayProcessKey,
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
        "runtime_provider_priority": ["gcp"],
        "source_review_provider_priority": ["gcp"],
        "build_provider_priority": ["gcp"],
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
            screening=["hetzner", "gcp"],
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
    assert capacity.json()["event_retention_days"] == 30

    stale = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["hetzner", "gcp"],
            builds=["hetzner", "gcp"],
        ),
    )
    assert stale.status_code == 409


@pytest.mark.parametrize(("configured", "reported"), [(0, None), (30, 30), (90, 90)])
async def test_capacity_view_reports_the_event_retention_window(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    configured: int,
    reported: int | None,
) -> None:
    _install(app, session_maker)
    app.state.config = replace(
        app.state.config,
        screener_auth=replace(
            app.state.config.screener_auth,
            capacity_event_retention_days=configured,
        ),
    )

    capacity = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)

    assert capacity.status_code == 200, capacity.text
    # Zero means "keep everything", which the operator sees as no window at all.
    assert capacity.json()["event_retention_days"] == reported


@pytest.mark.parametrize("enabled", [True, False])
async def test_capacity_view_reports_the_legacy_bearer_posture(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    enabled: bool,
) -> None:
    _install(app, session_maker)
    app.state.config = replace(
        app.state.config,
        screener_auth=replace(
            app.state.config.screener_auth, legacy_bearer_enabled=enabled
        ),
    )

    capacity = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)

    assert capacity.status_code == 200, capacity.text
    assert capacity.json()["legacy_bearer_accepted"] is enabled
    assert "api_token" not in capacity.text


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
        "canary_concurrency": 1,
    }
    assert initial.json()["usage"]["canary_active"] == 0
    assert initial.json()["usage"]["canary_queued"] == 0

    settings = {
        "screening_concurrency": 8,
        "sandbox_slots": 3,
        "build_concurrency": 3,
        "runtime_concurrency": 3,
        "source_review_concurrency": 6,
        "canary_concurrency": 2,
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
                "RUNTIME=3 SOURCE_REVIEW=6 CANARY=2"
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
                "RUNTIME=3 SOURCE_REVIEW=6 CANARY=2"
            ),
        },
    )
    assert stale.status_code == 409

    capacity = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)
    assert capacity.status_code == 200, capacity.text
    assert capacity.json()["node_controls"][0]["current"]["settings"] == settings


_OPEN_NODE_SETTINGS = dict.fromkeys(
    (
        "screening_concurrency",
        "sandbox_slots",
        "build_concurrency",
        "runtime_concurrency",
        "source_review_concurrency",
    ),
    4,
)
_CLOSE_CONFIRMATION = (
    "APPLY SCREENER NODE subnet-screener-1 SCREENING=0 SANDBOX=4 BUILD=4 "
    "RUNTIME=4 SOURCE_REVIEW=4 CANARY=1 CLOSE PRODUCTION ADMISSION"
)
_CLOSE_ADMISSION = {
    "environment": "prod",
    "expected_revision": 1,
    "settings": {**_OPEN_NODE_SETTINGS, "screening_concurrency": 0},
    "reason": "Pause production admission on the primary",
    "actor": "operator@example.com",
    "confirmation": _CLOSE_CONFIRMATION,
}


async def _seed_open_primary(maker: async_sessionmaker[AsyncSession]) -> None:
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
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
                capacity=4,
            )
        )
        session.add(
            ScreenerNodeChannelSettingsRevision(
                environment="prod",
                node_id="subnet-screener-1",
                parent_revision=0,
                settings=_OPEN_NODE_SETTINGS,
                reason="Open production admission on the primary",
                actor="test",
            )
        )
        session.add(
            ScreenerProviderSettingsRevision(
                environment="prod",
                parent_revision=0,
                settings={
                    "runtime_provider_priority": ["hetzner", "gcp"],
                    "source_review_provider_priority": ["hetzner", "gcp"],
                    "build_provider_priority": ["hetzner", "gcp"],
                    "gce_overflow_enabled": False,
                    "primary_node_id": "subnet-screener-1",
                },
                reason="Route screening to the Hetzner primary",
                actor="test",
            )
        )


async def test_closing_last_node_with_backlog_needs_only_explicit_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.api_models.agent_status import AgentStatus
    from ditto.tests.api_server.endpoints.test_screener import _seed_agent

    open_settings = ScreenerNodeChannelSettings(**_OPEN_NODE_SETTINGS)
    assert node_channel_settings_confirmation("subnet-screener-1", open_settings) == (
        "APPLY SCREENER NODE subnet-screener-1 SCREENING=4 SANDBOX=4 BUILD=4 "
        "RUNTIME=4 SOURCE_REVIEW=4 CANARY=1"
    )
    _install(app, session_maker)
    await _seed_open_primary(session_maker)
    await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    path = "/api/v1/admin/screener-nodes/subnet-screener-1/channel-settings"

    unsuffixed = await client.post(
        path,
        headers=_HEADERS,
        json={
            **_CLOSE_ADMISSION,
            "confirmation": _CLOSE_CONFIRMATION.removesuffix(
                " CLOSE PRODUCTION ADMISSION"
            ),
        },
    )
    assert unsuffixed.status_code == 409
    assert _CLOSE_CONFIRMATION in unsuffixed.text

    # Closure is a deliberate operator stop, even with waiting agents and
    # no GCE overflow to pick them up.
    closed = await client.post(path, headers=_HEADERS, json=_CLOSE_ADMISSION)
    assert closed.status_code == 200, closed.text
    assert closed.json()["settings"]["screening_concurrency"] == 0


async def test_legacy_node_revision_reads_one_canary_and_counts_canary_leases(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.api_models.agent_status import AgentStatus
    from ditto.db.models import ScreenerL2ReportCanary, ScreeningAttempt
    from ditto.db.queries.screener_node_settings import (
        resolve_screener_node_channel_settings,
    )
    from ditto.tests.api_server.endpoints.test_screener import _seed_agent

    _install(app, session_maker)
    # The seeded revision is stored JSON written before canary_concurrency.
    await _seed_open_primary(session_maker)
    assert "canary_concurrency" not in _OPEN_NODE_SETTINGS
    async with session_maker() as session:
        revision, limits = await resolve_screener_node_channel_settings(
            session, node_id="subnet-screener-1"
        )
    assert revision == 1
    assert limits.canary_concurrency == 1

    sha = "e" * 64
    agent_id = await _seed_agent(session_maker, status=AgentStatus.REJECTED, sha256=sha)
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        attempt_ids = [uuid4() for _ in range(3)]
        for attempt_id in attempt_ids:
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    artifact_sha256=sha,
                    screener_hotkey="5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
                    policy_version=13,
                    status="rejected",
                    started_at=now - timedelta(minutes=1),
                    deadline=now,
                    finished_at=now,
                )
            )
        await session.flush()
        # One live lease, one lease past its deadline, one still queued.
        for attempt_id, status, lease_expires_at in (
            (attempt_ids[0], "leased", now + timedelta(minutes=30)),
            (attempt_ids[1], "leased", now - timedelta(minutes=1)),
            (attempt_ids[2], "queued", None),
        ):
            session.add(
                ScreenerL2ReportCanary(
                    canary_id=uuid4(),
                    request_id=uuid4(),
                    agent_id=agent_id,
                    source_attempt_id=attempt_id,
                    artifact_sha256=sha,
                    policy_version=13,
                    bench_version=13,
                    target_node_id="subnet-screener-1",
                    expected_agent_status="rejected",
                    expected_score_count=0,
                    review_label="known_reject",
                    run_mode="source_only",
                    status=status,
                    claimed_instance_id=(
                        "subnet-screener-1-worker-1" if status == "leased" else None
                    ),
                    lease_expires_at=lease_expires_at,
                )
            )

    path = "/api/v1/admin/screener-nodes/subnet-screener-1/channel-settings"
    control = await client.get(path, headers=_HEADERS)
    assert control.status_code == 200, control.text
    assert control.json()["current"]["settings"]["canary_concurrency"] == 1
    assert control.json()["usage"]["canary_active"] == 1
    assert control.json()["usage"]["canary_queued"] == 1

    # The confirmation must name the canary cap the revision will store,
    # including the default an operator did not type.
    legacy_confirmation = (
        "APPLY SCREENER NODE subnet-screener-1 SCREENING=4 SANDBOX=4 BUILD=4 "
        "RUNTIME=4 SOURCE_REVIEW=4"
    )
    write = {
        "environment": "prod",
        "expected_revision": 1,
        "settings": _OPEN_NODE_SETTINGS,
        "reason": "Reapply the primary limits after the canary field",
        "actor": "operator@example.com",
        "confirmation": legacy_confirmation,
    }
    refused = await client.post(path, headers=_HEADERS, json=write)
    assert refused.status_code == 409
    assert f"{legacy_confirmation} CANARY=1" in refused.text
    applied = await client.post(
        path,
        headers=_HEADERS,
        json={
            **write,
            "settings": {**_OPEN_NODE_SETTINGS, "canary_concurrency": 0},
            "confirmation": f"{legacy_confirmation} CANARY=0",
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["settings"]["canary_concurrency"] == 0
    too_many = await client.post(
        path,
        headers=_HEADERS,
        json={
            **write,
            "expected_revision": 2,
            "settings": {**_OPEN_NODE_SETTINGS, "canary_concurrency": 9},
            "confirmation": f"{legacy_confirmation} CANARY=9",
        },
    )
    assert too_many.status_code == 422


async def test_independent_replay_capacity_is_guarded_and_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ditto.api_server.endpoints import admin_screener_capacity

    _install(app, session_maker)
    now = datetime.now(UTC)
    source_hotkey = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
    replay_hotkey = "5EKvqERH4xCV2MuQwb8cenyCVayfvrfjHaoeDPb9RFXxbsND"
    async with session_maker() as session, session.begin():
        for node_id, hotkey in (
            ("subnet-screener-1", source_hotkey),
            ("subnet-screener-2", replay_hotkey),
        ):
            session.add(
                ScreenerNode(
                    environment="prod",
                    node_id=node_id,
                    provider="hetzner",
                    provider_resource_id=f"robot-{node_id}",
                    screener_hotkey=hotkey,
                    token_hash=hashlib.sha256(
                        (node_id + _NODE_TOKEN).encode()
                    ).hexdigest(),
                    token_expires_at=now + timedelta(hours=6),
                    status="active",
                    capacity=1,
                )
            )
    path = "/api/v1/admin/screener-nodes/subnet-screener-2/verification-replay-capacity"
    headers = {**_HEADERS, "X-Admin-Actor": "operator@example.com"}
    payload = {
        "expected_hotkey": replay_hotkey,
        "expected_status": "active",
        "expected_capacity": 0,
        "capacity": 1,
        "reason": "Enable one report-only canary after independent enrollment",
        "confirmation": (
            f"SET SCREENER NODE subnet-screener-2 HOTKEY={replay_hotkey} "
            "REPLAY_CAPACITY=1"
        ),
    }
    before = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)
    assert before.status_code == 200
    assert (
        next(n for n in before.json()["nodes"] if n["node_id"] == "subnet-screener-2")[
            "verification_replay_capacity"
        ]
        == 0
    )
    assert (
        await client.post(path, headers=headers, json={**payload, "capacity": 2})
    ).status_code == 422
    assert (
        await client.post(
            path, headers=headers, json={**payload, "expected_hotkey": source_hotkey}
        )
    ).status_code == 409
    # Enrolled identity alone cannot turn on replay without its signed runner.
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    monkeypatch.setattr(
        admin_screener_capacity,
        "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE",
        (0, 999, 0),
    )
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerHeartbeat(
                screener_hotkey=replay_hotkey,
                instance_id="subnet-screener-2-worker-1",
                software_version="v0.999.0",
                protocol_version=7,
                policy_version=13,
                state="polling",
                first_seen_at=now - timedelta(minutes=10),
                reported_at=now - timedelta(minutes=10),
                seen_at=now - timedelta(minutes=10),
                signature="ab" * 64,
                system_metrics={
                    "release": {
                        "builtin_policy_version": 13,
                        "revision": "a" * 40,
                        "version": "v0.999.0",
                        "activated_at": int(now.timestamp()),
                    }
                },
            )
        )
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(
            ScreenerHeartbeat, (replay_hotkey, "subnet-screener-2-worker-1")
        )
        assert heartbeat is not None
        heartbeat.seen_at = now
        heartbeat.policy_version = 12
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(
            ScreenerHeartbeat, (replay_hotkey, "subnet-screener-2-worker-1")
        )
        assert heartbeat is not None
        heartbeat.policy_version = 13
        heartbeat.system_metrics = {
            "release": {
                "builtin_policy_version": 13,
                "revision": "a" * 40,
                "version": "v0.998.9",
                "activated_at": int(now.timestamp()),
            }
        }
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(
            ScreenerHeartbeat, (replay_hotkey, "subnet-screener-2-worker-1")
        )
        assert heartbeat is not None
        heartbeat.system_metrics = {
            "release": {
                "builtin_policy_version": 13,
                "revision": "a" * 40,
                "version": "v0.999.0",
                "activated_at": int(now.timestamp()),
            }
        }
        session.add(
            ScreenerHeartbeat(
                screener_hotkey=replay_hotkey,
                instance_id="subnet-screener-2-worker-2",
                software_version="v0.998.9",
                protocol_version=7,
                policy_version=13,
                state="polling",
                first_seen_at=now,
                reported_at=now,
                seen_at=now,
                signature="cd" * 64,
                system_metrics={
                    "release": {
                        "builtin_policy_version": 13,
                        "revision": "b" * 40,
                        "version": "v0.998.9",
                        "activated_at": int(now.timestamp()),
                    }
                },
            )
        )
    # One newly adopted worker cannot enable while its sibling can still claim.
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    async with session_maker() as session, session.begin():
        heartbeat = await session.get(
            ScreenerHeartbeat, (replay_hotkey, "subnet-screener-2-worker-2")
        )
        assert heartbeat is not None
        heartbeat.system_metrics = {
            "release": {
                "builtin_policy_version": 13,
                "revision": "b" * 40,
                "version": "v0.999.0",
                "activated_at": int(now.timestamp()),
            }
        }
    # A second ordinary worker is not the single signed replay process, even
    # when both workers advertise a new enough release.
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    key_hex = "a1" * 32
    key_sha = hashlib.sha256(bytes.fromhex(key_hex)).hexdigest()
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerReplayProcessKey(
                node_id="subnet-screener-2",
                instance_id="subnet-screener-2-worker-1",
                public_key_hex=key_hex,
                key_sha256=key_sha,
                revision=1,
                status="active",
                registered_at=now,
            )
        )
        primary = await session.get(
            ScreenerHeartbeat, (replay_hotkey, "subnet-screener-2-worker-1")
        )
        sibling = await session.get(
            ScreenerHeartbeat, (replay_hotkey, "subnet-screener-2-worker-2")
        )
        assert primary is not None and sibling is not None
        assert isinstance(primary.system_metrics, dict)
        primary.system_metrics = {
            **primary.system_metrics,
            "replay_process": {"key_sha256": key_sha},
        }
        sibling.seen_at = now - timedelta(minutes=10)
    applied = await client.post(path, headers=headers, json=payload)
    assert applied.status_code == 204, applied.text
    assert (await client.post(path, headers=headers, json=payload)).status_code == 409
    after = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)
    assert (
        next(n for n in after.json()["nodes"] if n["node_id"] == "subnet-screener-2")[
            "verification_replay_capacity"
        ]
        == 1
    )
    assert any(
        e["event_type"] == "verification_replay_capacity_changed"
        and "operator@example.com" in e["detail"]
        for e in after.json()["events"]
    )
    # Emergency disable remains available even when worker evidence disappears.
    monkeypatch.setattr(
        admin_screener_capacity, "_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE", None
    )
    disabled = await client.post(
        path,
        headers=headers,
        json={
            **payload,
            "expected_capacity": 1,
            "capacity": 0,
            "confirmation": (
                f"SET SCREENER NODE subnet-screener-2 HOTKEY={replay_hotkey} "
                "REPLAY_CAPACITY=0"
            ),
        },
    )
    assert disabled.status_code == 204, disabled.text


async def test_capacity_attributes_all_persistent_worker_heartbeats_to_node(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The capacity view must not hide suffixed workers behind a base node id."""
    _install(app, session_maker)
    now = datetime.now(UTC)
    hotkey = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
    node_id = "subnet-screener-1"
    metrics = {
        "system_metrics": {
            "collected_at": int(now.timestamp()),
            "cpu_percent": 25,
            "memory_percent": 15,
            "disk_percent": 0,
            "docker": {
                "status": "healthy",
                "running_containers": 2,
                "unhealthy_containers": 0,
            },
        },
        "screening_progress": {"stage": "building", "started_at": int(now.timestamp())},
        "host_specs": {
            "cpu_count": 32,
            "cpu_physical_cores": 24,
            "memory_total_mib": 64075,
            "disk_total_gib": 1726,
            "architecture": "x86_64",
        },
        "release": {
            "builtin_policy_version": 12,
            "revision": "c393bc10488ee0b203d08e6d29df877d508f25d9",
            "version": "0.230.0",
            "activated_at": 1788717939,
        },
    }
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id=node_id,
                provider="hetzner",
                provider_resource_id="3062657",
                screener_hotkey=hotkey,
                token_hash=hashlib.sha256(_NODE_TOKEN.encode()).hexdigest(),
                token_expires_at=now + timedelta(hours=6),
                status="active",
                capacity=2,
            )
        )
        session.add_all(
            [
                ScreenerHeartbeat(
                    screener_hotkey=hotkey,
                    instance_id=f"{node_id}-worker-1",
                    software_version="0.21.2",
                    protocol_version=6,
                    policy_version=10,
                    state="screening",
                    first_seen_at=now - timedelta(minutes=1),
                    reported_at=now - timedelta(seconds=4),
                    seen_at=now - timedelta(seconds=4),
                    signature="ab" * 64,
                    system_metrics=metrics,
                ),
                ScreenerHeartbeat(
                    screener_hotkey=hotkey,
                    instance_id=f"{node_id}-worker-2",
                    software_version="0.21.2",
                    protocol_version=6,
                    policy_version=10,
                    state="polling",
                    first_seen_at=now - timedelta(minutes=1),
                    reported_at=now - timedelta(seconds=1),
                    seen_at=now - timedelta(seconds=1),
                    signature="cd" * 64,
                    system_metrics=metrics,
                ),
                ScreenerHeartbeat(
                    screener_hotkey=hotkey,
                    instance_id="subnet-screener-10-worker-1",
                    software_version="0.21.2",
                    protocol_version=6,
                    policy_version=10,
                    state="polling",
                    first_seen_at=now - timedelta(minutes=1),
                    reported_at=now,
                    seen_at=now,
                    signature="ef" * 64,
                    system_metrics=metrics,
                ),
            ]
        )

    capacity = await client.get("/api/v1/admin/screener-capacity", headers=_HEADERS)
    assert capacity.status_code == 200, capacity.text
    node = capacity.json()["nodes"][0]
    assert node["heartbeat_seen_at"] == (
        (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    )
    assert node["current_phase"] == "building"
    assert [worker["instance_id"] for worker in node["workers"]] == [
        f"{node_id}-worker-2",
        f"{node_id}-worker-1",
    ]
    assert node["workers"][0]["system_metrics"] == {
        "collected_at": int(now.timestamp()),
        "cpu_percent": 25,
        "memory_percent": 15,
        "disk_percent": 0,
        "docker": {
            "status": "healthy",
            "running_containers": 2,
            "unhealthy_containers": 0,
        },
    }
    assert node["workers"][0]["host_specs"]["cpu_count"] == 32
    assert node["workers"][0]["release"]["builtin_policy_version"] == 12
    assert (
        node["workers"][0]["release"]["revision"]
        == "c393bc10488ee0b203d08e6d29df877d508f25d9"
    )
    assert node["release"]["version"] == "0.230.0"


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
    assert settings.all_lanes_gcp_only() is False

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
    assert targon_first.runtime_provider_priority[0] == "targon"
    assert targon_first.all_lanes_gcp_only() is False
    assert ScreenerProviderSettings().all_lanes_gcp_only() is True


async def test_new_targon_routing_is_rejected(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    response = await client.post(
        _PATH,
        headers=_HEADERS,
        json=_payload(
            expected_revision=0,
            screening=["targon", "gcp"],
            builds=["targon", "gcp"],
        ),
    )
    assert response.status_code == 422, response.text


async def test_hetzner_job_claim_does_not_preclaim_signed_worker_attempt(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    from ditto.api_models.agent_status import AgentStatus
    from ditto.db.models import ScreeningAttempt, SubmissionImageBuild
    from ditto.tests.api_server.endpoints.test_screener import (
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
                capacity=1,
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
    node_headers = {
        "Authorization": f"Bearer {_NODE_TOKEN}",
        "X-Screener-Hotkey": _SCREENER_HOTKEY,
    }
    limits = await client.post(
        "/api/v1/admin/screener-nodes/subnet-screener-1/channel-settings",
        headers=_HEADERS,
        json={
            "environment": "prod",
            "expected_revision": 0,
            "settings": {
                "screening_concurrency": 1,
                "sandbox_slots": 0,
                "build_concurrency": 0,
                "runtime_concurrency": 0,
                "source_review_concurrency": 0,
            },
            "reason": "Permit the signed screener worker to claim work",
            "actor": "operator@example.com",
            "confirmation": (
                "APPLY SCREENER NODE subnet-screener-1 SCREENING=1 "
                "SANDBOX=0 BUILD=0 RUNTIME=0 SOURCE_REVIEW=0 CANARY=1"
            ),
        },
    )
    assert limits.status_code == 200, limits.text
    agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
    idle_job = await client.post(
        "/api/v1/screener/nodes/jobs/submission-image-builds/claim",
        headers=node_headers,
        json={"environment": "prod"},
    )
    assert idle_job.status_code == 200, idle_job.text
    assert idle_job.json()["build"] is None
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(ScreeningAttempt).where(ScreeningAttempt.agent_id == agent_id)
            )
            is None
        )
        assert (
            await session.scalar(
                select(SubmissionImageBuild).where(
                    SubmissionImageBuild.agent_id == agent_id
                )
            )
            is None
        )
    signed_claim = await client.post(_CLAIM_URL, headers=node_headers)
    assert signed_claim.status_code == 200, signed_claim.text
    assert len(signed_claim.json()["items"]) == 1
    assert signed_claim.json()["items"][0]["agent_id"] == str(agent_id)
