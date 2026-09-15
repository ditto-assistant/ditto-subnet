from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

import ditto.validator.coding_canary_runtime as runtime_module
from ditto.api_models.coding import (
    CodingCapabilityCertificationReceipt,
    SubmitCodingCertificationResponse,
)
from ditto.api_models.coding_certification_leases import (
    CodingCertificationHarnessLaunchResponse,
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseResponse,
    CodingCertificationLeaseStatus,
)
from ditto.api_models.coding_inference_grants import (
    CodingCertificationInferenceExchangeResponse,
    CodingCertificationInferenceGrantOffer,
    CodingCertificationInferenceRevokeResponse,
)
from ditto.validator.coding_canary import (
    CodingCanaryOutcome,
    CodingCanaryReadiness,
    CodingCanaryTargets,
    CodingCanaryWorker,
)
from ditto.validator.coding_canary_runtime import CodingCanaryRuntime
from ditto.validator.errors import (
    PlatformError,
    PlatformInfrastructureError,
    ValidatorInfrastructureError,
)

_TOKEN = "coding-canary-control-token-00000001"

_NOW = datetime(2026, 8, 30, 18, tzinfo=UTC)
_AGENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_LEASE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_UPLOAD = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_HOTKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_OTHER_AGENT = UUID("abababab-abab-4bab-8bab-abababababab")
_TARGETS = CodingCanaryTargets.of([_AGENT], _HOTKEY)


def _authority(**updates: object) -> CodingCertificationLeaseAuthority:
    value: dict[str, object] = {
        "schema": "dittobench-coding-certification-lease-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "lease_id": _LEASE,
        "validator_hotkey": _HOTKEY,
        "agent_id": _AGENT,
        "agent_artifact_sha256": "aa" * 32,
        "screened_image_sha256": "1a" * 32,
        "bench_version": 12,
        "core_qualification_observation_id": UUID(
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        ),
        "core_qualification_policy_checksum": "cc" * 32,
        "canary_manifest_sha256": "bb" * 32,
        "runner_plan_sha256": "ee" * 32,
        "grader_plan_sha256": "ff" * 32,
        "resource_profile_sha256": "11" * 32,
        "inference_policy_sha256": "22" * 32,
        "issued_at": _NOW,
        "deadline": _NOW + timedelta(minutes=20),
    }
    value.update(updates)
    return CodingCertificationLeaseAuthority.model_validate(value)


def _lease(
    *,
    status: CodingCertificationLeaseStatus = CodingCertificationLeaseStatus.ISSUED,
) -> CodingCertificationLeaseResponse:
    return CodingCertificationLeaseResponse(
        authority=_authority(),
        status=status,
        claimed_at=_NOW if status is CodingCertificationLeaseStatus.CLAIMED else None,
        screened_image_id="sha256:" + "ef" * 32,
        screened_image_ref=f"ditto-screen/{_AGENT}:latest",
        screened_image_upload_id=_UPLOAD,
        weight_eligible=False,
    )


def _harness() -> CodingCertificationHarnessLaunchResponse:
    return CodingCertificationHarnessLaunchResponse.model_validate(
        {
            "schema": "dittobench-coding-certification-harness-launch-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "lease_id": _LEASE,
            "agent_id": _AGENT,
            "lease_deadline": _NOW + timedelta(minutes=20),
            "bench_version": 12,
            "agent_artifact_sha256": "aa" * 32,
            "screened_image_sha256": "1a" * 32,
            "screened_image_size_bytes": 1024,
            "screened_image_id": "sha256:" + "ef" * 32,
            "screened_image_ref": f"ditto-screen/{_AGENT}:latest",
            "screening_policy_version": 9,
            "image_url": "https://storage.invalid/image.tar?signature=synthetic",
            "expires_at": _NOW + timedelta(minutes=5),
        }
    )


def _receipt() -> CodingCapabilityCertificationReceipt:
    vector = json.loads(
        (
            Path(__file__).parents[3]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_certification_v1.json"
        ).read_text(encoding="utf-8")
    )
    return CodingCapabilityCertificationReceipt.model_validate_json(
        json.dumps(vector["receipt"])
    )


