"""Validator platform client authentication tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import MagicMock
from uuid import UUID

import bittensor
import httpx
import pytest

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.coding import (
    CodingAuthoringEvidence,
    CodingAuthoringLeaseRequest,
    CodingCapabilityCertificationReceipt,
    CodingGradingLeaseRequest,
    CodingRunManifest,
    CodingTaskEvidence,
    SubmitCodingAuthoringFreezeRequest,
    SubmitCodingCertificationRequest,
    SubmitCodingShadowResultRequest,
    coding_authoring_freeze_signing_message,
    coding_authoring_lease_signing_message,
    coding_certification_signing_message,
    coding_grading_lease_signing_message,
    coding_shadow_result_signing_message,
)
from ditto.api_models.coding_certification_leases import (
    CodingCertificationHarnessLaunchRequest,
    coding_certification_harness_launch_signing_message,
)
from ditto.api_models.coding_claims import (
    CodingClaimActionRequest,
    CodingClaimNextRequest,
    CodingClaimResponse,
    coding_claim_action_signing_message,
    coding_claim_next_signing_message,
)
from ditto.api_models.coding_evidence_upload import (
    CodingSealedEvidenceFinalizeRequest,
    CodingSealedEvidenceKind,
    CodingSealedEvidenceUploadCapabilityRequest,
    coding_sealed_evidence_finalize_signing_message,
    coding_sealed_evidence_upload_signing_message,
)
from ditto.api_models.coding_harness import (
    CodingHarnessLaunchRequest,
    coding_harness_launch_signing_message,
)
from ditto.api_models.coding_inference_grants import (
    CodingInferenceExchangeRequest,
    CodingInferenceGrantRequest,
    CodingInferenceRevokeRequest,
    coding_inference_exchange_signing_message,
    coding_inference_grant_signing_message,
    coding_inference_revoke_signing_message,
)
from ditto.api_models.validator import (
    FAILURE_DETAIL_MAX_LENGTH,
    LEGACY_FAILURE_DETAIL_MAX_LENGTH,
    FailJobRequest,
    JobRequest,
    JobResponse,
    LedgerEntry,
    LedgerScoreProof,
    ScoreReport,
    ValidatorHeartbeatRequest,
)
from ditto.validator.coding_publication import PendingRelease, ReleaseReservation
from ditto.validator.errors import PlatformError, PlatformInfrastructureError
from ditto.validator.platform import PlatformClient
from ditto.validator.signing import (
    artifact_signing_message,
    job_fail_signing_message,
    job_signing_message,
    ledger_signing_message,
    sign_coding_certification,
    sign_score,
)

_SELECTION_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_selection_v1.json"
)
_ARTIFACT_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_artifact_capability_v1.json"
)
_EXECUTION_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_execution_plan_v1.json"
)
_AUTHORING_FREEZE_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_authoring_freeze_v1.json"
)
_GRADING_LEASE_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages/dittobench-coding-contract/testdata/coding_grading_lease_v1.json"
)
_SHADOW_RESULT_VECTOR_PATH = (
    Path(__file__).parents[3]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_shadow_result_submission_v1.json"
)
_SUPERVISOR_PATH = (
    Path(__file__).parents[3]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_attempt_supervisor_v1.json"
)


def _authoring_response() -> dict[str, Any]:
    selection = json.loads(_SELECTION_VECTOR_PATH.read_text(encoding="utf-8"))
    artifacts = json.loads(_ARTIFACT_VECTOR_PATH.read_text(encoding="utf-8"))
    execution = json.loads(_EXECUTION_VECTOR_PATH.read_text(encoding="utf-8"))
    task = selection["task_version"]["payload"]
    return {
        "schema": "dittobench-coding-authoring-lease-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "ticket_id": artifacts["capabilities"][0]["ticket_id"],
        "ticket_deadline": artifacts["capabilities"][0]["ticket_deadline"],
        "coding_run_id": selection["run_manifest"]["coding_run_id"],
        "run_manifest_sha256": selection["run_authority"]["run_manifest_sha256"],
        "task_set_manifest_sha256": selection["run_manifest"][
            "task_set_manifest_sha256"
        ],
        "repository_epoch": task["repository_epoch"],
        "issue_sha256": task["issue_sha256"],
        "runtime_policy_sha256": task["runtime_policy_sha256"],
        "budgets_sha256": task["budgets_sha256"],
        "issue": selection["issue"],
        "runtime_policy": selection["runtime_policy"],
        "budgets": selection["budgets"],
        "runner_plan_sha256": execution["expected"]["runner_plan_sha256"],
        "runner_plan": execution["runner_plan"],
        "run_manifest": selection["run_manifest"],
        "capabilities": artifacts["capabilities"][:3],
    }


def _grading_vector() -> dict[str, Any]:
    return json.loads(_GRADING_LEASE_VECTOR_PATH.read_text(encoding="utf-8"))


def _shadow_result_vector() -> dict[str, Any]:
    return json.loads(_SHADOW_RESULT_VECTOR_PATH.read_text(encoding="utf-8"))


def _shadow_result_authority(
    vector: dict[str, Any],
) -> tuple[CodingRunManifest, list[CodingTaskEvidence]]:
    manifest = CodingRunManifest.model_validate_json(
        json.dumps(vector["authority"]["run_manifest"])
    )
    tasks = [
        CodingTaskEvidence.model_validate_json(json.dumps(item))
        for item in vector["authority"]["task_evidence"]
    ]
    return manifest, tasks


async def test_job_claim_is_fresh_and_signed_by_validator_hotkey() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Validator-Hotkey"] == keypair.ss58_address
        claim = JobRequest.model_validate(json.loads(request.content))
        message = job_signing_message(
            validator_hotkey=claim.validator_hotkey,
            nonce=claim.nonce,
            requested_at=claim.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(claim.signature))
        return httpx.Response(204)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        platform = PlatformClient(config, http, keypair)  # type: ignore[arg-type]
        assert await platform.request_job() is None


async def test_coding_authoring_client_posts_signed_request_and_parses_lease() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    response_body = _authoring_response()
    ticket_id = UUID(response_body["ticket_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/validator/coding-shadow/authoring-lease")
        payload = CodingAuthoringLeaseRequest.model_validate_json(request.content)
        assert payload.ticket_id == ticket_id
        message = coding_authoring_lease_signing_message(
            validator_hotkey=payload.validator_hotkey,
            ticket_id=payload.ticket_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        return httpx.Response(200, json=response_body)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        lease = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).request_coding_authoring_lease(ticket_id)
    assert lease.ticket_id == ticket_id
    assert [item.artifact_kind.value for item in lease.capabilities] == [
        "visible-bundle",
        "memory-bundle",
        "resource-profile",
    ]


async def test_coding_inference_client_offer_exchange_and_revoke_are_signed() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    grant_id = UUID("44444444-4444-4444-8444-444444444444")
    run_row_id = UUID("55555555-5555-4555-8555-555555555555")
    expires_at = datetime.now(UTC).replace(microsecond=0)
    authority = {
        "coding_contract_version": 1,
        "weight_eligible": False,
        "grant_id": str(grant_id),
        "ticket_id": str(ticket_id),
        "run_row_id": str(run_row_id),
        "case_id": "private-case-001",
        "profile_capability_id": "private-profile-001",
        "inference_grant_sha256": "11" * 32,
        "model": "openai/gpt-5.6-luna",
        "provider_api": "openrouter",
        "provider_route": "azure/eu",
        "receipt_provider": "Azure",
        "provider_route_profile": "luna-azure-eu-zdr-v1",
        "provider_account_guardrail": "openrouter_private_account_v1",
        "provider_pipeline_policy": "no_plugins_no_transforms_v1",
        "provider_cache_policy": "disabled_v1",
        "reasoning_effort": "medium",
        "request_budget": 100,
        "prompt_token_budget": 200_000,
        "completion_token_budget": 30_000,
        "cost_budget_usd_micros": 10_000_000,
        "expires_at": expires_at.isoformat(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/coding-shadow/inference-grant"):
            grant_payload = CodingInferenceGrantRequest.model_validate_json(
                request.content
            )
            assert grant_payload.ticket_id == ticket_id
            assert keypair.verify(
                coding_inference_grant_signing_message(
                    validator_hotkey=grant_payload.validator_hotkey,
                    ticket_id=grant_payload.ticket_id,
                    nonce=grant_payload.nonce,
                    requested_at=grant_payload.requested_at,
                ),
                bytes.fromhex(grant_payload.signature),
            )
            return httpx.Response(
                200,
                json={
                    "schema": "dittobench-coding-inference-grant-offer-v1",
                    **authority,
                    "status": "pending",
                    "generation": 0,
                    "exchange_url": (
                        "https://platform.test/api/v1/validator/"
                        "coding-shadow/inference-exchange"
                    ),
                },
            )
        if request.url.path.endswith("/coding-shadow/inference-exchange"):
            exchange_payload = CodingInferenceExchangeRequest.model_validate_json(
                request.content
            )
            assert keypair.verify(
                coding_inference_exchange_signing_message(
                    validator_hotkey=exchange_payload.validator_hotkey,
                    grant_id=exchange_payload.grant_id,
                    broker_public_key=exchange_payload.broker_public_key,
                    nonce=exchange_payload.nonce,
                    requested_at=exchange_payload.requested_at,
                ),
                bytes.fromhex(exchange_payload.signature),
            )
            return httpx.Response(
                200,
                json={
                    "schema": "dittobench-coding-inference-exchange-v1",
                    **authority,
                    "status": "active",
                    "generation": 1,
                    "bearer": "b" * 43,
                    "proxy_url": (
                        "https://relay.invalid/api/v1/inference/coding/chat/completions"
                    ),
                    "revoke_bearer": "r" * 43,
                    "revoke_url": (
                        "https://platform.test/api/v1/validator/"
                        "coding-shadow/inference-revoke-capability"
                    ),
                },
            )
        revoke_payload = CodingInferenceRevokeRequest.model_validate_json(
            request.content
        )
        assert keypair.verify(
            coding_inference_revoke_signing_message(
                validator_hotkey=revoke_payload.validator_hotkey,
                grant_id=revoke_payload.grant_id,
                generation=revoke_payload.generation,
                nonce=revoke_payload.nonce,
                requested_at=revoke_payload.requested_at,
            ),
            bytes.fromhex(revoke_payload.signature),
        )
        return httpx.Response(
            200,
            json={
                "schema": "dittobench-coding-inference-revocation-v1",
                "coding_contract_version": 1,
                "weight_eligible": False,
                "grant_id": str(grant_id),
                "ticket_id": str(ticket_id),
                "status": "revoked",
                "generation": 1,
                "revoked_at": datetime.now(UTC).isoformat(),
                "idempotent": False,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        platform = PlatformClient(config, http, keypair)  # type: ignore[arg-type]
        offer = await platform.request_coding_inference_grant(ticket_id)
        exchange = await platform.exchange_coding_inference_grant(
            offer,
            broker_public_key="A" * 43,
        )
        revoked = await platform.revoke_coding_inference_grant(
            grant_id=grant_id,
            generation=exchange.generation,
        )
    assert exchange.bearer == "b" * 43
    assert exchange.revoke_bearer == "r" * 43
    assert exchange.generation == 1
    assert revoked.status == "revoked"


async def test_coding_authoring_client_redacts_invalid_bearer_response() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    response_body = _authoring_response()
    response_body["capabilities"][0]["sha256"] = "ff" * 32
    ticket_id = UUID(response_body["ticket_id"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformInfrastructureError) as captured:
            await PlatformClient(
                config,  # type: ignore[arg-type]
                http,
                keypair,
            ).request_coding_authoring_lease(ticket_id)
    assert "X-Amz-Signature" not in str(captured.value)
    assert response_body["capabilities"][0]["url"] not in str(captured.value)


async def test_coding_harness_client_posts_signed_request_and_parses_capability() -> (
    None
):
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = json.loads(_SUPERVISOR_PATH.read_text(encoding="utf-8"))
    response_body = vector["requests"]["author"]["harness"]
    ticket_id = UUID(response_body["ticket_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/validator/coding-shadow/harness-launch")
        payload = CodingHarnessLaunchRequest.model_validate_json(request.content)
        message = coding_harness_launch_signing_message(
            validator_hotkey=payload.validator_hotkey,
            ticket_id=payload.ticket_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        return httpx.Response(200, json=response_body)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        launch = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).request_coding_harness_launch(ticket_id)
    assert launch.ticket_id == ticket_id
    assert launch.screened_image_sha256 == "bb" * 32
    assert "X-Amz-Signature" not in repr(launch)


async def test_coding_certification_harness_client_posts_signed_lease_request() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    lease_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    agent_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    response_body = {
        "schema": "dittobench-coding-certification-harness-launch-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "lease_id": str(lease_id),
        "agent_id": str(agent_id),
        "lease_deadline": "2026-08-30T18:20:00+00:00",
        "bench_version": 12,
        "agent_artifact_sha256": "aa" * 32,
        "screened_image_sha256": "1a" * 32,
        "screened_image_size_bytes": 1024,
        "screened_image_id": "sha256:" + "ef" * 32,
        "screened_image_ref": f"ditto-screen/{agent_id}:latest",
        "screening_policy_version": 9,
        "image_url": "https://storage.invalid/image.tar?signature=synthetic",
        "expires_at": "2026-08-30T18:05:00+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            f"/validator/coding-certification-leases/{lease_id}/harness-launch"
        )
        payload = CodingCertificationHarnessLaunchRequest.model_validate_json(
            request.content
        )
        message = coding_certification_harness_launch_signing_message(
            validator_hotkey=payload.validator_hotkey,
            lease_id=payload.lease_id,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        return httpx.Response(200, json=response_body)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        launch = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).request_coding_certification_harness_launch(lease_id)
    assert launch.lease_id == lease_id
    assert launch.weight_eligible is False
    assert "X-Amz-Signature" not in repr(launch)


async def test_coding_claim_client_signs_claim_start_and_heartbeat() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    instance_id = "coding-worker-instance-001"
    calls = 0

    def response(*, started: bool) -> dict[str, object]:
        now = datetime.now(UTC)
        return {
            "schema": "dittobench-coding-ticket-claim-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "validator_hotkey": keypair.ss58_address,
            "instance_id": instance_id,
            "claim_generation": 1,
            "claim_expires_at": (now.replace(microsecond=0)).isoformat(),
            "claim_started_at": now.isoformat() if started else None,
            "idempotent": False,
            "agent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "run_row_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "ticket_id": str(ticket_id),
            "ticket_deadline": (now.replace(microsecond=0)).isoformat(),
            "bench_version": 12,
            "coding_run_id": "coding-run-001",
            "agent_artifact_sha256": "aa" * 32,
            "screened_image_sha256": "bb" * 32,
            "run_manifest_sha256": "cc" * 32,
            "task_set_manifest_sha256": "dd" * 32,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = CodingClaimNextRequest.model_validate_json(request.content)
            message = coding_claim_next_signing_message(
                validator_hotkey=payload.validator_hotkey,
                instance_id=payload.instance_id,
                nonce=payload.nonce,
                requested_at=payload.requested_at,
            )
            started = False
        else:
            action: Literal["start", "heartbeat"] = (
                "start" if calls == 2 else "heartbeat"
            )
            payload = CodingClaimActionRequest.model_validate_json(request.content)
            message = coding_claim_action_signing_message(
                action=action,
                validator_hotkey=payload.validator_hotkey,
                instance_id=payload.instance_id,
                ticket_id=payload.ticket_id,
                claim_generation=payload.claim_generation,
                nonce=payload.nonce,
                requested_at=payload.requested_at,
            )
            assert request.url.path.endswith(f"/{action}")
            started = True
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        body = response(started=started)
        body["claim_expires_at"] = (
            datetime.now(UTC) + timedelta(minutes=2)
        ).isoformat()
        body["ticket_deadline"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json=body,
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(config, http, keypair)  # type: ignore[arg-type]
        claim = await client.claim_next_coding_ticket(instance_id)
        assert claim is not None
        started = await client.start_coding_ticket_claim(claim)
        await client.heartbeat_coding_ticket_claim(started)
    assert calls == 3


async def test_coding_claim_client_treats_no_store_404_as_empty_queue() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"Cache-Control": "no-store"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        claim = await PlatformClient(
            SimpleNamespace(
                platform_api_url="https://platform.test",
                validator_hotkey=keypair.ss58_address,
            ),  # type: ignore[arg-type]
            http,
            keypair,
        ).claim_next_coding_ticket("coding-worker-instance-001")
    assert claim is None


async def test_coding_evidence_client_signs_capability_and_finalization() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    now = datetime.now(UTC).replace(microsecond=0)
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    upload_id = UUID("55555555-5555-4555-8555-555555555555")
    claim = CodingClaimResponse.model_validate(
        {
            "schema": "dittobench-coding-ticket-claim-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "validator_hotkey": keypair.ss58_address,
            "instance_id": "coding-worker-instance-001",
            "claim_generation": 7,
            "claim_expires_at": now + timedelta(minutes=2),
            "claim_started_at": now,
            "idempotent": False,
            "agent_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "run_row_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "ticket_id": ticket_id,
            "ticket_deadline": now + timedelta(hours=1),
            "bench_version": 12,
            "coding_run_id": "coding-run-001",
            "agent_artifact_sha256": "aa" * 32,
            "screened_image_sha256": "bb" * 32,
            "run_manifest_sha256": "cc" * 32,
            "task_set_manifest_sha256": "dd" * 32,
        }
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            upload_payload = (
                CodingSealedEvidenceUploadCapabilityRequest.model_validate_json(
                    request.content
                )
            )
            message = coding_sealed_evidence_upload_signing_message(
                validator_hotkey=upload_payload.validator_hotkey,
                instance_id=upload_payload.instance_id,
                ticket_id=upload_payload.ticket_id,
                claim_generation=upload_payload.claim_generation,
                evidence_kind=upload_payload.evidence_kind,
                sha256=upload_payload.sha256,
                size_bytes=upload_payload.size_bytes,
                nonce=upload_payload.nonce,
                requested_at=upload_payload.requested_at,
            )
            assert keypair.verify(message, bytes.fromhex(upload_payload.signature))
            body = {
                "schema": "dittobench-coding-sealed-evidence-upload-capability-v1",
                "coding_contract_version": 1,
                "weight_eligible": False,
                "ticket_id": str(ticket_id),
                "claim_generation": 7,
                "ticket_deadline": claim.ticket_deadline.isoformat(),
                "upload_id": str(upload_id),
                "evidence_kind": "authoring-transcript",
                "sha256": "ab" * 32,
                "size_bytes": 4096,
                "content_type": "application/octet-stream",
                "checksum_sha256_b64": "q6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6s=",
                "url": (
                    "https://evidence.invalid/coding-evidence/v1/authoring-transcript/"
                    f"sha256/{'ab' * 32}?X-Amz-Date={now.strftime('%Y%m%dT%H%M%SZ')}"
                    "&X-Amz-Expires=120&X-Amz-Signature=synthetic"
                ),
                "expires_at": (now + timedelta(minutes=2)).isoformat(),
            }
        else:
            finalize_payload = CodingSealedEvidenceFinalizeRequest.model_validate_json(
                request.content
            )
            message = coding_sealed_evidence_finalize_signing_message(
                validator_hotkey=finalize_payload.validator_hotkey,
                instance_id=finalize_payload.instance_id,
                ticket_id=finalize_payload.ticket_id,
                claim_generation=finalize_payload.claim_generation,
                upload_id=finalize_payload.upload_id,
                evidence_kind=finalize_payload.evidence_kind,
                sha256=finalize_payload.sha256,
                size_bytes=finalize_payload.size_bytes,
                nonce=finalize_payload.nonce,
                requested_at=finalize_payload.requested_at,
            )
            assert keypair.verify(message, bytes.fromhex(finalize_payload.signature))
            body = {
                "schema": "dittobench-coding-sealed-evidence-finalized-v1",
                "coding_contract_version": 1,
                "weight_eligible": False,
                "ticket_id": str(ticket_id),
                "claim_generation": 7,
                "upload_id": str(upload_id),
                "evidence_kind": "authoring-transcript",
                "sha256": "ab" * 32,
                "size_bytes": 4096,
                "finalized_at": datetime.now(UTC).isoformat(),
                "accepted": True,
                "idempotent": False,
            }
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json=body,
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        platform = PlatformClient(config, http, keypair)  # type: ignore[arg-type]
        capability = await platform.request_coding_evidence_upload_capability(
            claim,
            evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
            sha256="ab" * 32,
            size_bytes=4096,
        )
        finalized = await platform.finalize_coding_evidence_upload(
            claim,
            capability,
        )
    assert calls == 2
    assert finalized.upload_id == upload_id
    assert finalized.weight_eligible is False


async def test_coding_evidence_client_replays_finalized_terminal_receipt() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    upload_id = UUID("55555555-5555-4555-8555-555555555555")
    sha256 = "ab" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        payload = CodingSealedEvidenceFinalizeRequest.model_validate_json(
            request.content
        )
        message = coding_sealed_evidence_finalize_signing_message(
            validator_hotkey=payload.validator_hotkey,
            instance_id=payload.instance_id,
            ticket_id=payload.ticket_id,
            claim_generation=payload.claim_generation,
            upload_id=payload.upload_id,
            evidence_kind=payload.evidence_kind,
            sha256=payload.sha256,
            size_bytes=payload.size_bytes,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        assert payload.instance_id == "coding-worker-instance-001"
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json={
                "schema": "dittobench-coding-sealed-evidence-finalized-v1",
                "coding_contract_version": 1,
                "weight_eligible": False,
                "ticket_id": str(ticket_id),
                "claim_generation": 7,
                "upload_id": str(upload_id),
                "evidence_kind": "terminal-publication-acknowledgement",
                "sha256": sha256,
                "size_bytes": 4096,
                "finalized_at": datetime.now(UTC).isoformat(),
                "accepted": True,
                "idempotent": True,
            },
        )

    pending = PendingRelease(
        record_id="11" * 32,
        ticket_id=ticket_id,
        terminal_evidence_sha256="cd" * 32,
        reservation=ReleaseReservation(
            ticket_id=ticket_id,
            claim_generation=7,
            upload_id=upload_id,
            evidence_kind="terminal-publication-acknowledgement",
            sha256=sha256,
            size_bytes=4096,
        ),
    )
    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        finalized = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).replay_coding_evidence_finalization(
            pending,
            instance_id="coding-worker-instance-001",
        )
    assert finalized is not None
    assert finalized.upload_id == upload_id
    assert finalized.idempotent is True


async def test_coding_evidence_client_reports_missing_terminal_receipt() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    pending = PendingRelease(
        record_id="11" * 32,
        ticket_id=ticket_id,
        terminal_evidence_sha256="cd" * 32,
        reservation=ReleaseReservation(
            ticket_id=ticket_id,
            claim_generation=7,
            upload_id=UUID("55555555-5555-4555-8555-555555555555"),
            evidence_kind="terminal-publication-acknowledgement",
            sha256="ab" * 32,
            size_bytes=4096,
        ),
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            headers={"Cache-Control": "no-store"},
            json={"detail": "finalization not found"},
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        finalized = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).replay_coding_evidence_finalization(
            pending,
            instance_id="coding-worker-instance-001",
        )
    assert finalized is None


async def test_coding_authoring_client_rejects_oversized_response() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    seen_chunks = 0

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal seen_chunks
            for _ in range(33):
                seen_chunks += 1
                yield b"x" * (64 << 10)
            raise AssertionError("validator consumed data after the response bound")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=OversizedStream())

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformInfrastructureError, match="size"):
            await PlatformClient(
                config,  # type: ignore[arg-type]
                http,
                keypair,
            ).request_coding_authoring_lease(ticket_id)
    assert seen_chunks == 33


async def test_coding_authoring_client_rejects_ticket_identity_drift() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    response_body = _authoring_response()
    requested_ticket_id = UUID(response_body["ticket_id"])
    different_ticket_id = UUID("99999999-9999-4999-8999-999999999999")
    response_body["ticket_id"] = str(different_ticket_id)
    for capability in response_body["capabilities"]:
        capability["ticket_id"] = str(different_ticket_id)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformInfrastructureError, match="identity"):
            await PlatformClient(
                config,  # type: ignore[arg-type]
                http,
                keypair,
            ).request_coding_authoring_lease(requested_ticket_id)


async def test_coding_authoring_client_never_follows_redirects() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    ticket_id = UUID("33333333-3333-4333-8333-333333333333")
    observed_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_paths.append(request.url.path)
        if request.url.host == "platform.test":
            return httpx.Response(
                307,
                headers={"Location": "https://redirect.invalid/capture"},
            )
        raise AssertionError("signed authoring request followed a redirect")

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as http:
        with pytest.raises(PlatformError, match="307"):
            await PlatformClient(
                config,  # type: ignore[arg-type]
                http,
                keypair,
            ).request_coding_authoring_lease(ticket_id)
    assert observed_paths == ["/api/v1/validator/coding-shadow/authoring-lease"]


async def test_coding_grading_client_posts_signed_request_and_parses_lease() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _grading_vector()
    raw_request = vector["request"]
    response_body = vector["response"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/validator/coding-shadow/grading-lease")
        payload = CodingGradingLeaseRequest.model_validate_json(request.content)
        message = coding_grading_lease_signing_message(
            validator_hotkey=payload.validator_hotkey,
            agent_id=payload.agent_id,
            run_row_id=payload.run_row_id,
            ticket_id=payload.ticket_id,
            freeze_id=payload.freeze_id,
            claim_instance_id=payload.claim_instance_id,
            authoring_evidence_sha256=payload.authoring_evidence_sha256,
            nonce=payload.nonce,
            requested_at=payload.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        return httpx.Response(200, json=response_body)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        lease = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).request_coding_grading_lease(
            agent_id=UUID(raw_request["agent_id"]),
            run_row_id=UUID(raw_request["run_row_id"]),
            ticket_id=UUID(raw_request["ticket_id"]),
            freeze_id=UUID(raw_request["freeze_id"]),
            claim_instance_id=raw_request["claim_instance_id"],
            authoring_evidence_sha256=raw_request["authoring_evidence_sha256"],
            expected_frozen_patch_sha256=response_body["frozen_patch_sha256"],
        )
    assert [item.artifact_kind.value for item in lease.capabilities] == [
        "visible-bundle",
        "resource-profile",
        "grader-bundle",
    ]
    assert "memory-bundle" not in lease.model_dump_json()


async def test_coding_grading_client_bounds_and_refuses_redirects() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _grading_vector()
    raw = vector["request"]

    async def request_lease(http: httpx.AsyncClient) -> None:
        await PlatformClient(
            SimpleNamespace(
                platform_api_url="https://platform.test",
                validator_hotkey=keypair.ss58_address,
            ),  # type: ignore[arg-type]
            http,
            keypair,
        ).request_coding_grading_lease(
            agent_id=UUID(raw["agent_id"]),
            run_row_id=UUID(raw["run_row_id"]),
            ticket_id=UUID(raw["ticket_id"]),
            freeze_id=UUID(raw["freeze_id"]),
            claim_instance_id=raw["claim_instance_id"],
            authoring_evidence_sha256=raw["authoring_evidence_sha256"],
            expected_frozen_patch_sha256=vector["response"]["frozen_patch_sha256"],
        )

    observed: list[str] = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "https://redirect.invalid/capture"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect_handler),
        follow_redirects=True,
    ) as http:
        with pytest.raises(PlatformError, match="307"):
            await request_lease(http)
    assert len(observed) == 1

    seen_chunks = 0

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal seen_chunks
            for _ in range(33):
                seen_chunks += 1
                yield b"x" * (64 << 10)
            raise AssertionError("validator consumed data after the response bound")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=OversizedStream())
        )
    ) as http:
        with pytest.raises(PlatformInfrastructureError, match="size"):
            await request_lease(http)
    assert seen_chunks == 33


async def test_coding_grading_client_rejects_identity_drift_without_url_leak() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _grading_vector()
    raw = vector["request"]
    response_body = vector["response"]
    response_body["freeze_id"] = "99999999-9999-4999-8999-999999999999"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response_body)
        )
    ) as http:
        with pytest.raises(PlatformInfrastructureError, match="identity") as captured:
            await PlatformClient(
                SimpleNamespace(
                    platform_api_url="https://platform.test",
                    validator_hotkey=keypair.ss58_address,
                ),  # type: ignore[arg-type]
                http,
                keypair,
            ).request_coding_grading_lease(
                agent_id=UUID(raw["agent_id"]),
                run_row_id=UUID(raw["run_row_id"]),
                ticket_id=UUID(raw["ticket_id"]),
                freeze_id=UUID(raw["freeze_id"]),
                claim_instance_id=raw["claim_instance_id"],
                authoring_evidence_sha256=raw["authoring_evidence_sha256"],
                expected_frozen_patch_sha256=vector["response"]["frozen_patch_sha256"],
            )
    assert "X-Amz-Signature" not in str(captured.value)


async def test_coding_grading_client_rejects_frozen_patch_drift() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _grading_vector()
    raw = vector["request"]
    response_body = vector["response"]
    expected_patch_sha256 = response_body["frozen_patch_sha256"]
    response_body["frozen_patch_sha256"] = "cc" * 32
    response_body["frozen_submission_object_key"] = "sha256/" + "cc" * 32

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response_body)
        )
    ) as http:
        with pytest.raises(PlatformInfrastructureError, match="identity"):
            await PlatformClient(
                SimpleNamespace(
                    platform_api_url="https://platform.test",
                    validator_hotkey=keypair.ss58_address,
                ),  # type: ignore[arg-type]
                http,
                keypair,
            ).request_coding_grading_lease(
                agent_id=UUID(raw["agent_id"]),
                run_row_id=UUID(raw["run_row_id"]),
                ticket_id=UUID(raw["ticket_id"]),
                freeze_id=UUID(raw["freeze_id"]),
                claim_instance_id=raw["claim_instance_id"],
                authoring_evidence_sha256=raw["authoring_evidence_sha256"],
                expected_frozen_patch_sha256=expected_patch_sha256,
            )


async def test_coding_shadow_result_client_posts_signed_evidence() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _shadow_result_vector()
    raw = vector["request"]
    manifest, task_evidence = _shadow_result_authority(vector)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            f"/validator/agent/{vector['agent_id']}/coding-shadow-result"
        )
        payload = SubmitCodingShadowResultRequest.model_validate_json(request.content)
        message = coding_shadow_result_signing_message(
            validator_hotkey=payload.validator_hotkey,
            agent_id=UUID(vector["agent_id"]),
            run_row_id=payload.run_row_id,
            ticket_id=payload.ticket_id,
            bench_version=payload.bench_version,
            ticket_deadline=payload.ticket_deadline,
            agent_artifact_sha256=payload.agent_artifact_sha256,
            screened_image_sha256=payload.screened_image_sha256,
            run_evidence_sha256=payload.run_evidence_sha256,
        )
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        return httpx.Response(200, json=vector["response"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        accepted = await PlatformClient(
            SimpleNamespace(
                platform_api_url="https://platform.test",
                validator_hotkey=keypair.ss58_address,
            ),  # type: ignore[arg-type]
            http,
            keypair,
        ).submit_coding_shadow_result(
            UUID(vector["agent_id"]),
            bench_version=raw["bench_version"],
            run_row_id=UUID(raw["run_row_id"]),
            ticket_id=UUID(raw["ticket_id"]),
            ticket_deadline=datetime.fromisoformat(raw["ticket_deadline"]),
            agent_artifact_sha256=raw["agent_artifact_sha256"],
            screened_image_sha256=raw["screened_image_sha256"],
            run_manifest=manifest,
            evidence=SubmitCodingShadowResultRequest.model_validate_json(
                json.dumps(raw)
            ).evidence,
            task_evidence=task_evidence,
        )
    assert accepted.accepted is True
    assert accepted.weight_eligible is False


async def test_prepared_coding_publication_sends_the_exact_durable_bytes() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _shadow_result_vector()
    raw = vector["request"]
    manifest, task_evidence = _shadow_result_authority(vector)
    observed = b""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed
        observed = request.content
        return httpx.Response(
            200,
            headers={"Cache-Control": "no-store"},
            json=vector["response"],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlatformClient(
            SimpleNamespace(
                platform_api_url="https://platform.test",
                validator_hotkey=keypair.ss58_address,
            ),  # type: ignore[arg-type]
            http,
            keypair,
        )
        prepared = client.prepare_coding_shadow_result(
            UUID(vector["agent_id"]),
            bench_version=raw["bench_version"],
            run_row_id=UUID(raw["run_row_id"]),
            ticket_id=UUID(raw["ticket_id"]),
            ticket_deadline=datetime.fromisoformat(raw["ticket_deadline"]),
            agent_artifact_sha256=raw["agent_artifact_sha256"],
            screened_image_sha256=raw["screened_image_sha256"],
            run_manifest=manifest,
            evidence=SubmitCodingShadowResultRequest.model_validate_json(
                json.dumps(raw)
            ).evidence,
            task_evidence=task_evidence,
        )
        accepted, acknowledgement = await client.publish_prepared_coding_publication(
            prepared
        )
    assert observed == prepared.body
    assert json.loads(acknowledgement) == vector["response"]
    assert accepted.ticket_id == prepared.ticket_id


async def test_coding_shadow_result_client_bounds_and_refuses_redirects() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _shadow_result_vector()
    raw = SubmitCodingShadowResultRequest.model_validate_json(
        json.dumps(vector["request"])
    )
    manifest, task_evidence = _shadow_result_authority(vector)

    async def submit(http: httpx.AsyncClient) -> None:
        await PlatformClient(
            SimpleNamespace(
                platform_api_url="https://platform.test",
                validator_hotkey=keypair.ss58_address,
            ),  # type: ignore[arg-type]
            http,
            keypair,
        ).submit_coding_shadow_result(
            UUID(vector["agent_id"]),
            bench_version=raw.bench_version,
            run_row_id=raw.run_row_id,
            ticket_id=raw.ticket_id,
            ticket_deadline=raw.ticket_deadline,
            agent_artifact_sha256=raw.agent_artifact_sha256,
            screened_image_sha256=raw.screened_image_sha256,
            run_manifest=manifest,
            evidence=raw.evidence,
            task_evidence=task_evidence,
        )

    observed: list[str] = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        observed.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "https://redirect.invalid/capture"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect_handler),
        follow_redirects=True,
    ) as http:
        with pytest.raises(PlatformError, match="307"):
            await submit(http)
    assert len(observed) == 1

    seen_chunks = 0

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal seen_chunks
            for _ in range(5):
                seen_chunks += 1
                yield b"x" * (16 << 10)
            raise AssertionError("validator consumed data after the response bound")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=OversizedStream())
        )
    ) as http:
        with pytest.raises(PlatformInfrastructureError, match="size"):
            await submit(http)
    assert seen_chunks == 5


async def test_coding_shadow_result_client_rejects_response_identity_drift() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _shadow_result_vector()
    raw = SubmitCodingShadowResultRequest.model_validate_json(
        json.dumps(vector["request"])
    )
    manifest, task_evidence = _shadow_result_authority(vector)
    response = {**vector["response"], "coding_run_id": "different-run"}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=response)
        )
    ) as http:
        with pytest.raises(PlatformInfrastructureError, match="identity"):
            await PlatformClient(
                SimpleNamespace(
                    platform_api_url="https://platform.test",
                    validator_hotkey=keypair.ss58_address,
                ),  # type: ignore[arg-type]
                http,
                keypair,
            ).submit_coding_shadow_result(
                UUID(vector["agent_id"]),
                bench_version=raw.bench_version,
                run_row_id=raw.run_row_id,
                ticket_id=raw.ticket_id,
                ticket_deadline=raw.ticket_deadline,
                agent_artifact_sha256=raw.agent_artifact_sha256,
                screened_image_sha256=raw.screened_image_sha256,
                run_manifest=manifest,
                evidence=raw.evidence,
                task_evidence=task_evidence,
            )


async def test_coding_shadow_result_client_rejects_local_authority_before_http() -> (
    None
):
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = _shadow_result_vector()
    raw = SubmitCodingShadowResultRequest.model_validate_json(
        json.dumps(vector["request"])
    )
    manifest, task_evidence = _shadow_result_authority(vector)
    drifted_manifest = manifest.model_copy(update={"agent_artifact_sha256": "ff" * 32})

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid local evidence reached Platform")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformInfrastructureError, match="local authority"):
            await PlatformClient(
                SimpleNamespace(
                    platform_api_url="https://platform.test",
                    validator_hotkey=keypair.ss58_address,
                ),  # type: ignore[arg-type]
                http,
                keypair,
            ).submit_coding_shadow_result(
                UUID(vector["agent_id"]),
                bench_version=raw.bench_version,
                run_row_id=raw.run_row_id,
                ticket_id=raw.ticket_id,
                ticket_deadline=raw.ticket_deadline,
                agent_artifact_sha256=raw.agent_artifact_sha256,
                screened_image_sha256=raw.screened_image_sha256,
                run_manifest=drifted_manifest,
                evidence=raw.evidence,
                task_evidence=task_evidence,
            )


async def test_coding_authoring_freeze_client_posts_signed_evidence() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = json.loads(_AUTHORING_FREEZE_VECTOR_PATH.read_text(encoding="utf-8"))
    raw = vector["request"]
    response_body = vector["response"]
    agent_id = UUID(vector["agent_id"])
    evidence = CodingAuthoringEvidence.model_validate_json(json.dumps(raw["evidence"]))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/validator/coding-shadow/authoring-freeze")
        payload = SubmitCodingAuthoringFreezeRequest.model_validate_json(
            request.content
        )
        message = coding_authoring_freeze_signing_message(
            validator_hotkey=payload.validator_hotkey,
            agent_id=agent_id,
            bench_version=payload.bench_version,
            run_row_id=payload.run_row_id,
            ticket_id=payload.ticket_id,
            ticket_deadline=payload.ticket_deadline,
            coding_run_id=payload.coding_run_id,
            agent_artifact_sha256=payload.agent_artifact_sha256,
            screened_image_sha256=payload.screened_image_sha256,
            run_manifest_sha256=payload.run_manifest_sha256,
            task_set_manifest_sha256=payload.task_set_manifest_sha256,
            authoring_evidence_sha256=payload.authoring_evidence_sha256,
            authoring_transcript_object_key=(payload.authoring_transcript_object_key),
            authoring_transcript_bytes=payload.authoring_transcript_bytes,
            authoring_event_count=payload.authoring_event_count,
            frozen_submission_object_key=payload.frozen_submission_object_key,
        )
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        return httpx.Response(200, json=response_body)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        accepted = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).submit_coding_authoring_freeze(
            agent_id,
            bench_version=raw["bench_version"],
            run_row_id=UUID(raw["run_row_id"]),
            ticket_id=UUID(raw["ticket_id"]),
            ticket_deadline=datetime.fromisoformat(raw["ticket_deadline"]),
            coding_run_id=raw["coding_run_id"],
            agent_artifact_sha256=raw["agent_artifact_sha256"],
            screened_image_sha256=raw["screened_image_sha256"],
            run_manifest_sha256=raw["run_manifest_sha256"],
            task_set_manifest_sha256=raw["task_set_manifest_sha256"],
            evidence=evidence,
            authoring_transcript_object_key=raw["authoring_transcript_object_key"],
            authoring_transcript_bytes=raw["authoring_transcript_bytes"],
            authoring_event_count=raw["authoring_event_count"],
            frozen_submission_object_key=raw["frozen_submission_object_key"],
        )
    assert accepted.ticket_id == UUID(raw["ticket_id"])
    assert accepted.authoring_evidence_sha256 == raw["authoring_evidence_sha256"]


async def test_coding_authoring_freeze_client_bounds_and_refuses_redirects() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    vector = json.loads(_AUTHORING_FREEZE_VECTOR_PATH.read_text(encoding="utf-8"))
    raw = vector["request"]
    agent_id = UUID(vector["agent_id"])
    evidence = CodingAuthoringEvidence.model_validate_json(json.dumps(raw["evidence"]))

    async def submit(http: httpx.AsyncClient) -> None:
        await PlatformClient(
            SimpleNamespace(
                platform_api_url="https://platform.test",
                validator_hotkey=keypair.ss58_address,
            ),  # type: ignore[arg-type]
            http,
            keypair,
        ).submit_coding_authoring_freeze(
            agent_id,
            bench_version=raw["bench_version"],
            run_row_id=UUID(raw["run_row_id"]),
            ticket_id=UUID(raw["ticket_id"]),
            ticket_deadline=datetime.fromisoformat(raw["ticket_deadline"]),
            coding_run_id=raw["coding_run_id"],
            agent_artifact_sha256=raw["agent_artifact_sha256"],
            screened_image_sha256=raw["screened_image_sha256"],
            run_manifest_sha256=raw["run_manifest_sha256"],
            task_set_manifest_sha256=raw["task_set_manifest_sha256"],
            evidence=evidence,
            authoring_transcript_object_key=raw["authoring_transcript_object_key"],
            authoring_transcript_bytes=raw["authoring_transcript_bytes"],
            authoring_event_count=raw["authoring_event_count"],
            frozen_submission_object_key=raw["frozen_submission_object_key"],
        )

    redirected: list[str] = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        redirected.append(str(request.url))
        return httpx.Response(
            307,
            headers={"Location": "https://redirect.invalid/capture"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect_handler),
        follow_redirects=True,
    ) as http:
        with pytest.raises(PlatformError, match="307"):
            await submit(http)
    assert len(redirected) == 1

    seen_chunks = 0

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal seen_chunks
            for _ in range(5):
                seen_chunks += 1
                yield b"x" * (16 << 10)
            raise AssertionError("validator consumed data after the response bound")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=OversizedStream())
        )
    ) as http:
        with pytest.raises(PlatformInfrastructureError, match="size"):
            await submit(http)
    assert seen_chunks == 5


async def test_coding_certification_client_posts_exact_signed_envelope() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("11111111-1111-4111-8111-111111111111")
    vector = json.loads(
        (
            Path(__file__).parents[3]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_certification_v1.json"
        ).read_text(encoding="utf-8")
    )
    receipt = CodingCapabilityCertificationReceipt.model_validate_json(
        json.dumps(vector["receipt"])
    )
    expected = vector["expected"]
    lease_id = UUID(expected["lease_id"])
    signature = sign_coding_certification(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=agent_id,
        bench_version=expected["bench_version"],
        lease_id=lease_id,
        screened_image_sha256=expected["screened_image_sha256"],
        receipt=receipt,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/agent/{agent_id}/coding-certification")
        payload = SubmitCodingCertificationRequest.model_validate_json(request.content)
        message = coding_certification_signing_message(
            validator_hotkey=payload.validator_hotkey,
            agent_id=agent_id,
            bench_version=payload.bench_version,
            lease_id=payload.lease_id,
            screened_image_sha256=payload.screened_image_sha256,
            certification_sha256=payload.receipt.certification_sha256,
        )
        assert keypair.verify(message, bytes.fromhex(payload.signature))
        return httpx.Response(
            200,
            json={
                "agent_id": str(agent_id),
                "certification_id": receipt.certification_id,
                "status": receipt.status.value,
                "accepted": True,
                "idempotent": False,
                "active": True,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).submit_coding_certification(
            agent_id,
            bench_version=expected["bench_version"],
            lease_id=lease_id,
            screened_image_sha256=expected["screened_image_sha256"],
            receipt=receipt,
            signature=signature,
        )
    assert response.accepted is True


async def test_coding_certification_lease_issue_treats_404_as_ineligible() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/coding-certification-leases")
        return httpx.Response(404, headers={"Cache-Control": "no-store"})

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).issue_coding_certification_lease(agent_id, bench_version=12)
    assert result is None


async def test_coding_certification_lease_issue_parks_503_as_infrastructure() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/coding-certification-leases")
        return httpx.Response(503, headers={"Cache-Control": "no-store"})

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformInfrastructureError, match="unavailable"):
            await PlatformClient(
                config,  # type: ignore[arg-type]
                http,
                keypair,
            ).issue_coding_certification_lease(agent_id, bench_version=12)


def _coding_certification_submit_args() -> tuple[
    UUID, int, UUID, str, CodingCapabilityCertificationReceipt, str
]:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("11111111-1111-4111-8111-111111111111")
    vector = json.loads(
        (
            Path(__file__).parents[3]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_certification_v1.json"
        ).read_text(encoding="utf-8")
    )
    receipt = CodingCapabilityCertificationReceipt.model_validate_json(
        json.dumps(vector["receipt"])
    )
    expected = vector["expected"]
    lease_id = UUID(expected["lease_id"])
    signature = sign_coding_certification(
        keypair,
        validator_hotkey=keypair.ss58_address,
        agent_id=agent_id,
        bench_version=expected["bench_version"],
        lease_id=lease_id,
        screened_image_sha256=expected["screened_image_sha256"],
        receipt=receipt,
    )
    return (
        agent_id,
        expected["bench_version"],
        lease_id,
        expected["screened_image_sha256"],
        receipt,
        signature,
    )


async def test_submit_coding_certification_parks_502_as_infrastructure() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, bench_version, lease_id, image, receipt, signature = (
        _coding_certification_submit_args()
    )
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(502, text="")

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(
            PlatformInfrastructureError,
            match="coding certification rejected \\(502\\)",
        ):
            await PlatformClient(
                cast(Any, config), http, keypair
            ).submit_coding_certification(
                agent_id,
                bench_version=bench_version,
                lease_id=lease_id,
                screened_image_sha256=image,
                receipt=receipt,
                signature=signature,
            )
    assert attempts == 1


async def test_submit_coding_certification_4xx_stays_a_client_error() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id, bench_version, lease_id, image, receipt, signature = (
        _coding_certification_submit_args()
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "artifact mismatch"})

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(
            PlatformError, match="coding certification rejected \\(409\\)"
        ) as caught:
            await PlatformClient(
                cast(Any, config), http, keypair
            ).submit_coding_certification(
                agent_id,
                bench_version=bench_version,
                lease_id=lease_id,
                screened_image_sha256=image,
                receipt=receipt,
                signature=signature,
            )
    assert type(caught.value) is PlatformError


async def test_artifact_request_is_fresh_agent_bound_and_signed() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")

    def handler(request: httpx.Request) -> httpx.Response:
        nonce = UUID(request.headers["X-Validator-Artifact-Nonce"])
        requested_at = datetime.fromisoformat(
            request.headers["X-Validator-Artifact-Requested-At"]
        )
        message = artifact_signing_message(
            validator_hotkey=keypair.ss58_address,
            agent_id=agent_id,
            nonce=nonce,
            requested_at=requested_at,
        )
        assert keypair.verify(
            message,
            bytes.fromhex(request.headers["X-Validator-Artifact-Signature"]),
        )
        return httpx.Response(
            200,
            json={
                "agent_id": str(agent_id),
                "sha256": "ab" * 32,
                "download_url": "https://storage.test/artifact",
                "expires_at": datetime.now(UTC).isoformat(),
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).get_artifact(agent_id)

    assert response.agent_id == agent_id


async def test_report_ticket_failed_is_fresh_lease_bound_and_signed() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    deadline = datetime(2026, 7, 14, 12, 30, tzinfo=UTC)
    job = JobResponse(
        agent_id=agent_id,
        miner_hotkey="5MinerA" + "x" * 41,
        sha256="ab" * 32,
        deadline=deadline,
        seed=12345,
        dataset_sha256="cd" * 32,
        run_size="full",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/validator/job/fail")
        fail = FailJobRequest.model_validate(json.loads(request.content))
        assert fail.validator_hotkey == keypair.ss58_address
        assert fail.agent_id == agent_id
        assert fail.ticket_deadline == deadline
        assert fail.reason == "infrastructure"
        message = job_fail_signing_message(
            validator_hotkey=fail.validator_hotkey,
            agent_id=fail.agent_id,
            ticket_deadline=fail.ticket_deadline,
            nonce=fail.nonce,
            requested_at=fail.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(fail.signature))
        return httpx.Response(200, json={"agent_id": str(agent_id), "reopened": True})

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).report_ticket_failed(job, "infrastructure")

    assert response.agent_id == agent_id
    assert response.reopened is True


async def test_report_ticket_failed_carries_the_scorer_failure_code() -> None:
    # ditto-subnet#279 deliverable (1). `reason` is a three-value class chosen to
    # drive the platform's reissue policy, so it says how the platform should
    # respond and nothing about what happened. The validator knows which of the
    # five `_SANDBOX_INFRASTRUCTURE_CODES` fired; before this it threw the code
    # away and left only a host log line, which is why the ~60-minute `mnemo*`
    # killer was never identified. Now it reaches the ticket.
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    job = JobResponse(
        agent_id=agent_id,
        miner_hotkey="5MinerA" + "x" * 41,
        sha256="ab" * 32,
        deadline=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        seed=1,
        dataset_sha256="cd" * 32,
        run_size="full",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["failure_detail"] == "sandbox_network_unavailable"
        fail = FailJobRequest.model_validate(body)
        assert fail.failure_detail == "sandbox_network_unavailable"
        # Advisory, so it is deliberately outside the signed payload, exactly as
        # `reason` is. Signing it would have made an additive field a protocol
        # break with no security gained.
        message = job_fail_signing_message(
            validator_hotkey=fail.validator_hotkey,
            agent_id=fail.agent_id,
            ticket_deadline=fail.ticket_deadline,
            nonce=fail.nonce,
            requested_at=fail.requested_at,
        )
        assert keypair.verify(message, bytes.fromhex(fail.signature))
        return httpx.Response(200, json={"agent_id": str(agent_id), "reopened": True})

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).report_ticket_failed(job, "infrastructure", "sandbox_network_unavailable")

    assert response.reopened is True


async def test_report_without_a_detail_sends_no_new_key() -> None:
    # Backward compatibility from the sending side. A report with no detail must
    # be byte-identical to what this client sent before the field existed, so a
    # platform predating it sees no new key at all and cannot 422 the hand-back.
    # A rejected hand-back leaves the lease to expire silently, which is exactly
    # the ambiguity this whole change exists to remove.
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    job = JobResponse(
        agent_id=agent_id,
        miner_hotkey="5MinerA" + "x" * 41,
        sha256="ab" * 32,
        deadline=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        seed=1,
        dataset_sha256="cd" * 32,
        run_size="full",
    )
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"agent_id": str(agent_id), "reopened": True})

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).report_ticket_failed(job, "scoring_error")

    assert "failure_detail" not in seen[0]
    assert set(seen[0]) == {
        "validator_hotkey",
        "agent_id",
        "ticket_deadline",
        "reason",
        "nonce",
        "requested_at",
        "signature",
    }


def _fail_job() -> JobResponse:
    return JobResponse(
        agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
        miner_hotkey="5MinerA" + "x" * 41,
        sha256="ab" * 32,
        deadline=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        seed=1,
        dataset_sha256="cd" * 32,
        run_size="full",
    )


async def test_a_long_detail_reaches_an_upgraded_platform_whole() -> None:
    # The forward case, and the point of the whole change: a validator carrying
    # the widened bound talking to a platform that has it too. The real
    # 2026-07-27 message, which the old 200-char cap cut at "inference r", must
    # arrive character for character.
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    job = _fail_job()
    detail = (
        "DittobenchError: run 2b7c6b6c-ae45-493d-b8f5-b1a4a6ff8b3a failed: "
        "harness exhausted its inference allowance: agent-attributable "
        "inference decline: the platform rejected 81 of the harness's "
        "inference request(s) outright, before reserving any capacity"
    )
    assert len(detail) > LEGACY_FAILURE_DETAIL_MAX_LENGTH
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["failure_detail"])
        # Validated through the real wire model, so the length bound is the
        # actual one and not the test's opinion of it.
        assert FailJobRequest.model_validate(body).failure_detail == detail
        return httpx.Response(
            200, json={"agent_id": str(job.agent_id), "reopened": True}
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).report_ticket_failed(job, "infrastructure", detail)

    assert response.reopened is True
    # One request. No retry fires when nothing rejects the detail.
    assert seen == [detail]
    assert seen[0].endswith("before reserving any capacity")


async def test_a_long_detail_is_not_replayed_on_legacy_422() -> None:
    # A rolling old Platform may reject the widened detail. Replaying a mutated
    # failure report would still be an automatic retry, so the validator parks
    # locally and lets the lease expire with the original request as evidence.
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    job = _fail_job()
    detail = "D" * (LEGACY_FAILURE_DETAIL_MAX_LENGTH * 3)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(422, text="string_too_long")

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError):
            await PlatformClient(
                config,  # type: ignore[arg-type]
                http,
                keypair,
            ).report_ticket_failed(job, "infrastructure", detail)

    assert len(seen) == 1
    assert len(seen[0]["failure_detail"]) > LEGACY_FAILURE_DETAIL_MAX_LENGTH


async def test_a_short_detail_is_not_retried_on_422() -> None:
    # The retry is a targeted skew workaround, not a general one. A 422 on a
    # detail already inside the legacy bound cannot be about length, so
    # re-sending it would burn a round trip to get the same answer. It stays a
    # single attempt and a typed error, exactly as before this change.
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    job = _fail_job()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(422, text="some other validation failure")

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError):
            await PlatformClient(
                config,  # type: ignore[arg-type]
                http,
                keypair,
            ).report_ticket_failed(job, "scoring_error", "sandbox_oom")

    assert attempts == 1


async def test_a_detail_at_the_widened_cap_is_sent_unmodified() -> None:
    # The boundary. Exactly at the cap is legal and must not be trimmed,
    # re-marked, or otherwise touched on the way out.
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    job = _fail_job()
    detail = "q" * FAILURE_DETAIL_MAX_LENGTH
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["failure_detail"])
        return httpx.Response(
            200, json={"agent_id": str(job.agent_id), "reopened": True}
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).report_ticket_failed(job, "scoring_error", detail)

    assert seen == [detail]
    assert len(seen[0]) == FAILURE_DETAIL_MAX_LENGTH


async def test_report_ticket_failed_raises_typed_error_on_rejection() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    job = JobResponse(
        agent_id=UUID("550e8400-e29b-41d4-a716-446655440000"),
        miner_hotkey="5MinerA" + "x" * 41,
        sha256="ab" * 32,
        deadline=datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
        seed=1,
        dataset_sha256="cd" * 32,
        run_size="full",
    )

    def handler(_: httpx.Request) -> httpx.Response:
        # An old platform without the endpoint answers 404; the client surfaces a
        # typed PlatformError that the worker treats as best-effort.
        return httpx.Response(404, text="not found")

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError):
            await PlatformClient(
                config,  # type: ignore[arg-type]
                http,
                keypair,
            ).report_ticket_failed(job, "scoring_error")


@pytest.mark.parametrize(
    "invalid_image_fields",
    [
        {"screened_image_url": "https://storage.test/image.tar"},
        {
            "screened_image_url": "",
            "screened_image_sha256": "12" * 32,
            "screened_image_size_bytes": 123,
            "screened_image_id": "sha256:" + "34" * 32,
            "screened_image_ref": "ditto-screen/agent:latest",
        },
    ],
)
async def test_invalid_artifact_image_contract_is_a_typed_platform_error(
    invalid_image_fields: dict[str, object],
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "agent_id": str(agent_id),
                "sha256": "ab" * 32,
                "download_url": "https://storage.test/artifact",
                "expires_at": datetime.now(UTC).isoformat(),
                **invalid_image_fields,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match="artifact response was invalid"):
            await PlatformClient(config, http, keypair).get_artifact(agent_id)  # type: ignore[arg-type]


async def test_ledger_request_is_fresh_and_signed() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")

    def handler(request: httpx.Request) -> httpx.Response:
        nonce = UUID(request.headers["X-Validator-Ledger-Nonce"])
        requested_at = datetime.fromisoformat(
            request.headers["X-Validator-Ledger-Requested-At"]
        )
        message = ledger_signing_message(
            validator_hotkey=keypair.ss58_address,
            nonce=nonce,
            requested_at=requested_at,
        )
        assert keypair.verify(
            message,
            bytes.fromhex(request.headers["X-Validator-Ledger-Signature"]),
        )
        return httpx.Response(
            200,
            json={
                "entries": [],
                "count": 0,
                "generated_at": datetime.now(UTC).isoformat(),
                "stale": False,
                "age_seconds": 0,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            config,  # type: ignore[arg-type]
            http,
            keypair,
        ).get_ledger()

    assert response.entries == []


def _signed_ledger_payload(
    *, bench_version: int, agent_id: UUID, miner_hotkey: str
) -> dict[str, object]:
    deadline = datetime(2026, 8, 10, 12, 30, tzinfo=UTC)
    keypairs = [
        bittensor.Keypair.create_from_uri(f"//PlatformLedger{i}") for i in range(3)
    ]
    proofs: list[LedgerScoreProof] = []
    for index, (keypair, composite) in enumerate(
        zip(keypairs, (0.4, 0.6, 0.8), strict=True)
    ):
        run_id = f"run_{index}"
        transcript = "cd" * 32
        base_evidence = "ef" * 32 if bench_version == 9 else None
        signature = sign_score(
            keypair,
            validator_hotkey=keypair.ss58_address,
            agent_id=agent_id,
            ticket_deadline=deadline,
            run_id=run_id,
            composite=composite,
            seed=index,
            bench_version=bench_version,
            transcript_sha256=transcript,
            base_evidence_sha256=base_evidence,
        )
        proofs.append(
            LedgerScoreProof(
                validator_hotkey=keypair.ss58_address,
                run_id=run_id,
                composite=composite,
                seed=index,
                bench_version=bench_version,
                ticket_deadline=deadline,
                transcript_sha256=transcript,
                base_evidence_sha256=base_evidence,
                signature=signature,
            )
        )
    median = proofs[1]
    return LedgerEntry(
        miner_hotkey=miner_hotkey,
        agent_id=agent_id,
        composite=median.composite,
        n=114,
        first_seen=deadline,
        sha256="ab" * 32,
        size_bytes=1024,
        run_id=median.run_id,
        seed=median.seed,
        validator_hotkey=median.validator_hotkey,
        bench_version=bench_version,
        signature=median.signature,
        score_proofs=proofs,
        status=AgentStatus.SCORED,
    ).model_dump(mode="json")


async def test_ledger_accepts_mixed_verified_v8_and_v9_rows() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    v8_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    v9_id = UUID("650e8400-e29b-41d4-a716-446655440000")
    entries = [
        _signed_ledger_payload(
            bench_version=8, agent_id=v8_id, miner_hotkey="5MinerV8" + "x" * 40
        ),
        _signed_ledger_payload(
            bench_version=9, agent_id=v9_id, miner_hotkey="5MinerV9" + "x" * 40
        ),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "entries": entries,
                "count": len(entries),
                "generated_at": datetime.now(UTC).isoformat(),
                "stale": False,
                "age_seconds": 0,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ledger = await PlatformClient(config, http, keypair).get_ledger()  # type: ignore[arg-type]

    assert [entry.agent_id for entry in ledger.entries] == [v8_id, v9_id]
    assert [entry.bench_version for entry in ledger.entries] == [8, 9]


@pytest.mark.parametrize("bench_version", [7, 9])
async def test_ledger_rejects_entry_without_verifiable_quorum_receipts(
    bench_version: int,
) -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "entries": [
                    {
                        "miner_hotkey": _HOTKEY,
                        "agent_id": str(agent_id),
                        "composite": 0.9,
                        "n": 114,
                        "first_seen": datetime.now(UTC).isoformat(),
                        "sha256": "ab" * 32,
                        "size_bytes": 1024,
                        "run_id": "run_1",
                        "seed": 42,
                        "validator_hotkey": keypair.ss58_address,
                        "bench_version": bench_version,
                        "signature": "cd" * 64,
                        "score_proofs": [],
                        "status": "scored",
                    }
                ],
                "count": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "stale": False,
                "age_seconds": 0,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match="score proof verification failed"):
            await PlatformClient(config, http, keypair).get_ledger()  # type: ignore[arg-type]


_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _config() -> MagicMock:
    config = MagicMock()
    config.platform_api_url = "https://platform.example"
    config.validator_hotkey = _HOTKEY
    return config


def _request() -> ValidatorHeartbeatRequest:
    return ValidatorHeartbeatRequest(
        validator_hotkey=_HOTKEY,
        software_version="0.1.0",
        protocol_version=1,
        code_digest="ab" * 32,
        state="running_benchmark",
        timestamp=1_752_443_200,
        signature="cd" * 64,
    )


async def test_submit_heartbeat_posts_signed_contract() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["header"] = request.headers["X-Validator-Hotkey"]
        captured["body"] = request.read()
        return httpx.Response(
            200,
            json={"accepted": True, "seen_at": datetime.now(UTC).isoformat()},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(_config(), http, MagicMock()).submit_heartbeat(
            _request()
        )

    assert response.accepted is True
    assert captured["url"] == "https://platform.example/api/v1/validator/heartbeat"
    assert captured["header"] == _HOTKEY
    assert b'"software_version":"0.1.0"' in captured["body"]


async def test_submit_heartbeat_surfaces_rejection() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="no")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match=r"heartbeat rejected \(401\)"):
            await PlatformClient(_config(), http, MagicMock()).submit_heartbeat(
                _request()
            )


async def test_exchange_inference_grant_preserves_ticket_route_identity() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    grant_id = UUID("00000000-0000-0000-0000-000000000001")
    expires_at = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/inference/exchange"
        return httpx.Response(
            200,
            headers={
                "X-Ditto-Request-Budget": "8192",
                "X-Ditto-Token-Budget": "75000000",
                "X-Ditto-Embedding-Request-Budget": "100000",
                "X-Ditto-Embedding-Token-Budget": "1000000000",
                "X-Ditto-Max-Output-Tokens": "8192",
            },
            json={
                "grant_id": str(grant_id),
                "bearer": "b" * 32,
                "proxy_url": "https://platform.test/api/v1/inference/chat/completions",
                "expires_at": expires_at.isoformat(),
                "generation": 1,
                "provider": "WandB",
                "profile_revision": "openrouter-route-wandb-v1",
                "model": "openai/gpt-oss-20b",
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            cast(Any, config),
            http,
            keypair,
        ).exchange_inference_grant(
            grant_id,
            "A" * 43,
            "https://platform.test/api/v1/inference/exchange",
        )

    assert response.provider == "WandB"
    assert response.model == "openai/gpt-oss-20b"
    assert response.request_budget == 8192
    assert response.token_budget == 75_000_000
    assert response.embedding_request_budget == 100_000
    assert response.embedding_token_budget == 1_000_000_000
    assert response.max_output_tokens == 8192


async def test_exchange_inference_grant_prefers_json_budget_evidence() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    grant_id = UUID("00000000-0000-0000-0000-000000000001")
    expires_at = datetime.now(UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/inference/exchange"
        return httpx.Response(
            200,
            json={
                "grant_id": str(grant_id),
                "bearer": "b" * 32,
                "proxy_url": "https://platform.test/api/v1/inference/chat/completions",
                "expires_at": expires_at.isoformat(),
                "generation": 1,
                "provider": "WandB",
                "profile_revision": "openrouter-route-wandb-v1",
                "model": "openai/gpt-oss-20b",
                "request_budget": 8192,
                "token_budget": 75_000_000,
                "embedding_request_budget": 100_000,
                "embedding_token_budget": 1_000_000_000,
                "max_output_tokens": 8192,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            cast(Any, config),
            http,
            keypair,
        ).exchange_inference_grant(
            grant_id,
            "A" * 43,
            "https://platform.test/api/v1/inference/exchange",
        )

    assert response.request_budget == 8192
    assert response.token_budget == 75_000_000
    assert response.embedding_request_budget == 100_000
    assert response.embedding_token_budget == 1_000_000_000
    assert response.max_output_tokens == 8192


async def test_exchange_inference_grant_json_survives_partial_headers() -> None:
    """A Cloudflare hop that drops some X-Ditto-* headers must not disarm JSON."""
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    grant_id = UUID("00000000-0000-0000-0000-000000000001")
    expires_at = datetime.now(UTC)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Ditto-Request-Budget": "8192"},
            json={
                "grant_id": str(grant_id),
                "bearer": "b" * 32,
                "proxy_url": "https://platform.test/api/v1/inference/chat/completions",
                "expires_at": expires_at.isoformat(),
                "generation": 1,
                "request_budget": 8192,
                "token_budget": 75_000_000,
                "embedding_request_budget": 100_000,
                "embedding_token_budget": 1_000_000_000,
                "max_output_tokens": 8192,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await PlatformClient(
            cast(Any, config), http, keypair
        ).exchange_inference_grant(
            grant_id,
            "A" * 43,
            "https://platform.test/api/v1/inference/exchange",
        )

    assert response.token_budget == 75_000_000
    assert response.max_output_tokens == 8192


async def test_exchange_inference_grant_rejects_partial_json_budget_evidence() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    grant_id = UUID("00000000-0000-0000-0000-000000000001")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "grant_id": str(grant_id),
                "bearer": "b" * 32,
                "proxy_url": "https://platform.test/api/v1/inference/chat/completions",
                "expires_at": datetime.now(UTC).isoformat(),
                "generation": 1,
                "request_budget": 8192,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(
            PlatformInfrastructureError, match="incomplete budget evidence"
        ):
            await PlatformClient(
                cast(Any, config), http, keypair
            ).exchange_inference_grant(
                grant_id,
                "A" * 43,
                "https://platform.test/api/v1/inference/exchange",
            )


async def test_exchange_inference_grant_rejects_partial_budget_evidence() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    grant_id = UUID("00000000-0000-0000-0000-000000000001")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Ditto-Request-Budget": "8192"},
            json={
                "grant_id": str(grant_id),
                "bearer": "b" * 32,
                "proxy_url": "https://platform.test/api/v1/inference/chat/completions",
                "expires_at": datetime.now(UTC).isoformat(),
                "generation": 1,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(
            PlatformInfrastructureError, match="incomplete budget evidence"
        ):
            await PlatformClient(
                cast(Any, config), http, keypair
            ).exchange_inference_grant(
                grant_id,
                "A" * 43,
                "https://platform.test/api/v1/inference/exchange",
            )


async def test_exchange_inference_grant_parks_transient_503() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    grant_id = UUID("00000000-0000-0000-0000-000000000001")
    attempts: list[UUID] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        attempts.append(UUID(payload["nonce"]))
        return httpx.Response(503)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformInfrastructureError, match="rejected \\(503\\)"):
            await PlatformClient(
                cast(Any, config), http, keypair
            ).exchange_inference_grant(
                grant_id,
                "A" * 43,
                "https://platform.test/api/v1/inference/exchange",
            )

    assert len(attempts) == 1


async def test_exchange_inference_grant_single_503_is_infrastructure() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformInfrastructureError, match="rejected \\(503\\)"):
            await PlatformClient(
                cast(Any, config), http, keypair
            ).exchange_inference_grant(
                UUID("00000000-0000-0000-0000-000000000001"),
                "A" * 43,
                "https://platform.test/api/v1/inference/exchange",
            )

    assert attempts == 1


def _score_report() -> ScoreReport:
    return ScoreReport(
        run_id="run_1",
        seed=1,
        composite=0.5,
        tool_mean=0.5,
        memory_mean=0.5,
        median_ms=10,
        n=1,
        generated_at=datetime.now(UTC),
        per_case=[],
        structural_fingerprint=None,
        details=None,
    )


async def test_submit_score_parks_empty_502_as_infrastructure() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(502, text="")

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(
            PlatformInfrastructureError, match="score rejected \\(502\\)"
        ):
            await PlatformClient(cast(Any, config), http, keypair).submit_score(
                agent_id,
                signature="ab" * 64,
                report=_score_report(),
            )

    assert attempts == 1


async def test_submit_score_does_not_replay_transient_502() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, text="")
        return httpx.Response(
            200,
            json={
                "agent_id": str(agent_id),
                "status": "evaluating",
                "accepted": True,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(
            PlatformInfrastructureError, match="score rejected \\(502\\)"
        ):
            await PlatformClient(cast(Any, config), http, keypair).submit_score(
                agent_id, signature="ab" * 64, report=_score_report()
            )

    assert attempts == 1


async def test_submit_score_4xx_stays_a_scoring_error() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "agent is not scoreable"})

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(PlatformError, match="score rejected \\(409\\)") as caught:
            await PlatformClient(cast(Any, config), http, keypair).submit_score(
                agent_id,
                signature="ab" * 64,
                report=_score_report(),
            )
    assert type(caught.value) is PlatformError


async def test_submit_transcript_puts_raw_bytes_with_hotkey_header() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")
    body = b'{"run_id":"run_1","cases":[]}'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == (
            f"/api/v1/validator/agent/{agent_id}/transcript/run_1"
        )
        assert request.headers["X-Validator-Hotkey"] == keypair.ss58_address
        assert request.content == body
        return httpx.Response(
            200,
            json={
                "agent_id": str(agent_id),
                "run_id": "run_1",
                "transcript_sha256": "ab" * 32,
                "stored": True,
            },
        )

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        platform = PlatformClient(config, http, keypair)  # type: ignore[arg-type]
        await platform.submit_transcript(agent_id, run_id="run_1", body=body)


async def test_submit_transcript_rejection_raises_platform_error() -> None:
    keypair = bittensor.Keypair.create_from_uri("//Alice")
    agent_id = UUID("550e8400-e29b-41d4-a716-446655440000")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="digest mismatch")

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        validator_hotkey=keypair.ss58_address,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        platform = PlatformClient(config, http, keypair)  # type: ignore[arg-type]
        with pytest.raises(PlatformError, match="transcript rejected"):
            await platform.submit_transcript(agent_id, run_id="run_1", body=b"{}")


def test_exchange_accepts_the_split_inference_host_and_nothing_else() -> None:
    """The platform may serve its inference plane on its own public hostname.

    DITTO_INFERENCE_PUBLIC_BASE_URL is independent of the API host a validator
    posts jobs and scores to. Both are allowlisted; anything else is refused, so
    a hostile ticket still cannot redirect a grant exchange.
    """
    import asyncio
    from types import SimpleNamespace
    from uuid import uuid4

    config = SimpleNamespace(
        platform_api_url="https://platform.test",
        platform_inference_base_url="https://inference.test",
        validator_hotkey="5Test",
        http_timeout_seconds=5.0,
    )
    client = PlatformClient.__new__(PlatformClient)
    client._config = cast(Any, config)
    client._base = config.platform_api_url.rstrip("/")
    client._inference_base = config.platform_inference_base_url.rstrip("/")

    for hostile in (
        "https://attacker.example/api/v1/inference/exchange",
        "https://platform.test.attacker.example/api/v1/inference/exchange",
        "https://inference.test/api/v1/inference/exchange/../../evil",
    ):
        with pytest.raises(PlatformError, match="not the platform"):
            asyncio.run(client.exchange_inference_grant(uuid4(), "key", hostile))
