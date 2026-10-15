"""Stored artifact binding and retry compatibility across CLA versions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
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
from ditto.db.models import (
    Agent,
    AgentClaBinding,
    AgentStatus,
    ClaShadowUpload,
    EvaluationPayment,
    UploadAdmissionReservation,
)
from ditto.tests.api_server.conftest import make_api_server_config
from ditto.tests.api_server.endpoints.test_contributor_cla import (
    elapse_cla_notice,
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
    await elapse_cla_notice(session_maker)
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
        await elapse_cla_notice(session_maker)
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
        # Without the payment proof this is a request to buy another seed of the
        # accepted artifact: a new contribution, so the current CLA applies too.
        # The Go relay serves only these proof-less checks and must match.
        unpaid = {
            key: value
            for key, value in recovery.items()
            if not key.startswith("payment_")
        }
        unpaid_check = await client.post("/api/v1/upload/check", json=unpaid)
        assert unpaid_check.status_code == 200, unpaid_check.text
        assert not unpaid_check.json()["ok"]
        assert {1104, 1106} <= set(unpaid_check.json()["error_codes"])
        legacy_reserved_retry = await client.post(
            "/api/v1/upload/check",
            json={**recovery, "name": None, "reserve_submission_slot": True},
        )
        assert legacy_reserved_retry.status_code == 200, legacy_reserved_retry.text
        assert legacy_reserved_retry.json()["ok"]
        assert not legacy_reserved_retry.json()["payment_required"]
        legacy_unnamed = await client.post(
            "/api/v1/upload/check", json={**recovery, "name": None}
        )
        assert legacy_unnamed.status_code == 200, legacy_unnamed.text
        assert legacy_unnamed.json()["ok"]
        assert not legacy_unnamed.json()["payment_required"]
        # Any other upload on the assigned proof is the replay the multipart
        # commit reports before its CLA gate; signing could never fix it.
        for change in (
            {"name": "different-name"},
            _signed_request_body(keypair=alice, sha256="ab" * 32),
        ):
            replay = await client.post(
                "/api/v1/upload/check", json={**recovery, **change}
            )
            assert replay.status_code == 402, replay.text
            assert replay.json()["error_code"] == 3207
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
        # Another hotkey's credit is a replay that signing cannot fix, so both
        # the dry run and the commit report 3207 before consent.
        foreign_block = "0x" + "33" * 32
        async with session_maker() as session, session.begin():
            session.add(
                EvaluationPayment(
                    block_hash=foreign_block,
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
        foreign_check = await client.post(
            "/api/v1/upload/check",
            json={**recovery, "payment_block_hash": foreign_block},
        )
        assert foreign_check.status_code == 402, foreign_check.text
        assert foreign_check.json()["error_code"] == 3207
        foreign_data, foreign_files = _build_form(
            keypair=alice, block_hash=foreign_block, name="credit-agent"
        )
        foreign = await client.post(
            "/api/v1/upload/agent", data=foreign_data, files=foreign_files
        )
        assert foreign.status_code == 402, foreign.text
        assert foreign.json()["error_code"] == 3207
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
        # An exact retry returns the original row whatever its status, so the
        # preflight must agree with the multipart endpoint after a rejection.
        async with session_maker() as session, session.begin():
            (await session.get(Agent, agent_id)).status = AgentStatus.REJECTED
        rejected_check = await client.post("/api/v1/upload/check", json=recovery)
        assert rejected_check.status_code == 200, rejected_check.text
        assert rejected_check.json()["ok"], rejected_check.text
        rejected_data, rejected_files = _build_form(
            keypair=alice, block_hash=block_hash
        )
        rejected_retry = await client.post(
            "/api/v1/upload/agent", data=rejected_data, files=rejected_files
        )
        assert rejected_retry.status_code == 200, rejected_retry.text
        assert rejected_retry.json()["agent_id"] == str(agent_id)
        assert rejected_retry.json()["status"] == AgentStatus.REJECTED.value
        # No new storage/payment or retroactive receipt is created.
        verifier.verify_payment.assert_awaited_once()
        async with session_maker() as session:
            assert list(await session.scalars(select(AgentClaBinding))) == [
                await session.get(AgentClaBinding, agent_id)
            ]
            assert (await session.get(AgentClaBinding, agent_id)).signature_id == UUID(
                receipt.json()["signature_id"]
            )


def _paying(app, alice, owner, block_hash):
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
    return verifier


async def _row_counts(session_maker) -> tuple[int, int, int, int]:
    async with session_maker() as session:
        return tuple(  # type: ignore[return-value]
            [
                int(await session.scalar(select(func.count()).select_from(model)))
                for model in (
                    Agent,
                    EvaluationPayment,
                    AgentClaBinding,
                    ClaShadowUpload,
                )
            ]
        )


async def test_final_consent_gate_runs_last_and_rolls_back_every_write(
    app, client, session_maker, object_storage, monkeypatch
):
    """The in-transaction gate decides after the lock-waiting writes."""
    del object_storage
    from ditto.api_server.endpoints import upload as upload_endpoint

    setup(app, session_maker)
    app.state.session_maker = session_maker
    app.state.embedder = SimpleNamespace(embed=AsyncMock(return_value=None))
    alice = bittensor.Keypair.create_from_uri("//Alice")
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    version = await publish(client)
    receipt = await client.post("/api/v1/cla/sign", json=signed(alice, version))
    assert receipt.status_code == 200, receipt.text
    await elapse_cla_notice(session_maker)
    await policy(client, "enforce")
    block_hash = "0x" + "51" * 32
    _paying(app, alice, owner, block_hash)
    real_gate = upload_endpoint.evaluate_cla_gate
    seen_in_transaction: list[int] = []

    async def gate(session, **kwargs):
        decision = await real_gate(session, **kwargs)
        if session.in_transaction() and await session.scalar(
            select(func.count()).select_from(Agent)
        ):
            # The final gate: the agent and its payment are already written.
            seen_in_transaction.append(
                int(
                    await session.scalar(
                        select(func.count()).select_from(EvaluationPayment)
                    )
                )
            )
            # Simulate a version that became effective during the lock waits.
            return replace(decision, allowed=False, signature_id=None)
        return decision

    monkeypatch.setattr(upload_endpoint, "evaluate_cla_gate", gate)
    async with create_storage_client(parse_storage_config_from_env()) as storage:

        async def get_storage():
            return storage

        app.dependency_overrides[get_storage_client] = get_storage
        data, files = _build_form(keypair=alice, block_hash=block_hash)
        refused = await client.post("/api/v1/upload/agent", data=data, files=files)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error_code"] == 1106
    assert seen_in_transaction == [1]
    assert await _row_counts(session_maker) == (0, 0, 0, 0)


@pytest.mark.parametrize(
    "refused_at", ["consent_pregate", "payment_record", "duplicate_credit", "admission"]
)
async def test_lost_race_to_an_identical_retry_returns_the_original(
    app, client, session_maker, object_storage, monkeypatch, refused_at
):
    """A loser refused by the winner's commit still gets the original agent."""
    del object_storage
    from ditto.api_server.endpoints import upload as upload_endpoint
    from ditto.db.queries.agents import SubmissionCooldownError

    setup(app, session_maker)
    app.state.session_maker = session_maker
    app.state.embedder = SimpleNamespace(embed=AsyncMock(return_value=None))
    alice = bittensor.Keypair.create_from_uri("//Alice")
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    block_hash = "0x" + "52" * 32
    _paying(app, alice, owner, block_hash)
    async with create_storage_client(parse_storage_config_from_env()) as storage:

        async def get_storage():
            return storage

        app.dependency_overrides[get_storage_client] = get_storage
        data, files = _build_form(keypair=alice, block_hash=block_hash)
        winner = await client.post("/api/v1/upload/agent", data=data, files=files)
        assert winner.status_code == 200, winner.text
        # The loser carries the temporary reservation that a recovery check
        # issues for an exact retry. Every recovery must release it.
        check_data, _ = _build_form(keypair=alice, block_hash=block_hash)
        reserved = await client.post(
            "/api/v1/upload/check",
            json={
                "hotkey": alice.ss58_address,
                "sha256": _TAR_SHA,
                "name": check_data["name"],
                "signature": check_data["signature"],
                "signature_timestamp": check_data["signature_timestamp"],
                "signature_nonce": check_data["signature_nonce"],
                "file_size_bytes": 100,
                "payment_block_hash": block_hash,
                "payment_block_number": 123,
                "payment_extrinsic_index": 0,
                "reserve_submission_slot": True,
            },
        )
        assert reserved.status_code == 200, reserved.text
        token = reserved.json()["admission_token"]
        assert token is not None
        # Replay the race: the loser's first proof lookup ran before the
        # winner committed. Later steps then meet the winner's commit.
        real_lookup = upload_endpoint.get_agent_for_payment_proof
        lookups = 0

        async def lookup(*args, **kwargs):
            nonlocal lookups
            lookups += 1
            return None if lookups == 1 else await real_lookup(*args, **kwargs)

        monkeypatch.setattr(upload_endpoint, "get_agent_for_payment_proof", lookup)
        if refused_at == "consent_pregate":
            # A policy change refuses the loser before its payment record.
            real_gate = upload_endpoint.evaluate_cla_gate

            async def refusing_gate(session, **kwargs):
                return replace(
                    await real_gate(session, **kwargs),
                    allowed=False,
                    message="fixture consent refusal",
                )

            monkeypatch.setattr(upload_endpoint, "evaluate_cla_gate", refusing_gate)
        if refused_at != "payment_record":
            # The payment record is real only where the winner's assignment
            # itself is the refusal.
            monkeypatch.setattr(
                upload_endpoint,
                "get_evaluation_payment_for_proof",
                AsyncMock(return_value=None),
            )
        if refused_at == "admission":
            # The duplicate lookup also ran early; the winner's admission
            # then refuses the loser inside its transaction.
            monkeypatch.setattr(
                upload_endpoint,
                "get_same_owner_agent_by_sha",
                AsyncMock(return_value=None),
            )

            async def cooldown(*_args, **_kwargs):
                raise SubmissionCooldownError(datetime.now(UTC))

            monkeypatch.setattr(
                upload_endpoint, "consume_or_enforce_upload_admission", cooldown
            )
        retry_data, retry_files = _build_form(keypair=alice, block_hash=block_hash)
        retry_data["admission_token"] = token
        loser = await client.post(
            "/api/v1/upload/agent", data=retry_data, files=retry_files
        )
    assert loser.status_code == 200, loser.text
    assert loser.json()["agent_id"] == winner.json()["agent_id"]
    async with session_maker() as session:
        assert await session.scalar(select(func.count()).select_from(Agent)) == 1
        # The reservation never outlives the recovered retry.
        assert (
            await session.scalar(
                select(UploadAdmissionReservation).where(
                    UploadAdmissionReservation.token == UUID(token)
                )
            )
            is None
        )


