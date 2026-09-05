"""Admin tests for the append-only private Coding v2 release registry."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2PublicationReceipt,
    CodingPrivateV2RegistrationAuthority,
)
from ditto.api_server.coding_private_v2_publication import (
    private_v2_publication_signing_message,
)
from ditto.api_server.dependencies import get_session
from ditto.db.models import CodingPrivateV2Release, CodingPrivateV2ReleaseEvent

_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _publication_receipt(
    private_key: Ed25519PrivateKey,
    *,
    valid_signature: bool = True,
) -> CodingPrivateV2PublicationReceipt:
    raw_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    object_record = {
        "object_index": 0,
        "remote_object_key_sha256": "91" * 32,
        "ciphertext_sha256": "92" * 32,
        "ciphertext_size_bytes": 33,
        "status": "uploaded",
    }
    message = private_v2_publication_signing_message(
        manifest={
            "catalog_merkle_root": "33" * 32,
            "catalog_sha256": "22" * 32,
            "coding_contract_version": 2,
            "objects": [object_record],
            "payload_sha256": "44" * 32,
            "schema": "dittobench-coding-private-v2-transport-v1",
            "transport_sha256": "55" * 32,
            "weight_eligible": False,
            "wrapping_key_sha256": "66" * 32,
        },
        source_sha="a" * 40,
        probe_receipt_payload_sha256="77" * 32,
        private_input_authority_sha256="88" * 32,
        curator_signing_key_sha256=hashlib.sha256(raw_public_key).hexdigest(),
    )
    signature = private_key.sign(message if valid_signature else b"wrong message")
    projection = {
        "schema": "dittobench-coding-private-v2-publication-v1",
        "source_sha": "a" * 40,
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "provider": "hippius",
        "probe_receipt_payload_sha256": "77" * 32,
        "private_input_authority_sha256": "88" * 32,
        "transport_sha256": "55" * 32,
        "payload_sha256": "44" * 32,
        "catalog_sha256": "22" * 32,
        "catalog_merkle_root": "33" * 32,
        "wrapping_key_sha256": "66" * 32,
        "curator_signing_key_sha256": hashlib.sha256(raw_public_key).hexdigest(),
        "curator_signature_b64": base64.b64encode(signature).decode("ascii"),
        "object_count": 1,
        "objects": [object_record],
        "ready": True,
        "shadow_only": True,
        "weight_eligible": False,
    }
    return CodingPrivateV2PublicationReceipt.model_validate(
        {
            **projection,
            "receipt_payload_sha256": coding_canonical_sha256(
                projection,
                maximum_bytes=16 << 20,
                label="private v2 publication receipt",
            ),
        }
    )


def _registration(
    receipt: CodingPrivateV2PublicationReceipt,
    **overrides: object,
) -> CodingPrivateV2RegistrationAuthority:
    projection: dict[str, object] = {
        "schema": "dittobench-coding-private-v2-registration-v1",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "shadow_only": True,
        "corpus_release_id": "private-coding-v2-release-001",
        "private_release_sha256": "11" * 32,
        "catalog_sha256": receipt.catalog_sha256,
        "catalog_merkle_root": receipt.catalog_merkle_root,
        "payload_sha256": receipt.payload_sha256,
        "transport_sha256": receipt.transport_sha256,
        "wrapping_key_sha256": receipt.wrapping_key_sha256,
        "publication_receipt_sha256": receipt.receipt_payload_sha256,
        "previous_registration_sha256": None,
    }
    projection.update(overrides)
    return CodingPrivateV2RegistrationAuthority.model_validate(
        {
            **projection,
            "registration_sha256": coding_canonical_sha256(
                projection,
                maximum_bytes=64 << 10,
                label="private v2 registration authority",
            ),
        }
    )


def _public_key_pem(private_key: Ed25519PrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def _register_payload(
    private_key: Ed25519PrivateKey,
    *,
    receipt: CodingPrivateV2PublicationReceipt | None = None,
    registration: CodingPrivateV2RegistrationAuthority | None = None,
) -> dict[str, object]:
    receipt = receipt or _publication_receipt(private_key)
    registration = registration or _registration(receipt)
    return {
        "registration": registration.model_dump(mode="json", by_alias=True),
        "publication_receipt": receipt.model_dump(mode="json", by_alias=True),
        "curator_public_key_pem": _public_key_pem(private_key),
        "reason": "register verified private coding v2 release",
        "actor": "operator@example.com",
        "confirmation": (
            "REGISTER SHADOW CODING PRIVATE V2 RELEASE "
            f"{registration.corpus_release_id} {registration.registration_sha256} "
            f"{receipt.curator_signing_key_sha256}"
        ),
    }


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


@pytest.mark.asyncio
async def test_private_v2_registration_verifies_authorities_and_is_idempotent(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    url = "/api/v1/admin/coding-private-v2-releases"
    assert (await client.get(url)).status_code == 401
    empty = await client.get(url, headers=_HEADERS)
    assert empty.status_code == 200
    assert empty.headers["cache-control"] == "no-store"
    assert empty.json() == {
        "total": 0,
        "releases": [],
        "shadow_only": True,
        "selectable": False,
        "weight_eligible": False,
    }

    private_key = Ed25519PrivateKey.generate()
    payload = _register_payload(private_key)
    bad_confirmation = {**payload, "confirmation": "REGISTER PRIVATE RELEASE"}
    assert (
        await client.post(f"{url}/register", headers=_HEADERS, json=bad_confirmation)
    ).status_code == 422

    wrong_key = Ed25519PrivateKey.generate()
    wrong_identity = {
        **payload,
        "curator_public_key_pem": _public_key_pem(wrong_key),
    }
    assert (
        await client.post(f"{url}/register", headers=_HEADERS, json=wrong_identity)
    ).status_code == 403

    invalid_receipt = _publication_receipt(private_key, valid_signature=False)
    invalid_signature = _register_payload(private_key, receipt=invalid_receipt)
    assert (
        await client.post(f"{url}/register", headers=_HEADERS, json=invalid_signature)
    ).status_code == 401

    registered = await client.post(f"{url}/register", headers=_HEADERS, json=payload)
    assert registered.status_code == 200, registered.text
    assert registered.headers["cache-control"] == "no-store"
    record = registered.json()["releases"][0]
    assert record["status"] == "registered"
    assert record["selectable"] is False
    assert record["weight_eligible"] is False
    assert record["publication_object_count"] == 1
    assert "publication_receipt" not in record
    assert "curator_public_key_pem" not in record

    replay = await client.post(f"{url}/register", headers=_HEADERS, json=payload)
    assert replay.status_code == 200
    assert replay.json()["total"] == 1

    receipt = CodingPrivateV2PublicationReceipt.model_validate(
        payload["publication_receipt"]
    )
    changed = _registration(receipt, private_release_sha256="99" * 32)
    conflict = await client.post(
        f"{url}/register",
        headers=_HEADERS,
        json=_register_payload(
            private_key,
            receipt=receipt,
            registration=changed,
        ),
    )
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_private_v2_lifecycle_is_append_only_and_never_selectable(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    url = "/api/v1/admin/coding-private-v2-releases"
    private_key = Ed25519PrivateKey.generate()
    payload = _register_payload(private_key)
    registered = await client.post(f"{url}/register", headers=_HEADERS, json=payload)
    assert registered.status_code == 200, registered.text
    registration = CodingPrivateV2RegistrationAuthority.model_validate(
        payload["registration"]
    )
    transition = {
        "corpus_release_id": registration.corpus_release_id,
        "expected_registration_sha256": registration.registration_sha256,
        "reason": "quarantine release for private audit " + ("evidence " * 3_000),
        "actor": "operator@example.com",
        "confirmation": (
            "QUARANTINE SHADOW CODING PRIVATE V2 RELEASE "
            f"{registration.corpus_release_id} {registration.registration_sha256}"
        ),
    }
    stale = {
        **transition,
        "expected_registration_sha256": "ff" * 32,
        "confirmation": (
            "QUARANTINE SHADOW CODING PRIVATE V2 RELEASE "
            f"{registration.corpus_release_id} {'ff' * 32}"
        ),
    }
    assert (
        await client.post(f"{url}/quarantine", headers=_HEADERS, json=stale)
    ).status_code == 409

    quarantined = await client.post(
        f"{url}/quarantine", headers=_HEADERS, json=transition
    )
    assert quarantined.status_code == 200, quarantined.text
    record = quarantined.json()["releases"][0]
    assert record["status"] == "quarantined"
    assert record["lifecycle_event_count"] == 1
    assert record["selectable"] is False

    replay = await client.post(f"{url}/quarantine", headers=_HEADERS, json=transition)
    assert replay.status_code == 200
    changed_reason = {**transition, "reason": "another quarantine explanation"}
    assert (
        await client.post(f"{url}/quarantine", headers=_HEADERS, json=changed_reason)
    ).status_code == 409

    retirement = {
        **transition,
        "reason": "retire release after completed audit",
        "confirmation": (
            "RETIRE SHADOW CODING PRIVATE V2 RELEASE "
            f"{registration.corpus_release_id} {registration.registration_sha256}"
        ),
    }
    retired = await client.post(f"{url}/retire", headers=_HEADERS, json=retirement)
    assert retired.status_code == 200, retired.text
    record = retired.json()["releases"][0]
    assert record["status"] == "retired"
    assert record["lifecycle_event_count"] == 2
    assert record["selectable"] is False
    assert record["weight_eligible"] is False

    async with session_maker() as session:
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await session.execute(
                    update(CodingPrivateV2Release)
                    .where(
                        CodingPrivateV2Release.corpus_release_id
                        == registration.corpus_release_id
                    )
                    .values(actor="mutated")
                )
    async with session_maker() as session:
        with pytest.raises(SAIntegrityError):
            async with session.begin():
                await session.execute(delete(CodingPrivateV2ReleaseEvent))
