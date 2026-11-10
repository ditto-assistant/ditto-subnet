"""SN-466: a dueling season rotation must not silently delete a board row.

The two-season window is deliberate cost control: scores count for their own
season and the next, then expire, so a rotation never pays to re-score the
board. These tests pin the display contract built on top of it:

* an agent in the epoch pin validators fold right now keeps a ranked board row
  with the pin's official composite until the next pin, even when its season
  expires 28 seconds after that pin was taken;
* the next pin's input (the live, season-scoped ledger) never mixes the expired
  season back in, so emissions move only when a pin is taken;
* an agent whose quorum expired earlier stays visible as an unranked
  ``expired`` reference row, and nothing is re-scored for any of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.endpoints.public as public_endpoints
from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_server.endpoints import admin_leaderboard
from ditto.db.models import (
    Agent,
    DuelingSeason,
    LedgerEpochSnapshot,
    Score,
    ScoreAuditEntry,
    ValidatorTicket,
)
from ditto.db.queries.dueling_season_expiry import (
    expired_season_ledger_rows,
    season_coverage,
)
from ditto.db.queries.scores import LedgerRow, list_eligible_ledger
from ditto.tests.api_server.endpoints.test_public import _activate_era, _install_db
from ditto.tests.api_server.test_dueling_seasons import _generator, _open_season

pytestmark = pytest.mark.asyncio

_VERSION = 16
_VALIDATORS = ("5ExpV1", "5ExpV2", "5ExpV3")
_PIN_EPOCH = 26_259
# The live timeline, in UTC: pin 26259 at 10:09:03 ET, season 26103 opened at
# 10:09:31 ET -- 28 seconds later.
_PINNED_AT = datetime(2026, 11, 10, 15, 9, 3, tzinfo=UTC)
_ROTATED_AT = _PINNED_AT + timedelta(seconds=28)


def _hotkey(uri: str) -> str:
    return bittensor.Keypair.create_from_uri(uri).ss58_address


_CHAMPION = _hotkey("//ExpiryChampion")
_LOGAN = _hotkey("//ExpiryLogan")
_RIVAL = _hotkey("//ExpiryRival")
_DIANA = _hotkey("//ExpiryDiana")


async def _agent(
    maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    name: str,
    season_id: UUID,
    composite: float,
) -> UUID:
    """A scored agent with a full three-validator quorum in one season."""
    agent_id = uuid4()
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name=name,
                version=1,
                sha256="ab" * 32,
                size_bytes=1024,
                status=AgentStatus.SCORED,
                created_at=datetime(2026, 11, 9, tzinfo=UTC),
            )
        )
        await session.flush()
        for validator in _VALIDATORS:
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey=validator,
                    bench_version=_VERSION,
                    run_id=f"run-{name}-{validator}",
                    seed=3,
                    composite=composite,
                    tool_mean=composite,
                    memory_mean=composite,
                    median_ms=10,
                    n=350,
                    generated_at=datetime(2026, 11, 10, 10, tzinfo=UTC),
                    dueling_season_id=season_id,
                    dueling={"columns": []},
                )
            )
    return agent_id


def _pin_entry(agent_id: UUID, hotkey: str, composite: float, season: UUID) -> dict:
    return {
        "miner_hotkey": hotkey,
        "agent_id": str(agent_id),
        "composite": composite,
        "n": 350,
        "first_seen": datetime(2026, 11, 9, tzinfo=UTC).isoformat(),
        "sha256": "ab" * 32,
        "run_id": "run",
        "seed": 3,
        "validator_hotkey": _VALIDATORS[1],
        "status": "scored",
        "bench_version": _VERSION,
        "dueling_season_id": str(season),
    }


async def _pin(
    maker: async_sessionmaker[AsyncSession],
    *,
    epoch_index: int,
    pinned_at: datetime,
    entries: list[dict],
    champion: UUID,
) -> None:
    async with maker() as session, session.begin():
        session.add(
            LedgerEpochSnapshot(
                snapshot_id=uuid4(),
                netuid=118,
                epoch_index=epoch_index,
                last_epoch_block=epoch_index * 360,
                pinned_block=epoch_index * 360 + 2,
                pinned_block_hash="0x" + "ab" * 32,
                pinned_at=pinned_at,
                bench_version=_VERSION,
                entries=entries,
                context={"served": {"burn_share": 0.0}},
                champion_agent_id=champion,
                ledger_digest=f"{epoch_index:064x}",
            )
        )


async def _rotate(
    maker: async_sessionmaker[AsyncSession], default: object, *, close: UUID
) -> UUID:
    """Close the open season and open the next one at ``_ROTATED_AT``."""
    async with maker() as session, session.begin():
        season = await session.get(DuelingSeason, close)
        assert season is not None
        season.status = "closed"
        season.closed_at = _ROTATED_AT
    opened = await _open_season(maker, default, season_index=26_103)  # type: ignore[arg-type]
    async with maker() as session, session.begin():
        season = await session.get(DuelingSeason, opened)
        assert season is not None
        season.opened_at = _ROTATED_AT
    return opened


async def _retest_work(maker: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    """Tickets and audit-log rows: the board read must create neither."""
    async with maker() as session:
        tickets = await session.scalar(
            select(func.count()).select_from(ValidatorTicket)
        )
        audit = await session.scalar(select(func.count()).select_from(ScoreAuditEntry))
    return int(tickets or 0), int(audit or 0)


class TestSeasonRotationAfterPin:
    async def test_pinned_member_survives_a_rotation_28s_after_the_pin(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        maker = session_maker
        _install_db(app, maker)
        app.state.session_maker = maker
        await _activate_era(maker, _VERSION)
        default = await _generator(
            maker,
            hotkey=_hotkey("//ExpiryDefault"),
            owner="public-default",
            salt="expiry-default",
            public_default=True,
        )
        # 26100 and 26101 closed, 26102 open: the window is 26102 + 26101.
        seasons: dict[int, UUID] = {}
        for index in (26_100, 26_101, 26_102):
            seasons[index] = await _open_season(maker, default, season_index=index)
            if index != 26_102:
                async with maker() as session, session.begin():
                    row = await session.get(DuelingSeason, seasons[index])
                    assert row is not None
                    row.status = "closed"
        champion = await _agent(
            maker,
            hotkey=_CHAMPION,
            name="champion",
            season_id=seasons[26_102],
            composite=0.97,
        )
        # logan v15: scored at the very end of 26101, so it is the provisional
        # previous season while 26102 runs.
        logan = await _agent(
            maker,
            hotkey=_LOGAN,
            name="logan",
            season_id=seasons[26_101],
            composite=0.960714,
        )
        rival = await _agent(
            maker,
            hotkey=_RIVAL,
            name="rival",
            season_id=seasons[26_102],
            composite=0.95,
        )
        # XXX v3: both scores already two seasons back.
        diana = await _agent(
            maker,
            hotkey=_DIANA,
            name="diana",
            season_id=seasons[26_100],
            composite=0.915857,
        )
        await _pin(
            maker,
            epoch_index=_PIN_EPOCH,
            pinned_at=_PINNED_AT,
            entries=[
                _pin_entry(champion, _CHAMPION, 0.97, seasons[26_102]),
                _pin_entry(logan, _LOGAN, 0.960714, seasons[26_101]),
                _pin_entry(rival, _RIVAL, 0.95, seasons[26_102]),
            ],
            champion=champion,
        )
        work_before = await _retest_work(maker)
        projected: list[set[UUID]] = []
        projection = public_endpoints._public_koth_emissions

        def _capture(rows: list[LedgerRow], **kwargs: Any) -> Any:
            projected.append({row.agent_id for row in rows})
            return projection(rows, **kwargs)

        monkeypatch.setattr(public_endpoints, "_public_koth_emissions", _capture)

        # Before the rotation logan is an ordinary finalized row, rank 2.
        before = (await client.get("/api/v1/public/leaderboard")).json()
        by_id = {entry["agent_id"]: entry for entry in before["entries"]}
        assert by_id[str(logan)]["rank"] == 2
        assert "season_expiry" not in by_id[str(logan)]

        # Season 26103 opens 28 seconds after pin 26259: 26101 expires.
        current = await _rotate(maker, default, close=seasons[26_102])
        async with maker() as session:
            # The next pin is built from this read. It never mixes the
            # expired season back in: logan and diana are out of it.
            ledger_ids = {
                row.agent_id
                for row in await list_eligible_ledger(
                    session, include_fingerprints=False, include_details=False
                )
            }
        assert ledger_ids == {champion, rival}

        after = (await client.get("/api/v1/public/leaderboard")).json()
        entries = after["entries"]
        by_id = {entry["agent_id"]: entry for entry in entries}
        carried = by_id[str(logan)]
        # Still on the board, at its pinned place, with a non-null official
        # composite: the one validators fold until the next pin.
        assert carried["rank"] == 2
        assert carried["finalized"] is True
        assert carried["official_composite"] == pytest.approx(0.960714)
        assert carried["score_count"] == 3
        expiry = carried["season_expiry"]
        assert expiry["state"] == "pinned_carry"
        assert expiry["pinned_epoch_index"] == _PIN_EPOCH
        assert expiry["score_dueling_season_ids"] == [str(seasons[26_101])]
        assert expiry["window_dueling_season_ids"] == [
            str(current),
            str(seasons[26_102]),
        ]
        assert expiry["in_window_score_count"] == 0
        assert expiry["last_valid_composite"] == pytest.approx(0.960714)
        assert [entry["rank"] for entry in entries if entry["finalized"]] == [1, 2, 3]
        assert by_id[str(rival)]["rank"] == 3

        # The emissions block projects the NEXT pin, which is season-scoped:
        # the carried row never reaches the fold that projection runs.
        assert projected[-1] == {champion, rival}

        # diana: expired two seasons ago, shown unranked for reference.
        reference = by_id[str(diana)]
        assert reference["rank"] is None
        assert reference["finalized"] is False
        assert reference["emission_eligible"] in (False, None)
        assert reference["season_expiry"]["state"] == "expired"
        assert reference["season_expiry"]["pinned_epoch_index"] is None
        assert reference["season_expiry"]["score_dueling_season_ids"] == [
            str(seasons[26_100])
        ]
        assert reference["season_expiry"]["last_valid_composite"] == pytest.approx(
            0.915857
        )
        assert entries[-1]["agent_id"] == str(diana)

        # The next pin is taken from the live window and drops logan; the
        # board then shows it as expired too. Nothing was re-scored.
        await _pin(
            maker,
            epoch_index=_PIN_EPOCH + 1,
            pinned_at=_ROTATED_AT + timedelta(hours=1, minutes=12),
            entries=[
                _pin_entry(champion, _CHAMPION, 0.97, seasons[26_102]),
                _pin_entry(rival, _RIVAL, 0.95, seasons[26_102]),
            ],
            champion=champion,
        )
        later = (await client.get("/api/v1/public/leaderboard")).json()
        by_id = {entry["agent_id"]: entry for entry in later["entries"]}
        assert by_id[str(logan)]["rank"] is None
        assert by_id[str(logan)]["season_expiry"]["state"] == "expired"
        assert by_id[str(rival)]["rank"] == 2
        assert await _retest_work(maker) == work_before

    async def test_held_agent_in_the_pin_is_not_carried(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Only ``scored`` agents are carried: a hold still removes the row."""
        maker = session_maker
        _install_db(app, maker)
        app.state.session_maker = maker
        await _activate_era(maker, _VERSION)
        default = await _generator(
            maker,
            hotkey=_hotkey("//ExpiryDefault2"),
            owner="public-default",
            salt="expiry-default-2",
            public_default=True,
        )
        old = await _open_season(maker, default, season_index=40)
        async with maker() as session, session.begin():
            row = await session.get(DuelingSeason, old)
            assert row is not None
            row.status = "closed"
        mid = await _open_season(maker, default, season_index=41)
        champion = await _agent(
            maker, hotkey=_CHAMPION, name="champion", season_id=mid, composite=0.9
        )
        held = await _agent(
            maker, hotkey=_LOGAN, name="held", season_id=old, composite=0.95
        )
        await _pin(
            maker,
            epoch_index=_PIN_EPOCH,
            pinned_at=_PINNED_AT,
            entries=[
                _pin_entry(held, _LOGAN, 0.95, old),
                _pin_entry(champion, _CHAMPION, 0.9, mid),
            ],
            champion=held,
        )
        await _rotate(maker, default, close=mid)
        async with maker() as session, session.begin():
            agent = await session.get(Agent, held)
            assert agent is not None
            agent.status = AgentStatus.ATH_PENDING_REVIEW
        body = (await client.get("/api/v1/public/leaderboard")).json()
        assert str(held) not in {entry["agent_id"] for entry in body["entries"]}


