"""Admin tests for the trusted hosted-v2 assignment operator path."""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime, timedelta
from pathlib import Path as _Path
from typing import Any
from unittest.mock import AsyncMock as _AsyncMock
from uuid import UUID, uuid4

import bittensor as _bittensor
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.coding_certification import (
    CodingCapabilityCertificationReceipt,
    CodingCertificationModelEvidence,
    CodingCertificationModelUsageStatus,
    CodingCertificationStatus,
    CodingCertificationTerminalDomain,
    coding_certification_receipt_digest,
    coding_certification_signing_message,
)
from ditto.api_models.coding_inference import (
    CodingInferencePolicy,
    CodingInferenceProviderSettlement,
    CodingInferenceReceiptSet,
    coding_inference_digest,
)
from ditto.api_models.core_qualification import CoreQualificationPolicy
from ditto.api_server.dependencies import get_chain_client
from ditto.api_server.endpoints import admin_coding_hosted_assignments as endpoint
from ditto.api_server.endpoints import (
    validator_coding_certification as receipt_endpoint,
)
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingCertificationInferenceGrant,
    CodingCertificationInferenceRequest,
    CodingHostedAssignment,
    CodingHostedPrivateTask,
    CoreQualificationObservation,
)
from ditto.db.queries.coding_certification_inference_grants import (
    activate_coding_certification_inference_grant,
    ensure_coding_certification_inference_grant,
    revoke_coding_certification_inference_grant,
)
from ditto.db.queries.coding_certification_inference_requests import (
    begin_coding_certification_inference_request,
    settle_coding_certification_inference_request,
)
from ditto.db.queries.coding_certification_leases import (
    claim_coding_certification_lease,
    issue_coding_certification_lease,
)
from ditto.db.queries.coding_certifications import _receipt_from_settlement
from ditto.db.queries.coding_inference_requests import CodingInferenceDispatchAuthority
from ditto.db.queries.coding_private_v2_releases import insert_private_v2_release
from ditto.db.queries.core_qualification import insert_core_qualification_policy
from ditto.tests.api_server.endpoints.test_admin_coding_private_v2_releases import (
    _HEADERS,
    _install,
    _publication_receipt,
    _registration,
)

_URL = "/api/v1/admin/coding-hosted-assignments"
_BENCH = 12
_VALIDATOR = "5" + "V" * 47
_ARTIFACT = "a" * 64
_IMAGE = "b" * 64


async def _seed(
    maker: async_sessionmaker[AsyncSession],
    *,
    certified: bool = True,
    certification_contract_version: int = 1,
) -> tuple[UUID, UUID, str]:
    receipt = _publication_receipt(Ed25519PrivateKey.generate())
    registration = _registration(receipt)
    agent_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        release = await insert_private_v2_release(
            session,
            registration=registration,
            receipt=receipt,
            reason="synthetic registration",
            actor="test-operator",
        )
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="synthetic-miner",
                name="synthetic-agent",
                sha256=_ARTIFACT,
                status=AgentStatus.EVALUATING,
                screened_image_sha256=_IMAGE,
                screened_image_size_bytes=1,
                screened_image_id="sha256:" + "c" * 64,
                screened_image_ref="synthetic-image",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
            )
        )
        await session.flush()
        if certified:
            session.add(
                CodingCapabilityCertification(
                    certification_row_id=uuid4(),
                    agent_id=agent_id,
                    artifact_sha256=_ARTIFACT,
                    screened_image_sha256=_IMAGE,
                    validator_hotkey=_VALIDATOR,
                    bench_version=_BENCH,
                    settlement_generation=1,
                    settlement_inference_grant_sha256="5a" * 32,
                    settlement_provider_receipt_set_sha256="5b" * 32,
                    ticket_deadline=now + timedelta(hours=1),
                    coding_contract_version=certification_contract_version,
                    certification_id="operator-path-cert-001",
                    status="certified",
                    failure_stage=None,
                    failure_code=None,
                    certification_sha256="55" * 32,
                    canary_manifest_sha256="56" * 32,
                    transcript_object_key="sha256/" + "57" * 32,
                    frozen_submission_object_key="sha256/" + "58" * 32,
                    issued_at=now - timedelta(minutes=5),
                    expires_at=now + timedelta(hours=2),
                    weight_eligible=False,
                    receipt={},
                    signature="59" * 64,
                    created_at=now,
                )
            )
    return agent_id, release.row.release_row_id, registration.registration_sha256


