"""Effective v13 verification deadline and finalizer state (issue #2100).

Backroom already shows the SCREENING ATTEMPT's lease deadline
(``ScreeningAttempt.deadline``), which is not the policy's artifact-level
verification deadline. This endpoint answers the different question: for one
exact agent, is there a live v13 deadline governing its current hold, has it
passed, and what finalizer state would apply -- without mutating anything and
without inferring a deadline the platform never actually computed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_server.dependencies import get_session
from ditto.db.models import (
    Agent,
    ScreeningAttempt,
    ScreeningQuarantine,
)
from ditto.db.queries.screening_decisions import record_screening_decision
from ditto_screening_protocol import (
    PUBLISHED_REVIEW_TIMEOUT_POLICY,
    REVIEW_TIMED_OUT_OUTCOME,
    REVIEW_TIMEOUT_FINALIZER_ACTOR,
    REVIEW_TIMEOUT_FINALIZER_MODE_ENV,
    STRICT_TWO_OUTCOME_POLICY_VERSION,
    V2_PLATFORM_VERIFICATION_FAILED,
)

pytestmark = pytest.mark.asyncio

_TOKEN = "test-admin-token-at-least-32-characters"
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"}
_SCREENER = "5ScreenerHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_OTHER_WORKER = "5OtherWorkerAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_WINDOW = PUBLISHED_REVIEW_TIMEOUT_POLICY.max_verification_window


def _install(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session


async def _seed_hold(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID | None = None,
    reason_code: str = "source-review-inconclusive",
    policy_version: int = STRICT_TWO_OUTCOME_POLICY_VERSION,
    age: timedelta = _WINDOW + timedelta(hours=1),
    finding: dict | None = None,
    finding_digest: str | None = None,
    failure_provider: str | None = None,
    extra_attempts: int = 0,
    extra_workers: tuple[str, ...] = (),
    quarantine_status: str = "active",
) -> tuple[UUID, UUID, UUID]:
    """One agent held ``age`` ago; returns (agent_id, attempt_id, quarantine_id).

    Pass ``agent_id`` when a caller needs to know it ahead of the call, e.g. to
    bind a finding's ``artifact_sha256`` to this exact agent's SHA (which is
    always ``agent_id.hex * 2`` for these fixtures).
    """
    agent_id = agent_id if agent_id is not None else uuid4()
    attempt_id, quarantine_id = uuid4(), uuid4()
    created = datetime.now(UTC) - age
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"5Miner{agent_id.hex[:8]}",
                name="held",
                sha256=agent_id.hex * 2,
                status=AgentStatus.QUARANTINED,
                screening_policy_version=policy_version,
                screening_reason="Bounded source review was inconclusive; held",
                screening_reason_code=reason_code,
                created_at=created - timedelta(hours=1),
            )
        )
        await session.flush()
        for index in range(extra_attempts):
            worker = extra_workers[index] if index < len(extra_workers) else _SCREENER
            session.add(
                ScreeningAttempt(
                    attempt_id=uuid4(),
                    agent_id=agent_id,
                    screener_hotkey=worker,
                    policy_version=policy_version,
                    status="expired",
                    started_at=created - timedelta(hours=2 + index),
                    deadline=created - timedelta(hours=1 + index),
                    finished_at=created - timedelta(hours=1 + index),
                )
            )
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER,
                policy_version=policy_version,
                status="quarantined",
                started_at=created - timedelta(minutes=30),
                deadline=created + timedelta(minutes=30),
                finished_at=created,
                public_reason="held",
                reason_code=reason_code,
                failure_provider=failure_provider,
            )
        )
        await session.flush()
        session.add(
            ScreeningQuarantine(
                quarantine_id=quarantine_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_SCREENER,
                policy_version=policy_version,
                manifest_digest="a" * 64,
                finding_digest=finding_digest,
                reason_code=reason_code,
                evidence=None,
                finding=finding,
                status=quarantine_status,
                created_at=created,
            )
        )
    return agent_id, attempt_id, quarantine_id


async def _get(client: httpx.AsyncClient, agent_id: UUID) -> dict:
    response = await client.get(
        f"/api/v1/admin/screening-verification-deadline/{agent_id}",
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_requires_admin(
    app: FastAPI, client: httpx.AsyncClient, session_maker: async_sessionmaker
) -> None:
    _install(app, session_maker)
    response = await client.get(
        f"/api/v1/admin/screening-verification-deadline/{uuid4()}"
    )
    assert response.status_code in (401, 403)


async def test_unknown_agent_is_404(
    app: FastAPI, client: httpx.AsyncClient, session_maker: async_sessionmaker
) -> None:
    _install(app, session_maker)
    response = await client.get(
        f"/api/v1/admin/screening-verification-deadline/{uuid4()}",
        headers=_HEADERS,
    )
    assert response.status_code == 404


async def test_no_active_quarantine_reports_plainly_with_no_fabricated_deadline(
    app: FastAPI, client: httpx.AsyncClient, session_maker: async_sessionmaker
) -> None:
    """A clean/never-quarantined agent gets no deadline and no processing state."""
    _install(app, session_maker)
    agent_id = uuid4()
    async with session_maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5Clean",
                name="clean",
                sha256=agent_id.hex * 2,
                status=AgentStatus.EVALUATING,
                screening_policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
                created_at=datetime.now(UTC),
            )
        )

    body = await _get(client, agent_id)

    assert body["has_active_quarantine"] is False
    assert body["is_operator_finding_hold"] is False
    assert body["finalizer_state"] == "not_configured"
    assert body["not_applicable_reason"] == "no_active_quarantine"
    assert body["verification_deadline"] is None
    assert body["verification_window_start"] is None
    assert body["decision"] is None
    assert body["artifact_sha256"] == agent_id.hex * 2


async def test_mode_off_is_not_configured_even_for_an_otherwise_eligible_hold(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "off")
    _install(app, session_maker)
    agent_id, _attempt_id, _quarantine_id = await _seed_hold(session_maker)

    body = await _get(client, agent_id)

    assert body["finalizer_mode"] == "off"
    assert body["finalizer_state"] == "not_configured"
    assert body["not_applicable_reason"] == "finalizer_mode_off"
    assert body["verification_deadline"] is None


async def test_pending_before_the_deadline(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "shadow")
    _install(app, session_maker)
    agent_id, attempt_id, quarantine_id = await _seed_hold(
        session_maker, age=timedelta(hours=1)
    )

    body = await _get(client, agent_id)

    assert body["finalizer_state"] == "pending"
    assert body["not_applicable_reason"] is None
    assert body["quarantine_id"] == str(quarantine_id)
    assert body["attempt_id"] == str(attempt_id)
    assert body["policy_covered_by_finalizer"] is True
    assert body["verification_window_start"] is not None
    assert body["verification_deadline"] is not None
    assert body["verification_deadline_provenance"] == "shipped_default"
    start = datetime.fromisoformat(body["verification_window_start"])
    deadline = datetime.fromisoformat(body["verification_deadline"])
    assert deadline - start == _WINDOW
    assert body["failure_domain"] == "platform"
    assert (
        body["required_retries"]
        == PUBLISHED_REVIEW_TIMEOUT_POLICY.platform_failure_retries
    )
    assert body["recorded_retry_attempts"] == 1
    assert body["independent_worker_hotkeys"] == [_SCREENER]
    assert body["independent_worker_count"] == 1
    assert body["completed_checks"] is None
    assert body["failed_checks"] is None
    assert body["decision"] is None


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_ready_past_the_deadline_before_any_decision_is_written(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Past the deadline with no decision yet reads `ready` in shadow AND enforce.

    In shadow this is the row the finalizer would silently keep proposing
    forever; in enforce it is the row about to be swept on the next tick.
    Neither has actually written a decision record yet.
    """
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, mode)
    _install(app, session_maker)
    agent_id, _attempt_id, _quarantine_id = await _seed_hold(
        session_maker,
        extra_attempts=1,
        extra_workers=(_OTHER_WORKER,),
    )

    body = await _get(client, agent_id)

    assert body["finalizer_mode"] == mode
    assert body["finalizer_state"] == "ready"
    assert body["verification_deadline"] is not None
    now = datetime.now(UTC)
    assert datetime.fromisoformat(body["verification_deadline"]) <= now
    assert body["recorded_retry_attempts"] == 2
    assert set(body["independent_worker_hotkeys"]) == {_SCREENER, _OTHER_WORKER}
    assert body["independent_worker_count"] == 2
    assert body["independent_worker_requirement_met"] is True
    assert body["decision"] is None


