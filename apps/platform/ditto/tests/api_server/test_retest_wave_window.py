"""Tempo-aligned wave pacing for continual retests (``wave_cadence_tempos``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.retest_wave_window import (
    WaveWindow,
    current_wave_window,
    first_retest_issue_by_seed,
    growth_open,
    seeds_opened_in_window,
)
from ditto.db.models import (
    Agent,
    ConfirmationScore,
    LedgerEpochSnapshot,
    ValidatorTicket,
)

_NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _snapshot(epoch_index: int, pinned_at: datetime) -> LedgerEpochSnapshot:
    return LedgerEpochSnapshot(
        snapshot_id=uuid4(),
        netuid=118,
        epoch_index=epoch_index,
        last_epoch_block=epoch_index * 361,
        pinned_block=epoch_index * 361 + 2,
        pinned_block_hash="0x" + "ab" * 32,
        pinned_at=pinned_at,
        bench_version=14,
        entries=[],
        context={},
        ledger_digest="cd" * 32,
    )


def test_cadence_zero_is_the_historical_pacing() -> None:
    settings = ContinualRetestSettings(aggregate_mode="fleet_ready")
    assert settings.wave_cadence_tempos == 0
    assert settings.wave_seeds_per_window == 3
    unpaced = WaveWindow(0, 3, None, None, None)
    assert not unpaced.paced
    assert growth_open(unpaced, next_seed=7, first_issue={1: _NOW, 2: _NOW, 3: _NOW})


def test_a_spent_window_holds_fresh_seeds_but_never_an_open_wave() -> None:
    start = _NOW - timedelta(hours=1)
    window = WaveWindow(4, 2, 26089, 26088, start)
    before = start - timedelta(hours=3)
    first_issue = {11: before, 12: start + timedelta(minutes=5)}
    # One fresh seed opened this window (12); 11 belongs to an earlier window.
    assert seeds_opened_in_window(window, first_issue) == frozenset({12})
    assert growth_open(window, next_seed=13, first_issue=first_issue)
    first_issue[13] = start + timedelta(minutes=20)
    assert not growth_open(window, next_seed=14, first_issue=first_issue)
    # A seed already leased keeps growing so its wave can finish.
    assert growth_open(window, next_seed=13, first_issue=first_issue)
    assert window.next_window_epoch == 26092


@pytest.mark.asyncio
async def test_window_starts_at_the_first_pin_of_its_epoch_grid(
    session: AsyncSession,
) -> None:
    # Epochs 26086..26090; cadence 4 puts the current window at 26088.
    for offset, epoch in enumerate(range(26086, 26091)):
        session.add(_snapshot(epoch, _NOW + timedelta(minutes=72 * offset)))
    await session.flush()

    window = await current_wave_window(session, cadence_tempos=4, seeds_per_window=3)

    assert window.latest_epoch == 26090
    assert window.window_epoch == 26088
    assert window.window_start == _NOW + timedelta(minutes=144)
    assert window.paced


@pytest.mark.asyncio
async def test_no_ledger_pin_fails_open(session: AsyncSession) -> None:
    window = await current_wave_window(session, cadence_tempos=4, seeds_per_window=3)
    assert window.window_start is None and not window.paced
    assert growth_open(window, next_seed=1, first_issue={2: _NOW, 3: _NOW, 4: _NOW})


@pytest.mark.asyncio
async def test_first_issue_is_lane_wide_and_survives_ticket_reuse(
    session: AsyncSession,
) -> None:
    """Seed first-seen times come from durable scores plus live leases, for
    every agent at the version, so a reused ticket row or a member leaving the
    cohort cannot make an opened seed look fresh."""
    agents = [
        Agent(
            agent_id=uuid4(),
            miner_hotkey=f"miner-{index}",
            name=f"wave-window-{index}",
            sha256=f"{index:02x}" * 32,
            status=AgentStatus.SCORED,
            created_at=_NOW,
        )
        for index in range(2)
    ]
    session.add_all(agents)
    await session.flush()

    def ticket(agent: Agent, seed: int, issued: datetime, **overrides: object):
        fields: dict[str, object] = {
            "agent_id": agent.agent_id,
            "validator_hotkey": f"validator-{uuid4().hex[:6]}",
            "slot_id": "slot-0",
            "status": TicketStatus.ISSUED,
            "issued_at": issued,
            "deadline": issued + timedelta(minutes=30),
            "bench_version": 14,
            "attempt_count": 1,
            "seed": seed,
            "purpose": TicketPurpose.CONTINUAL_RETEST,
        }
        fields.update(overrides)
        return ValidatorTicket(**fields)

    session.add_all(
        [
            # Seed 5 was scored early; its ticket row was later reused for 9.
            ConfirmationScore(
                agent_id=agents[0].agent_id,
                validator_hotkey="validator-a",
                bench_version=14,
                seed=5,
                composite=0.9,
                run_id="run-5",
                created_at=_NOW,
            ),
            ticket(agents[0], 9, _NOW + timedelta(hours=3)),
            # Seed 6 lives only on another agent (one no longer in the cohort).
            ticket(agents[1], 6, _NOW + timedelta(hours=1)),
            # Other versions and lanes never count.
            ticket(agents[1], 7, _NOW, bench_version=13),
            ticket(agents[1], 8, _NOW, purpose=TicketPurpose.CANONICAL_QUORUM),
        ]
    )
    await session.flush()

    # Since before everything: every seed with lane activity is a candidate.
    first = await first_retest_issue_by_seed(
        session, bench_version=14, since=_NOW - timedelta(days=1)
    )

    assert set(first) == {5, 6, 9}
    assert first[5].replace(tzinfo=UTC) == _NOW
    assert first[6].replace(tzinfo=UTC) == _NOW + timedelta(hours=1)


@pytest.mark.asyncio
async def test_window_is_scoped_to_the_lane_netuid(session: AsyncSession) -> None:
    session.add(_snapshot(26090, _NOW))
    other = _snapshot(30001, _NOW + timedelta(hours=9))
    other.netuid = 999
    session.add(other)
    await session.flush()

    window = await current_wave_window(
        session, cadence_tempos=4, seeds_per_window=3, netuid=118
    )

    assert window.latest_epoch == 26090 and window.window_epoch == 26088
    assert window.window_start == _NOW


@pytest.mark.asyncio
async def test_first_issue_reads_only_seeds_active_since_the_window(
    session: AsyncSession,
) -> None:
    agent = Agent(
        agent_id=uuid4(),
        miner_hotkey="miner-window",
        name="wave-window-since",
        sha256="aa" * 32,
        status=AgentStatus.SCORED,
        created_at=_NOW,
    )
    session.add(agent)
    await session.flush()
    for seed, created in ((1, _NOW), (2, _NOW + timedelta(hours=5))):
        session.add(
            ConfirmationScore(
                agent_id=agent.agent_id,
                validator_hotkey=f"validator-{seed}",
                bench_version=14,
                seed=seed,
                composite=0.9,
                run_id=f"run-{seed}",
                created_at=created,
            )
        )
    await session.flush()
    since = _NOW + timedelta(hours=4)

    first = await first_retest_issue_by_seed(
        session, bench_version=14, since=since, include=(1, 3)
    )

    # Seed 1 is old but named; seed 2 is active in the window; 3 never ran.
    assert set(first) == {1, 2}
    assert first[1].replace(tzinfo=UTC) == _NOW
