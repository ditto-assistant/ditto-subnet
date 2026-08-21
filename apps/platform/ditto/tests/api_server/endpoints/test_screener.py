"""Unit tests for :mod:`ditto.api_server.endpoints.screener`.

Exercise the real endpoints end to end against in-memory SQLite (real queries,
real status transitions) with chain + storage mocked. Signatures use a real
sr25519 dev keypair so the verification path runs for real.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import tarfile
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.screener import (
    SCREENING_POLICY_VERSION,
    ScreenerHeartbeatRequest,
    SourceReviewEvidenceItem,
    SourceReviewFinding,
)
from ditto.api_models.screener_review_settings import ScreenerReviewSettings
from ditto.api_models.system_health import (
    SystemMetrics,
    system_metrics_signing_token,
)
from ditto.api_models.ticket_status import TicketStatus
from ditto.api_server.datapipeline import DataPipelineError, NullGenerator
from ditto.api_server.dependencies import (
    get_chain_client,
    get_dataset_generator,
    get_session,
    get_storage_client,
)
from ditto.api_server.endpoints.public import screening_dispute_signing_message
from ditto.api_server.endpoints.screener import (
    _heartbeat_signing_message,
    _public_screening_reason,
    _review_settings_checksum,
)
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_AGENT_NOT_FOUND,
    ERROR_CODE_AGENT_NOT_SCREENABLE,
    ERROR_CODE_SCREENER_AUTH,
    ERROR_CODE_VALIDATION,
)
from ditto.api_server.storage import (
    ObjectMetadata,
    ObjectNotFoundError,
    VerifiedObject,
)
from ditto.chain import ChainError
from ditto.chain.models import BlockInfo, NeuronInfo
from ditto.db.models import (
    Agent,
    ArtifactFetchAudit,
    ArtifactReleaseSettingsRevision,
    AthReview,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    EvaluationPayment,
    Score,
    ScoreAuditEntry,
    ScreenedImageUpload,
    ScreenerCapacityEvent,
    ScreenerHeartbeat,
    ScreenerProviderSettingsRevision,
    ScreenerReviewSettingsRevision,
    ScreenerShadowReview,
    ScreeningAttempt,
    ScreeningDispute,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ScreeningRetryOverride,
    SubmissionImageBuild,
    SubmissionSourceReview,
    TrustedImageBuild,
    ValidatorTicket,
)
from ditto.db.queries.attestation import record_attestation
from ditto.db.queries.screening import MAX_SCREENING_EXPIRIES
from ditto.db.queries.tickets import issue_ticket, ticket_attempt_cap
from ditto.tests.legacy_era import retired_era_writes_allowed
from ditto_screening_protocol import (
    ScreenResultOutcome,
    ScreenReviewAudit,
    verdict_signing_message,
)

_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_SCREENER_HOTKEY = _KEYPAIR.ss58_address
_MINER_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_SHA256 = "ab" * 32
# A fixed block the mocked chain returns for on-chain seed derivation.
_BLOCK = BlockInfo(number=4321, hash="0x" + "9f" * 32, timestamp=0)


def _sign(message: str | bytes) -> str:
    return _KEYPAIR.sign(
        message.encode() if isinstance(message, str) else message
    ).hex()


def test_public_rust_contract_reason_is_actionable() -> None:
    detail = (
        "error[SCR-RUST-002]: archive contains a duplicate path\n\n"
        "help: package each path exactly once"
    )

    assert _public_screening_reason(detail, "rust-harness-contract") == (
        "Rust harness contract failed (SCR-RUST-002): archive contains a duplicate "
        "path. Package each path exactly once."
    )


def test_unknown_rust_contract_detail_stays_public_safe() -> None:
    reason = _public_screening_reason(
        "error[SCR-RUST-999]: SECRET_FROM_UNTRUSTED_DETAIL",
        "rust-harness-contract",
    )

    assert reason.startswith("Submission does not satisfy the Rust harness contract")
    assert "SECRET_FROM_UNTRUSTED_DETAIL" not in reason


def test_public_container_contract_reason_is_language_neutral() -> None:
    detail = (
        "error[SCR-CONTRACT-001]: Dockerfile is missing from the archive root\n\n"
        "help: package the harness contents so Dockerfile is at the top level"
    )

    assert _public_screening_reason(detail, "container-harness-contract") == (
        "Container harness contract failed (SCR-CONTRACT-001): Dockerfile is "
        "missing from the archive root. Package the harness contents so Dockerfile "
        "is at the top level."
    )


def test_unknown_container_contract_detail_stays_public_safe() -> None:
    reason = _public_screening_reason(
        "error[SCR-CONTRACT-999]: SECRET_FROM_UNTRUSTED_DETAIL",
        "container-harness-contract",
    )

    assert reason.startswith(
        "Submission does not satisfy the container harness contract"
    )
    assert "SECRET_FROM_UNTRUSTED_DETAIL" not in reason


@pytest.mark.parametrize(
    ("detail", "reason_code", "expected"),
    [
        (
            "build failed: error: couldn't read `src/private-name.rs`: No such "
            "file or directory (os error 2)\nSECRET_FROM_BUILD",
            "docker-build",
            "Docker image build failed: source code referenced by the build is "
            "missing from the submitted archive.",
        ),
        (
            "build failed: failed to calculate checksum: /private-name: not found",
            "docker-build",
            "Docker image build failed: Dockerfile COPY references a path that is "
            "missing from the submitted archive.",
        ),
        (
            "screener error: Docker build infrastructure: failed to Lchown "
            "Dockerfile for UID 197108",
            "docker-build-infrastructure",
            "Docker build infrastructure failed before screening completed. This "
            "is an operator-owned, retryable failure.",
        ),
    ],
)
def test_public_docker_build_reason_is_actionable_and_redacted(
    detail: str, reason_code: str, expected: str
) -> None:
    reason = _public_screening_reason(detail, reason_code)

    assert reason == expected
    assert "private-name" not in reason
    assert "SECRET_FROM_BUILD" not in reason
    assert "197108" not in reason


def _result_payload(
    agent_id: UUID,
    *,
    passed: bool = True,
    policy_version: int = SCREENING_POLICY_VERSION,
    **overrides: object,
) -> dict:
    attempt_id = overrides.get("attempt_id")
    if (
        not isinstance(attempt_id, UUID)
        and policy_version == SCREENING_POLICY_VERSION
        and "outcome" not in overrides
    ):
        # Legacy no-attempt fixtures exercise the rolling compatibility path;
        # policy 9 itself requires an attempt-bound typed outcome.
        policy_version = SCREENING_POLICY_VERSION - 1
    if passed and isinstance(attempt_id, UUID):
        overrides.setdefault("outcome", ScreenResultOutcome.PASS)
        overrides.setdefault("image_sha256", "12" * 32)
        overrides.setdefault("image_size_bytes", 123)
        overrides.setdefault("image_id", "sha256:" + "34" * 32)
        overrides.setdefault("image_ref", f"ditto-screen/{agent_id}:latest")
        overrides.setdefault(
            "image_upload_id",
            uuid5(NAMESPACE_URL, f"{agent_id}:{attempt_id}:screened-image"),
        )
    outcome_raw = overrides.get("outcome")
    outcome = ScreenResultOutcome(outcome_raw) if isinstance(outcome_raw, str) else None
    signed = (
        verdict_signing_message(
            screener_hotkey=_SCREENER_HOTKEY,
            agent_id=agent_id,
            attempt_id=attempt_id,
            passed=passed,
            policy_version=policy_version,
            outcome=outcome,
            manifest_digest=overrides.get("manifest_digest")
            if isinstance(overrides.get("manifest_digest"), str)
            else None,
            finding_digest=overrides.get("finding_digest")
            if isinstance(overrides.get("finding_digest"), str)
            else None,
            review_audit_digest=overrides.get("review_audit_digest")
            if isinstance(overrides.get("review_audit_digest"), str)
            else None,
            deferred_source_review=bool(overrides.get("deferred_source_review", False)),
            review_settings_revision=overrides.get("review_settings_revision")
            if isinstance(overrides.get("review_settings_revision"), int)
            else None,
            review_settings_instance_id=overrides.get("review_settings_instance_id")
            if isinstance(overrides.get("review_settings_instance_id"), str)
            else None,
            review_settings_scope=overrides.get("review_settings_scope")
            if isinstance(overrides.get("review_settings_scope"), str)
            else None,
            review_settings_checksum=overrides.get("review_settings_checksum")
            if isinstance(overrides.get("review_settings_checksum"), str)
            else None,
            reason_code=overrides.get("reason_code")
            if isinstance(overrides.get("reason_code"), str)
            else None,
            image_sha256=overrides.get("image_sha256")
            if isinstance(overrides.get("image_sha256"), str)
            else None,
            image_size_bytes=overrides.get("image_size_bytes")
            if isinstance(overrides.get("image_size_bytes"), int)
            else None,
            image_id=overrides.get("image_id")
            if isinstance(overrides.get("image_id"), str)
            else None,
            image_ref=overrides.get("image_ref")
            if isinstance(overrides.get("image_ref"), str)
            else None,
            image_upload_id=overrides.get("image_upload_id")
            if isinstance(overrides.get("image_upload_id"), UUID)
            else None,
        )
        if isinstance(attempt_id, UUID)
        else f"{_SCREENER_HOTKEY}:{agent_id}:{passed}:{policy_version}"
    )
    body = {
        "screener_hotkey": _SCREENER_HOTKEY,
        "signature": _sign(signed),
        "passed": passed,
        "policy_version": policy_version,
        "detail": "",
    }
    body.update(overrides)
    if isinstance(body.get("attempt_id"), UUID):
        body["attempt_id"] = str(body["attempt_id"])
    if isinstance(body.get("image_upload_id"), UUID):
        body["image_upload_id"] = str(body["image_upload_id"])
    return body


async def _seed_verified_image_upload(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    attempt_id: UUID,
) -> UUID:
    """Persist the completed multipart proof required by a policy-9 PASS."""
    image_upload_id = uuid5(NAMESPACE_URL, f"{agent_id}:{attempt_id}:screened-image")
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            ScreenedImageUpload(
                image_upload_id=image_upload_id,
                agent_id=agent_id,
                attempt_id=attempt_id,
                screener_hotkey=_SCREENER_HOTKEY,
                storage_upload_id=f"storage-{image_upload_id}",
                sha256="12" * 32,
                size_bytes=123,
                image_id="sha256:" + "34" * 32,
                image_ref=f"ditto-screen/{agent_id}:latest",
                status="verified",
                expires_at=now + timedelta(minutes=15),
                verified_at=now,
            )
        )
    return image_upload_id


def _heartbeat_payload(
    *,
    timestamp: int | None = None,
    state: str = "polling",
    active_agent_id: UUID | None = None,
    protocol_version: int = 1,
    instance_id: str | None = None,
    progress: dict[str, object] | None = None,
    system_metrics: dict[str, object] | None = None,
    review_settings: dict[str, object] | None = None,
) -> dict[str, object]:
    ts = timestamp if timestamp is not None else int(datetime.now(UTC).timestamp())
    metrics = (
        SystemMetrics.model_validate(system_metrics)
        if system_metrics is not None
        else None
    )
    progress_token = (
        f"{progress['stage']},{progress['started_at']}" if progress else "-"
    )
    if protocol_version == 1:
        message = (
            "ditto-screener-heartbeat:v1:"
            f"{_SCREENER_HOTKEY}:0.4.2:1:{SCREENING_POLICY_VERSION}:{state}:"
            f"{active_agent_id or ''}:{system_metrics_signing_token(metrics)}:{ts}"
        ).encode()
    elif protocol_version >= 4:
        assert review_settings is not None
        review_token = ",".join(
            str(review_settings[key])
            for key in ("revision", "scope", "mode", "checksum", "source")
        )
        message = (
            "ditto-screener-heartbeat:v4:"
            f"{_SCREENER_HOTKEY}:0.4.2:{protocol_version}:"
            f"{SCREENING_POLICY_VERSION}:{state}:{active_agent_id or ''}:{instance_id}:"
            f"{progress_token}:{system_metrics_signing_token(metrics)}:"
            f"{review_token}:{ts}"
        ).encode()
    elif protocol_version >= 3:
        message = (
            "ditto-screener-heartbeat:v3:"
            f"{_SCREENER_HOTKEY}:0.4.2:{protocol_version}:"
            f"{SCREENING_POLICY_VERSION}:{state}:{active_agent_id or ''}:{instance_id}:"
            f"{progress_token}:{system_metrics_signing_token(metrics)}:{ts}"
        ).encode()
    else:
        message = (
            "ditto-screener-heartbeat:v2:"
            f"{_SCREENER_HOTKEY}:0.4.2:{protocol_version}:"
            f"{SCREENING_POLICY_VERSION}:{state}:{active_agent_id or ''}:"
            f"{progress_token}:{system_metrics_signing_token(metrics)}:{ts}"
        ).encode()
    payload: dict[str, object] = {
        "screener_hotkey": _SCREENER_HOTKEY,
        "software_version": "0.4.2",
        "protocol_version": protocol_version,
        "policy_version": SCREENING_POLICY_VERSION,
        "state": state,
        "timestamp": ts,
        "signature": _sign(message),
    }
    if active_agent_id is not None:
        payload["active_agent_id"] = str(active_agent_id)
    if instance_id is not None:
        payload["instance_id"] = instance_id
    if progress is not None:
        payload["progress"] = progress
    if system_metrics is not None:
        payload["system_metrics"] = system_metrics
    if review_settings is not None:
        payload["review_settings"] = review_settings
    return payload


@pytest.mark.parametrize(
    "stage",
    [
        "preparing",
        "downloading",
        "validating",
        "building",
        "starting",
        "health_check",
        "submitting",
    ],
)
def test_v2_canonical_signing_matches_screener_contract(stage: str) -> None:
    payload = _heartbeat_payload(
        timestamp=456,
        state="screening",
        active_agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
        protocol_version=2,
        progress={"stage": stage, "started_at": 400},
    )
    request = ScreenerHeartbeatRequest.model_validate(payload)
    assert (
        _heartbeat_signing_message(request)
        == (
            "ditto-screener-heartbeat:v2:"
            f"{_SCREENER_HOTKEY}:0.4.2:2:{SCREENING_POLICY_VERSION}:screening:"
            f"550e8400-e29b-41d4-a716-446655440000:{stage},400:-:456"
        ).encode()
    )


# --- DB + dependency wiring ------------------------------------------------


# The transition the rollout-shaped tests below run against. It used to be the
# arbitrary 2 -> 3 (and once 2 -> 4); nothing in these tests is about which two
# eras they are, only that one succeeds the other. The floor makes the choice
# for us: ``benchmark_rollout_desired_floor`` refuses a target under
# MIN_SCOREABLE_BENCH_VERSION and v7 is the newest shipped contract, so the only
# transition that can be both open and functional is the real 6 -> 7 -- which
# also means the SOURCE era is retired, and the one source-era rollout row
# seeded below needs ``retired_era_writes_allowed`` the way production needed
# NOT VALID.
_SOURCE_VERSION = 6
_TARGET_VERSION = 7


class _FakeGenerator:
    """Test double for the dataset generator: pins a fixed hash, or raises."""

    def __init__(
        self, *, run_size: str = "full", sha: str = "ca" * 32, fail: bool = False
    ):
        self.run_size: str | None = run_size
        self._sha = sha
        self._fail = fail
        self.calls = 0
        self.bench_versions: list[int] = []
        self.seeds: list[int] = []

    async def generate(self, seed: int, bench_version: int = 2) -> str:
        self.calls += 1
        self.seeds.append(seed)
        self.bench_versions.append(bench_version)
        if self._fail:
            raise DataPipelineError("generate service unavailable (test)")
        return self._sha

    async def aclose(self) -> None:
        return None


def _install_db(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    # Default: generation disabled (NullGenerator) so the existing verdict tests
    # promote without pinning a dataset. Tests that exercise the pinned path call
    # _install_generator afterward to override.
    app.dependency_overrides.setdefault(get_dataset_generator, lambda: NullGenerator())


def _install_generator(app: FastAPI, generator: object) -> None:
    app.dependency_overrides[get_dataset_generator] = lambda: generator


def _install_chain(
    app: FastAPI,
    *,
    permitted: bool = True,
    registered: bool = True,
    block: BlockInfo | None = _BLOCK,
    block_error: bool = False,
) -> None:
    neurons = []
    if registered:
        neurons.append(
            NeuronInfo(
                hotkey=_SCREENER_HOTKEY,
                coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                uid=1,
                stake=1000.0,
                validator_permit=permitted,
            )
        )

    async def _chain() -> MagicMock:
        c = MagicMock()
        c.get_recent_neurons = AsyncMock(return_value=neurons)
        if block_error:
            c.get_latest_block = AsyncMock(side_effect=ChainError("pylon down"))
        else:
            c.get_latest_block = AsyncMock(return_value=block)
        return c

    app.dependency_overrides[get_chain_client] = _chain


def _install_storage(app: FastAPI) -> MagicMock:
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock(
        return_value="https://signed.example/ditto-agents/x.tar.gz?sig=1"
    )
    storage.presigned_put_url = AsyncMock(
        return_value="https://signed.example/ditto-agents/x-image.tar?sig=1"
    )
    storage.create_multipart_upload = AsyncMock(return_value="storage-upload-1")
    storage.presigned_upload_part_url = AsyncMock(
        return_value="https://signed.example/ditto-agents/x-image-part?sig=1"
    )
    storage.complete_multipart_upload = AsyncMock()
    storage.abort_multipart_upload = AsyncMock()
    storage.delete_object = AsyncMock()
    storage.copy_object = AsyncMock()
    storage.download_object_to_path = AsyncMock()
    storage.object_exists = AsyncMock(return_value=True)
    storage.verify_object_sha256 = AsyncMock(
        return_value=VerifiedObject(size_bytes=123, sha256="12" * 32)
    )

    async def _head(*, key: str) -> ObjectMetadata:
        agent_id = key.split("/", 1)[0]
        return ObjectMetadata(
            size_bytes=123,
            metadata={
                "sha256": "12" * 32,
                "image-id": "sha256:" + "34" * 32,
                "image-ref": f"ditto-screen/{agent_id}:latest",
            },
        )

    storage.head_object = AsyncMock(side_effect=_head)

    async def _storage() -> MagicMock:
        return storage

    app.dependency_overrides[get_storage_client] = _storage
    return storage


async def _seed_agent(
    maker: async_sessionmaker[AsyncSession],
    *,
    status: AgentStatus,
    name: str = "alpha-agent",
    created_at: datetime | None = None,
    agent_id: UUID | None = None,
    screening_policy_version: int | None = None,
    miner_hotkey: str = _MINER_HOTKEY,
    sha256: str = _SHA256,
    version: int | None = None,
    miner_coldkey: str | None = None,
) -> UUID:
    aid = agent_id or uuid4()
    async with maker() as s, s.begin():
        created = created_at or datetime.now(UTC)
        agent = Agent(
            agent_id=aid,
            miner_hotkey=miner_hotkey,
            name=name,
            version=version,
            sha256=sha256,
            status=status,
            screening_policy_version=(
                SCREENING_POLICY_VERSION
                if screening_policy_version is None and status == AgentStatus.EVALUATING
                else (screening_policy_version or 0)
            ),
            created_at=created,
        )
        s.add(agent)
        await s.flush()
        if miner_coldkey is not None:
            s.add(
                EvaluationPayment(
                    block_hash=f"0x{aid.hex}",
                    extrinsic_index=0,
                    agent_id=aid,
                    miner_hotkey=miner_hotkey,
                    miner_coldkey=miner_coldkey,
                    amount_rao=1,
                    dest_address="5Destination",
                    timestamp=created,
                )
            )
    return aid


async def _seed_score(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
    validator_hotkey: str = "5ScoreValidatorHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXX",
    composite: float = 0.5,
) -> None:
    async with maker() as session, session.begin():
        session.add(
            Score(
                agent_id=agent_id,
                validator_hotkey=validator_hotkey,
                run_id=str(uuid4()),
                signature=None,
                seed=1,
                composite=composite,
                tool_mean=composite,
                memory_mean=composite,
                median_ms=100,
                n=1,
                details=None,
                generated_at=datetime.now(UTC),
            )
        )


_AUTH_HEADER = {
    "Authorization": "Bearer test-screener-token-at-least-32-characters",
    "X-Screener-Hotkey": _SCREENER_HOTKEY,
}
_CLAIM_URL = f"/api/v1/screener/claim?policy_version={SCREENING_POLICY_VERSION}"
_CONTROLLER_TOKEN = "test-controller-token-at-least-32-characters"


def _bounded_review_audit(*, steps_used: int = 6) -> ScreenReviewAudit:
    return ScreenReviewAudit(
        stage="l1",
        reason_code="source-review-inconclusive",
        prompt_revision="source-review-v9",
        harness_revision="policy-v9",
        max_steps=8,
        steps_used=steps_used,
        max_read_bytes=4_000_000,
        read_bytes_used=2_000_000,
        max_input_tokens=200_000,
        input_tokens_used=120_000,
        max_output_tokens=32_000,
        output_tokens_used=20_000,
        max_cost_usd=5.0,
        cost_usd_used=3.0,
    )


@pytest.fixture(autouse=True)
def _authenticate_screener_client(client: httpx.AsyncClient) -> None:
    client.headers.update(_AUTH_HEADER)


def _capacity_payload(epoch: str) -> dict[str, object]:
    return {
        "environment": "prod",
        "controller_epoch": epoch,
        "controller_source_sha": "a" * 40,
        "provider_settings_revision": 0,
        "provider_ready": True,
        "runnable_backlog": 0,
        "active_leases": 0,
        "desired_slots": 0,
        "global_cap": 6,
        "targon_capability": "nogo",
        "targon_available": 6,
        "targon_healthy": 0,
        "targon_pending": 0,
        "targon_draining": 0,
        "gce_target": 0,
        "gce_healthy": 0,
        "gce_pending": 0,
        "gce_draining": 0,
        "fallback_reason": "ROOTLESSKIT_OPERATION_NOT_PERMITTED",
        "events": [],
    }


class TestFederatedScreenerNodes:
    async def test_submission_source_review_is_attempt_bound_and_digest_verified(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        async with session_maker() as session, session.begin():
            session.add(
                TrustedImageBuild(
                    build_id=uuid4(),
                    environment="prod",
                    component="screener",
                    source_repository=(
                        "https://github.com/ditto-assistant/ditto-subnet.git"
                    ),
                    source_sha="a" * 40,
                    context_path=".",
                    dockerfile_path="workers/screener/Dockerfile",
                    destination=(
                        "us-central1-docker.pkg.dev/ditto-app-dev/"
                        "ditto-public-runtime/screener:sha-test"
                    ),
                    status="succeeded",
                    provider="targon",
                    image_digest="sha256:" + "b" * 64,
                    completed_at=datetime.now(UTC),
                    created_by="test",
                    reason="provide a pinned reviewed source worker image",
                )
            )
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]
        queued = await client.post(
            f"/api/v1/screener/agent/{agent_id}/submission-source-reviews",
            headers=_AUTH_HEADER,
            json={"attempt_id": attempt_id},
        )
        assert queued.status_code == 200, queued.text
        review_id = queued.json()["review_id"]
        controller_headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        leased = await client.post(
            "/api/v1/screener/controller/submission-source-reviews/claim",
            headers=controller_headers,
            json={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert leased.status_code == 200, leased.text
        job = leased.json()["review"]
        assert job["review_id"] == review_id
        assert job["image_reference"].endswith("@sha256:" + "b" * 64)
        running = await client.put(
            f"/api/v1/screener/controller/submission-source-reviews/{review_id}",
            headers=controller_headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "status": "running",
                "provider_resource_id": "wrk-source-review",
            },
        )
        assert running.status_code == 204, running.text
        job_headers = {"Authorization": f"Bearer {job['job_token']}"}
        source = await client.get(
            f"/api/v1/screener/submission-source-reviews/{review_id}/source",
            headers=job_headers,
        )
        assert source.status_code == 200, source.text
        assert source.json()["artifact_sha256"] == _SHA256
        complete = await client.post(
            f"/api/v1/screener/submission-source-reviews/{review_id}/complete",
            headers=job_headers,
            json={
                "observation": {
                    "ok": True,
                    "risk_level": "low",
                    "categories": [],
                    "clearance_certified": True,
                }
            },
        )
        assert complete.status_code == 200, complete.text
        cleanup = await client.post(
            f"/api/v1/screener/controller/submission-source-reviews/{review_id}"
            "/cleanup-required",
            headers=controller_headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "provider_resource_id": "wrk-source-review",
            },
        )
        assert cleanup.status_code == 204, cleanup.text
        ready = await client.get(
            f"/api/v1/screener/agent/{agent_id}/submission-source-reviews/{review_id}",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id},
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["observation"]["clearance_certified"] is True
        async with session_maker() as session:
            row = await session.get(SubmissionSourceReview, UUID(review_id))
            assert row is not None
            assert row.job_token_hash is None
            assert row.status == "succeeded"
            assert row.error_code is None
            events = list(
                await session.scalars(
                    select(ScreenerCapacityEvent).where(
                        ScreenerCapacityEvent.event_type == "provider_cleanup_required"
                    )
                )
            )
            assert len(events) == 1
            assert events[0].provider == "targon"
            assert "source-review" in events[0].detail

    async def test_submission_build_is_attempt_bound_and_fully_verified(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]
        queued = await client.post(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
            headers=_AUTH_HEADER,
            json={"attempt_id": attempt_id},
        )
        assert queued.status_code == 200, queued.text
        build_id = queued.json()["build_id"]
        assert queued.json()["status"] == "queued"
        assert "download_url" in queued.json() and queued.json()["download_url"] is None

        controller_headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        leased = await client.post(
            "/api/v1/screener/controller/submission-image-builds/claim",
            headers=controller_headers,
            json={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert leased.status_code == 200, leased.text
        job = leased.json()["build"]
        assert job["build_id"] == build_id
        job_token = job["job_token"]
        async with session_maker() as session:
            row = await session.get(SubmissionImageBuild, UUID(build_id))
            assert row is not None
            assert row.job_token_hash != job_token
            assert row.job_token_hash is not None and len(row.job_token_hash) == 64

        running = await client.put(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}",
            headers=controller_headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "status": "running",
                "provider_resource_id": "wrk-attempt-bound",
            },
        )
        assert running.status_code == 204, running.text
        controller_status = await client.get(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}",
            headers=controller_headers,
            params={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert controller_status.status_code == 200, controller_status.text
        assert controller_status.json()["status"] == "running"
        assert set(controller_status.json()) == {"build_id", "status"}
        job_headers = {"Authorization": f"Bearer {job_token}"}
        source = await client.get(
            f"/api/v1/screener/submission-image-builds/{build_id}/source",
            headers=job_headers,
        )
        assert source.status_code == 200, source.text
        assert base64.b64decode(source.json()["source_url_b64"]).startswith(b"https://")
        assert source.json()["artifact_sha256"] == _SHA256

        output_sha = "12" * 32
        image_id = "sha256:" + "ab" * 32
        upload = await client.post(
            f"/api/v1/screener/submission-image-builds/{build_id}/upload",
            headers=job_headers,
            json={
                "output_sha256": output_sha,
                "output_size_bytes": 123,
                "image_id": image_id,
            },
        )
        assert upload.status_code == 200, upload.text
        assert base64.b64decode(upload.json()["upload_url_b64"]).startswith(b"https://")
        required = upload.json()["required_headers"]
        assert required["Content-Length"] == "123"
        assert required["x-amz-meta-artifact-sha256"] == _SHA256
        expected_metadata = {
            "sha256": output_sha,
            "build-id": build_id,
            "attempt-id": attempt_id,
            "artifact-sha256": _SHA256,
        }
        storage.head_object.side_effect = None
        storage.head_object.return_value = ObjectMetadata(
            size_bytes=123, metadata=expected_metadata
        )
        storage.verify_object_sha256.return_value = VerifiedObject(
            size_bytes=123, sha256=output_sha
        )
        complete = await client.post(
            f"/api/v1/screener/submission-image-builds/{build_id}/complete",
            headers=job_headers,
            json={
                "output_sha256": output_sha,
                "output_size_bytes": 123,
                "image_id": image_id,
            },
        )
        assert complete.status_code == 200, complete.text
        assert complete.json() == {"verified": True}
        async with session_maker() as session:
            stored = await session.get(SubmissionImageBuild, UUID(build_id))
            assert stored is not None
            assert stored.output_image_id == image_id
        controller_complete = await client.get(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}",
            headers=controller_headers,
            params={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert controller_complete.status_code == 200, controller_complete.text
        assert controller_complete.json()["status"] == "succeeded"
        storage.verify_object_sha256.assert_awaited_with(
            key=f"remote-builds/{build_id}/image.tar", expected_size_bytes=123
        )

        runtime = await client.post(
            "/api/v1/screener/controller/submission-runtime-smokes/claim",
            headers=controller_headers,
            json={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert runtime.status_code == 200, runtime.text
        assert runtime.json()["artifact"]["build_id"] == build_id
        assert base64.b64decode(
            runtime.json()["artifact"]["archive_url_b64"]
        ).startswith(b"https://")
        runtime_fallback = await client.post(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}/runtime-result",
            headers=controller_headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "status": "fallback_required",
                "provider_resource_id": "wrk-runtime",
                "error_code": "TARGON_RUNTIME_HEALTH_FAILED",
            },
        )
        assert runtime_fallback.status_code == 204, runtime_fallback.text

        runtime_cleanup = await client.post(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}"
            "/runtime-cleanup-required",
            headers=controller_headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "provider_resource_id": "wrk-runtime",
            },
        )
        assert runtime_cleanup.status_code == 204, runtime_cleanup.text

        ready = await client.get(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds/{build_id}",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id},
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["status"] == "succeeded"
        assert ready.json()["runtime_status"] == "fallback_required"
        assert ready.json()["output_sha256"] == output_sha
        assert ready.json()["download_url"].startswith("https://")

        cleanup = await client.post(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}/cleanup-required",
            headers=controller_headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "provider_resource_id": "wrk-attempt-bound",
            },
        )
        assert cleanup.status_code == 204, cleanup.text
        async with session_maker() as session:
            events = list(
                await session.scalars(
                    select(ScreenerCapacityEvent).where(
                        ScreenerCapacityEvent.event_type == "provider_cleanup_required"
                    )
                )
            )
            assert len(events) == 2
            assert all(event.provider == "targon" for event in events)
            assert any("zero-replica" in event.detail for event in events)
            assert any("runtime-smoke" in event.detail for event in events)

        consumed = await client.delete(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds/{build_id}",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id},
        )
        assert consumed.status_code == 204, consumed.text
        storage.delete_object.assert_awaited_with(
            key=f"remote-builds/{build_id}/image.tar"
        )
        async with session_maker() as session:
            row = await session.get(SubmissionImageBuild, UUID(build_id))
            assert row is not None and row.status == "consumed"

    async def test_runtime_success_queues_source_review(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]
        queued = await client.post(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
            headers=_AUTH_HEADER,
            json={"attempt_id": attempt_id},
        )
        assert queued.status_code == 200, queued.text
        build_id = queued.json()["build_id"]
        async with session_maker() as session, session.begin():
            row = await session.get(SubmissionImageBuild, UUID(build_id))
            assert row is not None
            row.status = "succeeded"
            row.output_sha256 = "12" * 32
            row.output_size_bytes = 123
            row.runtime_status = "running"
            row.controller_epoch = "builder:test"
            row.completed_at = datetime.now(UTC)
        finished = await client.post(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}/runtime-result",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "status": "succeeded",
                "provider_resource_id": "wrk-runtime",
                "image_reference": (
                    "us-central1-docker.pkg.dev/ditto-app-dev/"
                    "ditto-screening-candidates/miner@sha256:" + "ab" * 32
                ),
            },
        )
        assert finished.status_code == 204, finished.text
        async with session_maker() as session:
            review = await session.scalar(
                select(SubmissionSourceReview).where(
                    SubmissionSourceReview.attempt_id == UUID(attempt_id)
                )
            )
            assert review is not None
            assert review.status == "queued"
            assert review.artifact_sha256 == _SHA256

    async def test_controller_claim_admits_uploaded_agent_without_gce_worker(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        claimed = await client.post(
            "/api/v1/screener/controller/submission-image-builds/claim",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert claimed.status_code == 200, claimed.text
        job = claimed.json()["build"]
        assert job is not None
        assert job["agent_id"] == str(agent_id)
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.SCREENING

    async def test_controller_claim_is_refused_when_platform_owns_miner_rentals(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        app.state.targon_rental_loop = object()
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        claimed = await client.post(
            "/api/v1/screener/controller/submission-image-builds/claim",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json={
                "environment": "prod",
                "controller_epoch": "builder:ditto-screener-capacity-prod:1",
            },
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["build"] is None
        smoke = await client.post(
            "/api/v1/screener/controller/submission-runtime-smokes/claim",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json={
                "environment": "prod",
                "controller_epoch": "builder:ditto-screener-capacity-prod:1",
            },
        )
        assert smoke.status_code == 200, smoke.text
        assert smoke.json()["artifact"] is None
        review = await client.post(
            "/api/v1/screener/controller/submission-source-reviews/claim",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json={
                "environment": "prod",
                "controller_epoch": "builder:ditto-screener-capacity-prod:1",
            },
        )
        assert review.status_code == 200, review.text
        assert review.json()["review"] is None

    async def test_platform_attests_targon_pass_without_screener_signature(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        generator = _FakeGenerator(sha="ee" * 32)
        _install_generator(app, generator)
        config_digest = "ab" * 32
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        async with session_maker() as session, session.begin():
            session.add(
                TrustedImageBuild(
                    build_id=uuid4(),
                    environment="prod",
                    component="screener",
                    source_repository=(
                        "https://github.com/ditto-assistant/ditto-subnet.git"
                    ),
                    source_sha="a" * 40,
                    context_path=".",
                    dockerfile_path="workers/screener/Dockerfile",
                    destination=(
                        "us-central1-docker.pkg.dev/ditto-app-dev/"
                        "ditto-public-runtime/screener:sha-test"
                    ),
                    status="succeeded",
                    provider="targon",
                    image_digest="sha256:" + "b" * 64,
                    completed_at=datetime.now(UTC),
                    created_by="test",
                    reason="provide a pinned reviewed source worker image",
                )
            )
        controller_headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        claimed = await client.post(
            "/api/v1/screener/controller/submission-image-builds/claim",
            headers=controller_headers,
            json={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert claimed.status_code == 200, claimed.text
        job = claimed.json()["build"]
        build_id = job["build_id"]
        attempt_id = job["attempt_id"]
        async with session_maker() as session, session.begin():
            row = await session.get(SubmissionImageBuild, UUID(build_id))
            assert row is not None
            row.status = "succeeded"
            row.output_sha256 = "12" * 32
            row.output_size_bytes = 123
            row.output_image_id = "sha256:" + config_digest
            row.runtime_status = "running"
            row.controller_epoch = "builder:test"
            row.completed_at = datetime.now(UTC)
        smoked = await client.post(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}/runtime-result",
            headers=controller_headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "status": "succeeded",
                "provider_resource_id": "wrk-runtime",
                "image_reference": (
                    "us-central1-docker.pkg.dev/ditto-app-dev/"
                    "ditto-screening-candidates/miner@sha256:" + "cd" * 32
                ),
            },
        )
        assert smoked.status_code == 204, smoked.text
        leased = await client.post(
            "/api/v1/screener/controller/submission-source-reviews/claim",
            headers=controller_headers,
            json={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert leased.status_code == 200, leased.text
        review = leased.json()["review"]
        complete = await client.post(
            f"/api/v1/screener/submission-source-reviews/{review['review_id']}/complete",
            headers={"Authorization": f"Bearer {review['job_token']}"},
            json={
                "observation": {
                    "ok": True,
                    "risk_level": "low",
                    "categories": [],
                    "clearance_certified": True,
                }
            },
        )
        assert complete.status_code == 200, complete.text
        storage.copy_object.assert_awaited()
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.EVALUATING
            assert agent.screened_image_sha256 == "12" * 32
            assert agent.screened_image_id == "sha256:" + config_digest
            attempt = await session.get(ScreeningAttempt, UUID(attempt_id))
            assert attempt is not None
            assert attempt.status == "passed"
            dataset = (
                await session.scalars(
                    select(BenchmarkDataset).where(
                        BenchmarkDataset.agent_id == agent_id
                    )
                )
            ).one()
            assert dataset.sha256 == "ee" * 32
            assert generator.calls == 1

    async def test_consuming_succeeded_build_keeps_pending_runtime_archive(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]
        queued = await client.post(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
            headers=_AUTH_HEADER,
            json={"attempt_id": attempt_id},
        )
        assert queued.status_code == 200, queued.text
        build_id = queued.json()["build_id"]
        async with session_maker() as session, session.begin():
            row = await session.get(SubmissionImageBuild, UUID(build_id))
            assert row is not None
            row.status = "succeeded"
            row.output_sha256 = "12" * 32
            row.output_size_bytes = 123
            row.runtime_status = "pending"
            row.completed_at = datetime.now(UTC)

        consumed = await client.delete(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds/{build_id}",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id},
        )

        assert consumed.status_code == 204, consumed.text
        storage.delete_object.assert_not_awaited()
        async with session_maker() as session:
            row = await session.get(SubmissionImageBuild, UUID(build_id))
            assert row is not None
            assert row.status == "consumed"
            assert row.runtime_status == "pending"
            assert row.runtime_error_code is None
            assert row.runtime_completed_at is None

        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        runtime = await client.post(
            "/api/v1/screener/controller/submission-runtime-smokes/claim",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json={"environment": "prod", "controller_epoch": "prod:epoch"},
        )
        assert runtime.status_code == 200, runtime.text
        assert runtime.json()["artifact"]["build_id"] == build_id

    async def test_consuming_succeeded_build_keeps_in_flight_runtime_archive(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]
        queued = await client.post(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
            headers=_AUTH_HEADER,
            json={"attempt_id": attempt_id},
        )
        assert queued.status_code == 200, queued.text
        build_id = queued.json()["build_id"]
        async with session_maker() as session, session.begin():
            row = await session.get(SubmissionImageBuild, UUID(build_id))
            assert row is not None
            row.status = "succeeded"
            row.output_sha256 = "12" * 32
            row.output_size_bytes = 123
            row.runtime_status = "running"
            row.controller_epoch = "prod:epoch"
            row.completed_at = datetime.now(UTC)

        consumed = await client.delete(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds/{build_id}",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id},
        )

        assert consumed.status_code == 204, consumed.text
        storage.delete_object.assert_not_awaited()
        async with session_maker() as session:
            row = await session.get(SubmissionImageBuild, UUID(build_id))
            assert row is not None
            assert row.status == "consumed"
            assert row.runtime_status == "running"
            assert row.runtime_error_code is None

        finished = await client.post(
            f"/api/v1/screener/controller/submission-image-builds/{build_id}/runtime-result",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json={
                "environment": "prod",
                "controller_epoch": "prod:epoch",
                "status": "fallback_required",
                "error_code": "TARGON_RUNTIME_PROVIDER_ERROR",
            },
        )
        assert finished.status_code == 204, finished.text
        storage.delete_object.assert_awaited_with(
            key=f"remote-builds/{build_id}/image.tar"
        )

    async def test_submission_build_rejects_wrong_attempt_and_job_token(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]
        wrong = await client.post(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
            headers=_AUTH_HEADER,
            json={"attempt_id": str(uuid4())},
        )
        assert wrong.status_code == 409
        queued = await client.post(
            f"/api/v1/screener/agent/{agent_id}/submission-image-builds",
            headers=_AUTH_HEADER,
            json={"attempt_id": attempt_id},
        )
        build_id = queued.json()["build_id"]
        rejected = await client.get(
            f"/api/v1/screener/submission-image-builds/{build_id}/source",
            headers={"Authorization": "Bearer wrong-attempt-token"},
        )
        assert rejected.status_code == 401

    async def test_trusted_build_enqueue_is_concurrently_idempotent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        payload = {
            "component": "screener",
            "source_sha": "c" * 40,
            "reason": "prove concurrent release enqueue is idempotent",
        }
        responses = await asyncio.gather(
            client.post(
                "/api/v1/screener/controller/trusted-image-builds",
                headers=headers,
                json=payload,
            ),
            client.post(
                "/api/v1/screener/controller/trusted-image-builds",
                headers=headers,
                json=payload,
            ),
        )
        assert [response.status_code for response in responses] == [200, 200]
        assert responses[0].json()["build_id"] == responses[1].json()["build_id"]
        async with session_maker() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(TrustedImageBuild)
                .where(TrustedImageBuild.source_sha == "c" * 40)
            )
        assert count == 1

    async def test_gcp_first_builder_policy_authorizes_immediate_fallback(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        async with session_maker() as session, session.begin():
            session.add(
                ScreenerProviderSettingsRevision(
                    environment="prod",
                    parent_revision=0,
                    settings={
                        "runtime_provider_priority": ["targon", "gcp"],
                        "source_review_provider_priority": ["targon", "gcp"],
                        "build_provider_priority": ["gcp"],
                    },
                    reason="Disable Targon builders during provider maintenance",
                    actor="operator@example.com",
                )
            )
        headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        settings = await client.get(
            "/api/v1/screener/controller/provider-settings?environment=prod",
            headers=headers,
        )
        assert settings.status_code == 200, settings.text
        assert settings.json()["settings"]["build_provider_priority"] == ["gcp"]

        queued = await client.post(
            "/api/v1/screener/controller/trusted-image-builds",
            headers=headers,
            json={
                "component": "screener",
                "source_sha": "e" * 40,
                "reason": "prove operator-disabled Targon fallback is immediate",
            },
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "fallback_required"
        assert queued.json()["error_code"] == "TARGON_BUILD_DISABLED_BY_POLICY"

    async def test_third_expired_build_lease_requests_explicit_fallback(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        build_id = uuid4()
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                TrustedImageBuild(
                    build_id=build_id,
                    environment="prod",
                    component="screener",
                    source_repository=(
                        "https://github.com/ditto-assistant/ditto-subnet.git"
                    ),
                    source_sha="d" * 40,
                    context_path=".",
                    dockerfile_path="workers/screener/Dockerfile",
                    destination="registry.invalid/screener:sha-test",
                    status="leased",
                    provider="targon",
                    attempt_count=3,
                    controller_epoch="builder:stale",
                    lease_expires_at=now - timedelta(seconds=1),
                    created_by="release-test",
                    reason="exhaust the bounded provider lease budget",
                )
            )
        headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        claim = await client.post(
            "/api/v1/screener/controller/trusted-image-builds/claim",
            headers=headers,
            json={"environment": "prod", "controller_epoch": "builder:next"},
        )
        assert claim.status_code == 200, claim.text
        assert claim.json()["build"] is None
        detail = await client.get(
            f"/api/v1/screener/controller/trusted-image-builds/{build_id}",
            headers=headers,
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "fallback_required"
        assert detail.json()["error_code"] == "TARGON_BUILD_LEASE_EXHAUSTED"

    async def test_trusted_build_claim_is_leased_and_digest_bound(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        build_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                TrustedImageBuild(
                    build_id=build_id,
                    environment="prod",
                    component="screener",
                    source_repository=(
                        "https://github.com/ditto-assistant/ditto-subnet.git"
                    ),
                    source_sha="a" * 40,
                    context_path=".",
                    dockerfile_path="workers/screener/Dockerfile",
                    destination=(
                        "us-central1-docker.pkg.dev/ditto-app-dev/"
                        "ditto-public-runtime/screener:sha-test"
                    ),
                    status="queued",
                    created_by="release-test",
                    reason="verify the dedicated trusted builder contract",
                )
            )
        headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        claim = await client.post(
            "/api/v1/screener/controller/trusted-image-builds/claim",
            headers=headers,
            json={"environment": "prod", "controller_epoch": "builder:test"},
        )
        assert claim.status_code == 200, claim.text
        assert claim.json()["build"]["status"] == "leased"

        invalid = await client.put(
            f"/api/v1/screener/controller/trusted-image-builds/{build_id}",
            headers=headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "status": "succeeded",
                "provider": "targon",
                "provider_resource_id": "rental-test",
            },
        )
        assert invalid.status_code == 422

        completed = await client.put(
            f"/api/v1/screener/controller/trusted-image-builds/{build_id}",
            headers=headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "status": "succeeded",
                "provider": "targon",
                "provider_resource_id": "rental-test",
                "image_digest": "sha256:" + "b" * 64,
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["image_digest"] == "sha256:" + "b" * 64

        latest = await client.get(
            "/api/v1/screener/controller/trusted-image-builds/latest?environment=prod",
            headers=headers,
        )
        assert latest.status_code == 200, latest.text
        assert latest.json()["build_id"] == str(build_id)
        assert latest.json()["source_sha"] == "a" * 40
        assert latest.json()["image_digest"] == "sha256:" + "b" * 64

        overwritten = await client.put(
            f"/api/v1/screener/controller/trusted-image-builds/{build_id}",
            headers=headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:test",
                "status": "failed",
                "provider": "targon",
                "provider_resource_id": "rental-test",
                "error_code": "LATE_PROVIDER_ERROR",
            },
        )
        assert overwritten.status_code == 409

    async def test_trusted_build_cleanup_required_records_event_without_clobber(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        build_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                TrustedImageBuild(
                    build_id=build_id,
                    environment="prod",
                    component="screener",
                    source_repository=(
                        "https://github.com/ditto-assistant/ditto-subnet.git"
                    ),
                    source_sha="f" * 40,
                    context_path=".",
                    dockerfile_path="workers/screener/Dockerfile",
                    destination=(
                        "us-central1-docker.pkg.dev/ditto-app-dev/"
                        "ditto-public-runtime/screener:sha-cleanup"
                    ),
                    status="succeeded",
                    provider="targon",
                    provider_resource_id="wrk-trusted-cleanup",
                    image_digest="sha256:" + "c" * 64,
                    controller_epoch="builder:cleanup",
                    created_by="release-test",
                    reason="prove trusted Kaniko cleanup is durable",
                    completed_at=datetime.now(UTC),
                )
            )
        headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        cleanup = await client.post(
            f"/api/v1/screener/controller/trusted-image-builds/{build_id}"
            "/cleanup-required",
            headers=headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:cleanup",
                "provider_resource_id": "wrk-trusted-cleanup",
            },
        )
        assert cleanup.status_code == 204, cleanup.text
        stale = await client.post(
            f"/api/v1/screener/controller/trusted-image-builds/{build_id}"
            "/cleanup-required",
            headers=headers,
            json={
                "environment": "prod",
                "controller_epoch": "builder:other",
                "provider_resource_id": "wrk-trusted-cleanup",
            },
        )
        assert stale.status_code == 409
        detail = await client.get(
            f"/api/v1/screener/controller/trusted-image-builds/{build_id}",
            headers=headers,
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "succeeded"
        assert detail.json()["error_code"] is None
        assert detail.json()["image_digest"] == "sha256:" + "c" * 64
        async with session_maker() as session:
            events = list(
                await session.scalars(
                    select(ScreenerCapacityEvent).where(
                        ScreenerCapacityEvent.event_type == "provider_cleanup_required"
                    )
                )
            )
            assert len(events) == 1
            assert events[0].provider == "targon"
            assert "trusted Kaniko" in events[0].detail

    async def test_current_controller_can_release_lease_for_graceful_handoff(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        first = await client.put(
            "/api/v1/screener/controller/capacity",
            headers=headers,
            json=_capacity_payload("prod:first"),
        )
        assert first.status_code == 200, first.text

        released = await client.post(
            "/api/v1/screener/controller/release",
            headers=headers,
            json={"environment": "prod", "controller_epoch": "prod:first"},
        )
        assert released.status_code == 204, released.text

        second = await client.put(
            "/api/v1/screener/controller/capacity",
            headers=headers,
            json=_capacity_payload("prod:second"),
        )
        assert second.status_code == 200, second.text

        stale_release = await client.post(
            "/api/v1/screener/controller/release",
            headers=headers,
            json={"environment": "prod", "controller_epoch": "prod:first"},
        )
        assert stale_release.status_code == 409

    async def test_controller_lease_fences_other_epochs_and_bootstraps_node(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        controller_headers = {"Authorization": f"Bearer {_CONTROLLER_TOKEN}"}
        legacy_payload = _capacity_payload("prod:first")
        legacy_payload.pop("provider_settings_revision")
        first = await client.put(
            "/api/v1/screener/controller/capacity",
            headers=controller_headers,
            json=legacy_payload,
        )
        assert first.status_code == 200, first.text
        assert first.json()["controller_lease_expires_at"]
        assert first.json()["provider_settings_revision"] == 0

        fenced = await client.put(
            "/api/v1/screener/controller/capacity",
            headers=controller_headers,
            json=_capacity_payload("prod:second"),
        )
        assert fenced.status_code == 409
        still_owned = await client.post(
            "/api/v1/screener/controller/fence",
            headers=controller_headers,
            json={"environment": "prod", "controller_epoch": "prod:first"},
        )
        assert still_owned.status_code == 204

        node_id = "ditto-screener-prod-test"
        resource_id = "targon-workload-test"
        grant = await client.post(
            "/api/v1/screener/controller/bootstrap-grants",
            headers=controller_headers,
            json={
                "environment": "prod",
                "node_id": node_id,
                "provider": "targon",
                "provider_resource_id": resource_id,
                "controller_epoch": "prod:first",
                "image_reference": (
                    "us-central1-docker.pkg.dev/ditto-app-dev/"
                    "ditto-public-runtime/screener@sha256:" + "b" * 64
                ),
            },
        )
        assert grant.status_code == 200, grant.text

        node_keypair = bittensor.Keypair.create_from_uri("//Bob")
        registration_id = uuid4()
        timestamp = int(datetime.now(UTC).timestamp())
        message = (
            "ditto-screener-node-register:v1:"
            f"prod:{node_id}:targon:{resource_id}:"
            f"{node_keypair.ss58_address}:{timestamp}:{registration_id}"
        )
        registration = await client.post(
            "/api/v1/screener/nodes/register",
            headers={
                "Authorization": f"Bootstrap {grant.json()['registration_token']}"
            },
            json={
                "environment": "prod",
                "node_id": node_id,
                "provider": "targon",
                "provider_resource_id": resource_id,
                "screener_hotkey": node_keypair.ss58_address,
                "timestamp": timestamp,
                "signature": node_keypair.sign(message.encode()).hex(),
                "registration_id": str(registration_id),
            },
        )
        assert registration.status_code == 200, registration.text
        node_token = registration.json()["api_token"]
        registration_replay = await client.post(
            "/api/v1/screener/nodes/register",
            headers={
                "Authorization": f"Bootstrap {grant.json()['registration_token']}"
            },
            json={
                "environment": "prod",
                "node_id": node_id,
                "provider": "targon",
                "provider_resource_id": resource_id,
                "screener_hotkey": node_keypair.ss58_address,
                "timestamp": timestamp,
                "signature": node_keypair.sign(message.encode()).hex(),
                "registration_id": str(registration_id),
            },
        )
        assert registration_replay.status_code == 200
        assert registration_replay.json()["api_token"] == node_token
        readiness = await client.get(
            "/api/v1/screener/controller/nodes?environment=prod",
            headers=controller_headers,
        )
        assert readiness.status_code == 200
        assert readiness.json()["nodes"][0]["ready"] is False
        assert readiness.json()["nodes"][0]["active_lease"] is False
        assert readiness.json()["nodes"][0]["image_reference"].endswith(
            "@sha256:" + "b" * 64
        )

        heartbeat_timestamp = int(datetime.now(UTC).timestamp())
        heartbeat_message = (
            "ditto-screener-heartbeat:v3:"
            f"{node_keypair.ss58_address}:0.4.2:3:{SCREENING_POLICY_VERSION}:"
            f"polling::{node_id}:-:-:{heartbeat_timestamp}"
        ).encode()
        node_headers = {
            "Authorization": f"Bearer {node_token}",
            "X-Screener-Hotkey": node_keypair.ss58_address,
        }
        heartbeat_response = await client.post(
            "/api/v1/screener/heartbeat",
            headers=node_headers,
            json={
                "screener_hotkey": node_keypair.ss58_address,
                "software_version": "0.4.2",
                "protocol_version": 3,
                "policy_version": SCREENING_POLICY_VERSION,
                "state": "polling",
                "instance_id": node_id,
                "timestamp": heartbeat_timestamp,
                "signature": node_keypair.sign(heartbeat_message).hex(),
            },
        )
        assert heartbeat_response.status_code == 200, heartbeat_response.text
        readiness = await client.get(
            "/api/v1/screener/controller/nodes?environment=prod",
            headers=controller_headers,
        )
        assert readiness.json()["nodes"][0]["ready"] is True

        node_queue = await client.get(
            "/api/v1/screener/queue",
            headers=node_headers,
        )
        assert node_queue.status_code == 200, node_queue.text

        refresh_id = uuid4()
        refresh_timestamp = int(datetime.now(UTC).timestamp())
        refresh_message = (
            "ditto-screener-node-refresh:v1:"
            f"{node_id}:{node_keypair.ss58_address}:{refresh_timestamp}:{refresh_id}"
        ).encode()
        refresh_payload = {
            "node_id": node_id,
            "screener_hotkey": node_keypair.ss58_address,
            "timestamp": refresh_timestamp,
            "signature": node_keypair.sign(refresh_message).hex(),
            "refresh_id": str(refresh_id),
        }
        refresh = await client.post(
            "/api/v1/screener/nodes/refresh",
            headers=node_headers,
            json=refresh_payload,
        )
        assert refresh.status_code == 200, refresh.text
        rotated_token = refresh.json()["api_token"]
        assert rotated_token != node_token

        # A lost response can be retried with the immediately prior bearer and
        # the same signed request identity; it returns the same new authority.
        refresh_replay = await client.post(
            "/api/v1/screener/nodes/refresh",
            headers=node_headers,
            json=refresh_payload,
        )
        assert refresh_replay.status_code == 200, refresh_replay.text
        assert refresh_replay.json()["api_token"] == rotated_token

        different_refresh_id = uuid4()
        different_message = (
            "ditto-screener-node-refresh:v1:"
            f"{node_id}:{node_keypair.ss58_address}:{refresh_timestamp}:"
            f"{different_refresh_id}"
        ).encode()
        stale_authority = await client.post(
            "/api/v1/screener/nodes/refresh",
            headers=node_headers,
            json={
                **refresh_payload,
                "refresh_id": str(different_refresh_id),
                "signature": node_keypair.sign(different_message).hex(),
            },
        )
        assert stale_authority.status_code == 401

        replay = await client.post(
            "/api/v1/screener/nodes/register",
            headers={
                "Authorization": f"Bootstrap {grant.json()['registration_token']}"
            },
            json={
                "environment": "prod",
                "node_id": node_id,
                "provider": "targon",
                "provider_resource_id": resource_id,
                "screener_hotkey": node_keypair.ss58_address,
                "timestamp": timestamp,
                "signature": node_keypair.sign(message.encode()).hex(),
                "registration_id": str(registration_id),
            },
        )
        # Enrollment recovery closes once the node has successfully rotated;
        # the consumed bootstrap token cannot recover an older authority.
        assert replay.status_code == 401

    async def test_watchdog_is_quiet_while_controller_lease_is_fresh(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        app.state.config = replace(
            app.state.config,
            screener_auth=replace(
                app.state.config.screener_auth,
                controller_api_token=_CONTROLLER_TOKEN,
            ),
        )
        missing = await client.get(
            "/api/v1/public/screener-capacity-watchdog?environment=prod"
        )
        assert missing.status_code == 200
        assert missing.json()["activate_fallback"] is True

        capacity = await client.put(
            "/api/v1/screener/controller/capacity",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json=_capacity_payload("prod:first"),
        )
        assert capacity.status_code == 200, capacity.text
        fresh = await client.get(
            "/api/v1/public/screener-capacity-watchdog?environment=prod"
        )
        assert fresh.status_code == 200
        assert fresh.json() == {
            "generated_at": fresh.json()["generated_at"],
            "controller_stale": False,
            "activate_fallback": False,
            "reason": "controller_fresh",
            "controller_epoch": "prod:first",
            "controller_source_sha": "a" * 40,
            "provider_ready": True,
        }

        provider_failure = await client.put(
            "/api/v1/screener/controller/capacity",
            headers={"Authorization": f"Bearer {_CONTROLLER_TOKEN}"},
            json={
                **_capacity_payload("prod:first"),
                "provider_ready": False,
                "last_provider_error_code": "TARGON_SCALE_UP_FAILED",
                "last_provider_error_at": datetime.now(UTC).isoformat(),
            },
        )
        assert provider_failure.status_code == 200, provider_failure.text
        degraded = await client.get(
            "/api/v1/public/screener-capacity-watchdog?environment=prod"
        )
        assert degraded.status_code == 200
        assert degraded.json() == {
            "generated_at": degraded.json()["generated_at"],
            "controller_stale": False,
            "activate_fallback": True,
            "reason": "provider_not_ready",
            "controller_epoch": "prod:first",
            "controller_source_sha": "a" * 40,
            "provider_ready": False,
        }


# --- Queue -----------------------------------------------------------------


class TestShadowReview:
    async def test_attempt_owned_observation_is_idempotent_and_non_authoritative(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING, name="shadow-agent"
        )
        attempt_id = uuid4()
        settings = ScreenerReviewSettings(mode="shadow")
        checksum = _review_settings_checksum(settings)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            revision = ScreenerReviewSettingsRevision(
                parent_revision=0,
                scope="ditto-screener-prod",
                settings=settings.model_dump(mode="json"),
                checksum=checksum,
                reason="bounded shadow canary",
                actor="test",
            )
            session.add(revision)
            await session.flush()
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="running",
                    started_at=now,
                    deadline=now + timedelta(minutes=30),
                )
            )
            revision_id = revision.revision
        payload = {
            "attempt_id": str(attempt_id),
            "artifact_sha256": _SHA256,
            "settings_revision": revision_id,
            "settings_scope": "ditto-screener-prod",
            "settings_checksum": checksum,
            "disposition": "safe",
            "risk_level": "low",
            "categories": ["none"],
            "finding_digest": "cd" * 32,
            "resolution_basis": "authoritative_model_tool_path",
            "clearance_path": "l3_adjudicated_safe",
            "critic_disposition": "confirm_safe",
            "adjudicator_disposition": "confirm_safe",
            "response_models": ["moonshotai/kimi-k3", "openai/gpt-5.6-sol"],
            "response_providers": ["openrouter", "openrouter"],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "cached_input_tokens": 80,
                "reasoning_tokens": 5,
                "estimated_cost_usd": 0.1,
                "reported_cost_usd": 0.09,
            },
        }
        url = f"/api/v1/screener/agent/{agent_id}/shadow-review"

        first = await client.post(url, json=payload)
        second = await client.post(url, json=payload)

        assert first.status_code == second.status_code == 200
        async with session_maker() as session:
            observation = await session.get(ScreenerShadowReview, attempt_id)
            agent = await session.get(Agent, agent_id)
            assert observation is not None
            assert observation.settings_revision == revision_id
            assert observation.disposition == "safe"
            assert agent is not None and agent.status == AgentStatus.SCREENING

        conflicting = {**payload, "disposition": "violation", "risk_level": "high"}
        response = await client.post(url, json=conflicting)
        assert response.status_code == 409

        async with session_maker() as session, session.begin():
            attempt = await session.get(ScreeningAttempt, attempt_id)
            assert attempt is not None
            attempt.started_at = datetime.now(UTC) - timedelta(minutes=2)
            attempt.deadline = datetime.now(UTC) - timedelta(minutes=1)
        response = await client.post(url, json=payload)
        assert response.status_code == 409


class TestHeartbeat:
    async def test_v2_progress_is_public_and_clears_on_idle_and_terminal(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=2)
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING, name="steady-agent"
        )
        attempt_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="running",
                    started_at=started,
                    deadline=started + timedelta(minutes=30),
                )
            )

        timestamp = int(datetime.now(UTC).timestamp())
        progress = {"stage": "building", "started_at": int(started.timestamp())}
        response = await client.post(
            "/api/v1/screener/heartbeat",
            json=_heartbeat_payload(
                timestamp=timestamp,
                state="screening",
                active_agent_id=agent_id,
                protocol_version=2,
                progress=progress,
            ),
        )
        assert response.status_code == 200, response.text
        entry = (await client.get("/api/v1/public/screeners")).json()["screeners"][0]
        assert entry["active_agent_id"] == str(agent_id)
        assert entry["active_agent_name"] == "steady-agent"
        assert entry["screening_progress"]["stage"] == "building"
        assert entry["screening_progress"]["started_at"].startswith(
            started.isoformat().replace("+00:00", "")
        )

        review = await client.post(
            "/api/v1/screener/heartbeat",
            json=_heartbeat_payload(
                timestamp=timestamp + 1,
                state="screening",
                active_agent_id=agent_id,
                protocol_version=2,
                progress={
                    "stage": "source_review_30",
                    "started_at": int(started.timestamp()),
                },
            ),
        )
        assert review.status_code == 200, review.text
        review_entry = (await client.get("/api/v1/public/screeners")).json()[
            "screeners"
        ][0]
        assert review_entry["screening_progress"]["stage"] == "source_review_30"

        idle = await client.post(
            "/api/v1/screener/heartbeat",
            json=_heartbeat_payload(timestamp=timestamp + 2, protocol_version=2),
        )
        assert idle.status_code == 200
        idle_entry = (await client.get("/api/v1/public/screeners")).json()["screeners"][
            0
        ]
        assert idle_entry["active_agent_id"] is None
        assert idle_entry["active_agent_name"] is None
        assert idle_entry["screening_progress"] is None

        legacy = await client.post(
            "/api/v1/screener/heartbeat",
            json=_heartbeat_payload(
                timestamp=timestamp + 3,
                state="screening",
                active_agent_id=agent_id,
                protocol_version=1,
            ),
        )
        assert legacy.status_code == 200
        legacy_entry = (await client.get("/api/v1/public/screeners")).json()[
            "screeners"
        ][0]
        assert legacy_entry["active_agent_name"] == "steady-agent"
        assert legacy_entry["screening_progress"] is None

        active = await client.post(
            "/api/v1/screener/heartbeat",
            json=_heartbeat_payload(
                timestamp=timestamp + 4,
                state="screening",
                active_agent_id=agent_id,
                protocol_version=2,
                progress=progress,
            ),
        )
        assert active.status_code == 200
        async with session_maker() as session, session.begin():
            attempt = await session.get(ScreeningAttempt, attempt_id)
            agent = await session.get(Agent, agent_id)
            assert attempt is not None and agent is not None
            attempt.status = "passed"
            attempt.finished_at = datetime.now(UTC)
            agent.status = AgentStatus.EVALUATING
        terminal_entry = (await client.get("/api/v1/public/screeners")).json()[
            "screeners"
        ][0]
        assert terminal_entry["active_agent_id"] is None
        assert terminal_entry["screening_progress"] is None

    async def test_stale_progress_is_offline_and_not_projected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        started = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=2)
        agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING)
        async with session_maker() as session, session.begin():
            session.add(
                ScreeningAttempt(
                    attempt_id=uuid4(),
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="running",
                    started_at=started,
                    deadline=started + timedelta(minutes=30),
                )
            )
        timestamp = int(datetime.now(UTC).timestamp())
        response = await client.post(
            "/api/v1/screener/heartbeat",
            json=_heartbeat_payload(
                timestamp=timestamp,
                state="screening",
                active_agent_id=agent_id,
                protocol_version=2,
                progress={
                    "stage": "health_check",
                    "started_at": int(started.timestamp()),
                },
            ),
        )
        assert response.status_code == 200
        async with session_maker() as session, session.begin():
            heartbeat = await session.get(
                ScreenerHeartbeat, (_SCREENER_HOTKEY, "legacy")
            )
            assert heartbeat is not None
            heartbeat.seen_at = datetime.now(UTC) - timedelta(minutes=10)
        entry = (await client.get("/api/v1/public/screeners")).json()["screeners"][0]
        assert entry["online"] is False
        assert entry["active_agent_id"] is None
        assert entry["screening_progress"] is None

    async def test_records_signed_metrics_and_is_publicly_visible(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        timestamp = int(datetime.now(UTC).timestamp())
        metrics = {
            "collected_at": timestamp,
            "cpu_percent": 20,
            "memory_percent": 35,
            "disk_percent": 50,
            "docker": {
                "status": "healthy",
                "running_containers": 3,
                "unhealthy_containers": 0,
            },
        }
        payload = _heartbeat_payload(timestamp=timestamp, system_metrics=metrics)
        response = await client.post("/api/v1/screener/heartbeat", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True

        async with session_maker() as session:
            stored = await session.get(ScreenerHeartbeat, (_SCREENER_HOTKEY, "legacy"))
            assert stored is not None
            assert stored.first_seen_at is not None
            assert stored.system_metrics is not None
            assert stored.system_metrics["docker"]["running_containers"] == 3

        public = (await client.get("/api/v1/public/screeners")).json()
        assert public["reported_count"] == 1
        entry = public["screeners"][0]
        assert entry["screener_hotkey"] == _SCREENER_HOTKEY
        assert entry["availability"] == "available"
        assert entry["health"] == "healthy"
        assert entry["system_metrics"]["docker_status"] == "healthy"
        assert "signature" not in entry

        replay = await client.post("/api/v1/screener/heartbeat", json=payload)
        assert replay.status_code == 200
        assert replay.json()["accepted"] is False

    async def test_v3_lists_each_fleet_instance_separately(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The shared-hotkey fleet no longer collapses into one /screeners row."""
        _install_db(app, session_maker)
        ts = int(datetime.now(UTC).timestamp())
        for name in ("ditto-screener-prod", "ditto-screener-fleet-abcd"):
            resp = await client.post(
                "/api/v1/screener/heartbeat",
                json=_heartbeat_payload(
                    timestamp=ts, protocol_version=3, instance_id=name
                ),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["accepted"] is True

        public = (await client.get("/api/v1/public/screeners")).json()
        assert public["reported_count"] == 2
        by_instance = {e["instance_id"]: e for e in public["screeners"]}
        assert set(by_instance) == {
            "ditto-screener-prod",
            "ditto-screener-fleet-abcd",
        }
        assert all(
            e["screener_hotkey"] == _SCREENER_HOTKEY for e in public["screeners"]
        )

    async def test_v3_requires_instance_id(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        payload = _heartbeat_payload(protocol_version=3, instance_id=None)
        payload.pop("instance_id", None)
        resp = await client.post("/api/v1/screener/heartbeat", json=payload)
        assert resp.status_code == 422

    async def test_v4_persists_signed_applied_review_settings(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        review = {
            "revision": 42,
            "scope": "ditto-screener-prod",
            "mode": "shadow",
            "checksum": "cd" * 32,
            "source": "platform",
        }
        response = await client.post(
            "/api/v1/screener/heartbeat",
            json=_heartbeat_payload(
                protocol_version=4,
                instance_id="ditto-screener-prod",
                review_settings=review,
            ),
        )
        assert response.status_code == 200, response.text
        async with session_maker() as session:
            heartbeat = await session.get(
                ScreenerHeartbeat, (_SCREENER_HOTKEY, "ditto-screener-prod")
            )
            assert heartbeat is not None
            assert heartbeat.system_metrics is not None
            assert heartbeat.system_metrics["review_settings"] == review

    async def test_rejects_tampering_arbitrary_metrics_and_wrong_auth(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        # A real agent row: the accepted heartbeat below persists
        # ``active_agent_id``, which is a foreign key onto ``agents``.
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING, name="tamper-agent"
        )
        timestamp = int(datetime.now(UTC).timestamp())
        metrics = {
            "collected_at": timestamp,
            "cpu_percent": 20,
            "memory_percent": 35,
            "disk_percent": 50,
            "docker": {
                "status": "healthy",
                "running_containers": 3,
                "unhealthy_containers": 0,
            },
        }
        tampered = _heartbeat_payload(timestamp=timestamp, system_metrics=metrics)
        tampered["system_metrics"]["disk_percent"] = 90  # type: ignore[index]
        response = await client.post("/api/v1/screener/heartbeat", json=tampered)
        assert response.status_code == 401

        additive = _heartbeat_payload(timestamp=timestamp, system_metrics=metrics)
        additive["system_metrics"]["container_names"] = ["secret"]  # type: ignore[index]
        response = await client.post("/api/v1/screener/heartbeat", json=additive)
        assert response.status_code == 200, response.text
        async with session_maker() as session:
            heartbeat = await session.scalar(
                select(ScreenerHeartbeat).where(
                    ScreenerHeartbeat.screener_hotkey == _SCREENER_HOTKEY
                )
            )
            assert heartbeat is not None
            assert heartbeat.system_metrics is not None
            assert "container_names" not in heartbeat.system_metrics

        response = await client.post(
            "/api/v1/screener/heartbeat",
            headers={**_AUTH_HEADER, "Authorization": "Bearer wrong-token"},
            json=_heartbeat_payload(),
        )
        assert response.status_code == 401

        # Strictly newer than the accepted heartbeat above, not a fresh clock
        # read: a same-second read makes the upsert a no-op (so the private-field
        # assertion below passes vacuously), and a read that happens to tick over
        # makes it a real write. Same convention as the other tests here.
        now = timestamp + 1
        progress = {"stage": "building", "started_at": now - 30}
        tampered_progress = _heartbeat_payload(
            timestamp=now,
            state="screening",
            active_agent_id=agent_id,
            protocol_version=2,
            progress=progress,
        )
        tampered_progress["progress"]["stage"] = "submitting"  # type: ignore[index]
        response = await client.post(
            "/api/v1/screener/heartbeat", json=tampered_progress
        )
        assert response.status_code == 401

        private_field = _heartbeat_payload(
            timestamp=now,
            state="screening",
            active_agent_id=agent_id,
            protocol_version=2,
            progress={"stage": "building", "started_at": now - 30},
        )
        private_field["progress"]["dependency"] = "private-package"  # type: ignore[index]
        response = await client.post("/api/v1/screener/heartbeat", json=private_field)
        assert response.status_code == 200, response.text
        async with session_maker() as session:
            heartbeat = await session.scalar(
                select(ScreenerHeartbeat).where(
                    ScreenerHeartbeat.screener_hotkey == _SCREENER_HOTKEY
                )
            )
            assert heartbeat is not None
            assert "private-package" not in json.dumps(heartbeat.system_metrics)

        invalid_stage = _heartbeat_payload(
            timestamp=now + 1,
            state="screening",
            active_agent_id=agent_id,
            protocol_version=2,
            progress={"stage": "docker_layer", "started_at": now - 30},
        )
        response = await client.post("/api/v1/screener/heartbeat", json=invalid_stage)
        assert response.status_code == 422

    async def test_heartbeat_payload_size_is_bounded(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        response = await client.post(
            "/api/v1/screener/heartbeat",
            headers={"Content-Length": "4097"},
            json=_heartbeat_payload(),
        )
        assert response.status_code == 413

        payload = json.dumps(_heartbeat_payload())
        response = await client.post(
            "/api/v1/screener/heartbeat",
            headers={"Content-Type": "application/json"},
            content=(" " * 4097) + payload,
        )
        assert response.status_code == 413


class TestQueue:
    async def test_excludes_historical_agent_without_current_era_admission(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="historical-unadmitted",
            created_at=now - timedelta(hours=2),
        )
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_TARGET_VERSION - 1,
                    desired_version=_TARGET_VERSION,
                    status="activated",
                    cohort_size=5,
                    created_at=now - timedelta(hours=1),
                    activated_at=now,
                )
            )
        _install_db(app, session_maker)

        response = await client.get("/api/v1/screener/queue")

        assert response.status_code == 200, response.text
        assert agent_id not in {
            UUID(item["agent_id"]) for item in response.json()["items"]
        }

    async def test_lists_only_uploaded_oldest_first(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        base = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            name="younger",
            created_at=base + timedelta(minutes=5),
        )
        await _seed_agent(
            session_maker, status=AgentStatus.UPLOADED, name="older", created_at=base
        )
        # Already promoted -> excluded from the screener queue.
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING, name="promoted")
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.get("/api/v1/screener/queue", headers=_AUTH_HEADER)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        body = response.json()
        assert body["count"] == 2
        assert [i["name"] for i in body["items"]] == ["older", "younger"]
        assert all(i["status"] == AgentStatus.UPLOADED for i in body["items"])
        assert body["required_policy_version"] == SCREENING_POLICY_VERSION

    async def test_prioritizes_zero_score_submission_before_older_scored_one(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        base = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        scored = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="older-scored",
            created_at=base,
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        await _seed_score(session_maker, agent_id=scored)
        await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            name="younger-unscored",
            created_at=base + timedelta(minutes=5),
        )
        _install_db(app, session_maker)

        response = await client.get("/api/v1/screener/queue")

        assert response.status_code == 200
        assert [item["name"] for item in response.json()["items"]] == [
            "younger-unscored",
            "older-scored",
        ]

    async def test_prioritizes_highest_two_score_contender_before_backlog(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        base = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        lower = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="older-lower-contender",
            created_at=base,
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        higher = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="newer-higher-contender",
            created_at=base + timedelta(minutes=5),
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            name="unscored-backlog",
            created_at=base - timedelta(minutes=5),
        )
        for agent_id, prefix, composite in (
            (lower, "5Lower", 0.60),
            (higher, "5Higher", 0.80),
        ):
            await _seed_score(
                session_maker,
                agent_id=agent_id,
                validator_hotkey=f"{prefix}OneXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
                composite=composite,
            )
            await _seed_score(
                session_maker,
                agent_id=agent_id,
                validator_hotkey=f"{prefix}TwoXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
                composite=composite,
            )
        _install_db(app, session_maker)

        response = await client.get("/api/v1/screener/queue")

        assert response.status_code == 200
        assert [item["name"] for item in response.json()["items"]] == [
            "newer-higher-contender",
            "older-lower-contender",
            "unscored-backlog",
        ]

    async def test_requeues_legacy_evaluating_submission(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            screening_policy_version=0,
        )
        _install_db(app, session_maker)
        response = await client.get("/api/v1/screener/queue")
        assert response.status_code == 200
        assert response.json()["count"] == 1

    async def test_requeues_retryable_failures_regardless_of_policy(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        stale_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCREENING_FAILED,
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        current_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCREENING_FAILED,
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        _install_db(app, session_maker)

        response = await client.get("/api/v1/screener/queue")

        assert response.status_code == 200
        assert {item["agent_id"] for item in response.json()["items"]} == {
            str(stale_id),
            str(current_id),
        }

    async def test_limit_caps_results(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        for i in range(3):
            await _seed_agent(session_maker, status=AgentStatus.UPLOADED, name=f"a{i}")
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.get(
            "/api/v1/screener/queue?limit=2", headers=_AUTH_HEADER
        )
        assert response.status_code == 200
        assert response.json()["count"] == 2

    async def test_missing_auth_header_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        client.headers.clear()
        response = await client.get("/api/v1/screener/queue")
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_SCREENER_AUTH

    async def test_invalid_bearer_token_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        response = await client.get(
            "/api/v1/screener/queue",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_SCREENER_AUTH

    async def test_unapproved_hotkey_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        response = await client.get(
            "/api/v1/screener/queue",
            headers={
                "X-Screener-Hotkey": (
                    "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
                )
            },
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_SCREENER_AUTH

    async def test_dedicated_screener_needs_no_validator_permit(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app, permitted=False, registered=False)
        response = await client.get("/api/v1/screener/queue")
        assert response.status_code == 200

    async def test_limit_out_of_range_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.get(
            "/api/v1/screener/queue?limit=0", headers=_AUTH_HEADER
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == ERROR_CODE_VALIDATION


# --- Leased claims ---------------------------------------------------------


class TestClaim:
    async def test_mechanical_admission_claim_uses_its_dedicated_contract_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.screener.resolve_queue_policy_settings",
            AsyncMock(
                return_value=SimpleNamespace(
                    deferred_source_review=SimpleNamespace(mode="enforce")
                )
            ),
        )

        response = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)

        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        assert item["agent_id"] == str(agent_id)
        assert item["build_only"] is True
        assert item["deferred_source_review"] is True
        assert item["precheck_reason_code"] is None
        assert item["duplicate_of"] is None

    async def test_claim_prioritizes_zero_score_submission(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        base = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        scored = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="older-scored",
            created_at=base,
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        await _seed_score(session_maker, agent_id=scored)
        unscored = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            name="younger-unscored",
            created_at=base + timedelta(minutes=5),
        )
        _install_db(app, session_maker)

        response = await client.post(_CLAIM_URL)

        assert response.status_code == 200
        assert response.json()["items"][0]["agent_id"] == str(unscored)

    async def test_claim_prioritizes_highest_two_score_contender(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        base = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)
        lower = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="lower-contender",
            created_at=base,
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        higher = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="higher-contender",
            created_at=base + timedelta(minutes=5),
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        for agent_id, prefix, composite in (
            (lower, "5Lower", 0.60),
            (higher, "5Higher", 0.80),
        ):
            await _seed_score(
                session_maker,
                agent_id=agent_id,
                validator_hotkey=f"{prefix}OneXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
                composite=composite,
            )
            await _seed_score(
                session_maker,
                agent_id=agent_id,
                validator_hotkey=f"{prefix}TwoXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
                composite=composite,
            )
        _install_db(app, session_maker)

        response = await client.post(_CLAIM_URL)

        assert response.status_code == 200
        assert response.json()["items"][0]["agent_id"] == str(higher)

    async def test_claim_is_exclusive_and_lease_bound_verdict_is_idempotent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)

        claimed = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        assert claimed.status_code == 200
        item = claimed.json()["items"][0]
        assert item["agent_id"] == str(agent_id)
        assert item["status"] == AgentStatus.SCREENING
        assert item["attempt_id"]
        assert item["lease_deadline"]

        duplicate = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        assert duplicate.status_code == 200
        assert duplicate.json()["count"] == 0

        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=UUID(item["attempt_id"])
        )
        payload = _result_payload(
            agent_id,
            passed=True,
            attempt_id=UUID(item["attempt_id"]),
        )
        first = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            headers=_AUTH_HEADER,
            json=payload,
        )
        replay = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            headers=_AUTH_HEADER,
            json=payload,
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["status"] == AgentStatus.EVALUATING

    async def test_exact_duplicate_waits_for_usable_owner_then_rejects_before_screen(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Three hotkeys share one hash: a failed build claims nothing durable.

        Concurrent claims cannot admit both later uploads. The first later upload
        must pass the build gate before the other receives an exact-duplicate
        precheck. Replaying that signed rejection remains idempotent.
        """
        now = datetime.now(UTC)
        first = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            miner_hotkey="5FirstMinerHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            created_at=now - timedelta(minutes=3),
        )
        _install_db(app, session_maker)
        _install_chain(app)

        first_claim = (await client.post(_CLAIM_URL)).json()["items"][0]
        first_failure = await client.post(
            f"/api/v1/screener/agent/{first}/result",
            json=_result_payload(
                first,
                passed=False,
                attempt_id=UUID(first_claim["attempt_id"]),
                outcome="deterministic_reject",
                detail="build failed: synthetic compiler error",
                reason_code="docker-build",
            ),
        )
        assert first_failure.status_code == 200

        second = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            miner_hotkey="5SecondMinerHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            created_at=now - timedelta(minutes=2),
        )
        third = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            miner_hotkey="5ThirdMinerHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            created_at=now - timedelta(minutes=1),
        )

        simultaneous = await asyncio.gather(
            client.post(_CLAIM_URL), client.post(_CLAIM_URL)
        )
        admitted = [
            item for response in simultaneous for item in response.json()["items"]
        ]
        assert [item["agent_id"] for item in admitted] == [str(second)]
        assert admitted[0]["precheck_reason_code"] is None

        await _seed_verified_image_upload(
            session_maker,
            agent_id=second,
            attempt_id=UUID(admitted[0]["attempt_id"]),
        )
        second_pass = await client.post(
            f"/api/v1/screener/agent/{second}/result",
            json=_result_payload(
                second,
                attempt_id=UUID(admitted[0]["attempt_id"]),
                outcome="pass",
            ),
        )
        assert second_pass.status_code == 200

        duplicate_claim = (await client.post(_CLAIM_URL)).json()["items"][0]
        assert duplicate_claim["agent_id"] == str(third)
        assert duplicate_claim["precheck_reason_code"] == (
            "exact-cross-miner-duplicate"
        )
        assert duplicate_claim["duplicate_of"] == str(second)
        conflicting_pass = await client.post(
            f"/api/v1/screener/agent/{third}/result",
            json=_result_payload(
                third,
                attempt_id=UUID(duplicate_claim["attempt_id"]),
                outcome="pass",
            ),
        )
        assert conflicting_pass.status_code == 409
        duplicate_payload = _result_payload(
            third,
            passed=False,
            attempt_id=UUID(duplicate_claim["attempt_id"]),
            outcome="deterministic_reject",
            detail="exact cross-miner duplicate",
            reason_code="exact-cross-miner-duplicate",
        )
        rejected = await client.post(
            f"/api/v1/screener/agent/{third}/result", json=duplicate_payload
        )
        replay = await client.post(
            f"/api/v1/screener/agent/{third}/result", json=duplicate_payload
        )
        assert rejected.status_code == replay.status_code == 200
        assert replay.json()["status"] == AgentStatus.REJECTED

        async with session_maker() as session:
            failed = await session.get(Agent, first)
            owner = await session.get(Agent, second)
            duplicate = await session.get(Agent, third)
            attempt = await session.get(
                ScreeningAttempt, UUID(duplicate_claim["attempt_id"])
            )
            assert failed is not None and failed.status == AgentStatus.REJECTED
            assert owner is not None and owner.status == AgentStatus.EVALUATING
            assert duplicate is not None and duplicate.duplicate_of == second
            assert duplicate.screening_reason_code == "exact-cross-miner-duplicate"
            assert duplicate.screening_reason == (
                "Artifact is an exact duplicate of another miner submission"
            )
            assert attempt is not None
            assert attempt.reason_code == "exact-cross-miner-duplicate"
            assert attempt.duplicate_of == second

        status = await client.get(f"/api/v1/retrieval/agent/{third}/status")
        assert status.json()["screening_reason_code"] == ("exact-cross-miner-duplicate")

    async def test_same_miner_exact_hash_retry_is_not_prechecked_as_duplicate(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        retry = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)

        claimed = (await client.post(_CLAIM_URL)).json()["items"][0]

        assert claimed["agent_id"] == str(retry)
        assert claimed["precheck_reason_code"] is None
        assert claimed["duplicate_of"] is None

    async def test_same_coldkey_different_hotkey_is_not_prechecked_as_duplicate(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        coldkey = "5SharedColdkey"
        await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey="5OldHotkey",
            miner_coldkey=coldkey,
        )
        retry = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            miner_hotkey="5NewHotkey",
            miner_coldkey=coldkey,
        )
        _install_db(app, session_maker)

        claimed = (await client.post(_CLAIM_URL)).json()["items"][0]

        assert claimed["agent_id"] == str(retry)
        assert claimed["precheck_reason_code"] is None
        assert claimed["duplicate_of"] is None

    async def test_direct_owner_link_is_not_prechecked_as_exact_duplicate(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        old_hotkey = "5OldLinkedHotkey"
        new_hotkey = "5NewLinkedHotkey"
        await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=old_hotkey,
            miner_coldkey="5OldColdkey",
        )
        retry = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            miner_hotkey=new_hotkey,
            miner_coldkey="5NewColdkey",
        )
        lo, hi = sorted((old_hotkey, new_hotkey))
        async with session_maker() as session, session.begin():
            await record_attestation(
                session,
                netuid=118,
                hotkey_lo=lo,
                hotkey_hi=hi,
                nonce=uuid4(),
                issued_at=datetime.now(UTC),
                lo_key_kind="hotkey",
                lo_signer=lo,
                lo_signature="ab" * 64,
                hi_key_kind="hotkey",
                hi_signer=hi,
                hi_signature="cd" * 64,
            )
        _install_db(app, session_maker)

        claimed = (await client.post(_CLAIM_URL)).json()["items"][0]

        assert claimed["agent_id"] == str(retry)
        assert claimed["precheck_reason_code"] is None
        assert claimed["duplicate_of"] is None

    async def test_rescreened_older_hash_does_not_use_later_submission_as_owner(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        now = datetime.now(UTC)
        older = await _seed_agent(
            session_maker,
            status=AgentStatus.SCREENING_FAILED,
            miner_hotkey="5OlderMinerHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            created_at=now - timedelta(minutes=2),
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey="5LaterMinerHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            created_at=now - timedelta(minutes=1),
        )
        _install_db(app, session_maker)

        claimed = (await client.post(_CLAIM_URL)).json()["items"][0]

        assert claimed["agent_id"] == str(older)
        assert claimed["precheck_reason_code"] is None
        assert claimed["duplicate_of"] is None

    async def test_policy_mismatch_does_not_create_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)

        mismatch = await client.post(
            f"/api/v1/screener/claim?policy_version={SCREENING_POLICY_VERSION - 1}",
            headers=_AUTH_HEADER,
        )

        assert mismatch.status_code == 409
        assert mismatch.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.UPLOADED
            attempts = (await session.scalars(select(ScreeningAttempt))).all()
            assert attempts == []

    async def test_expired_lease_rejects_late_verdict(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        async with session_maker() as session, session.begin():
            attempt = await session.get(ScreeningAttempt, attempt_id)
            assert attempt is not None
            attempt.started_at = datetime.now(UTC) - timedelta(minutes=2)
            attempt.deadline = datetime.now(UTC) - timedelta(minutes=1)

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            headers=_AUTH_HEADER,
            json=_result_payload(agent_id, attempt_id=attempt_id),
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE

    async def test_attempt_cannot_be_replayed_for_another_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        claimed_agent = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            created_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        other_agent = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        item = next(
            row
            for row in claimed.json()["items"]
            if row["agent_id"] == str(claimed_agent)
        )
        attempt_id = UUID(item["attempt_id"])

        response = await client.post(
            f"/api/v1/screener/agent/{other_agent}/result",
            headers=_AUTH_HEADER,
            json=_result_payload(other_agent, attempt_id=attempt_id),
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE

    async def test_attempt_rejects_wrong_policy_even_for_signed_failure(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            headers=_AUTH_HEADER,
            json=_result_payload(
                agent_id,
                attempt_id=attempt_id,
                passed=False,
                policy_version=SCREENING_POLICY_VERSION - 1,
            ),
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE

    async def test_attempt_bound_quarantine_is_durable_and_idempotent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        payload = _result_payload(
            agent_id,
            passed=False,
            attempt_id=attempt_id,
            outcome="quarantine",
            manifest_digest="12" * 32,
            finding_digest="34" * 32,
            reason_code="agentic-source-review-tripwire",
        )
        first = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )
        replay = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )
        assert first.status_code == replay.status_code == 200, replay.text
        assert replay.json()["status"] == AgentStatus.QUARANTINED
        async with session_maker() as session:
            attempt = await session.get(ScreeningAttempt, attempt_id)
            quarantines = (await session.scalars(select(ScreeningQuarantine))).all()
            assert attempt is not None and attempt.status == "quarantined"
            assert len(quarantines) == 1

    async def test_deferred_mechanical_admission_retains_signed_audit_once(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING, name="mechanical-first"
        )
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
                    started_at=now,
                    deadline=now + timedelta(minutes=30),
                    reason_code="deferred-mechanical-admission",
                    build_only=True,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        audit = _bounded_review_audit()
        missing_signed_flag = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=True,
                attempt_id=attempt_id,
                outcome="pass_inconclusive",
                manifest_digest="12" * 32,
                reason_code="source-review-inconclusive",
                review_audit_digest=audit.canonical_digest(),
                review_audit=audit.model_dump(mode="json"),
                build_only=True,
            ),
        )
        assert missing_signed_flag.status_code == 409
        assert missing_signed_flag.json()["error_code"] == (
            ERROR_CODE_AGENT_NOT_SCREENABLE
        )
        payload = _result_payload(
            agent_id,
            passed=True,
            attempt_id=attempt_id,
            outcome="pass_inconclusive",
            manifest_digest="12" * 32,
            reason_code="source-review-inconclusive",
            review_audit_digest=audit.canonical_digest(),
            review_audit=audit.model_dump(mode="json"),
            build_only=True,
            deferred_source_review=True,
        )

        first = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )
        replay = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )

        assert first.status_code == replay.status_code == 200, replay.text
        assert replay.json()["status"] == AgentStatus.EVALUATING
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            attempt = await session.get(ScreeningAttempt, attempt_id)
            retained = (
                await session.scalars(
                    select(ScreeningQuarantine).where(
                        ScreeningQuarantine.attempt_id == attempt_id
                    )
                )
            ).all()
            assert agent is not None and agent.status == AgentStatus.EVALUATING
            assert attempt is not None and attempt.status == "passed"
            assert len(retained) == 1
            assert retained[0].status == "resolved"
            assert retained[0].review_audit_digest == audit.canonical_digest()
            assert retained[0].review_audit == audit.model_dump(mode="json")

        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        listed = await client.get(
            "/api/v1/admin/screening-quarantines?status=resolved",
            headers={"Authorization": "Bearer test-admin-token-at-least-32-characters"},
        )
        assert listed.status_code == 200, listed.text
        retained_item = listed.json()["items"][0]
        assert retained_item["review_audit_digest"] == audit.canonical_digest()
        assert retained_item["review_audit"] == audit.model_dump(mode="json")

        changed_audit = _bounded_review_audit(steps_used=7)
        conflict = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=True,
                attempt_id=attempt_id,
                outcome="pass_inconclusive",
                manifest_digest="12" * 32,
                reason_code="source-review-inconclusive",
                review_audit_digest=changed_audit.canonical_digest(),
                review_audit=changed_audit.model_dump(mode="json"),
                build_only=True,
                deferred_source_review=True,
            ),
        )
        assert conflict.status_code == 409
        assert conflict.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE

    async def test_ordinary_full_review_exhaustion_terminally_admits_once(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING, name="ordinary-review"
        )
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
                    started_at=now,
                    deadline=now + timedelta(minutes=30),
                    build_only=False,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        audit = _bounded_review_audit()

        payload = _result_payload(
            agent_id,
            passed=True,
            attempt_id=attempt_id,
            outcome="pass_inconclusive",
            manifest_digest="12" * 32,
            reason_code="source-review-inconclusive",
            review_audit_digest=audit.canonical_digest(),
            review_audit=audit.model_dump(mode="json"),
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=payload,
        )
        replay = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )

        assert response.status_code == replay.status_code == 200, replay.text
        assert replay.json()["status"] == AgentStatus.EVALUATING
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            attempts = (
                await session.scalars(
                    select(ScreeningAttempt).where(
                        ScreeningAttempt.agent_id == agent_id
                    )
                )
            ).all()
            retained = await session.scalar(
                select(ScreeningQuarantine).where(
                    ScreeningQuarantine.attempt_id == attempt_id
                )
            )
            assert agent is not None and agent.status == AgentStatus.EVALUATING
            assert agent.screening_reason == (
                "Bounded source review exhausted; admitted for scoring"
            )
            assert len(attempts) == 1 and attempts[0].status == "passed"
            assert retained is not None and retained.status == "resolved"
            assert retained.review_audit_digest == audit.canonical_digest()

    async def test_deferred_mechanical_admission_can_fail_closed_on_finding(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING, name="mechanical-finding"
        )
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
                    started_at=now,
                    deadline=now + timedelta(minutes=30),
                    reason_code="deferred-mechanical-admission",
                    build_only=True,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                attempt_id=attempt_id,
                outcome="quarantine",
                manifest_digest="56" * 32,
                finding_digest="78" * 32,
                reason_code="agentic-source-review-tripwire",
                build_only=True,
                deferred_source_review=True,
            ),
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == AgentStatus.QUARANTINED

    async def test_late_deep_review_result_cannot_reverse_operator_clear(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCORED,
            name="operator-cleared",
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        attempt_id = uuid4()
        opened_at = datetime.now(UTC) - timedelta(minutes=20)
        cleared_at = opened_at + timedelta(minutes=5)
        async with session_maker() as session, session.begin():
            session.add(
                AthReview(
                    review_id=uuid4(),
                    agent_id=agent_id,
                    status="resolved",
                    opened_at=opened_at,
                    resolved_at=cleared_at,
                    resolved_by="operator",
                    resolution="clear",
                    resolution_reason="Operator cleared the submission",
                    original_reason="Deferred source review",
                    original_policy_version=SCREENING_POLICY_VERSION,
                    original_evidence={"previous_status": AgentStatus.SCORED.value},
                    algorithm_provenance={"review_kind": "deferred_source_review"},
                )
            )
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="running",
                    started_at=opened_at + timedelta(minutes=1),
                    deadline=datetime.now(UTC) + timedelta(minutes=30),
                    build_only=False,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        audit = _bounded_review_audit()

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=True,
                attempt_id=attempt_id,
                outcome="pass_inconclusive",
                manifest_digest="12" * 32,
                reason_code="source-review-inconclusive",
                review_audit_digest=audit.canonical_digest(),
                review_audit=audit.model_dump(mode="json"),
            ),
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == AgentStatus.SCORED
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            review = await session.scalar(
                select(AthReview).where(AthReview.agent_id == agent_id)
            )
            retained = await session.scalar(
                select(ScreeningQuarantine).where(
                    ScreeningQuarantine.attempt_id == attempt_id
                )
            )
            assert agent is not None and agent.status == AgentStatus.SCORED
            assert agent.review_reason is None
            assert review is not None and review.status == "resolved"
            assert review.resolution_reason == "Operator cleared the submission"
            assert retained is not None and retained.status == "resolved"
            assert retained.resolution_reason == (
                "Late deep-review evidence retained after operator action"
            )

    async def test_deferred_review_health_miss_preserves_hold_and_retries(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.ATH_PENDING_REVIEW,
            name="deferred-health-retry",
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        attempt_id = uuid4()
        opened_at = datetime.now(UTC) - timedelta(minutes=5)
        async with session_maker() as session, session.begin():
            session.add(
                AthReview(
                    review_id=uuid4(),
                    agent_id=agent_id,
                    status="pending",
                    opened_at=opened_at,
                    original_reason=(
                        "Score qualified this submission for deferred source review"
                    ),
                    original_policy_version=SCREENING_POLICY_VERSION,
                    original_evidence={
                        "previous_status": AgentStatus.SCORED.value,
                        "score_count": 3,
                    },
                    algorithm_provenance={"review_kind": "deferred_source_review"},
                )
            )
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="running",
                    started_at=opened_at + timedelta(minutes=1),
                    deadline=datetime.now(UTC) + timedelta(minutes=30),
                    build_only=False,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)

        payload = _result_payload(
            agent_id,
            passed=False,
            attempt_id=attempt_id,
            outcome="deterministic_reject",
            reason_code="health-contract",
            detail="serve check failed: /health never healthy within 90s",
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )
        replay = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )

        assert response.status_code == replay.status_code == 200
        assert response.json()["status"] == AgentStatus.ATH_PENDING_REVIEW
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            attempt = await session.get(ScreeningAttempt, attempt_id)
            review = await session.scalar(
                select(AthReview).where(AthReview.agent_id == agent_id)
            )
            assert agent is not None
            assert agent.status == AgentStatus.ATH_PENDING_REVIEW
            assert agent.screening_reason == (
                "Deferred source review runtime verification was interrupted; "
                "retry scheduled"
            )
            assert agent.screening_reason_code == "health-contract"
            assert attempt is not None and attempt.status == "failed"
            assert review is not None and review.status == "pending"
            assert review.resolution is None

    async def test_ordinary_health_contract_failure_still_rejects_submission(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCREENING,
            name="ordinary-health-reject",
        )
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
                    started_at=now,
                    deadline=now + timedelta(minutes=30),
                    build_only=False,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                attempt_id=attempt_id,
                outcome="deterministic_reject",
                reason_code="health-contract",
                detail="serve check failed: /health never healthy within 90s",
            ),
        )

        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.REJECTED
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            attempt = await session.get(ScreeningAttempt, attempt_id)
            assert agent is not None and agent.status == AgentStatus.REJECTED
            assert agent.screening_reason == (
                "Container did not return a 2xx response from GET /health on port "
                "8080 during startup"
            )
            assert attempt is not None and attempt.status == "rejected"


class TestQuarantineAdmin:
    async def test_list_sorts_oldest_by_default_and_accepts_newest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        _install_db(app, session_maker)
        _install_chain(app)
        timestamps = [
            datetime(2026, 7, 13, 12, tzinfo=UTC),
            datetime(2026, 7, 15, 12, tzinfo=UTC),
        ]
        agent_ids: list[UUID] = []

        for index, created_at in enumerate(timestamps, start=1):
            agent_id = await _seed_agent(
                session_maker,
                status=AgentStatus.UPLOADED,
                name=f"quarantine-{index}",
                sha256=f"{index:02x}" * 32,
            )
            agent_ids.append(agent_id)
            claimed = await client.post(_CLAIM_URL)
            attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
            held = await client.post(
                f"/api/v1/screener/agent/{agent_id}/result",
                json=_result_payload(
                    agent_id,
                    passed=False,
                    attempt_id=attempt_id,
                    outcome="quarantine",
                    manifest_digest=f"{index + 10:02x}" * 32,
                    finding_digest=f"{index + 20:02x}" * 32,
                    reason_code="agentic-source-review-tripwire",
                ),
            )
            assert held.status_code == 200
            async with session_maker() as session, session.begin():
                quarantine = await session.scalar(
                    select(ScreeningQuarantine).where(
                        ScreeningQuarantine.agent_id == agent_id
                    )
                )
                assert quarantine is not None
                quarantine.created_at = created_at

        headers = {"Authorization": "Bearer test-admin-token-at-least-32-characters"}
        oldest = await client.get(
            "/api/v1/admin/screening-quarantines", headers=headers
        )
        newest = await client.get(
            "/api/v1/admin/screening-quarantines?sort=newest", headers=headers
        )

        assert oldest.status_code == newest.status_code == 200
        assert [item["agent_id"] for item in oldest.json()["items"]] == [
            str(agent_id) for agent_id in agent_ids
        ]
        assert [item["agent_id"] for item in newest.json()["items"]] == [
            str(agent_id) for agent_id in reversed(agent_ids)
        ]

    async def test_lists_and_safely_releases_live_validator_assignment(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        now = datetime.now(UTC)
        deadline = now + timedelta(minutes=45)
        validator_hotkey = "5ValidatorHotkeyForAdminReleaseTest"
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=validator_hotkey,
                    status=TicketStatus.ISSUED,
                    issued_at=now,
                    deadline=deadline,
                    bench_version=_TARGET_VERSION,
                    attempt_count=1,
                )
            )
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey="5CompletedValidator",
                    run_id="admin-release-preserved-score",
                    signature=None,
                    seed=42,
                    composite=0.75,
                    tool_mean=0.7,
                    memory_mean=0.8,
                    median_ms=123,
                    n=114,
                    # The admin listing counts the scores whose advisory era
                    # matches the ticket's, so this one is the era the lease is
                    # for and the one below is an older era that must not count.
                    details={"bench_version": _TARGET_VERSION},
                    generated_at=now,
                )
            )
            session.add(
                Score(
                    agent_id=agent_id,
                    validator_hotkey="5OlderBenchValidator",
                    run_id="admin-release-old-version-score",
                    signature=None,
                    seed=41,
                    composite=0.25,
                    tool_mean=0.2,
                    memory_mean=0.3,
                    median_ms=456,
                    n=114,
                    details={"bench_version": _SOURCE_VERSION},
                    generated_at=now - timedelta(days=1),
                )
            )
        _install_db(app, session_maker)
        headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:test-user",
        }

        listing = await client.get(
            "/api/v1/admin/validator-assignments", headers=headers
        )
        assert listing.status_code == 200
        assignment = listing.json()["items"][0]
        assert assignment["agent_id"] == str(agent_id)
        assert assignment["validator_hotkey"] == validator_hotkey
        assert assignment["score_count"] == 1
        assert assignment["provisional_composite"] == pytest.approx(0.75)

        released = await client.post(
            f"/api/v1/admin/validator-assignments/{agent_id}/{validator_hotkey}/release",
            headers=headers,
            json={
                "expected_deadline": assignment["deadline"],
                "reason": "Operator stopped a stale validator process",
            },
        )
        replay = await client.post(
            f"/api/v1/admin/validator-assignments/{agent_id}/{validator_hotkey}/release",
            headers=headers,
            json={
                "expected_deadline": assignment["deadline"],
                "reason": "Operator stopped a stale validator process",
            },
        )
        assert released.status_code == 200
        assert released.json()["status"] == TicketStatus.EXPIRED
        assert replay.status_code == 409

        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _TARGET_VERSION, validator_hotkey)
            )
            scores = (
                await session.scalars(select(Score).where(Score.agent_id == agent_id))
            ).all()
            assert ticket is not None
            assert ticket.status == TicketStatus.EXPIRED
            assert ticket.retry_after is not None
            retry_after = ticket.retry_after.replace(tzinfo=UTC)
            assert now + timedelta(hours=5, minutes=59) < retry_after
            assert retry_after < deadline + timedelta(hours=6)
            assert len(scores) == 2

    @pytest.mark.parametrize(
        ("resolution", "expected_status"),
        [
            ("release", AgentStatus.EVALUATING),
            ("rescreen", AgentStatus.SCREENING_FAILED),
            ("reject", AgentStatus.REJECTED),
        ],
    )
    async def test_detailed_resolution_reason_and_conflicting_second_resolution(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        resolution: str,
        expected_status: AgentStatus,
    ) -> None:
        detailed_reason = " ".join(
            [
                "Source review evidence shows src/router.py:118 selects providers from "
                "the declared runtime configuration instead of matching benchmark "
                "prompts.",
                "The branch at src/router.py:146 handles a documented timeout fallback "
                "and does not inspect prompt text, expected answers, evaluator "
                "metadata, or test fixture identifiers.",
                "A repository-wide search found no embedded benchmark answers, prompt "
                "hashes, fixture names, response lookup tables, or network calls to "
                "undeclared services.",
                "The submitted image was rebuilt from the reviewed archive, then "
                "smoke-tested with unrelated prompts that exercised both the primary "
                "provider and fallback path.",
                "Observed outputs varied with the request and provider response, which "
                "is inconsistent with replay or benchmark emulation.",
                "Release is appropriate because the suspicious fast path is general "
                "routing logic; retain this source-level evidence in the audited "
                "miner-visible decision.",
            ]
        )
        assert len(detailed_reason) > 500
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        quarantine_payload = _result_payload(
            agent_id,
            passed=False,
            attempt_id=attempt_id,
            outcome="quarantine",
            manifest_digest="56" * 32,
            finding_digest="78" * 32,
            reason_code="agentic-source-review-tripwire",
        )
        held = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=quarantine_payload
        )
        assert held.status_code == 200

        admin_headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:test-user",
        }
        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=admin_headers
        )
        assert listing.status_code == 200
        item = listing.json()["items"][0]
        assert item["agent_id"] == str(agent_id)
        assert item["reason_code"] == "agentic-source-review-tripwire"
        assert "source" not in item

        blank_reason = await client.post(
            f"/api/v1/admin/screening-quarantines/{item['quarantine_id']}/resolve",
            headers=admin_headers,
            json={"resolution": resolution, "reason": "   "},
        )
        resolved = await client.post(
            f"/api/v1/admin/screening-quarantines/{item['quarantine_id']}/resolve",
            headers=admin_headers,
            json={
                "resolution": resolution,
                "reason": detailed_reason,
            },
        )
        conflict = await client.post(
            f"/api/v1/admin/screening-quarantines/{item['quarantine_id']}/resolve",
            headers=admin_headers,
            json={"resolution": "reject", "reason": "Conflicting action"},
        )
        assert blank_reason.status_code == 422
        assert resolved.status_code == 200
        assert resolved.json()["agent_status"] == expected_status
        resolved_quarantine = resolved.json()["quarantine"]
        assert resolved_quarantine["resolution_reason"] == detailed_reason
        assert len(resolved_quarantine["resolution_history"]) == 1
        history_event = resolved_quarantine["resolution_history"][0]
        assert history_event["resolution"] == resolution
        assert history_event["reason"] == detailed_reason
        assert history_event["actor"] == "backroom:test-user"
        assert conflict.status_code == 409
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert agent.screening_reason == detailed_reason

    async def test_rejected_quarantine_can_be_corrected_to_release_with_history(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        held = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                attempt_id=attempt_id,
                outcome="quarantine",
                manifest_digest="56" * 32,
                finding_digest="78" * 32,
                reason_code="agentic-source-review-tripwire",
            ),
        )
        assert held.status_code == 200

        quarantine = (
            await client.get(
                "/api/v1/admin/screening-quarantines",
                headers={
                    "Authorization": "Bearer test-admin-token-at-least-32-characters"
                },
            )
        ).json()["items"][0]
        admin_headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:test-user",
        }
        rejected = await client.post(
            f"/api/v1/admin/screening-quarantines/{quarantine['quarantine_id']}/resolve",
            headers=admin_headers,
            json={"resolution": "reject", "reason": "Initial manual rejection"},
        )
        corrected = await client.post(
            f"/api/v1/admin/screening-quarantines/{quarantine['quarantine_id']}/resolve",
            headers={**admin_headers, "X-Admin-Actor": "backroom:second-reviewer"},
            json={
                "resolution": "release",
                "reason": "Second review confirmed a false positive",
            },
        )
        repeated = await client.post(
            f"/api/v1/admin/screening-quarantines/{quarantine['quarantine_id']}/resolve",
            headers=admin_headers,
            json={"resolution": "release", "reason": "Release it again"},
        )

        assert rejected.status_code == 200
        assert corrected.status_code == 200
        assert corrected.json()["agent_status"] == AgentStatus.EVALUATING
        assert corrected.json()["quarantine"]["resolution"] == "release"
        assert [
            event["resolution"]
            for event in corrected.json()["quarantine"]["resolution_history"]
        ] == ["reject", "release"]
        assert repeated.status_code == 409

        detail = await client.get(
            f"/api/v1/admin/screening-quarantines/{quarantine['quarantine_id']}",
            headers=admin_headers,
        )
        assert [event["actor"] for event in detail.json()["resolution_history"]] == [
            "backroom:test-user",
            "backroom:second-reviewer",
        ]

        pipeline = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        assert pipeline.status_code == 200
        assert pipeline.json()["status"] == "waiting_validator"
        assert pipeline.json()["screening_attempts"][0]["quarantine_resolution"] == (
            "release"
        )

        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            history = (
                await session.scalars(
                    select(ScreeningQuarantineResolution).order_by(
                        ScreeningQuarantineResolution.created_at
                    )
                )
            ).all()
            assert agent is not None
            assert agent.status == AgentStatus.EVALUATING
            assert agent.screening_reason == "Second review confirmed a false positive"
            assert [event.resolution for event in history] == ["reject", "release"]

    async def test_release_pins_dataset_when_generation_is_enabled(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        now = datetime.now(UTC)
        rollout_id = uuid4()
        async with session_maker() as session, session.begin():
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.dataset_seed = 42
            agent.dataset_sha256 = "ab" * 32
            agent.dataset_run_size = "full"
            agent.dataset_seed_block = 123
            agent.dataset_seed_block_hash = "0x" + "12" * 32
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_SOURCE_VERSION,
                    seed=42,
                    sha256="ab" * 32,
                    run_size="full",
                    seed_block=123,
                    seed_block_hash="0x" + "12" * 32,
                )
            )
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=_SOURCE_VERSION,
                    desired_version=_TARGET_VERSION,
                    status="activated",
                    cohort_size=5,
                    created_at=now,
                    activated_at=now,
                )
            )
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=rollout_id,
                    agent_id=agent_id,
                    position=1,
                    frozen_miner_hotkey=agent.miner_hotkey,
                    frozen_composite=0.9,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        held = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                attempt_id=attempt_id,
                outcome="quarantine",
                manifest_digest="56" * 32,
                finding_digest="78" * 32,
                reason_code="agentic-source-review-tripwire",
            ),
        )
        assert held.status_code == 200

        # Historical infrastructure expiries must not override the operator's
        # later release when this agent re-enters for its build-only image pass.
        expired_base = datetime.now(UTC) - timedelta(hours=6)
        async with session_maker() as session, session.begin():
            for index in range(MAX_SCREENING_EXPIRIES):
                started = expired_base + timedelta(minutes=45 * index)
                session.add(
                    ScreeningAttempt(
                        attempt_id=uuid4(),
                        agent_id=agent_id,
                        screener_hotkey=_SCREENER_HOTKEY,
                        policy_version=SCREENING_POLICY_VERSION,
                        status="expired",
                        started_at=started,
                        deadline=started + timedelta(minutes=45),
                        finished_at=started + timedelta(minutes=45),
                        public_reason="Screening lease expired",
                    )
                )

        generator = _FakeGenerator(run_size="full", sha="be" * 32)
        _install_generator(app, generator)
        headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:test-user",
        }
        quarantine = (
            await client.get("/api/v1/admin/screening-quarantines", headers=headers)
        ).json()["items"][0]
        released = await client.post(
            f"/api/v1/admin/screening-quarantines/{quarantine['quarantine_id']}/resolve",
            headers=headers,
            json={"resolution": "release", "reason": "Manual review passed"},
        )

        assert released.status_code == 200
        assert released.json()["agent_status"] == AgentStatus.EVALUATING
        assert [
            event["resolution"]
            for event in released.json()["quarantine"]["resolution_history"]
        ] == ["release"]
        assert generator.calls == 1
        assert generator.seeds == [42]
        assert generator.bench_versions == [_TARGET_VERSION]
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            source = await session.get(BenchmarkDataset, (agent_id, _SOURCE_VERSION))
            target = await session.get(BenchmarkDataset, (agent_id, _TARGET_VERSION))
            assert agent is not None
            assert agent.dataset_seed == 42
            assert agent.dataset_sha256 == "ab" * 32
            assert agent.dataset_run_size == "full"
            assert source is not None and source.sha256 == "ab" * 32
            assert target is not None and target.sha256 == "be" * 32

        # The release pinned the active dataset, so the missing-DATASET branch
        # must not re-fire. But this artifact tripped source review BEFORE its
        # screened image was built (agentic-source-review-tripwire), so it is
        # released to EVALUATING without the image v7 requires. It therefore
        # correctly re-enters screening via the missing-screened-image branch to
        # build that image — otherwise validators would skip it forever.
        next_claim = await client.post(_CLAIM_URL)
        assert next_claim.status_code == 200
        reclaimed = next(
            item
            for item in next_claim.json()["items"]
            if item["agent_id"] == str(agent_id)
        )
        assert reclaimed["build_only"] is True

    async def test_build_only_attempt_cannot_quarantine(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # An EVALUATING (already-adjudicated) agent missing its image is
        # re-claimed as a BUILD-ONLY pass. The screener must not be able to
        # quarantine it — that would let a re-screen silently override the prior
        # release/pass that made it EVALUATING.
        agent_id = uuid4()
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            agent = Agent(
                agent_id=agent_id,
                miner_hotkey="5HKapproved",
                name="approved-no-image",
                sha256=uuid4().hex * 2,
                status=AgentStatus.EVALUATING,
            )
            agent.screening_policy_version = SCREENING_POLICY_VERSION
            session.add(agent)
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_SOURCE_VERSION,
                    desired_version=_TARGET_VERSION,
                    status="activated",
                    cohort_size=5,
                    created_at=now - timedelta(hours=1),
                    activated_at=now,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)

        claimed = await client.post(_CLAIM_URL)
        item = next(
            entry
            for entry in claimed.json()["items"]
            if entry["agent_id"] == str(agent_id)
        )
        assert item["build_only"] is True
        attempt_id = UUID(item["attempt_id"])

        rejected = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                attempt_id=attempt_id,
                outcome="quarantine",
                manifest_digest="56" * 32,
                finding_digest="78" * 32,
                reason_code="agentic-source-review-tripwire",
            ),
        )
        assert rejected.status_code >= 400
        refreshed_status = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()["status"]
        assert refreshed_status != "under_review"

    async def test_admin_auth_is_required(
        self, app: FastAPI, client: httpx.AsyncClient
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        response = await client.get(
            "/api/v1/admin/screening-quarantines",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    async def test_miner_can_submit_one_private_dispute_and_operator_can_release(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            miner_hotkey=_KEYPAIR.ss58_address,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        held = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                attempt_id=attempt_id,
                outcome="quarantine",
                manifest_digest="56" * 32,
                finding_digest="78" * 32,
                reason_code="agentic-source-review-tripwire",
            ),
        )
        assert held.status_code == 200
        admin_headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:first-reviewer",
        }
        quarantine = (
            await client.get(
                "/api/v1/admin/screening-quarantines", headers=admin_headers
            )
        ).json()["items"][0]
        rejected = await client.post(
            f"/api/v1/admin/screening-quarantines/{quarantine['quarantine_id']}/resolve",
            headers=admin_headers,
            json={
                "resolution": "reject",
                "reason": "Initial review found benchmark-specific behavior",
            },
        )
        assert rejected.status_code == 200

        message = (
            "The implementation uses generic schema normalization and does not "
            "contain benchmark-specific answer logic."
        )
        invalid = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": "00" * 64},
        )
        assert invalid.status_code == 401

        signature = _sign(screening_dispute_signing_message(agent_id, message))
        submitted = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": signature},
        )
        repeated = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={"message": message, "signature": signature},
        )
        assert submitted.status_code == 201
        assert submitted.json()["dispute"]["status"] == "pending"
        assert message not in submitted.text
        assert repeated.status_code == 409

        pipeline = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        assert pipeline.json()["dispute"]["status"] == "pending"
        assert message not in pipeline.text

        listing = await client.get(
            "/api/v1/admin/screening-disputes", headers=admin_headers
        )
        dispute = listing.json()["items"][0]
        assert listing.json()["count"] == 1
        assert dispute["message"] == message
        assert dispute["original_reason"] == (
            "Initial review found benchmark-specific behavior"
        )

        resolved = await client.post(
            f"/api/v1/admin/screening-disputes/{dispute['dispute_id']}/resolve",
            headers={**admin_headers, "X-Admin-Actor": "backroom:appeals-reviewer"},
            json={
                "resolution": "release",
                "reason": "Second review confirmed the rejection was a false positive",
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["agent_status"] == AgentStatus.EVALUATING
        assert resolved.json()["dispute"]["resolution"] == "release"
        assert resolved.json()["dispute"]["original_reason"] == (
            "Initial review found benchmark-specific behavior"
        )

        pipeline = await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        assert pipeline.json()["status"] == "waiting_validator"
        assert pipeline.json()["dispute"]["resolution"] == "release"
        assert pipeline.json()["screening_attempts"][0]["quarantine_resolution"] == (
            "release"
        )
        async with session_maker() as session:
            disputes = (await session.scalars(select(ScreeningDispute))).all()
            assert len(disputes) == 1

    async def test_operator_can_uphold_dispute_without_changing_rejection(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.REJECTED,
            miner_hotkey=_KEYPAIR.ss58_address,
        )
        attempt_id = uuid4()
        quarantine_id = uuid4()
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="quarantined",
                    started_at=now,
                    deadline=now + timedelta(minutes=10),
                    finished_at=now,
                )
            )
            session.add(
                ScreeningQuarantine(
                    quarantine_id=quarantine_id,
                    agent_id=agent_id,
                    attempt_id=attempt_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    manifest_digest="56" * 32,
                    finding_digest="78" * 32,
                    reason_code="agentic-source-review-tripwire",
                    status="resolved",
                    created_at=now,
                    resolved_at=now,
                    resolved_by="backroom:first-reviewer",
                    resolution="reject",
                    resolution_reason="Initial rejection remains supported",
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)

        message = (
            "Please review the generic retrieval path and supporting source again."
        )
        submitted = await client.post(
            f"/api/v1/public/agent/{agent_id}/dispute",
            json={
                "message": message,
                "signature": _sign(
                    screening_dispute_signing_message(agent_id, message)
                ),
            },
        )
        assert submitted.status_code == 201
        listing = await client.get(
            "/api/v1/admin/screening-disputes",
            headers={"Authorization": "Bearer test-admin-token-at-least-32-characters"},
        )
        dispute_id = listing.json()["items"][0]["dispute_id"]
        upheld = await client.post(
            f"/api/v1/admin/screening-disputes/{dispute_id}/resolve",
            headers={
                "Authorization": "Bearer test-admin-token-at-least-32-characters",
                "X-Admin-Actor": "backroom:appeals-reviewer",
            },
            json={
                "resolution": "uphold",
                "reason": (
                    "Second review confirmed the original benchmark-specific finding"
                ),
            },
        )
        assert upheld.status_code == 200
        assert upheld.json()["agent_status"] == AgentStatus.REJECTED
        assert upheld.json()["dispute"]["resolution"] == "uphold"

    async def test_lists_all_screening_outcomes_and_issues_audited_artifact_url(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        duplicate_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCORED,
            name="Jackie",
            miner_hotkey="5DuplicateMinerHotkeyXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            version=2,
        )
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.REJECTED, version=3
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ScreeningAttempt(
                    attempt_id=uuid4(),
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="rejected",
                    started_at=now - timedelta(minutes=2),
                    deadline=now + timedelta(minutes=28),
                    finished_at=now,
                    public_reason="Docker image build failed",
                    reason_code="exact-cross-miner-duplicate",
                    duplicate_of=duplicate_id,
                )
            )
            session.add(
                ScreeningAttempt(
                    attempt_id=uuid4(),
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION - 1,
                    status="passed",
                    started_at=now - timedelta(days=1),
                    deadline=now - timedelta(days=1) + timedelta(minutes=30),
                    finished_at=now - timedelta(days=1) + timedelta(minutes=4),
                )
            )
        _install_db(app, session_maker)
        storage = _install_storage(app)
        headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:test-user",
        }

        listing = await client.get(
            "/api/v1/admin/screening-submissions", headers=headers
        )
        exact = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}", headers=headers
        )
        artifact = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/artifact",
            headers=headers,
        )

        assert listing.status_code == 200
        item = listing.json()["items"][0]
        assert item["agent_id"] == str(agent_id)
        assert item["agent_version"] == 3
        assert item["attempts"][0]["status"] == "rejected"
        assert item["attempts"][0]["reason"] == "Docker image build failed"
        assert item["attempts"][0]["duplicate_name"] == "Jackie"
        assert item["attempts"][0]["duplicate_version"] == 2
        assert [attempt["status"] for attempt in item["attempts"]] == [
            "rejected",
            "passed",
        ]
        assert exact.status_code == 200
        assert exact.json() == item
        assert "download_url" not in exact.json()
        assert artifact.status_code == 200
        assert artifact.json()["sha256"] == _SHA256
        assert storage.presigned_get_url.await_args.kwargs == {
            "key": f"{agent_id}/agent.tar.gz",
            "expires_in": 300,
        }

    async def test_screening_failure_summary_groups_live_pipeline_by_reason_code(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        failed_a = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING_FAILED, name="jam-a"
        )
        failed_b = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING_FAILED, name="jam-b"
        )
        running = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING, name="in-flight"
        )
        scored = await _seed_agent(
            session_maker, status=AgentStatus.SCORED, name="already-through"
        )
        async with session_maker() as session, session.begin():
            for agent_id, code in (
                (failed_a, "l2-analyzer-exited-125"),
                (failed_b, "l2-analyzer-exited-125"),
                (running, None),
                (scored, "l2-valueerror"),
            ):
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                agent.screening_reason_code = code
        _install_db(app, session_maker)
        headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
        }

        response = await client.get(
            "/api/v1/admin/screening-failures?example_limit=1", headers=headers
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["screening"] == 1
        assert payload["screening_failed"] == 2
        assert [group["reason_code"] for group in payload["groups"]] == [
            "l2-analyzer-exited-125",
            None,
        ]
        analyzer = payload["groups"][0]
        assert analyzer["agent_status"] == "screening_failed"
        assert analyzer["count"] == 2
        assert len(analyzer["examples"]) == 1
        assert {example["agent_name"] for example in analyzer["examples"]} <= {
            "jam-a",
            "jam-b",
        }

    async def test_exact_screening_submission_requires_auth_and_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        _install_db(app, session_maker)
        unknown_id = uuid4()

        unauthenticated = await client.get(
            f"/api/v1/admin/screening-submissions/{unknown_id}"
        )
        missing = await client.get(
            f"/api/v1/admin/screening-submissions/{unknown_id}",
            headers={"Authorization": "Bearer test-admin-token-at-least-32-characters"},
        )

        assert unauthenticated.status_code == 401
        assert missing.status_code == 404
        assert missing.json()["message"] == "screening submission not found"

    async def test_rejected_rescreen_preserves_score_and_attempt_history(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.REJECTED,
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        await _seed_score(session_maker, agent_id=agent_id)
        attempt_id = uuid4()
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="rejected",
                    started_at=now - timedelta(minutes=2),
                    deadline=now + timedelta(minutes=28),
                    finished_at=now,
                    public_reason="Docker image build failed",
                )
            )
        _install_db(app, session_maker)
        response = await client.post(
            f"/api/v1/admin/screening-submissions/{agent_id}/rescreen",
            headers={
                "Authorization": "Bearer test-admin-token-at-least-32-characters",
                "X-Admin-Actor": "backroom:test-user",
            },
            json={
                "reason": "Build was interrupted by a worker deployment",
                "expected_sha256": _SHA256,
                "expected_score_count": 1,
            },
        )
        assert response.status_code == 200
        assert response.json()["agent_status"] == AgentStatus.SCREENING_FAILED
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            attempts = list(
                await session.scalars(
                    select(ScreeningAttempt).where(
                        ScreeningAttempt.agent_id == agent_id
                    )
                )
            )
            scores = list(
                await session.scalars(select(Score).where(Score.agent_id == agent_id))
            )
            assert agent is not None
            assert agent.status == AgentStatus.SCREENING_FAILED
            assert agent.screening_policy_version == SCREENING_POLICY_VERSION
            assert [attempt.attempt_id for attempt in attempts] == [attempt_id]
            assert len(scores) == 1

    async def test_operator_retry_now_waives_only_exact_failed_attempt_backoff(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCREENING_FAILED,
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        attempt_id = uuid4()
        now = datetime.now(UTC)
        original_deadline = now + timedelta(minutes=50)
        async with session_maker() as session, session.begin():
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="expired",
                    started_at=now - timedelta(minutes=10),
                    deadline=original_deadline,
                    finished_at=now,
                    public_reason="Screening was inconclusive; retry scheduled",
                    reason_code="source-review-step-budget-exhausted",
                )
            )
        _install_db(app, session_maker)

        held = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        assert held.status_code == 200
        assert held.json()["items"] == []

        request = {
            "reason": "Retry immediately after source-review budget exhaustion",
            "expected_sha256": _SHA256,
            "expected_score_count": 0,
            "expected_attempt_id": str(attempt_id),
        }
        response = await client.post(
            f"/api/v1/admin/screening-submissions/{agent_id}/retry-now",
            headers={
                "Authorization": "Bearer test-admin-token-at-least-32-characters",
                "X-Admin-Actor": "backroom:test-user",
            },
            json=request,
        )
        assert response.status_code == 200, response.text
        assert response.json()["attempt_id"] == str(attempt_id)
        assert response.json()["idempotent"] is False

        repeated = await client.post(
            f"/api/v1/admin/screening-submissions/{agent_id}/retry-now",
            headers={
                "Authorization": "Bearer test-admin-token-at-least-32-characters",
                "X-Admin-Actor": "backroom:test-user",
            },
            json=request,
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["override_id"] == response.json()["override_id"]
        assert repeated.json()["idempotent"] is True

        async with session_maker() as session:
            attempt = await session.get(ScreeningAttempt, attempt_id)
            overrides = list(
                await session.scalars(
                    select(ScreeningRetryOverride).where(
                        ScreeningRetryOverride.agent_id == agent_id
                    )
                )
            )
        assert attempt is not None
        assert attempt.status == "expired"
        assert attempt.deadline == original_deadline
        assert len(overrides) == 1
        assert overrides[0].actor == "backroom:test-user"

        claimed = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["items"][0]["agent_id"] == str(agent_id)

    async def test_operator_rebuilds_only_the_screened_image_build_only(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        now = datetime.now(UTC)
        rollout_id = uuid4()
        attempt_id = uuid4()
        image_upload_id = uuid5(
            NAMESPACE_URL, f"{agent_id}:{attempt_id}:stale-screened-image"
        )
        validator_hotkey = "5ValidatorWithLegacyImageTransport"
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=_SOURCE_VERSION,
                    desired_version=_TARGET_VERSION,
                    status="activated",
                    cohort_size=5,
                    created_at=now,
                    activated_at=now,
                )
            )
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=rollout_id,
                    agent_id=agent_id,
                    position=1,
                    frozen_miner_hotkey=_MINER_HOTKEY,
                    frozen_composite=0.9,
                )
            )
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="passed",
                    started_at=now - timedelta(minutes=5),
                    deadline=now,
                    finished_at=now,
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
                    image_id="sha256:" + "34" * 32,
                    image_ref=f"ditto-screen/{agent_id}:latest",
                    status="verified",
                    expires_at=now + timedelta(minutes=15),
                    verified_at=now,
                )
            )
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.screened_image_sha256 = "12" * 32
            agent.screened_image_size_bytes = 123
            agent.screened_image_id = "sha256:" + "34" * 32
            agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
            agent.screened_image_upload_id = image_upload_id
            agent.screened_image_verified_at = now
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_TARGET_VERSION,
                    seed=42,
                    sha256="aa" * 32,
                    run_size="full",
                )
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=validator_hotkey,
                    status=TicketStatus.ISSUED,
                    issued_at=now,
                    deadline=now + timedelta(minutes=90),
                    bench_version=_TARGET_VERSION,
                    attempt_count=2,
                )
            )

        _install_db(app, session_maker)
        headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:test-user",
        }
        inspected = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/rebuild-screened-image",
            headers=headers,
        )
        assert inspected.status_code == 200, inspected.text
        assert inspected.json()["rebuild_allowed"] is True
        assert inspected.json()["validator_ticket_active"] is True

        rebuilt = await client.post(
            f"/api/v1/admin/screening-submissions/{agent_id}/rebuild-screened-image",
            headers=headers,
            json={
                "reason": "Rebuild legacy image transport for current validators",
                "expected_sha256": _SHA256,
                "expected_bench_version": _TARGET_VERSION,
                "expected_score_count": 0,
                "expected_image_sha256": "12" * 32,
                "expected_image_upload_id": str(image_upload_id),
            },
        )
        assert rebuilt.status_code == 200, rebuilt.text
        assert rebuilt.json()["expired_ticket_count"] == 1

        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            dataset = await session.get(BenchmarkDataset, (agent_id, _TARGET_VERSION))
            ticket = await session.get(
                ValidatorTicket, (agent_id, _TARGET_VERSION, validator_hotkey)
            )
            event = await session.scalar(
                select(ScoreAuditEntry).where(
                    ScoreAuditEntry.agent_id == agent_id,
                    ScoreAuditEntry.event
                    == f"screened_image_rebuild:v{_TARGET_VERSION}",
                )
            )
            assert agent is not None
            assert agent.status == AgentStatus.EVALUATING
            assert agent.screened_image_sha256 is None
            assert dataset is not None and dataset.sha256 == "aa" * 32
            assert ticket is not None and ticket.status == TicketStatus.EXPIRED
            assert ticket.attempt_count < ticket_attempt_cap(ticket)
            assert event is not None

        claim = await client.post(_CLAIM_URL)
        assert claim.status_code == 200, claim.text
        assert claim.json()["items"][0]["agent_id"] == str(agent_id)
        assert claim.json()["items"][0]["build_only"] is True

    async def test_contract_refresh_rescreens_rebuilds_and_reissues_the_dataset(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        now = datetime.now(UTC)
        attempt_id = uuid4()
        image_upload_id = uuid5(
            NAMESPACE_URL, f"{agent_id}:{attempt_id}:screened-image"
        )
        validator_hotkey = "5ValidatorWithStaleTargetContract"
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_SOURCE_VERSION,
                    desired_version=_TARGET_VERSION,
                    status="activated",
                    cohort_size=5,
                    created_at=now,
                    activated_at=now,
                )
            )
            session.add(
                ScreeningAttempt(
                    attempt_id=attempt_id,
                    agent_id=agent_id,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="passed",
                    started_at=now - timedelta(minutes=5),
                    deadline=now,
                    finished_at=now,
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
                    image_id="sha256:" + "34" * 32,
                    image_ref=f"ditto-screen/{agent_id}:latest",
                    status="verified",
                    expires_at=now + timedelta(minutes=15),
                    verified_at=now,
                )
            )
            await session.flush()
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.dataset_seed = 42
            agent.screened_image_sha256 = "12" * 32
            agent.screened_image_size_bytes = 123
            agent.screened_image_id = "sha256:" + "34" * 32
            agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
            agent.screened_image_upload_id = image_upload_id
            agent.screened_image_verified_at = now
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_TARGET_VERSION,
                    seed=42,
                    sha256="aa" * 32,
                    run_size="full",
                )
            )
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    validator_hotkey=validator_hotkey,
                    status=TicketStatus.ISSUED,
                    issued_at=now,
                    deadline=now + timedelta(minutes=90),
                    bench_version=_TARGET_VERSION,
                    attempt_count=2,
                )
            )

        _install_db(app, session_maker)
        _install_chain(app)
        generator = _FakeGenerator(run_size="full", sha="cd" * 32)
        _install_generator(app, generator)
        headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:test-user",
        }
        inspected = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/"
            "refresh-benchmark-contract",
            headers=headers,
        )
        assert inspected.status_code == 200, inspected.text
        assert inspected.json() == {
            "agent_id": str(agent_id),
            "agent_name": "alpha-agent",
            "agent_status": AgentStatus.EVALUATING,
            "artifact_sha256": _SHA256,
            "bench_version": _TARGET_VERSION,
            "dataset_sha256": "aa" * 32,
            "score_count": 0,
            "screening_attempt_active": False,
            "refresh_allowed": True,
            "blocking_reason": None,
        }
        refreshed = await client.post(
            f"/api/v1/admin/screening-submissions/{agent_id}/"
            "refresh-benchmark-contract",
            headers=headers,
            json={
                "reason": "Generator and scorer produced different v7 datasets",
                "expected_sha256": _SHA256,
                "expected_bench_version": _TARGET_VERSION,
                "expected_dataset_sha256": "aa" * 32,
                "expected_score_count": 0,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["expired_ticket_count"] == 1

        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            dataset = await session.get(BenchmarkDataset, (agent_id, _TARGET_VERSION))
            stale_ticket = await session.get(
                ValidatorTicket, (agent_id, _TARGET_VERSION, validator_hotkey)
            )
            assert agent is not None
            assert agent.status == AgentStatus.SCREENING_FAILED
            assert agent.screened_image_sha256 is None
            assert dataset is None
            assert stale_ticket is not None
            assert stale_ticket.status == TicketStatus.EXPIRED
            assert stale_ticket.attempt_count < ticket_attempt_cap(stale_ticket)

        claim = await client.post(_CLAIM_URL)
        fresh_attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=fresh_attempt_id
        )
        verdict = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=fresh_attempt_id),
        )
        assert verdict.status_code == 200, verdict.text
        assert generator.bench_versions == [_TARGET_VERSION]

        async with session_maker() as session, session.begin():
            dataset = await session.get(BenchmarkDataset, (agent_id, _TARGET_VERSION))
            assert dataset is not None
            assert dataset.sha256 == "cd" * 32
            fresh_ticket = await issue_ticket(
                session,
                validator_hotkey=validator_hotkey,
                now=now + timedelta(minutes=1),
                ttl=timedelta(minutes=90),
                bench_version=_TARGET_VERSION,
                artifact_mode="screened_only",
            )
            assert fresh_ticket is not None
            assert fresh_ticket.agent_id == agent_id
            assert fresh_ticket.bench_version == _TARGET_VERSION
            assert fresh_ticket.status == TicketStatus.ISSUED

    async def test_zero_score_v2_migration_can_no_longer_be_reached(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The v2-to-v3 rescue lane is closed, and closed at its own front door.

        This test used to run the lane end to end: an open v2 -> v3 rollout, a
        zero-score v2 submission, and an assertion that its history survived
        while a fresh v3 dataset and lease were issued. Every part of that is
        now unreachable, and deliberately so.

        ``inspect_benchmark_contract_migration`` and its POST both require
        ``rollout.from_version == 2 and rollout.desired_version == 3``
        literally. A rollout aiming at v3 cannot be created -- it violates
        ``benchmark_rollout_desired_floor`` -- and no v2 -> v3 rollout is open in
        production, because the fleet activated past it long ago and
        ``open_rollout`` only returns a collecting one. Even granted the
        premise, the v3 lease at the end would be refused by the
        ``validator_tickets`` floor trigger.

        So the assertion is inverted: against the only transition that CAN be
        open, the lane reports itself blocked rather than migrating anything.
        That is the guarantee worth pinning -- a retired era cannot be re-entered
        through an admin endpoint any more than through the queue.
        """
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            screening_policy_version=SCREENING_POLICY_VERSION,
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_SOURCE_VERSION,
                    desired_version=_TARGET_VERSION,
                    status="collecting",
                    cohort_size=5,
                    created_at=now,
                )
            )
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_SOURCE_VERSION,
                    seed=42,
                    sha256="aa" * 32,
                    run_size="full",
                    seed_block=4321,
                    seed_block_hash="0x" + "9f" * 32,
                )
            )

        _install_db(app, session_maker)
        _install_chain(app)
        generator = _FakeGenerator(run_size="full", sha="cd" * 32)
        _install_generator(app, generator)
        headers = {
            "Authorization": "Bearer test-admin-token-at-least-32-characters",
            "X-Admin-Actor": "backroom:test-user",
        }
        inspected = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/"
            "migrate-benchmark-contract",
            headers=headers,
        )
        assert inspected.status_code == 200, inspected.text
        assert inspected.json()["migration_allowed"] is False
        assert inspected.json()["blocking_reason"] == (
            "an open v2-to-v3 rollout is required"
        )

        migrated = await client.post(
            f"/api/v1/admin/screening-submissions/{agent_id}/"
            "migrate-benchmark-contract",
            headers=headers,
            json={
                "reason": "Legacy zero-score submission needs the active contract",
                "expected_sha256": _SHA256,
                "expected_source_bench_version": 2,
                "expected_target_bench_version": 3,
                "expected_source_dataset_sha256": "aa" * 32,
                "expected_source_score_count": 0,
                "expected_target_score_count": 0,
            },
        )
        assert migrated.status_code == 409, migrated.text
        # Refused before anything was generated: no dataset is rendered for an
        # era the ledger would not accept a score for.
        assert generator.calls == 0
        async with session_maker() as session:
            source = await session.get(BenchmarkDataset, (agent_id, _SOURCE_VERSION))
            target = await session.get(BenchmarkDataset, (agent_id, _TARGET_VERSION))
            assert source is not None and source.sha256 == "aa" * 32
            assert target is None


