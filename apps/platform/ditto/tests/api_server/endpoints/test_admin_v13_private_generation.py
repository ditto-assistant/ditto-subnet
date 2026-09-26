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
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints.admin_v13_private_generation import (
    generation_role_digest,
)
from ditto.api_server.endpoints.verification_replay import (
    append_replay_private_receipt,
    get_replay_private_inputs,
    get_replay_private_statistics,
)
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreenerNode,
    ScreeningAttempt,
    ScreeningPrivatePackageRegistration,
    ScreeningQuarantine,
    ScreeningVerificationReplay,
    V13PrivateGenerationGroup,
    V13ReplayPrivateGenerationGroup,
)
from ditto_screening_protocol.v13_private_clean_control import (
    TrustedGenerationGroup,
    V13MatchedCleanControlCommitment,
    compute_v13_generation_role_digest,
)
from ditto_screening_protocol.v13_private_execute import (
    PrivateExecutionResult,
    PrivatePairCounts,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    V13PrivateRunSummary,
)
from ditto_screening_protocol.v13_private_receipt import V13ReplayPrivateReceipt
from ditto_screening_protocol.v13_replay_observation import V13ReplayBinding

pytestmark = pytest.mark.asyncio
_BASE = "/api/v1/admin/v13-private-generation"
_HEADERS = {
    "Authorization": "Bearer test-admin-token-at-least-32-characters",
    "X-Admin-Actor": "test:independent-reviewer",
}


_ATTESTATION_SECRET = "y" * 48


