from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_inference import CodingInferencePolicy, policy_digest
from ditto.api_models.coding_inference_grants import (
    CodingInferenceExchangeRequest,
    CodingInferenceGrantRequest,
    CodingInferenceRevokeRequest,
    coding_inference_exchange_signing_message,
    coding_inference_grant_signing_message,
    coding_inference_revoke_signing_message,
)
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogIssue,
    CodingCatalogRuntimePolicy,
    CodingSelectionRunManifest,
    CodingTaskSetManifest,
)
from ditto.coding_selection import (
    CodingSelectionCatalogIntegrityError,
    CodingSelectionCatalogUnavailableError,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import validator_coding_inference as endpoint_module
from ditto.api_server.endpoints.validator_coding_inference import (
    CodingInferenceGrantTransport,
)
from ditto.db.queries.coding_inference_grants import (
    CodingInferenceGrantResult,
    CodingInferenceGrantRevocation,
)
from ditto.db.queries.coding_task_leases import CodingShadowTaskLeaseCore
from ditto.db.queries.validator_auth import ValidatorRequestReplayError

_ROOT = Path(__file__).parents[6]
_POLICY_PATH = (
    _ROOT
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)
_SELECTION_PATH = (
    _ROOT / "packages/dittobench-coding-contract/testdata/coding_selection_v1.json"
)
_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR = _KEYPAIR.ss58_address
_NOW = datetime.now(UTC).replace(microsecond=0)
_GRANT_ID = UUID("44444444-4444-4444-8444-444444444444")


def _policy() -> CodingInferencePolicy:
    return CodingInferencePolicy.model_validate(
        json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["policy"]
    )


def _lease(policy: CodingInferencePolicy) -> CodingShadowTaskLeaseCore:
    vector = json.loads(_SELECTION_PATH.read_text(encoding="utf-8"))
    manifest = CodingSelectionRunManifest.model_validate(
        {
            **vector["run_manifest"],
            "inference_grant_sha256": policy_digest(policy),
        }
    )
    return CodingShadowTaskLeaseCore(
        ticket_id=UUID("33333333-3333-4333-8333-333333333333"),
        validator_hotkey=_VALIDATOR,
        issued_at=_NOW - timedelta(minutes=1),
        deadline=_NOW + timedelta(hours=1),
        run_row_id=UUID("55555555-5555-4555-8555-555555555555"),
        run_manifest=manifest,
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


def _grant(lease: CodingShadowTaskLeaseCore, policy: CodingInferencePolicy):
    selected = lease.run_manifest.tasks[0]
    return SimpleNamespace(
        grant_id=_GRANT_ID,
        ticket_id=lease.ticket_id,
        run_row_id=lease.run_row_id,
        case_id=selected.case_id,
        profile_capability_id=selected.profile_capability_id,
        inference_grant_sha256=policy_digest(policy),
        model=policy.model,
        provider_api=policy.provider_api,
        provider_route=policy.provider_route,
        receipt_provider=policy.receipt_provider,
        provider_route_profile=policy.provider_route_profile,
        provider_account_guardrail=policy.provider_account_guardrail,
        provider_pipeline_policy=policy.provider_pipeline_policy,
        provider_cache_policy=policy.provider_cache_policy,
        reasoning_effort=policy.reasoning_effort,
        request_budget=100,
        prompt_token_budget=lease.budgets.model_input_tokens,
        completion_token_budget=lease.budgets.model_output_tokens,
        cost_budget_usd_micros=policy.max_cost_usd_micros,
        expires_at=lease.deadline,
        status="pending",
        generation=0,
        revoked_at=None,
    )


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> SimpleNamespace:
    policy = _policy()
    lease = _lease(policy)
    grant = _grant(lease, policy)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain():
        return object()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    app.state.coding_private_catalog_source = object()
    app.state.coding_inference_grant_transport = CodingInferenceGrantTransport(
        policy=policy,
        exchange_url=("https://test/api/v1/validator/coding-shadow/inference-exchange"),
        proxy_url="https://relay.invalid/api/v1/inference/coding/chat/completions",
    )
    mocks = SimpleNamespace(
        consume_nonce=AsyncMock(return_value=None),
        authorize=AsyncMock(return_value=None),
        build_lease=AsyncMock(return_value=lease),
        ensure=AsyncMock(
            return_value=CodingInferenceGrantResult(
                grant=grant,
                idempotent=False,
            )
        ),
        activate=AsyncMock(),
        revoke=AsyncMock(),
        lease=lease,
        grant=grant,
        policy=policy,
    )
    monkeypatch.setattr(
        endpoint_module,
        "_assert_validator_permitted",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(endpoint_module, "consume_validator_nonce", mocks.consume_nonce)
    monkeypatch.setattr(
        endpoint_module,
        "authorize_coding_shadow_task_delivery",
        mocks.authorize,
    )
    monkeypatch.setattr(
        endpoint_module, "build_coding_shadow_task_lease", mocks.build_lease
    )
    monkeypatch.setattr(endpoint_module, "ensure_coding_inference_grant", mocks.ensure)
    monkeypatch.setattr(
        endpoint_module, "activate_coding_inference_grant", mocks.activate
    )
    monkeypatch.setattr(endpoint_module, "revoke_coding_inference_grant", mocks.revoke)
    return mocks


def _grant_payload(lease: CodingShadowTaskLeaseCore, **updates) -> dict:
    nonce = updates.pop("nonce", uuid4())
    requested_at = updates.pop("requested_at", datetime.now(UTC))
    values = CodingInferenceGrantRequest(
        validator_hotkey=_VALIDATOR,
        ticket_id=lease.ticket_id,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(
            coding_inference_grant_signing_message(
                validator_hotkey=_VALIDATOR,
                ticket_id=lease.ticket_id,
                nonce=nonce,
                requested_at=requested_at,
            )
        ).hex(),
    ).model_dump(mode="json")
    values.update(updates)
    return values


async def test_signed_offer_exchange_and_revoke_are_no_store_and_shadow_only(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    offer_response = await client.post(
        "/api/v1/validator/coding-shadow/inference-grant",
        json=_grant_payload(mocks.lease),
    )
    assert offer_response.status_code == 200, offer_response.text
    assert offer_response.headers["Cache-Control"] == "no-store"
    offer = offer_response.json()
    assert offer["weight_eligible"] is False
    assert offer["status"] == "pending" and offer["generation"] == 0
    assert "bearer" not in offer_response.text

    mocks.grant.status = "active"
    mocks.grant.generation = 1
    mocks.activate.return_value = (mocks.grant, "b" * 43)
    exchange_nonce = uuid4()
    exchange_at = datetime.now(UTC)
    broker = "A" * 43
    exchange = CodingInferenceExchangeRequest(
        validator_hotkey=_VALIDATOR,
        grant_id=_GRANT_ID,
        broker_public_key=broker,
        nonce=exchange_nonce,
        requested_at=exchange_at,
        signature=_KEYPAIR.sign(
            coding_inference_exchange_signing_message(
                validator_hotkey=_VALIDATOR,
                grant_id=_GRANT_ID,
                broker_public_key=broker,
                nonce=exchange_nonce,
                requested_at=exchange_at,
            )
        ).hex(),
    )
    exchanged = await client.post(
        "/api/v1/validator/coding-shadow/inference-exchange",
        json=exchange.model_dump(mode="json"),
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.headers["Cache-Control"] == "no-store"
    assert exchanged.json()["bearer"] == "b" * 43
    assert (
        "api_key" not in exchanged.text and "provider_credential" not in exchanged.text
    )

    mocks.grant.revoked_at = datetime.now(UTC)
    mocks.revoke.return_value = CodingInferenceGrantRevocation(
        grant=mocks.grant,
        idempotent=False,
    )
    revoke_nonce = uuid4()
    revoke_at = datetime.now(UTC)
    revoke = CodingInferenceRevokeRequest(
        validator_hotkey=_VALIDATOR,
        grant_id=_GRANT_ID,
        generation=1,
        nonce=revoke_nonce,
        requested_at=revoke_at,
        signature=_KEYPAIR.sign(
            coding_inference_revoke_signing_message(
                validator_hotkey=_VALIDATOR,
                grant_id=_GRANT_ID,
                generation=1,
                nonce=revoke_nonce,
                requested_at=revoke_at,
            )
        ).hex(),
    )
    revoked = await client.post(
        "/api/v1/validator/coding-shadow/inference-revoke",
        json=revoke.model_dump(mode="json"),
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.headers["Cache-Control"] == "no-store"
    assert revoked.json()["status"] == "revoked"


async def test_coding_inference_endpoints_reject_disabled_forged_and_replayed(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    del app.state.coding_inference_grant_transport
    disabled = await client.post(
        "/api/v1/validator/coding-shadow/inference-grant",
        json=_grant_payload(mocks.lease),
    )
    assert disabled.status_code == 503

    app.state.coding_inference_grant_transport = CodingInferenceGrantTransport(
        policy=mocks.policy,
        exchange_url=("https://test/api/v1/validator/coding-shadow/inference-exchange"),
        proxy_url="https://relay.invalid/api/v1/inference/coding/chat/completions",
    )
    forged = _grant_payload(mocks.lease)
    forged["signature"] = "00" * 64
    assert (
        await client.post(
            "/api/v1/validator/coding-shadow/inference-grant",
            json=forged,
        )
    ).status_code == 401

    mocks.consume_nonce.side_effect = ValidatorRequestReplayError("replay")
    replay = await client.post(
        "/api/v1/validator/coding-shadow/inference-grant",
        json=_grant_payload(mocks.lease),
    )
    assert replay.status_code == 409
    mocks.ensure.assert_not_awaited()

    mocks.consume_nonce.side_effect = None
    future = await client.post(
        "/api/v1/validator/coding-shadow/inference-grant",
        json=_grant_payload(
            mocks.lease, requested_at=datetime.now(UTC) + timedelta(minutes=1)
        ),
    )
    assert future.status_code == 409
    mocks.build_lease.side_effect = CodingSelectionCatalogIntegrityError("digest")
    catalog_conflict = await client.post(
        "/api/v1/validator/coding-shadow/inference-grant",
        json=_grant_payload(mocks.lease),
    )
    assert catalog_conflict.status_code == 409
    mocks.build_lease.side_effect = CodingSelectionCatalogUnavailableError("timeout")
    catalog_unavailable = await client.post(
        "/api/v1/validator/coding-shadow/inference-grant",
        json=_grant_payload(mocks.lease),
    )
    assert catalog_unavailable.status_code == 503
    mocks.ensure.assert_not_awaited()
