from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseAbortRequest,
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseClaimRequest,
    CodingCertificationLeaseIssueRequest,
    CodingCertificationLeaseStatus,
    coding_certification_lease_abort_signing_message,
    coding_certification_lease_claim_signing_message,
    coding_certification_lease_issue_signing_message,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import (
    validator_coding_certification_leases as endpoint_module,
)
from ditto.db.models import CodingCertificationLease
from ditto.db.queries.coding_certification_leases import (
    CodingCertificationLeaseConflictError,
    CodingCertificationLeaseNotAvailableError,
    CodingCertificationLeaseResult,
    CodingCertificationLeaseUnavailableError,
)
from ditto.db.queries.validator_auth import ValidatorRequestReplayError

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
