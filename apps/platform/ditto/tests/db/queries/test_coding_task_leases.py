"""Tests for private shadow coding task-lease reconstruction."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_selection import (
    CodingCatalogMembershipProof,
    CodingCatalogTaskVersion,
    CodingPrivateCatalogRecord,
    CodingSelectionAssignment,
)
from ditto.coding_selection import rebuild_coding_selection_result
from ditto.db.models import (
    Agent,
    CodingCapabilityCertification,
    CodingSelectionAssignmentRow,
    CodingShadowAuthoringFreeze,
    CodingShadowRun,
    CodingShadowTicket,
)
from ditto.db.queries import coding_task_leases
from ditto.db.queries.coding_task_leases import (
    CodingShadowGradingAuthority,
    CodingShadowHarnessAuthority,
    CodingTaskLeaseIntegrityError,
    CodingTaskLeaseNotAvailableError,
    authorize_coding_shadow_grading_delivery,
    authorize_coding_shadow_harness_delivery,
    authorize_coding_shadow_task_delivery,
    build_coding_shadow_task_lease,
)

_VECTOR_PATH = (
    Path(__file__).parents[6]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_selection_v1.json"
)
_FREEZE_VECTOR_PATH = (
    Path(__file__).parents[6]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_authoring_freeze_v1.json"
)
_EXECUTION_VECTOR_PATH = (
    Path(__file__).parents[6]
    / "packages"
    / "dittobench-coding-contract"
    / "testdata"
    / "coding_execution_plan_v1.json"
)


def _fixture() -> SimpleNamespace:
    vector = json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))
    execution_vector = json.loads(_EXECUTION_VECTOR_PATH.read_text(encoding="utf-8"))
    freeze_vector = json.loads(_FREEZE_VECTOR_PATH.read_text(encoding="utf-8"))
    freeze_evidence = freeze_vector["request"]["evidence"]
    commitment = CodingCatalogCommitment.model_validate(vector["commitment"])
    assignment = CodingSelectionAssignment.model_validate(vector["assignment"])
    task = CodingCatalogTaskVersion.model_validate(vector["task_version"])
    proof = CodingCatalogMembershipProof.model_validate(vector["membership_proof"])
    material = CodingPrivateCatalogRecord.model_validate(
        {
            "schema": "dittobench-coding-private-catalog-record-v1",
            "catalog_commitment_sha256": commitment.commitment_sha256,
            "task_version": vector["task_version"],
            "membership_proof": vector["membership_proof"],
            "issue": vector["issue"],
            "runtime_policy": vector["runtime_policy"],
            "budgets": vector["budgets"],
            "runner_plan": execution_vector["runner_plan"],
            "grader_plan": execution_vector["grader_plan"],
            "grader_resource_profile": execution_vector["grader_resource_profile"],
        }
    )
    rebuilt = rebuild_coding_selection_result(
        assignment=assignment,
        commitment=commitment,
        selection_block_hash=vector["selection_proof"]["selection_block_hash"],
        candidate_probe=vector["selection_proof"]["candidate_probe"],
        task_version=task,
        membership=proof,
    )
    run_row_id = uuid4()
    assignment_row_id = uuid4()
    certification_row_id = uuid4()
    ticket_id = uuid4()
    authority = rebuilt.authority
    run = SimpleNamespace(
        run_row_id=run_row_id,
        **{
            column: getattr(authority, field)
            for column, field in (
                ("agent_id", "agent_id"),
                ("artifact_sha256", "agent_artifact_sha256"),
                ("screened_image_sha256", "screened_image_sha256"),
                ("bench_version", "bench_version"),
                ("coding_contract_version", "coding_contract_version"),
                ("coding_run_id", "coding_run_id"),
                ("corpus_release_id", "corpus_release_id"),
                ("catalog_merkle_root", "catalog_merkle_root"),
                ("selection_derivation_id", "selection_derivation_id"),
                ("selection_chain_genesis_hash", "selection_chain_genesis_hash"),
                ("selection_block_number", "selection_block_number"),
                ("selection_block_hash", "selection_block_hash"),
                ("inference_grant_sha256", "inference_grant_sha256"),
                ("grader_contract_sha256", "grader_contract_sha256"),
                ("task_set_id", "task_set_id"),
                ("task_set_manifest_sha256", "task_set_manifest_sha256"),
                ("run_manifest_sha256", "run_manifest_sha256"),
                ("task_count", "task_count"),
                ("weight_eligible", "weight_eligible"),
            )
        },
    )
    now = assignment.assigned_at
    ticket = SimpleNamespace(
        ticket_id=ticket_id,
        run_row_id=run_row_id,
        validator_hotkey="5" + "V" * 47,
        certification_row_id=certification_row_id,
        task_count=1,
        issued_at=now,
        deadline=now.replace(year=now.year + 1),
        claim_instance_id="coding-worker-test-001",
        claim_started_at=now,
        claim_expires_at=now.replace(year=now.year + 1),
    )
    certification = SimpleNamespace(
        validator_hotkey=ticket.validator_hotkey,
        agent_id=authority.agent_id,
        bench_version=authority.bench_version,
        coding_contract_version=1,
        artifact_sha256=authority.agent_artifact_sha256,
        screened_image_sha256=authority.screened_image_sha256,
        status="certified",
        expires_at=ticket.deadline.replace(year=ticket.deadline.year + 1),
    )
    agent = SimpleNamespace(
        agent_id=authority.agent_id,
        sha256=authority.agent_artifact_sha256,
        screened_image_sha256=authority.screened_image_sha256,
        screened_image_size_bytes=1024,
        screened_image_id="sha256:" + "77" * 32,
        screened_image_ref="ditto-screened/test:latest",
        screened_image_upload_id=uuid4(),
        screening_policy_version=9,
    )
    issuance = SimpleNamespace(
        assignment_row_id=assignment_row_id,
        run_row_id=run_row_id,
        assignment_sha256=assignment.assignment_sha256,
        agent_id=authority.agent_id,
        artifact_sha256=authority.agent_artifact_sha256,
        screened_image_sha256=authority.screened_image_sha256,
        bench_version=authority.bench_version,
        coding_contract_version=1,
        coding_run_id=authority.coding_run_id,
        corpus_release_id=authority.corpus_release_id,
        selection_block_number=authority.selection_block_number,
        selection_catalog_index=task.payload.catalog_index,
        selection_block_hash=authority.selection_block_hash,
        selection_candidate_probe=vector["selection_proof"]["candidate_probe"],
        selection_proof_sha256=rebuilt.selection_proof.selection_proof_sha256,
        task_count=1,
        weight_eligible=False,
    )
    exposure = SimpleNamespace(
        run_row_id=run_row_id,
        corpus_release_id=authority.corpus_release_id,
        run_task_count=1,
        weight_eligible=False,
        **rebuilt.exposure.model_dump(mode="python"),
    )
    freeze = SimpleNamespace(
        freeze_id=uuid4(),
        ticket_id=ticket_id,
        run_row_id=run_row_id,
        task_count=1,
        authoring_evidence_sha256=freeze_vector["expected"][
            "authoring_evidence_sha256"
        ],
        authoring_event_root=freeze_evidence["authoring_event_root"],
        authoring_transcript_sha256=freeze_evidence["authoring_transcript_sha256"],
        authoring_transcript_bytes=1024,
        authoring_event_count=8,
        frozen_patch_sha256=freeze_evidence["frozen_patch_sha256"],
        frozen_submission_object_key=freeze_vector["request"][
            "frozen_submission_object_key"
        ],
        changed_path_root=freeze_evidence["changed_path_root"],
        final_tree_sha256=freeze_evidence["final_tree_sha256"],
        changed_path_count=freeze_evidence["changed_path_count"],
        changed_bytes=freeze_evidence["changed_bytes"],
        protected_paths_intact=freeze_evidence["protected_paths_intact"],
        weight_eligible=False,
        evidence=freeze_evidence,
    )
    return SimpleNamespace(
        commitment=commitment,
        assignment=assignment,
        material=material,
        rebuilt=rebuilt,
        run=run,
        ticket=ticket,
        certification=certification,
        agent=agent,
        issuance=issuance,
        exposure=exposure,
        freeze=freeze,
        result_id=None,
        assignment_row=object(),
        now=now,
        catalog_now=now,
        delivery_now=now,
    )


class _Session:
    def __init__(self, fixture: SimpleNamespace) -> None:
        self.fixture = fixture
        self.scalar_index = 0

    async def get(self, model, identity):
        del identity
        return {
            CodingShadowTicket: self.fixture.ticket,
            CodingShadowRun: self.fixture.run,
            CodingSelectionAssignmentRow: self.fixture.assignment_row,
            CodingCapabilityCertification: self.fixture.certification,
            Agent: self.fixture.agent,
        }.get(model)

    async def scalar(self, statement):
        del statement
        values = (
            self.fixture.issuance,
            self.fixture.now,
            self.fixture.catalog_now,
            self.fixture.delivery_now,
        )
        value = values[self.scalar_index]
        self.scalar_index += 1
        return value

    async def scalars(self, statement):
        del statement
        return [self.fixture.exposure]


class _AuthorizationSession:
    def __init__(self, fixture: SimpleNamespace) -> None:
        self.fixture = fixture

    async def get(self, model, identity):
        assert model is CodingShadowTicket
        return (
            self.fixture.ticket if identity == self.fixture.ticket.ticket_id else None
        )

    async def scalar(self, statement):
        del statement
        return self.fixture.now


class _GradingAuthorizationSession:
    def __init__(self, fixture: SimpleNamespace) -> None:
        self.fixture = fixture
        self.scalar_index = 0

    async def get(self, model, identity):
        return {
            CodingShadowTicket: (
                self.fixture.ticket
                if identity == self.fixture.ticket.ticket_id
                else None
            ),
            CodingShadowRun: (
                self.fixture.run if identity == self.fixture.run.run_row_id else None
            ),
            CodingShadowAuthoringFreeze: (
                self.fixture.freeze
                if identity == self.fixture.freeze.freeze_id
                else None
            ),
            CodingCapabilityCertification: (
                self.fixture.certification
                if identity == self.fixture.ticket.certification_row_id
                else None
            ),
            Agent: (
                self.fixture.agent if identity == self.fixture.run.agent_id else None
            ),
        }.get(model)

    async def scalar(self, statement):
        del statement
        values = (self.fixture.now, self.fixture.result_id)
        value = values[self.scalar_index]
        self.scalar_index += 1
        return value


class _HarnessAuthorizationSession:
    def __init__(self, fixture: SimpleNamespace) -> None:
        self.fixture = fixture
        self.scalar_index = 0

    async def get(self, model, identity, **kwargs):
        del kwargs
        return {
            CodingShadowTicket: (
                self.fixture.ticket
                if identity == self.fixture.ticket.ticket_id
                else None
            ),
            CodingShadowRun: (
                self.fixture.run if identity == self.fixture.run.run_row_id else None
            ),
            CodingCapabilityCertification: (
                self.fixture.certification
                if identity == self.fixture.ticket.certification_row_id
                else None
            ),
            Agent: (
                self.fixture.agent if identity == self.fixture.run.agent_id else None
            ),
        }.get(model)

    async def scalar(self, statement):
        del statement
        values = (self.fixture.now, None, None)
        value = values[self.scalar_index]
        self.scalar_index += 1
        return value


async def _build(monkeypatch, fixture: SimpleNamespace):
    monkeypatch.setattr(
        coding_task_leases,
        "assignment_from_row",
        lambda _row: fixture.assignment,
    )
    monkeypatch.setattr(
        coding_task_leases,
        "get_coding_catalog_release",
        AsyncMock(
            return_value=SimpleNamespace(commitment=fixture.commitment.model_dump())
        ),
    )
    monkeypatch.setattr(
        coding_task_leases,
        "catalog_release_matches_commitment",
        lambda _row, *, commitment: bool(commitment),
    )
    source = AsyncMock()
    source.get_task_material.return_value = fixture.material
    return await build_coding_shadow_task_lease(
        _Session(fixture),  # type: ignore[arg-type]
        ticket_id=fixture.ticket.ticket_id,
        material_source=source,
    )


async def test_task_lease_reconstructs_exact_shared_authority(monkeypatch) -> None:
    fixture = _fixture()
    lease = await _build(monkeypatch, fixture)

    assert lease.run_manifest == fixture.rebuilt.run_manifest
    assert lease.task_set_manifest == fixture.rebuilt.task_set_manifest
    assert lease.issue == fixture.material.issue
    assert lease.runtime_policy == fixture.material.runtime_policy
    assert lease.budgets == fixture.material.budgets
    assert lease.weight_eligible is False


async def test_delivery_authorization_precedes_private_catalog_access() -> None:
    fixture = _fixture()
    session = _AuthorizationSession(fixture)
    await authorize_coding_shadow_task_delivery(
        session,  # type: ignore[arg-type]
        ticket_id=fixture.ticket.ticket_id,
        validator_hotkey=fixture.ticket.validator_hotkey,
    )
    with pytest.raises(CodingTaskLeaseNotAvailableError, match="unavailable"):
        await authorize_coding_shadow_task_delivery(
            session,  # type: ignore[arg-type]
            ticket_id=fixture.ticket.ticket_id,
            validator_hotkey="5" + "X" * 47,
        )
    fixture.now = fixture.ticket.deadline
    with pytest.raises(CodingTaskLeaseNotAvailableError, match="unavailable"):
        await authorize_coding_shadow_task_delivery(
            session,  # type: ignore[arg-type]
            ticket_id=fixture.ticket.ticket_id,
            validator_hotkey=fixture.ticket.validator_hotkey,
        )


async def test_harness_authorization_binds_current_screened_image(monkeypatch) -> None:
    fixture = _fixture()
    monkeypatch.setattr(
        coding_task_leases,
        "coding_certification_stale_reason",
        lambda *_args, **_kwargs: "active",
    )
    authority = await authorize_coding_shadow_harness_delivery(
        _HarnessAuthorizationSession(fixture),  # type: ignore[arg-type]
        ticket_id=fixture.ticket.ticket_id,
        validator_hotkey=fixture.ticket.validator_hotkey,
    )
    assert authority == CodingShadowHarnessAuthority(
        agent_id=fixture.run.agent_id,
        run_row_id=fixture.run.run_row_id,
        ticket_id=fixture.ticket.ticket_id,
        deadline=fixture.ticket.deadline,
        bench_version=fixture.run.bench_version,
        agent_artifact_sha256=fixture.run.artifact_sha256,
        screened_image_sha256=fixture.agent.screened_image_sha256,
        screened_image_size_bytes=fixture.agent.screened_image_size_bytes,
        screened_image_id=fixture.agent.screened_image_id,
        screened_image_ref=fixture.agent.screened_image_ref,
        screened_image_upload_id=fixture.agent.screened_image_upload_id,
        screening_policy_version=fixture.agent.screening_policy_version,
    )
    fixture.agent.screened_image_sha256 = "ff" * 32
    with pytest.raises(CodingTaskLeaseNotAvailableError, match="unavailable"):
        await authorize_coding_shadow_harness_delivery(
            _HarnessAuthorizationSession(fixture),  # type: ignore[arg-type]
            ticket_id=fixture.ticket.ticket_id,
            validator_hotkey=fixture.ticket.validator_hotkey,
        )


async def test_grading_authorization_requires_complete_immutable_freeze() -> None:
    fixture = _fixture()
    authority = await authorize_coding_shadow_grading_delivery(
        _GradingAuthorizationSession(fixture),  # type: ignore[arg-type]
        validator_hotkey=fixture.ticket.validator_hotkey,
        agent_id=fixture.run.agent_id,
        run_row_id=fixture.run.run_row_id,
        ticket_id=fixture.ticket.ticket_id,
        freeze_id=fixture.freeze.freeze_id,
        authoring_evidence_sha256=fixture.freeze.authoring_evidence_sha256,
    )
    assert authority == CodingShadowGradingAuthority(
        agent_id=fixture.run.agent_id,
        freeze_id=fixture.freeze.freeze_id,
        authoring_evidence_sha256=fixture.freeze.authoring_evidence_sha256,
        frozen_patch_sha256=fixture.freeze.frozen_patch_sha256,
        frozen_submission_object_key=fixture.freeze.frozen_submission_object_key,
    )


@pytest.mark.parametrize(
    "drift",
    [
        "zero transcript",
        "zero events",
        "no changed paths",
        "protected paths changed",
        "model usage incomplete",
        "evidence nonobject",
        "evidence field drift",
        "weighted run",
        "existing result",
        "evidence digest",
    ],
)
async def test_grading_authorization_rejects_ungradeable_freeze(drift: str) -> None:
    fixture = _fixture()
    requested_digest = fixture.freeze.authoring_evidence_sha256
    if drift == "zero transcript":
        fixture.freeze.authoring_transcript_bytes = 0
    elif drift == "zero events":
        fixture.freeze.authoring_event_count = 0
    elif drift == "no changed paths":
        fixture.freeze.changed_path_count = 0
    elif drift == "protected paths changed":
        fixture.freeze.protected_paths_intact = False
    elif drift == "model usage incomplete":
        fixture.freeze.evidence = {
            **fixture.freeze.evidence,
            "model": {
                **fixture.freeze.evidence["model"],
                "usage_status": "provider_failure",
            },
        }
    elif drift == "evidence nonobject":
        fixture.freeze.evidence = []
    elif drift == "evidence field drift":
        fixture.freeze.authoring_event_root = "ff" * 32
    elif drift == "weighted run":
        fixture.run.weight_eligible = True
    elif drift == "existing result":
        fixture.result_id = uuid4()
    else:
        requested_digest = "ff" * 32

    with pytest.raises(CodingTaskLeaseIntegrityError, match="not gradeable"):
        await authorize_coding_shadow_grading_delivery(
            _GradingAuthorizationSession(fixture),  # type: ignore[arg-type]
            validator_hotkey=fixture.ticket.validator_hotkey,
            agent_id=fixture.run.agent_id,
            run_row_id=fixture.run.run_row_id,
            ticket_id=fixture.ticket.ticket_id,
            freeze_id=fixture.freeze.freeze_id,
            authoring_evidence_sha256=requested_digest,
        )


async def test_grading_authorization_hides_wrong_owner_or_expired_ticket() -> None:
    fixture = _fixture()
    for validator_hotkey, agent_id in (
        ("5" + "X" * 47, fixture.run.agent_id),
        (fixture.ticket.validator_hotkey, uuid4()),
        (fixture.ticket.validator_hotkey, fixture.run.agent_id),
    ):
        if (
            agent_id == fixture.run.agent_id
            and validator_hotkey == fixture.ticket.validator_hotkey
        ):
            fixture.now = fixture.ticket.deadline
        with pytest.raises(CodingTaskLeaseNotAvailableError, match="unavailable"):
            await authorize_coding_shadow_grading_delivery(
                _GradingAuthorizationSession(fixture),  # type: ignore[arg-type]
                validator_hotkey=validator_hotkey,
                agent_id=agent_id,
                run_row_id=fixture.run.run_row_id,
                ticket_id=fixture.ticket.ticket_id,
                freeze_id=fixture.freeze.freeze_id,
                authoring_evidence_sha256=fixture.freeze.authoring_evidence_sha256,
            )

    fixture = _fixture()
    fixture.freeze.ticket_id = uuid4()
    with pytest.raises(CodingTaskLeaseNotAvailableError, match="unavailable"):
        await authorize_coding_shadow_grading_delivery(
            _GradingAuthorizationSession(fixture),  # type: ignore[arg-type]
            validator_hotkey=fixture.ticket.validator_hotkey,
            agent_id=fixture.run.agent_id,
            run_row_id=fixture.run.run_row_id,
            ticket_id=fixture.ticket.ticket_id,
            freeze_id=fixture.freeze.freeze_id,
            authoring_evidence_sha256=fixture.freeze.authoring_evidence_sha256,
        )


@pytest.mark.parametrize("drift", ["artifact", "certification expiry"])
async def test_grading_authorization_rechecks_artifact_certification(
    drift: str,
) -> None:
    fixture = _fixture()
    if drift == "artifact":
        fixture.agent.sha256 = "ff" * 32
        fixture.certification.artifact_sha256 = "ff" * 32
    else:
        fixture.certification.expires_at = fixture.now
    with pytest.raises(CodingTaskLeaseNotAvailableError, match="unavailable"):
        await authorize_coding_shadow_grading_delivery(
            _GradingAuthorizationSession(fixture),  # type: ignore[arg-type]
            validator_hotkey=fixture.ticket.validator_hotkey,
            agent_id=fixture.run.agent_id,
            run_row_id=fixture.run.run_row_id,
            ticket_id=fixture.ticket.ticket_id,
            freeze_id=fixture.freeze.freeze_id,
            authoring_evidence_sha256=fixture.freeze.authoring_evidence_sha256,
        )


async def test_different_validator_tickets_share_identical_manifests(
    monkeypatch,
) -> None:
    fixture = _fixture()
    first = await _build(monkeypatch, fixture)
    fixture.ticket.ticket_id = uuid4()
    fixture.ticket.validator_hotkey = "5" + "W" * 47
    fixture.certification.validator_hotkey = fixture.ticket.validator_hotkey
    second = await _build(monkeypatch, fixture)

    assert first.ticket_id != second.ticket_id
    assert first.validator_hotkey != second.validator_hotkey
    assert first.run_manifest == second.run_manifest
    assert first.task_set_manifest == second.task_set_manifest


@pytest.mark.parametrize(
    "drift",
    ["run", "exposure", "selection_proof", "issuance"],
)
async def test_task_lease_rejects_persisted_authority_drift(
    monkeypatch,
    drift: str,
) -> None:
    fixture = _fixture()
    if drift == "run":
        fixture.run.run_manifest_sha256 = "ff" * 32
    elif drift == "exposure":
        fixture.exposure.visible_bundle_sha256 = "ff" * 32
    else:
        if drift == "selection_proof":
            fixture.issuance.selection_proof_sha256 = "ff" * 32
        else:
            fixture.issuance.assignment_sha256 = "ff" * 32
    with pytest.raises(CodingTaskLeaseIntegrityError, match="disagrees"):
        await _build(monkeypatch, fixture)


async def test_task_lease_rejects_insufficient_lifetime_before_catalog_read(
    monkeypatch,
) -> None:
    fixture = _fixture()
    fixture.now = fixture.ticket.deadline - timedelta(seconds=30)
    fixture.catalog_now = fixture.now
    monkeypatch.setattr(
        coding_task_leases,
        "assignment_from_row",
        lambda _row: fixture.assignment,
    )
    monkeypatch.setattr(
        coding_task_leases,
        "get_coding_catalog_release",
        AsyncMock(
            return_value=SimpleNamespace(commitment=fixture.commitment.model_dump())
        ),
    )
    monkeypatch.setattr(
        coding_task_leases,
        "catalog_release_matches_commitment",
        lambda _row, *, commitment: bool(commitment),
    )
    source = AsyncMock()
    source.timeout_seconds = 60.0
    with pytest.raises(CodingTaskLeaseNotAvailableError, match="insufficient lifetime"):
        await build_coding_shadow_task_lease(
            _Session(fixture),  # type: ignore[arg-type]
            ticket_id=fixture.ticket.ticket_id,
            material_source=source,
        )
    source.get_task_material.assert_not_awaited()


async def test_task_lease_rejects_expired_ticket_before_catalog_read() -> None:
    fixture = _fixture()
    fixture.now = fixture.ticket.deadline
    source = AsyncMock()
    with pytest.raises(CodingTaskLeaseNotAvailableError, match="no longer active"):
        await build_coding_shadow_task_lease(
            _Session(fixture),  # type: ignore[arg-type]
            ticket_id=fixture.ticket.ticket_id,
            material_source=source,
        )
    source.get_task_material.assert_not_awaited()


@pytest.mark.parametrize("drift", ["benchmark", "artifact"])
async def test_task_lease_rejects_stale_certification(drift: str) -> None:
    fixture = _fixture()
    if drift == "benchmark":
        fixture.certification.bench_version = fixture.run.bench_version - 1
    else:
        fixture.agent.sha256 = "ff" * 32
        fixture.certification.artifact_sha256 = "ff" * 32
    source = AsyncMock()
    with pytest.raises(CodingTaskLeaseNotAvailableError, match="no longer active"):
        await build_coding_shadow_task_lease(
            _Session(fixture),  # type: ignore[arg-type]
            ticket_id=fixture.ticket.ticket_id,
            material_source=source,
        )
    source.get_task_material.assert_not_awaited()


async def test_task_lease_rechecks_deadline_after_catalog_read(monkeypatch) -> None:
    fixture = _fixture()
    fixture.delivery_now = fixture.ticket.deadline
    monkeypatch.setattr(
        coding_task_leases,
        "assignment_from_row",
        lambda _row: fixture.assignment,
    )
    monkeypatch.setattr(
        coding_task_leases,
        "get_coding_catalog_release",
        AsyncMock(
            return_value=SimpleNamespace(commitment=fixture.commitment.model_dump())
        ),
    )
    monkeypatch.setattr(
        coding_task_leases,
        "catalog_release_matches_commitment",
        lambda _row, *, commitment: bool(commitment),
    )
    source = AsyncMock()
    source.get_task_material.return_value = fixture.material

    with pytest.raises(CodingTaskLeaseNotAvailableError, match="expired during"):
        await build_coding_shadow_task_lease(
            _Session(fixture),  # type: ignore[arg-type]
            ticket_id=fixture.ticket.ticket_id,
            material_source=source,
        )
    source.get_task_material.assert_awaited_once()


async def test_task_lease_revalidates_material_source_boundary(monkeypatch) -> None:
    fixture = _fixture()
    monkeypatch.setattr(
        coding_task_leases,
        "assignment_from_row",
        lambda _row: fixture.assignment,
    )
    monkeypatch.setattr(
        coding_task_leases,
        "get_coding_catalog_release",
        AsyncMock(
            return_value=SimpleNamespace(commitment=fixture.commitment.model_dump())
        ),
    )
    monkeypatch.setattr(
        coding_task_leases,
        "catalog_release_matches_commitment",
        lambda _row, *, commitment: bool(commitment),
    )
    source = AsyncMock()
    source.get_task_material.return_value = fixture.material.model_copy(
        update={"catalog_commitment_sha256": "ff" * 32}
    )

    with pytest.raises(CodingTaskLeaseIntegrityError, match="another catalog"):
        await build_coding_shadow_task_lease(
            _Session(fixture),  # type: ignore[arg-type]
            ticket_id=fixture.ticket.ticket_id,
            material_source=source,
        )
