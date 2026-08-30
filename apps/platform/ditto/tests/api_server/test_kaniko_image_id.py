from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_server.storage.client import S3StorageClient
from ditto.api_server.targon_screening import (
    maybe_finalize_targon_screen,
    repair_kaniko_screened_image_identities,
)
from ditto.db.models import (
    Agent,
    ScreenedImageUpload,
    ScreeningAttempt,
    ScreeningQuarantine,
    SubmissionImageBuild,
    SubmissionSourceReview,
)
from ditto.tests.api_server.endpoints.test_screener import (
    _SCREENER_HOTKEY,
    _SHA256,
    _seed_agent,
)

_AR_DIGEST = "sha256:" + "cd" * 32
_CONFIG_DIGEST = "sha256:" + "ab" * 32
_RUNTIME_REF = (
    "us-central1-docker.pkg.dev/ditto-app-dev/"
    "ditto-screening-candidates/miner@" + _AR_DIGEST
)


async def _seed_evaluating_kaniko(
    maker: async_sessionmaker[AsyncSession],
    *,
    screened_image_id: str,
    output_image_id: str | None = _CONFIG_DIGEST,
    runtime_image_reference: str | None = _RUNTIME_REF,
) -> tuple[UUID, UUID]:
    agent_id = await _seed_agent(maker, status=AgentStatus.EVALUATING)
    attempt_id = uuid4()
    image_upload_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="passed",
                started_at=now - timedelta(minutes=10),
                deadline=now + timedelta(minutes=60),
                finished_at=now - timedelta(minutes=1),
            )
        )
        await session.flush()
        session.add(
            ScreenedImageUpload(
                image_upload_id=image_upload_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_SCREENER_HOTKEY,
                storage_upload_id=f"storage-{image_upload_id}",
                sha256="12" * 32,
                size_bytes=123,
                image_id=screened_image_id,
                image_ref=f"ditto-screen/{agent_id}:latest",
                status="verified",
                expires_at=now + timedelta(hours=1),
                verified_at=now,
            )
        )
        session.add(
            SubmissionImageBuild(
                build_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                environment="prod",
                artifact_sha256=_SHA256,
                image_ref=f"ditto-screen/{agent_id}-{attempt_id}:latest",
                output_key=f"{agent_id}/builds/{attempt_id}.tar",
                status="consumed",
                provider="targon",
                output_sha256="12" * 32,
                output_size_bytes=123,
                output_image_id=output_image_id,
                runtime_status="succeeded",
                runtime_image_reference=runtime_image_reference,
                attempt_count=1,
                created_at=now - timedelta(minutes=10),
                started_at=now - timedelta(minutes=9),
                completed_at=now - timedelta(minutes=2),
                consumed_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        agent.screened_image_sha256 = "12" * 32
        agent.screened_image_size_bytes = 123
        agent.screened_image_id = screened_image_id
        agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
        agent.screened_image_upload_id = image_upload_id
        agent.screened_image_verified_at = now
    return agent_id, image_upload_id


@pytest.mark.asyncio
async def test_repair_repins_stored_tar_config_digest(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, _upload_id = await _seed_evaluating_kaniko(
        session_maker, screened_image_id=_AR_DIGEST, output_image_id=_CONFIG_DIGEST
    )

    repaired = await repair_kaniko_screened_image_identities(session_maker)

    assert repaired == 1
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.screened_image_id == _CONFIG_DIGEST
        upload = await session.get(ScreenedImageUpload, agent.screened_image_upload_id)
        assert upload is not None
        assert upload.image_id == _CONFIG_DIGEST


@pytest.mark.asyncio
async def test_repair_skips_already_pinned_tar_config_digest(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_evaluating_kaniko(
        session_maker, screened_image_id=_CONFIG_DIGEST, output_image_id=_CONFIG_DIGEST
    )

    repaired = await repair_kaniko_screened_image_identities(session_maker)

    assert repaired == 0


@pytest.mark.asyncio
async def test_repair_skips_when_builder_did_not_post_tar_config_digest(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id, _upload_id = await _seed_evaluating_kaniko(
        session_maker, screened_image_id=_AR_DIGEST, output_image_id=None
    )

    repaired = await repair_kaniko_screened_image_identities(session_maker)

    assert repaired == 0
    async with session_maker() as session:
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.screened_image_id == _AR_DIGEST


class _FakeStorage:
    def __init__(self) -> None:
        self.copied: list[tuple[str, str]] = []

    async def copy_object(self, *, source_key: str, dest_key: str) -> None:
        self.copied.append((source_key, dest_key))


async def _seed_running_kaniko(
    maker: async_sessionmaker[AsyncSession],
    *,
    output_image_id: str | None,
) -> UUID:
    agent_id = await _seed_agent(maker, status=AgentStatus.SCREENING)
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="running",
                build_only=True,
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
                output_image_id=output_image_id,
                runtime_status="succeeded",
                runtime_image_reference=_RUNTIME_REF,
                attempt_count=1,
                created_at=now - timedelta(minutes=10),
                started_at=now - timedelta(minutes=9),
                completed_at=now - timedelta(minutes=2),
                updated_at=now - timedelta(minutes=1),
            )
        )
    return attempt_id


@pytest.mark.asyncio
async def test_bind_uses_stored_tar_config_digest(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    attempt_id = await _seed_running_kaniko(
        session_maker, output_image_id=_CONFIG_DIGEST
    )
    storage = _FakeStorage()
    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, storage),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=datetime.now(UTC),
        )
    assert finalized is True
    assert storage.copied
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.EVALUATING
        assert agent.screened_image_id == _CONFIG_DIGEST


@pytest.mark.asyncio
async def test_bind_fails_closed_without_tar_config_digest(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    attempt_id = await _seed_running_kaniko(session_maker, output_image_id=None)
    storage = _FakeStorage()
    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, storage),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=datetime.now(UTC),
        )
    assert finalized is True
    assert storage.copied == []
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.reason_code == "targon-image-bind-failed"
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.SCREENING_FAILED
        assert agent.screened_image_id is None


@pytest.mark.parametrize(
    "error_code",
    (
        "TARGON_SUBMISSION_KANIKO_FAILED",
        "CLOUDRUN_SUBMISSION_KANIKO_FAILED",
        "FLEET_SUBMISSION_BUILDKIT_FAILED",
        "FLEET_SUBMISSION_BUILDKIT_LOCAL_CARGO_DEPENDENCY_MISSING_FAILED",
    ),
)
@pytest.mark.asyncio
async def test_finalize_rejects_submission_image_build_exit_as_docker_build(
    session_maker: async_sessionmaker[AsyncSession],
    error_code: str,
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING)
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="running",
                build_only=True,
                started_at=now - timedelta(minutes=3),
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
                status="fallback_required",
                provider="targon",
                error_code=error_code,
                runtime_status="pending",
                attempt_count=1,
                created_at=now - timedelta(minutes=3),
                completed_at=now - timedelta(seconds=5),
                updated_at=now - timedelta(seconds=5),
            )
        )
    storage = _FakeStorage()
    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, storage),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "rejected"
        assert attempt.reason_code == "docker-build"
        assert attempt.public_reason == "artifact Docker image did not build"
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.REJECTED
        assert agent.screening_reason_code == "docker-build"


