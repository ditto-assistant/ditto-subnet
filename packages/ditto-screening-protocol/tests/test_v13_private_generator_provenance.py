"""Synthetic sealed-generator proof tests; no production case bank or key."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ditto_screening_protocol.v13_private_clean_control import (
    compute_v13_generation_role_digest,
)
from ditto_screening_protocol.v13_private_generator_provenance import (
    _SIGNATURE_DOMAIN,
    ApprovedBlueprintBank,
    CommittedGeneratorAttestation,
    GeneratorProvenanceUnavailable,
    ReplayedPrivateGeneratorProvenance,
    SignedGeneratorAttestation,
    _blueprint_bank_sha256,
    _canonical_body,
    _derived_pairs,
)
from ditto_screening_protocol.v13_private_package import (
    V13_PRIVATE_PROFILE_SHA256,
    V13PrivateManifest,
)
from ditto_screening_protocol.v13_private_provision import PrivateBlueprintPair
from ditto_screening_protocol.v13_private_seed import (
    SealedSeedRecord,
    SeedGenerationGroup,
    StoredSeedBundle,
    _ordered_payload_digests_sha256,
)

_KEY = b"synthetic-test-only-private-generator-key-32-bytes"
_SEEDS = (bytes.fromhex("01" * 32), bytes.fromhex("02" * 32))


class Registry:
    def __init__(self, group: SeedGenerationGroup) -> None:
        self.group = group

    async def get_verified_group(self, group_id: UUID) -> SeedGenerationGroup:
        assert group_id == self.group.group_id
        return self.group


class SeedStore:
    def __init__(self, record: SealedSeedRecord) -> None:
        self.record = record

    async def read(self, _group_id: UUID) -> SealedSeedRecord:
        return self.record


class PackageStore:
    def __init__(self, manifests: dict[str, bytes]) -> None:
        self.manifests = manifests

    async def read_manifest(self, sha256: str) -> bytes:
        return self.manifests[sha256]


class Bank:
    def __init__(self, blueprints: tuple[PrivateBlueprintPair, ...]) -> None:
        self.blueprints = blueprints

    async def load(self) -> tuple[PrivateBlueprintPair, ...]:
        return self.blueprints


class BankApprovals:
    def __init__(self, approval: ApprovedBlueprintBank) -> None:
        self.approval = approval

    async def get_verified_approval(self, _digest: str) -> ApprovedBlueprintBank:
        return self.approval


class Ledger:
    def __init__(self, records: dict[str, CommittedGeneratorAttestation]) -> None:
        self.records = records

    async def read(self, _group_id: UUID, role: str) -> CommittedGeneratorAttestation:
        return self.records[role]


def _raw(model: object) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def _signed(record: SignedGeneratorAttestation) -> bytes:
    signed = record.model_copy(
        update={
            "signature_sha256": hmac.new(
                _KEY, _SIGNATURE_DOMAIN + _canonical_body(record), hashlib.sha256
            ).hexdigest()
        }
    )
    return _raw(signed)


def fixture():
    started = datetime(2026, 9, 23, 12, tzinfo=UTC)
    group = SeedGenerationGroup(
        group_id=uuid4(),
        target_agent_id=uuid4(),
        target_attempt_id=uuid4(),
        target_artifact_sha256="a" * 64,
        target_image_sha256="b" * 64,
        control_agent_id=uuid4(),
        control_attempt_id=uuid4(),
        control_artifact_sha256="c" * 64,
        control_image_sha256="d" * 64,
        approval_id=uuid4(),
        approval_receipt_sha256="e" * 64,
        profile_sha256=V13_PRIVATE_PROFILE_SHA256,
        started_at=started,
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
    seed_commit = started + timedelta(seconds=10)
    bundle = StoredSeedBundle(
        group_id=group.group_id,
        profile_sha256=group.profile_sha256,
        target_receipt_sha256=group.target_receipt_sha256,
        control_receipt_sha256=group.control_receipt_sha256,
        approval_receipt_sha256=group.approval_receipt_sha256,
        provenance_receipt_sha256="f" * 64,
        seeds_hex=(_SEEDS[0].hex(), _SEEDS[1].hex()),
    )
    sealed = SealedSeedRecord(sealed_bytes=_raw(bundle), committed_at=seed_commit)
    blueprints = tuple(
        PrivateBlueprintPair(
            pair_id=uuid4(),
            transformation_class=class_name,
            control=f"{class_name}-{index}-control".encode(),
            variant=f"{class_name}-{index}-variant".encode(),
        )
        for class_name in (
            "field_entity_rename",
            "request_paraphrase",
            "record_reorder_decoy",
        )
        for index in range(20)
    )
    bank_approval = ApprovedBlueprintBank(
        bank_sha256=_blueprint_bank_sha256(blueprints),
        approval_receipt_sha256="9" * 64,
        status="independently_approved",
        approved_at=started - timedelta(seconds=1),
    )
    pairs = _derived_pairs(
        group_id=group.group_id,
        seeds=_SEEDS,
        blueprints=blueprints,
        tool_catalog_applicable=False,
    )
    generated = seed_commit + timedelta(seconds=10)
    manifests: dict[str, bytes] = {}
    records: dict[str, CommittedGeneratorAttestation] = {}
    for role, identity in (
        (
            "target",
            (
                group.target_agent_id,
                group.target_attempt_id,
                group.target_artifact_sha256,
                group.target_image_sha256,
            ),
        ),
        (
            "known_benign",
            (
                group.control_agent_id,
                group.control_attempt_id,
                group.control_artifact_sha256,
                group.control_image_sha256,
            ),
        ),
    ):
        manifest = V13PrivateManifest(
            agent_id=identity[0],
            attempt_id=identity[1],
            artifact_sha256=identity[2],
            image_sha256=identity[3],
            profile_sha256=group.profile_sha256,
            generated_at=generated,
            tool_catalog_applicable=False,
            pairs=pairs,
        )
        raw_manifest = _raw(manifest)
        digest = hashlib.sha256(raw_manifest).hexdigest()
        manifests[digest] = raw_manifest
        record = SignedGeneratorAttestation(
            revision="v13-private-generator-attestation-v1",
            group_id=group.group_id,
            role=role,
            sealed_bundle_sha256=hashlib.sha256(sealed.sealed_bytes).hexdigest(),
            blueprint_bank_sha256=_blueprint_bank_sha256(blueprints),
            blueprint_approval_receipt_sha256=(bank_approval.approval_receipt_sha256),
            generator_revision="v13-private-case-generator-v1",
            manifest_sha256=digest,
            ordered_payload_digests_sha256=_ordered_payload_digests_sha256(manifest),
            generated_at=generated,
            signature_sha256="0" * 64,
        )
        records[role] = CommittedGeneratorAttestation(
            signed_bytes=_signed(record), committed_at=generated + timedelta(seconds=1)
        )
    ledger = Ledger(records)
    bank = Bank(blueprints)
    bank_approvals = BankApprovals(bank_approval)
    packages = PackageStore(manifests)
    seeds = SeedStore(sealed)
    adapter = ReplayedPrivateGeneratorProvenance(
        signing_key=_KEY,
        ledger=ledger,
        seed_store=seeds,
        package_store=packages,
        bank=bank,
        bank_approvals=bank_approvals,
        generations=Registry(group),
    )
    return group, adapter, ledger, bank, bank_approvals, packages, seeds


def test_signed_ledger_and_rederived_bank_prove_both_roles() -> None:
    group, adapter, ledger, _, _, _, _ = fixture()
    for role in ("target", "known_benign"):
        receipt = asyncio.run(adapter.get_verified_derivation(group.group_id, role))
        assert receipt.role == role
        assert receipt.attested_at == ledger.records[role].committed_at
        assert (
            receipt.derivation_attestation_sha256
            == hashlib.sha256(ledger.records[role].signed_bytes).hexdigest()
        )


@pytest.mark.parametrize(
    "fault",
    [
        "signature",
        "role",
        "manifest",
        "ledger_time",
        "seed_time",
        "seed_bytes",
        "bank_drift",
        "bank_unapproved",
        "bank_approval_late",
        "bank_approval_receipt",
        "manifest_pairs",
    ],
)
def test_provenance_rejects_forgery_or_non_derived_inventory(fault: str) -> None:
    group, adapter, ledger, bank, approvals, packages, seeds = fixture()
    original = ledger.records["target"]
    record = SignedGeneratorAttestation.model_validate_json(original.signed_bytes)
    if fault == "signature":
        ledger.records["target"] = original.model_copy(
            update={
                "signed_bytes": _raw(
                    record.model_copy(update={"signature_sha256": "f" * 64})
                )
            }
        )
        # Isolate the MAC check: every signed field remains schema-valid.
        SignedGeneratorAttestation.model_validate_json(
            ledger.records["target"].signed_bytes
        )
    elif fault == "role":
        ledger.records["target"] = original.model_copy(
            update={
                "signed_bytes": _signed(
                    record.model_copy(update={"role": "known_benign"})
                )
            }
        )
    elif fault == "manifest":
        ledger.records["target"] = original.model_copy(
            update={
                "signed_bytes": _signed(
                    record.model_copy(update={"manifest_sha256": "0" * 64})
                )
            }
        )
    elif fault == "ledger_time":
        ledger.records["target"] = original.model_copy(
            update={"committed_at": record.generated_at - timedelta(seconds=1)}
        )
    elif fault == "seed_time":
        seeds.record = seeds.record.model_copy(
            update={"committed_at": record.generated_at + timedelta(seconds=1)}
        )
    elif fault == "seed_bytes":
        changed = json.loads(seeds.record.sealed_bytes)
        changed["seeds_hex"][0] = "03" * 32
        seeds.record = seeds.record.model_copy(
            update={
                "sealed_bytes": json.dumps(
                    changed, sort_keys=True, separators=(",", ":")
                ).encode()
            }
        )
    elif fault == "bank_drift":
        item = bank.blueprints[0]
        bank.blueprints = (
            item.model_copy(update={"control": b"drift"}),
        ) + bank.blueprints[1:]
    elif fault == "bank_unapproved":
        approvals.approval = approvals.approval.model_copy(
            update={"status": "recorded_unverified"}
        )
    elif fault == "bank_approval_late":
        approvals.approval = approvals.approval.model_copy(
            update={"approved_at": group.started_at + timedelta(seconds=1)}
        )
    elif fault == "bank_approval_receipt":
        approvals.approval = approvals.approval.model_copy(
            update={"approval_receipt_sha256": "0" * 64}
        )
    elif fault == "manifest_pairs":
        raw = packages.manifests[record.manifest_sha256]
        changed = json.loads(raw)
        changed["pairs"][0]["control_sha256"] = "0" * 64
        new_raw = json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
        new_digest = hashlib.sha256(new_raw).hexdigest()
        packages.manifests[new_digest] = new_raw
        changed_manifest = V13PrivateManifest.model_validate_json(new_raw)
        ledger.records["target"] = original.model_copy(
            update={
                "signed_bytes": _signed(
                    record.model_copy(
                        update={
                            "manifest_sha256": new_digest,
                            "ordered_payload_digests_sha256": (
                                _ordered_payload_digests_sha256(changed_manifest)
                            ),
                        }
                    )
                )
            }
        )
    with pytest.raises(GeneratorProvenanceUnavailable, match="private generator"):
        asyncio.run(adapter.get_verified_derivation(group.group_id, "target"))


def test_generator_key_is_explicit_and_not_a_default() -> None:
    group, _, ledger, bank, approvals, packages, seeds = fixture()
    with pytest.raises(GeneratorProvenanceUnavailable):
        ReplayedPrivateGeneratorProvenance(
            signing_key=b"",
            ledger=ledger,
            seed_store=seeds,
            package_store=packages,
            bank=bank,
            bank_approvals=approvals,
            generations=Registry(group),
        )