def _assertion(
    approval_id: str,
    evidence_sha256: str,
    *,
    sub: str,
    email: str,
    action: str = "attest-known-benign",
    secret: str = _ATTESTATION_SECRET,
) -> str:
    claims = {
        "aud": "ditto-platform-v13-benign-approval",
        "action": action,
        "approval_id": approval_id,
        "evidence_sha256": evidence_sha256,
        "sub": sub,
        "email": email,
        "iat": int(time.time()),
        "nonce": hashlib.sha256(f"{sub}:{action}:{approval_id}".encode()).hexdigest()[
            :32
        ],
    }
    encoded = (
        base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signed = f"v1.{encoded}"
    digest = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"{signed}.{digest}"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(
        app.state.config,
        admin_api_token="test-admin-token-at-least-32-characters",
        v13_benign_attestation_secret=_ATTESTATION_SECRET,
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


async def test_replay_generation_uses_independent_verified_image(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    target_agent, target_attempt, clean_agent, clean_attempt = (
        uuid4() for _ in range(4)
    )
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
    replay_id, quarantine_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningQuarantine(
                quarantine_id=quarantine_id,
                agent_id=target_agent,
                attempt_id=target_attempt,
                screener_hotkey=f"screener-{target_agent}",
                policy_version=13,
                manifest_digest="e" * 64,
                reason_code="source-review-inconclusive",
                status="active",
                created_at=now,
            )
        )
        await session.flush()
        session.add(
            ScreeningVerificationReplay(
                replay_id=replay_id,
                request_id=uuid4(),
                agent_id=target_agent,
                quarantine_id=quarantine_id,
                source_attempt_id=target_attempt,
                artifact_sha256="a" * 64,
                policy_version=13,
                image_upload_id=None,
                image_sha256="f" * 64,
                image_size_bytes=1024,
                image_id="sha256:" + "1" * 64,
                image_staging_id=uuid4(),
                image_verified_at=now - timedelta(minutes=2),
                image_verified_storage_key=f"verification-replays/{replay_id}/verified/image.tar",
                status="running",
                worker_hotkey="independent-worker",
                lease_deadline=now + timedelta(minutes=20),
                lease_started_at=now - timedelta(minutes=3),
                actor="test:operator",
                reason="Independent V13 source review",
            )
        )
    approval = await client.post(
        f"{_BASE}/known-benign-approvals",
        json={
            "agent_id": str(clean_agent),
            "attempt_id": str(clean_attempt),
            "artifact_sha256": "c" * 64,
            "image_sha256": "d" * 64,
            "profile_sha256": V13_PRIVATE_PROFILE_SHA256,
            "review_evidence_sha256": "e" * 64,
            "reason": "independent benign source and behavior review",
        },
        headers=_HEADERS,
    )
    assert approval.status_code == 200, approval.text
    listed = await client.get(
        f"{_BASE}/known-benign-approvals?limit=1", headers=_HEADERS
    )
    assert listed.status_code == 200
    assert [row["approval_id"] for row in listed.json()] == [
        approval.json()["approval_id"]
    ]
    later_page = await client.get(
        f"{_BASE}/known-benign-approvals?limit=1&offset=1", headers=_HEADERS
    )
    assert later_page.status_code == 200
    assert later_page.json() == []
    fetched = await client.get(
        f"{_BASE}/known-benign-approvals/{approval.json()['approval_id']}",
        headers=_HEADERS,
    )
    assert fetched.status_code == 200
    assert fetched.json() == approval.json()
    payload = {
        "target_agent_id": str(target_agent),
        "target_attempt_id": str(target_attempt),
        "target_artifact_sha256": "a" * 64,
        "target_image_sha256": "f" * 64,
        "approval_id": approval.json()["approval_id"],
        "profile_sha256": V13_PRIVATE_PROFILE_SHA256,
    }
    wrong = await client.post(
        f"{_BASE}/replays/{replay_id}/group",
        json={**payload, "target_image_sha256": "b" * 64},
        headers=_HEADERS,
    )
    assert wrong.status_code == 409
    created = await client.post(
        f"{_BASE}/replays/{replay_id}/group", json=payload, headers=_HEADERS
    )
    assert created.status_code == 200, created.text
    group = created.json()
    assert group["status"] == "recorded_unverified"
    assert group["target_image_sha256"] == "f" * 64
    assert group["target_receipt_sha256"] != group["control_receipt_sha256"]
    async with session_maker() as session:
        row = await session.get(
            V13ReplayPrivateGenerationGroup, UUID(group["group_id"])
        )
        assert row is not None
        assert row.replay_id == replay_id
        protocol_group = TrustedGenerationGroup.model_validate(
            row, from_attributes=True
        )
        assert group["target_receipt_sha256"] == compute_v13_generation_role_digest(
            protocol_group, "target"
        )
    package = await client.post(
        f"{_BASE}/replays/{replay_id}/packages/target",
        json={
            "generation_receipt_sha256": group["target_receipt_sha256"],
            "manifest_sha256": "2" * 64,
            "pair_inventory_sha256": "3" * 64,
        },
        headers=_HEADERS,
    )
    assert package.status_code == 200, package.text
    assert package.json()["status"] == "recorded_unverified"
    mismatched_control = await client.post(
        f"{_BASE}/replays/{replay_id}/packages/known_benign",
        json={
            "generation_receipt_sha256": group["control_receipt_sha256"],
            "manifest_sha256": "4" * 64,
            "pair_inventory_sha256": "5" * 64,
        },
        headers=_HEADERS,
    )
    assert mismatched_control.status_code == 409
    control_package = await client.post(
        f"{_BASE}/replays/{replay_id}/packages/known_benign",
        json={
            "generation_receipt_sha256": group["control_receipt_sha256"],
            "manifest_sha256": "4" * 64,
            "pair_inventory_sha256": "3" * 64,
        },
        headers=_HEADERS,
    )
    assert control_package.status_code == 200, control_package.text
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerNode(
                environment="prod",
                node_id="private-replay-node",
                provider="test",
                provider_resource_id="private-replay-resource",
                screener_hotkey="independent-worker",
                token_hash="9" * 64,
                token_expires_at=now + timedelta(hours=1),
                status="active",
                verification_replay_capacity=1,
            )
        )
        clean_image = await session.scalar(
            select(ScreenedImageUpload).where(
                ScreenedImageUpload.agent_id == clean_agent,
                ScreenedImageUpload.attempt_id == clean_attempt,
            )
        )
        clean_agent_row = await session.get(Agent, clean_agent)
        assert clean_image is not None and clean_agent_row is not None
        clean_image.image_id = "sha256:" + "d" * 64
        clean_agent_row.screened_image_upload_id = clean_image.image_upload_id
        clean_agent_row.screened_image_sha256 = clean_image.sha256
        clean_agent_row.screened_image_size_bytes = clean_image.size_bytes
        clean_agent_row.screened_image_id = clean_image.image_id
        clean_agent_row.screened_image_verified_at = clean_image.verified_at
    storage = Mock()
    storage.presigned_get_url = AsyncMock(side_effect=["target-url", "control-url"])
    app.state.storage = storage
    private_request = Request({"type": "http", "app": app})
    private_request.state.screener_node_id = "private-replay-node"
    private_request.state.screener_node_status = "active"
    async with session_maker() as session:
        inputs = await get_replay_private_inputs(
            replay_id, private_request, "independent-worker", session
        )
        assert inputs.target_image.url == "target-url"
        assert inputs.control_image.url == "control-url"
        assert inputs.target_image.image_sha256 == "f" * 64
        assert inputs.control_image.image_sha256 == "d" * 64
        assert inputs.policy_verification_complete is False
    aggregate = tuple(
        PrivatePairCounts(
            transformation_class=class_name,
            seed_commitment=seed,
            pairs=10,
            control_correct=10,
            variant_correct=9,
            control_only_correct=1,
            variant_only_correct=0,
        )
        for seed in ("1" * 64, "2" * 64)
        for class_name in (
            "field_entity_rename",
            "request_paraphrase",
            "record_reorder_decoy",
        )
    )

    def _execution(clean: bool) -> PrivateExecutionResult:
        return PrivateExecutionResult(
            summary=V13PrivateRunSummary(
                agent_id=clean_agent if clean else target_agent,
                attempt_id=clean_attempt if clean else target_attempt,
                artifact_sha256=("c" if clean else "a") * 64,
                image_sha256=("d" if clean else "f") * 64,
                profile_sha256=V13_PRIVATE_PROFILE_SHA256,
                manifest_sha256=("4" if clean else "2") * 64,
                runner_hotkey="independent-worker",
                status="completed",
                completed_pairs=60,
                evidence_sha256="8" * 64,
            ),
            aggregates=aggregate,
        )

    receipt = V13ReplayPrivateReceipt(
        binding=V13ReplayBinding(
            replay_id=replay_id,
            agent_id=target_agent,
            attempt_id=target_attempt,
            artifact_sha256="a" * 64,
            image_sha256="f" * 64,
            image_id="sha256:" + "1" * 64,
        ),
        matched=V13MatchedCleanControlCommitment(
            group_id=UUID(group["group_id"]),
            replay_id=replay_id,
            target_agent_id=target_agent,
            target_attempt_id=target_attempt,
            target_artifact_sha256="a" * 64,
            target_image_sha256="f" * 64,
            target_manifest_sha256="2" * 64,
            control_agent_id=clean_agent,
            control_attempt_id=clean_attempt,
            control_artifact_sha256="c" * 64,
            control_image_sha256="d" * 64,
            control_manifest_sha256="4" * 64,
            control_approval_id=UUID(approval.json()["approval_id"]),
            control_approval_receipt_sha256=approval.json()["approval_receipt_sha256"],
            target_generation_receipt_sha256=group["target_receipt_sha256"],
            control_generation_receipt_sha256=group["control_receipt_sha256"],
            profile_sha256=V13_PRIVATE_PROFILE_SHA256,
            pair_inventory_sha256="3" * 64,
            pair_count=60,
        ),
        target=_execution(False),
        known_benign=_execution(True),
        runner_hotkey="independent-worker",
        observed_at=datetime.now(UTC),
        signature="f" * 128,
    )
    from ditto.api_server.endpoints import verification_replay

    monkeypatch.setattr(verification_replay, "_verify_signature", lambda *_: True)
    request = private_request
    async with session_maker() as session:
        accepted = await append_replay_private_receipt(
            replay_id, receipt, request, "independent-worker", session
        )
        assert accepted.status == "recorded_unverified"
        assert accepted.policy_verification_complete is False
        again = await append_replay_private_receipt(
            replay_id, receipt, request, "independent-worker", session
        )
        assert again.receipt_sha256 == accepted.receipt_sha256
        statistics = await get_replay_private_statistics(replay_id, None, session)
        assert statistics.report.status == "inconclusive"
        assert statistics.source_binding_current is True
        assert statistics.terminal_eligible is False
        changed = receipt.model_copy(
            update={
                "matched": receipt.matched.model_copy(
                    update={"pair_inventory_sha256": "5" * 64}
                )
            }
        )
        with pytest.raises(HTTPException) as mismatch:
            await append_replay_private_receipt(
                replay_id, changed, request, "independent-worker", session
            )
        assert mismatch.value.status_code == 409
    with pytest.raises(DBAPIError):
        async with session_maker() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE v13_replay_private_generation_groups SET actor = 'forged' "
                    "WHERE group_id = :group_id"
                ),
                {"group_id": UUID(group["group_id"])},
            )


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

    # Old attempt-keyed rows are not promoted into target/control group proof.
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningPrivatePackageRegistration(
                agent_id=target_agent,
                attempt_id=target_attempt,
                artifact_sha256="a" * 64,
                image_sha256="b" * 64,
                profile_sha256=V13_PRIVATE_PROFILE_SHA256,
                manifest_sha256="9" * 64,
                pair_inventory_sha256="3" * 64,
                clean_agent_id=clean_agent,
                clean_attempt_id=clean_attempt,
                clean_artifact_sha256="c" * 64,
                clean_image_sha256="d" * 64,
                runner_hotkey="legacy-unverified",
                registrar_actor="test:legacy",
            )
        )

    target_package = {
        "generation_receipt_sha256": body["target_receipt_sha256"],
        "manifest_sha256": "1" * 64,
        "pair_inventory_sha256": "3" * 64,
    }
    target_path = f"{_BASE}/groups/{body['group_id']}/packages/target"
    control_path = f"{_BASE}/groups/{body['group_id']}/packages/known_benign"
    assert (await client.get(target_path, headers=_HEADERS)).status_code == 404
    assert (await client.post(target_path, json=target_package)).status_code == 401
    mismatch = await client.post(
        target_path,
        json={**target_package, "generation_receipt_sha256": "f" * 64},
        headers=_HEADERS,
    )
    assert mismatch.status_code == 409
    target_registered = await client.post(
        target_path, json=target_package, headers=_HEADERS
    )
    assert target_registered.status_code == 200, target_registered.text
    assert target_registered.json()["status"] == "recorded_unverified"
    assert target_registered.json()["attempt_id"] == str(target_attempt)
    assert (
        await client.post(target_path, json=target_package, headers=_HEADERS)
    ).json() == target_registered.json()
    control_package = {
        "generation_receipt_sha256": body["control_receipt_sha256"],
        "manifest_sha256": "2" * 64,
        "pair_inventory_sha256": "3" * 64,
    }
    mismatched_inventory = await client.post(
        control_path,
        json={**control_package, "pair_inventory_sha256": "4" * 64},
        headers=_HEADERS,
    )
    assert mismatched_inventory.status_code == 409
    control_registered = await client.post(
        control_path, json=control_package, headers=_HEADERS
    )
    assert control_registered.status_code == 200, control_registered.text
    assert control_registered.json()["attempt_id"] == str(clean_attempt)
    assert control_registered.json()["role"] == "known_benign"
    assert (
        await client.get(control_path, headers=_HEADERS)
    ).json() == control_registered.json()
    with pytest.raises(DBAPIError):
        async with session_maker() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE v13_group_package_registrations "
                    "SET manifest_sha256 = :sha WHERE group_id = :group_id"
                ),
                {"sha": "5" * 64, "group_id": UUID(body["group_id"])},
            )


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
    assert "v13_known_benign_attestations_immutable" in triggers


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