def _grant_offer() -> CodingCertificationInferenceGrantOffer:
    return CodingCertificationInferenceGrantOffer.model_validate(
        {
            "schema": "dittobench-coding-certification-inference-grant-offer-v1",
            "coding_contract_version": 1,
            "weight_eligible": False,
            "grant_id": UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            "lease_id": _LEASE,
            "case_id": "PRACTICE-LEDGER-001",
            "profile_capability_id": "public-certification-v1",
            "inference_grant_sha256": "33" * 32,
            "model": "openai/gpt-5.6-luna",
            "provider_api": "openrouter",
            "provider_route": "azure/eu",
            "receipt_provider": "Azure",
            "provider_route_profile": "luna-azure-eu-zdr-v1",
            "provider_account_guardrail": "openrouter_private_account_v1",
            "provider_pipeline_policy": "no_plugins_no_transforms_v1",
            "provider_cache_policy": "disabled_v1",
            "reasoning_effort": "medium",
            "request_budget": 32,
            "prompt_token_budget": 10_000,
            "completion_token_budget": 2_000,
            "cost_budget_usd_micros": 1_000_000,
            "expires_at": _NOW + timedelta(minutes=20),
            "status": "pending",
            "generation": 0,
            "exchange_url": (
                "https://platform.invalid/api/v1/validator/"
                "coding-certification-leases/inference-exchange"
            ),
        }
    )


def _grant_exchange() -> CodingCertificationInferenceExchangeResponse:
    return CodingCertificationInferenceExchangeResponse.model_validate(
        {
            **{
                key: value
                for key, value in _grant_offer()
                .model_dump(mode="json", by_alias=True)
                .items()
                if key != "exchange_url"
            },
            "schema": "dittobench-coding-certification-inference-exchange-v1",
            "status": "active",
            "generation": 1,
            "bearer": "b" * 43,
            "proxy_url": (
                "https://relay.invalid/api/v1/inference/coding/chat/completions"
            ),
            "revoke_bearer": "r" * 43,
            "revoke_url": (
                "https://platform.invalid/api/v1/validator/"
                "coding-shadow/inference-revoke-capability"
            ),
        }
    )