# --- Artifact --------------------------------------------------------------


class TestArtifactFetchAuditTrail:
    """Screener and admin source reads must be attributable."""

    async def test_screener_fetch_writes_an_audit_row(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]

        response = await client.get(
            f"/api/v1/screener/agent/{agent_id}/artifact",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id, "instance_id": "screener-fleet-abc1"},
        )

        assert response.status_code == 200
        async with session_maker() as s:
            rows = (await s.scalars(select(ArtifactFetchAudit))).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.agent_id == agent_id
        assert row.endpoint == "screener.agent_artifact"
        assert row.requester_kind == "screener"
        assert row.lease_id == UUID(attempt_id)
        assert row.artifact_sha256 == _SHA256
        # The fleet shares one hotkey, so this is the column that says which
        # worker actually took the source.
        assert row.requester_instance_id == "screener-fleet-abc1"

    async def test_screener_fetch_without_instance_id_still_audits(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A screener that has not been updated yet is still served and recorded.

        instance_id is additive: until ditto-screener sends it, the row lands
        with hotkey-only attribution rather than not landing at all.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]

        response = await client.get(
            f"/api/v1/screener/agent/{agent_id}/artifact",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id},
        )

        assert response.status_code == 200
        async with session_maker() as s:
            rows = (await s.scalars(select(ArtifactFetchAudit))).all()
        assert len(rows) == 1
        assert rows[0].requester_instance_id is None
        assert rows[0].requester_id is not None

    async def test_admin_artifact_fetch_writes_an_audit_row(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        _install_db(app, session_maker)
        _install_storage(app)

        response = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/artifact",
            headers={
                "Authorization": "Bearer test-admin-token-at-least-32-characters",
                "X-Admin-Actor": "backroom:test-user",
            },
        )

        assert response.status_code == 200
        async with session_maker() as s:
            rows = (await s.scalars(select(ArtifactFetchAudit))).all()
        assert len(rows) == 1
        assert rows[0].endpoint == "admin.get_screening_artifact"
        assert rows[0].requester_kind == "admin"
        assert rows[0].requester_id == "backroom:test-user"


