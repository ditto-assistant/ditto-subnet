from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from ditto.api_models.coding import CodingCapabilityCertificationReceipt
from ditto.api_models.coding_certification_leases import (
    CodingCertificationLeaseAuthority,
    CodingCertificationLeaseResponse,
    CodingCertificationLeaseStatus,
)
from ditto.validator.coding_canary import CodingCanaryOutcome, CodingCanaryWorker
from ditto.validator.errors import PlatformError, PlatformInfrastructureError

_NOW = datetime(2026, 8, 30, 18, tzinfo=UTC)
_AGENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_LEASE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_UPLOAD = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def _authority(**updates: object) -> CodingCertificationLeaseAuthority:
    value: dict[str, object] = {
        "schema": "dittobench-coding-certification-lease-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "lease_id": _LEASE,
        "validator_hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
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
        screened_image_ref="ditto-screen/coding-cert-lease:latest",
        screened_image_upload_id=_UPLOAD,
        weight_eligible=False,
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


class _Platform:
    def __init__(self) -> None:
        self.issues = 0
        self.claims = 0
        self.issued = _lease()
        self.claimed = _lease(status=CodingCertificationLeaseStatus.CLAIMED)

    async def issue_coding_certification_lease(
        self, agent_id: UUID, *, bench_version: int
    ) -> CodingCertificationLeaseResponse | None:
        self.issues += 1
        assert agent_id == _AGENT
        assert bench_version == 12
        return self.issued

    async def claim_coding_certification_lease(
        self, lease_id: UUID
    ) -> CodingCertificationLeaseResponse:
        self.claims += 1
        assert lease_id == _LEASE
        return self.claimed


class _Runtime:
    def __init__(self) -> None:
        self.probes = 0
        self.certified: list[CodingCertificationLeaseResponse] = []
        self.available = True

    async def require_available(self) -> None:
        self.probes += 1
        if not self.available:
            raise PlatformInfrastructureError("coding canary runtime is unavailable")

    async def certify(
        self, lease: CodingCertificationLeaseResponse
    ) -> CodingCanaryOutcome:
        self.certified.append(lease)
        return CodingCanaryOutcome(
            authority=lease.authority,
            receipt=_receipt(),
            capabilities_revoked=True,
            harness_destroyed=True,
        )


@pytest.mark.asyncio
async def test_canary_worker_claims_issued_lease_then_runs_certifier() -> None:
    platform = _Platform()
    runtime = _Runtime()
    worker = CodingCanaryWorker(platform=platform, runtime=runtime, clock=lambda: _NOW)
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is True
    assert platform.issues == 1
    assert platform.claims == 1
    assert len(runtime.certified) == 1
    assert runtime.certified[0].status is CodingCertificationLeaseStatus.CLAIMED
    assert runtime.certified[0].authority.weight_eligible is False


@pytest.mark.asyncio
async def test_canary_worker_skips_ineligible_or_conflicted_issue() -> None:
    platform = _Platform()
    runtime = _Runtime()
    worker = CodingCanaryWorker(platform=platform, runtime=runtime, clock=lambda: _NOW)

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
    worker = CodingCanaryWorker(platform=platform, runtime=runtime, clock=lambda: _NOW)
    worker.offer(_AGENT, 12)
    assert await worker.run_once() is False
    assert platform.issues == 0
    assert platform.claims == 0
