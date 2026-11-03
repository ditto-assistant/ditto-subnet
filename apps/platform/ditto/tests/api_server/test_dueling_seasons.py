"""Dueling-bench seasons (bench v16+): lifecycle and generator-scorer work.

Seasons commit ahead of their anchor, battery their candidates, open on a
round boundary with a frozen pin, serve slice work, and close and reveal on
schedule. Everything stays inert until dueling is enabled and a v16+ contract
is active or rolling out.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.dueling_seasons as dueling_mod
from ditto.api_models.source_disclosure import SourceDisclosure
from ditto.api_server.dueling_seasons import advance_seasons
from ditto.api_server.generator_competition import load_settings
from ditto.api_server.generator_rounds import ChainPosition
from ditto.db.models import (
    DuelingSeason,
    DuelingSeasonBattery,
    DuelingSlicePreparation,
    GeneratorRound,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
)
from ditto.db.queries.artifact_release_settings import ArtifactReleasePolicy
from ditto.tests.api_server.endpoints.test_generator_competition import (
    _SCORER,
    _archive,
    _install,
    _open_lane,
)
from ditto_screening_protocol.dueling import season_commitment, season_seed, slice_seed

_WORK = "/api/v1/generator-scorer/dueling-work"
_POLL = {"scorer_version": "challenge-scorer/9", "dueling_work_kinds": "slice,battery"}
_ROUND = 10
_EPOCHS = 4
_BLOCK = "0x" + "7c" * 32
_DEFAULT_KEY = bittensor.Keypair.create_from_uri("//Dave").ss58_address
_MINER_KEY = bittensor.Keypair.create_from_uri("//Eve").ss58_address
_WEAK_KEY = bittensor.Keypair.create_from_uri("//Ferdie").ss58_address


def _position(epoch: int) -> ChainPosition:
    return ChainPosition(
        subnet_epoch_index=epoch,
        last_epoch_block=1000 + epoch * 360,
        last_epoch_block_hash=_BLOCK,
        tempo=360,
        block=1000 + epoch * 360 + 5,
    )


async def _tick(maker: async_sessionmaker[AsyncSession], epoch: int) -> list[str]:
    async with maker() as session, session.begin():
        resolved = await load_settings(session)
        return await advance_seasons(
            session, resolved=resolved, position=_position(epoch), now=datetime.now(UTC)
        )


async def _generator(
    maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    owner: str,
    salt: str,
    public_default: bool = False,
) -> GeneratorSubmission:
    raw = _archive(salt)
    now = datetime.now(UTC)
    row = GeneratorSubmission(
        submission_id=uuid4(),
        miner_hotkey=hotkey,
        owner_key=owner,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        tree_sha256=hashlib.sha256(salt.encode()).hexdigest(),
        source_bytes=raw,
        size_bytes=len(raw),
        module="example.com/gen",
        go_version="1.23",
        file_count=2,
        status="active",
        attempts=0,
        next_run_at=now,
        submitted_at=now,
        updated_at=now,
        qualified_at=now,
        public_default=public_default,
    )
    async with maker() as session, session.begin():
        session.add(row)
    return row


async def _scored_round(
    maker: async_sessionmaker[AsyncSession],
    columns: list[tuple[GeneratorSubmission, float, float]],
) -> None:
    """A scored round ``_ROUND`` whose columns scored ``(score, unsolved)``."""
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            GeneratorRound(
                round_index=_ROUND,
                status="scored",
                epochs_per_round=_EPOCHS,
                first_epoch_index=_ROUND * _EPOCHS,
                secret=b"\x01" * 32,
                commitment="ab" * 32,
                committed_at=now,
                committed_epoch_index=_ROUND * _EPOCHS - 4,
                commitment_publication="disabled",
                seed="cd" * 32,
                columns=[
                    {
                        "column_id": f"c{i}",
                        "submission_id": str(sub.submission_id),
                        "owner_key": sub.owner_key,
                        "counted": True,
                        "public_default": False,
                    }
                    for i, (sub, _, _) in enumerate(columns)
                ],
                score={
                    "columns": [
                        {
                            "column_id": f"c{i}",
                            "pack": {
                                "score": score,
                                "discriminating": 10,
                                "unsolved_rate": unsolved,
                            },
                        }
                        for i, (_, score, unsolved) in enumerate(columns)
                    ]
                },
                scored_at=now,
                lease_attempts=0,
            )
        )
        await session.flush()
        for i, (sub, _, _) in enumerate(columns):
            session.add(
                GeneratorRoundCaseVerdict(
                    round_index=_ROUND,
                    column_id=f"c{i}",
                    case_id="case-0",
                    submission_id=sub.submission_id,
                    position=0,
                    fingerprint=f"fp-{i}",
                    status="discriminating",
                    panel_size=5,
                    solvers=3,
                    solver_owners=3,
                    mean_score=0.6,
                    champion_failed=False,
                    credit=1.0,
                )
            )


def _active(monkeypatch: pytest.MonkeyPatch, version: int) -> None:
    async def _version(*_args: Any, **_kwargs: Any) -> int:
        return version

    monkeypatch.setattr(dueling_mod, "active_bench_version", _version)


async def _seasons(maker: async_sessionmaker[AsyncSession]) -> list[DuelingSeason]:
    async with maker() as session:
        return list(
            await session.scalars(
                select(DuelingSeason).order_by(DuelingSeason.season_index)
            )
        )


def _slice_bytes(work: dict[str, Any], *, seed_override: str | None = None) -> bytes:
    doc = {
        "contract": "dittobench-dueling-slice/v1",
        "bench_version": work["bench_version"],
        "season_id": work["season_id"],
        "season_index": work["season_index"],
        "run_seed": work["run_seed"],
        "columns": [
            {
                "column_id": column["column_id"],
                "submission_id": column["submission_id"],
                "owner_key": column["owner_key"],
                "miner_hotkey": column["miner_hotkey"],
                "grading_bench_version": column["grading_bench_version"],
                "slice_seed": seed_override or column["slice_seed"],
                "public_default": column["public_default"],
                "substituted": False,
                "pack_sha256": "ee" * 32,
                "pack": {"cases": []},
            }
            for column in work["columns"]
        ],
    }
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()


def _slice_post(work: dict[str, Any], raw: bytes) -> dict[str, Any]:
    return {
        "lease_id": work["lease_id"],
        "preparation_id": work["preparation_id"],
        "scorer_version": "challenge-scorer/9",
        "columns": [{"column_id": c["column_id"], "ok": True} for c in work["columns"]],
        "slice_sha256": hashlib.sha256(raw).hexdigest(),
        "slice_base64": base64.b64encode(raw).decode(),
    }


class TestDuelingSeasons:
    async def test_inert_until_enabled_with_a_v16_contract_and_disclosure(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, epochs_per_round=_EPOCHS)
        _active(monkeypatch, 16)
        assert await _tick(session_maker, _ROUND * _EPOCHS + 1) == []
        assert await _seasons(session_maker) == []

        await _open_lane(client, epochs_per_round=_EPOCHS, dueling_enabled=True)
        _active(monkeypatch, 15)
        await _tick(session_maker, _ROUND * _EPOCHS + 1)
        assert await _seasons(session_maker) == []

        _active(monkeypatch, 16)

        async def _never(*_args: Any, **_kwargs: Any) -> ArtifactReleasePolicy:
            return ArtifactReleasePolicy(
                disclosure=SourceDisclosure.NEVER, embargo_hours=24
            )

        monkeypatch.setattr(dueling_mod, "artifact_release_policy", _never)
        await _tick(session_maker, _ROUND * _EPOCHS + 1)
        assert await _seasons(session_maker) == []
        resp = await client.get(_WORK, headers=_SCORER, params=_POLL)
        assert resp.status_code == 200 and resp.json() == {"work": None}

    async def test_commit_battery_open_slice_close_and_reveal(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _open_lane(
            client,
            epochs_per_round=_EPOCHS,
            dueling_enabled=True,
            privacy_window_rounds=6,
        )
        _active(monkeypatch, 16)
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        miner = await _generator(
            session_maker, hotkey=_MINER_KEY, owner="coldkey:a", salt="a"
        )
        weak = await _generator(
            session_maker, hotkey=_WEAK_KEY, owner="coldkey:b", salt="b"
        )
        await _scored_round(session_maker, [(miner, 0.4, 0.1), (weak, 0.5, 0.5)])

        # 1. Inside round 10 the season opening on round 11 commits, and its
        # eligible candidates (not the mostly unsolved one) queue a battery.
        notes = await _tick(session_maker, _ROUND * _EPOCHS + 1)
        assert any("season 11 v16: committed" in note for note in notes)
        [season] = await _seasons(session_maker)
        assert season.status == "committed" and season.shadow is False
        assert season.commitment == season_commitment(11, season.secret)
        async with session_maker() as session:
            batteries = {
                row.submission_id: row
                for row in await session.scalars(select(DuelingSeasonBattery))
            }
        assert set(batteries) == {miner.submission_id, default.submission_id}

        # 2. Only a scorer advertising the kind gets battery work.
        idle = await client.get(
            _WORK, headers=_SCORER, params={"scorer_version": "challenge-scorer/8"}
        )
        assert idle.json() == {"work": None}
        work = (await client.get(_WORK, headers=_SCORER, params=_POLL)).json()["work"]
        assert work["kind"] == "battery" and work["cases"] == 35
        assert len(work["seeds"]) == 32 and len(set(work["seeds"])) == 32
        assert {c["submission_id"] for c in work["candidates"]} == {
            str(miner.submission_id),
            str(default.submission_id),
        }
        posted = await client.post(
            "/api/v1/generator-scorer/dueling-battery",
            headers=_SCORER,
            json={
                "lease_id": work["lease_id"],
                "season_id": work["season_id"],
                "scorer_version": "challenge-scorer/9",
                "candidates": [
                    {
                        "submission_id": c["submission_id"],
                        "ok": True,
                        "grading_bench_version": 15,
                    }
                    for c in work["candidates"]
                ],
            },
        )
        assert posted.status_code == 200, posted.text
        assert len(posted.json()["passed"]) == 2

        # 3. At round 11's first epoch the season opens on that block.
        notes = await _tick(session_maker, 11 * _EPOCHS)
        assert any("season 11 v16: open (1 miner column(s))" in n for n in notes)
        seasons = await _seasons(session_maker)
        opened = next(s for s in seasons if s.season_index == 11)
        assert opened.status == "open" and opened.closes_at_round == 12
        assert opened.anchor_block == 1000 + 11 * _EPOCHS * 360
        assert opened.season_seed == season_seed(11, opened.secret, _BLOCK)
        assert [c["submission_id"] for c in opened.columns or ()] == [
            str(miner.submission_id)
        ] + [str(default.submission_id)] * 4
        assert [c["owner_key"] for c in opened.columns or ()] == ["coldkey:a"] + [
            ""
        ] * 4
        assert any(s.season_index == 12 and s.status == "committed" for s in seasons)

        # 4. A slice preparation leases with every column's slice seed; a
        # slice for another seed is refused and the lease stays current.
        prep_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                DuelingSlicePreparation(
                    id=prep_id,
                    season_id=opened.id,
                    run_seed=7,
                    status="pending",
                    attempts=0,
                )
            )
        work = (await client.get(_WORK, headers=_SCORER, params=_POLL)).json()["work"]
        assert work["kind"] == "slice" and work["preparation_id"] == str(prep_id)
        assert work["cases_per_column"] == 35
        assert opened.season_seed is not None
        assert [c["slice_seed"] for c in work["columns"]] == [
            slice_seed(opened.season_seed, 7, f"c{i}") for i in range(5)
        ]
        assert work["fallback"]["submission_id"] == str(default.submission_id)
        assert "secret" not in json.dumps(work) and opened.season_seed not in (
            json.dumps(work)
        )
        bad = _slice_bytes(work, seed_override="00" * 32)
        refused = await client.post(
            "/api/v1/generator-scorer/dueling-slice",
            headers=_SCORER,
            json=_slice_post(work, bad),
        )
        assert refused.status_code == 409 and "slice seed" in refused.text
        good = _slice_bytes(work)
        ready = await client.post(
            "/api/v1/generator-scorer/dueling-slice",
            headers=_SCORER,
            json=_slice_post(work, good),
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["status"] == "ready"
        async with session_maker() as session:
            row = await session.get(DuelingSlicePreparation, prep_id)
            assert row is not None and row.slice_bytes == good
            assert row.slice_sha256 == hashlib.sha256(good).hexdigest()
        # Pinned bytes win: the lease is spent, a repost is refused.
        again = await client.post(
            "/api/v1/generator-scorer/dueling-slice",
            headers=_SCORER,
            json=_slice_post(work, good),
        )
        assert again.status_code == 409

        # 5. The season closes at its boundary and reveals after the window.
        await _tick(session_maker, 12 * _EPOCHS)
        closed = next(s for s in await _seasons(session_maker) if s.season_index == 11)
        assert closed.status == "closed" and closed.reveal_at_epoch == 48 + 6
        await _tick(session_maker, 12 * _EPOCHS + 6)
        revealed = next(
            s for s in await _seasons(session_maker) if s.season_index == 11
        )
        assert revealed.status == "revealed" and revealed.revealed_at is not None

    async def test_a_season_voided_before_its_boundary_is_recommitted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, epochs_per_round=_EPOCHS, dueling_enabled=True)
        _active(monkeypatch, 16)
        await _tick(session_maker, _ROUND * _EPOCHS + 1)
        [first] = await _seasons(session_maker)
        assert (first.season_index, first.status) == (11, "committed")

        async def _never(*_args: Any, **_kwargs: Any) -> ArtifactReleasePolicy:
            return ArtifactReleasePolicy(
                disclosure=SourceDisclosure.NEVER, embargo_hours=24
            )

        with monkeypatch.context() as patch:
            patch.setattr(dueling_mod, "artifact_release_policy", _never)
            await _tick(session_maker, _ROUND * _EPOCHS + 2)
        [voided] = await _seasons(session_maker)
        assert voided.status == "void"
        await _tick(session_maker, _ROUND * _EPOCHS + 3)
        [again] = await _seasons(session_maker)
        assert (again.season_index, again.status) == (11, "committed")
        assert again.id != first.id and again.commitment != first.commitment

    async def test_scorer_fault_releases_a_slice_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, epochs_per_round=_EPOCHS, dueling_enabled=True)
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        season_id = uuid4()
        secret = b"\x02" * 32
        seed = season_seed(5, secret, _BLOCK)
        pin = {
            "column_id": "c0",
            "submission_id": str(default.submission_id),
            "miner_hotkey": default.miner_hotkey,
            "owner_key": "",
            "tree_sha256": default.tree_sha256,
            "source_sha256": default.source_sha256,
            "grading_bench_version": 15,
            "public_default": True,
        }
        async with session_maker() as session, session.begin():
            session.add(
                DuelingSeason(
                    id=season_id,
                    bench_version=16,
                    season_index=5,
                    shadow=True,
                    status="open",
                    commitment=season_commitment(5, secret),
                    secret=secret,
                    committed_at=datetime.now(UTC),
                    rounds_per_season=1,
                    anchor_block=1,
                    anchor_block_hash=_BLOCK,
                    season_seed=seed,
                    columns=[{**pin, "column_id": f"c{i}"} for i in range(5)],
                    detail={
                        "fallback": {
                            "submission_id": str(default.submission_id),
                            "source_sha256": default.source_sha256,
                        }
                    },
                )
            )
            await session.flush()
            session.add(
                DuelingSlicePreparation(
                    id=uuid4(),
                    season_id=season_id,
                    run_seed=9,
                    status="pending",
                    attempts=0,
                )
            )
        work = (await client.get(_WORK, headers=_SCORER, params=_POLL)).json()["work"]
        assert work["kind"] == "slice"
        ack = await client.post(
            "/api/v1/generator-scorer/fault",
            headers=_SCORER,
            json={
                "work": "dueling",
                "lease_id": work["lease_id"],
                "error_kind": "sandbox",
                "detail": "docker daemon restarted",
                "scorer_version": "challenge-scorer/9",
            },
        )
        assert ack.status_code == 200 and ack.json()["action"] == "released"
        again = (await client.get(_WORK, headers=_SCORER, params=_POLL)).json()["work"]
        assert again["preparation_id"] == work["preparation_id"]
        assert UUID(again["lease_id"]) != UUID(work["lease_id"])
        # An unsupported request (here a digest mismatch) fails it for good.
        refused = await client.post(
            "/api/v1/generator-scorer/fault",
            headers=_SCORER,
            json={
                "work": "dueling",
                "lease_id": again["lease_id"],
                "season_id": again["season_id"],
                "preparation_id": again["preparation_id"],
                "error_kind": "unsupported_request",
                "detail": "tree digest mismatch",
                "scorer_version": "challenge-scorer/9",
            },
        )
        assert refused.status_code == 200 and refused.json()["action"] == "voided"
        assert (await client.get(_WORK, headers=_SCORER, params=_POLL)).json() == {
            "work": None
        }
        async with session_maker() as session:
            prep = await session.get(
                DuelingSlicePreparation, UUID(work["preparation_id"])
            )
            assert prep is not None and prep.status == "failed"
            assert "tree digest mismatch" in (prep.error or "")