async def test_finalized_surfaces_the_decision_record_verbatim(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a decision exists, the response defers to it and recomputes nothing."""
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "enforce")
    _install(app, session_maker)
    agent_id, attempt_id, quarantine_id = await _seed_hold(session_maker)

    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        assert agent is not None and quarantine is not None
        record = await record_screening_decision(
            session,
            agent=agent,
            outcome=REVIEW_TIMED_OUT_OUTCOME,
            reason_codes=["review-timed-out", V2_PLATFORM_VERIFICATION_FAILED],
            violation_proven=False,
            failure_domain="platform",
            retry_count=7,  # deliberately not what a live recompute would say
            independent_workers=3,
            policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
            public_reason="no-fault timeout",
            reviewer=REVIEW_TIMEOUT_FINALIZER_ACTOR,
            decided_at=datetime.now(UTC),
            evidence_references=[],
            completed_checks=["archive-sha256"],
            failed_checks=["source-review-inconclusive"],
            limitations=["bounded review exhausted"],
            attempt_id=attempt_id,
            quarantine_id=quarantine_id,
            policy_digest=quarantine.manifest_digest,
            verification_profile_digest=quarantine.review_audit_digest,
        )
        quarantine.status = "resolved"
        quarantine.resolved_at = datetime.now(UTC)
        quarantine.resolved_by = REVIEW_TIMEOUT_FINALIZER_ACTOR
        quarantine.resolution = "rescreen"

    body = await _get(client, agent_id)

    assert body["finalizer_state"] == "finalized"
    assert body["not_applicable_reason"] is None
    assert body["decision"] is not None
    assert body["decision"]["decision_id"] == str(record.decision_id)
    assert body["decision"]["outcome"] == REVIEW_TIMED_OUT_OUTCOME
    # Recorded evidence is surfaced as-is, not recomputed from live attempts.
    assert body["recorded_retry_attempts"] == 7
    assert body["independent_worker_count"] == 3
    assert body["failure_domain"] == "platform"
    assert body["completed_checks"] == ["archive-sha256"]
    assert body["failed_checks"] == ["source-review-inconclusive"]
    assert body["artifact_identity_verified"] is True
    assert body["policy_digest"] == quarantine.manifest_digest
    assert body["verification_profile_digest"] == quarantine.review_audit_digest
    # The deadline the finalizer applied is still shown for context.
    assert body["verification_deadline"] is not None


async def test_finalized_state_survives_a_later_mode_flip_to_off(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision already on the books outranks the CURRENT finalizer mode.

    Shadow mode always rolls back its transaction
    (``finalize_review_timeouts``: ``session.begin() ... finally:
    session.rollback()``), so a ``ScreeningDecisionRecord`` can only exist in
    the database from a prior ENFORCE-mode pass. "mode later flipped to off,
    decision already exists" is therefore the realistic production case this
    endpoint exists to serve -- not a hypothetical -- and it must never read
    as `not_configured` just because the finalizer is off *today*.
    """
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "off")
    _install(app, session_maker)
    agent_id, attempt_id, quarantine_id = await _seed_hold(session_maker)

    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        quarantine = await session.get(ScreeningQuarantine, quarantine_id)
        assert agent is not None and quarantine is not None
        await record_screening_decision(
            session,
            agent=agent,
            outcome=REVIEW_TIMED_OUT_OUTCOME,
            reason_codes=["review-timed-out", V2_PLATFORM_VERIFICATION_FAILED],
            violation_proven=False,
            failure_domain="platform",
            retry_count=2,
            independent_workers=1,
            policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
            public_reason="no-fault timeout",
            reviewer=REVIEW_TIMEOUT_FINALIZER_ACTOR,
            decided_at=datetime.now(UTC),
            evidence_references=[],
            completed_checks=["archive-sha256"],
            failed_checks=["source-review-inconclusive"],
            limitations=["bounded review exhausted"],
            attempt_id=attempt_id,
            quarantine_id=quarantine_id,
            policy_digest=quarantine.manifest_digest,
            verification_profile_digest=quarantine.review_audit_digest,
        )
        quarantine.status = "resolved"
        quarantine.resolved_at = datetime.now(UTC)
        quarantine.resolved_by = REVIEW_TIMEOUT_FINALIZER_ACTOR
        quarantine.resolution = "rescreen"

    body = await _get(client, agent_id)

    assert body["finalizer_mode"] == "off"
    assert body["finalizer_state"] == "finalized"
    assert body["not_applicable_reason"] is None


