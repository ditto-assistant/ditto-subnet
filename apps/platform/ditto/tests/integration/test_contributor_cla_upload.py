"""Stored artifact binding and retry compatibility across CLA versions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import bittensor
import httpx
import pytest
from sqlalchemy import select

from ditto.api_server import create_api_server
from ditto.api_server.dependencies import get_payment_verifier, get_storage_client
from ditto.api_server.payment_verifier import VerifiedPayment
from ditto.api_server.storage import (
    create_storage_client,
    parse_storage_config_from_env,
)
from ditto.db.models import Agent, AgentClaBinding, EvaluationPayment
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.api_server.endpoints.test_contributor_cla import (
    policy,
    publish,
    setup,
    signed,
)
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
        await publish(client, 2)
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
        for name in (None, "different-name"):
            non_exact = await client.post(
                "/api/v1/upload/check", json={**recovery, "name": name}
            )
            assert non_exact.status_code == 200, non_exact.text
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
