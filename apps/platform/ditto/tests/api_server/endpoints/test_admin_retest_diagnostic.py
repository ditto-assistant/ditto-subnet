"""The operator read explains an exact generation without granting work."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.endpoints import admin_leaderboard

pytestmark = pytest.mark.asyncio


async def test_exact_agent_diagnostic_preserves_seeds_and_owner_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = UUID("11111111-1111-4111-8111-111111111111")
    newer = UUID("22222222-2222-4222-8222-222222222222")
    seed = 9_223_372_036_854_775_001
    session = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(status=SimpleNamespace(value="scored"))
        ),
        execute=AsyncMock(
            return_value=[
                (TicketStatus.ISSUED, datetime.now(UTC) + timedelta(minutes=5)),
                (TicketStatus.SCORED, datetime.now(UTC) - timedelta(minutes=5)),
            ]
        ),
    )
    resolver = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                wave_membership="participants",
                retest_cohort_size=5,
                retest_eligibility_mode="fixed",
                retest_eligibility_z=1.64,
                retest_cohort_max_size=25,
            )
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                session_maker=object(),
                continual_retest_settings=resolver,
                efficiency_settings=SimpleNamespace(
                    resolve=AsyncMock(return_value=None)
                ),
            )
        )
    )
    snapshot = SimpleNamespace(
        owner_by_agent={older: "owner", newer: "owner"},
        owner_representatives={"owner": older},
        canonical_scores={older: 0.91, newer: 0.93},
        official_scores={older: 0.94, newer: 0.93},
        folded_seeds_by_agent={older: (seed,), newer: ()},
    )
    monkeypatch.setattr(
        admin_leaderboard, "active_bench_version", AsyncMock(return_value=13)
    )
    monkeypatch.setattr(
        admin_leaderboard, "_current_koth_entries", AsyncMock(return_value=snapshot)
    )
    cohort_mock = AsyncMock(
        return_value=(
            (SimpleNamespace(agent_id=older),),
            (SimpleNamespace(agent_id=newer),),
            (SimpleNamespace(agent_id=older), SimpleNamespace(agent_id=newer)),
            frozenset({newer}),
        )
    )
    monkeypatch.setattr(admin_leaderboard, "_current_retest_cohort", cohort_mock)
    monkeypatch.setattr(
        admin_leaderboard,
        "confirmation_composites_by_seed",
        AsyncMock(return_value={newer: {seed: 0.95}}),
    )
    monkeypatch.setattr(
        admin_leaderboard,
        "read_reign_seed_anchor",
        AsyncMock(return_value=SimpleNamespace(anchor_block=123456, pinned=True)),
    )
    response = Response()
    result = await admin_leaderboard.continual_retest_diagnostic(
        cast(Request, request), response, cast(AsyncSession, session), None, newer
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert result.generated_at <= datetime.now(UTC)
    assert result.canonical_composite == 0.93
    assert result.official_composite == 0.93
    assert result.owner_representative_id == older
    assert [member.agent_id for member in result.family] == [newer, older]
    assert result.raw_confirmation_seeds == [str(seed)]
    assert result.folded_confirmation_seeds == []
    assert result.in_retest_cohort
    assert result.is_same_owner_challenger
    assert result.admission_reason == "same_owner_challenger"
    assert result.cohort_position == 2
    assert result.active_ticket_count == 1
    assert result.ticket_status_counts == {"issued": 1, "scored": 1}
    assert result.seed_anchor_champion_id == newer
    assert result.seed_anchor_pinned
    cohort_mock.assert_awaited_once()
    assert cohort_mock.await_args is not None
    assert cohort_mock.await_args.kwargs["snapshot"] is snapshot

    cohort_mock.return_value = (
        (SimpleNamespace(agent_id=older),),
        (SimpleNamespace(agent_id=older),),
        (SimpleNamespace(agent_id=older),),
        frozenset(),
    )
    excluded = await admin_leaderboard.continual_retest_diagnostic(
        cast(Request, request), Response(), cast(AsyncSession, session), None, newer
    )
    assert not excluded.in_retest_cohort
    assert excluded.admission_reason == "owner_suppressed"
