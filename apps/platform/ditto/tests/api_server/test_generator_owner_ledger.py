"""SN-487: round selection and the generator ledger use one owner identity.

Round 26124 had seven columns and no c7. Every active, qualified, unscored
generator waiting at open belonged to an owner that already held a column
(lh2gen, bountygen, kinlist), while those owners' superseded rows on their
other hotkeys stayed on the ledger at their old scores: never re-scored, but
paid per hotkey for the whole freshness window. The kin* owner also froze
rows on purpose with uploads made to fail ("withdrawn by owner ... not meant
to run").

These tests seed that board directly (real Postgres) and pin the fix:

* the ledger pays one entry per ``owner_key``, the column round selection
  last seated that owner in;
* a superseded row counts only while its hotkey's newer upload is still
  active, so a rejected or withdrawn replacement cannot freeze it;
* a replacement rejected on its own evaluation restores the row it
  superseded, which must qualify again before it earns.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import ditto.api_server.generator_competition as competition
from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_server.generator_competition import ResolvedSettings
from ditto.api_server.generator_rounds import build_round_ledger, select_round_columns
from ditto.db.models import (
    GeneratorRound,
    GeneratorRoundCaseVerdict,
    GeneratorSubmission,
)
from ditto.db.queries.generator import restore_superseded, restored_reason

_NOW = datetime.now(UTC).replace(microsecond=0)
_SETTINGS = GeneratorCompetitionSettings(
    mode="enforce", counted_columns=5, challenger_columns=1, window_days=7
)
_RESOLVED = ResolvedSettings(settings=_SETTINGS, revision=1, checksum="x", valid=True)


@dataclass
class _Sub:
    key: str
    owner: str
    hotkey: str
    status: str = "active"
    qualified: bool = True
    submitted_hours_ago: float = 48.0
    status_reason: str | None = None


def _row(sub: _Sub, submission_id: UUID) -> GeneratorSubmission:
    raw = f"generator:{sub.key}".encode()
    return GeneratorSubmission(
        submission_id=submission_id,
        miner_hotkey=sub.hotkey,
        owner_key=sub.owner,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        tree_sha256=hashlib.sha256(raw + b":tree").hexdigest(),
        source_bytes=raw,
        size_bytes=len(raw),
        # A module per owner, so no two owners share a template family.
        module=f"example.com/{sub.owner.split(':')[-1]}",
        go_version="1.23",
        file_count=2,
        status=sub.status,
        status_reason=sub.status_reason,
        attempts=0,
        next_run_at=_NOW,
        submitted_at=_NOW - timedelta(hours=sub.submitted_hours_ago),
        updated_at=_NOW,
        qualified_at=_NOW - timedelta(days=8) if sub.qualified else None,
    )


def _round(
    index: int, scored_at: datetime, columns: list[tuple[str, UUID, _Sub, float, bool]]
) -> tuple[GeneratorRound, list[GeneratorRoundCaseVerdict]]:
    """A scored round with one verdict per column, enough for the ledger."""
    secret = hashlib.sha256(f"secret:{index}".encode()).digest()
    rnd = GeneratorRound(
        round_index=index,
        status="scored",
        epochs_per_round=1,
        first_epoch_index=index,
        secret=secret,
        commitment=hashlib.sha256(secret).hexdigest(),
        committed_at=scored_at - timedelta(hours=1),
        committed_epoch_index=index - 1,
        commitment_publication="disabled",
        seed="ab" * 32,
        columns=[
            {
                "column_id": column_id,
                "submission_id": str(submission_id),
                "miner_hotkey": sub.hotkey,
                "owner_key": sub.owner,
                "source_sha256": "0" * 64,
                "counted": counted,
                "public_default": False,
            }
            for column_id, submission_id, sub, _score, counted in columns
        ],
        score={
            "columns": [
                {
                    "column_id": column_id,
                    "pack": {"score": score, "discriminating": 3},
                }
                for column_id, _sid, _sub, score, _counted in columns
            ]
        },
        scored_at=scored_at,
    )
    verdicts = [
        GeneratorRoundCaseVerdict(
            round_index=index,
            column_id=column_id,
            case_id=f"{column_id}/case-0",
            submission_id=submission_id,
            position=0,
            fingerprint=f"fp:{index}:{column_id}",
            status="discriminating",
            panel_size=3,
            solvers=1,
            solver_owners=1,
            mean_score=0.5,
            champion_failed=True,
            credit=1.0,
        )
        for column_id, submission_id, _sub, _score, _counted in columns
    ]
    return rnd, verdicts


# The round-26124 board (SN-487), with the ticket's submission prefixes.
_SUBS = [
    # Round 26124's counted columns c0..c4 and challengers c5, c6.
    _Sub("33c60a4b-g10cand", "coldkey:g10cand", "5G10cand"),
    _Sub("c955422c-qv2", "coldkey:qv2", "5Qv2"),
    _Sub("ce635a13-bountygen", "coldkey:5HQk61qz", "5Dex4afD"),
    _Sub("663cf753-desearch", "coldkey:desearch", "5Desearch"),
    _Sub("a0b29a05-kinlist", "coldkey:5Gv89Pdw", "5KinListA"),
    _Sub("05e146d8-lh2gen", "coldkey:5DRYjGxd", "5CCrBmKy"),
    _Sub("70effa69-starter", "coldkey:starter", "5Starter"),
    # lh2gen's other hotkey: the scored row at board #1, superseded by an
    # active replacement the owner dedupe kept out of round 26124.
    _Sub(
        "79d8eb2a-lh2gen",
        "coldkey:5DRYjGxd",
        "5EyKrm2f",
        status="superseded",
        submitted_hours_ago=72,
        status_reason="superseded by 1074d277",
    ),
    _Sub("1074d277-lh2gen", "coldkey:5DRYjGxd", "5EyKrm2f", submitted_hours_ago=20),
    # bountygen's other hotkey: the same shape.
    _Sub(
        "22caa69d-bountygen",
        "coldkey:5HQk61qz",
        "5GTo8TLp",
        status="superseded",
        submitted_hours_ago=72,
    ),
    _Sub("0506250c-bountygen", "coldkey:5HQk61qz", "5GTo8TLp", submitted_hours_ago=20),
    # kin*: a row frozen by a self-withdraw upload, rejected generation_failed.
    _Sub(
        "eed24f51-kinlist",
        "coldkey:5Gv89Pdw",
        "5HGQghQp",
        status="superseded",
        submitted_hours_ago=72,
    ),
    _Sub(
        "f1944b00-kin-withdraw",
        "coldkey:5Gv89Pdw",
        "5HGQghQp",
        status="rejected",
        submitted_hours_ago=20,
        status_reason=(
            "generation_failed: seed 0: withdrawn by owner: this upload only "
            "replaces my entry on hotkey 5HGQghQp; it is not meant to run"
        ),
    ),
    # kin*: a row frozen behind an owner-blocked active replacement since
    # round 26090, almost seven days ago.
    _Sub(
        "69f12191-kin",
        "coldkey:5Gv89Pdw",
        "5KinFrozen",
        status="superseded",
        submitted_hours_ago=24 * 7,
    ),
    _Sub(
        "6a1f0000-kin-blocked", "coldkey:5Gv89Pdw", "5KinFrozen", submitted_hours_ago=20
    ),
    # A legitimate miner whose replacement failed a round pack ("invalid
    # pack"): its superseded row is no longer paid either.
    _Sub(
        "206867a5-sn118gen",
        "coldkey:5F22h7oh",
        "5HQsecKg",
        status="superseded",
        submitted_hours_ago=72,
    ),
    _Sub(
        "f21477a2-sn118gen",
        "coldkey:5F22h7oh",
        "5HQsecKg",
        status="rejected",
        submitted_hours_ago=20,
        status_reason="round_generation_failed: round 26114 column c6: invalid pack",
    ),
]

# Round 26124's counted columns and challengers, by key prefix.
_R26124 = [
    ("c0", "33c60a4b", 0.30, True),
    ("c1", "c955422c", 0.25, True),
    ("c2", "ce635a13", 0.05, True),
    ("c3", "663cf753", 0.20, True),
    ("c4", "a0b29a05", 0.10, True),
    ("c5", "05e146d8", 0.344, False),
    ("c6", "70effa69", 0.01, False),
]
# Older single-column rounds that scored the now-superseded rows.
_OLDER = [
    (26123, timedelta(hours=2), "79d8eb2a", 0.372),
    (26121, timedelta(hours=4), "22caa69d", 0.281),
    (26120, timedelta(hours=5), "eed24f51", 0.228),
    (26113, timedelta(hours=12), "206867a5", 0.065),
    (26090, timedelta(days=6, hours=21), "69f12191", 0.201),
]


async def _seed_board(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, tuple[UUID, _Sub]]:
    ids = {sub.key.split("-")[0]: (uuid4(), sub) for sub in _SUBS}
    async with maker() as session, session.begin():
        for submission_id, sub in ids.values():
            session.add(_row(sub, submission_id))
        await session.flush()
        for index, ago, prefix, score in _OLDER:
            sid, sub = ids[prefix]
            rnd, verdicts = _round(index, _NOW - ago, [("c0", sid, sub, score, True)])
            session.add(rnd)
            await session.flush()
            session.add_all(verdicts)
        rnd, verdicts = _round(
            26124,
            _NOW - timedelta(hours=1),
            [
                (column_id, *ids[prefix], score, counted)
                for column_id, prefix, score, counted in _R26124
            ],
        )
        session.add(rnd)
        await session.flush()
        session.add_all(verdicts)
        # Active generators carry their latest round score for selection.
        for _column_id, prefix, score, _counted in _R26124:
            row = await session.get(GeneratorSubmission, ids[prefix][0])
            assert row is not None
            row.round_score = score
            row.round_scored_index = 26124
    return ids


async def _ledger(maker: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with maker() as session:
        return (await build_round_ledger(session, resolved=_RESOLVED, now=_NOW)).entries


async def test_round_26124_board_pays_each_owner_once_for_its_latest_column(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed_board(session_maker)
    by_id = {sid: prefix for prefix, (sid, _sub) in ids.items()}
    entries = await _ledger(session_maker)

    # Before SN-487 the ledger also paid 79d8eb2a (board #1), 22caa69d,
    # eed24f51, 69f12191 and 206867a5: superseded rows nobody re-scores.
    assert sorted(by_id[e.submission_id] for e in entries) == sorted(
        prefix for _column_id, prefix, _score, _counted in _R26124
    )
    owners = [ids[by_id[e.submission_id]][1].owner for e in entries]
    assert len(owners) == len(set(owners)), owners
    assert all(e.round_index == 26124 for e in entries)
    assert all(e.weight_eligible for e in entries)
    # lh2gen is paid for the column it held (c5, 0.344), not its frozen 0.372.
    lh2 = next(e for e in entries if e.miner_hotkey == "5CCrBmKy")
    assert by_id[lh2.submission_id] == "05e146d8" and lh2.score == pytest.approx(0.344)
    assert not {e.miner_hotkey for e in entries} & {
        "5EyKrm2f",
        "5GTo8TLp",
        "5HGQghQp",
        "5KinFrozen",
        "5HQsecKg",
    }


async def test_the_next_round_seats_and_pays_the_same_owner_identity(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed_board(session_maker)
    by_id = {sid: prefix for prefix, (sid, _sub) in ids.items()}
    async with session_maker() as session:
        selection = await select_round_columns(session, _SETTINGS)
    columns = selection.columns
    seated = [column["owner_key"] for column in columns]
    assert len(seated) == len(set(seated)), seated
    # bountygen's waiting replacement takes a challenger column, so its
    # owner's column moves to hotkey 5GTo8TLp; nobody's superseded row is
    # seated (superseded rows are never selected).
    assert "0506250c" in {by_id[UUID(c["submission_id"])] for c in columns}

    # Score that round: every owner's paid entry follows its new column.
    async with session_maker() as session, session.begin():
        subs = dict(ids.values())
        rnd, verdicts = _round(
            26125,
            _NOW - timedelta(minutes=5),
            [
                (
                    column["column_id"],
                    UUID(column["submission_id"]),
                    subs[UUID(column["submission_id"])],
                    0.15,
                    column["counted"],
                )
                for column in columns
            ],
        )
        session.add(rnd)
        await session.flush()
        session.add_all(verdicts)
    entries = await _ledger(session_maker)
    paid = {ids[by_id[e.submission_id]][1].owner: e.submission_id for e in entries}
    for column in columns:
        assert paid[column["owner_key"]] == UUID(column["submission_id"]), column
    assert len(paid) == len(entries)


async def test_a_superseded_row_is_paid_only_while_its_replacement_is_active(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    old = _Sub(
        "old", "coldkey:solo", "5Solo", status="superseded", submitted_hours_ago=48
    )
    new = _Sub("new", "coldkey:solo", "5Solo", submitted_hours_ago=1)
    old_id, new_id = uuid4(), uuid4()
    async with session_maker() as session, session.begin():
        session.add_all([_row(old, old_id), _row(new, new_id)])
        await session.flush()
        rnd, verdicts = _round(
            500, _NOW - timedelta(hours=3), [("c0", old_id, old, 0.4, True)]
        )
        session.add(rnd)
        await session.flush()
        session.add_all(verdicts)

    # The grace: the newer upload is waiting for its first round.
    assert [e.submission_id for e in await _ledger(session_maker)] == [old_id]

    # The newer upload is rejected (here by an operator, so nothing is
    # restored): the superseded row could never be re-scored, so it is dropped.
    async with session_maker() as session, session.begin():
        row = await session.get(GeneratorSubmission, new_id)
        assert row is not None
        row.status = "rejected"
        row.status_reason = "operator (backroom:test): copy"
    assert await _ledger(session_maker) == []


async def test_a_withdraw_upload_rejected_on_its_own_run_restores_the_row(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The kin* freeze: a replacement made to fail its run."""
    scored_id, withdraw_id = uuid4(), uuid4()
    scored = _Sub(
        "scored",
        "coldkey:5Gv89Pdw",
        "5HGQghQp",
        status="superseded",
        submitted_hours_ago=48,
        status_reason=f"superseded by {withdraw_id}",
    )
    withdraw = _Sub(
        "withdraw",
        "coldkey:5Gv89Pdw",
        "5HGQghQp",
        qualified=False,
        submitted_hours_ago=1,
    )
    async with session_maker() as session, session.begin():
        session.add_all([_row(scored, scored_id), _row(withdraw, withdraw_id)])
        await session.flush()
        rnd, verdicts = _round(
            600, _NOW - timedelta(hours=3), [("c0", scored_id, scored, 0.228, True)]
        )
        session.add(rnd)
        await session.flush()
        session.add_all(verdicts)
        row = await session.get(GeneratorSubmission, scored_id)
        assert row is not None
        row.round_score, row.round_scored_index = 0.228, 600

    async with session_maker() as session, session.begin():
        row = await session.get(GeneratorSubmission, withdraw_id)
        assert row is not None
        await competition._reject(  # noqa: SLF001 - the scorer's reject path
            session,
            row,
            "generation_failed: seed 0: withdrawn by owner; it is not meant to run",
            now=_NOW,
        )

    async with session_maker() as session:
        restored = await session.get(GeneratorSubmission, scored_id)
        assert restored is not None
        assert restored.status == "active"
        assert restored.status_reason == restored_reason(withdraw_id)
        # It qualifies again from scratch before it can earn or be seated.
        assert restored.qualified_at is None
        assert restored.next_run_at is not None
    entries = await _ledger(session_maker)
    assert [(e.submission_id, e.weight_eligible) for e in entries] == [
        (scored_id, False)
    ]

    # Re-qualified, it is seated (and so re-scored) in the next round.
    async with session_maker() as session, session.begin():
        row = await session.get(GeneratorSubmission, scored_id)
        assert row is not None
        row.qualified_at = _NOW
    async with session_maker() as session:
        columns = (await select_round_columns(session, _SETTINGS)).columns
    assert [c["submission_id"] for c in columns] == [str(scored_id)]
    assert [e.weight_eligible for e in await _ledger(session_maker)] == [True]