async def test_trusted_control_requires_two_reviewers_and_live_image(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    clean_agent, clean_attempt = uuid4(), uuid4()
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
    approved = await client.post(
        f"{_BASE}/known-benign-approvals",
        json=approval_payload,
        headers={**_HEADERS, "X-Admin-Actor": "forged:not-a-principal"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "recorded_unverified"
    approval_id = approved.json()["approval_id"]
    trusted_path = f"{_BASE}/known-benign-approvals/{approval_id}/trusted"
    legacy = await client.get(
        trusted_path,
        headers={**_HEADERS, "X-Admin-Actor": "forged:not-a-principal"},
    )
    assert legacy.status_code == 409
    assert "benign control provenance unavailable" in legacy.text
    shared = await client.post(
        f"{_BASE}/known-benign-approvals/{approval_id}/attest",
        json={"reason": "shared token is not a reviewer"},
        headers=_HEADERS,
    )
    assert shared.status_code == 422
    other_approval = str(uuid4())
    reused = await client.post(
        f"{_BASE}/known-benign-approvals/{approval_id}/attest",
        json={
            "assertion": _assertion(
                other_approval, "e" * 64, sub="google:one", email="one@example.com"
            ),
            "reason": "assertion bound to another approval",
        },
        headers=_HEADERS,
    )
    assert reused.status_code == 401
    first = _assertion(approval_id, "e" * 64, sub="google:one", email="one@example.com")
    forged = await client.post(
        f"{_BASE}/known-benign-approvals/{approval_id}/attest",
        json={"assertion": first[:-8] + "00000000", "reason": "forged signature"},
        headers=_HEADERS,
    )
    assert forged.status_code == 401
    attested = await client.post(
        f"{_BASE}/known-benign-approvals/{approval_id}/attest",
        json={"assertion": first, "reason": "reviewed benign source and behavior"},
        headers=_HEADERS,
    )
    assert attested.status_code == 200, attested.text
    assert attested.json()["status"] == "recorded_unverified"
    still_one = await client.get(trusted_path, headers=_HEADERS)
    assert still_one.status_code == 409
    second = _assertion(
        approval_id, "e" * 64, sub="google:two", email="two@example.com"
    )
    assert (
        await client.post(
            f"{_BASE}/known-benign-approvals/{approval_id}/attest",
            json={"assertion": second, "reason": "independent served-behavior review"},
            headers=_HEADERS,
        )
    ).status_code == 200
    trusted = await client.get(trusted_path, headers=_HEADERS)
    assert trusted.status_code == 200, trusted.text
    body = trusted.json()
    assert body["provenance_status"] == "two_person_authenticated"
    assert body["authenticated_reviewers"] == 2
    assert body["artifact_sha256"] == "c" * 64
    assert body["image_sha256"] == "d" * 64
    assert "actor" not in body
    assert "challenge" not in trusted.text
    same_person = await client.post(
        f"{_BASE}/known-benign-approvals/{approval_id}/authorize-generation",
        json={
            "assertion": _assertion(
                approval_id,
                "e" * 64,
                sub="google:one",
                email="one@example.com",
                action="authorize-generation",
            ),
            "reason": "reviewer cannot also generate",
        },
        headers=_HEADERS,
    )
    assert same_person.status_code == 409
    assert "generation principal also approved" in same_person.text
    authorized = await client.post(
        f"{_BASE}/known-benign-approvals/{approval_id}/authorize-generation",
        json={
            "assertion": _assertion(
                approval_id,
                "e" * 64,
                sub="google:three",
                email="three@example.com",
                action="authorize-generation",
            ),
            "reason": "separate generation principal",
        },
        headers=_HEADERS,
    )
    assert authorized.status_code == 200, authorized.text

    async with session_maker() as session, session.begin():
        image = await session.scalar(
            select(ScreenedImageUpload).where(
                ScreenedImageUpload.attempt_id == clean_attempt
            )
        )
        assert image is not None
        image.status = "initiated"
    stale_image = await client.get(trusted_path, headers=_HEADERS)
    assert stale_image.status_code == 409
    assert "stale control image" in stale_image.text

    async with session_maker() as session, session.begin():
        image = await session.scalar(
            select(ScreenedImageUpload).where(
                ScreenedImageUpload.attempt_id == clean_attempt
            )
        )
        assert image is not None
        image.status = "verified"
        attempt = await session.get(ScreeningAttempt, clean_attempt)
        assert attempt is not None
        attempt.policy_version = 12
    stale_attempt = await client.get(trusted_path, headers=_HEADERS)
    assert stale_attempt.status_code == 409
    assert "stale control attempt" in stale_attempt.text
