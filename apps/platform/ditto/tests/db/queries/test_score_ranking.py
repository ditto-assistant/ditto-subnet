"""The ordering is canonical, or it is not an ordering.

Every surface that ranks submissions -- the public board's ``rank``, the
validator ledger, the KOTH fold, the queue's score floors, the efficiency
lineage dedupe -- reads :mod:`ditto.score_order`. These tests exist because the
previous arrangement (each surface restating the comparator, and two of them
disagreeing about which number "the score" is) told a miner he was below a
"fifth place" that named a different agent than the board did.

Three things are pinned here:

1. The comparator's tiebreaks, including the cases that only matter when two
   rows are otherwise identical.
2. That the SQL form and the Python form agree row for row. They are the same
   rule written twice, in two languages, and that is exactly the drift this
   module exists to prevent -- so it is asserted, not assumed.
3. That the queue's continuation floor is cut on ``official_composite``, the
   same score the board ranks by, on a board where the two candidate keys
   invert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_server.config import EfficiencyBonusConfig
from ditto.api_server.efficiency import epoch_index_for
from ditto.db.models import Agent, ValidatorHeartbeat, ValidatorTicket
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.confirmation_scores import (
    ConfirmationSeedScore,
    append_confirmation_scores,
)
from ditto.db.queries.score_ranking import (
    EfficiencyFactorRequesterNotReady,
    dedupe_owner_rows,
    official_composites,
    resolve_efficiency_adjustments,
    resolve_official_composites,
)
from ditto.db.queries.scores import LedgerRow, list_eligible_ledger, upsert_score
from ditto.db.queries.tickets import (
    get_score_priority_floor_rows,
    score_priority_floor_rows_from_resolved_ledger,
)
from ditto.score_order import (
    rank_submissions,
    ranking_first_seen,
    score_order_key,
    select_owner_representative,
)

_BENCH = MIN_SCOREABLE_BENCH_VERSION
_BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_VALIDATORS = (
    "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
    "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm",
)


@dataclass(frozen=True)
class _Row:
    """The smallest thing the comparator will accept."""

    agent_id: UUID
    first_seen: datetime
    composite: float
    eligible: bool = True


@dataclass(frozen=True)
class _CrownRow:
    agent_id: UUID
    miner_hotkey: str
    first_seen: datetime
    composite: float
    bench_version: int
    emission_owner_root: str
    crown_first_seen: datetime | None = None
    eligible: bool = True

    @property
    def fold_first_seen(self) -> datetime:
        return self.crown_first_seen or self.first_seen


@dataclass(frozen=True)
class _FinalRow:
    agent_id: UUID
    miner_hotkey: str
    first_seen: datetime
    composite: float
    bench_version: int = 7
    v9_confirmation: dict[str, int] | None = None
    emission_owner_root: str | None = None
    eligible: bool = True


def _uuid(nibble: str) -> UUID:
    return UUID(nibble * 32)


class TestComparator:
    def test_ranks_by_score_descending(self) -> None:
        low = _Row(_uuid("1"), _BASE, 0.10)
        high = _Row(_uuid("2"), _BASE, 0.90)
        assert rank_submissions([low, high]) == [high, low]

    def test_factor_adjusted_score_selects_the_leaner_owner_generation(self) -> None:
        earlier_expensive = _FinalRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
            emission_owner_root="coldkey:owner-a",
        )
        later_lean = _FinalRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=1),
            composite=0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
            emission_owner_root="coldkey:owner-a",
        )

        selected = dedupe_owner_rows(
            [earlier_expensive, later_lean],
            scores={
                earlier_expensive.agent_id: 0.8 * 0.85,
                later_lean.agent_id: 0.8 * 1.10,
            },
        )

        assert selected == [later_lean]

    def test_factor_recomputes_owner_crown_before_first_seen_tiebreak(self) -> None:
        early_expensive = _CrownRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.9,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        later_lean = _CrownRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=2),
            composite=0.9,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            # This is the pre-factor SQL anchor and must not survive.
            crown_first_seen=_BASE,
        )

        [winner] = dedupe_owner_rows(
            [early_expensive, later_lean],
            scores={
                early_expensive.agent_id: 0.9 * 0.85,
                later_lean.agent_id: 0.9 * 1.10,
            },
        )

        assert winner.agent_id == later_lean.agent_id
        assert winner.fold_first_seen == later_lean.first_seen

    def test_a_worse_resubmission_never_represents_the_owner(self) -> None:
        strong = _CrownRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        weak_and_newer = _CrownRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=1),
            composite=0.80,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE + timedelta(days=1),
        )

        [winner] = dedupe_owner_rows(
            [strong, weak_and_newer],
            scores={strong.agent_id: 0.90, weak_and_newer.agent_id: 0.80},
        )

        assert winner.agent_id == strong.agent_id
        assert winner.composite == pytest.approx(0.90)

    def test_a_tied_resubmission_is_shown_and_keeps_the_crown(self) -> None:
        first = _CrownRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        later = _CrownRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=1),
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )

        [winner] = dedupe_owner_rows(
            [first, later],
            scores={first.agent_id: 0.90, later.agent_id: 0.90},
        )

        assert winner.agent_id == later.agent_id
        assert winner.fold_first_seen == _BASE
        assert select_owner_representative([first, later])[0].agent_id == later.agent_id

    def test_equal_quality_keeps_the_earlier_crown_across_efficiency(self) -> None:
        early_expensive = _CrownRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        later_lean = _CrownRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=2),
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )

        [winner] = dedupe_owner_rows(
            [early_expensive, later_lean],
            scores={
                early_expensive.agent_id: 0.90,
                later_lean.agent_id: 0.90,
            },
            secondary_scores={
                early_expensive.agent_id: 0.90 * 0.85,
                later_lean.agent_id: 0.90 * 1.10,
            },
        )

        assert winner.agent_id == later_lean.agent_id
        assert winner.fold_first_seen == _BASE

    def test_a_marginal_quality_improvement_keeps_the_crown(self) -> None:
        early = _CrownRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.8991,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        later = _CrownRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=2),
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE + timedelta(days=2),
        )

        [winner] = dedupe_owner_rows(
            [early, later],
            scores={early.agent_id: 0.8991, later.agent_id: 0.90},
            secondary_scores={early.agent_id: 0.8991, later.agent_id: 0.90},
        )

        assert winner.agent_id == later.agent_id
        assert winner.fold_first_seen == _BASE

    def test_a_sub_dethrone_improvement_keeps_the_crown(self) -> None:
        """0.889 -> 0.893 is inside KOTH_MARGIN; iterating must not reset the clock."""
        early = _CrownRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.889,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        later = _CrownRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=2),
            composite=0.893,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE + timedelta(days=2),
        )

        [winner] = dedupe_owner_rows(
            [early, later],
            scores={early.agent_id: 0.889, later.agent_id: 0.893},
            secondary_scores={early.agent_id: 0.889, later.agent_id: 0.893},
        )

        assert winner.agent_id == later.agent_id
        assert winner.fold_first_seen == _BASE

    def test_a_massive_quality_jump_resets_the_crown(self) -> None:
        early = _CrownRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.50,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        later = _CrownRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=2),
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE + timedelta(days=2),
        )

        [winner] = dedupe_owner_rows(
            [early, later],
            scores={early.agent_id: 0.50, later.agent_id: 0.90},
            secondary_scores={early.agent_id: 0.50, later.agent_id: 0.90},
        )

        assert winner.agent_id == later.agent_id
        assert winner.fold_first_seen == later.first_seen

    def test_owners_rank_by_crown_not_the_winning_upload(self) -> None:
        early = _CrownRow(
            agent_id=_uuid("0"),
            miner_hotkey="5" + "A" * 47,
            first_seen=_BASE,
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        iterating = _CrownRow(
            agent_id=_uuid("1"),
            miner_hotkey="5" + "C" * 47,
            first_seen=_BASE + timedelta(days=2),
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-a",
            crown_first_seen=_BASE,
        )
        later_rival = _CrownRow(
            agent_id=_uuid("2"),
            miner_hotkey="5" + "B" * 47,
            first_seen=_BASE + timedelta(days=1),
            composite=0.90,
            bench_version=9,
            emission_owner_root="coldkey:owner-b",
            crown_first_seen=_BASE + timedelta(days=1),
        )

        ranked = dedupe_owner_rows(
            [early, iterating, later_rival],
            scores={
                early.agent_id: 0.90,
                iterating.agent_id: 0.90,
                later_rival.agent_id: 0.90,
            },
        )

        assert [row.emission_owner_root for row in ranked] == [
            "coldkey:owner-a",
            "coldkey:owner-b",
        ]
        assert ranked[0].agent_id == iterating.agent_id
        assert ranked[0].fold_first_seen == _BASE
        assert ranking_first_seen(later_rival) == _BASE + timedelta(days=1)

    def test_a_score_tie_is_broken_by_the_earlier_first_seen(self) -> None:
        later = _Row(_uuid("2"), _BASE + timedelta(days=1), 0.50)
        earlier = _Row(_uuid("1"), _BASE, 0.50)
        assert rank_submissions([later, earlier]) == [earlier, later]

    def test_a_first_seen_tie_is_broken_by_the_lower_agent_id(self) -> None:
        high_id = _Row(_uuid("f"), _BASE, 0.50)
        low_id = _Row(_uuid("0"), _BASE, 0.50)
        assert rank_submissions([high_id, low_id]) == [low_id, high_id]

    def test_an_unranked_row_sorts_below_every_ranked_row(self) -> None:
        """A smoke run can carry a high composite; it can never outrank a real
        one. The eligibility term leads the key precisely so that an inflated
        12-case run cannot take a leaderboard position or hold a queue floor."""
        smoke = _Row(_uuid("1"), _BASE, 0.99, eligible=False)
        real = _Row(_uuid("2"), _BASE, 0.10)
        assert rank_submissions([smoke, real]) == [real, smoke]

    def test_an_override_score_replaces_the_stored_composite(self) -> None:
        """``official_composite`` is supplied per agent, not stored on the row."""
        median_leader = _Row(_uuid("1"), _BASE, 0.90)
        mean_leader = _Row(_uuid("2"), _BASE, 0.80)
        official = {median_leader.agent_id: 0.75, mean_leader.agent_id: 0.875}
        assert rank_submissions([median_leader, mean_leader], scores=official) == [
            mean_leader,
            median_leader,
        ]

    def test_rows_without_an_eligible_flag_are_treated_as_ranked(self) -> None:
        """``KothEntry`` carries no eligibility flag -- the fold filters first."""

        @dataclass(frozen=True)
        class _Bare:
            agent_id: UUID
            first_seen: datetime
            composite: float

        low = _Bare(_uuid("1"), _BASE, 0.10)
        high = _Bare(_uuid("2"), _BASE, 0.90)
        assert rank_submissions([low, high]) == [high, low]

    def test_the_key_is_a_plain_tuple_so_it_composes(self) -> None:
        row = _Row(_uuid("3"), _BASE, 0.25)
        assert score_order_key(row) == (
            False,
            -0.25,
            -0.25,
            _BASE,
            str(row.agent_id),
        )
        assert score_order_key(row, score=0.5) == (
            False,
            -0.5,
            -0.5,
            _BASE,
            str(row.agent_id),
        )

    def test_efficiency_bonus_folds_after_continual_mean(self) -> None:
        row = _FinalRow(_uuid("3"), "5" + "a" * 47, _BASE, 0.8)

        scores = official_composites(
            [row],
            quorum={row.agent_id: [0.7, 0.8, 0.9]},
            completed_waves={row.agent_id: {10: 0.6, 20: 0.9}},
            continual_mean_active=True,
            efficiency_bonuses={row.agent_id: 0.1},
            efficiency_fold_active=True,
        )

        assert scores[row.agent_id] == pytest.approx(0.78 * 1.1)

    def test_v9_factor_applies_after_full_quality_and_ignores_continual_data(
        self,
    ) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 750_000},
        )

        scores = official_composites(
            [row],
            quorum={row.agent_id: [1.0, 1.0, 1.0]},
            completed_waves={row.agent_id: {10: 1.0}},
            continual_mean_active=True,
            efficiency_bonuses={row.agent_id: 0.1},
            efficiency_factors={row.agent_id: 0.85},
            efficiency_fold_active=True,
        )

        # Full v9 quality is the primary order. Curve-v3 is retained only as
        # the secondary exact-quality tiebreak projection.
        assert scores[row.agent_id] == pytest.approx(0.75)
        assert scores.secondary_scores[row.agent_id] == pytest.approx(0.75 * 0.85)

    def test_v9_factor_applies_to_authoritative_base_only_quality(self) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation=None,
        )

        scores = official_composites(
            [row],
            quorum={row.agent_id: [0.7, 0.8, 0.9]},
            completed_waves={row.agent_id: {10: 0.6, 20: 0.9}},
            continual_mean_active=True,
            efficiency_factors={row.agent_id: 0.85},
            efficiency_fold_active=True,
        )

        assert scores[row.agent_id] == pytest.approx(0.78)
        assert scores.secondary_scores[row.agent_id] == pytest.approx(0.78 * 0.85)

    def test_equal_v9_quality_lower_cost_factor_beats_submission_time(self) -> None:
        earlier = _FinalRow(
            _uuid("1"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        later = _FinalRow(
            _uuid("2"),
            "5" + "b" * 47,
            _BASE + timedelta(hours=1),
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        scores = official_composites(
            [earlier, later],
            quorum={},
            completed_waves={},
            continual_mean_active=False,
            efficiency_factors={earlier.agent_id: 0.85, later.agent_id: 1.10},
            efficiency_fold_active=True,
        )

        assert rank_submissions([earlier, later], scores=scores) == [later, earlier]

    def test_live_harry_regression_never_crosses_the_quality_tier(self) -> None:
        harry = _FinalRow(
            _uuid("1"),
            "5" + "a" * 47,
            _BASE,
            0.995020,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 995_020},
        )
        higher_quality = _FinalRow(
            _uuid("2"),
            "5" + "b" * 47,
            _BASE + timedelta(hours=1),
            0.997012,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 997_012},
        )
        scores = official_composites(
            [harry, higher_quality],
            quorum={},
            completed_waves={},
            continual_mean_active=False,
            efficiency_factors={harry.agent_id: 1.10},
            efficiency_fold_active=True,
        )

        assert scores.secondary_scores[harry.agent_id] == pytest.approx(0.995518)
        assert rank_submissions([harry, higher_quality], scores=scores) == [
            higher_quality,
            harry,
        ]

    def test_equal_headroom_adjusted_scores_use_submission_time_tie_break(self) -> None:
        earlier = _FinalRow(
            _uuid("1"),
            "5" + "a" * 47,
            _BASE,
            0.95,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 950_000},
        )
        later = _FinalRow(
            _uuid("2"),
            "5" + "b" * 47,
            _BASE + timedelta(hours=1),
            0.95,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 950_000},
        )
        scores = official_composites(
            [later, earlier],
            quorum={},
            completed_waves={},
            continual_mean_active=False,
            efficiency_factors={earlier.agent_id: 1.10, later.agent_id: 1.10},
            efficiency_fold_active=True,
        )

        assert scores == {earlier.agent_id: 0.95, later.agent_id: 0.95}
        assert scores.secondary_scores == {
            earlier.agent_id: pytest.approx(0.955),
            later.agent_id: pytest.approx(0.955),
        }
        assert rank_submissions([later, earlier], scores=scores) == [earlier, later]

    def test_headroom_uplift_keeps_banblackycat_ahead_of_earlier_crown(self) -> None:
        crown = _FinalRow(
            _uuid("1"),
            "5" + "a" * 47,
            _BASE,
            0.980723,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 980_723},
        )
        banblackycat = _FinalRow(
            _uuid("2"),
            "5" + "b" * 47,
            _BASE + timedelta(minutes=15),
            0.997012,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 997_012},
        )
        scores = official_composites(
            [crown, banblackycat],
            quorum={},
            completed_waves={},
            continual_mean_active=False,
            efficiency_factors={
                crown.agent_id: 1.09426,
                banblackycat.agent_id: 1.034716,
            },
            efficiency_fold_active=True,
        )

        assert scores[crown.agent_id] == pytest.approx(0.980723)
        assert scores[banblackycat.agent_id] == pytest.approx(0.997012)
        assert scores.secondary_scores[crown.agent_id] == pytest.approx(0.98254005002)
        assert scores.secondary_scores[banblackycat.agent_id] == pytest.approx(
            0.997115731408
        )
        assert rank_submissions([crown, banblackycat], scores=scores) == [
            banblackycat,
            crown,
        ]


@pytest.mark.asyncio
class TestEfficiencyAdjustedFloors:
    @pytest.mark.parametrize(
        ("fleet_ready", "expected"),
        [(True, 0.82), (False, 0.80)],
    )
    async def test_resolver_uses_threaded_policy_and_the_v9_capable_fleet_gate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fleet_ready: bool,
        expected: float,
    ) -> None:
        """An env-seed config is authoritative even without a DB revision."""
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)

        async def bonus_rows(*_args: object, **kwargs: object) -> dict[UUID, object]:
            assert kwargs["epoch_index"] == epoch_index_for(_BASE, config.epoch_hours)
            return {
                row.agent_id: SimpleNamespace(factor=1.10, bonus=0.0),
            }

        async def fleet_supports(*_args: object, **kwargs: object) -> bool:
            assert kwargs["minimum_protocol"] == 21
            assert kwargs["bench_version"] == 9
            assert kwargs["now"] == _BASE
            return fleet_ready

        monkeypatch.setattr("ditto.db.queries.efficiency.get_bonus_rows", bonus_rows)
        monkeypatch.setattr(
            "ditto.db.queries.heartbeats.live_validator_fleet_supports_protocol",
            fleet_supports,
        )
        scores = await resolve_official_composites(
            object(),  # type: ignore[arg-type]
            rows=[row],
            bench_version=9,
            continual_mean_active=False,
            efficiency_config=config,
            now=_BASE,
        )

        assert scores[row.agent_id] == pytest.approx(0.8)
        assert scores.secondary_scores.get(row.agent_id, 0.8) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "requester",
        [
            None,
            SimpleNamespace(
                protocol_version=18,
                seen_at=_BASE - timedelta(hours=1),
            ),
        ],
    )
    async def test_absent_or_stale_ledger_requester_is_rejected_only_for_factor(
        self,
        monkeypatch: pytest.MonkeyPatch,
        requester: object | None,
    ) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)
        session = SimpleNamespace(get=AsyncMock(return_value=requester))

        monkeypatch.setattr(
            "ditto.db.queries.efficiency.get_bonus_rows",
            AsyncMock(
                return_value={row.agent_id: SimpleNamespace(factor=1.10, bonus=0.0)}
            ),
        )
        fleet_gate = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ditto.db.queries.heartbeats.live_validator_fleet_supports_protocol",
            fleet_gate,
        )

        with pytest.raises(
            EfficiencyFactorRequesterNotReady,
            match="fresh validator heartbeat.*bounded efficiency factors",
        ):
            await resolve_efficiency_adjustments(
                session,  # type: ignore[arg-type]
                rows=[row],
                efficiency_config=config,
                now=_BASE,
                requesting_validator_hotkey="5" + "z" * 47,
            )

        fleet_gate.assert_not_awaited()

    async def test_fresh_pre_v19_requester_receives_neutral_factor_projection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)
        session = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(protocol_version=18, seen_at=_BASE)
            )
        )
        monkeypatch.setattr(
            "ditto.db.queries.efficiency.get_bonus_rows",
            AsyncMock(
                return_value={row.agent_id: SimpleNamespace(factor=1.10, bonus=0.0)}
            ),
        )
        fleet_gate = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ditto.db.queries.heartbeats.live_validator_fleet_supports_protocol",
            fleet_gate,
        )

        bonuses, factors, curve_versions = await resolve_efficiency_adjustments(
            session,  # type: ignore[arg-type]
            rows=[row],
            efficiency_config=config,
            now=_BASE,
            requesting_validator_hotkey="5" + "z" * 47,
        )

        assert bonuses == {}
        assert factors == {}
        assert curve_versions == {}
        fleet_gate.assert_not_awaited()

    def _session_with_snapshots(
        self,
        snapshots: dict[UUID, int],
        *,
        requester: object | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            get=AsyncMock(return_value=requester),
            execute=AsyncMock(
                return_value=SimpleNamespace(all=lambda: list(snapshots.items()))
            ),
        )

    async def test_resolver_withholds_v4_factor_until_protocol_25_fleet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        snapshot_id = uuid4()
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)
        session = self._session_with_snapshots({snapshot_id: 4})
        monkeypatch.setattr(
            "ditto.db.queries.efficiency.get_bonus_rows",
            AsyncMock(
                return_value={
                    row.agent_id: SimpleNamespace(
                        factor=1.5, bonus=0.0, snapshot_id=snapshot_id
                    )
                }
            ),
        )
        fleet_gate = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "ditto.db.queries.heartbeats.live_validator_fleet_supports_protocol",
            fleet_gate,
        )

        bonuses, factors, curve_versions = await resolve_efficiency_adjustments(
            session,  # type: ignore[arg-type]
            rows=[row],
            efficiency_config=config,
            now=_BASE,
        )

        assert bonuses == {}
        assert factors == {}
        assert curve_versions == {}
        assert fleet_gate.await_args is not None
        assert fleet_gate.await_args.kwargs["minimum_protocol"] == 25

    async def test_resolver_serves_v4_factor_when_fleet_reports_protocol_25(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        snapshot_id = uuid4()
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)
        session = self._session_with_snapshots({snapshot_id: 4})
        monkeypatch.setattr(
            "ditto.db.queries.efficiency.get_bonus_rows",
            AsyncMock(
                return_value={
                    row.agent_id: SimpleNamespace(
                        factor=1.5, bonus=0.0, snapshot_id=snapshot_id
                    )
                }
            ),
        )
        fleet_gate = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ditto.db.queries.heartbeats.live_validator_fleet_supports_protocol",
            fleet_gate,
        )

        bonuses, factors, curve_versions = await resolve_efficiency_adjustments(
            session,  # type: ignore[arg-type]
            rows=[row],
            efficiency_config=config,
            now=_BASE,
        )

        assert bonuses == {}
        assert factors == {row.agent_id: pytest.approx(1.5)}
        assert curve_versions == {row.agent_id: 4}
        assert fleet_gate.await_args is not None
        assert fleet_gate.await_args.kwargs["minimum_protocol"] == 25

    async def test_protocol_24_requester_does_not_inherit_a_v4_factor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        snapshot_id = uuid4()
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)
        session = self._session_with_snapshots(
            {snapshot_id: 4},
            requester=SimpleNamespace(protocol_version=24, seen_at=_BASE),
        )
        monkeypatch.setattr(
            "ditto.db.queries.efficiency.get_bonus_rows",
            AsyncMock(
                return_value={
                    row.agent_id: SimpleNamespace(
                        factor=1.5, bonus=0.0, snapshot_id=snapshot_id
                    )
                }
            ),
        )
        fleet_gate = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ditto.db.queries.heartbeats.live_validator_fleet_supports_protocol",
            fleet_gate,
        )

        bonuses, factors, curve_versions = await resolve_efficiency_adjustments(
            session,  # type: ignore[arg-type]
            rows=[row],
            efficiency_config=config,
            now=_BASE,
            requesting_validator_hotkey="5" + "z" * 47,
        )

        assert bonuses == {}
        assert factors == {}
        assert curve_versions == {}
        fleet_gate.assert_not_awaited()

    async def test_frozen_v3_snapshot_still_activates_on_protocol_21(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        snapshot_id = uuid4()
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)
        session = self._session_with_snapshots(
            {snapshot_id: 3},
            requester=SimpleNamespace(protocol_version=21, seen_at=_BASE),
        )
        monkeypatch.setattr(
            "ditto.db.queries.efficiency.get_bonus_rows",
            AsyncMock(
                return_value={
                    row.agent_id: SimpleNamespace(
                        factor=1.10, bonus=0.0, snapshot_id=snapshot_id
                    )
                }
            ),
        )
        fleet_gate = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ditto.db.queries.heartbeats.live_validator_fleet_supports_protocol",
            fleet_gate,
        )

        bonuses, factors, curve_versions = await resolve_efficiency_adjustments(
            session,  # type: ignore[arg-type]
            rows=[row],
            efficiency_config=config,
            now=_BASE,
            requesting_validator_hotkey="5" + "z" * 47,
        )

        assert bonuses == {}
        assert factors == {row.agent_id: pytest.approx(1.10)}
        assert curve_versions == {row.agent_id: 3}
        assert fleet_gate.await_args is not None
        assert fleet_gate.await_args.kwargs["minimum_protocol"] == 21

    async def test_resolver_withholds_when_snapshot_curve_metadata_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _FinalRow(
            _uuid("3"),
            "5" + "a" * 47,
            _BASE,
            0.8,
            bench_version=9,
            v9_confirmation={"full_effective_micros": 800_000},
        )
        snapshot_id = uuid4()
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)
        session = self._session_with_snapshots({})
        monkeypatch.setattr(
            "ditto.db.queries.efficiency.get_bonus_rows",
            AsyncMock(
                return_value={
                    row.agent_id: SimpleNamespace(
                        factor=1.5, bonus=0.0, snapshot_id=snapshot_id
                    )
                }
            ),
        )
        fleet_gate = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ditto.db.queries.heartbeats.live_validator_fleet_supports_protocol",
            fleet_gate,
        )

        bonuses, factors, curve_versions = await resolve_efficiency_adjustments(
            session,  # type: ignore[arg-type]
            rows=[row],
            efficiency_config=config,
            now=_BASE,
        )

        assert bonuses == {}
        assert factors == {}
        assert curve_versions == {}
        fleet_gate.assert_not_awaited()

    async def test_fifth_and_tenth_floors_use_factor_adjusted_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            _FinalRow(
                UUID(int=index + 1),
                "5" + chr(ord("a") + index) * 47,
                _BASE + timedelta(minutes=index),
                0.8,
                bench_version=9,
                v9_confirmation={"full_effective_micros": 800_000},
            )
            for index in range(10)
        ]
        factors = {row.agent_id: 0.85 + index * 0.02 for index, row in enumerate(rows)}
        config = EfficiencyBonusConfig(enabled=True, fold_enabled=True)

        async def eligible_rows(*_args: object, **kwargs: object) -> list[_FinalRow]:
            assert kwargs["include_fingerprints"] is False
            assert kwargs["include_details"] is False
            return rows

        async def ranking_scores(*_args: object, **kwargs: object) -> dict[UUID, float]:
            assert kwargs["efficiency_config"] is config
            assert kwargs["now"] == _BASE
            return official_composites(
                rows,
                quorum={},
                completed_waves={},
                continual_mean_active=False,
                efficiency_factors=factors,
                efficiency_fold_active=True,
            )

        monkeypatch.setattr(
            "ditto.db.queries.tickets.list_eligible_ledger", eligible_rows
        )
        monkeypatch.setattr(
            "ditto.db.queries.tickets.resolve_ranking_scores", ranking_scores
        )

        continuation, provisional = await get_score_priority_floor_rows(
            object(),  # type: ignore[arg-type]
            bench_version=9,
            efficiency_config=config,
            now=_BASE,
        )
        shared_continuation, shared_provisional = (
            score_priority_floor_rows_from_resolved_ledger(
                cast(list[LedgerRow], rows),
                scores=official_composites(
                    rows,
                    quorum={},
                    completed_waves={},
                    continual_mean_active=False,
                    efficiency_factors=factors,
                    efficiency_fold_active=True,
                ),
            )
        )

        assert continuation is not None
        assert provisional is not None
        assert shared_continuation == continuation
        assert shared_provisional == provisional
        # Factor order is the reverse of submission-time order. Raw-score ties
        # would put rows[4] fifth; the canonical adjusted floor is rows[5].
        assert continuation.row.agent_id == rows[5].agent_id
        assert continuation.score == pytest.approx(0.8)
        assert provisional.row.agent_id == rows[0].agent_id
        assert provisional.score == pytest.approx(0.8)


async def _seed(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    hotkey: str,
    composites: tuple[float, ...],
    created_at: datetime,
    n: int = 114,
) -> UUID:
    """One finalized submission, with the accepted tickets its scores imply."""
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=hotkey,
                name="agent",
                sha256="ab" * 32,
                size_bytes=524288,
                status=AgentStatus.SCORED,
                created_at=created_at,
            )
        )
        await session.flush()
        for index, composite in enumerate(composites):
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey=_VALIDATORS[index],
                run_id=f"{hotkey[:8]}-{index}",
                seed=987654321,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=500,
                n=n,
                generated_at=_BASE + timedelta(minutes=index),
                signature="ab" * 64,
                details={"bench_version": _BENCH},
                bench_version=_BENCH,
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH,
                    validator_hotkey=_VALIDATORS[index],
                    slot_id="slot-0",
                    status=TicketStatus.SCORED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=_BASE + timedelta(minutes=index),
                    deadline=_BASE + timedelta(minutes=index + 90),
                    attempt_count=1,
                    manual_retry_grants=0,
                )
            )
    return agent_id


@pytest.mark.asyncio
class TestSqlMatchesPython:
    async def test_ledger_sql_order_reproduces_the_python_comparator(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The one place the rule is written twice, checked against itself.

        The fixture is built out of the cases where a comparator can silently
        drift: an exact composite tie resolved by age, a same-instant tie
        resolved by ``agent_id``, and an unranked run whose composite outranks
        every real one.
        """
        await _seed(
            session_maker,
            hotkey="5" + "A" * 47,
            composites=(0.90, 0.90, 0.90),
            created_at=_BASE,
        )
        # Exact score tie, different ages.
        await _seed(
            session_maker,
            hotkey="5" + "B" * 47,
            composites=(0.70, 0.70, 0.70),
            created_at=_BASE + timedelta(days=2),
        )
        await _seed(
            session_maker,
            hotkey="5" + "C" * 47,
            composites=(0.70, 0.70, 0.70),
            created_at=_BASE + timedelta(days=1),
        )
        # Same score AND same instant: only agent_id can separate these.
        for marker in ("D", "E", "F"):
            await _seed(
                session_maker,
                hotkey="5" + marker * 47,
                composites=(0.50, 0.50, 0.50),
                created_at=_BASE + timedelta(days=3),
            )
        # A 12-case smoke run scoring higher than anything real.
        await _seed(
            session_maker,
            hotkey="5" + "G" * 47,
            composites=(0.99, 0.99, 0.99),
            created_at=_BASE,
            n=12,
        )

        async with session_maker() as session:
            rows = await list_eligible_ledger(session, bench_version=_BENCH)

        assert len(rows) == 7
        assert rows == rank_submissions(rows)
        # The fixture actually exercises the tiebreaks, or the assertion above
        # is satisfied by an ordering that never had to break a tie.
        assert len({row.composite for row in rows}) < len(rows)
        assert rows[-1].eligible is False
        assert rows[-1].composite == pytest.approx(0.99)


