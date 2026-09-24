"""The operator read explains an exact generation without granting work.

The fixtures here reproduce the live shape from #2105: a retested incumbent
whose 15 shared seeds carry it into the OFFICIAL top five while its canonical
median sits outside the canonical top five, and a newer same-owner generation
whose canonical median beats the incumbent's but which holds no shared-seed
evidence at all. Everything except the database reads is the production code
path -- ``_current_retest_cohort`` runs for real over the snapshot, so the
cohort, the cutoff, and the tie band in these assertions are the ones the
issuance lane would apply.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.continual_retest_settings import ContinualRetestSettings
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.endpoints import admin_leaderboard
from ditto.api_server.endpoints.validator import _KothLaneSnapshot
from ditto.api_server.koth import KothEntry, continual_composite
from ditto.chain import ChainClient

pytestmark = pytest.mark.asyncio

_BENCH_VERSION = 13
_NOW = datetime(2026, 9, 22, 23, 51, tzinfo=UTC)
# Deliberately int63-scale: the wire contract keeps seeds as decimal strings.
_SEEDS = tuple(9_223_372_036_854_775_000 + index for index in range(15))
_INCUMBENT = UUID("7a2ca671-788d-4b18-b6bf-799cc450680f")
_SUCCESSOR = UUID("01f5373c-c626-4271-ac68-348bd52510d3")
_OWNER = "owner:gryffindor"
_RIVAL_IDS = tuple(
    UUID(
        f"{index}{index}{index}{index}{index}{index}{index}{index}-1111-4111-8111-111111111111"
    )
    for index in range(1, 6)
)
_RIVAL_CANONICAL = (0.90, 0.88, 0.86, 0.84, 0.82)


def _entry(
    agent_id: UUID,
    *,
    hotkey: str,
    canonical: float,
    rank: int,
    first_seen: datetime,
    waves: tuple[float, ...] = (),
) -> KothEntry:
    return KothEntry(
        miner_hotkey=hotkey,
        agent_id=agent_id,
        composite=canonical,
        first_seen=first_seen,
        raw_rank=rank,
        bench_version=_BENCH_VERSION,
        composite_stderr=0.01,
        quorum_composites=(canonical, canonical, canonical),
        completed_wave_composites=waves or None,
    )


def _snapshot(*, challenger: bool) -> _KothLaneSnapshot:
    """The #2105 ledger shape, with or without the same-owner challenger path.

    ``challenger=False`` is the pre-#2106 behaviour and the exact question the
    issue asked: what happens to a stronger newer generation that neither the
    folded cohort nor the canonical wave can see.
    """
    incumbent = _entry(
        _INCUMBENT,
        hotkey="5Gryffindor",
        canonical=0.479032,
        rank=7,
        # Earliest lineage clock: the fold's champion walk starts here.
        first_seen=_NOW - timedelta(days=10),
        waves=(0.62,) * 15,
    )
    successor = _entry(
        _SUCCESSOR,
        hotkey="5Gryffindor",
        canonical=0.540720,
        rank=6,
        first_seen=_NOW - timedelta(hours=1),
    )
    rivals = [
        _entry(
            agent_id,
            hotkey=f"5Rival{index}",
            canonical=canonical,
            rank=index,
            first_seen=_NOW - timedelta(days=9 - index),
            waves=(0.20,) * 15,
        )
        for index, (agent_id, canonical) in enumerate(
            zip(_RIVAL_IDS, _RIVAL_CANONICAL, strict=True), start=1
        )
    ]
    all_entries = (incumbent, successor, *rivals)
    return _KothLaneSnapshot(
        # Owner dedupe on OFFICIAL scores keeps the incumbent, so the newer
        # generation is not in the folded pool the cohort is built from at all.
        folded_entries=[incumbent, *rivals],
        # Owner dedupe on CANONICAL scores picks the successor for this owner,
        # but 0.5407 is only canonical rank six, so the raw wave excludes it.
        raw_emission=tuple(rivals),
        all_entries=all_entries,
        official_scores={
            entry.agent_id: continual_composite(entry) for entry in all_entries
        },
        canonical_scores={entry.agent_id: entry.composite for entry in all_entries},
        owner_representatives={
            _OWNER: _INCUMBENT,
            **{f"agent:{entry.agent_id}": entry.agent_id for entry in rivals},
        },
        owner_by_agent={
            _INCUMBENT: _OWNER,
            _SUCCESSOR: _OWNER,
            **{entry.agent_id: f"agent:{entry.agent_id}" for entry in rivals},
        },
        folded_seeds_by_agent={
            _INCUMBENT: _SEEDS,
            _SUCCESSOR: (),
            **{entry.agent_id: _SEEDS for entry in rivals},
        },
        owner_challengers=((_INCUMBENT, successor),) if challenger else (),
    )


def _request() -> Request:
    return cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    session_maker=object(),
                    config=SimpleNamespace(
                        top5_backoff_base=2,
                        top5_backoff_doubling_tempos=20,
                        top5_backoff_cap=8,
                    ),
                    continual_retest_settings=SimpleNamespace(
                        resolve=AsyncMock(
                            return_value=ContinualRetestSettings(
                                retest_cohort_size=5,
                                idle_retests_enabled=False,
                            )
                        )
                    ),
                    efficiency_settings=SimpleNamespace(
                        resolve=AsyncMock(return_value=None)
                    ),
                )
            )
        ),
    )


def _session(*, ticket_rows: list[tuple[object, ...]]) -> AsyncSession:
    agent = SimpleNamespace(
        status=SimpleNamespace(value="scored"),
        miner_hotkey="5Gryffindor",
        created_at=_NOW - timedelta(hours=1),
        dataset_seed_block=1_000,
    )
    return cast(
        AsyncSession,
        SimpleNamespace(
            get=AsyncMock(return_value=agent),
            execute=AsyncMock(side_effect=[ticket_rows, [(0.5412, _NOW)]]),
        ),
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: _KothLaneSnapshot,
    catchup: frozenset[UUID],
    pending_seeds: tuple[int, ...] = (_SEEDS[0], _SEEDS[1]),
    leases: dict[str, int | None] | None = None,
    least_covered: bool = True,
) -> None:
    """Stub only the database and chain edges; the fold logic stays real."""
    monkeypatch.setattr(
        admin_leaderboard,
        "active_bench_version",
        AsyncMock(return_value=_BENCH_VERSION),
    )
    monkeypatch.setattr(
        admin_leaderboard, "_current_koth_entries", AsyncMock(return_value=snapshot)
    )
    monkeypatch.setattr(
        admin_leaderboard,
        "confirmation_composites_by_seed",
        AsyncMock(
            return_value={_SUCCESSOR: {}, _INCUMBENT: dict.fromkeys(_SEEDS, 0.62)}
        ),
    )
    monkeypatch.setattr(
        admin_leaderboard,
        "read_reign_seed_anchor",
        AsyncMock(return_value=SimpleNamespace(anchor_block=123456, pinned=True)),
    )
    monkeypatch.setattr(
        admin_leaderboard, "_canonical_tail_is_draining", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        admin_leaderboard, "_unserved_catchup_members", AsyncMock(return_value=catchup)
    )
    monkeypatch.setattr(
        admin_leaderboard,
        "_top5_confirmation_seed_plan",
        AsyncMock(return_value=pending_seeds),
    )
    monkeypatch.setattr(
        admin_leaderboard,
        "_live_retest_leases",
        AsyncMock(return_value={_SUCCESSOR: leases} if leases else {}),
    )
    monkeypatch.setattr(
        admin_leaderboard,
        "_top5_member_is_least_covered",
        AsyncMock(return_value=least_covered),
    )
    monkeypatch.setattr(
        admin_leaderboard,
        "miner_has_newer_canonical_work",
        AsyncMock(return_value=False),
    )


def _chain(block: int = 1_000) -> object:
    return SimpleNamespace(
        get_latest_block=AsyncMock(return_value=SimpleNamespace(number=block))
    )


async def test_exact_agent_diagnostic_preserves_seeds_and_owner_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(challenger=True)
    _install(monkeypatch, snapshot=snapshot, catchup=frozenset({_SUCCESSOR}))
    live_deadline = datetime.now(UTC) + timedelta(minutes=5)
    session = _session(
        ticket_rows=[
            (TicketStatus.ISSUED, live_deadline, _NOW, "5Val1", None),
            (
                TicketStatus.SCORED,
                _NOW - timedelta(minutes=5),
                _NOW - timedelta(minutes=5),
                "5Val2",
                None,
            ),
        ]
    )
    response = Response()
    result = await admin_leaderboard.continual_retest_diagnostic(
        _request(), response, session, cast(ChainClient, _chain()), None, _SUCCESSOR
    )

    assert response.headers["Cache-Control"] == "no-store"
    assert result.active_bench_version == _BENCH_VERSION
    assert result.canonical_composite == pytest.approx(0.540720)
    assert result.official_composite == pytest.approx(0.540720)
    assert result.owner_representative_id == _INCUMBENT
    assert result.owner_key == _OWNER
    assert [member.agent_id for member in result.family] == [_SUCCESSOR, _INCUMBENT]
    representative = result.family[1]
    assert representative.representative
    assert representative.completed_wave_depth == 15
    assert result.completed_wave_depth == 0
    assert result.canonical_sample_count == 3
    assert result.official_sample_count == 3
    assert result.raw_confirmation_depth == 0
    assert result.ledger_eligible
    assert result.aggregate_mode == "fleet_ready"
    assert result.wave_membership == "participants"

    # The incumbent's 15 folded seeds, not a better submission, hold the slot.
    assert result.representative_official_composite == pytest.approx(0.5965, abs=1e-4)
    assert result.representative_margin is not None
    assert result.representative_margin > 0
    assert result.representative_selection == "official_composite"

    assert not result.in_raw_wave
    assert not result.in_emission_set
    assert result.in_retest_cohort
    assert result.is_same_owner_challenger
    assert result.admission_reason == "same_owner_challenger"
    assert result.cohort_size == 7
    assert result.cohort_position == 7
    assert result.configured_cohort_size == 5
    assert result.seed_anchor_champion_id == _RIVAL_IDS[0]
    assert result.seed_anchor_pinned

    assert result.latest_ticket_status == "issued"
    assert result.latest_ticket_validator_hotkey == "5Val1"
    assert result.ticket_status_counts == {"issued": 1, "scored": 1}
    assert result.active_ticket_count == 1
    assert result.terminal_ticket_count == 1
    assert result.latest_confirmation_composite == pytest.approx(0.5412)
    assert result.latest_confirmation_recorded_at == _NOW

    claim = result.claim
    assert claim is not None
    assert claim.lane_enabled
    assert claim.latest_block == 1_000
    assert claim.champion_agent_id == _RIVAL_IDS[0]
    assert claim.in_catchup_set
    assert claim.route_priority == "catchup"
    assert claim.route_position == 2
    assert claim.pending_seed_count == 2
    assert claim.claimable_seed_available
    assert claim.live_lease_count == 0
    assert claim.decision == "claimable"
    # Outstanding work is a count. No seed value reaches the claim projection.
    assert all(
        str(seed) not in claim.model_dump_json() for seed in (_SEEDS[0], _SEEDS[1])
    )


async def test_stronger_successor_is_outside_the_cohort_it_outscores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exclusion is structural, not a cutoff the successor failed.

    Without the same-owner challenger path the newer generation is absent from
    both the folded cohort and the canonical wave, so it can never earn the
    shared-seed evidence its owner's representative is selected on -- even
    though its official composite is above the cohort cutoff. That is the
    admission loop #2105 asked to be able to see, and it resolves only through
    the bounded challenger admission, never through the score comparison.
    """
    snapshot = _snapshot(challenger=False)
    _install(monkeypatch, snapshot=snapshot, catchup=frozenset())
    result = await admin_leaderboard.continual_retest_diagnostic(
        _request(),
        Response(),
        _session(ticket_rows=[]),
        cast(ChainClient, _chain()),
        None,
        _SUCCESSOR,
    )

    assert not result.in_raw_wave
    assert not result.in_emission_set
    assert not result.in_retest_cohort
    assert not result.is_same_owner_challenger
    assert result.admission_reason == "owner_suppressed"
    assert result.cohort_size == 6
    assert result.cohort_position is None
    assert result.ticket_status_counts == {}
    assert result.latest_ticket_status is None

    # It beats the cutoff it is excluded behind: the gap is negative and inside
    # the band, so no score comparison explains the exclusion.
    assert result.cohort_cutoff.agent_id == _RIVAL_IDS[3]
    assert result.cohort_cutoff.gap is not None
    assert result.cohort_cutoff.gap < 0
    assert result.cohort_cutoff.within_tie_band
    assert result.emission_cutoff.gap is not None
    assert result.emission_cutoff.gap < 0
    assert result.claim is not None
    assert result.claim.decision == "not_in_cohort"
    assert result.claim.route_priority == "not_routed"
    assert result.claim.route_position is None

    # The one-owner emission rule is untouched by either answer.
    incumbent_result = await admin_leaderboard.continual_retest_diagnostic(
        _request(),
        Response(),
        _session(ticket_rows=[]),
        cast(ChainClient, _chain()),
        None,
        _INCUMBENT,
    )
    assert incumbent_result.in_emission_set
    assert incumbent_result.completed_wave_depth == 15
    assert incumbent_result.official_sample_count == 18
    assert incumbent_result.representative_selection == "self"
    assert incumbent_result.representative_margin == 0


