"""Dropping the audited-quarantine reason allowlist round-trips (#2445).

A signed V13 INCONCLUSIVE verdict carries its review audit under the deciding
evidence code. Upgrade must admit such a row; downgrade must restore the exact
six-code allowlist, which still admits its own codes and refuses any other.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "a2f4d9c51e60"
_AGENT = "3f0b8f4e-5d1c-4f55-9a57-2445a0000001"
_ATTEMPT = "3f0b8f4e-5d1c-4f55-9a57-2445a0000002"
_QUARANTINE = "3f0b8f4e-5d1c-4f55-9a57-2445a0000003"
_DECIDING_CODE = "l2-runtime-evidence-unavailable"
_ALLOWLISTED_CODE = "l2-model-inconclusive"

_SCREENER_HOTKEY = "5FyqKcNrTtv7VvGrLDbYQ2JvHcAkWnMbPzXsQdRtEoUhJm9k"
_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"

_PARAMS = {"agent_id": _AGENT, "attempt_id": _ATTEMPT, "quarantine_id": _QUARANTINE}


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _seed(engine: AsyncEngine, *, reason_code: str) -> None:
    """Write one audited INCONCLUSIVE quarantine in a single transaction."""
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO agents (agent_id, miner_hotkey, name, sha256, status) "
                "VALUES (CAST(:agent_id AS uuid), :hotkey, 'audited-agent', :sha, "
                "'screening_failed')"
            ),
            {**_PARAMS, "hotkey": _HOTKEY, "sha": "b" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO screening_attempts (attempt_id, agent_id, "
                "screener_hotkey, policy_version, status, started_at, deadline) "
                "VALUES (CAST(:attempt_id AS uuid), CAST(:agent_id AS uuid), "
                ":screener, 13, 'expired', :started, :deadline)"
            ),
            {
                **_PARAMS,
                "screener": _SCREENER_HOTKEY,
                "started": now - timedelta(minutes=30),
                "deadline": now,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO screening_quarantines (quarantine_id, agent_id, "
                "attempt_id, screener_hotkey, policy_version, manifest_digest, "
                "review_audit_digest, review_audit, reason_code, status) "
                "VALUES (CAST(:quarantine_id AS uuid), CAST(:agent_id AS uuid), "
                "CAST(:attempt_id AS uuid), :screener, 13, :digest, :digest, "
                "CAST('{}' AS jsonb), :reason_code, 'active')"
            ),
            {
                **_PARAMS,
                "screener": _SCREENER_HOTKEY,
                "digest": "a" * 64,
                "reason_code": reason_code,
            },
        )


async def _teardown(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for statement in (
            "DELETE FROM screening_quarantines "
            "WHERE quarantine_id = CAST(:quarantine_id AS uuid)",
            "DELETE FROM screening_attempts "
            "WHERE attempt_id = CAST(:attempt_id AS uuid)",
            "DELETE FROM agents WHERE agent_id = CAST(:agent_id AS uuid)",
        ):
            await connection.execute(text(statement), _PARAMS)


async def test_audit_reason_allowlist_drop_round_trip(engine: AsyncEngine) -> None:
    try:
        _alembic("downgrade", _PARENT)
        with pytest.raises(IntegrityError):
            await _seed(engine, reason_code=_DECIDING_CODE)
        await _seed(engine, reason_code=_ALLOWLISTED_CODE)
        await _teardown(engine)

        _alembic("upgrade", "head")
        await _seed(engine, reason_code=_DECIDING_CODE)
    finally:
        # Keep this worker database usable even when an assertion above fails.
        await _teardown(engine)
        _alembic("upgrade", "head")