class _Platform:
    def __init__(self) -> None:
        self.issues = 0
        self.claims = 0
        self.aborts = 0
        self.launches = 0
        self.grants = 0
        self.exchanges = 0
        self.revokes = 0
        self.submits = 0
        self.issued = _lease()
        self.claimed = _lease(status=CodingCertificationLeaseStatus.CLAIMED)
        self.issue_error: Exception | None = None
        self.claim_error: Exception | None = None

    async def issue_coding_certification_lease(
        self, agent_id: UUID, *, bench_version: int
    ) -> CodingCertificationLeaseResponse | None:
        self.issues += 1
        assert agent_id == _AGENT
        assert bench_version == 12
        if self.issue_error is not None:
            raise self.issue_error
        return self.issued

    async def claim_coding_certification_lease(
        self, lease_id: UUID
    ) -> CodingCertificationLeaseResponse:
        self.claims += 1
        assert lease_id == _LEASE
        if self.claim_error is not None:
            raise self.claim_error
        return self.claimed

    async def abort_coding_certification_lease(
        self, lease_id: UUID
    ) -> CodingCertificationLeaseResponse:
        self.aborts += 1
        assert lease_id == _LEASE
        return _lease(status=CodingCertificationLeaseStatus.ABORTED)

    async def request_coding_certification_harness_launch(
        self, lease_id: UUID
    ) -> CodingCertificationHarnessLaunchResponse:
        self.launches += 1
        assert lease_id == _LEASE
        return _harness()

    async def request_coding_certification_inference_grant(
        self, lease_id: UUID
    ) -> CodingCertificationInferenceGrantOffer:
        self.grants += 1
        assert lease_id == _LEASE
        return _grant_offer()

    async def exchange_coding_certification_inference_grant(
        self,
        offer: CodingCertificationInferenceGrantOffer,
        *,
        broker_public_key: str,
    ) -> CodingCertificationInferenceExchangeResponse:
        self.exchanges += 1
        assert offer.lease_id == _LEASE
        assert len(broker_public_key) == 43
        return _grant_exchange()

    async def revoke_coding_certification_inference_grant(
        self,
        *,
        grant_id: UUID,
        generation: int,
    ) -> CodingCertificationInferenceRevokeResponse:
        self.revokes += 1
        assert grant_id == UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
        assert generation == 1
        return CodingCertificationInferenceRevokeResponse(
            schema="dittobench-coding-certification-inference-revocation-v1",
            coding_contract_version=1,
            weight_eligible=False,
            grant_id=grant_id,
            lease_id=_LEASE,
            status="revoked",
            generation=generation,
            revoked_at=_NOW,
            idempotent=False,
        )

    async def submit_coding_certification(
        self,
        agent_id: UUID,
        *,
        bench_version: int,
        lease_id: UUID,
        screened_image_sha256: str,
        receipt: CodingCapabilityCertificationReceipt,
        signature: str,
    ) -> SubmitCodingCertificationResponse:
        self.submits += 1
        assert agent_id == _AGENT
        assert bench_version == 12
        assert lease_id == _LEASE
        assert screened_image_sha256 == "1a" * 32
        assert signature == "ab" * 64
        assert receipt.weight_eligible is False
        return SubmitCodingCertificationResponse(
            agent_id=agent_id,
            certification_id=receipt.certification_id,
            status=receipt.status,
            accepted=True,
            idempotent=False,
            active=receipt.status.value == "certified",
        )


def _readiness(**updates: str) -> CodingCanaryReadiness:
    authority = _authority()
    value = {
        "canary_manifest_sha256": authority.canary_manifest_sha256,
        "runner_plan_sha256": authority.runner_plan_sha256,
        "grader_plan_sha256": authority.grader_plan_sha256,
        "resource_profile_sha256": authority.resource_profile_sha256,
        "inference_policy_sha256": authority.inference_policy_sha256,
    }
    value.update(updates)
    return CodingCanaryReadiness(**value)


class _Runtime:
    def __init__(self) -> None:
        self.probes = 0
        self.certified: list[CodingCertificationLeaseResponse] = []
        self.available = True
        self.readiness = _readiness()

    async def require_ready(self) -> CodingCanaryReadiness:
        self.probes += 1
        if not self.available:
            raise PlatformInfrastructureError("coding canary runtime is not ready")
        return self.readiness

    async def certify(
        self,
        lease: CodingCertificationLeaseResponse,
        harness: CodingCertificationHarnessLaunchResponse,
        grant: CodingCertificationInferenceExchangeResponse,
        *,
        broker_public_key: str,
        broker_private_key: str,
    ) -> CodingCanaryOutcome:
        self.certified.append(lease)
        assert harness.lease_id == lease.authority.lease_id
        assert grant.lease_id == lease.authority.lease_id
        assert grant.status == "active"
        assert len(broker_public_key) == 43
        assert len(broker_private_key) == 86
        return CodingCanaryOutcome(
            authority=lease.authority,
            receipt=_receipt(),
            capabilities_revoked=True,
            harness_destroyed=True,
        )


def _worker(
    platform: _Platform,
    runtime: _Runtime,
    *,
    targets: CodingCanaryTargets = _TARGETS,
    validator_hotkey: str = _HOTKEY,
) -> CodingCanaryWorker:
    return CodingCanaryWorker(
        platform=platform,
        runtime=runtime,
        sign_receipt=lambda _lease, _receipt: "ab" * 64,
        validator_hotkey=validator_hotkey,
        targets=targets,
        clock=lambda: _NOW,
    )