async def test_operator_finding_hold_is_reported_distinctly(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hold with a finding is an operator decision, never a processing state."""
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "enforce")
    _install(app, session_maker)
    agent_id = uuid4()
    await _seed_hold(
        session_maker,
        agent_id=agent_id,
        reason_code="agentic-source-review-tripwire",
        finding_digest="b" * 64,
        finding={
            "artifact_sha256": agent_id.hex * 2,
            "prompt_revision": "source-review-v2",
            "risk_level": "high",
            "confidence": 0.9,
            "categories": ["benchmark_emulation"],
            "evidence": [],
            "summary": "flagged pattern",
        },
    )

    body = await _get(client, agent_id)

    assert body["is_operator_finding_hold"] is True
    assert body["finalizer_state"] == "not_configured"
    assert body["not_applicable_reason"] == "operator_finding_hold"
    assert body["verification_deadline"] is None
    assert body["artifact_identity_verified"] is True


async def test_changed_artifact_guard_flags_a_mismatched_bound_sha(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finding bound to a different artifact SHA is never reported as verified.

    Mirrors ``admin_quarantine._review_payloads``'s ``finding_verified`` guard
    (a finding copied from another submission must never present as
    verified), narrowed to the artifact-SHA identity policy-v13.md's retry
    procedure requires ("verify the artifact SHA remains unchanged").
    """
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "enforce")
    _install(app, session_maker)
    foreign_sha = "f" * 64
    agent_id, _attempt_id, _quarantine_id = await _seed_hold(
        session_maker,
        reason_code="agentic-source-review-tripwire",
        finding_digest="b" * 64,
        finding={
            "artifact_sha256": foreign_sha,
            "prompt_revision": "source-review-v2",
            "risk_level": "high",
            "confidence": 0.9,
            "categories": ["benchmark_emulation"],
            "evidence": [],
            "summary": "flagged pattern",
        },
    )

    body = await _get(client, agent_id)

    assert body["is_operator_finding_hold"] is True
    assert body["artifact_identity_verified"] is False
    assert body["artifact_sha256"] != foreign_sha