@pytest.mark.asyncio
class TestContinuationFloor:
    async def test_floor_is_cut_by_official_composite(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Six finalized owners, built so the two candidate keys invert.

        By raw ``composite`` fifth place is "E" at 0.82. "F" is last at 0.80 but
        completes waves at 0.95, so its ``official_composite`` is 0.875 and it
        sits third -- which pushes fifth place onto "D" at 0.84. The floor is a
        gate on whether a submission can still reach the emission set, and
        emission-set membership is decided by ``official_composite``, so 0.84 is
        the number that gate has to use.
        """
        by_marker: dict[str, UUID] = {}
        for rank, marker in enumerate("ABCDEF"):
            by_marker[marker] = await _seed(
                session_maker,
                hotkey="5" + marker * 47,
                composites=((0.90 - rank * 0.02),) * 3,
                created_at=_BASE + timedelta(days=rank),
            )

        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_VALIDATORS[0],
                    software_version="0.28.0",
                    protocol_version=14,
                    code_digest="ab" * 32,
                    state="idle",
                    reported_at=now,
                    seen_at=now,
                    signature="cd" * 64,
                    capabilities={
                        "screened_images": True,
                        "require_screened_image": True,
                        "source_build_fallback": False,
                        "full_stack_managed": True,
                        "stack_updater": True,
                        "sandbox_egress_restricted": True,
                        "ticket_inference": False,
                        "signed_score_quorum": False,
                        "executor_isolation": "ephemeral_vm",
                        "scorer_benchmarks": {
                            "status": "fresh_verified",
                            "supported_bench_versions": [_BENCH],
                            "observed_at": int(now.timestamp()),
                            "software_version": "1.0.0",
                            "source_revision": "a" * 40,
                        },
                    },
                )
            )
            # A wave only counts once every cohort member has scored that seed,
            # so all six retest; everyone but "F" retests at its own composite.
            await append_confirmation_scores(
                session,
                rows=[
                    ConfirmationSeedScore(
                        agent_id,
                        "5V1",
                        seed,
                        0.95 if marker == "F" else 0.90 - index * 0.02,
                        f"r-{marker}-{seed}",
                        None,
                    )
                    for index, (marker, agent_id) in enumerate(by_marker.items())
                    for seed in (100, 200, 300)
                ],
                bench_version=_BENCH,
                created_at=now,
            )

        async with session_maker() as session:
            continuation, _provisional = await get_score_priority_floor_rows(
                session, bench_version=_BENCH
            )

        assert continuation is not None
        assert continuation.row.agent_id == by_marker["D"]
        assert continuation.score == pytest.approx(0.84)
        # The retired raw-composite cut is absent, not merely different.
        assert continuation.row.agent_id != by_marker["E"]
        assert continuation.score != pytest.approx(0.82)

    async def test_floor_read_does_not_hydrate_score_telemetry(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """The allocator floor read must stay scalar-only.

        Every ordinary idle-slot claim reaches this read, and it consumes
        nothing but ranking fields. Hydrating `details` detoasted and decoded
        the full per-case audit blob for every eligible row on every claim and
        then discarded it -- user CPU burned on the single API worker's event
        loop, which is the shape production saturated on (#388).
        """
        for rank, marker in enumerate("ABCDEF"):
            await _seed(
                session_maker,
                hotkey="5" + marker * 47,
                composites=((0.90 - rank * 0.02),) * 3,
                created_at=_BASE + timedelta(days=rank),
            )

        async with session_maker() as session:
            continuation, provisional = await get_score_priority_floor_rows(
                session, bench_version=_BENCH
            )

        assert continuation is not None
        # None here means the column was never selected, not that the row has
        # no telemetry: these seeded scores carry details.
        assert continuation.row.details is None
        if provisional is not None:
            assert provisional.row.details is None

    async def test_narrow_details_projection_ships_only_requested_keys(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """`details_keys` must return the asked-for keys and nothing else.

        The KOTH/retest builder reads three fields out of a per-case audit blob
        that runs ~22KB a row. Shipping the whole blob for every eligible agent,
        on a path that executes for every continual-retest request, put the API
        worker at 41% of CPU inside asyncpg's JSONB decoder and drove
        /top5-confirmation-job to a measured 152 seconds.
        """
        await _seed(
            session_maker,
            hotkey="5" + "N" * 47,
            composites=(0.90, 0.90, 0.90),
            created_at=_BASE,
        )

        async with session_maker() as session:
            rows = await list_eligible_ledger(
                session,
                include_fingerprints=False,
                details_keys=("bench_version",),
                bench_version=_BENCH,
            )

        assert rows
        details = rows[0].details
        assert isinstance(details, dict)
        # Exactly the requested key survives; the seeded blob's other content
        # never leaves Postgres.
        assert set(details) == {"bench_version"}
        assert details["bench_version"] == _BENCH

    async def test_narrow_details_projection_nulls_absent_keys(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """A key the row lacks must arrive as None, not raise or vanish.

        Every reader of these keys already treats absent as None, so this is the
        contract that lets the projection stand in for the whole blob.
        """
        await _seed(
            session_maker,
            hotkey="5" + "O" * 47,
            composites=(0.90, 0.90, 0.90),
            created_at=_BASE,
        )

        async with session_maker() as session:
            rows = await list_eligible_ledger(
                session,
                include_fingerprints=False,
                details_keys=("composite_stderr", "confirmation_seeds"),
                bench_version=_BENCH,
            )

        assert rows
        details = rows[0].details
        assert isinstance(details, dict)
        assert set(details) == {"composite_stderr", "confirmation_seeds"}
        assert details["confirmation_seeds"] is None

    async def test_no_floor_below_five_finalized_owners(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        for rank, marker in enumerate("ABCD"):
            await _seed(
                session_maker,
                hotkey="5" + marker * 47,
                composites=((0.90 - rank * 0.02),) * 3,
                created_at=_BASE + timedelta(days=rank),
            )
        async with session_maker() as session:
            assert await get_score_priority_floor_rows(
                session, bench_version=_BENCH
            ) == (None, None)


# A comparator over ledger rows: a negated score, then ``first_seen``, then
# ``agent_id``. Matching this outside ``ditto/score_order.py`` means someone has
# started a second ordering, which is the failure mode this whole module exists
# to prevent.
_HAND_ROLLED_COMPARATOR = re.compile(
    r"-\s*\w+(\.\w+)*\.composite\s*,\s*\n?\s*\w+(\.\w+)*\.first_seen"
    r"|-\s*effective_composite\(\w+\)\s*,\s*\n?\s*\w+\.first_seen",
)


def test_the_comparator_has_exactly_one_implementation() -> None:
    """No second ordering may reappear.

    The operator's rule is that each thing we maintain has one canonical
    version. The comparator now lives in ``ditto/score_order.py`` (Python and
    SQL side by side) and callers reach it through ``rank_submissions`` /
    ``score_order_terms``. A hand-rolled ``(-composite, first_seen, agent_id)``
    anywhere else is the beginning of the divergence again, so it fails here
    rather than in a miner's support ticket.
    """
    root = Path(__file__).resolve().parents[3]
    canonical = root / "score_order.py"
    assert canonical.is_file(), canonical

    offenders = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path != canonical
        and "tests" not in path.parts
        and _HAND_ROLLED_COMPARATOR.search(path.read_text())
    )
    assert offenders == [], (
        "hand-rolled score comparator outside ditto/score_order.py: "
        + ", ".join(offenders)
    )