@pytest.mark.asyncio
async def test_finalize_labels_exhausted_gcp_fallback_as_cloudrun(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING)
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="running",
                build_only=True,
                started_at=now - timedelta(minutes=3),
                deadline=now + timedelta(minutes=60),
            )
        )
        session.add(
            SubmissionImageBuild(
                build_id=uuid4(),
                agent_id=agent_id,
                attempt_id=attempt_id,
                environment="prod",
                artifact_sha256=_SHA256,
                image_ref=f"ditto-screen/{agent_id}-{attempt_id}:latest",
                output_key=f"{agent_id}/builds/{attempt_id}.tar",
                status="fallback_required",
                provider="gcp",
                error_code="CLOUDRUN_PROVISION_ERROR",
                runtime_status="pending",
                attempt_count=2,
                created_at=now - timedelta(minutes=3),
                completed_at=now - timedelta(seconds=5),
                updated_at=now - timedelta(seconds=5),
            )
        )

    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, _FakeStorage()),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.reason_code == "cloudrun-build-unavailable"
        assert attempt.public_reason == "Cloud Run submission build was unavailable"


@pytest.mark.asyncio
async def test_finalize_labels_cloudrun_runtime_provision_failure(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING)
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="running",
                build_only=True,
                started_at=now - timedelta(minutes=3),
                deadline=now + timedelta(minutes=60),
            )
        )
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
                runtime_status="fallback_required",
                runtime_error_code="CLOUDRUN_PROVISION_ERROR",
                attempt_count=1,
                created_at=now - timedelta(minutes=3),
                completed_at=now - timedelta(seconds=5),
                updated_at=now - timedelta(seconds=5),
            )
        )

    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, _FakeStorage()),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.reason_code == "cloudrun-runtime-unavailable"
        assert attempt.public_reason == "Cloud Run runtime smoke was unavailable"
        agent = await session.get(Agent, agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.SCREENING_FAILED
        assert agent.screening_reason_code == "cloudrun-runtime-unavailable"


@pytest.mark.asyncio
async def test_finalize_keeps_targon_label_for_runtime_timeout(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING)
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="running",
                build_only=True,
                started_at=now - timedelta(minutes=3),
                deadline=now + timedelta(minutes=60),
            )
        )
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
                runtime_status="fallback_required",
                runtime_error_code="TARGON_PROVISION_TIMEOUT",
                attempt_count=1,
                created_at=now - timedelta(minutes=3),
                completed_at=now - timedelta(seconds=5),
                updated_at=now - timedelta(seconds=5),
            )
        )

    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, _FakeStorage()),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.reason_code == "targon-runtime-unavailable"


