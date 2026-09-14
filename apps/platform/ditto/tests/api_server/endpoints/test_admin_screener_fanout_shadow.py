from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_server.endpoints.admin_screener_fanout_shadow import (
    _view,
    get_screener_fanout_shadow,
)
from ditto.db.models import (
    ScreenerFanoutShadowReview,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
)
from ditto.tests.api_server.endpoints.test_screener import _SCREENER_HOTKEY, _seed_agent


@pytest.mark.parametrize(
    "status,coverage,expected",
    [
        ("incomplete", False, None),
        ("succeeded", False, None),
        ("succeeded", True, True),
    ],
)
def test_legacy_incomplete_reports_do_not_display_as_disagreements(
    status, coverage, expected
):
    now = datetime.now(UTC)
    row = ScreenerFanoutShadowReview(
        shadow_id=uuid4(),
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        policy_version=12,
        policy_manifest_profile="l1_l2",
        policy_manifest_rotation_id="rotation",
        policy_manifest_digest="b" * 64,
        settings_revision=108,
        settings_scope="subnet-screener-1",
        settings_checksum="c" * 64,
        status=status,
        outcome="incomplete" if status == "incomplete" else "no_findings",
        baseline={"outcome": "quarantine"},
        report={"outcome": "incomplete"},
        disagrees_with_baseline=True,
        coverage_complete=coverage,
        error_code=None,
        provider="gcp",
        reserved_cost_microusd=3_000_000,
        reported_cost_microusd=30_000,
        unmetered=False,
        reserved_at=now,
        created_at=now,
        started_at=now,
        completed_at=now,
    )
    result = _view(row)
    assert result.disagrees_with_baseline is expected
    assert result.report == row.report
    assert result.reserved_cost_usd == 3


@pytest.mark.asyncio
async def test_metrics_exclude_incomplete_and_partial_historical_comparisons(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime.now(UTC)
    rows: list[tuple[str, bool, bool]] = [
        ("succeeded", True, True),
        ("succeeded", False, True),
        ("incomplete", False, True),
    ]
    agents = [
        await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            name=f"fanout-metrics-{index}",
            sha256=f"{index + 1:02x}" * 32,
        )
        for index in range(len(rows))
    ]
    attempts = [uuid4() for _ in rows]
    settings = ScreenerReviewSettings()
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerReviewSettingsRevision(
                revision=1,
                parent_revision=0,
                scope="*",
                settings=settings.model_dump(mode="json"),
                checksum="c" * 64,
                reason="seed fanout metrics regression",
                actor="test",
            )
        )
        session.add_all(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="passed",
                started_at=now - timedelta(minutes=1),
                deadline=now,
                finished_at=now,
            )
            for attempt_id, agent_id in zip(attempts, agents, strict=True)
        )
        await session.flush()
        session.add_all(
            ScreenerFanoutShadowReview(
                shadow_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                environment="prod",
                artifact_sha256=f"{index + 1:02x}" * 32,
                policy_version=SCREENING_POLICY_VERSION,
                policy_manifest_profile="l1_l2",
                policy_manifest_rotation_id="rotation",
                policy_manifest_digest="d" * 64,
                settings_revision=1,
                settings_scope="*",
                settings_checksum="c" * 64,
                status=status,
                outcome="no_findings" if status == "succeeded" else "incomplete",
                baseline={"outcome": "quarantine"},
                report={"outcome": "no_findings", "coverage_complete": coverage},
                disagrees_with_baseline=disagreement,
                coverage_complete=coverage,
                error_code=None if status == "succeeded" else "historical-incomplete",
                provider="gcp",
                reserved_cost_microusd=3_000_000,
                reported_cost_microusd=30_000,
                unmetered=False,
                reserved_at=now,
                created_at=now + timedelta(seconds=index),
                started_at=now,
                completed_at=now,
            )
            for index, (
                (status, coverage, disagreement),
                agent_id,
                attempt_id,
            ) in enumerate(zip(rows, agents, attempts, strict=True))
        )

    async with session_maker() as session:
        response = await get_screener_fanout_shadow(
            None, session, status=None, limit=50, offset=0
        )

    assert response.metrics.total == 3
    assert response.metrics.succeeded == 2
    assert response.metrics.incomplete == 1
    assert response.metrics.incomplete_coverage == 2
    assert response.metrics.compared == 1
    assert response.metrics.disagreements == 1
    by_attempt = {item.attempt_id: item for item in response.items}
    assert by_attempt[attempts[0]].disagrees_with_baseline is True
    assert by_attempt[attempts[1]].disagrees_with_baseline is None
    assert by_attempt[attempts[2]].disagrees_with_baseline is None
