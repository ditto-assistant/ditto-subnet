from __future__ import annotations

import hashlib
import json
import stat
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from ditto.api_models.coding_artifacts import CodingArtifactDeliveryPhase
from ditto.api_models.coding_canonical import (
    coding_canonical_json_bytes,
    coding_canonical_sha256,
)
from ditto.api_models.coding_catalog import CodingCatalogCommitment
from ditto.api_models.coding_evidence import CodingSealedEvidenceKind
from ditto.api_models.coding_selection import (
    CodingCatalogTaskPayload,
    CodingPrivateCatalogRecord,
    coding_catalog_task_commitment_digest,
)
from ditto.api_server.coding_hippius_canary import (
    HIPPIUS_SHADOW_CANARY_CONFIRMATION,
    HippiusShadowCanaryAuthoringMaterial,
    HippiusShadowCanaryAuthoringOutcome,
    HippiusShadowCanaryGradingMaterial,
    HippiusShadowCanaryGradingOutcome,
    HippiusShadowCanaryIntegrity,
    HippiusShadowCanaryPlan,
    load_hippius_shadow_canary_receipt,
    run_hippius_shadow_canary,
    write_hippius_shadow_canary_receipt,
)
from ditto.api_server.coding_hippius_custody import (
    HippiusEvidenceCustodyReadiness,
)
from ditto.api_server.coding_hippius_evidence import (
    HippiusSealedEvidenceReceipt,
    HippiusSealedEvidenceSourceAuthority,
    HippiusSealedEvidenceStatus,
)
from ditto.api_server.coding_hippius_retrieval import (
    HippiusPrivateInputTicketAuthority,
)
from ditto.coding_selection import coding_catalog_leaf_hash
from ditto.tests.api_server.test_coding_catalog_publication import _fixture

_NOW = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
_RELEASE = "hippius-synthetic-canary-v1"
_VALIDATOR = "5" + "A" * 47
_SOURCE = "a" * 40


def _synthetic_record() -> tuple[CodingCatalogCommitment, CodingPrivateCatalogRecord]:
    commitment, record = deepcopy(_fixture())
    commitment["corpus_release_id"] = _RELEASE
    payload = record["task_version"]["payload"]
    payload["corpus_release_id"] = _RELEASE
    task_payload = CodingCatalogTaskPayload.model_validate(payload)
    task_commitment = coding_catalog_task_commitment_digest(task_payload)
    root = coding_catalog_leaf_hash(
        catalog_index=0,
        task_commitment_sha256=task_commitment,
    )
    commitment["catalog_merkle_root"] = root
    commitment.pop("commitment_sha256")
    commitment["commitment_sha256"] = coding_canonical_sha256(
        commitment,
        maximum_bytes=1 << 20,
        label="synthetic canary catalog commitment",
    )
    membership = record["membership_proof"]
    membership.update(
        {
            "corpus_release_id": _RELEASE,
            "catalog_merkle_root": root,
            "task_commitment_sha256": task_commitment,
        }
    )
    membership.pop("catalog_membership_proof_sha256")
    membership["catalog_membership_proof_sha256"] = coding_canonical_sha256(
        membership,
        maximum_bytes=1 << 20,
        label="synthetic canary membership proof",
    )
    record["catalog_commitment_sha256"] = commitment["commitment_sha256"]
    record["task_version"]["task_commitment_sha256"] = task_commitment
    return (
        CodingCatalogCommitment.model_validate(commitment),
        CodingPrivateCatalogRecord.model_validate(record),
    )


def _record_sha256(record: CodingPrivateCatalogRecord) -> str:
    return hashlib.sha256(
        coding_canonical_json_bytes(
            record.model_dump(mode="json", by_alias=True),
            maximum_bytes=2 << 20,
            label="synthetic canary private record",
        )
    ).hexdigest()


class _PrivateInput:
    authority_sha256 = "1" * 64

    def __init__(self, record: CodingPrivateCatalogRecord) -> None:
        self.record = record
        self.calls: list[HippiusPrivateInputTicketAuthority] = []
        self.events: list[str] = []

    async def get_task_material(
        self,
        *,
        authority: HippiusPrivateInputTicketAuthority,
        now: datetime | None = None,
    ) -> CodingPrivateCatalogRecord:
        assert now == _NOW
        self.calls.append(authority)
        self.events.append(f"retrieve:{authority.delivery_phase.value}")
        return self.record


