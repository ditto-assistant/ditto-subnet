"""Endpoint tests for signed shadow coding-result persistence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import bittensor
import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.coding_catalog import (
    CodingCatalogCommitment,
    CodingCatalogTaskExposure,
)
from ditto.api_models.coding_evaluation import (
    CodingAuthoringEvidence,
    CodingRunEvidence,
    CodingShadowRunAuthority,
    coding_authoring_evidence_digest,
    coding_authoring_freeze_signing_message,
    coding_run_evidence_digest,
    coding_shadow_result_signing_message,
)
from ditto.api_models.core_qualification import CoreQualificationPolicy
from ditto.api_server.dependencies import get_chain_client, get_session
from ditto.api_server.endpoints import validator_coding_evaluation as endpoint_module
from ditto.api_server.endpoints import (
    validator_coding_freezes as freeze_endpoint_module,
)
from ditto.chain.models import BlockInfo
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingShadowAuthoringFreeze,
    CodingShadowResult,
    CodingShadowRunIssuance,
    CodingShadowTicket,
    Score,
)
from ditto.db.queries.coding_assignments import (
    CodingAssignmentPolicy,
    create_coding_selection_assignment,
)
from ditto.db.queries.coding_catalog import (
    expose_coding_shadow_run_tasks,
    insert_coding_catalog_release,
)
from ditto.db.queries.coding_evaluations import (
    insert_coding_authoring_freeze,
    insert_coding_shadow_result,
    insert_coding_shadow_run,
    issue_coding_shadow_ticket,
)
from ditto.db.queries.core_qualification import (
    insert_core_qualification_policy,
    observe_core_qualification,
)
from ditto.db.queries.scores import MIN_ELIGIBLE_CASES, upsert_score

_NOW = datetime.now(UTC)
_BENCH = 12
_KEYPAIR = bittensor.Keypair.create_from_uri("//Alice")
_VALIDATOR = _KEYPAIR.ss58_address
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_TRANSCRIPT_BYTES = 4096
_AUTHORING_EVENTS = 4


class _FinalizedBlocks:
    async def get_finalized_block_hash(self, block_number: int) -> str:
        if block_number == 0:
            return "0x" + "22" * 32
        return "0x" + "33" * 32

    async def get_finalized_block(self) -> BlockInfo:
        return BlockInfo(number=103, hash="0x" + "ff" * 32)


def _catalog_commitment() -> CodingCatalogCommitment:
    values: dict[str, object] = {
        "schema": "dittobench-coding-catalog-commitment-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "corpus_release_id": "private-coding-corpus-v1",
        "catalog_merkle_root": "11" * 32,
        "selection_derivation_id": "coding-selection-v1",
        "selection_chain_genesis_hash": "0x" + "22" * 32,
        "grader_contract_sha256": "99" * 32,
        "inference_grant_sha256": "01" * 32,
        "task_version_count": 100,
        "curator_hotkey": _VALIDATOR,
        "committed_at_unix": int(_NOW.timestamp()),
    }
    body = (
        json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode()
    values["commitment_sha256"] = hashlib.sha256(body).hexdigest()
    return CodingCatalogCommitment.model_validate(values)


def _task_exposure() -> CodingCatalogTaskExposure:
    return CodingCatalogTaskExposure(
        manifest_index=0,
        task_version_id="private-task-v1",
        task_commitment_sha256="aa" * 32,
        selection_proof_sha256="ab" * 32,
        catalog_membership_proof_sha256="ac" * 32,
        visible_bundle_sha256="ee" * 32,
        base_tree_sha256="ff" * 32,
        memory_bundle_sha256=(
            "b943e6586a202b2ede36cee985e1ebb76d2dc0ab2734e30c174763f81bf51f53"
        ),
        environment_image_digest="sha256:" + "11" * 32,
        resource_profile_sha256=(
            "5d01f16bedb5af936e58d79f7ebc9ca0356dcadb89c06607ba27131a7d3ba8e6"
        ),
        grader_bundle_sha256="33" * 32,
        grader_image_digest="sha256:" + "44" * 32,
        test_manifest_sha256="55" * 32,
        grader_plan_sha256=(
            "cb517c8d7b85cfbe1277a78cf0124b0440544aae6b692ce78ff647b2b5570c3e"
        ),
    )


def _evidence(ticket_id) -> CodingRunEvidence:
    value = json.loads(
        (
            Path(__file__).parents[6]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_contract_v1.json"
        ).read_text(encoding="utf-8")
    )["run_evidence"]
    value["validator_ticket_id"] = str(ticket_id)
    return CodingRunEvidence.model_validate_json(json.dumps(value))


def _failed_evidence(ticket_id) -> CodingRunEvidence:
    value = _evidence(ticket_id).model_dump(mode="json", by_alias=True)
    value["tasks"][0]["terminal_domain"] = "repair_failure"
    value["tasks"][0]["repair_score_micros"] = 0
    value["resolved_count"] = 0
    value["repair_failure_count"] = 1
    value["repair_mean_micros"] = 0
    return CodingRunEvidence.model_validate_json(json.dumps(value))


def _infrastructure_evidence(ticket_id) -> CodingRunEvidence:
    value = _evidence(ticket_id).model_dump(mode="json", by_alias=True)
    value["tasks"][0]["terminal_domain"] = "validator_infrastructure"
    value["tasks"][0]["repair_score_micros"] = 0
    value["resolved_count"] = 0
    value["infrastructure_count"] = 1
    value["scoreable_task_count"] = 0
    value["repair_mean_micros"] = 0
    return CodingRunEvidence.model_validate_json(json.dumps(value))


def _authoring_evidence() -> CodingAuthoringEvidence:
    value = json.loads(
        (
            Path(__file__).parents[6]
            / "packages"
            / "dittobench-coding-contract"
            / "testdata"
            / "coding_authoring_freeze_v1.json"
        ).read_text(encoding="utf-8")
    )["request"]["evidence"]
    value["model"]["inference_grant_sha256"] = "01" * 32
    return CodingAuthoringEvidence.model_validate_json(json.dumps(value))


def _authority(agent_id) -> CodingShadowRunAuthority:
    return CodingShadowRunAuthority(
        schema="dittobench-coding-shadow-run-authority-v1",
        bench_family="coding",
        coding_contract_version=1,
        weight_eligible=False,
        bench_version=_BENCH,
        coding_run_id="coding-run-001",
        agent_id=agent_id,
        agent_artifact_sha256="ab" * 32,
        screened_image_sha256="cd" * 32,
        corpus_release_id="private-coding-corpus-v1",
        catalog_merkle_root="11" * 32,
        selection_derivation_id="coding-selection-v1",
        selection_chain_genesis_hash="0x" + "22" * 32,
        selection_block_number=123,
        selection_block_hash="0x" + "33" * 32,
        inference_grant_sha256="01" * 32,
        grader_contract_sha256="99" * 32,
        task_set_id="task-set-001",
        task_set_manifest_sha256="dd" * 32,
        run_manifest_sha256=(
            "e7b431a640aca1f35a5cabe7341ee0aaba25ccee9a174ef0c8f4ab0f3ff80dc4"
        ),
        task_count=1,
    )


def _install(
    app: FastAPI,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)

    async def _session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    async def _chain():
        return object()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_chain_client] = _chain
    monkeypatch.setattr(
        endpoint_module,
        "_assert_validator_permitted",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        freeze_endpoint_module,
        "_assert_validator_permitted",
        AsyncMock(return_value=None),
    )


async def _seed(
    maker: async_sessionmaker[AsyncSession],
):
    agent_id = uuid4()
    ticket_id = uuid4()
    deadline = _NOW + timedelta(hours=1)
    async with maker() as session, session.begin():
        await insert_coding_catalog_release(
            session,
            commitment=_catalog_commitment(),
            signature="88" * 64,
            reason="register endpoint shadow catalog",
            actor="test-admin",
        )
    agent_created_at = datetime.now(UTC)
    async with maker() as session, session.begin():
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey="5CodingEndpointMiner111111111111111111111111111111",
                name="coding-endpoint-agent",
                sha256="ab" * 32,
                status=AgentStatus.SCORED,
                screening_policy_version=9,
                screened_image_sha256="cd" * 32,
                screened_image_size_bytes=1234,
                screened_image_id="sha256:" + "ef" * 32,
                screened_image_ref="ditto-screen/coding-endpoint:latest",
                screened_image_upload_id=uuid4(),
                screened_image_verified_at=_NOW,
                created_at=agent_created_at,
            )
        )
        await insert_core_qualification_policy(
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
                enter_observations=1,
                exit_observations=2,
            ),
            reason="calibrate endpoint shadow qualification",
            actor="test-admin",
        )
        for index in range(3):
            await upsert_score(
                session,
                agent_id=agent_id,
                validator_hotkey=f"core-validator-{index}",
                bench_version=_BENCH,
                run_id=f"core-run-{index}",
                seed=index,
                composite=0.9,
                tool_mean=0.9,
                memory_mean=0.9,
                median_ms=100,
                n=MIN_ELIGIBLE_CASES,
                generated_at=_NOW,
                signature=(f"{index + 1:02x}" * 64),
            )
        observed = await observe_core_qualification(
            session,
            agent_id=agent_id,
            bench_version=_BENCH,
            now=_NOW,
        )
        assert observed is not None and observed.row.qualified
        session.add(
            CodingCapabilityCertification(
                certification_row_id=uuid4(),
                agent_id=agent_id,
                artifact_sha256="ab" * 32,
                screened_image_sha256="cd" * 32,
                validator_hotkey=_VALIDATOR,
                bench_version=_BENCH,
                settlement_generation=1,
                settlement_inference_grant_sha256="99" * 32,
                settlement_provider_receipt_set_sha256="aa" * 32,
                ticket_deadline=deadline,
                coding_contract_version=1,
                certification_id="cert-endpoint-shadow-001",
                status="certified",
                failure_stage=None,
                failure_code=None,
                certification_sha256="44" * 32,
                canary_manifest_sha256="55" * 32,
                transcript_object_key="sha256/" + "66" * 32,
                frozen_submission_object_key="sha256/" + "77" * 32,
                issued_at=_NOW - timedelta(minutes=5),
                expires_at=deadline + timedelta(minutes=10),
                weight_eligible=False,
                receipt={},
                signature="88" * 64,
                created_at=_NOW,
            )
        )
    async with maker() as session, session.begin():
        assignment = await create_coding_selection_assignment(
            session,
            finalized_source=_FinalizedBlocks(),
            agent_id=agent_id,
            bench_version=_BENCH,
            coding_run_id="coding-run-001",
            corpus_release_id="private-coding-corpus-v1",
            policy=CodingAssignmentPolicy(selection_delay_blocks=20),
        )
        run = await insert_coding_shadow_run(
            session,
            authority=_authority(agent_id),
        )
        await expose_coding_shadow_run_tasks(
            session,
            run_row_id=run.row.run_row_id,
            exposures=[_task_exposure()],
        )
        session.add(
            CodingShadowRunIssuance(
                assignment_row_id=assignment.row.assignment_row_id,
                run_row_id=run.row.run_row_id,
                assignment_sha256=assignment.row.assignment_sha256,
                agent_id=agent_id,
                artifact_sha256="ab" * 32,
                screened_image_sha256="cd" * 32,
                bench_version=_BENCH,
                coding_contract_version=1,
                coding_run_id="coding-run-001",
                corpus_release_id="private-coding-corpus-v1",
                selection_block_number=123,
                selection_block_hash="0x" + "33" * 32,
                selection_candidate_probe=0,
                selection_catalog_index=0,
                selection_proof_sha256="ab" * 32,
                selection_block_timestamp=(
                    assignment.row.created_at + timedelta(seconds=1)
                ),
                task_count=1,
                weight_eligible=False,
                issued_at=assignment.row.created_at + timedelta(seconds=2),
            )
        )
        await session.flush()
        ticket = await issue_coding_shadow_ticket(
            session,
            run_row_id=run.row.run_row_id,
            ticket_id=ticket_id,
            validator_hotkey=_VALIDATOR,
            issued_at=_NOW,
            deadline=deadline,
        )
        assert isinstance(ticket.row, CodingShadowTicket)
        ticket.row.claim_generation = 1
        ticket.row.claim_instance_id = "coding-endpoint-worker-001"
        ticket.row.claim_acquired_at = _NOW
        ticket.row.claim_heartbeat_at = _NOW
        ticket.row.claim_expires_at = deadline
        ticket.row.claim_started_at = _NOW
    return agent_id, run.row, ticket.row, deadline


async def _record_core_qualification_exit(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
) -> None:
    for wave in (1, 2):
        async with maker() as session, session.begin():
            for index in range(3):
                await upsert_score(
                    session,
                    agent_id=agent_id,
                    validator_hotkey=f"core-validator-{index}",
                    bench_version=_BENCH,
                    run_id=f"core-exit-{wave}-{index}",
                    seed=wave * 10 + index,
                    composite=0.5,
                    tool_mean=0.5,
                    memory_mean=0.5,
                    median_ms=100,
                    n=MIN_ELIGIBLE_CASES,
                    generated_at=_NOW + timedelta(minutes=wave),
                    signature=(f"{wave * 3 + index + 1:02x}" * 64),
                )
            observed = await observe_core_qualification(
                session,
                agent_id=agent_id,
                bench_version=_BENCH,
                now=_NOW + timedelta(minutes=wave),
            )
            assert observed is not None and observed.row.complete_wave
            assert observed.row.qualified is (wave == 1)


async def _remove_settlement_binding(
    maker: async_sessionmaker[AsyncSession],
    *,
    agent_id: UUID,
) -> None:
    """Model the pre-binding legacy row attached to an already-issued ticket."""

    async with maker() as session, session.begin():
        certification = await session.scalar(
            select(CodingCapabilityCertification).where(
                CodingCapabilityCertification.agent_id == agent_id,
                CodingCapabilityCertification.validator_hotkey == _VALIDATOR,
            )
        )
        assert certification is not None
        certification.settlement_generation = None
        certification.settlement_inference_grant_sha256 = None
        certification.settlement_provider_receipt_set_sha256 = None


async def test_unbound_legacy_certification_cannot_write_private_ticket_steps(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, monkeypatch)
    agent_id, run, ticket, deadline = await _seed(session_maker)
    await _remove_settlement_binding(session_maker, agent_id=agent_id)

    authoring = _authoring_evidence()
    authoring_digest = coding_authoring_evidence_digest(authoring)
    transcript_key = f"sha256/{authoring.authoring_transcript_sha256}"
    frozen_key = f"sha256/{authoring.frozen_patch_sha256}"
    freeze_message = coding_authoring_freeze_signing_message(
        validator_hotkey=_VALIDATOR,
        agent_id=agent_id,
        bench_version=_BENCH,
        run_row_id=run.run_row_id,
        ticket_id=ticket.ticket_id,
        ticket_deadline=deadline,
        coding_run_id=run.coding_run_id,
        agent_artifact_sha256="ab" * 32,
        screened_image_sha256="cd" * 32,
        run_manifest_sha256=run.run_manifest_sha256,
        task_set_manifest_sha256=run.task_set_manifest_sha256,
        authoring_evidence_sha256=authoring_digest,
        authoring_transcript_object_key=transcript_key,
        authoring_transcript_bytes=_TRANSCRIPT_BYTES,
        authoring_event_count=_AUTHORING_EVENTS,
        frozen_submission_object_key=frozen_key,
    )
    freeze = await client.post(
        "/api/v1/validator/coding-shadow/authoring-freeze",
        json={
            "validator_hotkey": _VALIDATOR,
            "agent_id": str(agent_id),
            "bench_version": _BENCH,
            "run_row_id": str(run.run_row_id),
            "ticket_id": str(ticket.ticket_id),
            "ticket_deadline": deadline.isoformat(),
            "coding_run_id": run.coding_run_id,
            "agent_artifact_sha256": "ab" * 32,
            "screened_image_sha256": "cd" * 32,
            "run_manifest_sha256": run.run_manifest_sha256,
            "task_set_manifest_sha256": run.task_set_manifest_sha256,
            "authoring_evidence_sha256": authoring_digest,
            "evidence": authoring.model_dump(mode="json"),
            "authoring_transcript_object_key": transcript_key,
            "authoring_transcript_bytes": _TRANSCRIPT_BYTES,
            "authoring_event_count": _AUTHORING_EVENTS,
            "frozen_submission_object_key": frozen_key,
            "signature": _KEYPAIR.sign(freeze_message).hex(),
        },
    )
    assert freeze.status_code == 409, freeze.text

    evidence = _evidence(ticket.ticket_id)
    result_digest = coding_run_evidence_digest(evidence)
    result_message = coding_shadow_result_signing_message(
        validator_hotkey=_VALIDATOR,
        agent_id=agent_id,
        run_row_id=run.run_row_id,
        ticket_id=ticket.ticket_id,
        bench_version=_BENCH,
        ticket_deadline=deadline,
        agent_artifact_sha256="ab" * 32,
        screened_image_sha256="cd" * 32,
        run_evidence_sha256=result_digest,
    )
    result = await client.post(
        f"/api/v1/validator/agent/{agent_id}/coding-shadow-result",
        json={
            "validator_hotkey": _VALIDATOR,
            "bench_version": _BENCH,
            "run_row_id": str(run.run_row_id),
            "ticket_id": str(ticket.ticket_id),
            "ticket_deadline": deadline.isoformat(),
            "agent_artifact_sha256": "ab" * 32,
            "screened_image_sha256": "cd" * 32,
            "run_evidence_sha256": result_digest,
            "evidence": evidence.model_dump(mode="json", by_alias=True),
            "signature": _KEYPAIR.sign(result_message).hex(),
        },
    )
    assert result.status_code == 409, result.text


async def test_signed_authoring_freeze_is_idempotent_and_operator_visible(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, monkeypatch)
    agent_id, run, ticket, deadline = await _seed(session_maker)
    evidence = _authoring_evidence()
    digest = coding_authoring_evidence_digest(evidence)
    transcript_key = f"sha256/{evidence.authoring_transcript_sha256}"
    frozen_key = f"sha256/{evidence.frozen_patch_sha256}"

    def payload_for(current: CodingAuthoringEvidence) -> dict:
        current_digest = coding_authoring_evidence_digest(current)
        message = coding_authoring_freeze_signing_message(
            validator_hotkey=_VALIDATOR,
            agent_id=agent_id,
            bench_version=_BENCH,
            run_row_id=run.run_row_id,
            ticket_id=ticket.ticket_id,
            ticket_deadline=deadline,
            coding_run_id=run.coding_run_id,
            agent_artifact_sha256="ab" * 32,
            screened_image_sha256="cd" * 32,
            run_manifest_sha256=run.run_manifest_sha256,
            task_set_manifest_sha256=run.task_set_manifest_sha256,
            authoring_evidence_sha256=current_digest,
            authoring_transcript_object_key=transcript_key,
            authoring_transcript_bytes=_TRANSCRIPT_BYTES,
            authoring_event_count=_AUTHORING_EVENTS,
            frozen_submission_object_key=frozen_key,
        )
        return {
            "validator_hotkey": _VALIDATOR,
            "agent_id": str(agent_id),
            "bench_version": _BENCH,
            "run_row_id": str(run.run_row_id),
            "ticket_id": str(ticket.ticket_id),
            "ticket_deadline": deadline.isoformat(),
            "coding_run_id": run.coding_run_id,
            "agent_artifact_sha256": "ab" * 32,
            "screened_image_sha256": "cd" * 32,
            "run_manifest_sha256": run.run_manifest_sha256,
            "task_set_manifest_sha256": run.task_set_manifest_sha256,
            "authoring_evidence_sha256": current_digest,
            "evidence": current.model_dump(mode="json"),
            "authoring_transcript_object_key": transcript_key,
            "authoring_transcript_bytes": _TRANSCRIPT_BYTES,
            "authoring_event_count": _AUTHORING_EVENTS,
            "frozen_submission_object_key": frozen_key,
            "signature": _KEYPAIR.sign(message).hex(),
        }

    payload = payload_for(evidence)
    url = "/api/v1/validator/coding-shadow/authoring-freeze"
    first = await client.post(url, json=payload)
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["idempotent"] is False
    assert first.json()["authoring_evidence_sha256"] == digest
    async with session_maker() as session, session.begin():
        stored_ticket = await session.get(CodingShadowTicket, ticket.ticket_id)
        assert stored_ticket is not None
        stored_ticket.claim_acquired_at = _NOW - timedelta(minutes=4)
        stored_ticket.claim_started_at = _NOW - timedelta(minutes=3)
        stored_ticket.claim_heartbeat_at = _NOW - timedelta(minutes=2)
        stored_ticket.claim_expires_at = _NOW - timedelta(minutes=1)
    replay = await client.post(url, json=payload)
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True

    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(CodingShadowAuthoringFreeze)
            )
            == 1
        )
    admin = await client.get(
        f"/api/v1/admin/agents/{agent_id}/coding-shadow-evaluations",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
    )
    freeze = admin.json()["runs"][0]["tickets"][0]["authoring_freeze"]
    assert freeze["authoring_evidence_sha256"] == digest
    assert freeze["frozen_patch_sha256"] == evidence.frozen_patch_sha256
    assert freeze["weight_eligible"] is False

    changed = evidence.model_copy(update={"final_tree_sha256": "fe" * 32})
    assert (await client.post(url, json=payload_for(changed))).status_code == 409
    forged = dict(payload)
    forged["signature"] = "00" * 64
    assert (await client.post(url, json=forged)).status_code == 401


async def test_authoring_freeze_cannot_be_created_after_final_result(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, monkeypatch)
    agent_id, run, ticket, deadline = await _seed(session_maker)
    result_evidence = _evidence(ticket.ticket_id)
    async with session_maker() as session, session.begin():
        session.add(
            CodingShadowResult(
                result_id=uuid4(),
                ticket_id=ticket.ticket_id,
                run_row_id=run.run_row_id,
                run_evidence_sha256=coding_run_evidence_digest(result_evidence),
                task_count=1,
                resolved_count=1,
                repair_failure_count=0,
                infrastructure_count=0,
                invalid_count=0,
                candidate_integrity_count=0,
                control_plane_integrity_count=0,
                scoreable_task_count=1,
                repair_mean_micros=1_000_000,
                weight_eligible=False,
                evidence=result_evidence.model_dump(mode="json", by_alias=True),
                signature="99" * 64,
                created_at=_NOW,
            )
        )

    evidence = _authoring_evidence()
    digest = coding_authoring_evidence_digest(evidence)
    transcript_key = f"sha256/{evidence.authoring_transcript_sha256}"
    frozen_key = f"sha256/{evidence.frozen_patch_sha256}"
    message = coding_authoring_freeze_signing_message(
        validator_hotkey=_VALIDATOR,
        agent_id=agent_id,
        bench_version=_BENCH,
        run_row_id=run.run_row_id,
        ticket_id=ticket.ticket_id,
        ticket_deadline=deadline,
        coding_run_id=run.coding_run_id,
        agent_artifact_sha256="ab" * 32,
        screened_image_sha256="cd" * 32,
        run_manifest_sha256=run.run_manifest_sha256,
        task_set_manifest_sha256=run.task_set_manifest_sha256,
        authoring_evidence_sha256=digest,
        authoring_transcript_object_key=transcript_key,
        authoring_transcript_bytes=_TRANSCRIPT_BYTES,
        authoring_event_count=_AUTHORING_EVENTS,
        frozen_submission_object_key=frozen_key,
    )
    response = await client.post(
        "/api/v1/validator/coding-shadow/authoring-freeze",
        json={
            "validator_hotkey": _VALIDATOR,
            "agent_id": str(agent_id),
            "bench_version": _BENCH,
            "run_row_id": str(run.run_row_id),
            "ticket_id": str(ticket.ticket_id),
            "ticket_deadline": deadline.isoformat(),
            "coding_run_id": run.coding_run_id,
            "agent_artifact_sha256": "ab" * 32,
            "screened_image_sha256": "cd" * 32,
            "run_manifest_sha256": run.run_manifest_sha256,
            "task_set_manifest_sha256": run.task_set_manifest_sha256,
            "authoring_evidence_sha256": digest,
            "evidence": evidence.model_dump(mode="json"),
            "authoring_transcript_object_key": transcript_key,
            "authoring_transcript_bytes": _TRANSCRIPT_BYTES,
            "authoring_event_count": _AUTHORING_EVENTS,
            "frozen_submission_object_key": frozen_key,
            "signature": _KEYPAIR.sign(message).hex(),
        },
    )
    assert response.status_code == 409
    async with session_maker() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(CodingShadowAuthoringFreeze)
            )
            == 0
        )


async def test_infrastructure_result_can_close_without_authoring_freeze(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _agent_id, _run, ticket, _deadline = await _seed(session_maker)
    evidence = _infrastructure_evidence(ticket.ticket_id)
    async with session_maker() as session, session.begin():
        stored_ticket = await session.get(CodingShadowTicket, ticket.ticket_id)
        assert stored_ticket is not None
        inserted = await insert_coding_shadow_result(
            session,
            ticket=stored_ticket,
            evidence=evidence,
            run_evidence_sha256=coding_run_evidence_digest(evidence),
            signature="99" * 64,
        )
    assert inserted.idempotent is False
    assert isinstance(inserted.row, CodingShadowResult)
    assert inserted.row.scoreable_task_count == 0


async def test_signed_shadow_result_is_idempotent_visible_and_score_separate(
    app: FastAPI,
    client: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch,
) -> None:
    _install(app, session_maker, monkeypatch)
    agent_id, run, ticket, deadline = await _seed(session_maker)
    evidence = _evidence(ticket.ticket_id)
    digest = coding_run_evidence_digest(evidence)
    authoring = _authoring_evidence()
    async with session_maker() as session, session.begin():
        stored_ticket = await session.get(CodingShadowTicket, ticket.ticket_id)
        assert stored_ticket is not None
        await insert_coding_authoring_freeze(
            session,
            ticket=stored_ticket,
            evidence=authoring,
            authoring_evidence_sha256=coding_authoring_evidence_digest(authoring),
            authoring_transcript_object_key=(
                f"sha256/{authoring.authoring_transcript_sha256}"
            ),
            authoring_transcript_bytes=_TRANSCRIPT_BYTES,
            authoring_event_count=_AUTHORING_EVENTS,
            frozen_submission_object_key=f"sha256/{authoring.frozen_patch_sha256}",
            signature="98" * 64,
        )
    message = coding_shadow_result_signing_message(
        validator_hotkey=_VALIDATOR,
        agent_id=agent_id,
        run_row_id=run.run_row_id,
        ticket_id=ticket.ticket_id,
        bench_version=_BENCH,
        ticket_deadline=deadline,
        agent_artifact_sha256="ab" * 32,
        screened_image_sha256="cd" * 32,
        run_evidence_sha256=digest,
    )
    payload = {
        "validator_hotkey": _VALIDATOR,
        "bench_version": _BENCH,
        "run_row_id": str(run.run_row_id),
        "ticket_id": str(ticket.ticket_id),
        "ticket_deadline": deadline.isoformat(),
        "agent_artifact_sha256": "ab" * 32,
        "screened_image_sha256": "cd" * 32,
        "run_evidence_sha256": digest,
        "evidence": evidence.model_dump(mode="json", by_alias=True),
        "signature": _KEYPAIR.sign(message).hex(),
    }
    url = f"/api/v1/validator/agent/{agent_id}/coding-shadow-result"
    first = await client.post(url, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["idempotent"] is False
    assert first.json()["weight_eligible"] is False
    replay = await client.post(url, json=payload)
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True

    async with session_maker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(CodingShadowResult))
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(Score)) == 3
    admin = await client.get(
        f"/api/v1/admin/agents/{agent_id}/coding-shadow-evaluations",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
    )
    assert admin.status_code == 200, admin.text
    body = admin.json()
    assert body["shadow_only"] is True
    assert body["total_assignments"] == 1
    assert body["assignments"][0]["current"] is True
    assert body["assignments"][0]["selection_block_number"] == 123
    assert body["runs"][0]["issued"] is True
    assert (
        body["runs"][0]["assignment_row_id"]
        == body["assignments"][0]["assignment_row_id"]
    )
    assert body["runs"][0]["result_count"] == 1
    assert body["runs"][0]["quorum_complete"] is False
    assert body["runs"][0]["median_repair_mean_micros"] is None

    forged = dict(payload)
    forged["signature"] = "00" * 64
    assert (await client.post(url, json=forged)).status_code == 401

    validators = ("5" + "C" * 47, "5" + "D" * 47)
    ticket_ids = (uuid4(), uuid4())
    async with session_maker() as session, session.begin():
        for index, validator in enumerate(validators, start=2):
            session.add(
                CodingCapabilityCertification(
                    certification_row_id=uuid4(),
                    agent_id=agent_id,
                    artifact_sha256="ab" * 32,
                    screened_image_sha256="cd" * 32,
                    validator_hotkey=validator,
                    bench_version=_BENCH,
                    settlement_generation=1,
                    settlement_inference_grant_sha256="99" * 32,
                    settlement_provider_receipt_set_sha256="aa" * 32,
                    ticket_deadline=deadline,
                    coding_contract_version=1,
                    certification_id=f"cert-endpoint-shadow-00{index}",
                    status="certified",
                    failure_stage=None,
                    failure_code=None,
                    certification_sha256=f"{index:02x}" * 32,
                    canary_manifest_sha256="55" * 32,
                    transcript_object_key="sha256/" + "66" * 32,
                    frozen_submission_object_key="sha256/" + "77" * 32,
                    issued_at=_NOW - timedelta(minutes=5),
                    expires_at=deadline + timedelta(minutes=10),
                    weight_eligible=False,
                    receipt={},
                    signature="88" * 64,
                    created_at=_NOW,
                )
            )
        await session.flush()
        for validator, extra_ticket_id in zip(validators, ticket_ids, strict=True):
            issued = await issue_coding_shadow_ticket(
                session,
                run_row_id=run.run_row_id,
                ticket_id=extra_ticket_id,
                validator_hotkey=validator,
                issued_at=_NOW,
                deadline=deadline,
            )
            assert isinstance(issued.row, CodingShadowTicket)
            evidence = (
                _failed_evidence(extra_ticket_id)
                if validator == validators[0]
                else _evidence(extra_ticket_id)
            )
            authoring = _authoring_evidence()
            await insert_coding_authoring_freeze(
                session,
                ticket=issued.row,
                evidence=authoring,
                authoring_evidence_sha256=coding_authoring_evidence_digest(authoring),
                authoring_transcript_object_key=(
                    f"sha256/{authoring.authoring_transcript_sha256}"
                ),
                authoring_transcript_bytes=_TRANSCRIPT_BYTES,
                authoring_event_count=_AUTHORING_EVENTS,
                frozen_submission_object_key=(
                    f"sha256/{authoring.frozen_patch_sha256}"
                ),
                signature="98" * 64,
            )
            await insert_coding_shadow_result(
                session,
                ticket=issued.row,
                evidence=evidence,
                run_evidence_sha256=coding_run_evidence_digest(evidence),
                signature="99" * 64,
            )

    quorum = await client.get(
        f"/api/v1/admin/agents/{agent_id}/coding-shadow-evaluations",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
    )
    assert quorum.status_code == 200, quorum.text
    run_view = quorum.json()["runs"][0]
    assert run_view["result_count"] == 3
    assert run_view["quorum_complete"] is True
    assert run_view["median_repair_mean_micros"] == 1_000_000

    await _record_core_qualification_exit(session_maker, agent_id=agent_id)
    qualification_stale = await client.get(
        f"/api/v1/admin/agents/{agent_id}/coding-shadow-evaluations",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
    )
    assert qualification_stale.status_code == 200
    stale_body = qualification_stale.json()
    assert stale_body["assignments"][0]["stale_reason"] == "qualification_stale"
    assert stale_body["runs"][0]["stale_reason"] == "qualification_stale"

    async with session_maker() as session, session.begin():
        agent = await session.get(Agent, agent_id, with_for_update=True)
        assert agent is not None
        agent.sha256 = "fa" * 32
    stale = await client.get(
        f"/api/v1/admin/agents/{agent_id}/coding-shadow-evaluations",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
    )
    assert stale.status_code == 200
    assignment_view = stale.json()["assignments"][0]
    assert assignment_view["current"] is False
    assert assignment_view["stale_reason"] == "artifact_changed"
