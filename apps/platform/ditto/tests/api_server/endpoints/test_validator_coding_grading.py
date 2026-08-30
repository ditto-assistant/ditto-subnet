"""Endpoint tests for freeze-gated shadow coding grading delivery."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_artifacts import (
    CodingGradingLeaseRequest,
    coding_grading_lease_signing_message,
)
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogIssue,
    CodingCatalogRuntimePolicy,
    CodingSelectionRunManifest,
    CodingTaskSetManifest,
)
from ditto.api_server.coding_artifact_capabilities import (
    CodingArtifactCapability,
    CodingArtifactCapabilitySet,
    CodingArtifactKind,
    coding_artifact_object_key,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import validator_coding_grading as endpoint_module
from ditto.db.queries.coding_task_leases import (
    CodingShadowGradingAuthority,
    CodingShadowTaskLeaseCore,
    CodingTaskLeaseIntegrityError,
)
from ditto.db.queries.validator_auth import ValidatorRequestReplayError

_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR = _KEYPAIR.ss58_address
_NOW = datetime.now(UTC).replace(microsecond=0)
_AGENT_ID = UUID("00000000-0000-4000-8000-000000000001")
_FREEZE_ID = UUID("44444444-4444-4444-8444-444444444444")
_EVIDENCE_SHA256 = "4e63fece4539d15ca915c30409b97ad41f1554732631b55d633642622ea6aac9"
_SELECTION_PATH = (
    Path(__file__).parents[6]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_selection_v1.json"
)


def _lease() -> CodingShadowTaskLeaseCore:
    vector = json.loads(_SELECTION_PATH.read_text(encoding="utf-8"))
    return CodingShadowTaskLeaseCore(
        ticket_id=UUID("33333333-3333-4333-8333-333333333333"),
        validator_hotkey=_VALIDATOR,
        issued_at=_NOW - timedelta(minutes=1),
        deadline=_NOW + timedelta(hours=1),
        run_row_id=UUID("22222222-2222-4222-8222-222222222222"),
        run_manifest=CodingSelectionRunManifest.model_validate(vector["run_manifest"]),
        task_set_manifest=CodingTaskSetManifest.model_validate(
            vector["task_set_manifest"]
        ),
        repository_epoch=vector["task_version"]["payload"]["repository_epoch"],
        issue=CodingCatalogIssue.model_validate(vector["issue"]),
        runtime_policy=CodingCatalogRuntimePolicy.model_validate(
            vector["runtime_policy"]
        ),
        budgets=CodingCatalogBudgets.model_validate(vector["budgets"]),
    )


def _authority() -> CodingShadowGradingAuthority:
    return CodingShadowGradingAuthority(
        agent_id=_AGENT_ID,
        freeze_id=_FREEZE_ID,
        authoring_evidence_sha256=_EVIDENCE_SHA256,
        frozen_patch_sha256="bb" * 32,
        frozen_submission_object_key="sha256/" + "bb" * 32,
    )


def _capability_set(lease: CodingShadowTaskLeaseCore) -> CodingArtifactCapabilitySet:
    task = lease.run_manifest.tasks[0]
    expires_at = _NOW + timedelta(minutes=5)
    capabilities = []
    for kind, digest in (
        (CodingArtifactKind.VISIBLE_BUNDLE, task.visible_bundle_sha256),
        (CodingArtifactKind.RESOURCE_PROFILE, task.resource_profile_sha256),
        (CodingArtifactKind.GRADER_BUNDLE, task.grader_bundle_sha256),
    ):
        key = coding_artifact_object_key(kind=kind, sha256=digest)
        capabilities.append(
            CodingArtifactCapability(
                kind=kind,
                sha256=digest,
                size_bytes=1024,
                expires_at=expires_at,
                url=(
                    f"https://storage.invalid/private-coding/{key}"
                    f"?X-Amz-Date={_NOW:%Y%m%dT%H%M%SZ}"
                    "&X-Amz-Expires=300&X-Amz-Signature=synthetic-grading"
                ),
            )
        )
    return CodingArtifactCapabilitySet(
        ticket_id=lease.ticket_id,
        run_row_id=lease.run_row_id,
        validator_hotkey=lease.validator_hotkey,
        ticket_deadline=lease.deadline,
        expires_at=expires_at,
        capabilities=tuple(capabilities),
    )


def _payload(
    lease: CodingShadowTaskLeaseCore,
    *,
    nonce=None,
    requested_at: datetime | None = None,
) -> dict:
    nonce = nonce or uuid4()
    requested_at = requested_at or datetime.now(UTC)
    message = coding_grading_lease_signing_message(
        validator_hotkey=_VALIDATOR,
        agent_id=_AGENT_ID,
        run_row_id=lease.run_row_id,
        ticket_id=lease.ticket_id,
        freeze_id=_FREEZE_ID,
        authoring_evidence_sha256=_EVIDENCE_SHA256,
        nonce=nonce,
        requested_at=requested_at,
    )
    return CodingGradingLeaseRequest(
        validator_hotkey=_VALIDATOR,
        agent_id=_AGENT_ID,
        run_row_id=lease.run_row_id,
        ticket_id=lease.ticket_id,
        freeze_id=_FREEZE_ID,
        authoring_evidence_sha256=_EVIDENCE_SHA256,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(message).hex(),
    ).model_dump(mode="json")


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch,
    lease: CodingShadowTaskLeaseCore,
) -> SimpleNamespace:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain():
        return object()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    app.state.coding_private_catalog_source = object()
    minter = SimpleNamespace(
        mint_grading=AsyncMock(return_value=_capability_set(lease))
    )
    app.state.coding_artifact_capability_minter = minter
    consume_nonce = AsyncMock(return_value=None)
    authorize = AsyncMock(return_value=_authority())
    build_lease = AsyncMock(return_value=lease)
    monkeypatch.setattr(
        endpoint_module,
        "_assert_validator_permitted",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(endpoint_module, "consume_validator_nonce", consume_nonce)
    monkeypatch.setattr(
        endpoint_module,
        "authorize_coding_shadow_grading_delivery",
        authorize,
    )
    monkeypatch.setattr(
        endpoint_module,
        "build_coding_shadow_task_lease",
        build_lease,
    )
    return SimpleNamespace(
        minter=minter,
        consume_nonce=consume_nonce,
        authorize=authorize,
        build_lease=build_lease,
    )


async def test_signed_grading_lease_returns_no_memory_and_no_store(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    lease = _lease()
    mocks = _install(app, session_maker, monkeypatch, lease)
    response = await client.post(
        "/api/v1/validator/coding-shadow/grading-lease",
        json=_payload(lease),
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["weight_eligible"] is False
    assert body["freeze_id"] == str(_FREEZE_ID)
    assert [item["artifact_kind"] for item in body["capabilities"]] == [
        "visible-bundle",
        "resource-profile",
        "grader-bundle",
    ]
    assert all(item["delivery_phase"] == "grading" for item in body["capabilities"])
    assert "memory-bundle" not in response.text
    assert mocks.authorize.await_count == 2
    mocks.minter.mint_grading.assert_awaited_once_with(lease)


async def test_grading_lease_retains_future_nonce_for_full_freshness_window(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    lease = _lease()
    mocks = _install(app, session_maker, monkeypatch, lease)
    requested_at = datetime.now(UTC) + timedelta(minutes=4, seconds=30)
    response = await client.post(
        "/api/v1/validator/coding-shadow/grading-lease",
        json=_payload(lease, requested_at=requested_at),
    )
    assert response.status_code == 200, response.text
    consumed = mocks.consume_nonce.await_args.kwargs
    assert consumed["expires_at"] == requested_at + timedelta(minutes=5)
    assert consumed["expires_at"] - consumed["now"] > timedelta(minutes=5)


async def test_grading_lease_rejects_forgery_replay_stale_and_unconfigured(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    lease = _lease()
    mocks = _install(app, session_maker, monkeypatch, lease)
    forged = _payload(lease)
    forged["signature"] = "00" * 64
    assert (
        await client.post("/api/v1/validator/coding-shadow/grading-lease", json=forged)
    ).status_code == 401

    mocks.consume_nonce.side_effect = ValidatorRequestReplayError("replay")
    assert (
        await client.post(
            "/api/v1/validator/coding-shadow/grading-lease", json=_payload(lease)
        )
    ).status_code == 409
    mocks.consume_nonce.side_effect = None

    stale = _payload(lease, requested_at=datetime.now(UTC) - timedelta(minutes=6))
    assert (
        await client.post("/api/v1/validator/coding-shadow/grading-lease", json=stale)
    ).status_code == 409

    app.state.coding_private_catalog_source = None
    response = await client.post(
        "/api/v1/validator/coding-shadow/grading-lease",
        json=_payload(lease),
    )
    assert response.status_code == 503
    assert "X-Amz-Signature" not in response.text


async def test_grading_lease_discards_urls_when_freeze_changes_after_mint(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    lease = _lease()
    mocks = _install(app, session_maker, monkeypatch, lease)
    changed = replace(_authority(), frozen_patch_sha256="cc" * 32)
    mocks.authorize.side_effect = [_authority(), changed]

    response = await client.post(
        "/api/v1/validator/coding-shadow/grading-lease",
        json=_payload(lease),
    )
    assert response.status_code == 409
    assert "X-Amz-Signature" not in response.text
    mocks.minter.mint_grading.assert_awaited_once()


async def test_grading_lease_requires_gradeable_freeze_before_catalog_access(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    lease = _lease()
    mocks = _install(app, session_maker, monkeypatch, lease)
    mocks.authorize.side_effect = CodingTaskLeaseIntegrityError("not gradeable")
    response = await client.post(
        "/api/v1/validator/coding-shadow/grading-lease",
        json=_payload(lease),
    )
    assert response.status_code == 409
    mocks.build_lease.assert_not_awaited()
    mocks.minter.mint_grading.assert_not_awaited()