def _subject(agent_id: UUID, release_row_id: UUID) -> dict[str, object]:
    return {
        "agent_id": str(agent_id),
        "release_row_id": str(release_row_id),
        "catalog_index": 0,
        "validator_hotkey": _VALIDATOR,
        "policy_sha256": "2" * 64,
        "execution_profile_sha256": "3" * 64,
        "grading_profile_sha256": "4" * 64,
        "max_patch_bytes": 1 << 20,
    }


async def _count(maker: async_sessionmaker[AsyncSession], model: type) -> int:
    async with maker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture(autouse=True)
def _fixed_bench_version(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _active(_session: AsyncSession, **_kwargs: object) -> int:
        return _BENCH

    monkeypatch.setattr(endpoint, "active_bench_version", _active)


@pytest.mark.asyncio
async def test_hosted_assignment_endpoints_require_admin(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, _ = await _seed(session_maker)
    body = _subject(agent_id, release_row_id)
    assert (await client.post(f"{_URL}/preview", json=body)).status_code == 401
    assert (await client.post(_URL, json=body)).status_code in (401, 422)


@pytest.mark.asyncio
async def test_preview_derives_authority_server_side_and_writes_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, registration_sha256 = await _seed(session_maker)

    preview = await client.post(
        f"{_URL}/preview", headers=_HEADERS, json=_subject(agent_id, release_row_id)
    )
    assert preview.status_code == 200, preview.text
    assert preview.headers["cache-control"] == "no-store"
    plan = preview.json()
    assert plan["artifact_sha256"] == _ARTIFACT
    assert plan["screened_image_sha256"] == _IMAGE
    assert plan["registration_sha256"] == registration_sha256
    assert plan["bench_version"] == _BENCH
    assert plan["selection"]["catalog_index"] == 0
    assert plan["authority"]["selection_sha256"] == plan["selection_sha256"]
    assert plan["confirmation"] == (
        "CREATE SHADOW CODING HOSTED ASSIGNMENT "
        f"{plan['evaluation_id']} {plan['assignment_sha256']}"
    )
    assert plan["shadow_only"] is True and plan["weight_eligible"] is False
    assert await _count(session_maker, CodingHostedAssignment) == 0
    assert await _count(session_maker, CodingHostedPrivateTask) == 0


@pytest.mark.asyncio
async def test_preview_refuses_subject_without_active_certification(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, _ = await _seed(session_maker, certified=False)

    refused = await client.post(
        f"{_URL}/preview", headers=_HEADERS, json=_subject(agent_id, release_row_id)
    )
    assert refused.status_code == 409
    assert "certification" in refused.json()["message"]


@pytest.mark.asyncio
async def test_preview_consumes_only_the_supported_v1_certification_contract(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """The supported lease/receipt path only ever writes contract-v1 rows."""

    _install(app, session_maker)
    agent_id, release_row_id, _ = await _seed(
        session_maker, certification_contract_version=2
    )

    refused = await client.post(
        f"{_URL}/preview", headers=_HEADERS, json=_subject(agent_id, release_row_id)
    )
    assert refused.status_code == 409
    assert "certification" in refused.json()["message"]


@pytest.mark.asyncio
async def test_create_requires_exact_confirmation_then_creates_and_binds(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id, _ = await _seed(session_maker)
    subject = _subject(agent_id, release_row_id)
    plan = (await client.post(f"{_URL}/preview", headers=_HEADERS, json=subject)).json()
    create = {
        **subject,
        "evaluation_id": plan["evaluation_id"],
        "attempt_id": plan["attempt_id"],
        "deadline_unix": plan["deadline_unix"],
        "confirmed_assignment_sha256": plan["assignment_sha256"],
        "reason": "synthetic operator canary assignment",
        "actor": "test-operator",
        "confirmation": plan["confirmation"],
    }

    wrong = await client.post(
        _URL, headers=_HEADERS, json={**create, "confirmation": "CREATE ASSIGNMENT"}
    )
    assert wrong.status_code == 422
    assert await _count(session_maker, CodingHostedAssignment) == 0

    tampered = await client.post(
        _URL,
        headers=_HEADERS,
        json={**create, "confirmed_assignment_sha256": "f" * 64},
    )
    assert tampered.status_code == 422
    assert await _count(session_maker, CodingHostedAssignment) == 0

    created = await client.post(_URL, headers=_HEADERS, json=create)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["assignment_sha256"] == plan["assignment_sha256"]

    async with session_maker() as session:
        row = await session.get(CodingHostedAssignment, UUID(plan["evaluation_id"]))
        task = await session.get(CodingHostedPrivateTask, UUID(plan["evaluation_id"]))
    assert row is not None and row.assignment_sha256 == plan["assignment_sha256"]
    assert row.artifact_sha256 == _ARTIFACT
    assert row.shadow_only is True and row.weight_eligible is False
    assert task is not None and task.selection_sha256 == plan["selection_sha256"]
    assert str(task.authoring_grant_id) == body["authoring_grant_id"]

    replay = await client.post(_URL, headers=_HEADERS, json=create)
    assert replay.status_code == 200, replay.text
    assert replay.json()["authoring_grant_id"] == body["authoring_grant_id"]
    assert await _count(session_maker, CodingHostedAssignment) == 1
    assert await _count(session_maker, CodingHostedPrivateTask) == 1


# --- Real contract-v1 certification through the supported lease/receipt path ---

_ALICE = _bittensor.Keypair.create_from_uri("//Alice")
_POLICY_VECTOR = (
    _Path(__file__).parents[6]
    / "packages/dittobench-coding-contract/testdata/coding_inference_policy_v1.json"
)


async def _seed_certifiable_subject(
    maker: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID]:
    receipt = _publication_receipt(Ed25519PrivateKey.generate())
    registration = _registration(receipt)
    agent_id = uuid4()
    now = datetime.now(UTC)
    async with maker() as session, session.begin():
        release = await insert_private_v2_release(
            session,
            registration=registration,
            receipt=receipt,
            reason="synthetic registration",
            actor="test-operator",
        )
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=_bittensor.Keypair.create_from_uri(
                    "//Charlie"
                ).ss58_address,
                name="hosted-canary-certified-agent",
                sha256=_ARTIFACT,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256=_IMAGE,
                screened_image_size_bytes=1234,
                screened_image_id="sha256:" + "ef" * 32,
                screened_image_ref=f"ditto-screen/{agent_id}:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=now,
                created_at=now,
            )
        )
        await session.flush()
        policy = await insert_core_qualification_policy(
            session,
            parent_revision=0,
            policy=CoreQualificationPolicy(
                schema="ditto-core-qualification-policy-v1",
                weight_eligible=False,
                bench_version=_BENCH,
                enter_composite=0.8,
                enter_tool_mean=0.8,
                enter_memory_mean=0.8,
                exit_composite=0.7,
                exit_tool_mean=0.7,
                exit_memory_mean=0.7,
                enter_observations=2,
                exit_observations=2,
            ),
            reason="start shadow qualification",
            actor="test-admin",
        )
        session.add(
            CoreQualificationObservation(
                observation_id=uuid4(),
                agent_id=agent_id,
                artifact_sha256=_ARTIFACT,
                screened_image_sha256=_IMAGE,
                bench_version=_BENCH,
                policy_revision=policy.revision,
                policy_checksum=policy.checksum,
                score_evidence_sha256="11" * 32,
                score_count=3,
                full_size=True,
                complete_wave=True,
                score_evidence={"scores": []},
                median_composite=0.9,
                median_tool_mean=0.9,
                median_memory_mean=0.9,
                entry_passed=True,
                retention_passed=True,
                qualified=True,
                enter_streak=2,
                exit_streak=0,
                decision="entered",
                source="score_commit",
                actor=None,
                reason=None,
                weight_eligible=False,
                observed_at=now,
            )
        )
    return agent_id, release.row.release_row_id


async def _certify_through_supported_path(
    app: FastAPI,
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    agent_id: UUID,
) -> UUID:
    vector = _json.loads(_POLICY_VECTOR.read_text(encoding="utf-8"))
    policy = CodingInferencePolicy.model_validate(vector["policy"])
    template = CodingInferenceProviderSettlement.model_validate_json(
        _json.dumps(vector["provider_settlements"]["complete"][0])
    )
    validator = _ALICE.ss58_address

    async with maker() as session, session.begin():
        issued = await issue_coding_certification_lease(
            session, validator_hotkey=validator, agent_id=agent_id, bench_version=_BENCH
        )
    lease_id = issued.row.lease_id
    authority = issued.authority
    async with maker() as session, session.begin():
        await claim_coding_certification_lease(
            session, validator_hotkey=validator, lease_id=lease_id
        )
    async with maker() as session, session.begin():
        ensured = await ensure_coding_certification_inference_grant(
            session, lease_id=lease_id, validator_hotkey=validator, policy=policy
        )
    grant_id = ensured.grant.grant_id
    async with maker() as session, session.begin():
        activation = await activate_coding_certification_inference_grant(
            session,
            grant_id=grant_id,
            validator_hotkey=validator,
            broker_public_key="A" * 43,
            policy=policy,
        )
    grant = activation.grant
    settlement = CodingInferenceProviderSettlement.model_validate_json(
        _json.dumps(
            {
                **template.model_dump(mode="json", by_alias=True),
                "ticket_id": str(lease_id),
                "grant_id": str(grant_id),
                "generation": grant.generation,
                "case_id": grant.case_id,
                "profile_capability_id": grant.profile_capability_id,
                "inference_grant_sha256": grant.inference_grant_sha256,
                "request_id": str(uuid4()),
                "request_sequence": 1,
                "attempt": 1,
                "locked_request_sha256": "12" * 32,
            }
        )
    )
    dispatch = CodingInferenceDispatchAuthority(
        grant_id=grant_id,
        ticket_id=lease_id,
        generation=grant.generation,
        sequence=1,
        request_sequence=1,
        attempt=1,
        request_id=settlement.request_id,
        case_id=grant.case_id,
        profile_capability_id=grant.profile_capability_id,
        inference_grant_sha256=grant.inference_grant_sha256,
        locked_request_sha256=settlement.locked_request_sha256,
    )
    async with maker() as session, session.begin():
        await begin_coding_certification_inference_request(
            session, authority=dispatch, bearer=activation.bearer
        )
    async with maker() as session, session.begin():
        await settle_coding_certification_inference_request(
            session, authority=dispatch, settlement=settlement, policy=policy
        )
    async with maker() as session, session.begin():
        await revoke_coding_certification_inference_grant(
            session,
            grant_id=grant_id,
            validator_hotkey=validator,
            generation=grant.generation,
        )

    prompt_sha, tool_sha = "21" * 32, "22" * 32
    async with maker() as session:
        settled = await session.get(CodingCertificationInferenceGrant, grant_id)
        rows = list(
            (
                await session.scalars(
                    select(CodingCertificationInferenceRequest)
                    .where(CodingCertificationInferenceRequest.lease_id == lease_id)
                    .order_by(CodingCertificationInferenceRequest.sequence.asc())
                )
            ).all()
        )
    assert settled is not None and rows
    receipts = [
        _receipt_from_settlement(
            CodingInferenceProviderSettlement.model_validate_json(
                row.provider_settlement_json or ""
            ),
            sequence=row.sequence,
            prompt_sha256=prompt_sha,
            tool_schema_sha256=tool_sha,
            settlement_sha256=row.provider_settlement_sha256 or "",
        )
        for row in rows
    ]
    receipt_set_digest = coding_inference_digest(
        CodingInferenceReceiptSet.model_validate_json(
            _json.dumps(
                {
                    "schema": "dittobench-coding-inference-receipt-set-v1",
                    "coding_contract_version": 1,
                    "ticket_id": str(settled.lease_id),
                    "case_id": settled.case_id,
                    "profile_capability_id": settled.profile_capability_id,
                    "grant_id": str(settled.grant_id),
                    "generation": settled.generation,
                    "inference_grant_sha256": settled.inference_grant_sha256,
                    "request_budget": settled.request_budget,
                    "prompt_token_budget": settled.prompt_token_budget,
                    "completion_token_budget": settled.completion_token_budget,
                    "receipts": [
                        item.model_dump(mode="json", by_alias=True) for item in receipts
                    ],
                }
            )
        )
    )
    evidence = CodingCertificationModelEvidence(
        model=policy.model,
        provider=policy.provider_api,
        provider_route_profile=policy.provider_route_profile,
        reasoning_effort="medium",
        inference_grant_sha256=settled.inference_grant_sha256,
        prompt_sha256=prompt_sha,
        tool_schema_sha256=tool_sha,
        usage_status=CodingCertificationModelUsageStatus.COMPLETE,
        fallback_used=False,
        cost_source="provider_receipt_v1",
        currency="USD",
        provider_receipt_set_sha256=receipt_set_digest,
        requests=settled.request_count,
        prompt_tokens=settled.prompt_tokens,
        completion_tokens=settled.completion_tokens,
        total_tokens=settled.prompt_tokens + settled.completion_tokens,
        cost_usd_micros=settled.cost_usd_micros,
        retry_count=0,
    )
    now = int(datetime.now(UTC).timestamp())
    fields: dict[str, Any] = {
        "schema_name": "dittobench-coding-capability-certification-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "certification_id": "hosted-canary-e2e-001",
        "agent_artifact_sha256": _ARTIFACT,
        "harness_instance_id": "hosted-canary-harness-001",
        "canary_manifest_sha256": authority.canary_manifest_sha256,
        "issued_at_unix": now,
        "expires_at_unix": now + 7200,
        "status": CodingCertificationStatus.CERTIFIED,
        "failure_stage": None,
        "failure_code": None,
        "supported_coding_contract_versions": [1],
        "capabilities": [
            "case_scoped_inference_v1",
            "coding_runner_tools_v1",
            "scoped_memory_seed_v1",
        ],
        "memory_bundle_sha256": "31" * 32,
        "visible_bundle_sha256": "32" * 32,
        "base_tree_sha256": "33" * 32,
        "inference_grant_sha256": settled.inference_grant_sha256,
        "model_evidence": evidence,
        "frozen_patch_sha256": "34" * 32,
        "frozen_submission_object_key": "sha256/" + "34" * 32,
        "changed_path_root": "35" * 32,
        "final_tree_sha256": "36" * 32,
        "authoring_event_root": "37" * 32,
        "authoring_transcript_sha256": "38" * 32,
        "authoring_transcript_object_key": "sha256/" + "38" * 32,
        "authoring_transcript_bytes": 1024,
        "authoring_event_count": 4,
        "protected_paths_intact": True,
        "canary_terminal_domain": CodingCertificationTerminalDomain.RESOLVED,
        "grader_plan_sha256": authority.grader_plan_sha256,
        "grader_execution_receipt_root_sha256": "39" * 32,
        "certification_sha256": "0" * 64,
    }
    draft = CodingCapabilityCertificationReceipt.model_construct(**fields)
    receipt = CodingCapabilityCertificationReceipt.model_validate(
        {
            **draft.model_dump(mode="json", by_alias=True),
            "certification_sha256": coding_certification_receipt_digest(draft),
        }
    )
    signature = _ALICE.sign(
        coding_certification_signing_message(
            validator_hotkey=validator,
            agent_id=agent_id,
            bench_version=_BENCH,
            lease_id=lease_id,
            screened_image_sha256=_IMAGE,
            certification_sha256=receipt.certification_sha256,
        )
    ).hex()

    async def _chain() -> object:
        return object()

    app.dependency_overrides[get_chain_client] = _chain
    monkeypatch.setattr(
        receipt_endpoint, "_assert_validator_permitted", _AsyncMock(return_value=None)
    )
    submitted = await client.post(
        f"/api/v1/validator/agent/{agent_id}/coding-certification",
        json={
            "validator_hotkey": validator,
            "bench_version": _BENCH,
            "lease_id": str(lease_id),
            "screened_image_sha256": _IMAGE,
            "receipt": receipt.model_dump(mode="json", by_alias=True),
            "signature": signature,
        },
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["accepted"] is True and body["active"] is True
    async with maker() as session:
        row = await session.scalar(
            select(CodingCapabilityCertification).where(
                CodingCapabilityCertification.lease_id == lease_id
            )
        )
    assert row is not None
    assert row.coding_contract_version == 1
    assert row.settlement_generation is not None
    return row.certification_row_id


@pytest.mark.asyncio
async def test_real_v1_certification_through_supported_path_admits_assignment(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(app, session_maker)
    agent_id, release_row_id = await _seed_certifiable_subject(session_maker)
    certification_row_id = await _certify_through_supported_path(
        app, client, session_maker, monkeypatch, agent_id
    )
    subject = {
        **_subject(agent_id, release_row_id),
        "validator_hotkey": _ALICE.ss58_address,
    }

    preview = await client.post(f"{_URL}/preview", headers=_HEADERS, json=subject)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["certification_row_id"] == str(certification_row_id)
    assert plan["artifact_sha256"] == _ARTIFACT

    created = await client.post(
        _URL,
        headers=_HEADERS,
        json={
            **subject,
            "evaluation_id": plan["evaluation_id"],
            "attempt_id": plan["attempt_id"],
            "deadline_unix": plan["deadline_unix"],
            "confirmed_assignment_sha256": plan["assignment_sha256"],
            "reason": "real v1 certification canary assignment",
            "actor": "test-operator",
            "confirmation": plan["confirmation"],
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["certification_row_id"] == str(certification_row_id)
