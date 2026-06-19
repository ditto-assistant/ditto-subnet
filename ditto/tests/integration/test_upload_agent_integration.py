"""Integration tests for ``POST /upload/agent``.

Exercises the real api_server lifespan against real Postgres + real
minio, focused on endpoint composition + DB writes + storage. The
chain client, price oracle, AND :class:`PaymentVerifier` are
overridden via ``app.dependency_overrides`` because the lifespan-
opened verifier holds a real ChainClient that would try to talk to
Pylon. The mocked verifier echoes ``expected_hotkey`` back into a
canned :class:`VerifiedPayment` so the composite FK on
``evaluation_payments`` resolves at INSERT time. The verifier's
chain-side logic is exercised at the unit-test layer in
``ditto/tests/api_server/payment_verifier/``.

Run via ``make test-integration`` (excluded from the default suite).
Requires ``docker compose up`` for postgres + minio + the
minio-create-bucket sidecar that provisions the ``ditto-agents``
bucket.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from ditto.api_server import create_api_server, parse_api_server_config_from_env
from ditto.api_server.dependencies import (
    get_chain_client,
    get_payment_verifier,
    get_price_oracle,
)
from ditto.api_server.middleware.error_envelope import ERROR_CODE_PAYMENT_REPLAYED
from ditto.api_server.payment_verifier import VerifiedPayment

pytestmark = pytest.mark.integration


_TAR_BYTES = b"\x1f\x8b" + b"x" * 1024
_TAR_SHA = hashlib.sha256(_TAR_BYTES).hexdigest()
# 17_500_000 rao = $5 fee * 1.4 buffer / $400 oracle price.
_QUOTE_RAO = 17_500_000
_COLDKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def _build_fake_chain(*, is_registered: bool = True) -> MagicMock:
    """A MagicMock ChainClient for the hotkey-registered check.

    The PaymentVerifier itself is overridden separately, so the chain
    client only needs to answer ``is_registered``. We mock the chain
    rather than letting the real lifespan-opened ChainClient talk to
    Pylon: this test does not require chain access, and the lifespan
    has already opened a real client that would not understand canned
    payment data.

    Pass ``is_registered=False`` to exercise the server-side
    belt-and-suspenders path where /upload/agent rejects unregistered
    hotkeys regardless of what the CLI did at /upload/check.
    """
    chain = MagicMock()
    chain.is_registered = AsyncMock(return_value=is_registered)
    chain.get_latest_block = AsyncMock(return_value=MagicMock(number=13579))
    return chain


def _build_fake_verifier(
    *, block_hash: str, ext_idx: int, dest_address: str
) -> MagicMock:
    """Stub PaymentVerifier returning a canned VerifiedPayment.

    The verifier's chain-side logic is exercised at the unit-test
    layer. Here the integration test focuses on endpoint composition +
    DB writes + storage; the verifier is mocked so the test does not
    have to construct plausible chain state.

    ``verify_payment`` echoes the ``expected_hotkey`` argument into the
    returned VerifiedPayment.miner_hotkey so the composite FK on
    evaluation_payments (which references agents.miner_hotkey) is
    satisfied at INSERT time.
    """
    verifier = MagicMock()

    async def _verify(_proof, *, expected_hotkey: str) -> VerifiedPayment:
        return VerifiedPayment(
            block_hash=block_hash,
            extrinsic_index=ext_idx,
            miner_hotkey=expected_hotkey,
            miner_coldkey=_COLDKEY,
            amount_rao=_QUOTE_RAO,
            dest_address=dest_address,
            block_timestamp=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
        )

    verifier.verify_payment = AsyncMock(side_effect=_verify)
    return verifier


@asynccontextmanager
async def _running_app(
    *,
    block_hash: str,
    ext_idx: int = 0,
    chain_is_registered: bool = True,
) -> AsyncIterator[FastAPI]:
    config = parse_api_server_config_from_env(commit_hash="integration-test")
    app = create_api_server(config)

    async def _fake_oracle() -> MagicMock:
        oracle = MagicMock()
        oracle.get_tao_usd = AsyncMock(return_value=Decimal("400"))
        return oracle

    fake_chain = _build_fake_chain(is_registered=chain_is_registered)

    async def _fake_chain_client() -> MagicMock:
        return fake_chain

    fake_verifier = _build_fake_verifier(
        block_hash=block_hash,
        ext_idx=ext_idx,
        dest_address=config.upload_payment_address,
    )

    async def _fake_payment_verifier() -> MagicMock:
        return fake_verifier

    app.dependency_overrides[get_price_oracle] = _fake_oracle
    app.dependency_overrides[get_chain_client] = _fake_chain_client
    app.dependency_overrides[get_payment_verifier] = _fake_payment_verifier

    async with app.router.lifespan_context(app):
        yield app


def _build_form(
    *,
    keypair: bittensor.Keypair,
    block_hash: str,
    ext_idx: int = 0,
    sha256: str = _TAR_SHA,
    name: str = "alpha-agent",
) -> tuple[dict[str, Any], dict[str, tuple[str, bytes, str]]]:
    hotkey = keypair.ss58_address
    payload = f"{hotkey}:{sha256}".encode()
    data: dict[str, Any] = {
        "hotkey": hotkey,
        "sha256": sha256,
        "name": name,
        "signature": keypair.sign(payload).hex(),
        "payment_block_hash": block_hash,
        "payment_block_number": 13579,
        "payment_extrinsic_index": ext_idx,
    }
    files = {"agent_tar": ("harness.tar.gz", _TAR_BYTES, "application/gzip")}
    return data, files


def _new_block_hash(salt: int) -> str:
    """Distinct (block_hash, extrinsic_index) per test so payments do not
    collide across test functions (DB persists across runs)."""
    return "0x" + format(salt, "064x")


@pytest.fixture(scope="module", autouse=True)
def _alembic_upgrade_head() -> None:
    """Apply migrations to the integration database once per session.

    Uses ``alembic upgrade head`` over the same DSN the api_server reads
    from ``.env``, so the test schema matches production exactly.
    """
    env = os.environ.copy()
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        check=True,
        env=env,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
async def _truncate_between_tests() -> AsyncIterator[None]:
    """Wipe agents + evaluation_payments before each test so PK + UNIQUE
    constraints don't bleed across function boundaries."""
    from ditto.db import create_db_engine

    engine = create_db_engine()
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE evaluation_payments CASCADE"))
        await conn.execute(text("TRUNCATE TABLE agents CASCADE"))
    await engine.dispose()
    yield