@pytest.mark.asyncio
async def test_canary_worker_claims_issued_lease_then_runs_certifier() -> None:
    platform = _Platform()
    runtime = _Runtime()
    worker = _worker(platform, runtime)
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is True
    assert platform.issues == 1
    assert platform.claims == 1
    assert platform.launches == 1
    assert platform.grants == 1
    assert platform.exchanges == 1
    assert platform.revokes == 1
    assert platform.submits == 1
    assert len(runtime.certified) == 1
    assert runtime.certified[0].status is CodingCertificationLeaseStatus.CLAIMED
    assert runtime.certified[0].authority.weight_eligible is False


@pytest.mark.asyncio
async def test_canary_worker_skips_ineligible_or_conflicted_issue() -> None:
    platform = _Platform()
    runtime = _Runtime()
    worker = _worker(platform, runtime)

    async def missing(*_args: object, **_kwargs: object) -> None:
        return None

    platform.issue_coding_certification_lease = missing  # type: ignore[method-assign]
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is False
    assert platform.claims == 0

    async def conflict(*_args: object, **_kwargs: object) -> None:
        raise PlatformError("coding certification lease request rejected (409)")

    platform.issue_coding_certification_lease = conflict  # type: ignore[method-assign]
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is False
    assert platform.claims == 0


@pytest.mark.asyncio
async def test_canary_worker_does_not_claim_when_runtime_is_down() -> None:
    platform = _Platform()
    runtime = _Runtime()
    runtime.available = False
    worker = _worker(platform, runtime)
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is False
    assert platform.issues == 0
    assert platform.claims == 0
    assert platform.aborts == 0


@pytest.mark.asyncio
async def test_canary_worker_does_not_skip_issue_infrastructure_failure() -> None:
    platform = _Platform()
    platform.issue_error = PlatformInfrastructureError(
        "public certification canary is unavailable"
    )
    runtime = _Runtime()
    worker = _worker(platform, runtime)
    worker.offer(_AGENT, 12)
    with pytest.raises(PlatformInfrastructureError, match="unavailable"):
        await worker.run_once()
    assert platform.issues == 1
    assert platform.claims == 0
    assert platform.aborts == 0
    assert runtime.certified == []

    platform.issue_error = None
    assert await worker.run_once() is True
    assert platform.issues == 2
    assert platform.claims == 1
    assert len(runtime.certified) == 1


@pytest.mark.asyncio
async def test_canary_worker_aborts_issued_lease_if_claim_fails() -> None:
    platform = _Platform()
    platform.claim_error = PlatformInfrastructureError("claim failed")
    runtime = _Runtime()
    worker = _worker(platform, runtime)
    worker.offer(_AGENT, 12)
    with pytest.raises(PlatformInfrastructureError, match="claim failed"):
        await worker.run_once()
    assert platform.issues == 1
    assert platform.claims == 1
    assert platform.aborts == 1
    assert runtime.certified == []


@pytest.mark.parametrize(
    "targets",
    [
        CodingCanaryTargets(),
        CodingCanaryTargets.of([], _HOTKEY),
        CodingCanaryTargets.of([_AGENT], ""),
        CodingCanaryTargets.of([_OTHER_AGENT], _HOTKEY),
        # Exact targets copied onto a validator with a different hotkey.
        CodingCanaryTargets.of([_AGENT], "5" + "F" * 47),
    ],
)
async def test_canary_worker_refuses_every_lease_outside_its_exact_targets(
    targets: CodingCanaryTargets,
) -> None:
    platform = _Platform()
    runtime = _Runtime()
    worker = _worker(platform, runtime, targets=targets)
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is False
    # Defense in depth: even a target that reached the queue is refused.
    worker._queue.put_nowait((_AGENT, 12))
    assert await worker.run_once() is False
    assert platform.issues == 0
    assert platform.claims == 0
    assert runtime.certified == []