async def test_legacy_policy_version_is_never_treated_as_covered(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "enforce")
    _install(app, session_maker)
    agent_id, _attempt_id, _quarantine_id = await _seed_hold(
        session_maker, policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION - 1
    )

    body = await _get(client, agent_id)

    assert body["policy_covered_by_finalizer"] is False
    assert body["finalizer_state"] == "not_configured"
    assert body["not_applicable_reason"] == "policy_version_not_covered"
    assert body["verification_deadline"] is None


async def test_reason_code_not_covered_by_the_finalizer(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active, no-finding hold whose reason code the finalizer never selects."""
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "enforce")
    _install(app, session_maker)
    agent_id, _attempt_id, _quarantine_id = await _seed_hold(
        session_maker, reason_code="deferred-source-review"
    )

    body = await _get(client, agent_id)

    assert body["finalizer_state"] == "not_configured"
    assert body["not_applicable_reason"] == "reason_code_not_covered"
    assert body["verification_deadline"] is None


async def test_resolved_quarantine_without_a_decision_reports_no_active_quarantine(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator release/reject through resolve_screening_quarantine writes no
    ScreeningDecisionRecord -- so once resolved, this reads as no active hold,
    never a fabricated `finalized` or a stale `ready`.
    """
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "enforce")
    _install(app, session_maker)
    agent_id, _attempt_id, quarantine_id = await _seed_hold(
        session_maker, quarantine_status="resolved"
    )

    body = await _get(client, agent_id)

    assert body["has_active_quarantine"] is False
    assert body["finalizer_state"] == "not_configured"
    assert body["not_applicable_reason"] == "no_active_quarantine"
    assert body["quarantine_id"] == str(quarantine_id)
    assert body["quarantine_status"] == "resolved"
    assert body["verification_deadline"] is None
    assert body["decision"] is None


