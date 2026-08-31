from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ditto.api_models.coding_certification import (
    CodingCapabilityCertificationReceipt,
    _canonical_json_bytes,
)
from ditto.api_models.coding_inference import (
    CodingInferencePolicy,
    CodingInferenceProviderSettlement,
    CodingInferenceReceiptSet,
    coding_inference_digest,
    policy_digest,
    provider_settlement_digest,
)
from ditto.db.models import (
    CodingCertificationInferenceGrant,
    CodingCertificationInferenceRequest,
)
from ditto.db.queries.coding_certifications import (
    CodingCertificationSettlementError,
    _receipt_from_settlement,
    require_coding_certification_settlement,
)

_ROOT = Path(__file__).parents[6]
_POLICY_PATH = (
    _ROOT
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)
_RECEIPT_PATH = (
    _ROOT / "packages/dittobench-coding-contract/testdata/coding_certification_v1.json"
)
_NOW = datetime(2026, 8, 31, 18, tzinfo=UTC)
_LEASE = UUID("77777777-7777-4777-8777-777777777777")


def _finalize_receipt(
    vector: dict[str, object],
) -> CodingCapabilityCertificationReceipt:
    payload = json.loads(json.dumps(vector))
    payload.pop("certification_sha256", None)
    payload["certification_sha256"] = hashlib.sha256(
        _canonical_json_bytes(payload)
    ).hexdigest()
    return CodingCapabilityCertificationReceipt.model_validate(payload)


def _policy_and_settlement() -> tuple[
    CodingInferencePolicy, CodingInferenceProviderSettlement
]:
    vector = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    policy = CodingInferencePolicy.model_validate(vector["policy"])
    raw = dict(vector["provider_settlements"]["complete"][0])
    raw["ticket_id"] = str(_LEASE)
    return policy, CodingInferenceProviderSettlement.model_validate_json(
        json.dumps(raw)
    )


def _grant(
    policy: CodingInferencePolicy, settlement: CodingInferenceProviderSettlement
) -> CodingCertificationInferenceGrant:
    return CodingCertificationInferenceGrant(
        grant_id=settlement.grant_id,
        lease_id=settlement.ticket_id,
        validator_hotkey="5" + "V" * 47,
        case_id=settlement.case_id,
        profile_capability_id=settlement.profile_capability_id,
        inference_grant_sha256=policy_digest(policy),
        model=policy.model,
        provider_api=policy.provider_api,
        provider_route=policy.provider_route,
        receipt_provider=policy.receipt_provider,
        provider_route_profile=policy.provider_route_profile,
        provider_account_guardrail=policy.provider_account_guardrail,
        provider_pipeline_policy=policy.provider_pipeline_policy,
        provider_cache_policy=policy.provider_cache_policy,
        reasoning_effort=policy.reasoning_effort,
        status="revoked",
        bearer_digest=None,
        revoke_bearer_digest="dd" * 32,
        broker_public_key=None,
        generation=settlement.generation,
        request_budget=32,
        prompt_token_budget=10_000,
        completion_token_budget=2_000,
        cost_budget_usd_micros=policy.max_cost_usd_micros,
        request_count=1,
        prompt_tokens=settlement.prompt_tokens,
        completion_tokens=settlement.completion_tokens,
        cost_usd_micros=settlement.cost_usd_micros,
        active_requests=0,
        expires_at=_NOW + timedelta(minutes=20),
        revoked_at=_NOW,
        weight_eligible=False,
        created_at=_NOW - timedelta(minutes=1),
        updated_at=_NOW,
    )


def _request(
    grant: CodingCertificationInferenceGrant,
    settlement: CodingInferenceProviderSettlement,
    policy: CodingInferencePolicy,
    *,
    status: str = "complete",
) -> CodingCertificationInferenceRequest:
    return CodingCertificationInferenceRequest(
        request_row_id=uuid4(),
        grant_id=grant.grant_id,
        lease_id=grant.lease_id,
        generation=grant.generation,
        sequence=1,
        request_sequence=settlement.request_sequence,
        attempt=settlement.attempt,
        request_id=settlement.request_id,
        case_id=grant.case_id,
        profile_capability_id=grant.profile_capability_id,
        inference_grant_sha256=grant.inference_grant_sha256,
        locked_request_sha256=settlement.locked_request_sha256,
        status=status,
        provider_settlement_sha256=(
            None
            if status == "started"
            else provider_settlement_digest(settlement, policy)
        ),
        provider_generation_id=(
            None if status == "started" else settlement.provider_generation_id
        ),
        provider_settlement_json=(
            None if status == "started" else settlement.model_dump_json(by_alias=True)
        ),
        unsettled_reason=None,
        started_at=_NOW,
        settled_at=None if status == "started" else _NOW,
        weight_eligible=False,
    )


def _unused_receipt() -> CodingCapabilityCertificationReceipt:
    vector = json.loads(_RECEIPT_PATH.read_text(encoding="utf-8"))["receipt"]
    vector["status"] = "failed"
    vector["failure_stage"] = "run"
    vector["failure_code"] = "coding_inference_not_observed"
    vector["model_evidence"] = None
    vector["issued_at_unix"] = int(_NOW.timestamp())
    vector["expires_at_unix"] = int(_NOW.timestamp()) + 3600
    return _finalize_receipt(vector)