class _Authoring:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.material: HippiusShadowCanaryAuthoringMaterial | None = None

    async def execute_authoring(
        self,
        *,
        material: HippiusShadowCanaryAuthoringMaterial,
    ) -> HippiusShadowCanaryAuthoringOutcome:
        self.material = material
        self.events.append("execute:authoring")
        return HippiusShadowCanaryAuthoringOutcome(
            execution_authority_sha256=material.execution_authority_sha256,
            task_commitment_sha256=material.task_commitment_sha256,
            transcript=b"synthetic authoring transcript",
            frozen_submission=b"synthetic frozen patch",
            resolved=True,
        )


class _Grading:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.material: HippiusShadowCanaryGradingMaterial | None = None

    async def execute_grading(
        self,
        *,
        material: HippiusShadowCanaryGradingMaterial,
    ) -> HippiusShadowCanaryGradingOutcome:
        self.material = material
        self.events.append("execute:grading")
        return HippiusShadowCanaryGradingOutcome(
            execution_authority_sha256=material.execution_authority_sha256,
            task_commitment_sha256=material.task_commitment_sha256,
            frozen_submission_sha256=material.frozen_submission_sha256,
            terminal_evidence=b"synthetic pristine grading evidence",
            resolved=True,
            pristine=True,
        )


class _Evidence:
    readiness = HippiusEvidenceCustodyReadiness(
        configured=True,
        provider="hippius",
        private_input_authority_sha256="1" * 64,
        sealed_evidence_authority_sha256="2" * 64,
        probe_receipt_payload_sha256="3" * 64,
        wrapping_key_sha256="4" * 64,
        spool_ready=True,
        runtime_wired=True,
        worker_active=False,
        weight_eligible=False,
    )

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.prepared: dict[
            str, tuple[HippiusSealedEvidenceSourceAuthority, bytes]
        ] = {}

    async def prepare_and_store(
        self,
        *,
        authority: HippiusSealedEvidenceSourceAuthority,
        plaintext: bytes,
    ) -> str:
        self.events.append(f"prepare:{authority.evidence_kind.value}")
        identity = hashlib.sha256(
            authority.evidence_kind.value.encode() + b"\0" + plaintext
        ).hexdigest()
        self.prepared[identity] = (authority, plaintext)
        return identity

    async def publish(self, identity_sha256: str) -> HippiusSealedEvidenceReceipt:
        authority, plaintext = self.prepared[identity_sha256]
        self.events.append(f"publish:{authority.evidence_kind.value}")
        reservation_id = uuid5(
            UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            identity_sha256,
        )
        return HippiusSealedEvidenceReceipt(
            schema="dittobench-coding-hippius-sealed-evidence-receipt-v1",
            reservation_id=reservation_id,
            identity_sha256=identity_sha256,
            object_key_sha256=hashlib.sha256(b"object" + plaintext).hexdigest(),
            ciphertext_sha256=hashlib.sha256(b"cipher" + plaintext).hexdigest(),
            ciphertext_size_bytes=len(plaintext) + 16,
            envelope_sha256=hashlib.sha256(b"envelope" + plaintext).hexdigest(),
            probe_receipt_payload_sha256="3" * 64,
            status=HippiusSealedEvidenceStatus.UPLOADED,
            finalized_at="2026-09-02T20:00:00Z",
            ready=True,
            weight_eligible=False,
        )


def _plan(
    commitment: CodingCatalogCommitment,
    record: CodingPrivateCatalogRecord,
) -> HippiusShadowCanaryPlan:
    ticket_id = UUID("11111111-1111-4111-8111-111111111111")
    deadline = _NOW + timedelta(minutes=30)
    private_input = HippiusPrivateInputTicketAuthority(
        ticket_id=ticket_id,
        run_row_id=UUID("22222222-2222-4222-8222-222222222222"),
        validator_hotkey=_VALIDATOR,
        coding_run_id="hippius-canary-run-001",
        assignment_sha256="5" * 64,
        run_manifest_sha256="6" * 64,
        ticket_deadline=deadline,
        delivery_phase=CodingArtifactDeliveryPhase.AUTHORING,
        commitment=commitment,
        catalog_index=0,
        transport_manifest_sha256="7" * 64,
        publication_receipt_payload_sha256="8" * 64,
        weight_eligible=False,
    )
    return HippiusShadowCanaryPlan(
        canary_id=UUID("33333333-3333-4333-8333-333333333333"),
        source_sha=_SOURCE,
        synthetic_corpus_release_id=_RELEASE,
        synthetic_record_sha256=_record_sha256(record),
        private_input=private_input,
        sealed_evidence=HippiusSealedEvidenceSourceAuthority(
            ticket_id=ticket_id,
            claim_generation=1,
            validator_hotkey=_VALIDATOR,
            instance_id="hippius-canary-validator-001",
            ticket_deadline=deadline,
            evidence_kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
            weight_eligible=False,
        ),
        synthetic_only=True,
        single_validator=True,
        weight_eligible=False,
    )


