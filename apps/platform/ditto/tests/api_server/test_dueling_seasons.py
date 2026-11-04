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
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.dueling_seasons as dueling_mod
import ditto.api_server.generator_source_releases as releases_mod
from ditto.api_models.generator import generator_settings_confirmation
from ditto.api_models.source_disclosure import SourceDisclosure
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.confirmation_seed_anchor import (
    list_reign_seed_anchors,
    resolve_reign_seed_anchor,
)
from ditto.api_server.dueling_seasons import (
    DuelingEvidenceRejected,
    DuelingSliceUnavailable,
    advance_seasons,
    dueling_score_evidence,
    resolve_dueling_slice,
)
from ditto.api_server.generator_competition import load_settings
from ditto.api_server.generator_rounds import ChainPosition
from ditto.api_server.generator_source_releases import (
    prune_released_slice_bytes,
    sync_generator_source_releases,
)
from ditto.db.models import (
    DuelingSeason,
    DuelingSeasonBattery,
    DuelingSlicePreparation,
    GeneratorOperatorAction,
    GeneratorRound,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
    Score,
    ValidatorTicket,
)
from ditto.db.queries.artifact_release_settings import ArtifactReleasePolicy
from ditto.db.queries.confirmation_seed_anchors import NIL_SEASON_KEY
from ditto.db.queries.dueling_season_window import (
    dueling_season_window,
    season_scope_predicate,
)
from ditto.db.queries.score_retests import (
    DUELING_ROTATION_RETEST_BASIS,
    activate_next_score_retest,
    queue_dueling_rotation_retests,
)
from ditto.tests.api_server.endpoints.test_generator_competition import (
    _ADMIN,
    _SCORER,
    _archive,
    _install,
    _open_lane,
    _settings,
)
from ditto.tests.api_server.endpoints.test_generator_rounds import _seed_agent
from ditto_screening_protocol.dueling import season_commitment, season_seed, slice_seed

_WORK = "/api/v1/generator-scorer/dueling-work"
_SETTINGS_URL = "/api/v1/admin/generator-competition/settings"
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


async def _open_season(
    maker: async_sessionmaker[AsyncSession],
    default: GeneratorSubmission,
    *,
    season_index: int = 5,
) -> UUID:
    """An open v16 season whose five columns are the public default."""
    season_id = uuid4()
    secret = b"\x02" * 32
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
    async with maker() as session, session.begin():
        session.add(
            DuelingSeason(
                id=season_id,
                bench_version=16,
                season_index=season_index,
                shadow=False,
                status="open",
                commitment=season_commitment(season_index, secret),
                secret=secret,
                committed_at=datetime.now(UTC),
                rounds_per_season=4,
                anchor_block=1,
                anchor_block_hash=_BLOCK,
                season_seed=season_seed(season_index, secret, _BLOCK),
                columns=[{**pin, "column_id": f"c{i}"} for i in range(5)],
                closes_at_round=season_index + 4,
                detail={
                    "fallback": {
                        "submission_id": str(default.submission_id),
                        "source_sha256": default.source_sha256,
                    }
                },
            )
        )
    return season_id


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
        season_id = await _open_season(session_maker, default)
        async with session_maker() as session, session.begin():
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


async def _pass_batteries(client: httpx.AsyncClient) -> int:
    """Pass every queued battery candidate; returns how many batteries ran."""
    ran = 0
    while True:
        work = (await client.get(_WORK, headers=_SCORER, params=_POLL)).json()["work"]
        if work is None or work["kind"] != "battery":
            return ran
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
        ran += 1


