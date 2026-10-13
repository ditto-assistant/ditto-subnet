"""Versioned consent, real signatures, immutable storage and upload rollout."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import bittensor
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ditto.api_models.contributor_identity import cla_message
from ditto.api_server.cla import evaluate_cla_gate, record_shadow_upload
from ditto.api_server.dependencies import (
    get_payment_verifier,
    get_storage_client,
)
from ditto.chain.models import BlockInfo
from ditto.db.models import Agent, AgentStatus, ClaSignature
from ditto.tests.api_server.endpoints.test_admin_artifact_release_settings import (
    _HEADERS,
    _install,
)
from ditto.tests.api_server.endpoints.test_upload import (
    _signed_request_body,
    _signed_upload_auth,
)

pytestmark = pytest.mark.asyncio
FIXTURE = "TEST FIXTURE ONLY. NOT A LEGAL AGREEMENT.\nplaceholder version {version}\n"


def setup(app, maker):
    _install(app, maker)
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    chain = SimpleNamespace(
        get_finalized_block=AsyncMock(
            return_value=BlockInfo(number=123, hash="0x" + "ab" * 32)
        ),
        get_coldkey_for_hotkey=AsyncMock(return_value=owner),
        get_registered_coldkey=AsyncMock(return_value=owner),
        is_registered=AsyncMock(return_value=True),
    )
    app.state.chain = chain
    app.state.payment_verifier = SimpleNamespace(verify_payment=AsyncMock())
    return chain


async def publish(client, version=1, *, effective=None):
    content = FIXTURE.format(version=version)
    digest = hashlib.sha256(content.encode()).hexdigest()
    response = await client.post(
        "/api/v1/admin/cla/versions",
        headers=_HEADERS,
        json={
            "version": version,
            "text": content,
            "sha256": digest,
            "effective_at": (effective or datetime.now(UTC)).isoformat(),
            "actor": "maintainer-fixture",
            "reason": "publish test-only placeholder fixture",
            "confirmation": f"PUBLISH CLA {version} {digest}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def policy(client, mode):
    current = (await client.get("/api/v1/admin/cla/settings", headers=_HEADERS)).json()[
        "current"
    ]
    response = await client.post(
        "/api/v1/admin/cla/settings",
        headers=_HEADERS,
        json={
            "expected_revision": current["revision"],
            "mode": mode,
            "actor": "operator-fixture",
            "reason": "test the staged consent rollout",
            "confirmation": f"SET CLA GATE {mode.upper()}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def signed(key, version, *, nonce=None, issued=None, signer=None):
    nonce, issued = nonce or uuid4(), issued or datetime.now(UTC)
    payload = cla_message(
        netuid=118,
        cla_version=version["version"],
        cla_sha256=version["sha256"],
        hotkey=key.ss58_address,
        nonce=nonce,
        issued_at=issued,
    )
    return {
        "netuid": 118,
        "hotkey": key.ss58_address,
        "cla_version": version["version"],
        "cla_sha256": version["sha256"],
        "nonce": str(nonce),
        "issued_at": issued.isoformat(),
        "signature": (signer or key).sign(payload).hex(),
    }


async def test_registry_off_default_pinned_bytes_signature_status_and_immutable_history(
    app, client, session_maker
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    before = await client.get(f"/api/v1/public/cla/status/{alice.ss58_address}")
    assert before.json()["mode"] == "off" and before.json()["current"] is None
    blocked = await client.post(
        "/api/v1/admin/cla/settings",
        headers=_HEADERS,
        json={
            "expected_revision": 0,
            "mode": "enforce",
            "reason": "cannot enforce missing text",
            "confirmation": "SET CLA GATE ENFORCE",
        },
    )
    assert blocked.status_code == 409
    version = await publish(client)
    document = await client.get("/api/v1/public/cla/versions/1/text")
    assert document.content == FIXTURE.format(version=1).encode()
    assert hashlib.sha256(document.content).hexdigest() == version["sha256"]
    body = signed(alice, version)
    result = await client.post(
        "/api/v1/cla/sign", json={**body, "future_unknown": "ignored"}
    )
    assert result.status_code == 200, result.text
    assert (
        result.json()["coldkey"]
        == bittensor.Keypair.create_from_uri("//Bob").ss58_address
    )
    assert result.json()["owner_block"] == 123
    assert (await client.post("/api/v1/cla/sign", json=body)).status_code == 409
    status = (
        await client.get(f"/api/v1/public/cla/status/{alice.ss58_address}")
    ).json()
    assert (
        status["is_current"] is True
        and status["signature"]["signature"] == body["signature"]
    )
    for table in ("cla_signatures", "cla_versions", "cla_settings_revisions"):
        for operation in (
            f"DELETE FROM {table}",
            f"UPDATE {table} SET "
            + ("signature=signature" if table == "cla_signatures" else "reason=reason"),
        ):
            async with session_maker() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(text(operation))
                await session.rollback()
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ClaSignature)) == 1


@pytest.mark.parametrize(
    "case,status",
    [
        ("stale", 400),
        ("future", 400),
        ("wrong_signer", 400),
        ("wrong_digest", 409),
        ("naive", 422),
        ("wrong_netuid", 400),
    ],
)
async def test_cla_rejects_invalid_proofs(app, client, session_maker, case, status):
    setup(app, session_maker)
    version = await publish(client)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    kwargs = {}
    if case == "stale":
        kwargs["issued"] = datetime.now(UTC) - timedelta(hours=25)
    if case == "future":
        kwargs["issued"] = datetime.now(UTC) + timedelta(minutes=6)
    if case == "wrong_signer":
        kwargs["signer"] = bittensor.Keypair.create_from_uri("//Charlie")
    if case == "wrong_digest":
        version = {**version, "sha256": "cd" * 32}
    body = signed(alice, version, **kwargs)
    if case == "naive":
        body["issued_at"] = datetime.now().isoformat()
    if case == "wrong_netuid":
        body["netuid"] = 119
    result = await client.post("/api/v1/cla/sign", json=body)
    assert result.status_code == status, result.text
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ClaSignature)) == 0


@pytest.mark.parametrize(
    "mode,allowed", [("off", True), ("shadow", True), ("enforce", False)]
)
async def test_upload_check_modes_require_current_consent_before_payment(
    app, client, session_maker, mode, allowed
):
    setup(app, session_maker)
    version = await publish(client)
    await policy(client, mode)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    response = await client.post(
        "/api/v1/upload/check", json=_signed_request_body(keypair=alice)
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is allowed
    assert (1106 in response.json()["error_codes"]) is (not allowed)
    if not allowed:
        assert response.json()["payment_required"] is False
    assert (
        await client.post("/api/v1/cla/sign", json=signed(alice, version))
    ).status_code == 200
    accepted = await client.post(
        "/api/v1/upload/check", json=_signed_request_body(keypair=alice)
    )
    assert accepted.json()["ok"] is True, accepted.text
    await publish(client, 2)
    outdated = await client.post(
        "/api/v1/upload/check", json=_signed_request_body(keypair=alice)
    )
    assert outdated.json()["ok"] is allowed
    status = (
        await client.get(f"/api/v1/public/cla/status/{alice.ss58_address}")
    ).json()
    assert status["is_current"] is False and status["signature"]["cla_version"] == 1


async def test_upload_agent_enforces_before_payment_verification_or_storage(
    app, client, session_maker
):
    setup(app, session_maker)
    await publish(client)
    await policy(client, "enforce")
    verifier, storage = (
        SimpleNamespace(verify_payment=AsyncMock()),
        SimpleNamespace(put_object=AsyncMock()),
    )
    app.dependency_overrides[get_payment_verifier] = lambda: verifier
    app.dependency_overrides[get_storage_client] = lambda: storage
    alice = bittensor.Keypair.create_from_uri("//Alice")
    raw = b"an unsigned-consent artifact must be refused before payment or storage"
    digest = hashlib.sha256(raw).hexdigest()
    auth = _signed_upload_auth(alice, hotkey=alice.ss58_address, sha256=digest)
    response = await client.post(
        "/api/v1/upload/agent",
        data={
            "hotkey": alice.ss58_address,
            "sha256": digest,
            "name": "fixture",
            **auth,
            "payment_block_hash": "0x" + "12" * 32,
            "payment_block_number": 123,
            "payment_extrinsic_index": 0,
        },
        files={"agent_tar": ("agent.tgz", raw, "application/gzip")},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == 1106
    assert "ditto cla sign" in response.json()["message"]
    verifier.verify_payment.assert_not_awaited()
    storage.put_object.assert_not_awaited()


async def test_future_version_publication_hash_guards_policy_cas_and_adoption(
    app, client, session_maker
):
    setup(app, session_maker)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    bob = bittensor.Keypair.create_from_uri("//Bob")
    first = await publish(client)
    assert (
        await client.post("/api/v1/cla/sign", json=signed(alice, first))
    ).status_code == 200
    await publish(client, 2, effective=datetime.now(UTC) + timedelta(days=1))
    status = (
        await client.get(f"/api/v1/public/cla/status/{alice.ss58_address}")
    ).json()
    assert status["current"]["version"] == 1 and status["is_current"]
    async with session_maker() as session, session.begin():
        for hotkey in (alice.ss58_address, bob.ss58_address):
            session.add(
                Agent(
                    agent_id=uuid4(),
                    miner_hotkey=hotkey,
                    name="fixture",
                    sha256="ab" * 32,
                    status=AgentStatus.SCORED,
                )
            )
    await policy(client, "shadow")
    async with session_maker() as session, session.begin():
        decision = await evaluate_cla_gate(session, netuid=118, hotkey=bob.ss58_address)
        await record_shadow_upload(
            session, netuid=118, hotkey=bob.ss58_address, decision=decision
        )
    control = (
        await client.get("/api/v1/admin/cla/settings?history_limit=1", headers=_HEADERS)
    ).json()
    assert (
        control["active_hotkeys"] == 2
        and control["signed_active_hotkeys"] == 1
        and control["unsigned_active_hotkeys"] == 1
    )
    assert control["unsigned_shadow_uploads"] == 1 and control["history_has_more"]
    denied = await client.post(
        "/api/v1/admin/cla/settings",
        headers=_HEADERS,
        json={
            "expected_revision": 0,
            "mode": "enforce",
            "reason": "stale update rejected",
            "confirmation": "SET CLA GATE ENFORCE",
        },
    )
    assert denied.status_code == 409
    bad = await client.post(
        "/api/v1/admin/cla/versions",
        headers=_HEADERS,
        json={
            "version": 3,
            "text": "TEST FIXTURE ONLY",
            "sha256": "ab" * 32,
            "effective_at": datetime.now(UTC).isoformat(),
            "reason": "hash mismatch rejected",
            "confirmation": "PUBLISH CLA 3 " + "ab" * 32,
        },
    )
    assert bad.status_code == 400


async def test_nonexistent_owner_is_rejected_without_uid_registration(
    app, client, session_maker
):
    chain = setup(app, session_maker)
    version = await publish(client)
    zero = bittensor.Keypair(public_key="0x" + "00" * 32, ss58_format=42).ss58_address
    chain.get_coldkey_for_hotkey.return_value = zero
    alice = bittensor.Keypair.create_from_uri("//Alice")
    result = await client.post("/api/v1/cla/sign", json=signed(alice, version))
    assert result.status_code == 400, result.text
    assert "Owner(hotkey)" in result.json()["message"]
    chain.is_registered.assert_not_awaited()
    assert (
        await client.get(f"/api/v1/public/cla/status/{alice.ss58_address}")
    ).headers["cache-control"] == "no-store"
