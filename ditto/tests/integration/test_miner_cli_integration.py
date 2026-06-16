"""Integration tests for the miner CLI orchestrator.

Exercises the CLI's subcommand handlers (``commands.upload.run``,
``commands.status.run``, ``commands.verify.run``) end-to-end against
the real api_server lifespan, real Postgres, and real minio.

Design notes
------------

The CLI is synchronous and uses :class:`httpx.Client`. The integration
test injects a :class:`fastapi.testclient.TestClient` into the CLI's
:class:`ApiClient` so calls reach the ASGI app via anyio inside the
TestClient's portal loop. We never open the FastAPI lifespan ourselves
- TestClient owns it. Verification queries also go through the same
TestClient (calling the retrieval endpoints) so we never cross the
TestClient's lifespan loop and the test's own event loop. The shipped
:file:`test_upload_agent_integration.py` pattern (ASGITransport
+ direct ``app.state`` access) cannot be reused here because httpx's
ASGITransport is async-only and the CLI is sync.

What is mocked
- ``bittensor.Subtensor`` (no real chain payment)
- :func:`ditto.miner_cli.wallet.load_wallet` (no real keyfile on disk)
- API dependencies that would otherwise reach Pylon or the oracle:
  ``get_chain_client``, ``get_price_oracle``, ``get_payment_verifier``

What is real
- The full FastAPI lifespan (DB engine + S3 client)
- Postgres TRUNCATE between tests
- The CLI orchestrator's control flow + the API endpoints it calls

Run via ``make test-integration``; excluded from the default suite.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import subprocess
import tarfile
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import bittensor
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from ditto.api_server import create_api_server, parse_api_server_config_from_env
from ditto.api_server.dependencies import (
    get_chain_client,
    get_payment_verifier,
    get_price_oracle,
)
from ditto.api_server.payment_verifier import VerifiedPayment
from ditto.db.models import Agent, AgentStatus
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.commands import status as status_cmd
from ditto.miner_cli.commands import upload as upload_cmd
from ditto.miner_cli.commands import verify as verify_cmd
from ditto.miner_cli.models import WalletHandle

pytestmark = pytest.mark.integration


# ---- constants + helpers -------------------------------------------------


_QUOTE_RAO = 17_500_000
_COLDKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"


def _build_tar(tmp_path: Path, name: str = "harness.tar.gz") -> Path:
    """Build a small valid .tar.gz on disk + return the path."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"package main\n\nfunc main() {}\n"
        info = tarfile.TarInfo(name="main.go")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    path = tmp_path / name
    path.write_bytes(buf.getvalue())
    return path


def _block_hash(salt: int) -> str:
    return "0x" + format(salt, "064x")


# ---- fixtures ------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _alembic_upgrade_head() -> None:
    """Apply migrations once per module against the real database."""
    env = os.environ.copy()
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        check=True,
        env=env,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def _truncate_between_tests() -> Iterator[None]:
    """Wipe agents + evaluation_payments before each test so PK + UNIQUE
    constraints don't bleed across function boundaries.

    Sync wrapper around the async sqlalchemy engine so the fixture works
    regardless of whether the test function is sync or async.
    """

    async def _truncate() -> None:
        from ditto.db import create_db_engine

        engine = create_db_engine()
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE TABLE evaluation_payments CASCADE"))
            await conn.execute(text("TRUNCATE TABLE agents CASCADE"))
        await engine.dispose()

    asyncio.run(_truncate())
    yield


# ---- mocked dependencies -------------------------------------------------


def _build_fake_chain() -> MagicMock:
    chain = MagicMock()
    chain.is_registered = AsyncMock(return_value=True)
    chain.get_latest_block = AsyncMock(return_value=MagicMock(number=13579))
    return chain


def _build_fake_chain_unregistered() -> MagicMock:
    chain = MagicMock()
    chain.is_registered = AsyncMock(return_value=False)
    chain.get_latest_block = AsyncMock(return_value=MagicMock(number=13579))
    return chain


