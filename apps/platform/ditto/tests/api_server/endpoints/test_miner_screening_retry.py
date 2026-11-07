"""The owning miner's self-serve retry of an interrupted screening (SN-456).

The miner route writes the same exact-attempt ``screening_retry_overrides``
grant as the operator's ``retry-now``, so these tests pin the narrower rules it
adds: ownership, the ``upload`` scope, ruling and agent-fault exclusions, the
per-agent budget, the operator kill switch, and replay idempotency. The claim
test in ``test_screener.TestQuarantineAdmin`` proves the grant reaches the
screener exactly as an operator grant does.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.endpoints.miner_screening_retry import (
    MINER_SELF_SERVE_ACTOR_PREFIX,
)
from ditto.db.models import (
    AgentStatus,
    MinerScreeningRetrySettingsRevision,
    ScreeningAttempt,
    ScreeningRetryOverride,
)
from ditto.tests.api_server.endpoints.test_miner_logs import _login
from ditto.tests.api_server.endpoints.test_screener import _SCREENER_HOTKEY
from ditto.tests.api_server.endpoints.test_validator import (
    _SHA256,
    _install_chain,
    _install_db,
    _seed_agent,
)
from ditto_screening_protocol.models import SCREENING_POLICY_VERSION

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_INTERRUPTED = "Screening was interrupted; manual retry required."


async def _seed_failed(
    maker: async_sessionmaker[AsyncSession],
    *,
    owner: str,
    attempt_status: str = "failed",
    reason_code: str | None = "source-review-model-tool-call-invalid",
    agent_status: AgentStatus = AgentStatus.SCREENING_FAILED,
) -> tuple[UUID, UUID]:
    agent_id = await _seed_agent(
        maker,
        status=agent_status,
        miner_hotkey=owner,
        screening_policy_version=SCREENING_POLICY_VERSION,
    )
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status=attempt_status,
                started_at=now - timedelta(minutes=2),
                deadline=now - timedelta(seconds=1),
                finished_at=now - timedelta(seconds=30),
                public_reason=_INTERRUPTED,
                reason_code=reason_code,
            )
        )
    return agent_id, attempt_id


async def _session(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    keypair: bittensor.Keypair,
    *,
    scopes: list[str] | None = None,
) -> dict[str, str]:
    _install_db(app, maker)
    _install_chain(app)
    token = await _login(client, keypair=keypair, scopes=scopes or ["read", "upload"])
    return {"authorization": f"Bearer {token}"}


def _body(attempt_id: UUID, *, sha: str = _SHA256) -> dict[str, object]:
    return {
        "expected_attempt_id": str(attempt_id),
        "expected_sha256": sha,
        "acknowledge_cost": True,
    }


async def _overrides(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> list[ScreeningRetryOverride]:
    async with maker() as session:
        return list(
            await session.scalars(
                select(ScreeningRetryOverride).where(
                    ScreeningRetryOverride.agent_id == agent_id
                )
            )
        )


@pytest.mark.asyncio
async def test_owner_retry_is_recorded_and_replay_is_idempotent(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, attempt_id = await _seed_failed(session_maker, owner=miner.ss58_address)
    headers = await _session(app, client, session_maker, miner)

    status = await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-retry", headers=headers
    )
    assert status.status_code == 200, status.text
    view = status.json()
    assert view["eligible"] is True
    assert view["blocker"] is None
    assert view["latest_attempt_id"] == str(attempt_id)
    assert view["artifact_sha256"] == _SHA256
    assert view["retries_used"] == 0
    assert view["retries_allowed"] == 1
    assert "TAO" in view["cost_notice"]

    first = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(attempt_id),
    )
    assert first.status_code == 200, first.text
    assert first.json()["idempotent"] is False
    assert first.json()["authorized_by_operator"] is False
    assert first.json()["retries_used"] == 1

    again = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(attempt_id),
    )
    assert again.status_code == 200, again.text
    assert again.json()["idempotent"] is True
    assert again.json()["override_id"] == first.json()["override_id"]

    overrides = await _overrides(session_maker, agent_id)
    assert len(overrides) == 1
    assert overrides[0].actor == f"{MINER_SELF_SERVE_ACTOR_PREFIX}{miner.ss58_address}"
    assert overrides[0].force_full_review is False
    assert overrides[0].attempt_id == attempt_id

    after = await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-retry", headers=headers
    )
    assert after.json()["already_authorized"] is True
    assert after.json()["eligible"] is False


@pytest.mark.asyncio
async def test_other_miner_gets_404_for_read_and_write(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    owner = bittensor.Keypair.create_from_uri("//Alice")
    attacker = bittensor.Keypair.create_from_uri("//Bob")
    agent_id, attempt_id = await _seed_failed(session_maker, owner=owner.ss58_address)
    headers = await _session(app, client, session_maker, attacker)

    read = await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-retry", headers=headers
    )
    write = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(attempt_id),
    )
    assert read.status_code == 404
    assert write.status_code == 404
    assert await _overrides(session_maker, agent_id) == []


@pytest.mark.asyncio
async def test_read_only_session_cannot_retry(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, attempt_id = await _seed_failed(session_maker, owner=miner.ss58_address)
    headers = await _session(app, client, session_maker, miner, scopes=["read"])

    status = await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-retry", headers=headers
    )
    assert status.status_code == 200
    denied = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(attempt_id),
    )
    assert denied.status_code == 403
    assert "upload" in denied.json()["message"]
    assert await _overrides(session_maker, agent_id) == []


@pytest.mark.asyncio
async def test_cost_acknowledgement_is_required(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, attempt_id = await _seed_failed(session_maker, owner=miner.ss58_address)
    headers = await _session(app, client, session_maker, miner)

    for acknowledge in (False, 1, "true", None):
        body = _body(attempt_id)
        if acknowledge is None:
            body.pop("acknowledge_cost")
        else:
            body["acknowledge_cost"] = acknowledge
        response = await client.post(
            f"/api/v1/me/agents/{agent_id}/screening-retry",
            headers=headers,
            json=body,
        )
        assert response.status_code == 422
    assert await _overrides(session_maker, agent_id) == []


@pytest.mark.asyncio
async def test_stale_guards_are_refused(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, attempt_id = await _seed_failed(session_maker, owner=miner.ss58_address)
    headers = await _session(app, client, session_maker, miner)

    wrong_attempt = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(uuid4()),
    )
    wrong_sha = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(attempt_id, sha="cd" * 32),
    )
    assert wrong_attempt.status_code == 409
    assert "attempt changed" in wrong_attempt.json()["message"]
    assert wrong_sha.status_code == 409
    assert "artifact identity changed" in wrong_sha.json()["message"]
    assert await _overrides(session_maker, agent_id) == []


@pytest.mark.parametrize(
    ("agent_status", "attempt_status", "reason_code", "blocker"),
    (
        # A review ruling is never a miner retry; it has the appeal path.
        (AgentStatus.REJECTED, "rejected", "docker-build", "not_screening_failed"),
        (
            AgentStatus.SCREENING_FAILED,
            "quarantined",
            "source-review-inconclusive",
            "attempt_not_retryable",
        ),
        # The artifact owns a deterministic failure.
        (
            AgentStatus.SCREENING_FAILED,
            "failed",
            "docker-build",
            "reason_not_retryable",
        ),
        # An operator policy hold.
        (
            AgentStatus.SCREENING_FAILED,
            "failed",
            "repeatedly-inconclusive",
            "reason_not_retryable",
        ),
        # ATH holds belong to operators even though retry-now accepts them.
        (
            AgentStatus.ATH_PENDING_REVIEW,
            "failed",
            "worker-lease-orphaned",
            "not_screening_failed",
        ),
    ),
)
@pytest.mark.asyncio
async def test_rulings_and_owned_failures_are_not_retryable(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    agent_status: AgentStatus,
    attempt_status: str,
    reason_code: str,
    blocker: str,
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, attempt_id = await _seed_failed(
        session_maker,
        owner=miner.ss58_address,
        agent_status=agent_status,
        attempt_status=attempt_status,
        reason_code=reason_code,
    )
    headers = await _session(app, client, session_maker, miner)

    status = await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-retry", headers=headers
    )
    assert status.json()["eligible"] is False
    assert status.json()["blocker"] == blocker
    response = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(attempt_id),
    )
    assert response.status_code == 409
    assert blocker in response.json()["message"]
    assert await _overrides(session_maker, agent_id) == []


@pytest.mark.parametrize(
    "reason_code",
    (
        # Unregistered (the SN-456 incident's code): the registry's default is
        # an operator retry, which the miner may now authorize.
        "source-review-model-tool-call-invalid",
        "worker-lease-orphaned",
        "screening-lease-expired",
        "source-review-model-timeout",
        None,
    ),
)
@pytest.mark.asyncio
async def test_infrastructure_shaped_failures_are_retryable(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    reason_code: str | None,
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, _ = await _seed_failed(
        session_maker,
        owner=miner.ss58_address,
        attempt_status="expired",
        reason_code=reason_code,
    )
    headers = await _session(app, client, session_maker, miner)

    status = await client.get(
        f"/api/v1/me/agents/{agent_id}/screening-retry", headers=headers
    )
    assert status.json()["eligible"] is True, status.json()


@pytest.mark.asyncio
async def test_budget_is_per_agent_and_operator_grants_do_not_count(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, first_attempt = await _seed_failed(
        session_maker, owner=miner.ss58_address
    )
    headers = await _session(app, client, session_maker, miner)
    used = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(first_attempt),
    )
    assert used.status_code == 200, used.text

    # The retry ran and failed again: a newer terminal attempt.
    second_attempt = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=second_attempt,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="failed",
                started_at=now,
                deadline=now,
                finished_at=now,
                public_reason=_INTERRUPTED,
                reason_code="worker-lease-orphaned",
            )
        )
    exhausted = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(second_attempt),
    )
    assert exhausted.status_code == 409
    assert "budget_exhausted" in exhausted.json()["message"]

    # An operator then retries it; the miner's replay reports that grant.
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    operator = await client.post(
        f"/api/v1/admin/screening-submissions/{agent_id}/retry-now",
        headers={**_ADMIN_HEADERS, "X-Admin-Actor": "backroom:test-user"},
        json={
            "reason": "Operator retry after the miner budget ran out",
            "expected_sha256": _SHA256,
            "expected_score_count": 0,
            "expected_attempt_id": str(second_attempt),
        },
    )
    assert operator.status_code == 200, operator.text
    replay = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(second_attempt),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent"] is True
    assert replay.json()["authorized_by_operator"] is True
    assert replay.json()["retries_used"] == 1


@pytest.mark.asyncio
async def test_operator_kill_switch_pauses_and_is_audited(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    miner = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, attempt_id = await _seed_failed(session_maker, owner=miner.ss58_address)
    headers = await _session(app, client, session_maker, miner)
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    current = await client.get(
        "/api/v1/admin/miner-screening-retry-settings", headers=_ADMIN_HEADERS
    )
    assert current.status_code == 200, current.text
    assert current.json()["current"]["enabled"] is True
    assert current.json()["current"]["max_retries_per_agent"] == 1
    revision = current.json()["current"]["revision"]

    wrong_phrase = await client.post(
        "/api/v1/admin/miner-screening-retry-settings",
        headers=_ADMIN_HEADERS,
        json={
            "expected_revision": revision,
            "enabled": False,
            "max_retries_per_agent": 1,
            "reason": "Pause while the reviewer is failing every attempt",
            "confirmation": "yes",
        },
    )
    assert wrong_phrase.status_code == 409
    paused = await client.post(
        "/api/v1/admin/miner-screening-retry-settings",
        headers=_ADMIN_HEADERS,
        json={
            "expected_revision": revision,
            "enabled": False,
            "max_retries_per_agent": 1,
            "reason": "Pause while the reviewer is failing every attempt",
            "actor": "backroom:test-user",
            "confirmation": "DISABLE MINER SCREENING RETRY",
        },
    )
    assert paused.status_code == 200, paused.text
    stale = await client.post(
        "/api/v1/admin/miner-screening-retry-settings",
        headers=_ADMIN_HEADERS,
        json={
            "expected_revision": revision,
            "enabled": True,
            "max_retries_per_agent": 2,
            "reason": "Stale writer must not win this race",
            "confirmation": "ENABLE MINER SCREENING RETRY MAX 2",
        },
    )
    assert stale.status_code == 409

    response = await client.post(
        f"/api/v1/me/agents/{agent_id}/screening-retry",
        headers=headers,
        json=_body(attempt_id),
    )
    assert response.status_code == 409
    assert "disabled" in response.json()["message"]
    assert await _overrides(session_maker, agent_id) == []

    async with session_maker() as session:
        rows = list(
            await session.scalars(
                select(MinerScreeningRetrySettingsRevision).order_by(
                    MinerScreeningRetrySettingsRevision.revision
                )
            )
        )
    assert [row.enabled for row in rows][-2:] == [True, False]
    assert rows[-1].actor == "backroom:test-user"


@pytest.mark.asyncio
async def test_admin_settings_require_admin_token(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install_db(app, session_maker)
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
    response = await client.get("/api/v1/admin/miner-screening-retry-settings")
    assert response.status_code in (401, 403)
