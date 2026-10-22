import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from ditto_screening_protocol.v13_official_execute import (
    OfficialIsolatedCaseObservationV2,
    execute_official_private_pairs_v2,
)
from ditto_screening_protocol.v13_official_execution import (
    ExecutionSubjectV2,
    MinerSubjectV2,
    OfficialControlSubjectV2,
    OfficialExecutionApprovalV2,
    OfficialGenerationGroupV2,
    OfficialPackageRegistrationV2,
    OfficialPackageUnavailable,
    OfficialPrivateManifestV2,
    OfficialPrivateReceiptV2,
    SubjectCommitmentV2,
    authentic_official_private_receipt_v2,
    match_official_packages_v2,
    prepare_official_package_v2,
    subject_digest,
)
from ditto_screening_protocol.v13_private_execute import (
    IsolatedCaseObservation,
    PrivateCasePayload,
    PrivateExecutionUnavailable,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    V13PrivatePair,
)


def group_document():
    now = datetime(2026, 10, 21, 12, tzinfo=UTC)
    miner = MinerSubjectV2(
        kind="miner",
        agent_id=UUID(int=1),
        attempt_id=UUID(int=2),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        image_id="sha256:" + "c" * 64,
    )
    control = OfficialControlSubjectV2(
        kind="official_control",
        control_id=UUID(int=3),
        fixture_id=UUID(int=4),
        artifact_sha256="d" * 64,
        image_sha256="e" * 64,
        image_id="sha256:" + "f" * 64,
    )
    approval = OfficialExecutionApprovalV2(
        task_id=UUID(int=5),
        control=control,
        control_approval_id=UUID(int=6),
        control_approval_receipt_sha256="1" * 64,
        profile_sha256="2" * 64,
        policy_sha256="3" * 64,
        runtime_configuration_sha256="4" * 64,
        source_review_sha256="5" * 64,
        bank_review_sha256="6" * 64,
        approved_at=now + timedelta(seconds=2),
    )
    return OfficialGenerationGroupV2(
        group_id=UUID(int=7),
        task_id=UUID(int=5),
        target=SubjectCommitmentV2(
            subject=miner,
            committed_at=now,
            image_verified_at=now + timedelta(seconds=1),
        ),
        known_benign=SubjectCommitmentV2(
            subject=control,
            committed_at=now,
            image_verified_at=now + timedelta(seconds=1),
        ),
        approval=approval,
        started_at=now + timedelta(seconds=3),
    ).model_dump(mode="json")


def test_official_control_never_acquires_miner_authority_from_extra_json():
    raw = group_document()["known_benign"]["subject"]
    raw.update(agent_id=str(UUID(int=100)), attempt_id=str(UUID(int=101)))
    subject = TypeAdapter(ExecutionSubjectV2).validate_python(raw)
    assert isinstance(subject, OfficialControlSubjectV2)
    assert "agent_id" not in subject.model_dump()
    assert "attempt_id" not in subject.model_dump()
    before = subject_digest(subject)
    raw["agent_id"] = str(UUID(int=102))
    assert (
        subject_digest(TypeAdapter(ExecutionSubjectV2).validate_python(raw)) == before
    )
    raw["kind"] = "miner"
    del raw["attempt_id"]
    with pytest.raises(ValidationError):
        TypeAdapter(ExecutionSubjectV2).validate_python(raw)


@pytest.mark.parametrize(
    "change", ["swap", "same_image", "same_config", "task", "approval", "late", "naive"]
)
def test_unapproved_or_spliced_group_is_rejected(change):
    raw = group_document()
    if change == "swap":
        raw["target"], raw["known_benign"] = raw["known_benign"], raw["target"]
    elif change == "same_image":
        raw["known_benign"]["subject"]["image_sha256"] = raw["target"]["subject"][
            "image_sha256"
        ]
    elif change == "same_config":
        raw["known_benign"]["subject"]["image_id"] = raw["target"]["subject"][
            "image_id"
        ]
    elif change == "task":
        raw["task_id"] = str(UUID(int=8))
    elif change == "approval":
        raw["approval"]["control"]["fixture_id"] = str(UUID(int=8))
    elif change == "late":
        raw["approval"]["approved_at"] = raw["started_at"]
    else:
        raw["target"]["committed_at"] = "2026-10-21T12:00:00"
    with pytest.raises(ValidationError):
        OfficialGenerationGroupV2.model_validate(raw)


