"""Versioned consent, real signatures, immutable storage and upload rollout."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import bittensor
import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from ditto.api_models.contributor_identity import cla_message
from ditto.api_models.http_errors import HttpErrorEnvelope
from ditto.api_server.cla import evaluate_cla_gate, record_shadow_upload
from ditto.api_server.dependencies import (
    get_payment_verifier,
    get_storage_client,
)
from ditto.chain import ChainConnectionError, ExtrinsicNotFoundError
from ditto.chain.models import BlockInfo
from ditto.db.models import Agent, AgentStatus, ClaShadowUpload, ClaSignature
from ditto.db.queries.contributor_identity import current_cla_version
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


async def test_missing_policy_seed_cannot_silently_disable_consent(session_maker):
    async with session_maker() as session, session.begin():
        await session.execute(
            text(
                "ALTER TABLE cla_settings_revisions DISABLE TRIGGER "
                "cla_settings_revisions_no_truncate"
            )
        )
        await session.execute(text("TRUNCATE cla_settings_revisions"))
        await session.execute(
            text(
                "ALTER TABLE cla_settings_revisions ENABLE TRIGGER "
                "cla_settings_revisions_no_truncate"
            )
        )
        with pytest.raises(RuntimeError, match="migration seed is missing"):
            await evaluate_cla_gate(session, netuid=118, hotkey="fixture")


@pytest.mark.parametrize("suffix", ["", "/text"])
async def test_unpublished_document_returns_documented_json_error(
    app, client, session_maker, suffix
):
    setup(app, session_maker)
    response = await client.get(f"/api/v1/public/cla/versions/999{suffix}")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert HttpErrorEnvelope.model_validate(response.json()).error_code == 3002


@pytest.mark.parametrize(
    "table",
    [
        "miner_github_action_nonces",
        "miner_github_operator_revocations",
        "miner_github_links",
        "cla_versions",
        "cla_signatures",
        "cla_settings_revisions",
        "cla_shadow_uploads",
        "agent_cla_bindings",
    ],
)
async def test_contributor_history_rejects_truncate(session_maker, table):
    async with session_maker() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(text(f'TRUNCATE "{table}" CASCADE'))
        await session.rollback()


@pytest.mark.parametrize("failure", ["owner_missing", "finalized_missing", "rpc"])
async def test_owner_absence_and_chain_unavailability_are_distinct(
    app, client, session_maker, failure
):
    setup(app, session_maker)
    version = await publish(client)
    if failure == "owner_missing":
        app.state.chain.get_coldkey_for_hotkey.side_effect = ExtrinsicNotFoundError(
            "no Owner entry"
        )
        expected = 400
    elif failure == "finalized_missing":
        app.state.chain.get_finalized_block.side_effect = ExtrinsicNotFoundError(
            "finalized chain unavailable"
        )
        expected = 503
    else:
        app.state.chain.get_coldkey_for_hotkey.side_effect = ChainConnectionError(
            "temporary RPC unavailable"
        )
        expected = 503
    alice = bittensor.Keypair.create_from_uri("//Alice")
    response = await client.post("/api/v1/cla/sign", json=signed(alice, version))
    assert response.status_code == expected, response.text
    if expected == 400:
        assert "register ownership" in response.text


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


async def elapse_cla_notice(maker):
    """Backdate every publication past the enforcement notice period.

    Publication time is server-assigned and the history is append-only, so
    tests age it directly instead of waiting a day.
    """
    async with maker() as session, session.begin():
        await session.execute(
            text("ALTER TABLE cla_versions DISABLE TRIGGER cla_versions_immutable")
        )
        await session.execute(
            text(
                "UPDATE cla_versions "
                "SET published_at = published_at - interval '25 hours'"
            )
        )
        await session.execute(
            text("ALTER TABLE cla_versions ENABLE TRIGGER cla_versions_immutable")
        )


async def test_higher_version_can_supersede_same_future_activation_date(
    app, client, session_maker
):
    setup(app, session_maker)
    effective = datetime.now(UTC) + timedelta(days=1)
    await publish(client, 1, effective=effective)
    await publish(client, 2, effective=effective)
    async with session_maker() as session:
        assert (
            await current_cla_version(session, now=effective - timedelta(seconds=1))
            is None
        )
        current = await current_cla_version(session, now=effective)
        assert current is not None and current.version == 2
    content = FIXTURE.format(version=3)
    digest = hashlib.sha256(content.encode()).hexdigest()
    rejected = await client.post(
        "/api/v1/admin/cla/versions",
        headers=_HEADERS,
        json={
            "version": 3,
            "text": content,
            "sha256": digest,
            "effective_at": (effective - timedelta(seconds=1)).isoformat(),
            "reason": "test backwards activation rejection",
            "confirmation": f"PUBLISH CLA 3 {digest}",
        },
    )
    assert rejected.status_code == 409
    assert "effective dates must not move backwards" in rejected.text


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
    etag = document.headers["etag"]
    for validator in (etag, "W/" + etag, "*", '"unrelated", ' + etag):
        cached = await client.get(
            "/api/v1/public/cla/versions/1/text", headers={"if-none-match": validator}
        )
        assert cached.status_code == 304 and cached.content == b""
        assert cached.headers["etag"] == etag
        assert cached.headers["cache-control"] == document.headers["cache-control"]
        assert cached.headers["x-content-type-options"] == "nosniff"
    fresh = await client.get(
        "/api/v1/public/cla/versions/1/text", headers={"if-none-match": '"unrelated"'}
    )
    assert fresh.status_code == 200 and fresh.content == document.content
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
    replay = await client.post("/api/v1/cla/sign", json=body)
    assert replay.status_code == 409 and "nonce already used" in replay.text
    async with session_maker() as session:
        nonce_constraints = await session.scalars(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'cla_signatures'::regclass AND contype = 'u'"
            )
        )
        assert "uq_cla_signatures_nonce" in nonce_constraints
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


async def test_cla_other_integrity_failures_are_server_errors(
    app, client, session_maker
):
    setup(app, session_maker)
    version = await publish(client)
    async with session_maker() as session, session.begin():
        await session.execute(
            text(
                "ALTER TABLE cla_signatures ADD CONSTRAINT fixture_cla_owner_guard "
                "CHECK (owner_block <> 123)"
            )
        )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as failures:
            response = await failures.post(
                "/api/v1/cla/sign",
                json=signed(bittensor.Keypair.create_from_uri("//Alice"), version),
            )
        assert response.status_code == 500, response.text
        assert "nonce already used" not in response.text
        async with session_maker() as session:
            assert (
                await session.scalar(select(func.count()).select_from(ClaSignature))
                == 0
            )
    finally:
        async with session_maker() as session, session.begin():
            await session.execute(
                text(
                    "ALTER TABLE cla_signatures DROP CONSTRAINT fixture_cla_owner_guard"
                )
            )


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
    await elapse_cla_notice(session_maker)
    await policy(client, mode)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    response = await client.post(
        "/api/v1/upload/check", json=_signed_request_body(keypair=alice)
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is allowed
    assert (1106 in response.json()["error_codes"]) is (not allowed)
    async with session_maker() as session:
        decision = await evaluate_cla_gate(
            session, netuid=118, hotkey=alice.ss58_address
        )
        assert ("is required" in decision.message) is (not allowed)
        if allowed:
            assert "Uploads are accepted" in decision.message
    if not allowed:
        assert response.json()["payment_required"] is False
    assert (
        await client.post("/api/v1/cla/sign", json=signed(alice, version))
    ).status_code == 200
    accepted = await client.post(
        "/api/v1/upload/check", json=_signed_request_body(keypair=alice)
    )
    assert accepted.json()["ok"] is True, accepted.text
    if mode == "enforce":
        await policy(client, "off")
    await publish(client, 2)
    if mode == "enforce":
        await elapse_cla_notice(session_maker)
        await policy(client, "enforce")
    outdated = await client.post(
        "/api/v1/upload/check", json=_signed_request_body(keypair=alice)
    )
    assert outdated.json()["ok"] is allowed
    status = (
        await client.get(f"/api/v1/public/cla/status/{alice.ss58_address}")
    ).json()
    assert status["is_current"] is False and status["signature"]["cla_version"] == 1


@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_policy_change_before_admission_rechecks_consent(
    app, client, session_maker, monkeypatch, mode
):
    from ditto.api_server.endpoints import upload

    setup(app, session_maker)
    await publish(client)
    await elapse_cla_notice(session_maker)
    await policy(client, mode)
    original_lock = upload.lock_identity_writes
    original_gate = upload.evaluate_cla_gate
    observed = []

    async def changing_policy(session, *, namespace, shared=False):
        assert namespace == "cla-policy"
        assert shared is True
        await policy(client, "enforce")
        await original_lock(session, namespace=namespace, shared=shared)

    async def observe_gate(session, *, netuid, hotkey):
        decision = await original_gate(session, netuid=netuid, hotkey=hotkey)
        observed.append(decision.mode)
        return decision

    monkeypatch.setattr(upload, "lock_identity_writes", changing_policy)
    monkeypatch.setattr(upload, "evaluate_cla_gate", observe_gate)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    body = _signed_request_body(keypair=alice)
    body["reserve_submission_slot"] = True
    response = await client.post("/api/v1/upload/check", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["error_codes"] == [1106]
    assert result["ok"] is False and result["payment_required"] is False
    assert result["admission_token"] is None
    assert observed == [mode, "enforce"]
    async with session_maker() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM upload_admission_reservations")
            )
            == 0
        )


async def test_upload_agent_enforces_before_payment_verification_or_storage(
    app, client, session_maker
):
    setup(app, session_maker)
    await publish(client)
    await elapse_cla_notice(session_maker)
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


@pytest.mark.parametrize("has_consent", [False, True])
async def test_shadow_does_not_count_uploads_rejected_after_the_cla_gate(
    app, client, session_maker, has_consent
):
    setup(app, session_maker)
    version = await publish(client)
    await policy(client, "shadow")
    alice = bittensor.Keypair.create_from_uri("//Alice")
    if has_consent:
        signed_response = await client.post(
            "/api/v1/cla/sign", json=signed(alice, version)
        )
        assert signed_response.status_code == 200, signed_response.text
    verifier = SimpleNamespace(verify_payment=AsyncMock())
    storage = SimpleNamespace(put_object=AsyncMock())
    app.dependency_overrides[get_payment_verifier] = lambda: verifier
    app.dependency_overrides[get_storage_client] = lambda: storage
    raw = b"TEST FIXTURE: not a gzip archive"
    digest = hashlib.sha256(raw).hexdigest()
    checked = await client.post(
        "/api/v1/upload/check",
        json={
            "hotkey": alice.ss58_address,
            "sha256": digest,
            "file_size_bytes": len(raw),
            **_signed_upload_auth(alice, hotkey=alice.ss58_address, sha256=digest),
        },
    )
    assert checked.status_code == 200 and checked.json()["ok"], checked.text
    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ClaShadowUpload)) == 0
        )
    response = await client.post(
        "/api/v1/upload/agent",
        data={
            "hotkey": alice.ss58_address,
            "sha256": digest,
            "name": "fixture",
            **_signed_upload_auth(alice, hotkey=alice.ss58_address, sha256=digest),
            "payment_block_hash": "0x" + "12" * 32,
            "payment_block_number": 123,
            "payment_extrinsic_index": 0,
        },
        files={"agent_tar": ("agent.tgz", raw, "application/gzip")},
    )
    assert response.status_code == 400, response.text
    verifier.verify_payment.assert_not_awaited()
    storage.put_object.assert_not_awaited()
    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(ClaShadowUpload)) == 0
        )


async def test_cla_ownership_lookup_does_not_hold_a_database_connection(
    app, client, session_maker
):
    setup(app, session_maker)
    version = await publish(client)
    alice = bittensor.Keypair.create_from_uri("//Alice")

    async def owner_lookup(*_args):
        assert session_maker.kw["bind"].sync_engine.pool.checkedout() == 0
        return bittensor.Keypair.create_from_uri("//Bob").ss58_address

    owner_rpc = AsyncMock(side_effect=owner_lookup)
    app.state.chain.get_coldkey_for_hotkey = owner_rpc
    response = await client.post("/api/v1/cla/sign", json=signed(alice, version))
    assert response.status_code == 200, response.text
    owner_rpc.assert_awaited_once()


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
        session.add(
            Agent(
                agent_id=uuid4(),
                miner_hotkey=bittensor.Keypair.create_from_uri("//Eve").ss58_address,
                name="banned-only",
                sha256="ab" * 32,
                status=AgentStatus.BANNED,
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


@pytest.mark.parametrize("case", ["missing", "digest", "nonce"])
async def test_invalid_document_and_nonce_replay_do_not_spend_chain_rpcs(
    app, client, session_maker, case
):
    chain = setup(app, session_maker)
    version = await publish(client)
    alice = bittensor.Keypair.create_from_uri("//Alice")
    body = signed(alice, version)
    if case == "missing":
        body = signed(alice, {**version, "version": 2})
    elif case == "digest":
        body = signed(alice, {**version, "sha256": "cd" * 32})
    else:
        assert (await client.post("/api/v1/cla/sign", json=body)).status_code == 200
    chain.get_finalized_block.reset_mock()
    chain.get_coldkey_for_hotkey.reset_mock()
    rejected = await client.post("/api/v1/cla/sign", json=body)
    assert rejected.status_code == 409, rejected.text
    chain.get_finalized_block.assert_not_awaited()
    chain.get_coldkey_for_hotkey.assert_not_awaited()


async def test_concurrent_nonce_replay_still_returns_conflict_after_both_prechecks(
    app, client, session_maker
):
    chain = setup(app, session_maker)
    version = await publish(client)
    body = signed(bittensor.Keypair.create_from_uri("//Alice"), version)
    arrivals = 0
    ready = asyncio.Event()

    async def owner(*_args, **_kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        return bittensor.Keypair.create_from_uri("//Bob").ss58_address

    chain.get_coldkey_for_hotkey.side_effect = owner
    results = await asyncio.wait_for(
        asyncio.gather(
            client.post("/api/v1/cla/sign", json=body),
            client.post("/api/v1/cla/sign", json=body),
        ),
        15,
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    assert "nonce already used" in next(
        result.text for result in results if result.status_code == 409
    )
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(ClaSignature)) == 1


@pytest.mark.parametrize("hours,accepted", [(0, False), (23, False), (24, True)])
async def test_enforced_cla_publication_requires_one_day_notice(
    app, client, session_maker, monkeypatch, hours, accepted
):
    from ditto.api_server.endpoints import contributor_cla

    setup(app, session_maker)
    await publish(client)
    await elapse_cla_notice(session_maker)
    await policy(client, "enforce")
    now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(contributor_cla, "datetime", Clock)
    metadata = (await client.get("/api/v1/admin/cla/settings", headers=_HEADERS)).json()
    assert metadata["minimum_enforced_notice_seconds"] == 86400
    assert datetime.fromisoformat(
        metadata["earliest_enforced_effective_at"]
    ) == now + timedelta(hours=24)
    content = FIXTURE.format(version=2)
    digest = hashlib.sha256(content.encode()).hexdigest()
    result = await client.post(
        "/api/v1/admin/cla/versions",
        headers=_HEADERS,
        json={
            "version": 2,
            "text": content,
            "sha256": digest,
            "effective_at": (now + timedelta(hours=hours)).isoformat(),
            "reason": "test notice before enforced agreement changes",
            "confirmation": f"PUBLISH CLA 2 {digest}",
        },
    )
    assert result.status_code == (200 if accepted else 409), result.text
    if not accepted:
        assert "24 hours notice" in result.text
    current = (await client.get("/api/v1/public/cla")).json()
    assert current["current"]["version"] == 1


async def enforce_attempt(client):
    current = (await client.get("/api/v1/admin/cla/settings", headers=_HEADERS)).json()
    return await client.post(
        "/api/v1/admin/cla/settings",
        headers=_HEADERS,
        json={
            "expected_revision": current["current"]["revision"],
            "mode": "enforce",
            "actor": "operator-fixture",
            "reason": "test the enforcement notice window",
            "confirmation": "SET CLA GATE ENFORCE",
        },
    )


async def test_enabling_enforcement_requires_notice_for_the_current_version(
    app, client, session_maker
):
    setup(app, session_maker)
    empty = (await client.get("/api/v1/admin/cla/settings", headers=_HEADERS)).json()
    assert empty["earliest_enforcement_at"] is None
    version = await publish(client)
    await policy(client, "shadow")
    control = (await client.get("/api/v1/admin/cla/settings", headers=_HEADERS)).json()
    assert datetime.fromisoformat(
        control["earliest_enforcement_at"]
    ) == datetime.fromisoformat(version["published_at"]) + timedelta(hours=24)
    rejected = await enforce_attempt(client)
    assert rejected.status_code == 409, rejected.text
    assert "24 hours notice" in rejected.text
    await elapse_cla_notice(session_maker)
    ready = (await client.get("/api/v1/admin/cla/settings", headers=_HEADERS)).json()
    assert datetime.fromisoformat(ready["earliest_enforcement_at"]) <= datetime.now(UTC)
    accepted = await enforce_attempt(client)
    assert accepted.status_code == 200, accepted.text
    enforced = (await client.get("/api/v1/admin/cla/settings", headers=_HEADERS)).json()
    assert enforced["earliest_enforcement_at"] is None


@pytest.mark.parametrize("notice_hours,accepted", [(1, False), (25, True)])
async def test_enabling_enforcement_requires_notice_for_scheduled_versions(
    app, client, session_maker, notice_hours, accepted
):
    setup(app, session_maker)
    await publish(client)
    await elapse_cla_notice(session_maker)
    # Off and shadow accept short notice; enforcement must not inherit it.
    scheduled = await publish(
        client, 2, effective=datetime.now(UTC) + timedelta(hours=notice_hours)
    )
    result = await enforce_attempt(client)
    assert result.status_code == (200 if accepted else 409), result.text
    if not accepted:
        assert "24 hours notice" in result.text
        control = (
            await client.get("/api/v1/admin/cla/settings", headers=_HEADERS)
        ).json()
        assert datetime.fromisoformat(
            control["earliest_enforcement_at"]
        ) == datetime.fromisoformat(scheduled["published_at"]) + timedelta(hours=24)
