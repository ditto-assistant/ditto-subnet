"""Stored artifact binding and retry compatibility across CLA versions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import bittensor
import httpx
import pytest
from sqlalchemy import func, select, text

from ditto.api_server import create_api_server
from ditto.api_server.dependencies import get_payment_verifier, get_storage_client
from ditto.api_server.payment_verifier import VerifiedPayment
from ditto.api_server.storage import (
    create_storage_client,
    parse_storage_config_from_env,
)
from ditto.db.models import Agent, AgentClaBinding, ClaShadowUpload, EvaluationPayment
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.api_server.endpoints.test_contributor_cla import (
    policy,
    publish,
    setup,
    signed,
)
from ditto.tests.api_server.endpoints.test_upload import _signed_request_body
from ditto.tests.integration.test_upload_agent_integration import _TAR_SHA, _build_form

pytestmark = pytest.mark.integration


@pytest.fixture
def app():
    return create_api_server(make_api_server_config())


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_artifact_binds_exact_receipt_and_retries_do_not_require_new_consent(
    app, client, session_maker, object_storage
):
    del object_storage
    setup(app, session_maker)
    app.state.session_maker = session_maker
    app.state.embedder = SimpleNamespace(embed=AsyncMock(return_value=None))
    alice = bittensor.Keypair.create_from_uri("//Alice")
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    version = await publish(client)
    receipt = await client.post("/api/v1/cla/sign", json=signed(alice, version))
    assert receipt.status_code == 200, receipt.text
    await policy(client, "enforce")
    block_hash = "0x" + "31" * 32
    verifier = SimpleNamespace(
        verify_payment=AsyncMock(
            return_value=VerifiedPayment(
                block_hash=block_hash,
                extrinsic_index=0,
                miner_hotkey=alice.ss58_address,
                miner_coldkey=owner,
                amount_rao=40_000_000,
                tao_usd_rate=Decimal("400"),
                dest_address=app.state.config.upload_payment_address,
                block_timestamp=datetime.now(UTC),
            )
        )
    )
    app.dependency_overrides[get_payment_verifier] = lambda: verifier
    async with create_storage_client(parse_storage_config_from_env()) as storage:

        async def get_storage():
            return storage

        app.dependency_overrides[get_storage_client] = get_storage
        data, files = _build_form(keypair=alice, block_hash=block_hash)
        uploaded = await client.post("/api/v1/upload/agent", data=data, files=files)
        assert uploaded.status_code == 200, uploaded.text
        agent_id = UUID(uploaded.json()["agent_id"])
        async with session_maker() as session:
            binding = await session.get(AgentClaBinding, agent_id)
            agent = await session.get(Agent, agent_id)
            assert binding.signature_id == UUID(receipt.json()["signature_id"])
            assert agent.sha256 == _TAR_SHA and agent.miner_hotkey == alice.ss58_address
        # Stage an already effective replacement before explicitly re-enabling
        # enforcement; publishing during enforcement now requires notice.
        await policy(client, "off")
        await publish(client, 2)
        await policy(client, "enforce")
        stale = (
            await client.get(f"/api/v1/public/cla/status/{alice.ss58_address}")
        ).json()
        assert not stale["is_current"]
        fresh_data, _ = _build_form(keypair=alice, block_hash=block_hash)
        recovery = {
            "hotkey": alice.ss58_address,
            "sha256": _TAR_SHA,
            "name": fresh_data["name"],
            "signature": fresh_data["signature"],
            "signature_timestamp": fresh_data["signature_timestamp"],
            "signature_nonce": fresh_data["signature_nonce"],
            "file_size_bytes": 100,
            "payment_block_hash": block_hash,
            "payment_block_number": 123,
            "payment_extrinsic_index": 0,
        }
        exact_check = await client.post("/api/v1/upload/check", json=recovery)
        assert exact_check.status_code == 200 and exact_check.json()["ok"], (
            exact_check.text
        )
        legacy_reserved_retry = await client.post(
            "/api/v1/upload/check",
            json={**recovery, "name": None, "reserve_submission_slot": True},
        )
        assert legacy_reserved_retry.status_code == 200, legacy_reserved_retry.text
        assert legacy_reserved_retry.json()["ok"]
        assert not legacy_reserved_retry.json()["payment_required"]
        for name in (None, "different-name"):
            non_exact = await client.post(
                "/api/v1/upload/check", json={**recovery, "name": name}
            )
            assert non_exact.status_code == 200, non_exact.text
            if name is None:
                assert non_exact.json()["ok"]
                assert not non_exact.json()["payment_required"]
            else:
                assert not non_exact.json()["ok"]
                assert 1106 in non_exact.json()["error_codes"]
        credit_block = "0x" + "32" * 32
        async with session_maker() as session, session.begin():
            session.add(
                EvaluationPayment(
                    block_hash=credit_block,
                    extrinsic_index=0,
                    agent_id=None,
                    credit_for_agent_id=agent_id,
                    miner_hotkey=alice.ss58_address,
                    miner_coldkey=owner,
                    amount_rao=40_000_000,
                    dest_address=app.state.config.upload_payment_address,
                    timestamp=datetime.now(UTC),
                )
            )
        credit_check = await client.post(
            "/api/v1/upload/check",
            json={**recovery, "payment_block_hash": credit_block},
        )
        assert credit_check.status_code == 200, credit_check.text
        assert not credit_check.json()["ok"]
        assert 1106 in credit_check.json()["error_codes"]
        assert not credit_check.json()["payment_required"]
        renamed_data, renamed_files = _build_form(keypair=alice, block_hash=block_hash)
        renamed_data["name"] = "different-name"
        renamed = await client.post(
            "/api/v1/upload/agent", data=renamed_data, files=renamed_files
        )
        assert renamed.status_code == 402, renamed.text
        assert renamed.json()["error_code"] == 3207
        retry_data, retry_files = _build_form(keypair=alice, block_hash=block_hash)
        retried = await client.post(
            "/api/v1/upload/agent", data=retry_data, files=retry_files
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["agent_id"] == str(agent_id)
        # No new storage/payment or retroactive receipt is created.
        verifier.verify_payment.assert_awaited_once()
        async with session_maker() as session:
            assert list(await session.scalars(select(AgentClaBinding))) == [
                await session.get(AgentClaBinding, agent_id)
            ]
            assert (await session.get(AgentClaBinding, agent_id)).signature_id == UUID(
                receipt.json()["signature_id"]
            )


async def test_check_and_artifact_commit_share_policy_then_coldkey_lock_order(
    app, client, session_maker, object_storage, monkeypatch
):
    from ditto.api_server.endpoints import upload

    del object_storage
    setup(app, session_maker)
    app.state.session_maker = session_maker
    app.state.embedder = SimpleNamespace(embed=AsyncMock(return_value=None))
    alice = bittensor.Keypair.create_from_uri("//Alice")
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    version = await publish(client)
    assert (
        await client.post("/api/v1/cla/sign", json=signed(alice, version))
    ).status_code == 200
    await policy(client, "enforce")
    block_hash = "0x" + "41" * 32
    verifier = SimpleNamespace(
        verify_payment=AsyncMock(
            return_value=VerifiedPayment(
                block_hash=block_hash,
                extrinsic_index=0,
                miner_hotkey=alice.ss58_address,
                miner_coldkey=owner,
                amount_rao=40_000_000,
                tao_usd_rate=Decimal("400"),
                dest_address=app.state.config.upload_payment_address,
                block_timestamp=datetime.now(UTC),
            )
        )
    )
    app.dependency_overrides[get_payment_verifier] = lambda: verifier
    consumed = asyncio.Event()
    continue_commit = asyncio.Event()
    original = upload.consume_or_enforce_upload_admission

    async def pause_after_coldkey_lock(*args, **kwargs):
        await original(*args, **kwargs)
        consumed.set()
        await continue_commit.wait()

    monkeypatch.setattr(
        upload, "consume_or_enforce_upload_admission", pause_after_coldkey_lock
    )
    async with create_storage_client(parse_storage_config_from_env()) as storage:
        app.dependency_overrides[get_storage_client] = lambda: storage
        data, files = _build_form(keypair=alice, block_hash=block_hash)
        uploading = asyncio.create_task(
            client.post("/api/v1/upload/agent", data=data, files=files)
        )
        checking = None
        try:
            async with asyncio.timeout(10):
                await consumed.wait()
                body = _signed_request_body(keypair=alice, sha256="e" * 64)
                body["reserve_submission_slot"] = True
                checking = asyncio.create_task(
                    client.post("/api/v1/upload/check", json=body)
                )
                # Observe real PostgreSQL contention before releasing the upload;
                # otherwise request scheduling could hide the reversed lock order.
                async with session_maker() as session:
                    while True:
                        assert not checking.done(), (
                            "check did not contend with the artifact transaction"
                        )
                        waiting = await session.scalar(
                            text("""
                            SELECT EXISTS (
                                SELECT 1 FROM pg_locks
                                WHERE locktype = 'advisory' AND NOT granted
                                  AND database = (SELECT oid FROM pg_database
                                                  WHERE datname = current_database())
                            )
                        """)
                        )
                        if waiting:
                            break
                        await asyncio.sleep(0.01)
                continue_commit.set()
                uploaded, checked = await asyncio.gather(uploading, checking)
            assert uploaded.status_code == 200, uploaded.text
            assert checked.status_code == 200, checked.text
            assert checked.json()["error_codes"] == [1105]
            assert checked.json()["admission_token"] is None
        finally:
            continue_commit.set()
            tasks = [uploading] + ([checking] if checking is not None else [])
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_shadow_observes_only_accepted_unsigned_artifacts(
    app, client, session_maker, object_storage
):
    del object_storage
    setup(app, session_maker)
    app.state.session_maker = session_maker
    app.state.embedder = SimpleNamespace(embed=AsyncMock(return_value=None))
    alice = bittensor.Keypair.create_from_uri("//Alice")
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    await publish(client)
    await policy(client, "shadow")
    block_hash = "0x" + "51" * 32
    verifier = SimpleNamespace(
        verify_payment=AsyncMock(
            return_value=VerifiedPayment(
                block_hash=block_hash,
                extrinsic_index=0,
                miner_hotkey=alice.ss58_address,
                miner_coldkey=owner,
                amount_rao=40_000_000,
                tao_usd_rate=Decimal("400"),
                dest_address=app.state.config.upload_payment_address,
                block_timestamp=datetime.now(UTC),
            )
        )
    )
    app.dependency_overrides[get_payment_verifier] = lambda: verifier

    async def observations() -> int:
        async with session_maker() as session:
            return int(
                await session.scalar(select(func.count()).select_from(ClaShadowUpload))
            )

    async with create_storage_client(parse_storage_config_from_env()) as storage:
        app.dependency_overrides[get_storage_client] = lambda: storage
        data, files = _build_form(keypair=alice, block_hash=block_hash)
        uploaded = await client.post("/api/v1/upload/agent", data=data, files=files)
        assert uploaded.status_code == 200, uploaded.text
        agent_id = UUID(uploaded.json()["agent_id"])
        assert await observations() == 1
        retry_data, retry_files = _build_form(keypair=alice, block_hash=block_hash)
        retried = await client.post(
            "/api/v1/upload/agent", data=retry_data, files=retry_files
        )
        assert retried.status_code == 200, retried.text
        assert await observations() == 1
        # A credit owned by another hotkey passes the CLA gate but is rejected
        # by the later payment checks, so it must not count as an upload.
        credit_block = "0x" + "52" * 32
        async with session_maker() as session, session.begin():
            session.add(
                EvaluationPayment(
                    block_hash=credit_block,
                    extrinsic_index=0,
                    agent_id=None,
                    credit_for_agent_id=agent_id,
                    miner_hotkey=bittensor.Keypair.create_from_uri(
                        "//Charlie"
                    ).ss58_address,
                    miner_coldkey=owner,
                    amount_rao=40_000_000,
                    dest_address=app.state.config.upload_payment_address,
                    timestamp=datetime.now(UTC),
                )
            )
        stolen_data, stolen_files = _build_form(
            keypair=alice, block_hash=credit_block, name="credit-agent"
        )
        stolen = await client.post(
            "/api/v1/upload/agent", data=stolen_data, files=stolen_files
        )
        assert stolen.status_code == 402, stolen.text
        assert stolen.json()["error_code"] == 3207
        assert await observations() == 1
        verifier.verify_payment.assert_awaited_once()