async def test_canary_worker_default_targets_refuse_everything() -> None:
    assert CodingCanaryTargets().refuses_all(_HOTKEY)
    assert not CodingCanaryTargets().permits(_AGENT, _HOTKEY)
    assert _TARGETS.permits(_AGENT, _HOTKEY)
    assert not _TARGETS.permits(_OTHER_AGENT, _HOTKEY)
    assert not _TARGETS.permits(_AGENT, "5" + "F" * 47)
    assert not _TARGETS.refuses_all(_HOTKEY)
    assert _TARGETS.refuses_all("5" + "F" * 47)


async def test_canary_worker_offers_only_allowlisted_agents() -> None:
    platform = _Platform()
    runtime = _Runtime()
    worker = _worker(platform, runtime)
    worker.offer(_OTHER_AGENT, 12)
    assert worker._queue.empty()
    assert await worker.run_once() is False
    assert runtime.probes == 0
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is True
    assert platform.issues == 1


async def test_canary_worker_keeps_the_offer_until_the_scorer_is_ready() -> None:
    platform = _Platform()
    runtime = _Runtime()
    runtime.available = False
    worker = _worker(platform, runtime)
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is False
    assert await worker.run_once() is False
    assert runtime.probes == 2
    assert platform.issues == 0
    runtime.available = True
    assert await worker.run_once() is True
    assert platform.issues == 1
    assert platform.claims == 1


@pytest.mark.parametrize(
    "authority",
    [
        {"agent_id": _OTHER_AGENT},
        {"validator_hotkey": "5" + "F" * 47},
    ],
)
async def test_canary_worker_aborts_an_issued_lease_for_another_target(
    authority: dict[str, object],
) -> None:
    platform = _Platform()
    platform.issued = CodingCertificationLeaseResponse(
        authority=_authority(**authority),
        status=CodingCertificationLeaseStatus.ISSUED,
        claimed_at=None,
        screened_image_id="sha256:" + "ef" * 32,
        screened_image_ref=f"ditto-screen/{_AGENT}:latest",
        screened_image_upload_id=_UPLOAD,
        weight_eligible=False,
    )
    runtime = _Runtime()
    worker = _worker(platform, runtime)
    worker.offer(_AGENT, 12)
    with pytest.raises(PlatformInfrastructureError, match="allowlisted target"):
        await worker.run_once()
    assert platform.issues == 1
    assert platform.aborts == 1
    assert platform.claims == 0
    assert runtime.certified == []


@pytest.mark.parametrize(
    "field",
    [
        "canary_manifest_sha256",
        "runner_plan_sha256",
        "grader_plan_sha256",
        "resource_profile_sha256",
        "inference_policy_sha256",
    ],
)
async def test_canary_worker_aborts_a_lease_the_ready_scorer_pack_cannot_run(
    field: str,
) -> None:
    platform = _Platform()
    runtime = _Runtime()
    runtime.readiness = _readiness(**{field: "9" * 64})
    worker = _worker(platform, runtime)
    worker.offer(_AGENT, 12)
    with pytest.raises(PlatformInfrastructureError, match="scorer pack"):
        await worker.run_once()
    assert platform.issues == 1
    assert platform.aborts == 1
    assert platform.claims == 0
    assert runtime.certified == []


def _runtime_config(url: str = "http://127.0.0.1:18081") -> Any:
    return SimpleNamespace(
        dittobench_api_url=url,
        dittobench_control_token=_TOKEN,
    )


def _canary_response_payload() -> dict[str, object]:
    return {
        "schema": "dittobench-coding-certification-canary-response-v1",
        "lease_id": str(_LEASE),
        "capabilities_revoked": True,
        "harness_destroyed": True,
        "receipt": json.loads(_receipt().model_dump_json()),
    }