def _build_fake_verifier(*, dest_address: str) -> MagicMock:
    verifier = MagicMock()

    async def _verify(proof, *, expected_hotkey: str) -> VerifiedPayment:
        return VerifiedPayment(
            block_hash=proof.block_hash,
            extrinsic_index=proof.extrinsic_index,
            miner_hotkey=expected_hotkey,
            miner_coldkey=_COLDKEY,
            amount_rao=_QUOTE_RAO,
            dest_address=dest_address,
            block_timestamp=datetime(2026, 6, 16, 12, 0, tzinfo=UTC),
        )

    verifier.verify_payment = AsyncMock(side_effect=_verify)
    return verifier


def _build_app(*, chain_is_registered: bool = True) -> FastAPI:
    """Build a FastAPI app with chain / oracle / verifier overrides set.

    Lifespan is NOT opened here; the caller opens it via TestClient so
    we never run the lifespan twice on the same app.
    """
    config = parse_api_server_config_from_env(commit_hash="miner-cli-int-test")
    app = create_api_server(config)

    async def _fake_oracle() -> MagicMock:
        oracle = MagicMock()
        oracle.get_tao_usd = AsyncMock(return_value=Decimal("400"))
        return oracle

    fake_chain = (
        _build_fake_chain() if chain_is_registered else _build_fake_chain_unregistered()
    )

    async def _fake_chain_client() -> MagicMock:
        return fake_chain

    fake_verifier = _build_fake_verifier(dest_address=config.upload_payment_address)

    async def _fake_payment_verifier() -> MagicMock:
        return fake_verifier

    app.dependency_overrides[get_price_oracle] = _fake_oracle
    app.dependency_overrides[get_chain_client] = _fake_chain_client
    app.dependency_overrides[get_payment_verifier] = _fake_payment_verifier

    return app


# ---- CLI patch helpers ---------------------------------------------------


def _install_cli_api_client_patches(test_client: TestClient) -> list:
    """Patch the ``ApiClient`` symbol inside each subcommand module so
    the CLI flows through the in-process ASGI app + lifespan.

    The constructor returns a MagicMock context manager whose
    ``__enter__`` yields a real :class:`ApiClient` wrapping the shared
    TestClient and whose ``__exit__`` does nothing — bypassing the
    real ``ApiClient.__exit__`` that would otherwise close the
    TestClient mid-test. (Python looks up dunders on the class, not
    the instance, so we cannot patch ``__exit__`` on the api_client
    object directly.)
    """
    api_client = ApiClient(client=test_client)

    ctor = MagicMock()
    ctor.return_value.__enter__ = MagicMock(return_value=api_client)
    ctor.return_value.__exit__ = MagicMock(return_value=False)

    return [
        patch.object(upload_cmd, "ApiClient", ctor),
        patch.object(status_cmd, "ApiClient", ctor),
    ]


def _install_chain_payment_patches(*, block_hash: str, ext_idx: int = 0) -> list:
    fake_subtensor = MagicMock()
    fake_response = MagicMock()
    fake_response.success = True
    receipt = MagicMock()
    receipt.block_hash = block_hash
    receipt.block_number = 13579
    receipt.extrinsic_idx = ext_idx
    fake_response.extrinsic_receipt = receipt
    fake_subtensor.transfer = MagicMock(return_value=fake_response)

    return [
        patch.object(bittensor, "Subtensor", lambda **_kw: fake_subtensor),
    ]


def _install_wallet_patch(*, hotkey_ss58: str) -> list:
    """Patch load_wallet so CLI never reads a real keyfile."""
    handle = WalletHandle(
        coldkey_name="miner",
        hotkey_name="default",
        hotkey_ss58=hotkey_ss58,
    )

    keypair = bittensor.Keypair.create_from_uri("//Alice")
    live = MagicMock()
    live.hotkey = keypair
    live.coldkey = keypair

    return [
        patch(
            "ditto.miner_cli.commands.upload.load_wallet", return_value=(handle, live)
        ),
        patch(
            "ditto.miner_cli.commands.status.load_wallet", return_value=(handle, live)
        ),
    ]


def _multi(patches: list):  # type: ignore[no-untyped-def]
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


