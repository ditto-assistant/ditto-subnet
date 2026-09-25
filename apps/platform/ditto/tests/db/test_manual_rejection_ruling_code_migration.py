"""The manual-rejection ruling-code backfill repairs its pinned row, and only it.

The cited live row (issue #2260) landed with the operator's ruling on the
quarantine but the screening lead the screener opened with still on the agent,
so the miner-facing pair read a CLEAR-side screening code as the decision. The
repair is guarded rather than blanket, so it must be shown both to fire when the
pinned identity holds and to leave the row alone when it does not.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_PARENT = "a40f7d9c621e"
_AGENT = "bfe3276e-0454-4b6a-a90f-17f2bb63d144"
_SHA = "03bd670787ff1816be8de940868df0669fd9f86ce4380292418ce5956a752ad1"
_ATTEMPT = "e8ed33bd-192e-4738-a633-7b77cd7c0a76"
_QUARANTINE = "c7e4edef-7c23-4ef4-9095-4781f882dc32"
_STALE = "behavioral-oracle-passed"
_RULING = "operator-rejected-quarantine"

_SCREENER_HOTKEY = "5FyqKcNrTtv7VvGrLDbYQ2JvHcAkWnMbPzXsQdRtEoUhJm9k"
_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_MANIFEST_DIGEST = "a" * 64

_PARAMS = {"agent_id": _AGENT}


def _alembic(*args: str) -> None:
    subprocess.run(
        ["uv", "run", "alembic", *args],
        check=True,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )


async def _seed(engine: AsyncEngine, *, status: str) -> None:
    """Write the cited row exactly as the issue describes it."""
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO agents (agent_id, miner_hotkey, name, sha256, status, "
                "screening_reason, screening_reason_code) "
                "VALUES (CAST(:agent_id AS uuid), :hotkey, :name, :sha, :status, "
                ":reason, :stale)"
            ),
            {
                **_PARAMS,
                "hotkey": _HOTKEY,
                "name": "cited-agent",
                "sha": _SHA,
                "status": status,
                "reason": "Manually rejected after source review",
                "stale": _STALE,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO screening_attempts (attempt_id, agent_id, "
                "screener_hotkey, policy_version, status, started_at, deadline) "
                "VALUES (CAST(:attempt_id AS uuid), CAST(:agent_id AS uuid), "
                ":screener, 13, 'rejected', :started, :deadline)"
            ),
            {
                **_PARAMS,
                "attempt_id": _ATTEMPT,
                "screener": _SCREENER_HOTKEY,
                "started": now - timedelta(minutes=30),
                "deadline": now,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO screening_quarantines (quarantine_id, agent_id, "
                "attempt_id, screener_hotkey, policy_version, manifest_digest, "
                "reason_code, status, resolution) "
                "VALUES (CAST(:quarantine_id AS uuid), CAST(:agent_id AS uuid), "
                "CAST(:attempt_id AS uuid), :screener, 13, :manifest, :stale, "
                "'resolved', 'reject')"
            ),
            {
                **_PARAMS,
                "quarantine_id": _QUARANTINE,
                "attempt_id": _ATTEMPT,
                "screener": _SCREENER_HOTKEY,
                "manifest": _MANIFEST_DIGEST,
                "stale": _STALE,
            },
        )


async def _teardown(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "DELETE FROM screening_quarantines "
                "WHERE quarantine_id = CAST(:quarantine_id AS uuid)"
            ),
            {**_PARAMS, "quarantine_id": _QUARANTINE},
        )
        await connection.execute(
            text(
                "DELETE FROM screening_attempts "
                "WHERE attempt_id = CAST(:attempt_id AS uuid)"
            ),
            {**_PARAMS, "attempt_id": _ATTEMPT},
        )
        await connection.execute(
            text("DELETE FROM agents WHERE agent_id = CAST(:agent_id AS uuid)"),
            _PARAMS,
        )


async def _state(engine: AsyncEngine) -> tuple[str | None, str]:
    async with engine.connect() as connection:
        agent_code = await connection.scalar(
            text(
                "SELECT screening_reason_code FROM agents "
                "WHERE agent_id = CAST(:agent_id AS uuid)"
            ),
            _PARAMS,
        )
        quarantine_code = await connection.scalar(
            text(
                "SELECT reason_code FROM screening_quarantines "
                "WHERE quarantine_id = CAST(:quarantine_id AS uuid)"
            ),
            {**_PARAMS, "quarantine_id": _QUARANTINE},
        )
    return agent_code, quarantine_code


async def test_manual_rejection_ruling_code_backfill(engine: AsyncEngine) -> None:
    try:
        # Every guard but the status holds here: same agent, same immutable
        # artifact SHA, same inherited code, and a quarantine this agent owns
        # resolved "reject" carrying that same lead. The row is not a rejected
        # submission any more, so the repair must not relabel it.
        _alembic("downgrade", _PARENT)
        await _seed(engine, status="evaluating")
        _alembic("upgrade", "head")
        agent_code, quarantine_code = await _state(engine)
        assert agent_code == _STALE
        assert quarantine_code == _STALE

        # Once the row is the rejected submission the issue describes, the
        # repair applies -- and it repairs the agent row only. The quarantine
        # keeps the screening lead verbatim, because that code is
        # screening-origin provenance, not the ruling.
        _alembic("downgrade", _PARENT)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE agents SET status = 'rejected' "
                    "WHERE agent_id = CAST(:agent_id AS uuid)"
                ),
                _PARAMS,
            )
        _alembic("upgrade", "head")
        agent_code, quarantine_code = await _state(engine)
        assert agent_code == _RULING
        assert quarantine_code == _STALE
    finally:
        # Keep this worker database usable even when an assertion above fails.
        await _teardown(engine)
        _alembic("upgrade", "head")