@pytest.mark.asyncio
async def test_canary_runtime_certify_accepts_private_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Cache-Control"] == "no-store"
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
        body = json.loads(request.content)
        assert body["grant"]["lease_id"] == str(_LEASE)
        assert body["grant"]["schema"].endswith("inference-exchange-v1")
        assert body["grant"]["broker_public_key"] == "A" * 43
        assert body["grant"]["broker_private_key"] == "B" * 86
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            json=_canary_response_payload(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(_runtime_config(), http, clock=lambda: _NOW)
        outcome = await runtime.certify(
            _lease(status=CodingCertificationLeaseStatus.CLAIMED),
            _harness(),
            _grant_exchange(),
            broker_public_key="A" * 43,
            broker_private_key="B" * 86,
        )
    assert outcome.receipt.weight_eligible is False
    assert outcome.capabilities_revoked is True
    assert outcome.harness_destroyed is True
    assert outcome.authority.lease_id == _LEASE


@pytest.mark.asyncio
async def test_canary_runtime_rejects_missing_no_store() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Cache-Control"] == "no-store"
        assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=_canary_response_payload(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(_runtime_config(), http, clock=lambda: _NOW)
        with pytest.raises(ValidatorInfrastructureError, match="cache policy"):
            await runtime.certify(
                _lease(status=CodingCertificationLeaseStatus.CLAIMED),
                _harness(),
                _grant_exchange(),
                broker_public_key="A" * 43,
                broker_private_key="B" * 86,
            )


def _readiness_payload(**updates: object) -> dict[str, object]:
    authority = _authority()
    value: dict[str, object] = {
        "schema": "dittobench-coding-certification-canary-readiness-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "ready": True,
        "failure": "",
        "pack_loaded": True,
        "executor_daemon_ready": True,
        "runtime_image_ready": True,
        "canary_manifest_sha256": authority.canary_manifest_sha256,
        "runner_plan_sha256": authority.runner_plan_sha256,
        "grader_plan_sha256": authority.grader_plan_sha256,
        "resource_profile_sha256": authority.resource_profile_sha256,
        "inference_policy_sha256": authority.inference_policy_sha256,
    }
    value.update(updates)
    return value


async def test_canary_runtime_readiness_returns_the_ready_pack_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            json=_readiness_payload(),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(_runtime_config(), http, clock=lambda: _NOW)
        readiness = await runtime.require_ready()
    assert readiness == _readiness()
    assert readiness.binds(_authority())
    (request,) = requests
    assert request.method == "GET"
    assert request.url.path == "/v1/coding/certifier/canary/readiness"
    assert request.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert request.content == b""


@pytest.mark.parametrize(
    ("status", "headers", "payload"),
    [
        (404, {"Cache-Control": "no-store"}, {"error": "not_found"}),
        (401, {"Cache-Control": "no-store"}, {"error": "unauthorized"}),
        (503, {"Cache-Control": "no-store"}, {"error": "busy"}),
        (302, {"Location": "https://elsewhere.invalid"}, {}),
        (200, {}, _readiness_payload()),
        (200, {"Cache-Control": "no-store"}, "not json"),
        (200, {"Cache-Control": "no-store"}, _readiness_payload(ready=False)),
        (
            200,
            {"Cache-Control": "no-store"},
            _readiness_payload(ready=False, failure="executor_daemon"),
        ),
        (
            200,
            {"Cache-Control": "no-store"},
            _readiness_payload(executor_daemon_ready=False),
        ),
        (
            200,
            {"Cache-Control": "no-store"},
            _readiness_payload(runtime_image_ready=False),
        ),
        (200, {"Cache-Control": "no-store"}, _readiness_payload(pack_loaded=False)),
        (200, {"Cache-Control": "no-store"}, _readiness_payload(failure="pack")),
        (200, {"Cache-Control": "no-store"}, _readiness_payload(weight_eligible=True)),
        (
            200,
            {"Cache-Control": "no-store"},
            _readiness_payload(coding_contract_version=2),
        ),
        (200, {"Cache-Control": "no-store"}, _readiness_payload(schema="other-v1")),
        (
            200,
            {"Cache-Control": "no-store"},
            _readiness_payload(canary_manifest_sha256="AB" * 32),
        ),
        (200, {"Cache-Control": "no-store"}, _readiness_payload(ready="true")),
        (
            200,
            {"Cache-Control": "no-store"},
            {
                key: value
                for key, value in _readiness_payload().items()
                if key != "runtime_image_ready"
            },
        ),
    ],
)
async def test_canary_runtime_readiness_fails_closed(
    status: int, headers: dict[str, str], payload: object
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(
                status,
                headers={"Content-Type": "application/json", **headers},
                content=payload.encode(),
            )
        return httpx.Response(
            status,
            headers={"Content-Type": "application/json", **headers},
            json=payload,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(_runtime_config(), http, clock=lambda: _NOW)
        with pytest.raises(PlatformInfrastructureError, match="canary runtime"):
            await runtime.require_ready()


async def test_canary_runtime_readiness_accepts_the_shared_contract_vector() -> None:
    vector = json.loads(
        (
            Path(__file__).parents[3]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_certification_canary_readiness_v1.json"
        ).read_text(encoding="utf-8")
    )
    ready = vector["ready"]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            json=ready,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(_runtime_config(), http, clock=lambda: _NOW)
        readiness = await runtime.require_ready()
        assert readiness == CodingCanaryReadiness(
            canary_manifest_sha256=ready["canary_manifest_sha256"],
            runner_plan_sha256=ready["runner_plan_sha256"],
            grader_plan_sha256=ready["grader_plan_sha256"],
            resource_profile_sha256=ready["resource_profile_sha256"],
            inference_policy_sha256=ready["inference_policy_sha256"],
        )
        for failure in vector["not_ready_failures"]:
            ready = {**vector["ready"], "ready": False, "failure": failure}
            with pytest.raises(PlatformInfrastructureError, match=failure):
                await runtime.require_ready()
    # The pinned public canary identity Platform binds into every lease.
    assert readiness.canary_manifest_sha256 == (
        "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903"
    )


async def test_canary_runtime_readiness_refuses_an_unreachable_scorer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(_runtime_config(), http, clock=lambda: _NOW)
        with pytest.raises(PlatformInfrastructureError, match="unreachable"):
            await runtime.require_ready()


@pytest.mark.parametrize(
    "url",
    [
        # The fixed Compose scorer origin on the stack's private bridge.
        "http://sandbox-docker:8000",
        "http://sandbox-docker:8000/",
        "http://127.0.0.1:18081",
        "http://localhost:18081",
        "https://scorer.invalid",
    ],
)
async def test_canary_runtime_accepts_compose_loopback_and_tls_origins(
    url: str,
) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        CodingCanaryRuntime(_runtime_config(url), http)


@pytest.mark.parametrize(
    "url",
    [
        "http://sandbox-docker",
        "http://sandbox-docker:8001",
        "http://SANDBOX-DOCKER:8000",
        "http://sandbox-docker.:8000",
        "http://sandbox-docker.invalid:8000",
        "http://scorer.invalid:8000",
        "http://10.0.0.5:8000",
        "https://sandbox-docker:8000/v1",
        "http://sandbox-docker:8000/v1",
        "http://operator:secret@sandbox-docker:8000",
        "http://sandbox-docker:8000?next=1",
        "http://sandbox-docker:8000#fragment",
        "ws://sandbox-docker:8000",
        "http://127.0.0.1:not-a-port",
        "sandbox-docker:8000",
    ],
)
async def test_canary_runtime_rejects_other_plaintext_or_shaped_origins(
    url: str,
) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(ValueError, match="configuration is invalid"):
            CodingCanaryRuntime(_runtime_config(url), http)


async def test_canary_runtime_rejects_a_proxy_inheriting_client() -> None:
    async with httpx.AsyncClient() as http:
        assert http.trust_env is True
        with pytest.raises(ValueError, match="configuration is invalid"):
            CodingCanaryRuntime(_runtime_config("http://sandbox-docker:8000"), http)


class _HangingBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await asyncio.Event().wait()
        yield b""  # pragma: no cover - never reached

    async def aclose(self) -> None:
        self.closed = True


async def _certify(runtime: CodingCanaryRuntime) -> CodingCanaryOutcome:
    return await runtime.certify(
        _lease(status=CodingCertificationLeaseStatus.CLAIMED),
        _harness(),
        _grant_exchange(),
        broker_public_key="A" * 43,
        broker_private_key="B" * 86,
    )


@pytest.mark.parametrize(
    "remaining",
    [timedelta(minutes=20), timedelta(seconds=5), timedelta(hours=1)],
)
async def test_canary_runtime_bounds_certify_only_by_the_lease_deadline(
    remaining: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[dict[str, float | None]] = []
    budgets: list[float | None] = []
    deadline = _authority().deadline
    original_timeout = runtime_module.asyncio.timeout

    def recording_timeout(delay: float | None) -> Any:
        budgets.append(delay)
        return original_timeout(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.extensions["timeout"])
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            json=_canary_response_payload(),
        )

    monkeypatch.setattr(runtime_module.asyncio, "timeout", recording_timeout)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, timeout=30.0
    ) as http:
        runtime = CodingCanaryRuntime(
            _runtime_config("http://sandbox-docker:8000"),
            http,
            clock=lambda: deadline - remaining,
        )
        await _certify(runtime)
    # The whole exchange is bounded by exactly the time left on the lease, with
    # no separate cap and no per-read timeout that could fire first.
    assert budgets == [remaining.total_seconds()]
    assert observed == [{"connect": 10.0, "read": None, "write": 60.0, "pool": 10.0}]


@pytest.mark.parametrize(
    "offset", [timedelta(0), timedelta(seconds=1), timedelta(hours=2)]
)
async def test_canary_runtime_never_sends_after_the_lease_deadline(
    offset: timedelta,
) -> None:
    calls = 0
    deadline = _authority().deadline

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(
            _runtime_config(), http, clock=lambda: deadline + offset
        )
        with pytest.raises(ValidatorInfrastructureError, match="deadline exceeded"):
            await _certify(runtime)
    assert calls == 0


async def test_canary_runtime_rejects_a_naive_clock_before_sending() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(
            _runtime_config(), http, clock=lambda: _NOW.replace(tzinfo=None)
        )
        with pytest.raises(ValidatorInfrastructureError, match="clock is invalid"):
            await _certify(runtime)
    assert calls == 0


async def test_canary_runtime_deadline_closes_a_stalled_stream_without_retry() -> None:
    calls = 0
    body = _HangingBody()
    deadline = _authority().deadline

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            stream=body,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, timeout=30.0
    ) as http:
        runtime = CodingCanaryRuntime(
            _runtime_config("http://sandbox-docker:8000"),
            http,
            clock=lambda: deadline - timedelta(milliseconds=100),
        )
        started = time.monotonic()
        with pytest.raises(ValidatorInfrastructureError, match="deadline exceeded"):
            await asyncio.wait_for(_certify(runtime), timeout=5)
        assert time.monotonic() - started < 5
    assert calls == 1
    assert body.closed is True


async def test_canary_runtime_deadline_abandons_a_scorer_that_never_answers() -> None:
    calls = 0
    released = asyncio.Event()
    deadline = _authority().deadline

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
        raise AssertionError("unreachable")  # pragma: no cover

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, timeout=30.0
    ) as http:
        runtime = CodingCanaryRuntime(
            _runtime_config(),
            http,
            clock=lambda: deadline - timedelta(milliseconds=100),
        )
        with pytest.raises(ValidatorInfrastructureError, match="deadline exceeded"):
            await asyncio.wait_for(_certify(runtime), timeout=5)
    assert calls == 1
    assert released.is_set()


async def test_canary_runtime_cancellation_closes_the_stream_and_propagates() -> None:
    body = _HangingBody()
    streaming = asyncio.Event()

    def handler(_: httpx.Request) -> httpx.Response:
        streaming.set()
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
            stream=body,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False
    ) as http:
        runtime = CodingCanaryRuntime(_runtime_config(), http, clock=lambda: _NOW)
        task = asyncio.create_task(_certify(runtime))
        await asyncio.wait_for(streaming.wait(), timeout=5)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert body.closed is True