class TestIdleSeasonExtension:
    async def test_an_idle_boundary_extends_the_season_and_a_change_rotates(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ditto.api_server.season_duel import (
            season_duel_entrant_excluded,
            season_duel_excluded_entrants,
        )

        _install(app, session_maker)
        await _open_lane(
            client,
            epochs_per_round=_EPOCHS,
            dueling_enabled=True,
            privacy_window_rounds=6,
        )
        _active(monkeypatch, 16)
        incumbent = await _seed_agent(session_maker, 1)
        runner_up = await _seed_agent(session_maker, 3)
        queued: list[UUID] = []
        board = [incumbent, runner_up]

        async def _cohort(*_args: Any) -> tuple[list[UUID], str]:
            return list(board), "continual_cohort"

        async def _queue(*_args: Any, season_id: UUID, **_kwargs: Any) -> dict:
            queued.append(season_id)
            return {"queued": 1}

        monkeypatch.setattr(dueling_mod, "_rotation_cohort", _cohort)
        import ditto.db.queries.score_retests as retests_mod

        monkeypatch.setattr(retests_mod, "queue_dueling_rotation_retests", _queue)
        await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        miner = await _generator(
            session_maker, hotkey=_MINER_KEY, owner="coldkey:a", salt="a"
        )
        await _scored_round(session_maker, [(miner, 0.4, 0.1)])

        # Season 11 commits, batteries, and opens with one rotation.
        await _tick(session_maker, _ROUND * _EPOCHS + 1)
        assert await _pass_batteries(client) == 1
        await _tick(session_maker, 11 * _EPOCHS)
        [opened] = [s for s in await _seasons(session_maker) if s.season_index == 11]
        assert opened.status == "open" and opened.closes_at_round == 12
        assert queued == [opened.id]
        async with session_maker() as session:
            window = await dueling_season_window(session, 16)
        assert window is not None and window.current == opened.id

        # Season 12's batteries pass on the same generators. Nothing else
        # changed, so its boundary extends season 11: no rotation, no new
        # season, the score window does not move and nothing reveals.
        assert await _pass_batteries(client) == 1
        notes = await _tick(session_maker, 12 * _EPOCHS)
        assert any("season 11 v16: extended to round 13 (idle" in n for n in notes)
        seasons = {s.season_index: s for s in await _seasons(session_maker)}
        assert seasons[11].status == "open" and seasons[11].closes_at_round == 13
        assert seasons[12].status == "void" and seasons[12].opened_at is None
        assert seasons[13].status == "committed"
        extensions = (seasons[11].detail or {})["idle_extensions"]
        assert extensions["count"] == 1
        assert extensions["recent"][0]["at_round"] == 12
        assert queued == [opened.id]
        async with session_maker() as session:
            window = await dueling_season_window(session, 16)
            assert window is not None
            assert (window.current, window.previous) == (opened.id, None)
            # Present since before the extension: still owes the duel seeds.
            assert not await season_duel_entrant_excluded(
                session, bench_version=16, member_agent_id=incumbent
            )
        # Idle again at round 13 even though the board reordered the same
        # cohort: membership, not rank order, decides. Extended once more.
        # Had season 11 closed at round 12 it would reveal at epoch 48 + 6;
        # extended, it stays private.
        board.reverse()
        assert await _pass_batteries(client) == 1
        notes = await _tick(session_maker, 13 * _EPOCHS)
        assert any("season 11 v16: extended to round 14" in n for n in notes)
        await _tick(session_maker, 12 * _EPOCHS + 6)
        [still] = [s for s in await _seasons(session_maker) if s.season_index == 11]
        assert still.status == "open" and still.revealed_at is None
        assert still.reveal_at_epoch is None
        assert (still.detail or {})["idle_extensions"]["count"] == 2
        assert queued == [opened.id]
        view = (
            await client.get(
                f"/api/v1/admin/generator-competition/dueling/seasons/{opened.id}",
                headers=_ADMIN,
            )
        ).json()
        assert view["detail"]["idle_extensions"]["count"] == 2

        # A new eligible upload makes the next boundary rotate: season 11
        # closes, its privacy window counts from the extended close, and
        # season 14 opens with a rotation.
        entrant = await _seed_agent(session_maker, 2)
        async with session_maker() as session:
            # Uploaded after the first extension: no duel seeds this season,
            # and no catch-up priority either (the batch form the catch-up
            # planner reads).
            assert await season_duel_entrant_excluded(
                session, bench_version=16, member_agent_id=entrant
            )
            assert await season_duel_excluded_entrants(
                session, bench_version=16, agent_ids=[incumbent, entrant]
            ) == frozenset({entrant})
        assert await _pass_batteries(client) == 1
        await _tick(session_maker, 14 * _EPOCHS)
        seasons = {s.season_index: s for s in await _seasons(session_maker)}
        assert seasons[11].status == "closed"
        assert seasons[11].reveal_at_epoch == 14 * _EPOCHS + 6
        assert seasons[14].status == "open"
        assert queued == [opened.id, seasons[14].id]
        async with session_maker() as session:
            window = await dueling_season_window(session, 16)
            assert window is not None
            assert (window.current, window.previous) == (seasons[14].id, opened.id)
            # The new season was never extended: nobody is excluded.
            assert not await season_duel_entrant_excluded(
                session, bench_version=16, member_agent_id=entrant
            )
        await _tick(session_maker, 14 * _EPOCHS + 5)
        assert (
            next(
                s for s in await _seasons(session_maker) if s.season_index == 11
            ).status
            == "closed"
        )
        await _tick(session_maker, 14 * _EPOCHS + 6)
        assert (
            next(
                s for s in await _seasons(session_maker) if s.season_index == 11
            ).status
            == "revealed"
        )

    async def test_skip_off_rotates_an_idle_boundary(
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
            skip_unchanged_rounds=False,
        )
        _active(monkeypatch, 16)

        async def _cohort(*_args: Any) -> tuple[list[UUID], str]:
            return [], "continual_cohort"

        monkeypatch.setattr(dueling_mod, "_rotation_cohort", _cohort)
        await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        miner = await _generator(
            session_maker, hotkey=_MINER_KEY, owner="coldkey:a", salt="a"
        )
        await _scored_round(session_maker, [(miner, 0.4, 0.1)])
        await _tick(session_maker, _ROUND * _EPOCHS + 1)
        await _pass_batteries(client)
        await _tick(session_maker, 11 * _EPOCHS)
        await _pass_batteries(client)
        await _tick(session_maker, 12 * _EPOCHS)
        seasons = {s.season_index: s for s in await _seasons(session_maker)}
        assert seasons[11].status == "closed" and seasons[12].status == "open"


def test_first_extension_time_is_always_timezone_aware() -> None:
    """Compared with Agent.created_at (TIMESTAMPTZ): never naive."""
    from types import SimpleNamespace
    from typing import cast

    from ditto.api_server.dueling_seasons import season_first_extended_at

    def season(first: object) -> Any:
        return SimpleNamespace(detail={"idle_extensions": {"first_extended_at": first}})

    aware = season_first_extended_at(season("2026-11-04T06:00:00+00:00"))
    naive = season_first_extended_at(season("2026-11-04T06:00:00"))
    assert aware == naive == datetime(2026, 11, 4, 6, tzinfo=UTC)
    assert aware is not None and aware.tzinfo is not None
    assert naive is not None and naive.tzinfo is not None
    assert season_first_extended_at(None) is None
    assert season_first_extended_at(cast(Any, SimpleNamespace(detail=None))) is None
    assert season_first_extended_at(season(None)) is None


class TestDuelingAdmin:
    async def test_reads_hide_the_secret_and_rotation_is_audited(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, epochs_per_round=_EPOCHS, dueling_enabled=True)
        _active(monkeypatch, 16)
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        season_id = await _open_season(session_maker, default, season_index=20)
        agent_id = await _seed_agent(session_maker, 1)
        async with session_maker() as session, session.begin():
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey=_MINER_KEY,
                    bench_version=16,
                    run_id="run-1",
                    seed=3,
                    composite=0.5,
                    tool_mean=0.5,
                    memory_mean=0.5,
                    median_ms=10,
                    n=350,
                    generated_at=datetime.now(UTC),
                    dueling_season_id=season_id,
                    dueling={
                        "columns": [
                            {
                                "column_id": f"c{i}",
                                "n": 35,
                                "sum": 7.0 + i,
                                "sum_sq": 7.0,
                            }
                            for i in range(5)
                        ]
                    },
                )
            )
            session.add(
                DuelingSlicePreparation(
                    id=uuid4(),
                    season_id=season_id,
                    run_seed=4,
                    status="pending",
                    attempts=0,
                )
            )
        base = "/api/v1/admin/generator-competition/dueling"
        assert (await client.get(f"{base}/seasons")).status_code == 401
        board = (await client.get(f"{base}/seasons", headers=_ADMIN)).json()
        assert board["target"]["bench_version"] == 16
        [current] = board["current"]
        assert current["id"] == str(season_id)
        assert current["secret_hex"] is None and current["season_seed"] is None
        means = {m["column_id"]: m for m in current["column_means"]}
        assert means["c0"]["mean"] == pytest.approx(0.2)
        assert means["c0"]["below_floor"] is False and means["c0"]["scores"] == 1
        assert current["slices"] == {"pending": 1}
        slices = (await client.get(f"{base}/slices", headers=_ADMIN)).json()
        assert slices["counts"] == {"pending": 1}
        assert slices["slices"][0]["season_index"] == 20

        rotate = {"reason": "pinned generator was banned", "confirmation": "ROTATE"}
        wrong = await client.post(f"{base}/rotate", headers=_ADMIN, json=rotate)
        assert wrong.status_code == 409 and "ROTATE DUELING SEASON" in wrong.text
        rotate["confirmation"] = "ROTATE DUELING SEASON"
        ok = await client.post(f"{base}/rotate", headers=_ADMIN, json=rotate)
        assert ok.status_code == 200, ok.text
        assert ok.json()["rotation_reason"] == "pinned generator was banned"
        again = await client.post(f"{base}/rotate", headers=_ADMIN, json=rotate)
        assert again.status_code == 409

        # The next tick moves its close to the next round boundary.
        notes = await _tick(session_maker, 21 * _EPOCHS + 1)
        assert any("rotation closes it at round 22" in note for note in notes)
        # Escalating to immediate closes it on the next tick.
        now_body = {**rotate, "immediate": True}
        assert (
            await client.post(f"{base}/rotate", headers=_ADMIN, json=now_body)
        ).status_code == 200
        await _tick(session_maker, 21 * _EPOCHS + 2)
        detail = (
            await client.get(f"{base}/seasons/{season_id}", headers=_ADMIN)
        ).json()
        assert detail["status"] == "closed"
        assert detail["detail"]["close_reason"].startswith("rotated immediately")
        await _tick(session_maker, 21 * _EPOCHS + 2 + 6)
        revealed = (
            await client.get(f"{base}/seasons/{season_id}", headers=_ADMIN)
        ).json()
        assert revealed["status"] == "revealed"
        assert revealed["secret_hex"] == ("02" * 32)
        assert revealed["season_seed"] == season_seed(20, b"\x02" * 32, _BLOCK)


