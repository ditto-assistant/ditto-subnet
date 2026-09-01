from __future__ import annotations

import base64
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

from ditto.api_models.coding_evidence_upload import (
    CodingSealedEvidenceKind,
    CodingSealedEvidenceUploadCapability,
    CodingSealedEvidenceUploadCapabilityRequest,
    coding_sealed_evidence_upload_signing_message,
)
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import validator_coding_evidence as endpoint_module
from ditto.db.models import CodingSealedEvidenceUpload, CodingShadowTicket
from ditto.db.queries.coding_evidence_uploads import (
    CodingSealedEvidenceUploadReservation,
)

_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR = _KEYPAIR.ss58_address
_TICKET = UUID("33333333-3333-4333-8333-333333333333")
_UPLOAD = UUID("55555555-5555-4555-8555-555555555555")
_INSTANCE = "coding-worker-instance-001"


def _payload(**updates: object) -> dict[str, object]:
    requested_at = cast(datetime, updates.pop("requested_at", datetime.now(UTC)))
    nonce = cast(UUID, updates.pop("nonce", uuid4()))
    values: dict[str, object] = {
        "validator_hotkey": _VALIDATOR,
        "instance_id": _INSTANCE,
        "ticket_id": _TICKET,
        "claim_generation": 7,
        "evidence_kind": CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
        "sha256": "ab" * 32,
        "size_bytes": 4096,
        "nonce": nonce,
        "requested_at": requested_at,
    }
    values["signature"] = _KEYPAIR.sign(
        coding_sealed_evidence_upload_signing_message(
            validator_hotkey=_VALIDATOR,
            instance_id=_INSTANCE,
            ticket_id=_TICKET,
            claim_generation=7,
            evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
            sha256="ab" * 32,
            size_bytes=4096,
            nonce=nonce,
            requested_at=requested_at,
        )
    ).hex()
    values.update(updates)
    return CodingSealedEvidenceUploadCapabilityRequest.model_validate(
        values
    ).model_dump(mode="json")


def _reservation() -> CodingSealedEvidenceUploadReservation:
    now = datetime.now(UTC)
    upload = SimpleNamespace(
        upload_id=_UPLOAD,
        ticket_id=_TICKET,
        claim_generation=7,
        evidence_kind="authoring-transcript",
        sha256="ab" * 32,
        size_bytes=4096,
        content_type="application/octet-stream",
        weight_eligible=False,
    )
    ticket = SimpleNamespace(
        ticket_id=_TICKET,
        deadline=now + timedelta(hours=1),
        claim_expires_at=now + timedelta(minutes=2),
    )
    return CodingSealedEvidenceUploadReservation(
        upload=cast(CodingSealedEvidenceUpload, upload),
        ticket=cast(CodingShadowTicket, ticket),
        idempotent=False,
    )


def _capability() -> CodingSealedEvidenceUploadCapability:
    now = datetime.now(UTC).replace(microsecond=0)
    return CodingSealedEvidenceUploadCapability(
        schema="dittobench-coding-sealed-evidence-upload-capability-v1",
        coding_contract_version=1,
        weight_eligible=False,
        ticket_id=_TICKET,
        claim_generation=7,
        ticket_deadline=now + timedelta(hours=1),
        upload_id=_UPLOAD,
        evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
        sha256="ab" * 32,
        size_bytes=4096,
        content_type="application/octet-stream",
        checksum_sha256_b64=base64.b64encode(bytes.fromhex("ab" * 32)).decode(),
        url=(
            "https://evidence.invalid/coding-evidence/v1/authoring-transcript/"
            f"sha256/{'ab' * 32}?X-Amz-Date={now.strftime('%Y%m%dT%H%M%SZ')}"
            "&X-Amz-Expires=120&X-Amz-Signature=synthetic"
        ),
        expires_at=now + timedelta(minutes=2),
    )


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
        reserve=AsyncMock(return_value=_reservation()),
        minter=SimpleNamespace(mint=AsyncMock(return_value=_capability())),
    )
    app.state.coding_sealed_evidence_capability_minter = mocks.minter
    monkeypatch.setattr(
        endpoint_module, "_assert_validator_permitted", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(endpoint_module, "consume_validator_nonce", mocks.consume)
    monkeypatch.setattr(
        endpoint_module, "reserve_coding_sealed_evidence_upload", mocks.reserve
    )
    return mocks


async def test_capability_endpoint_is_signed_no_store_and_exact(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    response = await client.post(
        "/api/v1/validator/coding-shadow/evidence-upload-capability",
        json=_payload(),
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["upload_id"] == str(_UPLOAD)
    assert response.json()["weight_eligible"] is False
    assert mocks.consume.await_count == 1
    assert mocks.reserve.await_count == 1
    assert mocks.minter.mint.await_count == 1


async def test_capability_endpoint_rejects_forgery_and_disabled_store(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    mocks = _install(app, session_maker, monkeypatch)
    forged = _payload()
    forged["signature"] = "00" * 64
    response = await client.post(
        "/api/v1/validator/coding-shadow/evidence-upload-capability",
        json=forged,
    )
    assert response.status_code == 401
    app.state.coding_sealed_evidence_capability_minter = None
    response = await client.post(
        "/api/v1/validator/coding-shadow/evidence-upload-capability",
        json=_payload(),
    )
    assert response.status_code == 503
    assert mocks.consume.await_count == 0
    assert mocks.reserve.await_count == 0
