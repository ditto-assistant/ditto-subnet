"""Platform acting on an automated clear/reject of a held source review."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.targon_screening import maybe_finalize_targon_screen
from ditto.db.models import (
    Agent,
    ScreenerReviewSettingsRevision,
    ScreeningAttempt,
    ScreeningQuarantine,
    SubmissionImageBuild,
    SubmissionSourceReview,
)
from ditto.tests.api_server.endpoints.test_public import _install_db
from ditto.tests.api_server.endpoints.test_screener import (
    _SCREENER_HOTKEY,
    _SHA256,
    _seed_agent,
)
from ditto_screening_protocol.models import (
    ScreenReviewAudit,
    SourceReviewNote,
    source_review_notes_digest,
)

_CONFIG_DIGEST = "sha256:" + "ab" * 32
_RUNTIME_REF = (
    "us-central1-docker.pkg.dev/ditto-app-dev/"
    "ditto-screening-candidates/miner@sha256:" + "cd" * 32
)


class _FakeStorage:
    def __init__(self) -> None:
        self.copied: list[tuple[str, str]] = []

    async def copy_object(self, *, source_key: str, dest_key: str) -> None:
        self.copied.append((source_key, dest_key))


def _held_observation(adjudication: dict[str, object] | None) -> dict[str, object]:
    """A budget-terminated review that would otherwise wait on an operator."""
    return {
        "ok": False,
        "risk_level": None,
        "finding_digest": None,
        "categories": [],
        "error_code": "l2-model-step-budget",
        "finding": None,
        "failure_disposition": "inconclusive",
        "clearance_certified": False,
        "review_audit": None,
        "notes": [
            {
                "kind": "concern",
                "category": "benchmark_emulation",
                "path": "src/main.rs",
                "line": 10,
                "summary": "looked like a family table",
            }
        ],
        "adjudication": adjudication,
    }


def _adjudication(decision: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": decision,
        "reason": "the model authors the served reply from this user's records",
        "citations": [{"path": "src/main.rs", "line": 6}],
        "notes_considered": 1,
        "model": "z-ai/glm-5.3-flash",
        "prompt_revision": "adjudicator-v2-policy-v10",
    }
    if decision == "clear":
        payload["clear_clause"] = "model_authors_graded_slot"
    elif decision == "reject":
        payload["reject_invariant"] = "i5_production_engine"
    payload.update(overrides)
    return payload


async def _seed_held_screen(
    maker: async_sessionmaker[AsyncSession],
    *,
    observation: dict[str, object],
    adjudicator_mode: Literal["off", "shadow", "enforce"],
    policy_version: int = SCREENING_POLICY_VERSION,
) -> UUID:
    agent_id = await _seed_agent(maker, status=AgentStatus.SCREENING)
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ScreenerReviewSettingsRevision(
                revision=1,
                parent_revision=0,
                scope="*",
                settings=ScreenerReviewSettings(
                    mode="enforce", adjudicator_mode=adjudicator_mode
                ).model_dump(mode="json"),
                reason="test revision for adjudicator posture",
                actor="tests",
                checksum="ab" * 32,
            )
        )
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=policy_version,
                status="running",
                build_only=False,
                review_settings_revision=1,
                review_settings_instance_id="test-screener",
                review_settings_scope="*",
                review_settings_checksum="ab" * 32,
                started_at=now - timedelta(minutes=10),
                deadline=now + timedelta(minutes=60),
            )
        )
        await session.flush()
        session.add(
            SubmissionImageBuild(
                build_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                environment="prod",
                artifact_sha256=_SHA256,
                image_ref=f"ditto-screen/{agent_id}-{attempt_id}:latest",
                output_key=f"{agent_id}/builds/{attempt_id}.tar",
                status="succeeded",
                provider="targon",
                output_sha256="12" * 32,
                output_size_bytes=123,
                output_image_id=_CONFIG_DIGEST,
                runtime_status="succeeded",
                runtime_image_reference=_RUNTIME_REF,
                attempt_count=1,
                created_at=now - timedelta(minutes=10),
                started_at=now - timedelta(minutes=9),
                completed_at=now - timedelta(minutes=2),
                updated_at=now - timedelta(minutes=1),
            )
        )
        session.add(
            SubmissionSourceReview(
                review_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                environment="prod",
                artifact_sha256=_SHA256,
                status="succeeded",
                provider="targon",
                observation=observation,
                attempt_count=1,
                created_at=now - timedelta(minutes=10),
                completed_at=now - timedelta(minutes=2),
                updated_at=now - timedelta(minutes=1),
            )
        )
    return attempt_id


async def _finalize(
    maker: async_sessionmaker[AsyncSession], attempt_id: UUID
) -> _FakeStorage:
    storage = _FakeStorage()
    async with maker() as session, session.begin():
        await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, storage),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=datetime.now(UTC),
        )
    return storage


@pytest.mark.asyncio
async def test_v13_adjudicated_clear_stays_held_without_private_verification(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=_held_observation(_adjudication("clear")),
        adjudicator_mode="enforce",
    )

    storage = await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.QUARANTINED
        assert storage.copied == []


@pytest.mark.asyncio
async def test_v13_adjudicated_reject_stays_held_without_private_verification(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=_held_observation(_adjudication("reject")),
        adjudicator_mode="enforce",
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.QUARANTINED
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is not None


@pytest.mark.asyncio
async def test_v13_l4_verdict_cannot_be_overridden_by_low_risk_flag(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    observation = _held_observation(_adjudication("clear"))
    observation.update(ok=True, risk_level="low", clearance_certified=True)
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=observation,
        adjudicator_mode="enforce",
    )

    storage = await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        assert storage.copied == []


@pytest.mark.asyncio
async def test_v13_receipt_fence_survives_bound_enforce_setting(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=_held_observation(_adjudication("clear")),
        adjudicator_mode="enforce",
    )
    async with session_maker() as session, session.begin():
        session.add(
            ScreenerReviewSettingsRevision(
                revision=2,
                parent_revision=1,
                scope="*",
                settings=ScreenerReviewSettings(
                    mode="off", adjudicator_mode="off"
                ).model_dump(mode="json"),
                reason="close exact canary after its claim",
                actor="tests",
                checksum="cd" * 32,
            )
        )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"


@pytest.mark.asyncio
async def test_v13_l4_clear_holds_an_early_l1_provider_fault(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    observation = _held_observation(_adjudication("clear"))
    observation.update(
        error_code="source-review-model-response-invalid",
        failure_disposition="retryable_infra",
        notes=[],
    )
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=observation,
        adjudicator_mode="enforce",
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"


@pytest.mark.asyncio
async def test_v12_l4_clear_retains_legacy_admission(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=_held_observation(_adjudication("clear")),
        adjudicator_mode="enforce",
        policy_version=12,
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "passed"


@pytest.mark.asyncio
async def test_shadow_mode_records_the_decision_and_keeps_holding(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The posture is resolved at Platform, not trusted from the worker."""
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=_held_observation(_adjudication("reject")),
        adjudicator_mode="shadow",
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        quarantine = await session.scalar(
            ScreeningQuarantine.__table__.select().where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is not None


@pytest.mark.asyncio
async def test_an_escalated_adjudication_still_holds(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A refused decision is indistinguishable from having none: it holds."""
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=_held_observation(
            {
                "decision": "escalate",
                "reason": "Automated adjudication cited source it did not read",
                "citations": [],
                "notes_considered": 1,
                "model": "z-ai/glm-5.3-flash",
                "prompt_revision": "adjudicator-v2-policy-v10",
                "escalation_code": "cited-unread-source",
            }
        ),
        adjudicator_mode="enforce",
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"


@pytest.mark.asyncio
async def test_l4_escalation_terminally_holds_an_early_l1_provider_fault(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    observation = _held_observation(
        {
            "decision": "escalate",
            "reason": "Automated adjudication exhausted its bounded review",
            "citations": [],
            "notes_considered": 0,
            "model": "z-ai/glm-5.3-flash",
            "prompt_revision": "adjudicator-v2-policy-v10",
            "escalation_code": "adjudicator-failed",
        }
    )
    observation.update(
        error_code="source-review-model-response-invalid",
        failure_disposition="retryable_infra",
        notes=[],
    )
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=observation,
        adjudicator_mode="enforce",
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        assert attempt.reason_code == "agentic-source-review-tripwire"
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.QUARANTINED
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is not None


@pytest.mark.asyncio
async def test_adjudicator_failure_diagnostic_is_retained_on_the_hold(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A court crash stays a hold and keeps the sanitized trace."""
    diagnostic = {
        "error_class": "ValueError",
        "escalation_code": "adjudicator-failed",
        "timeout_stage": "response",
        "http_status": None,
        "elapsed_ms": 42,
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "final_tool_call_returned": True,
        "model": "z-ai/glm-5.3-flash",
        "provider": "openrouter",
        # The gateway is one value for every call; the upstream behind it is
        # what tells two bursts apart, so it has to survive the round trip.
        "upstream": "sail-research",
    }
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=_held_observation(
            {
                "decision": "escalate",
                "reason": (
                    "Automated adjudication did not complete; held for operator review"
                ),
                "citations": [],
                "notes_considered": 1,
                "model": "z-ai/glm-5.3-flash",
                "prompt_revision": "adjudicator-v2-policy-v10",
                "escalation_code": "adjudicator-failed",
                "run_diagnostic": diagnostic,
            }
        ),
        adjudicator_mode="enforce",
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        assert attempt.private_failure_detail is None
        assert attempt.private_failure_log_tail is None
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.QUARANTINED
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is not None
        # Older traces have no subtype; the forward-compatible wire model
        # records it as null without changing the adjudication outcome.
        assert quarantine.court_diagnostic == {
            **diagnostic,
            "failure_code": None,
            "response_bound_kind": None,
            "completion_ceiling_reached": None,
            "request_count": 0,
            "request_attempts": [],
        }
        assert quarantine.resolution is None


@pytest.mark.asyncio
async def test_an_unadjudicated_hold_is_unchanged(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    attempt_id = await _seed_held_screen(
        session_maker,
        observation=_held_observation(None),
        adjudicator_mode="enforce",
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"


# ── #562: the Targon hold retains its notes ledger for the public conclusion ──


def _budget_hold_observation(concern_lines: list[int]) -> dict[str, object]:
    """A read-budget-terminated L1 review with ``len(concern_lines)`` concerns."""
    return {
        "ok": False,
        "risk_level": None,
        "finding_digest": None,
        "categories": [],
        "error_code": "source-review-read-budget-exhausted",
        "finding": None,
        "failure_disposition": "inconclusive",
        "clearance_certified": False,
        "review_audit": ScreenReviewAudit(
            stage="l1",
            reason_code="source-review-read-budget-exhausted",
            prompt_revision="l1-v13",
            max_steps=240,
            steps_used=37,
            max_read_bytes=320_000,
            read_bytes_used=338_278,
        ).model_dump(mode="json"),
        "notes": [
            {
                "kind": "concern",
                "category": "none",
                "path": "src/main.rs",
                "line": line,
                "summary": "reads the grader's expected answer",
            }
            for line in concern_lines
        ]
        + [{"kind": "cleared", "category": "none", "summary": "tool loop is honest"}],
        "adjudication": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("concern_lines", "expected"),
    [
        # Three substantiated concerns reach the pinned default threshold (3):
        # the worker held on the concerns, so the public reads a concern.
        ([10, 20, 30], "adverse_signal"),
        # One concern is thin coverage: a budget hold with no finding.
        ([10], "budget_exhausted"),
    ],
)
async def test_targon_budget_hold_persists_notes_for_the_public_conclusion(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    concern_lines: list[int],
    expected: str,
) -> None:
    observation = _budget_hold_observation(concern_lines)
    attempt_id = await _seed_held_screen(
        session_maker, observation=observation, adjudicator_mode="off"
    )

    await _finalize(session_maker, attempt_id)

    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is not None
        assert quarantine.reason_code == "source-review-inconclusive"
        assert quarantine.review_audit is not None
        # The typed ledger is retained exactly as the host path retains it.
        notes = [
            SourceReviewNote.model_validate(note)
            for note in cast(list[object], observation["notes"])
        ]
        assert quarantine.review_notes == [
            note.model_dump(mode="json") for note in notes
        ]
        assert quarantine.review_notes_digest == source_review_notes_digest(notes)
        agent_id = attempt.agent_id

    _install_db(app, session_maker)
    summary = await client.get(f"/api/v1/public/agent/{agent_id}/summary")
    assert summary.status_code == 200
    assert summary.json()["status"] == "under_review"
    assert summary.json()["review_conclusion"] == expected
    assert "src/main.rs" not in summary.text
    assert "grader" not in summary.text
