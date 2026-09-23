"""Exact-image, append-only pre-randomness V13 generation audit contract."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_v13_private_generation import (
    generation_role_digest,
)
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    V13PrivateGenerationGroup,
)
from ditto_screening_protocol.v13_private_package import V13_PRIVATE_PROFILE_SHA256

pytestmark = pytest.mark.asyncio
_BASE = "/api/v1/admin/v13-private-generation"
_HEADERS = {
    "Authorization": "Bearer test-admin-token-at-least-32-characters",
    "X-Admin-Actor": "test:independent-reviewer",
}


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


async def _seed_image(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    attempt_id: UUID,
    artifact_sha256: str,
    image_sha256: str,
    status: AgentStatus,
) -> None:
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"miner-{agent_id}",
                name=f"private-{agent_id}",
                sha256=artifact_sha256,
                status=status,
                screening_policy_version=13,
                created_at=now,
            )
        )
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                artifact_sha256=artifact_sha256,
                screener_hotkey=f"screener-{agent_id}",
                policy_version=13,
                status="quarantined" if status == AgentStatus.QUARANTINED else "passed",
                started_at=now,
                deadline=now + timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            ScreenedImageUpload(
                image_upload_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=f"screener-{agent_id}",
                storage_upload_id=f"upload-{agent_id}",
                sha256=image_sha256,
                size_bytes=1024,
                image_id=f"image-{agent_id}",
                image_ref=f"ref-{agent_id}",
                status="verified",
                expires_at=now + timedelta(days=1),
                verified_at=now,
            )
        )


def _attestation_assertion(
    approval_id: str,
    evidence_sha256: str,
    *,
    sub: str,
    email: str,
    issued_at: int | None = None,
    secret: str = "x" * 48,
) -> str:
    claims = {
        "aud": "ditto-platform-v13-benign-approval",
        "action": "attest-known-benign",
        "approval_id": approval_id,
        "evidence_sha256": evidence_sha256,
        "sub": sub,
        "email": email,
        "iat": issued_at if issued_at is not None else int(time.time()),
        "nonce": "a" * 32,
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signed = f"v1.{encoded}"
    digest = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"{signed}.{digest}"


async def test_generation_start_requires_preapproved_exact_clean_image(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    target_agent, target_attempt = uuid4(), uuid4()
    clean_agent, clean_attempt = uuid4(), uuid4()
    await _seed_image(
        session_maker,
        agent_id=target_agent,
        attempt_id=target_attempt,
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        status=AgentStatus.QUARANTINED,
    )
    await _seed_image(
        session_maker,
        agent_id=clean_agent,
        attempt_id=clean_attempt,
        artifact_sha256="c" * 64,
        image_sha256="d" * 64,
        status=AgentStatus.SCORED,
    )
    approval_payload = {
        "agent_id": str(clean_agent),
        "attempt_id": str(clean_attempt),
        "artifact_sha256": "c" * 64,
        "image_sha256": "d" * 64,
        "profile_sha256": V13_PRIVATE_PROFILE_SHA256,
        "review_evidence_sha256": "e" * 64,
        "reason": "independent benign source and behavior review",
    }
    assert (
        await client.post(f"{_BASE}/known-benign-approvals", json=approval_payload)
    ).status_code == 401
    wrong = await client.post(
        f"{_BASE}/known-benign-approvals",
        json={**approval_payload, "image_sha256": "f" * 64},
        headers=_HEADERS,
    )
    assert wrong.status_code == 409
    approved = await client.post(
        f"{_BASE}/known-benign-approvals",
        json=approval_payload,
        headers=_HEADERS,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "recorded_unverified"
    repeated = await client.post(
        f"{_BASE}/known-benign-approvals",
        json=approval_payload,
        headers=_HEADERS,
    )
    assert repeated.json()["approval_id"] == approved.json()["approval_id"]

    group_payload = {
        "target_agent_id": str(target_agent),
        "target_attempt_id": str(target_attempt),
        "target_artifact_sha256": "a" * 64,
        "target_image_sha256": "b" * 64,
        "approval_id": approved.json()["approval_id"],
        "profile_sha256": V13_PRIVATE_PROFILE_SHA256,
    }
    wrong_target = await client.post(
        f"{_BASE}/groups",
        json={**group_payload, "target_image_sha256": "f" * 64},
        headers={**_HEADERS, "X-Admin-Actor": "test:generator"},
    )
    assert wrong_target.status_code == 409
    # X-Admin-Actor is an audit label, not an authenticated principal. The
    # row remains recorded_unverified even when the labels match.
    same_claimed_actor = await client.post(
        f"{_BASE}/groups", json=group_payload, headers=_HEADERS
    )
    assert same_claimed_actor.status_code == 200
    assert same_claimed_actor.json()["status"] == "recorded_unverified"
    started = await client.post(
        f"{_BASE}/groups",
        json=group_payload,
        headers={**_HEADERS, "X-Admin-Actor": "test:generator"},
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "recorded_unverified"
    assert body["target_receipt_sha256"] != body["control_receipt_sha256"]
    assert datetime.fromisoformat(body["started_at"]) > datetime.fromisoformat(
        approved.json()["approved_at"]
    )
    async with session_maker() as session:
        row = await session.get(V13PrivateGenerationGroup, UUID(body["group_id"]))
        assert row is not None
        assert row.target_receipt_sha256 == generation_role_digest(row, "target")
        assert row.control_receipt_sha256 == generation_role_digest(row, "known_benign")
    with pytest.raises(DBAPIError):
        async with session_maker() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE v13_private_generation_groups SET actor = 'forged' "
                    "WHERE group_id = :group_id"
                ),
                {"group_id": UUID(body["group_id"])},
            )
    reread = await client.get(f"{_BASE}/groups/{body['group_id']}", headers=_HEADERS)
    assert reread.json() == body
    repeat_start = await client.post(
        f"{_BASE}/groups",
        json=group_payload,
        headers={**_HEADERS, "X-Admin-Actor": "test:generator"},
    )
    assert repeat_start.json()["group_id"] == body["group_id"]


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


async def test_generation_role_digest_matches_protocol_fixed_vector() -> None:
    group = V13PrivateGenerationGroup(
        group_id=UUID(int=1),
        target_agent_id=UUID(int=2),
        target_attempt_id=UUID(int=3),
        target_artifact_sha256="a" * 64,
        target_image_sha256="b" * 64,
        control_agent_id=UUID(int=4),
        control_attempt_id=UUID(int=5),
        control_artifact_sha256="c" * 64,
        control_image_sha256="d" * 64,
        approval_id=UUID(int=6),
        profile_sha256="e" * 64,
        started_at=datetime(2026, 9, 23, 12, 0, 45, 123456, tzinfo=UTC),
    )
    assert generation_role_digest(group, "target") == (
        "96fd7f66ddc49df69bce3c57af4a4f75c97fb4a3ed45f98a75d8cb649ac31973"
    )
    assert generation_role_digest(group, "known_benign") == (
        "1575e4205e10e50c2f2b3d3c33e6c1a920aa1897fcdd3c0d3f398c809f304d87"
    )


async def test_known_benign_needs_two_authenticated_distinct_reviewers(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, attempt_id = uuid4(), uuid4()
    await _seed_image(
        session_maker,
        agent_id=agent_id,
        attempt_id=attempt_id,
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        status=AgentStatus.SCORED,
    )
    created = await client.post(
        f"{_BASE}/known-benign-approvals",
        json={
            "agent_id": str(agent_id),
            "attempt_id": str(attempt_id),
            "artifact_sha256": "a" * 64,
            "image_sha256": "b" * 64,
            "profile_sha256": V13_PRIVATE_PROFILE_SHA256,
            "review_evidence_sha256": "e" * 64,
            "reason": "independent source and runtime review",
        },
        headers=_HEADERS,
    )
    assert created.status_code == 200, created.text
    approval_id = created.json()["approval_id"]
    path = f"{_BASE}/known-benign-approvals/{approval_id}"
    assert (await client.get(f"{path}/provenance", headers=_HEADERS)).json()[
        "status"
    ] == "recorded_unverified"

    first = _attestation_assertion(
        approval_id, "e" * 64, sub="google-sub-one", email="one@omniaura.ai"
    )
    disabled = await client.post(
        f"{path}/attest",
        json={"assertion": first, "reason": "checked source and runtime"},
        headers=_HEADERS,
    )
    assert disabled.status_code == 503
    app.state.config = replace(
        app.state.config,
        v13_benign_attestation_secret="x" * 48,
    )
    wrong_evidence = await client.post(
        f"{path}/attest",
        json={
            "assertion": _attestation_assertion(
                approval_id, "f" * 64, sub="google-sub-one", email="one@omniaura.ai"
            ),
            "reason": "checked source and runtime",
        },
        headers=_HEADERS,
    )
    assert wrong_evidence.status_code == 401
    expired = await client.post(
        f"{path}/attest",
        json={
            "assertion": _attestation_assertion(
                approval_id,
                "e" * 64,
                sub="google-sub-one",
                email="one@omniaura.ai",
                issued_at=int(time.time()) - 121,
            ),
            "reason": "checked source and runtime",
        },
        headers=_HEADERS,
    )
    assert expired.status_code == 401
    forged = await client.post(
        f"{path}/attest",
        json={
            "assertion": first[:-64] + "0" * 64,
            "reason": "checked source and runtime",
        },
        headers={**_HEADERS, "X-Admin-Actor": "second-reviewer"},
    )
    assert forged.status_code == 401
    one = await client.post(
        f"{path}/attest",
        json={"assertion": first, "reason": "checked source and runtime"},
        headers=_HEADERS,
    )
    assert one.status_code == 200, one.text
    assert one.json()["status"] == "one_authenticated_reviewer"
    same = await client.post(
        f"{path}/attest",
        json={"assertion": first, "reason": "checked again independently"},
        headers={**_HEADERS, "X-Admin-Actor": "second-reviewer"},
    )
    assert same.json()["authenticated_reviewers"] == 1
    same_email = await client.post(
        f"{path}/attest",
        json={
            "assertion": _attestation_assertion(
                approval_id, "e" * 64, sub="google-sub-two", email="one@omniaura.ai"
            ),
            "reason": "same email cannot be second reviewer",
        },
        headers=_HEADERS,
    )
    assert same_email.status_code == 409
    second = await client.post(
        f"{path}/attest",
        json={
            "assertion": _attestation_assertion(
                approval_id, "e" * 64, sub="google-sub-two", email="two@omniaura.ai"
            ),
            "reason": "checked independently too",
        },
        headers=_HEADERS,
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "two_person_authenticated"
    assert second.json()["authenticated_reviewers"] == 2