async def test_upload_while_the_gate_is_off_binds_nothing(
    app, client, session_maker, object_storage
):
    """A signed miner's upload stays unbound, so it remains deletable."""
    del object_storage
    setup(app, session_maker)
    app.state.session_maker = session_maker
    app.state.embedder = SimpleNamespace(embed=AsyncMock(return_value=None))
    alice = bittensor.Keypair.create_from_uri("//Alice")
    owner = bittensor.Keypair.create_from_uri("//Bob").ss58_address
    version = await publish(client)
    receipt = await client.post("/api/v1/cla/sign", json=signed(alice, version))
    assert receipt.status_code == 200, receipt.text
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
    async with create_storage_client(parse_storage_config_from_env()) as storage:

        async def get_storage():
            return storage

        app.dependency_overrides[get_storage_client] = get_storage
        data, files = _build_form(keypair=alice, block_hash=block_hash)
        uploaded = await client.post("/api/v1/upload/agent", data=data, files=files)
    assert uploaded.status_code == 200, uploaded.text
    async with session_maker() as session:
        assert await session.get(Agent, UUID(uploaded.json()["agent_id"])) is not None
        assert (
            await session.scalar(select(func.count()).select_from(AgentClaBinding)) == 0
        )


async def test_policy_then_coldkey_lock_order_survives_a_queued_policy_writer(
    app, client, session_maker, object_storage, monkeypatch
):
    """Pin the shared policy -> coldkey lock order with a real deadlock setup.

    The upload holds the shared policy lock and is about to take the coldkey
    lock; an operator policy write queues for the exclusive lock behind it; a
    reserving check then arrives. With the shared order the check waits on the
    policy lock and all three finish. A check that took the coldkey lock first
    would wait behind the queued writer while the upload waited on the check,
    and PostgreSQL would abort one of them as a deadlock.
    """
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
    await elapse_cla_notice(session_maker)
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
    holding_policy = asyncio.Event()
    continue_commit = asyncio.Event()
    original = upload.consume_or_enforce_upload_admission

    async def pause_before_coldkey_lock(*args, **kwargs):
        holding_policy.set()
        await continue_commit.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(
        upload, "consume_or_enforce_upload_admission", pause_before_coldkey_lock
    )

    async def wait_for_advisory_waiters(count: int, *tasks: asyncio.Task) -> None:
        # Observe real PostgreSQL lock queues; request scheduling alone could
        # let the requests run one after another and hide an ordering bug.
        async with session_maker() as session:
            while True:
                for task in tasks:
                    assert not task.done(), "request did not queue on a lock"
                waiting = await session.scalar(
                    text("""
                    SELECT count(*) FROM pg_locks
                    WHERE locktype = 'advisory' AND NOT granted
                      AND database = (SELECT oid FROM pg_database
                                      WHERE datname = current_database())
                """)
                )
                await session.rollback()
                if waiting >= count:
                    return
                await asyncio.sleep(0.01)

    async with create_storage_client(parse_storage_config_from_env()) as storage:
        app.dependency_overrides[get_storage_client] = lambda: storage
        data, files = _build_form(keypair=alice, block_hash=block_hash)
        uploading = asyncio.create_task(
            client.post("/api/v1/upload/agent", data=data, files=files)
        )
        tasks = [uploading]
        try:
            async with asyncio.timeout(15):
                await holding_policy.wait()
                writing = asyncio.create_task(policy(client, "enforce"))
                tasks.append(writing)
                await wait_for_advisory_waiters(1, writing)
                body = _signed_request_body(keypair=alice, sha256="e" * 64)
                body["reserve_submission_slot"] = True
                checking = asyncio.create_task(
                    client.post("/api/v1/upload/check", json=body)
                )
                tasks.append(checking)
                await wait_for_advisory_waiters(2, writing, checking)
                continue_commit.set()
                uploaded, _written, checked = await asyncio.gather(*tasks)
            assert uploaded.status_code == 200, uploaded.text
            assert checked.status_code == 200, checked.text
            assert checked.json()["error_codes"] == [1105]
            assert checked.json()["admission_token"] is None
        finally:
            continue_commit.set()
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