async def _seed_completed_review(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    observation: dict,
) -> UUID:
    """A full-depth attempt whose lanes finished and whose review reported."""
    agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING)
    attempt_id = uuid4()
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        session.add(
            ScreeningAttempt(
                attempt_id=attempt_id,
                agent_id=agent_id,
                screener_hotkey=_SCREENER_HOTKEY,
                policy_version=SCREENING_POLICY_VERSION,
                status="running",
                build_only=False,
                started_at=now - timedelta(minutes=30),
                deadline=now + timedelta(minutes=40),
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
                provider="gcp",
                output_sha256="12" * 32,
                output_size_bytes=123,
                output_image_id=_CONFIG_DIGEST,
                runtime_status="succeeded",
                runtime_image_reference=_RUNTIME_REF,
                attempt_count=1,
                created_at=now - timedelta(minutes=30),
                completed_at=now - timedelta(minutes=20),
                updated_at=now - timedelta(minutes=20),
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
                provider="gcp",
                observation=observation,
                created_at=now - timedelta(minutes=20),
                completed_at=now - timedelta(minutes=1),
                updated_at=now - timedelta(minutes=1),
            )
        )
    return attempt_id


@pytest.mark.asyncio
async def test_finalize_retries_review_reported_retryable_infra(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A reviewer's own retryable_infra self-report is a retry, not a hold.

    The v10 court's model responses can fail validation
    (``source-review-model-response-invalid``); the worker marks those
    ``retryable_infra`` expecting the attempt to burn and rescreen.
    Quarantining them held miners under an anti-cheat label for an outage.
    """
    attempt_id = await _seed_completed_review(
        session_maker,
        observation={
            "ok": False,
            "categories": [],
            "error_code": "source-review-model-response-invalid",
            "failure_disposition": "retryable_infra",
            "clearance_certified": False,
        },
    )
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, _FakeStorage()),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "failed"
        assert attempt.reason_code == "source-review-retryable-infra"
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.SCREENING_FAILED
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is None


_REVIEW_AUDIT = {
    "stage": "l2",
    "reason_code": "l2-model-tool-budget",
    "prompt_revision": "l2-2026-08",
    "max_steps": 20,
    "steps_used": 17,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["inconclusive", "pass_inconclusive"])
async def test_finalize_still_holds_inconclusive_review_outcomes(
    session_maker: async_sessionmaker[AsyncSession],
    disposition: str,
) -> None:
    """Budget exhaustion without a notes ledger remains an operator hold.

    Production observations carry the reviewer's budget audit; storing it must
    satisfy the audit/digest/reason constraints, which is exactly what 500ed
    every platform-attested verdict before this regression test existed. A
    legacy ``pass_inconclusive`` with no notes stays fail-closed: only a
    ledger with cleared coverage earns admission (see the coverage test).
    """
    attempt_id = await _seed_completed_review(
        session_maker,
        observation={
            "ok": False,
            "categories": [],
            "error_code": "source-review-read-budget-exhausted",
            "failure_disposition": disposition,
            "clearance_certified": False,
            "review_audit": _REVIEW_AUDIT,
        },
    )
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, _FakeStorage()),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        assert attempt.reason_code == "source-review-inconclusive"
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.QUARANTINED
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is not None
        assert quarantine.reason_code == "source-review-inconclusive"
        assert quarantine.review_audit is not None
        assert quarantine.review_audit_digest is not None


@pytest.mark.asyncio
async def test_finalize_tripwire_quarantine_persists_review_audit(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """An elevated-risk verdict holds as a tripwire and keeps its audit."""
    attempt_id = await _seed_completed_review(
        session_maker,
        observation={
            "ok": True,
            "risk_level": "high",
            "categories": ["scorer_contract_manipulation"],
            "failure_disposition": "inconclusive",
            "clearance_certified": False,
            "review_audit": _REVIEW_AUDIT,
        },
    )
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, _FakeStorage()),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        assert attempt.reason_code == "agentic-source-review-tripwire"
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is not None
        assert quarantine.reason_code == "agentic-source-review-tripwire"
        assert quarantine.review_audit is not None
        assert quarantine.review_audit_digest is not None
        assert quarantine.review_audit_digest == quarantine.manifest_digest


@pytest.mark.asyncio
async def test_finalize_admits_exhausted_review_on_clean_coverage_notes(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Zero concerns + cleared coverage notes = admission, not a hold.

    The gradient contract: the worker emits ``pass_inconclusive`` only when
    an exhausted review recorded no concerns and enough positive coverage,
    so the platform admits it instead of parking the miner behind an
    evidence-free inconclusive.
    """
    attempt_id = await _seed_completed_review(
        session_maker,
        observation={
            "ok": False,
            "categories": [],
            "error_code": "source-review-step-budget-exhausted",
            "failure_disposition": "pass_inconclusive",
            "clearance_certified": False,
            "review_audit": _REVIEW_AUDIT,
            "notes": [
                {
                    "kind": "cleared",
                    "category": "none",
                    "summary": "Entrypoint routes to a genuine model call.",
                },
                {
                    "kind": "cleared",
                    "category": "none",
                    "summary": "Tool dispatch executes model-authored calls.",
                },
                {
                    "kind": "cleared",
                    "category": "none",
                    "summary": "Answer construction forwards model text.",
                },
            ],
        },
    )
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, _FakeStorage()),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "passed"
        assert attempt.public_reason is not None
        assert "coverage" in attempt.public_reason
        agent = await session.get(Agent, attempt.agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.EVALUATING
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is None


@pytest.mark.asyncio
async def test_finalize_holds_concern_notes_with_ledger_as_evidence(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """Concern notes hold the artifact and land on the quarantine evidence."""
    attempt_id = await _seed_completed_review(
        session_maker,
        observation={
            "ok": False,
            "categories": [],
            "error_code": "source-review-step-budget-exhausted",
            "failure_disposition": "inconclusive",
            "clearance_certified": False,
            "review_audit": _REVIEW_AUDIT,
            "notes": [
                {
                    "kind": "concern",
                    "category": "scorer_contract_manipulation",
                    "path": "src/main.rs",
                    "line": 7,
                    "summary": "Host gate rejects model drafts on derived values.",
                    "confidence": 0.8,
                },
                {
                    "kind": "cleared",
                    "category": "none",
                    "summary": "Retrieval is user-scoped.",
                },
            ],
        },
    )
    now = datetime.now(UTC)
    async with session_maker() as session, session.begin():
        finalized = await maybe_finalize_targon_screen(
            session,
            storage=cast(S3StorageClient, _FakeStorage()),
            screener_hotkey=_SCREENER_HOTKEY,
            attempt_id=attempt_id,
            now=now,
        )
    assert finalized is True
    async with session_maker() as session:
        attempt = await session.get(ScreeningAttempt, attempt_id)
        assert attempt is not None
        assert attempt.status == "quarantined"
        quarantine = await session.scalar(
            select(ScreeningQuarantine).where(
                ScreeningQuarantine.attempt_id == attempt_id
            )
        )
        assert quarantine is not None
        assert quarantine.evidence is not None
        codes = [row["code"] for row in quarantine.evidence]
        assert "concern:scorer_contract_manipulation" in codes
        assert any(
            row["module"] == "review-note"
            and row["summary"].startswith("src/main.rs:7")
            for row in quarantine.evidence
        )
