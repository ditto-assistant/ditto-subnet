"""Retired private-audit APIs retain authenticated historical reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    ScreeningAttempt,
    V13GroupPackageRegistration,
    V13KnownBenignControlApproval,
    V13PrivateGenerationGroup,
)


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token="test-admin-token-at-least-32-characters",
    )
    app.state.session_maker = maker

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


pytestmark = pytest.mark.asyncio


async def test_generation_group_rows_are_immutable_in_postgres(
    app: FastAPI,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The database, not only the admin handler, rejects receipt mutation."""
    _install(app, session_maker)
    # The migration test inspects the trigger even before any live group exists.
    async with session_maker() as session:
        triggers = set(
            await session.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                    "AND tgname LIKE 'v13_%_immutable'"
                )
            )
        )
    assert "v13_private_generation_groups_immutable" in triggers
    assert "v13_known_benign_control_approvals_immutable" in triggers
    assert "v13_group_package_registrations_immutable" in triggers


async def test_legacy_private_reads_require_admin_and_cannot_start_generation(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    base = "/api/v1/admin/v13-private-generation"
    headers = {"Authorization": "Bearer test-admin-token-at-least-32-characters"}
    assert (await client.get(base + "/known-benign-approvals")).status_code == 401
    response = await client.get(base + "/known-benign-approvals", headers=headers)
    assert response.status_code == 200
    assert response.json() == []
    assert (
        await client.post(base + "/known-benign-approvals", json={}, headers=headers)
    ).status_code == 405
    assert (
        await client.post(base + "/groups", json={}, headers=headers)
    ).status_code == 404


async def test_private_execution_routes_are_absent_from_openapi(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    for path, operations in paths.items():
        if "/v13-private-generation" in path or "/v13-official-controls" in path:
            assert set(operations) <= {"get"}, path
        assert not path.endswith("/private-package-registration"), path
        assert not path.endswith("/private-inputs"), path
        if path.endswith("/private-receipt"):
            assert set(operations) == {"get"}


async def test_populated_legacy_group_and_role_packages_remain_readable(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    target_id, control_id, target_attempt, control_attempt = (uuid4() for _ in range(4))
    group_id, approval_id = uuid4(), uuid4()
    async with session_maker() as session, session.begin():
        for agent_id, attempt_id, sha in (
            (target_id, target_attempt, "a" * 64),
            (control_id, control_attempt, "b" * 64),
        ):
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"historical-{agent_id}",
                    name=f"historical-{agent_id}",
                    sha256=sha,
                    status=AgentStatus.QUARANTINED,
                    screening_policy_version=13,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    artifact_sha256=sha,
                    screener_hotkey="historical-worker",
                    policy_version=13,
                    status="quarantined",
                    started_at=now,
                    deadline=now + timedelta(hours=1),
                )
            )
        await session.flush()
        session.add(
            V13KnownBenignControlApproval(
                approval_id=approval_id,
                agent_id=control_id,
                attempt_id=control_attempt,
                artifact_sha256="b" * 64,
                image_sha256="d" * 64,
                profile_sha256="e" * 64,
                review_evidence_sha256="f" * 64,
                approval_receipt_sha256="1" * 64,
                actor="historical-reviewer",
                reason="Historical unverified operator claim",
                approved_at=now,
            )
        )
        await session.flush()
        session.add(
            V13PrivateGenerationGroup(
                group_id=group_id,
                target_agent_id=target_id,
                target_attempt_id=target_attempt,
                target_artifact_sha256="a" * 64,
                target_image_sha256="c" * 64,
                control_agent_id=control_id,
                control_attempt_id=control_attempt,
                control_artifact_sha256="b" * 64,
                control_image_sha256="d" * 64,
                approval_id=approval_id,
                approval_receipt_sha256="1" * 64,
                profile_sha256="e" * 64,
                target_receipt_sha256="2" * 64,
                control_receipt_sha256="3" * 64,
                actor="historical-registrar",
                started_at=now,
            )
        )
        await session.flush()
        for role, receipt in (("target", "2" * 64), ("known_benign", "3" * 64)):
            session.add(
                V13GroupPackageRegistration(
                    group_id=group_id,
                    role=role,
                    generation_receipt_sha256=receipt,
                    manifest_sha256="4" * 64,
                    pair_inventory_sha256="5" * 64,
                    registrar_actor="historical-registrar",
                    registered_at=now,
                )
            )
    headers = {"Authorization": "Bearer test-admin-token-at-least-32-characters"}
    base = "/api/v1/admin/v13-private-generation"
    group = await client.get(f"{base}/groups/{group_id}", headers=headers)
    assert group.status_code == 200, group.text
    assert group.json()["status"] == "recorded_unverified"
    assert group.json()["approval_id"] == str(approval_id)
    for role, agent_id in (("target", target_id), ("known_benign", control_id)):
        package = await client.get(
            f"{base}/groups/{group_id}/packages/{role}", headers=headers
        )
        assert package.status_code == 200, package.text
        assert package.json()["agent_id"] == str(agent_id)
        assert package.json()["status"] == "recorded_unverified"
        assert package.json()["manifest_sha256"] == "4" * 64
    approval = await client.get(
        f"{base}/known-benign-approvals/{approval_id}", headers=headers
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["approval_receipt_sha256"] == "1" * 64
    assert approval.json()["status"] == "recorded_unverified"