class TestArtifact:
    async def test_returns_presigned_url_and_sha(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]

        response = await client.get(
            f"/api/v1/screener/agent/{agent_id}/artifact",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == str(agent_id)
        assert body["sha256"] == _SHA256
        assert body["download_url"].startswith("https://")
        assert (
            storage.presigned_get_url.await_args.kwargs["key"]
            == f"{agent_id}/agent.tar.gz"
        )

    async def test_a_never_disclose_policy_still_serves_the_screener(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Under `never`, submissions are screened exactly as before.

        A deliberate policy choice, pinned here so nobody later "fixes" it into
        a leak-proof-looking gate. `disclosure = never` withholds source from
        the **public** release path. Extending it to the screener would mean no
        submission could ever be screened, therefore never scored, and the
        subnet would stop -- which is not a privacy policy, it is an outage.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        async with session_maker() as session, session.begin():
            head = await session.scalar(
                select(func.max(ArtifactReleaseSettingsRevision.revision))
            )
            session.add(
                ArtifactReleaseSettingsRevision(
                    parent_revision=head or 0,
                    disclosure="never",
                    embargo_hours=48,
                    reason="Subnet policy: submitted source is not published",
                    actor="operator@example.com",
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]

        response = await client.get(
            f"/api/v1/screener/agent/{agent_id}/artifact",
            headers=_AUTH_HEADER,
            params={"attempt_id": attempt_id},
        )
        assert response.status_code == 200
        assert response.json()["agent_id"] == str(agent_id)
        assert (
            storage.presigned_get_url.await_args.kwargs["key"]
            == f"{agent_id}/agent.tar.gz"
        )

    async def test_active_claim_without_attempt_query_still_allows_download(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        assert claim.status_code == 200

        response = await client.get(
            f"/api/v1/screener/agent/{agent_id}/artifact", headers=_AUTH_HEADER
        )
        assert response.status_code == 200
        assert response.json()["agent_id"] == str(agent_id)

    async def test_without_active_attempt_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        storage = _install_storage(app)

        response = await client.get(
            f"/api/v1/screener/agent/{agent_id}/artifact", headers=_AUTH_HEADER
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE
        storage.presigned_get_url.assert_not_awaited()

    async def test_wrong_attempt_id_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_storage(app)
        await client.post(_CLAIM_URL, headers=_AUTH_HEADER)

        response = await client.get(
            f"/api/v1/screener/agent/{agent_id}/artifact",
            headers=_AUTH_HEADER,
            params={"attempt_id": str(uuid4())},
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE

    async def test_expired_attempt_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        storage = _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        async with session_maker() as session, session.begin():
            attempt = await session.get(ScreeningAttempt, attempt_id)
            assert attempt is not None
            attempt.started_at = datetime.now(UTC) - timedelta(minutes=2)
            attempt.deadline = datetime.now(UTC) - timedelta(minutes=1)

        response = await client.get(
            f"/api/v1/screener/agent/{agent_id}/artifact",
            headers=_AUTH_HEADER,
            params={"attempt_id": str(attempt_id)},
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE
        storage.presigned_get_url.assert_not_awaited()

    async def test_unknown_agent_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        response = await client.get(
            f"/api/v1/screener/agent/{uuid4()}/artifact", headers=_AUTH_HEADER
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_FOUND


class TestScreenedImageUpload:
    async def test_active_attempt_mints_metadata_bound_upload(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        storage = _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/screened-image-upload",
            headers=_AUTH_HEADER,
            json={
                "attempt_id": attempt_id,
                "sha256": "12" * 32,
                "size_bytes": 123,
                "image_id": "sha256:" + "34" * 32,
                "image_ref": f"ditto-screen/{agent_id}:latest",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["storage_upload_id"] == "storage-upload-1"
        assert body["part_size_bytes"] == 64 * 1024**2
        assert storage.create_multipart_upload.await_args.kwargs == {
            "key": f"{agent_id}/screened-images/{body['image_upload_id']}.tar",
            "metadata": {
                "sha256": "12" * 32,
                "image-id": "sha256:" + "34" * 32,
                "image-ref": f"ditto-screen/{agent_id}:latest",
                "attempt-id": attempt_id,
                "image-upload-id": body["image_upload_id"],
            },
        }

    async def test_multipart_completion_hashes_full_bytes_before_verification(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        storage = _install_storage(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = claim.json()["items"][0]["attempt_id"]
        metadata = {
            "attempt_id": attempt_id,
            "sha256": "12" * 32,
            "size_bytes": 123,
            "image_id": "sha256:" + "34" * 32,
            "image_ref": f"ditto-screen/{agent_id}:latest",
        }
        initiated = await client.post(
            f"/api/v1/screener/agent/{agent_id}/screened-image-upload",
            headers=_AUTH_HEADER,
            json=metadata,
        )
        upload = initiated.json()
        part = await client.post(
            f"/api/v1/screener/agent/{agent_id}/screened-image-upload/"
            f"{upload['image_upload_id']}/part",
            headers=_AUTH_HEADER,
            json={
                "attempt_id": attempt_id,
                "storage_upload_id": upload["storage_upload_id"],
                "part_number": 1,
                "size_bytes": 123,
            },
        )
        assert part.status_code == 200
        assert part.json()["required_headers"] == {"Content-Length": "123"}
        storage.head_object.side_effect = None
        storage.head_object.return_value = ObjectMetadata(
            size_bytes=123,
            metadata={
                "sha256": "12" * 32,
                "image-id": "sha256:" + "34" * 32,
                "image-ref": f"ditto-screen/{agent_id}:latest",
                "attempt-id": attempt_id,
                "image-upload-id": upload["image_upload_id"],
            },
        )
        completed = await client.post(
            f"/api/v1/screener/agent/{agent_id}/screened-image-upload/"
            f"{upload['image_upload_id']}/complete",
            headers=_AUTH_HEADER,
            json={
                **metadata,
                "storage_upload_id": upload["storage_upload_id"],
                "parts": [{"part_number": 1, "etag": '"etag-1"'}],
            },
        )

        assert completed.status_code == 200, completed.text
        assert completed.json() == {"verified": True}
        storage.complete_multipart_upload.assert_awaited_once()
        storage.verify_object_sha256.assert_awaited_once_with(
            key=f"{agent_id}/screened-images/{upload['image_upload_id']}.tar",
            expected_size_bytes=123,
        )
        async with session_maker() as session:
            row = await session.get(
                ScreenedImageUpload, UUID(upload["image_upload_id"])
            )
            assert row is not None and row.status == "verified"
        reuse = await client.post(
            f"/api/v1/screener/agent/{agent_id}/screened-image-upload/"
            f"{upload['image_upload_id']}/part",
            json={
                "attempt_id": attempt_id,
                "storage_upload_id": upload["storage_upload_id"],
                "part_number": 1,
                "size_bytes": 123,
            },
        )
        assert reuse.status_code == 409

    async def test_mint_rejects_wrong_agent_ref_and_expired_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        first = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        second = await _seed_agent(
            session_maker,
            status=AgentStatus.UPLOADED,
            created_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        _install_db(app, session_maker)
        storage = _install_storage(app)
        attempt_id = (await client.post(_CLAIM_URL)).json()["items"][0]["attempt_id"]
        base = {
            "attempt_id": attempt_id,
            "sha256": "12" * 32,
            "size_bytes": 123,
            "image_id": "sha256:" + "34" * 32,
        }

        wrong_owner = await client.post(
            f"/api/v1/screener/agent/{second}/screened-image-upload",
            json={**base, "image_ref": f"ditto-screen/{second}:latest"},
        )
        wrong_ref = await client.post(
            f"/api/v1/screener/agent/{first}/screened-image-upload",
            json={**base, "image_ref": f"ditto-screen/{second}:latest"},
        )
        async with session_maker() as session, session.begin():
            attempt = await session.get(
                ScreeningAttempt, UUID(attempt_id), with_for_update=True
            )
            assert attempt is not None
            attempt.started_at = datetime.now(UTC) - timedelta(seconds=2)
            attempt.deadline = datetime.now(UTC) - timedelta(seconds=1)
        expired = await client.post(
            f"/api/v1/screener/agent/{first}/screened-image-upload",
            json={**base, "image_ref": f"ditto-screen/{first}:latest"},
        )

        assert wrong_owner.status_code == 409
        assert wrong_ref.status_code == 409
        assert expired.status_code == 409
        storage.create_multipart_upload.assert_not_awaited()

    async def test_tampered_multipart_is_deleted_and_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        storage = _install_storage(app)
        attempt_id = (await client.post(_CLAIM_URL)).json()["items"][0]["attempt_id"]
        metadata = {
            "attempt_id": attempt_id,
            "sha256": "12" * 32,
            "size_bytes": 123,
            "image_id": "sha256:" + "34" * 32,
            "image_ref": f"ditto-screen/{agent_id}:latest",
        }
        upload = (
            await client.post(
                f"/api/v1/screener/agent/{agent_id}/screened-image-upload",
                json=metadata,
            )
        ).json()
        storage.head_object.side_effect = None
        storage.head_object.return_value = ObjectMetadata(
            size_bytes=123,
            metadata={
                "sha256": "12" * 32,
                "image-id": "sha256:" + "34" * 32,
                "image-ref": f"ditto-screen/{agent_id}:latest",
                "attempt-id": attempt_id,
                "image-upload-id": upload["image_upload_id"],
            },
        )
        storage.verify_object_sha256.return_value = VerifiedObject(
            size_bytes=123, sha256="ff" * 32
        )

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/screened-image-upload/"
            f"{upload['image_upload_id']}/complete",
            json={
                **metadata,
                "storage_upload_id": upload["storage_upload_id"],
                "parts": [{"part_number": 1, "etag": '"etag"'}],
            },
        )

        assert response.status_code == 409
        storage.delete_object.assert_awaited_once()

    @pytest.mark.parametrize(
        ("field", "bad_value"),
        [
            ("sha256", "ff" * 32),
            ("image-id", "sha256:" + "ff" * 32),
            (
                "image-ref",
                "ditto-screen/00000000-0000-0000-0000-000000000000:latest",
            ),
        ],
    )
    async def test_completion_rejects_storage_metadata_mismatch(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        field: str,
        bad_value: str,
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        storage = _install_storage(app)
        attempt_id = (await client.post(_CLAIM_URL)).json()["items"][0]["attempt_id"]
        declared = {
            "attempt_id": attempt_id,
            "sha256": "12" * 32,
            "size_bytes": 123,
            "image_id": "sha256:" + "34" * 32,
            "image_ref": f"ditto-screen/{agent_id}:latest",
        }
        upload = (
            await client.post(
                f"/api/v1/screener/agent/{agent_id}/screened-image-upload",
                json=declared,
            )
        ).json()
        stored_metadata = {
            "sha256": "12" * 32,
            "image-id": "sha256:" + "34" * 32,
            "image-ref": f"ditto-screen/{agent_id}:latest",
            "attempt-id": attempt_id,
            "image-upload-id": upload["image_upload_id"],
        }
        stored_metadata[field] = bad_value
        storage.head_object.side_effect = None
        storage.head_object.return_value = ObjectMetadata(
            size_bytes=123, metadata=stored_metadata
        )

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/screened-image-upload/"
            f"{upload['image_upload_id']}/complete",
            json={
                **declared,
                "storage_upload_id": upload["storage_upload_id"],
                "parts": [{"part_number": 1, "etag": '"etag"'}],
            },
        )

        assert response.status_code == 409
        storage.delete_object.assert_awaited_once()

    async def test_missing_multipart_upload_is_typed_conflict(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        storage = _install_storage(app)
        attempt_id = (await client.post(_CLAIM_URL)).json()["items"][0]["attempt_id"]
        metadata = {
            "attempt_id": attempt_id,
            "sha256": "12" * 32,
            "size_bytes": 123,
            "image_id": "sha256:" + "34" * 32,
            "image_ref": f"ditto-screen/{agent_id}:latest",
        }
        upload = (
            await client.post(
                f"/api/v1/screener/agent/{agent_id}/screened-image-upload",
                json=metadata,
            )
        ).json()
        storage.complete_multipart_upload.side_effect = ObjectNotFoundError("missing")

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/screened-image-upload/"
            f"{upload['image_upload_id']}/complete",
            json={
                **metadata,
                "storage_upload_id": upload["storage_upload_id"],
                "parts": [{"part_number": 1, "etag": '"etag"'}],
            },
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE

    async def test_signed_pass_verifies_and_persists_uploaded_image(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, attempt_id=attempt_id),
        )

        assert response.status_code == 200, response.text
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.EVALUATING
            assert agent.screened_image_sha256 == "12" * 32
            assert agent.screened_image_size_bytes == 123
            assert agent.screened_image_id == "sha256:" + "34" * 32
            assert agent.screened_image_ref == f"ditto-screen/{agent_id}:latest"

    async def test_signed_pass_rejects_storage_metadata_mismatch(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.head_object.side_effect = None
        storage.head_object.return_value = ObjectMetadata(
            size_bytes=122,
            metadata={
                "sha256": "12" * 32,
                "image-id": "sha256:" + "34" * 32,
                "image-ref": f"ditto-screen/{agent_id}:latest",
            },
        )
        claim = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, attempt_id=attempt_id),
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE


# --- Submit result ---------------------------------------------------------


class TestSubmitResult:
    async def test_legacy_outcome_none_rejects_image_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                policy_version=SCREENING_POLICY_VERSION - 1,
                image_sha256="12" * 32,
                image_size_bytes=123,
                image_id="sha256:" + "34" * 32,
                image_ref=f"ditto-screen/{agent_id}:latest",
                image_upload_id=uuid4(),
            ),
        )

        assert response.status_code == 422

    async def test_legacy_pass_cannot_promote(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _result_payload(agent_id, policy_version=1)
        payload["signature"] = _sign(f"{_SCREENER_HOTKEY}:{agent_id}:True")
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )
        assert response.status_code == 409

    async def test_v2_pass_rescreens_in_place_and_preserves_dataset(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            screening_policy_version=0,
        )
        async with session_maker() as s, s.begin():
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            agent.dataset_seed = 42
            agent.dataset_sha256 = "cd" * 32
            agent.dataset_run_size = "full"
        _install_db(app, session_maker)
        _install_chain(app)
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, attempt_id=attempt_id),
        )
        assert response.status_code == 200
        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.EVALUATING
            assert agent.screening_policy_version == SCREENING_POLICY_VERSION
            assert agent.dataset_seed == 42

    async def test_pass_promotes_to_evaluating(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == AgentStatus.EVALUATING
        assert body["accepted"] is True

        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.EVALUATING

    async def test_deterministic_fail_moves_to_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                detail="build failed: cargo error SECRET_FROM_BUILD",
            ),
        )
        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.REJECTED

        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.screening_reason == "Docker image build failed"
            assert agent.screening_policy_version == 0
            assert "SECRET_FROM_BUILD" not in agent.screening_reason

    async def test_rust_contract_rejection_persists_actionable_reason(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        detail = (
            "error[SCR-RUST-002]: archive contains a duplicate path\n\n"
            "help: package each path exactly once"
        )

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            headers=_AUTH_HEADER,
            json=_result_payload(
                agent_id,
                attempt_id=attempt_id,
                passed=False,
                outcome="deterministic_reject",
                detail=detail,
                reason_code="rust-harness-contract",
            ),
        )

        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.REJECTED
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert agent.screening_reason == (
                "Rust harness contract failed (SCR-RUST-002): archive contains a "
                "duplicate path. Package each path exactly once."
            )

    @pytest.mark.parametrize(
        ("outcome", "detail", "expected"),
        [
            (
                "retryable_infra",
                "build failed: dependency fetch returned 503",
                AgentStatus.SCREENING_FAILED,
            ),
            (
                "deterministic_reject",
                "screener error: deliberately misleading legacy detail",
                AgentStatus.REJECTED,
            ),
        ],
    )
    async def test_typed_failure_outcome_is_authoritative(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        outcome: str,
        detail: str,
        expected: AgentStatus,
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            headers=_AUTH_HEADER,
            json=_result_payload(
                agent_id,
                attempt_id=attempt_id,
                passed=False,
                outcome=outcome,
                detail=detail,
            ),
        )
        assert response.status_code == 200
        assert response.json()["status"] == expected

    async def test_current_pass_recovers_stale_screening_failure(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCREENING_FAILED,
            screening_policy_version=SCREENING_POLICY_VERSION - 1,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )

        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.EVALUATING
        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.screening_policy_version == SCREENING_POLICY_VERSION

    async def test_infrastructure_failure_is_retryable_not_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                detail="screener error: Docker daemon unavailable SECRET",
            ),
        )
        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.SCREENING_FAILED

        retry = await client.post(_CLAIM_URL, headers=_AUTH_HEADER)
        assert retry.status_code == 200
        assert retry.json()["items"][0]["agent_id"] == str(agent_id)

    async def test_model_canary_failure_has_public_safe_reason(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                detail="model canary observed no model call",
            ),
        )
        assert response.status_code == 200
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert (
                agent.screening_reason
                == "Harness did not use the validator model gateway"
            )

    async def test_pass_is_idempotent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        payload = _result_payload(agent_id, passed=True, attempt_id=attempt_id)
        first = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=payload,
        )
        second = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=payload,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == AgentStatus.EVALUATING

    async def test_pass_pins_dataset_when_enabled(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        gen = _FakeGenerator(run_size="full", sha="be" * 32)
        _install_generator(app, gen)
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )
        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.EVALUATING
        assert gen.calls == 1

        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.EVALUATING
            assert agent.dataset_seed is not None and agent.dataset_seed >= 0
            assert agent.dataset_sha256 == "be" * 32
            assert agent.dataset_run_size == "full"
            # The seed is derived from the on-chain block and pinned with its
            # provenance, so anyone can recompute + verify it.
            from ditto.api_server.onchain_seed import derive_seed

            assert agent.dataset_seed_block == _BLOCK.number
            assert agent.dataset_seed_block_hash == _BLOCK.hash
            assert agent.dataset_seed == derive_seed(_BLOCK.hash, agent_id)

    async def test_pass_after_activation_generates_and_persists_the_dataset(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_SOURCE_VERSION,
                    desired_version=_TARGET_VERSION,
                    status="activated",
                    cohort_size=5,
                    created_at=datetime.now(UTC),
                    activated_at=datetime.now(UTC),
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        generator = _FakeGenerator(run_size="full", sha="cd" * 32)
        _install_generator(app, generator)
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )

        assert response.status_code == 200, response.text
        assert generator.bench_versions == [_TARGET_VERSION]
        async with session_maker() as session:
            dataset = await session.get(BenchmarkDataset, (agent_id, _TARGET_VERSION))
            assert dataset is not None
            assert dataset.sha256 == "cd" * 32
            assert dataset.run_size == "full"

    async def test_new_submission_during_rollout_enters_desired_benchmark(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        rollout_started = datetime.now(UTC) - timedelta(minutes=1)
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        # The earlier transition is the one that PUT the fleet on the source
        # era, so its target IS the source era -- retired, and refused by
        # ``benchmark_rollout_desired_floor`` today. It is exactly the row
        # production keeps as its audit trail, so it is seeded the way
        # production came by it: written under the lifted floor, then
        # grandfathered. The open transition beside it needs no such help.
        async with (
            session_maker() as floor_session,
            retired_era_writes_allowed(floor_session),
            session_maker() as session,
            session.begin(),
        ):
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_SOURCE_VERSION - 1,
                    desired_version=_SOURCE_VERSION,
                    status="activated",
                    cohort_size=5,
                    created_at=rollout_started - timedelta(hours=1),
                    activated_at=rollout_started - timedelta(minutes=30),
                )
            )
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_SOURCE_VERSION,
                    desired_version=_TARGET_VERSION,
                    status="collecting",
                    cohort_size=5,
                    created_at=rollout_started,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        generator = _FakeGenerator(run_size="full", sha="cd" * 32)
        _install_generator(app, generator)
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )

        assert response.status_code == 200, response.text
        assert generator.bench_versions == [_TARGET_VERSION]
        async with session_maker() as session:
            target = await session.get(BenchmarkDataset, (agent_id, _TARGET_VERSION))
            source = await session.get(BenchmarkDataset, (agent_id, _SOURCE_VERSION))
            assert target is not None
            assert source is None

        # The persisted activated row is still the source era while this open
        # rollout targets the next one. The completed target-era submission must
        # not be mistaken for a missing source-era backfill and claimed again.
        next_claim = await client.post(_CLAIM_URL)
        assert next_claim.status_code == 200, next_claim.text
        assert next_claim.json()["items"] == []

    async def test_rescreen_after_activation_backfills_the_missing_dataset(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A legacy source-era pin must not strand an active-era agent at 0/3."""
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            screening_policy_version=9,
        )
        now = datetime.now(UTC)
        rollout_id = uuid4()
        async with session_maker() as session, session.begin():
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.dataset_seed = 42
            agent.dataset_sha256 = "ab" * 32
            agent.dataset_run_size = "full"
            agent.dataset_seed_block = 123
            agent.dataset_seed_block_hash = "0x" + "12" * 32
            session.add(
                BenchmarkDataset(
                    agent_id=agent_id,
                    bench_version=_SOURCE_VERSION,
                    seed=42,
                    sha256="ab" * 32,
                    run_size="full",
                    seed_block=123,
                    seed_block_hash="0x" + "12" * 32,
                )
            )
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=_SOURCE_VERSION,
                    desired_version=_TARGET_VERSION,
                    status="activated",
                    cohort_size=5,
                    created_at=now,
                    activated_at=now,
                )
            )
            session.add(
                BenchmarkRolloutMember(
                    rollout_id=rollout_id,
                    agent_id=agent_id,
                    position=1,
                    frozen_miner_hotkey=agent.miner_hotkey,
                    frozen_composite=0.9,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)
        generator = _FakeGenerator(run_size="full", sha="cd" * 32)
        _install_generator(app, generator)
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )

        assert response.status_code == 200, response.text
        assert generator.bench_versions == [_TARGET_VERSION]
        assert generator.seeds == [42]
        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            assert agent.dataset_seed == 42
            assert agent.dataset_sha256 == "ab" * 32
            source = await session.get(BenchmarkDataset, (agent_id, _SOURCE_VERSION))
            target = await session.get(BenchmarkDataset, (agent_id, _TARGET_VERSION))
            assert source is not None and source.sha256 == "ab" * 32
            assert target is not None
            assert target.seed == 42
            assert target.sha256 == "cd" * 32
            assert target.seed_block == 123
            assert target.seed_block_hash == "0x" + "12" * 32

    async def test_seed_falls_back_when_chain_unavailable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A chain outage must not halt submissions: the seed falls back to a local
        # CSPRNG value, with null block provenance flagging it as not chain-derived.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app, block_error=True)
        _install_generator(app, _FakeGenerator(run_size="full", sha="be" * 32))
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )
        assert response.status_code == 200
        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.EVALUATING
            assert agent.dataset_seed is not None and agent.dataset_seed >= 0
            # Fallback provenance: no block reference.
            assert agent.dataset_seed_block is None
            assert agent.dataset_seed_block_hash is None

    async def test_generation_failure_does_not_promote(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_generator(app, _FakeGenerator(fail=True))
        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )
        # Required dataset failed to generate: the verdict must NOT have promoted
        # the agent past its active screening lease (it can be retried).
        assert response.status_code == 500
        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.SCREENING
            assert agent.dataset_seed is None

    async def test_idempotent_repeat_does_not_regenerate(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        gen = _FakeGenerator(sha="ab" * 32)
        _install_generator(app, gen)

        claim = await client.post(_CLAIM_URL)
        attempt_id = UUID(claim.json()["items"][0]["attempt_id"])
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        payload = _result_payload(agent_id, passed=True, attempt_id=attempt_id)

        first = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=payload,
        )
        second = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=payload,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        # The dataset was pinned once; the re-report did not call the generator
        # again (the pre-read guard sees dataset_seed already set).
        assert gen.calls == 1
        async with session_maker() as s:
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.dataset_sha256 == "ab" * 32

    async def test_promotes_from_screening_state(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.SCREENING)
        _install_db(app, session_maker)
        _install_chain(app)
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
                    started_at=now,
                    deadline=now + timedelta(minutes=30),
                )
            )
        await _seed_verified_image_upload(
            session_maker, agent_id=agent_id, attempt_id=attempt_id
        )
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True, attempt_id=attempt_id),
        )
        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.EVALUATING

    async def test_conflicting_verdict_on_promoted_agent_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Agent already promoted; a fail verdict now must not demote it.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=False),
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE

    async def test_verdict_on_scored_agent_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.SCORED)
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, passed=True),
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_SCREENABLE

    async def test_bad_signature_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _result_payload(agent_id)
        payload["signature"] = "ab" * 64  # well-formed but wrong
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_SCREENER_AUTH

    async def test_flipped_verdict_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A pass signed by the screener must not be replayable as a fail: the
        # signature binds the ``passed`` flag, so flipping it 401s.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _result_payload(agent_id, passed=True)
        payload["passed"] = False  # grief attempt: replay the pass sig as a fail
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_SCREENER_AUTH

    async def test_payload_hotkey_must_match_authenticated_hotkey(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        other = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(agent_id, screener_hotkey=other),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_SCREENER_AUTH

    async def test_unknown_agent_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        aid = uuid4()
        response = await client.post(
            f"/api/v1/screener/agent/{aid}/result",
            json=_result_payload(aid, passed=False),
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_FOUND


_ADMIN_HEADERS = {
    "Authorization": "Bearer test-admin-token-at-least-32-characters",
    "X-Admin-Actor": "backroom:test-user",
}


def _review_finding(artifact_sha256: str = _SHA256) -> SourceReviewFinding:
    return SourceReviewFinding(
        artifact_sha256=artifact_sha256,
        prompt_revision="source-review-v2",
        risk_level="high",
        confidence=0.97,
        categories=["benchmark_emulation"],
        evidence=[
            SourceReviewEvidenceItem(
                path="src/main.rs", line=2, category="benchmark_emulation"
            )
        ],
        summary="Deterministic shortcut bypasses the general provider path.",
    )


def _review_evidence(digest: str) -> list[dict[str, object]]:
    return [
        {
            "module_id": "luna-source-review",
            "code": "agentic-source-review-tripwire",
            "summary": "private source analysis selected a behavioral audit",
            "digest": digest,
        }
    ]


def _source_tarball() -> tuple[bytes, str]:

    files = {
        "Cargo.toml": b'[package]\nname="agent"\nversion="0.1.0"\n',
        "src/main.rs": b"fn main() {\n    fast_path();\n}\n",
        "assets/table.bin": b"\xff\xfe\x00binary-table" * 4,
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    body = buffer.getvalue()
    return body, hashlib.sha256(body).hexdigest()


class TestQuarantineReviewContext:
    async def _quarantine(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        finding_model: SourceReviewFinding | None = None,
        shadow: dict | None = None,
        **payload_overrides: object,
    ) -> tuple[UUID, dict]:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        if shadow is not None:
            # Production ordering: the shadow reviewer reports while the lease
            # is still running, before the authoritative verdict lands.
            await self._observe_shadow(
                client, session_maker, agent_id, attempt_id, shadow
            )
        finding = finding_model or _review_finding()
        digest = finding.canonical_digest()
        payload = _result_payload(
            agent_id,
            passed=False,
            attempt_id=attempt_id,
            outcome="quarantine",
            manifest_digest="56" * 32,
            finding_digest=digest,
            reason_code="agentic-source-review-tripwire",
            evidence=_review_evidence(digest),
            finding=finding.model_dump(mode="json"),
        )
        payload.update(payload_overrides)
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=payload
        )
        return agent_id, {"response": response, "finding": finding}

    async def _observe_shadow(
        self,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        agent_id: UUID,
        attempt_id: UUID,
        overrides: dict,
    ) -> dict:
        """Record one L2/L3 observation against a still-running attempt."""
        settings = ScreenerReviewSettings(mode="shadow")
        checksum = _review_settings_checksum(settings)
        async with session_maker() as session, session.begin():
            revision = ScreenerReviewSettingsRevision(
                parent_revision=0,
                scope="ditto-screener-prod",
                settings=settings.model_dump(mode="json"),
                checksum=checksum,
                reason="bounded shadow canary",
                actor="test",
            )
            session.add(revision)
            await session.flush()
            revision_id = revision.revision
        payload = {
            "attempt_id": str(attempt_id),
            "artifact_sha256": _SHA256,
            "settings_revision": revision_id,
            "settings_scope": "ditto-screener-prod",
            "settings_checksum": checksum,
            "disposition": "safe",
            "risk_level": "low",
            "categories": ["none"],
            "finding_digest": None,
            "resolution_basis": "authoritative_model_tool_path",
            "clearance_path": "l3_adjudicated_safe",
            "critic_disposition": "confirm_safe",
            "adjudicator_disposition": "confirm_safe",
            "response_models": ["moonshotai/kimi-k3", "openai/gpt-5.6-sol"],
            "response_providers": ["openrouter", "openrouter"],
            "usage": {
                "input_tokens": 41000,
                "output_tokens": 3100,
                "cached_input_tokens": 26000,
                "reasoning_tokens": 900,
                "estimated_cost_usd": 0.82,
                "reported_cost_usd": 0.79,
            },
            **overrides,
        }
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/shadow-review", json=payload
        )
        assert response.status_code == 200
        return payload

    async def test_review_payloads_are_stored_listed_and_digest_verified(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, ctx = await self._quarantine(app, client, session_maker)
        assert ctx["response"].status_code == 200

        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
        )
        item = listing.json()["items"][0]
        assert item["agent_id"] == str(agent_id)
        assert item["finding_verified"] is True
        assert item["finding"]["risk_level"] == "high"
        assert item["finding"]["summary"] == ctx["finding"].summary
        assert item["finding"]["evidence"] == [
            {"path": "src/main.rs", "line": 2, "category": "benchmark_emulation"}
        ]
        assert [entry["code"] for entry in item["evidence"]] == [
            "agentic-source-review-tripwire"
        ]

    async def test_finding_that_does_not_match_signed_digest_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        tampered = _review_finding().model_dump(mode="json")
        tampered["summary"] = "tampered summary"
        _agent_id, ctx = await self._quarantine(
            app, client, session_maker, finding=tampered
        )
        assert ctx["response"].status_code == 422

    async def test_context_reports_miner_history_and_duplicates(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, ctx = await self._quarantine(app, client, session_maker)
        assert ctx["response"].status_code == 200
        now = datetime.now(UTC)
        other_miner = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        prior_agent = uuid4()
        prior_attempt = uuid4()
        duplicate_agent = uuid4()
        shared_coldkey = "5SharedPaymentOwner"
        async with session_maker() as session, session.begin():
            # An earlier, already-resolved quarantine from the same miner.
            session.add(
                Agent(
                    agent_id=prior_agent,
                    miner_hotkey=_MINER_HOTKEY,
                    name="alpha-agent-v1",
                    sha256="99" * 32,
                    status=AgentStatus.REJECTED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=now - timedelta(days=2),
                )
            )
            session.add_all(
                (
                    EvaluationPayment(
                        block_hash=f"0x{agent_id.hex}",
                        extrinsic_index=0,
                        agent_id=agent_id,
                        miner_hotkey=_MINER_HOTKEY,
                        miner_coldkey=shared_coldkey,
                        amount_rao=1,
                        dest_address="5Destination",
                        timestamp=now,
                    ),
                    EvaluationPayment(
                        block_hash=f"0x{duplicate_agent.hex}",
                        extrinsic_index=0,
                        agent_id=duplicate_agent,
                        miner_hotkey=other_miner,
                        miner_coldkey=shared_coldkey,
                        amount_rao=1,
                        dest_address="5Destination",
                        timestamp=now,
                    ),
                )
            )
            session.add(
                ScreeningAttempt(
                    attempt_id=prior_attempt,
                    agent_id=prior_agent,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    status="quarantined",
                    started_at=now - timedelta(days=2),
                    deadline=now - timedelta(days=2, minutes=-30),
                    finished_at=now - timedelta(days=2),
                )
            )
            session.add(
                ScreeningQuarantine(
                    quarantine_id=uuid4(),
                    agent_id=prior_agent,
                    attempt_id=prior_attempt,
                    screener_hotkey=_SCREENER_HOTKEY,
                    policy_version=SCREENING_POLICY_VERSION,
                    manifest_digest="11" * 32,
                    reason_code="behavioral-oracle-wrong-answer",
                    status="resolved",
                    created_at=now - timedelta(days=2),
                    resolved_at=now - timedelta(days=1),
                    resolved_by="backroom:test-user",
                    resolution="reject",
                    resolution_reason="Static table confirmed",
                )
            )
            # A byte-identical artifact submitted by a different miner.
            session.add(
                Agent(
                    agent_id=duplicate_agent,
                    miner_hotkey=other_miner,
                    name="copycat-agent",
                    sha256=_SHA256,
                    status=AgentStatus.UPLOADED,
                    screening_policy_version=0,
                    created_at=now - timedelta(hours=3),
                )
            )

        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
        )
        quarantine_id = listing.json()["items"][0]["quarantine_id"]
        context = await client.get(
            f"/api/v1/admin/screening-quarantines/{quarantine_id}/context",
            headers=_ADMIN_HEADERS,
        )
        assert context.status_code == 200
        body = context.json()
        assert body["quarantine"]["quarantine_id"] == quarantine_id
        assert body["agent"]["agent_id"] == str(agent_id)
        assert body["agent"]["agent_status"] == AgentStatus.QUARANTINED
        assert [a["status"] for a in body["attempts"]] == ["quarantined"]
        assert body["miner"]["total_submissions"] == 2
        assert body["miner"]["quarantine_count"] == 2
        assert body["miner"]["rejected_count"] == 1
        assert [q["agent_name"] for q in body["miner"]["recent_quarantines"]] == [
            "alpha-agent-v1"
        ]
        # The coldkey behind ``same_owner`` is now named, so a reviewer can see
        # WHY two hotkeys were treated as one owner instead of trusting a flag.
        assert body["agent"]["miner_coldkey"] == "5SharedPaymentOwner"
        assert body["miner"]["miner_coldkeys"] == ["5SharedPaymentOwner"]
        assert body["duplicates"] == [
            {
                "agent_id": str(duplicate_agent),
                "miner_hotkey": other_miner,
                "miner_coldkey": "5SharedPaymentOwner",
                "agent_name": "copycat-agent",
                "agent_status": AgentStatus.UPLOADED,
                "submitted_at": body["duplicates"][0]["submitted_at"],
                "match": "identical_artifact",
                "same_owner": True,
            }
        ]
        # Attribution comes from authoritative SQL aggregates, not the sample.
        assert body["duplicate_summary"] == {
            "total": 1,
            "cross_miner": 1,
            "same_miner": 0,
            "cross_owner": 0,
            "same_owner": 1,
            "sample_truncated": False,
        }

    async def test_context_carries_the_attempt_shadow_review(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The case that motivates surfacing this at all: L1 quarantined on a
        # high-risk finding while the L2/L3 escalation adjudicated it safe.
        agent_id, ctx = await self._quarantine(app, client, session_maker, shadow={})
        assert ctx["response"].status_code == 200

        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
        )
        item = listing.json()["items"][0]
        quarantine_id = item["quarantine_id"]
        context = await client.get(
            f"/api/v1/admin/screening-quarantines/{quarantine_id}/context",
            headers=_ADMIN_HEADERS,
        )
        assert context.status_code == 200
        shadow = context.json()["shadow_review"]
        assert shadow is not None
        # Keyed to this quarantine's own attempt, not merely the agent.
        assert shadow["attempt_id"] == item["attempt_id"]
        assert shadow["agent_id"] == str(agent_id)
        assert shadow["disposition"] == "safe"
        assert shadow["risk_level"] == "low"
        assert shadow["categories"] == ["none"]
        assert shadow["resolution_basis"] == "authoritative_model_tool_path"
        assert shadow["clearance_path"] == "l3_adjudicated_safe"
        assert shadow["critic_disposition"] == "confirm_safe"
        assert shadow["adjudicator_disposition"] == "confirm_safe"
        assert shadow["usage"]["estimated_cost_usd"] == 0.82
        assert shadow["created_at"]
        # Advisory only: the L1 quarantine stands untouched beside it.
        assert context.json()["quarantine"]["finding"]["risk_level"] == "high"
        assert context.json()["agent"]["agent_status"] == AgentStatus.QUARANTINED

        # The same observation reaches the batch fan-out the queue workbench
        # uses, so a reviewer sees it however they opened the case.
        batch = await client.post(
            "/api/v1/admin/screening-quarantines/batch-context",
            headers=_ADMIN_HEADERS,
            json={"quarantine_ids": [quarantine_id]},
        )
        assert batch.status_code == 200
        batched = batch.json()["items"][0]["context"]["shadow_review"]
        assert batched == shadow

    async def test_context_without_a_shadow_review_reports_null(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Shadow mode off, or a quarantine older than the reviewer: the field
        # is absent rather than an error, and every other section still builds.
        _agent_id, ctx = await self._quarantine(app, client, session_maker)
        assert ctx["response"].status_code == 200

        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
        )
        quarantine_id = listing.json()["items"][0]["quarantine_id"]
        context = await client.get(
            f"/api/v1/admin/screening-quarantines/{quarantine_id}/context",
            headers=_ADMIN_HEADERS,
        )
        assert context.status_code == 200
        body = context.json()
        assert body["shadow_review"] is None
        assert body["quarantine"]["quarantine_id"] == quarantine_id

    async def test_batch_context_and_signed_preview_reject_changed_decisions(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, ctx = await self._quarantine(app, client, session_maker)
        assert ctx["response"].status_code == 200
        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
        )
        quarantine = listing.json()["items"][0]
        missing_id = uuid4()

        contexts = await client.post(
            "/api/v1/admin/screening-quarantines/batch-context",
            headers=_ADMIN_HEADERS,
            json={"quarantine_ids": [quarantine["quarantine_id"], str(missing_id)]},
        )
        assert contexts.status_code == 200
        assert contexts.json()["items"][0]["context"]["agent"]["agent_id"] == str(
            agent_id
        )
        assert contexts.json()["items"][1] == {
            "quarantine_id": str(missing_id),
            "context": None,
            "error": "quarantine not found",
        }

        decision = {
            "quarantine_id": quarantine["quarantine_id"],
            "expected_agent_id": quarantine["agent_id"],
            "expected_artifact_sha256": quarantine["artifact_sha256"],
            "resolution": "rescreen",
            "reason": "Run the preserved artifact against the current screening policy",
        }
        preview = await client.post(
            "/api/v1/admin/screening-quarantines/batch-preview",
            headers=_ADMIN_HEADERS,
            json={"decisions": [decision]},
        )
        assert preview.status_code == 200
        assert preview.json()["ready_count"] == 1
        assert preview.json()["items"][0]["resulting_agent_status"] == (
            AgentStatus.SCREENING_FAILED
        )

        changed = {**decision, "resolution": "reject"}
        execute = await client.post(
            "/api/v1/admin/screening-quarantines/batch-resolve",
            headers=_ADMIN_HEADERS,
            json={
                "decisions": [changed],
                "preview_token": preview.json()["preview_token"],
                "confirmed": True,
            },
        )
        assert execute.status_code == 409
        assert execute.json()["message"] == (
            "batch decisions changed after preview; preview again"
        )

    async def test_batch_execute_is_idempotent_and_reports_partial_failures(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        first_agent, first_ctx = await self._quarantine(app, client, session_maker)
        second_agent, second_ctx = await self._quarantine(app, client, session_maker)
        assert (
            first_ctx["response"].status_code
            == second_ctx["response"].status_code
            == 200
        )
        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
        )
        by_agent = {item["agent_id"]: item for item in listing.json()["items"]}
        decisions = [
            {
                "quarantine_id": by_agent[str(agent_id)]["quarantine_id"],
                "expected_agent_id": str(agent_id),
                "expected_artifact_sha256": by_agent[str(agent_id)]["artifact_sha256"],
                "resolution": "rescreen",
                "reason": (
                    f"Batch review requested a current-policy rescreen for {agent_id}"
                ),
            }
            for agent_id in (first_agent, second_agent)
        ]
        preview = await client.post(
            "/api/v1/admin/screening-quarantines/batch-preview",
            headers=_ADMIN_HEADERS,
            json={"decisions": decisions},
        )
        assert preview.status_code == 200
        assert preview.json()["ready_count"] == 2

        # Simulate another operator changing one row after this batch preview.
        changed = await client.post(
            f"/api/v1/admin/screening-quarantines/{decisions[1]['quarantine_id']}/resolve",
            headers={**_ADMIN_HEADERS, "X-Admin-Actor": "backroom:other-user"},
            json={"resolution": "reject", "reason": "Independent review rejected it"},
        )
        assert changed.status_code == 200

        request = {
            "decisions": decisions,
            "preview_token": preview.json()["preview_token"],
            "confirmed": True,
        }
        executed = await client.post(
            "/api/v1/admin/screening-quarantines/batch-resolve",
            headers=_ADMIN_HEADERS,
            json=request,
        )
        replay = await client.post(
            "/api/v1/admin/screening-quarantines/batch-resolve",
            headers=_ADMIN_HEADERS,
            json=request,
        )
        assert executed.status_code == replay.status_code == 200
        assert (executed.json()["applied_count"], executed.json()["failed_count"]) == (
            1,
            1,
        )
        assert (
            replay.json()["already_applied_count"],
            replay.json()["failed_count"],
        ) == (1, 1)

        async with session_maker() as session:
            events = (
                await session.scalars(
                    select(ScreeningQuarantineResolution).where(
                        ScreeningQuarantineResolution.quarantine_id
                        == UUID(decisions[0]["quarantine_id"])
                    )
                )
            ).all()
            assert [(event.actor, event.resolution) for event in events] == [
                ("backroom:test-user", "rescreen")
            ]

    async def test_finding_for_a_different_artifact_is_not_verified(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A digest-consistent finding about ANOTHER artifact must not verify."""
        foreign = _review_finding(artifact_sha256="ee" * 32)
        agent_id, ctx = await self._quarantine(
            app, client, session_maker, finding_model=foreign
        )
        assert ctx["response"].status_code == 200
        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
        )
        item = listing.json()["items"][0]
        assert item["agent_id"] == str(agent_id)
        assert item["finding_verified"] is False
        assert item["finding"]["artifact_sha256"] == "ee" * 32

    async def test_idempotent_replay_backfills_missing_review_payloads(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A retry can restore payloads the first report did not carry."""
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        finding = _review_finding()
        digest = finding.canonical_digest()
        bare = _result_payload(
            agent_id,
            passed=False,
            attempt_id=attempt_id,
            outcome="quarantine",
            manifest_digest="56" * 32,
            finding_digest=digest,
            reason_code="agentic-source-review-tripwire",
        )
        first = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=bare
        )
        assert first.status_code == 200

        enriched = dict(bare)
        enriched["evidence"] = _review_evidence(digest)
        enriched["finding"] = finding.model_dump(mode="json")
        replay = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result", json=enriched
        )
        assert replay.status_code == 200

        listing = await client.get(
            "/api/v1/admin/screening-quarantines", headers=_ADMIN_HEADERS
        )
        item = listing.json()["items"][0]
        assert item["finding_verified"] is True
        assert item["finding"]["summary"] == finding.summary
        assert [entry["code"] for entry in item["evidence"]] == [
            "agentic-source-review-tripwire"
        ]

    async def test_retryable_infra_tells_the_miner_a_retry_is_scheduled(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A retried attempt must not read as a hard subnet failure.

        A retryable outcome is re-queued and normally passes on the next
        attempt. Reporting it as a bare "Screening infrastructure error" made
        a self-healing interruption look like the subnet was broken.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])

        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                attempt_id=attempt_id,
                outcome="retryable_infra",
                reason_code="source-review-model-response-invalid",
            ),
        )

        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.SCREENING_FAILED
        async with session_maker() as session:
            refreshed = await session.get(Agent, agent_id)
            assert refreshed is not None
            # The status and the retry are unchanged; only the sentence is.
            assert refreshed.status == AgentStatus.SCREENING_FAILED
            assert refreshed.screening_reason == (
                "Screening was interrupted; retry scheduled"
            )
            attempt = await session.get(ScreeningAttempt, attempt_id)
            assert attempt is not None
            assert attempt.reason_code == "source-review-model-response-invalid"

    async def test_inconclusive_finishes_attempt_and_preserves_lease_backoff(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        claimed = await client.post(_CLAIM_URL)
        attempt_id = UUID(claimed.json()["items"][0]["attempt_id"])
        response = await client.post(
            f"/api/v1/screener/agent/{agent_id}/result",
            json=_result_payload(
                agent_id,
                passed=False,
                attempt_id=attempt_id,
                outcome="inconclusive",
                reason_code="behavioral-oracle-inconclusive",
            ),
        )
        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.SCREENING_FAILED
        async with session_maker() as session:
            refreshed = await session.get(Agent, agent_id)
            assert refreshed is not None
            assert refreshed.status == AgentStatus.SCREENING_FAILED
            assert refreshed.screening_reason == (
                "Screening was inconclusive; retry scheduled"
            )
            attempt = await session.get(ScreeningAttempt, attempt_id)
            assert attempt is not None
            assert attempt.status == "expired"
            assert attempt.finished_at is not None
            assert attempt.deadline > attempt.finished_at
            assert attempt.reason_code == "behavioral-oracle-inconclusive"

        # Completing the attempt must not hot-loop the ambiguous submission.
        blocked = await client.post(_CLAIM_URL)
        assert blocked.status_code == 200
        assert blocked.json()["items"] == []

        # Once the original lease deadline passes, the ordinary bounded retry
        # path becomes eligible again.
        async with session_maker() as session, session.begin():
            attempt = await session.get(ScreeningAttempt, attempt_id)
            assert attempt is not None
            attempt.deadline = attempt.started_at
        retry = await client.post(_CLAIM_URL)
        assert retry.status_code == 200
        assert retry.json()["items"][0]["agent_id"] == str(agent_id)

    async def test_missing_context_is_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        _install_db(app, session_maker)
        response = await client.get(
            f"/api/v1/admin/screening-quarantines/{uuid4()}/context",
            headers=_ADMIN_HEADERS,
        )
        assert response.status_code == 404


class TestQuarantineSourceInspection:
    async def _seed_with_tarball(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> tuple[UUID, MagicMock]:
        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        body, sha256 = _source_tarball()
        agent_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=_MINER_HOTKEY,
                    name="alpha-agent",
                    sha256=sha256,
                    status=AgentStatus.QUARANTINED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=datetime.now(UTC),
                )
            )
        _install_db(app, session_maker)
        storage = _install_storage(app)
        storage.get_object = AsyncMock(return_value=body)
        return agent_id, storage

    async def test_listing_surfaces_files_and_opaque_blobs(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, storage = await self._seed_with_tarball(app, session_maker)
        response = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-files",
            headers=_ADMIN_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["file_count"] == 3
        assert {entry["path"] for entry in body["files"]} == {
            "Cargo.toml",
            "src/main.rs",
            "assets/table.bin",
        }
        assert body["opaque_blobs"] == [
            {
                "path": "assets/table.bin",
                "bytes": body["opaque_blobs"][0]["bytes"],
                "reason": "non_utf8",
            }
        ]
        storage.get_object.assert_awaited_once()

    async def test_source_reads_are_audited_per_file(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Reading source through the operator console is a source fetch too.

        The listing and the excerpt both decrypt real miner code to a human, so
        both leave a row -- and the excerpt records which path was opened, which
        is what makes an operator read reconstructable after the fact.
        """
        agent_id, _storage = await self._seed_with_tarball(app, session_maker)

        listing = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-files",
            headers=_ADMIN_HEADERS,
        )
        excerpt = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-file",
            params={"path": "src/main.rs", "start_line": 1, "end_line": 999},
            headers=_ADMIN_HEADERS,
        )

        assert listing.status_code == 200
        assert excerpt.status_code == 200
        async with session_maker() as s:
            rows = (
                await s.scalars(
                    select(ArtifactFetchAudit).order_by(ArtifactFetchAudit.seq)
                )
            ).all()
        assert [row.endpoint for row in rows] == [
            "admin.list_screening_source_files",
            "admin.read_screening_source_file",
        ]
        assert all(row.agent_id == agent_id for row in rows)
        assert all(row.requester_kind == "admin" for row in rows)
        assert (rows[1].detail or {}).get("path") == "src/main.rs"

    async def test_excerpt_reads_bounded_flagged_lines(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, _storage = await self._seed_with_tarball(app, session_maker)
        response = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-file",
            params={"path": "src/main.rs", "start_line": 1, "end_line": 999},
            headers=_ADMIN_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["path"] == "src/main.rs"
        assert body["total_lines"] == 3
        assert body["lines"][1] == {"line": 2, "text": "    fast_path();"}

        missing = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-file",
            params={"path": "src/nope.rs"},
            headers=_ADMIN_HEADERS,
        )
        assert missing.status_code == 404

        binary = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-file",
            params={"path": "assets/table.bin"},
            headers=_ADMIN_HEADERS,
        )
        assert binary.status_code == 422

    async def test_source_search_locates_code_across_the_whole_artifact(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """One request answers "where", which the manifest and excerpt cannot.

        The excerpt reader is capped at 400 lines and needs a line number the
        operator does not have yet, so locating a construction in a real
        10,000-line ``baseline.rs`` used to mean bisecting with blind reads.
        """
        agent_id, _storage = await self._seed_with_tarball(app, session_maker)
        response = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-search",
            params={"pattern": "fast_path", "context": 1},
            headers=_ADMIN_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["artifact_sha256"]
        assert body["match_count"] == 1
        assert body["has_more"] is False
        assert body["truncated"] is False
        assert body["matches"] == [
            {
                "path": "src/main.rs",
                "line": 2,
                "text": "    fast_path();",
                "context_before": [{"line": 1, "text": "fn main() {"}],
                "context_after": [{"line": 3, "text": "}"}],
            }
        ]
        # The binary member is never searched; its count travels with the answer.
        assert body["opaque_skipped"] == 1
        assert body["files_searched"] == 2

    async def test_source_search_is_audited_and_records_the_pattern(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A search reads miner source, so it leaves the same trail a read does."""
        agent_id, _storage = await self._seed_with_tarball(app, session_maker)
        response = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-search",
            params={"pattern": "fast_path"},
            headers=_ADMIN_HEADERS,
        )
        assert response.status_code == 200
        async with session_maker() as s:
            rows = (
                await s.scalars(
                    select(ArtifactFetchAudit).order_by(ArtifactFetchAudit.seq)
                )
            ).all()
        assert [row.endpoint for row in rows] == ["admin.search_screening_source"]
        assert (rows[0].detail or {}).get("pattern") == "fast_path"
        assert rows[0].requester_kind == "admin"

    async def test_source_search_rejects_a_bad_pattern_and_anonymous_callers(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, _storage = await self._seed_with_tarball(app, session_maker)
        broken = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-search",
            params={"pattern": "(unclosed"},
            headers=_ADMIN_HEADERS,
        )
        assert broken.status_code == 422

        headers = dict(_ADMIN_HEADERS)
        headers.pop("X-Admin-Actor")
        anonymous = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-search",
            params={"pattern": "fast_path"},
            headers=headers,
        )
        assert anonymous.status_code == 422

    async def test_source_reads_require_admin_actor_and_matching_digest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, storage = await self._seed_with_tarball(app, session_maker)
        headers = dict(_ADMIN_HEADERS)
        headers.pop("X-Admin-Actor")
        anonymous = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-files",
            headers=headers,
        )
        assert anonymous.status_code == 422

        storage.get_object = AsyncMock(return_value=b"not the stored artifact")
        tampered = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/source-files",
            headers=_ADMIN_HEADERS,
        )
        assert tampered.status_code == 502


def test_shadow_review_accepts_a_full_length_provider_trajectory() -> None:
    """A real L2/L3 escalation reports one provider stage per model call.

    Analyst turns, the critic, and each adjudicator all append a stage, and
    model failover retries add more. Production trajectories were observed
    spanning 9 to 25 stages while this bound was 8, which rejected every
    shadow observation with HTTP 422 and silently discarded the telemetry.
    """
    from ditto.api_models.screener import (
        MAX_SHADOW_PROVIDER_STAGES,
        ShadowReviewObservationRequest,
        ShadowReviewUsage,
    )

    usage = ShadowReviewUsage(
        input_tokens=849180,
        output_tokens=11502,
        cached_input_tokens=665728,
        reasoning_tokens=6253,
        estimated_cost_usd=1.59,
        reported_cost_usd=1.18,
    )
    stages = 25
    assert stages <= MAX_SHADOW_PROVIDER_STAGES

    observation = ShadowReviewObservationRequest(
        attempt_id=uuid4(),
        artifact_sha256="ab" * 32,
        settings_revision=1,
        settings_scope="*",
        settings_checksum="cd" * 32,
        disposition="violation",
        risk_level="high",
        categories=("benchmark_emulation",),
        finding_digest="ef" * 32,
        resolution_basis="benchmark_answer_replacement",
        clearance_path="l3_adjudicated_violation_cause",
        critic_disposition="not_required",
        adjudicator_disposition=None,
        response_models=tuple(["moonshotai/kimi-k3"] * stages),
        response_providers=tuple(["Moonshot AI"] * stages),
        usage=usage,
    )

    assert len(observation.response_models) == stages
    assert len(observation.response_providers) == stages

    with pytest.raises(ValidationError):
        ShadowReviewObservationRequest(
            attempt_id=uuid4(),
            artifact_sha256="ab" * 32,
            settings_revision=1,
            settings_scope="*",
            settings_checksum="cd" * 32,
            disposition="violation",
            risk_level="high",
            response_models=tuple(
                ["moonshotai/kimi-k3"] * (MAX_SHADOW_PROVIDER_STAGES + 1)
            ),
            response_providers=("Moonshot AI",),
            usage=usage,
        )


class TestQuarantineBaselineDiff:
    """The starter-kit subtraction an operator relies on to find real code."""

    async def _seed_kit_derived_agent(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> tuple[UUID, MagicMock]:

        from ditto.api_server.starter_kit import starter_kit_head_text

        app.state.config = replace(
            app.state.config,
            admin_api_token="test-admin-token-at-least-32-characters",
        )
        head = starter_kit_head_text()
        # A realistic submission: verbatim kit files plus the miner's own code.
        files = {
            "Cargo.toml": head["Cargo.toml"].encode(),
            "src/baseline.rs": head["src/baseline.rs"].encode(),
            "src/solver.rs": b"fn solve_as_of() -> u64 {\n    42\n}\n",
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, raw in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        body = buffer.getvalue()
        agent_id = uuid4()
        async with session_maker() as session, session.begin():
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=_MINER_HOTKEY,
                    name="kit-derived-agent",
                    sha256=hashlib.sha256(body).hexdigest(),
                    status=AgentStatus.QUARANTINED,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=datetime.now(UTC),
                )
            )
        _install_db(app, session_maker)
        storage = _install_storage(app)
        storage.get_object = AsyncMock(return_value=body)
        return agent_id, storage

    async def test_manifest_separates_stock_kit_from_miner_code(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, _storage = await self._seed_kit_derived_agent(app, session_maker)
        response = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/baseline-diff",
            headers=_ADMIN_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        by_path = {entry["path"]: entry for entry in body["files"]}

        # The two verbatim kit files are stock; only solver.rs is the miner's.
        assert by_path["Cargo.toml"]["stock_kit"] is True
        assert by_path["src/baseline.rs"]["stock_kit"] is True
        assert by_path["src/solver.rs"]["stock_kit"] is False
        assert by_path["src/solver.rs"]["status"] == "added"
        assert body["custom_file_count"] == 1
        assert body["custom_added_lines"] == 3
        assert body["baseline"]["revision"]
        assert body["baseline"]["source"].endswith("dittobench-starter-kit")
        assert body["path_aligned"] is False

    async def test_file_diff_returns_bounded_body_and_stock_flag(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, _storage = await self._seed_kit_derived_agent(app, session_maker)
        response = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/baseline-diff/file",
            params={"path": "src/solver.rs"},
            headers=_ADMIN_HEADERS,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["stock_kit"] is False
        assert body["reference_present"] is False
        assert any("solve_as_of" in line for line in body["diff_lines"])

        missing = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/baseline-diff/file",
            params={"path": "src/ghost.rs"},
            headers=_ADMIN_HEADERS,
        )
        assert missing.status_code == 404

    async def test_baseline_diff_requires_admin_actor(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id, _storage = await self._seed_kit_derived_agent(app, session_maker)
        headers = dict(_ADMIN_HEADERS)
        headers.pop("X-Admin-Actor")
        response = await client.get(
            f"/api/v1/admin/screening-submissions/{agent_id}/baseline-diff",
            headers=headers,
        )
        assert response.status_code == 422
