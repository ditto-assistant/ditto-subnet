from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_certification_leases import (
    CodingCertificationHarnessLaunchRequest,
    CodingCertificationLeaseAbortRequest,
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseClaimRequest,
    CodingCertificationLeaseIssueRequest,
    CodingCertificationLeaseStatus,
    coding_certification_harness_launch_signing_message,
    coding_certification_lease_abort_signing_message,
    coding_certification_lease_claim_signing_message,
    coding_certification_lease_issue_signing_message,
)
from ditto.api_models.coding_inference import CodingInferencePolicy
from ditto.api_models.coding_inference_grants import (
    CodingCertificationInferenceGrantRequest,
    CodingInferenceExchangeRequest,
    CodingInferenceRevokeRequest,
    coding_certification_inference_grant_signing_message,
    coding_inference_exchange_signing_message,
    coding_inference_revoke_signing_message,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import (
    validator_coding_certification_leases as endpoint_module,
)
from ditto.api_server.endpoints.validator_coding_inference import (
    CodingInferenceGrantTransport,
)
from ditto.db.models import (
    CodingCertificationInferenceGrant,
    CodingCertificationLease,
)
from ditto.db.queries.coding_certification_inference_grants import (
    CodingCertificationInferenceGrantActivation,
    CodingCertificationInferenceGrantResult,
    CodingCertificationInferenceGrantRevocation,
)
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseConflictError,
    CodingCertificationLeaseNotAvailableError,
    CodingCertificationLeaseResult,
    CodingCertificationLeaseUnavailableError,
)
from ditto.db.queries.validator_auth import ValidatorRequestReplayError
from ditto.tests.api_server.conftest import override_get_storage_client

_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR = _KEYPAIR.ss58_address
_AGENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_LEASE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_OBSERVATION = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_BENCH = 12


def _authority() -> CodingCertificationLeaseAuthority:
    issued = datetime.now(UTC)
    return CodingCertificationLeaseAuthority.model_validate(
        {
            "schema": "dittobench-coding-certification-lease-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "lease_id": _LEASE,
            "validator_hotkey": _VALIDATOR,
            "agent_id": _AGENT,
            "agent_artifact_sha256": "aa" * 32,
            "screened_image_sha256": "bb" * 32,
            "bench_version": _BENCH,
            "core_qualification_observation_id": _OBSERVATION,
            "core_qualification_policy_checksum": "cc" * 32,
            "canary_manifest_sha256": "dd" * 32,
            "runner_plan_sha256": "ee" * 32,
            "grader_plan_sha256": "ff" * 32,
            "resource_profile_sha256": "11" * 32,
            "inference_policy_sha256": "22" * 32,
            "issued_at": issued,
            "deadline": issued + timedelta(minutes=20),
        }
    )


def _result(
    *,
    status: CodingCertificationLeaseStatus = CodingCertificationLeaseStatus.ISSUED,
    idempotent: bool = False,
) -> CodingCertificationLeaseResult:
    authority = _authority()
    row = SimpleNamespace(
        status=status.value,
        claimed_at=authority.issued_at
        if status is CodingCertificationLeaseStatus.CLAIMED
        else None,
        aborted_at=authority.issued_at
        if status is CodingCertificationLeaseStatus.ABORTED
        else None,
        screened_image_id="sha256:" + "ef" * 32,
        screened_image_ref="ditto-screen/coding-cert-lease:latest",
        screened_image_upload_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        weight_eligible=False,
    )
    return CodingCertificationLeaseResult(
        row=cast(CodingCertificationLease, row),
        authority=authority,
        idempotent=idempotent,
    )


def _issue_payload(**updates) -> dict:
    nonce = updates.pop("nonce", uuid4())
    requested_at = updates.pop("requested_at", datetime.now(UTC))
    request = CodingCertificationLeaseIssueRequest(
        validator_hotkey=_VALIDATOR,
        agent_id=_AGENT,
        bench_version=_BENCH,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(
            coding_certification_lease_issue_signing_message(
                validator_hotkey=_VALIDATOR,
                agent_id=_AGENT,
                bench_version=_BENCH,
                coding_contract_version=1,
                nonce=nonce,
                requested_at=requested_at,
            )
        ).hex(),
    ).model_dump(mode="json")
    request.update(updates)
    return request