async def test_restore_never_makes_a_second_active_submission(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    prior_id, rejected_id, other_id = uuid4(), uuid4(), uuid4()
    prior = _Sub(
        "prior",
        "coldkey:x",
        "5X",
        status="superseded",
        submitted_hours_ago=48,
        status_reason=f"superseded by {rejected_id}",
    )
    rejected = _Sub(
        "rejected", "coldkey:x", "5X", status="rejected", submitted_hours_ago=2
    )
    other = _Sub("other", "coldkey:x", "5X", submitted_hours_ago=1)
    async with session_maker() as session, session.begin():
        session.add_all(
            [_row(prior, prior_id), _row(rejected, rejected_id), _row(other, other_id)]
        )
    async with session_maker() as session, session.begin():
        row = await session.get(GeneratorSubmission, rejected_id)
        assert row is not None
        assert await restore_superseded(session, row, now=_NOW) is None
        assert (await session.get(GeneratorSubmission, prior_id)).status == "superseded"  # type: ignore[union-attr]


async def test_restore_yields_to_an_upload_holding_the_hotkey_lock(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An upload committing a new active row makes the restore moot; the
    reject never waits on it (that order could deadlock)."""
    prior_id, rejected_id = uuid4(), uuid4()
    prior = _Sub(
        "prior",
        "coldkey:y",
        "5Y",
        status="superseded",
        submitted_hours_ago=48,
        status_reason=f"superseded by {rejected_id}",
    )
    rejected = _Sub(
        "rejected", "coldkey:y", "5Y", status="rejected", submitted_hours_ago=2
    )
    async with session_maker() as session, session.begin():
        session.add_all([_row(prior, prior_id), _row(rejected, rejected_id)])
    async with session_maker() as upload, upload.begin():
        await upload.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "generator-upload:5Y"},
        )
        async with session_maker() as session, session.begin():
            row = await session.get(GeneratorSubmission, rejected_id)
            assert row is not None
            assert await restore_superseded(session, row, now=_NOW) is None
    async with session_maker() as session, session.begin():
        row = await session.get(GeneratorSubmission, rejected_id)
        assert row is not None
        assert await restore_superseded(session, row, now=_NOW) == prior_id


async def test_a_restore_skipped_for_a_racing_upload_is_recovered_on_its_reject(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A is superseded by S. S is rejected while upload U holds the hotkey
    lock, so the restore is skipped and U commits active, superseding nothing.
    When U is rejected in turn, A (still "superseded by S") is restored and
    becomes runnable again."""
    a_id, s_id, u_id = uuid4(), uuid4(), uuid4()
    a = _Sub(
        "a",
        "coldkey:z",
        "5Z",
        status="superseded",
        submitted_hours_ago=48,
        status_reason=f"superseded by {s_id}",
    )
    s = _Sub("s", "coldkey:z", "5Z", qualified=False, submitted_hours_ago=2)
    async with session_maker() as session, session.begin():
        session.add_all([_row(a, a_id), _row(s, s_id)])
    async with session_maker() as upload, upload.begin():
        await upload.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "generator-upload:5Z"},
        )
        async with session_maker() as session, session.begin():
            row = await session.get(GeneratorSubmission, s_id)
            assert row is not None
            await competition._reject(  # noqa: SLF001 - the scorer's reject path
                session, row, "build_failed: x", now=_NOW
            )
        # The upload commits U; S is no longer active, so U supersedes nothing.
        upload.add(
            _row(
                _Sub("u", "coldkey:z", "5Z", qualified=False, submitted_hours_ago=1),
                u_id,
            )
        )
    async with session_maker() as session:
        skipped = await session.get(GeneratorSubmission, a_id)
        assert skipped is not None and skipped.status == "superseded"

    async with session_maker() as session, session.begin():
        row = await session.get(GeneratorSubmission, u_id)
        assert row is not None
        await competition._reject(  # noqa: SLF001 - the scorer's reject path
            session, row, "generation_failed: seed 0: withdrawn by owner", now=_NOW
        )
    async with session_maker() as session:
        restored = await session.get(GeneratorSubmission, a_id)
        assert restored is not None and restored.status == "active"
        assert restored.status_reason == restored_reason(u_id)
        assert restored.qualified_at is None
        assert restored.next_run_at is not None and restored.next_run_at <= _NOW
