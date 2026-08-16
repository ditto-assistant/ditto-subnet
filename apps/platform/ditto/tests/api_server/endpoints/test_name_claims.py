"""End-to-end coverage for signed handle claims and endorsements."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.api_server.name_claim import (
    ENDORSEMENT_THRESHOLD,
    claim_message,
    endorse_message,
    normalize_name_stem,
    require_name_stem,
    withdraw_message,
)
from ditto.db.models import Agent, EvaluationPayment
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, upsert_score

_URL = "/api/v1/name-claims"
_VALIDATOR = "5CiPPseXPECbkjWCa6MnjNokrgYjMqmKndv2rSnekmSK2DjL"


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


def _kp(uri: str) -> bittensor.Keypair:
    return bittensor.Keypair.create_from_uri(uri)


async def _seed_family(
    maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    coldkey: str,
    name: str,
    age: timedelta = timedelta(days=10),
) -> None:
    created = datetime.now(UTC) - age
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                sha256=uuid4().hex + uuid4().hex,
                size_bytes=524288,
                status=AgentStatus.SCORED,
                created_at=created,
            )
        )
        await session.flush()
        session.add(
            EvaluationPayment(
                block_hash=f"0x{agent_id.hex}",
                extrinsic_index=0,
                agent_id=agent_id,
                miner_hotkey=hotkey,
                miner_coldkey=coldkey,
                amount_rao=1,
                dest_address="5Destination",
                timestamp=created,
            )
        )
        await upsert_score(
            session,
            agent_id=agent_id,
            validator_hotkey=_VALIDATOR,
            run_id="run_1",
            seed=42,
            composite=0.5,
            tool_mean=0.5,
            memory_mean=0.5,
            median_ms=500,
            n=MIN_ELIGIBLE_CASES,
            generated_at=created,
            bench_version=MIN_SCOREABLE_BENCH_VERSION,
        )


def _claim_body(
    claimant: bittensor.Keypair,
    *,
    name: str,
    netuid: int = 118,
    nonce: UUID | None = None,
    issued_at: datetime | None = None,
) -> dict:
    nonce = nonce or uuid4()
    issued_at = issued_at or datetime.now(UTC)
    stem = require_name_stem(name)
    payload = claim_message(
        netuid=netuid,
        name_stem=stem,
        claimant_hotkey=claimant.ss58_address,
        nonce=nonce,
        issued_at=issued_at,
        key_kind="hotkey",
        signer=claimant.ss58_address,
    )
    return {
        "netuid": netuid,
        "name": name,
        "claimant_hotkey": claimant.ss58_address,
        "nonce": str(nonce),
        "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "proof": {
            "key_kind": "hotkey",
            "signer": claimant.ss58_address,
            "signature": claimant.sign(payload).hex(),
        },
    }


def _endorse_body(
    endorser: bittensor.Keypair,
    *,
    claim_id: UUID,
    name_stem: str,
    netuid: int = 118,
) -> dict:
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    payload = endorse_message(
        netuid=netuid,
        claim_id=claim_id,
        name_stem=name_stem,
        endorser_hotkey=endorser.ss58_address,
        nonce=nonce,
        issued_at=issued_at,
        key_kind="hotkey",
        signer=endorser.ss58_address,
    )
    return {
        "netuid": netuid,
        "name_stem": name_stem,
        "endorser_hotkey": endorser.ss58_address,
        "nonce": str(nonce),
        "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "proof": {
            "key_kind": "hotkey",
            "signer": endorser.ss58_address,
            "signature": endorser.sign(payload).hex(),
        },
    }


async def test_claim_requires_existing_stem_use(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice = _kp("//Alice")
    response = await client.post(_URL, json=_claim_body(alice, name="Jupiter"))
    assert response.status_code == 400, response.text
    assert "no existing submission" in response.text


async def test_claim_and_endorsements_uphold(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice = _kp("//Alice")
    endorsers = [_kp("//Bob"), _kp("//Charlie"), _kp("//Dave")]
    await _seed_family(
        session_maker,
        hotkey=alice.ss58_address,
        coldkey=_kp("//Alice//stash").ss58_address,
        name="Jupiter-ditto-v1",
    )
    for index, endorser in enumerate(endorsers):
        await _seed_family(
            session_maker,
            hotkey=endorser.ss58_address,
            coldkey=_kp(f"//{['Bob', 'Charlie', 'Dave'][index]}//stash").ss58_address,
            name=f"family-{index}",
        )

    created = await client.post(_URL, json=_claim_body(alice, name="Jupiter-ditto-v10"))
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["name_stem"] == "jupiter"
    assert payload["status"] == "pending"
    assert payload["endorsement_threshold"] == ENDORSEMENT_THRESHOLD
    claim_id = UUID(payload["claim_id"])

    for endorser in endorsers:
        response = await client.post(
            f"{_URL}/{claim_id}/endorsements",
            json=_endorse_body(endorser, claim_id=claim_id, name_stem="jupiter"),
        )
        assert response.status_code == 201, response.text
    upheld = response.json()
    assert upheld["status"] == "upheld"
    assert upheld["endorsement_count"] == 3
    assert upheld["scope"] == "public-handle-only"

    listing = await client.get("/api/v1/public/name-claims")
    assert listing.status_code == 200, listing.text
    stems = [row["name_stem"] for row in listing.json()["claims"]]
    assert "jupiter" in stems

    from ditto.db.queries.name_claims import upload_name_is_reserved

    thief = _kp("//Ferdie")
    async with session_maker() as session, session.begin():
        blocked = await upload_name_is_reserved(
            session,
            netuid=118,
            agent_name="Jupiter-ditto-v11",
            miner_hotkey=thief.ss58_address,
            miner_coldkey=_kp("//Ferdie//stash").ss58_address,
        )
        allowed = await upload_name_is_reserved(
            session,
            netuid=118,
            agent_name="Jupiter-ditto-v11",
            miner_hotkey=alice.ss58_address,
            miner_coldkey=_kp("//Alice//stash").ss58_address,
        )
    assert blocked is not None and "reserved" in blocked
    assert allowed is None


async def test_endorser_must_be_entrenched(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice = _kp("//Alice")
    newbie = _kp("//Bob")
    await _seed_family(
        session_maker,
        hotkey=alice.ss58_address,
        coldkey=_kp("//Alice//stash").ss58_address,
        name="Jupiter",
    )
    await _seed_family(
        session_maker,
        hotkey=newbie.ss58_address,
        coldkey=_kp("//Bob//stash").ss58_address,
        name="newbie",
        age=timedelta(days=1),
    )
    created = await client.post(_URL, json=_claim_body(alice, name="Jupiter"))
    claim_id = UUID(created.json()["claim_id"])
    response = await client.post(
        f"{_URL}/{claim_id}/endorsements",
        json=_endorse_body(newbie, claim_id=claim_id, name_stem="jupiter"),
    )
    assert response.status_code == 400, response.text
    assert "entrenched" in response.text


async def test_cannot_endorse_own_claim(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice = _kp("//Alice")
    await _seed_family(
        session_maker,
        hotkey=alice.ss58_address,
        coldkey=_kp("//Alice//stash").ss58_address,
        name="Jupiter",
    )
    created = await client.post(_URL, json=_claim_body(alice, name="Jupiter"))
    claim_id = UUID(created.json()["claim_id"])
    response = await client.post(
        f"{_URL}/{claim_id}/endorsements",
        json=_endorse_body(alice, claim_id=claim_id, name_stem="jupiter"),
    )
    assert response.status_code == 400, response.text
    assert "own handle" in response.text


async def test_withdraw_releases_stem(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    alice = _kp("//Alice")
    await _seed_family(
        session_maker,
        hotkey=alice.ss58_address,
        coldkey=_kp("//Alice//stash").ss58_address,
        name="Jupiter",
    )
    created = await client.post(_URL, json=_claim_body(alice, name="Jupiter"))
    claim_id = UUID(created.json()["claim_id"])
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    payload = withdraw_message(
        netuid=118,
        claim_id=claim_id,
        name_stem="jupiter",
        claimant_hotkey=alice.ss58_address,
        nonce=nonce,
        issued_at=issued_at,
        key_kind="hotkey",
        signer=alice.ss58_address,
    )
    response = await client.post(
        f"{_URL}/{claim_id}/withdraw",
        json={
            "netuid": 118,
            "claimant_hotkey": alice.ss58_address,
            "nonce": str(nonce),
            "issued_at": issued_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "proof": {
                "key_kind": "hotkey",
                "signer": alice.ss58_address,
                "signature": alice.sign(payload).hex(),
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "withdrawn"
    assert normalize_name_stem("Jupiter-ditto-v10") == "jupiter"