class TestSeasonCoverage:
    async def test_coverage_separates_expired_from_provisional_quorums(
        self,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        maker = session_maker
        await _activate_era(maker, _VERSION)
        default = await _generator(
            maker,
            hotkey=_hotkey("//ExpiryDefault3"),
            owner="public-default",
            salt="expiry-default-3",
            public_default=True,
        )
        ids = []
        for index in (50, 51, 52):
            ids.append(await _open_season(maker, default, season_index=index))
            if index != 52:
                async with maker() as session, session.begin():
                    row = await session.get(DuelingSeason, ids[-1])
                    assert row is not None
                    row.status = "closed"
        expired = await _agent(
            maker, hotkey=_DIANA, name="expired", season_id=ids[0], composite=0.8
        )
        provisional = await _agent(
            maker, hotkey=_LOGAN, name="provisional", season_id=ids[1], composite=0.7
        )
        async with maker() as session:
            coverage = await season_coverage(session, bench_version=_VERSION)
            assert coverage[expired].expired
            assert coverage[expired].in_window_count == 0
            assert coverage[expired].score_dueling_season_ids == (ids[0],)
            assert not coverage[provisional].expired
            assert coverage[provisional].in_window_count == 3
            rows = await expired_season_ledger_rows(session, bench_version=_VERSION)
            assert [row.agent_id for row, _coverage in rows] == [expired]
            assert rows[0][0].eligible is False
            # Inert below the dueling floor.
            assert await season_coverage(session, bench_version=15) == {}

    async def test_median_reads_only_the_last_window_that_held_the_quorum(
        self,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A stored row from a season outside the last valid window never
        shifts the reported last valid score."""
        maker = session_maker
        await _activate_era(maker, _VERSION)
        default = await _generator(
            maker,
            hotkey=_hotkey("//ExpiryDefault4"),
            owner="public-default",
            salt="expiry-default-4",
            public_default=True,
        )
        ids = []
        for index in (60, 61, 62, 63):
            ids.append(await _open_season(maker, default, season_index=index))
            if index != 63:
                async with maker() as session, session.begin():
                    row = await session.get(DuelingSeason, ids[-1])
                    assert row is not None
                    row.status = "closed"
        # V1's row is from 60, already outside the window {61, 62} in which
        # V2-V4 made the quorum; {62, 63} is live now and holds none of them.
        agent_id = await _agent(
            maker, hotkey=_DIANA, name="stale", season_id=ids[1], composite=0.85
        )
        spread = await _agent(
            maker, hotkey=_RIVAL, name="spread", season_id=ids[0], composite=0.9
        )
        async with maker() as session, session.begin():
            for validator, composite in zip(
                _VALIDATORS, (0.80, 0.85, 0.90), strict=True
            ):
                await session.execute(
                    update(Score)
                    .where(
                        Score.agent_id == agent_id,
                        Score.validator_hotkey == validator,
                    )
                    .values(composite=composite)
                )
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey="5ExpV0",
                    bench_version=_VERSION,
                    run_id="run-stale",
                    seed=3,
                    composite=0.10,
                    tool_mean=0.10,
                    memory_mean=0.10,
                    median_ms=10,
                    n=350,
                    generated_at=datetime(2026, 11, 10, 9, tzinfo=UTC),
                    dueling_season_id=ids[0],
                    dueling={"columns": []},
                )
            )
            # "spread": one row in each of three seasons, so no window ever
            # held its quorum and it has no last valid score at all.
            for validator, season in zip(_VALIDATORS, ids[:3], strict=True):
                await session.execute(
                    update(Score)
                    .where(
                        Score.agent_id == spread,
                        Score.validator_hotkey == validator,
                    )
                    .values(dueling_season_id=season)
                )
        async with maker() as session:
            coverage = await season_coverage(session, bench_version=_VERSION)
            stale = coverage[agent_id]
            assert stale.expired
            assert stale.last_valid_window == frozenset({ids[1], ids[2]})
            assert stale.median is not None
            # All four rows would pick 0.80; the counted three pick 0.85.
            assert stale.median.composite == pytest.approx(0.85)
            assert not coverage[spread].expired
            assert coverage[spread].median is None
            rows = await expired_season_ledger_rows(session, bench_version=_VERSION)
            assert [(row.agent_id, row.composite) for row, _ in rows] == [
                (agent_id, pytest.approx(0.85))
            ]


class TestInCurrentPin:
    async def test_a_pin_frozen_under_another_version_is_not_current(
        self,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        maker = session_maker
        agent_id = uuid4()
        await _pin(
            maker,
            epoch_index=_PIN_EPOCH,
            pinned_at=_PINNED_AT,
            entries=[_pin_entry(agent_id, _LOGAN, 0.96, uuid4())],
            champion=agent_id,
        )
        request = cast(
            Request,
            SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        config=SimpleNamespace(chain=SimpleNamespace(netuid=118))
                    )
                )
            ),
        )
        settings = ContinualRetestSettings(ledger_pin_mode="epoch")
        async with maker() as session:
            assert (
                await admin_leaderboard._in_current_pin(
                    request, session, agent_id, settings, bench_version=_VERSION
                )
                is True
            )
            assert (
                await admin_leaderboard._in_current_pin(
                    request, session, uuid4(), settings, bench_version=_VERSION
                )
                is False
            )
            # After a bench-version change the old pin says nothing.
            assert (
                await admin_leaderboard._in_current_pin(
                    request, session, agent_id, settings, bench_version=_VERSION + 1
                )
                is None
            )
            live = ContinualRetestSettings(ledger_pin_mode="live")
            assert (
                await admin_leaderboard._in_current_pin(
                    request, session, agent_id, live, bench_version=_VERSION
                )
                is None
            )