def _certified_receipt(
    grant: CodingCertificationInferenceGrant,
    request: CodingCertificationInferenceRequest,
    settlement: CodingInferenceProviderSettlement,
    policy: CodingInferencePolicy,
) -> CodingCapabilityCertificationReceipt:
    reconstructed = _receipt_from_settlement(
        settlement,
        sequence=request.sequence,
        prompt_sha256=policy.prompt_sha256,
        tool_schema_sha256=policy.tool_schema_sha256,
        settlement_sha256=request.provider_settlement_sha256 or "",
    )
    receipt_set = CodingInferenceReceiptSet.model_validate_json(
        json.dumps(
            {
                "schema": "dittobench-coding-inference-receipt-set-v1",
                "coding_contract_version": 1,
                "ticket_id": str(grant.lease_id),
                "case_id": grant.case_id,
                "profile_capability_id": grant.profile_capability_id,
                "grant_id": str(grant.grant_id),
                "generation": grant.generation,
                "inference_grant_sha256": grant.inference_grant_sha256,
                "request_budget": grant.request_budget,
                "prompt_token_budget": grant.prompt_token_budget,
                "completion_token_budget": grant.completion_token_budget,
                "receipts": [reconstructed.model_dump(mode="json", by_alias=True)],
            }
        )
    )
    vector = json.loads(_RECEIPT_PATH.read_text(encoding="utf-8"))["receipt"]
    vector["issued_at_unix"] = int(_NOW.timestamp())
    vector["expires_at_unix"] = int(_NOW.timestamp()) + 3600
    vector["inference_grant_sha256"] = grant.inference_grant_sha256
    vector["model_evidence"] = {
        "model": policy.model,
        "provider": policy.provider_route,
        "provider_route_profile": policy.provider_route_profile,
        "reasoning_effort": policy.reasoning_effort,
        "inference_grant_sha256": grant.inference_grant_sha256,
        "prompt_sha256": policy.prompt_sha256,
        "tool_schema_sha256": policy.tool_schema_sha256,
        "usage_status": "complete",
        "fallback_used": False,
        "cost_source": "provider_receipt_v1",
        "currency": "USD",
        "provider_receipt_set_sha256": coding_inference_digest(receipt_set),
        "requests": 1,
        "prompt_tokens": settlement.prompt_tokens,
        "completion_tokens": settlement.completion_tokens,
        "total_tokens": settlement.total_tokens,
        "cost_usd_micros": settlement.cost_usd_micros,
        "retry_count": 0,
    }
    return _finalize_receipt(vector)


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return list(self._rows)


class _Session:
    def __init__(self, grant: object, rows: list[object]) -> None:
        self.grant = grant
        self.rows = rows

    async def scalar(self, statement):
        del statement
        return self.grant

    async def scalars(self, statement):
        del statement
        return _ScalarResult(self.rows)


async def test_unused_inference_persists_without_settlement() -> None:
    await require_coding_certification_settlement(
        _Session(None, []),  # type: ignore[arg-type]
        lease_id=_LEASE,
        receipt=_unused_receipt(),
    )


async def test_unused_inference_rejects_observed_settlement() -> None:
    policy, settlement = _policy_and_settlement()
    grant = _grant(policy, settlement)
    request = _request(grant, settlement, policy)
    with pytest.raises(CodingCertificationSettlementError, match="unused inference"):
        await require_coding_certification_settlement(
            _Session(grant, [request]),  # type: ignore[arg-type]
            lease_id=_LEASE,
            receipt=_unused_receipt(),
        )


async def test_certified_receipt_requires_matching_settlement() -> None:
    policy, settlement = _policy_and_settlement()
    grant = _grant(policy, settlement)
    request = _request(grant, settlement, policy)
    receipt = _certified_receipt(grant, request, settlement, policy)
    binding = await require_coding_certification_settlement(
        _Session(grant, [request]),  # type: ignore[arg-type]
        lease_id=_LEASE,
        receipt=receipt,
    )
    assert receipt.model_evidence is not None
    assert binding is not None
    assert binding.generation == grant.generation
    assert binding.inference_grant_sha256 == grant.inference_grant_sha256
    assert binding.provider_receipt_set_sha256 == (
        receipt.model_evidence.provider_receipt_set_sha256
    )


async def test_certified_receipt_rejects_missing_and_unsettled_ledger() -> None:
    policy, settlement = _policy_and_settlement()
    grant = _grant(policy, settlement)
    request = _request(grant, settlement, policy)
    receipt = _certified_receipt(grant, request, settlement, policy)
    with pytest.raises(CodingCertificationSettlementError, match="missing"):
        await require_coding_certification_settlement(
            _Session(None, []),  # type: ignore[arg-type]
            lease_id=_LEASE,
            receipt=receipt,
        )
    started = _request(grant, settlement, policy, status="started")
    with pytest.raises(CodingCertificationSettlementError, match="unsettled"):
        await require_coding_certification_settlement(
            _Session(grant, [started]),  # type: ignore[arg-type]
            lease_id=_LEASE,
            receipt=receipt,
        )


async def test_certified_receipt_rejects_active_grant_after_settlement() -> None:
    policy, settlement = _policy_and_settlement()
    grant = _grant(policy, settlement)
    request = _request(grant, settlement, policy)
    receipt = _certified_receipt(grant, request, settlement, policy)
    grant.status = "active"
    with pytest.raises(CodingCertificationSettlementError, match="missing"):
        await require_coding_certification_settlement(
            _Session(grant, [request]),  # type: ignore[arg-type]
            lease_id=_LEASE,
            receipt=receipt,
        )


async def test_certified_receipt_rejects_accounting_drift() -> None:
    policy, settlement = _policy_and_settlement()
    grant = _grant(policy, settlement)
    request = _request(grant, settlement, policy)
    receipt = _certified_receipt(grant, request, settlement, policy)
    grant.prompt_tokens += 1
    with pytest.raises(CodingCertificationSettlementError, match="disagrees"):
        await require_coding_certification_settlement(
            _Session(grant, [request]),  # type: ignore[arg-type]
            lease_id=_LEASE,
            receipt=receipt,
        )
