"""Unit tests for :mod:`ditto.api_server.endpoints.validator`.

These exercise the real endpoints end to end against an in-memory SQLite
database (real queries, real status transitions) with the chain + storage
dependencies mocked. Signatures are produced with a real sr25519 dev
keypair so the signature-verification path runs for real.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import statistics
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call
from uuid import UUID, uuid4

import bittensor
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.benchmark_capacity import (
    BenchmarkCapacity,
    benchmark_capacity_signing_token,
)
from ditto.api_models.benchmark_progress import (
    BenchmarkProgress,
    benchmark_progress_signing_token,
)
from ditto.api_models.confirmation_progress import (
    ConfirmationProgress,
    confirmation_progress_signing_token,
)
from ditto.api_models.queue_policy_settings import (
    DeferredSourceReviewSettings,
    PrevGenCarryoverSettings,
    QueuePolicySettings,
)
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.stack_health import (
    ValidatorStackHealth,
    validator_stack_health_signing_token,
)
from ditto.api_models.system_health import (
    SystemMetrics,
    system_metrics_signing_token,
)
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus
from ditto.api_models.validator import (
    CONTAINER_LOG_TAIL_MAX_LENGTH,
    FAILURE_DETAIL_MAX_LENGTH,
    LEGACY_FAILURE_DETAIL_MAX_LENGTH,
)
from ditto.api_models.validator_capabilities import (
    InferenceCalibrationRoute,
    ScorerBenchmarkCapability,
    V7InferenceCalibration,
    ValidatorCapabilities,
    ValidatorStackIdentity,
    validator_identity_signing_token,
)
from ditto.api_models.validator_updater import (
    ValidatorUpdaterStatus,
    validator_updater_status_signing_token,
)
from ditto.api_server.config import ValidatorCompatibilityConfig
from ditto.api_server.dependencies import (
    get_chain_client,
    get_dataset_generator,
    get_session,
    get_storage_client,
)
from ditto.api_server.endpoints import validator as validator_endpoint
from ditto.api_server.endpoints.validator import (
    _fresh_submission_lane_due,
    _heartbeat_signing_message,
    _issue_source_backfill_ticket,
)
from ditto.api_server.fingerprint import reference_corpus_provenance
from ditto.api_server.middleware.error_envelope import (
    ERROR_CODE_AGENT_NOT_EVALUATABLE,
    ERROR_CODE_AGENT_NOT_FOUND,
    ERROR_CODE_BENCH_VERSION_RETIRED,
    ERROR_CODE_VALIDATION,
    ERROR_CODE_VALIDATOR_AUTH,
)
from ditto.chain.models import NeuronInfo
from ditto.db.models import (
    Agent,
    ArtifactFetchAudit,
    ArtifactReleaseSettingsRevision,
    AthReview,
    BenchmarkDataset,
    BenchmarkRollout,
    BenchmarkRolloutMember,
    ConfirmationBundle,
    ConfirmationBundleSettingsRevision,
    ConfirmationBundleSubject,
    ConfirmationBundleTicket,
    ConfirmationScore,
    ContinualRetestSettingsRevision,
    InferenceGrant,
    InferenceProviderRoute,
    InferenceRoutingPolicy,
    Score,
    ScoreAuditEntry,
    ScreenerHeartbeat,
    ValidatorHeartbeat,
    ValidatorLeaseAudit,
    ValidatorRequestNonce,
    ValidatorSlotSettingsRevision,
    ValidatorTicket,
)
from ditto.db.queries.artifact_fetch_audit import (
    AUDIT_WRITE_FAILED,
    record_artifact_fetch,
)
from ditto.db.queries.audit import (
    EVENT_COPY_NO_OPPORTUNITY,
    EVENT_SCORE_RETEST_QUEUED,
    append_audit_entry,
)
from ditto.db.queries.benchmark_rollout import MIN_SCOREABLE_BENCH_VERSION
from ditto.db.queries.confirmation_scores import (
    ConfirmationSeedScore,
    append_confirmation_scores,
)
from ditto.db.queries.king_reign import (
    record_first_crowned,
    record_weight_confirmed,
)
from ditto.db.queries.queue_policy_settings import (
    insert_queue_policy_settings_revision,
)
from ditto.db.queries.retry_budget import (
    INFRA_RETRY_BACKOFF_CAP,
    MAX_AGENT_INFRA_RETRY_GRANTS,
    MAX_INFRA_RETRY_GRANTS,
)
from ditto.db.queries.rollout_dispatch import ROLLOUT_DISPATCH_LOCK_KEY
from ditto.db.queries.tickets import issue_confirmation_ticket
from ditto.tests.legacy_era import retired_era_writes_allowed

# Real dev keypairs: sign for real so _verify_signature runs end to end. The k=3
# quorum needs three distinct permitted validators before an agent finalizes.
_KEYPAIRS = [
    bittensor.Keypair.create_from_uri(uri) for uri in ("//Alice", "//Bob", "//Charlie")
]
_KEYPAIR = _KEYPAIRS[0]
_VALIDATOR_HOTKEY = _KEYPAIR.ss58_address
# A fourth validator, used only to prove an expired ticket re-opens a slot for a
# validator that was shut out when the k=3 pool was full.
_DAVE = bittensor.Keypair.create_from_uri("//Dave")
_MINER_HOTKEY = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_SHA256 = "ab" * 32
_TICKET_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)
# The generic "a benchmark version" fixture value. It used to be 2 simply
# because 2 was the oldest number available; nothing in the tests that use it
# is about v2. Since the floor landed, v2-v6 cannot be written at all -- the
# ``scores_bench_version_floor`` CHECK and the ``validator_tickets`` insert
# trigger refuse them -- so the arbitrary value has to sit at or above
# MIN_SCOREABLE_BENCH_VERSION. Tests that need several distinct versions count
# up from here (7/8/9); tests that are genuinely about the retired era say so
# and reach for ``retired_era_writes_allowed``.
_BENCH_VERSION = 7
# The one calibrated route the v7 fixtures agree on. The scorer advertises this
# pair in its heartbeat and ``select_route`` matches the stored route against
# it, so the same literal has to appear on both sides or the grant comes back
# ``None`` and the lane answers 503 with nothing pointing at the mismatch.
_V7_ROUTE_PROFILE = "openrouter-route-test-v1"
_V7_CALIBRATION_MANIFEST = "c" * 64


def test_v4_heartbeat_canonical_vector() -> None:
    """Freeze the cross-repository v4 bytes independently of test helpers."""
    agent_id = UUID("11111111-2222-4333-8444-555555555555")
    progress = BenchmarkProgress(
        stage="running_benchmark",
        completed=51,
        total=114,
        ticket_deadline=_TICKET_DEADLINE,
    )
    actual = _heartbeat_signing_message(
        validator_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        software_version="1.2.3",
        protocol_version=4,
        code_digest="ab" * 32,
        state="running_benchmark",
        active_agent_id=agent_id,
        system_metrics=None,
        benchmark_progress=progress,
        timestamp=1784020800,
    )
    assert actual == (
        b"ditto-validator-heartbeat:v4:"
        b"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY:"
        b"1.2.3:4:"
        b"abababababababababababababababababababababababababababababababab:"
        b"running_benchmark:11111111-2222-4333-8444-555555555555:-:"
        b"running_benchmark,51,114,2030-01-01T00:00:00.000000+00:00:"
        b"1784020800"
    )


@pytest.mark.parametrize(
    ("protocol_version", "domain", "suffix"),
    [
        (1, "v1", "idle:1784020800"),
        (2, "v2", "idle::1784020800"),
        (3, "v3", "idle::-:1784020800"),
        (5, "v4", "idle::-:-:1784020800"),
        (6, "v4", "idle::-:-:1784020800"),
    ],
)
def test_v1_v2_v3_v5_v6_heartbeat_domains_are_frozen(
    protocol_version: int, domain: str, suffix: str
) -> None:
    hotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    digest = "ab" * 32
    actual = _heartbeat_signing_message(
        validator_hotkey=hotkey,
        software_version="1.2.3",
        protocol_version=protocol_version,
        code_digest=digest,
        state="idle",
        timestamp=1784020800,
    )
    assert (
        actual
        == (
            f"ditto-validator-heartbeat:{domain}:{hotkey}:1.2.3:"
            f"{protocol_version}:{digest}:{suffix}"
        ).encode()
    )


def test_v7_heartbeat_matches_shared_cross_repo_vector() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[2] / "contract" / "validator_heartbeat_v7.json"
        ).read_text()
    )
    request = fixture["request"]
    capabilities = ValidatorCapabilities.model_validate(request["capabilities"])
    stack = ValidatorStackIdentity.model_validate(request["stack"])
    actual = _heartbeat_signing_message(
        validator_hotkey=request["validator_hotkey"],
        software_version=request["software_version"],
        protocol_version=request["protocol_version"],
        code_digest=request["code_digest"],
        state=request["state"],
        active_agent_id=request["active_agent_id"],
        system_metrics=request["system_metrics"],
        benchmark_progress=request["benchmark_progress"],
        capabilities=capabilities,
        stack=stack,
        timestamp=request["timestamp"],
    )
    assert actual == fixture["expected_message_utf8"].encode()
    assert actual.hex() == fixture["expected_message_hex"]


def test_v9_heartbeat_matches_shared_cross_repo_vectors() -> None:
    """Both the managed-GHCR and source-Compose v9 vectors verify byte-for-byte."""
    fixtures = json.loads(
        (
            Path(__file__).parents[2] / "contract" / "validator_heartbeat_v9.json"
        ).read_text()
    )
    for name in ("managed", "source"):
        request = fixtures[name]["request"]
        capabilities = ValidatorCapabilities.model_validate_json(
            json.dumps(request["capabilities"])
        )
        stack = ValidatorStackIdentity.model_validate_json(json.dumps(request["stack"]))
        stack_health = ValidatorStackHealth.model_validate_json(
            json.dumps(request["stack_health"])
        )
        actual = _heartbeat_signing_message(
            validator_hotkey=request["validator_hotkey"],
            software_version=request["software_version"],
            protocol_version=request["protocol_version"],
            code_digest=request["code_digest"],
            state=request["state"],
            active_agent_id=request["active_agent_id"],
            system_metrics=request["system_metrics"],
            benchmark_progress=request["benchmark_progress"],
            capabilities=capabilities,
            stack=stack,
            stack_health=stack_health,
            timestamp=request["timestamp"],
        )
        assert actual == fixtures[name]["expected_message_utf8"].encode(), name
        assert (
            hashlib.sha256(actual).hexdigest()
            == fixtures[name]["expected_message_sha256"]
        ), name


def test_optional_scorer_capability_preserves_legacy_v7_token() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[2] / "contract" / "validator_heartbeat_v7.json"
        ).read_text()
    )
    request = fixture["request"]
    capabilities = ValidatorCapabilities.model_validate(request["capabilities"])
    stack = ValidatorStackIdentity.model_validate(request["stack"])

    assert capabilities.scorer_benchmarks is None
    assert (
        validator_identity_signing_token(capabilities, stack)
        in (fixture["expected_message_utf8"])
    )


def test_scorer_benchmark_capability_is_conservative_unless_fresh_verified() -> None:
    legacy = ScorerBenchmarkCapability(
        status="legacy_v2", supported_bench_versions=(2,)
    )
    assert legacy.supported_bench_versions == (2,)
    unavailable = ScorerBenchmarkCapability(
        status="unreachable", supported_bench_versions=()
    )
    assert unavailable.supported_bench_versions == ()

    with pytest.raises(
        ValueError, match="may advertise no work or legacy benchmark v2"
    ):
        ScorerBenchmarkCapability(
            status="identity_mismatch", supported_bench_versions=(2, 3)
        )
    with pytest.raises(ValueError, match="requires observation and identity"):
        ScorerBenchmarkCapability(
            status="fresh_verified", supported_bench_versions=(2, 3)
        )

    verified = ScorerBenchmarkCapability(
        status="fresh_verified",
        supported_bench_versions=(2, 3),
        observed_at=1784020800,
        software_version="1.3.0",
        source_revision="a" * 40,
    )
    assert verified.supported_bench_versions == (2, 3)

    legacy_v7 = ScorerBenchmarkCapability(
        status="fresh_verified",
        supported_bench_versions=(2, 7),
        observed_at=1784020800,
        software_version="1.3.0",
        source_revision="a" * 40,
    )
    assert legacy_v7.v7_calibration is None
    calibrated = ScorerBenchmarkCapability(
        status="fresh_verified",
        supported_bench_versions=(2, 7),
        observed_at=1784020800,
        software_version="1.3.0",
        source_revision="a" * 40,
        v7_calibration=V7InferenceCalibration(
            manifest_sha256="b" * 64,
            supported_routes=(
                InferenceCalibrationRoute(
                    provider="Groq",
                    profile_revision="openrouter-route-groq-v1",
                    model="openai/gpt-oss-20b",
                ),
            ),
        ),
    )
    assert calibrated.v7_calibration is not None

    # Heartbeats arrive as JSON, where tuples are necessarily encoded as arrays.
    # Strict validation must preserve the immutable tuple internally without
    # rejecting the wire representation before the endpoint can authenticate it.
    from_wire = V7InferenceCalibration.model_validate(
        {
            "manifest_sha256": "b" * 64,
            "supported_routes": [
                {
                    "provider": "openrouter",
                    "profile_revision": "openrouter-route-groq-v1",
                    "model": "openai/gpt-oss-20b",
                }
            ],
        }
    )
    assert isinstance(from_wire.supported_routes, tuple)
    assert from_wire.supported_routes[0].provider == "openrouter"

    with pytest.raises(ValueError, match="calibration requires benchmark v7 support"):
        ScorerBenchmarkCapability(
            status="fresh_verified",
            supported_bench_versions=(2, 6),
            observed_at=1784020800,
            software_version="1.3.0",
            source_revision="a" * 40,
            v7_calibration=calibrated.v7_calibration,
        )


def _sign(message: str) -> str:
    return _KEYPAIR.sign(message.encode()).hex()


def _score_payload(
    agent_id: UUID,
    run_id: str = "run_test_1",
    *,
    keypair: bittensor.Keypair = _KEYPAIR,
    **overrides: object,
) -> dict:
    ticket_deadline = overrides.pop("ticket_deadline", _TICKET_DEADLINE)
    assert isinstance(ticket_deadline, datetime)
    report = {
        "run_id": run_id,
        "seed": 8675309,
        "composite": 0.82,
        "tool_mean": 0.88,
        "memory_mean": 0.73,
        "median_ms": 812,
        "n": 30,
        "generated_at": "2026-06-08T12:04:30Z",
        "per_case": [],
        # Bound explicitly, because a version-less report is no longer a
        # generic one. ``report.bench_version or LEGACY_BENCH_VERSION`` reads
        # an omitted version as v2, and v2 is beneath the scoreable floor, so
        # the endpoint now answers 410 before it ever looks for a lease. Pass
        # ``bench_version=None`` to get the legacy-shaped payload back for the
        # handful of tests that are about that path.
        "bench_version": _BENCH_VERSION,
    }
    report.update(overrides)
    hotkey = keypair.ss58_address
    lease = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    signed = (
        f"{hotkey}:{agent_id}:{lease}:{run_id}:{report['composite']!r}:{report['seed']}"
    )
    # CANONICAL ORDER, mirroring _score_signing_message and ditto-subnet:
    #   base : bench_version? : transcript_sha256? : base_evidence_sha256(v9)?
    if report.get("bench_version") is not None:
        signed += f":{report['bench_version']}"
    details = report.get("details")
    transcript = details.get("transcript_sha256") if isinstance(details, dict) else None
    if isinstance(transcript, str) and transcript:
        signed += f":{transcript}"
    base_evidence = report.get("base_evidence_sha256")
    if isinstance(base_evidence, str) and base_evidence:
        signed += f":{base_evidence}"
    return {
        "validator_hotkey": hotkey,
        "ticket_deadline": ticket_deadline.isoformat(),
        "signature": keypair.sign(signed.encode()).hex(),
        "report": report,
    }


def _v9_score_overrides() -> dict[str, Any]:
    vector_path = (
        Path(__file__).resolve().parents[6]
        / "services/dittobench-api/testdata/v9_base_contract_vectors.json"
    )
    vector = json.loads(vector_path.read_text())["vectors"][0]
    evidence = vector["details"]
    return {
        "bench_version": 9,
        "base_evidence_sha256": vector["base_evidence_sha256"],
        "composite": evidence["effective_composite_micros"] / 1_000_000,
        "composite_stderr": evidence["effective_stderr_micros"] / 1_000_000,
        "n": evidence["score_gates"]["model_use"]["administered_cases"],
        "details": {
            "dataset_sha256": evidence["dataset_sha256"],
            "transcript_sha256": evidence["transcript_sha256"],
            "v9_base": evidence,
        },
    }


def _job_payload(
    keypair: bittensor.Keypair = _KEYPAIR,
    *,
    nonce: UUID | None = None,
    requested_at: datetime | None = None,
    slot_id: str | None = None,
) -> dict:
    nonce = nonce or uuid4()
    requested_at = requested_at or datetime.now(UTC)
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    signed = (
        f"validator-job:{keypair.ss58_address}:{nonce}:{requested}"
        if slot_id is None
        else f"validator-job:v2:{keypair.ss58_address}:{slot_id}:{nonce}:{requested}"
    ).encode()
    payload = {
        "validator_hotkey": keypair.ss58_address,
        "nonce": str(nonce),
        "requested_at": requested_at.isoformat(),
        "signature": keypair.sign(signed).hex(),
    }
    if slot_id is not None:
        payload["slot_id"] = slot_id
    return payload


def _job_fail_payload(
    agent_id: UUID,
    keypair: bittensor.Keypair = _KEYPAIR,
    *,
    nonce: UUID | None = None,
    requested_at: datetime | None = None,
    ticket_deadline: datetime = _TICKET_DEADLINE,
    reason: str = "infrastructure",
    failure_detail: str | None = None,
    container_log_tail: str | None = None,
) -> dict:
    nonce = nonce or uuid4()
    requested_at = requested_at or datetime.now(UTC)
    deadline = ticket_deadline.astimezone(UTC).isoformat(timespec="microseconds")
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    # failure_detail is deliberately absent from the signed payload, exactly as
    # `reason` is: neither authorizes anything, and signing it would have made
    # the field a protocol break instead of an additive one.
    signed = (
        f"validator-job-fail:v1:{keypair.ss58_address}:{agent_id}:{deadline}:"
        f"{nonce}:{requested}"
    ).encode()
    payload = {
        "validator_hotkey": keypair.ss58_address,
        "agent_id": str(agent_id),
        "ticket_deadline": ticket_deadline.isoformat(),
        "reason": reason,
        "nonce": str(nonce),
        "requested_at": requested_at.isoformat(),
        "signature": keypair.sign(signed).hex(),
    }
    # Omitted entirely when None, so the default shape is byte-identical to what
    # a validator predating the field sends.
    if failure_detail is not None:
        payload["failure_detail"] = failure_detail
    if container_log_tail is not None:
        payload["container_log_tail"] = container_log_tail
    return payload


def _artifact_headers(
    agent_id: UUID,
    keypair: bittensor.Keypair = _KEYPAIR,
    *,
    nonce: UUID | None = None,
    requested_at: datetime | None = None,
) -> dict[str, str]:
    nonce = nonce or uuid4()
    requested_at = requested_at or datetime.now(UTC)
    requested = requested_at.astimezone(UTC).isoformat(timespec="microseconds")
    signed = (
        f"validator-artifact:v1:{keypair.ss58_address}:{agent_id}:{nonce}:{requested}"
    ).encode()
    return {
        "X-Validator-Hotkey": keypair.ss58_address,
        "X-Validator-Artifact-Nonce": str(nonce),
        "X-Validator-Artifact-Requested-At": requested_at.isoformat(),
        "X-Validator-Artifact-Signature": keypair.sign(signed).hex(),
    }


def _heartbeat_payload(
    *,
    keypair: bittensor.Keypair = _KEYPAIR,
    timestamp: int | None = None,
    code_digest: str = "ab" * 32,
    state: str = "idle",
    protocol_version: int = 1,
    active_agent_id: UUID | None = None,
    system_metrics: dict[str, object] | None = None,
    benchmark_progress: dict[str, object] | None = None,
    capabilities: dict[str, object] | None = None,
    stack: dict[str, object] | None = None,
    stack_health: dict[str, object] | None = None,
    benchmark_capacity: dict[str, object] | None = None,
    confirmation_progress: list[dict[str, object]] | None = None,
    updater_status: dict[str, object] | None = None,
) -> dict[str, object]:
    ts = timestamp if timestamp is not None else int(datetime.now(UTC).timestamp())
    hotkey = keypair.ss58_address
    if protocol_version >= 7:
        metrics = (
            SystemMetrics.model_validate(system_metrics)
            if system_metrics is not None
            else None
        )
        progress = (
            BenchmarkProgress.model_validate_json(json.dumps(benchmark_progress))
            if benchmark_progress is not None
            else None
        )
        typed_capabilities = ValidatorCapabilities.model_validate_json(
            json.dumps(capabilities)
        )
        typed_stack = ValidatorStackIdentity.model_validate_json(json.dumps(stack))
        identity_token = validator_identity_signing_token(
            typed_capabilities, typed_stack
        )
        if protocol_version >= 10:
            typed_health = ValidatorStackHealth.model_validate_json(
                json.dumps(stack_health)
            )
            typed_capacity = BenchmarkCapacity.model_validate_json(
                json.dumps(benchmark_capacity)
            )
            if protocol_version >= 22:
                typed_confirmation = [
                    ConfirmationProgress.model_validate(item)
                    for item in (confirmation_progress or [])
                ]
                if protocol_version >= 23:
                    typed_updater = ValidatorUpdaterStatus.model_validate(
                        updater_status
                    )
                    message = (
                        f"ditto-validator-heartbeat:v23:{hotkey}:0.1.0:"
                        f"{protocol_version}:{code_digest}:{state}:"
                        f"{active_agent_id or ''}:"
                        f"{system_metrics_signing_token(metrics)}:"
                        f"{benchmark_progress_signing_token(progress)}:"
                        f"{identity_token}:"
                        f"{validator_stack_health_signing_token(typed_health)}:"
                        f"{benchmark_capacity_signing_token(typed_capacity)}:"
                        f"{confirmation_progress_signing_token(typed_confirmation)}:"
                        f"{validator_updater_status_signing_token(typed_updater)}:{ts}"
                    )
                else:
                    message = (
                        f"ditto-validator-heartbeat:v22:{hotkey}:0.1.0:"
                        f"{protocol_version}:{code_digest}:{state}:"
                        f"{active_agent_id or ''}:"
                        f"{system_metrics_signing_token(metrics)}:"
                        f"{benchmark_progress_signing_token(progress)}:"
                        f"{identity_token}:"
                        f"{validator_stack_health_signing_token(typed_health)}:"
                        f"{benchmark_capacity_signing_token(typed_capacity)}:"
                        f"{confirmation_progress_signing_token(typed_confirmation)}:"
                        f"{ts}"
                    )
            else:
                domain = "v11" if protocol_version >= 11 else "v10"
                message = (
                    f"ditto-validator-heartbeat:{domain}:{hotkey}:0.1.0:"
                    f"{protocol_version}:{code_digest}:{state}:"
                    f"{active_agent_id or ''}:"
                    f"{system_metrics_signing_token(metrics)}:"
                    f"{benchmark_progress_signing_token(progress)}:"
                    f"{identity_token}:"
                    f"{validator_stack_health_signing_token(typed_health)}:"
                    f"{benchmark_capacity_signing_token(typed_capacity)}:{ts}"
                )
        elif protocol_version >= 9:
            typed_health = ValidatorStackHealth.model_validate_json(
                json.dumps(stack_health)
            )
            message = (
                f"ditto-validator-heartbeat:v9:{hotkey}:0.1.0:{protocol_version}:"
                f"{code_digest}:{state}:{active_agent_id or ''}:"
                f"{system_metrics_signing_token(metrics)}:"
                f"{benchmark_progress_signing_token(progress)}:"
                f"{identity_token}:"
                f"{validator_stack_health_signing_token(typed_health)}:{ts}"
            )
        else:
            domain = "v8" if protocol_version >= 8 else "v7"
            message = (
                f"ditto-validator-heartbeat:{domain}:{hotkey}:0.1.0:"
                f"{protocol_version}:"
                f"{code_digest}:{state}:{active_agent_id or ''}:"
                f"{system_metrics_signing_token(metrics)}:"
                f"{benchmark_progress_signing_token(progress)}:"
                f"{identity_token}:{ts}"
            )
    elif protocol_version >= 4:
        metrics = (
            SystemMetrics.model_validate(system_metrics)
            if system_metrics is not None
            else None
        )
        progress = (
            BenchmarkProgress.model_validate_json(json.dumps(benchmark_progress))
            if benchmark_progress is not None
            else None
        )
        message = (
            f"ditto-validator-heartbeat:v4:{hotkey}:0.1.0:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(metrics)}:"
            f"{benchmark_progress_signing_token(progress)}:{ts}"
        )
    elif protocol_version >= 3:
        metrics = (
            SystemMetrics.model_validate(system_metrics)
            if system_metrics is not None
            else None
        )
        message = (
            f"ditto-validator-heartbeat:v3:{hotkey}:0.1.0:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:"
            f"{system_metrics_signing_token(metrics)}:{ts}"
        )
    elif protocol_version >= 2:
        message = (
            f"ditto-validator-heartbeat:v2:{hotkey}:0.1.0:{protocol_version}:"
            f"{code_digest}:{state}:{active_agent_id or ''}:{ts}"
        )
    else:
        message = (
            f"ditto-validator-heartbeat:v1:{hotkey}:0.1.0:1:{code_digest}:{state}:{ts}"
        )
    payload: dict[str, object] = {
        "validator_hotkey": hotkey,
        "software_version": "0.1.0",
        "protocol_version": protocol_version,
        "code_digest": code_digest,
        "state": state,
        "timestamp": ts,
        "signature": keypair.sign(message.encode()).hex(),
    }
    if active_agent_id is not None:
        payload["active_agent_id"] = str(active_agent_id)
    if system_metrics is not None:
        payload["system_metrics"] = system_metrics
    if benchmark_progress is not None:
        payload["benchmark_progress"] = benchmark_progress
    if capabilities is not None:
        payload["capabilities"] = capabilities
    if stack is not None:
        payload["stack"] = stack
    if stack_health is not None:
        payload["stack_health"] = stack_health
    if benchmark_capacity is not None:
        payload["benchmark_capacity"] = benchmark_capacity
    if confirmation_progress is not None:
        payload["confirmation_progress"] = confirmation_progress
    if updater_status is not None:
        payload["updater_status"] = updater_status
    return payload


def _progress(
    stage: str,
    *,
    completed: int | None = None,
    total: int | None = None,
    ticket_deadline: datetime = _TICKET_DEADLINE,
) -> dict[str, object]:
    """Build the exact privacy-safe progress shape accepted by protocol v4."""
    return {
        "stage": stage,
        "completed": completed,
        "total": total,
        "ticket_deadline": ticket_deadline.isoformat(),
    }


def _as_utc(value: datetime) -> datetime:
    """The SQLite unit-test fallback hands back naive datetimes; compare in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _score_to_quorum(
    client: httpx.AsyncClient,
    agent_id: UUID,
    *,
    maker: async_sessionmaker[AsyncSession],
    run_id: str = "run_q",
    composite: float = 0.82,
    **overrides: object,
) -> httpx.Response:
    """Seed a ticket for each quorum validator and post one score each (all at
    ``composite``, so the median is ``composite``); return the final response,
    finalized on the last."""
    resp: httpx.Response | None = None
    for i, kp in enumerate(_KEYPAIRS):
        await _seed_ticket(maker, agent_id, keypair=kp)
        resp = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                run_id=f"{run_id}_{i}",
                keypair=kp,
                composite=composite,
                **overrides,
            ),
        )
        assert resp.status_code == 200, resp.text
    assert resp is not None
    return resp


# --- DB + dependency wiring ------------------------------------------------


def _install_db(app: FastAPI, maker: async_sessionmaker[AsyncSession]) -> None:
    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _session


async def _widest_carryover_policy(
    app: FastAPI, maker: async_sessionmaker[AsyncSession]
) -> None:
    """Write the most permissive previous-generation policy still expressible.

    This used to be ``_enable_retired_era_backfill``, and it set exactly one
    field: ``allow_retired_era_backfill``. That field is gone, along with the
    branch that read it, so the helper's job has inverted. It now writes every
    remaining carryover knob wide open and lets the endpoint's own resolver
    pick the revision up, which is what makes "no setting can re-open a retired
    era" a claim about the settings board rather than about a default.
    """
    settings = QueuePolicySettings(
        prev_gen_carryover=PrevGenCarryoverSettings(
            enabled=True,
            include_exhausted=True,
            dedupe_scope="none",
            require_cohort_complete=False,
            require_desired_era_drained=False,
        )
    )
    payload = settings.model_dump(mode="json")
    async with maker() as session, session.begin():
        await insert_queue_policy_settings_revision(
            session,
            parent_revision=0,
            scope="*",
            settings=payload,
            checksum=hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            reason="test: widest previous-generation carryover policy",
            actor="test",
        )
    # The resolver reads through app.state, not the request session override.
    app.state.session_maker = maker
    app.state.queue_policy_settings.invalidate()


async def _install_deferred_review_mode(
    app: FastAPI, maker: async_sessionmaker[AsyncSession], mode: str
) -> None:
    """Write a queue policy that only changes the source-review mode."""
    settings = QueuePolicySettings(
        deferred_source_review=DeferredSourceReviewSettings(mode=mode)  # type: ignore[arg-type]
    )
    payload = settings.model_dump(mode="json")
    async with maker() as session, session.begin():
        await insert_queue_policy_settings_revision(
            session,
            parent_revision=0,
            scope="*",
            settings=payload,
            checksum=hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            reason=f"test: deferred source review mode {mode}",
            actor="test",
        )
    app.state.session_maker = maker
    app.state.queue_policy_settings.invalidate()
    # Self-verifying: a helper that silently failed to install the policy would
    # make every test using it pass for the wrong reason.
    resolved = await app.state.queue_policy_settings.resolve(maker)
    assert resolved.deferred_source_review.mode == mode


async def _seed_activated_era(
    maker: async_sessionmaker[AsyncSession],
    *,
    version: int = _BENCH_VERSION,
    activated_at: datetime | None = None,
) -> UUID:
    """Record the activation that puts the fleet on ``version``.

    With no rollout row at all ``active_bench_version`` still answers
    ``DEFAULT_BENCH_VERSION``, which is 2 -- and 2 is now beneath the ticket
    floor, so every lease the job endpoint tries to cut is refused by the
    ``validator_tickets_bench_version_floor`` trigger and surfaces as a 500
    that says nothing about benchmarks. Production is never in that state: it
    has the v6 -> v7 activation on record. A test that asks the platform to
    issue work has to say the same thing.

    Activated an hour in the past so agents seeded with ``created_at=now`` are
    submissions of this era and pass ``benchmark_admission_predicate``.
    """
    when = activated_at or (datetime.now(UTC) - timedelta(hours=1))
    # Idempotent: (from_version, desired_version) is UNIQUE, and more than one
    # fixture legitimately wants the fleet on this era -- the autouse
    # ``_current_era`` fixtures and ``_seed_top5_emission_set`` both say so.
    # Returning the existing transition lets them compose instead of colliding
    # on a duplicate-key error that says nothing about benchmarks.
    async with maker() as probe:
        existing = await probe.scalar(
            select(BenchmarkRollout.rollout_id).where(
                BenchmarkRollout.from_version == version - 1,
                BenchmarkRollout.desired_version == version,
            )
        )
    if existing is not None:
        return existing
    rollout_id = uuid4()
    async with maker() as session:
        # An activation into a RETIRED era is history, and history is exactly
        # what production holds: `benchmark_rollout_desired_floor` is NOT VALID,
        # so the real v2->v3 and v3->v4 rows are grandfathered. A fresh test
        # database has nothing to grandfather, so it has to write them the same
        # way -- beneath a lifted floor, which is then restored.
        if version < 7:
            async with retired_era_writes_allowed(session), session.begin():
                session.add(
                    BenchmarkRollout(
                        rollout_id=rollout_id,
                        from_version=version - 1,
                        desired_version=version,
                        status="activated",
                        cohort_size=5,
                        created_at=when,
                        activated_at=when,
                    )
                )
        else:
            async with session.begin():
                session.add(
                    BenchmarkRollout(
                        rollout_id=rollout_id,
                        from_version=version - 1,
                        desired_version=version,
                        status="activated",
                        cohort_size=5,
                        created_at=when,
                        activated_at=when,
                    )
                )
    return rollout_id


# The one healthy, accepting slot the v7 job fixtures lease through. Any
# heartbeat at protocol >= 10 is required to publish capacity, and ``request_job``
# answers 204 for a slot that is not in ``healthy_slots`` or for an admission
# that is not ``accepting`` -- so this is the minimum shape that still issues.
_ACCEPTING_CAPACITY: dict[str, object] = {
    "configured_slots": 1,
    "healthy_slots": ["slot-0"],
    "admission": "accepting",
    "active": [],
}
# The slot those fixtures claim. Protocol >= 10 claims MUST name a slot (409
# otherwise), which is why the v7 job tests pass ``slot_id`` where the v2 ones
# could send a bare claim.
_SLOT_ID = "slot-0"


async def _seed_capable_pool(
    maker: async_sessionmaker[AsyncSession],
    *,
    version: int = _BENCH_VERSION,
    keypairs: Sequence[bittensor.Keypair] = _KEYPAIRS,
) -> None:
    """Heartbeats the allocator will actually count as capable of ``version``.

    At v2 a validator needed no heartbeat at all to be handed work, so most of
    these fixtures never seeded one. From v7 the bar is concrete and stacked:
    ``verified_scorer_for_version`` wants ticket inference, a signed score
    quorum and a v7 calibration manifest; ``request_job`` additionally refuses
    to treat inference as ready below protocol 11 (10 for pre-v7 targets); and
    protocol >= 10 makes published capacity and a named slot mandatory.

    Protocol 13 with ``_ACCEPTING_CAPACITY`` clears all of it. Claims against
    this pool have to name ``_SLOT_ID``.
    """
    capabilities = _scorer_capable_capabilities(
        now=datetime.now(UTC), versions=(version,)
    )
    for keypair in keypairs:
        await _seed_validator_heartbeat(
            maker,
            keypair=keypair,
            protocol_version=13,
            capabilities=capabilities,
            stack=_V7_STACK,
            benchmark_capacity=_ACCEPTING_CAPACITY,
        )


async def _install_ticket_inference(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    version: int = _BENCH_VERSION,
) -> None:
    """Make a ticket-scoped inference grant actually mintable for ``version``.

    v7 is not simply a larger number on the lease. ``request_job`` and the
    top-five lane both refuse outright -- 503 ``ticket inference capability is
    unavailable`` -- unless ``ensure_inference_grant`` can produce a grant, and
    that needs the proxy enabled, a routing policy for the era's model, and one
    calibration-eligible route whose profile and manifest match what the
    scorer advertises. None of this existed below v7, which is why fixtures
    that only ever leased v2 never had to state it.
    """
    from ditto.api_server.inference_routing import benchmark_model

    now = datetime.now(UTC)
    model = benchmark_model(version)
    async with maker() as session, session.begin():
        session.add(
            InferenceRoutingPolicy(
                model=model,
                enabled=True,
                speed_weight=0.65,
                cost_weight=0.25,
                exploration_weight=0.10,
                exploration_ticket_budget=3,
                min_tool_accuracy=0.55,
                min_composite=0.15,
                min_calibration_samples=20,
                max_error_rate=0.25,
                max_timeout_rate=0.15,
                cooldown_seconds=30,
                ewma_alpha=0.20,
                updated_at=now,
            )
        )
        session.add(
            InferenceProviderRoute(
                model=model,
                provider="Groq",
                profile_revision=_V7_ROUTE_PROFILE,
                status="healthy",
                calibration_status="eligible",
                calibration_tool_accuracy=0.65,
                calibration_composite=0.20,
                calibration_sample_count=60,
                calibration_manifest_sha256=_V7_CALIBRATION_MANIFEST,
                ewma_error_rate=0,
                ewma_timeout_rate=0,
                sample_count=60,
                selected_ticket_count=0,
                exploration_ticket_count=0,
                discovered_at=now,
                last_observed_at=now,
                updated_at=now,
            )
        )
    app.state.config = replace(
        app.state.config,
        inference_proxy=replace(
            app.state.config.inference_proxy,
            enabled=True,
            openrouter_api_key="test-only",
            allowed_models=(model,),
            routing_mode="adaptive",
        ),
    )


def _install_dataset_generator(app: FastAPI) -> MagicMock:
    """A deterministic full-run dataset generator, keyed by (version, seed)."""
    generator = MagicMock(run_size="full")
    generator.generate = AsyncMock(
        side_effect=lambda seed, *, bench_version: hashlib.sha256(
            f"{bench_version}:{seed}".encode()
        ).hexdigest()
    )
    app.dependency_overrides[get_dataset_generator] = lambda: generator
    return generator


def _install_chain(
    app: FastAPI,
    *,
    permitted: bool = True,
    registered: bool = True,
    extra_keypairs: tuple[bittensor.Keypair, ...] = (),
) -> None:
    neurons = []
    if registered:
        for uid, kp in enumerate((*_KEYPAIRS, *extra_keypairs), start=1):
            neurons.append(
                NeuronInfo(
                    hotkey=kp.ss58_address,
                    coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
                    uid=uid,
                    stake=1000.0,
                    validator_permit=permitted,
                )
            )

    async def _chain() -> MagicMock:
        c = MagicMock()
        c.get_recent_neurons = AsyncMock(return_value=neurons)
        return c

    app.dependency_overrides[get_chain_client] = _chain


def _install_storage(app: FastAPI) -> MagicMock:
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock(
        return_value="https://signed.example/ditto-agents/x.tar.gz?sig=1"
    )

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
    miner_hotkey: str = _MINER_HOTKEY,
    sha256: str = _SHA256,
    size_bytes: int = 524288,
    screening_policy_version: int = SCREENING_POLICY_VERSION,
    screened_image: bool = True,
    dataset_version: int | None = _BENCH_VERSION,
) -> UUID:
    """Seed a submission in the shape the current era admits.

    ``screened_image`` defaults on because every contract from v3 up sets
    ``requires_screened_image``, and the allocator's candidate filter drops a
    submission without a verified one. While the only fixture era was v2 that
    never mattered -- v2 is the one contract that does not require it -- so an
    agent seeded with none was still leasable. Pass ``False`` where the absence
    is the point.

    ``dataset_version`` pins the versioned dataset the allocator requires for
    the same reason. ``queue_candidate_predicate`` demands a
    ``benchmark_datasets`` row for the era being leased on every version except
    2 -- literally ``if bench_version != 2`` -- so the one era these fixtures
    used to run in was also the one era that needed no pin. Now that the floor
    puts them on v7, an agent without one is silently unleasable: it is simply
    absent from the candidate set, and the endpoint answers 204 with nothing
    pointing at the dataset. Pass ``None`` where the missing pin is the point.
    """
    aid = agent_id or uuid4()
    now = datetime.now(UTC)
    async with maker() as s, s.begin():
        s.add(
            Agent(
                agent_id=aid,
                miner_hotkey=miner_hotkey,
                name=name,
                sha256=sha256,
                size_bytes=size_bytes,
                status=status,
                screening_policy_version=screening_policy_version,
                created_at=created_at or now,
                screened_image_sha256="12" * 32 if screened_image else None,
                screened_image_size_bytes=123 if screened_image else None,
                screened_image_id=("sha256:" + "34" * 32) if screened_image else None,
                screened_image_ref=(
                    f"ditto-screen/{aid}:latest" if screened_image else None
                ),
                screened_image_upload_id=uuid4() if screened_image else None,
                screened_image_verified_at=now if screened_image else None,
            )
        )
        if dataset_version is not None:
            # After the agent row exists: benchmark_datasets carries an FK onto
            # agents, and nothing relates the two mappers, so the unit of work
            # is free to order the inserts the other way round.
            await s.flush()
            s.add(
                BenchmarkDataset(
                    agent_id=aid,
                    bench_version=dataset_version,
                    seed=8675309,
                    sha256="cd" * 32,
                    run_size="full",
                )
            )
    return aid


async def _seed_revoked_grant(
    maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    *,
    bench_version: int = _BENCH_VERSION,
    deadline: datetime = _TICKET_DEADLINE,
    status: str = "revoked",
    keypair: bittensor.Keypair = _KEYPAIR,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    request_count: int = 0,
    request_budget: int = 1000,
    token_budget: int = 1_000_000,
    usage_accounting_version: int = 2,
) -> None:
    """Record the platform having terminated a lease's inference grant.

    Mirrors what begin_inference_request writes when it finds the owning ticket
    no longer ISSUED or its deadline rewritten: the grant flips to ``revoked``
    and every later request on it is declined 429.
    """
    async with maker() as s, s.begin():
        s.add(
            InferenceGrant(
                grant_id=uuid4(),
                agent_id=agent_id,
                bench_version=bench_version,
                validator_hotkey=keypair.ss58_address,
                slot_id="slot-0",
                ticket_deadline=deadline,
                expires_at=deadline,
                status=status,
                generation=0,
                allowed_models=["qwen/qwen3-32b"],
                request_budget=request_budget,
                token_budget=token_budget,
                request_count=request_count,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                usage_accounting_version=usage_accounting_version,
                embedding_model="test-embed",
                embedding_profile="test-embed-v1",
                embedding_provider="test",
                embedding_dimensions=768,
                embedding_request_budget=1000,
                embedding_token_budget=1_000_000,
            )
        )


async def _seed_ticket(
    maker: async_sessionmaker[AsyncSession],
    agent_id: UUID,
    *,
    keypair: bittensor.Keypair = _KEYPAIR,
    deadline: datetime = _TICKET_DEADLINE,
    bench_version: int = _BENCH_VERSION,
    issued_at: datetime | None = None,
    slot_id: str = "slot-0",
    purpose: TicketPurpose = TicketPurpose.CANONICAL_QUORUM,
    purpose_revision: int = 1,
    legacy_completion_allowed: bool = False,
    seed: int | None = None,
    dataset_sha256: str | None = None,
) -> None:
    """Seat (or re-open) an issued ticket for a specific (agent, validator) so a
    score against that agent is accepted by the k=3 gate. Upserts so a test can
    simulate the platform re-issuing a ticket to the same validator."""
    issued = (
        issued_at if issued_at is not None else datetime.now(UTC) - timedelta(seconds=1)
    )
    async with maker() as s:
        # A ticket in a RETIRED era is a lease that predates the floor. The
        # trigger refuses to create or re-lease one, which is the whole point,
        # so seeding it needs the floor lifted -- exactly as production's
        # surviving v6 leases predate the constraint. The floor is restored
        # before the test runs, so the drain it then asserts is a real drain.
        ctx: contextlib.AbstractAsyncContextManager[None]
        if bench_version < MIN_SCOREABLE_BENCH_VERSION:
            ctx = retired_era_writes_allowed(s)
        else:
            ctx = contextlib.nullcontext()
        async with ctx, s.begin():
            await _seed_ticket_row(
                s,
                agent_id=agent_id,
                bench_version=bench_version,
                keypair=keypair,
                slot_id=slot_id,
                purpose=purpose,
                purpose_revision=purpose_revision,
                legacy_completion_allowed=legacy_completion_allowed,
                issued=issued,
                deadline=deadline,
                seed=seed,
                dataset_sha256=dataset_sha256,
            )


async def _seed_ticket_row(
    s: AsyncSession,
    *,
    agent_id: UUID,
    bench_version: int,
    keypair: bittensor.Keypair,
    slot_id: str,
    purpose: TicketPurpose,
    purpose_revision: int,
    legacy_completion_allowed: bool,
    issued: datetime,
    deadline: datetime,
    seed: int | None,
    dataset_sha256: str | None,
) -> None:
    existing = await s.get(
        ValidatorTicket, (agent_id, bench_version, keypair.ss58_address)
    )
    if existing is None:
        s.add(
            ValidatorTicket(
                agent_id=agent_id,
                bench_version=bench_version,
                validator_hotkey=keypair.ss58_address,
                slot_id=slot_id,
                status=TicketStatus.ISSUED,
                purpose=purpose,
                purpose_revision=purpose_revision,
                legacy_completion_allowed=legacy_completion_allowed,
                issued_at=issued,
                deadline=deadline,
                seed=seed,
                dataset_sha256=dataset_sha256,
            )
        )
    else:
        existing.status = TicketStatus.ISSUED
        existing.purpose = purpose
        existing.purpose_revision = purpose_revision
        existing.legacy_completion_allowed = legacy_completion_allowed
        existing.slot_id = slot_id
        existing.issued_at = issued
        existing.deadline = deadline
        existing.seed = seed
        existing.dataset_sha256 = dataset_sha256


async def _seed_validator_heartbeat(
    maker: async_sessionmaker[AsyncSession],
    *,
    keypair: bittensor.Keypair = _KEYPAIR,
    software_version: str = "0.7.0",
    protocol_version: int = 4,
    seen_at: datetime | None = None,
    capabilities: dict[str, object] | None = None,
    stack: dict[str, object] | None = None,
    state: str = "polling",
    benchmark_capacity: dict[str, object] | None = None,
) -> None:
    now = seen_at or datetime.now(UTC)
    async with maker() as s, s.begin():
        # ``merge``, not ``add``: the v7 lane fixtures seed a capable heartbeat
        # for the whole pool up front, and a test that wants a DIFFERENT shape
        # for one hotkey (v2-only, stale, capability-less) has to be able to say
        # so by seeding over it. With ``add`` that second call is a primary-key
        # violation on ``validator_hotkey`` instead of an override.
        await s.merge(
            ValidatorHeartbeat(
                validator_hotkey=keypair.ss58_address,
                software_version=software_version,
                protocol_version=protocol_version,
                code_digest="ab" * 32,
                state=state,
                active_agent_id=None,
                first_seen_at=now,
                system_metrics=None,
                benchmark_progress=None,
                benchmark_progress_reported=False,
                benchmark_progress_agent_id=None,
                capabilities=capabilities,
                stack=stack,
                benchmark_capacity=benchmark_capacity,
                reported_at=now,
                seen_at=now,
                signature="ab" * 64,
            )
        )


_AUTH_HEADER = {"X-Validator-Hotkey": _VALIDATOR_HOTKEY}
_SYSTEM_METRICS = {
    "collected_at": 0,
    "cpu_percent": 15,
    "memory_percent": 40,
    "disk_percent": 55,
    "docker": {
        "status": "healthy",
        "running_containers": 4,
        "unhealthy_containers": 0,
    },
}

_V7_CAPABILITIES: dict[str, object] = {
    "screened_images": True,
    "require_screened_image": False,
    "source_build_fallback": True,
    "full_stack_managed": False,
    "stack_updater": False,
    "sandbox_egress_restricted": True,
    "executor_isolation": "rootless_dind",
}
_V7_COMPONENTS: dict[str, object] = {
    name: {
        "source_revision": f"{index:x}" * 40,
        "version": f"1.2.{index}",
        "provenance": "committed_pin",
    }
    for index, name in enumerate(
        (
            "ditto_subnet",
            "dittobench_api",
            "sandbox_docker",
            "model_relay",
            "pylon",
            "ollama",
        ),
        start=1,
    )
}
_V7_STACK: dict[str, object] = {
    "mode": "source",
    "compose_schema": 1,
    "release_descriptor_digest": None,
    "components": _V7_COMPONENTS,
}


_V9_SCORER: dict[str, object] = {
    "status": "fresh_verified",
    # The era the fleet is on, not [2, 3].
    #
    # These were arbitrary "some versions the scorer supports" and the telemetry
    # tests using this blob are not about version support. They became
    # load-bearing once the active era stopped defaulting to 2: a scorer that
    # advertises only retired versions cannot serve the active benchmark, and
    # `bench_serviceability != "serving"` is ranked CRITICAL in the fleet
    # health roll-up -- correctly, but it swamped every unrelated health
    # assertion in this file.
    "supported_bench_versions": [_BENCH_VERSION],
    "observed_at": 1_784_020_800,
    "software_version": "1.2.2",
    "source_revision": "2" * 40,
}
_V9_CAPABILITIES: dict[str, object] = {
    **_V7_CAPABILITIES,
    "scorer_benchmarks": _V9_SCORER,
}
_V9_STACK_HEALTH: dict[str, object] = {
    "ditto_subnet": {
        "health": "healthy",
        "required": True,
        "observed_at": 1_784_020_800,
        "ready": True,
        "observed_identity": {"version": "1.2.3"},
    },
    "dittobench_api": {
        "health": "healthy",
        "required": True,
        "observed_at": 1_784_020_800,
        "ready": True,
        "observed_identity": {"source_revision": "2" * 40, "version": "1.2.2"},
    },
    "sandbox_docker": {
        "health": "unknown",
        "required": True,
    },
    "model_relay": {
        "health": "identity_mismatch",
        "required": True,
        "observed_at": 1_784_020_700,
        "ready": True,
        "model_ready": True,
        "observed_identity": {"source_revision": "c" * 40},
    },
    "pylon": {
        "health": "degraded",
        "required": True,
        "observed_at": 1_784_020_700,
        "ready": False,
    },
    "ollama": {
        "health": "unreachable",
        "required": True,
        "observed_at": 1_784_017_200,
    },
}


# A v10+ capacity that claims no occupied slot. The lease roster is derived from
# the ledger and not from this, so an idle claim is the cleanest way to show the
# two are independent: the platform answers with the leases it issued regardless
# of what the reporter says it is running.
_IDLE_CAPACITY: dict[str, object] = {
    "configured_slots": 2,
    "healthy_slots": ["slot-0", "slot-1"],
    "admission": "accepting",
    "active": [],
}


async def test_v22_heartbeat_persists_only_live_signed_confirmation_progress(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The independent LongMem lane is visible but cannot forge Platform work."""
    agent_id = await _seed_agent(
        session_maker,
        status=AgentStatus.SCORED,
        name="confirmation-progress-agent",
        sha256="ef" * 32,
    )
    now = datetime.now(UTC)
    deadline = now + timedelta(minutes=90)
    bundle_id = uuid4()
    ticket_id = uuid4()
    async with session_maker() as session, session.begin():
        revision = ConfirmationBundleSettingsRevision(
            parent_revision=0,
            scope="*",
            settings={},
            checksum="a" * 64,
            reason="exercise signed confirmation heartbeat progress",
            actor="pytest@example.com",
        )
        session.add(revision)
        await session.flush()
        session.add(
            ConfirmationBundle(
                bundle_id=bundle_id,
                artifact_sha256="ef" * 32,
                bench_version=9,
                profile_revision="longmemeval-s-native-memory-tools-v2",
                profile_checksum="b" * 64,
                settings_revision=revision.revision,
                settings_checksum=revision.checksum,
                state="leased",
            )
        )
        await session.flush()
        session.add(
            ConfirmationBundleSubject(
                agent_id=agent_id,
                bench_version=9,
                artifact_sha256="ef" * 32,
                bundle_id=bundle_id,
                result_status="provisional",
                base_evidence_sha256="c" * 64,
                base_quality_micros=900_000,
                base_stderr_micros=10_000,
                base_model_factor_bps=10_000,
                base_tool_factor_bps=10_000,
            )
        )
        session.add(
            ConfirmationBundleTicket(
                ticket_id=ticket_id,
                bundle_id=bundle_id,
                validator_hotkey=_KEYPAIR.ss58_address,
                slot_id="longmem-0",
                status="issued",
                attempt=1,
                issued_at=now,
                deadline=deadline,
            )
        )
    _install_db(app, session_maker)
    _install_chain(app)
    capabilities = _quorum_capabilities()
    progress = {
        "bundle_id": str(bundle_id),
        "ticket_id": str(ticket_id),
        "agent_id": str(agent_id),
        "slot_id": "longmem-0",
        "stage": "running_confirmation",
        "completed": 17,
        "total": 500,
        "ticket_deadline": deadline.isoformat(),
    }
    accepted = await client.post(
        "/api/v1/validator/heartbeat",
        headers=_AUTH_HEADER,
        json=_heartbeat_payload(
            protocol_version=22,
            state="polling",
            timestamp=int(now.timestamp()),
            capabilities=capabilities,
            stack=_V7_STACK,
            stack_health=_V9_STACK_HEALTH,
            benchmark_capacity=_IDLE_CAPACITY,
            confirmation_progress=[progress],
        ),
    )
    assert accepted.status_code == 200, accepted.text
    async with session_maker() as session:
        stored = await session.get(ValidatorHeartbeat, _KEYPAIR.ss58_address)
        assert stored is not None
        assert stored.confirmation_progress is not None
        assert ConfirmationProgress.model_validate(
            stored.confirmation_progress[0]
        ) == ConfirmationProgress.model_validate(progress)
        assert stored.benchmark_capacity is not None
        assert stored.benchmark_capacity["active"] == []

    forged = {**progress, "ticket_id": str(uuid4())}
    dropped = await client.post(
        "/api/v1/validator/heartbeat",
        headers=_AUTH_HEADER,
        json=_heartbeat_payload(
            protocol_version=22,
            state="polling",
            timestamp=int(now.timestamp()) + 1,
            capabilities=capabilities,
            stack=_V7_STACK,
            stack_health=_V9_STACK_HEALTH,
            benchmark_capacity=_IDLE_CAPACITY,
            confirmation_progress=[forged],
        ),
    )
    assert dropped.status_code == 200, dropped.text
    async with session_maker() as session:
        stored = await session.get(ValidatorHeartbeat, _KEYPAIR.ss58_address)
        assert stored is not None
        assert stored.confirmation_progress == []


def _quorum_capabilities() -> dict[str, object]:
    """A fresh, mutable v12+ capability set that can serve the current era.

    Serving the era is new here, and it is not decoration. The public fleet view
    grades a validator ``critical`` when it cannot serve the benchmark being
    scored, ahead of every host-metric and stack finding -- and with no rollout
    row at all the era is now the floor (v7), not ``DEFAULT_BENCH_VERSION``. A
    capability set advertising only ``[2, 3]`` therefore reads as
    ``software_obsolete``, which swamps the badge the probe and stall tests are
    actually asserting on. Protocol 12 is exactly the floor at which a heartbeat
    may advertise v7, so a "v12+ capability set" that could not is a fixture
    describing a validator that cannot exist.
    """
    capabilities = json.loads(json.dumps(_V9_CAPABILITIES))
    capabilities["signed_score_quorum"] = True
    capabilities["ticket_inference"] = True
    capabilities["scorer_benchmarks"] = _scorer_capable_capabilities(
        now=datetime.now(UTC), versions=(_BENCH_VERSION,)
    )["scorer_benchmarks"]
    return dict(capabilities)


def _screener_heartbeat_payload(
    *, timestamp: int, system_metrics: dict[str, object]
) -> dict[str, object]:
    metrics = SystemMetrics.model_validate(system_metrics)
    message = (
        "ditto-screener-heartbeat:v1:"
        f"{_KEYPAIR.ss58_address}:0.4.2:1:{SCREENING_POLICY_VERSION}:polling::"
        f"{system_metrics_signing_token(metrics)}:{timestamp}"
    )
    return {
        "screener_hotkey": _KEYPAIR.ss58_address,
        "software_version": "0.4.2",
        "protocol_version": 1,
        "policy_version": SCREENING_POLICY_VERSION,
        "state": "polling",
        "timestamp": timestamp,
        "signature": _KEYPAIR.sign(message.encode()).hex(),
        "system_metrics": system_metrics,
    }


# --- Queue -----------------------------------------------------------------


class TestHeartbeat:
    async def test_v8_requires_signed_scorer_capability_and_v7_rejects_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        scorer = {
            "status": "fresh_verified",
            "supported_bench_versions": [2, 3],
            "observed_at": int(datetime.now(UTC).timestamp()),
            "software_version": "1.2.2",
            "source_revision": "2" * 40,
        }
        capabilities = {**_V7_CAPABILITIES, "scorer_benchmarks": scorer}

        rejected_v7 = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=7,
                capabilities=capabilities,
                stack=_V7_STACK,
            ),
        )
        assert rejected_v7.status_code == 422

        accepted_v8 = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=8,
                capabilities=capabilities,
                stack=_V7_STACK,
            ),
        )
        assert accepted_v8.status_code == 200, accepted_v8.text
        async with session_maker() as session:
            row = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert row is not None
            assert row.protocol_version == 8
            assert row.capabilities is not None
            assert row.capabilities["scorer_benchmarks"][
                "supported_bench_versions"
            ] == [2, 3]

    async def test_v9_persists_and_publishes_component_stack_health(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)

        accepted = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=9,
                capabilities=_V9_CAPABILITIES,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
            ),
        )
        assert accepted.status_code == 200, accepted.text

        expected_health = ValidatorStackHealth.model_validate_json(
            json.dumps(_V9_STACK_HEALTH)
        ).model_dump(mode="json", exclude_none=True)
        async with session_maker() as session:
            row = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert row is not None
            assert row.protocol_version == 9
            assert row.stack_health == expected_health

        public = (await client.get("/api/v1/public/validators")).json()["validators"][0]
        health = public["stack_health"]
        assert health is not None
        assert health["ditto_subnet"]["health"] == "healthy"
        assert health["sandbox_docker"]["health"] == "unknown"
        assert health["model_relay"]["health"] == "identity_mismatch"
        assert health["model_relay"]["observed_identity"]["source_revision"] == "c" * 40
        assert health["pylon"]["health"] == "degraded"
        assert health["ollama"]["health"] == "unreachable"
        # Probe URLs and host identity have no schema slot; belt-and-braces
        # regression that nothing network-shaped leaked into the public view.
        assert "://" not in json.dumps(health)

    async def test_v9_requires_stack_health_and_v8_rejects_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)

        missing = _heartbeat_payload(
            protocol_version=9,
            capabilities=_V9_CAPABILITIES,
            stack=_V7_STACK,
            stack_health=_V9_STACK_HEALTH,
        )
        missing.pop("stack_health")
        assert (
            await client.post(
                "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=missing
            )
        ).status_code == 422

        downgraded = _heartbeat_payload(
            protocol_version=8,
            capabilities=_V9_CAPABILITIES,
            stack=_V7_STACK,
        )
        downgraded["stack_health"] = _V9_STACK_HEALTH
        assert (
            await client.post(
                "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=downgraded
            )
        ).status_code == 422

        tampered = _heartbeat_payload(
            protocol_version=9,
            capabilities=_V9_CAPABILITIES,
            stack=_V7_STACK,
            stack_health=_V9_STACK_HEALTH,
        )
        upgraded_health = json.loads(json.dumps(_V9_STACK_HEALTH))
        upgraded_health["ollama"] = {
            "health": "healthy",
            "required": True,
            "observed_at": 1_784_017_200,
            "ready": True,
            "model_ready": True,
        }
        tampered["stack_health"] = upgraded_health
        assert (
            await client.post(
                "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=tampered
            )
        ).status_code == 401

    async def test_v23_persists_and_publicly_projects_only_closed_updater_state(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())
        current = "ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:" + "a" * 64
        candidate = "ghcr.io/ditto-assistant/ditto-subnet-stack@sha256:" + "b" * 64
        updater = {
            "enabled": True,
            "channel": "compat-2",
            "state": "backoff",
            "current_descriptor": current,
            "current_version": "0.63.1",
            "candidate_descriptor": candidate,
            "candidate_version": "0.64.0",
            "failed_candidate_count": 2,
            "retry_after": timestamp + 900,
            "suppressed": False,
            "last_failure_at": timestamp - 30,
            "last_failure_reason": "candidate_readiness_failed",
            "observed_at": timestamp,
        }
        payload = _heartbeat_payload(
            timestamp=timestamp,
            protocol_version=23,
            capabilities=_quorum_capabilities(),
            stack=_V7_STACK,
            stack_health=_V9_STACK_HEALTH,
            benchmark_capacity=_IDLE_CAPACITY,
            confirmation_progress=[],
            updater_status=updater,
        )

        response = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=payload
        )
        assert response.status_code == 200, response.text
        async with session_maker() as session:
            row = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert row is not None
            assert row.updater_status == updater

        fleet = (await client.get("/api/v1/public/validators")).json()
        member = next(
            item
            for item in fleet["validators"]
            if item["validator_hotkey"] == _VALIDATOR_HOTKEY
        )
        assert member["updater_status"] == {
            **updater,
            "transaction_phase": None,
            "last_success_at": None,
        }
        assert "error" not in json.dumps(member["updater_status"])

        malformed = dict(payload)
        malformed["updater_status"] = {**updater, "journal": "secret host log"}
        accepted = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=malformed
        )
        assert accepted.status_code == 200, accepted.text
        async with session_maker() as session:
            row = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert row is not None
            assert row.updater_status == updater

    async def test_pre_v23_heartbeat_remains_valid_without_updater_state(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _heartbeat_payload(
            protocol_version=22,
            capabilities=_quorum_capabilities(),
            stack=_V7_STACK,
            stack_health=_V9_STACK_HEALTH,
            benchmark_capacity=_IDLE_CAPACITY,
            confirmation_progress=[],
        )

        assert (
            await client.post(
                "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=payload
            )
        ).status_code == 200

    async def test_v10_persists_and_publishes_every_active_slot(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        engine: AsyncEngine,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        first = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, name="slot-a"
        )
        second = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="slot-b",
            miner_hotkey="5SecondMiner" + "x" * 35,
        )
        await _seed_ticket(session_maker, first, slot_id="slot-0")
        await _seed_ticket(session_maker, second, slot_id="slot-1")
        _install_db(app, session_maker)
        _install_chain(app)
        first_progress = _progress("running_benchmark", completed=3, total=10)
        second_progress = _progress("running_benchmark", completed=7, total=10)
        capacity = {
            "configured_slots": 2,
            "healthy_slots": ["slot-0", "slot-1"],
            "admission": "accepting",
            "active": [
                {
                    "slot_id": "slot-0",
                    "agent_id": str(first),
                    "bench_version": _BENCH_VERSION,
                    "progress": first_progress,
                },
                {
                    "slot_id": "slot-1",
                    "agent_id": str(second),
                    "bench_version": _BENCH_VERSION,
                    "progress": second_progress,
                },
            ],
        }
        statements: list[str] = []

        def record_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
        try:
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=10,
                    state="running_benchmark",
                    active_agent_id=first,
                    benchmark_progress=first_progress,
                    capabilities=_V9_CAPABILITIES,
                    stack=_V7_STACK,
                    stack_health=_V9_STACK_HEALTH,
                    benchmark_capacity=capacity,
                ),
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_statement)
        assert response.status_code == 200, response.text
        slot_ticket_reads = [
            statement
            for statement in statements
            if "FROM validator_tickets" in statement
            and "validator_tickets.slot_id" in statement
        ]
        # Validation plus SKIP-LOCKED first-report stamping: constant at two
        # ticket round trips instead of two or three per active slot.
        assert len(slot_ticket_reads) == 2

        public = (await client.get("/api/v1/public/validators")).json()["validators"][0]
        assert public["configured_slots"] == 2
        assert public["healthy_slots"] == ["slot-0", "slot-1"]
        assert public["admission"] == "accepting"
        assert [item["slot_id"] for item in public["active_benchmarks"]] == [
            "slot-0",
            "slot-1",
        ]
        assert public["active_benchmark"] == public["active_benchmarks"][0]

    async def test_confirmed_slot_stamps_the_lease_as_reported_once(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Ingest is what tells the liveness gate this lease ever ran.

        Until a slot confirms, the lease is unrevocable: its silence means "has
        not announced itself yet". The stamp is what converts later silence into
        evidence, so it has to be written exactly when the ledger first agrees
        the slot is live -- and it must not move afterwards, since the question
        is whether the lease ever testified, not when it last did.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, slot_id="slot-0")
        _install_db(app, session_maker)
        _install_chain(app)

        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.first_reported_at is None

        async def _beat(completed: int) -> None:
            progress = _progress("running_benchmark", completed=completed, total=10)
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=10,
                    state="running_benchmark",
                    active_agent_id=agent_id,
                    benchmark_progress=progress,
                    capabilities=_V9_CAPABILITIES,
                    stack=_V7_STACK,
                    stack_health=_V9_STACK_HEALTH,
                    benchmark_capacity={
                        "configured_slots": 1,
                        "healthy_slots": ["slot-0"],
                        "admission": "accepting",
                        "active": [
                            {
                                "slot_id": "slot-0",
                                "agent_id": str(agent_id),
                                "bench_version": _BENCH_VERSION,
                                "progress": progress,
                            }
                        ],
                    },
                ),
            )
            assert response.status_code == 200, response.text

        await _beat(3)
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.first_reported_at is not None
            stamped = ticket.first_reported_at

        await _beat(7)
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.first_reported_at == stamped

    async def test_v16_claimed_slot_without_progress_is_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Protocol 16 announces a leased slot before it has anything to report.

        The platform has to accept this *before* any validator sends it. A
        stricter model would 422 during FastAPI parsing, before the handler runs,
        which freezes ``seen_at`` -- the input to force-expiry -- and causes the
        very revocation the change exists to prevent. So this is the deploy
        ordering encoded as a test: tolerate first, emit second.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, slot_id="slot-0")
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=16,
                state="running_benchmark",
                # The v10 mirror rule still applies and needs no relaxing: the
                # legacy scalars mirror the primary slot, so `active_agent_id`
                # names it and `benchmark_progress` is null for the same reason
                # the slot's own progress is.
                active_agent_id=agent_id,
                capabilities=_quorum_capabilities(),
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity={
                    "configured_slots": 1,
                    "healthy_slots": ["slot-0"],
                    "admission": "accepting",
                    "active": [
                        {
                            "slot_id": "slot-0",
                            "agent_id": str(agent_id),
                            "bench_version": _BENCH_VERSION,
                            "progress": None,
                        }
                    ],
                },
            ),
        )
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True

        # The slot is now visible as occupied, which is the whole point: the
        # liveness gate sees it in `capacity.active` and refuses to revoke,
        # without needing the never-reported fallback at all.
        async with session_maker() as session:
            stored = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert stored is not None
            assert stored.claimed_slots == [
                {"slot_id": "slot-0", "agent_id": str(agent_id)}
            ]
            assert stored.benchmark_capacity is not None
            active = stored.benchmark_capacity["active"]
            assert [slot["slot_id"] for slot in active] == ["slot-0"]
            assert active[0]["progress"] is None

    async def test_v17_answers_with_the_leases_the_ledger_holds(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The roster is the platform's assignment truth, not the reporter's."""
        first = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        second = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, first, slot_id="slot-0")
        await _seed_ticket(session_maker, second, slot_id="slot-1", bench_version=3)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=17,
                capabilities=_quorum_capabilities(),
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=_IDLE_CAPACITY,
            ),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["accepted"] is True
        assert [(lease["slot_id"], lease["agent_id"]) for lease in body["leases"]] == [
            ("slot-0", str(first)),
            ("slot-1", str(second)),
        ]
        # One live-era lease and one grandfathered v3 lease still draining --
        # which is the shape that makes the point: the roster reports the era
        # of each lease the ledger holds, not one era for the whole fleet.
        assert [lease["bench_version"] for lease in body["leases"]] == [
            _BENCH_VERSION,
            3,
        ]
        for lease in body["leases"]:
            assert datetime.fromisoformat(lease["deadline"]) == _TICKET_DEADLINE

    async def test_v17_holding_nothing_answers_an_empty_roster_not_null(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """``[]`` and ``null`` are different answers and must stay different.

        ``[]`` is the authoritative "you hold no lease" a reporter may act on;
        ``null`` is "not answered" and never authorizes stopping anything. A
        platform that collapsed the empty roster to ``null`` would silently
        disable cancellation in the one case it matters most -- every lease
        evicted at once.
        """
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=17,
                capabilities=_quorum_capabilities(),
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=_IDLE_CAPACITY,
            ),
        )

        assert response.status_code == 200, response.text
        assert response.json()["leases"] == []

    async def test_a_pre_v17_reporter_is_told_nothing_about_its_leases(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The fleet is mixed; an older validator must see today's response.

        Protocols 15 and 16 are both live in production. Neither understands the
        roster, so neither is sent one -- and because ``null`` is the "not
        answered" value, a v16 build that later learns to parse the field still
        cannot be talked into cancelling by an older platform.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, slot_id="slot-0")
        _install_db(app, session_maker)
        _install_chain(app)

        for protocol_version in (15, 16):
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=protocol_version,
                    capabilities=_quorum_capabilities(),
                    stack=_V7_STACK,
                    stack_health=_V9_STACK_HEALTH,
                    benchmark_capacity=_IDLE_CAPACITY,
                ),
            )
            assert response.status_code == 200, response.text
            assert response.json()["leases"] is None

    async def test_an_evicted_lease_leaves_the_roster_immediately(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """This is the whole point: eviction becomes visible to the reporter.

        Before v17 an operator eviction (#515) freed the platform-side slot
        instantly while the validator kept running the benchmark to completion on
        a lease it no longer held -- a full host slot burned for up to the lease
        TTL to produce a score the platform then refuses with a 409.
        """
        evicted = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        kept = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, evicted, slot_id="slot-0")
        await _seed_ticket(session_maker, kept, slot_id="slot-1")
        _install_db(app, session_maker)
        _install_chain(app)

        async def _roster() -> list[dict]:
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=17,
                    capabilities=_quorum_capabilities(),
                    stack=_V7_STACK,
                    stack_health=_V9_STACK_HEALTH,
                    benchmark_capacity=_IDLE_CAPACITY,
                ),
            )
            assert response.status_code == 200, response.text
            return response.json()["leases"]

        assert [lease["slot_id"] for lease in await _roster()] == ["slot-0", "slot-1"]

        # Exactly what `force_expire_lease` writes on an operator eviction.
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            ticket = await session.get(
                ValidatorTicket, (evicted, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            ticket.status = TicketStatus.EXPIRED
            ticket.deadline = now
            ticket.retry_after = now

        remaining = await _roster()
        assert [lease["slot_id"] for lease in remaining] == ["slot-1"]
        assert [lease["agent_id"] for lease in remaining] == [str(kept)]

    async def test_a_failed_roster_read_answers_null_and_never_an_empty_list(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A broken read must degrade to silence, never to "you hold nothing".

        This is the single most dangerous line in the feature. A reporter cancels
        on an authoritative empty roster, so answering ``[]`` when the query blew
        up would kill every run on the fleet at once. Absence of information has
        to stay absence of information -- the same inverted burden of proof that
        #437, #443 and #496 each had to be written to restore.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, slot_id="slot-0")
        _install_db(app, session_maker)
        _install_chain(app)

        async def boom(*_args: object, **_kwargs: object) -> list[ValidatorTicket]:
            raise RuntimeError("lease roster read exploded")

        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.list_validator_live_leases", boom
        )

        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=17,
                capabilities=_quorum_capabilities(),
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=_IDLE_CAPACITY,
            ),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["leases"] is None
        # Liveness still lands: a failed roster read is not a failed heartbeat.
        assert body["accepted"] is True
        async with session_maker() as session:
            stored = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert stored is not None
            assert stored.protocol_version == 17

    async def test_capacity_validation_failure_still_advances_liveness(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A broken capacity payload must not make a live validator look dead.

        Liveness (``seen_at`` / ``reported_at``) is proven by the signature alone.
        When payload validation blows up — here a ``KeyError`` from a stage
        missing from ``_STAGE_ORDER``, the exact shape of #430, which no
        ``except HeartbeatProgressRegressionError`` catches — the ingest must
        keep the liveness write and drop only the work payload. Rolling both back
        is what froze the capacity blob that a lease revocation then acted on,
        destroying three healthy v7 runs (#437).
        """
        agent = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent, slot_id="slot-0")
        _install_db(app, session_maker)
        _install_chain(app)

        def _capacity(progress: dict[str, object]) -> dict[str, object]:
            return {
                "configured_slots": 1,
                "healthy_slots": ["slot-0"],
                "admission": "accepting",
                "active": [
                    {
                        "slot_id": "slot-0",
                        "agent_id": str(agent),
                        "bench_version": _BENCH_VERSION,
                        "progress": progress,
                    }
                ],
            }

        base_ts = int(datetime.now(UTC).timestamp()) - 10
        first_progress = _progress("running_benchmark", completed=3, total=10)
        first = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                timestamp=base_ts,
                protocol_version=10,
                state="running_benchmark",
                active_agent_id=agent,
                benchmark_progress=first_progress,
                capabilities=_V9_CAPABILITIES,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=_capacity(first_progress),
            ),
        )
        assert first.status_code == 200, first.text
        async with session_maker() as s:
            stored = await s.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert stored is not None
            assert stored.benchmark_capacity is not None
            before_seen_at = _as_utc(stored.seen_at)
            before_reported_at = _as_utc(stored.reported_at)

        # The next heartbeat carries a slot the loop compares against the stored
        # one, so the patched validator is reached while the row locks are held.
        def _boom(*_args: object) -> None:
            raise KeyError("generating_dataset")

        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator._validate_same_lease_progress", _boom
        )

        second_progress = _progress("running_benchmark", completed=6, total=10)
        second = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                timestamp=base_ts + 5,
                protocol_version=10,
                state="running_benchmark",
                active_agent_id=agent,
                benchmark_progress=second_progress,
                capabilities=_V9_CAPABILITIES,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=_capacity(second_progress),
            ),
        )
        assert second.status_code == 200, second.text
        assert second.json()["accepted"] is True

        async with session_maker() as s:
            stored = await s.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert stored is not None
            # Liveness advanced: this validator does not read as heartbeat_stale.
            assert _as_utc(stored.seen_at) > before_seen_at
            assert _as_utc(stored.reported_at) > before_reported_at
            assert _as_utc(stored.reported_at) == datetime.fromtimestamp(
                base_ts + 5, tz=UTC
            )
            # The payload — and only the payload — was dropped. A NULL capacity
            # makes the next `/job` claim fail closed (428) instead of letting a
            # revocation act on a frozen blob.
            assert stored.benchmark_capacity is None
            assert stored.active_agent_id is None

    async def test_v10_accepts_v7_scorer_advertisement_without_v11_calibration(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A newer source scorer must not invalidate its legacy validator."""
        _install_db(app, session_maker)
        _install_chain(app)
        capabilities = json.loads(json.dumps(_V9_CAPABILITIES))
        capabilities["scorer_benchmarks"]["supported_bench_versions"] = [2, 7]
        capacity = {
            "configured_slots": 1,
            "healthy_slots": ["slot-0"],
            "admission": "accepting",
            "active": [],
        }

        accepted = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=10,
                capabilities=capabilities,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert accepted.status_code == 200, accepted.text

        rejected_v11 = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=11,
                capabilities=capabilities,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert rejected_v11.status_code == 422, rejected_v11.text
        assert rejected_v11.json()["message"] == "request validation failed"

        capabilities["scorer_benchmarks"]["v7_calibration"] = {
            "manifest_sha256": "c" * 64,
            "supported_routes": [
                {
                    "provider": "openrouter",
                    "profile_revision": "openrouter-route-8efde5ce9f5a4e58-v1",
                    "model": "openai/gpt-oss-20b",
                }
            ],
        }
        capabilities["ticket_inference"] = True
        accepted_v11 = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=11,
                capabilities=capabilities,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert accepted_v11.status_code == 200, accepted_v11.text

    async def test_v15_publishes_the_evidence_behind_the_scorer_status(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The TAO.com sidecar, end to end.

        Its ``/v1/capabilities`` route 404d, so the validator reported
        ``legacy_v2`` with every identity field null -- byte-identical to a real
        pre-capabilities scorer. The fleet view read ``warning`` beside
        ``accepting`` while the validator took leases it could never finish.
        With the probe the same heartbeat is publicly ``critical`` and says why.
        """
        _install_db(app, session_maker)
        _install_chain(app)
        capabilities = _quorum_capabilities()
        capabilities["scorer_benchmarks"] = {
            "status": "legacy_v2",
            "supported_bench_versions": [2],
            "probe": {
                "outcome": "http_error",
                "observed_at": 1_784_020_800,
                "http_status": 404,
                "consecutive_failures": 97,
            },
        }
        capacity = {
            "configured_slots": 1,
            "healthy_slots": ["slot-0"],
            "admission": "accepting",
            "active": [],
        }

        accepted = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=15,
                capabilities=capabilities,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert accepted.status_code == 200, accepted.text

        public = (await client.get("/api/v1/public/validators")).json()["validators"][0]
        assert public["scorer_liveness"] == "not_serving"
        assert public["health"] == "critical"
        assert "scorer not serving: http 404 (97 in a row)" in public["health_reasons"]
        probe = public["capabilities"]["scorer_benchmarks"]["probe"]
        assert probe["outcome"] == "http_error"
        assert probe["http_status"] == 404
        assert probe.get("last_served_at") is None
        # Admission semantics are deliberately untouched by this change: the
        # validator is now visibly broken, and whether it should stop accepting
        # work is a separate fleet-wide policy decision.
        assert public["admission"] == "accepting"

    async def test_probe_evidence_requires_protocol_v15(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A validator cannot claim an old protocol and send a new field.

        The signed envelope and the declared protocol have to agree, or the
        platform cannot reason about what a given protocol number guarantees.
        """
        _install_db(app, session_maker)
        _install_chain(app)
        capabilities = _quorum_capabilities()
        scorer = cast(dict[str, object], capabilities["scorer_benchmarks"])
        scorer["probe"] = {
            "outcome": "served",
            "observed_at": 1_784_020_800,
            "http_status": 200,
            "last_served_at": 1_784_020_800,
        }
        capacity = {
            "configured_slots": 1,
            "healthy_slots": ["slot-0"],
            "admission": "accepting",
            "active": [],
        }

        rejected = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=14,
                capabilities=capabilities,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert rejected.status_code == 422, rejected.text

        accepted = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=15,
                capabilities=capabilities,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert accepted.status_code == 200, accepted.text

    async def test_a_v14_validator_keeps_working_against_a_v15_platform(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Old validators must be unaffected while the fleet rolls forward.

        Nothing about their signing bytes changed, so the only observable
        difference is that they read ``unreported`` rather than claiming a
        liveness they never measured.
        """
        _install_db(app, session_maker)
        _install_chain(app)
        capacity = {
            "configured_slots": 1,
            "healthy_slots": ["slot-0"],
            "admission": "accepting",
            "active": [],
        }

        accepted = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=14,
                capabilities=_quorum_capabilities(),
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert accepted.status_code == 200, accepted.text

        public = (await client.get("/api/v1/public/validators")).json()["validators"][0]
        assert public["scorer_liveness"] == "unreported"
        assert public["health"] != "critical"
        assert public["capabilities"]["scorer_benchmarks"]["probe"] is None
        # No scorer-liveness reason is invented for software that cannot report
        # one; the reasons it does carry are the pre-existing stack findings.
        assert not [
            reason
            for reason in public["health_reasons"]
            if reason.startswith("scorer ")
        ]

    async def test_malformed_stored_stack_health_is_omitted_publicly(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        accepted = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=9,
                capabilities=_V9_CAPABILITIES,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
            ),
        )
        assert accepted.status_code == 200, accepted.text
        async with session_maker() as session, session.begin():
            row = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert row is not None
            row.stack_health = {"hostname": "validator-vm", "logs": ["leak"]}

        public = (await client.get("/api/v1/public/validators")).json()["validators"][0]
        assert public["stack_health"] is None

    async def test_v7_persists_and_publishes_typed_capabilities(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _heartbeat_payload(
            protocol_version=7,
            capabilities=_V7_CAPABILITIES,
            stack=_V7_STACK,
        )

        response = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=payload
        )

        assert response.status_code == 200, response.text
        async with session_maker() as session:
            row = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert row is not None
            assert row.capabilities == _V7_CAPABILITIES
            expected_stack = ValidatorStackIdentity.model_validate(
                _V7_STACK
            ).model_dump(mode="json")
            assert row.stack == expected_stack
        public = (await client.get("/api/v1/public/validators")).json()["validators"][0]
        assert public["capabilities"] == _V7_CAPABILITIES
        assert public["stack"] == expected_stack
        assert "signature" not in public

    async def test_v7_rejects_missing_contradictory_and_tampered_identity(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)

        missing = _heartbeat_payload()
        missing["protocol_version"] = 7
        assert (
            await client.post(
                "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=missing
            )
        ).status_code == 422

        contradictory = {**_V7_CAPABILITIES, "source_build_fallback": False}
        payload = _heartbeat_payload(
            protocol_version=7,
            capabilities=_V7_CAPABILITIES,
            stack=_V7_STACK,
        )
        payload["capabilities"] = contradictory
        rejected = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=payload
        )
        assert rejected.status_code == 422

        tampered = _heartbeat_payload(
            protocol_version=7,
            capabilities=_V7_CAPABILITIES,
            stack=_V7_STACK,
        )
        tampered_stack = dict(_V7_STACK)
        tampered_stack["compose_schema"] = 2
        tampered["stack"] = tampered_stack
        rejected = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=tampered
        )
        assert rejected.status_code == 401

    async def test_records_signed_build_and_publishes_status(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _heartbeat_payload()
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=payload,
        )
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True

        async with session_maker() as session:
            row = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert row is not None
            assert row.software_version == "0.1.0"
            assert row.code_digest == "ab" * 32
            assert row.state == "idle"

        public = await client.get("/api/v1/public/validators")
        assert public.status_code == 200
        body = public.json()
        assert body["reported_count"] == 1
        assert body["online_count"] == 1
        assert body["validators"][0]["validator_hotkey"] == _VALIDATOR_HOTKEY
        assert body["validators"][0]["state"] == "idle"
        assert body["validators"][0]["online"] is True

        replay = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=payload
        )
        assert replay.status_code == 200
        assert replay.json()["accepted"] is False
        assert replay.json()["seen_at"] == response.json()["seen_at"]

    async def test_early_stage_past_threshold_flags_stalled_and_warns(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A wedged early stage is surfaced on the run, whatever the badge says.

        ``stalled=True`` on the active benchmark is the claim, and it is
        unchanged. The fleet-health badge that used to accompany it is not:
        this validator reports protocol 4, and ``protocol_serves_version`` puts
        the floor for advertising v7 at protocol 12, so it now cannot serve the
        era being scored at all. The public view grades that ``critical`` ahead
        of every host-metric and stall finding, deliberately -- a validator that
        can complete no lease is a worse problem than a slow one, and must not
        read like a warning.

        So "warning" is not reachable for this fixture any more, and asserting
        it would only be asserting that the era is still v2. The stall is still
        proven, on the run itself and in the reasons.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        issued = datetime.now(UTC) - timedelta(minutes=20)
        await _seed_ticket(session_maker, agent_id, issued_at=issued)
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress("building_harness"),
            ),
        )
        assert response.status_code == 200, response.text
        validator = (await client.get("/api/v1/public/validators")).json()[
            "validators"
        ][0]
        assert validator["active_benchmark"]["stage"] == "building_harness"
        assert validator["active_benchmark"]["stalled"] is True
        assert validator["health"] == "critical"
        # The stall is still named, so it is surfaced rather than swallowed by
        # the obsolescence verdict that outranks it.
        assert any("stalled" in reason for reason in validator["health_reasons"]), (
            validator["health_reasons"]
        )

    async def test_v2_reports_current_agent_publicly(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _heartbeat_payload(
            protocol_version=2,
            state="running_benchmark",
            active_agent_id=agent_id,
        )

        response = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=payload
        )
        assert response.status_code == 200, response.text
        public = (await client.get("/api/v1/public/validators")).json()
        assert public["validators"][0]["active_agent_id"] == str(agent_id)
        active_benchmark = public["validators"][0]["active_benchmark"]
        started_at = datetime.fromisoformat(
            active_benchmark.pop("started_at").replace("Z", "+00:00")
        )
        assert started_at.tzinfo == UTC
        assert active_benchmark == {
            "slot_id": "slot-0",
            "agent_id": str(agent_id),
            "agent_name": "alpha-agent",
            "bench_version": _BENCH_VERSION,
            "stage": None,
            "completed_checks": None,
            "total_checks": None,
            "percent": None,
            "stalled": False,
            "purpose": "canonical_quorum",
        }

    async def test_operations_snapshot_is_atomic_and_synchronized(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        heartbeat = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=2,
                state="running_benchmark",
                active_agent_id=agent_id,
            ),
        )
        assert heartbeat.status_code == 200, heartbeat.text

        response = await client.get("/api/v1/public/operations")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == (
            "public, max-age=5, stale-while-revalidate=30"
        )
        snapshot = response.json()
        # No rollout row at all, so this is the floor: with nothing durable on
        # record ``persisted_active_bench_version`` answers
        # MIN_SCOREABLE_BENCH_VERSION rather than DEFAULT_BENCH_VERSION, which
        # is frozen at 2 and names an era that can no longer be scored.
        assert snapshot["active_bench_version"] == _BENCH_VERSION
        assert snapshot["desired_bench_version"] == _BENCH_VERSION
        assert snapshot["benchmark_rollout_status"] == "inactive"
        assert snapshot["generated_at"] == snapshot["activity"]["generated_at"]
        assert snapshot["generated_at"] == snapshot["validators"]["generated_at"]
        validator = snapshot["validators"]["validators"][0]
        assert validator["assignment_state"] == "synchronized"
        assert validator["assigned_agent_id"] == str(agent_id)
        assert validator["reported_agent_id"] == str(agent_id)
        assert validator["active_agent_id"] == str(agent_id)
        activity = snapshot["activity"]["entries"][0]
        assert activity["status"] == "evaluating"
        assert activity["active_benchmarks"][0]["agent_id"] == str(agent_id)

        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            rollout_id = uuid4()
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=_BENCH_VERSION,
                    desired_version=_BENCH_VERSION + 1,
                    status="collecting",
                    cohort_size=5,
                    created_at=now,
                )
            )
        rollout_snapshot = (await client.get("/api/v1/public/operations")).json()
        assert rollout_snapshot["active_bench_version"] == _BENCH_VERSION
        assert rollout_snapshot["desired_bench_version"] == _BENCH_VERSION + 1
        assert rollout_snapshot["benchmark_rollout_status"] == "collecting"

    async def test_operations_snapshot_keeps_live_work_and_bounds_history(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        active_id = await _seed_agent(
            session_maker, status=AgentStatus.SCREENING, name="active-agent"
        )
        for index in range(51):
            await _seed_agent(
                session_maker,
                status=AgentStatus.SCORED,
                name=f"scored-agent-{index}",
                created_at=datetime(2026, 7, 24, 12, index, tzinfo=UTC),
            )
        _install_db(app, session_maker)

        response = await client.get("/api/v1/public/operations")

        assert response.status_code == 200
        activity = response.json()["activity"]
        assert activity["total"] == 52
        assert activity["count"] == 51
        assert activity["status_counts"]["screening"] == 1
        assert activity["status_counts"]["scored"] == 51
        assert sum(entry["status"] == "scored" for entry in activity["entries"]) == 50
        assert any(entry["agent_id"] == str(active_id) for entry in activity["entries"])

    async def test_operations_snapshot_surfaces_different_reported_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        assigned_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        reported_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, name="reported-agent"
        )
        now = datetime.now(UTC)
        # Lease older than the hand-off grace: the validator has had ample time to
        # pick this up and is instead reporting a different agent — a real mismatch.
        await _seed_ticket(
            session_maker, assigned_id, issued_at=now - timedelta(minutes=5)
        )
        await _seed_validator_heartbeat(session_maker, protocol_version=2)
        async with session_maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert heartbeat is not None
            heartbeat.state = "running_benchmark"
            heartbeat.active_agent_id = reported_id
            heartbeat.reported_at = now
            heartbeat.seen_at = now
        _install_db(app, session_maker)

        snapshot = (await client.get("/api/v1/public/operations")).json()
        validator = snapshot["validators"]["validators"][0]
        assert validator["assignment_state"] == "assignment_mismatch"
        assert validator["assigned_agent_id"] == str(assigned_id)
        assert validator["assigned_agent_name"] == "alpha-agent"
        assert validator["reported_agent_id"] == str(reported_id)
        assert validator["active_agent_id"] is None
        assigned = next(
            entry
            for entry in snapshot["activity"]["entries"]
            if entry["agent_id"] == str(assigned_id)
        )
        assert assigned["status"] == "waiting_validator"
        assert assigned["active_benchmarks"] == []

    async def test_operations_snapshot_grace_window_reads_as_assigning(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A lease issued within the hand-off grace, before the validator has
        # reported picking it up, must read as a transient "assigning" — not a
        # mismatch — so the fleet view does not flap red between jobs.
        assigned_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        now = datetime.now(UTC)
        await _seed_ticket(
            session_maker, assigned_id, issued_at=now - timedelta(seconds=5)
        )
        await _seed_validator_heartbeat(session_maker, protocol_version=2)
        async with session_maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert heartbeat is not None
            heartbeat.state = "polling"
            heartbeat.active_agent_id = None
            heartbeat.reported_at = now
            heartbeat.seen_at = now
        _install_db(app, session_maker)

        snapshot = (await client.get("/api/v1/public/operations")).json()
        validator = snapshot["validators"]["validators"][0]
        assert validator["assignment_state"] == "assigning"
        assert validator["assigned_agent_id"] == str(assigned_id)
        assert validator["active_agent_id"] is None

    async def test_operations_snapshot_surfaces_stale_heartbeat_assignment(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        stale = datetime.now(UTC) - timedelta(minutes=10)
        await _seed_validator_heartbeat(
            session_maker, protocol_version=2, seen_at=stale
        )
        async with session_maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert heartbeat is not None
            heartbeat.state = "running_benchmark"
            heartbeat.active_agent_id = agent_id
        _install_db(app, session_maker)

        snapshot = (await client.get("/api/v1/public/operations")).json()
        validator = snapshot["validators"]["validators"][0]
        assert validator["assignment_state"] == "heartbeat_stale"
        assert validator["availability"] == "stale"
        assert validator["assigned_agent_id"] == str(agent_id)
        assert validator["reported_agent_id"] == str(agent_id)
        assert validator["active_agent_id"] is None
        assert snapshot["activity"]["entries"][0]["status"] == "waiting_validator"

    @pytest.mark.e2e
    async def test_v4_progresses_public_lifecycle_and_terminal_score_clears_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Fake build -> run -> finalize -> submit against one real ticket."""
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())
        stages = [
            _progress("preparing"),
            _progress("building_harness"),
            _progress("starting_harness"),
            _progress("running_benchmark", completed=0, total=114),
            _progress("running_benchmark", completed=51, total=114),
            _progress("finalizing", completed=114, total=114),
            _progress("submitting_result", completed=114, total=114),
        ]

        for offset, progress in enumerate(stages):
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=4,
                    timestamp=timestamp + offset,
                    state="running_benchmark",
                    active_agent_id=agent_id,
                    benchmark_progress=progress,
                ),
            )
            assert response.status_code == 200, response.text
            public = (await client.get("/api/v1/public/validators")).json()
            shown = public["validators"][0]["active_benchmark"]
            assert shown["stage"] == progress["stage"]
            assert shown["agent_id"] == str(agent_id)

            if progress["stage"] == "running_benchmark" and progress["completed"] == 51:
                started_at = datetime.fromisoformat(
                    shown.pop("started_at").replace("Z", "+00:00")
                )
                assert started_at.tzinfo == UTC
                assert shown == {
                    "agent_id": str(agent_id),
                    "agent_name": "alpha-agent",
                    "bench_version": _BENCH_VERSION,
                    "stage": "running_benchmark",
                    "completed_checks": 51,
                    "total_checks": 114,
                    "percent": 44,
                    "stalled": False,
                    # ``slot_id`` joined the public payload in 9e81a3f
                    # (2026-07-22, bounded validator capacity); see
                    # ``api_models/public.py`` where it defaults to
                    # "slot-0". This exhaustive comparison was never
                    # updated and the test has been red on BOTH dialects
                    # since -- invisible because it is ``e2e``-marked and
                    # pytest addopts deselected ``e2e``.
                    "slot_id": "slot-0",
                    "purpose": "canonical_quorum",
                }
            if progress["stage"] in {"finalizing", "submitting_result"}:
                # Only a terminal stage may report a full bar.
                assert shown["percent"] == 100
                assert shown["completed_checks"] == shown["total_checks"] == 114

        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        attempt = pipeline["validation_attempts"][0]
        assert attempt["deadline"] is not None
        assert attempt["actively_running"] is True
        assert attempt["benchmark_progress"]["stage"] == "submitting_result"

        scored = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )
        assert scored.status_code == 200, scored.text
        fleet = (await client.get("/api/v1/public/validators")).json()
        assert fleet["validators"][0]["active_agent_id"] is None
        assert fleet["validators"][0]["active_benchmark"] is None
        activity = (await client.get("/api/v1/public/activity")).json()
        assert activity["entries"][0]["active_benchmarks"] == []

    async def test_v4_fail_open_regression_omission_and_signature_tampering(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())

        initial = _heartbeat_payload(
            protocol_version=4,
            timestamp=timestamp,
            state="running_benchmark",
            active_agent_id=agent_id,
            benchmark_progress=_progress("running_benchmark", completed=51, total=114),
        )
        assert (
            await client.post(
                "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=initial
            )
        ).status_code == 200

        # A same-run regression must NOT be rejected (fail-open). The signed
        # liveness report is accepted (200) and the public display keeps the last
        # good progress (51/114) instead of moving backward.
        regressions = [
            _progress("starting_harness"),
            _progress("running_benchmark", completed=40, total=114),
            _progress("running_benchmark", completed=52, total=120),
        ]
        for offset, progress in enumerate(regressions, start=1):
            accepted = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=4,
                    timestamp=timestamp + offset,
                    state="running_benchmark",
                    active_agent_id=agent_id,
                    benchmark_progress=progress,
                ),
            )
            assert accepted.status_code == 200, accepted.text
            shown = (await client.get("/api/v1/public/validators")).json()[
                "validators"
            ][0]["active_benchmark"]
            assert shown["stage"] == "running_benchmark"
            assert shown["completed_checks"] == 51
            assert shown["total_checks"] == 114

        omitted = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp + 4,
                state="running_benchmark",
                active_agent_id=agent_id,
            ),
        )
        assert omitted.status_code == 200, omitted.text
        public_unknown = (await client.get("/api/v1/public/validators")).json()
        active_benchmark = public_unknown["validators"][0]["active_benchmark"]
        started_at = datetime.fromisoformat(
            active_benchmark.pop("started_at").replace("Z", "+00:00")
        )
        assert started_at.tzinfo == UTC
        assert active_benchmark == {
            "slot_id": "slot-0",
            "agent_id": str(agent_id),
            "agent_name": "alpha-agent",
            "bench_version": _BENCH_VERSION,
            "stage": None,
            "completed_checks": None,
            "total_checks": None,
            "percent": None,
            "stalled": False,
            "purpose": "canonical_quorum",
        }

        downgraded = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=3,
                timestamp=timestamp + 5,
                state="running_benchmark",
                active_agent_id=agent_id,
            ),
        )
        assert downgraded.status_code == 200, downgraded.text
        public_unknown = (await client.get("/api/v1/public/validators")).json()
        assert public_unknown["validators"][0]["active_benchmark"]["stage"] is None

        lower_after_downgrade = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp + 6,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress(
                    "running_benchmark", completed=50, total=114
                ),
            ),
        )
        # Fail-open: a regression after the reported flag toggled is accepted and
        # the stored progress floor is kept.
        assert lower_after_downgrade.status_code == 200, lower_after_downgrade.text

        tampered = _heartbeat_payload(
            protocol_version=4,
            timestamp=timestamp + 7,
            state="running_benchmark",
            active_agent_id=agent_id,
            benchmark_progress=_progress("running_benchmark", completed=52, total=114),
        )
        assert isinstance(tampered["benchmark_progress"], dict)
        tampered["benchmark_progress"]["completed"] = 53
        rejected = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=tampered
        )
        assert rejected.status_code == 401

        cleared = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(protocol_version=4, timestamp=timestamp + 8),
        )
        assert cleared.status_code == 200, cleared.text
        fleet = (await client.get("/api/v1/public/validators")).json()
        assert fleet["validators"][0]["active_benchmark"] is None

        lower_after_idle = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp + 9,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress(
                    "running_benchmark", completed=1, total=114
                ),
            ),
        )
        # Fail-open: accepted even though it regresses the stored floor.
        assert lower_after_idle.status_code == 200, lower_after_idle.text

        other_agent_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, name="new-agent"
        )
        async with session_maker() as session, session.begin():
            previous = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert previous is not None
            previous.status = TicketStatus.SCORED
        await _seed_ticket(session_maker, other_agent_id)
        different_agent = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp + 10,
                state="running_benchmark",
                active_agent_id=other_agent_id,
                benchmark_progress=_progress(
                    "running_benchmark", completed=1, total=114
                ),
            ),
        )
        assert different_agent.status_code == 200, different_agent.text

    async def test_v4_failed_retrying_explicitly_restarts_at_preparing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())

        sequence = [
            _progress("running_benchmark", completed=51, total=114),
            _progress("failed_retrying", completed=51, total=114),
        ]
        for offset, progress in enumerate(sequence):
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=4,
                    timestamp=timestamp + offset,
                    state="running_benchmark",
                    active_agent_id=agent_id,
                    benchmark_progress=progress,
                ),
            )
            assert response.status_code == 200, response.text

        same_lease_restart = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp + 2,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress("preparing"),
            ),
        )
        assert same_lease_restart.status_code == 200, same_lease_restart.text

        resumed = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp + 3,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress(
                    "running_benchmark", completed=1, total=114
                ),
            ),
        )
        assert resumed.status_code == 200, resumed.text

        new_deadline = _TICKET_DEADLINE + timedelta(hours=1)
        await _seed_ticket(session_maker, agent_id, deadline=new_deadline)
        restarted = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp + 4,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress("preparing", ticket_deadline=new_deadline),
            ),
        )
        assert restarted.status_code == 200, restarted.text

    async def test_v4_next_confirmation_seed_restarts_at_preparing(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Multi-seed confirmation runs several evaluations under ONE ticket lease.
        # A completed run (finalizing) followed by the next seed (preparing) must
        # rebaseline, not read as a regression — otherwise every heartbeat of the
        # next seed is rejected and the validator freezes into heartbeat_stale
        # while it is in fact scoring normally.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())

        sequence = [
            _progress("running_benchmark", completed=114, total=114),
            _progress("finalizing", completed=114, total=114),
            # Next confirmation seed in the same lease: fresh run, progress resets.
            _progress("preparing"),
            _progress("running_benchmark", completed=1, total=114),
        ]
        for offset, progress in enumerate(sequence):
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=4,
                    timestamp=timestamp + offset,
                    state="running_benchmark",
                    active_agent_id=agent_id,
                    benchmark_progress=progress,
                ),
            )
            assert response.status_code == 200, response.text

    @pytest.mark.parametrize("status", [AgentStatus.SCORED, AgentStatus.LIVE])
    async def test_v4_preserves_rollout_progress_for_scored_and_live_agents(
        self,
        status: AgentStatus,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=status)
        await _seed_ticket(session_maker, agent_id, bench_version=_BENCH_VERSION)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress(
                    "running_benchmark", completed=51, total=114
                ),
            ),
        )

        assert response.status_code == 200, response.text
        fleet = (await client.get("/api/v1/public/validators")).json()
        validator = fleet["validators"][0]
        assert validator["active_agent_id"] == str(agent_id)
        assert validator["active_benchmark"]["stage"] == "running_benchmark"
        assert validator["active_benchmark"]["bench_version"] == _BENCH_VERSION

        pipeline = (
            await client.get(f"/api/v1/public/agent/{agent_id}/pipeline")
        ).json()
        attempt = pipeline["validation_attempts"][0]
        assert attempt["bench_version"] == _BENCH_VERSION
        assert attempt["actively_running"] is True
        assert attempt["benchmark_progress"]["completed_checks"] == 51

    async def test_v4_drops_progress_for_non_scoreable_agent_with_live_ticket(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress("preparing"),
            ),
        )

        assert response.status_code == 200, response.text
        fleet = (await client.get("/api/v1/public/validators")).json()
        validator = fleet["validators"][0]
        assert validator["active_agent_id"] is None
        assert validator["active_benchmark"] is None

    async def test_v4_drops_progress_without_matching_live_ticket_but_stays_live(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())

        missing = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress("preparing"),
            ),
        )
        assert missing.status_code == 200, missing.text
        assert missing.json()["accepted"] is True

        async with session_maker() as session:
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert heartbeat is not None
            assert heartbeat.state == "running_benchmark"
            assert heartbeat.active_agent_id is None
            assert heartbeat.benchmark_progress_reported is False

        await _seed_ticket(session_maker, agent_id)
        wrong_deadline = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp + 1,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress(
                    "preparing", ticket_deadline=_TICKET_DEADLINE + timedelta(days=1)
                ),
            ),
        )
        assert wrong_deadline.status_code == 200, wrong_deadline.text
        assert wrong_deadline.json()["accepted"] is True

        async with session_maker() as session:
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert heartbeat is not None
            assert heartbeat.active_agent_id is None
            assert heartbeat.benchmark_progress_reported is False
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == TicketStatus.ISSUED
            assert ticket.deadline.replace(tzinfo=UTC) == _TICKET_DEADLINE

    async def test_v4_expired_ticket_progress_cannot_block_heartbeat_recovery(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        deadline = datetime.now(UTC) - timedelta(minutes=1)
        await _seed_ticket(session_maker, agent_id, deadline=deadline)
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())

        recovered = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=4,
                timestamp=timestamp,
                state="running_benchmark",
                active_agent_id=agent_id,
                benchmark_progress=_progress(
                    "running_benchmark",
                    completed=51,
                    total=114,
                    ticket_deadline=deadline,
                ),
            ),
        )

        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["accepted"] is True
        fleet = (await client.get("/api/v1/public/validators")).json()
        validator = fleet["validators"][0]
        assert validator["availability"] == "available"
        assert validator["online"] is True
        assert validator["active_agent_id"] is None
        assert validator["active_benchmark"] is None

        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert ticket is not None
            assert ticket.status == TicketStatus.ISSUED
            assert ticket.deadline.replace(tzinfo=UTC) == deadline
            assert heartbeat is not None
            assert heartbeat.seen_at is not None
            assert heartbeat.active_agent_id is None
            assert heartbeat.benchmark_progress is None
            assert heartbeat.benchmark_progress_reported is False

    async def test_v3_binds_coarse_metrics_and_public_response_is_redacted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """v3 binds the coarse metrics it sent, and the public copy is redacted.

        Both halves are unchanged. What changed is the health badge that used to
        ride along: a protocol-3 heartbeat cannot advertise v7 -- the floor for
        that is protocol 12 -- and with no rollout row the era is now the
        scoreable floor rather than ``DEFAULT_BENCH_VERSION``. The public view
        grades a validator that cannot serve the era being scored ``critical``
        ahead of any host-metric reading, so "healthy" here would now only be
        asserting that v2 is still live. The metrics binding and the redaction,
        which is what this test is for, are untouched.
        """
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())
        metrics = {**_SYSTEM_METRICS, "collected_at": timestamp}
        payload = _heartbeat_payload(
            protocol_version=3, timestamp=timestamp, system_metrics=metrics
        )
        response = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=payload
        )
        assert response.status_code == 200, response.text

        public = (await client.get("/api/v1/public/validators")).json()
        entry = public["validators"][0]
        assert entry["availability"] == "available"
        assert entry["health"] == "critical"
        # Obsolete software, not a sick host: the reason has to say which.
        assert entry["health_reasons"] == [
            f"software too old for bench v{_BENCH_VERSION} (heartbeat protocol 3)"
        ]
        assert entry["first_seen_at"] is not None
        assert entry["system_metrics"] == {
            "cpu_percent": 15,
            "memory_percent": 40,
            "disk_percent": 55,
            "docker_status": "healthy",
            "running_containers": 4,
            "unhealthy_containers": 0,
        }
        assert "signature" not in entry
        assert "code_digest" not in entry
        for forbidden in (
            "hostname",
            "ip",
            "instance_id",
            "path",
            "container_name",
            "image_digest",
        ):
            assert forbidden not in str(entry).lower()

    async def test_v3_rejects_tampering_and_ignores_additive_metrics(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        timestamp = int(datetime.now(UTC).timestamp())
        metrics = {**_SYSTEM_METRICS, "collected_at": timestamp}
        payload = _heartbeat_payload(
            protocol_version=3, timestamp=timestamp, system_metrics=metrics
        )
        payload["system_metrics"]["memory_percent"] = 90  # type: ignore[index]
        tampered = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=payload
        )
        assert tampered.status_code == 401

        malformed = _heartbeat_payload(
            protocol_version=3, timestamp=timestamp, system_metrics=metrics
        )
        malformed["system_metrics"]["hostname"] = "private"  # type: ignore[index]
        accepted = await client.post(
            "/api/v1/validator/heartbeat", headers=_AUTH_HEADER, json=malformed
        )
        assert accepted.status_code == 200, accepted.text

    async def test_heartbeat_payload_size_is_bounded(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers={**_AUTH_HEADER, "Content-Length": str(16 * 1024 + 1)},
            json=_heartbeat_payload(),
        )
        assert response.status_code == 413

        payload = json.dumps(_heartbeat_payload())
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers={**_AUTH_HEADER, "Content-Type": "application/json"},
            content=(" " * (16 * 1024 + 1)) + payload,
        )
        assert response.status_code == 413

    async def test_rejects_stale_heartbeat(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        stale = int(datetime.now(UTC).timestamp()) - 301
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(timestamp=stale),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    @pytest.mark.e2e
    async def test_mixed_fleet_and_malformed_telemetry(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Exercise reporter ingestion through both public fleet views."""
        _install_db(app, session_maker)
        _install_chain(app)
        now = datetime.now(UTC)
        timestamp = int(now.timestamp())
        metrics = {**_SYSTEM_METRICS, "collected_at": timestamp}

        old_validator = await client.post(
            "/api/v1/validator/heartbeat",
            headers={"X-Validator-Hotkey": _KEYPAIRS[1].ss58_address},
            json=_heartbeat_payload(keypair=_KEYPAIRS[1], protocol_version=2),
        )
        assert old_validator.status_code == 200, old_validator.text

        metric_validator = await client.post(
            "/api/v1/validator/heartbeat",
            headers={"X-Validator-Hotkey": _KEYPAIRS[2].ss58_address},
            json=_heartbeat_payload(
                keypair=_KEYPAIRS[2],
                protocol_version=3,
                timestamp=timestamp,
                system_metrics=metrics,
            ),
        )
        assert metric_validator.status_code == 200, metric_validator.text

        screener_headers = {
            "Authorization": "Bearer test-screener-token-at-least-32-characters",
            "X-Screener-Hotkey": _KEYPAIR.ss58_address,
        }
        healthy_screener = await client.post(
            "/api/v1/screener/heartbeat",
            headers=screener_headers,
            json=_screener_heartbeat_payload(
                timestamp=timestamp, system_metrics=metrics
            ),
        )
        assert healthy_screener.status_code == 200, healthy_screener.text

        stale_at = now - timedelta(minutes=10)
        async with session_maker() as session, session.begin():
            session.add(
                ScreenerHeartbeat(
                    screener_hotkey=_DAVE.ss58_address,
                    software_version="0.4.1",
                    protocol_version=1,
                    policy_version=SCREENING_POLICY_VERSION,
                    state="polling",
                    active_agent_id=None,
                    first_seen_at=stale_at - timedelta(hours=2),
                    system_metrics=metrics,
                    reported_at=stale_at,
                    seen_at=stale_at,
                    signature="ab" * 64,
                )
            )

        additive = _heartbeat_payload(
            keypair=_KEYPAIRS[2],
            protocol_version=3,
            timestamp=timestamp,
            system_metrics=metrics,
        )
        additive_metrics = additive["system_metrics"]
        assert isinstance(additive_metrics, dict)
        additive_metrics["hostname"] = "must-never-be-published"
        accepted = await client.post(
            "/api/v1/validator/heartbeat",
            headers={"X-Validator-Hotkey": _KEYPAIRS[2].ss58_address},
            json=additive,
        )
        assert accepted.status_code == 200, accepted.text

        validators = (await client.get("/api/v1/public/validators")).json()
        assert validators["reported_count"] == 2
        old = next(v for v in validators["validators"] if v["protocol_version"] == 2)
        current = next(
            v for v in validators["validators"] if v["protocol_version"] == 3
        )
        assert old["availability"] == "available"
        assert old["system_metrics"] is None
        assert current["availability"] == "available"
        assert "hostname" not in json.dumps(current)
        # Both are legacy reporters (protocol 2 and 3) and the floor for
        # advertising v7 is protocol 12, so neither can serve the era being
        # scored and both are graded on that before their host metrics are
        # considered at all. The metric-driven split this pair used to show --
        # "unknown" with nothing reported, "healthy" with good numbers -- is no
        # longer expressible by any protocol old enough to omit them. What the
        # metrics still decide is whether they are PUBLISHED, which is asserted
        # above and is the redaction claim this test carries.
        assert old["health"] == "critical"
        assert current["health"] == "critical"

        screeners = (await client.get("/api/v1/public/screeners")).json()
        assert screeners["reported_count"] == 2
        available = next(s for s in screeners["screeners"] if s["online"])
        stale = next(s for s in screeners["screeners"] if not s["online"])
        assert available["availability"] == "available"
        assert available["health"] == "healthy"
        assert stale["availability"] == "stale"

    async def test_rejects_tampered_digest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _heartbeat_payload()
        payload["code_digest"] = "cd" * 32
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=payload,
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_rejects_tampered_runtime_state(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _heartbeat_payload(state="idle")
        payload["state"] = "running_benchmark"
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=payload,
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH


@contextlib.contextmanager
def _capture_artifact_audit_logs(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[None]:
    """Capture the concrete audit logger even if another test replaced root handlers."""
    audit_logger = logging.getLogger("ditto.db.queries.artifact_fetch_audit")
    was_disabled = audit_logger.disabled
    audit_logger.disabled = False
    audit_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger=audit_logger.name):
            yield
    finally:
        audit_logger.removeHandler(caplog.handler)
        audit_logger.disabled = was_disabled


class TestArtifactFetchAudit:
    """Every served artifact must leave a durable, attributable row."""

    async def test_fetch_writes_an_audit_row(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, bench_version=_BENCH_VERSION)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)

        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact",
            headers=_artifact_headers(agent_id),
        )

        assert response.status_code == 200
        async with session_maker() as s:
            rows = (await s.scalars(select(ArtifactFetchAudit))).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.agent_id == agent_id
        assert row.endpoint == "validator.agent_artifact"
        assert row.requester_kind == "validator"
        # The whole point: the fetch is attributable to a specific hotkey.
        assert row.requester_id == _KEYPAIR.ss58_address
        assert row.artifact_sha256 == _SHA256
        assert row.bench_version == _BENCH_VERSION
        assert row.fetched_at is not None

    async def test_each_fetch_appends_its_own_row(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Per-fetch history survives a ticket UPSERT.

        ``validator_tickets`` is keyed by (agent_id, bench_version, hotkey) and
        is overwritten on reissue, so a claim/fail/reclaim loop collapses into a
        single row. The audit trail is what preserves that each hand-off of the
        source actually happened, which is the record the leak investigation
        did not have.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, bench_version=_BENCH_VERSION)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)

        for _ in range(3):
            # A fresh nonce each time: the same lease, fetched repeatedly.
            response = await client.get(
                f"/api/v1/validator/agent/{agent_id}/artifact",
                headers=_artifact_headers(agent_id),
            )
            assert response.status_code == 200

        async with session_maker() as s:
            rows = (await s.scalars(select(ArtifactFetchAudit))).all()
        assert len(rows) == 3
        assert {r.seq for r in rows} == {r.seq for r in rows}

    async def test_audit_failure_does_not_deny_the_artifact(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A logging fault must never become a scoring outage.

        The validator has already proved hotkey possession, passed the chain
        permit check and burned its nonce by this point. Refusing the artifact
        because a bookkeeping INSERT failed would take the fleet down over a
        full disk. Fail open, and make the gap loud instead of silent.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, bench_version=_BENCH_VERSION)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)

        def _explode(**_kwargs: object) -> ArtifactFetchAudit:
            raise RuntimeError("audit table is on fire")

        # Break the row write itself, not the helper that wraps it, so this
        # exercises the real fail-open path rather than a stubbed one.
        monkeypatch.setattr(
            "ditto.db.queries.artifact_fetch_audit.ArtifactFetchAudit",
            _explode,
        )
        with _capture_artifact_audit_logs(caplog):
            response = await client.get(
                f"/api/v1/validator/agent/{agent_id}/artifact",
                headers=_artifact_headers(agent_id),
            )

        # The artifact is still served, in full.
        assert response.status_code == 200
        body = response.json()
        assert body["download_url"].startswith("https://")
        assert body["sha256"] == _SHA256
        # ...and the gap is loud, not silent: this is the string alerting keys on.
        assert AUDIT_WRITE_FAILED in caplog.text
        async with session_maker() as s:
            assert (
                await s.scalar(select(func.count()).select_from(ArtifactFetchAudit))
            ) == 0

    async def test_audit_write_failure_is_swallowed_and_logged(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The helper itself never raises, and never fails silently."""
        # Reproduce the xdist order where earlier logging configuration left
        # this already-imported named logger disabled in the worker.
        monkeypatch.setattr(
            logging.getLogger("ditto.db.queries.artifact_fetch_audit"),
            "disabled",
            True,
        )
        with _capture_artifact_audit_logs(caplog):
            async with session_maker() as s:
                wrote = await record_artifact_fetch(
                    s,
                    agent_id=uuid4(),
                    endpoint="validator.agent_artifact",
                    requester_kind="validator",
                    # Violates the requester_id presence CHECK for a non-public
                    # kind, so the INSERT fails inside the database.
                    requester_id=None,
                )

        assert wrote is False
        assert AUDIT_WRITE_FAILED in caplog.text


class TestArtifact:
    async def test_returns_presigned_url_and_sha(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The source-artifact half of this endpoint, so the submission is seeded
        # WITHOUT a screened image on purpose. ``_seed_agent`` provides one by
        # default now (every contract from v3 up requires it), which would make
        # the two screened-image assertions below unreachable -- the sibling
        # test covers that response shape.
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, screened_image=False
        )
        await _seed_ticket(session_maker, agent_id, bench_version=_BENCH_VERSION)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)

        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact",
            headers=_artifact_headers(agent_id),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == str(agent_id)
        assert body["sha256"] == _SHA256
        assert body["download_url"].startswith("https://")
        assert body["screened_image_url"] is None
        assert body["screened_image_sha256"] is None
        assert body["bench_version"] == _BENCH_VERSION
        storage.presigned_get_url.assert_awaited_once()
        assert (
            storage.presigned_get_url.await_args.kwargs["key"]
            == f"{agent_id}/agent.tar.gz"
        )

    async def test_a_never_disclose_policy_still_serves_a_ticketed_validator(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Under `never`, submissions are scored exactly as before.

        Same deliberate choice as the screener path, same reason:
        `disclosure = never` governs the public release route, not who may
        execute the code. A validator that could not fetch the tarball could
        not produce a k=3 score, and the subnet would stop paying anyone.
        """
        # No screened image on purpose: this test is about fetching the SOURCE
        # TARBALL under `disclosure = never`, and it asserts that exact key
        # below. `_seed_agent` now supplies a verified screened image unless
        # asked not to, which would quietly move the assertion onto the image
        # path and stop testing the tarball the docstring is about.
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, screened_image=False
        )
        await _seed_ticket(session_maker, agent_id, bench_version=3)
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

        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact",
            headers=_artifact_headers(agent_id),
        )
        assert response.status_code == 200
        assert response.json()["agent_id"] == str(agent_id)
        assert (
            storage.presigned_get_url.await_args.kwargs["key"]
            == f"{agent_id}/agent.tar.gz"
        )

    async def test_returns_verified_screened_image_fields(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, bench_version=_BENCH_VERSION)
        upload_id = uuid4()
        async with session_maker() as session, session.begin():
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.screened_image_sha256 = "12" * 32
            agent.screened_image_size_bytes = 123
            agent.screened_image_id = "sha256:" + "34" * 32
            agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
            agent.screened_image_upload_id = upload_id
            agent.screened_image_verified_at = datetime.now(UTC)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)

        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact",
            headers=_artifact_headers(agent_id),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["screened_image_url"].startswith("https://")
        assert body["screened_image_sha256"] == "12" * 32
        assert body["screened_image_size_bytes"] == 123
        assert body["screened_image_id"] == "sha256:" + "34" * 32
        assert body["screened_image_ref"] == f"ditto-screen/{agent_id}:latest"
        assert body["bench_version"] == _BENCH_VERSION
        assert storage.presigned_get_url.await_args_list[1].kwargs["key"] == (
            f"{agent_id}/screened-images/{upload_id}.tar"
        )

    async def test_without_open_ticket_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)

        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact",
            headers=_artifact_headers(agent_id),
        )
        assert response.status_code == 409
        assert "no open scoring ticket" in response.json()["message"]
        storage.presigned_get_url.assert_not_awaited()

    async def test_expired_ticket_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(
            session_maker,
            agent_id,
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)

        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact",
            headers=_artifact_headers(agent_id),
        )
        assert response.status_code == 409
        storage.presigned_get_url.assert_not_awaited()

    async def test_ticket_for_other_validator_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        other = bittensor.Keypair.create_from_uri("//Dave")
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, keypair=other)
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)

        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact",
            headers=_artifact_headers(agent_id),
        )
        assert response.status_code == 409
        storage.presigned_get_url.assert_not_awaited()

    async def test_unknown_agent_returns_404(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock()
        agent_id = uuid4()
        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact",
            headers=_artifact_headers(agent_id),
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_FOUND

    async def test_public_validator_identity_without_signature_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)

        response = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact", headers=_AUTH_HEADER
        )

        assert response.status_code == 401

    async def test_replayed_artifact_proof_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        headers = _artifact_headers(agent_id)

        first = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact", headers=headers
        )
        replay = await client.get(
            f"/api/v1/validator/agent/{agent_id}/artifact", headers=headers
        )

        assert first.status_code == 200
        assert replay.status_code == 409


# --- Submit score ----------------------------------------------------------


class TestRequestJob:
    @pytest.fixture(autouse=True)
    async def _current_era(
        self, app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Put the fleet on the era these tests lease work in.

        Two things that used to come for free at v2 have to be stated now.
        ``active_bench_version`` answers ``DEFAULT_BENCH_VERSION`` (2) when no
        rollout row exists, and 2 is beneath the ticket floor, so the allocator
        cuts a lease the database refuses -- a 500 with nothing in it about
        benchmarks. And every v7 lease must carry an inference grant, so the
        routing policy has to exist before a job can be issued at all.

        The third precondition -- a heartbeat the fleet counts as v7-capable --
        is deliberately NOT seeded here: several tests in these classes are
        about the absence or the wrong shape of that heartbeat. Tests that want
        a job issued call ``_seed_capable_pool``.
        """
        await _seed_activated_era(session_maker)
        await _install_ticket_inference(app, session_maker)

    async def test_paused_validator_gets_no_new_lease_while_peer_keeps_working(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_capable_pool(session_maker, keypairs=_KEYPAIRS[:2])
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorSlotSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "max_concurrent_slots": 2,
                        "disk_percent_ceiling": 90,
                        "memory_percent_ceiling": 90,
                        "cpu_percent_ceiling": 0,
                        "resource_block_percent_ceiling": 95,
                        "paused_validator_hotkeys": [_VALIDATOR_HOTKEY],
                    },
                    checksum="d" * 64,
                    reason="drain one unhealthy validator",
                    actor="backroom:test",
                )
            )
        app.state.validator_slot_settings.invalidate()
        app.state.session_maker = session_maker
        _install_db(app, session_maker)
        _install_chain(app)

        paused = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )
        assert paused.status_code == 204, paused.text
        async with session_maker() as session:
            assert (
                await session.get(
                    ValidatorTicket,
                    (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY),
                )
                is None
            )

        peer = _KEYPAIRS[1]
        served = await client.post(
            "/api/v1/validator/job",
            headers={"X-Validator-Hotkey": peer.ss58_address},
            json=_job_payload(peer, slot_id=_SLOT_ID),
        )
        assert served.status_code == 200, served.text
        assert served.json()["agent_id"] == str(agent_id)

    async def test_paused_validator_can_resume_its_live_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(
            session_maker,
            agent_id,
            deadline=datetime.now(UTC) + timedelta(minutes=30),
            slot_id=_SLOT_ID,
        )
        await _seed_capable_pool(session_maker, keypairs=(_KEYPAIR,))
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorSlotSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "max_concurrent_slots": 2,
                        "disk_percent_ceiling": 90,
                        "memory_percent_ceiling": 90,
                        "cpu_percent_ceiling": 0,
                        "resource_block_percent_ceiling": 95,
                        "paused_validator_hotkeys": [_VALIDATOR_HOTKEY],
                    },
                    checksum="e" * 64,
                    reason="drain after the current lease",
                    actor="backroom:test",
                )
            )
        app.state.validator_slot_settings.invalidate()
        app.state.session_maker = session_maker
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(agent_id)
        assert response.json()["slot_id"] == _SLOT_ID

    async def test_busy_dispatch_fence_returns_immediately_and_retries_same_nonce(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A forced allocator interleaving neither waits nor burns the claim."""
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_capable_pool(session_maker, keypairs=(_KEYPAIR,))
        _install_db(app, session_maker)
        _install_chain(app)
        claim = _job_payload(slot_id=_SLOT_ID)
        nonce = UUID(claim["nonce"])

        async with session_maker() as holder:
            await holder.begin()
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": ROLLOUT_DISPATCH_LOCK_KEY},
            )
            response = await asyncio.wait_for(
                client.post("/api/v1/validator/job", headers=_AUTH_HEADER, json=claim),
                timeout=0.5,
            )
            assert response.status_code == 204
            async with session_maker() as probe:
                assert await probe.get(ValidatorRequestNonce, nonce) is None
                assert (
                    await probe.scalar(
                        select(func.count())
                        .select_from(ValidatorTicket)
                        .where(ValidatorTicket.agent_id == agent_id)
                    )
                    == 0
                )
            await holder.rollback()

        retry = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=claim
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["agent_id"] == str(agent_id)
        async with session_maker() as probe:
            assert await probe.get(ValidatorRequestNonce, nonce) is not None
            assert (
                await probe.scalar(
                    select(func.count())
                    .select_from(ValidatorTicket)
                    .where(
                        ValidatorTicket.agent_id == agent_id,
                        ValidatorTicket.validator_hotkey == _VALIDATOR_HOTKEY,
                    )
                )
                == 1
            )

    async def test_source_backfill_declines_while_the_new_era_has_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Previous-generation work is strictly last, fleet-wide.

        Reaching this helper only means the polling validator's own new-era
        lanes came back empty, which happens constantly while the queue is deep.
        Leasing a v6 artifact on that evidence takes a slot away from a v7 queue
        the validator simply could not see.

        Scoped to an OPEN rollout (``active_version`` is still the source era),
        because that is the only shape where the drain gate is the deciding one.
        Once v7 activates the era gate below refuses first, and always.
        """
        now = datetime.now(UTC)
        rollout = MagicMock(
            rollout_id=uuid4(),
            from_version=_BENCH_VERSION,
            desired_version=_BENCH_VERSION + 1,
            cohort_size=10,
        )
        session = AsyncMock()
        session.get_bind = MagicMock(
            return_value=MagicMock(dialect=MagicMock(name="sqlite"))
        )
        # No live lease on this slot, so the helper reaches new admission.
        session.scalar = AsyncMock(return_value=None)
        issue = AsyncMock(return_value=MagicMock())
        outstanding = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.heartbeat_supports_version",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.rollout_cohort_complete",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr("ditto.api_server.endpoints.validator.issue_ticket", issue)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.desired_era_work_outstanding",
            outstanding,
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator._desired_era_capable_hotkeys",
            AsyncMock(return_value={"validator-a"}),
        )

        blocked = await _issue_source_backfill_ticket(
            session,
            rollout=rollout,
            heartbeat=MagicMock(),
            validator_hotkey="validator-a",
            now=now,
            active_version=_BENCH_VERSION,
            artifact_mode="screened_only",
            validator_running_benchmark=False,
            slot_id="slot-1",
            carryover_settings=PrevGenCarryoverSettings(enabled=True),
        )

        assert blocked is None
        issue.assert_not_awaited()
        outstanding.assert_awaited()

    async def test_source_backfill_master_switch_disables_resume_and_admission(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The carryover switch closes both previous-generation lanes."""
        now = datetime.now(UTC)
        rollout = MagicMock(
            rollout_id=uuid4(),
            from_version=_BENCH_VERSION,
            desired_version=_BENCH_VERSION + 1,
            cohort_size=10,
        )
        session = AsyncMock()
        supports = MagicMock(return_value=True)
        issue = AsyncMock(return_value=MagicMock())
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.heartbeat_supports_version",
            supports,
        )
        monkeypatch.setattr("ditto.api_server.endpoints.validator.issue_ticket", issue)

        ticket = await _issue_source_backfill_ticket(
            session,
            rollout=rollout,
            heartbeat=MagicMock(),
            validator_hotkey="validator-a",
            now=now,
            active_version=_BENCH_VERSION,
            artifact_mode="screened_only",
            validator_running_benchmark=True,
            slot_id="slot-6",
            carryover_settings=PrevGenCarryoverSettings(enabled=False),
        )

        assert ticket is None
        supports.assert_not_called()
        issue.assert_not_awaited()
        session.scalar.assert_not_awaited()

    async def test_source_backfill_never_tickets_a_retired_era(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After activation the source era is not last, it is closed.

        Every other gate is deliberately wide open here -- the desired era is
        drained, the cohort is complete, the heartbeat serves v6 -- so the only
        thing that can decline is the era itself. This is the shape the fleet
        was actually in when three of one validator's four slots were running
        v6 benchmarks whose scores no quorum would ever accept.
        """
        now = datetime.now(UTC)
        rollout = MagicMock(
            rollout_id=uuid4(), from_version=6, desired_version=7, cohort_size=10
        )
        session = AsyncMock()
        session.get_bind = MagicMock(
            return_value=MagicMock(dialect=MagicMock(name="sqlite"))
        )
        session.scalar = AsyncMock(return_value=None)
        issue = AsyncMock(return_value=MagicMock())
        outstanding = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.heartbeat_supports_version",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.rollout_cohort_complete",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr("ditto.api_server.endpoints.validator.issue_ticket", issue)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.desired_era_work_outstanding",
            outstanding,
        )

        blocked = await _issue_source_backfill_ticket(
            session,
            rollout=rollout,
            heartbeat=MagicMock(),
            validator_hotkey="validator-a",
            now=now,
            active_version=7,
            artifact_mode="screened_only",
            validator_running_benchmark=False,
            slot_id="slot-1",
            carryover_settings=PrevGenCarryoverSettings(enabled=True),
        )

        assert blocked is None
        issue.assert_not_awaited()
        # Refused on the era, before the priority question is even asked.
        outstanding.assert_not_awaited()

    async def test_source_backfill_honors_relaxed_open_rollout_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit interleave policy also opens source-version backfill.

        Carryover and source backfill are the two previous-generation lanes.
        Requiring the full cohort unconditionally here made the shared
        ``require_cohort_complete=False`` operator setting effective for only
        the adopted lane, leaving legitimate source work idle through the
        entire rollout.
        """
        now = datetime.now(UTC)
        rollout = MagicMock(
            rollout_id=uuid4(),
            from_version=_BENCH_VERSION,
            desired_version=_BENCH_VERSION + 1,
            cohort_size=15,
        )
        session = AsyncMock()
        session.get_bind = MagicMock(
            return_value=MagicMock(dialect=MagicMock(name="sqlite"))
        )
        # No live source lease and no active source backfill consume the one-slot
        # floor. The compatible heartbeat list may be empty in this focused
        # helper test; capacity=1 is the intentional minimum.
        session.scalar = AsyncMock(side_effect=(None, 0))
        session.scalars = AsyncMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        issued = MagicMock()
        issue = AsyncMock(return_value=issued)
        complete = AsyncMock(return_value=False)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.heartbeat_supports_version",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.rollout_cohort_complete", complete
        )
        monkeypatch.setattr("ditto.api_server.endpoints.validator.issue_ticket", issue)

        ticket = await _issue_source_backfill_ticket(
            session,
            rollout=rollout,
            heartbeat=MagicMock(),
            validator_hotkey="validator-a",
            now=now,
            active_version=_BENCH_VERSION,
            artifact_mode="screened_only",
            validator_running_benchmark=False,
            slot_id="slot-1",
            carryover_settings=PrevGenCarryoverSettings(
                enabled=True,
                require_cohort_complete=False,
                require_desired_era_drained=False,
            ),
        )

        assert ticket is issued
        complete.assert_not_awaited()
        issue.assert_awaited_once()

    async def test_no_operator_setting_can_reopen_the_retired_era_backfill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """There is no field left that turns a retired era back on.

        This test used to assert the opposite: one field
        (``allow_retired_era_backfill``) re-opened the lane, so the fix was
        undoable without a deploy. That knob was reachable from Backroom, which
        made "v6 is closed" a statement about a default rather than about the
        system. v2-v6 are retired for good, so the widest carryover settings the
        model can still express must not move this lane at all.
        """
        now = datetime.now(UTC)
        rollout = MagicMock(
            rollout_id=uuid4(), from_version=6, desired_version=7, cohort_size=10
        )
        session = AsyncMock()
        session.get_bind = MagicMock(
            return_value=MagicMock(dialect=MagicMock(name="sqlite"))
        )
        session.scalar = AsyncMock(return_value=None)
        session.scalars = AsyncMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        issued = MagicMock()
        issue = AsyncMock(return_value=issued)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.heartbeat_supports_version",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.rollout_cohort_complete",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr("ditto.api_server.endpoints.validator.issue_ticket", issue)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.desired_era_work_outstanding",
            AsyncMock(return_value=False),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator._desired_era_capable_hotkeys",
            AsyncMock(return_value={"validator-a"}),
        )

        ticket = await _issue_source_backfill_ticket(
            session,
            rollout=rollout,
            heartbeat=MagicMock(),
            validator_hotkey="validator-a",
            now=now,
            active_version=7,
            artifact_mode="screened_only",
            validator_running_benchmark=False,
            slot_id="slot-1",
            carryover_settings=PrevGenCarryoverSettings(
                enabled=True,
                include_exhausted=True,
                dedupe_scope="none",
                require_cohort_complete=False,
                require_desired_era_drained=False,
            ),
        )

        assert ticket is None
        issue.assert_not_awaited()

    async def test_source_backfill_will_not_resume_a_retired_era_lease(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resumption is not exempt from the floor. This was the live v6 hole.

        The inverse of this test used to pass, and the reasoning behind it was
        sound in isolation: refusing to resume a running benchmark strands the
        lease and burns a retry attempt for a submission that did nothing
        wrong. The problem is what "resume" turned into once the era retired.

        The resume lookup sat ABOVE the retired-era gate, and ``request_job``
        resurrects the ACTIVATED v7 rollout to feed this lane -- and that row's
        ``from_version`` is 6. So an unexpired v6 lease re-issued itself on
        every poll, with no rollout open and no flag set, and the score at the
        end of it was validated against ``ticket.bench_version`` rather than
        the active version. v6 was scoreable in production the whole time.

        A lease for an era that can no longer be scored is not work in
        progress. The score it is heading toward will be refused by the ledger
        (410, ``ERROR_CODE_BENCH_VERSION_RETIRED``), so resuming it only holds
        a slot the live era does not get. In-flight leases now drain instead:
        they expire at their own deadline, at most ~90 minutes out, and nothing
        re-leases them.
        """
        now = datetime.now(UTC)
        rollout = MagicMock(
            rollout_id=uuid4(), from_version=6, desired_version=7, cohort_size=10
        )
        session = AsyncMock()
        session.get_bind = MagicMock(
            return_value=MagicMock(dialect=MagicMock(name="sqlite"))
        )
        issued = MagicMock()
        issue = AsyncMock(return_value=issued)
        outstanding = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.heartbeat_supports_version",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.rollout_cohort_complete",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr("ditto.api_server.endpoints.validator.issue_ticket", issue)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.desired_era_work_outstanding",
            outstanding,
        )

        ticket = await _issue_source_backfill_ticket(
            session,
            rollout=rollout,
            heartbeat=MagicMock(),
            validator_hotkey="validator-a",
            now=now,
            active_version=7,
            artifact_mode="screened_only",
            validator_running_benchmark=True,
            slot_id="slot-1",
            carryover_settings=PrevGenCarryoverSettings(enabled=True),
        )

        assert ticket is None
        issue.assert_not_awaited()
        # Refused on the era, before the resume lookup or the priority question.
        outstanding.assert_not_awaited()

    async def test_source_backfill_waits_for_top_ten_then_reuses_v6_allocator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        rollout = MagicMock(
            from_version=_BENCH_VERSION,
            desired_version=_BENCH_VERSION + 1,
            cohort_size=10,
        )
        heartbeat = MagicMock()
        session = AsyncMock()
        session.get_bind = MagicMock(
            return_value=MagicMock(dialect=MagicMock(name="sqlite"))
        )
        complete = AsyncMock(side_effect=(False, True))
        issued = MagicMock()
        issue = AsyncMock(return_value=issued)
        supports_version = MagicMock(return_value=True)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.heartbeat_supports_version",
            supports_version,
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.rollout_cohort_complete", complete
        )
        monkeypatch.setattr("ditto.api_server.endpoints.validator.issue_ticket", issue)

        blocked = await _issue_source_backfill_ticket(
            session,
            rollout=rollout,
            heartbeat=heartbeat,
            validator_hotkey="validator-a",
            now=now,
            active_version=_BENCH_VERSION,
            artifact_mode="screened_only",
            validator_running_benchmark=False,
            slot_id="slot-1",
            carryover_settings=PrevGenCarryoverSettings(enabled=True),
        )
        assert blocked is None
        issue.assert_not_awaited()

        ticket = await _issue_source_backfill_ticket(
            session,
            rollout=rollout,
            heartbeat=heartbeat,
            validator_hotkey="validator-a",
            now=now,
            active_version=_BENCH_VERSION,
            artifact_mode="screened_only",
            validator_running_benchmark=False,
            slot_id="slot-1",
            carryover_settings=PrevGenCarryoverSettings(enabled=True),
        )
        assert ticket is issued
        supports_version.assert_any_call(heartbeat, now=now, version=_BENCH_VERSION)
        issue.assert_awaited_once_with(
            session,
            validator_hotkey="validator-a",
            now=now,
            ttl=timedelta(minutes=180),
            bench_version=_BENCH_VERSION,
            artifact_mode="screened_only",
            validator_running_benchmark=False,
            slot_id="slot-1",
            # The retired-era lane answers to the same operator ceiling as the
            # desired-era one; a backfill slot is still fleet capacity.
            owner_concurrent_submission_limit=2,
            # And to the same similarity budget, for the same reason: a family
            # that can spill onto the retired-era lane has simply moved the
            # monopoly rather than lost it. This call site names no policy, so
            # the helper's default (the rail off) is what reaches the allocator.
            similarity_policy=None,
            similarity_concurrent_submission_limit=1,
            efficiency_config=None,
        )

    async def test_activated_v7_retires_v6_permanently(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """v6 is retired, and there is no longer a knob that un-retires it.

        The rollout below has ACTIVATED, so no v6 score will ever join a quorum
        again, and every ticket the source-backfill lane could issue here is a
        validator slot spent on a number nobody will read.

        This test used to continue past the assertions below: it turned on
        ``allow_retired_era_backfill`` and asserted the whole v6 contract came
        back -- lease, resume, quorum, a finalized v6 ledger entry. That half is
        gone with the setting. It was the proof that "v6 is closed" was a
        statement about a default rather than about the system, which is exactly
        what the floor replaced. What survives is the half that is still true,
        and now permanently: the lane issues nothing, and nothing can ask it to.
        """
        now = datetime.now(UTC)
        cohort = (
            await _seed_top5_emission_set(
                session_maker,
                bench_version=7,
                seed_heartbeats=False,
            )
        )[:5]
        source_agent = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="waiting-v6",
            created_at=now - timedelta(days=1),
        )
        capabilities = {
            **_V7_CAPABILITIES,
            "require_screened_image": True,
            "source_build_fallback": False,
            "ticket_inference": True,
            "signed_score_quorum": True,
            "scorer_benchmarks": {
                "status": "fresh_verified",
                "supported_bench_versions": [6, 7],
                "observed_at": int(now.timestamp()),
                "software_version": "1.2.2",
                "source_revision": "2" * 40,
                "v7_calibration": {
                    "manifest_sha256": "c" * 64,
                    "supported_routes": [
                        {
                            "provider": "openrouter",
                            "profile_revision": "openrouter-route-test-v1",
                            "model": "openai/gpt-oss-20b",
                        }
                    ],
                },
            },
        }
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=12,
            capabilities=capabilities,
            stack=_V7_STACK,
        )
        source_only_capabilities = {
            **capabilities,
            "ticket_inference": False,
            "scorer_benchmarks": {
                "status": "fresh_verified",
                "supported_bench_versions": [6],
                "observed_at": int(now.timestamp()),
                "software_version": "1.2.2",
                "source_revision": "2" * 40,
            },
        }
        await _seed_validator_heartbeat(
            session_maker,
            keypair=_DAVE,
            protocol_version=12,
            capabilities=source_only_capabilities,
            stack=_V7_STACK,
        )
        # The v6 -> v7 activation is already on record -- the autouse
        # ``_current_era`` fixture seeds it and (from_version, desired_version)
        # is unique -- so attach the cohort to that row instead of inserting a
        # duplicate transition.
        async with session_maker() as probe:
            rollout_id = await probe.scalar(
                select(BenchmarkRollout.rollout_id).where(
                    BenchmarkRollout.desired_version == _BENCH_VERSION
                )
            )
        assert rollout_id is not None
        # The v6 rows this test needs are HISTORY. In production they are
        # grandfathered by the NOT VALID floor; a fresh test database has to
        # write them beneath a lifted floor, which the helper restores before
        # the assertions run against a live one.
        async with (
            session_maker() as session,
            retired_era_writes_allowed(session),
            session.begin(),
        ):
            for position, agent_id in enumerate(cohort, start=1):
                session.add(
                    BenchmarkRolloutMember(
                        rollout_id=rollout_id,
                        agent_id=agent_id,
                        position=position,
                        frozen_miner_hotkey=f"5TopMiner{position - 1}",
                        frozen_composite=1 - position / 100,
                    )
                )
            agent = await session.get(Agent, source_agent)
            assert agent is not None
            agent.screened_image_sha256 = "12" * 32
            agent.screened_image_size_bytes = 123
            agent.screened_image_id = "sha256:" + "34" * 32
            agent.screened_image_ref = f"ditto-screen/{source_agent}:latest"
            agent.screened_image_upload_id = uuid4()
            agent.screened_image_verified_at = now
            session.add(
                BenchmarkDataset(
                    agent_id=source_agent,
                    bench_version=6,
                    seed=8675309,
                    sha256="cd" * 32,
                    run_size="full",
                )
            )
            session.add(
                ValidatorTicket(
                    agent_id=source_agent,
                    bench_version=6,
                    validator_hotkey=_DAVE.ss58_address,
                    status=TicketStatus.SCORED,
                    issued_at=now - timedelta(minutes=10),
                    deadline=now - timedelta(minutes=5),
                    attempt_count=1,
                )
            )
            session.add(
                Score(
                    agent_id=source_agent,
                    bench_version=6,
                    validator_hotkey=_DAVE.ss58_address,
                    run_id="historical-v6-1",
                    signature="aa",
                    seed=8675309,
                    composite=0.71,
                    tool_mean=0.7,
                    memory_mean=0.7,
                    median_ms=100,
                    n=114,
                    details={"bench_version": 6},
                    generated_at=now,
                )
            )
            for hotkey in (_VALIDATOR_HOTKEY, _DAVE.ss58_address):
                heartbeat = await session.get(ValidatorHeartbeat, hotkey)
                assert heartbeat is not None
                heartbeat.benchmark_capacity = {
                    "configured_slots": 1,
                    "healthy_slots": ["slot-0"],
                    "admission": "accepting",
                    "active": [],
                }

        _install_db(app, session_maker)
        _install_chain(app, extra_keypairs=(_DAVE,))
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock()

        ineligible_source_only = await client.post(
            "/api/v1/validator/job",
            headers={"X-Validator-Hotkey": _DAVE.ss58_address},
            json=_job_payload(_DAVE, slot_id="slot-0"),
        )
        assert ineligible_source_only.status_code == 204

        # Default policy: the era is retired, so the lane issues nothing --
        # even though every other gate it has (cohort complete, capable
        # heartbeat, an empty v7 queue, spare fleet capacity) is wide open.
        retired = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id="slot-0"),
        )
        assert retired.status_code == 204
        async with session_maker() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ValidatorTicket)
                    .where(
                        ValidatorTicket.bench_version == 6,
                        ValidatorTicket.status == TicketStatus.ISSUED,
                    )
                )
                == 0
            )

    async def test_fresh_submission_lane_uses_three_of_four_completed_jobs(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        started_at = datetime.now(UTC) - timedelta(minutes=1)
        validator_hotkey = "5LaneValidator"
        async with session_maker() as session, session.begin():
            assert await _fresh_submission_lane_due(
                session,
                validator_hotkey=validator_hotkey,
                bench_version=_BENCH_VERSION,
                rollout_started_at=started_at,
                now=datetime.now(UTC),
                settings=QueuePolicySettings(),
            )
            for completed in range(1, 4):
                agent_id = uuid4()
                session.add(
                    Agent(
                        agent_id=agent_id,
                        miner_hotkey=f"5Miner-{completed}",
                        name=f"lane-{completed}",
                        sha256=f"{completed:064x}",
                        status=AgentStatus.SCORED,
                        screening_policy_version=SCREENING_POLICY_VERSION,
                        created_at=started_at,
                    )
                )
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        bench_version=_BENCH_VERSION,
                        validator_hotkey=validator_hotkey,
                        status=TicketStatus.SCORED,
                        issued_at=started_at,
                        deadline=started_at + timedelta(minutes=90),
                        attempt_count=1,
                        created_at=started_at,
                    )
                )
                await session.flush()
                due = await _fresh_submission_lane_due(
                    session,
                    validator_hotkey=validator_hotkey,
                    bench_version=_BENCH_VERSION,
                    rollout_started_at=started_at,
                    now=datetime.now(UTC),
                    settings=QueuePolicySettings(),
                )
                assert due is (completed != 2)

    async def test_fresh_submission_lane_counts_only_live_reservations(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        now = datetime.now(UTC)
        started_at = now - timedelta(minutes=1)
        validator_hotkey = "5ReservedLaneValidator"
        async with session_maker() as session, session.begin():
            for ordinal, deadline in enumerate(
                (now + timedelta(minutes=90), now + timedelta(minutes=90), now)
            ):
                agent_id = uuid4()
                session.add(
                    Agent(
                        agent_id=agent_id,
                        miner_hotkey=f"5ReservedMiner-{ordinal}",
                        name=f"reserved-lane-{ordinal}",
                        sha256=f"{ordinal + 10:064x}",
                        status=AgentStatus.EVALUATING,
                        screening_policy_version=SCREENING_POLICY_VERSION,
                        created_at=started_at,
                    )
                )
                session.add(
                    ValidatorTicket(
                        agent_id=agent_id,
                        bench_version=_BENCH_VERSION,
                        validator_hotkey=validator_hotkey,
                        slot_id=f"slot-{ordinal}",
                        status=TicketStatus.ISSUED,
                        issued_at=started_at,
                        deadline=deadline,
                        attempt_count=1,
                        created_at=started_at,
                    )
                )
            await session.flush()

            assert not await _fresh_submission_lane_due(
                session,
                validator_hotkey=validator_hotkey,
                bench_version=_BENCH_VERSION,
                rollout_started_at=started_at,
                now=now,
                settings=QueuePolicySettings(),
            )

    async def test_concurrent_lane_claims_serialize_before_counting(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        now = datetime.now(UTC)
        started_at = now - timedelta(minutes=1)
        validator_hotkey = "5ConcurrentLaneValidator"
        settings = QueuePolicySettings(lane_cycle_size=2, fresh_submission_slots=(0,))
        async with session_maker() as first:
            if first.get_bind().dialect.name != "postgresql":
                return
            await first.begin()
            assert await _fresh_submission_lane_due(
                first,
                validator_hotkey=validator_hotkey,
                bench_version=_BENCH_VERSION,
                rollout_started_at=started_at,
                now=now,
                settings=settings,
            )

            async def second_claim() -> bool:
                async with session_maker() as second, second.begin():
                    return await _fresh_submission_lane_due(
                        second,
                        validator_hotkey=validator_hotkey,
                        bench_version=_BENCH_VERSION,
                        rollout_started_at=started_at,
                        now=now,
                        settings=settings,
                    )

            waiting = asyncio.create_task(second_claim())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiting), timeout=0.1)

            agent_id = uuid4()
            first.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey="5ConcurrentLaneMiner",
                    name="concurrent-lane",
                    sha256="ef" * 32,
                    status=AgentStatus.EVALUATING,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                    created_at=started_at,
                )
            )
            first.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH_VERSION,
                    validator_hotkey=validator_hotkey,
                    status=TicketStatus.ISSUED,
                    issued_at=now,
                    deadline=now + timedelta(minutes=90),
                    attempt_count=1,
                    created_at=now,
                )
            )
            await first.commit()

            assert await asyncio.wait_for(waiting, timeout=5) is False

    @staticmethod
    async def _activate_benchmark(
        session_maker: async_sessionmaker[AsyncSession],
        agent_id: UUID,
        *,
        bench_version: int,
    ) -> None:
        now = datetime.now(UTC)
        # These fixtures activate v3/v4 -- eras that are retired now. In
        # production those rollout rows are real and grandfathered by the
        # NOT VALID floor; a fresh test database has to write them beneath a
        # lifted floor, which the helper restores on exit. The tests themselves
        # then run against a live floor, which is the state that matters.
        async with (
            session_maker() as session,
            retired_era_writes_allowed(session),
            session.begin(),
        ):
            agent = await session.get(Agent, agent_id)
            assert agent is not None
            agent.screening_policy_version = SCREENING_POLICY_VERSION
            agent.screened_image_sha256 = "12" * 32
            agent.screened_image_size_bytes = 123
            agent.screened_image_id = "sha256:" + "34" * 32
            agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
            agent.screened_image_upload_id = uuid4()
            agent.screened_image_verified_at = now
            # (from_version, desired_version) is UNIQUE, and the autouse
            # ``_current_era`` fixture may already have recorded this exact
            # transition. Attach to that row rather than inserting a duplicate.
            rollout_id = await session.scalar(
                select(BenchmarkRollout.rollout_id).where(
                    BenchmarkRollout.from_version == bench_version - 1,
                    BenchmarkRollout.desired_version == bench_version,
                )
            )
            if rollout_id is None:
                rollout_id = uuid4()
                session.add(
                    BenchmarkRollout(
                        rollout_id=rollout_id,
                        from_version=bench_version - 1,
                        desired_version=bench_version,
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
                    frozen_composite=0.0,
                )
            )
            # ``_seed_agent`` already pins a dataset at the active era, so
            # activating that same era would collide on (agent_id,
            # bench_version). Only pin what is missing.
            if (await session.get(BenchmarkDataset, (agent_id, bench_version))) is None:
                session.add(
                    BenchmarkDataset(
                        agent_id=agent_id,
                        bench_version=bench_version,
                        seed=8675309,
                        sha256="cd" * 32,
                        run_size="full",
                    )
                )

    @staticmethod
    def _enable_compatibility_gate(app: FastAPI) -> None:
        app.state.config = replace(
            app.state.config,
            validator_compatibility=ValidatorCompatibilityConfig(
                minimum_software_version="0.7.0",
                minimum_protocol_version=4,
                heartbeat_max_age_seconds=300,
            ),
        )

    async def test_requires_heartbeat_before_issuing_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        self._enable_compatibility_gate(app)

        response = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )

        assert response.status_code == 428
        assert "heartbeat required" in response.json()["message"]

    @pytest.mark.parametrize(
        ("software_version", "protocol_version", "expected_detail"),
        [
            ("0.6.9", 4, "software '0.6.9' is below required 0.7.0"),
            ("0.7.0", 3, "protocol 3 is below required 4"),
        ],
    )
    async def test_requires_supported_validator_release(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        software_version: str,
        protocol_version: int,
        expected_detail: str,
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_validator_heartbeat(
            session_maker,
            software_version=software_version,
            protocol_version=protocol_version,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        self._enable_compatibility_gate(app)

        response = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )

        assert response.status_code == 426
        assert expected_detail in response.json()["message"]
        assert "update ditto-subnet" in response.json()["message"]

    async def test_supported_validator_receives_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        # A bare heartbeat used to be enough to be handed work. The point here is
        # that a validator clearing the compatibility gate gets a job, so the
        # heartbeat has to clear the v7 capability bar too -- otherwise the 204
        # would come from capability, not from the gate under test.
        await _seed_capable_pool(session_maker, keypairs=(_KEYPAIR,))
        _install_db(app, session_maker)
        _install_chain(app)
        self._enable_compatibility_gate(app)
        refresh = AsyncMock(return_value=0)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.refresh_rolling_qualification",
            refresh,
        )

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(agent_id)
        refresh.assert_not_awaited()

    async def test_v8_only_validator_receives_work_without_v7_calibration(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A v8-only scorer is not forced to advertise the retired v7 manifest.

        The shared capability gate already treated v7 calibration as specific
        to v7.  The job endpoint duplicated the old broader condition, making
        an otherwise healthy v8-only validator poll successfully but receive a
        204 while eligible v8 submissions waited.
        """
        await _seed_activated_era(session_maker, version=8)
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            dataset_version=8,
        )
        capabilities = _scorer_capable_capabilities(
            now=datetime.now(UTC), versions=(8,)
        )
        scorer = capabilities["scorer_benchmarks"]
        assert isinstance(scorer, dict)
        scorer.pop("v7_calibration")
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=18,
            capabilities=capabilities,
            stack=_V7_STACK,
            benchmark_capacity=_ACCEPTING_CAPACITY,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        self._enable_compatibility_gate(app)
        refresh = AsyncMock(return_value=0)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.refresh_rolling_qualification",
            refresh,
        )

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(agent_id)
        assert response.json()["bench_version"] == 8
        refresh.assert_not_awaited()

    async def test_v9_rollout_job_mints_grant_without_legacy_calibration(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The operator-ready route must also be usable by the ticket path.

        Rollout start stopped requiring the retired 60-sample provider
        calibration for v8/v9, but grant creation retained that old gate. The
        rollout therefore opened successfully while every slot received 503
        and its lease transaction rolled back. Exercise the production shape:
        an exact v9 aggregate route with operational discovery state and no
        calibration evidence must mint a real grant and return the job.
        """
        from ditto.api_server.inference_routing import (
            AGGREGATE_PROVIDER,
            aggregate_profile_revision,
            benchmark_model,
        )

        await _seed_activated_era(session_maker, version=8)
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCORED,
            dataset_version=9,
        )
        now = datetime.now(UTC)
        fresh_agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="fresh-during-rollout",
            created_at=now + timedelta(seconds=1),
            miner_hotkey="5FreshDuringRollout",
            sha256="ef" * 32,
            dataset_version=9,
        )
        rollout_id = uuid4()
        profile = aggregate_profile_revision(benchmark_model(9), bench_version=9)
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=8,
                    desired_version=9,
                    status="collecting",
                    cohort_size=5,
                    rescore_cohort_target=5,
                    priority_cohort_target=5,
                    created_at=now,
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
                InferenceProviderRoute(
                    model=benchmark_model(9),
                    provider=AGGREGATE_PROVIDER,
                    profile_revision=profile,
                    status="discovered",
                    calibration_status="shadow",
                    calibration_manifest_sha256=None,
                    calibration_tool_accuracy=None,
                    calibration_composite=None,
                    calibration_sample_count=0,
                    ewma_error_rate=0,
                    ewma_timeout_rate=0,
                    sample_count=0,
                    selected_ticket_count=0,
                    exploration_ticket_count=0,
                    discovered_at=now,
                    updated_at=now,
                )
            )

        capabilities = _scorer_capable_capabilities(now=now, versions=(9,))
        scorer = capabilities["scorer_benchmarks"]
        assert isinstance(scorer, dict)
        scorer.pop("v7_calibration")
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=18,
            capabilities=capabilities,
            stack=_V7_STACK,
            benchmark_capacity={
                "configured_slots": 2,
                "healthy_slots": ["slot-0", "slot-1"],
                "admission": "accepting",
                "active": [],
            },
        )
        _install_db(app, session_maker)
        _install_chain(app)
        activate_retest = AsyncMock(wraps=validator_endpoint.activate_next_score_retest)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.activate_next_score_retest",
            activate_retest,
        )
        app.state.config = replace(
            app.state.config,
            inference_proxy=replace(
                app.state.config.inference_proxy,
                enabled=True,
                openrouter_api_key="test-only",
                allowed_models=(benchmark_model(9),),
                routing_mode="aggregate_throughput",
            ),
        )

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(agent_id)
        assert response.json()["agent_id"] != str(fresh_agent_id)
        assert response.json()["bench_version"] == 9
        assert response.json()["inference"]["profile_revision"] == profile
        activate_retest.assert_awaited_once()
        assert activate_retest.await_args is not None
        call_kwargs = activate_retest.await_args.kwargs
        assert call_kwargs["required_basis"] == "v9_contract_mismatch"
        assert call_kwargs["allow_parallel_ordinary"] is True
        assert call_kwargs["allow_parallel_contract_retests"] is True

        # This validator now already holds every frozen member it can advance.
        # A sibling slot must not park: it falls through to the ordinary v9
        # lane and keeps fleet capacity full.
        fallback = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id="slot-1"),
        )
        assert fallback.status_code == 200, fallback.text
        assert fallback.json()["agent_id"] == str(fresh_agent_id)
        assert fallback.json()["bench_version"] == 9
        async with session_maker() as session:
            grant = await session.scalar(
                select(InferenceGrant).where(
                    InferenceGrant.agent_id == agent_id,
                    InferenceGrant.bench_version == 9,
                )
            )
            assert grant is not None
            assert grant.route_profile == profile
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ValidatorTicket)
                    .where(ValidatorTicket.agent_id == fresh_agent_id)
                )
                == 1
            )

    async def test_open_v9_rollout_never_resumes_a_live_v8_ticket(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The source era cannot bypass rollout policy through resumption.

        Production had carryover disabled but a source-mode validator still
        received v8 because this live-ticket lookup sat below every v9 lane and
        re-issued the active-era row whenever the heartbeat called the slot
        busy. During rollout that is never useful work: the transition requires
        v9 quorum, while a v8 score cannot move activation forward.
        """
        await _seed_activated_era(session_maker, version=8)
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            dataset_version=8,
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=8,
                    desired_version=9,
                    status="collecting",
                    cohort_size=5,
                    rescore_cohort_target=5,
                    priority_cohort_target=5,
                    created_at=now,
                )
            )
        await _seed_ticket(
            session_maker,
            agent_id,
            bench_version=8,
            slot_id=_SLOT_ID,
            deadline=now + timedelta(minutes=90),
        )
        capabilities = _scorer_capable_capabilities(now=now, versions=(8, 9))
        scorer = capabilities["scorer_benchmarks"]
        assert isinstance(scorer, dict)
        scorer.pop("v7_calibration")
        capacity = {
            **_ACCEPTING_CAPACITY,
            "active": [
                {
                    "slot_id": _SLOT_ID,
                    "agent_id": str(agent_id),
                    "bench_version": 8,
                    "progress": None,
                }
            ],
        }
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=18,
            capabilities=capabilities,
            stack=_V7_STACK,
            benchmark_capacity=capacity,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )

        assert response.status_code == 204, response.text
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, 8, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == TicketStatus.ISSUED

    async def test_v9_rollout_fallback_rejects_pre_contract_scorer(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Every desired-era lane must share the scorer release floor.

        ``issue_rollout_ticket`` already rejected this heartbeat, but the
        fresh-submission fallback called ``issue_ticket`` directly and checked
        only inference readiness.  In production that handed v9 work to a
        v0.53.8 scorer minutes after Platform raised the floor to v0.53.10.
        Keep a fully operational route and eligible agent in the fixture so a
        204 can only mean the scorer capability stopped every fallback lane.
        """
        from ditto.api_server.inference_routing import (
            AGGREGATE_PROVIDER,
            aggregate_profile_revision,
            benchmark_model,
        )

        await _seed_activated_era(session_maker, version=8)
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.SCORED,
            dataset_version=9,
        )
        now = datetime.now(UTC)
        profile = aggregate_profile_revision(benchmark_model(9), bench_version=9)
        async with session_maker() as session, session.begin():
            rollout_id = uuid4()
            session.add(
                BenchmarkRollout(
                    rollout_id=rollout_id,
                    from_version=8,
                    desired_version=9,
                    status="collecting",
                    cohort_size=5,
                    rescore_cohort_target=5,
                    priority_cohort_target=5,
                    created_at=now,
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
                InferenceProviderRoute(
                    model=benchmark_model(9),
                    provider=AGGREGATE_PROVIDER,
                    profile_revision=profile,
                    status="discovered",
                    calibration_status="shadow",
                    calibration_manifest_sha256=None,
                    calibration_tool_accuracy=None,
                    calibration_composite=None,
                    calibration_sample_count=0,
                    ewma_error_rate=0,
                    ewma_timeout_rate=0,
                    sample_count=0,
                    selected_ticket_count=0,
                    exploration_ticket_count=0,
                    discovered_at=now,
                    updated_at=now,
                )
            )

        capabilities = _scorer_capable_capabilities(now=now, versions=(9,))
        scorer = capabilities["scorer_benchmarks"]
        assert isinstance(scorer, dict)
        scorer.update(
            software_version="0.53.9",
            source_revision="9" * 40,
        )
        scorer.pop("v7_calibration")
        stack = json.loads(json.dumps(_V7_STACK))
        scorer_component = stack["components"]["dittobench_api"]
        scorer_component.update(
            version="0.53.9",
            source_revision="9" * 40,
        )
        await _seed_validator_heartbeat(
            session_maker,
            software_version="0.53.9",
            protocol_version=18,
            capabilities=capabilities,
            stack=stack,
            benchmark_capacity=_ACCEPTING_CAPACITY,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        app.state.config = replace(
            app.state.config,
            inference_proxy=replace(
                app.state.config.inference_proxy,
                enabled=True,
                openrouter_api_key="test-only",
                allowed_models=(benchmark_model(9),),
                routing_mode="aggregate_throughput",
            ),
        )

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )

        assert response.status_code == 204, response.text
        async with session_maker() as session:
            assert (
                await session.scalar(select(func.count()).select_from(ValidatorTicket))
                == 0
            )
            assert (
                await session.scalar(select(func.count()).select_from(InferenceGrant))
                == 0
            )

    async def test_required_proxy_issues_only_to_v10_ticket_inference_slots(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=9,
            capabilities=_V9_CAPABILITIES,
            stack=_V7_STACK,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        app.state.config = replace(
            app.state.config,
            inference_proxy=replace(
                app.state.config.inference_proxy,
                enabled=True,
                required=True,
                openrouter_api_key="test-only",
            ),
        )

        legacy = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )
        assert legacy.status_code == 204

        # The upgraded shape. The floor in this test's name moved: ticket
        # inference is only treated as ready from protocol 11 for a v7 target
        # (10 below it), and a heartbeat cannot advertise v7 at all below
        # protocol 12. So "the v10 slot that can do ticket inference" is a v12+
        # slot now, and it has to carry the full v7 scorer capability rather
        # than a ticket_inference flag bolted onto the v9 blob.
        async with session_maker() as session, session.begin():
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert heartbeat is not None
            heartbeat.protocol_version = 13
            heartbeat.capabilities = _scorer_capable_capabilities(
                now=datetime.now(UTC), versions=(_BENCH_VERSION,)
            )
            heartbeat.benchmark_capacity = dict(_ACCEPTING_CAPACITY)

        issued = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )
        assert issued.status_code == 200, issued.text
        assert issued.json()["agent_id"] == str(agent_id)
        assert issued.json()["inference"]["grant_id"]

    async def test_retired_era_only_validator_is_permanently_unserviceable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A validator that can only serve a retired era never works again.

        This used to be a statement about a moment -- v2 had just been
        superseded, so a v2-only validator was idle until it upgraded. The floor
        makes it a statement about the system: v2 through v6 cannot be written
        at all, so there is no future rollout, setting or backfill lane that
        gives this validator work. Permanently unserviceable is the intended
        consequence of the floor, not an accident of the fixture.

        The full arc, and all of it terminal: it is OFFERED nothing (204 with an
        active-era agent sitting in the queue), it LEASES nothing (204 again,
        with no new ticket cut in any era), it DRAINS what it already holds (the
        grandfathered v2 lease it is carrying moves to EXPIRED, which is the one
        transition the ticket trigger still permits below the floor), and then it
        STAYS IDLE, with nothing left to drain and nothing it can be given.

        The v2 lease is seeded beneath a lifted floor because that is the only
        way such a row can exist now -- exactly like the real pre-floor leases
        production is still draining. The floor is live again by the time the
        endpoint is called, so the drain asserted below is a real drain.
        """
        active_agent = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await self._activate_benchmark(
            session_maker, active_agent, bench_version=_BENCH_VERSION
        )
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=7,
            capabilities=_V7_CAPABILITIES,
            stack=_V7_STACK,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        no_new_work = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )
        assert no_new_work.status_code == 204

        legacy_agent = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, legacy_agent, bench_version=2)
        resumed = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )
        assert resumed.status_code == 204, resumed.text
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (legacy_agent, 2, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == TicketStatus.EXPIRED
            # Nothing was cut to replace it, in any era. The drain is the end of
            # this validator's work, not a handover to a new lease.
            live = (
                (
                    await session.execute(
                        select(ValidatorTicket).where(
                            ValidatorTicket.validator_hotkey == _VALIDATOR_HOTKEY,
                            ValidatorTicket.status == TicketStatus.ISSUED,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert live == []

        # Asked again with nothing left to drain, it is still offered nothing.
        still_idle = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )
        assert still_idle.status_code == 204, still_idle.text

    # Parametrized over the RETIRED era, not the active one.
    #
    # It used to range over active_version [3, 4] to show the behaviour held
    # across post-legacy benchmarks. That axis no longer exists: shipped
    # contracts stop at v7 and the floor is 7, so exactly one era is both
    # leasable and writable. The narrowing is inherent to there being one live
    # era, not a weakening -- and moving the parameter to the retired side keeps
    # two cases exercising both halves of the boundary, which is what the test
    # was really about.
    @pytest.mark.parametrize("legacy_version", [5, 6])
    async def test_after_activation_capable_validator_replaces_retired_ticket(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        legacy_version: int,
    ) -> None:
        active_version = _BENCH_VERSION
        active_agent = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await self._activate_benchmark(
            session_maker, active_agent, bench_version=active_version
        )
        legacy_agent = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, legacy_agent, bench_version=legacy_version)
        # A protocol-8 heartbeat was enough to be handed work in the v2/v3 era
        # this test was written for. It is not enough now: a v7 lease requires
        # the v10 capacity blob and a ticket-scoped inference grant, so the
        # capable-pool helper is the only shape that still gets a job issued.
        # The autouse ``_current_era`` fixture already installed the routing
        # policy; only the capable heartbeat is missing.
        await _seed_capable_pool(session_maker, keypairs=[_KEYPAIR])
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id="slot-0"),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(active_agent)
        assert response.json()["bench_version"] == active_version
        async with session_maker() as session:
            legacy_ticket = await session.get(
                ValidatorTicket, (legacy_agent, legacy_version, _VALIDATOR_HOTKEY)
            )
            assert legacy_ticket is not None
            assert legacy_ticket.status == TicketStatus.EXPIRED

    async def test_after_activation_new_submission_finalizes_on_three_scores(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await self._activate_benchmark(
            session_maker, agent_id, bench_version=_BENCH_VERSION
        )
        # These three used to be protocol-8 heartbeats advertising
        # ``supported_bench_versions: [2, 3]``. Neither half survives the floor:
        # a validator that offers only retired eras is offered nothing, and a
        # protocol-8 validator cannot take a v7 lease at all (inference is not
        # treated as ready below protocol 11, and protocol >= 10 makes published
        # capacity and a named slot mandatory). The quorum-of-three behaviour
        # under test is unchanged -- only the shape of a validator that can
        # reach it is, so the fixture is the capable pool the rest of the class
        # leases through.
        await _seed_capable_pool(session_maker)
        _install_db(app, session_maker)
        _install_chain(app, extra_keypairs=tuple(_KEYPAIRS[1:]))

        for index, keypair in enumerate(_KEYPAIRS, start=1):
            job = await client.post(
                "/api/v1/validator/job",
                headers={"X-Validator-Hotkey": keypair.ss58_address},
                json=_job_payload(keypair, slot_id=_SLOT_ID),
            )
            assert job.status_code == 200, job.text
            assert job.json()["bench_version"] == _BENCH_VERSION
            assert job.json()["minimum_screening_policy_version"] == 9
            assert job.json()["requires_screened_image"] is True
            deadline = datetime.fromisoformat(job.json()["deadline"])
            score = await client.post(
                f"/api/v1/validator/agent/{agent_id}/score",
                json=_score_payload(
                    agent_id,
                    run_id=f"v3-{index}",
                    keypair=keypair,
                    ticket_deadline=deadline,
                    bench_version=_BENCH_VERSION,
                    n=114,
                    details={"bench_version": 3},
                ),
            )
            assert score.status_code == 200, score.text
            expected = AgentStatus.SCORED if index == 3 else AgentStatus.EVALUATING
            assert score.json()["status"] == expected

        async with session_maker() as session:
            scores = (
                (
                    await session.execute(
                        select(Score).where(
                            Score.agent_id == agent_id,
                            Score.bench_version == _BENCH_VERSION,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(scores) == 3

    async def test_v7_screened_only_does_not_claim_source_only_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # No verified screened image: that is what makes this work source-only,
        # and ``_seed_agent`` now supplies one unless asked not to.
        await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, screened_image=False
        )
        capabilities = {
            **_V7_CAPABILITIES,
            "require_screened_image": True,
            "source_build_fallback": False,
        }
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=7,
            capabilities=capabilities,
            stack=_V7_STACK,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )

        assert response.status_code == 204

    @pytest.mark.parametrize(
        ("slot_reported_active", "expected_status"),
        [
            (False, TicketStatus.EXPIRED),
            (True, TicketStatus.ISSUED),
        ],
    )
    async def test_v7_screened_only_does_not_resume_source_only_live_ticket(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        slot_reported_active: bool,
        expected_status: TicketStatus,
    ) -> None:
        """A screened-only validator releases an idle source-only lease.

        The claim is unchanged -- an idle slot gives the lease back, a working
        slot keeps it -- but at v7 the fixture can no longer say "idle" the way
        it used to, so the parameter had to move.

        This test used to parametrize the heartbeat's whole-validator ``state``,
        which is the only occupancy signal a pre-v10 reporter has. A validator
        cannot advertise v7 at all below protocol 12 (``protocol_serves_version``
        is an explicit ``version >= 7 and protocol_version < 12``), and at
        protocol >= 10 ``request_job`` stops reading ``state`` entirely: slot
        occupancy comes from the per-slot capacity blob, and ``lease_liveness``
        follows it there. So a protocol-7 heartbeat now fails the capability gate
        before ``issue_ticket`` -- and with it the release path -- is ever
        reached, and the lease would sit untouched in BOTH parameters: the
        ``running_benchmark`` case would still have passed, for entirely the
        wrong reason.

        Parametrizing ``capacity.active`` instead asks the same question of the
        signal that now answers it.
        """
        # Source-only is a property of the SUBMISSION: no verified screened
        # image, so a screened-only validator cannot serve it. ``_seed_agent``
        # provides one by default now, which would make this lease resumable and
        # invert the test.
        agent_id = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, screened_image=False
        )
        now = datetime.now(UTC)
        # Scorer-capable AND screened-only. Both halves are load-bearing: the
        # release lives inside ``issue_ticket``, which a validator that cannot
        # serve the canonical era never reaches.
        capabilities = {
            **_scorer_capable_capabilities(now=now, versions=(_BENCH_VERSION,)),
            "require_screened_image": True,
            "source_build_fallback": False,
        }
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=13,
            capabilities=capabilities,
            stack=_V7_STACK,
            state=("running_benchmark" if slot_reported_active else "polling"),
            benchmark_capacity={
                **_ACCEPTING_CAPACITY,
                "active": (
                    [
                        {
                            "slot_id": _SLOT_ID,
                            "agent_id": str(agent_id),
                            "bench_version": _BENCH_VERSION,
                            "progress": None,
                        }
                    ]
                    if slot_reported_active
                    else []
                ),
            },
        )
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH_VERSION,
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    slot_id=_SLOT_ID,
                    status=TicketStatus.ISSUED,
                    # Reported once already, so a heartbeat now reporting no
                    # work is evidence of idleness rather than a run still
                    # starting up and not yet advertising its slot.
                    issued_at=now - timedelta(minutes=10),
                    deadline=now + timedelta(minutes=90),
                    attempt_count=1,
                    manual_retry_grants=0,
                    first_reported_at=now - timedelta(minutes=9),
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )

        assert response.status_code == 204
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == expected_status

    async def test_job_claim_does_not_destroy_a_run_behind_a_frozen_capacity_blob(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """End to end over the shape that lost three v7 runs: heartbeat ingest
        has been failing for four minutes, so the stored capacity blob still
        shows an empty slot while the benchmark underneath keeps scoring. The
        next job claim must leave that lease alone."""
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        capabilities = {
            **_V7_CAPABILITIES,
            "require_screened_image": True,
            "source_build_fallback": False,
        }
        now = datetime.now(UTC)
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=7,
            capabilities=capabilities,
            stack=_V7_STACK,
            state="polling",
            seen_at=now - timedelta(minutes=4),
        )
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=agent_id,
                    bench_version=_BENCH_VERSION,
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    status=TicketStatus.ISSUED,
                    issued_at=now - timedelta(minutes=19),
                    deadline=now + timedelta(minutes=71),
                    attempt_count=1,
                    manual_retry_grants=0,
                )
            )
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )

        assert response.status_code == 204
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == TicketStatus.ISSUED
            assert ticket.attempt_count == 1
            assert (
                await session.scalar(
                    select(func.count()).select_from(ValidatorLeaseAudit)
                )
            ) == 0

    async def test_stale_heartbeat_cannot_claim_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_validator_heartbeat(
            session_maker, seen_at=datetime.now(UTC) - timedelta(minutes=6)
        )
        _install_db(app, session_maker)
        _install_chain(app)
        self._enable_compatibility_gate(app)

        response = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )

        assert response.status_code == 428
        assert "heartbeat is stale" in response.json()["message"]

    async def test_issues_ticket_for_evaluating_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_capable_pool(session_maker)
        _install_db(app, session_maker)
        _install_chain(app)
        before = datetime.now(UTC)
        resp = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )
        after = datetime.now(UTC)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_id"] == str(agent_id)
        deadline = datetime.fromisoformat(body["deadline"].replace("Z", "+00:00"))
        assert before + timedelta(minutes=180) <= deadline
        assert deadline <= after + timedelta(minutes=180)

    async def test_canonical_candidate_preempts_runnable_score_retest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A saturated re-test lane is backfill, never queue precedence."""
        now = datetime.now(UTC)
        retest_agent = await _seed_agent(
            session_maker,
            status=AgentStatus.SCORED,
            name="queued-retest",
        )
        canonical_agent = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="waiting-canonical",
            miner_hotkey="5WaitingCanonical",
            sha256="cd" * 32,
        )
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=retest_agent,
                    bench_version=MIN_SCOREABLE_BENCH_VERSION,
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    status=TicketStatus.SCORED,
                    issued_at=now - timedelta(hours=2),
                    deadline=now - timedelta(minutes=30),
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                )
            )
            session.add(
                Score(
                    agent_id=retest_agent,
                    bench_version=MIN_SCOREABLE_BENCH_VERSION,
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    run_id="queued-retest-run",
                    seed=1,
                    composite=0.8,
                    tool_mean=0.8,
                    memory_mean=0.8,
                    median_ms=100,
                    n=114,
                    details={"bench_version": MIN_SCOREABLE_BENCH_VERSION},
                    generated_at=now - timedelta(hours=1),
                )
            )
            await append_audit_entry(
                session,
                agent_id=retest_agent,
                validator_hotkey=_VALIDATOR_HOTKEY,
                event=EVENT_SCORE_RETEST_QUEUED,
                payload={
                    "request_id": str(uuid4()),
                    "bench_version": MIN_SCOREABLE_BENCH_VERSION,
                    "run_id": "queued-retest-run",
                },
                recorded_at=now,
            )
        await _seed_capable_pool(session_maker, keypairs=(_KEYPAIR,))
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(canonical_agent)
        async with session_maker() as session:
            retest_ticket = await session.get(
                ValidatorTicket,
                (retest_agent, MIN_SCOREABLE_BENCH_VERSION, _VALIDATOR_HOTKEY),
            )
            assert retest_ticket is not None
            assert retest_ticket.status == TicketStatus.SCORED

    async def test_no_work_returns_204(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        resp = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )
        assert resp.status_code == 204

    async def test_no_work_is_counted_as_an_empty_queue_not_a_refusal(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """An idle fleet has to be readable off the metric, not off raw SQL.

        This poll clears every gate and finds nothing to hand out, which is the
        one decline an operator must be able to tell apart from dispatch
        refusing to issue -- the reasons look identical over the wire.
        """
        from prometheus_client import REGISTRY

        _install_db(app, session_maker)
        _install_chain(app)

        def counted(reason: str) -> float:
            return (
                REGISTRY.get_sample_value(
                    "ditto_validator_dispatch_declined_total", {"reason": reason}
                )
                or 0.0
            )

        before = counted("no_candidate")
        resp = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )

        assert resp.status_code == 204
        assert counted("no_candidate") == before + 1

    async def test_caps_at_quorum_across_validators(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        # Dave included: the fourth validator must be refused for want of a SLOT,
        # not for want of a capable heartbeat, or the closing 204 would prove
        # nothing about the quorum cap.
        await _seed_capable_pool(session_maker, keypairs=(*_KEYPAIRS, _DAVE))
        _install_db(app, session_maker)
        _install_chain(app, extra_keypairs=(_DAVE,))
        # Three distinct validators each get the single agent (fills the pool).
        for kp in _KEYPAIRS:
            r = await client.post(
                "/api/v1/validator/job",
                headers={"X-Validator-Hotkey": kp.ss58_address},
                json=_job_payload(kp, slot_id=_SLOT_ID),
            )
            assert r.status_code == 200, r.text
        # A further request finds no open slot -> no job.
        r = await client.post(
            "/api/v1/validator/job",
            headers={"X-Validator-Hotkey": _DAVE.ss58_address},
            json=_job_payload(_DAVE, slot_id=_SLOT_ID),
        )
        assert r.status_code == 204

    async def test_unpermitted_validator_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app, permitted=False)
        resp = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )
        assert resp.status_code == 401

    async def test_cannot_claim_by_naming_another_permitted_hotkey(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        forged = _job_payload(_KEYPAIRS[1])
        forged["validator_hotkey"] = _VALIDATOR_HOTKEY
        resp = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=forged
        )
        assert resp.status_code == 401

    async def test_replayed_job_claim_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        # The first claim has to genuinely succeed for the second to be a REPLAY
        # rather than a second miss, so the pool is seeded v7-capable.
        await _seed_capable_pool(session_maker, keypairs=(_KEYPAIR,))
        _install_db(app, session_maker)
        _install_chain(app)
        claim = _job_payload(slot_id=_SLOT_ID)
        first = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=claim
        )
        replay = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=claim
        )
        assert first.status_code == 200, first.text
        assert replay.status_code == 409

    async def test_stale_job_claim_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        stale = _job_payload(requested_at=datetime.now(UTC) - timedelta(minutes=3))
        resp = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=stale
        )
        assert resp.status_code == 409


class TestFailJob:
    @pytest.fixture(autouse=True)
    async def _current_era(
        self, app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Put the fleet on the era these tests lease work in.

        Two things that used to come for free at v2 have to be stated now.
        ``active_bench_version`` answers ``DEFAULT_BENCH_VERSION`` (2) when no
        rollout row exists, and 2 is beneath the ticket floor, so the allocator
        cuts a lease the database refuses -- a 500 with nothing in it about
        benchmarks. And every v7 lease must carry an inference grant, so the
        routing policy has to exist before a job can be issued at all.

        The third precondition -- a heartbeat the fleet counts as v7-capable --
        is deliberately NOT seeded here: several tests in these classes are
        about the absence or the wrong shape of that heartbeat. Tests that want
        a job issued call ``_seed_capable_pool``.
        """
        await _seed_activated_era(session_maker)
        await _install_ticket_inference(app, session_maker)

    async def test_closes_live_ticket_for_immediate_reissue(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="scoring_error"),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"agent_id": str(agent_id), "reopened": True}

        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            # A scoring_error closes immediately: expired now with
            # retry_after=now, not the 6h timeout cooldown. Another validator
            # may take the open quorum slot, while this validator has spent its
            # one-attempt base budget. (Infrastructure failures earn a grant and
            # back off.)
            assert ticket.status == TicketStatus.EXPIRED
            now = datetime.now(UTC)
            assert ticket.retry_after is not None
            retry_after = ticket.retry_after
            if retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=UTC)
            assert abs((retry_after - now).total_seconds()) < 60

    async def test_scoring_error_exhausts_same_validator_without_a_grant(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A deterministic scoring_error fails the lease and spends this
        # validator's one-attempt base budget. Reissuing it automatically would
        # pay for the same broken run twice.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        # Exercise the real allocator path with a validator eligible for v7.
        await _seed_capable_pool(session_maker, keypairs=(_KEYPAIR,))
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="scoring_error"),
        )
        assert failed.status_code == 200, failed.text

        reissued = await client.post(
            "/api/v1/validator/job",
            headers=_AUTH_HEADER,
            json=_job_payload(slot_id=_SLOT_ID),
        )
        assert reissued.status_code == 204, reissued.text

    async def test_continual_retest_scoring_error_cools_the_validator_pair(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.SCORED)
        await _seed_ticket(
            session_maker,
            agent_id,
            purpose=TicketPurpose.CONTINUAL_RETEST,
            seed=12345,
            dataset_sha256="cd" * 32,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="scoring_error"),
        )
        assert failed.status_code == 200, failed.text

        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.failure_reason == "scoring_error"
            assert ticket.retry_after is not None
            retry_after = ticket.retry_after
            if retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=UTC)
            assert retry_after - datetime.now(UTC) > timedelta(hours=5)

        # The continual allocator honors the same ticket cooldown. This exact
        # validator/agent pair cannot spin, while another validator can still
        # contribute the shared confirmation seed.
        async with session_maker() as s, s.begin():
            ticket = await issue_confirmation_ticket(
                s,
                agent_id=agent_id,
                validator_hotkey=_VALIDATOR_HOTKEY,
                now=datetime.now(UTC),
                ttl=timedelta(minutes=90),
                bench_version=_BENCH_VERSION,
                seed=12345,
                dataset_sha256="cd" * 32,
            )
            assert ticket is None
            peer_ticket = await issue_confirmation_ticket(
                s,
                agent_id=agent_id,
                validator_hotkey=_KEYPAIRS[1].ss58_address,
                now=datetime.now(UTC),
                ttl=timedelta(minutes=90),
                bench_version=_BENCH_VERSION,
                seed=12345,
                dataset_sha256="cd" * 32,
            )
            assert peer_ticket is not None

    async def test_infrastructure_failure_backs_off_before_reissue(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A sustained outage must not be hammered: an infrastructure failure sets
        # a short (escalating) cooldown, so the same agent is NOT re-leased on the
        # very next request_job the way a scoring_error is.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="infrastructure"),
        )
        assert failed.status_code == 200, failed.text

        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            now = datetime.now(UTC)
            retry_after = ticket.retry_after
            assert retry_after is not None
            if retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=UTC)
            # Future cooldown (well short of the 6h agent-failure cooldown).
            assert retry_after > now
            assert (retry_after - now) <= timedelta(minutes=31)

        # The agent is in cooldown, so request_job does not immediately re-lease it.
        reissued = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )
        assert reissued.status_code == 204, reissued.text

    async def test_sandbox_oom_is_recorded_and_defers_same_harness(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="sandbox_oom"),
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["reopened"] is True

        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == TicketStatus.EXPIRED
            assert ticket.failure_reason == "sandbox_oom"
            assert ticket.failed_at is not None
            assert ticket.retry_after is not None
            now = datetime.now(UTC)
            retry_after = ticket.retry_after
            if retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=UTC)
            assert retry_after - now > timedelta(hours=5)

        # With no other agent seeded, the failed harness is not immediately
        # reclaimed. A validator can advance to other eligible work instead.
        reissued = await client.post(
            "/api/v1/validator/job", headers=_AUTH_HEADER, json=_job_payload()
        )
        assert reissued.status_code == 204, reissued.text

    async def test_infrastructure_failure_earns_a_compensating_grant(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # An infrastructure failure is not the agent's fault: it bumps
        # infra_retry_grants (which offsets the attempt the reissue consumes),
        # so the agent's genuine per-version budget is never spent.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="infrastructure"),
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["reopened"] is True
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == 1

    async def test_seed_store_timeout_preserves_retry_budget_and_code(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id,
                reason="infrastructure",
                failure_detail="seed_store_lock_timeout",
            ),
        )

        assert failed.status_code == 200, failed.text
        assert failed.json()["reopened"] is True
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == TicketStatus.EXPIRED
            assert ticket.failure_reason == "infrastructure"
            assert ticket.failure_detail == "seed_store_lock_timeout"
            assert ticket.infra_retry_grants == 1
            assert ticket.attempt_count == 1

    async def test_container_log_tail_lands_on_the_ticket(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The 5fdadd33 shape: a scoring_error with no code, but with a reason.

        Agent 5fdadd33 burned four leases in 82-108s each and every hand-back
        carried `scoring_error` and nothing else, because the only field that
        could have said why was never on the wire. The tail must persist even
        when `failure_detail` is absent -- that combination IS the failure this
        column exists for, so a tail that only survived alongside a code would
        miss it entirely.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        tail = "thread 'main' panicked at src/main.rs:42: cannot open /data/index"

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id,
                reason="scoring_error",
                container_log_tail=tail,
            ),
        )

        assert failed.status_code == 200, failed.text
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.failure_reason == "scoring_error"
            assert ticket.container_log_tail == tail
            assert ticket.container_log_tail_attempt == ticket.attempt_count
            # Carried beside failure_detail, never folded into it: that field
            # stays the one thing an operator can GROUP BY.
            assert ticket.failure_detail is None

    async def test_fail_job_without_a_tail_stores_null(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A validator predating the field omits the key and stays valid.

        `FailJobRequest` does not forbid extras and the field defaults to None,
        so an un-upgraded validator's byte-identical payload must still be
        accepted and simply leave the column NULL -- never a 422, which would
        lose the whole hand-back and strand the lease until its deadline.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        payload = _job_fail_payload(agent_id, reason="scoring_error")
        assert "container_log_tail" not in payload

        failed = await client.post(
            "/api/v1/validator/job/fail", headers=_AUTH_HEADER, json=payload
        )

        assert failed.status_code == 200, failed.text
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.container_log_tail is None

    async def test_overlong_container_log_tail_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The wire bound is enforced here, not merely respected by senders.

        The column is written by validators on a hot table once per failed
        ticket, and its content is a miner's harness output verbatim. The cap is
        what keeps the ticket ledger from becoming a log sink with an
        adversarial write path into it.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id,
                reason="scoring_error",
                container_log_tail="x" * (CONTAINER_LOG_TAIL_MAX_LENGTH + 1),
            ),
        )

        assert failed.status_code == 422, failed.text

    async def test_scoring_error_failure_consumes_the_budget(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A scoring_error is the agent's own failure — no compensating grant.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id,
                reason="scoring_error",
                failure_detail="model_inference_required",
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == 0
            assert ticket.attempt_count == 1
            assert ticket.failure_reason == "scoring_error"
            assert ticket.failure_detail == "model_inference_required"

    async def test_platform_revoked_lease_is_not_billed_to_the_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The regression this exists for: the platform revoked the run's
        # inference grant mid-lease, the run died, and the validator -- which
        # could only see "the scorer failed" -- reported scoring_error. That
        # spent one of the agent's finite attempts for an outage it did not
        # cause. The platform holds the evidence that it revoked the lease, so
        # it compensates regardless of the label the validator put on it.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        await _seed_revoked_grant(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="scoring_error"),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == 1
            # The diagnosis is preserved verbatim; only the billing changed.
            assert ticket.failure_reason == "scoring_error"

    async def test_scoring_error_without_a_revoked_lease_still_bills_the_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The other half of the guarantee. A genuinely broken harness -- one
        # that crashed the scorer with its lease perfectly healthy -- must keep
        # consuming its own attempts, or one bad agent burns fleet capacity
        # forever. An `exhausted` grant is the agent spending the budget it was
        # given, so it is deliberately not treated as a platform fault.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        await _seed_revoked_grant(session_maker, agent_id, status="exhausted")
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="scoring_error"),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == 0

    async def test_spent_grant_reported_as_infra_is_billed_to_the_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Crown-v12-Final: every validator hit the 75M token wall, the Go
        # exchange omitted budget evidence, and the scorer reported
        # budget_evidence_absent as retryable infrastructure. Re-leasing the
        # same image cannot finish the remaining cases. The grant row is the
        # authoritative meter; settled spend at the wall is the agent's.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        await _seed_revoked_grant(
            session_maker,
            agent_id,
            status="exhausted",
            prompt_tokens=74_000_000,
            completion_tokens=1_000_000,
            token_budget=75_000_000,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id,
                reason="infrastructure",
                failure_detail="model_relay_unavailable:budget_evidence_absent",
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == 0
            assert ticket.failure_reason == "scoring_error"
            assert ticket.failure_detail == "inference_allowance_exhausted"

    async def test_unspent_grant_keeps_budget_evidence_absent_as_infra(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The Crown-v11 protection: a 4104 the broker could not confirm, while
        # the grant still sits at a few percent of the wall, must not bill the
        # miner. Missing activation evidence is a mixed-rollout gap.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        await _seed_revoked_grant(
            session_maker,
            agent_id,
            status="exhausted",
            prompt_tokens=3_000_000,
            completion_tokens=50_000,
            token_budget=75_000_000,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id,
                reason="infrastructure",
                failure_detail="model_relay_unavailable:budget_evidence_absent",
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == 1
            assert ticket.failure_reason == "infrastructure"
            assert (
                ticket.failure_detail
                == "model_relay_unavailable:budget_evidence_absent"
            )

    async def test_infra_retry_grants_are_bounded(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A persistent validator-side outage cannot re-lease one agent forever:
        # infra grants stop climbing at the cap.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        async with session_maker() as s, s.begin():
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            ticket.infra_retry_grants = MAX_INFRA_RETRY_GRANTS
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id, reason="infrastructure"),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == MAX_INFRA_RETRY_GRANTS

    async def test_failure_detail_is_recorded_on_the_ticket(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # ditto-subnet#279 deliverable (1). The three-value `reason` says how the
        # platform should respond and nothing about what happened, which is why
        # twelve `infrastructure` verdicts could not name which of the
        # validator's five sandbox codes killed the run. The code now survives on
        # the ticket instead of only in a log line on the validator host.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id,
                reason="infrastructure",
                failure_detail="sandbox_network_unavailable",
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.failure_reason == "infrastructure"
            assert ticket.failure_detail == "sandbox_network_unavailable"

    async def test_validator_omitting_failure_detail_still_succeeds(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Backward compatibility, pinned. A validator built before the field
        # exists sends a body with no `failure_detail` key at all. That must
        # still resolve the lease: a hand-back rejected as 422 would leave the
        # ticket to expire silently, which is precisely the ambiguity the field
        # was added to remove. No heartbeat protocol bump gates this -- the fail
        # route has its own signature and does not read protocol_version.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        body = _job_fail_payload(agent_id, reason="scoring_error")
        assert "failure_detail" not in body
        failed = await client.post(
            "/api/v1/validator/job/fail", headers=_AUTH_HEADER, json=body
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["reopened"] is True
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == TicketStatus.EXPIRED
            assert ticket.failure_reason == "scoring_error"
            assert ticket.failure_detail is None

    async def test_overlong_failure_detail_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The field is a diagnosis, not a log-shipping channel into a hot table.
        # The cap moved 200 -> 4096; what must not move is that there *is* one.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id,
                reason="infrastructure",
                failure_detail="x" * (FAILURE_DETAIL_MAX_LENGTH + 1),
            ),
        )
        assert failed.status_code == 422, failed.text

    async def test_diagnostic_message_past_the_old_bound_survives_intact(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The regression this widening exists for. This is the real 2026-07-27
        # value, the one that finally named a root cause three investigations had
        # missed. At the old 200-char bound it arrived cut at "inference r",
        # losing the clause that said what the platform actually did -- and the
        # surviving half read as a finished sentence, so nothing signalled that
        # anything was missing. Asserted end to end (HTTP body -> ticket row),
        # character for character, because a cap that truncates *quietly* is the
        # failure mode, not a cap as such.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        detail = (
            "DittobenchError: run 2b7c6b6c-ae45-493d-b8f5-b1a4a6ff8b3a failed: "
            "harness exhausted its inference allowance: agent-attributable "
            "inference decline: the platform rejected 81 of the harness's "
            "inference request(s) outright, before reserving any capacity"
        )
        assert len(detail) > LEGACY_FAILURE_DETAIL_MAX_LENGTH
        assert len(detail) <= FAILURE_DETAIL_MAX_LENGTH

        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id, reason="infrastructure", failure_detail=detail
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            # The whole message, not a prefix of it. `endswith` is asserted
            # separately so a future silent re-truncation fails loudly on the
            # clause that was lost, rather than on an opaque length mismatch.
            assert ticket.failure_detail == detail
            assert ticket.failure_detail.endswith("before reserving any capacity")

    async def test_a_detail_at_the_widened_cap_round_trips(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The boundary itself. `failure_detail` is a TEXT column, so nothing
        # narrower than the wire model can truncate it -- this is the assertion
        # that says so, and it is what would catch a future VARCHAR(n) being
        # introduced underneath the widened wire type.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        detail = "y" * FAILURE_DETAIL_MAX_LENGTH
        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id, reason="scoring_error", failure_detail=detail
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.failure_detail is not None
            assert len(ticket.failure_detail) == FAILURE_DETAIL_MAX_LENGTH
            assert ticket.failure_detail == detail

    async def test_legacy_validator_detail_at_the_old_bound_still_validates(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Fleet version skew, pinned in the direction that actually ships first.
        # Validators run mixed versions (0.34.1 through 0.37.3 concurrently at
        # time of writing) and every one of them truncates to 200 before sending.
        # Widening a max_length only ever admits more, so those reports must keep
        # validating byte-identically -- this is the test that says the widening
        # is additive rather than a re-specification of the field.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        detail = "z" * LEGACY_FAILURE_DETAIL_MAX_LENGTH
        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id, reason="infrastructure", failure_detail=detail
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.failure_detail == detail

    async def test_repeated_infrastructure_stops_minting_grants_per_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # ditto-subnet#279 deliverable (3), and the one that stops recurrence.
        # MAX_INFRA_RETRY_GRANTS bounds a *validator*, and every validator gets
        # its own eight, so an artifact that reliably reports a scorer-side
        # infrastructure code collects eight free attempts per validator that
        # ever touches it -- unbounded per agent. Here a second validator arrives
        # at an agent whose fleet total is already at the bound and is refused,
        # even though its own ticket has never earned a grant.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        await _seed_ticket(session_maker, agent_id, keypair=_KEYPAIRS[1])
        async with session_maker() as s, s.begin():
            first = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert first is not None
            first.infra_retry_grants = MAX_AGENT_INFRA_RETRY_GRANTS
        _install_db(app, session_maker)
        _install_chain(app)

        second_hotkey = _KEYPAIRS[1].ss58_address
        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers={"X-Validator-Hotkey": second_hotkey},
            json=_job_fail_payload(
                agent_id,
                keypair=_KEYPAIRS[1],
                reason="infrastructure",
                failure_detail="model_relay_unavailable",
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, second_hotkey)
            )
            assert ticket is not None
            # Refused: this failure bills the miner, so the ticket walks its
            # ordinary budget down to `exhausted` and the artifact surfaces on
            # the operator stuck-list instead of looping invisibly forever.
            assert ticket.infra_retry_grants == 0
            # And it cools all the way down rather than off its own count of
            # zero, so the fleet is not immediately handed the same artifact.
            assert ticket.retry_after is not None
            retry_after = ticket.retry_after
            if retry_after.tzinfo is None:
                retry_after = retry_after.replace(tzinfo=UTC)
            assert retry_after - datetime.now(UTC) > INFRA_RETRY_BACKOFF_CAP / 2
            # The diagnosis is still recorded; only the billing changed.
            assert ticket.failure_detail == "model_relay_unavailable"

    async def test_per_agent_bound_leaves_room_below_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The bound is generous on purpose: an agent's genuine budget at quorum
        # is 2 x 3 = 6 leases, and this hands one artifact twice that for free
        # before a single attempt is billed. One short of it still grants.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        await _seed_ticket(session_maker, agent_id, keypair=_KEYPAIRS[1])
        async with session_maker() as s, s.begin():
            first = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert first is not None
            first.infra_retry_grants = MAX_AGENT_INFRA_RETRY_GRANTS - 1
        _install_db(app, session_maker)
        _install_chain(app)

        second_hotkey = _KEYPAIRS[1].ss58_address
        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers={"X-Validator-Hotkey": second_hotkey},
            json=_job_fail_payload(
                agent_id, keypair=_KEYPAIRS[1], reason="infrastructure"
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, second_hotkey)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == 1

    async def test_per_agent_bound_does_not_apply_to_platform_revoked_leases(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The converse guarantee. Repetition on a *reported* infrastructure
        # verdict is evidence about the artifact, but repetition on a lease the
        # platform itself revoked is evidence about the platform -- and billing
        # the miner for that is exactly the rule #460/#497 settled in the other
        # direction. Only the per-ticket bound governs that path.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        await _seed_ticket(session_maker, agent_id, keypair=_KEYPAIRS[1])
        await _seed_revoked_grant(session_maker, agent_id, keypair=_KEYPAIRS[1])
        async with session_maker() as s, s.begin():
            first = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert first is not None
            first.infra_retry_grants = MAX_AGENT_INFRA_RETRY_GRANTS
        _install_db(app, session_maker)
        _install_chain(app)

        second_hotkey = _KEYPAIRS[1].ss58_address
        failed = await client.post(
            "/api/v1/validator/job/fail",
            headers={"X-Validator-Hotkey": second_hotkey},
            json=_job_fail_payload(
                agent_id, keypair=_KEYPAIRS[1], reason="scoring_error"
            ),
        )
        assert failed.status_code == 200, failed.text
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, second_hotkey)
            )
            assert ticket is not None
            assert ticket.infra_retry_grants == 1

    async def test_no_live_ticket_is_a_noop(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(agent_id),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"agent_id": str(agent_id), "reopened": False}

    async def test_wrong_deadline_does_not_close_the_ticket(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.post(
            "/api/v1/validator/job/fail",
            headers=_AUTH_HEADER,
            json=_job_fail_payload(
                agent_id, ticket_deadline=_TICKET_DEADLINE + timedelta(hours=1)
            ),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reopened"] is False
        async with session_maker() as s:
            ticket = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.status == TicketStatus.ISSUED

    async def test_header_mismatch_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        resp = await client.post(
            "/api/v1/validator/job/fail",
            headers={"X-Validator-Hotkey": _DAVE.ss58_address},
            json=_job_fail_payload(agent_id),
        )
        assert resp.status_code == 401
        assert resp.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_tampered_signature_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        forged = _job_fail_payload(agent_id)
        # Move the signed lease deadline without re-signing: the signature binds
        # (agent_id, ticket_deadline, nonce, requested_at), so it must not verify.
        forged["ticket_deadline"] = (_TICKET_DEADLINE + timedelta(hours=1)).isoformat()
        resp = await client.post(
            "/api/v1/validator/job/fail", headers=_AUTH_HEADER, json=forged
        )
        assert resp.status_code == 401

    async def test_replayed_nonce_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        claim = _job_fail_payload(agent_id)
        first = await client.post(
            "/api/v1/validator/job/fail", headers=_AUTH_HEADER, json=claim
        )
        replay = await client.post(
            "/api/v1/validator/job/fail", headers=_AUTH_HEADER, json=claim
        )
        assert first.status_code == 200, first.text
        assert replay.status_code == 409

    async def test_stale_request_is_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)

        stale = _job_fail_payload(
            agent_id, requested_at=datetime.now(UTC) - timedelta(minutes=3)
        )
        resp = await client.post(
            "/api/v1/validator/job/fail", headers=_AUTH_HEADER, json=stale
        )
        assert resp.status_code == 409


class TestSubmitScore:
    @staticmethod
    async def _activate_confirmation_epoch(
        session_maker: async_sessionmaker[AsyncSession], *, bench_version: int
    ) -> None:
        """Make ``bench_version`` live.

        Confirmation reconciliation converges the LIVE benchmark's cohort, so a
        score for a superseded epoch deliberately creates no work. These
        fixtures score at v9, so v9 has to be the activated benchmark.
        """
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=bench_version - 1,
                    desired_version=bench_version,
                    status="activated",
                    cohort_size=5,
                    activated_at=datetime.now(UTC) - timedelta(days=2),
                )
            )

    async def test_v9_quorum_reconciles_once_without_starting_confirmation_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        overrides = _v9_score_overrides()
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="a" * 64,
            dataset_version=9,
        )
        await self._activate_confirmation_epoch(session_maker, bench_version=9)
        _install_db(app, session_maker)
        _install_chain(app)
        reconcile = AsyncMock()
        monkeypatch.setattr(
            validator_endpoint,
            "reconcile_confirmation_candidates",
            reconcile,
        )

        for keypair in _KEYPAIRS:
            await _seed_ticket(
                session_maker, agent_id, keypair=keypair, bench_version=9
            )
            response = await client.post(
                f"/api/v1/validator/agent/{agent_id}/score",
                json=_score_payload(
                    agent_id,
                    run_id="run-v9-vector",
                    keypair=keypair,
                    **overrides,
                ),
            )
            assert response.status_code == 200, response.text

        reconcile.assert_awaited_once()
        awaited = reconcile.await_args
        assert awaited is not None
        assert len(awaited.kwargs["finalized_scores"]) == 3
        assert awaited.kwargs["finalized_agent"].agent_id == agent_id

    async def test_quorum_for_a_superseded_epoch_creates_no_confirmation_work(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A straggler score for a dead epoch must not manufacture work.

        Confirmation converges the live cohort only. Reconciling a superseded
        epoch is how the lane accumulated bundles against submissions that no
        longer ranked, burning the daily cap on a cohort nobody would promote.
        """
        overrides = _v9_score_overrides()
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="a" * 64,
            dataset_version=9,
        )
        # The network has moved past the epoch these scores are reported for.
        await self._activate_confirmation_epoch(session_maker, bench_version=10)
        _install_db(app, session_maker)
        _install_chain(app)
        reconcile = AsyncMock()
        monkeypatch.setattr(
            validator_endpoint,
            "reconcile_confirmation_candidates",
            reconcile,
        )

        for keypair in _KEYPAIRS:
            await _seed_ticket(
                session_maker, agent_id, keypair=keypair, bench_version=9
            )
            response = await client.post(
                f"/api/v1/validator/agent/{agent_id}/score",
                json=_score_payload(
                    agent_id,
                    run_id="run-v9-vector",
                    keypair=keypair,
                    **overrides,
                ),
            )
            assert response.status_code == 200, response.text

        reconcile.assert_not_awaited()

    async def test_v9_reconciliation_failure_cannot_roll_back_canonical_quorum(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        overrides = _v9_score_overrides()
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="a" * 64,
            dataset_version=9,
        )
        await self._activate_confirmation_epoch(session_maker, bench_version=9)
        _install_db(app, session_maker)
        _install_chain(app)
        reconcile = AsyncMock(side_effect=RuntimeError("auxiliary projection failed"))
        monkeypatch.setattr(
            validator_endpoint,
            "reconcile_confirmation_candidates",
            reconcile,
        )

        response: httpx.Response | None = None
        for keypair in _KEYPAIRS:
            await _seed_ticket(
                session_maker, agent_id, keypair=keypair, bench_version=9
            )
            response = await client.post(
                f"/api/v1/validator/agent/{agent_id}/score",
                json=_score_payload(
                    agent_id,
                    run_id="run-v9-vector",
                    keypair=keypair,
                    **overrides,
                ),
            )
        assert response is not None
        assert response.status_code == 200, response.text
        reconcile.assert_awaited_once()

        async with session_maker() as session:
            agent = await session.get(Agent, agent_id)
            score_count = await session.scalar(
                select(func.count())
                .select_from(Score)
                .where(Score.agent_id == agent_id)
            )
            scored_ticket_count = await session.scalar(
                select(func.count())
                .select_from(ValidatorTicket)
                .where(
                    ValidatorTicket.agent_id == agent_id,
                    ValidatorTicket.status == TicketStatus.SCORED,
                )
            )
        assert agent is not None and agent.status == AgentStatus.SCORED
        assert score_count == 3
        assert scored_ticket_count == 3

    async def test_accepts_digest_verified_v9_base_evidence_without_double_gate(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        overrides = _v9_score_overrides()
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="a" * 64,
            dataset_version=9,
        )
        await _seed_ticket(session_maker, agent_id, bench_version=9)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, run_id="run-v9-vector", **overrides),
        )
        assert response.status_code == 200, response.text

        async with session_maker() as session:
            score = await session.get(Score, (agent_id, 9, _VALIDATOR_HOTKEY))
            assert score is not None
            assert score.details is not None
            assert (
                score.details["base_evidence_sha256"]
                == overrides["base_evidence_sha256"]
            )
            assert "platform_model_use_reconciliation" in score.details
            assert "model_use" not in score.details

    async def test_v9_base_evidence_must_bind_the_agent_artifact(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="f" * 64,
            dataset_version=9,
        )
        await _seed_ticket(session_maker, agent_id, bench_version=9)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id, run_id="run-v9-vector", **_v9_score_overrides()
            ),
        )
        assert response.status_code == 409
        assert "artifact digest" in response.text

    async def test_v9_base_evidence_tamper_fails_before_signature_verification(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="a" * 64,
            dataset_version=9,
        )
        await _seed_ticket(session_maker, agent_id, bench_version=9)
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _score_payload(
            agent_id, run_id="run-v9-vector", **_v9_score_overrides()
        )
        payload["report"]["details"]["v9_base"]["score_gates"]["model_use"][
            "successful_inference_cases"
        ] = 3

        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score", json=payload
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("run_id",), "different-run"),
            (("details", "dataset_sha256"), "0" * 64),
            (("details", "transcript_sha256"), "1" * 64),
            (("composite",), 0.7),
        ],
    )
    async def test_authoritative_v9_submit_rejects_root_identity_tampering(
        self,
        path: tuple[str, ...],
        value: object,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="a" * 64,
            dataset_version=9,
        )
        await _seed_ticket(session_maker, agent_id, bench_version=9)
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _score_payload(
            agent_id, run_id="run-v9-vector", **_v9_score_overrides()
        )
        target = payload["report"]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score", json=payload
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "purpose",
        [TicketPurpose.CONTINUAL_RETEST, TicketPurpose.LEGACY_UNCLASSIFIED],
    )
    async def test_rejects_noncanonical_ticket_purpose(
        self,
        purpose: TicketPurpose,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id, purpose=purpose)
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )

        assert response.status_code == 409
        assert "not authorized for canonical scoring" in response.text

    async def test_accepts_grandfathered_inflight_canonical_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(
            session_maker,
            agent_id,
            purpose=TicketPurpose.LEGACY_UNCLASSIFIED,
            purpose_revision=0,
            legacy_completion_allowed=True,
        )
        _install_db(app, session_maker)
        _install_chain(app)

        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )

        assert response.status_code == 200, response.text
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
        assert ticket is not None
        assert ticket.purpose == TicketPurpose.CANONICAL_QUORUM
        assert ticket.purpose_revision == 1
        assert ticket.legacy_completion_allowed is False

    async def test_validator_ticket_binds_seed_and_dataset_digest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        pinned_seed = 8675309
        pinned_digest = "ab" * 32
        await _seed_ticket(
            session_maker,
            agent_id,
            seed=pinned_seed,
            dataset_sha256=pinned_digest,
        )

        missing_digest = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, seed=pinned_seed),
        )
        assert missing_digest.status_code == 409
        assert "dataset digest" in missing_digest.text

        wrong_digest = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                seed=pinned_seed,
                details={"dataset_sha256": "cd" * 32},
            ),
        )
        assert wrong_digest.status_code == 409
        assert "dataset digest" in wrong_digest.text

        wrong_seed = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                seed=pinned_seed + 1,
                details={"dataset_sha256": pinned_digest},
            ),
        )
        assert wrong_seed.status_code == 409
        assert "score seed" in wrong_seed.text

        accepted = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                seed=pinned_seed,
                details={"dataset_sha256": pinned_digest},
            ),
        )
        assert accepted.status_code == 200, accepted.text

    async def test_post_legacy_ticket_requires_explicit_bench_version_binding(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A post-v2 lease is only satisfiable by a report that binds it.

        The binding is enforced twice: the ticket lookup pins a version-less
        report to LEGACY_BENCH_VERSION (so it can never find a post-v2 lease),
        and the endpoint re-checks the bound version against the lease it found.

        This used to range over ticket_version [3, 4] to show the rule was not
        keyed to whichever version happened to be the canary. That axis is gone,
        and unlike the retired-ticket test next door it cannot simply move to
        the retired side: the accepting case has to WRITE a score, and only v7
        is writable. One era is all there is, which is the same inherent
        narrowing, not a weaker test.

        The version-less case also changed answer. It used to 409 ("no open
        scoring ticket") because it looked for a v2 lease and found none. It now
        410s before the lookup happens: pinning to LEGACY_BENCH_VERSION means
        pinning to a RETIRED era, and that is terminal rather than a conflict.
        Asserting the new code here is what stops a future reader reading the
        410 as a regression.
        """
        ticket_version = _BENCH_VERSION
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        await _seed_ticket(session_maker, agent_id, bench_version=ticket_version)

        # A version-less (legacy-shaped) report still cannot consume a post-v2
        # lease. It is pinned to LEGACY_BENCH_VERSION, and that era is retired,
        # so the refusal is now terminal (410) rather than a conflict (409).
        unbound = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, bench_version=None),
        )
        assert unbound.status_code == 410
        assert unbound.json()["error_code"] == ERROR_CODE_BENCH_VERSION_RETIRED

        # Binding the WRONG post-v2 version is refused too.
        mismatched = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, bench_version=ticket_version + 1),
        )
        assert mismatched.status_code == 409

        # Binding the lease's own version is accepted.
        bound = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, bench_version=ticket_version),
        )
        assert bound.status_code == 200, bound.text
        async with session_maker() as session:
            stored = await session.get(
                Score, (agent_id, ticket_version, _VALIDATOR_HOTKEY)
            )
            assert stored is not None

    async def test_rejects_score_until_current_screening_policy_passes(
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
        _install_db(app, session_maker)
        _install_chain(app)
        await _seed_ticket(session_maker, agent_id)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )
        assert response.status_code == 409
        async with session_maker() as session:
            assert (
                await session.get(Score, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY))
                is None
            )

    async def test_records_score_and_finalizes(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        # A single below-quorum score records the row but keeps the agent
        # provisional (evaluating) — no finalization until the k=3 quorum.
        await _seed_ticket(session_maker, agent_id, keypair=_KEYPAIRS[0])
        first = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, keypair=_KEYPAIRS[0]),
        )
        assert first.status_code == 200
        assert first.json()["status"] == AgentStatus.EVALUATING

        # The quorum-th score finalizes it on the median composite.
        response = await _score_to_quorum(
            client, agent_id, maker=session_maker, composite=0.82
        )
        assert response.status_code == 200
        body = response.json()
        assert body["agent_id"] == str(agent_id)
        assert body["status"] == AgentStatus.SCORED
        assert body["accepted"] is True

        # A scores row landed and the agent transitioned.
        async with session_maker() as s:
            score = await s.get(Score, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY))
            assert score is not None
            assert score.composite == pytest.approx(0.82)
            agent = await s.get(Agent, agent_id)
            assert agent is not None
            assert agent.status == AgentStatus.SCORED

    async def test_score_commit_does_not_activate_operator_retest_queue(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A finished score never widens into validator-wide re-test locks."""
        activate = AsyncMock(
            side_effect=AssertionError("score commit entered the re-test lock lane")
        )
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.activate_next_score_retest",
            activate,
        )
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        await _seed_ticket(session_maker, agent_id)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )

        assert response.status_code == 200, response.text
        activate.assert_not_awaited()

    async def test_finalized_score_retest_hot_swaps_without_leaving_finalized_state(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.db.queries.audit import (
            EVENT_FINALIZED,
            EVENT_SCORE,
            EVENT_SCORE_INVALIDATED,
            EVENT_SCORE_RETEST_REQUESTED,
            list_audit_entries,
        )

        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        await _score_to_quorum(
            client, agent_id, maker=session_maker, run_id="original", composite=0.82
        )
        token = "test-admin-token-at-least-32-characters"
        app.state.config = replace(app.state.config, admin_api_token=token)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Admin-Actor": "operator",
        }
        inspect = await client.get(
            f"/api/v1/admin/validation-retries/{agent_id}/validators/{_VALIDATOR_HOTKEY}",
            headers=headers,
        )
        assert inspect.status_code == 200, inspect.text
        request_id = uuid4()
        requested = await client.post(
            f"/api/v1/admin/validation-retries/{agent_id}/validators/{_VALIDATOR_HOTKEY}/replace-score",
            headers=headers,
            json={
                "request_id": str(request_id),
                "expected_snapshot": inspect.json()["snapshot"],
                "expected_run_id": "original_0",
                "reason": (
                    "Outlying validator result requires an exact same-validator re-test"
                ),
            },
        )
        assert requested.status_code == 200, requested.text
        async with session_maker() as session:
            preserved = await session.get(
                Score, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            agent = await session.get(Agent, agent_id)
        assert preserved is not None and preserved.run_id == "original_0"
        assert agent is not None and agent.status == AgentStatus.SCORED

        deadline = datetime.fromisoformat(
            requested.json()["replacement_deadline"].replace("Z", "+00:00")
        )
        replacement = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                keypair=_KEYPAIRS[0],
                run_id="replacement_0",
                composite=0.91,
                ticket_deadline=deadline,
            ),
        )
        assert replacement.status_code == 200, replacement.text
        assert replacement.json()["status"] == AgentStatus.SCORED
        async with session_maker() as session:
            swapped = await session.get(
                Score, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            scores = list(
                (
                    await session.scalars(
                        select(Score).where(Score.agent_id == agent_id)
                    )
                ).all()
            )
            entries = await list_audit_entries(session, limit=1000)
        assert swapped is not None and swapped.run_id == "replacement_0"
        assert swapped.composite == pytest.approx(0.91)
        assert len(scores) == 3
        lifecycle = [
            entry.event
            for entry in entries
            if entry.agent_id == agent_id
            and entry.event
            in {
                EVENT_SCORE_RETEST_REQUESTED,
                EVENT_SCORE_INVALIDATED,
                EVENT_SCORE,
                EVENT_FINALIZED,
            }
        ]
        assert lifecycle[-4:] == [
            EVENT_SCORE_RETEST_REQUESTED,
            EVENT_SCORE_INVALIDATED,
            EVENT_SCORE,
            EVENT_FINALIZED,
        ]

    async def test_finalize_writes_verifiable_audit_chain(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.db.queries.audit import (
            EVENT_FINALIZED,
            EVENT_SCORE,
            list_audit_entries,
            verify_audit_chain,
        )

        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        # One below-quorum score, then the k=3 quorum (which re-scores validator 0
        # with a fresh ticket): 4 append-only score events + 1 finalize.
        await _seed_ticket(session_maker, agent_id, keypair=_KEYPAIRS[0])
        await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, keypair=_KEYPAIRS[0]),
        )
        await _score_to_quorum(client, agent_id, maker=session_maker, composite=0.82)

        async with session_maker() as s:
            entries = await list_audit_entries(s, limit=1000)
        # Append-only: the re-score is its own entry even though the table upserts.
        score_entries = [e for e in entries if e.event == EVENT_SCORE]
        finalized = [e for e in entries if e.event == EVENT_FINALIZED]
        assert len(score_entries) == 4
        assert len(finalized) == 1
        assert entries[-1].event == EVENT_FINALIZED
        # The finalize entry carries the median + quorum + scoring validators.
        fin = finalized[0].payload
        assert fin["median_composite"] == pytest.approx(0.82)
        assert fin["quorum"] == 3
        assert fin["score_count"] == 3
        assert len(fin["validator_hotkeys"]) == 3
        assert fin["status"] == AgentStatus.SCORED.value
        # The whole chain replays and verifies (tamper-evident end to end).
        assert verify_audit_chain(entries) is True

    async def test_stamps_current_bench_version_when_omitted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A report that omits bench_version (as the default payload does) must be
        # stamped with the current version so it is never recorded as legacy.

        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        await _seed_ticket(session_maker, agent_id)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )
        assert response.status_code == 200

        async with session_maker() as s:
            score = await s.get(Score, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY))
            assert score is not None
            assert score.details is not None
            # The payload now declares bench_version explicitly, so stamping
            # preserves it rather than overwriting with CURRENT: a report that
            # genuinely ran an older contract stays honestly labelled, and the
            # label matches the row's key.
            assert score.details["bench_version"] == _BENCH_VERSION
            assert score.details["ticket_deadline"] == (
                _TICKET_DEADLINE.isoformat(timespec="microseconds")
            )

    async def test_overwrites_advisory_detail_with_ticket_bench_version(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The locked ticket, not unsigned scorer details, owns benchmark identity.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        await _seed_ticket(session_maker, agent_id)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, details={"bench_version": 1}),
        )
        assert response.status_code == 200

        async with session_maker() as s:
            score = await s.get(Score, (agent_id, _BENCH_VERSION, _VALIDATOR_HOTKEY))
            assert score is not None
            assert score.details is not None
            assert score.details["bench_version"] == _BENCH_VERSION

    async def test_one_ticket_one_score_no_rescore(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # One ticket, one score: a validator's first score is accepted and
        # consumes its ticket; a second score without a fresh ticket is rejected
        # (409), so a validator cannot re-roll for a better number.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        await _seed_ticket(session_maker, agent_id)
        r1 = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, run_id="run_a", composite=0.5),
        )
        assert r1.status_code == 200
        # Transport retries of the exact signed request return the original
        # acceptance instead of turning a committed score into a false failure.
        exact_retry = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, run_id="run_a", composite=0.5),
        )
        assert exact_retry.status_code == 200
        assert exact_retry.json()["accepted"] is True
        # Ticket spent: the re-score has no open ticket.
        r2 = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, run_id="run_b", composite=0.9),
        )
        assert r2.status_code == 409

        async with session_maker() as s:
            from ditto.db.queries.scores import list_scores_for_agent

            scores = await list_scores_for_agent(s, agent_id=agent_id)
            assert len(scores) == 1
            assert scores[0].run_id == "run_a"  # the first (only) score stands
            assert scores[0].composite == pytest.approx(0.5)

    async def test_exact_retry_survives_quorum_finalization(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        final_payload: dict[str, object] | None = None
        for index, keypair in enumerate(_KEYPAIRS):
            await _seed_ticket(session_maker, agent_id, keypair=keypair)
            payload = _score_payload(
                agent_id,
                run_id=f"finalize_{index}",
                keypair=keypair,
                composite=0.82,
            )
            response = await client.post(
                f"/api/v1/validator/agent/{agent_id}/score", json=payload
            )
            assert response.status_code == 200, response.text
            final_payload = payload

        assert final_payload is not None
        retry = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score", json=final_payload
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["status"] == AgentStatus.SCORED

    async def test_superseded_ticket_lease_rejects_late_score(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        old_deadline = _TICKET_DEADLINE
        new_deadline = old_deadline + timedelta(hours=1)
        await _seed_ticket(session_maker, agent_id, deadline=new_deadline)

        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, ticket_deadline=old_deadline),
        )

        assert response.status_code == 409
        assert "no open scoring ticket" in response.json()["message"]

    async def test_operator_evicted_lease_rejects_a_late_score(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A validator still mid-run on an evicted lease must fail cleanly.

        The operator eviction route revokes a live lease before its deadline, so
        the validator holding it may finish its benchmark minutes later and post
        a perfectly well-formed, correctly-signed score for work the platform has
        already written off. That has to be a plain refusal — not a crash for the
        validator, and not a row in the ledger for an era the submission has been
        removed from.
        """
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        _install_db(app, session_maker)
        _install_chain(app)
        token = "test-admin-token-at-least-32-characters"
        app.state.config = replace(app.state.config, admin_api_token=token)
        admin_headers = {
            "Authorization": f"Bearer {token}",
            "X-Admin-Actor": "operator",
        }

        detail = await client.get(
            f"/api/v1/admin/validation-retries/{agent_id}", headers=admin_headers
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["live_ticket_count"] == 1
        eviction = await client.post(
            f"/api/v1/admin/validation-retries/{agent_id}/evict",
            headers=admin_headers,
            json={
                "request_id": str(uuid4()),
                "expected_snapshot": detail.json()["snapshot"],
                "reason": "hangs every lease and reports nothing; freeing the fleet",
                "confirmation": "EVICT LIVE VALIDATOR LEASES",
            },
        )
        assert eviction.status_code == 200, eviction.text
        assert eviction.json()["freed_slots"] == 1

        late = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, run_id="late_after_eviction"),
        )

        assert late.status_code == 409
        assert "no open scoring ticket" in late.json()["message"]
        async with session_maker() as session:
            from ditto.db.queries.scores import list_scores_for_agent

            assert await list_scores_for_agent(session, agent_id=agent_id) == []
            agent = await session.get(Agent, agent_id)
        assert agent is not None and agent.status == AgentStatus.EVALUATING

    async def test_bad_signature_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        payload = _score_payload(agent_id)
        payload["signature"] = "ab" * 64  # well-formed but wrong
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score", json=payload
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_unpermitted_validator_returns_401(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app, permitted=False)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

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
            f"/api/v1/validator/agent/{aid}/score", json=_score_payload(aid)
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_FOUND

    async def test_non_scoreable_status_returns_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.UPLOADED)
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == ERROR_CODE_AGENT_NOT_EVALUATABLE

    async def test_re_score_live_agent_keeps_live(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.LIVE)
        _install_db(app, session_maker)
        _install_chain(app)
        await _seed_ticket(session_maker, agent_id)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id),
        )
        assert response.status_code == 200
        assert response.json()["status"] == AgentStatus.LIVE

    async def test_out_of_range_composite_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, composite=1.5),
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == ERROR_CODE_VALIDATION

    async def test_out_of_range_seed_returns_422(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A seed outside signed int64 would 500 at the BigInteger insert; it must
        # be a clean 422 before signing/DB work.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(agent_id, seed=2**63),
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == ERROR_CODE_VALIDATION

    async def test_cross_agent_replay_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A signature valid for agent A must not be accepted for agent B: the
        # signed payload binds the agent id.
        agent_a = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        agent_b = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _score_payload(agent_a)  # signed for A
        response = await client.post(
            f"/api/v1/validator/agent/{agent_b}/score", json=payload
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH

    async def test_tampered_composite_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The composite is signed: altering it after signing invalidates the sig.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        payload = _score_payload(agent_id, composite=0.50)
        payload["report"]["composite"] = 0.99  # tamper post-signing
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score", json=payload
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == ERROR_CODE_VALIDATOR_AUTH


_MINER_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


class TestAntiCopyGate:
    """The score-write path holds a suspected copy in ath_pending_review."""

    async def _score(
        self,
        client: httpx.AsyncClient,
        agent_id: UUID,
        *,
        maker: async_sessionmaker[AsyncSession],
        run_id: str,
        composite: float,
    ) -> httpx.Response:
        # Score to the k=3 quorum so the agent finalizes and the gate runs on
        # the median (= composite, since all three validators post the same).
        return await _score_to_quorum(
            client, agent_id, maker=maker, run_id=run_id, composite=composite
        )

    async def test_exact_copy_is_held(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        # Incumbent scores + becomes eligible.
        incumbent = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, sha256="cc" * 32
        )
        await self._score(
            client, incumbent, maker=session_maker, run_id="run_inc", composite=0.80
        )
        # A byte-identical resubmission from another miner.
        copy = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=_MINER_B,
            sha256="cc" * 32,
        )
        resp = await self._score(
            client, copy, maker=session_maker, run_id="run_copy", composite=0.80
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == AgentStatus.ATH_PENDING_REVIEW

        async with session_maker() as s:
            held = await s.get(Agent, copy)
            review = await s.scalar(select(AthReview).where(AthReview.agent_id == copy))
            assert held is not None
            assert review is not None
            assert held.status == AgentStatus.ATH_PENDING_REVIEW
            assert held.duplicate_of == incumbent
            assert "sha256" in (held.review_reason or "")
            assert review.original_evidence["sha256"] == "cc" * 32
            assert review.original_evidence["score_count"] == 3
            assert review.original_evidence["previous_status"] == AgentStatus.SCORED
            provenance = reference_corpus_provenance()
            assert review.algorithm_provenance == {
                "snapshot": "score-finalization",
                "algorithm_version": "reference-aware-v2",
                "canonical_reference_revision": provenance["revision"],
                "reference_corpus_id": provenance["corpus_id"],
                "reference_exclusion_mode": (
                    "starter-kit-mainline-history+stock-file-exclusion"
                ),
                "backfilled": False,
                "opened_at_source": "agent_finalized_audit",
            }

    async def test_resubmission_of_a_rejected_artifact_is_held(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A rejected artifact re-uploaded by its own owner is held again.

        Every anti-copy rule skips the candidate's own owner, so before the
        rejected-resubmission gate this path was wide open: reject an artifact
        and the same miner re-uploads it minutes later as a fresh agent row,
        which screens, scores and ranks with nothing to match it against.
        """
        _install_db(app, session_maker)
        _install_chain(app)
        rejected = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, sha256="ce" * 32
        )
        await self._score(
            client, rejected, maker=session_maker, run_id="run_rej", composite=0.80
        )
        # The operator adjudicates against it: `reject` lands as BANNED.
        async with session_maker() as s, s.begin():
            banned = await s.get(Agent, rejected)
            assert banned is not None
            banned.status = AgentStatus.BANNED
        # The same miner re-uploads the same bytes under a new agent row.
        again = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="ce" * 32,
        )
        await self._score(
            client, again, maker=session_maker, run_id="run_again", composite=0.80
        )
        async with session_maker() as s:
            held = await s.get(Agent, again)
            review = await s.scalar(
                select(AthReview).where(AthReview.agent_id == again)
            )
            assert held is not None
            assert review is not None
            assert held.status == AgentStatus.ATH_PENDING_REVIEW
            assert held.duplicate_of == rejected
            assert "rejected artifact" in (held.review_reason or "")

    async def test_upload_predating_the_rejection_is_not_held(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The gate is prospective: it never reaches back past its own decision.

        `Crown-v11-v2` was uploaded fourteen minutes before its predecessor was
        held, so an in-flight submission must not inherit a rejection that did
        not exist when it arrived. The operator still reviews it on the merits;
        it just is not held automatically by this gate.
        """
        _install_db(app, session_maker)
        _install_chain(app)
        rejected = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, sha256="cf" * 32
        )
        await self._score(
            client, rejected, maker=session_maker, run_id="run_rej2", composite=0.80
        )
        # The resubmission arrives BEFORE the artifact it resembles was uploaded.
        async with session_maker() as s, s.begin():
            ancestor = await s.get(Agent, rejected)
            assert ancestor is not None
            ancestor.status = AgentStatus.BANNED
            ancestor.created_at = datetime.now(UTC) + timedelta(hours=1)
        early = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="cf" * 32,
        )
        await self._score(
            client, early, maker=session_maker, run_id="run_early", composite=0.80
        )
        async with session_maker() as s:
            agent = await s.get(Agent, early)
            assert agent is not None
            assert agent.status == AgentStatus.SCORED

    async def test_exact_copy_is_still_held_with_source_review_bypassed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """``deferred_source_review.mode="bypass"`` is not "no plagiarism check".

        The mode names the SOURCE-INTEGRITY branch and nothing else. Copy holds
        come from the duplicate-signal decision at score finalization, which
        never reads that policy, so the whole anti-copy gate must survive the
        one setting an operator is most likely to read as "screening off".
        """
        _install_db(app, session_maker)
        _install_chain(app)
        await _install_deferred_review_mode(app, session_maker, "bypass")
        incumbent = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, sha256="cd" * 32
        )
        await self._score(
            client, incumbent, maker=session_maker, run_id="run_inc_np", composite=0.80
        )
        copy = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=_MINER_B,
            sha256="cd" * 32,
        )
        resp = await self._score(
            client, copy, maker=session_maker, run_id="run_copy_np", composite=0.80
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == AgentStatus.ATH_PENDING_REVIEW
        async with session_maker() as s:
            held = await s.get(Agent, copy)
            review = await s.scalar(select(AthReview).where(AthReview.agent_id == copy))
            assert held is not None
            assert review is not None
            assert held.status == AgentStatus.ATH_PENDING_REVIEW
            assert held.duplicate_of == incumbent
            assert review.status == "pending"
            assert review.original_duplicate_of == incumbent
            assert (
                review.algorithm_provenance["opened_at_source"]
                == "agent_finalized_audit"
            )

    async def test_near_dup_dethroner_is_held(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        incumbent = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="aa" * 32,
            size_bytes=500000,
        )
        await self._score(
            client, incumbent, maker=session_maker, run_id="run_inc", composite=0.80
        )
        # Different bytes, near-identical size, beats incumbent by a hair.
        tweaked = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=_MINER_B,
            sha256="bb" * 32,
            size_bytes=500100,
        )
        resp = await self._score(
            client, tweaked, maker=session_maker, run_id="run_tweak", composite=0.805
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == AgentStatus.ATH_PENDING_REVIEW

    async def test_later_scored_upload_is_not_original_for_earlier_submission(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        earlier_time = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
        later = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=_MINER_B,
            sha256="aa" * 32,
            size_bytes=500000,
            created_at=earlier_time + timedelta(hours=1),
        )
        await self._score(
            client, later, maker=session_maker, run_id="run_later", composite=0.80
        )
        earlier = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="aa" * 32,
            size_bytes=500100,
            created_at=earlier_time,
        )
        resp = await self._score(
            client, earlier, maker=session_maker, run_id="run_earlier", composite=0.805
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == AgentStatus.SCORED

    async def test_genuine_improvement_not_held(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        incumbent = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            sha256="aa" * 32,
            size_bytes=500000,
        )
        await self._score(
            client, incumbent, maker=session_maker, run_id="run_inc", composite=0.80
        )
        better = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=_MINER_B,
            sha256="bb" * 32,
            size_bytes=700000,
        )
        resp = await self._score(
            client, better, maker=session_maker, run_id="run_better", composite=0.92
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == AgentStatus.SCORED

    async def test_rescore_of_held_agent_stays_held_no_409(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        incumbent = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, sha256="cc" * 32
        )
        await self._score(
            client, incumbent, maker=session_maker, run_id="run_inc", composite=0.80
        )
        copy = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=_MINER_B,
            sha256="cc" * 32,
        )
        await self._score(
            client, copy, maker=session_maker, run_id="run_copy", composite=0.80
        )
        # Re-scoring a held agent must not 409 and must not un-hold it.
        resp = await self._score(
            client, copy, maker=session_maker, run_id="run_copy2", composite=0.81
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == AgentStatus.ATH_PENDING_REVIEW

    async def test_copy_of_a_subnet_published_artifact_is_not_held(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The white-bolt / red-dragon shape, end to end through the score path.

        Identical setup to ``test_exact_copy_is_held`` with one fact added: the
        incumbent reigned, its weights were confirmed on-chain, and the embargo
        lapsed before the second submission was uploaded -- so the subnet itself
        had already published that source. The submission scores normally, and
        the match is recorded on the audit chain rather than on the agent.
        """
        _install_db(app, session_maker)
        _install_chain(app)
        incumbent = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, sha256="cc" * 32
        )
        await self._score(
            client, incumbent, maker=session_maker, run_id="run_inc", composite=0.80
        )
        confirmed_at = datetime.now(UTC) - timedelta(hours=200)
        async with session_maker() as s, s.begin():
            await record_first_crowned(
                s, agent_id=incumbent, now=confirmed_at - timedelta(hours=1)
            )
            await record_weight_confirmed(s, agent_id=incumbent, now=confirmed_at)
        # Uploaded after the 120-hour window elapsed: the source was public.
        derived = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            miner_hotkey=_MINER_B,
            sha256="cc" * 32,
        )

        resp = await self._score(
            client, derived, maker=session_maker, run_id="run_derived", composite=0.80
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == AgentStatus.SCORED
        async with session_maker() as s:
            cleared = await s.get(Agent, derived)
            review = await s.scalar(
                select(AthReview).where(AthReview.agent_id == derived)
            )
            assert cleared is not None
            assert cleared.status == AgentStatus.SCORED
            # Not branded: `duplicate_of` and `review_reason` are the hold record.
            assert cleared.duplicate_of is None
            assert cleared.review_reason is None
            assert review is None
            entry = await s.scalar(
                select(ScoreAuditEntry).where(
                    ScoreAuditEntry.agent_id == derived,
                    ScoreAuditEntry.event == EVENT_COPY_NO_OPPORTUNITY,
                )
            )
            assert entry is not None
            assert entry.payload["kind"] == "public_release"
            assert entry.payload["signal"] == "exact_byte"
            assert entry.payload["source_agent_id"] == str(incumbent)
            assert entry.payload["disclosure"] == "public"


class TestPublicMirror:
    """The finalize hook mirrors the run record to the public bucket."""

    @pytest.fixture(autouse=True)
    async def _current_era(
        self, app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Put the fleet on the era these tests lease work in.

        Two things that used to come for free at v2 have to be stated now.
        ``active_bench_version`` answers ``DEFAULT_BENCH_VERSION`` (2) when no
        rollout row exists, and 2 is beneath the ticket floor, so the allocator
        cuts a lease the database refuses -- a 500 with nothing in it about
        benchmarks. And every v7 lease must carry an inference grant, so the
        routing policy has to exist before a job can be issued at all.

        The third precondition -- a heartbeat the fleet counts as v7-capable --
        is deliberately NOT seeded here: several tests in these classes are
        about the absence or the wrong shape of that heartbeat. Tests that want
        a job issued call ``_seed_capable_pool``.
        """
        await _seed_activated_era(session_maker)
        await _install_ticket_inference(app, session_maker)

    async def test_finalize_publishes_when_public_bucket_configured(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock()
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _score_to_quorum(
            client, agent_id, maker=session_maker, run_id="run_pub", composite=0.5
        )
        assert storage.put_object.await_count == 2
        versioned, current = storage.put_object.await_args_list
        assert versioned.kwargs["key"] == f"scored/{agent_id}/v{_BENCH_VERSION}.json"
        kwargs = current.kwargs
        assert kwargs["bucket"] == "ditto-public"
        assert kwargs["key"] == f"scored/{agent_id}.json"
        assert versioned.kwargs["body"] == kwargs["body"]
        record = json.loads(kwargs["body"])
        assert record["median_composite"] == 0.5
        assert len(record["scores"]) == 3
        assert all(sc["signature"] for sc in record["scores"])
        assert record["status"] == AgentStatus.SCORED.value

    async def test_finalize_skips_publish_when_unconfigured(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = None
        storage.put_object = AsyncMock()
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _score_to_quorum(
            client, agent_id, maker=session_maker, run_id="run_nopub", composite=0.5
        )
        storage.put_object.assert_not_awaited()

    async def test_migrated_scored_agent_publishes_new_version_at_quorum(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock()
        # The v7 dataset pin this test used to add by hand now comes from
        # ``_seed_agent``, which pins one for the fixture era.
        agent_id = await _seed_agent(session_maker, status=AgentStatus.SCORED)

        for index, keypair in enumerate(_KEYPAIRS):
            await _seed_ticket(
                session_maker, agent_id, keypair=keypair, bench_version=7
            )
            response = await client.post(
                f"/api/v1/validator/agent/{agent_id}/score",
                json=_score_payload(
                    agent_id,
                    keypair=keypair,
                    run_id=f"run_v7_{index}",
                    bench_version=7,
                    n=206,
                    details={"bench_version": 7},
                ),
            )
            assert response.status_code == 200, response.text

        assert storage.put_object.await_count == 2
        assert [
            awaited.kwargs["key"] for awaited in storage.put_object.await_args_list
        ] == [f"scored/{agent_id}/v7.json", f"scored/{agent_id}.json"]
        record = json.loads(storage.put_object.await_args.kwargs["body"])
        assert record["bench_version"] == 7
        assert record["dataset_sha256"] == "cd" * 32
        assert len(record["scores"]) == 3

    async def test_versioned_publish_failure_does_not_block_current_alias(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock(side_effect=(RuntimeError("versioned"), None))
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)

        await _score_to_quorum(
            client, agent_id, maker=session_maker, run_id="run_alias", composite=0.5
        )

        assert storage.put_object.await_count == 2
        assert storage.put_object.await_args_list[-1].kwargs["key"] == (
            f"scored/{agent_id}.json"
        )


class TestTranscriptPublication:
    """Offline-reproducibility hardening (v3 review finding 3): the transcript
    digest is bound into the score signature, and the transcript upload path
    only ever stores bytes that hash to a digest a signed score declared."""

    _TRANSCRIPT = b'{"run_id":"run_t_0","cases":[{"case_id":"a","response":{}}]}'
    _digest = hashlib.sha256(_TRANSCRIPT).hexdigest()

    async def test_score_signature_binds_transcript_digest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                run_id="run_t_0",
                details={"transcript_sha256": self._digest},
            ),
        )
        assert response.status_code == 200, response.text

    async def test_transcript_digest_outside_signature_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # A report that declares a digest the signature does not cover must be
        # rejected: otherwise the artifact binding would be spoofable.
        _install_db(app, session_maker)
        _install_chain(app)
        _install_storage(app)
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _seed_ticket(session_maker, agent_id)
        payload = _score_payload(agent_id, run_id="run_t_0")
        payload["report"]["details"] = {"transcript_sha256": self._digest}
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score", json=payload
        )
        assert response.status_code == 401

    async def _record_score_with_transcript(
        self,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        agent_id: UUID,
    ) -> None:
        await _seed_ticket(session_maker, agent_id)
        response = await client.post(
            f"/api/v1/validator/agent/{agent_id}/score",
            json=_score_payload(
                agent_id,
                run_id="run_t_0",
                details={"transcript_sha256": self._digest},
            ),
        )
        assert response.status_code == 200, response.text

    async def test_submit_transcript_stores_content_addressed(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock()
        storage.object_exists = AsyncMock(return_value=False)
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await self._record_score_with_transcript(client, session_maker, agent_id)

        response = await client.put(
            f"/api/v1/validator/agent/{agent_id}/transcript/run_t_0",
            content=self._TRANSCRIPT,
            headers={"X-Validator-Hotkey": _VALIDATOR_HOTKEY},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stored"] is True
        assert body["transcript_sha256"] == self._digest
        key = f"transcripts/{self._digest}.json"
        assert storage.put_object.await_args_list == [
            call(key=key, body=self._TRANSCRIPT, content_type="application/json"),
            call(
                key=key,
                body=self._TRANSCRIPT,
                content_type="application/json",
                bucket="ditto-public",
            ),
        ]

        # Idempotent: a re-upload of an existing object writes nothing new.
        storage.object_exists = AsyncMock(return_value=True)
        response = await client.put(
            f"/api/v1/validator/agent/{agent_id}/transcript/run_t_0",
            content=self._TRANSCRIPT,
            headers={"X-Validator-Hotkey": _VALIDATOR_HOTKEY},
        )
        assert response.status_code == 200
        assert storage.put_object.await_count == 2  # still exactly two writes

    async def test_submit_transcript_stores_without_public_mirror(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = None
        storage.put_object = AsyncMock()
        storage.object_exists = AsyncMock(return_value=False)
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await self._record_score_with_transcript(client, session_maker, agent_id)

        response = await client.put(
            f"/api/v1/validator/agent/{agent_id}/transcript/run_t_0",
            content=self._TRANSCRIPT,
            headers={"X-Validator-Hotkey": _VALIDATOR_HOTKEY},
        )

        assert response.status_code == 200
        assert response.json()["stored"] is True
        storage.put_object.assert_awaited_once_with(
            key=f"transcripts/{self._digest}.json",
            body=self._TRANSCRIPT,
            content_type="application/json",
        )

    async def test_submit_transcript_digest_mismatch_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock()
        storage.object_exists = AsyncMock(return_value=False)
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await self._record_score_with_transcript(client, session_maker, agent_id)

        response = await client.put(
            f"/api/v1/validator/agent/{agent_id}/transcript/run_t_0",
            content=b'{"tampered": true}',
            headers={"X-Validator-Hotkey": _VALIDATOR_HOTKEY},
        )
        assert response.status_code == 409
        storage.put_object.assert_not_awaited()

    async def test_submit_transcript_without_score_rejected(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock()
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)

        response = await client.put(
            f"/api/v1/validator/agent/{agent_id}/transcript/run_t_0",
            content=self._TRANSCRIPT,
            headers={"X-Validator-Hotkey": _VALIDATOR_HOTKEY},
        )
        assert response.status_code == 409
        storage.put_object.assert_not_awaited()

    async def test_publish_record_carries_transcript_refs(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        _install_db(app, session_maker)
        _install_chain(app)
        storage = _install_storage(app)
        storage.public_bucket = "ditto-public"
        storage.put_object = AsyncMock()
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        await _score_to_quorum(
            client,
            agent_id,
            maker=session_maker,
            run_id="run_t",
            composite=0.5,
            details={"transcript_sha256": self._digest},
        )
        kwargs = storage.put_object.await_args.kwargs
        record = json.loads(kwargs["body"])
        for sc in record["scores"]:
            assert sc["transcript_sha256"] == self._digest
            assert sc["transcript_key"] == f"transcripts/{self._digest}.json"


class TestMultiValidatorConsensus:
    @pytest.fixture(autouse=True)
    async def _current_era(
        self, app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Put the fleet on the era these tests lease work in.

        Same reason as the identical fixture on ``TestFailJob``: with no
        rollout row the platform reports the floor, and every v7 lease has to
        carry an inference grant, so the routing policy must exist before a job
        can be issued at all. At v2 both came for free.
        """
        await _seed_activated_era(session_maker)
        await _install_ticket_inference(app, session_maker)

    """The k=3 consensus semantics the decentralized design promises: the
    canonical score is the MEDIAN of the (differing) independent validator
    composites, the full per-validator record is exposed publicly, and an
    expired ticket re-opens the slot so a shut-out validator can pick the agent
    up. These exercise consensus correctness end to end, complementing the
    all-equal-composite quorum tests above."""

    async def test_finalizes_on_median_of_differing_scores(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # Three independent validators disagree. The platform must finalize on the
        # MEDIAN (0.82), never the mean (0.7067) or any single validator's number.
        composites = {_KEYPAIRS[0]: 0.40, _KEYPAIRS[1]: 0.82, _KEYPAIRS[2]: 0.90}
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)

        last: httpx.Response | None = None
        for i, (kp, comp) in enumerate(composites.items()):
            await _seed_ticket(session_maker, agent_id, keypair=kp)
            last = await client.post(
                f"/api/v1/validator/agent/{agent_id}/score",
                json=_score_payload(
                    agent_id, run_id=f"run_med_{i}", keypair=kp, composite=comp
                ),
            )
            assert last.status_code == 200, last.text
        assert last is not None
        assert last.json()["status"] == AgentStatus.SCORED

        # Public transparency record (the diagram's "which validators / all 3
        # scores + median"): all three validators, their exact composites +
        # signatures, and the median the platform finalized on.
        record = await client.get(f"/api/v1/public/agent/{agent_id}/scores")
        assert record.status_code == 200, record.text
        body = record.json()
        assert body["score_count"] == 3
        assert body["quorum"] == 3
        assert body["median_composite"] == pytest.approx(0.82)
        by_hotkey = {s["validator_hotkey"]: s["composite"] for s in body["scores"]}
        assert by_hotkey == {
            kp.ss58_address: pytest.approx(comp) for kp, comp in composites.items()
        }
        assert all(s["signature"] for s in body["scores"])
        assert all(
            datetime.fromisoformat(s["ticket_deadline"].replace("Z", "+00:00"))
            == _TICKET_DEADLINE
            for s in body["scores"]
        )

    async def test_expired_ticket_reopens_slot_for_a_new_validator(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_capable_pool(session_maker, keypairs=[*_KEYPAIRS, _DAVE])
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app, extra_keypairs=(_DAVE,))

        # Three distinct validators claim the k=3 slots via the job endpoint.
        for kp in _KEYPAIRS:
            r = await client.post(
                "/api/v1/validator/job",
                headers={"X-Validator-Hotkey": kp.ss58_address},
                json=_job_payload(kp, slot_id="slot-0"),
            )
            assert r.status_code == 200, r.text
        # A fourth, never-assigned validator is shut out (pool full, not
        # already-mine): "no job for you".
        dave_hdr = {"X-Validator-Hotkey": _DAVE.ss58_address}
        assert (
            await client.post(
                "/api/v1/validator/job",
                headers=dave_hdr,
                json=_job_payload(_DAVE, slot_id="slot-0"),
            )
        ).status_code == 204

        # One validator's ticket lapses past its deadline, re-opening its slot.
        async with session_maker() as s, s.begin():
            lapsed = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, _KEYPAIRS[0].ss58_address)
            )
            assert lapsed is not None
            lapsed.deadline = datetime.now(UTC) - timedelta(minutes=1)

        # The fourth validator now picks up the re-opened slot.
        reopened = await client.post(
            "/api/v1/validator/job",
            headers=dave_hdr,
            json=_job_payload(_DAVE, slot_id="slot-0"),
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["agent_id"] == str(agent_id)

    async def test_expired_ticket_does_not_retry_without_a_grant(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_capable_pool(session_maker)
        agent_id = await _seed_agent(session_maker, status=AgentStatus.EVALUATING)
        _install_db(app, session_maker)
        _install_chain(app)
        keypair = _KEYPAIRS[0]
        headers = {"X-Validator-Hotkey": keypair.ss58_address}

        claimed = await client.post(
            "/api/v1/validator/job",
            headers=headers,
            json=_job_payload(keypair, slot_id="slot-0"),
        )
        assert claimed.status_code == 200, claimed.text

        async with session_maker() as s, s.begin():
            lapsed = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, keypair.ss58_address)
            )
            assert lapsed is not None
            lapsed.deadline = datetime.now(UTC) - timedelta(minutes=1)

        cooling_down = await client.post(
            "/api/v1/validator/job",
            headers=headers,
            json=_job_payload(keypair, slot_id=_SLOT_ID),
        )
        assert cooling_down.status_code == 204

        async with session_maker() as s, s.begin():
            lapsed = await s.get(
                ValidatorTicket, (agent_id, _BENCH_VERSION, keypair.ss58_address)
            )
            assert lapsed is not None
            lapsed.retry_after = datetime.now(UTC) - timedelta(seconds=1)

        retried = await client.post(
            "/api/v1/validator/job",
            headers=headers,
            json=_job_payload(keypair, slot_id=_SLOT_ID),
        )
        assert retried.status_code == 204, retried.text


def test_dev_bypass_permit_refused_on_mainnet(monkeypatch) -> None:
    """The dev permit-bypass flag is honored off mainnet but refused on finney,
    so a stray env var can never open the validator surface on production."""
    from ditto.api_server.endpoints.validator import _dev_bypass_permit

    # Unset: never bypass, regardless of network.
    monkeypatch.delenv("DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR", raising=False)
    assert _dev_bypass_permit("finney") is False
    assert _dev_bypass_permit("ws://localhost:9944") is False

    # Set: honored on a dev/local network...
    monkeypatch.setenv("DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR", "true")
    assert _dev_bypass_permit("ws://localhost:9944") is True
    assert _dev_bypass_permit("test") is True
    # ...but refused on mainnet even when explicitly set.
    assert _dev_bypass_permit("finney") is False
    assert _dev_bypass_permit("Finney") is False
    assert _dev_bypass_permit("mainnet") is False


async def test_idle_qualification_refresh_is_single_flight_and_throttled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ditto.api_server.endpoints import validator

    refresh = AsyncMock(return_value=0)
    monkeypatch.setattr(validator, "refresh_rolling_qualification", refresh)
    monkeypatch.setattr(validator, "_qualification_refresh_due", 0.0)
    monkeypatch.setattr(validator.time, "monotonic", lambda: 100.0)
    session = AsyncMock()
    generator = AsyncMock()
    now = datetime.now(UTC)

    await validator._refresh_qualification_if_due(session, generator=generator, now=now)
    await validator._refresh_qualification_if_due(session, generator=generator, now=now)

    refresh.assert_awaited_once_with(session, generator=generator, now=now)


def test_infra_retry_backoff_doubles_and_caps() -> None:
    from ditto.db.queries.retry_budget import (
        INFRA_RETRY_BACKOFF_BASE,
        INFRA_RETRY_BACKOFF_CAP,
        infra_retry_backoff,
    )

    # First infra failure gets the base cooldown; each subsequent one doubles
    # until the cap, so a sustained outage backs off but never past the ceiling.
    assert infra_retry_backoff(1) == INFRA_RETRY_BACKOFF_BASE
    assert infra_retry_backoff(2) == INFRA_RETRY_BACKOFF_BASE * 2
    assert infra_retry_backoff(3) == INFRA_RETRY_BACKOFF_BASE * 4
    assert infra_retry_backoff(99) == INFRA_RETRY_BACKOFF_CAP
    # Monotonic non-decreasing and never above the cap.
    prev = timedelta(0)
    for grants in range(1, 20):
        current = infra_retry_backoff(grants)
        assert current >= prev
        assert current <= INFRA_RETRY_BACKOFF_CAP
        prev = current


def _install_chain_with_block(
    app: FastAPI,
    *,
    block_number: int,
    extra_keypairs: tuple[bittensor.Keypair, ...] = (),
) -> None:
    from ditto.chain.models import BlockInfo

    neurons = [
        NeuronInfo(
            hotkey=keypair.ss58_address,
            coldkey="5GReceiverColdkeyPlaceholderXXXXXXXXXXXXXXXXXXX",
            uid=uid,
            stake=1000.0,
            validator_permit=True,
        )
        for uid, keypair in enumerate((*_KEYPAIRS, *extra_keypairs), start=1)
    ]

    async def _chain() -> MagicMock:
        chain = MagicMock()
        chain.get_recent_neurons = AsyncMock(return_value=neurons)
        chain.get_latest_block = AsyncMock(
            return_value=BlockInfo(number=block_number, hash="00" * 32, timestamp=0)
        )
        return chain

    app.dependency_overrides[get_chain_client] = _chain


async def _seed_top5_emission_set(
    maker: async_sessionmaker[AsyncSession],
    *,
    bench_version: int = _BENCH_VERSION,
    composites: list[float] | None = None,
    composite_stderr: float = 0.03,
    seed_heartbeats: bool = True,
) -> list[UUID]:
    """Seed a scored emission set on ``bench_version`` AND make it the era.

    The activation row is not decoration. Every reader of this ledger -- the
    confirmation lane, the KOTH fold, the public projection -- keys on
    ``active_bench_version``, which with no rollout row at all still answers
    ``DEFAULT_BENCH_VERSION`` (2). While these scores lived at v2 that happened
    to line up; now that the floor forces them to v7 it does not, and without
    the activation the whole set would be an island nothing reads.

    The activation goes through ``_seed_activated_era`` rather than being
    inserted here. ``(from_version, desired_version)`` is UNIQUE, so a second
    writer of the same transition is a duplicate-key error, not a second era --
    and every test class that leases work already installs that exact row from
    an autouse fixture. Sharing the one idempotent writer lets the two compose.
    """
    composites = composites or [0.90, 0.88, 0.86, 0.84, 0.82, 0.80]
    agent_ids = [
        await _seed_agent(
            maker,
            status=AgentStatus.SCORED,
            name=f"top5-{rank}",
            miner_hotkey=f"5TopMiner{rank}",
            sha256=f"{rank:02d}" * 32,
            created_at=datetime.now(UTC) - timedelta(days=10 - rank),
        )
        for rank in range(len(composites))
    ]
    activated_at = datetime.now(UTC) - timedelta(hours=2)
    await _seed_activated_era(maker, version=bench_version, activated_at=activated_at)
    async with maker() as session, session.begin():
        for agent_id, composite in zip(agent_ids, composites, strict=True):
            for index, keypair in enumerate(_KEYPAIRS):
                session.add(
                    Score(
                        agent_id=agent_id,
                        bench_version=bench_version,
                        validator_hotkey=keypair.ss58_address,
                        run_id=f"top5-{agent_id}-{index}",
                        signature=None,
                        seed=index,
                        composite=composite,
                        tool_mean=composite,
                        memory_mean=composite,
                        median_ms=100,
                        n=114,
                        details={
                            "bench_version": bench_version,
                            "composite_stderr": composite_stderr,
                        },
                        generated_at=datetime.now(UTC),
                    )
                )
    if seed_heartbeats:
        # Capability-bearing, not bare. Above v6 the confirmation lane refuses
        # (428) any validator whose heartbeat cannot show ticket inference and
        # a v7 calibration, so a heartbeat with no capabilities at all -- which
        # is all these fixtures used to need -- now reads as incapable.
        capabilities = _scorer_capable_capabilities(
            now=datetime.now(UTC), versions=(bench_version,)
        )
        for keypair in _KEYPAIRS:
            await _seed_validator_heartbeat(
                maker,
                keypair=keypair,
                protocol_version=13,
                capabilities=capabilities,
                stack=_V7_STACK,
            )
    return agent_ids


async def _seed_ranked_pool(
    maker: async_sessionmaker[AsyncSession], *, size: int
) -> list[UUID]:
    """Seed ``size`` ranked agents so the cohort can extend past the top five."""
    return await _seed_top5_emission_set(
        maker,
        composites=[round(0.90 - rank / 100, 2) for rank in range(size)],
    )


async def _seed_confirmation_wave(
    maker: async_sessionmaker[AsyncSession],
    agent_ids: Sequence[UUID],
    *,
    seed: int,
    bench_version: int = _BENCH_VERSION,
    composite: float = 0.90,
) -> None:
    """Record one already-scored wave seed for ``agent_ids``.

    ``composite`` defaults to the champion's own score on purpose: a completed
    wave feeds ``effective_composite``, so a low value here would re-rank the
    pool and quietly change which agents the test is even talking about.
    """
    async with maker() as session, session.begin():
        for index, agent_id in enumerate(agent_ids):
            session.add(
                ConfirmationScore(
                    agent_id=agent_id,
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    bench_version=bench_version,
                    seed=seed,
                    composite=composite,
                    run_id=f"wave-{seed}-{index}",
                    signature=None,
                )
            )


async def _set_retest_cohort_size(
    maker: async_sessionmaker[AsyncSession], size: int, **band: object
) -> None:
    settings: dict[str, object] = {
        "aggregate_mode": "fleet_ready",
        "idle_retests_enabled": False,
        "retest_cohort_size": size,
    }
    settings.update(band)
    async with maker() as session, session.begin():
        session.add(
            ContinualRetestSettingsRevision(
                parent_revision=0,
                scope="*",
                settings=settings,
                checksum="b" * 64,
                reason="retest deeper than the emission set",
                actor="operator@example.com",
            )
        )


def _top5_job_payload(
    champion: UUID,
    member: UUID,
    *,
    keypair: bittensor.Keypair = _KEYPAIR,
) -> dict[str, str]:
    nonce = uuid4()
    requested_at = datetime.now(UTC)
    requested = requested_at.isoformat(timespec="microseconds")
    validator_hotkey = keypair.ss58_address
    message = (
        "validator-top5-confirmation-job:v1:"
        f"{validator_hotkey}:{champion}:{member}:{nonce}:{requested}"
    ).encode()
    return {
        "validator_hotkey": validator_hotkey,
        "champion_agent_id": str(champion),
        "member_agent_id": str(member),
        "nonce": str(nonce),
        "requested_at": requested_at.isoformat(),
        "signature": keypair.sign(message).hex(),
    }


def _auto_top5_job_payload(
    slot_id: str,
    *,
    keypair: bittensor.Keypair = _KEYPAIR,
) -> dict[str, str]:
    nonce = uuid4()
    requested_at = datetime.now(UTC)
    requested = requested_at.isoformat(timespec="microseconds")
    validator_hotkey = keypair.ss58_address
    message = (
        "validator-top5-confirmation-job:v2:"
        f"{validator_hotkey}:{slot_id}:{nonce}:{requested}"
    ).encode()
    return {
        "validator_hotkey": validator_hotkey,
        "slot_id": slot_id,
        "nonce": str(nonce),
        "requested_at": requested_at.isoformat(),
        "signature": keypair.sign(message).hex(),
    }


def _top5_auth_header(keypair: bittensor.Keypair) -> dict[str, str]:
    return {"X-Validator-Hotkey": keypair.ss58_address}


def _top5_score_payload(
    agent_id: UUID,
    *,
    deadline: datetime,
    seeds: list[int],
    composites: list[float],
) -> dict[str, object]:
    report: dict[str, object] = {
        "run_id": "top5-confirmation-run",
        "bench_version": _BENCH_VERSION,
        "seed": seeds[0],
        "composite": statistics.median(composites),
        "tool_mean": statistics.median(composites),
        "memory_mean": statistics.median(composites),
        "median_ms": 100,
        "n": 114,
        "confirmation_seeds": seeds,
        "confirmation_composites": composites,
        "generated_at": datetime.now(UTC).isoformat(),
        "per_case": [],
    }
    lease = deadline.astimezone(UTC).isoformat(timespec="microseconds")
    pairs = json.dumps(list(zip(seeds, composites, strict=True)), separators=(",", ":"))
    message = (
        "validator-top5-confirmation-score:v1:"
        f"{_VALIDATOR_HOTKEY}:{agent_id}:{lease}:top5-confirmation-run:"
        f"{_BENCH_VERSION}:{pairs}"
    ).encode()
    return {
        "validator_hotkey": _VALIDATOR_HOTKEY,
        "ticket_deadline": deadline.isoformat(),
        "signature": _KEYPAIR.sign(message).hex(),
        "report": report,
    }


class TestTop5ConfirmationLane:
    @pytest.fixture(autouse=True)
    async def _v7_lane(
        self, app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Everything the lane needs once the era it retests is v7.

        The emission sets below used to sit on v2, where a confirmation claim
        needed no dataset generator and no inference grant at all. Neither is
        optional now: ``top5-confirmation-job`` pins a dataset per seed and
        refuses (503) any lease it cannot attach a grant to. Wiring both here
        keeps every test in the class about what it was about -- claims,
        cohorts, bands, waves -- instead of about v7 plumbing.
        """
        _install_dataset_generator(app)
        await _install_ticket_inference(app, session_maker)

    async def test_platform_routed_claim_recovers_from_stale_local_member(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        members = await _seed_top5_emission_set(session_maker)
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        stale = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(members[0], uuid4()),
        )
        routed = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_auto_top5_job_payload("slot-0"),
        )

        assert stale.status_code == 409, stale.text
        assert "not in the current retest cohort" in stale.text
        assert routed.status_code == 200, routed.text
        assert UUID(routed.json()["agent_id"]) in set(members)
        assert routed.json()["slot_id"] == "slot-0"

    async def test_paused_validator_gets_no_new_continual_retest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, *_ = await _seed_top5_emission_set(session_maker)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorSlotSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "max_concurrent_slots": 2,
                        "disk_percent_ceiling": 90,
                        "memory_percent_ceiling": 90,
                        "cpu_percent_ceiling": 0,
                        "resource_block_percent_ceiling": 95,
                        "paused_validator_hotkeys": [_VALIDATOR_HOTKEY],
                    },
                    checksum="9" * 64,
                    reason="drain continual retests for this validator",
                    actor="backroom:test",
                )
            )
        app.state.session_maker = session_maker
        app.state.validator_slot_settings.invalidate()
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, champion),
        )

        assert response.status_code == 204, response.text
        async with session_maker() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ValidatorTicket)
                    .where(
                        ValidatorTicket.validator_hotkey == _VALIDATOR_HOTKEY,
                        ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
                    )
                )
                == 0
            )

    async def test_paused_validator_preserves_live_continual_retest(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, *_ = await _seed_top5_emission_set(session_maker)
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)
        first = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, champion),
        )
        assert first.status_code == 200, first.text
        async with session_maker() as session:
            ticket_before = await session.get(
                ValidatorTicket,
                (champion, _BENCH_VERSION, _VALIDATOR_HOTKEY),
            )
            grant_before = await session.scalar(
                select(InferenceGrant).where(
                    InferenceGrant.agent_id == champion,
                    InferenceGrant.validator_hotkey == _VALIDATOR_HOTKEY,
                    InferenceGrant.bench_version == _BENCH_VERSION,
                )
            )
            assert ticket_before is not None
            assert grant_before is not None
            original_deadline = ticket_before.deadline
            original_slot = ticket_before.slot_id
            original_purpose_revision = ticket_before.purpose_revision
            original_grant_id = grant_before.grant_id
            original_grant_generation = grant_before.generation
            original_grant_status = grant_before.status
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorSlotSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "max_concurrent_slots": 2,
                        "disk_percent_ceiling": 90,
                        "memory_percent_ceiling": 90,
                        "cpu_percent_ceiling": 0,
                        "resource_block_percent_ceiling": 95,
                        "paused_validator_hotkeys": [_VALIDATOR_HOTKEY],
                    },
                    checksum="8" * 64,
                    reason="drain only after the continual lease",
                    actor="backroom:test",
                )
            )
        app.state.session_maker = session_maker
        app.state.validator_slot_settings.invalidate()

        duplicate = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, champion),
        )

        # Pause is issuance-only: the process that already holds this lease may
        # keep reporting and submit it. A second local task cannot "resume" a
        # stateless 351-case run, however. Handing the job out again would bind
        # two scorers to one ticket and rotate the first scorer's inference
        # grant, which is the continual-retest reset loop this guard prevents.
        assert duplicate.status_code == 409, duplicate.text
        assert "another live assignment" in duplicate.json()["message"]
        async with session_maker() as session:
            ticket_after = await session.get(
                ValidatorTicket,
                (champion, _BENCH_VERSION, _VALIDATOR_HOTKEY),
            )
            grant_after = await session.scalar(
                select(InferenceGrant).where(
                    InferenceGrant.agent_id == champion,
                    InferenceGrant.validator_hotkey == _VALIDATOR_HOTKEY,
                    InferenceGrant.bench_version == _BENCH_VERSION,
                )
            )
            assert ticket_after is not None
            assert grant_after is not None
            assert ticket_after.deadline == original_deadline
            assert ticket_after.slot_id == original_slot
            assert ticket_after.purpose_revision == original_purpose_revision
            assert grant_after.grant_id == original_grant_id
            assert grant_after.generation == original_grant_generation
            assert grant_after.status == original_grant_status

    async def test_emission_set_uses_completed_wave_mean_for_champion(
        self,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Retest claims and the validator ledger must agree on the incumbent.

        A completed continual wave can preserve an older incumbent even when a
        newer agent leads the raw three-score median.  Omitting the quorum and
        completed-wave samples here made every healthy validator claim the raw
        leader and receive a 409 from the authoritative job endpoint.
        """
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_top5_emission_set(
            session_maker,
            composites=[0.90, 0.92, 0.86, 0.84, 0.82, 0.80],
        )
        # The fold is scoped to the reigning champion's CRN anchor, so the wave
        # has to be recorded on seeds that champion's reign would really issue.
        # The incumbent keeps the crown here: 0.92 leads the raw median but at
        # ``composite_stderr=0.03`` it is well inside the dethrone band.
        seed = champion_anchored_seeds(
            agent_ids[0], version=_BENCH_VERSION, max_seeds=16
        )[0]
        async with session_maker() as session, session.begin():
            for index, agent_id in enumerate(agent_ids[:5]):
                session.add(
                    ConfirmationScore(
                        agent_id=agent_id,
                        validator_hotkey=_VALIDATOR_HOTKEY,
                        bench_version=_BENCH_VERSION,
                        seed=seed,
                        composite=1.0 if index == 0 else 0.0,
                        run_id=f"completed-wave-{index}",
                        signature=None,
                    )
                )

        from ditto.api_server.endpoints.validator import _current_emission_set

        async with session_maker() as session:
            members = await _current_emission_set(
                session, canonical_version=_BENCH_VERSION
            )

        assert members[0].agent_id == agent_ids[0]
        assert members[0].quorum_composites == (0.90, 0.90, 0.90)
        assert members[0].completed_wave_composites == (1.0,)

    async def test_requires_single_seed_capable_validator_protocol(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, member, *_ = await _seed_top5_emission_set(
            session_maker,
            seed_heartbeats=False,
        )
        await _seed_validator_heartbeat(session_maker, protocol_version=12)
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 428
        assert "protocol 13" in response.json()["message"]

    async def test_requires_fresh_validator_heartbeat(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, member, *_ = await _seed_top5_emission_set(
            session_maker,
            seed_heartbeats=False,
        )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 428
        assert "fresh heartbeat" in response.json()["message"]

    async def test_v8_only_validator_claims_retest_without_v7_calibration(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, *_ = await _seed_top5_emission_set(
            session_maker,
            bench_version=8,
            seed_heartbeats=False,
        )
        capabilities = _scorer_capable_capabilities(
            now=datetime.now(UTC), versions=(8,)
        )
        scorer = capabilities["scorer_benchmarks"]
        assert isinstance(scorer, dict)
        scorer.pop("v7_calibration")
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=18,
            capabilities=capabilities,
            stack=_V7_STACK,
        )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, champion),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(champion)
        assert response.json()["bench_version"] == 8

    async def test_distributes_concurrent_claims_across_least_covered_members(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, second, third = agent_ids[:3]
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        first = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_top5_job_payload(champion, champion, keypair=_KEYPAIRS[0]),
        )
        assert first.status_code == 200, first.text

        repeated_champion = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_top5_job_payload(champion, champion, keypair=_KEYPAIRS[1]),
        )
        assert repeated_champion.status_code == 409
        assert "less confirmation coverage" in repeated_champion.json()["message"]

        second_claim = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_top5_job_payload(champion, second, keypair=_KEYPAIRS[1]),
        )
        assert second_claim.status_code == 200, second_claim.text

        repeated_second = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[2]),
            json=_top5_job_payload(champion, second, keypair=_KEYPAIRS[2]),
        )
        assert repeated_second.status_code == 409
        assert "less confirmation coverage" in repeated_second.json()["message"]

        third_claim = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[2]),
            json=_top5_job_payload(champion, third, keypair=_KEYPAIRS[2]),
        )
        assert third_claim.status_code == 200, third_claim.text

    async def test_stale_champion_hint_uses_authoritative_current_incumbent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A ledger race must not stop otherwise-valid continual work.

        The signed champion is the validator's observation. Platform owns the
        current fold, so it validates the requested member against that cohort
        and anchors the issued seed to the current incumbent instead.
        """
        from ditto.api_server.crn import champion_anchored_seeds

        champion, stale_champion, *_ = await _seed_top5_emission_set(session_maker)
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(stale_champion, champion),
        )

        assert response.status_code == 200, response.text
        pins = response.json()["confirmation_datasets"]
        assert [pin["seed"] for pin in pins] == [
            champion_anchored_seeds(champion, version=_BENCH_VERSION, max_seeds=16)[0]
        ]

    async def test_protocol_13_validator_without_canonical_score_can_claim(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Fleet additions may append evidence without changing canonical k=3."""
        champion, *_ = await _seed_top5_emission_set(session_maker)
        await _seed_validator_heartbeat(
            session_maker,
            keypair=_DAVE,
            protocol_version=13,
            capabilities=_scorer_capable_capabilities(
                now=datetime.now(UTC), versions=(_BENCH_VERSION,)
            ),
            stack=_V7_STACK,
        )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1, extra_keypairs=(_DAVE,))

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_DAVE),
            json=_top5_job_payload(champion, champion, keypair=_DAVE),
        )

        assert response.status_code == 200, response.text
        async with session_maker() as session:
            canonical = await session.get(
                Score, (champion, _BENCH_VERSION, _DAVE.ss58_address)
            )
            ticket = await session.get(
                ValidatorTicket,
                (champion, _BENCH_VERSION, _DAVE.ss58_address),
            )
        assert canonical is None
        assert ticket is not None
        assert ticket.purpose == TicketPurpose.CONTINUAL_RETEST

    async def test_confirmation_retry_cooldown_defers_then_releases_member(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, *_ = await _seed_top5_emission_set(session_maker)
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorTicket(
                    agent_id=champion,
                    bench_version=_BENCH_VERSION,
                    validator_hotkey=_VALIDATOR_HOTKEY,
                    status=TicketStatus.EXPIRED,
                    purpose=TicketPurpose.CONTINUAL_RETEST,
                    purpose_revision=1,
                    issued_at=now - timedelta(hours=1),
                    deadline=now - timedelta(minutes=30),
                    retry_after=now + timedelta(minutes=30),
                )
            )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        deferred = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, champion),
        )
        assert deferred.status_code == 409

        async with session_maker() as session, session.begin():
            ticket = await session.get(
                ValidatorTicket,
                (champion, _BENCH_VERSION, _VALIDATOR_HOTKEY),
            )
            assert ticket is not None
            ticket.retry_after = datetime.now(UTC) - timedelta(seconds=1)

        released = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, champion),
        )
        assert released.status_code == 200, released.text

    async def test_claim_uses_version_aware_koth_band_decay(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        # The band decay is keyed on ``KOTH_BAND_DECAY_MIN_BENCH_VERSION`` (6),
        # so it is live for every era the ledger can still be written in: the
        # 0.006 lead clears the decayed band around the older 0.900 incumbent
        # but not the legacy flat 0.007 band. The fixture moved from v6 to the
        # current era when the floor retired v6; the branch under test is the
        # same one, because ">= 6" covers both.  The validator ledger fold and
        # public projection both carry bench_version; the claim verifier must
        # do the same or it rejects the real champion.
        agent_ids = await _seed_top5_emission_set(
            session_maker,
            composites=[0.900, 0.906, 0.86, 0.84, 0.82, 0.80],
            composite_stderr=0.0,
        )
        champion = agent_ids[1]
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, champion),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(champion)

    async def test_completed_wave_champion_can_claim_the_next_wave(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The claim fold must match the completed-wave scoring ledger fold.

        A complete shared-seed wave can legitimately dethrone the canonical-score
        incumbent.  Rechecking the claim against a fold with confirmation history
        disabled would reject every subsequent claim with a stale champion and
        permanently stop the retest lane.
        """
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_top5_emission_set(
            session_maker,
            composites=[0.90, 0.906, 0.86, 0.84, 0.82, 0.80],
            composite_stderr=0.0,
        )
        old_champion, new_champion = agent_ids[:2]
        cohort = agent_ids[:5]
        completed_seeds = champion_anchored_seeds(
            old_champion,
            version=_BENCH_VERSION,
            max_seeds=16,
        )[:2]
        async with session_maker() as session, session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    ConfirmationSeedScore(
                        agent_id,
                        _VALIDATOR_HOTKEY,
                        seed,
                        0.95 if agent_id == new_champion else 0.80,
                        f"completed-wave-{agent_id}-{seed}",
                        None,
                    )
                    for agent_id in cohort
                    for seed in completed_seeds
                ],
                bench_version=_BENCH_VERSION,
                created_at=datetime.now(UTC),
            )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=1)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(new_champion, new_champion),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(new_champion)

    async def test_rejects_out_of_cadence_claim_without_canonical_tail(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        async with session_maker() as session, session.begin():
            champion_row = await session.get(Agent, champion)
            assert champion_row is not None
            champion_row.dataset_seed_block = 1
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=361)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 409
        assert "not due" in response.json()["message"]

    async def test_idle_retest_switch_uses_spare_validator_capacity(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        async with session_maker() as session, session.begin():
            champion_row = await session.get(Agent, champion)
            assert champion_row is not None
            champion_row.dataset_seed_block = 1
            session.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "aggregate_mode": "fleet_ready",
                        "idle_retests_enabled": True,
                    },
                    checksum="a" * 64,
                    reason="use spare capacity for bounded continual retests",
                    actor="operator@example.com",
                )
            )
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=361)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(member)

    async def _arm_idle_retest_lane(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
        champion: UUID,
        *,
        capacity: dict[str, object] | None,
    ) -> None:
        """Open every retest gate and publish ``capacity`` for the validator."""
        async with session_maker() as session, session.begin():
            champion_row = await session.get(Agent, champion)
            assert champion_row is not None
            champion_row.dataset_seed_block = 1
            session.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings={
                        "aggregate_mode": "fleet_ready",
                        "idle_retests_enabled": True,
                    },
                    checksum="b" * 64,
                    reason="idle retests claim spare slots",
                    actor="operator@example.com",
                )
            )
            heartbeat = await session.get(ValidatorHeartbeat, _VALIDATOR_HOTKEY)
            assert heartbeat is not None
            heartbeat.benchmark_capacity = capacity
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=361)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

    async def test_retest_claims_an_idle_slot_beside_a_canonical_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A busy sibling slot must not veto the whole validator.

        The lane refused a confirmation whenever the validator held any live
        lease anywhere, which is the pre-#433 one-benchmark-per-validator
        assumption. With multi-slot validators that made continual retests --
        enabled precisely to consume *idle* capacity -- the one kind of work
        that could never claim it.
        """
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        busy_agent = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="canonical-occupant",
            miner_hotkey="5CanonicalOccupant",
            sha256="ab" * 32,
        )
        await _seed_ticket(session_maker, busy_agent, slot_id="slot-0")
        await self._arm_idle_retest_lane(
            app,
            session_maker,
            champion,
            capacity={
                "configured_slots": 4,
                "healthy_slots": ["slot-0", "slot-1", "slot-2", "slot-3"],
                "admission": "accepting",
                "active": [],
            },
        )

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(member)
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (member, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.purpose == TicketPurpose.CONTINUAL_RETEST
            # The lowest free slot, NOT the ``slot-0`` column default: the
            # validator binds its execution slot to the ticket, so a wrong id
            # sends every progress report to a slot with no matching lease.
            assert ticket.slot_id == "slot-1"

    async def test_duplicate_live_retest_cannot_rotate_its_grant(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A second task cannot replace an already-running retest lease."""
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        await self._arm_idle_retest_lane(
            app,
            session_maker,
            champion,
            capacity={
                "configured_slots": 2,
                "healthy_slots": ["slot-0", "slot-1"],
                "admission": "accepting",
                "active": [],
            },
        )

        first = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )
        assert first.status_code == 200, first.text
        async with session_maker.begin() as session:
            ticket = await session.get(
                ValidatorTicket, (member, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            ticket.first_reported_at = datetime.now(UTC)
        second = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert second.status_code == 409, second.text
        assert first.json()["slot_id"] == "slot-0"
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (member, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            assert ticket is not None
            assert ticket.attempt_count == 1
            assert ticket.infra_retry_grants == 0
            assert ticket.slot_id == "slot-0"
            assert ticket.deadline == datetime.fromisoformat(
                first.json()["deadline"].replace("Z", "+00:00")
            )
            grants = list(
                (
                    await session.scalars(
                        select(InferenceGrant).where(
                            InferenceGrant.agent_id == member,
                            InferenceGrant.validator_hotkey == _VALIDATOR_HOTKEY,
                        )
                    )
                ).all()
            )
        assert len(grants) == 1
        assert grants[0].status == "pending"

    async def test_one_validator_fills_two_slots_with_distinct_members(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A lease for member A must not veto member B on a free slot."""
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, first_member, second_member = agent_ids[:3]
        await self._arm_idle_retest_lane(
            app,
            session_maker,
            champion,
            capacity={
                "configured_slots": 4,
                "healthy_slots": ["slot-0", "slot-1", "slot-2", "slot-3"],
                "admission": "accepting",
                "active": [],
            },
        )

        first = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, first_member),
        )
        second = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, second_member),
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert {first.json()["slot_id"], second.json()["slot_id"]} == {
            "slot-0",
            "slot-1",
        }

    async def test_operator_slot_cap_bounds_the_retest_lane(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Retests are leases, so the operator cap has to count them too.

        The validator advertises four healthy slots but the default policy
        allows two concurrent leases, and both are already spent on canonical
        work. Ramping the fleet stays an operator decision.
        """
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        for index, slot in enumerate(("slot-0", "slot-1")):
            busy_agent = await _seed_agent(
                session_maker,
                status=AgentStatus.EVALUATING,
                name=f"canonical-occupant-{index}",
                miner_hotkey=f"5CanonicalOccupant{index}",
                sha256=f"{index + 12:02d}" * 32,
            )
            await _seed_ticket(session_maker, busy_agent, slot_id=slot)
        await self._arm_idle_retest_lane(
            app,
            session_maker,
            champion,
            capacity={
                "configured_slots": 4,
                "healthy_slots": ["slot-0", "slot-1", "slot-2", "slot-3"],
                "admission": "accepting",
                "active": [],
            },
        )

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 409
        assert "no idle slot" in response.json()["message"]

    async def test_single_slot_validator_still_serializes_against_its_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """No advertised capacity falls back to the one-slot machine.

        Absence of a capacity blob is not evidence of free capacity, and it is
        not evidence of a busy validator either: the lane keeps its historical
        single-``slot-0`` behaviour, which here means the canonical lease on
        that slot still blocks the retest.
        """
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        busy_agent = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="canonical-occupant",
            miner_hotkey="5CanonicalOccupant",
            sha256="cd" * 32,
        )
        await _seed_ticket(session_maker, busy_agent, slot_id="slot-0")
        await self._arm_idle_retest_lane(app, session_maker, champion, capacity=None)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 409
        assert "no idle slot" in response.json()["message"]

    async def test_allows_out_of_cadence_claim_while_canonical_tail_drains(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        draining_agent = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="canonical-tail",
            miner_hotkey="5CanonicalTailMiner",
        )
        async with session_maker() as session, session.begin():
            champion_row = await session.get(Agent, champion)
            assert champion_row is not None
            champion_row.dataset_seed_block = 1
        await _seed_ticket(
            session_maker,
            draining_agent,
            keypair=_KEYPAIRS[1],
            deadline=datetime.now(UTC) + timedelta(minutes=30),
        )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=361)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["agent_id"] == str(member)

    async def test_allows_out_of_cadence_claim_just_after_canonical_tail_finishes(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        finished_agent = await _seed_agent(
            session_maker,
            status=AgentStatus.SCORED,
            name="canonical-tail-finished",
            miner_hotkey="5CanonicalTailFinishedMiner",
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            champion_row = await session.get(Agent, champion)
            assert champion_row is not None
            champion_row.dataset_seed_block = 1
            session.add(
                ValidatorTicket(
                    agent_id=finished_agent,
                    bench_version=_BENCH_VERSION,
                    validator_hotkey=_KEYPAIRS[1].ss58_address,
                    status=TicketStatus.SCORED,
                    purpose=TicketPurpose.CANONICAL_QUORUM,
                    purpose_revision=1,
                    issued_at=now - timedelta(minutes=30),
                    deadline=now + timedelta(minutes=60),
                    updated_at=now - timedelta(minutes=1),
                )
            )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=361)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(member)

    @pytest.mark.parametrize(
        "purpose",
        [TicketPurpose.CANONICAL_QUORUM, TicketPurpose.LEGACY_UNCLASSIFIED],
    )
    async def test_rejects_nonconfirmation_ticket_purpose(
        self,
        purpose: TicketPurpose,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        deadline = datetime.now(UTC) + timedelta(minutes=30)
        await _seed_ticket(
            session_maker,
            member,
            deadline=deadline,
            purpose=purpose,
        )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=0)
        seeds = list(
            champion_anchored_seeds(champion, version=_BENCH_VERSION, max_seeds=16)[:2]
        )

        response = await client.post(
            f"/api/v1/validator/agent/{member}/top5-confirmation-score",
            json=_top5_score_payload(
                member,
                deadline=deadline,
                seeds=seeds,
                composites=[0.81, 0.83],
            ),
        )

        assert response.status_code == 409
        assert "not authorized for continual retesting" in response.text

    async def test_accepts_grandfathered_inflight_confirmation_lease(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_top5_emission_set(session_maker)
        # This lease was valid when issued, but the member has since fallen out
        # of the current five. Deployment must not strand already-running old
        # multi-seed work; its live signed ticket remains the authorization.
        champion, member = agent_ids[0], agent_ids[5]
        deadline = datetime.now(UTC) + timedelta(minutes=30)
        await _seed_ticket(
            session_maker,
            member,
            deadline=deadline,
            purpose=TicketPurpose.LEGACY_UNCLASSIFIED,
            purpose_revision=0,
            legacy_completion_allowed=True,
        )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=0)
        seeds = list(
            champion_anchored_seeds(champion, version=_BENCH_VERSION, max_seeds=16)[:2]
        )

        response = await client.post(
            f"/api/v1/validator/agent/{member}/top5-confirmation-score",
            json=_top5_score_payload(
                member,
                deadline=deadline,
                seeds=seeds,
                composites=[0.81, 0.83],
            ),
        )

        assert response.status_code == 200, response.text
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (member, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
        assert ticket is not None
        assert ticket.purpose == TicketPurpose.CONTINUAL_RETEST
        assert ticket.purpose_revision == 1
        assert ticket.legacy_completion_allowed is False

    async def test_v7_job_includes_ticket_scoped_inference_offer(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent_ids = await _seed_top5_emission_set(
            session_maker,
            seed_heartbeats=False,
        )
        champion, member = agent_ids[0], agent_ids[1]
        now = datetime.now(UTC)
        profile = "openrouter-route-a471cd87ae7df5b9-v1"
        capabilities = {
            **_V7_CAPABILITIES,
            "ticket_inference": True,
            "scorer_benchmarks": {
                "status": "fresh_verified",
                "supported_bench_versions": [2, 7],
                "observed_at": int(now.timestamp()),
                "software_version": "1.3.0",
                "source_revision": "2" * 40,
                "v7_calibration": {
                    "manifest_sha256": "c" * 64,
                    "supported_routes": (
                        {
                            "provider": "openrouter",
                            "profile_revision": profile,
                            "model": "openai/gpt-oss-20b",
                        },
                    ),
                },
            },
        }
        await _seed_validator_heartbeat(
            session_maker,
            protocol_version=13,
            capabilities=capabilities,
            stack=_V7_STACK,
        )
        # The activation is recorded by ``_seed_top5_emission_set`` now, and
        # ``benchmark_rollouts_transition_idx`` refuses a second 6 -> 7 row.
        async with session_maker() as session, session.begin():
            for agent_id in agent_ids:
                agent = await session.get(Agent, agent_id)
                assert agent is not None
                agent.screening_policy_version = 9
                agent.screened_image_sha256 = "12" * 32
                agent.screened_image_size_bytes = 123
                agent.screened_image_id = "sha256:" + "34" * 32
                agent.screened_image_ref = f"ditto-screen/{agent_id}:latest"
                agent.screened_image_upload_id = uuid4()
                agent.screened_image_verified_at = now

        grant_id = uuid4()
        grant = MagicMock(
            grant_id=grant_id,
            allowed_models=["openai/gpt-oss-20b"],
            request_budget=1203,
            token_budget=3_000_000,
            expires_at=now + timedelta(minutes=90),
            route_provider="openrouter",
            route_profile=profile,
        )
        ensure = AsyncMock(return_value=grant)
        monkeypatch.setattr(
            "ditto.api_server.endpoints.validator.ensure_inference_grant", ensure
        )
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=0)
        generator = MagicMock(run_size="full")
        generator.generate = AsyncMock(
            side_effect=lambda seed, *, bench_version: hashlib.sha256(
                f"{bench_version}:{seed}".encode()
            ).hexdigest()
        )
        app.dependency_overrides[get_dataset_generator] = lambda: generator
        app.state.config = replace(
            app.state.config,
            top5_backoff_base=2,
            inference_proxy=replace(
                app.state.config.inference_proxy,
                enabled=True,
                openrouter_api_key="test-only",
            ),
        )

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["bench_version"] == 7
        assert body["slot_id"] == "slot-0"
        assert body["minimum_screening_policy_version"] == 9
        assert body["requires_screened_image"] is True
        from ditto.api_server.crn import champion_anchored_seeds

        expected_seeds = list(
            champion_anchored_seeds(champion, version=7, max_seeds=16)[:1]
        )
        assert [pin["seed"] for pin in body["confirmation_datasets"]] == expected_seeds
        assert all(pin["run_size"] == "full" for pin in body["confirmation_datasets"])
        assert generator.generate.await_count == len(expected_seeds)
        assert body["inference"]["grant_id"] == str(grant_id)
        assert body["inference"]["profile_revision"] == profile
        ensure.assert_awaited_once()

    async def test_appends_evidence_without_replacing_canonical_score(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.api_server.crn import champion_anchored_seeds
        from ditto.db.models import ConfirmationScore

        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, member = agent_ids[0], agent_ids[1]
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=0)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

        job = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )
        assert job.status_code == 200, job.text
        deadline = datetime.fromisoformat(job.json()["deadline"])
        # One lease, one seed. From v3 onward the lane pins a dataset onto the
        # ticket and refuses any report that does not name exactly that seed;
        # a v2 retest lease carried no seed at all, which is why this used to
        # be free to submit the first two of the champion's anchored plan.
        seeds = [pin["seed"] for pin in job.json()["confirmation_datasets"]]
        assert seeds == list(
            champion_anchored_seeds(champion, version=_BENCH_VERSION, max_seeds=16)[:1]
        )
        submitted = await client.post(
            f"/api/v1/validator/agent/{member}/top5-confirmation-score",
            json=_top5_score_payload(
                member,
                deadline=deadline,
                seeds=seeds,
                composites=[0.81],
            ),
        )
        assert submitted.status_code == 200, submitted.text

        async with session_maker() as session:
            canonical = await session.get(
                Score, (member, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
            confirmations = await session.scalar(
                select(func.count()).where(ConfirmationScore.agent_id == member)
            )
            ticket = await session.get(
                ValidatorTicket, (member, _BENCH_VERSION, _VALIDATOR_HOTKEY)
            )
        assert canonical is not None
        assert canonical.run_id.startswith("top5-")
        assert confirmations == 1
        assert ticket is not None and ticket.status == TicketStatus.SCORED
        assert ticket.purpose == TicketPurpose.CONTINUAL_RETEST

    async def test_rejects_member_outside_the_retest_cohort(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        agent_ids = await _seed_top5_emission_set(session_maker)
        champion, sixth = agent_ids[0], agent_ids[5]
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=0)
        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, sixth),
        )
        assert response.status_code == 409
        # Default cohort is the emission set, so rank six is still refused.
        assert "retest cohort (top 5)" in response.json()["message"]

    async def test_widened_cohort_admits_ranked_agents_below_the_emission_set(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Top-10 is one operator revision, not a redeploy."""
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_ranked_pool(session_maker, size=10)
        champion, sixth = agent_ids[0], agent_ids[5]
        wave_seed = champion_anchored_seeds(
            champion, version=_BENCH_VERSION, max_seeds=16
        )[0]
        # Every emission-set member but the champion already holds this seed;
        # the champion is leased below. Nothing in the top five is left waiting,
        # so the extended cohort is what the spare slot is for.
        await _seed_confirmation_wave(session_maker, agent_ids[1:5], seed=wave_seed)
        await _set_retest_cohort_size(session_maker, 10)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=1)

        champion_claim = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_top5_job_payload(champion, champion, keypair=_KEYPAIRS[0]),
        )
        assert champion_claim.status_code == 200, champion_claim.text

        extended = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_top5_job_payload(champion, sixth, keypair=_KEYPAIRS[1]),
        )

        assert extended.status_code == 200, extended.text
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (sixth, _BENCH_VERSION, _KEYPAIRS[1].ss58_address)
            )
        assert ticket is not None
        assert ticket.purpose == TicketPurpose.CONTINUAL_RETEST

    async def test_a_tie_at_the_cutoff_is_refused_under_a_fixed_rank(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The defect, stated as a test: identical scores, opposite outcomes.

        Rank five and rank six hold the same composite. The fixed rank keeps one
        and refuses the other purely on the ``first_seen`` tiebreak.
        """
        agent_ids = await _seed_top5_emission_set(
            session_maker,
            composites=[0.90, 0.89, 0.88, 0.87, 0.86, 0.86, 0.80],
        )
        champion, sixth = agent_ids[0], agent_ids[5]
        _install_db(app, session_maker)
        _install_chain_with_block(app, block_number=0)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, sixth),
        )

        assert response.status_code == 409
        assert "retest cohort (top 5)" in response.json()["message"]

    async def test_the_statistical_band_admits_the_tied_agent(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Same pool, one operator revision, and the tie is no longer split."""
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_top5_emission_set(
            session_maker,
            composites=[0.90, 0.89, 0.88, 0.87, 0.86, 0.86, 0.80],
        )
        champion, sixth = agent_ids[0], agent_ids[5]
        wave_seed = champion_anchored_seeds(
            champion, version=_BENCH_VERSION, max_seeds=16
        )[0]
        # Clear the emission set off the open seed so the spare slot is genuinely
        # available to the extended member rather than contended.
        await _seed_confirmation_wave(session_maker, agent_ids[1:5], seed=wave_seed)
        await _set_retest_cohort_size(
            session_maker,
            5,
            retest_eligibility_mode="statistical",
            retest_eligibility_z=1.64,
            retest_cohort_max_size=25,
        )
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=1)

        champion_claim = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_top5_job_payload(champion, champion, keypair=_KEYPAIRS[0]),
        )
        assert champion_claim.status_code == 200, champion_claim.text

        tied = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_top5_job_payload(champion, sixth, keypair=_KEYPAIRS[1]),
        )

        assert tied.status_code == 200, tied.text
        async with session_maker() as session:
            ticket = await session.get(
                ValidatorTicket, (sixth, _BENCH_VERSION, _KEYPAIRS[1].ss58_address)
            )
        assert ticket is not None
        assert ticket.purpose == TicketPurpose.CONTINUAL_RETEST

    async def test_the_band_does_not_widen_the_emission_set(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Extra evidence, never an extra emission recipient.

        The band is allowed to widen who gets *retested*. It must not widen the
        five agents the weight fold pays, which is frozen consensus shared with
        the subnet.
        """
        # A tight stderr keeps the band narrow enough that ONLY the exact tie is
        # absorbed: 1.64 * sqrt(0.001^2 + 0.001^2) is about 0.0023, so rank seven
        # at 0.80 is six hundredths clear of the cutoff and stays out.
        agent_ids = await _seed_top5_emission_set(
            session_maker,
            composites=[0.90, 0.89, 0.88, 0.87, 0.86, 0.86, 0.80],
            composite_stderr=0.001,
        )
        await _set_retest_cohort_size(
            session_maker,
            5,
            retest_eligibility_mode="statistical",
            retest_eligibility_z=1.64,
            retest_cohort_max_size=25,
        )
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        from ditto.api_server.continual_retest_settings import settings_from_row
        from ditto.api_server.endpoints.validator import _current_retest_cohort
        from ditto.db.queries.continual_retest_settings import (
            latest_continual_retest_settings_revision,
        )

        async with session_maker() as session:
            settings = settings_from_row(
                await latest_continual_retest_settings_revision(session)
            )
            emission, wave_members, cohort = await _current_retest_cohort(
                session, canonical_version=_BENCH_VERSION, settings=settings
            )

        assert len(emission) == 5
        assert len(wave_members) == 5
        assert len(cohort) == 6
        assert agent_ids[5] in {member.agent_id for member in cohort}
        assert agent_ids[5] not in {member.agent_id for member in emission}
        # Rank seven is genuinely behind, so the band stops rather than running on.
        assert agent_ids[6] not in {member.agent_id for member in cohort}

    async def test_raw_wave_gate_members_stay_in_the_retest_cohort(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A folded-out raw member must still receive the work gating the fold.

        Production reached exactly this fixed point: retained rows moved one
        raw top-five member below the folded cutoff, the scheduler stopped
        retesting it, and the shared-wave count stayed pinned to its shallow
        depth while the visible leaders kept accumulating seeds.
        """
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_top5_emission_set(
            session_maker,
            composites=[0.90, 0.89, 0.88, 0.87, 0.86, 0.85],
        )
        champion, *_, raw_cutoff, folded_entrant = agent_ids
        seeds = champion_anchored_seeds(champion, version=_BENCH_VERSION, max_seeds=16)[
            :3
        ]
        async with session_maker() as session, session.begin():
            for agent_id in agent_ids:
                wave_composite = 0.10 if agent_id == raw_cutoff else 0.90
                for seed in seeds:
                    session.add(
                        ConfirmationScore(
                            agent_id=agent_id,
                            validator_hotkey=_VALIDATOR_HOTKEY,
                            bench_version=_BENCH_VERSION,
                            seed=seed,
                            composite=wave_composite,
                            run_id=f"gate-{agent_id}-{seed}",
                            signature=None,
                        )
                    )
        await _set_retest_cohort_size(session_maker, 5)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()

        from ditto.api_server.continual_retest_settings import settings_from_row
        from ditto.api_server.endpoints.validator import _current_retest_cohort
        from ditto.db.queries.continual_retest_settings import (
            latest_continual_retest_settings_revision,
        )

        async with session_maker() as session:
            settings = settings_from_row(
                await latest_continual_retest_settings_revision(session)
            )
            emission, wave_members, cohort = await _current_retest_cohort(
                session, canonical_version=_BENCH_VERSION, settings=settings
            )

        emission_ids = {member.agent_id for member in emission}
        wave_ids = {member.agent_id for member in wave_members}
        cohort_ids = {member.agent_id for member in cohort}
        assert folded_entrant in emission_ids
        assert raw_cutoff not in emission_ids
        assert raw_cutoff in wave_ids
        assert folded_entrant not in wave_ids
        assert {raw_cutoff, folded_entrant} <= cohort_ids
        assert len(cohort) == 6

    async def test_folded_top_five_entrant_preempts_raw_member_for_catchup(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Current top-five membership, not raw rank, owns catch-up priority.

        This reproduces the production split where a folded rank-two member had
        zero continual confirmations while a raw top-five member retained deep
        history. The raw member must remain in the cohort so already-issued wave
        work can finish, but it must not outrank the current emission member
        that is missing the shared seed set.

        Putting the folded champion in the seed-completion wave empties the
        strict intersection until that depth-zero member is scored. The raw
        cutoff already holds every old seed, so its plan is empty rather than
        a growth seed the fairness guard would have to refuse.
        """
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_top5_emission_set(
            session_maker,
            composites=[0.90, 0.89, 0.88, 0.87, 0.86, 0.85],
        )
        champion, *_, raw_cutoff, folded_entrant = agent_ids
        seeds = champion_anchored_seeds(champion, version=_BENCH_VERSION, max_seeds=16)[
            :3
        ]
        async with session_maker() as session, session.begin():
            # Only the raw top five participated in the old wave. A very low
            # retained aggregate moves its cutoff member below the newcomer,
            # which enters the authoritative top five at depth zero.
            for agent_id in agent_ids[:5]:
                wave_composite = 0.10 if agent_id == raw_cutoff else 0.90
                for seed in seeds:
                    session.add(
                        ConfirmationScore(
                            agent_id=agent_id,
                            validator_hotkey=_VALIDATOR_HOTKEY,
                            bench_version=_BENCH_VERSION,
                            seed=seed,
                            composite=wave_composite,
                            run_id=f"priority-{agent_id}-{seed}",
                            signature=None,
                        )
                    )
        await _set_retest_cohort_size(session_maker, 5, idle_retests_enabled=True)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=1)

        raw_claim = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_top5_job_payload(champion, raw_cutoff, keypair=_KEYPAIRS[0]),
        )
        assert raw_claim.status_code == 409
        assert "no pending confirmation seeds" in raw_claim.json()["message"]

        entrant_claim = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_top5_job_payload(champion, folded_entrant, keypair=_KEYPAIRS[1]),
        )
        assert entrant_claim.status_code == 200, entrant_claim.text
        assert entrant_claim.json()["agent_id"] == str(folded_entrant)
        assert entrant_claim.json()["confirmation_datasets"][0]["seed"] in seeds

    async def test_emission_set_is_served_before_the_extended_cohort(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A wave completes on the top five, so they get the slots first."""
        agent_ids = await _seed_ranked_pool(session_maker, size=10)
        champion, sixth = agent_ids[0], agent_ids[5]
        await _set_retest_cohort_size(session_maker, 10)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=1)

        too_early = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, sixth),
        )

        assert too_early.status_code == 409
        assert "less confirmation coverage" in too_early.json()["message"]

    async def test_extended_member_cannot_hold_a_wave_open(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Wave completion stays keyed to the emission set at any cohort width.

        With all five emission members scored on the current seed the wave is
        complete and the lane opens the next one, so the champion can be leased
        again. Were completion keyed to the whole cohort, rank six's missing
        result would hold the wave open and this claim would 409 --- and the
        crown would sit behind whichever extended member was slowest.
        """
        from ditto.api_server.crn import champion_anchored_seeds

        agent_ids = await _seed_ranked_pool(session_maker, size=10)
        champion = agent_ids[0]
        seeds = champion_anchored_seeds(champion, version=_BENCH_VERSION, max_seeds=16)
        await _seed_confirmation_wave(session_maker, agent_ids[:5], seed=seeds[0])
        await _set_retest_cohort_size(session_maker, 10)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=1)

        advanced = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, champion),
        )

        assert advanced.status_code == 200, advanced.text


def _scorer_capable_capabilities(
    *,
    now: datetime,
    versions: Sequence[int] = (_BENCH_VERSION, _BENCH_VERSION + 1),
) -> dict[str, object]:
    """Capabilities that satisfy ``heartbeat_supports_version`` for ``versions``.

    This used to advertise ``[2, 3]`` and nothing else, because below v7 the
    version list was the whole story. It is not any more:
    ``verified_scorer_for_version`` additionally demands ticket inference, the
    signed score quorum and a v7 calibration manifest before it will call a
    scorer capable of v7 or later. Renumbering the list alone would have
    produced a fixture that reads as capable and is silently refused, which
    shows up as an unexplained 409 rather than as a capability problem.
    """
    return {
        **_V7_CAPABILITIES,
        "ticket_inference": True,
        "signed_score_quorum": True,
        "scorer_benchmarks": {
            "status": "fresh_verified",
            "supported_bench_versions": list(versions),
            "observed_at": int(now.timestamp()),
            "software_version": "1.2.2",
            "source_revision": "2" * 40,
            "v7_calibration": {
                "manifest_sha256": _V7_CALIBRATION_MANIFEST,
                "supported_routes": [
                    {
                        "provider": "openrouter",
                        "profile_revision": _V7_ROUTE_PROFILE,
                        "model": "openai/gpt-oss-20b",
                    }
                ],
            },
        },
    }


async def _seed_top5_rollout_standdown_fixture(
    maker: async_sessionmaker[AsyncSession],
    *,
    rollout_status: str | None = "collecting",
    desired_version_capable: bool = True,
    settings: dict[str, object] | None = None,
) -> tuple[UUID, UUID]:
    """A due top-five cohort plus an optional rollout off the current era.

    The open shapes are a rollout from the canonical version to the next one,
    which is what the stand-down is about. ``"activated"`` is different in kind
    and no longer needs a row of its own: the activation that put the fleet on
    the canonical version is already on record from ``_seed_top5_emission_set``,
    and it is the only activated transition the ledger can hold, since
    ``benchmark_rollouts_transition_idx`` is unique on (from, desired).
    """
    agent_ids = await _seed_top5_emission_set(maker, seed_heartbeats=False)
    now = datetime.now(UTC)
    capabilities = (
        _scorer_capable_capabilities(now=now)
        if desired_version_capable
        # A validator that cannot serve the desired version can never take
        # cohort work, so the rollout is not competing with it at all. It must
        # still be able to serve the CANONICAL version, or the lane refuses it
        # on capability (428) before the stand-down question is reached -- a
        # distinction that did not exist while the canonical era was v2.
        else _scorer_capable_capabilities(now=now, versions=(_BENCH_VERSION,))
    )
    for keypair in _KEYPAIRS:
        await _seed_validator_heartbeat(
            maker,
            keypair=keypair,
            protocol_version=13,
            capabilities=capabilities,
            stack=_V7_STACK,
        )
    async with maker() as session, session.begin():
        champion_row = await session.get(Agent, agent_ids[0])
        assert champion_row is not None
        champion_row.dataset_seed_block = 1
        # "activated" is already on record (see the docstring), so only the
        # open shapes add a row.
        if rollout_status is not None and rollout_status != "activated":
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_BENCH_VERSION,
                    desired_version=_BENCH_VERSION + 1,
                    status=rollout_status,
                    cohort_size=5,
                    created_at=now - timedelta(hours=1),
                    activated_at=(now if rollout_status == "activated" else None),
                )
            )
        if settings is not None:
            session.add(
                ContinualRetestSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings=settings,
                    checksum="b" * 64,
                    reason="operator override for rollout stand-down policy",
                    actor="operator@example.com",
                )
            )
    return agent_ids[0], agent_ids[1]


class TestTop5RolloutStanddown:
    """Continual retests yield scarce validator slots to an open rollout."""

    @pytest.fixture(autouse=True)
    async def _v7_lane(
        self, app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Everything the lane needs once the era it retests is v7.

        The emission sets below used to sit on v2, where a confirmation claim
        needed no dataset generator and no inference grant at all. Neither is
        optional now: ``top5-confirmation-job`` pins a dataset per seed and
        refuses (503) any lease it cannot attach a grant to. Wiring both here
        keeps every test in the class about what it was about -- claims,
        cohorts, bands, waves -- instead of about v7 plumbing.
        """
        _install_dataset_generator(app)
        await _install_ticket_inference(app, session_maker)

    @staticmethod
    def _install(app: FastAPI, session_maker: async_sessionmaker[AsyncSession]) -> None:
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=0)

    async def test_stands_down_capable_validator_while_rollout_collects(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, member = await _seed_top5_rollout_standdown_fixture(session_maker)
        self._install(app, session_maker)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 409
        message = response.json()["message"]
        # The refusal must name the rollout, or an operator reads it as a bug.
        assert "standing down" in message
        assert f"benchmark version {_BENCH_VERSION + 1}" in message
        async with session_maker() as session:
            issued = await session.scalar(
                select(func.count()).where(
                    ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST
                )
            )
        assert issued == 0

    async def test_stands_down_while_rollout_is_blocked_ineligible(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, member = await _seed_top5_rollout_standdown_fixture(
            session_maker, rollout_status="blocked_ineligible"
        )
        self._install(app, session_maker)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 409
        assert "standing down" in response.json()["message"]

    async def test_resumes_after_rollout_activates(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Activation retires the rollout, and retests confirm the new era.

        The activated transition here is the one that made the canonical
        version canonical -- a rollout that has already landed is terminal, so
        there is nothing left for the lane to stand down for. That is a
        different statement from ``rollout_status=None``, which is the fleet
        having never rolled at all.
        """
        champion, member = await _seed_top5_rollout_standdown_fixture(
            session_maker, rollout_status="activated"
        )
        self._install(app, session_maker)
        generator = MagicMock(run_size="full")
        generator.generate = AsyncMock(return_value="ab" * 32)
        app.dependency_overrides[get_dataset_generator] = lambda: generator

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(member)

    async def test_resumes_after_rollout_is_superseded(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, member = await _seed_top5_rollout_standdown_fixture(
            session_maker, rollout_status="superseded"
        )
        self._install(app, session_maker)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(member)

    async def test_no_open_rollout_leaves_the_lane_untouched(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, member = await _seed_top5_rollout_standdown_fixture(
            session_maker, rollout_status=None
        )
        self._install(app, session_maker)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(member)

    async def test_validator_that_cannot_serve_the_rollout_keeps_retesting(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Yield only the capacity the rollout can actually consume."""
        champion, member = await _seed_top5_rollout_standdown_fixture(
            session_maker, desired_version_capable=False
        )
        self._install(app, session_maker)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(member)

    async def test_all_mode_stands_down_incapable_validators_too(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, member = await _seed_top5_rollout_standdown_fixture(
            session_maker,
            desired_version_capable=False,
            settings={
                "aggregate_mode": "fleet_ready",
                "idle_retests_enabled": False,
                "rollout_standdown": "all",
            },
        )
        self._install(app, session_maker)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 409
        assert "standing down" in response.json()["message"]

    async def test_operator_override_forces_retests_during_a_rollout(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        champion, member = await _seed_top5_rollout_standdown_fixture(
            session_maker,
            settings={
                "aggregate_mode": "fleet_ready",
                "idle_retests_enabled": False,
                "rollout_standdown": "off",
            },
        )
        self._install(app, session_maker)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(member)

    async def test_in_flight_wave_still_reports_during_a_rollout(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The stand-down gates issuance only; leased waves finish intact."""
        from ditto.api_server.crn import champion_anchored_seeds

        champion, member = await _seed_top5_rollout_standdown_fixture(
            session_maker, rollout_status=None
        )
        self._install(app, session_maker)
        leased = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_AUTH_HEADER,
            json=_top5_job_payload(champion, member),
        )
        assert leased.status_code == 200, leased.text
        deadline = datetime.fromisoformat(leased.json()["deadline"])
        # The lease pins its own seed above v2, and the report has to name that
        # one; taking it from the job response keeps this test about the
        # stand-down rather than about seed derivation.
        seeds = [pin["seed"] for pin in leased.json()["confirmation_datasets"]]
        assert seeds == list(
            champion_anchored_seeds(champion, version=_BENCH_VERSION, max_seeds=16)[:1]
        )
        async with session_maker() as session, session.begin():
            session.add(
                BenchmarkRollout(
                    rollout_id=uuid4(),
                    from_version=_BENCH_VERSION,
                    desired_version=_BENCH_VERSION + 1,
                    status="collecting",
                    cohort_size=5,
                    created_at=datetime.now(UTC),
                )
            )

        reported = await client.post(
            f"/api/v1/validator/agent/{member}/top5-confirmation-score",
            json=_top5_score_payload(
                member,
                deadline=deadline,
                seeds=seeds,
                composites=[0.81],
            ),
        )

        assert reported.status_code == 200, reported.text


class TestReissuedLeaseKeepsSlotProgress:
    """A lease re-issued in place must not blank its slot in the fleet view.

    The platform refreshes ``deadline`` on an existing ticket row, but the
    validator keeps signing progress with the deadline it was handed at claim
    time. Confirming the slot on that stamp evicted a healthy run from the
    stored capacity, so slot-1 rendered as "Benchmark progress not reported"
    while slot-0 reported normally -- and the revoker then read the same absence
    as evidence the slot was idle.
    """

    async def test_deadline_drift_still_publishes_every_active_slot(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        first = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, name="slot-a"
        )
        second = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="slot-b",
            miner_hotkey="5SecondMiner" + "x" * 35,
        )
        await _seed_ticket(session_maker, first, slot_id="slot-0", bench_version=7)
        await _seed_ticket(session_maker, second, slot_id="slot-1", bench_version=7)
        _install_db(app, session_maker)
        _install_chain(app)
        first_progress = _progress("running_benchmark", completed=3, total=283)
        # Signed against the deadline this slot cached before its lease was
        # re-issued. One microsecond of drift used to be enough to erase it.
        drifted = _progress(
            "running_benchmark",
            completed=61,
            total=283,
            ticket_deadline=_TICKET_DEADLINE + timedelta(microseconds=1),
        )
        capacity = {
            "configured_slots": 4,
            "healthy_slots": ["slot-0", "slot-1", "slot-2", "slot-3"],
            "admission": "accepting",
            "active": [
                {
                    "slot_id": "slot-0",
                    "agent_id": str(first),
                    "bench_version": 7,
                    "progress": first_progress,
                },
                {
                    "slot_id": "slot-1",
                    "agent_id": str(second),
                    "bench_version": 7,
                    "progress": drifted,
                },
            ],
        }
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=10,
                state="running_benchmark",
                active_agent_id=first,
                benchmark_progress=first_progress,
                capabilities=_V9_CAPABILITIES,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert response.status_code == 200, response.text

        public = (await client.get("/api/v1/public/validators")).json()["validators"][0]
        assert [item["slot_id"] for item in public["active_benchmarks"]] == [
            "slot-0",
            "slot-1",
        ]
        by_slot = {item["slot_id"]: item for item in public["active_benchmarks"]}
        assert by_slot["slot-1"]["stage"] == "running_benchmark"
        assert by_slot["slot-1"]["completed_checks"] == 61
        # Every assigned slot resolves to real progress, so nothing renders as
        # "Benchmark progress not reported".
        assert all(item["stage"] is not None for item in public["assigned_benchmarks"])

    async def test_unconfirmed_slot_is_recorded_as_a_claim(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """The signed occupancy claim is stored whole, before confirmation."""
        agent = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, name="slot-a"
        )
        stray = uuid4()
        await _seed_ticket(session_maker, agent, slot_id="slot-0", bench_version=7)
        _install_db(app, session_maker)
        _install_chain(app)
        confirmed = _progress("running_benchmark", completed=3, total=283)
        capacity = {
            "configured_slots": 2,
            "healthy_slots": ["slot-0", "slot-1"],
            "admission": "accepting",
            "active": [
                {
                    "slot_id": "slot-0",
                    "agent_id": str(agent),
                    "bench_version": 7,
                    "progress": confirmed,
                },
                {
                    # No ticket for this one: it drops out of the confirmed
                    # capacity but must survive as a claim.
                    "slot_id": "slot-1",
                    "agent_id": str(stray),
                    "bench_version": 7,
                    "progress": _progress("running_benchmark", completed=9, total=283),
                },
            ],
        }
        response = await client.post(
            "/api/v1/validator/heartbeat",
            headers=_AUTH_HEADER,
            json=_heartbeat_payload(
                protocol_version=10,
                state="running_benchmark",
                active_agent_id=agent,
                benchmark_progress=confirmed,
                capabilities=_V9_CAPABILITIES,
                stack=_V7_STACK,
                stack_health=_V9_STACK_HEALTH,
                benchmark_capacity=capacity,
            ),
        )
        assert response.status_code == 200, response.text
        async with session_maker() as s:
            row = await s.get(ValidatorHeartbeat, _KEYPAIR.ss58_address)
            assert row is not None
            stored = row.benchmark_capacity
            assert stored is not None
            assert [slot["slot_id"] for slot in stored["active"]] == ["slot-0"]
            assert row.claimed_slots == [
                {"slot_id": "slot-0", "agent_id": str(agent)},
                {"slot_id": "slot-1", "agent_id": str(stray)},
            ]


class TestUnmatchableWorkClaimIsLoud:
    """A validator reporting work no lease matches must not fail silently.

    A continual-retest run reported its progress against the wrong slot: the
    lane executed under the default ``slot-0`` while the lease was issued on
    ``slot-1``. Every slot then failed confirmation and was dropped, so the
    stored capacity was empty and the fleet view rendered a healthy, scoring
    validator exactly like an idle one -- ``reported_agent_id: null``,
    ``active_benchmarks: []``. The per-slot drop lines are INFO, so nothing in
    the logs distinguished it either.

    Both heartbeats below stay ACCEPTED on purpose. Rejecting either would stop
    refreshing ``seen_at``, and a frozen ``seen_at`` is what force-expires
    leases.
    """

    async def test_retest_reported_on_the_wrong_slot_warns_and_is_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        agent = await _seed_agent(
            session_maker, status=AgentStatus.EVALUATING, name="retest-a"
        )
        # The lease the platform actually issued: a retest on slot-1.
        await _seed_ticket(
            session_maker,
            agent,
            slot_id="slot-1",
            bench_version=7,
            purpose=TicketPurpose.CONTINUAL_RETEST,
        )
        _install_db(app, session_maker)
        _install_chain(app)
        progress = _progress("running_benchmark", completed=41, total=283)
        capacity = {
            "configured_slots": 2,
            "healthy_slots": ["slot-0", "slot-1"],
            "admission": "accepting",
            "active": [
                {
                    # ...but the validator reports it against slot-0.
                    "slot_id": "slot-0",
                    "agent_id": str(agent),
                    "bench_version": 7,
                    "progress": progress,
                }
            ],
        }
        with caplog.at_level(logging.WARNING):
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers=_AUTH_HEADER,
                json=_heartbeat_payload(
                    protocol_version=10,
                    state="running_benchmark",
                    active_agent_id=agent,
                    benchmark_progress=progress,
                    capabilities=_V9_CAPABILITIES,
                    stack=_V7_STACK,
                    stack_health=_V9_STACK_HEALTH,
                    benchmark_capacity=capacity,
                ),
            )

        # Accepted, so seen_at keeps moving and the lease is not force-expired.
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True
        async with session_maker() as s:
            row = await s.get(ValidatorHeartbeat, _KEYPAIR.ss58_address)
            assert row is not None
            assert row.benchmark_capacity is not None
            # The unmatchable slot is still dropped -- that behaviour is load
            # bearing for the revoker -- but now it is announced.
            assert row.benchmark_capacity["active"] == []
            assert row.claimed_slots == [{"slot_id": "slot-0", "agent_id": str(agent)}]
        assert any(
            "no claimed slot matches a live lease" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), "the wrong-slot heartbeat produced no warning"

    async def test_running_benchmark_with_no_active_slot_warns_and_is_accepted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The wire model constrains ``state`` only when ``active`` is non-empty,
        # so this contradiction validates cleanly. The lease gate ignores
        # ``state`` under v10+ and reads the empty capacity as idle.
        _install_db(app, session_maker)
        _install_chain(app)
        capacity = {
            "configured_slots": 2,
            "healthy_slots": ["slot-0", "slot-1"],
            "admission": "accepting",
            "active": [],
        }
        # A distinct validator, so this never races the sibling test's row for
        # the same wall-clock second (a same-second repeat stores nothing).
        with caplog.at_level(logging.WARNING):
            response = await client.post(
                "/api/v1/validator/heartbeat",
                headers={"X-Validator-Hotkey": _KEYPAIRS[1].ss58_address},
                json=_heartbeat_payload(
                    keypair=_KEYPAIRS[1],
                    protocol_version=10,
                    state="running_benchmark",
                    capabilities=_V9_CAPABILITIES,
                    stack=_V7_STACK,
                    stack_health=_V9_STACK_HEALTH,
                    benchmark_capacity=capacity,
                ),
            )

        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True
        assert any(
            "self-contradictory" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), "the contradictory heartbeat produced no warning"


async def _seed_catchup_board(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    *,
    settled_depth: int = 4,
    newcomer_composite: float = 0.87,
    pool_composites: Sequence[float] = (0.90, 0.88, 0.86, 0.84, 0.82),
    extra_keypairs: Sequence[bittensor.Keypair] = (),
    benchmark_capacity: dict[str, object] | None = None,
    bench_version: int = _BENCH_VERSION,
) -> tuple[UUID, UUID, list[int]]:
    """Reproduce today's incident: a promotion into a settled emission set.

    Five members share ``settled_depth`` completed waves, then a freshly
    finalized agent lands in the top five at seed depth zero and gates the
    intersection for everyone. Returns ``(champion, newcomer, settled_seeds)``.

    Runs on benchmark v3 because that is the first version where the platform
    pins a dataset -- and therefore a ``seed`` -- onto the retest ticket. At v2
    every retest lease is a seedless legacy bundle, so nothing here about which
    seed a validator holds would be observable.
    """
    from ditto.api_server.crn import champion_anchored_seeds

    keypairs = (*_KEYPAIRS, *extra_keypairs)
    agent_ids = await _seed_top5_emission_set(
        maker,
        bench_version=bench_version,
        composites=list(pool_composites),
        seed_heartbeats=False,
    )
    now = datetime.now(UTC)
    for keypair in keypairs:
        await _seed_validator_heartbeat(
            maker,
            keypair=keypair,
            protocol_version=13,
            capabilities=_scorer_capable_capabilities(now=now),
            stack=_V7_STACK,
            benchmark_capacity=benchmark_capacity,
        )
    champion = agent_ids[0]
    settled_seeds = champion_anchored_seeds(
        champion, version=bench_version, max_seeds=16
    )[:settled_depth]
    newcomer = await _seed_agent(
        maker,
        status=AgentStatus.SCORED,
        name="promoted-newcomer",
        miner_hotkey="5PromotedNewcomer",
        sha256="ee" * 32,
        created_at=now,
    )
    async with maker() as session, session.begin():
        champion_row = await session.get(Agent, champion)
        assert champion_row is not None
        champion_row.dataset_seed_block = 1
        for index, keypair in enumerate(_KEYPAIRS):
            session.add(
                Score(
                    agent_id=newcomer,
                    bench_version=bench_version,
                    validator_hotkey=keypair.ss58_address,
                    run_id=f"newcomer-{index}",
                    signature=None,
                    seed=index,
                    composite=newcomer_composite,
                    tool_mean=newcomer_composite,
                    memory_mean=newcomer_composite,
                    median_ms=100,
                    n=114,
                    details={
                        "bench_version": bench_version,
                        "composite_stderr": 0.03,
                    },
                    generated_at=now,
                )
            )
        # Each settled member's wave composite equals its own quorum median, so
        # folding the waves in leaves the ranking exactly where it was. Any
        # other value would re-rank the pool and change which agents the test
        # is talking about.
        await append_confirmation_scores(
            session,
            rows=[
                ConfirmationSeedScore(
                    agent_id,
                    _VALIDATOR_HOTKEY,
                    seed,
                    composite,
                    f"settled-{agent_id}-{seed}",
                    None,
                )
                for agent_id, composite in zip(
                    agent_ids[:5], pool_composites[:5], strict=True
                )
                for seed in settled_seeds
            ],
            bench_version=bench_version,
            created_at=now,
        )
    _install_db(app, maker)
    app.state.session_maker = maker
    app.state.continual_retest_settings.invalidate()
    _install_chain_with_block(app, block_number=1, extra_keypairs=tuple(extra_keypairs))
    generator = MagicMock(run_size="full")
    generator.generate = AsyncMock(
        side_effect=lambda seed, *, bench_version: hashlib.sha256(
            f"{bench_version}:{seed}".encode()
        ).hexdigest()
    )
    app.dependency_overrides[get_dataset_generator] = lambda: generator
    return champion, newcomer, settled_seeds


async def _leased_retest_seeds(
    maker: async_sessionmaker[AsyncSession], agent_id: UUID
) -> dict[str, int | None]:
    """``{validator_hotkey: leased seed}`` for one agent's open retest leases."""
    async with maker() as session:
        rows = await session.execute(
            select(ValidatorTicket.validator_hotkey, ValidatorTicket.seed).where(
                ValidatorTicket.agent_id == agent_id,
                ValidatorTicket.bench_version == _BENCH_VERSION,
                ValidatorTicket.purpose == TicketPurpose.CONTINUAL_RETEST,
                ValidatorTicket.status == TicketStatus.ISSUED,
            )
        )
        return dict(rows.all())  # type: ignore[arg-type]


class TestTop5CatchUpConvergence:
    """A member promoted at depth zero converges in one round, not N.

    ``ConfirmationScore`` is append-only and a completed wave is recomputed at
    read time by intersecting the CURRENT emission set's seed sets. A promotion
    therefore arrives at depth zero and gates that intersection for everyone.
    ditto-platform#489 stopped it from discarding accumulated evidence on the
    read side; these tests cover the write side -- making the newcomer's whole
    coverage gap claimable at once so the degraded window is brief.
    """

    @pytest.fixture(autouse=True)
    async def _v7_lane(
        self, app: FastAPI, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        """Everything the lane needs once the era it retests is v7.

        The emission sets below used to sit on v2, where a confirmation claim
        needed no dataset generator and no inference grant at all. Neither is
        optional now: ``top5-confirmation-job`` pins a dataset per seed and
        refuses (503) any lease it cannot attach a grant to. Wiring both here
        keeps every test in the class about what it was about -- claims,
        cohorts, bands, waves -- instead of about v7 plumbing.
        """
        _install_dataset_generator(app)
        await _install_ticket_inference(app, session_maker)

    async def test_promotion_makes_the_whole_backlog_claimable_at_once(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from ditto.api_server.endpoints.validator import (
            _current_emission_set,
            _top5_confirmation_seed_plan,
        )

        champion, newcomer, settled = await _seed_catchup_board(app, session_maker)

        async with session_maker() as session:
            members = await _current_emission_set(
                session, canonical_version=_BENCH_VERSION
            )
        member_ids = [member.agent_id for member in members]
        assert members[0].agent_id == champion
        assert newcomer in member_ids, "the newcomer must have been promoted"
        # The incident state: four settled members still carry their waves
        # (ditto-platform#489), the newcomer carries none and gates the strict
        # intersection.
        by_id = {member.agent_id: member for member in members}
        assert by_id[newcomer].completed_wave_composites is None
        assert all(
            len(by_id[agent_id].completed_wave_composites or ()) == len(settled)
            for agent_id in member_ids
            if agent_id != newcomer
        )

        async with session_maker() as session:
            plan = await _top5_confirmation_seed_plan(
                session,
                champion_agent_id=champion,
                member_agent_id=newcomer,
                wave_member_ids=tuple(member_ids),
                canonical_version=_BENCH_VERSION,
            )

        # Every missing pair at once -- not the single open wave seed that took
        # one round per seed to work through.
        assert plan == tuple(settled)

        # ...and three validators claim three DIFFERENT seeds in one round.
        for keypair in _KEYPAIRS:
            response = await client.post(
                "/api/v1/validator/top5-confirmation-job",
                headers=_top5_auth_header(keypair),
                json=_top5_job_payload(champion, newcomer, keypair=keypair),
            )
            assert response.status_code == 200, response.text
            assert response.json()["agent_id"] == str(newcomer)

        leased = await _leased_retest_seeds(session_maker, newcomer)
        assert sorted(leased) == sorted(keypair.ss58_address for keypair in _KEYPAIRS)
        assert sorted(seed for seed in leased.values() if seed is not None) == sorted(
            settled[:3]
        ), "each validator must hold a distinct backlog seed"

    async def test_emission_catchup_bypasses_the_growth_cadence(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Already-owed coverage drains even when a new wave is not due.

        The reign cadence limits introduction of fresh shared seeds. It must not
        strand a promoted emission member below the already-accepted wave depth:
        that is reconciliation work, and until it lands the fold has less paired
        evidence even while healthy validator slots sit idle.
        """
        champion, newcomer, settled = await _seed_catchup_board(app, session_maker)
        _install_chain_with_block(app, block_number=361)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_top5_job_payload(champion, newcomer, keypair=_KEYPAIRS[0]),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(newcomer)
        assert response.json()["confirmation_datasets"][0]["seed"] in settled

    async def test_auto_routed_idle_claim_leases_depth_zero_champion(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A new crown at depth zero must win auto-route over a catching-up tail.

        Production 2026-08-25: aceron_v23 took the KOTH crown at 0 shared seeds
        while Hogwarts_v2 v18 (13) / unione (32) / lets (32) still held the
        previous family's trail. Auto-routed v2 claims (slot only) spent every
        idle slot on the tail. Explicit v1 member claims already prefer the
        newcomer; the live fleet never sends those.

        The newcomer here is the champion (0.99 vs a 0.90 settled pool), already
        holds scored canonical quorum tickets, the reign cadence is not due, and
        idle retests are on — the same shape as the live board.
        """
        old_champion, newcomer, _settled = await _seed_catchup_board(
            app, session_maker, newcomer_composite=0.99
        )
        now = datetime.now(UTC)
        async with session_maker() as session, session.begin():
            for index, keypair in enumerate(_KEYPAIRS):
                session.add(
                    ValidatorTicket(
                        agent_id=newcomer,
                        bench_version=_BENCH_VERSION,
                        validator_hotkey=keypair.ss58_address,
                        slot_id="slot-0",
                        status=TicketStatus.SCORED,
                        purpose=TicketPurpose.CANONICAL_QUORUM,
                        purpose_revision=1,
                        issued_at=now - timedelta(hours=1),
                        deadline=now + timedelta(hours=6),
                        seed=index,
                    )
                )
        await _set_retest_cohort_size(session_maker, 5, idle_retests_enabled=True)
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=361)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_auto_top5_job_payload("slot-0", keypair=_KEYPAIRS[0]),
        )

        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == str(newcomer), (
            f"auto-route leased {response.json()['agent_id']} "
            f"(old champion {old_champion}) instead of depth-zero crown {newcomer}"
        )

    async def test_auto_routed_idle_claim_leases_public_board_champion_not_predecessor(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A newer UUID that holds the public crown must beat its predecessor.

        Production 2026-08-25 after #1152: aceron_v23 was the public-board
        champion at 0 confirmation seeds, but confirmation-enriched owner-dedupe
        still named aceron_v20. Auto-route logged champion=v20 and leased
        Hogwarts_v2 v16 from the previous family. The seed lane must follow the
        board, not the folded predecessor.
        """
        from ditto.api_server.crn import champion_anchored_seeds

        pool = await _seed_top5_emission_set(
            session_maker,
            composites=[0.90, 0.88, 0.86, 0.84, 0.82],
            seed_heartbeats=False,
        )
        predecessor = pool[0]
        now = datetime.now(UTC)
        for keypair in _KEYPAIRS:
            await _seed_validator_heartbeat(
                session_maker,
                keypair=keypair,
                protocol_version=13,
                capabilities=_scorer_capable_capabilities(now=now),
                stack=_V7_STACK,
            )
        async with session_maker() as session, session.begin():
            predecessor_row = await session.get(Agent, predecessor)
            assert predecessor_row is not None
            predecessor_hotkey = predecessor_row.miner_hotkey
            predecessor_row.dataset_seed_block = 1
            old_seeds = champion_anchored_seeds(
                predecessor, version=_BENCH_VERSION, max_seeds=16
            )[:12]
            for agent_id in pool:
                for seed in old_seeds:
                    session.add(
                        ConfirmationScore(
                            agent_id=agent_id,
                            validator_hotkey=_VALIDATOR_HOTKEY,
                            bench_version=_BENCH_VERSION,
                            seed=seed,
                            composite=0.998,
                            run_id=f"old-family-{agent_id}-{seed}",
                            signature=None,
                        )
                    )
        newcomer = await _seed_agent(
            session_maker,
            status=AgentStatus.SCORED,
            name="public-board-champion",
            miner_hotkey=predecessor_hotkey,
            sha256="ab" * 32,
            created_at=now,
        )
        async with session_maker() as session, session.begin():
            for index, keypair in enumerate(_KEYPAIRS):
                session.add(
                    Score(
                        agent_id=newcomer,
                        bench_version=_BENCH_VERSION,
                        validator_hotkey=keypair.ss58_address,
                        run_id=f"newcomer-{index}",
                        signature=None,
                        seed=index,
                        composite=0.975,
                        tool_mean=0.975,
                        memory_mean=0.975,
                        median_ms=100,
                        n=114,
                        details={
                            "bench_version": _BENCH_VERSION,
                            "composite_stderr": 0.03,
                        },
                        generated_at=now,
                    )
                )
                session.add(
                    ValidatorTicket(
                        agent_id=newcomer,
                        bench_version=_BENCH_VERSION,
                        validator_hotkey=keypair.ss58_address,
                        slot_id="slot-0",
                        status=TicketStatus.SCORED,
                        purpose=TicketPurpose.CANONICAL_QUORUM,
                        purpose_revision=1,
                        issued_at=now - timedelta(hours=1),
                        deadline=now + timedelta(hours=6),
                        seed=index,
                    )
                )
        await _set_retest_cohort_size(session_maker, 5, idle_retests_enabled=True)
        _install_db(app, session_maker)
        app.state.session_maker = session_maker
        app.state.continual_retest_settings.invalidate()
        _install_chain_with_block(app, block_number=361)
        app.state.config = replace(app.state.config, top5_backoff_base=2)

        response = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_auto_top5_job_payload("slot-0", keypair=_KEYPAIRS[0]),
        )

        assert response.status_code == 200, response.text
        leased = response.json()["agent_id"]
        assert leased == str(newcomer), (
            f"auto-route leased {leased} "
            f"(predecessor {predecessor}, tail {pool[1]}) "
            f"instead of public-board champion {newcomer}"
        )

    async def test_draining_the_backlog_restores_the_shared_seed_set(
        self,
        app: FastAPI,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """One round of parallel catch-up puts every member back on one wave."""
        from ditto.api_server.endpoints.validator import _current_emission_set

        champion, newcomer, settled = await _seed_catchup_board(app, session_maker)
        async with session_maker() as session:
            before = {
                member.agent_id: member.completed_wave_composites
                for member in await _current_emission_set(
                    session, canonical_version=_BENCH_VERSION
                )
            }

        async with session_maker() as session, session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    ConfirmationSeedScore(
                        newcomer,
                        _VALIDATOR_HOTKEY,
                        seed,
                        0.87,
                        f"catchup-{seed}",
                        None,
                    )
                    for seed in settled
                ],
                bench_version=_BENCH_VERSION,
                created_at=datetime.now(UTC),
            )

        async with session_maker() as session:
            after = {
                member.agent_id: member.completed_wave_composites
                for member in await _current_emission_set(
                    session, canonical_version=_BENCH_VERSION
                )
            }

        assert before[newcomer] is None
        assert after[newcomer] == (0.87,) * len(settled)
        # The survivors never moved. Faster convergence must add the newcomer's
        # evidence, not perturb anybody else's published score.
        assert {
            agent_id: value for agent_id, value in after.items() if agent_id != newcomer
        } == {
            agent_id: value
            for agent_id, value in before.items()
            if agent_id != newcomer
        }

    async def test_reconciliation_covers_exactly_the_gap_and_repeats_cleanly(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Exactly the missing pairs, nothing else, stable under repetition."""
        from ditto.api_server.endpoints.validator import (
            _claimable_confirmation_seed,
            _current_emission_set,
            _top5_confirmation_seed_plan,
        )

        champion, newcomer, settled = await _seed_catchup_board(app, session_maker)
        async with session_maker() as session:
            member_ids = tuple(
                member.agent_id
                for member in await _current_emission_set(
                    session, canonical_version=_BENCH_VERSION
                )
            )

        # Half the gap already covered: the plan must name the other half only.
        covered, missing = settled[:2], settled[2:]
        async with session_maker() as session, session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    ConfirmationSeedScore(
                        newcomer, _VALIDATOR_HOTKEY, seed, 0.87, f"partial-{seed}", None
                    )
                    for seed in covered
                ],
                bench_version=_BENCH_VERSION,
                created_at=datetime.now(UTC),
            )

        async def _plan(member: UUID) -> tuple[int, ...]:
            async with session_maker() as session:
                return await _top5_confirmation_seed_plan(
                    session,
                    champion_agent_id=champion,
                    member_agent_id=member,
                    wave_member_ids=member_ids,
                    canonical_version=_BENCH_VERSION,
                )

        assert await _plan(newcomer) == tuple(missing)
        # Idempotent: reading the plan is not what makes work happen, so a
        # repeat invocation must return the identical set.
        assert await _plan(newcomer) == tuple(missing)

        # A member that already holds every settled seed has no backlog, and no
        # growth seed either while the newcomer is holding the wave open.
        settled_member = next(
            agent_id for agent_id in member_ids if agent_id != newcomer
        )
        assert await _plan(settled_member) == ()

        # A lease is not evidence: claiming one seed leaves the plan alone but
        # takes that seed out of circulation for other validators.
        first = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_top5_job_payload(champion, newcomer, keypair=_KEYPAIRS[0]),
        )
        assert first.status_code == 200, first.text
        assert await _plan(newcomer) == tuple(missing)
        assert await _leased_retest_seeds(session_maker, newcomer) == {
            _KEYPAIRS[0].ss58_address: missing[0]
        }

        # A second validator takes the NEXT seed rather than queueing behind the
        # first, and the holder re-polling resumes its own seed instead of
        # opening a second one. (End to end the re-poll is refused one layer
        # further down -- a single-slot validator has no idle slot left while it
        # holds this very lease -- so the idempotency is asserted where it
        # lives.)
        leases = await _leased_retest_seeds(session_maker, newcomer)
        assert (
            _claimable_confirmation_seed(
                seeds=missing,
                leases=leases,
                validator_hotkey=_KEYPAIRS[1].ss58_address,
            )
            == missing[1]
        )
        assert (
            _claimable_confirmation_seed(
                seeds=missing,
                leases=leases,
                validator_hotkey=_KEYPAIRS[0].ss58_address,
            )
            == missing[0]
        )

    async def test_catchup_preempts_extended_cohort_top_up(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """A behind emission member outranks spare-capacity work below it.

        Preemption is a fact about stored evidence, not a promotion timestamp:
        the privilege exists exactly while a backlog does. A member that leaves
        and re-enters the top five keeps its append-only rows and so cannot
        re-acquire a backlog by oscillating.
        """
        from ditto.api_server.endpoints.validator import (
            _current_emission_set,
            _unserved_catchup_members,
        )

        champion, newcomer, settled = await _seed_catchup_board(
            app,
            session_maker,
            pool_composites=(0.90, 0.88, 0.86, 0.84, 0.82, 0.70),
        )
        await _set_retest_cohort_size(session_maker, 10, idle_retests_enabled=True)
        app.state.continual_retest_settings.invalidate()
        async with session_maker() as session:
            member_ids = {
                member.agent_id
                for member in await _current_emission_set(
                    session, canonical_version=_BENCH_VERSION
                )
            }
            extended = await session.scalars(
                select(Agent.agent_id).where(Agent.name == "top5-5")
            )
        extended_member = extended.one()
        assert extended_member not in member_ids

        # The newcomer takes one backlog seed. Before this change that emptied
        # the waiting set -- it was the only member wanting the open wave seed --
        # and the next validator spent its slot on rank six while fifteen
        # backlog seeds sat unclaimed.
        held = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[0]),
            json=_top5_job_payload(champion, newcomer, keypair=_KEYPAIRS[0]),
        )
        assert held.status_code == 200, held.text

        # Rank six has no confirmation rows at all, so it genuinely wants work.
        # It is refused while the newcomer's backlog is still unserved.
        preempted = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_top5_job_payload(champion, extended_member, keypair=_KEYPAIRS[1]),
        )
        assert preempted.status_code == 409
        assert "less confirmation coverage" in preempted.json()["message"]

        async def _behind() -> frozenset[UUID]:
            async with session_maker() as session:
                return await _unserved_catchup_members(
                    session,
                    champion_agent_id=champion,
                    emission_member_ids=tuple(member_ids),
                    canonical_version=_BENCH_VERSION,
                    now=datetime.now(UTC),
                )

        assert await _behind() == frozenset({newcomer})

        # The privilege is bounded by the backlog, and expires with it. Once the
        # newcomer holds every settled seed it stops preempting anything, and
        # ordinary emission-first ordering is all that is left. There is no
        # promotion timestamp here to re-arm, so a member churning in and out of
        # the top five cannot keep jumping the queue.
        async with session_maker() as session, session.begin():
            await append_confirmation_scores(
                session,
                rows=[
                    ConfirmationSeedScore(
                        newcomer, _VALIDATOR_HOTKEY, seed, 0.87, f"caught-{seed}", None
                    )
                    for seed in settled
                ],
                bench_version=_BENCH_VERSION,
                created_at=datetime.now(UTC),
            )

        assert await _behind() == frozenset()

    async def test_catchup_never_takes_a_slot_from_ordinary_scoring(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        """Preemption reorders the retest lane and nothing else.

        Retests are the idle-time consumer: a validator asks ``/job`` for
        ordinary work first and claims a retest only when that returns nothing.
        The strongest thing catch-up preemption can do to any claim is turn one
        409 on the *retest* endpoint into a different one -- it has no reach
        into ``request_job`` at all -- so a validator it refuses still gets a
        first-quorum submission the instant it asks for one.
        """
        one_slot: dict[str, object] = {
            "configured_slots": 1,
            "healthy_slots": ["slot-0"],
            "admission": "accepting",
            "active": [],
        }
        champion, newcomer, settled = await _seed_catchup_board(
            app,
            session_maker,
            bench_version=_BENCH_VERSION,
            pool_composites=(0.90, 0.88, 0.86, 0.84, 0.82, 0.70),
            benchmark_capacity=one_slot,
        )
        await _set_retest_cohort_size(session_maker, 10, idle_retests_enabled=True)
        app.state.continual_retest_settings.invalidate()
        async with session_maker() as session:
            extended_member = (
                await session.scalars(
                    select(Agent.agent_id).where(Agent.name == "top5-5")
                )
            ).one()
        # A brand-new submission with no scores at all: exactly the work that
        # must never queue behind catch-up.
        fresh = await _seed_agent(
            session_maker,
            status=AgentStatus.EVALUATING,
            name="fresh-submission",
            miner_hotkey="5FreshSubmission",
            sha256="cd" * 32,
        )
        # One validator is already running the newcomer's first backlog seed,
        # which is precisely the state that used to empty the waiting set and
        # let the next validator spend its slot on rank six.
        await _seed_ticket(
            session_maker,
            newcomer,
            keypair=_KEYPAIRS[0],
            deadline=datetime.now(UTC) + timedelta(hours=1),
            purpose=TicketPurpose.CONTINUAL_RETEST,
            seed=settled[0],
        )

        # Catch-up preempts the extended cohort's top-up claim...
        preempted = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_top5_job_payload(champion, extended_member, keypair=_KEYPAIRS[1]),
        )
        assert preempted.status_code == 409
        assert "less confirmation coverage" in preempted.json()["message"]

        # ...and costs it nothing: the same validator's slot is still free, and
        # ordinary scoring hands it the new submission. This is the starvation
        # property -- catch-up reorders the retest lane, never the queue.
        after_preemption = await client.post(
            "/api/v1/validator/job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_job_payload(_KEYPAIRS[1], slot_id="slot-0"),
        )
        assert after_preemption.status_code == 200, after_preemption.text
        assert after_preemption.json()["agent_id"] == str(fresh)

        # And the reverse rail still holds: a validator whose only slot is on a
        # canonical lease cannot be handed catch-up work, however badly the
        # newcomer needs it. Catch-up never evicts or double-books queue work.
        denied = await client.post(
            "/api/v1/validator/top5-confirmation-job",
            headers=_top5_auth_header(_KEYPAIRS[1]),
            json=_top5_job_payload(champion, newcomer, keypair=_KEYPAIRS[1]),
        )
        assert denied.status_code == 409
        assert "no idle slot" in denied.json()["message"]

        # A third validator, untouched by any of it, still scores the queue.
        third = await client.post(
            "/api/v1/validator/job",
            headers=_top5_auth_header(_KEYPAIRS[2]),
            json=_job_payload(_KEYPAIRS[2], slot_id="slot-0"),
        )
        assert third.status_code == 200, third.text
        assert third.json()["agent_id"] == str(fresh)
        async with session_maker() as session:
            issued = await session.scalar(
                select(func.count())
                .select_from(ValidatorTicket)
                .where(
                    ValidatorTicket.agent_id == fresh,
                    ValidatorTicket.status == TicketStatus.ISSUED,
                    ValidatorTicket.purpose == TicketPurpose.CANONICAL_QUORUM,
                )
            )
        assert issued == 2, "ordinary quorum scoring must be untouched"
