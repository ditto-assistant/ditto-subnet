"""Validators publish dueling season commitments on chain (``dds1``).

Real Postgres. The season twin of ``test_generator_round_commitments``:
Platform elects up to three fresh ``season_commitments`` validators per
committed dueling season in a stable per-season order, hands each the exact
``dds1:{season_index}:{commitment}`` payload, records their signed reports
(idempotently), replaces a validator that failed, keeps the first confirmed
block on the season row, settles publication when the season leaves
``committed``, and lets an operator re-elect under an audited confirmation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.dueling_seasons as dueling_mod
import ditto.api_server.endpoints.validator_generator_round as lane_mod
from ditto.api_models.dueling import REPUBLISH_DUELING_COMMITMENT_CONFIRMATION
from ditto.api_models.generator_round_validator import (
    DuelingSeasonCommitmentJobRequest,
    DuelingSeasonCommitmentJobResponse,
    DuelingSeasonCommitmentReportRequest,
    GeneratorRoundCommitmentJobRequest,
    season_commitment_payload,
)
from ditto.api_server.dueling_season_commitments import (
    elect_season_publishers,
    season_commitment_capable,
    season_publisher_order,
    settle_season_commitment_publications,
)
from ditto.api_server.generator_rounds import (
    COMMITMENT_CLAIM_TTL,
    ROUND_COMMITMENT_PUBLISHERS,
    ChainPosition,
    publisher_order,
)
from ditto.db.models import (
    DuelingSeason,
    DuelingSeasonCommitmentPublication,
    GeneratorOperatorAction,
)
from ditto.tests.api_server.endpoints.test_generator_rounds import (
    _ADMIN,
    _apply_settings,
    _install,
    round_capable_fleet,  # noqa: F401  (autouse fixture: a round-capable fleet)
)
from ditto.tests.db.queries.test_benchmark_rollout import _heartbeat
from ditto_screening_protocol.dueling import season_commitment

_JOB_URL = "/api/v1/validator/dueling-season-commitment-job"
_REPORT_URL = "/api/v1/validator/dueling-season-commitment-report"
_ROUND_JOB_URL = "/api/v1/validator/generator-round-commitment-job"
_SEASONS_URL = "/api/v1/admin/generator-competition/dueling/seasons"
_KEYS = [
    bittensor.Keypair.create_from_uri(f"//season-commitment-validator-{i}")
    for i in range(6)
]


def _heartbeat_row(
    key: bittensor.Keypair, *, season: bool = True, age: timedelta | None = None
) -> Any:
    now = datetime.now(UTC).replace(microsecond=0) - (age or timedelta())
    row = _heartbeat(key.ss58_address, now, versions=[7, 13], protocol_version=18)
    assert row.capabilities is not None
    row.capabilities["round_commitments"] = True
    if season:
        row.capabilities["season_commitments"] = True
    return row


async def _seed(maker: async_sessionmaker[AsyncSession], rows: list[Any]) -> None:
    async with maker() as session, session.begin():
        for row in rows:
            session.add(row)


def _signed_job(key: bittensor.Keypair, model: Any = DuelingSeasonCommitmentJobRequest):
    payload = model(
        validator_hotkey=key.ss58_address,
        nonce=uuid4(),
        requested_at=datetime.now(UTC),
    )
    payload.signature = key.sign(payload.signing_message()).hex()
    return payload.model_dump(mode="json")


async def _poll(
    client: httpx.AsyncClient, key: bittensor.Keypair, url: str = _JOB_URL
) -> httpx.Response:
    model = (
        GeneratorRoundCommitmentJobRequest
        if url == _ROUND_JOB_URL
        else DuelingSeasonCommitmentJobRequest
    )
    return await client.post(
        url,
        json=_signed_job(key, model),
        headers={"X-Validator-Hotkey": key.ss58_address},
    )


async def _report(
    client: httpx.AsyncClient,
    key: bittensor.Keypair,
    *,
    season: DuelingSeason,
    outcome: str = "confirmed",
    block_number: int = 4242,
    extrinsic_hash: str | None = None,
) -> httpx.Response:
    payload = DuelingSeasonCommitmentReportRequest(
        validator_hotkey=key.ss58_address,
        season_id=season.id,
        season_index=season.season_index,
        commitment=season.commitment,
        outcome=outcome,  # type: ignore[arg-type]
        block_number=block_number if outcome == "confirmed" else None,
        extrinsic_hash=extrinsic_hash if outcome == "confirmed" else None,
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


def _season(
    *,
    season_index: int = 12,
    bench_version: int = 16,
    shadow: bool = False,
    publication: str = "pending",
) -> DuelingSeason:
    secret = bytes([season_index % 251]) * 32
    return DuelingSeason(
        id=uuid4(),
        bench_version=bench_version,
        season_index=season_index,
        shadow=shadow,
        status="committed",
        commitment=season_commitment(season_index, secret),
        secret=secret,
        committed_at=datetime.now(UTC),
        rounds_per_season=1,
        detail={"committed_epoch_index": 40},
        commitment_publication=publication,
    )


async def _committed(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    **season: Any,
) -> DuelingSeason:
    _install(app, maker)
    await _apply_settings(client, onchain_commitment=True)
    row = _season(**season)
    await _seed(maker, [row])
    return row


async def _load(maker: async_sessionmaker[AsyncSession], season_id: UUID) -> Any:
    async with maker() as session:
        season = await session.get(DuelingSeason, season_id)
        publications = list(
            await session.scalars(
                select(DuelingSeasonCommitmentPublication).where(
                    DuelingSeasonCommitmentPublication.season_id == season_id
                )
            )
        )
        return season, publications


class TestSeasonCommitmentPublication:
    async def test_elected_validators_publish_dds1_and_platform_stops_at_three(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _permit_everyone(monkeypatch)
        season = await _committed(app, client, session_maker, shadow=True)
        await _seed(session_maker, [_heartbeat_row(k) for k in _KEYS[:5]])
        by_hotkey = {k.ss58_address: k for k in _KEYS}
        order = season_publisher_order(
            season.season_index, [k.ss58_address for k in _KEYS[:5]]
        )
        elected, waiting = order[:3], order[3:]

        for hotkey in order:
            resp = await _poll(client, by_hotkey[hotkey])
            if hotkey in elected:
                assert resp.status_code == 200, resp.text
                job = DuelingSeasonCommitmentJobResponse.model_validate(resp.json())
                assert job.season_id == season.id
                assert job.shadow is True and job.bench_version == 16
                assert job.payload == season_commitment_payload(
                    season.season_index, season.commitment
                )
                assert job.payload == (
                    f"dds1:{season.season_index}:{season.commitment}"
                )
            else:
                assert resp.status_code == 204, resp.text
        # A live claim is not handed out twice, and the round lane is untouched:
        # no round is committed, so it has nothing for anyone.
        assert (await _poll(client, by_hotkey[elected[0]])).status_code == 204
        assert (
            await _poll(client, by_hotkey[elected[0]], url=_ROUND_JOB_URL)
        ).status_code == 204

        # Two confirm (the later block first), one fails: the next validator in
        # the season's order takes its place.
        first = await _report(
            client,
            by_hotkey[elected[0]],
            season=season,
            block_number=5005,
            extrinsic_hash="0x" + "ab" * 32,
        )
        assert first.status_code == 200 and first.json()["accepted"] is True
        second = await _report(
            client,
            by_hotkey[elected[1]],
            season=season,
            block_number=5001,
            extrinsic_hash="0x" + "cd" * 32,
        )
        assert second.status_code == 200 and second.json()["confirmations"] == 2
        failed = await _report(
            client, by_hotkey[elected[2]], season=season, outcome="failed"
        )
        assert failed.status_code == 200 and failed.json()["confirmations"] == 2
        assert (await _poll(client, by_hotkey[elected[2]])).status_code == 204
        assert (await _poll(client, by_hotkey[waiting[1]])).status_code == 204
        assert (await _poll(client, by_hotkey[waiting[0]])).status_code == 200

        # Idempotent: a repeated confirmation and a failure after it change
        # nothing.
        again = await _report(client, by_hotkey[elected[0]], season=season)
        assert again.status_code == 200 and again.json()["duplicate"] is True
        late_fail = await _report(
            client, by_hotkey[elected[0]], season=season, outcome="failed"
        )
        assert late_fail.json()["duplicate"] is True

        row, _ = await _load(session_maker, season.id)
        assert row.commitment_publication == "pending"
        assert row.commitment_block == 5001
        assert row.commitment_extrinsic_hash == "0x" + "cd" * 32

        third = await _report(
            client, by_hotkey[waiting[0]], season=season, block_number=5010
        )
        assert third.json()["confirmations"] == ROUND_COMMITMENT_PUBLISHERS
        row, publications = await _load(session_maker, season.id)
        assert row.commitment_publication == "published"
        assert row.commitment_published_at is not None
        assert row.commitment_block == 5001
        assert sorted(p.status for p in publications) == [
            "confirmed",
            "confirmed",
            "confirmed",
            "failed",
        ]
        # Published: nobody else is asked.
        assert (await _poll(client, by_hotkey[waiting[1]])).status_code == 204

        view = (await client.get(f"{_SEASONS_URL}/{season.id}", headers=_ADMIN)).json()
        assert view["shadow"] is True
        assert view["commitment_publication"] == "published"
        assert view["commitment_block"] == 5001
        assert view["commitment_extrinsic_hash"] == "0x" + "cd" * 32
        assert [p["validator_hotkey"] for p in view["commitment_publications"]] == [
            h for h in order if h in {p.validator_hotkey for p in publications}
        ]
        assert view["commitment_publishers_elected"] == []

    async def test_only_season_capable_fresh_validators_are_elected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _permit_everyone(monkeypatch)
        await _committed(app, client, session_maker)
        await _seed(
            session_maker,
            [
                _heartbeat_row(_KEYS[0], season=False),
                _heartbeat_row(_KEYS[1], age=timedelta(minutes=30)),
            ],
        )
        for key in _KEYS[:2]:
            assert (await _poll(client, key)).status_code == 204

    async def test_a_report_must_name_the_claimed_commitment(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _permit_everyone(monkeypatch)
        season = await _committed(app, client, session_maker)
        await _seed(session_maker, [_heartbeat_row(_KEYS[0])])
        unclaimed = await _report(client, _KEYS[0], season=season)
        assert unclaimed.status_code == 409
        assert (await _poll(client, _KEYS[0])).status_code == 200
        other = _season(season_index=season.season_index + 1)
        other.id = season.id
        resp = await _report(client, _KEYS[0], season=other)
        assert resp.status_code == 409, resp.text

    async def test_republish_reelects_and_is_audited(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _permit_everyone(monkeypatch)
        season = await _committed(app, client, session_maker)
        await _seed(session_maker, [_heartbeat_row(k) for k in _KEYS[:3]])
        order = season_publisher_order(
            season.season_index, [k.ss58_address for k in _KEYS[:3]]
        )
        by_hotkey = {k.ss58_address: k for k in _KEYS}
        for hotkey in order:
            assert (await _poll(client, by_hotkey[hotkey])).status_code == 200
        await _report(client, by_hotkey[order[0]], season=season)
        await _report(client, by_hotkey[order[1]], season=season, outcome="failed")
        # The failed validator is not asked again until an operator re-elects.
        assert (await _poll(client, by_hotkey[order[1]])).status_code == 204

        url = f"{_SEASONS_URL}/{season.id}/republish-commitment"
        body = {
            "reason": "pylon outage during publication",
            "actor": "backroom:test",
            "confirmation": REPUBLISH_DUELING_COMMITMENT_CONFIRMATION,
        }
        wrong = await client.post(
            url, headers=_ADMIN, json={**body, "confirmation": "yes"}
        )
        assert wrong.status_code == 409
        resp = await client.post(url, headers=_ADMIN, json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["commitment_publication"] == "pending"
        assert [
            p["validator_hotkey"] for p in resp.json()["commitment_publications"]
        ] == [order[0]]
        assert (await _poll(client, by_hotkey[order[1]])).status_code == 200
        async with session_maker() as session:
            actions = list(
                await session.scalars(
                    select(GeneratorOperatorAction).where(
                        GeneratorOperatorAction.action
                        == "republish_dueling_season_commitment"
                    )
                )
            )
        assert len(actions) == 1
        assert (actions[0].detail or {})["cleared_claims"] == 2

        async with session_maker() as session, session.begin():
            row = await session.get(DuelingSeason, season.id)
            assert row is not None
            row.status = "void"
            row.void_reason = "test"
        late = await client.post(url, headers=_ADMIN, json=body)
        assert late.status_code == 409

    async def test_settlement_when_the_season_leaves_committed(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        confirmed = _season(season_index=20, bench_version=16)
        silent = _season(season_index=21, bench_version=17)
        waiting = _season(season_index=22, bench_version=18)
        await _seed(session_maker, [confirmed, silent, waiting])
        now = datetime.now(UTC)
        await _seed(
            session_maker,
            [
                DuelingSeasonCommitmentPublication(
                    season_id=confirmed.id,
                    validator_hotkey=_KEYS[0].ss58_address,
                    commitment=confirmed.commitment,
                    status="confirmed",
                    claimed_at=now,
                    claim_expires_at=now + COMMITMENT_CLAIM_TTL,
                    block_number=77,
                )
            ],
        )
        async with session_maker() as session, session.begin():
            for season_id in (confirmed.id, silent.id):
                row = await session.get(DuelingSeason, season_id)
                assert row is not None
                row.status = "void"
                row.void_reason = "test"
            notes = await settle_season_commitment_publications(session)
        assert len(notes) == 2
        # One confirmation is enough; a void season nobody published is
        # disabled; a still-committed season keeps collecting.
        assert (await _load(session_maker, confirmed.id))[0].commitment_publication == (
            "published"
        )
        assert (await _load(session_maker, silent.id))[0].commitment_publication == (
            "disabled"
        )
        assert (await _load(session_maker, waiting.id))[0].commitment_publication == (
            "pending"
        )


def test_commit_marks_publication_pending_only_with_onchain_commitment() -> None:
    position = ChainPosition(
        subnet_epoch_index=40,
        last_epoch_block=1,
        last_epoch_block_hash="0x" + "7c" * 32,
        tempo=360,
        block=2,
    )
    for onchain, expected in ((True, "pending"), (False, "disabled")):
        resolved = SimpleNamespace(
            settings=SimpleNamespace(rounds_per_season=1, onchain_commitment=onchain),
            revision=3,
        )
        season = dueling_mod._commit_season(
            bench_version=16,
            shadow=True,
            season_index=9,
            resolved=resolved,  # type: ignore[arg-type]
            position=position,
            now=datetime.now(UTC),
        )
        assert season.commitment_publication == expected


def test_season_order_is_stable_and_differs_from_the_round_order() -> None:
    hotkeys = [k.ss58_address for k in _KEYS]
    assert season_publisher_order(7, hotkeys) == season_publisher_order(
        7, reversed(hotkeys)
    )
    assert any(
        season_publisher_order(index, hotkeys) != publisher_order(index, hotkeys)
        for index in range(4)
    )


def test_expired_claims_hand_the_place_on() -> None:
    now = datetime.now(UTC)
    season = _season()
    hotkeys = [k.ss58_address for k in _KEYS[:4]]
    order = season_publisher_order(season.season_index, hotkeys)
    expired = DuelingSeasonCommitmentPublication(
        season_id=season.id,
        validator_hotkey=order[0],
        commitment=season.commitment,
        status="claimed",
        claimed_at=now - timedelta(minutes=11),
        claim_expires_at=now - timedelta(minutes=1),
    )
    election = elect_season_publishers(season, [expired], hotkeys, now=now)
    assert order[0] not in election.elected
    assert election.elected == tuple(order[1:4])


def test_capability_needs_a_fresh_heartbeat_advertising_seasons() -> None:
    now = datetime.now(UTC)
    assert season_commitment_capable(_heartbeat_row(_KEYS[0]), now=now)
    assert not season_commitment_capable(
        _heartbeat_row(_KEYS[0], season=False), now=now
    )
    assert not season_commitment_capable(
        _heartbeat_row(_KEYS[0], age=timedelta(minutes=10)), now=now
    )