def _action_payload(kind: str, **updates) -> dict:
    nonce = updates.pop("nonce", uuid4())
    requested_at = updates.pop("requested_at", datetime.now(UTC))
    model = (
        CodingCertificationLeaseClaimRequest
        if kind == "claim"
        else CodingCertificationLeaseAbortRequest
    )
    signer = (
        coding_certification_lease_claim_signing_message
        if kind == "claim"
        else coding_certification_lease_abort_signing_message
    )
    request = model(
        validator_hotkey=_VALIDATOR,
        lease_id=_LEASE,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(
            signer(
                validator_hotkey=_VALIDATOR,
                lease_id=_LEASE,
                nonce=nonce,
                requested_at=requested_at,
            )
        ).hex(),
    ).model_dump(mode="json")
    request.update(updates)
    return request


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> SimpleNamespace:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain():
        return object()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    mocks = SimpleNamespace(
        consume=AsyncMock(return_value=None),
        issue=AsyncMock(return_value=_result()),
        claim=AsyncMock(
            return_value=_result(status=CodingCertificationLeaseStatus.CLAIMED)
        ),
        abort=AsyncMock(
            return_value=_result(status=CodingCertificationLeaseStatus.ABORTED)
        ),
    )
    monkeypatch.setattr(
        endpoint_module,
        "_assert_validator_permitted",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(endpoint_module, "consume_validator_nonce", mocks.consume)
    monkeypatch.setattr(
        endpoint_module, "issue_coding_certification_lease", mocks.issue
    )
    monkeypatch.setattr(
        endpoint_module, "claim_coding_certification_lease", mocks.claim
    )
    monkeypatch.setattr(
        endpoint_module, "abort_coding_certification_lease", mocks.abort
    )
    storage = override_get_storage_client(app)
    storage.presigned_get_url = AsyncMock(
        return_value="https://storage.invalid/screened.tar?signature=test"
    )
    authority = SimpleNamespace(
        agent_id=_AGENT,
        lease_id=_LEASE,
        deadline=datetime.now(UTC) + timedelta(minutes=20),
        bench_version=_BENCH,
        agent_artifact_sha256="aa" * 32,
        screened_image_sha256="bb" * 32,
        screened_image_size_bytes=1024,
        screened_image_id="sha256:" + "ef" * 32,
        screened_image_ref=f"ditto-screen/{_AGENT}:latest",
        screened_image_upload_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        screening_policy_version=9,
    )
    mocks.storage = storage
    mocks.authorize = AsyncMock(return_value=authority)
    mocks.audit = AsyncMock(return_value=True)
    monkeypatch.setattr(
        endpoint_module,
        "authorize_coding_certification_harness_delivery",
        mocks.authorize,
    )
    monkeypatch.setattr(endpoint_module, "record_artifact_fetch", mocks.audit)
    return mocks


async def test_issue_claim_and_abort_are_signed_and_shadow_only(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    issued = await client.post(
        "/api/v1/validator/coding-certification-leases",
        json=_issue_payload(),
    )
    assert issued.status_code == 200, issued.text
    assert issued.headers["Cache-Control"] == "no-store"
    assert issued.json()["weight_eligible"] is False
    assert issued.json()["status"] == "issued"
    assert issued.json()["authority"]["schema"] == (
        "dittobench-coding-certification-lease-v1"
    )
    assert issued.json()["screened_image_id"] == "sha256:" + "ef" * 32
    assert issued.json()["screened_image_ref"] == (
        "ditto-screen/coding-cert-lease:latest"
    )

    claimed = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/claim",
        json=_action_payload("claim"),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["status"] == "claimed"
    aborted = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/abort",
        json=_action_payload("abort"),
    )
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["status"] == "aborted"
    assert mocks.issue.await_count == 1
    assert mocks.claim.await_count == 1
    assert mocks.abort.await_count == 1
    assert mocks.consume.await_count == 3


async def test_ineligible_conflict_and_unavailable_map_to_http(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    mocks.issue.side_effect = CodingCertificationLeaseNotAvailableError("no")
    missing = await client.post(
        "/api/v1/validator/coding-certification-leases",
        json=_issue_payload(),
    )
    assert missing.status_code == 404
    mocks.issue.side_effect = CodingCertificationLeaseConflictError("exists")
    conflict = await client.post(
        "/api/v1/validator/coding-certification-leases",
        json=_issue_payload(),
    )
    assert conflict.status_code == 409
    mocks.issue.side_effect = CodingCertificationLeaseUnavailableError("canary")
    unavailable = await client.post(
        "/api/v1/validator/coding-certification-leases",
        json=_issue_payload(),
    )
    assert unavailable.status_code == 503
    mocks.claim.side_effect = CodingCertificationLeaseNotAvailableError("gone")
    claim_missing = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/claim",
        json=_action_payload("claim"),
    )
    assert claim_missing.status_code == 404
    mocks.abort.side_effect = CodingCertificationLeaseConflictError("claimed")
    abort_claimed = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/abort",
        json=_action_payload("abort"),
    )
    assert abort_claimed.status_code == 409


