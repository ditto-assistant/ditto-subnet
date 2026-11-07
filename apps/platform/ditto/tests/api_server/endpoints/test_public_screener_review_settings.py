"""Public screener review limits: miners can match a production screen locally."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.screener_review_settings import (
    INTEGRITY_DOUBLE_CHECK_SCOPE,
    ScreenerReviewSettings,
    review_settings_checksum,
)
from ditto.api_server.dependencies import get_session
from ditto.db.models import ScreenerReviewSettingsRevision

pytestmark = pytest.mark.asyncio

_URL = "/api/v1/public/screener-review-settings"


@pytest.fixture(autouse=True)
def _real_session(
    app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _add(
    maker: async_sessionmaker[AsyncSession],
    *,
    scope: str,
    parent: int,
    settings: ScreenerReviewSettings,
) -> int:
    async with maker() as session, session.begin():
        row = ScreenerReviewSettingsRevision(
            parent_revision=parent,
            scope=scope,
            settings=settings.model_dump(mode="json"),
            checksum=review_settings_checksum(settings),
            reason="private operator reason text",
            actor="private-operator",
        )
        session.add(row)
        await session.flush()
        return row.revision


async def test_builtin_default_is_published_without_revisions(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(_URL)
    assert response.status_code == 200
    body = response.json()
    assert body["resolution_order"] == ["worker", "node", "*"]
    assert isinstance(body["required_policy_version"], int)
    [posture] = body["postures"]
    assert posture["scope"] == "builtin-default"
    assert posture["role"] == "global"
    assert posture["revision"] == 0
    assert posture["source_review_max_steps"] == 200


async def test_latest_revision_per_scope_is_public_allowlist_only(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    first = await _add(
        session_maker,
        scope="*",
        parent=0,
        settings=ScreenerReviewSettings(source_review_max_steps=120),
    )
    live = await _add(
        session_maker,
        scope="*",
        parent=first,
        settings=ScreenerReviewSettings(
            mode="enforce",
            source_review_max_steps=240,
            source_review_timeout_seconds=3_600,
            max_steps=256,
            timeout_seconds=2_700,
            max_input_tokens=5_000_000,
            max_output_tokens=1_000_000,
            max_cost_usd=25.0,
            adjudicator_max_steps=128,
            adjudicator_timeout_seconds=600,
        ),
    )
    await _add(
        session_maker,
        scope=INTEGRITY_DOUBLE_CHECK_SCOPE,
        parent=0,
        settings=ScreenerReviewSettings(),
    )
    await _add(
        session_maker,
        scope="subnet-screener-1",
        parent=0,
        settings=ScreenerReviewSettings(mode="inherit"),
    )

    response = await client.get(_URL)
    assert response.status_code == 200
    assert "max-age=60" in response.headers["cache-control"]
    postures = {row["scope"]: row for row in response.json()["postures"]}
    assert set(postures) == {"*", INTEGRITY_DOUBLE_CHECK_SCOPE, "subnet-screener-1"}

    glob = postures["*"]
    assert glob["revision"] == live
    assert glob["role"] == "global"
    assert glob["mode"] == "enforce"
    assert glob["source_review_max_steps"] == 240
    assert glob["source_review_timeout_seconds"] == 3_600
    assert glob["max_steps"] == 256
    assert glob["timeout_seconds"] == 2_700
    assert glob["max_input_tokens"] == 5_000_000
    assert glob["max_cost_usd"] == 25.0
    assert glob["adjudicator_max_steps"] == 128
    assert glob["adjudicator_timeout_seconds"] == 600
    assert postures[INTEGRITY_DOUBLE_CHECK_SCOPE]["role"] == "integrity_double_check"
    assert postures["subnet-screener-1"]["role"] == "node_or_worker"
    assert postures["subnet-screener-1"]["mode"] == "inherit"

    raw = response.text
    for private in (
        "policy_manifest_rotation_id",
        "private operator reason text",
        "private-operator",
        "audit_retention_days",
        "fanout_shadow",
        "probe_review",
    ):
        assert private not in raw


async def test_global_inherit_publishes_builtin_default(
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    first = await _add(
        session_maker,
        scope="*",
        parent=0,
        settings=ScreenerReviewSettings(source_review_max_steps=240),
    )
    await _add(
        session_maker,
        scope="*",
        parent=first,
        settings=ScreenerReviewSettings(mode="inherit"),
    )

    response = await client.get(_URL)
    assert response.status_code == 200
    postures = response.json()["postures"]
    globals_ = [row for row in postures if row["role"] == "global"]
    assert [row["scope"] for row in globals_] == ["builtin-default"]
    assert globals_[0]["source_review_max_steps"] == 200