# ---- argparse namespace builders -----------------------------------------


def _upload_args(tar_path: Path, **overrides) -> argparse.Namespace:
    base = {
        "tar_path": tar_path,
        "name": "smoke-agent",
        "coldkey_name": "miner",
        "hotkey_name": "default",
        "yes": True,
        "network": "local",
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _status_args(**overrides) -> argparse.Namespace:
    base = {
        "agent_id": None,
        "coldkey_name": None,
        "hotkey_name": None,
        "json": False,
        "network": "local",
        "verbose": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _verify_args(tar_path: Path) -> argparse.Namespace:
    return argparse.Namespace(tar_path=tar_path, network="local", verbose=False)


# ---- DB verification helpers ---------------------------------------------


def _agent_for_hotkey_via_api(test_client: TestClient, *, hotkey: str) -> dict:
    """Pull the agent row via the retrieval endpoint instead of DB."""
    response = test_client.get(
        f"/api/v1/retrieval/agent-by-hotkey?miner_hotkey={hotkey}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seed_agents(*, hotkey: str, count: int) -> list[UUID]:
    """Seed N agent rows for the given hotkey at increasing timestamps.

    Used by hotkey-resolves-latest. Runs in a sync wrapper around the
    async engine so it does not require the test to be async.
    """
    ids: list[UUID] = []

    async def _seed() -> None:
        from ditto.db import create_db_engine

        engine = create_db_engine()
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session, session.begin():
            for i in range(count):
                row = Agent(
                    agent_id=uuid4(),
                    miner_hotkey=hotkey,
                    name=f"alpha-{i}",
                    sha256="ab" * 32,
                    status=AgentStatus.UPLOADED,
                )
                row.created_at = datetime(2026, 6, 16, 12, i, tzinfo=UTC)
                session.add(row)
                ids.append(row.agent_id)
        await engine.dispose()

    asyncio.run(_seed())
    return ids


# ---- tests ---------------------------------------------------------------


class TestMinerCliIntegration:
    def test_verify_happy_and_failure_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pure local: no app, no chain, no DB needed."""
        good = _build_tar(tmp_path, "good.tar.gz")
        bad = tmp_path / "bad.tar.gz"
        bad.write_bytes(b"NOTGZIP" * 100)

        assert verify_cmd.run(_verify_args(good)) == 0
        assert verify_cmd.run(_verify_args(bad)) == 1
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "FAIL" in out

    def test_upload_full_happy_path(self, tmp_path: Path) -> None:
        tar_path = _build_tar(tmp_path)
        keypair = bittensor.Keypair.create_from_uri("//Alice")
        app = _build_app()

        with TestClient(app, base_url="http://test") as test_client:
            patches = [
                *_install_cli_api_client_patches(test_client),
                *_install_chain_payment_patches(block_hash=_block_hash(1)),
                *_install_wallet_patch(hotkey_ss58=keypair.ss58_address),
            ]
            with _multi(patches):
                rc = upload_cmd.run(_upload_args(tar_path))

            assert rc == 0

            # Verify via retrieval endpoint instead of direct DB.
            body = _agent_for_hotkey_via_api(test_client, hotkey=keypair.ss58_address)
            assert body["status"] == "uploaded"
            assert body["miner_hotkey"] == keypair.ss58_address

    def test_upload_preflight_fail_aborts_before_chain(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.tar.gz"
        bad.write_bytes(b"NOTGZIP" * 100)
        keypair = bittensor.Keypair.create_from_uri("//Alice")
        app = _build_app()

        with TestClient(app, base_url="http://test") as test_client:
            fake_subtensor = MagicMock()
            fake_subtensor.transfer = MagicMock()
            patches = [
                *_install_cli_api_client_patches(test_client),
                patch.object(bittensor, "Subtensor", lambda **_kw: fake_subtensor),
                *_install_wallet_patch(hotkey_ss58=keypair.ss58_address),
            ]
            with _multi(patches):
                rc = upload_cmd.run(_upload_args(bad))

            assert rc == 1
            # Chain payment never attempted.
            fake_subtensor.transfer.assert_not_called()
            # No row inserted: retrieval returns 404.
            response = test_client.get(
                f"/api/v1/retrieval/agent-by-hotkey?miner_hotkey={keypair.ss58_address}"
            )
            assert response.status_code == 404

    def test_upload_check_rejection_aborts_before_chain(self, tmp_path: Path) -> None:
        """When /upload/check rejects (hotkey not registered), no
        extrinsic should be submitted."""
        tar_path = _build_tar(tmp_path)
        keypair = bittensor.Keypair.create_from_uri("//Bob")
        app = _build_app(chain_is_registered=False)

        with TestClient(app, base_url="http://test") as test_client:
            fake_subtensor = MagicMock()
            fake_subtensor.transfer = MagicMock()
            patches = [
                *_install_cli_api_client_patches(test_client),
                patch.object(bittensor, "Subtensor", lambda **_kw: fake_subtensor),
                *_install_wallet_patch(hotkey_ss58=keypair.ss58_address),
            ]
            with _multi(patches):
                rc = upload_cmd.run(_upload_args(tar_path))

            assert rc == 1
            fake_subtensor.transfer.assert_not_called()

    def test_upload_payment_proof_recorded_in_db(self, tmp_path: Path) -> None:
        """Replay protection: re-uploading with the same payment proof
        hits the (block_hash, extrinsic_index) PK and is rejected with
        a 402 envelope. Proves the proof tuple was committed.
        """
        tar_path = _build_tar(tmp_path)
        keypair = bittensor.Keypair.create_from_uri("//Alice")
        app = _build_app()
        block_hash = _block_hash(4)

        with TestClient(app, base_url="http://test") as test_client:
            patches = [
                *_install_cli_api_client_patches(test_client),
                *_install_chain_payment_patches(block_hash=block_hash, ext_idx=7),
                *_install_wallet_patch(hotkey_ss58=keypair.ss58_address),
            ]
            with _multi(patches):
                rc = upload_cmd.run(_upload_args(tar_path))
                assert rc == 0

                # Second upload with the same patched payment proof
                # must be rejected: PK collision on
                # evaluation_payments(block_hash, extrinsic_index).
                rc_replay = upload_cmd.run(_upload_args(tar_path))

            assert rc_replay == 1  # upload-after-payment error

    def test_status_by_id_after_upload(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        tar_path = _build_tar(tmp_path)
        keypair = bittensor.Keypair.create_from_uri("//Alice")
        app = _build_app()

        with TestClient(app, base_url="http://test") as test_client:
            patches = [
                *_install_cli_api_client_patches(test_client),
                *_install_chain_payment_patches(block_hash=_block_hash(5)),
                *_install_wallet_patch(hotkey_ss58=keypair.ss58_address),
            ]
            with _multi(patches):
                rc_upload = upload_cmd.run(_upload_args(tar_path))
                assert rc_upload == 0
                capsys.readouterr()  # drain upload output

                body = _agent_for_hotkey_via_api(
                    test_client, hotkey=keypair.ss58_address
                )
                agent_id = UUID(body["agent_id"])

                rc_status = status_cmd.run(_status_args(agent_id=agent_id))

            assert rc_status == 0
            out = capsys.readouterr().out
            assert str(agent_id) in out
            assert "uploaded" in out

    def test_status_by_hotkey_resolves_latest(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Seed three rows for the same hotkey; ``ditto status`` with
        no positional resolves via hotkey + returns the newest."""
        keypair = bittensor.Keypair.create_from_uri("//Alice")
        ids = _seed_agents(hotkey=keypair.ss58_address, count=3)

        app = _build_app()
        with TestClient(app, base_url="http://test") as test_client:
            patches = [
                *_install_cli_api_client_patches(test_client),
                *_install_wallet_patch(hotkey_ss58=keypair.ss58_address),
            ]
            with _multi(patches):
                rc = status_cmd.run(
                    _status_args(coldkey_name="miner", hotkey_name="default")
                )

            assert rc == 0
            out = capsys.readouterr().out
            # Newest row's agent_id appears in the printout.
            assert str(ids[-1]) in out
