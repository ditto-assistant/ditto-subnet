"""Allowing a withdrawn ATH resolution round-trips, and refuses to lose one.

Upgrade must admit ``resolution = 'withdraw'`` on ``ath_reviews`` and
``action = 'withdraw'`` on ``ath_review_actions``. Downgrade restores the
two-value constraints exactly: they still admit ``clear`` / ``reject`` and
refuse ``withdraw``, and the downgrade itself fails while a withdrawn row
exists instead of rewriting or deleting it.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_REVISION = "c4e8a1b27d90"
_PARENT = "6a7a2a03a65f"
_AGENT = "7c1d2e3f-4a5b-4c6d-8e7f-2197a0000001"
_REVIEW = "7c1d2e3f-4a5b-4c6d-8e7f-2197a0000002"
_ACTION = "7c1d2e3f-4a5b-4c6d-8e7f-2197a0000003"
_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"

_PARAMS = {"agent_id": _AGENT, "review_id": _REVIEW, "action_id": _ACTION}


def _alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        check=check,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _seed_agent(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO agents (agent_id, miner_hotkey, name, sha256, status) "
                "VALUES (CAST(:agent_id AS uuid), :hotkey, 'withdrawn-agent', :sha, "
                "'scored')"
            ),
            {**_PARAMS, "hotkey": _HOTKEY, "sha": "c" * 64},
        )


async def _seed_review(engine: AsyncEngine, *, resolution: str) -> None:
    """One resolved review and its ledger row, in a single transaction."""
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO ath_reviews (review_id, agent_id, status, opened_at, "
                "resolved_at, resolved_by, resolution, resolution_reason, "
                "original_reason, original_policy_version, original_evidence, "
                "algorithm_provenance) VALUES (CAST(:review_id AS uuid), "
                "CAST(:agent_id AS uuid), 'resolved', :now, :now, 'operator', "
                ":resolution, 'Precautionary hold settled', 'Manual hold', 13, "
                "CAST(:evidence AS jsonb), CAST(:provenance AS jsonb))"
            ),
            {
                **_PARAMS,
                "now": now,
                "resolution": resolution,
                "evidence": json.dumps({"sha256": "c" * 64, "score_count": 3}),
                "provenance": json.dumps({"snapshot": "manual-admin-hold"}),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO ath_review_actions (action_id, review_id, action, "
                "reason, actor, evidence, created_at) VALUES ("
                "CAST(:action_id AS uuid), CAST(:review_id AS uuid), :action, "
                "'Precautionary hold settled', 'operator', CAST('{}' AS jsonb), :now)"
            ),
            {**_PARAMS, "action": resolution, "now": now},
        )


async def _delete_review(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        # ath_review_actions cascades from ath_reviews.
        await connection.execute(
            text("DELETE FROM ath_reviews WHERE review_id = CAST(:review_id AS uuid)"),
            _PARAMS,
        )


async def _teardown(engine: AsyncEngine) -> None:
    await _delete_review(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM agents WHERE agent_id = CAST(:agent_id AS uuid)"),
            _PARAMS,
        )


async def test_withdraw_resolution_migration_round_trip(engine: AsyncEngine) -> None:
    try:
        await _seed_agent(engine)

        _alembic("downgrade", _PARENT)
        with pytest.raises(IntegrityError):
            await _seed_review(engine, resolution="withdraw")
        await _seed_review(engine, resolution="clear")
        await _delete_review(engine)

        _alembic("upgrade", "head")
        await _seed_review(engine, resolution="withdraw")

        # A withdrawn review blocks the downgrade rather than being rewritten.
        refused = _alembic("downgrade", _PARENT, check=False)
        assert refused.returncode != 0
        assert "ath_review_actions_action_check" in refused.stderr
        current = _alembic("current")
        assert _REVISION in current.stdout
        async with engine.connect() as connection:
            kept = (
                await connection.execute(
                    text(
                        "SELECT r.resolution, a.action FROM ath_reviews r "
                        "JOIN ath_review_actions a USING (review_id) "
                        "WHERE r.review_id = CAST(:review_id AS uuid)"
                    ),
                    _PARAMS,
                )
            ).one()
        assert tuple(kept) == ("withdraw", "withdraw")

        # Once the operator has settled it, the downgrade and re-upgrade work.
        await _delete_review(engine)
        _alembic("downgrade", _PARENT)
        _alembic("upgrade", "head")
        await _seed_review(engine, resolution="withdraw")
    finally:
        # Keep this worker database usable even when an assertion above fails.
        await _teardown(engine)
        _alembic("upgrade", "head")