def test_role_and_approval_commitments_bind_runtime_and_known_fields_only():
    raw = group_document()
    group = OfficialGenerationGroupV2.model_validate(raw)
    target = group.role_digest("target")
    assert target != group.role_digest("known_benign")
    approval = group.approval.receipt_sha256()
    raw["ignored_authority"] = {"approved": True}
    raw["target"]["subject"]["future_metadata"] = "ignored"
    assert OfficialGenerationGroupV2.model_validate(raw).role_digest("target") == target
    raw["approval"]["runtime_configuration_sha256"] = "7" * 64
    changed = OfficialGenerationGroupV2.model_validate(raw)
    assert changed.role_digest("target") != target
    assert changed.approval.receipt_sha256() != approval
    raw["revision"] = "v13-generation-group-v1"
    with pytest.raises(ValidationError):
        OfficialGenerationGroupV2.model_validate(raw)


def test_canonical_clock_offsets_cannot_create_distinct_commitments():
    raw = group_document()
    group = OfficialGenerationGroupV2.model_validate(raw)
    shifted = timezone(timedelta(hours=-4))
    raw["started_at"] = group.started_at.astimezone(shifted).isoformat()
    raw["approval"]["approved_at"] = group.approval.approved_at.astimezone(
        shifted
    ).isoformat()
    for role in ("target", "known_benign"):
        original = getattr(group, role)
        for field in ("committed_at", "image_verified_at"):
            raw[role][field] = getattr(original, field).astimezone(shifted).isoformat()
    same = OfficialGenerationGroupV2.model_validate(raw)
    assert same.role_digest("target") == group.role_digest("target")
    assert same.approval.receipt_sha256() == group.approval.receipt_sha256()


class MemoryStore:
    def __init__(self):
        self.manifests = {}
        self.payloads = {}

    async def read_manifest(self, digest):
        return self.manifests[digest]

    async def read_payload(self, digest):
        return self.payloads[digest]


def package_inputs(pairs_per_cell=10, payload_factory=None):
    raw = group_document()
    raw["approval"]["profile_sha256"] = V13_PRIVATE_PROFILE_SHA256
    group = OfficialGenerationGroupV2.model_validate(raw)
    store = MemoryStore()
    pairs = []
    for seed in ("8" * 64, "9" * 64):
        for kind in (
            "field_entity_rename",
            "request_paraphrase",
            "record_reorder_decoy",
        ):
            for index in range(pairs_per_cell):
                digests = []
                for side in ("control", "variant"):
                    data = (
                        payload_factory(UUID(int=100 + len(pairs)), side)
                        if payload_factory is not None
                        else f"sealed-{seed}-{kind}-{index}-{side}".encode()
                    )
                    digest = hashlib.sha256(data).hexdigest()
                    store.payloads[digest] = data
                    digests.append(digest)
                pairs.append(
                    V13PrivatePair(
                        pair_id=UUID(int=100 + len(pairs)),
                        seed_commitment=seed,
                        transformation_class=kind,
                        control_sha256=digests[0],
                        variant_sha256=digests[1],
                    )
                )
    registrations = []
    for role in ("target", "known_benign"):
        manifest = OfficialPrivateManifestV2(
            task_id=group.task_id,
            group_id=group.group_id,
            role=role,
            subject=getattr(group, role).subject,
            generation_receipt_sha256=group.role_digest(role),
            profile_sha256=V13_PRIVATE_PROFILE_SHA256,
            generated_at=group.started_at + timedelta(seconds=1),
            tool_catalog_applicable=False,
            pairs=tuple(pairs),
        )
        data = json.dumps(manifest.model_dump(mode="json"), sort_keys=True).encode()
        digest = hashlib.sha256(data).hexdigest()
        store.manifests[digest] = data
        registrations.append(
            OfficialPackageRegistrationV2(
                task_id=group.task_id,
                group_id=group.group_id,
                role=role,
                subject=manifest.subject,
                generation_receipt_sha256=manifest.generation_receipt_sha256,
                profile_sha256=V13_PRIVATE_PROFILE_SHA256,
                manifest_sha256=digest,
                pair_inventory_sha256=manifest.inventory_sha256(),
                registered_at=group.started_at + timedelta(seconds=2),
            )
        )
    return group, store, registrations


