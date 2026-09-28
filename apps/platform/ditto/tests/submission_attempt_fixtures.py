"""Paid source history with real Postgres and controlled completion times."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.submission_attempts import (
    AttemptControlSettings,
    AttemptGuidance,
    AttemptKind,
)
from ditto.api_server.submission_attempts import settings_digest
from ditto.db.models import Agent, EvaluationPayment, ScreeningAttempt
from ditto.db.queries.submission_attempts import add_attempt_record

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def profile(runtime: str = "a", packaging: str = "b") -> dict[str, Any]:
    return {
        "version": 1,
        "runtime_hash": runtime * 64,
        "packaging_hash": packaging * 64,
        "runtime_files": 1,
        "sha256": "c" * 64,
        "fingerprint": {
            "v": 2,
            "corpus": "fixture",
            "k": 256,
            "card": 100,
            "m": list(range(100)),
        },
    }


async def paid_attempt(
    session: AsyncSession,
    *,
    hotkey: str = "hotkey-a",
    coldkey: str = "owner-a",
    submitted_at: datetime = NOW - timedelta(minutes=10),
    classification: AttemptKind = "small_source_delta",
    lineage: UUID | None = None,
    source: dict[str, Any] | None = None,
    outcome: str = "rejected",
    reason: str = "source-review",
    fast_repair: bool = False,
    decision: AttemptGuidance | None = None,
) -> UUID:
    agent_id = uuid4()
    artifact = source or profile()
    session.add(
        Agent(
            agent_id=agent_id,
            miner_hotkey=hotkey,
            name="renamed-agent",
            sha256=agent_id.hex * 2,
            created_at=submitted_at,
        )
    )
    await session.flush()
    session.add(
        EvaluationPayment(
            agent_id=agent_id,
            miner_hotkey=hotkey,
            miner_coldkey=coldkey,
            block_hash="0x" + agent_id.hex,
            extrinsic_index=0,
            amount_rao=1,
            dest_address="destination",
            timestamp=submitted_at,
        )
    )
    add_attempt_record(
        session,
        agent_id=agent_id,
        profile=artifact,
        guidance=decision
        or AttemptGuidance(
            policy_revision=0,
            mode="shadow",
            classification=classification,
            settings_digest=settings_digest(AttemptControlSettings()),
            lineage_agent_id=lineage,
            fast_repair=fast_repair,
            reason="fixture",
        ),
    )
    if outcome != "pending":
        session.add(
            ScreeningAttempt(
                attempt_id=uuid4(),
                agent_id=agent_id,
                screener_hotkey="screener",
                policy_version=1,
                status=outcome,
                reason_code=reason,
                started_at=submitted_at,
                deadline=submitted_at + timedelta(minutes=5),
                finished_at=submitted_at + timedelta(seconds=30),
            )
        )
    await session.flush()
    return agent_id