async def test_canary_runs_two_separated_phases_and_seals_three_objects(
    tmp_path: Path,
) -> None:
    commitment, record = _synthetic_record()
    private_input = _PrivateInput(record)
    authoring = _Authoring(private_input.events)
    grading = _Grading(private_input.events)
    evidence = _Evidence(private_input.events)

    receipt = await run_hippius_shadow_canary(
        plan=_plan(commitment, record),
        private_input=private_input,
        evidence=evidence,
        authoring=authoring,
        grading=grading,
        confirmation=HIPPIUS_SHADOW_CANARY_CONFIRMATION,
        deployed_source_sha=_SOURCE,
        now=_NOW,
    )

    assert [item.delivery_phase for item in private_input.calls] == [
        CodingArtifactDeliveryPhase.AUTHORING,
        CodingArtifactDeliveryPhase.GRADING,
    ]
    assert private_input.events == [
        "retrieve:authoring",
        "execute:authoring",
        "prepare:authoring-transcript",
        "publish:authoring-transcript",
        "prepare:frozen-submission",
        "publish:frozen-submission",
        "retrieve:grading",
        "execute:grading",
        "prepare:terminal-publication-request",
        "publish:terminal-publication-request",
    ]
    assert authoring.material is not None
    assert not hasattr(authoring.material, "grader_plan")
    assert grading.material is not None
    assert not hasattr(grading.material, "issue")
    assert grading.material.frozen_submission == b"synthetic frozen patch"
    assert (
        authoring.material.execution_authority_sha256
        == grading.material.execution_authority_sha256
        == receipt.execution_authority_sha256
    )
    assert authoring.material.ticket_deadline == grading.material.ticket_deadline
    assert (
        grading.material.frozen_submission_sha256
        == hashlib.sha256(b"synthetic frozen patch").hexdigest()
    )
    assert tuple(item.evidence_kind for item in receipt.evidence) == (
        "authoring-transcript",
        "frozen-submission",
        "terminal-publication-request",
    )
    assert receipt.ready is True
    assert receipt.single_validator is True
    assert receipt.worker_active is False
    assert receipt.weight_eligible is False

    output = (tmp_path / "canary-receipt.json").resolve()
    payload_sha256 = write_hippius_shadow_canary_receipt(
        receipt=receipt,
        output=output,
    )
    loaded, loaded_sha256 = load_hippius_shadow_canary_receipt(output)
    assert loaded == receipt
    assert loaded_sha256 == payload_sha256
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    serialized = output.read_text()
    for private_value in (
        str(_plan(commitment, record).canary_id),
        str(_plan(commitment, record).private_input.ticket_id),
        _VALIDATOR,
        _RELEASE,
        "synthetic authoring transcript",
        "synthetic frozen patch",
        "synthetic pristine grading evidence",
    ):
        assert private_value not in serialized
    with pytest.raises(HippiusShadowCanaryIntegrity, match="output"):
        write_hippius_shadow_canary_receipt(receipt=receipt, output=output)


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "validator",
        "claim",
        "corpus",
        "record",
        "worker",
        "probe",
    ],
)
async def test_canary_rejects_non_synthetic_or_widened_authority(
    mutation: str,
) -> None:
    commitment, record = _synthetic_record()
    plan = _plan(commitment, record)
    private_input = _PrivateInput(record)
    evidence = _Evidence(private_input.events)
    deployed_source_sha = _SOURCE
    if mutation == "source":
        deployed_source_sha = "b" * 40
    elif mutation == "validator":
        plan = replace(
            plan,
            sealed_evidence=replace(
                plan.sealed_evidence,
                validator_hotkey="5" + "B" * 47,
            ),
        )
    elif mutation == "claim":
        plan = replace(
            plan,
            sealed_evidence=replace(plan.sealed_evidence, claim_generation=2),
        )
    elif mutation == "corpus":
        plan = replace(plan, synthetic_corpus_release_id="private-release-001")
    elif mutation == "record":
        plan = replace(plan, synthetic_record_sha256="f" * 64)
    elif mutation == "worker":
        evidence.readiness = replace(evidence.readiness, worker_active=True)
    elif mutation == "probe":
        evidence.readiness = replace(
            evidence.readiness,
            probe_receipt_payload_sha256="9" * 64,
        )

    with pytest.raises(HippiusShadowCanaryIntegrity):
        await run_hippius_shadow_canary(
            plan=plan,
            private_input=private_input,
            evidence=evidence,
            authoring=_Authoring(private_input.events),
            grading=_Grading(private_input.events),
            confirmation=HIPPIUS_SHADOW_CANARY_CONFIRMATION,
            deployed_source_sha=deployed_source_sha,
            now=_NOW,
        )


