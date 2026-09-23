"""Synthetic issuer tests; no hidden production cases, keys, or endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest

from ditto_screening_protocol.v13_private_clean_control import (
    compute_v13_generation_role_digest,
    prepare_v13_matched_clean_control,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    ArtifactCommitment,
    PrivatePackageRegistration,
    V13PrivateManifest,
    V13PrivatePair,
)
from ditto_screening_protocol.v13_private_seed import (
    AuthenticatedGeneratorDerivationReceipt,
    AuthenticatedKnownBenignApproval,
    SealedSeedRecord,
    SeedGenerationGroup,
    V13SeedIssuanceUnavailable,
    VerifiedRoleCommitment,
    _ordered_payload_digests_sha256,
    issue_v13_hidden_group_seeds,
    verify_v13_issued_matched_inventory,
)


class Registry:
    def __init__(
        self,
        group: SeedGenerationGroup,
        target: VerifiedRoleCommitment,
        control: VerifiedRoleCommitment,
        approval: AuthenticatedKnownBenignApproval,
    ) -> None:
        self.group = group
        self.roles = {"target": target, "known_benign": control}
        self.approval = approval
        self.packages: dict[tuple[UUID, str], PrivatePackageRegistration] = {}

    async def get_verified_group(self, group_id: UUID) -> SeedGenerationGroup:
        assert group_id == self.group.group_id
        return self.group

    async def get_verified_role_commitment(
        self, _group_id: UUID, role: Literal["target", "known_benign"]
    ) -> VerifiedRoleCommitment:
        return self.roles[role]

    async def get_authenticated_approval(
        self, _approval_id: UUID
    ) -> AuthenticatedKnownBenignApproval:
        return self.approval

    async def get_approval(
        self, _agent_id: UUID, _attempt_id: UUID
    ) -> AuthenticatedKnownBenignApproval:
        return self.approval

    async def get_group_registration(
        self, group_id: UUID, role: Literal["target", "known_benign"]
    ) -> PrivatePackageRegistration:
        return self.packages[(group_id, role)]


class Store:
    def __init__(self, committed_at: datetime) -> None:
        self.committed_at = committed_at
        self.records: dict[UUID, SealedSeedRecord] = {}
        self.create_calls = 0

    async def create_once(self, group_id: UUID, new_bundle):
        if group_id not in self.records:
            self.create_calls += 1
            self.records[group_id] = SealedSeedRecord(
                sealed_bytes=new_bundle(), committed_at=self.committed_at
            )
        return self.records[group_id]

    async def read(self, group_id: UUID) -> SealedSeedRecord:
        return self.records[group_id]


class PackageStore:
    def __init__(self) -> None:
        self.manifests: dict[str, bytes] = {}
        self.payloads: dict[str, bytes] = {}

    async def read_manifest(self, sha256: str) -> bytes:
        return self.manifests[sha256]

    async def read_payload(self, sha256: str) -> bytes:
        return self.payloads[sha256]


class GeneratorProvenance:
    """Synthetic trusted-generator projection, only for contract tests."""

    def __init__(self, registry: Registry, store: PackageStore, issued) -> None:
        self.receipts: dict[str, AuthenticatedGeneratorDerivationReceipt] = {}
        for role in ("target", "known_benign"):
            registration = registry.packages[(issued.group_id, role)]
            raw = store.manifests[registration.manifest_sha256]
            manifest = V13PrivateManifest.model_validate_json(raw)
            self.receipts[role] = AuthenticatedGeneratorDerivationReceipt(
                group_id=issued.group_id,
                role=role,
                sealed_bundle_sha256=issued.sealed_bundle_sha256,
                generator_revision="v13-private-case-generator-v1",
                manifest_sha256=registration.manifest_sha256,
                ordered_payload_digests_sha256=_ordered_payload_digests_sha256(
                    manifest
                ),
                generated_at=manifest.generated_at,
                attested_at=manifest.generated_at + timedelta(milliseconds=500),
                derivation_attestation_sha256=hashlib.sha256(
                    (role + registration.manifest_sha256).encode()
                ).hexdigest(),
            )

    async def get_verified_derivation(
        self, _group_id: UUID, role: Literal["target", "known_benign"]
    ) -> AuthenticatedGeneratorDerivationReceipt:
        return self.receipts[role]


def register_matched_packages(
    registry: Registry,
    package_store: PackageStore,
    seed_commitments: tuple[str, str],
) -> None:
    pairs: list[V13PrivatePair] = []
    for seed in seed_commitments:
        for transformation_class in (
            "field_entity_rename",
            "request_paraphrase",
            "record_reorder_decoy",
        ):
            for index in range(10):
                digests: list[str] = []
                for side in ("control", "variant"):
                    payload = f"{seed}-{transformation_class}-{index}-{side}".encode()
                    digest = hashlib.sha256(payload).hexdigest()
                    package_store.payloads[digest] = payload
                    digests.append(digest)
                pairs.append(
                    V13PrivatePair(
                        pair_id=uuid4(),
                        seed_commitment=seed,
                        transformation_class=transformation_class,
                        control_sha256=digests[0],
                        variant_sha256=digests[1],
                    )
                )
    for role, commitment, receipt in (
        (
            "target",
            registry.roles["target"],
            registry.group.target_receipt_sha256,
        ),
        (
            "known_benign",
            registry.roles["known_benign"],
            registry.group.control_receipt_sha256,
        ),
    ):
        generated_at = registry.group.started_at + timedelta(minutes=1, seconds=10)
        manifest = V13PrivateManifest(
            agent_id=commitment.agent_id,
            attempt_id=commitment.attempt_id,
            artifact_sha256=commitment.artifact_sha256,
            image_sha256=commitment.image_sha256,
            profile_sha256=V13_PRIVATE_PROFILE_SHA256,
            generated_at=generated_at,
            tool_catalog_applicable=False,
            pairs=tuple(pairs),
        )
        raw = json.dumps(
            manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        manifest_sha256 = hashlib.sha256(raw).hexdigest()
        package_store.manifests[manifest_sha256] = raw
        registry.packages[(registry.group.group_id, role)] = PrivatePackageRegistration(
            agent_id=commitment.agent_id,
            attempt_id=commitment.attempt_id,
            artifact_sha256=commitment.artifact_sha256,
            image_sha256=commitment.image_sha256,
            profile_sha256=V13_PRIVATE_PROFILE_SHA256,
            manifest_sha256=manifest_sha256,
            generation_group_id=registry.group.group_id,
            generation_role=role,
            generation_receipt_sha256=receipt,
            registered_at=generated_at + timedelta(seconds=1),
            registrar_id="synthetic-private-store",
        )


def fixture() -> tuple[Registry, Store]:
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    target = VerifiedRoleCommitment(
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="a" * 64,
        image_sha256="b" * 64,
        committed_at=now,
        policy_version=13,
        image_verification_receipt_sha256="1" * 64,
    )
    control = VerifiedRoleCommitment(
        agent_id=uuid4(),
        attempt_id=uuid4(),
        artifact_sha256="c" * 64,
        image_sha256="d" * 64,
        committed_at=now,
        policy_version=13,
        image_verification_receipt_sha256="2" * 64,
    )
    approval = AuthenticatedKnownBenignApproval(
        approval_id=uuid4(),
        agent_id=control.agent_id,
        attempt_id=control.attempt_id,
        artifact_sha256=control.artifact_sha256,
        image_sha256=control.image_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        approved_at=now + timedelta(minutes=1),
        approver_id="operator",
        approval_receipt_sha256="e" * 64,
        provenance_status="two_person_authenticated",
        authenticated_reviewers=2,
        review_evidence_sha256="9" * 64,
        provenance_review_evidence_sha256="9" * 64,
        provenance_receipt_sha256="f" * 64,
        completed_at=now + timedelta(minutes=2),
    )
    group = SeedGenerationGroup(
        group_id=uuid4(),
        target_agent_id=target.agent_id,
        target_attempt_id=target.attempt_id,
        target_artifact_sha256=target.artifact_sha256,
        target_image_sha256=target.image_sha256,
        control_agent_id=control.agent_id,
        control_attempt_id=control.attempt_id,
        control_artifact_sha256=control.artifact_sha256,
        control_image_sha256=control.image_sha256,
        approval_id=approval.approval_id,
        approval_receipt_sha256=approval.approval_receipt_sha256,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        started_at=now + timedelta(minutes=3),
        target_receipt_sha256="0" * 64,
        control_receipt_sha256="0" * 64,
    )
    group = group.model_copy(
        update={
            "target_receipt_sha256": compute_v13_generation_role_digest(
                group, "target"
            ),
            "control_receipt_sha256": compute_v13_generation_role_digest(
                group, "known_benign"
            ),
        }
    )
    return Registry(group, target, control, approval), Store(now + timedelta(minutes=4))


def test_group_issues_one_shared_two_seed_bundle_once() -> None:
    registry, store = fixture()
    first = asyncio.run(
        issue_v13_hidden_group_seeds(
            group_id=registry.group.group_id, registry=registry, store=store
        )
    )
    second = asyncio.run(
        issue_v13_hidden_group_seeds(
            group_id=registry.group.group_id, registry=registry, store=store
        )
    )
    assert first == second
    assert store.create_calls == 1
    assert len(set(first.seed_commitments)) == 2
    assert first.target_receipt_sha256 == registry.group.target_receipt_sha256
    assert first.control_receipt_sha256 == registry.group.control_receipt_sha256
    assert "seeds_hex" not in first.model_dump_json()
    assert "seed" not in repr(store.records[registry.group.group_id])
    sealed = json.loads(store.records[registry.group.group_id].sealed_bytes)
    assert len(sealed["seeds_hex"]) == 2


@pytest.mark.parametrize("role", ["target", "known_benign"])
@pytest.mark.parametrize("fault", ["base_type", "wrong_policy", "missing_receipt"])
def test_unverified_role_never_issues_seed(role: str, fault: str) -> None:
    registry, store = fixture()
    current = registry.roles[role]
    if fault == "base_type":
        registry.roles[role] = ArtifactCommitment.model_validate(
            current.model_dump(mode="python")
        )
    elif fault == "wrong_policy":
        registry.roles[role] = current.model_copy(update={"policy_version": 12})
    else:
        registry.roles[role] = current.model_copy(
            update={"image_verification_receipt_sha256": None}
        )
    with pytest.raises(V13SeedIssuanceUnavailable):
        asyncio.run(
            issue_v13_hidden_group_seeds(
                group_id=registry.group.group_id, registry=registry, store=store
            )
        )
    assert store.create_calls == 0


@pytest.mark.parametrize(
    "fault",
    [
        "approval_receipt",
        "approval_identity",
        "approval_evidence",
        "approval_status",
        "approval_reviewers",
        "late_attestation",
        "target_image",
        "group_receipt",
        "group_clock",
        "profile",
    ],
)
def test_prerequisite_fault_never_calls_seed_store(fault: str) -> None:
    registry, store = fixture()
    if fault == "approval_receipt":
        registry.approval = registry.approval.model_copy(
            update={"approval_receipt_sha256": "0" * 64}
        )
    elif fault == "approval_identity":
        registry.approval = registry.approval.model_copy(update={"agent_id": uuid4()})
    elif fault == "approval_evidence":
        registry.approval = registry.approval.model_copy(
            update={"provenance_review_evidence_sha256": "0" * 64}
        )
    elif fault == "approval_status":
        registry.approval = registry.approval.model_copy(
            update={"provenance_status": "recorded_unverified"}
        )
    elif fault == "approval_reviewers":
        registry.approval = registry.approval.model_copy(
            update={"authenticated_reviewers": 1}
        )
    elif fault == "late_attestation":
        registry.approval = registry.approval.model_copy(
            update={"completed_at": registry.group.started_at + timedelta(seconds=1)}
        )
    elif fault == "target_image":
        registry.roles["target"] = registry.roles["target"].model_copy(
            update={"image_sha256": "0" * 64}
        )
    elif fault == "group_receipt":
        registry.group = registry.group.model_copy(
            update={"target_receipt_sha256": "0" * 64}
        )
    elif fault == "group_clock":
        registry.group = registry.group.model_copy(
            update={"started_at": registry.roles["target"].committed_at}
        )
    elif fault == "profile":
        registry.group = registry.group.model_copy(update={"profile_sha256": "0" * 64})
    with pytest.raises(V13SeedIssuanceUnavailable):
        asyncio.run(
            issue_v13_hidden_group_seeds(
                group_id=registry.group.group_id, registry=registry, store=store
            )
        )
    assert store.create_calls == 0


def test_existing_seed_bundle_cannot_be_spliced_into_another_group() -> None:
    first_registry, store = fixture()
    asyncio.run(
        issue_v13_hidden_group_seeds(
            group_id=first_registry.group.group_id, registry=first_registry, store=store
        )
    )
    second_registry, _ = fixture()
    store.records[second_registry.group.group_id] = store.records[
        first_registry.group.group_id
    ]
    with pytest.raises(V13SeedIssuanceUnavailable):
        asyncio.run(
            issue_v13_hidden_group_seeds(
                group_id=second_registry.group.group_id,
                registry=second_registry,
                store=store,
            )
        )
    assert store.create_calls == 1


def test_sealed_store_must_commit_after_group_start() -> None:
    registry, store = fixture()
    store.committed_at = registry.group.started_at
    with pytest.raises(V13SeedIssuanceUnavailable):
        asyncio.run(
            issue_v13_hidden_group_seeds(
                group_id=registry.group.group_id, registry=registry, store=store
            )
        )


def test_registry_failure_does_not_generate_seed(monkeypatch) -> None:
    registry, store = fixture()

    async def fail(_group_id: UUID) -> SeedGenerationGroup:
        raise RuntimeError("private approval response with secret")

    monkeypatch.setattr(registry, "get_verified_group", fail)
    with pytest.raises(V13SeedIssuanceUnavailable) as raised:
        asyncio.run(
            issue_v13_hidden_group_seeds(
                group_id=registry.group.group_id, registry=registry, store=store
            )
        )
    assert "secret" not in str(raised.value)
    assert store.create_calls == 0


def test_matched_packages_require_issued_seeds_and_same_ordered_inventory() -> None:
    registry, seed_store = fixture()
    issued = asyncio.run(
        issue_v13_hidden_group_seeds(
            group_id=registry.group.group_id, registry=registry, store=seed_store
        )
    )
    package_store = PackageStore()
    register_matched_packages(registry, package_store, issued.seed_commitments)
    generator = GeneratorProvenance(registry, package_store, issued)
    matched = asyncio.run(
        verify_v13_issued_matched_inventory(
            issuance=issued,
            target=registry.roles["target"],
            control=registry.roles["known_benign"],
            seed_store=seed_store,
            package_store=package_store,
            packages=registry,
            controls=registry,
            generations=registry,
            generator_provenance=generator,
        )
    )
    assert matched.group_id == issued.group_id
    assert matched.pair_count == 60
    assert (
        matched.target_generator_receipt_sha256
        == generator.receipts["target"].derivation_attestation_sha256
    )

    # An internally matched pair package generated from different randomness
    # still cannot claim this group's previously issued seeds.
    other = PackageStore()
    register_matched_packages(registry, other, ("1" * 64, "2" * 64))
    with pytest.raises(V13SeedIssuanceUnavailable):
        asyncio.run(
            verify_v13_issued_matched_inventory(
                issuance=issued,
                target=registry.roles["target"],
                control=registry.roles["known_benign"],
                seed_store=seed_store,
                package_store=other,
                packages=registry,
                controls=registry,
                generations=registry,
                generator_provenance=generator,
            )
        )


@pytest.mark.parametrize(
    "fault",
    [
        "preissued_manifest",
        "preissued_registration",
        "missing_receipt",
        "wrong_bundle",
        "wrong_payloads",
        "wrong_role",
        "wrong_revision",
        "late_attestation",
        "attestation_before_generation",
        "missing_attestation_time",
    ],
)
def test_generator_provenance_fails_closed(fault: str) -> None:
    registry, seed_store = fixture()
    issued = asyncio.run(
        issue_v13_hidden_group_seeds(
            group_id=registry.group.group_id, registry=registry, store=seed_store
        )
    )
    package_store = PackageStore()
    register_matched_packages(registry, package_store, issued.seed_commitments)
    if fault in {"preissued_manifest", "preissued_registration"}:
        # Preserve the valid group-start ordering enforced by #2177 while
        # moving a target package to *before* the later sealed seed commit.
        key = (issued.group_id, "target")
        registration = registry.packages[key]
        manifest = V13PrivateManifest.model_validate_json(
            package_store.manifests[registration.manifest_sha256]
        )
        old_time = issued.committed_at - timedelta(seconds=20)
        manifest = manifest.model_copy(update={"generated_at": old_time})
        raw = json.dumps(
            manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(raw).hexdigest()
        package_store.manifests[digest] = raw
        updates = {"manifest_sha256": digest}
        if fault == "preissued_registration":
            updates["registered_at"] = issued.committed_at - timedelta(seconds=10)
        registry.packages[key] = registration.model_copy(update=updates)
        # The older matched-control preflight accepts this valid group-start
        # order. The new issuer boundary must reject its pre-seed generation.
        asyncio.run(
            prepare_v13_matched_clean_control(
                group_id=issued.group_id,
                target=registry.roles["target"],
                control=registry.roles["known_benign"],
                store=package_store,
                packages=registry,
                controls=registry,
                generations=registry,
            )
        )
    generator = GeneratorProvenance(registry, package_store, issued)
    target = generator.receipts["target"]
    if fault == "missing_receipt":
        generator.receipts["target"] = None
    elif fault == "wrong_bundle":
        generator.receipts["target"] = target.model_copy(
            update={"sealed_bundle_sha256": "0" * 64}
        )
    elif fault == "wrong_payloads":
        generator.receipts["target"] = target.model_copy(
            update={"ordered_payload_digests_sha256": "0" * 64}
        )
    elif fault == "wrong_role":
        generator.receipts["target"] = target.model_copy(
            update={"role": "known_benign"}
        )
    elif fault == "wrong_revision":
        generator.receipts["target"] = target.model_copy(
            update={"generator_revision": ""}
        )
    elif fault == "late_attestation":
        registration = registry.packages[(issued.group_id, "target")]
        generator.receipts["target"] = target.model_copy(
            update={"attested_at": registration.registered_at + timedelta(seconds=1)}
        )
    elif fault == "attestation_before_generation":
        generator.receipts["target"] = target.model_copy(
            update={"attested_at": target.generated_at - timedelta(seconds=1)}
        )
    elif fault == "missing_attestation_time":
        generator.receipts["target"] = target.model_copy(update={"attested_at": None})
    with pytest.raises(V13SeedIssuanceUnavailable):
        asyncio.run(
            verify_v13_issued_matched_inventory(
                issuance=issued,
                target=registry.roles["target"],
                control=registry.roles["known_benign"],
                seed_store=seed_store,
                package_store=package_store,
                packages=registry,
                controls=registry,
                generations=registry,
                generator_provenance=generator,
            )
        )
