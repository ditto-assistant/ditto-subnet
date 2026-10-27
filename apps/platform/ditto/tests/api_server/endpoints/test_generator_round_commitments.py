"""Validators, not Platform, publish generator-round seed commitments.

Real Postgres: Platform elects up to three fresh ``round_commitments``
validators per committed round in a stable per-round order, hands each the
exact ``dgr1:{round}:{commitment}`` payload, records their signed reports,
replaces a validator that failed, and stops asking once three confirmed.
A round that opens settles its publication without ever waiting on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.endpoints.validator_generator_round as lane_mod
from ditto.api_models.generator_round_validator import (
    GeneratorRoundCommitmentJobRequest,
    GeneratorRoundCommitmentJobResponse,
    GeneratorRoundCommitmentReportRequest,
    round_commitment_payload,
)
from ditto.api_server.generator_rounds import (
    COMMITMENT_CLAIM_TTL,
    COMMITMENT_ESCALATION_INTERVAL,
    ROUND_COMMITMENT_PUBLISHERS,
    commitment_capable,
    elect_commitment_publishers,
    get_round,
    publisher_order,
    settle_commitment_publications,
)
from ditto.db.models import GeneratorRoundCommitmentPublication
from ditto.tests.api_server.endpoints.test_generator_rounds import (
    _ADMIN,
    _ROUNDS_URL,
    _advance,
    _apply_settings,
    _install,
    round_capable_fleet,  # noqa: F401  (autouse fixture: a round-capable fleet)
)
from ditto.tests.db.queries.test_benchmark_rollout import _heartbeat

_JOB_URL = "/api/v1/validator/generator-round-commitment-job"
_REPORT_URL = "/api/v1/validator/generator-round-commitment-report"
_KEYS = [
    bittensor.Keypair.create_from_uri(f"//commitment-validator-{i}") for i in range(6)
]


def _heartbeat_row(key: bittensor.Keypair, *, capable: bool = True, age=None):
    now = datetime.now(UTC).replace(microsecond=0) - (age or timedelta())
    row = _heartbeat(key.ss58_address, now, versions=[7, 13], protocol_version=18)
    assert row.capabilities is not None
    if capable:
        row.capabilities["round_commitments"] = True
    return row


async def _seed_heartbeats(
    maker: async_sessionmaker[AsyncSession], rows: list[Any]
) -> None:
    async with maker() as session, session.begin():
        for row in rows:
            session.add(row)


def _job_body(key: bittensor.Keypair) -> dict[str, Any]:
    payload = GeneratorRoundCommitmentJobRequest(
        validator_hotkey=key.ss58_address,
        nonce=uuid4(),
        requested_at=datetime.now(UTC),
    )
    payload.signature = key.sign(payload.signing_message()).hex()
    return payload.model_dump(mode="json")


async def _poll(client: httpx.AsyncClient, key: bittensor.Keypair) -> httpx.Response:
    return await client.post(
        _JOB_URL,
        json=_job_body(key),
        headers={"X-Validator-Hotkey": key.ss58_address},
    )


async def _report(
    client: httpx.AsyncClient,
    key: bittensor.Keypair,
    *,
    round_index: int,
    commitment: str,
    outcome: str = "confirmed",
    block_number: int | None = 4242,
) -> httpx.Response:
    payload = GeneratorRoundCommitmentReportRequest(
        validator_hotkey=key.ss58_address,
        round_index=round_index,
        commitment=commitment,
        outcome=outcome,  # type: ignore[arg-type]
        block_number=block_number if outcome == "confirmed" else None,
        detail=None if outcome == "confirmed" else "pylon rejected the extrinsic",
        nonce=uuid4(),
        requested_at=datetime.now(UTC),
    )
    payload.signature = key.sign(payload.signing_message()).hex()
    return await client.post(_REPORT_URL, json=payload.model_dump(mode="json"))


def _permit_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _permitted(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(lane_mod.v, "_assert_validator_permitted", _permitted)


async def _committed_round(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> tuple[int, str]:
    _install(app, maker)
    await _apply_settings(client, onchain_commitment=True)
    await _advance(maker, 5)  # epochs_per_round=4: commits round 2
    async with maker() as session:
        row = await get_round(session, 2)
        assert row is not None and row.status == "committed"
        assert row.commitment_publication == "pending"
        return row.round_index, row.commitment


class TestValidatorCommitmentPublication:
    async def test_elected_validators_publish_and_platform_stops_at_three(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _permit_everyone(monkeypatch)
        round_index, commitment = await _committed_round(app, client, session_maker)
        await _seed_heartbeats(session_maker, [_heartbeat_row(k) for k in _KEYS[:5]])
        by_hotkey = {k.ss58_address: k for k in _KEYS}
        order = publisher_order(round_index, [k.ss58_address for k in _KEYS[:5]])
        elected, waiting = order[:3], order[3:]

        # Only the first three in the round's publisher order are asked, and
        # each gets the exact on-chain payload.
        for hotkey in order:
            resp = await _poll(client, by_hotkey[hotkey])
            if hotkey in elected:
                assert resp.status_code == 200, resp.text
                job = GeneratorRoundCommitmentJobResponse.model_validate(resp.json())
                assert job.round_index == round_index
                assert job.commitment == commitment
                assert job.payload == round_commitment_payload(round_index, commitment)
                assert job.payload == f"dgr1:{round_index}:{commitment}"
            else:
                assert resp.status_code == 204, resp.text
        # A live claim is not handed out twice.
        assert (await _poll(client, by_hotkey[elected[0]])).status_code == 204

        # Two confirm, one fails: the next validator in order takes its place.
        for hotkey in elected[:2]:
            resp = await _report(
                client,
                by_hotkey[hotkey],
                round_index=round_index,
                commitment=commitment,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["accepted"] is True
        failed = await _report(
            client,
            by_hotkey[elected[2]],
            round_index=round_index,
            commitment=commitment,
            outcome="failed",
        )
        assert failed.status_code == 200 and failed.json()["confirmations"] == 2
        assert (await _poll(client, by_hotkey[elected[2]])).status_code == 204
        assert (await _poll(client, by_hotkey[waiting[1]])).status_code == 204
        replacement = await _poll(client, by_hotkey[waiting[0]])
        assert replacement.status_code == 200, replacement.text

        # Idempotent reporting: a repeat changes nothing.
        again = await _report(
            client,
            by_hotkey[elected[0]],
            round_index=round_index,
            commitment=commitment,
        )
        assert again.status_code == 200
        assert again.json() | {"confirmations": 0} == {
            "round_index": round_index,
            "accepted": False,
            "duplicate": True,
            "confirmations": 0,
            "target": ROUND_COMMITMENT_PUBLISHERS,
        }
        # A validator that claimed nothing, or names another commitment, is refused.
        stranger = await _report(
            client,
            by_hotkey[waiting[1]],
            round_index=round_index,
            commitment=commitment,
        )
        assert stranger.status_code == 409
        wrong = await _report(
            client,
            by_hotkey[waiting[0]],
            round_index=round_index,
            commitment="ee" * 32,
        )
        assert wrong.status_code == 409

        third = await _report(
            client,
            by_hotkey[waiting[0]],
            round_index=round_index,
            commitment=commitment,
            block_number=4250,
        )
        assert third.json()["confirmations"] == 3
        async with session_maker() as session:
            row = await get_round(session, round_index)
            assert row is not None and row.commitment_publication == "published"
            assert row.commitment_published_at is not None
        # Target reached: nobody else is asked.
        for key in _KEYS[:5]:
            assert (await _poll(client, key)).status_code == 204

        detail = (
            await client.get(f"{_ROUNDS_URL}/{round_index}", headers=_ADMIN)
        ).json()
        assert detail["summary"]["commitment_confirmations"] == 3
        assert detail["commitment_publishers_target"] == 3
        assert detail["commitment_elected"] == []
        statuses = {
            p["validator_hotkey"]: (p["status"], p["block_number"])
            for p in detail["commitment_publications"]
        }
        assert statuses == {
            elected[0]: ("confirmed", 4242),
            elected[1]: ("confirmed", 4242),
            elected[2]: ("failed", None),
            waiting[0]: ("confirmed", 4250),
        }
        public = (await client.get("/api/v1/generator/rounds")).json()
        summary = next(r for r in public["rounds"] if r["round_index"] == round_index)
        assert summary["commitment_confirmations"] == 3

    async def test_stale_and_incapable_validators_are_never_elected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _permit_everyone(monkeypatch)
        round_index, _ = await _committed_round(app, client, session_maker)
        stale, incapable, fresh = _KEYS[0], _KEYS[1], _KEYS[2]
        await _seed_heartbeats(
            session_maker,
            [
                _heartbeat_row(stale, age=timedelta(minutes=10)),
                _heartbeat_row(incapable, capable=False),
                _heartbeat_row(fresh),
            ],
        )
        assert (await _poll(client, stale)).status_code == 204
        assert (await _poll(client, incapable)).status_code == 204
        assert (await _poll(client, fresh)).status_code == 200

        detail = (
            await client.get(f"{_ROUNDS_URL}/{round_index}", headers=_ADMIN)
        ).json()
        assert [p["status"] for p in detail["commitment_publications"]] == ["claimed"]
        assert detail["commitment_elected"] == [fresh.ss58_address]

    async def test_onchain_commitment_off_asks_nobody(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _permit_everyone(monkeypatch)
        _install(app, session_maker)
        await _apply_settings(client, onchain_commitment=False)
        await _advance(session_maker, 5)
        await _seed_heartbeats(session_maker, [_heartbeat_row(_KEYS[0])])
        assert (await _poll(client, _KEYS[0])).status_code == 204

    async def test_round_open_settles_publication_without_waiting(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        round_index, commitment = await _committed_round(app, client, session_maker)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                GeneratorRoundCommitmentPublication(
                    round_index=round_index,
                    validator_hotkey=_KEYS[0].ss58_address,
                    commitment=commitment,
                    status="confirmed",
                    claimed_at=now,
                    claim_expires_at=now + COMMITMENT_CLAIM_TTL,
                    reported_at=now,
                    block_number=99,
                )
            )
            row = await get_round(session, round_index, for_update=True)
            assert row is not None
            row.status, row.seed = "generating", "ab" * 32
        async with session_maker() as session, session.begin():
            notes = await settle_commitment_publications(session)
        assert notes == [
            f"round {round_index}: commitment publication published (1 confirmed)"
        ]

        # The next committed round opens with nobody confirmed: failed, and
        # nothing ever held the round back.
        await _advance(session_maker, 9)  # commits round 3
        async with session_maker() as session, session.begin():
            nxt = await get_round(session, 3, for_update=True)
            assert nxt is not None and nxt.commitment_publication == "pending"
            nxt.status, nxt.seed = "generating", "cd" * 32
        async with session_maker() as session, session.begin():
            await settle_commitment_publications(session)
        async with session_maker() as session:
            nxt = await get_round(session, 3)
            assert nxt is not None and nxt.commitment_publication == "failed"
            assert "no validator confirmed" in (nxt.commitment_error or "")


def _pub(hotkey: str, status: str, *, claimed: datetime, ttl=COMMITMENT_CLAIM_TTL):
    return SimpleNamespace(
        validator_hotkey=hotkey,
        status=status,
        claimed_at=claimed,
        claim_expires_at=claimed + ttl,
    )


def test_election_is_a_stable_per_round_order() -> None:
    hotkeys = [k.ss58_address for k in _KEYS]
    now = datetime.now(UTC)
    row = SimpleNamespace(round_index=7)
    first = elect_commitment_publishers(row, [], hotkeys, now=now)  # type: ignore[arg-type]
    shuffled = elect_commitment_publishers(row, [], hotkeys[::-1], now=now)  # type: ignore[arg-type]
    assert first.elected == shuffled.elected == tuple(publisher_order(7, hotkeys)[:3])
    other = elect_commitment_publishers(
        SimpleNamespace(round_index=8),  # type: ignore[arg-type]
        [],
        hotkeys,
        now=now,
    )
    assert other.elected == tuple(publisher_order(8, hotkeys)[:3])


def test_expired_claims_and_silent_validators_hand_the_place_on() -> None:
    hotkeys = [k.ss58_address for k in _KEYS]
    order = publisher_order(7, hotkeys)
    row = SimpleNamespace(round_index=7)
    start = datetime.now(UTC)
    claims = [_pub(h, "claimed", claimed=start) for h in order[:3]]
    # Live claims hold their places.
    live = elect_commitment_publishers(
        row,  # type: ignore[arg-type]
        claims,  # type: ignore[arg-type]
        hotkeys,
        now=start + timedelta(minutes=1),
    )
    assert live.elected == tuple(order[:3])
    # A claim that expired without a report frees its place, and every
    # escalation interval without reaching the target widens by one more.
    later = start + COMMITMENT_ESCALATION_INTERVAL + timedelta(seconds=1)
    expired = elect_commitment_publishers(
        row,  # type: ignore[arg-type]
        claims,  # type: ignore[arg-type]
        hotkeys,
        now=later,
    )
    assert expired.elected == tuple(order[3:6])
    # Confirmed publications count toward the target; at the target nobody is.
    done = [_pub(h, "confirmed", claimed=start) for h in order[:3]]
    assert (
        elect_commitment_publishers(row, done, hotkeys, now=later).elected  # type: ignore[arg-type]
        == ()
    )


def test_commitment_capability_needs_a_fresh_heartbeat() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    fresh = _heartbeat_row(_KEYS[0])
    assert commitment_capable(fresh, now=now)
    assert not commitment_capable(_heartbeat_row(_KEYS[0], capable=False), now=now)
    assert not commitment_capable(fresh, now=now + timedelta(minutes=6))