class TestUploadAgentIntegration:
    async def test_happy_path_full_submit(self):
        kp = bittensor.Keypair.create_from_uri("//Alice")
        block_hash = _new_block_hash(1)
        async with _running_app(block_hash=block_hash) as app:
            data, files = _build_form(keypair=kp, block_hash=block_hash)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/upload/agent", data=data, files=files
                )

            assert response.status_code == 200, response.text
            body = response.json()
            agent_id = body["agent_id"]
            assert body["status"] == "uploaded"

            # DB rows committed atomically.
            from sqlalchemy import select

            from ditto.db.models import Agent, EvaluationPayment

            session_maker = app.state.session_maker
            async with session_maker() as session:
                agent_row = (
                    await session.execute(
                        select(Agent).where(Agent.miner_hotkey == kp.ss58_address)
                    )
                ).scalar_one()
                assert str(agent_row.agent_id) == agent_id

                payment_row = (
                    await session.execute(
                        select(EvaluationPayment).where(
                            EvaluationPayment.block_hash == block_hash
                        )
                    )
                ).scalar_one()
                assert str(payment_row.agent_id) == agent_id
                assert payment_row.amount_rao == _QUOTE_RAO

            # Object landed in minio at the expected key.
            storage = app.state.storage
            assert await storage.object_exists(key=f"{agent_id}/agent.tar.gz")

    async def test_replay_rejected(self):
        kp = bittensor.Keypair.create_from_uri("//Alice")
        block_hash = _new_block_hash(2)
        async with _running_app(block_hash=block_hash) as app:
            data, files = _build_form(keypair=kp, block_hash=block_hash)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                first = await client.post(
                    "/api/v1/upload/agent", data=data, files=files
                )
                assert first.status_code == 200, first.text

                # Replay the exact same form.
                data2, files2 = _build_form(keypair=kp, block_hash=block_hash)
                second = await client.post(
                    "/api/v1/upload/agent", data=data2, files=files2
                )

            assert second.status_code == 402
            assert second.json()["error_code"] == ERROR_CODE_PAYMENT_REPLAYED

    async def test_pk_constraint_name_matches_dispatch_constant(self):
        """The queries-layer dispatch hard-codes
        ``evaluation_payments_pkey`` as the PK constraint name. If a
        future migration ever renames the PK, this test fires before
        the silent regression where the dispatch stops translating
        replays into PaymentReplayedError."""
        from ditto.db import create_db_engine
        from ditto.db.queries.payments import _PAYMENT_REPLAY_CONSTRAINT

        engine = create_db_engine()
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'evaluation_payments'::regclass "
                    "AND contype = 'p'"
                )
            )
            names = [row[0] for row in result]
        await engine.dispose()

        assert _PAYMENT_REPLAY_CONSTRAINT in names, (
            f"PK constraint name drift: dispatch expects "
            f"{_PAYMENT_REPLAY_CONSTRAINT!r}, runtime has {names!r}"
        )

    async def test_atomic_rollback_on_payment_replay(self):
        """Reject the second submit and verify NO orphan agent row was
        inserted. The composite tx must roll the agent insert back too."""
        from sqlalchemy import func, select

        from ditto.db.models import Agent

        kp = bittensor.Keypair.create_from_uri("//Alice")
        block_hash = _new_block_hash(3)
        async with _running_app(block_hash=block_hash) as app:
            data, files = _build_form(keypair=kp, block_hash=block_hash)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                ok = await client.post("/api/v1/upload/agent", data=data, files=files)
                assert ok.status_code == 200, ok.text

                data2, files2 = _build_form(keypair=kp, block_hash=block_hash)
                replay = await client.post(
                    "/api/v1/upload/agent", data=data2, files=files2
                )
            assert replay.status_code == 402

            session_maker = app.state.session_maker
            async with session_maker() as session:
                count = (
                    await session.execute(
                        select(func.count())
                        .select_from(Agent)
                        .where(Agent.miner_hotkey == kp.ss58_address)
                    )
                ).scalar_one()
            # Exactly one agent row: the original. The replayed insert
            # must have rolled back together with its agent companion.
            assert count == 1

    async def test_unregistered_hotkey_rejected_with_no_side_effects(self):
        """Server-side belt-and-suspenders gate.

        A forked CLI that skips /upload/check and posts straight to
        /upload/agent must still be blocked at the endpoint when the
        signing hotkey is not registered on the configured netuid.
        The rejection must fire BEFORE any side effect: no agent row,
        no payment row, no S3 object. The proof tuple stays vacant in
        evaluation_payments so the miner can retry after registering.
        """
        from sqlalchemy import func, select

        from ditto.db.models import Agent, EvaluationPayment

        kp = bittensor.Keypair.create_from_uri("//Alice")
        block_hash = _new_block_hash(4)
        async with _running_app(
            block_hash=block_hash, chain_is_registered=False
        ) as app:
            data, files = _build_form(keypair=kp, block_hash=block_hash)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/upload/agent", data=data, files=files
                )

            assert response.status_code == 400, response.text
            assert "not registered" in response.json()["message"]

            # No agent row written.
            session_maker = app.state.session_maker
            async with session_maker() as session:
                agent_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(Agent)
                        .where(Agent.miner_hotkey == kp.ss58_address)
                    )
                ).scalar_one()
                # No payment row written: PK stays vacant so the proof
                # is still consumable after the hotkey registers.
                payment_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(EvaluationPayment)
                        .where(EvaluationPayment.block_hash == block_hash)
                    )
                ).scalar_one()
            assert agent_count == 0
            assert payment_count == 0

            # No S3 object should have been written either; the rejection
            # fires BEFORE the storage.put_object call at upload.py:255.
            # Object keys are agent-id-prefixed and agent_id is generated
            # only after the registration + payment checks pass, so any
            # object under the bucket from THIS test would be a regression.
            # (Other tests' objects coexist; we cannot list-and-assert-empty
            # without disrupting them. The DB-rowcount assertions above are
            # the canonical side-effect check for this path.)