async def test_idempotent_claim_retry_accepts_replayed_nonce(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    mocks.claim.return_value = _result(
        status=CodingCertificationLeaseStatus.CLAIMED, idempotent=True
    )
    payload = _action_payload("claim")
    first = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/claim",
        json=payload,
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "claimed"
    mocks.consume.side_effect = ValidatorRequestReplayError("replayed")
    retry = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/claim",
        json=payload,
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "claimed"
    assert mocks.consume.await_count == 2


async def test_expired_claim_returns_404_after_commit(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    mocks.claim.return_value = _result(status=CodingCertificationLeaseStatus.EXPIRED)
    expired = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/claim",
        json=_action_payload("claim"),
    )
    assert expired.status_code == 404
    assert mocks.consume.await_count == 1
    assert mocks.claim.await_count == 1


def _harness_payload(**updates) -> dict:
    nonce = updates.pop("nonce", uuid4())
    requested_at = updates.pop("requested_at", datetime.now(UTC))
    request = CodingCertificationHarnessLaunchRequest(
        validator_hotkey=_VALIDATOR,
        lease_id=_LEASE,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(
            coding_certification_harness_launch_signing_message(
                validator_hotkey=_VALIDATOR,
                lease_id=_LEASE,
                nonce=nonce,
                requested_at=requested_at,
            )
        ).hex(),
    ).model_dump(mode="json")
    request.update(updates)
    return request


async def test_claimed_lease_harness_launch_is_signed_and_no_store(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    response = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/harness-launch",
        json=_harness_payload(),
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["schema"] == "dittobench-coding-certification-harness-launch-v1"
    assert body["weight_eligible"] is False
    assert body["lease_id"] == str(_LEASE)
    assert body["screened_image_ref"] == f"ditto-screen/{_AGENT}:latest"
    assert body["image_url"].startswith("https://storage.invalid/")
    assert mocks.authorize.await_count == 2
    mocks.storage.presigned_get_url.assert_awaited_once()
    mocks.audit.assert_awaited_once()


async def test_claimed_lease_harness_launch_rejects_forgery(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, monkeypatch)
    forged = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/harness-launch",
        json=_harness_payload(signature="00" * 64),
    )
    assert forged.status_code == 401


_GRANT = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
_POLICY_PATH = (
    Path(__file__).parents[6]
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)


def _policy() -> CodingInferencePolicy:
    return CodingInferencePolicy.model_validate(
        json.loads(_POLICY_PATH.read_text(encoding="utf-8"))["policy"]
    )


def _cert_grant(policy: CodingInferencePolicy) -> SimpleNamespace:
    return SimpleNamespace(
        grant_id=_GRANT,
        lease_id=_LEASE,
        case_id="PRACTICE-LEDGER-001",
        profile_capability_id="public-certification-v1",
        inference_grant_sha256="33" * 32,
        model=policy.model,
        provider_api=policy.provider_api,
        provider_route=policy.provider_route,
        receipt_provider=policy.receipt_provider,
        provider_route_profile=policy.provider_route_profile,
        provider_account_guardrail=policy.provider_account_guardrail,
        provider_pipeline_policy=policy.provider_pipeline_policy,
        provider_cache_policy=policy.provider_cache_policy,
        reasoning_effort=policy.reasoning_effort,
        request_budget=32,
        prompt_token_budget=10_000,
        completion_token_budget=2_000,
        cost_budget_usd_micros=policy.max_cost_usd_micros,
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
        status="pending",
        generation=0,
        revoked_at=None,
    )


def _install_grant_transport(app: FastAPI, monkeypatch) -> SimpleNamespace:
    policy = _policy()
    grant = _cert_grant(policy)
    app.state.coding_inference_grant_transport = CodingInferenceGrantTransport(
        policy=policy,
        exchange_url=("https://test/api/v1/validator/coding-shadow/inference-exchange"),
        proxy_url="https://relay.invalid/api/v1/inference/coding/chat/completions",
        revoke_url="https://test/api/v1/validator/coding-shadow/inference-revoke-capability",
    )
    mocks = SimpleNamespace(
        ensure=AsyncMock(
            return_value=CodingCertificationInferenceGrantResult(
                grant=cast(CodingCertificationInferenceGrant, grant),
                idempotent=False,
            )
        ),
        activate=AsyncMock(),
        revoke=AsyncMock(),
        grant=grant,
        policy=policy,
    )
    monkeypatch.setattr(
        endpoint_module, "ensure_coding_certification_inference_grant", mocks.ensure
    )
    monkeypatch.setattr(
        endpoint_module, "activate_coding_certification_inference_grant", mocks.activate
    )
    monkeypatch.setattr(
        endpoint_module, "revoke_coding_certification_inference_grant", mocks.revoke
    )
    return mocks


def _grant_payload(**updates) -> dict:
    nonce = updates.pop("nonce", uuid4())
    requested_at = updates.pop("requested_at", datetime.now(UTC))
    request = CodingCertificationInferenceGrantRequest(
        validator_hotkey=_VALIDATOR,
        lease_id=_LEASE,
        nonce=nonce,
        requested_at=requested_at,
        signature=_KEYPAIR.sign(
            coding_certification_inference_grant_signing_message(
                validator_hotkey=_VALIDATOR,
                lease_id=_LEASE,
                nonce=nonce,
                requested_at=requested_at,
            )
        ).hex(),
    ).model_dump(mode="json")
    request.update(updates)
    return request


async def test_claimed_lease_inference_grant_is_signed_no_store_and_default_off(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, monkeypatch)
    disabled = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/inference-grant",
        json=_grant_payload(),
    )
    assert disabled.status_code == 503

    mocks = _install_grant_transport(app, monkeypatch)
    offer_response = await client.post(
        f"/api/v1/validator/coding-certification-leases/{_LEASE}/inference-grant",
        json=_grant_payload(),
    )
    assert offer_response.status_code == 200, offer_response.text
    assert offer_response.headers["Cache-Control"] == "no-store"
    offer = offer_response.json()
    assert offer["weight_eligible"] is False
    assert offer["status"] == "pending"
    assert offer["lease_id"] == str(_LEASE)
    assert "bearer" not in offer_response.text
    assert offer["exchange_url"].endswith(
        "/api/v1/validator/coding-certification-leases/inference-exchange"
    )

    mocks.grant.status = "active"
    mocks.grant.generation = 1
    mocks.activate.return_value = CodingCertificationInferenceGrantActivation(
        grant=cast(CodingCertificationInferenceGrant, mocks.grant),
        bearer="b" * 43,
        revoke_bearer="r" * 43,
    )
    exchange_nonce = uuid4()
    exchange_at = datetime.now(UTC)
    broker = "A" * 43
    exchanged = await client.post(
        "/api/v1/validator/coding-certification-leases/inference-exchange",
        json=CodingInferenceExchangeRequest(
            validator_hotkey=_VALIDATOR,
            grant_id=_GRANT,
            broker_public_key=broker,
            nonce=exchange_nonce,
            requested_at=exchange_at,
            signature=_KEYPAIR.sign(
                coding_inference_exchange_signing_message(
                    validator_hotkey=_VALIDATOR,
                    grant_id=_GRANT,
                    broker_public_key=broker,
                    nonce=exchange_nonce,
                    requested_at=exchange_at,
                )
            ).hex(),
        ).model_dump(mode="json"),
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["bearer"] == "b" * 43
    assert exchanged.json()["revoke_url"].endswith(
        "/api/v1/validator/coding-shadow/inference-revoke-capability"
    )

    mocks.grant.revoked_at = datetime.now(UTC)
    mocks.revoke.return_value = CodingCertificationInferenceGrantRevocation(
        grant=cast(CodingCertificationInferenceGrant, mocks.grant),
        idempotent=False,
    )
    revoke_nonce = uuid4()
    revoke_at = datetime.now(UTC)
    revoked = await client.post(
        "/api/v1/validator/coding-certification-leases/inference-revoke",
        json=CodingInferenceRevokeRequest(
            validator_hotkey=_VALIDATOR,
            grant_id=_GRANT,
            generation=1,
            nonce=revoke_nonce,
            requested_at=revoke_at,
            signature=_KEYPAIR.sign(
                coding_inference_revoke_signing_message(
                    validator_hotkey=_VALIDATOR,
                    grant_id=_GRANT,
                    generation=1,
                    nonce=revoke_nonce,
                    requested_at=revoke_at,
                )
            ).hex(),
        ).model_dump(mode="json"),
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["lease_id"] == str(_LEASE)