async def test_a_decision_tied_to_a_finding_hold_never_reads_as_finalized(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decision tied to the latest quarantine is "finalized" only when the
    deadline finalizer itself wrote it (``outcome == review_timed_out``).

    Nothing today writes a ``ScreeningDecisionRecord`` against an operator
    finding hold's ``quarantine_id`` -- ``resolve_copy_review`` never sets
    ``quarantine_id`` at all -- but the read endpoint must not fabricate a
    ``finalized`` state and a 24-hour ``verification_deadline`` for one if it
    ever did: an operator's own manual reject on a finding hold is not a
    no-fault processing timeout.
    """
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "enforce")
    _install(app, session_maker)
    agent_id = uuid4()
    agent_id, attempt_id, quarantine_id = await _seed_hold(
        session_maker,
        agent_id=agent_id,
        reason_code="agentic-source-review-tripwire",
        finding_digest="b" * 64,
        finding={
            "artifact_sha256": agent_id.hex * 2,
            "prompt_revision": "source-review-v2",
            "risk_level": "high",
            "confidence": 0.9,
            "categories": ["benchmark_emulation"],
            "evidence": [],
            "summary": "flagged pattern",
        },
    )
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        await record_screening_decision(
            session,
            agent=agent,
            outcome="reject",
            reason_codes=["I5.benchmark_semantic_compiler"],
            violation_proven=True,
            failure_domain="artifact",
            retry_count=0,
            independent_workers=0,
            policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION,
            public_reason="Operator confirmed the finding manually",
            reviewer="operator",
            decided_at=datetime.now(UTC),
            evidence_references=["src/main.rs:42"],
            completed_checks=["operator-source-review"],
            failed_checks=["operator-source-review"],
            limitations=[],
            attempt_id=attempt_id,
            quarantine_id=quarantine_id,
        )

    body = await _get(client, agent_id)

    assert body["finalizer_state"] == "not_configured"
    assert body["not_applicable_reason"] == "operator_finding_hold"
    assert body["verification_deadline"] is None
    assert body["decision"] is None


async def test_a_decision_tied_to_a_pre_v13_quarantine_never_reads_as_finalized(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same guard, for a quarantine predating the finalizer's policy floor.

    ``select_timed_out_quarantines`` never selects a pre-v13 quarantine, so
    the finalizer itself cannot write this combination today either -- this
    defends the read endpoint's own precedence order regardless.
    """
    monkeypatch.setenv(REVIEW_TIMEOUT_FINALIZER_MODE_ENV, "enforce")
    _install(app, session_maker)
    agent_id, attempt_id, quarantine_id = await _seed_hold(
        session_maker, policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION - 1
    )
    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        await record_screening_decision(
            session,
            agent=agent,
            outcome="reject",
            reason_codes=["I5.benchmark_semantic_compiler"],
            violation_proven=True,
            failure_domain="artifact",
            retry_count=0,
            independent_workers=0,
            policy_version=STRICT_TWO_OUTCOME_POLICY_VERSION - 1,
            public_reason="Operator confirmed the finding manually",
            reviewer="operator",
            decided_at=datetime.now(UTC),
            evidence_references=["src/main.rs:42"],
            completed_checks=["operator-source-review"],
            failed_checks=["operator-source-review"],
            limitations=[],
            attempt_id=attempt_id,
            quarantine_id=quarantine_id,
        )

    body = await _get(client, agent_id)

    assert body["policy_covered_by_finalizer"] is False
    assert body["finalizer_state"] == "not_configured"
    assert body["not_applicable_reason"] == "policy_version_not_covered"
    assert body["verification_deadline"] is None
    assert body["decision"] is None