def test_matched_roles_have_different_manifests_and_one_exact_inventory():
    group, store, registrations = package_inputs()

    async def prepare():
        return [
            await prepare_official_package_v2(
                group=group, registration=registration, store=store
            )
            for registration in registrations
        ]

    target, control = asyncio.run(prepare())
    matched = match_official_packages_v2(target, control)
    assert matched.target_manifest_sha256 != matched.control_manifest_sha256
    assert matched.pair_count == 60
    assert matched.pair_inventory_sha256 == target.manifest.inventory_sha256()
    assert target.manifest.pairs == control.manifest.pairs
    with pytest.raises(OfficialPackageUnavailable):
        match_official_packages_v2(control, target)


@pytest.mark.parametrize(
    "tamper",
    ["task", "subject", "role_receipt", "inventory", "clock", "manifest", "payload"],
)
def test_sealed_v2_registration_rejects_splicing_and_byte_changes(tamper):
    group, store, registrations = package_inputs()
    raw = registrations[0].model_dump(mode="json")
    if tamper == "task":
        raw["task_id"] = str(UUID(int=999))
    elif tamper == "subject":
        raw["subject"]["attempt_id"] = str(UUID(int=999))
    elif tamper == "role_receipt":
        raw["generation_receipt_sha256"] = group.role_digest("known_benign")
    elif tamper == "inventory":
        raw["pair_inventory_sha256"] = "0" * 64
    elif tamper == "clock":
        raw["registered_at"] = group.started_at.isoformat()
    elif tamper == "manifest":
        store.manifests[raw["manifest_sha256"]] += b" "
    else:
        digest = next(iter(store.payloads))
        store.payloads[digest] += b"different"
    registration = OfficialPackageRegistrationV2.model_validate(raw)
    with pytest.raises(OfficialPackageUnavailable):
        asyncio.run(
            prepare_official_package_v2(
                group=group, registration=registration, store=store
            )
        )


def receipt_document(pairs_per_cell=10):
    group, store, registrations = package_inputs(pairs_per_cell)

    async def prepare():
        return [
            await prepare_official_package_v2(
                group=group, registration=registration, store=store
            )
            for registration in registrations
        ]

    matched = match_official_packages_v2(*asyncio.run(prepare()))
    roles = {}
    for role, manifest in (
        ("target", matched.target_manifest_sha256),
        ("known_benign", matched.control_manifest_sha256),
    ):
        roles[role] = {
            "summary": {
                "task_id": str(group.task_id),
                "group_id": str(group.group_id),
                "role": role,
                "subject": getattr(group, role).subject.model_dump(mode="json"),
                "profile_sha256": V13_PRIVATE_PROFILE_SHA256,
                "manifest_sha256": manifest,
                "pair_inventory_sha256": matched.pair_inventory_sha256,
                "runner_hotkey": "independent-runner",
                "status": "completed",
                "completed_pairs": matched.pair_count,
                "evidence_sha256": "a" * 64,
            },
            "aggregates": [
                {
                    **cell.model_dump(),
                    "control_correct": 8,
                    "variant_correct": 7,
                    "control_only_correct": 2,
                    "variant_only_correct": 1,
                }
                for cell in matched.inventory_cells
            ],
        }
    return {
        "binding": {
            "task_id": str(group.task_id),
            "lease_id": str(UUID(int=900)),
            "process_key_sha256": "b" * 64,
            "process_instance_id": "independent-process",
            "worker_release_sha256": "c" * 64,
        },
        "matched": matched.model_dump(mode="json"),
        **roles,
        "runner_hotkey": "independent-runner",
        "observed_at": (group.started_at + timedelta(seconds=3)).isoformat(),
        "signature": "d" * 128,
    }