def _decide(
    *,
    lane_enabled: bool = True,
    in_cohort: bool = True,
    scheduled_round: bool | None = False,
    spare_capacity: bool = False,
    idle_retests_enabled: bool = False,
    in_catchup: bool = True,
    pending_seed_count: int = 2,
    claimable_seed_available: bool = True,
    newer_canonical_work_pending: bool = False,
    least_covered_admitted: bool | None = True,
) -> str:
    """Call the lane-order decision with one gate flipped at a time."""
    return admin_leaderboard._claim_decision(
        lane_enabled=lane_enabled,
        in_cohort=in_cohort,
        scheduled_round=scheduled_round,
        spare_capacity=spare_capacity,
        idle_retests_enabled=idle_retests_enabled,
        in_catchup=in_catchup,
        pending_seed_count=pending_seed_count,
        claimable_seed_available=claimable_seed_available,
        newer_canonical_work_pending=newer_canonical_work_pending,
        least_covered_admitted=least_covered_admitted,
    )


async def test_claim_decision_follows_the_lane_gate_order() -> None:
    assert _decide() == "claimable"
    assert _decide(lane_enabled=False) == "lane_disabled"
    assert _decide(in_cohort=False) == "not_in_cohort"
    assert _decide(scheduled_round=None) == "chain_unavailable"
    assert _decide(in_catchup=False) == "round_not_due"
    assert _decide(spare_capacity=True, in_catchup=False) == "claimable"
    assert _decide(newer_canonical_work_pending=True) == "newer_canonical_work_pending"
    assert _decide(pending_seed_count=0) == "no_pending_seeds"
    assert _decide(claimable_seed_available=False) == "all_pending_seeds_leased"
    assert _decide(least_covered_admitted=False) == "another_member_less_covered"
    # A due round overrides the idle gate and the newer-work deferral alike.
    assert (
        _decide(
            scheduled_round=True,
            in_catchup=False,
            newer_canonical_work_pending=True,
        )
        == "claimable"
    )