async def test_canary_requires_exact_confirmation_and_ready_evidence() -> None:
    commitment, record = _synthetic_record()
    private_input = _PrivateInput(record)
    evidence = _Evidence(private_input.events)
    with pytest.raises(HippiusShadowCanaryIntegrity, match="confirmed"):
        await run_hippius_shadow_canary(
            plan=_plan(commitment, record),
            private_input=private_input,
            evidence=evidence,
            authoring=_Authoring(private_input.events),
            grading=_Grading(private_input.events),
            confirmation="RUN",
            deployed_source_sha=_SOURCE,
            now=_NOW,
        )
    evidence.readiness = replace(evidence.readiness, spool_ready=False)
    with pytest.raises(HippiusShadowCanaryIntegrity, match="custody"):
        await run_hippius_shadow_canary(
            plan=_plan(commitment, record),
            private_input=private_input,
            evidence=evidence,
            authoring=_Authoring(private_input.events),
            grading=_Grading(private_input.events),
            confirmation=HIPPIUS_SHADOW_CANARY_CONFIRMATION,
            deployed_source_sha=_SOURCE,
            now=_NOW,
        )


async def test_canary_rejects_unresolved_or_non_pristine_execution() -> None:
    commitment, record = _synthetic_record()
    private_input = _PrivateInput(record)
    evidence = _Evidence(private_input.events)

    class Unresolved(_Authoring):
        async def execute_authoring(
            self,
            *,
            material: HippiusShadowCanaryAuthoringMaterial,
        ) -> HippiusShadowCanaryAuthoringOutcome:
            return replace(
                await super().execute_authoring(material=material),
                resolved=False,
            )

    with pytest.raises(HippiusShadowCanaryIntegrity, match="authoring"):
        await run_hippius_shadow_canary(
            plan=_plan(commitment, record),
            private_input=private_input,
            evidence=evidence,
            authoring=Unresolved(private_input.events),
            grading=_Grading(private_input.events),
            confirmation=HIPPIUS_SHADOW_CANARY_CONFIRMATION,
            deployed_source_sha=_SOURCE,
            now=_NOW,
        )

    private_input = _PrivateInput(record)
    evidence = _Evidence(private_input.events)

    class Impure(_Grading):
        async def execute_grading(
            self,
            *,
            material: HippiusShadowCanaryGradingMaterial,
        ) -> HippiusShadowCanaryGradingOutcome:
            return replace(
                await super().execute_grading(material=material),
                pristine=False,
            )

    with pytest.raises(HippiusShadowCanaryIntegrity, match="grading"):
        await run_hippius_shadow_canary(
            plan=_plan(commitment, record),
            private_input=private_input,
            evidence=evidence,
            authoring=_Authoring(private_input.events),
            grading=Impure(private_input.events),
            confirmation=HIPPIUS_SHADOW_CANARY_CONFIRMATION,
            deployed_source_sha=_SOURCE,
            now=_NOW,
        )


async def test_canary_receipt_tampering_fails_closed(tmp_path: Path) -> None:
    commitment, record = _synthetic_record()
    private_input = _PrivateInput(record)
    receipt = await run_hippius_shadow_canary(
        plan=_plan(commitment, record),
        private_input=private_input,
        evidence=_Evidence(private_input.events),
        authoring=_Authoring(private_input.events),
        grading=_Grading(private_input.events),
        confirmation=HIPPIUS_SHADOW_CANARY_CONFIRMATION,
        deployed_source_sha=_SOURCE,
        now=_NOW,
    )
    output = (tmp_path / "receipt.json").resolve()
    write_hippius_shadow_canary_receipt(receipt=receipt, output=output)
    raw = json.loads(output.read_text())
    raw["ready"] = False
    output.write_text(json.dumps(raw))
    output.chmod(0o600)
    with pytest.raises(HippiusShadowCanaryIntegrity, match="receipt"):
        load_hippius_shadow_canary_receipt(output)
