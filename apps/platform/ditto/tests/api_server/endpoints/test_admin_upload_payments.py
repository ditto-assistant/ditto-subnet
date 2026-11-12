"""What Platform recorded for one upload-payment proof (SN-455)."""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    AgentStatus,
    EvaluationPayment,
    GeneratorSubmission,
    GeneratorSubmissionPayment,
)

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}", "X-Admin-Actor": "operator"}
_HOTKEY = "5MinerHotkey"
_COLDKEY = "5PaymentColdkey"
_DEST = "5UploadFeeAddress"


def _proof(seed: str) -> str:
    return "0x" + hashlib.sha256(seed.encode()).hexdigest()


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_agent(
    maker: async_sessionmaker[AsyncSession], *, name: str, created_at: datetime
) -> UUID:
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=_HOTKEY,
                name=name,
                version=34,
                sha256=agent_id.hex * 2,
                status=AgentStatus.SCORED,
                created_at=created_at,
                screening_policy_version=14,
            )
        )
    return agent_id


async def _seed_payment(
    maker: async_sessionmaker[AsyncSession],
    *,
    block_hash: str,
    block_time: datetime,
    agent_id: UUID | None = None,
    credit_for_agent_id: UUID | None = None,
) -> None:
    async with maker() as session, session.begin():
        session.add(
            EvaluationPayment(
                block_hash=block_hash,
                extrinsic_index=12,
                agent_id=agent_id,
                credit_for_agent_id=credit_for_agent_id,
                miner_hotkey=_HOTKEY,
                miner_coldkey=_COLDKEY,
                amount_rao=100_000_000,
                dest_address=_DEST,
                timestamp=block_time,
            )
        )


async def _read(
    client: httpx.AsyncClient, block_hash: str, extrinsic_index: int = 12
) -> dict:
    response = await client.get(
        f"/api/v1/admin/upload-payments/{block_hash}/{extrinsic_index}",
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_a_consumed_proof_names_the_agent_it_funded(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    block_time = datetime(2026, 11, 6, 9, 38, 36, tzinfo=UTC)
    agent_id = await _seed_agent(
        session_maker, name="agent", created_at=block_time + timedelta(hours=2)
    )
    block_hash = _proof("funded")
    await _seed_payment(
        session_maker, block_hash=block_hash, block_time=block_time, agent_id=agent_id
    )

    # Miners quote hashes in whatever case their explorer prints.
    body = await _read(client, block_hash.upper().replace("0X", "0x"))

    assert body["state"] == "funded_agent"
    assert body["block_hash"] == block_hash
    assert body["extrinsic_index"] == 12
    assert body["miner_hotkey"] == _HOTKEY
    assert body["miner_coldkey"] == _COLDKEY
    assert body["amount_rao"] == 100_000_000
    assert body["dest_address"] == _DEST
    assert body["block_timestamp"].startswith("2026-11-06T09:38:36")
    assert body["recorded_at"] is not None
    assert body["agent"] == {
        "agent_id": str(agent_id),
        "agent_name": "agent",
        "agent_version": 34,
        "agent_status": "scored",
        "artifact_sha256": agent_id.hex * 2,
        "submitted_at": body["agent"]["submitted_at"],
    }
    assert body["agent"]["submitted_at"].startswith("2026-11-06T11:38:36")
    # A consumed proof has no recovery window left to report.
    assert body["recovery_expires_at"] is None
    assert body["recovery_expired"] is None
    assert body["generator_submission_id"] is None


@pytest.mark.asyncio
async def test_a_credit_reports_its_identical_upload_and_recovery_window(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    earlier = await _seed_agent(session_maker, name="dup", created_at=now)
    fresh_hash, stale_hash = _proof("fresh-credit"), _proof("stale-credit")
    await _seed_payment(
        session_maker,
        block_hash=fresh_hash,
        block_time=now - timedelta(hours=1),
        credit_for_agent_id=earlier,
    )
    await _seed_payment(
        session_maker,
        block_hash=stale_hash,
        block_time=now - timedelta(hours=25),
        credit_for_agent_id=earlier,
    )

    fresh = await _read(client, fresh_hash)
    stale = await _read(client, stale_hash)

    assert fresh["state"] == stale["state"] == "reusable_credit"
    assert fresh["agent"]["agent_id"] == str(earlier)
    assert fresh["recovery_expired"] is False
    assert datetime.fromisoformat(fresh["recovery_expires_at"]) == (
        datetime.fromisoformat(fresh["block_timestamp"]) + timedelta(hours=24)
    )
    assert stale["recovery_expired"] is True


@pytest.mark.asyncio
async def test_a_generator_proof_names_its_submission(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    now = datetime.now(UTC)
    raw = b"generator source"
    submission_id = uuid4()
    block_hash = _proof("generator")
    async with session_maker() as session, session.begin():
        session.add(
            GeneratorSubmission(
                submission_id=submission_id,
                miner_hotkey=_HOTKEY,
                owner_key=_COLDKEY,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                tree_sha256=hashlib.sha256(b"tree").hexdigest(),
                source_bytes=raw,
                size_bytes=len(raw),
                module="example.com/gen",
                go_version="1.23",
                file_count=1,
                status="active",
                attempts=0,
                next_run_at=now,
                submitted_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            GeneratorSubmissionPayment(
                block_hash=block_hash,
                extrinsic_index=3,
                submission_id=submission_id,
                miner_hotkey=_HOTKEY,
                miner_coldkey=_COLDKEY,
                amount_rao=100_000_000,
                dest_address=_DEST,
                timestamp=now,
            )
        )

    body = await _read(client, block_hash, 3)

    assert body["state"] == "funded_generator"
    assert body["generator_submission_id"] == str(submission_id)
    assert body["agent"] is None
    assert body["miner_coldkey"] == _COLDKEY


@pytest.mark.asyncio
async def test_an_unrecorded_proof_reports_no_record(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    block_hash = _proof("never-recorded")

    body = await _read(client, block_hash.upper().replace("0X", "0x"), 7)

    assert body["state"] == "unrecorded"
    assert body["block_hash"] == block_hash
    assert body["extrinsic_index"] == 7
    for field in (
        "miner_hotkey",
        "miner_coldkey",
        "amount_rao",
        "block_timestamp",
        "recorded_at",
        "agent",
        "generator_submission_id",
        "recovery_expires_at",
        "recovery_expired",
    ):
        assert body[field] is None, field


@pytest.mark.asyncio
async def test_requires_admin_and_a_well_formed_proof(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    block_hash = _proof("auth")

    unauthenticated = await client.get(f"/api/v1/admin/upload-payments/{block_hash}/0")
    short_hash = await client.get(
        "/api/v1/admin/upload-payments/0x1234/0", headers=_HEADERS
    )
    negative_index = await client.get(
        f"/api/v1/admin/upload-payments/{block_hash}/-1", headers=_HEADERS
    )
    oversized_index = await client.get(
        f"/api/v1/admin/upload-payments/{block_hash}/{2**31}", headers=_HEADERS
    )

    assert unauthenticated.status_code in {401, 403}
    assert short_hash.status_code == 422
    assert negative_index.status_code == 422
    assert oversized_index.status_code == 422