@pytest.mark.parametrize("tamper", ["seeds", "counts", "early", "package_clock"])
def test_receipt_cannot_substitute_inventory_or_predate_sealed_packages(tamper):
    raw = receipt_document()
    if tamper == "seeds":
        for role in ("target", "known_benign"):
            for cell in raw[role]["aggregates"]:
                cell["seed_commitment"] = (
                    "0" if cell["seed_commitment"] == "8" * 64 else "1"
                ) * 64
    elif tamper == "counts":
        for role in ("target", "known_benign"):
            raw[role]["aggregates"][0]["pairs"] += 1
    elif tamper == "early":
        raw["observed_at"] = raw["matched"]["target_registered_at"]
    else:
        raw["matched"]["target_generated_at"] = raw["matched"]["target_registered_at"]
    with pytest.raises(ValidationError):
        OfficialPrivateReceiptV2.model_validate(raw)


def test_receipt_signing_normalizes_every_authoritative_clock():
    raw = receipt_document()
    receipt = OfficialPrivateReceiptV2.model_validate(raw)
    shifted = timezone(timedelta(hours=-4))

    def shift(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_at"):
                    value[key] = (
                        datetime.fromisoformat(item).astimezone(shifted).isoformat()
                    )
                else:
                    shift(item)

    shift(raw)
    same = OfficialPrivateReceiptV2.model_validate(raw)
    assert same == receipt
    assert same.signing_message() == receipt.signing_message()
    raw["future_authority"] = {"complete": True}
    assert (
        OfficialPrivateReceiptV2.model_validate(raw).signing_message()
        == receipt.signing_message()
    )


def test_receipt_cannot_redistribute_pairs_between_cells_with_same_total():
    raw = receipt_document(pairs_per_cell=12)
    for role in ("target", "known_benign"):
        raw[role]["aggregates"][0]["pairs"] += 1
        raw[role]["aggregates"][1]["pairs"] -= 1
    with pytest.raises(ValidationError, match="differ from committed inventory"):
        OfficialPrivateReceiptV2.model_validate(raw)


def test_receipt_authentication_binds_current_lease_runner_and_signature():
    receipt = OfficialPrivateReceiptV2.model_validate(receipt_document())
    expected_message = receipt.signing_message()
    args = {
        "expected_binding": receipt.binding,
        "expected_matched": receipt.matched,
        "enrolled_runner_hotkey": receipt.runner_hotkey,
        "source_worker_hotkey": "source-worker",
        "lease_started_at": receipt.observed_at - timedelta(seconds=1),
        "lease_deadline": receipt.observed_at + timedelta(seconds=1),
        "verify_signature": lambda key, message, signature: (
            key == "independent-runner"
            and message == expected_message
            and signature == "d" * 128
        ),
    }
    assert authentic_official_private_receipt_v2(receipt, **args)
    for override in (
        {
            "expected_binding": receipt.binding.model_copy(
                update={"lease_id": UUID(int=901)}
            )
        },
        {
            "expected_binding": receipt.binding.model_copy(
                update={"process_key_sha256": "e" * 64}
            )
        },
        {"source_worker_hotkey": receipt.runner_hotkey},
        {"enrolled_runner_hotkey": "other-runner"},
        {"lease_deadline": receipt.observed_at},
        {"lease_started_at": receipt.observed_at + timedelta(seconds=1)},
        {"verify_signature": lambda *_: False},
    ):
        assert not authentic_official_private_receipt_v2(receipt, **(args | override))


def execution_inputs(*, tools=False):
    def payload(pair_id, side):
        return json.dumps(
            PrivateCasePayload(
                pair_id=pair_id,
                side=side,
                semantic_contract_sha256="7" * 64,
                run_envelope={
                    "case_id": str(pair_id),
                    "user_input": "synthetic request",
                    "tools": [{"name": "synthetic_read"}] if tools else [],
                },
                expected_answer_sha256=hashlib.sha256(b"synthetic answer").hexdigest(),
            ).model_dump(mode="json")
        ).encode()

    group, store, registrations = package_inputs(payload_factory=payload)

    async def prepare():
        return [
            await prepare_official_package_v2(
                group=group, registration=registration, store=store
            )
            for registration in registrations
        ]

    return store, asyncio.run(prepare())


class SyntheticOfficialExecutor:
    def __init__(self, *, tamper=None):
        self.contexts = []
        self.tamper = tamper

    async def run_case(self, *, context, seed_envelope, run_envelope, timeout_seconds):
        self.contexts.append(context)
        assert seed_envelope is None
        assert run_envelope["user_input"] == "synthetic request"
        assert timeout_seconds == 30
        if self.tamper == "subject":
            context = replace(
                context,
                subject=context.subject.model_copy(
                    update={"image_id": "sha256:" + "0" * 64}
                ),
            )
        elif self.tamper == "runtime":
            context = replace(context, runtime_configuration_sha256="0" * 64)
        elif self.tamper == "case":
            context = replace(context, execution_id=UUID(int=999))
        elif self.tamper == "request":
            context = replace(context, request_sha256="0" * 64)
        return OfficialIsolatedCaseObservationV2(
            context=context,
            observation=IsolatedCaseObservation(
                response={"answer": "synthetic answer"},
                model_authority_verified=self.tamper != "authority",
            ),
        )


@pytest.mark.parametrize("role_index", [0, 1])
def test_official_execution_keeps_real_subject_and_config_on_every_case(role_index):
    store, roles = execution_inputs()
    prepared = roles[role_index]
    executor = SyntheticOfficialExecutor()
    result = asyncio.run(
        execute_official_private_pairs_v2(
            prepared=prepared, store=store, executor=executor, runner_hotkey="runner"
        )
    )
    assert len(executor.contexts) == 120
    assert len({context.execution_id for context in executor.contexts}) == 120
    assert all(
        context.subject == prepared.manifest.subject for context in executor.contexts
    )
    assert result.summary.subject == prepared.manifest.subject
    assert result.summary.completed_pairs == 60
    assert all(
        cell.control_correct == cell.variant_correct == cell.pairs
        for cell in result.aggregates
    )
    if role_index == 1:
        assert "agent_id" not in result.summary.subject.model_dump()
        assert "attempt_id" not in result.summary.subject.model_dump()


@pytest.mark.parametrize(
    "tamper", ["subject", "runtime", "case", "request", "authority", "bytes", "tools"]
)
def test_official_execution_rejects_stale_bytes_wrong_sandbox_or_missing_authority(
    tamper,
):
    store, roles = execution_inputs(tools=tamper == "tools")
    prepared = roles[1]
    if tamper == "bytes":
        store.payloads[next(iter(store.payloads))] += b"changed"
    executor = SyntheticOfficialExecutor(tamper=tamper)
    with pytest.raises(PrivateExecutionUnavailable):
        asyncio.run(
            execute_official_private_pairs_v2(
                prepared=prepared,
                store=store,
                executor=executor,
                runner_hotkey="runner",
            )
        )
    assert len(executor.contexts) == (0 if tamper in ("bytes", "tools") else 1)