def _report(season_id: UUID, sha: str) -> dict[str, Any]:
    return {
        "dueling": {
            "season_id": str(season_id),
            "season_index": 5,
            "slice_sha256": sha,
            "owner_key": "coldkey:a",
            "datagen_raw": 0.8,
            "datagen_gate": 1.0,
            "column_weight": 0.1,
            "columns": [
                {
                    "column_id": f"c{i}",
                    "owner_key": "",
                    "n": 35,
                    "sum": 20.0,
                    "sum_sq": 14.0,
                }
                for i in range(5)
            ],
            "composite_stderr": 0.01,
        }
    }


class TestDuelingLease:
    async def test_lease_reserves_then_serves_and_keeps_pinned_bytes(
        self,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        now = datetime.now(UTC)
        # No season: refused, nothing reserved.
        with pytest.raises(DuelingSliceUnavailable) as none:
            await resolve_dueling_slice(
                session_maker,
                bench_version=16,
                run_seed=7,
                pinned_season_id=None,
                pinned_slice_sha256=None,
                now=now,
            )
        assert "no dueling season" in none.value.detail
        season_id = await _open_season(session_maker, default)
        # Two concurrent leases of one seed share one preparation.
        for _ in range(2):
            with pytest.raises(DuelingSliceUnavailable) as pending:
                await resolve_dueling_slice(
                    session_maker,
                    bench_version=16,
                    run_seed=7,
                    pinned_season_id=None,
                    pinned_slice_sha256=None,
                    now=now,
                )
            assert pending.value.detail == "dueling slice preparation pending"
        async with session_maker() as session, session.begin():
            [row] = list(await session.scalars(select(DuelingSlicePreparation)))
            raw = b'{"slice":1}'
            row.status = "ready"
            row.slice_bytes = raw
            row.slice_sha256 = hashlib.sha256(raw).hexdigest()
        served = await resolve_dueling_slice(
            session_maker,
            bench_version=16,
            run_seed=7,
            pinned_season_id=None,
            pinned_slice_sha256=None,
            now=now,
        )
        assert served.season_id == season_id and served.slice_bytes == raw
        assert "slice" not in repr(served).replace("DuelingLeaseSlice", "")
        # The season closes; a re-lease of the pinned seed keeps its bytes.
        async with session_maker() as session, session.begin():
            season = await session.get(DuelingSeason, season_id)
            assert season is not None
            season.status = "closed"
        again = await resolve_dueling_slice(
            session_maker,
            bench_version=16,
            run_seed=7,
            pinned_season_id=season_id,
            pinned_slice_sha256=served.slice_sha256,
            now=now,
        )
        assert again == served
        # Pinned bytes that are no longer servable refuse the lease rather
        # than serving another season's slice for the same seed.
        async with session_maker() as session, session.begin():
            [row] = list(await session.scalars(select(DuelingSlicePreparation)))
            row.slice_bytes = None
            row.bytes_pruned_at = now
        with pytest.raises(DuelingSliceUnavailable) as pruned:
            await resolve_dueling_slice(
                session_maker,
                bench_version=16,
                run_seed=7,
                pinned_season_id=season_id,
                pinned_slice_sha256=served.slice_sha256,
                now=now,
            )
        assert "no longer servable" in pruned.value.detail
        # A later retest on the same ticket row (a new seed, the old pin)
        # resolves through the open season instead of the pin.
        async with session_maker() as session, session.begin():
            season = await session.get(DuelingSeason, season_id)
            assert season is not None
            season.status = "open"
        with pytest.raises(DuelingSliceUnavailable) as new_seed:
            await resolve_dueling_slice(
                session_maker,
                bench_version=16,
                run_seed=8,
                pinned_season_id=season_id,
                pinned_slice_sha256=served.slice_sha256,
                now=now,
            )
        assert new_seed.value.detail == "dueling slice preparation pending"
        with pytest.raises(DuelingSliceUnavailable) as still_new:
            await resolve_dueling_slice(
                session_maker,
                bench_version=16,
                run_seed=8,
                pinned_season_id=season_id,
                pinned_slice_sha256=served.slice_sha256,
                now=now,
            )
        assert still_new.value.detail == "dueling slice preparation pending"

    def test_score_evidence_is_bound_to_the_lease(self) -> None:
        season_id = uuid4()
        sha = "ab" * 32
        assert dueling_score_evidence(
            None, bench_version=15, season_id=None, slice_sha256=None, owner_key=None
        ) == (None, None)
        stored_season, stored = dueling_score_evidence(
            _report(season_id, sha),
            bench_version=16,
            season_id=season_id,
            slice_sha256=sha,
            owner_key="coldkey:a",
        )
        assert stored_season == season_id and stored is not None
        assert stored["slice_sha256"] == sha
        with pytest.raises(DuelingEvidenceRejected, match="owner_key"):
            dueling_score_evidence(
                _report(season_id, sha),
                bench_version=16,
                season_id=season_id,
                slice_sha256=sha,
                owner_key="coldkey:other",
            )
        for details, season, digest in (
            ({}, season_id, sha),
            (_report(uuid4(), sha), season_id, sha),
            (_report(season_id, sha), season_id, "cd" * 32),
            ({"dueling": {"season_id": "x"}}, season_id, sha),
        ):
            with pytest.raises(DuelingEvidenceRejected):
                dueling_score_evidence(
                    details,
                    bench_version=16,
                    season_id=season,
                    slice_sha256=digest,
                    owner_key="coldkey:a",
                )


class TestShadowCanarySeasons:
    async def test_a_canary_version_gets_shadow_seasons_and_slices(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With 16 neither active nor rolling out, an operator-named shadow
        version commits and opens shadow seasons, so a canary lease at 16 can
        get a slice before start_benchmark_rollout."""
        import ditto.api_server.endpoints.admin_generator_competition as admin_mod

        _install(app, session_maker)
        _active(monkeypatch, 15)
        current = (await client.get(_SETTINGS_URL, headers=_ADMIN)).json()
        body = {
            "expected_revision": current["effective"]["revision"],
            "settings": _settings(
                epochs_per_round=_EPOCHS,
                dueling_enabled=True,
                dueling_shadow_bench_version=99,
            ),
            "reason": "shadow dueling seasons for the v16 canary",
            "confirmation": generator_settings_confirmation("shadow"),
        }
        refused = await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        assert refused.status_code == 422
        assert "not a shipped bench contract" in refused.text
        monkeypatch.setattr(admin_mod, "benchmark_contract", lambda version: version)
        body["settings"] = {**body["settings"], "dueling_shadow_bench_version": 16}
        ok = await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        assert ok.status_code == 200, ok.text

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
        await _scored_round(session_maker, [(miner, 0.4, 0.1)])

        notes = await _tick(session_maker, _ROUND * _EPOCHS + 1)
        assert any("season 11 v16: committed (shadow)" in n for n in notes)
        work = (await client.get(_WORK, headers=_SCORER, params=_POLL)).json()["work"]
        assert work["kind"] == "battery" and work["bench_version"] == 16
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
        await _tick(session_maker, 11 * _EPOCHS)
        [opened] = [s for s in await _seasons(session_maker) if s.status == "open"]
        assert (opened.season_index, opened.shadow) == (11, True)
        assert str(default.submission_id) in {
            c["submission_id"] for c in opened.columns or ()
        }

        now = datetime.now(UTC)
        with pytest.raises(DuelingSliceUnavailable):
            await resolve_dueling_slice(
                session_maker,
                bench_version=16,
                run_seed=42,
                pinned_season_id=None,
                pinned_slice_sha256=None,
                now=now,
            )
        async with session_maker() as session, session.begin():
            [row] = list(await session.scalars(select(DuelingSlicePreparation)))
            raw = b'{"canary":1}'
            row.status = "ready"
            row.slice_bytes = raw
            row.slice_sha256 = hashlib.sha256(raw).hexdigest()
        served = await resolve_dueling_slice(
            session_maker,
            bench_version=16,
            run_seed=42,
            pinned_season_id=None,
            pinned_slice_sha256=None,
            now=now,
        )
        assert served.season_id == opened.id and served.slice_bytes == raw

        # Clearing the shadow version closes the season on the next tick.
        body["expected_revision"] = ok.json()["revision"]
        body["settings"] = {**body["settings"], "dueling_shadow_bench_version": None}
        assert (
            await client.post(_SETTINGS_URL, headers=_ADMIN, json=body)
        ).status_code == 200
        await _tick(session_maker, 11 * _EPOCHS + 1)
        closed = next(s for s in await _seasons(session_maker) if s.id == opened.id)
        assert closed.status == "closed"


class TestDuelingSliceRetry:
    async def test_retry_requeues_one_failed_preparation_behind_its_guards(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        await _open_lane(client, epochs_per_round=_EPOCHS, dueling_enabled=True)
        _active(monkeypatch, 16)
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        season_id = await _open_season(session_maker, default)
        prep_id, refused_id = uuid4(), uuid4()
        async with session_maker() as session, session.begin():
            for pid, seed, error in (
                (prep_id, 7, "c0: build failed"),
                (refused_id, 8, "infra: unsupported_request: tree digest mismatch"),
            ):
                session.add(
                    DuelingSlicePreparation(
                        id=pid,
                        season_id=season_id,
                        run_seed=seed,
                        status="failed",
                        attempts=3,
                        error=error,
                        failed_at=datetime.now(UTC),
                    )
                )
        base = "/api/v1/admin/generator-competition/dueling/slices"
        listing = (await client.get(base, headers=_ADMIN)).json()["slices"]
        views = {view["id"]: view for view in listing}
        assert views[str(prep_id)]["retryable"] is True
        assert views[str(refused_id)]["retryable"] is False
        assert "rotate the season" in views[str(refused_id)]["retry_refusal"]
        assert (
            await client.get(
                base, headers=_ADMIN, params={"preparation_id": str(prep_id)}
            )
        ).json()["slices"][0]["error"] == "c0: build failed"

        def body(pid: UUID, error: str, **extra: Any) -> dict[str, Any]:
            return {
                "expected_status": "failed",
                "expected_error": error,
                "reason": "builder host was replaced",
                "confirmation": f"RETRY DUELING SLICE {pid}",
                **extra,
            }

        url = f"{base}/{prep_id}/retry"
        good = body(prep_id, "c0: build failed")
        assert (
            await client.post(url, headers=_ADMIN, json={**good, "confirmation": "x"})
        ).status_code == 409
        assert (
            await client.post(url, headers=_ADMIN, json={**good, "reason": "short"})
        ).status_code == 422
        stale = await client.post(
            url, headers=_ADMIN, json={**good, "expected_error": "something else"}
        )
        assert stale.status_code == 409 and "re-read" in stale.text
        refused_url = f"{base}/{refused_id}/retry"
        refused_body = body(
            refused_id, "infra: unsupported_request: tree digest mismatch"
        )
        unsupported = await client.post(refused_url, headers=_ADMIN, json=refused_body)
        assert unsupported.status_code == 409 and "rotate" in unsupported.text

        ok = await client.post(url, headers=_ADMIN, json=good)
        assert ok.status_code == 200, ok.text
        view = ok.json()
        assert (view["status"], view["error"], view["attempts"]) == ("pending", None, 3)
        assert view["retries"] == 1 and view["last_retry_reason"] == good["reason"]
        again = await client.post(url, headers=_ADMIN, json=good)
        assert again.status_code == 409  # no longer failed
        async with session_maker() as session:
            action = await session.scalar(
                select(GeneratorOperatorAction).where(
                    GeneratorOperatorAction.subject == f"dueling_slice:{prep_id}"
                )
            )
            assert action is not None
            assert action.action == "retry_dueling_slice_preparation"
            assert (action.detail or {})["previous_error"] == "c0: build failed"

        # The re-queued preparation leases again (its attempt cap restarted)
        # and pins a slice.
        work = (await client.get(_WORK, headers=_SCORER, params=_POLL)).json()["work"]
        assert work["kind"] == "slice" and work["preparation_id"] == str(prep_id)
        raw = _slice_bytes(work)
        ready = await client.post(
            "/api/v1/generator-scorer/dueling-slice",
            headers=_SCORER,
            json=_slice_post(work, raw),
        )
        assert ready.status_code == 200 and ready.json()["status"] == "ready"

        # force retries the scorer-refused one, recorded; a closed season's
        # slices are never retried.
        forced = await client.post(
            refused_url, headers=_ADMIN, json={**refused_body, "force": True}
        )
        assert forced.status_code == 200, forced.text
        async with session_maker() as session, session.begin():
            row = await session.get(DuelingSlicePreparation, refused_id)
            assert row is not None
            row.status, row.error = "failed", "c1: build failed"
            row.failed_at = datetime.now(UTC)
            season = await session.get(DuelingSeason, season_id)
            assert season is not None
            season.status = "closed"
        closed = await client.post(
            refused_url, headers=_ADMIN, json=body(refused_id, "c1: build failed")
        )
        assert closed.status_code == 409 and "season is closed" in closed.text


async def _season_sequence(
    maker: async_sessionmaker[AsyncSession], default: GeneratorSubmission
) -> list[UUID]:
    """Three v16 seasons: two closed, then the open one (last)."""
    ids: list[UUID] = []
    for index in (30, 31, 32):
        season_id = await _open_season(maker, default, season_index=index)
        ids.append(season_id)
        if index != 32:
            async with maker() as session, session.begin():
                season = await session.get(DuelingSeason, season_id)
                assert season is not None
                season.status = "closed"
    return ids


def _v16_score(
    agent_id: UUID, validator: str, season_id: UUID, *, composite: float
) -> Score:
    return Score(
        agent_id=agent_id,
        validator_hotkey=validator,
        bench_version=16,
        run_id=f"run-{validator}",
        seed=3,
        composite=composite,
        tool_mean=composite,
        memory_mean=composite,
        median_ms=10,
        n=350,
        generated_at=datetime.now(UTC),
        dueling_season_id=season_id,
        dueling={"columns": []},
    )


class TestDuelingSeasonRotation:
    async def test_rotation_requeues_the_cohort_and_old_seasons_expire(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _install(app, session_maker)
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        expired, provisional, current = await _season_sequence(session_maker, default)
        agent_id = await _seed_agent(session_maker, 1)
        validators = {"5RotV1": expired, "5RotV2": provisional, "5RotV3": current}
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            for validator, season_id in validators.items():
                session.add(_v16_score(agent_id, validator, season_id, composite=0.6))
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        validator_hotkey=validator,
                        bench_version=16,
                        slot_id="slot-0",
                        status=TicketStatus.SCORED,
                        purpose=TicketPurpose.CANONICAL_QUORUM,
                        purpose_revision=1,
                        issued_at=now,
                        deadline=now + timedelta(minutes=90),
                        attempt_count=1,
                        manual_retry_grants=0,
                        infra_retry_grants=0,
                        dueling_season_id=season_id,
                        slice_sha256="ab" * 32,
                    )
                )

        # The window is the open season plus the one before; the oldest
        # season's score no longer counts anywhere the window is read.
        async with session_maker() as session:
            window = await dueling_season_window(session, 16)
            assert window is not None
            assert (window.current, window.previous) == (current, provisional)
            assert await dueling_season_window(session, 15) is None
            counted = await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(
                    await season_scope_predicate(
                        session,
                        version_column=Score.bench_version,
                        season_column=Score.dueling_season_id,
                        bench_versions=(16,),
                    )
                )
            )
            assert counted == 2

        # Season open queues the cohort's out-of-season scores, champion first.
        async def _cohort(*_args: Any) -> tuple[list[UUID], str]:
            return [agent_id], "continual_cohort"

        monkeypatch.setattr(dueling_mod, "_rotation_cohort", _cohort)
        async with session_maker() as session, session.begin():
            season = await session.get(DuelingSeason, current)
            assert season is not None
            rotation = await dueling_mod.queue_rotation_retests(
                session, season, now=now
            )
        assert rotation["queued"] == 2 and rotation["already_current"] == 1
        async with session_maker() as session, session.begin():
            again = await queue_dueling_rotation_retests(
                session, season_id=current, bench_version=16, cohort=[agent_id], now=now
            )
        assert again["queued"] == 0  # idempotent per season

        # A free slot promotes it and drops the old season's slice pin, so
        # the lease takes the open season's slice.
        async with session_maker() as session, session.begin():
            ticket = await activate_next_score_retest(
                session,
                validator_hotkey="5RotV1",
                now=now,
                supports_version=lambda version: version == 16,
                required_basis=DUELING_ROTATION_RETEST_BASIS,
                allow_parallel_ordinary=True,
                allow_parallel_contract_retests=True,
            )
            assert ticket is not None and ticket.status == TicketStatus.ISSUED
            assert ticket.dueling_season_id is None and ticket.slice_sha256 is None

        view = (
            await client.get(
                f"/api/v1/admin/generator-competition/dueling/seasons/{current}",
                headers=_ADMIN,
            )
        ).json()["rotation"]
        assert view["standing"] == "current" and view["source"] == "continual_cohort"
        assert (view["pending"], view["running"], view["closed"]) == (1, 1, 0)
        assert view["cohort_scores_in_season"] == 1
        assert view["cohort_quorum_in_season"] == 0

    async def test_reign_anchors_restart_per_season(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        champion = await _seed_agent(session_maker, 1)
        first = await _open_season(session_maker, default, season_index=40)
        block_hash = "0x" + "9d" * 32
        now = datetime.now(UTC)

        async def _pin(season_id: UUID) -> None:
            async with session_maker() as session, session.begin():
                for version in (15, 16):
                    key = NIL_SEASON_KEY if version == 15 else season_id
                    await resolve_reign_seed_anchor(
                        session,
                        champion_agent_id=champion,
                        bench_version=version,
                        ready_block=100,
                        now=now,
                        finalized_hashes={(champion, version, key): block_hash},
                    )

        await _pin(first)
        async with session_maker() as session, session.begin():
            season = await session.get(DuelingSeason, first)
            assert season is not None
            season.status = "closed"
        second = await _open_season(session_maker, default, season_index=41)
        await _pin(second)
        async with session_maker() as session:
            served = await list_reign_seed_anchors(
                session, bench_version=16, current_season_only=True
            )
            every = await list_reign_seed_anchors(session, bench_version=16)
            legacy = await list_reign_seed_anchors(
                session, bench_version=15, current_season_only=True
            )
        # The ledger serves only the open season's reign; binding a reported
        # seed still sees both; v15 keeps one nil-keyed anchor per reign.
        assert [a.season_key for a in served] == [second]
        assert {a.season_key for a in every} == {first, second}
        assert [a.season_key for a in legacy] == [NIL_SEASON_KEY]


class TestDuelingReveal:
    async def test_revealed_season_publishes_pins_slices_and_released_sources(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install(app, session_maker)
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        miner = await _generator(
            session_maker, hotkey=_MINER_KEY, owner="coldkey:eve", salt="miner"
        )
        late = await _generator(
            session_maker, hotkey=_WEAK_KEY, owner="coldkey:ferdie", salt="late"
        )
        revealed = await _open_season(session_maker, default, season_index=50)
        current = None
        now = datetime.now(UTC)
        miner_pin = {
            "column_id": "c0",
            "submission_id": str(miner.submission_id),
            "miner_hotkey": miner.miner_hotkey,
            "owner_key": miner.owner_key,
            "tree_sha256": miner.tree_sha256,
            "source_sha256": miner.source_sha256,
            "grading_bench_version": 16,
            "public_default": False,
        }
        fallback = {
            "submission_id": str(default.submission_id),
            "miner_hotkey": default.miner_hotkey,
            "source_sha256": default.source_sha256,
            "tree_sha256": default.tree_sha256,
            "grading_bench_version": 15,
        }
        async with session_maker() as session, session.begin():
            season = await session.get(DuelingSeason, revealed)
            assert season is not None
            season.columns = [miner_pin, *(season.columns or [])[1:]]
            season.detail = {"fallback": fallback}
            season.status = "revealed"
            season.closed_at = now - timedelta(days=60)
            season.revealed_at = now - timedelta(hours=1)
            session.add(
                DuelingSlicePreparation(
                    id=uuid4(),
                    season_id=revealed,
                    run_seed=7,
                    status="ready",
                    attempts=1,
                    slice_sha256=hashlib.sha256(b"{}").hexdigest(),
                    slice_bytes=b"{}",
                    ready_at=now,
                    columns=[
                        {"column_id": "c0", "substituted": False},
                        {"column_id": "c1", "substituted": True},
                    ],
                )
            )
        current = await _open_season(session_maker, default, season_index=51)
        async with session_maker() as session, session.begin():
            season = await session.get(DuelingSeason, current)
            assert season is not None
            season.columns = [
                {**miner_pin, "submission_id": str(late.submission_id)},
                *(season.columns or [])[1:],
            ]
            notes = await sync_generator_source_releases(session, now=now)
        assert notes == [f"generator {miner.submission_id}: source released"]

        base = "/api/v1/public/dueling"
        # An unrevealed season is not public at all.
        assert (await client.get(f"{base}/seasons/{current}")).status_code == 404
        [season_view] = (await client.get(f"{base}/seasons")).json()["seasons"]
        assert season_view["id"] == str(revealed)
        # The (index, version) cursor keeps a tied index; the index alone skips it.
        tied = {"before_index": 50, "before_bench_version": 17}
        assert (
            len((await client.get(f"{base}/seasons", params=tied)).json()["seasons"])
            == 1
        )
        older = {"before_index": 50}
        assert (await client.get(f"{base}/seasons", params=older)).json()[
            "seasons"
        ] == []
        assert season_view["secret_hex"] == ("02" * 32)
        assert season_view["slices"] == 1
        assert season_view["pins"]["columns"][0]["submission_id"] == str(
            miner.submission_id
        )
        sources = {s["submission_id"]: s for s in season_view["sources"]}
        assert sources[str(miner.submission_id)]["available"] is True
        assert sources[str(default.submission_id)]["public_default"] is True

        page = (await client.get(f"{base}/seasons/{revealed}/slices")).json()
        [entry] = page["slices"]
        assert entry["run_seed"] == "7"
        filtered = await client.get(
            f"{base}/seasons/{revealed}/slices", params={"run_seed": "8"}
        )
        assert filtered.json()["total"] == 0
        # The late generator is pinned only by an open season: no public status.
        status = await client.get(f"{base}/generator-sources/{late.submission_id}")
        assert status.status_code == 404
        assert entry["slice_sha256"] == hashlib.sha256(b"{}").hexdigest()
        c0, c1 = entry["pins"]["columns"][:2]
        assert c0["substituted"] is False and c0["owner_key"] == "coldkey:eve"
        assert c1["substituted"] is True and c1["public_default"] is True
        assert c1["submission_id"] == str(default.submission_id)
        assert c1["owner_key"] == "" and c1["grading_bench_version"] == 15
        assert c1["slice_seed"] == slice_seed(
            season_seed(50, b"\x02" * 32, _BLOCK), 7, "c1"
        )

        archive = await client.get(
            f"{base}/generator-sources/{miner.submission_id}/archive"
        )
        assert archive.status_code == 200
        assert hashlib.sha256(archive.content).hexdigest() == miner.source_sha256
        # Pinned only by an open season: not public at all yet.
        held = await client.get(
            f"{base}/generator-sources/{late.submission_id}/archive"
        )
        assert held.status_code == 404
        unknown = await client.get(f"{base}/generator-sources/{uuid4()}/archive")
        assert unknown.status_code == 404

        board = (
            await client.get(
                "/api/v1/admin/generator-competition/dueling/source-releases",
                headers=_ADMIN,
            )
        ).json()
        assert board["counts"] == {"released": 1, "waiting_close": 1}

    async def test_slice_bytes_are_pruned_once_every_source_is_public(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        default = await _generator(
            session_maker,
            hotkey=_DEFAULT_KEY,
            owner="public-default",
            salt="default",
            public_default=True,
        )
        miner = await _generator(
            session_maker, hotkey=_MINER_KEY, owner="coldkey:eve", salt="miner"
        )
        season_id = await _open_season(session_maker, default, season_index=60)
        now = datetime.now(UTC)
        raw = b'{"slice":1}'
        prep_id = uuid4()
        async with session_maker() as session, session.begin():
            season = await session.get(DuelingSeason, season_id)
            assert season is not None
            season.columns = [
                {
                    **(season.columns or [])[0],
                    "submission_id": str(miner.submission_id),
                    "public_default": False,
                },
                *(season.columns or [])[1:],
            ]
            season.status = "revealed"
            season.closed_at = now - timedelta(days=30)
            season.revealed_at = now - timedelta(days=10)
            session.add(
                DuelingSlicePreparation(
                    id=prep_id,
                    season_id=season_id,
                    run_seed=9,
                    status="ready",
                    attempts=1,
                    slice_sha256=hashlib.sha256(raw).hexdigest(),
                    slice_bytes=raw,
                    ready_at=now,
                )
            )

        async def _prune(at: datetime) -> list[str]:
            async with session_maker() as session, session.begin():
                return await prune_released_slice_bytes(session, now=at)

        # The pinned miner source is not released: the bytes stay.
        assert await _prune(now) == []
        async with session_maker() as session, session.begin():
            await sync_generator_source_releases(session, now=now)
        # Released just now: still inside the score dispute window.
        assert await _prune(now) == []

        # A withholding policy keeps every byte: nobody else can regenerate.
        async def _withheld(*_args: Any, **_kwargs: Any) -> ArtifactReleasePolicy:
            return ArtifactReleasePolicy(
                disclosure=SourceDisclosure.NEVER, embargo_hours=48
            )

        with monkeypatch.context() as patch:
            patch.setattr(releases_mod, "artifact_release_policy", _withheld)
            assert await _prune(now + timedelta(days=8)) == []
        notes = await _prune(now + timedelta(days=8))
        assert notes == ["season 60 v16: pruned 1 slice(s); digests kept"]
        async with session_maker() as session:
            row = await session.get(DuelingSlicePreparation, prep_id)
            assert row is not None
            assert row.slice_bytes is None and row.bytes_pruned_at is not None
            assert row.slice_sha256 == hashlib.sha256(raw).hexdigest()
